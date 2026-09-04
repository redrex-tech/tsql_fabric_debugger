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
    messages = _run(_requests(("initialize", {}), ("restart", {}),
                              ("disconnect", {})))
    bad = _by(messages, command="restart")[0]
    assert bad["success"] is False and "unsupported" in bad["message"]


def test_parse_client_value():
    assert _parse_client_value("NULL") is None
    assert _parse_client_value("42") == 42
    assert _parse_client_value("1.5") == 1.5
    assert _parse_client_value("'abc'") == "abc"
    assert _parse_client_value("texto ção") == "texto ção"
