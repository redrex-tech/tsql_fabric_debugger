# -*- coding: utf-8 -*-
"""Offline tests for the 0.2.0 productivity features."""
import datetime
import decimal

import pytest

from tsql_fabric_debugger.engine import TSQLDebugger, _decode_value, _encode_value
from tsql_fabric_debugger.runner import diff_logs

PROC = """
CREATE PROCEDURE dbo.p_f @n INT, @big NVARCHAR(MAX) = NULL, @out INT OUTPUT AS
BEGIN
    SET @out = @n;
END;
"""


def _dbg(**kw):
    return TSQLDebugger(sql_text=PROC, params={"@n": 1}, server="offline",
                        database="offline", echo=lambda *_: None, **kw)


def test_watch_registration_and_validation():
    dbg = _dbg()
    name = dbg.watch("(SELECT 1)")
    assert dbg._watches[name] == "(SELECT 1)"
    dbg.watch("@n + 1", "n_plus")
    with pytest.raises(ValueError):
        dbg.watch("((unbalanced")
    with pytest.raises(ValueError):
        dbg.watch("(SELECT 1)", "bad name!")
    batch, _ = dbg._build_batch("SET @out = 1", [])
    assert "AS [__watch__n_plus]" in batch
    dbg.unwatch("n_plus")
    batch, _ = dbg._build_batch("SET @out = 1", [])
    assert "n_plus" not in batch
    dbg.unwatch()
    assert dbg._watches == {}


def test_breakpoint_registry():
    dbg = _dbg()
    dbg.break_at(4)
    dbg.break_at(5, "@n = 2")
    assert dbg.breaks() == {4: None, 5: "@n = 2"}
    dbg.clear_breaks(4)
    assert dbg.breaks() == {5: "@n = 2"}
    dbg.clear_breaks()
    assert dbg.breaks() == {}


def test_state_encode_decode_round_trip():
    values = [None, True, 42, 1.5, "texto ção",
              datetime.datetime(2026, 9, 3, 12, 30, 45, 123456),
              datetime.date(2026, 9, 3), datetime.time(23, 59, 1),
              decimal.Decimal("123.4500"), b"\x00\xff\x10"]
    for v in values:
        assert _decode_value(_encode_value(v)) == v


def test_save_and_load_state(tmp_path):
    dbg = _dbg()
    dbg._env["@OUT"] = 7
    dbg._env["@BIG"] = "x" * 10
    path = tmp_path / "state.json"
    dbg.save_state(str(path))

    dbg2 = _dbg()
    warnings = []
    dbg2._echo = warnings.append
    dbg2.load_state(str(path))
    assert dbg2._env["@OUT"] == 7 and dbg2._env["@BIG"] == "x" * 10

    # unknown variable is skipped with a warning, not applied
    payload = dbg.save_state()
    payload["vars"]["@GHOST"] = 1
    dbg2.load_state(payload)
    assert "@GHOST" not in dbg2._env or dbg2._env.get("@GHOST") != 1
    assert any("unknown variable" in w for w in warnings)


def test_reset_restores_pristine_plan_and_env():
    dbg = _dbg()
    dbg._env["@OUT"] = 99
    dbg._steps.append({"kind": "stmt", "text": "fake", "line": 1,
                       "catch_id": None, "catch_ids": (), "try": False,
                       "depth": 1, "s": 0, "e": 0, "ti": (0, 0)})
    dbg._pos = 1
    dbg._finished = True
    dbg._log.append({"step": 1, "status": "SUCCESS"})
    dbg.reset()
    assert len(dbg._steps) == len(dbg._initial_steps)
    assert dbg._env["@OUT"] is None and dbg._env["@N"] == 1
    assert dbg._pos == 0 and not dbg._finished and dbg._log == []


def test_offload_partitioning_in_batch():
    dbg = _dbg(offload_threshold=100)
    dbg._env["@BIG"] = "y" * 500          # above threshold -> hydrated, not bound
    batch, values = dbg._build_batch("SET @out = 1", [])
    assert "#tsqldbg_state" in batch and f"N'{dbg._state_ns}:@BIG'" in batch
    assert all(v != dbg._env["@BIG"] for v in values)
    # below threshold stays a plain parameter
    dbg._env["@BIG"] = "y" * 50
    batch, values = dbg._build_batch("SET @out = 1", [])
    assert "#tsqldbg_state" not in batch
    assert dbg._env["@BIG"] in values


def test_history_batches_prunes_old_success_payloads():
    dbg = _dbg(history_batches=2)
    import time as _t
    for n in range(5):
        dbg._record("stmt", n + 1, "SUCCESS", f"cmd {n}", _t.time(), {}, None,
                    batch=f"batch {n}")
    dbg._record("stmt", 9, "ERROR", "bad", _t.time(), {}, "boom", batch="err batch")
    assert dbg._details[1]["batch"] is None          # pruned
    assert dbg._details[6]["batch"] == "err batch"   # errors always kept
    assert dbg._details[5]["batch"] == "batch 4"     # recent kept


def test_diff_logs_alignment_and_divergence():
    a = [{"line": 1, "kind": "stmt", "command": "SET @x = 1", "status": "SUCCESS",
          "rows_affected": 1, "changed_vars": "@x=1", "duration_s": 0.1, "error": None},
         {"line": 2, "kind": "stmt", "command": "UPDATE t", "status": "SUCCESS",
          "rows_affected": 10, "changed_vars": None, "duration_s": 0.2, "error": None},
         {"line": 3, "kind": "stmt", "command": "only in a", "status": "SUCCESS",
          "rows_affected": 0, "changed_vars": None, "duration_s": 0.1, "error": None}]
    b = [dict(a[0]),
         dict(a[1], rows_affected=99),
         {"line": 9, "kind": "stmt", "command": "only in b", "status": "ERROR",
          "rows_affected": None, "changed_vars": None, "duration_s": 0.1, "error": "x"}]
    result = diff_logs(a, b)
    rows = result.to_dict("records") if hasattr(result, "to_dict") else result
    changes = {r["change"] for r in rows}
    assert changes == {"diverged", "only_in_a", "only_in_b"}
    diverged = next(r for r in rows if r["change"] == "diverged")
    assert diverged["rows_a"] == 10 and diverged["rows_b"] == 99


# ---------------------------------------------------------------------------
# nested EXEC step-into (offline paths)
# ---------------------------------------------------------------------------
from tsql_fabric_debugger.parser import parse_exec_call


@pytest.mark.parametrize("text, expected_proc", [
    ("EXEC dbo.child @a = 1, @b = @x OUTPUT", "dbo.child"),
    ("EXECUTE [sch].[proc] 5, N'oi', @v OUT", "[sch].[proc]"),
    ("EXEC @r = dbo.f", "dbo.f"),
])
def test_parse_exec_call_supported(text, expected_proc):
    call = parse_exec_call(text)
    assert call is not None and call["proc"] == expected_proc


@pytest.mark.parametrize("text", [
    "EXEC (@sql)",
    "EXEC sp_executesql @sql",
    "SELECT 1",
    "INSERT INTO t EXEC dbo.p",
])
def test_parse_exec_call_unsupported(text):
    assert parse_exec_call(text) is None


def test_map_exec_args_positional_named_and_output():
    dbg = _dbg()
    dbg._env["@OUT"] = 7
    child_src = ("CREATE PROCEDURE dbo.c @p1 INT, @p2 NVARCHAR(10) = N'd', "
                 "@p3 INT OUTPUT AS BEGIN SET @p3 = @p1; END")
    call = parse_exec_call("EXEC dbo.c 5, @p3 = @out OUTPUT")
    params, outputs = dbg._map_exec_args(call, child_src)
    assert params == {"@p1": 5, "@p3": 7}
    assert outputs == [("@P3", "@OUT")]

    with pytest.raises(ValueError, match="does not exist"):
        dbg._map_exec_args(parse_exec_call("EXEC dbo.c @nope = 1"), child_src)
    with pytest.raises(ValueError, match="OUTPUT argument"):
        dbg._map_exec_args(parse_exec_call("EXEC dbo.c 1, N'x', 9 OUTPUT"), child_src)
    with pytest.raises(ValueError, match="not a literal"):
        dbg._map_exec_args(parse_exec_call("EXEC dbo.c GETDATE()"), child_src)


def test_child_never_closes_a_borrowed_session():
    class FakeConn:
        closed = False
        def rollback(self): pass
        def close(self): self.closed = True

    child = _dbg()
    fake = FakeConn()
    child._conn = fake
    child._cursor = object()
    child._owns_connection = False
    child.close()
    assert not fake.closed and child._conn is None


def test_lock_timeout_issues_the_set_on_connect(monkeypatch):
    import tsql_fabric_debugger.connection as conn_mod

    executed = []

    class FakeCursor:
        def execute(self, sql, *a): executed.append(sql)
        def close(self): pass

    class FakeConn:
        def cursor(self): return FakeCursor()

    monkeypatch.setattr(conn_mod, "_get_token", lambda: "tok")
    import pyodbc
    monkeypatch.setattr(pyodbc, "connect", lambda *a, **k: FakeConn())
    conn_mod.connect(server="s", database="d", lock_timeout=7)
    assert executed == ["SET LOCK_TIMEOUT 7000;"]     # seconds -> ms
    executed.clear()
    conn_mod.connect(server="s", database="d")        # default: no SET
    assert executed == []
