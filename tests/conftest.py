# -*- coding: utf-8 -*-
"""A programmable fake pyodbc session.

The engine drives every step through pyodbc: it builds a batch, executes it,
reads the sentinel capture result set, and updates its variable environment.
None of that needs a real warehouse if we simulate the driver protocol —
which is what FakeSession does, so the whole engine (batch building, state
capture, error routing, CATCH emulation, child collection, offload, watches,
breakpoints) becomes testable and mutation-testable offline.

A test scripts the SERVER side: for each capture-execute the debugger issues,
the next queued turn decides the outcome — success (with variable updates,
rowcount, intermediate result sets, watch/condition values) or an error.
"""
import re

import pytest


class FakeError(Exception):
    """Stands in for a pyodbc driver error. Its str() mimics the [SQL Server] shape."""

    def __init__(self, message, number=50000):
        wrapped = (f"('42000', \"[42000] [Microsoft][ODBC Driver 18 for SQL Server]"
                   f"[SQL Server]{message} ({number}) (SQLExecDirectW)\")")
        super().__init__(wrapped)


class _Turn:
    def __init__(self, updates=None, rowcount=1, resultsets=None,
                 watches=None, cond=None, error=None):
        self.updates = updates or {}
        self.rowcount = rowcount
        self.resultsets = resultsets or []      # list of (columns, rows)
        self.watches = watches or {}
        self.cond = cond
        self.error = error


class FakeSession:
    """Server-side simulation shared by a FakeConnection + FakeCursor."""

    def __init__(self):
        self.vars = {}              # current server value per capture alias (UPPER, @-prefixed)
        self._turns = []            # queued outcomes for capture-executes
        self.object_defs = {}       # proc name -> source (for OBJECT_DEFINITION)
        self.adhoc = []             # (needle, columns, rows) for sql() queries
        self.executed = []          # every batch text, for assertions
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self.lock_timeout = None

    # -- test-facing scripting ---------------------------------------------
    def turn(self, **kw):
        """Queue one successful capture-execute outcome."""
        self._turns.append(_Turn(**kw))
        return self

    def fail(self, message, number=50000):
        """Queue one capture-execute that raises."""
        self._turns.append(_Turn(error=FakeError(message, number)))
        return self

    def define(self, proc_name, source):
        self.object_defs[proc_name.upper()] = source
        return self

    def next_turn(self):
        return self._turns.pop(0) if self._turns else _Turn()


class FakeCursor:
    def __init__(self, session):
        self._s = session
        self.description = None
        self._rows = []
        self._more = []          # queued extra result sets (list of (description, rows))
        self._input_sizes = None

    def setinputsizes(self, sizes):
        self._input_sizes = sizes

    def cancel(self):
        pass

    def close(self):
        pass

    def execute(self, sql, params=None):
        self._s.executed.append(sql)
        self.description = None
        self._rows = []
        self._more = []
        if "'__hcap__'" in sql:
            return self._capture(sql, params)
        low = sql.lower()
        if "object_definition" in low:
            name = (params[0] if params else "").upper()
            src = self._s.object_defs.get(name)
            self.description = [("def",)]
            self._rows = [(src,)] if src is not None else [(None,)]
            return self
        if "#tsqldbg_state" in sql or "create table" in low:
            return self                      # offload DDL/DML: no result set
        # ad-hoc sql(): match a registered needle
        for needle, columns, rows in self._s.adhoc:
            if needle.lower() in low:
                self.description = [(c,) for c in columns]
                self._rows = list(rows)
                return self
        self.description = None              # unknown read: empty
        return self

    def _capture(self, sql, params):
        turn = self._s.next_turn()
        if turn.error is not None:
            raise turn.error
        # reflect the re-injection: `SELECT @a = ?, @b = ?;` restores the
        # server state to the values the engine carried in (so a variable the
        # step does not touch keeps its value, exactly like the real session)
        m = re.search(r"SELECT ((?:@\w+ = \?(?:, )?)+);", sql)
        if m and params:
            names = re.findall(r"(@\w+) = \?", m.group(1))
            for name, value in zip(names, params):
                self._s.vars[name.upper()] = value
        self._s.vars.update({k.upper(): v for k, v in turn.updates.items()})
        # intermediate result sets come BEFORE the sentinel, in order
        queued = []
        for columns, rows in turn.resultsets:
            queued.append(([(c,) for c in columns], [tuple(r) for r in rows]))
        # the sentinel capture row: parse the aliases the batch asks for
        aliases = re.findall(r"AS \[([^\]]+)\]", sql.split("'__hcap__'", 1)[1])
        row = []
        cap_desc = []
        for alias in aliases:
            cap_desc.append((alias,))
            if alias == "__hcap__":
                row.append("__hcap__")
            elif alias == "__rowcount__":
                row.append(turn.rowcount)
            elif alias == "__cond__":
                row.append(turn.cond if turn.cond is not None else 0)
            elif alias.startswith("__watch__"):
                row.append(turn.watches.get(alias[len("__watch__"):]))
            else:
                row.append(self._s.vars.get(alias.upper()))
        queued.append((cap_desc, [tuple(row)]))
        # present the first, queue the rest for nextset()
        self.description, self._rows = queued[0]
        self._more = queued[1:]
        return self

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchmany(self, n):
        out, self._rows = self._rows[:n], self._rows[n:]
        return out

    def fetchall(self):
        out, self._rows = self._rows, []
        return out

    def nextset(self):
        if not self._more:
            return False
        self.description, self._rows = self._more.pop(0)
        return True


class FakeConnection:
    def __init__(self, session):
        self._s = session
        self.timeout = None

    def cursor(self):
        return FakeCursor(self._s)

    def commit(self):
        self._s.committed = True

    def rollback(self):
        self._s.rolled_back = True

    def close(self):
        self._s.closed = True


@pytest.fixture
def fake_session(monkeypatch):
    """Install a FakeSession as the engine's connection factory.

    Returns the session so a test can script turns and inspect committed/
    rolled_back/executed. Every TSQLDebugger built in the test shares it.
    """
    import tsql_fabric_debugger.connection as conn_mod
    import tsql_fabric_debugger.engine as eng
    import tsql_fabric_debugger.runner as run_mod

    session = FakeSession()

    def _connect(server=None, database=None, autocommit=True, lock_timeout=None):
        session.lock_timeout = lock_timeout
        return FakeConnection(session)

    monkeypatch.setattr(eng, "connect", _connect)
    monkeypatch.setattr(run_mod, "connect", _connect)
    monkeypatch.setattr(conn_mod, "connect", _connect)
    return session
