# -*- coding: utf-8 -*-
"""Engine behavior driven by a fake pyodbc session — no warehouse.

These exercise the whole execution machinery (capture parsing, env updates,
error routing, CATCH emulation, child EXEC, breakpoints, watches, offload)
so the engine is covered offline and by mutation testing.
"""
import pytest

from tsql_fabric_debugger.engine import TSQLDebugger

SIMPLE = """
CREATE PROCEDURE dbo.p @n INT, @out INT OUTPUT AS
BEGIN
    SET @out = 0;
    SET @out = @n;
    SET @out = @out + 1;
END;
"""

TRY = """
CREATE PROCEDURE dbo.p @r INT OUTPUT, @c NVARCHAR(200) OUTPUT, @z INT OUTPUT AS
BEGIN
    BEGIN TRY
        SET @r = 1;
        SET @r = 2;
        SET @z = 9;
    END TRY
    BEGIN CATCH
        SET @c = ERROR_MESSAGE();
    END CATCH
END;
"""


def _dbg(session, sql=SIMPLE, **kw):
    return TSQLDebugger(sql_text=sql, params={"@n": 5}, server="s", database="d",
                        echo=lambda *_: None, **kw)


# ---------------------------------------------------------------------------
# capture & state
# ---------------------------------------------------------------------------
def test_step_captures_variable_updates(fake_session):
    dbg = _dbg(fake_session)
    fake_session.turn(updates={"@OUT": 0}, rowcount=1)
    entry = dbg.step()
    assert entry["status"] == "SUCCESS"
    assert dbg._env["@OUT"] == 0
    assert entry["changed_vars"] == "@out=0"
    assert entry["rows_affected"] == 1
    dbg.close()


def test_changed_vars_only_lists_what_changed(fake_session):
    dbg = _dbg(fake_session)
    fake_session.turn(updates={"@OUT": 0})       # step 1
    fake_session.turn(updates={"@OUT": 5})       # step 2: @out 0 -> 5
    dbg.step()
    entry = dbg.step()
    assert entry["changed_vars"] == "@out=5"     # @n unchanged, not listed
    dbg.close()


def test_pending_default_runs_before_first_step(fake_session):
    sql = "CREATE PROCEDURE dbo.p @d DATETIME2 = SYSUTCDATETIME(), @o INT OUTPUT AS BEGIN SET @o = 1; END"
    dbg = TSQLDebugger(sql_text=sql, params={}, server="s", database="d", echo=lambda *_: None)
    fake_session.turn(updates={"@D": "2026-01-01"})   # the defaults capture
    fake_session.turn(updates={"@O": 1})              # the SET
    dbg.step()
    assert any(e["kind"] == "params" for e in dbg._log)
    dbg.close()


def test_run_all_executes_every_step(fake_session):
    dbg = _dbg(fake_session)
    for v in (0, 5, 6):
        fake_session.turn(updates={"@OUT": v})
    dbg.run_all()
    assert dbg._env["@OUT"] == 6
    assert len([e for e in dbg._log if e["kind"] == "stmt"]) == 3
    dbg.close()


def test_run_until_stops_at_n(fake_session):
    dbg = _dbg(fake_session)
    for v in (0, 5, 6):
        fake_session.turn(updates={"@OUT": v})
    dbg.run_until(2)
    assert dbg._pos == 2 and dbg._env["@OUT"] == 5
    dbg.close()


# ---------------------------------------------------------------------------
# error routing & CATCH emulation
# ---------------------------------------------------------------------------
def test_error_outside_try_finishes_and_rolls_back(fake_session):
    dbg = _dbg(fake_session)
    fake_session.turn(updates={"@OUT": 0})
    fake_session.fail("boom", number=1205)
    dbg.step()
    entry = dbg.step()
    assert entry["status"] == "ERROR" and "boom" in entry["error"]
    assert dbg._finished
    assert fake_session.rolled_back
    dbg.close()


def test_error_inside_try_emulates_catch_and_continues(fake_session):
    dbg = TSQLDebugger(sql_text=TRY, params={}, server="s", database="d", echo=lambda *_: None)
    fake_session.turn(updates={"@R": 1})                 # SET @r = 1
    fake_session.fail("kaboom")                          # SET @r = 2 fails
    fake_session.turn(updates={"@C": "kaboom"})          # CATCH: SET @c = ERROR_MESSAGE()
    dbg.run_all()
    assert dbg._env["@C"] == "kaboom"
    assert dbg._env["@Z"] is None                        # rest of TRY skipped
    assert dbg._pos >= len(dbg._steps)                   # nothing left after END CATCH
    dbg.close()


def test_error_entry_returned_not_the_catch_entry(fake_session):
    dbg = TSQLDebugger(sql_text=TRY, params={}, server="s", database="d", echo=lambda *_: None)
    dbg.step()                                           # SET @r = 1 (queue below)
    # re-do properly:
    dbg.close()
    dbg = TSQLDebugger(sql_text=TRY, params={}, server="s", database="d", echo=lambda *_: None)
    fake_session.turn(updates={"@R": 1})
    fake_session.fail("bad")
    fake_session.turn(updates={"@C": "bad"})
    dbg.step()                                           # ok
    entry = dbg.step()                                   # fails -> CATCH emulated
    assert entry["status"] == "ERROR"                    # not the SUCCESS catch entry
    dbg.close()


def test_last_error_and_show_error_find_the_error_entry(fake_session):
    # replaces the end-user idiom `next(e for e in dbg._log if e["status"] == "ERROR")`
    dbg = _dbg(fake_session)
    fake_session.turn(updates={"@OUT": 0})
    fake_session.fail("boom")
    dbg.step()
    dbg.step()
    entry = dbg.last_error()
    assert entry is not None and "boom" in entry["error"]
    assert dbg.show_error() is entry              # show_detail() of that entry
    dbg.close()


def test_last_error_returns_the_latest_of_several(fake_session):
    # a CATCH-handled error plus a later fatal one: last_error() is the latest
    dbg = TSQLDebugger(sql_text=TRY, params={}, server="s", database="d", echo=lambda *_: None)
    fake_session.turn(updates={"@R": 1})                 # SET @r = 1
    fake_session.fail("first")                           # SET @r = 2 fails -> CATCH
    fake_session.fail("second")                          # CATCH body fails too
    dbg.run_all()
    entry = dbg.last_error()
    assert entry is not None and "second" in entry["error"]
    dbg.close()


def test_show_error_without_error_returns_none(fake_session):
    dbg = _dbg(fake_session)
    fake_session.turn(updates={"@OUT": 0})
    dbg.step()
    assert dbg.last_error() is None
    assert dbg.show_error() is None
    dbg.close()


def test_proc_name_fetches_deployed_source(fake_session):
    # proc_name= fetches the source via OBJECT_DEFINITION and debugs it like sql_text
    fake_session.define("dbo.simple", SIMPLE)
    dbg = TSQLDebugger(proc_name="dbo.simple", params={"@n": 5}, server="s", database="d",
                       echo=lambda *_: None)
    fake_session.turn(updates={"@OUT": 5})
    fake_session.turn(updates={"@OUT": 6})
    dbg.run_all()
    assert dbg._env["@OUT"] == 6
    dbg.close()


def test_proc_name_missing_object_raises(fake_session):
    with pytest.raises(ValueError, match="VIEW DEFINITION|not available"):
        TSQLDebugger(proc_name="dbo.nao_existe", server="s", database="d",
                     echo=lambda *_: None)


def test_source_params_are_mutually_exclusive(fake_session):
    with pytest.raises(ValueError, match="only one"):
        TSQLDebugger(sql_text=SIMPLE, proc_name="dbo.p", server="s", database="d",
                     echo=lambda *_: None)


def test_fetch_source_is_public(fake_session):
    from tsql_fabric_debugger import fetch_source
    fake_session.define("dbo.simple", SIMPLE)
    assert fetch_source("dbo.simple", "s", "d") == SIMPLE
    with pytest.raises(ValueError):
        fetch_source("dbo.nao_existe", "s", "d")


def test_run_procedure_accepts_proc_name(fake_session):
    from tsql_fabric_debugger import run_procedure
    fake_session.define("dbo.simple", SIMPLE)
    fake_session.turn(updates={"@OUT": 5})
    fake_session.turn(updates={"@OUT": 6})
    log = run_procedure(proc_name="dbo.simple", params={"@n": 5},
                        server="s", database="d", echo=lambda *_: None)
    rows = log.to_dict("records") if hasattr(log, "to_dict") else log
    assert all(r["status"] == "SUCCESS" for r in rows)


def test_fatal_connection_error_is_not_swallowed(monkeypatch):
    import tsql_fabric_debugger.engine as eng

    def boom(*a, **k):
        raise RuntimeError("token expired")

    monkeypatch.setattr(eng, "connect", boom)
    echoes = []
    dbg = TSQLDebugger(sql_text=SIMPLE, params={"@n": 1}, server="s", database="d",
                       echo=echoes.append)
    with pytest.raises(RuntimeError):
        dbg.step()
    assert any("[FATAL]" in e for e in echoes)
    assert dbg._pos == 0                                 # cursor restored for retry


# ---------------------------------------------------------------------------
# transaction lifecycle
# ---------------------------------------------------------------------------
def test_close_rolls_back_by_default(fake_session):
    dbg = _dbg(fake_session)
    fake_session.turn(updates={"@OUT": 0})
    dbg.step()
    dbg.close()
    assert fake_session.rolled_back and not fake_session.committed
    assert fake_session.closed


def test_close_commit_true_commits(fake_session):
    dbg = _dbg(fake_session)
    fake_session.turn(updates={"@OUT": 0})
    dbg.step()
    dbg.close(commit=True)
    assert fake_session.committed and not fake_session.rolled_back


def test_context_manager_rolls_back(fake_session):
    with _dbg(fake_session) as dbg:
        fake_session.turn(updates={"@OUT": 0})
        dbg.step()
    assert fake_session.rolled_back and fake_session.closed


def test_rollback_public_truthful(fake_session):
    dbg = _dbg(fake_session)
    fake_session.turn(updates={"@OUT": 0})
    dbg.step()
    dbg.rollback()
    assert fake_session.rolled_back
    dbg.close()


def test_lock_timeout_forwarded_to_connect(fake_session):
    dbg = _dbg(fake_session, lock_timeout=15)
    fake_session.turn(updates={"@OUT": 0})
    dbg.step()
    assert fake_session.lock_timeout == 15
    dbg.close()


# ---------------------------------------------------------------------------
# result sets, watches, sql()
# ---------------------------------------------------------------------------
def test_intermediate_result_sets_captured(fake_session):
    dbg = _dbg(fake_session)
    fake_session.turn(updates={"@OUT": 0},
                      resultsets=[(["a", "b"], [[1, "x"], [2, "y"]])])
    entry = dbg.step()
    assert entry["result_sets"] == 1
    results = dbg.last_results()
    rows = results[0].values.tolist() if hasattr(results[0], "values") else results[0]["rows"]
    assert rows == [[1, "x"], [2, "y"]]
    dbg.close()


def test_watch_value_captured_and_not_in_env(fake_session):
    dbg = _dbg(fake_session)
    dbg.watch("(SELECT 1)", "one")
    fake_session.turn(updates={"@OUT": 0}, watches={"one": 42})
    dbg.step()
    assert dbg.watches()["one"] == 42
    assert "__WATCH__ONE" not in dbg._env
    dbg.close()


def test_sql_adhoc_returns_rows(fake_session):
    dbg = _dbg(fake_session)
    fake_session.adhoc.append(("count(*)", ["q"], [(7,)]))
    out = dbg.sql("SELECT COUNT(*) AS q FROM t")
    rows = out.values.tolist() if hasattr(out, "values") else out
    assert rows == [[7]] or rows == [{"q": 7}]
    dbg.close()


# ---------------------------------------------------------------------------
# step_into / breakpoints
# ---------------------------------------------------------------------------
IFPROC = """
CREATE PROCEDURE dbo.p @n INT, @x INT OUTPUT AS
BEGIN
    IF @n > 0
    BEGIN
        SET @x = 1;
    END
    ELSE
    BEGIN
        SET @x = 2;
    END;
END;
"""


def test_step_into_if_picks_the_true_branch(fake_session):
    dbg = TSQLDebugger(sql_text=IFPROC, params={"@n": 5}, server="s", database="d",
                       echo=lambda *_: None)
    fake_session.turn(cond=1)                     # IF @n > 0 -> true
    dbg.step_into()
    assert len(dbg._steps) >= 1
    fake_session.turn(updates={"@X": 1})
    dbg.step()
    assert dbg._env["@X"] == 1
    dbg.close()


def test_step_into_if_picks_else(fake_session):
    dbg = TSQLDebugger(sql_text=IFPROC, params={"@n": -1}, server="s", database="d",
                       echo=lambda *_: None)
    fake_session.turn(cond=0)                     # IF false -> ELSE
    dbg.step_into()
    fake_session.turn(updates={"@X": 2})
    dbg.step()
    assert dbg._env["@X"] == 2
    dbg.close()


def test_run_all_into_expands_blocks(fake_session):
    # into=True walks the procedure the step_into() way: the IF becomes a
    # logged "cond" step and the chosen branch runs — no manual step_into loop.
    dbg = TSQLDebugger(sql_text=IFPROC, params={"@n": 5}, server="s", database="d",
                       echo=lambda *_: None)
    fake_session.turn(cond=1)                     # IF @n > 0 -> true
    fake_session.turn(updates={"@X": 1})          # SET @x = 1 (the taken branch)
    dbg.run_all(into=True)
    kinds = [r["kind"] for r in dbg._log]
    assert "cond" in kinds                        # the block was expanded
    assert dbg._env["@X"] == 1
    dbg.close()


def test_run_all_without_into_runs_block_whole(fake_session):
    # the default runs the IF as one atomic step — no "cond" step is logged.
    dbg = TSQLDebugger(sql_text=IFPROC, params={"@n": 5}, server="s", database="d",
                       echo=lambda *_: None)
    fake_session.turn(updates={"@X": 1})          # the whole block, one execute
    dbg.run_all()
    kinds = [r["kind"] for r in dbg._log]
    assert "cond" not in kinds
    dbg.close()


def test_breakpoint_stops_before_the_line(fake_session):
    dbg = _dbg(fake_session)
    dbg.break_at(5)                               # line of "SET @out = @n"
    fake_session.turn(updates={"@OUT": 0})        # only the first step should run
    dbg.run_all()
    assert not dbg._finished
    assert dbg._env["@OUT"] == 0                  # stopped before step 2
    dbg.close()


# ---------------------------------------------------------------------------
# offload
# ---------------------------------------------------------------------------
def test_offloaded_value_hydrated_not_reinjected(fake_session):
    dbg = TSQLDebugger(sql_text="CREATE PROCEDURE dbo.p @big NVARCHAR(MAX), @o INT OUTPUT AS "
                                "BEGIN SET @o = LEN(@big); END",
                       params={"@big": "y" * 500}, server="s", database="d",
                       offload_threshold=100, echo=lambda *_: None)
    fake_session.turn(updates={"@O": 500})
    dbg.step()
    # the big value hit the state table, not the parameter binds
    assert any("#tsqldbg_state" in q for q in fake_session.executed)
    dbg.close()


# ---------------------------------------------------------------------------
# navigation guards & jump_to / run_step
# ---------------------------------------------------------------------------
def test_jump_to_then_run_step(fake_session):
    dbg = _dbg(fake_session)
    dbg.jump_to(3)
    assert dbg._pos == 2
    fake_session.turn(updates={"@OUT": 99})
    entry = dbg.run_step(3)
    assert entry["status"] == "SUCCESS" and dbg._pos == 2   # cursor not moved
    dbg.close()


def test_run_step_out_of_range(fake_session):
    dbg = _dbg(fake_session)
    with pytest.raises(ValueError):
        dbg.run_step(99)
    dbg.close()


def test_finished_step_is_noop(fake_session):
    dbg = _dbg(fake_session)
    for v in (0, 5, 6):
        fake_session.turn(updates={"@OUT": v})
    dbg.run_all()
    assert dbg.step() is None                    # nothing left


# ---------------------------------------------------------------------------
# save / load / reset
# ---------------------------------------------------------------------------
def test_save_and_load_state_round_trip(fake_session, tmp_path):
    dbg = _dbg(fake_session)
    fake_session.turn(updates={"@OUT": 42})
    dbg.step()
    path = str(tmp_path / "s.json")
    dbg.save_state(path)
    dbg.close()

    dbg2 = _dbg(fake_session)
    dbg2.load_state(path)
    assert dbg2._env["@OUT"] == 42
    dbg2.close()


def test_reset_replays_on_the_same_object(fake_session):
    dbg = _dbg(fake_session)
    for v in (0, 5, 6):
        fake_session.turn(updates={"@OUT": v})
    dbg.run_all()
    assert dbg._env["@OUT"] == 6
    dbg.reset()
    assert dbg._env["@OUT"] is None and dbg._log == [] and dbg._pos == 0
    assert fake_session.rolled_back
    for v in (0, 5, 6):
        fake_session.turn(updates={"@OUT": v})
    dbg.run_all()
    assert dbg._env["@OUT"] == 6
    dbg.close()


# ---------------------------------------------------------------------------
# nested EXEC through the fake
# ---------------------------------------------------------------------------
PARENT = """
CREATE PROCEDURE dbo.parent @n INT, @res INT OUTPUT AS
BEGIN
    EXEC dbo.child @x = @n, @doubled = @res OUTPUT;
    SET @res = @res + 1;
END;
"""
CHILD_SRC = ("CREATE PROCEDURE dbo.child @x INT, @doubled INT OUTPUT AS "
             "BEGIN SET @doubled = @x * 2; END")


def test_nested_exec_collects_output(fake_session):
    fake_session.define("dbo.child", CHILD_SRC)
    dbg = TSQLDebugger(sql_text=PARENT, params={"@n": 21}, server="s", database="d",
                       echo=lambda *_: None)
    child = dbg.step_into()                       # enter the EXEC
    assert child is not None and child is not dbg
    fake_session.turn(updates={"@DOUBLED": 42})   # child: SET @doubled = @x*2
    child.step()                                  # child's only step -> done
    entry = dbg.step()                            # parent collects the finished child
    assert entry["kind"] == "exec" and entry["status"] == "SUCCESS"
    assert dbg._env["@RES"] == 42                 # OUTPUT copied back
    fake_session.turn(updates={"@RES": 43})       # SET @res = @res + 1
    dbg.step()
    assert dbg._env["@RES"] == 43
    dbg.close()


def test_nested_exec_dynamic_falls_back(fake_session):
    sql = ("CREATE PROCEDURE dbo.p @o INT OUTPUT AS BEGIN "
           "DECLARE @s NVARCHAR(50) = N'SELECT 1'; EXEC(@s); SET @o = 1; END")
    dbg = TSQLDebugger(sql_text=sql, params={}, server="s", database="d",
                       echo=lambda *_: None)
    # first step is the DECLARE (init) then EXEC(@s) — step_into on EXEC(@s)
    fake_session.turn(updates={"@S": "SELECT 1"})   # DECLARE
    dbg.step()
    fake_session.turn(rowcount=1)                    # EXEC(@s) as a plain step over
    result = dbg.step_into()                         # dynamic -> step over, returns entry
    assert dbg._child is None                        # no child debugger created
    dbg.close()


def test_child_guard_blocks_parent_navigation(fake_session):
    fake_session.define("dbo.child", CHILD_SRC)
    dbg = TSQLDebugger(sql_text=PARENT, params={"@n": 3}, server="s", database="d",
                       echo=lambda *_: None)
    dbg.step_into()
    assert dbg.step_into() is None                # guarded while child active
    assert dbg.run_step(1) is None
    dbg.abort_child()
    assert dbg._child is None                     # EXEC step still pending
    dbg.close()


def test_child_unhandled_error_reaches_parent(fake_session):
    parent = """
CREATE PROCEDURE dbo.parent @caught NVARCHAR(200) OUTPUT, @after INT OUTPUT AS
BEGIN
    BEGIN TRY
        EXEC dbo.boom @x = 1;
        SET @after = 1;
    END TRY
    BEGIN CATCH
        SET @caught = ERROR_MESSAGE();
    END CATCH
END;
"""
    fake_session.define("dbo.boom",
                        "CREATE PROCEDURE dbo.boom @x INT AS BEGIN SELECT @x = 1/0; END")
    dbg = TSQLDebugger(sql_text=parent, params={}, server="s", database="d",
                       echo=lambda *_: None)
    child = dbg.step_into()
    fake_session.fail("Divide by zero")           # child's only step fails
    child.step()
    assert child._propagated_error is not None
    fake_session.turn(updates={"@CAUGHT": "Divide by zero"})   # parent CATCH
    dbg.run_all()
    exec_entries = [e for e in dbg._log if e["kind"] == "exec"]
    assert exec_entries and exec_entries[0]["status"] == "ERROR"
    assert dbg._env["@CAUGHT"] == "Divide by zero"
    assert dbg._env["@AFTER"] is None
    dbg.close()


# ---------------------------------------------------------------------------
# session SET & show_detail
# ---------------------------------------------------------------------------
def test_session_set_runs_unparameterized(fake_session):
    sql = "CREATE PROCEDURE dbo.p @o INT OUTPUT AS BEGIN SET NOCOUNT ON; SET @o = 1; END"
    dbg = TSQLDebugger(sql_text=sql, params={}, server="s", database="d",
                       echo=lambda *_: None)
    fake_session.turn(rowcount=0)                 # SET NOCOUNT ON (session mode)
    dbg.step()
    set_batch = fake_session.executed[-1]
    assert "SET NOCOUNT ON" in set_batch and "SELECT @o = ?" not in set_batch
    dbg.close()


def test_show_detail_after_error_has_raw(fake_session):
    dbg = _dbg(fake_session)
    fake_session.fail("kaput", number=8134)
    dbg.step()
    entry = dbg.show_detail()
    assert entry["status"] == "ERROR"
    detail = dbg._details[entry["step"]]
    assert "kaput" in detail["raw_error"]
    dbg.close()


# ---------------------------------------------------------------------------
# targeted: state propagation & offload sync (mutation-driven)
# ---------------------------------------------------------------------------
def test_loads_from_sql_file(fake_session, tmp_path):
    path = tmp_path / "p.sql"
    path.write_text(SIMPLE, encoding="utf-8")
    dbg = TSQLDebugger(sql_file=str(path), params={"@n": 5}, server="s", database="d",
                       echo=lambda *_: None)
    assert dbg.proc_name == "dbo.p"
    fake_session.turn(updates={"@OUT": 0})
    dbg.step()
    dbg.close()


def test_child_rollback_propagates_to_parent(fake_session):
    fake_session.define("dbo.child", CHILD_SRC)
    dbg = TSQLDebugger(sql_text=PARENT, params={"@n": 3}, server="s", database="d",
                       echo=lambda *_: None)
    dbg._state_table_ok = True                    # pretend the parent had offloaded
    child = dbg.step_into()
    fake_session.fail("child boom")               # child's step fails -> rollback
    child.step()
    assert child._rolled_back
    dbg.run_all()                                 # parent collects the failed child
    assert dbg._rolled_back is True               # propagated from child
    assert dbg._state_table_ok is False           # offload table gone with the rollback
    dbg.close()


def test_child_state_table_creation_propagates_on_success(fake_session):
    fake_session.define("dbo.child", CHILD_SRC)
    dbg = TSQLDebugger(sql_text=PARENT, params={"@n": 3}, server="s", database="d",
                       echo=lambda *_: None)
    child = dbg.step_into()
    child._state_table_ok = True                  # child created the shared table
    fake_session.turn(updates={"@DOUBLED": 6})
    child.step()
    dbg.step()                                    # collect
    assert dbg._state_table_ok is True            # parent learns the table exists
    dbg.close()


def test_offload_resyncs_only_on_change(fake_session):
    dbg = TSQLDebugger(sql_text="CREATE PROCEDURE dbo.p @big NVARCHAR(MAX), @o INT OUTPUT AS "
                                "BEGIN SET @o = 1; SET @o = 2; END",
                       params={"@big": "y" * 300}, server="s", database="d",
                       offload_threshold=100, echo=lambda *_: None)
    fake_session.turn(updates={"@O": 1})          # step 1
    dbg.step()
    syncs_after_1 = sum("INSERT INTO #tsqldbg_state" in q for q in fake_session.executed)
    fake_session.turn(updates={"@O": 2})          # step 2: @big unchanged
    dbg.step()
    syncs_after_2 = sum("INSERT INTO #tsqldbg_state" in q for q in fake_session.executed)
    assert syncs_after_1 == 1                      # synced once
    assert syncs_after_2 == 1                      # NOT re-synced (value unchanged)
    dbg.close()


def test_offload_falls_back_when_temp_table_unavailable(fake_session, monkeypatch):
    dbg = TSQLDebugger(sql_text="CREATE PROCEDURE dbo.p @big NVARCHAR(MAX), @o INT OUTPUT AS "
                                "BEGIN SET @o = 1; END",
                       params={"@big": "y" * 300}, server="s", database="d",
                       offload_threshold=100, echo=lambda *_: None)
    # make the CREATE TABLE raise so the debugger disables offload and binds normally
    orig = dbg._sync_offloaded

    from conftest import FakeCursor
    real_execute = FakeCursor.execute

    def execute(self, sql, params=None):
        if "CREATE TABLE #tsqldbg_state" in sql:
            raise RuntimeError("temp tables not supported")
        return real_execute(self, sql, params)

    monkeypatch.setattr(FakeCursor, "execute", execute)
    fake_session.turn(updates={"@O": 1})
    dbg.step()
    assert dbg._offload_disabled is True           # gracefully degraded
    dbg.close()


def test_hint_only_for_block_steps(fake_session):
    # the "jump_to + step_into" hint must only fire for *_block steps
    echoes = []
    dbg = TSQLDebugger(sql_text=IFPROC, params={"@n": 5}, server="s", database="d",
                       echo=echoes.append)
    fake_session.fail("if boom")                   # the IF block step fails (step over)
    dbg.step()
    assert any("jump_to(n) + step_into()" in e for e in echoes)
    dbg.close()


def test_max_result_rows_truncates(fake_session):
    dbg = _dbg(fake_session, max_result_rows=2)
    fake_session.turn(updates={"@OUT": 0},
                      resultsets=[(["a"], [[1], [2], [3], [4]])])
    entry = dbg.step()
    rs = dbg._details[entry["step"]]["resultsets"][0]
    assert len(rs["rows"]) == 2 and rs["truncated"] is True
    dbg.close()


def test_preview_chars_bounds_the_command(fake_session):
    long_sql = ("CREATE PROCEDURE dbo.p @o INT OUTPUT AS BEGIN "
                "SET @o = " + "0+" * 400 + "0; END")
    dbg = TSQLDebugger(sql_text=long_sql, params={}, server="s", database="d",
                       preview_chars=40, echo=lambda *_: None)
    fake_session.turn(updates={"@O": 0})
    entry = dbg.step()
    assert len(entry["command"]) <= 40
    dbg.close()


# ---------------------------------------------------------------------------
# second targeted round: kill real-logic engine mutants
# ---------------------------------------------------------------------------
def test_rollback_announces_success_only_when_it_ran(fake_session):
    msgs = []
    dbg = _dbg(fake_session)
    dbg._echo = msgs.append
    fake_session.turn(updates={"@OUT": 0})
    dbg.step()
    dbg.rollback()
    assert any("data effects undone" in m for m in msgs)   # _safe_rollback returned True
    dbg.close()


def test_error_entry_has_no_rows_affected(fake_session):
    dbg = _dbg(fake_session)
    fake_session.fail("boom")
    entry = dbg.step()
    assert entry["status"] == "ERROR" and entry["rows_affected"] is None
    dbg.close()


def test_cond_step_does_not_carry_rowcount(fake_session):
    dbg = TSQLDebugger(sql_text=IFPROC, params={"@n": 1}, server="s", database="d",
                       echo=lambda *_: None)
    fake_session.turn(cond=1, rowcount=99)      # the condition eval
    dbg.step_into()
    cond = next(e for e in dbg._log if e["kind"] == "cond")
    assert cond["rows_affected"] is None        # update_rowcount=False on conditions
    dbg.close()


def test_reset_clears_rolled_back(fake_session):
    dbg = _dbg(fake_session)
    fake_session.fail("boom")
    dbg.step()
    assert dbg._rolled_back
    for v in (0, 5, 6):
        fake_session.turn(updates={"@OUT": v})
    dbg.reset()
    assert dbg._rolled_back is False            # cleared on reset
    dbg.close()


def test_full_log_level_prints_the_whole_command(fake_session):
    msgs = []
    dbg = _dbg(fake_session, log_level="full")
    dbg._echo = msgs.append
    fake_session.turn(updates={"@OUT": 0})
    dbg.step()
    # full mode echoes the command body lines with a "| " prefix
    assert any("| " in m and "SET" in m for m in msgs)
    dbg.close()


def test_error_severity_and_state_in_catch(fake_session):
    sql = """
CREATE PROCEDURE dbo.p @out NVARCHAR(100) OUTPUT AS
BEGIN
    BEGIN TRY
        SET @out = N'x';
    END TRY
    BEGIN CATCH
        SET @out = CONCAT(ERROR_SEVERITY(), N'/', ERROR_STATE(), N'/', ERROR_NUMBER());
    END CATCH
END;
"""
    dbg = TSQLDebugger(sql_text=sql, params={}, server="s", database="d",
                       echo=lambda *_: None)
    fake_session.fail("kaboom", number=8134)      # the TRY step fails
    # the CATCH batch must bind ERROR_SEVERITY/STATE/NUMBER without KeyError
    fake_session.turn(updates={"@OUT": "16/1/8134"})
    dbg.run_all()
    assert dbg._env["@OUT"] == "16/1/8134"
    dbg.close()


def test_watch_not_echoed_on_condition_steps(fake_session):
    msgs = []
    dbg = TSQLDebugger(sql_text=IFPROC, params={"@n": 1}, server="s", database="d",
                       echo=msgs.append)
    dbg.watch("(SELECT 1)", "w")
    fake_session.turn(cond=1, watches={"w": 5})
    dbg.step_into()                               # a cond step
    # the "?? w = ..." watch echo must not fire on cond/params kinds
    assert not any("?? w =" in m for m in msgs)
    dbg.close()


def test_sql_without_pandas_returns_dicts(fake_session, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_pandas(name, *a, **k):
        if name == "pandas":
            raise ImportError("no pandas")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_pandas)
    dbg = _dbg(fake_session)
    fake_session.adhoc.append(("dual", ["a", "b"], [(1, 2)]))
    out = dbg.sql("SELECT a, b FROM dual")
    assert out == [{"a": 1, "b": 2}]              # dict path (zip columns, row)
    dbg.close()


# ---------------------------------------------------------------------------
# 0.2.1 usability: find_step, jump_to/run_until by text or line
# ---------------------------------------------------------------------------
def test_find_step_by_text_and_line(fake_session):
    dbg = _dbg(fake_session)              # SIMPLE: 3 SET @out statements
    assert dbg.find_step("= @n") == 2
    assert dbg.find_step(line=6) == 3
    with pytest.raises(ValueError, match="No step contains"):
        dbg.find_step("nonexistent")
    with pytest.raises(ValueError, match="No step starts at line"):
        dbg.find_step(line=999)
    with pytest.raises(ValueError, match="exactly one"):
        dbg.find_step("x", line=1)


def test_find_step_multiple_matches_returns_first(fake_session):
    notes = []
    dbg = _dbg(fake_session)
    dbg._echo = notes.append
    n = dbg.find_step("@out")             # all 3 statements mention @out
    assert n == 1
    assert any("3 steps contain" in m for m in notes)


def test_jump_to_by_text(fake_session):
    dbg = _dbg(fake_session)
    dbg.jump_to("= @n")               # step 2
    assert dbg._pos == 1


def test_run_until_by_text_and_line(fake_session):
    dbg = _dbg(fake_session)
    for v in (0, 5, 6):
        fake_session.turn(updates={"@OUT": v})
    dbg.run_until("= @n")              # runs steps 1..2
    assert dbg._pos == 2 and dbg._env["@OUT"] == 5


def test_summarize_ok_and_failed():
    from tsql_fabric_debugger import summarize
    ok_log = [{"step": 1, "line": 4, "kind": "stmt", "status": "SUCCESS", "duration_s": 0.1},
              {"step": 2, "line": 5, "kind": "stmt", "status": "SUCCESS", "duration_s": 0.2}]
    msgs = []
    info = summarize(ok_log, echo=msgs.append)
    assert info["ok"] and info["steps"] == 2 and info["error_step"] is None
    assert any("OK" in m and "no error" in m for m in msgs)

    fail_log = [{"step": 1, "line": 4, "kind": "stmt", "status": "SUCCESS", "duration_s": 0.1},
                {"step": 2, "line": 6, "kind": "stmt", "status": "ERROR", "duration_s": 0.0,
                 "error": "Divide by zero"}]
    msgs = []
    info = summarize(fail_log, echo=msgs.append)
    assert not info["ok"] and info["error_line"] == 6 and info["error"] == "Divide by zero"
    assert any("FAILED at step 2 (line 6)" in m for m in msgs)


def test_summarize_handled_by_catch():
    from tsql_fabric_debugger import summarize
    log = [{"step": 1, "line": 6, "kind": "stmt", "status": "ERROR", "duration_s": 0.0,
            "error": "boom"},
           {"step": 2, "line": 10, "kind": "catch", "status": "SUCCESS", "duration_s": 0.1}]
    msgs = []
    info = summarize(log, echo=msgs.append)
    assert not info["ok"] and info["handled"]
    assert any("handled by a CATCH" in m for m in msgs)


# ---------------------------------------------------------------------------
# 0.3.0: step_out, eval, stack, stop_on_error="any", hit-count/once, logpoints
# ---------------------------------------------------------------------------
LOOPPROC = """
CREATE PROCEDURE dbo.l @n INT, @i INT OUTPUT AS
BEGIN
    SET @i = 0;
    WHILE @i < @n
    BEGIN
        SET @i = @i + 1;
    END;
END;
"""


def test_step_out_finishes_the_expanded_block(fake_session):
    dbg = TSQLDebugger(sql_text=IFPROC, params={"@n": 5}, server="s", database="d",
                       echo=lambda *_: None)
    fake_session.turn(cond=1)                     # IF @n > 0 -> true
    dbg.step_into()                               # cursor inside the branch
    assert dbg._steps[dbg._pos]["depth"] == 1
    fake_session.turn(updates={"@X": 1})          # the branch body
    entry = dbg.step_out()
    assert entry is not None and entry["status"] == "SUCCESS"
    assert dbg._env["@X"] == 1
    # cursor left the block: end of plan or a depth-0 step
    assert dbg._pos >= len(dbg._steps) or dbg._steps[dbg._pos]["depth"] == 0
    dbg.close()


def test_step_out_at_top_level_is_a_noop(fake_session):
    dbg = _dbg(fake_session)
    assert dbg.step_out() is None
    assert dbg._pos == 0                          # nothing ran
    dbg.close()


def test_step_out_finishes_an_active_child(fake_session):
    fake_session.define("dbo.child", CHILD_SRC)
    dbg = TSQLDebugger(sql_text=PARENT, params={"@n": 21}, server="s", database="d",
                       echo=lambda *_: None)
    dbg.step_into()                               # enter the EXEC -> child active
    fake_session.turn(updates={"@DOUBLED": 42})   # child's only step
    entry = dbg.step_out()                        # finish child + collect
    assert entry["kind"] == "exec" and entry["status"] == "SUCCESS"
    assert dbg._env["@RES"] == 42
    assert dbg._child is None
    dbg.close()


def test_eval_returns_the_value_and_logs(fake_session):
    dbg = _dbg(fake_session)
    fake_session.turn(updates={"__eval__": 10})
    assert dbg.eval("@n * 2") == 10
    assert "__eval__" not in dbg._env             # no environment pollution
    assert dbg._log[-1]["kind"] == "eval"
    dbg.close()


def test_eval_failure_reports_and_returns_none(fake_session):
    dbg = _dbg(fake_session)
    fake_session.fail("Invalid column name 'nope'")
    assert dbg.eval("nope") is None
    assert dbg._log[-1]["status"] == "ERROR"
    assert not dbg._finished                      # an eval never ends the debug
    dbg.close()


def test_eval_rejects_unbalanced_parentheses(fake_session):
    dbg = _dbg(fake_session)
    with pytest.raises(ValueError, match="parentheses"):
        dbg.eval("(SELECT 1")
    dbg.close()


def test_stack_shows_block_and_child_frames(fake_session):
    fake_session.define("dbo.child", CHILD_SRC)
    dbg = TSQLDebugger(sql_text=PARENT, params={"@n": 21}, server="s", database="d",
                       echo=lambda *_: None)
    child = dbg.step_into()
    frames = child.stack()
    assert [f["procedure"] for f in frames] == ["dbo.parent", "dbo.child"]
    assert frames[0]["active"] is False and frames[1]["active"] is True
    dbg.close()

    dbg = TSQLDebugger(sql_text=IFPROC, params={"@n": 5}, server="s", database="d",
                       echo=lambda *_: None)
    fake_session.turn(cond=1)
    dbg.step_into()
    frames = dbg.stack()
    assert len(frames) == 1 and frames[0]["blocks"]
    assert frames[0]["blocks"][0].startswith("IF @n > 0")
    dbg.close()


def test_stop_on_error_any_pauses_on_handled_error(fake_session):
    dbg = TSQLDebugger(sql_text=TRY, params={}, server="s", database="d",
                       echo=lambda *_: None, stop_on_error="any")
    fake_session.turn(updates={"@R": 1})          # SET @r = 1
    fake_session.fail("kaboom")                   # SET @r = 2 fails
    fake_session.turn(updates={"@C": "kaboom"})   # CATCH emulated
    dbg.run_all()
    assert not dbg._finished                      # paused, not finished
    assert dbg.last_error() is not None
    dbg.run_all()                                 # resumes to the end
    assert dbg._pos >= len(dbg._steps)
    dbg.close()


def test_stop_on_error_rejects_bad_value(fake_session):
    with pytest.raises(ValueError, match="stop_on_error"):
        TSQLDebugger(sql_text=SIMPLE, server="s", database="d",
                     echo=lambda *_: None, stop_on_error="always")


def test_breakpoint_hit_count_stops_on_the_nth_pass(fake_session):
    dbg = TSQLDebugger(sql_text=LOOPPROC, params={"@n": 5}, server="s", database="d",
                       echo=lambda *_: None)
    dbg.break_at(7, hits=2)                       # the loop body line, 2nd pass
    fake_session.turn(updates={"@I": 0})          # SET @i = 0
    fake_session.turn(cond=1)                     # WHILE iter 1
    fake_session.turn(updates={"@I": 1})          # body iter 1 (hit #1: no stop)
    fake_session.turn(cond=1)                     # WHILE iter 2
    dbg.run_all()
    assert dbg._env["@I"] == 1                    # exactly one iteration ran
    assert dbg.breaks()[7]["count"] == 2
    dbg.close()


def test_breakpoint_once_removes_itself(fake_session):
    dbg = TSQLDebugger(sql_text=LOOPPROC, params={"@n": 1}, server="s", database="d",
                       echo=lambda *_: None)
    dbg.break_at(7, once=True)
    fake_session.turn(updates={"@I": 0})
    fake_session.turn(cond=1)
    dbg.run_all()                                 # stops before the body
    assert dbg.breaks() == {}                     # fired and removed itself
    fake_session.turn(updates={"@I": 1})          # body
    fake_session.turn(cond=0)                     # WHILE ends
    dbg.run_all()
    assert dbg._env["@I"] == 1
    dbg.close()


def test_logpoint_prints_without_stopping(fake_session):
    lines = []
    dbg = TSQLDebugger(sql_text=SIMPLE, params={"@n": 5}, server="s", database="d",
                       echo=lambda m: lines.append(str(m)))
    dbg.log_at(4, "@n")                           # SET @out = @n line
    fake_session.turn(updates={"@OUT": 5}, watches={"logpoint_l4": 5})
    fake_session.turn(updates={"@OUT": 6})
    dbg.run_all()
    assert any("?? logpoint_l4 = 5" in l for l in lines)
    assert dbg._pos >= len(dbg._steps)            # never stopped
    assert "logpoint_l4" not in dbg._watches      # disarmed after the step
    dbg.close()


def test_logpoint_without_expr_marks_the_passage(fake_session):
    lines = []
    dbg = TSQLDebugger(sql_text=SIMPLE, params={"@n": 5}, server="s", database="d",
                       echo=lambda m: lines.append(str(m)))
    dbg.log_at(4)
    fake_session.turn(updates={"@OUT": 5})
    dbg.step()
    assert any("logpoint: reached line 4" in l for l in lines)
    dbg.close()


# ---------------------------------------------------------------------------
# 0.3.0 review fixes (multi-persona: eng. dados / analista / QA / DBA)
# ---------------------------------------------------------------------------
def test_break_at_rejects_non_int_line_and_bad_hits(fake_session):
    dbg = _dbg(fake_session)
    with pytest.raises(ValueError, match="int"):
        dbg.break_at("7")
    with pytest.raises(ValueError, match="int"):
        dbg.break_at(7, hits=2.5)
    dbg.close()


def test_expression_validation_blocks_batch_breakers(fake_session):
    dbg = _dbg(fake_session)
    for bad in ("1 -- oops", "'abc", "1); SELECT (1", "1; SELECT 2"):
        with pytest.raises(ValueError):
            dbg.watch(bad)
        with pytest.raises(ValueError):
            dbg.eval(bad)
        with pytest.raises(ValueError):
            dbg.log_at(4, bad)
    dbg.close()


def test_watch_cannot_take_the_logpoint_name(fake_session):
    dbg = _dbg(fake_session)
    with pytest.raises(ValueError, match="reserved"):
        dbg.watch("@n", name="logpoint_l4")
    dbg.close()


def test_logpoint_inside_block_fires_via_auto_expand(fake_session):
    lines = []
    dbg = TSQLDebugger(sql_text=LOOPPROC, params={"@n": 1}, server="s", database="d",
                       echo=lambda m: lines.append(str(m)))
    dbg.log_at(7, "@i")                            # loop body line
    fake_session.turn(updates={"@I": 0})           # SET @i = 0
    fake_session.turn(cond=1)                      # WHILE iter 1
    fake_session.turn(updates={"@I": 1}, watches={"logpoint_l7": 1})
    fake_session.turn(cond=0)                      # WHILE ends
    dbg.run_all()                                  # WITHOUT into=True
    assert any("?? logpoint_l7 = 1" in l for l in lines)
    assert dbg._pos >= len(dbg._steps)             # never stopped
    dbg.close()


def test_logpoint_in_break_loop_notices_and_runs_whole(fake_session):
    loop_break = """
CREATE PROCEDURE dbo.p @i INT OUTPUT AS
BEGIN
    WHILE 1 = 1
    BEGIN
        SET @i = 1;
        BREAK;
    END;
END;
"""
    lines = []
    dbg = TSQLDebugger(sql_text=loop_break, params={}, server="s", database="d",
                       echo=lambda m: lines.append(str(m)))
    dbg.log_at(6, "@i")                            # inside the BREAK loop
    fake_session.turn(updates={"@I": 1})           # whole loop, one batch
    dbg.run_all()
    assert any("cannot fire" in l for l in lines)  # noticed, did NOT stop
    assert dbg._env["@I"] == 1
    dbg.close()


def test_logpoint_on_header_fires_with_step_into(fake_session):
    lines = []
    dbg = TSQLDebugger(sql_text=IFPROC, params={"@n": 5}, server="s", database="d",
                       echo=lambda m: lines.append(str(m)))
    dbg.log_at(4, "@n")                            # the IF header line
    fake_session.turn(cond=1, watches={"logpoint_l4": 5})
    dbg.step_into()
    assert any("?? logpoint_l4 = 5" in l for l in lines)
    assert "logpoint_l4" not in dbg._watches
    dbg.close()


def test_logpoint_disarmed_during_catch_emulation(fake_session):
    dbg = TSQLDebugger(sql_text=TRY, params={}, server="s", database="d",
                       echo=lambda *_: None)
    dbg.log_at(6, "@r")                            # the failing TRY line (SET @r = 2)
    fake_session.turn(updates={"@R": 1})           # SET @r = 1
    fake_session.fail("kaboom")                    # SET @r = 2 fails (logpoint armed)
    fake_session.turn(updates={"@C": "kaboom"})    # CATCH step
    dbg.run_all()
    failing = [b for b in fake_session.executed if "SET @r = 2" in b]
    catch = [b for b in fake_session.executed if "SET @c" in b]   # rewritten stmt
    assert failing and any("__watch__logpoint" in b for b in failing)
    assert catch and not any("__watch__logpoint" in b for b in catch)
    dbg.close()


def test_run_until_honors_stop_on_error_any(fake_session):
    dbg = TSQLDebugger(sql_text=TRY, params={}, server="s", database="d",
                       echo=lambda *_: None, stop_on_error="any")
    fake_session.turn(updates={"@R": 1})
    fake_session.fail("kaboom")
    fake_session.turn(updates={"@C": "kaboom"})
    dbg.run_until(line=7)                          # target past the failing step
    assert not dbg._finished and dbg.last_error() is not None   # paused
    dbg.close()


def test_step_out_stops_at_breakpoints_inside_the_block(fake_session):
    dbg = TSQLDebugger(sql_text=LOOPPROC, params={"@n": 5}, server="s", database="d",
                       echo=lambda *_: None)
    fake_session.turn(updates={"@I": 0})
    dbg.step()                                     # SET @i = 0
    fake_session.turn(cond=1)
    dbg.step_into()                                # inside iteration 1
    dbg.break_at(7)                                # body line
    entry = dbg.step_out()                         # must stop AT the breakpoint
    assert dbg._steps[dbg._pos]["line"] == 7       # cursor before the body
    assert dbg._env["@I"] == 0                     # body did not run
    dbg.close()


def test_reset_zeroes_breakpoint_hit_counters(fake_session):
    dbg = TSQLDebugger(sql_text=LOOPPROC, params={"@n": 5}, server="s", database="d",
                       echo=lambda *_: None)
    dbg.break_at(7, hits=2)
    fake_session.turn(updates={"@I": 0})
    fake_session.turn(cond=1)
    fake_session.turn(updates={"@I": 1})
    fake_session.turn(cond=1)
    dbg.run_all()                                  # stops on hit #2
    assert dbg.breaks()[7]["count"] == 2
    dbg.reset()
    assert dbg.breaks()[7]["count"] == 0           # replay counts from zero
    dbg.close()


def test_eval_failure_keeps_the_procedure_error_state(fake_session):
    dbg = _dbg(fake_session)
    dbg._error_msg, dbg._error_number = "erro original", 123
    fake_session.fail("eval kaboom")
    assert dbg.eval("@nope") is None
    assert dbg._error_msg == "erro original"       # not polluted by the eval
    assert dbg._error_number == 123
    dbg.close()


def test_broken_breakpoint_condition_pauses_not_crashes(fake_session):
    lines = []
    dbg = TSQLDebugger(sql_text=SIMPLE, params={"@n": 5}, server="s", database="d",
                       echo=lambda m: lines.append(str(m)))
    dbg.break_at(4, "@typo = 1")
    fake_session.fail("Invalid column name 'typo'")   # the condition eval fails
    dbg.run_all()                                  # must NOT raise
    assert any("FAILED to evaluate" in l for l in lines)
    assert dbg._pos == 0                           # paused before the step
    dbg.close()


def test_max_loop_iterations_pauses_run_all(fake_session):
    lines = []
    dbg = TSQLDebugger(sql_text=LOOPPROC, params={"@n": 99}, server="s", database="d",
                       echo=lambda m: lines.append(str(m)), max_loop_iterations=1)
    fake_session.turn(updates={"@I": 0})           # SET @i = 0
    fake_session.turn(cond=1)                      # iter 1
    fake_session.turn(updates={"@I": 1})           # body 1
    fake_session.turn(cond=1)                      # iter 2 -> exceeds max=1
    dbg.run_all(into=True)
    assert any("max_loop_iterations" in l and "[BREAK]" in l for l in lines)
    dbg.close()


def test_validate_expr_round2_bypasses(fake_session):
    dbg = _dbg(fake_session)
    for bad in ("[foo", '"foo', "'abc''", "1) AS [x], (2"):
        with pytest.raises(ValueError):
            dbg.watch(bad)
    dbg.watch("[a--b]")                            # valid identifier: accepted
    dbg.close()


def test_step_clears_the_breakpoint_pause(fake_session):
    dbg = _dbg(fake_session)
    dbg.break_at(5)                                # line of "SET @out = @out + 1"
    fake_session.turn(updates={"@OUT": 5})
    dbg.run_all()                                  # pauses BEFORE line 5
    assert dbg._break_resume is not None
    fake_session.turn(updates={"@OUT": 6})
    dbg.step()                                     # manual step past the pause
    assert dbg._break_resume is None               # stale pause consumed
    dbg.close()


def test_loop_abort_pauses_on_the_auto_expand_path(fake_session):
    lines = []
    dbg = TSQLDebugger(sql_text=LOOPPROC + "", params={"@n": 99}, server="s",
                       database="d", echo=lambda m: lines.append(str(m)),
                       max_loop_iterations=1)
    dbg.log_at(7, "@i")                            # forces auto-expansion
    fake_session.turn(updates={"@I": 0})
    fake_session.turn(cond=1, watches={"logpoint_l7": 0})
    fake_session.turn(updates={"@I": 1}, watches={"logpoint_l7": 1})
    fake_session.turn(cond=1)                      # iter 2 -> exceeds max=1
    dbg.run_all()                                  # into=False: auto-expand path
    assert any("[BREAK]" in l and "max_loop_iterations" in l for l in lines)
    dbg.close()


def test_dangling_loop_abort_pauses_next_run_all(fake_session):
    post_loop = LOOPPROC.replace("END;\nEND;", "END;\n    SET @i = @i * 100;\nEND;")
    lines = []
    dbg = TSQLDebugger(sql_text=post_loop, params={"@n": 99}, server="s",
                       database="d", echo=lambda m: lines.append(str(m)),
                       max_loop_iterations=1)
    fake_session.turn(updates={"@I": 0})
    dbg.step()                                     # SET @i = 0
    fake_session.turn(cond=1)
    dbg.step_into()                                # iter 1 expanded
    fake_session.turn(updates={"@I": 1})
    dbg.step()                                     # body
    fake_session.turn(cond=1)
    dbg.step_into()                                # iter 2 -> manual abort
    assert dbg._loop_aborted
    dbg.run_all()                                  # must pause BEFORE post-loop
    assert dbg._env["@I"] == 1                     # SET @i * 100 did NOT run
    assert any("[BREAK]" in l and "max_loop_iterations" in l for l in lines)
    dbg.close()


def test_run_until_notices_marks_inside_blocks(fake_session):
    lines = []
    dbg = TSQLDebugger(sql_text=LOOPPROC, params={"@n": 1}, server="s",
                       database="d", echo=lambda m: lines.append(str(m)))
    dbg.log_at(7, "@i")
    fake_session.turn(updates={"@I": 0})
    fake_session.turn(updates={"@I": 1})           # loop runs whole under run_until
    dbg.run_until(line=5)                          # up to the WHILE, inclusive
    assert any("[NOTICE] run_until runs blocks whole" in l for l in lines)
    dbg.close()


def test_breakpoint_on_while_header_and_body_stops_in_body(fake_session):
    # regression: breakpoint on the WHILE header (line 5) AND inside the loop
    # (line 7). Continuing from the header must expand the loop and stop at the
    # body breakpoint — not run the whole loop past it.
    dbg = TSQLDebugger(sql_text=LOOPPROC, params={"@n": 3}, server="s", database="d",
                       echo=lambda *_: None)
    dbg.break_at(5)                             # WHILE header
    dbg.break_at(7)                             # loop body (SET @i = @i + 1)
    fake_session.turn(updates={"@I": 0})        # SET @i = 0
    dbg.run_all()
    assert dbg._steps[dbg._pos]["line"] == 5    # stopped at the header
    fake_session.turn(cond=1)                   # WHILE @i < @n -> true (expand)
    dbg.run_all()
    assert dbg._steps[dbg._pos]["line"] == 7    # stopped INSIDE the loop
    assert dbg._env["@I"] == 0                  # body not executed yet
    dbg.close()


def test_list_parameters_via_fake(fake_session):
    from tsql_fabric_debugger.introspect import list_parameters
    # FakeSession answers registered ad-hoc queries by needle
    fake_session.adhoc.append(("information_schema.parameters",
                               ["PARAMETER_NAME", "DATA_TYPE", "PARAMETER_MODE"],
                               [("@year", "int", "IN"),
                                ("@out", "int", "INOUT")]))
    result = list_parameters("dbo.load_sales", "s", "d")
    assert result == [{"name": "@year", "type": "int", "mode": "IN"},
                      {"name": "@out", "type": "int", "mode": "INOUT"}]


def test_list_procedures_via_fake(fake_session):
    from tsql_fabric_debugger.introspect import list_procedures
    fake_session.adhoc.append(("information_schema.routines",
                               ["ROUTINE_SCHEMA", "ROUTINE_NAME"],
                               [("dbo", "a"), ("pck", "b")]))
    result = list_procedures("s", "d")
    assert result == [{"schema": "dbo", "name": "a"},
                      {"schema": "pck", "name": "b"}]


def test_access_token_env_skips_az(monkeypatch):
    # a caller-provided token (VS Code extension) is used, avoiding the az CLI
    import tsql_fabric_debugger.connection as conn_mod
    monkeypatch.setenv("FABRIC_TSQL_ACCESS_TOKEN", "a-ready-token")
    assert conn_mod._get_token() == "a-ready-token"
    monkeypatch.delenv("FABRIC_TSQL_ACCESS_TOKEN")


def test_steppable_lines_offline_parse():
    """steppable_lines() returns statement lines with no connection."""
    from tsql_fabric_debugger.introspect import steppable_lines
    sql = ("CREATE PROCEDURE dbo.p AS\n"   # 1 header (not steppable)
           "BEGIN\n"                        # 2 BEGIN (not steppable)
           "    DECLARE @i INT = 0;\n"      # 3 statement
           "    SET @i = @i + 1;\n"         # 4 statement
           "END;\n")                        # 5 END (not steppable)
    assert steppable_lines(sql) == [3, 4]


def test_steppable_lines_includes_nested_block_statements():
    """Statements inside IF/ELSE/WHILE/CATCH are breakpoint-able and returned."""
    from tsql_fabric_debugger.introspect import steppable_lines
    sql = (
        "CREATE PROCEDURE dbo.p @n INT AS\n"   # 1
        "BEGIN\n"                              # 2
        "    BEGIN TRY\n"                       # 3
        "        IF @n > 0\n"                   # 4  block header
        "        BEGIN\n"                       # 5
        "            SET @n = @n + 1;\n"        # 6  nested (IF body)
        "        END\n"                         # 7
        "        ELSE\n"                        # 8
        "            SET @n = -1;\n"            # 9  nested (ELSE body)
        "        WHILE @n < 3\n"                # 10 block header
        "            SET @n = @n + 1;\n"        # 11 nested (WHILE body)
        "    END TRY\n"                         # 12
        "    BEGIN CATCH\n"                     # 13
        "        SET @n = -99;\n"               # 14 nested (CATCH body)
        "    END CATCH\n"                       # 15
        "END;\n")                               # 16
    # headers 4 & 10, plus the nested bodies 6, 9, 11 and the CATCH body 14
    assert steppable_lines(sql) == [4, 6, 9, 10, 11, 14]


def test_fetch_source_offline(fake_session):
    """fetch-source returns a deployed procedure's OBJECT_DEFINITION."""
    from tsql_fabric_debugger.connection import fetch_source
    fake_session.object_defs["DBO.P"] = (
        "CREATE PROCEDURE dbo.p AS BEGIN SELECT 1; END;")
    assert "CREATE PROCEDURE dbo.p" in fetch_source("dbo.p", "s", "d")


def test_deploy_sql_offline(fake_session):
    """deploy_sql executes the batches and reports how many ran (autocommit)."""
    from tsql_fabric_debugger.introspect import deploy_sql
    sql = "CREATE OR ALTER PROCEDURE dbo.p AS SELECT 1;\nGO\n"
    assert deploy_sql(sql, "s", "d") == {"ok": True, "batches": 1}


def test_deploy_sql_splits_on_go(fake_session):
    """GO separates batches; blank batches are skipped."""
    from tsql_fabric_debugger.introspect import deploy_sql
    sql = "CREATE OR ALTER PROCEDURE dbo.a AS SELECT 1;\nGO\nSELECT 2;\nGO\n"
    assert deploy_sql(sql, "s", "d") == {"ok": True, "batches": 2}


def test_deploy_sql_empty_raises(fake_session):
    from tsql_fabric_debugger.introspect import deploy_sql
    import pytest
    with pytest.raises(ValueError):
        deploy_sql("   \n GO \n", "s", "d")


def test_fetch_source_cli_requires_proc():
    """The fetch-source CLI verb errors (JSON) without --proc."""
    from tsql_fabric_debugger.introspect import main
    import io
    import contextlib
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = main(["fetch-source", "--server", "s", "--database", "d"])
    assert rc == 1 and "error" in out.getvalue()


def test_steppable_lines_rejects_loose_script():
    """A non-procedure script raises (the CLI turns this into {"error": ...})."""
    from tsql_fabric_debugger.introspect import steppable_lines
    import pytest
    with pytest.raises(Exception):
        steppable_lines("SELECT 1;")


def test_fetch_all_sources_offline(fake_session):
    """fetch_all_sources lists procedures and reads each definition on one session."""
    from tsql_fabric_debugger.introspect import fetch_all_sources
    fake_session.adhoc.append(("information_schema.routines",
                               ["ROUTINE_SCHEMA", "ROUTINE_NAME"],
                               [("dbo", "a"), ("pck", "b")]))
    fake_session.object_defs["DBO.A"] = "CREATE PROCEDURE dbo.a AS SELECT 1;"
    fake_session.object_defs["PCK.B"] = "CREATE PROCEDURE pck.b AS SELECT 2;"
    result = fetch_all_sources("s", "d")
    assert result == [
        {"schema": "dbo", "name": "a", "source": "CREATE PROCEDURE dbo.a AS SELECT 1;"},
        {"schema": "pck", "name": "b", "source": "CREATE PROCEDURE pck.b AS SELECT 2;"},
    ]
