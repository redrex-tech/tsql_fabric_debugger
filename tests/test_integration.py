# -*- coding: utf-8 -*-
"""Integration with a real Fabric Warehouse.

Runs only when the FABRIC_TSQL_SERVER and FABRIC_TSQL_DATABASE environment
variables are set (plus a valid `az login` outside Fabric):

    FABRIC_TSQL_SERVER=<endpoint> FABRIC_TSQL_DATABASE=<wh> pytest -m integration

Everything runs in a transaction with ROLLBACK — nothing persists.
"""
import os
from pathlib import Path

import pytest

from tsql_fabric_debugger.engine import TSQLDebugger

pytestmark = pytest.mark.integration

FIXTURE = (Path(__file__).parent / "fixtures" / "demo_proc.sql").read_text(encoding="utf-8")

if not (os.environ.get("FABRIC_TSQL_SERVER") and os.environ.get("FABRIC_TSQL_DATABASE")):
    pytest.skip("FABRIC_TSQL_SERVER/FABRIC_TSQL_DATABASE not set", allow_module_level=True)


def test_run_all_matches_tsql_semantics():
    # real T-SQL: @bonus (declared inside the IF) is valid for the rest of the batch
    # total = 100 (IF) + 1+2+3 (WHILE) + 100 (@bonus) = 206
    dbg = TSQLDebugger(sql_text=FIXTURE, params={"@n": 1}, echo=lambda *_: None)
    dbg.run_all()
    state = {k: v for k, v in
             ((dbg._vars[c]["name"], dbg._env[c]) for c in dbg._vars)}
    dbg.close()
    assert state["@total"] == 206
    assert state["@bonus"] == 100
    assert state["@result"] == 206


def test_step_into_expands_if_and_while():
    dbg = TSQLDebugger(sql_text=FIXTURE, params={"@n": 1}, echo=lambda *_: None)
    while not dbg._finished and dbg._pos < len(dbg._steps):
        dbg.step_into()
    conds = [e for e in dbg._log if e["kind"] == "cond"]
    total = dbg._env["@TOTAL"]
    dbg.close()
    assert total == 206
    assert len(conds) == 6  # IF <0, IF >0, WHILE x4 (3 true + 1 false)


def test_emulated_catch_with_error_message():
    dbg = TSQLDebugger(sql_text=FIXTURE, params={"@n": -5}, echo=lambda *_: None)
    dbg.run_all()
    err = dbg._env["@ERR"]
    result = dbg._env["@RESULT"]
    dbg.close()
    assert result == -1
    assert "p_demo: negative @n" in err


PROC_RETURN = """
CREATE PROCEDURE dbo.p_ret @n INT, @done INT OUTPUT AS
BEGIN
    SET @done = 0;
    IF @n = 0 RETURN;
    SET @done = 1;
END;
"""


def test_return_in_guard_clause_ends_the_debug():
    dbg = TSQLDebugger(sql_text=PROC_RETURN, params={"@n": 0}, echo=lambda *_: None)
    dbg.run_all()
    assert dbg._env["@DONE"] == 0          # SET @done = 1 never ran
    assert dbg._finished
    dbg.close()

    dbg = TSQLDebugger(sql_text=PROC_RETURN, params={"@n": 1}, echo=lambda *_: None)
    dbg.run_all()
    assert dbg._env["@DONE"] == 1          # no RETURN, runs to the end
    dbg.close()


PROC_2TRY = """
CREATE PROCEDURE dbo.p_2try @f1 INT OUTPUT, @f2 INT OUTPUT,
                            @c1 NVARCHAR(200) OUTPUT, @c2 NVARCHAR(200) OUTPUT AS
BEGIN
    BEGIN TRY
        RAISERROR(N'phase 1 failure', 16, 1);
        SET @f1 = 1;
    END TRY
    BEGIN CATCH
        SET @c1 = ERROR_MESSAGE();
    END CATCH
    BEGIN TRY
        SET @f2 = 1;
    END TRY
    BEGIN CATCH
        SET @c2 = ERROR_MESSAGE();
    END CATCH
END;
"""


def test_right_catch_and_continuation_after_end_catch():
    dbg = TSQLDebugger(sql_text=PROC_2TRY, params={}, echo=lambda *_: None)
    dbg.run_all()
    env = dbg._env
    dbg.close()
    assert env["@C1"] == "phase 1 failure"  # the CATCH of the right TRY
    assert env["@F1"] is None               # rest of TRY 1 skipped
    assert env["@F2"] == 1                  # execution continued into phase 2
    assert env["@C2"] is None               # CATCH 2 untouched


PROC_RESULTSET = """
CREATE PROCEDURE dbo.p_diag @q INT OUTPUT AS
BEGIN
    SELECT 1 AS a, N'diag' AS b;
    SET @q = 7;
END;
"""


def test_intermediate_result_set_is_captured():
    dbg = TSQLDebugger(sql_text=PROC_RESULTSET, params={}, echo=lambda *_: None)
    dbg.run_all()
    entry = dbg._log[0]                     # the diagnostic SELECT step
    results = dbg.last_results(entry["step"])
    dbg.close()
    assert entry["result_sets"] == 1
    rs = results[0]
    rows = rs.values.tolist() if hasattr(rs, "values") else rs["rows"]
    assert rows == [[1, "diag"]]
