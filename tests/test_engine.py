# -*- coding: utf-8 -*-
"""Offline engine tests: batch building and environment — no connection."""
from pathlib import Path

import pytest

from tsql_fabric_debugger.engine import TSQLDebugger

FIXTURE = (Path(__file__).parent / "fixtures" / "demo_proc.sql").read_text(encoding="utf-8")


def _debugger(**kwargs):
    # fake server/database: the connection is lazy, nothing connects here
    return TSQLDebugger(sql_text=FIXTURE, params={"@n": 1},
                        server="offline", database="offline",
                        echo=lambda *_: None, **kwargs)


def test_test_params_and_defaults():
    dbg = _debugger()
    assert dbg.proc_name == "dbo.p_demo"
    assert dbg._env["@N"] == 1
    assert dbg._env["@LABEL"] == "demo"        # default N'demo' evaluated in Python
    assert dbg._env["@RESULT"] is None          # OUTPUT starts NULL


def test_batch_declares_injects_and_captures():
    dbg = _debugger()
    batch, values = dbg._build_batch("SET @total = 1", [])
    declare, inject, stmt, capture = batch.split("\n", 3)
    assert declare.startswith("DECLARE @n INT")
    assert "@bonus INT" in declare              # DECLARE from inside the IF is registered
    assert inject == "SELECT @n = ?, @label = ?;"
    assert values == [1, "demo"]
    assert "'__hcap__'" in capture and "@@ROWCOUNT AS [__rowcount__]" in capture


def test_block_internal_declare_is_excluded():
    dbg = _debugger()
    batch, _ = dbg._build_batch("IF @n > 0 BEGIN DECLARE @bonus INT = 100; END",
                                [], exclude=frozenset({"@BONUS"}))
    declare = batch.split("\n", 1)[0]
    assert "@bonus" not in declare              # no duplicate declaration for the block
    assert "@BONUS" in batch.rsplit("SELECT", 1)[1]   # but its value is captured


def test_set_var_validates_the_name():
    dbg = _debugger()
    dbg.set_var("@n", 2015)
    assert dbg._env["@N"] == 2015
    with pytest.raises(ValueError):
        dbg.set_var("@does_not_exist", 1)


def test_jump_to_moves_cursor_without_running():
    dbg = _debugger()
    dbg.jump_to(5)
    assert dbg._pos == 4
    assert dbg._log == []                       # nothing executed


def test_invalid_log_level():
    dbg = _debugger()
    with pytest.raises(ValueError):
        dbg.set_log_level("verbose")


# ---------------------------------------------------------------------------
# MUST fixes for 0.1.x (gap analysis 2026-09-03)
# ---------------------------------------------------------------------------
PROC_RETURN = """
CREATE PROCEDURE dbo.p_ret @n INT, @done INT OUTPUT AS
BEGIN
    SET @done = 0;
    IF @n = 0 RETURN;
    RETURN;
END;
"""

PROC_2TRY = """
CREATE PROCEDURE dbo.p_2try @a INT AS
BEGIN
    BEGIN TRY
        SET @a = 1;
        SET @a = 2;
    END TRY
    BEGIN CATCH
        SET @a = -1;
    END CATCH
    BEGIN TRY
        SET @a = 3;
    END TRY
    BEGIN CATCH
        SET @a = -2;
    END CATCH
END;
"""

PROC_TABLEVAR = """
CREATE PROCEDURE dbo.p_tv @q INT OUTPUT AS
BEGIN
    DECLARE @t TABLE (a INT);
    SET @q = 0;
END;
"""

PROC_COMMIT = """
CREATE PROCEDURE dbo.p_tx AS
BEGIN
    BEGIN TRAN;
    UPDATE dbo.x SET a = 1;
    COMMIT;
END;
"""


def test_return_becomes_its_own_step():
    dbg = TSQLDebugger(sql_text=PROC_RETURN, params={"@n": 1},
                       server="offline", database="offline", echo=lambda *_: None)
    kinds = [s["kind"] for s in dbg._steps]
    assert kinds == ["stmt", "if_block", "return"]


def test_each_try_gets_its_own_catch():
    dbg = TSQLDebugger(sql_text=PROC_2TRY, params={"@a": 0},
                       server="offline", database="offline", echo=lambda *_: None)
    assert len(dbg._catches) == 2
    assert [s["catch_id"] for s in dbg._steps] == [0, 0, 1]
    assert dbg._catches[0][0]["text"] == "SET @a = -1"
    assert dbg._catches[1][0]["text"] == "SET @a = -2"


def test_table_variable_declared_but_not_injected_or_captured():
    warnings = []
    dbg = TSQLDebugger(sql_text=PROC_TABLEVAR, params={},
                       server="offline", database="offline", echo=warnings.append)
    assert dbg._vars["@T"]["table"] is True
    assert any("table variable" in w for w in warnings)
    batch, values = dbg._build_batch("SET @q = 0", [])
    declare_line, rest = batch.split("\n", 1)
    assert "@t TABLE (a INT)" in declare_line          # declared (references compile)
    capture = rest.rsplit("SELECT", 1)[1]
    assert "[@T]" not in capture                       # not captured
    assert values == []                                # never re-injected


def test_inner_transaction_warning():
    warnings = []
    TSQLDebugger(sql_text=PROC_COMMIT, params={},
                 server="offline", database="offline", echo=warnings.append)
    warning = next(w for w in warnings if "its own transaction" in w)
    assert "COMMIT" in warning and "BEGIN TRAN" in warning and "line" in warning


def test_step_timeout_applied_to_connection(monkeypatch):
    import tsql_fabric_debugger.engine as eng

    class FakeConn:
        timeout = None
        def cursor(self):
            return object()

    fake = FakeConn()
    monkeypatch.setattr(eng, "connect", lambda *a, **k: fake)
    dbg = TSQLDebugger(sql_text=PROC_TABLEVAR, params={}, step_timeout=30,
                       server="offline", database="offline", echo=lambda *_: None)
    dbg._ensure_connection()
    assert fake.timeout == 30


# ---------------------------------------------------------------------------
# fixes from the pre-publish four-lens review
# ---------------------------------------------------------------------------
PROC_NESTED_TRY = """
CREATE PROCEDURE dbo.p_nested @a INT AS
BEGIN
    BEGIN TRY
        SET @a = 1;
        BEGIN TRY
            SET @a = 2;
        END TRY
        BEGIN CATCH
            SET @a = -2;
        END CATCH
        SET @a = 3;
    END TRY
    BEGIN CATCH
        SET @a = -1;
    END CATCH
END;
"""


def test_nested_try_catch_stack_on_steps():
    dbg = TSQLDebugger(sql_text=PROC_NESTED_TRY, params={"@a": 0},
                       server="offline", database="offline", echo=lambda *_: None)
    assert [s["catch_ids"] for s in dbg._steps] == [(0,), (0, 1), (0,)]
    assert len(dbg._catches) == 2


def test_requires_sql_file_text_or_proc_name():
    with pytest.raises(ValueError, match="sql_file, sql_text or proc_name"):
        TSQLDebugger(server="offline", database="offline", echo=lambda *_: None)


def test_set_var_rejects_table_variable():
    dbg = TSQLDebugger(sql_text=PROC_TABLEVAR, params={},
                       server="offline", database="offline", echo=lambda *_: None)
    with pytest.raises(ValueError, match="table variable"):
        dbg.set_var("@t", [1, 2])


def test_autocommit_mode_warns():
    warnings = []
    TSQLDebugger(sql_text=PROC_TABLEVAR, params={}, autocommit=True,
                 server="offline", database="offline", echo=warnings.append)
    assert any("autocommit=True" in w and "persists immediately" in w for w in warnings)


def test_exec_calls_trigger_opaque_notice():
    warnings = []
    sql = ("CREATE PROCEDURE dbo.p_exec AS BEGIN "
           "EXEC dbo.child 1; SET @x = 1; END;")
    TSQLDebugger(sql_text="CREATE PROCEDURE dbo.p AS BEGIN DECLARE @x INT; EXEC dbo.child; END;",
                 params={}, server="offline", database="offline", echo=warnings.append)
    assert any("EXEC call(s)" in w for w in warnings)


def test_dynamic_sql_commit_triggers_transaction_warning():
    warnings = []
    sql = ("CREATE PROCEDURE dbo.p_dyn AS BEGIN "
           "DECLARE @s NVARCHAR(100) = N'UPDATE t SET a=1; COMMIT;'; EXEC(@s); END;")
    TSQLDebugger(sql_text=sql, params={},
                 server="offline", database="offline", echo=warnings.append)
    assert any("its own transaction" in w and "string literal" in w for w in warnings)


def test_context_manager_closes_and_rolls_back(monkeypatch):
    import tsql_fabric_debugger.engine as eng

    class FakeConn:
        timeout = None
        rolled = False
        closed = False
        def cursor(self):
            return object()
        def rollback(self):
            self.rolled = True
        def close(self):
            self.closed = True

    fake = FakeConn()
    monkeypatch.setattr(eng, "connect", lambda *a, **k: fake)
    with TSQLDebugger(sql_text=PROC_TABLEVAR, params={},
                      server="offline", database="offline", echo=lambda *_: None) as dbg:
        dbg._ensure_connection()
    assert fake.rolled and fake.closed
    assert dbg._conn is None


def test_connection_failure_is_fatal_not_swallowed(monkeypatch):
    import tsql_fabric_debugger.engine as eng

    def boom(*a, **k):
        raise RuntimeError("az login expired")

    monkeypatch.setattr(eng, "connect", boom)
    echoes = []
    dbg = TSQLDebugger(sql_text=PROC_TABLEVAR, params={},
                       server="offline", database="offline", echo=echoes.append)
    dbg.step()                                     # DECLARE @t TABLE: registered, no connection
    with pytest.raises(RuntimeError, match="az login expired"):
        dbg.step()                                 # SET @q = 0 needs the server
    assert any("[FATAL]" in e for e in echoes)     # never a silent finish
    assert not dbg._finished                       # not a fake clean finish


def test_session_set_detection():
    dbg = _debugger()
    assert dbg._is_session_set({"kind": "stmt", "text": "SET NOCOUNT ON"})
    assert dbg._is_session_set({"kind": "stmt", "text": "SET XACT_ABORT ON"})
    assert not dbg._is_session_set({"kind": "stmt", "text": "SET @total = 1"})
    assert not dbg._is_session_set({"kind": "if_block", "text": "SET NOCOUNT ON"})


def test_parse_sql_error_multi_message_and_number():
    from tsql_fabric_debugger.engine import _parse_sql_error
    raw = ("('42000', \"[42000] [Microsoft][ODBC Driver 18 for SQL Server]"
           "[SQL Server]boom (50000) (SQLExecDirectW)\")")
    msg, num = _parse_sql_error(raw)
    assert msg == "boom" and num == 50000


def test_fatal_failure_keeps_the_cursor_on_the_step(monkeypatch):
    import tsql_fabric_debugger.engine as eng

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(eng, "connect", boom)
    dbg = TSQLDebugger(sql_text=PROC_TABLEVAR, params={},
                       server="offline", database="offline", echo=lambda *_: None)
    dbg.step()                      # DECLARE @t TABLE: no connection needed
    pos_before = dbg._pos
    with pytest.raises(RuntimeError):
        dbg.step()
    assert dbg._pos == pos_before   # retry lands on the same step


def test_rollback_is_truthful_about_what_it_did():
    messages = []
    dbg = TSQLDebugger(sql_text=PROC_TABLEVAR, params={}, autocommit=True,
                       server="offline", database="offline", echo=messages.append)
    dbg.rollback()
    assert any("nothing to roll back" in m for m in messages)
    assert not any("data effects undone" in m for m in messages)

    messages.clear()
    dbg2 = TSQLDebugger(sql_text=PROC_TABLEVAR, params={},
                        server="offline", database="offline", echo=messages.append)
    dbg2.rollback()          # no session opened yet
    assert any("No open session" in m for m in messages)
    assert not any("data effects undone" in m for m in messages)
