# -*- coding: utf-8 -*-
"""Debug Adapter Protocol (DAP) server for the T-SQL Fabric debugger.

Lets any DAP client (VS Code, nvim-dap, ...) debug a .sql procedure visually:
gutter breakpoints (condition and hit count included), step over/into/out,
variables pane, hover/REPL evaluation and CATCH-handled-error breakpoints —
all backed by the same TSQLDebugger engine, ROLLBACK guarantees included.

Run (usually from a client launch configuration, not by hand):

    tsql-fabric-dap

VS Code launch.json example:

    {
      "type": "tsql-fabric",            // via a generic DAP bridge extension
      "request": "launch",
      "program": "${file}",             // the .sql with CREATE PROCEDURE
      "procName": null,                 // or a deployed procedure name instead
      "params": {"@numAnoRef": 2015},
      "server": "<endpoint>.datawarehouse.fabric.microsoft.com",
      "database": "my_warehouse",
      "stopOnEntry": true
    }

The adapter is synchronous and single-session: one launch = one debugger =
one warehouse session, ROLLBACK on disconnect (commit is NEVER issued here).
"""

import json
import sys

from .engine import TSQLDebugger


class DapServer:
    """A minimal, synchronous DAP server over binary in/out streams."""

    THREAD_ID = 1
    VARS_REF = 1

    def __init__(self, rin, rout):
        self._in = rin
        self._out = rout
        self._seq = 0
        self._root = None          # the launched TSQLDebugger
        self._source_path = None   # .sql path shown back to the client
        self._launch_kwargs = None
        self._pending_breaks = []  # set before launch completes
        self._stop_on_entry = True
        self._configured = False
        self._running = True

    # -- wire protocol ------------------------------------------------------
    def _read_message(self):
        length = None
        while True:
            line = self._in.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                break
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
        if length is None:
            return None
        body = self._in.read(length)
        if not body:
            return None
        return json.loads(body.decode("utf-8"))

    def _send(self, payload):
        self._seq += 1
        payload["seq"] = self._seq
        data = json.dumps(payload).encode("utf-8")
        self._out.write(b"Content-Length: %d\r\n\r\n" % len(data))
        self._out.write(data)
        self._out.flush()

    def _respond(self, request, body=None, success=True, message=None):
        payload = {"type": "response", "request_seq": request["seq"],
                   "command": request["command"], "success": success}
        if body is not None:
            payload["body"] = body
        if message is not None:
            payload["message"] = message
        self._send(payload)

    def _event(self, event, body=None):
        payload = {"type": "event", "event": event}
        if body is not None:
            payload["body"] = body
        self._send(payload)

    def _output(self, text):
        self._event("output", {"category": "console", "output": text + "\n"})

    # -- debugger plumbing --------------------------------------------------
    def _active(self):
        """The deepest debugger in the nested-EXEC chain (where the cursor is)."""
        node = self._root
        while node is not None and node._child is not None:
            node = node._child
        return node

    def _finished(self):
        dbg = self._active()
        return (self._root is None
                or (dbg is self._root and (dbg._finished or dbg._pos >= len(dbg._steps))))

    def _stopped(self, reason, text=None):
        body = {"reason": reason, "threadId": self.THREAD_ID,
                "allThreadsStopped": True}
        if text:
            body["text"] = text
        self._event("stopped", body)

    def _terminate(self):
        if self._root is not None:
            try:
                self._root.close()
            except Exception:
                pass
            self._root = None
        self._event("terminated")
        self._event("exited", {"exitCode": 0})

    def _errors(self):
        dbg = self._active()
        if dbg is None:
            return 0
        return sum(1 for e in dbg._log if e["status"] == "ERROR")

    def _after_execution(self, errors_before=0):
        """Emit the right event after a step/continue operation."""
        dbg = self._active()
        new_error = dbg is not None and self._errors() > errors_before
        if new_error and (dbg._finished or dbg._stop_on_error == "any"):
            # unhandled error ended the run, or stop_on_error="any" paused on
            # a CATCH-handled one — either way, let the client inspect state
            last = next(e for e in reversed(dbg._log) if e["status"] == "ERROR")
            self._stopped("exception", text=last.get("error"))
            return
        if self._finished():
            self._terminate()
            return
        if dbg._break_resume is not None:
            self._stopped("breakpoint")
        else:
            self._stopped("step")

    # -- request handlers ---------------------------------------------------
    def serve(self):
        while self._running:
            request = self._read_message()
            if request is None:
                break
            if request.get("type") != "request":
                continue
            handler = getattr(self, "_on_" + request["command"], None)
            if handler is None:
                self._respond(request, success=False,
                              message=f"unsupported request: {request['command']}")
                continue
            try:
                handler(request)
            except Exception as exc:      # a broken request must not kill the session
                self._respond(request, success=False, message=str(exc))

    def _on_initialize(self, request):
        self._respond(request, body={
            "supportsConfigurationDoneRequest": True,
            "supportsConditionalBreakpoints": True,
            "supportsHitConditionalBreakpoints": True,
            "supportsEvaluateForHovers": True,
            "supportsSetVariable": True,
            "exceptionBreakpointFilters": [
                {"filter": "caught", "label": "Erros tratados por CATCH",
                 "default": False},
            ],
        })
        self._event("initialized")

    def _on_launch(self, request):
        args = request.get("arguments", {})
        self._source_path = args.get("program")
        self._stop_on_entry = bool(args.get("stopOnEntry", True))
        kwargs = dict(params=args.get("params") or {},
                      server=args.get("server"), database=args.get("database"),
                      echo=self._output)
        if args.get("procName"):
            kwargs["proc_name"] = args["procName"]
        else:
            kwargs["sql_file"] = self._source_path
        self._root = TSQLDebugger(**kwargs)
        self._respond(request)

    def _on_setBreakpoints(self, request):
        args = request.get("arguments", {})
        dbg = self._root
        results = []
        if dbg is not None:
            dbg.clear_breaks()
            for bp in args.get("breakpoints", []):
                hits = None
                if bp.get("hitCondition"):
                    try:
                        hits = int(str(bp["hitCondition"]).lstrip(">= "))
                    except ValueError:
                        hits = None
                dbg.break_at(bp["line"], condition=bp.get("condition"), hits=hits)
                results.append({"verified": True, "line": bp["line"]})
        self._respond(request, body={"breakpoints": results})

    def _on_setExceptionBreakpoints(self, request):
        filters = request.get("arguments", {}).get("filters", [])
        if self._root is not None:
            self._root._stop_on_error = "any" if "caught" in filters else True
        self._respond(request)

    def _on_configurationDone(self, request):
        self._configured = True
        self._respond(request)
        if self._stop_on_entry:
            self._stopped("entry")
        else:
            before = self._errors()
            self._root.run_all()
            self._after_execution(before)

    def _on_threads(self, request):
        self._respond(request, body={"threads": [
            {"id": self.THREAD_ID, "name": "T-SQL"}]})

    def _on_stackTrace(self, request):
        frames = []
        if self._root is not None:
            raw = []
            node = self._root
            while node is not None:
                raw.append(node)
                node = node._child
            for i, dbg in enumerate(reversed(raw)):     # innermost first
                if dbg._pos < len(dbg._steps):
                    step = dbg._steps[dbg._pos]
                    line = step["line"]
                    blocks = step.get("frames", [])
                    name = dbg.proc_name + (f" — in {blocks[-1]}" if blocks else "")
                else:
                    line, name = 1, dbg.proc_name + " (finished)"
                frame = {"id": i + 1, "name": name, "line": line, "column": 1}
                if self._source_path and dbg is self._root:
                    frame["source"] = {"path": self._source_path}
                frames.append(frame)
        self._respond(request, body={"stackFrames": frames,
                                     "totalFrames": len(frames)})

    def _on_scopes(self, request):
        self._respond(request, body={"scopes": [
            {"name": "Variáveis", "variablesReference": self.VARS_REF,
             "expensive": False}]})

    def _on_variables(self, request):
        out = []
        dbg = self._active()
        if dbg is not None:
            for key in dbg._vars:
                name = dbg._vars[key]["name"]
                if dbg._vars[key]["table"]:
                    value = "<table variable>"
                else:
                    value = repr(dbg._env.get(key))
                out.append({"name": name, "value": value, "variablesReference": 0})
            out.append({"name": "@@ROWCOUNT",
                        "value": repr(dbg._env.get("@@ROWCOUNT")),
                        "variablesReference": 0})
        self._respond(request, body={"variables": out})

    def _on_setVariable(self, request):
        args = request.get("arguments", {})
        dbg = self._active()
        value = _parse_client_value(args.get("value", ""))
        dbg.set_var(args["name"], value)
        self._respond(request, body={"value": repr(value)})

    def _on_evaluate(self, request):
        args = request.get("arguments", {})
        dbg = self._active()
        value = dbg.eval(args.get("expression", ""))
        self._respond(request, body={"result": repr(value), "variablesReference": 0})

    def _on_continue(self, request):
        self._respond(request, body={"allThreadsContinued": True})
        before = self._errors()
        self._active().run_all()
        self._after_execution(before)

    def _on_next(self, request):
        self._respond(request)
        before = self._errors()
        self._active().step()
        self._after_execution(before)

    def _on_stepIn(self, request):
        self._respond(request)
        before = self._errors()
        self._active().step_into()
        self._after_execution(before)

    def _on_stepOut(self, request):
        self._respond(request)
        before = self._errors()
        self._active().step_out()
        self._after_execution(before)

    def _on_disconnect(self, request):
        if self._root is not None:
            try:
                self._root.close()
            except Exception:
                pass
            self._root = None
        self._respond(request)
        self._running = False


def _parse_client_value(text):
    """Best-effort conversion of the string a DAP client sends for setVariable."""
    s = text.strip()
    if s.upper() in ("NULL", "NONE"):
        return None
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def main():
    DapServer(sys.stdin.buffer, sys.stdout.buffer).serve()


if __name__ == "__main__":
    main()
