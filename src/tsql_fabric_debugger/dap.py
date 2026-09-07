# -*- coding: utf-8 -*-
"""Debug Adapter Protocol (DAP) server for the T-SQL Fabric debugger.

Lets any DAP client (VS Code through a generic DAP bridge extension, nvim-dap,
...) debug a .sql procedure visually: gutter breakpoints (condition and
hit-count included), step over/into/out, variables pane, REPL evaluation and
CATCH-handled-error breakpoints — all backed by the same TSQLDebugger engine,
ROLLBACK guarantees included.

Run (usually from a client launch configuration, not by hand):

    tsql-fabric-dap

VS Code launch.json example (through a DAP bridge extension that spawns the
adapter executable — e.g. one where `debuggerPath` names the command):

    {
      "name": "Debug T-SQL procedure",
      "type": "tsql-fabric",            // the type your DAP bridge registers
      "request": "launch",
      "program": "${file}",             // the .sql with CREATE PROCEDURE
      "procName": null,                 // or a deployed procedure name instead
      "params": {"@year": 2015},
      "server": "<endpoint>.datawarehouse.fabric.microsoft.com",
      "database": "my_warehouse",
      "stopOnEntry": true,
      "maxLoopIterations": 1000,        // optional engine pass-throughs
      "historyBatches": 200,
      "logLevel": "simple"
    }

Behavior notes:

- The adapter is synchronous and single-session: one launch = one debugger =
  one warehouse session, and it NEVER commits — disconnect (or the client
  going away) rolls back. While a `continue` is executing on the server no
  other request is processed (there is no `pause`); bound long loops with
  breakpoints or `maxLoopIterations`.
- Hovering an expression does NOT execute it on the server: hover evaluation
  answers only for plain @variables, from the captured state. The debug
  console (REPL) does execute, with `eval()` semantics — a failing expression
  reports as an error and, like any server error in the transaction, rolls
  back prior data effects.
- Breakpoints apply to the launched procedure's source. A child debugger
  opened by stepping into an EXEC has its own source text — line breakpoints
  do not transfer into it.
"""

import json
import re
import sys

from .engine import TSQLDebugger

_INT_RE = re.compile(r"[+-]?\d+$")
_FLOAT_RE = re.compile(r"[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
_HIT_RE = re.compile(r"(>=|==|=|>)?\s*(\d+)$")


class DapServer:
    """A minimal, synchronous DAP server over binary in/out streams."""

    THREAD_ID = 1

    def __init__(self, rin, rout):
        self._in = rin
        self._out = rout
        self._seq = 0
        self._root = None            # the launched TSQLDebugger
        self._source_path = None     # .sql path shown back to the client
        self._pending_breaks = None  # setBreakpoints args seen before launch
        self._pending_filters = []   # setExceptionBreakpoints seen before launch
        self._stop_on_entry = True
        self._configured = False     # configurationDone seen
        self._running = True
        self._frame_nodes = {}       # frameId -> debugger (from stackTrace)
        self._launch_args = None     # last launch args, for Restart after end

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
        request["_responded"] = True
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
        self._event("output", {"category": "console", "output": str(text) + "\n"})

    # -- debugger plumbing --------------------------------------------------
    def _active(self):
        """The debugger holding the cursor: the deepest UNFINISHED child.

        A finished child is skipped — its parent is active, and the parent's
        next step()/run_all() collects the child's OUTPUT values (the engine
        does that on its own).
        """
        node = self._root
        while (node is not None and node._child is not None
               and not node._child_done()):
            node = node._child
        return node

    def _chain(self):
        out = []
        node = self._root
        while node is not None:
            out.append(node)
            node = node._child
        return out

    def _finished(self):
        dbg = self._active()
        return (self._root is None
                or (dbg is self._root
                    and (dbg._finished or dbg._pos >= len(dbg._steps))
                    and dbg._child is None))

    def _stopped(self, reason, text=None):
        body = {"reason": reason, "threadId": self.THREAD_ID,
                "allThreadsStopped": True}
        if text:
            body["text"] = text
        self._event("stopped", body)

    def _close_root(self):
        # Detach first, so teardown stays idempotent even if close() is cut
        # short by a signal (SystemExit/KeyboardInterrupt are BaseException,
        # not Exception): a second pass finds self._root already None.
        root, self._root = self._root, None
        if root is not None:
            try:
                root.close()                # ROLLBACK — the adapter never commits
            except Exception:
                pass

    def _terminate(self):
        self._close_root()
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

    def _require_root(self, request):
        if self._root is None:
            self._respond(request, success=False,
                          message="no active debug session (launch first)")
            return False
        return True

    def _run_op(self, request, op, body=None):
        """Execute one engine operation, then respond, then emit events.

        Ordering matters: exactly one response per request, and a fatal
        engine failure (connection death, internal error) turns into a
        failure response plus session teardown — never a hung client.
        """
        if not self._require_root(request):
            return
        active = self._active()
        before = self._errors()
        log_before = len(active._log) if active is not None else 0
        try:
            op(self._active())
        except Exception as exc:
            self._respond(request, success=False, message=str(exc))
            self._terminate()
            return
        self._respond(request, body=body)
        self._emit_result_sets(log_before)
        self._after_execution(before)

    def _emit_result_sets(self, log_before):
        """Surface any result sets the procedure produced in the steps that
        just ran: printed to the Debug Console AND sent as a custom event so
        the extension can show them in a grid."""
        dbg = self._active()
        if dbg is None:
            return
        for entry in dbg._log[log_before:]:
            if not entry.get("result_sets"):
                continue
            for rs in dbg._details.get(entry["step"], {}).get("resultsets", []):
                cols, rows = rs["columns"], rs["rows"]
                self._output(_format_table(cols, rows, rs["truncated"],
                                           entry["line"]))
                self._event("tsqlFabricResultSet", {
                    "line": entry["line"], "columns": cols,
                    "rows": [[_json_safe(v) for v in r] for r in rows],
                    "truncated": rs["truncated"],
                })

    # -- request handlers ---------------------------------------------------
    def serve(self):
        try:
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
                except Exception as exc:  # a broken request must not kill the session
                    if not request.get("_responded"):
                        self._respond(request, success=False, message=str(exc))
        finally:
            self._close_root()            # EOF/kill: never leave an open session

    def _on_initialize(self, request):
        self._respond(request, body={
            "supportsConfigurationDoneRequest": True,
            "supportsConditionalBreakpoints": True,
            "supportsHitConditionalBreakpoints": True,
            "supportsLogPoints": True,
            "supportsEvaluateForHovers": True,
            "supportsSetVariable": True,
            "supportsRestartRequest": True,
            "supportsTerminateRequest": True,
            "supportsExceptionInfoRequest": True,
            "supportsCompletionsRequest": True,
            "completionTriggerCharacters": ["@"],
            "exceptionBreakpointFilters": [
                {"filter": "caught", "label": "CATCH-handled errors",
                 "default": False},
            ],
        })
        self._event("initialized")

    def _build_root(self, args):
        """Create the root debugger from launch arguments."""
        self._source_path = args.get("program")
        self._stop_on_entry = bool(args.get("stopOnEntry", True))
        kwargs = dict(params=args.get("params") or {},
                      server=args.get("server"), database=args.get("database"),
                      echo=self._output)
        if args.get("procName"):
            kwargs["proc_name"] = args["procName"]
        else:
            kwargs["sql_file"] = self._source_path
        for launch_key, ctor_key in (("maxLoopIterations", "max_loop_iterations"),
                                     ("historyBatches", "history_batches"),
                                     ("logLevel", "log_level"),
                                     ("stepTimeout", "step_timeout"),
                                     ("lockTimeout", "lock_timeout")):
            if args.get(launch_key) is not None:
                kwargs[ctor_key] = args[launch_key]
        return TSQLDebugger(**kwargs)

    def _on_launch(self, request):
        args = request.get("arguments", {})
        self._close_root()               # a second launch must not leak a session
        self._launch_args = args         # kept so Restart can relaunch after end
        self._root = self._build_root(args)
        if self._pending_filters:
            self._apply_exception_filters(self._pending_filters)
        if self._pending_breaks is not None:
            self._apply_breakpoints(self._pending_breaks)
        self._respond(request)
        if self._configured:             # configurationDone arrived pre-launch
            self._start_debuggee()

    def _parse_hit_condition(self, raw):
        """Return (hits, note). Supported: N, =N, ==N, >=N (fire from the Nth
        pass on) and >N (from the N+1th). Anything else is ignored, noted."""
        m = _HIT_RE.match(str(raw).strip())
        if not m:
            return None, f"unsupported hitCondition {raw!r} ignored"
        n = int(m.group(2))
        if m.group(1) == ">":
            n += 1
        if n < 1:
            return None, f"hitCondition {raw!r} ignored (must be >= 1)"
        return n, None

    def _apply_breakpoints(self, args):
        """Validate, then swap the breakpoint set; per-line hit counts of
        unchanged breakpoints survive (clients re-send all on any change)."""
        dbg = self._root
        requested = args.get("breakpoints", [])
        results = []
        plan = []       # (line, condition, hits) for real breakpoints
        logplan = []    # (line, expr) for logpoints
        for bp in requested:
            line = bp.get("line")
            log_message = bp.get("logMessage")
            if log_message is not None:
                # a logpoint (diamond): print without stopping. VS Code sends a
                # message with {expr} placeholders — the engine evaluates each
                # {expr} and prints the rest as literal text (so a plain
                # "reached here" never runs on the server).
                try:
                    for e in re.findall(r"\{([^}]+)\}", log_message):
                        dbg._validate_expr(e.strip(), "logpoint")
                except ValueError as exc:
                    results.append({"verified": False, "line": line,
                                    "message": str(exc)})
                    continue
                logplan.append((line, log_message))
                results.append({"verified": True, "line": line})
                continue
            condition = bp.get("condition")
            note = None
            hits = None
            if bp.get("hitCondition"):
                hits, note = self._parse_hit_condition(bp["hitCondition"])
            if condition is not None:
                try:
                    dbg._validate_expr(condition, "breakpoint condition")
                except ValueError as exc:
                    results.append({"verified": False, "line": line,
                                    "message": str(exc)})
                    continue
            plan.append((line, condition, hits))
            entry = {"verified": True, "line": line}
            if note:
                entry["message"] = note
            results.append(entry)
        previous = dbg.breaks()
        dbg.clear_breaks()
        dbg.clear_logpoints()
        for line, condition, hits in plan:
            dbg.break_at(line, condition=condition, hits=hits)
            old = previous.get(line)
            if (old and old["condition"] == condition and old["hits"] == hits
                    and not old["once"]):
                dbg._breaks[line]["count"] = old["count"]
        for line, message in logplan:
            dbg.log_at(line, message=message)
        return results

    def _on_setBreakpoints(self, request):
        args = request.get("arguments", {})
        if self._root is None:
            # nvim-dap (and VS Code) send breakpoints on `initialized`, before
            # launch — queue them and confirm; launch applies them for real
            self._pending_breaks = args
            results = [{"verified": True, "line": bp.get("line")}
                       for bp in args.get("breakpoints", [])]
            self._respond(request, body={"breakpoints": results})
            return
        # remember the latest set so Restart (which relaunches without a fresh
        # setBreakpoints from the client) can reapply it
        self._pending_breaks = args
        self._respond(request, body={"breakpoints": self._apply_breakpoints(args)})

    def _apply_exception_filters(self, filters):
        mode = "any" if "caught" in filters else True
        for node in self._chain():       # an active child must honor it too
            node._stop_on_error = mode

    def _on_setExceptionBreakpoints(self, request):
        filters = request.get("arguments", {}).get("filters", [])
        self._pending_filters = filters
        if self._root is not None:
            self._apply_exception_filters(filters)
        self._respond(request)

    def _start_debuggee(self):
        if self._stop_on_entry:
            self._stopped("entry")
        else:
            before = self._errors()
            log_before = len(self._root._log)
            self._root.run_all()
            self._emit_result_sets(log_before)
            self._after_execution(before)

    def _on_configurationDone(self, request):
        self._configured = True
        self._respond(request)
        if self._root is not None:
            self._start_debuggee()

    def _on_threads(self, request):
        self._respond(request, body={"threads": [
            {"id": self.THREAD_ID, "name": "T-SQL"}]})

    def _on_stackTrace(self, request):
        frames = []
        self._frame_nodes = {}
        if self._root is not None:
            for i, dbg in enumerate(reversed(self._chain())):  # innermost first
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
                self._frame_nodes[i + 1] = dbg
                frames.append(frame)
        self._respond(request, body={"stackFrames": frames,
                                     "totalFrames": len(frames)})

    def _node_for_frame(self, frame_id):
        return self._frame_nodes.get(frame_id) or self._active()

    def _on_scopes(self, request):
        frame_id = request.get("arguments", {}).get("frameId", 0)
        self._respond(request, body={"scopes": [
            {"name": "Variables", "variablesReference": frame_id or 1,
             "expensive": False}]})

    def _on_variables(self, request):
        ref = request.get("arguments", {}).get("variablesReference", 0)
        out = []
        dbg = self._node_for_frame(ref)
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
        if not self._require_root(request):
            return
        dbg = self._node_for_frame(args.get("variablesReference", 0))
        value = _parse_client_value(args.get("value", ""))
        dbg.set_var(args["name"], value)
        self._respond(request, body={"value": repr(value)})

    def _on_evaluate(self, request):
        args = request.get("arguments", {})
        if not self._require_root(request):
            return
        dbg = self._node_for_frame(args.get("frameId", 0))
        expression = (args.get("expression") or "").strip()
        if args.get("context") == "hover":
            # a hover must NEVER touch the server (a failing expression would
            # roll back the transaction's data effects) — answer variables
            # only, from the already-captured state
            key = expression.upper()
            if key in dbg._vars and not dbg._vars[key]["table"]:
                self._respond(request, body={"result": repr(dbg._env.get(key)),
                                             "variablesReference": 0})
            else:
                self._respond(request, success=False,
                              message="hover shows variables only; use the "
                                      "debug console to evaluate expressions")
            return
        log_before = len(dbg._log)
        value = dbg.eval(expression)
        failed = any(e["kind"] == "eval" and e["status"] == "ERROR"
                     for e in dbg._log[log_before:])
        if failed:
            err = next(e for e in reversed(dbg._log) if e["status"] == "ERROR")
            self._respond(request, success=False,
                          message=f"eval failed: {err.get('error')} (prior data "
                                  "effects were rolled back; variables survive)")
            return
        self._respond(request, body={"result": repr(value),
                                     "variablesReference": 0})

    def _on_continue(self, request):
        self._run_op(request, lambda dbg: dbg.run_all(),
                     body={"allThreadsContinued": True})

    def _on_next(self, request):
        self._run_op(request, lambda dbg: dbg.step())

    def _on_stepIn(self, request):
        self._run_op(request, lambda dbg: dbg.step_into())

    def _on_stepOut(self, request):
        self._run_op(request, lambda dbg: dbg.step_out())

    def _on_restart(self, request):
        try:
            if self._root is not None:
                self._root.reset()          # replay in the same session
            elif self._launch_args is not None:
                # the run finished (terminated); relaunch from the saved args
                # and reapply the exception filters and breakpoints/logpoints
                self._root = self._build_root(self._launch_args)
                if self._pending_filters:
                    self._apply_exception_filters(self._pending_filters)
                if self._pending_breaks is not None:
                    self._apply_breakpoints(self._pending_breaks)
            else:
                self._respond(request, success=False, message="nothing to restart")
                return
        except Exception as exc:
            self._respond(request, success=False, message=str(exc))
            self._terminate()
            return
        self._respond(request)
        self._start_debuggee()   # honors stopOnEntry, exactly like launch

    def _on_terminate(self, request):
        self._respond(request)
        self._terminate()        # ROLLBACK + terminated

    def _on_exceptionInfo(self, request):
        dbg = self._active()
        err = dbg.last_error() if dbg is not None else None
        if err is None:
            self._respond(request, success=False, message="no exception recorded")
            return
        message = err.get("error") or "T-SQL error"
        self._respond(request, body={
            "exceptionId": "T-SQL error",
            "description": message,
            "breakMode": "always",
            "details": {"message": message, "fullTypeName": "T-SQL error"},
        })

    def _on_completions(self, request):
        # suggest the session's @variables in the Debug Console / REPL
        dbg = self._active()
        targets = []
        if dbg is not None:
            for key in dbg._vars:
                targets.append({"label": dbg._vars[key]["name"], "type": "variable"})
            targets.append({"label": "@@ROWCOUNT", "type": "variable"})
        self._respond(request, body={"targets": targets})

    def _on_disconnect(self, request):
        self._close_root()
        self._respond(request)
        self._running = False


def _json_safe(value):
    """Make a captured cell JSON-serializable for the custom event."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _format_table(columns, rows, truncated, line):
    """A compact fixed-width table for the Debug Console."""
    cells = [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = [len(c) for c in columns]
    for r in cells:
        for i, v in enumerate(r):
            widths[i] = max(widths[i], len(v))
    widths = [min(w, 40) for w in widths]

    def fmt(vals):
        return " | ".join(v[:40].ljust(widths[i]) for i, v in enumerate(vals))

    out = [f"result set (line {line}) — {len(rows)} row(s)"
           + (" [truncated]" if truncated else ""),
           fmt(columns), "-+-".join("-" * w for w in widths)]
    out += [fmt(r) for r in cells]
    return "\n".join(out)


def _parse_client_value(text):
    """Deterministic conversion of the string a DAP client sends for setVariable.

    NULL/None (any case) -> None; 'quoted' or "quoted" -> the inner string;
    strict integer / decimal literals -> int / float (no inf/nan/underscore
    forms); anything else stays a string.
    """
    s = text.strip()
    if s.upper() in ("NULL", "NONE"):
        return None
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    if _INT_RE.fullmatch(s):
        return int(s)
    if _FLOAT_RE.fullmatch(s):
        return float(s)
    return s


def main():
    import signal

    # a polite kill (SIGTERM) must roll the warehouse session back — raise
    # SystemExit so serve()'s finally still runs close()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    DapServer(sys.stdin.buffer, sys.stdout.buffer).serve()


if __name__ == "__main__":
    main()
