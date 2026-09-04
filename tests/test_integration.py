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


PROC_NESTED = """
CREATE PROCEDURE dbo.p_nested @a INT OUTPUT, @b INT OUTPUT, @c INT OUTPUT,
                              @m NVARCHAR(200) OUTPUT AS
BEGIN
    BEGIN TRY
        RAISERROR(N'outer boom', 16, 1);
        BEGIN TRY
            SET @b = 1;
        END TRY
        BEGIN CATCH
            SET @m = N'inner';
        END CATCH
        SET @c = 1;
    END TRY
    BEGIN CATCH
        SET @a = 99;
    END CATCH
END;
"""


def test_nested_try_fully_skipped_after_outer_catch():
    dbg = TSQLDebugger(sql_text=PROC_NESTED, params={}, echo=lambda *_: None)
    dbg.run_all()
    env = dbg._env
    dbg.close()
    assert env["@A"] == 99          # outer CATCH ran
    assert env["@B"] is None        # nested TRY skipped (real T-SQL skips it too)
    assert env["@M"] is None        # nested CATCH never emulated
    assert env["@C"] is None        # rest of outer TRY skipped


PROC_CATCH_DECL = """
CREATE PROCEDURE dbo.p_catchdecl @out NVARCHAR(400) OUTPUT AS
BEGIN
    BEGIN TRY
        RAISERROR(N'boom', 16, 1);
    END TRY
    BEGIN CATCH
        DECLARE @msg NVARCHAR(200) = ERROR_MESSAGE();
        SET @out = CONCAT(@msg, N'|', ERROR_NUMBER(), N'|', ERROR_PROCEDURE());
    END CATCH
END;
"""


def test_declare_and_error_functions_inside_emulated_catch():
    dbg = TSQLDebugger(sql_text=PROC_CATCH_DECL, params={}, echo=lambda *_: None)
    dbg.run_all()
    out = dbg._env["@OUT"]
    dbg.close()
    assert out == "boom|50000|dbo.p_catchdecl"


PROC_ELSE = """
CREATE PROCEDURE dbo.p_else @n INT, @x NVARCHAR(10) OUTPUT AS
BEGIN
    IF @n = 1 SET @x = N'one' ELSE SET @x = N'other';
END;
"""


def test_step_into_runs_the_else_branch_without_semicolon():
    dbg = TSQLDebugger(sql_text=PROC_ELSE, params={"@n": 2}, echo=lambda *_: None)
    while not dbg._finished and dbg._pos < len(dbg._steps):
        dbg.step_into()
    x = dbg._env["@X"]
    dbg.close()
    assert x == "other"


PROC_BARE_TRY = """
CREATE PROCEDURE dbo.p_baretry @r INT OUTPUT AS
BEGIN TRY
    SET @r = 1;
    RAISERROR(N'x', 16, 1);
END TRY
BEGIN CATCH
    SET @r = -1;
END CATCH
"""


def test_body_starting_at_begin_try_keeps_the_catch():
    dbg = TSQLDebugger(sql_text=PROC_BARE_TRY, params={}, echo=lambda *_: None)
    dbg.run_all()
    r = dbg._env["@R"]
    dbg.close()
    assert r == -1


PROC_RETURN_IN_CATCH = """
CREATE PROCEDURE dbo.p_retc @r INT OUTPUT, @z INT OUTPUT AS
BEGIN
    BEGIN TRY
        RAISERROR(N'x', 16, 1);
    END TRY
    BEGIN CATCH
        SET @r = -1;
        RETURN;
    END CATCH
    SET @z = 1;
END;
"""


def test_return_inside_emulated_catch_ends_the_debug():
    dbg = TSQLDebugger(sql_text=PROC_RETURN_IN_CATCH, params={}, echo=lambda *_: None)
    dbg.run_all()
    env = dbg._env
    finished = dbg._finished
    dbg.close()
    assert env["@R"] == -1
    assert env["@Z"] is None        # code after END CATCH never ran
    assert finished


PROC_DEFAULT_THEN_FAIL = """
CREATE PROCEDURE dbo.p_deffail @d DATETIME2 = SYSUTCDATETIME(), @x INT OUTPUT AS
BEGIN
    SELECT @x = 1 / 0;
END;
"""


def test_error_entry_returned_even_after_pending_defaults():
    # the lazy first connection logs the defaults entry BEFORE the failing
    # statement — step() must still return the ERROR entry
    dbg = TSQLDebugger(sql_text=PROC_DEFAULT_THEN_FAIL, params={}, echo=lambda *_: None)
    entry = dbg.step()
    d = dbg._env["@D"]
    dbg.close()
    assert entry["status"] == "ERROR"
    assert "Divide by zero" in entry["error"]
    assert d is not None                       # the server-side default was applied


PROC_SUBSTEP_FAIL = """
CREATE PROCEDURE dbo.p_subfail @n INT, @a INT OUTPUT, @b INT OUTPUT,
                               @c NVARCHAR(200) OUTPUT, @z INT OUTPUT AS
BEGIN
    BEGIN TRY
        IF @n = 1
        BEGIN
            SET @a = 1;
            SELECT @a = 1 / 0;
            SET @b = 1;
        END;
        SET @z = 1;
    END TRY
    BEGIN CATCH
        SET @c = ERROR_MESSAGE();
    END CATCH
END;
"""


def test_error_in_expanded_substep_routes_through_the_catch():
    dbg = TSQLDebugger(sql_text=PROC_SUBSTEP_FAIL, params={"@n": 1}, echo=lambda *_: None)
    while not dbg._finished and dbg._pos < len(dbg._steps):
        dbg.step_into()
    env = dbg._env
    dbg.close()
    assert env["@A"] == 1                      # sub-step before the failure ran
    assert "Divide by zero" in (env["@C"] or "")   # the right CATCH was emulated
    assert env["@B"] is None                   # remaining sub-steps of the TRY skipped
    assert env["@Z"] is None                   # rest of the TRY skipped too


def test_run_step_with_catch_return_does_not_kill_the_session():
    # a THROW/RETURN inside the CATCH of an ISOLATED run_step must not set
    # _finished on the sequential debug
    dbg = TSQLDebugger(sql_text=PROC_RETURN_IN_CATCH, params={}, echo=lambda *_: None)
    failing = next(i for i, s in enumerate(dbg._steps, start=1)
                   if "RAISERROR" in s["text"])
    entry = dbg.run_step(failing, emulate_catch=True)
    assert entry["status"] == "ERROR"
    assert not dbg._finished                   # sequential session intact
    assert dbg._pos == 0                       # cursor untouched
    dbg.close()


PROC_LOOP = """
CREATE PROCEDURE dbo.p_loop @total INT OUTPUT, @i INT OUTPUT AS
BEGIN
    SET @i = 0;
    SET @total = 0;
    WHILE @i < 5
    BEGIN
        SET @i = @i + 1;
        SET @total = @total + @i;
    END;
END;
"""


def test_conditional_breakpoint_and_watch_in_a_loop():
    dbg = TSQLDebugger(sql_text=PROC_LOOP, params={}, echo=lambda *_: None)
    dbg.watch("@i * 100", "i_x100")
    dbg.break_at(8, "@i = 3")        # file line of "SET @i = @i + 1"
    dbg.run_all()                    # auto-expands the WHILE (breakpoint inside)
    assert not dbg._finished
    assert dbg._env["@I"] == 3       # stopped BEFORE the 4th increment
    assert dbg.watches()["i_x100"] == 300
    dbg.clear_breaks()
    dbg.run_all()
    total = dbg._env["@TOTAL"]
    dbg.close()
    assert total == 15               # loop completed correctly after resume


def test_while_step_list_is_pruned_between_iterations():
    dbg = TSQLDebugger(sql_text=PROC_LOOP, params={}, echo=lambda *_: None)
    dbg.run_until(2)
    sizes = []
    while not dbg._finished and dbg._pos < len(dbg._steps):
        dbg.step_into()
        sizes.append(len(dbg._steps))
    dbg.close()
    assert dbg._env["@TOTAL"] == 15
    assert max(sizes) <= len(dbg._initial_steps) + 4   # bounded, not growing per iteration


def test_save_state_load_state_and_jump_resume(tmp_path):
    dbg = TSQLDebugger(sql_text=PROC_LOOP, params={}, echo=lambda *_: None)
    dbg.run_until(2)                 # @i=0, @total=0 set
    dbg._env["@I"] = 4               # pretend we got far
    path = str(tmp_path / "st.json")
    dbg.save_state(path)
    dbg.close()

    dbg2 = TSQLDebugger(sql_text=PROC_LOOP, params={}, echo=lambda *_: None)
    dbg2.load_state(path)
    assert dbg2._env["@I"] == 4 and dbg2._env["@TOTAL"] == 0
    dbg2.jump_to(3)                  # straight to the WHILE
    dbg2.run_all()
    total = dbg2._env["@TOTAL"]
    dbg2.close()
    assert total == 5                # only iteration @i=5 ran: 0 + 5


def test_reset_replays_from_scratch_on_same_object():
    dbg = TSQLDebugger(sql_text=PROC_LOOP, params={}, echo=lambda *_: None)
    dbg.run_all()
    assert dbg._env["@TOTAL"] == 15
    dbg.reset()
    assert dbg._env["@TOTAL"] is None and dbg._log == []
    dbg.run_all()
    total = dbg._env["@TOTAL"]
    dbg.close()
    assert total == 15               # full replay on the same object


PROC_BIG = """
CREATE PROCEDURE dbo.p_big @big NVARCHAR(MAX) OUTPUT, @len1 INT OUTPUT,
                           @len2 INT OUTPUT AS
BEGIN
    SET @big = @big + N'-tail';
    SET @len1 = LEN(@big);
    SET @len2 = LEN(@big);
END;
"""


def test_offloaded_large_value_round_trip():
    big = "α" * 5000                 # unicode, above the tiny test threshold
    dbg = TSQLDebugger(sql_text=PROC_BIG, params={"@big": big},
                       offload_threshold=1000, echo=lambda *_: None)
    dbg.run_all()
    env = dbg._env
    synced = dict(dbg._offload_synced)
    dbg.close()
    assert env["@BIG"] == big + "-tail"
    assert env["@LEN1"] == 5005 and env["@LEN2"] == 5005
    assert synced.get("@BIG") == big + "-tail"   # server copy re-synced after change


PROC_NO_SEMI = """
CREATE PROCEDURE dbo.p_nosemi @a INT OUTPUT, @b INT OUTPUT AS
BEGIN
    SET @a = 1
    SET @b = @a + 1
END;
"""


def test_semicolonless_procedure_debugs_statement_by_statement():
    dbg = TSQLDebugger(sql_text=PROC_NO_SEMI, params={}, echo=lambda *_: None)
    assert len(dbg._steps) == 2      # split on the starter keyword, not one giant step
    dbg.run_all()
    env = dbg._env
    dbg.close()
    assert env["@A"] == 1 and env["@B"] == 2
