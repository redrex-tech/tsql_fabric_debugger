# -*- coding: utf-8 -*-
"""Offline tests for the DAP server, driven over in-memory streams.

A scripted client feeds framed DAP requests; the FakeSession (conftest) plays
the warehouse. Assertions check the responses and events a real client (VS
Code) would rely on.
"""
import io
import json

from tsql_fabric_debugger.dap import DapServer, _parse_client_value

SIMPLE = """
CREATE PROCEDURE dbo.p @n INT, @out INT OUTPUT AS
BEGIN
    SET @out = @n;
    SET @out = @out + 1;
END;
"""


def _frame(payload):
    data = json.dumps(payload).encode()
    return b"Content-Length: %d\r\n\r\n%s" % (len(data), data)


def _requests(*commands):
    out = []
    for seq, (command, arguments) in enumerate(commands, start=1):
        out.append(_frame({"seq": seq, "type": "request", "command": command,
                           "arguments": arguments}))
    return b"".join(out)


def _run(script_bytes):
    rin, rout = io.BytesIO(script_bytes), io.BytesIO()
    DapServer(rin, rout).serve()
    rout.seek(0)
    messages = []
    while True:
        line = rout.readline()
        if not line:
            break
        length = int(line.split(b":")[1])
        rout.readline()                       # blank separator
        messages.append(json.loads(rout.read(length)))
    return messages


def _by(messages, *, command=None, event=None):
    if command is not None:
        return [m for m in messages if m.get("type") == "response"
                and m.get("command") == command]
    return [m for m in messages if m.get("type") == "event"
            and m.get("event") == event]


def _launch_args(tmp_path):
    sql = tmp_path / "proc.sql"
    sql.write_text(SIMPLE, encoding="utf-8")
    return {"program": str(sql), "params": {"@n": 5},
            "server": "s", "database": "d"}


def test_dap_full_session(fake_session, tmp_path):
    fake_session.turn(updates={"@OUT": 5})
    fake_session.turn(updates={"@OUT": 6})
    messages = _run(_requests(
        ("initialize", {}),
        ("launch", _launch_args(tmp_path)),
        ("setBreakpoints", {"source": {"path": "proc.sql"},
                            "breakpoints": [{"line": 5}]}),
        ("configurationDone", {}),
        ("threads", {}),
        ("stackTrace", {"threadId": 1}),
        ("scopes", {"frameId": 1}),
        ("variables", {"variablesReference": 1}),
        ("continue", {"threadId": 1}),
        ("disconnect", {}),
    ))
    init = _by(messages, command="initialize")[0]
    assert init["success"] and init["body"]["supportsConditionalBreakpoints"]
    assert _by(messages, event="initialized")
    assert _by(messages, command="launch")[0]["success"]
    bps = _by(messages, command="setBreakpoints")[0]["body"]["breakpoints"]
    assert bps == [{"verified": True, "line": 5}]
    assert _by(messages, event="stopped")[0]["body"]["reason"] == "entry"
    threads = _by(messages, command="threads")[0]["body"]["threads"]
    assert threads == [{"id": 1, "name": "T-SQL"}]
    frames = _by(messages, command="stackTrace")[0]["body"]["stackFrames"]
    assert frames and frames[0]["line"] == 4          # cursor at SET @out = @n
    variables = _by(messages, command="variables")[0]["body"]["variables"]
    names = {v["name"] for v in variables}
    assert "@out" in names and "@@ROWCOUNT" in names
    # continue hits the line-5 breakpoint, then disconnect rolls back
    stops = [e["body"]["reason"] for e in _by(messages, event="stopped")]
    assert "breakpoint" in stops
    assert fake_session.rolled_back or fake_session.closed


def test_dap_step_and_evaluate(fake_session, tmp_path):
    fake_session.turn(updates={"@OUT": 5})            # next
    fake_session.turn(updates={"__eval__": 10})       # evaluate
    fake_session.turn(updates={"@OUT": 6})            # continue to the end
    messages = _run(_requests(
        ("initialize", {}),
        ("launch", _launch_args(tmp_path)),
        ("configurationDone", {}),
        ("next", {"threadId": 1}),
        ("evaluate", {"expression": "@n * 2"}),
        ("continue", {"threadId": 1}),
        ("disconnect", {}),
    ))
    stops = [e["body"]["reason"] for e in _by(messages, event="stopped")]
    assert stops[0] == "entry" and "step" in stops
    result = _by(messages, command="evaluate")[0]["body"]["result"]
    assert result == "10"
    assert _by(messages, event="terminated")          # ran to the end


def test_dap_exception_breakpoint_on_caught(fake_session, tmp_path):
    sql = tmp_path / "proc.sql"
    sql.write_text("""
CREATE PROCEDURE dbo.p @r INT OUTPUT, @c NVARCHAR(200) OUTPUT AS
BEGIN
    BEGIN TRY
        SET @r = 1 / 0;
    END TRY
    BEGIN CATCH
        SET @c = ERROR_MESSAGE();
    END CATCH
END;
""", encoding="utf-8")
    fake_session.fail("Divide by zero")
    fake_session.turn(updates={"@C": "Divide by zero"})
    messages = _run(_requests(
        ("initialize", {}),
        ("launch", {"program": str(sql), "params": {},
                    "server": "s", "database": "d"}),
        ("setExceptionBreakpoints", {"filters": ["caught"]}),
        ("configurationDone", {}),
        ("continue", {"threadId": 1}),
        ("continue", {"threadId": 1}),
        ("disconnect", {}),
    ))
    stops = [e["body"]["reason"] for e in _by(messages, event="stopped")]
    assert "exception" in stops                       # paused on the handled error
    assert _by(messages, event="terminated")          # second continue finishes


def test_dap_unsupported_request_is_answered(fake_session, tmp_path):
    messages = _run(_requests(("initialize", {}), ("restartFrame", {}),
                              ("disconnect", {})))
    bad = _by(messages, command="restartFrame")[0]
    assert bad["success"] is False and "unsupported" in bad["message"]


def test_parse_client_value():
    assert _parse_client_value("NULL") is None
    assert _parse_client_value("42") == 42
    assert _parse_client_value("1.5") == 1.5
    assert _parse_client_value("'abc'") == "abc"
    assert _parse_client_value("texto ção") == "texto ção"


# ---------------------------------------------------------------------------
# round-2 review fixes
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


def test_dap_breakpoints_before_launch_are_applied(fake_session, tmp_path):
    # nvim-dap/VS Code order: breakpoints arrive right after `initialized`
    fake_session.turn(updates={"@OUT": 5})
    messages = _run(_requests(
        ("initialize", {}),
        ("setBreakpoints", {"source": {"path": "proc.sql"},
                            "breakpoints": [{"line": 5}]}),
        ("configurationDone", {}),
        ("launch", dict(_launch_args(tmp_path), stopOnEntry=False)),
        ("disconnect", {}),
    ))
    pre = _by(messages, command="setBreakpoints")[0]["body"]["breakpoints"]
    assert pre == [{"verified": True, "line": 5}]
    stops = [e["body"]["reason"] for e in _by(messages, event="stopped")]
    assert stops == ["breakpoint"]                # ran and STOPPED at line 5


def test_dap_child_is_collected_and_session_terminates(fake_session, tmp_path):
    sql = tmp_path / "parent.sql"
    sql.write_text(PARENT, encoding="utf-8")
    fake_session.define("dbo.child", CHILD_SRC)
    fake_session.turn(updates={"@DOUBLED": 42})   # child's only step
    fake_session.turn(updates={"@RES": 43})       # parent: SET @res = @res + 1
    messages = _run(_requests(
        ("initialize", {}),
        ("launch", {"program": str(sql), "params": {"@n": 21},
                    "server": "s", "database": "d"}),
        ("configurationDone", {}),
        ("stepIn", {"threadId": 1}),              # enter the EXEC -> child
        ("continue", {"threadId": 1}),            # child runs to its end
        ("continue", {"threadId": 1}),            # parent collects + finishes
        ("disconnect", {}),
    ))
    assert _by(messages, event="terminated")      # session did NOT get stuck
    conts = _by(messages, command="continue")
    assert all(c["success"] for c in conts)


def test_dap_requests_after_terminated_fail_once(fake_session, tmp_path):
    fake_session.turn(updates={"@OUT": 5})
    fake_session.turn(updates={"@OUT": 6})
    messages = _run(_requests(
        ("initialize", {}),
        ("launch", _launch_args(tmp_path)),
        ("configurationDone", {}),
        ("continue", {"threadId": 1}),            # runs to the end -> terminated
        ("next", {"threadId": 1}),                # after terminated
        ("disconnect", {}),
    ))
    next_responses = [m for m in messages if m.get("type") == "response"
                      and m.get("command") == "next"]
    assert len(next_responses) == 1               # exactly ONE response
    assert next_responses[0]["success"] is False
    assert "launch first" in next_responses[0]["message"]


def test_dap_evaluate_server_failure_is_an_error_response(fake_session, tmp_path):
    fake_session.fail("Invalid column name 'nope'")
    messages = _run(_requests(
        ("initialize", {}),
        ("launch", _launch_args(tmp_path)),
        ("configurationDone", {}),
        ("evaluate", {"expression": "nope", "context": "repl"}),
        ("disconnect", {}),
    ))
    ev = _by(messages, command="evaluate")[0]
    assert ev["success"] is False and "rolled back" in ev["message"]


def test_dap_hover_never_touches_the_server(fake_session, tmp_path):
    # no turns queued: any server call would blow up the FakeSession ordering
    messages = _run(_requests(
        ("initialize", {}),
        ("launch", _launch_args(tmp_path)),
        ("configurationDone", {}),
        ("evaluate", {"expression": "@n", "context": "hover"}),
        ("evaluate", {"expression": "1 + 1", "context": "hover"}),
        ("disconnect", {}),
    ))
    var_hover, expr_hover = _by(messages, command="evaluate")
    assert var_hover["success"] and var_hover["body"]["result"] == "5"
    assert expr_hover["success"] is False
    assert "debug console" in expr_hover["message"]


def test_dap_invalid_condition_rejected_others_applied(fake_session, tmp_path):
    fake_session.turn(updates={"@OUT": 5})
    messages = _run(_requests(
        ("initialize", {}),
        ("launch", _launch_args(tmp_path)),
        ("setBreakpoints", {"source": {"path": "proc.sql"},
                            "breakpoints": [{"line": 4, "condition": "((@n"},
                                            {"line": 5}]}),
        ("configurationDone", {}),
        ("continue", {"threadId": 1}),
        ("disconnect", {}),
    ))
    bps = _by(messages, command="setBreakpoints")[0]["body"]["breakpoints"]
    assert bps[0]["verified"] is False and "parenthes" in bps[0]["message"].lower()
    assert bps[1] == {"verified": True, "line": 5}
    stops = [e["body"]["reason"] for e in _by(messages, event="stopped")]
    assert "breakpoint" in stops                  # the valid one still works


def test_dap_hit_condition_parser():
    import io
    srv = DapServer(io.BytesIO(), io.BytesIO())
    assert srv._parse_hit_condition("4") == (4, None)
    assert srv._parse_hit_condition(">= 4") == (4, None)
    assert srv._parse_hit_condition("== 4") == (4, None)
    assert srv._parse_hit_condition("> 3") == (4, None)
    hits, note = srv._parse_hit_condition("%3")
    assert hits is None and "ignored" in note


def test_parse_client_value_strict_numbers():
    assert _parse_client_value("inf") == "inf"       # a string, not float('inf')
    assert _parse_client_value("nan") == "nan"
    assert _parse_client_value("1_000") == "1_000"   # not 1000
    assert _parse_client_value("-2.5e3") == -2500.0


# ---------------------------------------------------------------------------
# layer 1: restart, terminate, exceptionInfo, completions, logpoints
# ---------------------------------------------------------------------------
def test_dap_capabilities_layer1():
    import io
    msgs = _run(_requests(("initialize", {}), ("disconnect", {})))
    caps = _by(msgs, command="initialize")[0]["body"]
    for c in ("supportsRestartRequest", "supportsTerminateRequest",
              "supportsLogPoints", "supportsExceptionInfoRequest",
              "supportsCompletionsRequest"):
        assert caps.get(c) is True, c


def test_dap_restart_replays(fake_session, tmp_path):
    fake_session.turn(updates={"@OUT": 5})       # 1st run: step 1
    fake_session.turn(updates={"@OUT": 6})       # 1st run: step 2 (to the end)
    fake_session.turn(updates={"@OUT": 5})       # after restart: step 1
    fake_session.turn(updates={"@OUT": 6})       # after restart: step 2
    messages = _run(_requests(
        ("initialize", {}),
        ("launch", _launch_args(tmp_path)),      # stopOnEntry defaults True
        ("configurationDone", {}),
        ("restart", {}),
        ("continue", {"threadId": 1}),
        ("disconnect", {}),
    ))
    assert _by(messages, command="restart")[0]["success"]
    stops = [e["body"]["reason"] for e in _by(messages, event="stopped")]
    assert stops == ["entry", "entry"]           # entry, then entry again on restart
    assert _by(messages, event="terminated")


def test_dap_terminate(fake_session, tmp_path):
    messages = _run(_requests(
        ("initialize", {}),
        ("launch", _launch_args(tmp_path)),
        ("configurationDone", {}),
        ("terminate", {}),
        ("disconnect", {}),
    ))
    assert _by(messages, command="terminate")[0]["success"]
    assert _by(messages, event="terminated")


def test_dap_exception_info(fake_session, tmp_path):
    fake_session.fail("Divide by zero error encountered")
    messages = _run(_requests(
        ("initialize", {}),
        ("launch", dict(_launch_args(tmp_path), stopOnEntry=False)),
        ("configurationDone", {}),               # runs, hits the error, stops
        ("exceptionInfo", {"threadId": 1}),
        ("disconnect", {}),
    ))
    info = _by(messages, command="exceptionInfo")[0]
    assert info["success"]
    assert "Divide by zero" in info["body"]["description"]


def test_dap_completions_suggests_variables(fake_session, tmp_path):
    messages = _run(_requests(
        ("initialize", {}),
        ("launch", _launch_args(tmp_path)),
        ("configurationDone", {}),
        ("completions", {"text": "@", "column": 2}),
        ("disconnect", {}),
    ))
    targets = _by(messages, command="completions")[0]["body"]["targets"]
    labels = {t["label"] for t in targets}
    assert "@n" in labels and "@out" in labels and "@@ROWCOUNT" in labels


def test_dap_logpoint_via_logmessage(fake_session, tmp_path):
    fake_session.turn(updates={"@OUT": 5}, watches={"__lp__4_0": 5})
    fake_session.turn(updates={"@OUT": 6})
    messages = _run(_requests(
        ("initialize", {}),
        ("launch", dict(_launch_args(tmp_path), stopOnEntry=False)),
        ("setBreakpoints", {"source": {"path": "proc.sql"},
                            "breakpoints": [{"line": 4, "logMessage": "out={@out}"}]}),
        ("configurationDone", {}),
        ("disconnect", {}),
    ))
    bps = _by(messages, command="setBreakpoints")[0]["body"]["breakpoints"]
    assert bps == [{"verified": True, "line": 4}]
    # logpoint never stops: the run went straight to terminated
    stops = [e["body"]["reason"] for e in _by(messages, event="stopped")]
    assert stops == []
    assert _by(messages, event="terminated")


def test_dap_logpoint_plain_text_never_evaluates(fake_session, tmp_path):
    # regression: a plain-text logMessage must NOT run on the server (it would
    # be invalid SQL and roll back). It prints literally, session survives.
    fake_session.turn(updates={"@OUT": 5})       # SET @out = @n
    fake_session.turn(updates={"@OUT": 6})       # SET @out = @out + 1 (to end)
    messages = _run(_requests(
        ("initialize", {}),
        ("launch", dict(_launch_args(tmp_path), stopOnEntry=False)),
        ("setBreakpoints", {"source": {"path": "proc.sql"},
                            "breakpoints": [{"line": 4, "logMessage": "reached here"}]}),
        ("configurationDone", {}),
        ("disconnect", {}),
    ))
    bps = _by(messages, command="setBreakpoints")[0]["body"]["breakpoints"]
    assert bps == [{"verified": True, "line": 4}]
    stops = [e["body"]["reason"] for e in _by(messages, event="stopped")]
    assert stops == []                            # never stopped, no error
    assert _by(messages, event="terminated")


def test_dap_restart_after_run_finished_relaunches(fake_session, tmp_path):
    # restart must work even after the run finished (terminated) — the whole
    # point of a replay debugger.
    fake_session.turn(updates={"@OUT": 5})       # 1st run to the end
    fake_session.turn(updates={"@OUT": 6})
    fake_session.turn(updates={"@OUT": 5})       # relaunched run
    fake_session.turn(updates={"@OUT": 6})
    messages = _run(_requests(
        ("initialize", {}),
        ("launch", dict(_launch_args(tmp_path), stopOnEntry=False)),
        ("configurationDone", {}),               # runs to the end -> terminated
        ("restart", {}),                          # after terminated: relaunch
        ("continue", {"threadId": 1}),
        ("disconnect", {}),
    ))
    assert _by(messages, command="restart")[0]["success"]
    # two terminated events: the first run, and the relaunched run
    assert len(_by(messages, event="terminated")) == 2


def test_dap_restart_after_terminate_keeps_breakpoints(fake_session, tmp_path):
    # regression: after the run finished, restart must relaunch AND reapply the
    # breakpoints (VS Code does not re-send them on restart).
    fake_session.turn(updates={"@OUT": 5})       # 1st run: line 4, stops at bp line 5
    fake_session.turn(updates={"@OUT": 6})       # 1st run: line 5 (after continue)
    fake_session.turn(updates={"@OUT": 5})       # relaunch: line 4, stops at bp line 5
    fake_session.turn(updates={"@OUT": 6})       # relaunch: line 5
    messages = _run(_requests(
        ("initialize", {}),
        ("launch", dict(_launch_args(tmp_path), stopOnEntry=False)),
        ("setBreakpoints", {"source": {"path": "proc.sql"},
                            "breakpoints": [{"line": 5}]}),
        ("configurationDone", {}),               # runs, stops at bp line 5
        ("continue", {"threadId": 1}),           # runs to end -> terminated
        ("restart", {}),                          # relaunch + reapply bp
        ("continue", {"threadId": 1}),           # should stop at bp line 5 again
        ("disconnect", {}),
    ))
    stops = [e["body"]["reason"] for e in _by(messages, event="stopped")]
    # breakpoint before restart, and breakpoint again AFTER restart
    assert stops.count("breakpoint") == 2, stops


def test_logpoint_message_renders_plain(fake_session):
    from tsql_fabric_debugger.engine import TSQLDebugger
    lines = []
    dbg = TSQLDebugger(sql_text=SIMPLE, params={"@n": 5}, server="s", database="d",
                       echo=lambda m: lines.append(str(m)))
    dbg.log_at(4, message="n={@n} out={@out} done")
    fake_session.turn(updates={"@OUT": 5}, watches={"__lp__4_0": 5, "__lp__4_1": None})
    dbg.step()
    rendered = [l for l in lines if "logpoint (line 4)" in l]
    assert rendered and "n=5 out=NULL done" in rendered[0]   # plain, NULL not None
    dbg.close()


def test_dap_emits_result_set_event_and_console(fake_session, tmp_path):
    # a procedure that produces a diagnostic SELECT -> result-set event + console
    sql = tmp_path / "diag.sql"
    sql.write_text(
        "CREATE PROCEDURE dbo.d @o INT OUTPUT AS\n"
        "BEGIN\n"
        "    SELECT 1 AS a, N'x' AS b;\n"
        "    SET @o = 7;\n"
        "END;\n", encoding="utf-8")
    fake_session.turn(resultsets=[(["a", "b"], [[1, "x"]])])   # the SELECT step
    fake_session.turn(updates={"@O": 7})                       # SET @o = 7
    messages = _run(_requests(
        ("initialize", {}),
        ("launch", {"program": str(sql), "params": {}, "server": "s",
                    "database": "d", "stopOnEntry": False}),
        ("configurationDone", {}),
        ("disconnect", {}),
    ))
    rs = [m for m in messages if m.get("type") == "event"
          and m.get("event") == "tsqlFabricResultSet"]
    assert rs, "no tsqlFabricResultSet event"
    body = rs[0]["body"]
    assert body["columns"] == ["a", "b"] and body["rows"] == [[1, "x"]]
    # also printed to the console
    console = "".join(e["body"]["output"] for e in messages
                      if e.get("event") == "output")
    assert "result set" in console and "a | b" in console
