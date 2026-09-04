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
