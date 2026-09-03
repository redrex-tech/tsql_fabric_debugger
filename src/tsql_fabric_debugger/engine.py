# -*- coding: utf-8 -*-
"""Debugger engine: step-by-step execution with preserved state.

Since the Fabric Warehouse has no T-SQL debugger and DECLARE variables die at
the end of each batch, every step runs as an independent batch on the same
session:

    DECLARE <all variables>;
    SELECT @a = ?, @b = ?, ...;      -- state re-injection (pyodbc parameters)
    <original statement, untouched>;
    SELECT '__hcap__', @@ROWCOUNT, @a, @b, ...;   -- capture of the new state

Python keeps the "stack frame" between steps. @@ROWCOUNT and the ERROR_*()
family are rewritten as parameters to preserve cross-batch semantics, and the
procedure's BEGIN CATCH is emulated when a step inside its TRY fails —
including the T-SQL rules that execution continues after END CATCH and that
the whole failed TRY (nested TRYs included) is skipped.

Everything runs inside a transaction with ROLLBACK at the end by default —
nothing persists in the Warehouse unless close(commit=True).
"""

import re
import time

from .connection import connect
from .parser import (
    eval_literal,
    extract_declares,
    find_procedure,
    parse_params,
    procedure_body,
    read_sql_file,
    rewrite,
    scan_declares,
    scan_transaction_controls,
    split_steps,
)
from .scanner import is_word, scan

_SENTINEL = "__hcap__"


class TSQLDebugger:
    """Interactive step-by-step debugger for a T-SQL procedure on Fabric.

    Typical flows:
      - Sequential: step() / run_until(n) / run_all(), inspecting with
        show_vars() and sql() between steps.
      - Inside blocks: step_into() evaluates the IF/WHILE condition
        server-side and expands the chosen branch into individual sub-steps.
      - Specific step: jump_to(n) or run_step(n), building any missing state
        with set_var().
      - Diagnosis: set_log_level("full") and show_detail() to see the whole
        command, the error and the executed SQL batch.

    Main parameters: sql_file OR sql_text (the original procedure,
    untouched), params (test values, e.g. {"@year": 2015}), server and
    database (or the FABRIC_TSQL_SERVER/FABRIC_TSQL_DATABASE env vars).
    echo redirects console output (default: print).

    Prefer the context-manager form — it guarantees ROLLBACK + close even on
    exceptions, so no orphan session is left holding locks on the warehouse:

        with TSQLDebugger("proc.sql", params={...}) as dbg:
            dbg.run_all()

    Notes: instances are NOT thread-safe (one pyodbc session, one shared
    environment). Step NUMBERS shown by list_steps() change after a
    step_into() expansion — re-list before using jump_to()/run_until().
    show_detail()/last_results() take LOG entry numbers, which differ from
    step numbers (the log also records cond/params/catch entries).
    """

    def __init__(self, sql_file: str | None = None, sql_text: str | None = None,
                 params: dict | None = None,
                 server: str | None = None, database: str | None = None, autocommit: bool = False,
                 log_level: str = "simple", stop_on_error: bool = True,
                 preview_chars: int = 500, max_loop_iterations: int = 1000,
                 step_timeout: int | None = None, max_result_rows: int = 50, echo=print):
        if sql_text is None:
            if sql_file is None:
                raise ValueError("Provide sql_file or sql_text.")
            sql_text = read_sql_file(sql_file)
        self._sql = sql_text
        self._server = server
        self._database = database
        self._autocommit = autocommit
        self._log_level = log_level.lower()          # "simple" | "full"
        self._stop_on_error = stop_on_error
        self._preview_chars = preview_chars
        self._max_loop_iterations = max_loop_iterations
        self._step_timeout = step_timeout            # seconds per step (None = unlimited)
        self._max_result_rows = max_result_rows      # rows captured per procedure result set
        self._echo = echo
        self._conn = None
        self._cursor = None
        self._log = []
        self._details = {}   # log step -> {"text","batch","resultsets","captured","changed","raw_error"}
        self._error_msg = None
        self._error_number = None
        self._error_line = None
        self._finished = False
        self._rolled_back = False
        self._warned_post_rollback = False
        self._pos = 0

        tokens = scan(sql_text)
        self._tokens = tokens
        i_proc = find_procedure(tokens)
        if i_proc is None:
            raise ValueError("CREATE PROCEDURE not found — use run_script() for loose scripts.")
        self.proc_name, self._param_defs, i_as = parse_params(sql_text, tokens, i_proc)
        i0, i1 = procedure_body(tokens, i_as)

        ctx = {"catches": [], "span_ids": {}}
        self._steps = split_steps(sql_text, tokens, i0, i1, ctx)
        self._catches = ctx["catches"]     # one CATCH block per TRY, in body order
        self._span_ids = ctx["span_ids"]   # keeps catch registration idempotent on re-slicing

        # does the procedure manage its own transaction? The debugger's
        # rollback-by-default cannot undo whatever an inner COMMIT persists —
        # including one hidden inside dynamic SQL or a child procedure
        controls, exec_lines = scan_transaction_controls(sql_text, tokens, i0, i1)
        if controls:
            spots = ", ".join(f"{keyword} (line {line})" for keyword, line in controls)
            self._echo(f"[WARNING] the procedure manages its own transaction: {spots}. "
                       "An inner COMMIT persists data EVEN WITH the debugger's default "
                       "ROLLBACK — review before running those steps.")
        if exec_lines and not autocommit:
            shown = ", ".join(str(line) for line in exec_lines[:8])
            more = f" (+{len(exec_lines) - 8} more)" if len(exec_lines) > 8 else ""
            self._echo(f"[NOTICE] EXEC call(s) at line(s) {shown}{more}: child procedures "
                       "or dynamic SQL may contain transaction control the parser cannot see.")
        if autocommit:
            self._echo("[WARNING] autocommit=True: every step persists immediately — "
                       "close() will NOT undo anything.")

        # variable registry: parameters + EVERY DECLARE in the body, including
        # inside IF/WHILE blocks and the CATCH — a variable declared inside a
        # block stays visible and capturable in later steps
        self._vars = {}   # UPPER key -> {"name","type","table"}
        self._env = {}    # UPPER key -> Python value
        for param in self._param_defs:
            self._register_var(param["name"], param["type"])
        for var_name, var_type, _ in scan_declares(sql_text, tokens, i0, i1):
            self._register_var(var_name, var_type)
        for step in self._steps:
            if step["kind"] == "declare":
                step["declares"] = extract_declares(step["text"])

        # test values: explicit params win over header defaults
        params = {(k if k.startswith("@") else "@" + k).upper(): v for k, v in (params or {}).items()}
        self._pending_defaults = []
        missing = []
        for param in self._param_defs:
            key = param["name"].upper()
            if key in params:
                self._env[key] = params[key]
            elif param["default"] is not None:
                ok, value = eval_literal(param["default"])
                if ok:
                    self._env[key] = value
                else:
                    self._pending_defaults.append((param["name"], param["default"]))
            elif not param["output"]:
                missing.append(param["name"])
        if missing:
            self._echo(f"[WARNING] Parameters without a test value (will stay NULL): {', '.join(missing)}")

        self._echo(f"Procedure: {self.proc_name} | {len(self._steps)} steps "
                   f"| {sum(len(c) for c in self._catches)} step(s) in {len(self._catches)} "
                   f"CATCH block(s) | {len(self._vars)} variables")

    # -- context manager ----------------------------------------------------
    def __enter__(self) -> "TSQLDebugger":
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close(commit=False)
        return False

    # -- infrastructure -----------------------------------------------------
    def _register_var(self, name, sql_type):
        key = name.upper()
        if key not in self._vars:
            is_table = sql_type is not None and sql_type.strip().upper().startswith("TABLE")
            self._vars[key] = {"name": name, "type": sql_type, "table": is_table}
            self._env[key] = None
            if is_table:
                self._echo(f"[WARNING] {name} is a table variable: its content does NOT survive "
                           "across steps (redeclared empty on every batch). Step over the block "
                           "that fills AND consumes it, or switch to #temp while investigating.")

    def _ensure_connection(self):
        if self._conn is None:
            self._conn = connect(self._server, self._database, autocommit=self._autocommit)
            if self._step_timeout:
                self._conn.timeout = self._step_timeout   # per-command timeout, in seconds
            self._cursor = self._conn.cursor()
            if self._pending_defaults:
                pending, self._pending_defaults = self._pending_defaults, []
                assigns = ", ".join(f"{n} = ({d})" for n, d in pending)
                self._exec_batch(f"SELECT {assigns}", [], f"defaults: {assigns}", "params", 0)
        return self._cursor

    def _safe_rollback(self, announce=False):
        """Rollback that never raises; reports a dead session instead of hiding it."""
        if self._autocommit or self._conn is None:
            return
        try:
            self._conn.rollback()
            self._rolled_back = True
            if announce:
                self._echo("[TRANSACTION] error inside a Fabric transaction — ROLLBACK "
                           "executed (data effects of previous steps undone; captured "
                           "VARIABLES keep their values).")
        except Exception:
            self._echo("[TRANSACTION] rollback FAILED — the session is likely dead; "
                       "close() and start a new debugger.")

    def _build_batch(self, stmt_text, stmt_binds, exclude=frozenset(), extra_capture=None):
        """Build the state-preserving batch: DECLARE + re-injection + statement + capture.

        exclude: variables the statement itself declares (DECLARE inside a
        block) — they leave the initial DECLARE and the re-injection so the
        declaration is not duplicated, but scalar ones stay in the capture,
        since after the statement they exist in the batch.
        """
        order = list(self._vars)
        outer = [k for k in order if k not in exclude]
        # table variables are declared (references must compile) but never
        # re-injected or captured — there is no scalar round-trip for them
        scalars = [k for k in order if not self._vars[k]["table"]]
        declare = "DECLARE " + ", ".join(f"{self._vars[k]['name']} {self._vars[k]['type']}" for k in outer)
        to_inject = [k for k in outer if k in set(scalars) and self._env[k] is not None]
        parts = [declare + ";"] if outer else []
        values = []
        if to_inject:
            parts.append("SELECT " + ", ".join(f"{self._vars[k]['name']} = ?" for k in to_inject) + ";")
            values = [self._env[k] for k in to_inject]
        if stmt_text is not None:
            parts.append(stmt_text + "\n;")
        capture = f"SELECT '{_SENTINEL}' AS [{_SENTINEL}], @@ROWCOUNT AS [__rowcount__]"
        if scalars:
            capture += ", " + ", ".join(f"{self._vars[k]['name']} AS [{k}]" for k in scalars)
        if extra_capture:
            capture += ", " + extra_capture
        parts.append(capture + ";")
        return "\n".join(parts), values + stmt_binds

    def _exec_batch(self, stmt_text, stmt_binds, text, kind, line,
                    exclude=frozenset(), extra_capture=None, update_rowcount=True,
                    session_mode=False):
        cur = self._ensure_connection()
        if session_mode:
            # session SET options (NOCOUNT, XACT_ABORT, ...) must run in an
            # UNPARAMETERIZED batch: pyodbc routes parameterized batches
            # through sp_prepexec, where SET options revert at batch end
            batch = (stmt_text + "\n;\n"
                     + f"SELECT '{_SENTINEL}' AS [{_SENTINEL}], @@ROWCOUNT AS [__rowcount__];")
            values = list(stmt_binds)
        else:
            batch, values = self._build_batch(stmt_text, stmt_binds, exclude, extra_capture)
        started = time.time()
        changed = {}
        resultsets = []
        captured = False
        try:
            if values:
                # Strings must bind as nvarchar(max), never ntext (legacy) —
                # Fabric's UTF-8 collation rejects ntext when re-injecting
                # NVARCHAR(MAX) values
                import pyodbc
                cur.setinputsizes([(pyodbc.SQL_WVARCHAR, 0, 0) if isinstance(v, str) else None
                                   for v in values])
                cur.execute(batch, values)
                cur.setinputsizes(None)
            else:
                cur.execute(batch)
            while True:
                if cur.description and cur.description[0][0] == _SENTINEL:
                    captured = True
                    row = cur.fetchone()
                    columns = [d[0] for d in cur.description]
                    before = dict(self._env)
                    for col, val in zip(columns, row):
                        if col == "__rowcount__":
                            if update_rowcount:
                                self._env["@@ROWCOUNT"] = val
                        elif col != _SENTINEL:
                            self._env[col] = val
                    changed = {self._vars[k]["name"]: self._env[k]
                               for k in self._vars if before.get(k) != self._env[k]}
                    break
                if cur.description:
                    # result set produced by the statement ITSELF (diagnostic
                    # SELECT, sample) — captured, not thrown away
                    columns = [d[0] for d in cur.description]
                    rows = cur.fetchmany(self._max_result_rows + 1)
                    resultsets.append({
                        "columns": columns,
                        "rows": [list(r) for r in rows[:self._max_result_rows]],
                        "truncated": len(rows) > self._max_result_rows,
                    })
                if not cur.nextset():
                    break
            rows_affected = self._env.get("@@ROWCOUNT") if (captured and update_rowcount) else None
            entry = self._record(kind, line, "SUCCESS", text, started, changed, None,
                                 batch=batch, resultsets=resultsets, captured=captured,
                                 rows_affected=rows_affected)
        except KeyboardInterrupt:
            # pyodbc does not abort the server-side statement on SIGINT —
            # cancel it explicitly, then leave the session in a clean state
            try:
                cur.cancel()
            except Exception:
                pass
            self._echo("[INTERRUPTED] step canceled on the server; rolling back.")
            self._safe_rollback()
            raise
        except Exception as exc:
            raw = str(exc)
            self._error_msg, self._error_number = _parse_sql_error(raw)
            self._error_line = line
            entry = self._record(kind, line, "ERROR", text, started, {}, self._error_msg,
                                 batch=batch, resultsets=resultsets, captured=False,
                                 rows_affected=None, raw_error=raw)
            self._safe_rollback(announce=not self._autocommit)
            raise
        return entry

    def _record(self, kind, line, status, text, started, changed, error,
                batch=None, resultsets=None, captured=True, rows_affected=None,
                raw_error=None):
        entry = {
            "step": len(self._log) + 1,
            "line": line,
            "kind": kind,
            "status": status,
            "rows_affected": rows_affected,
            "duration_s": round(time.time() - started, 3),
            "command": " ".join(text[:self._preview_chars].split()),
            "changed_vars": "; ".join(f"{k}={_shorten(v)}" for k, v in changed.items()) or None,
            "result_sets": len(resultsets) if resultsets else 0,
            "post_rollback": self._rolled_back,
            "error": error,
        }
        self._log.append(entry)
        self._details[entry["step"]] = {"text": text, "batch": batch,
                                        "resultsets": resultsets or [], "captured": captured,
                                        "changed": dict(changed), "raw_error": raw_error}
        symbol = {"SUCCESS": "ok", "REGISTERED": "reg"}.get(status, "ERR")
        full = self._log_level == "full"
        if full:
            self._echo(f"[{entry['step']:>3}] line {line:>4} | {symbol:<4} | {kind}")
            for ln in text.rstrip().splitlines():
                self._echo(f"      | {ln}")
        else:
            self._echo(f"[{entry['step']:>3}] line {line:>4} | {symbol:<4} | {entry['command'][:100]}")
        if changed:
            for k, v in changed.items():
                self._echo(f"      -> {k} = {v!r}" if full else f"      -> {k} = {_shorten(v)}")
        for n, rs in enumerate(resultsets or [], start=1):
            extra = " (truncated)" if rs["truncated"] else ""
            self._echo(f"      ~~ result set {n}: {len(rs['rows'])} row(s){extra} | "
                       + ", ".join(rs["columns"]))
            if full:
                for r in rs["rows"][:10]:
                    self._echo(f"      ~~   {r}")
                if len(rs["rows"]) > 10:
                    self._echo(f"      ~~   ... +{len(rs['rows']) - 10} more row(s) — last_results()")
        if error:
            self._echo(f"      !! file line {line} | {error}")
            if full and batch:
                self._echo("      -- SQL batch executed (generated by the debugger):")
                for ln in batch.rstrip().splitlines():
                    self._echo(f"      |  {ln}")
        return entry

    # -- step execution core ------------------------------------------------
    def _is_session_set(self, step):
        """SET of a session option (NOCOUNT, XACT_ABORT, ISOLATION LEVEL...)."""
        if step["kind"] != "stmt":
            return False
        toks = scan(step["text"])
        return (len(toks) >= 2 and is_word(toks[0], "SET")
                and toks[1]["k"] == "w" and not toks[1]["u"].startswith("@"))

    def _bind_for(self, marker, fallback_line=None):
        if marker == "@@ROWCOUNT":
            return self._env.get("@@ROWCOUNT", 0)
        return {
            "ERROR_MESSAGE": self._error_msg,
            "ERROR_NUMBER": self._error_number,
            "ERROR_SEVERITY": 16,   # the driver does not expose it; RAISERROR-compatible default
            "ERROR_STATE": 1,       # idem
            "ERROR_LINE": self._error_line,
            "ERROR_PROCEDURE": self.proc_name,
        }[marker]

    def _execute_step(self, step, prefix=""):
        """Dispatch one step by kind. Raises on SQL errors.

        Returns (log_entry, returned) — returned=True when the step was (or
        contained) a RETURN, i.e. the procedure execution ends here.
        """
        text_for_log = prefix + step["text"]
        kind = step["kind"]
        if kind == "declare":
            declares = step.get("declares")
            if declares is None:
                declares = extract_declares(step["text"])
            inits = [(n, e) for n, t, e in declares
                     if e and not t.strip().upper().startswith("TABLE")]
            if not inits:
                started = time.time()
                return self._record("declare", step["line"], "REGISTERED",
                                    text_for_log, started, {}, None), False
            stmt = "SELECT " + ", ".join(f"{n} = ({e})" for n, e in inits)
            # a DECLARE inside a CATCH may initialize from ERROR_MESSAGE() etc.
            stmt, markers = rewrite(stmt, self._error_msg,
                                    self._env.get("@@ROWCOUNT") is not None)
            binds = [self._bind_for(m) for m in markers]
            return self._exec_batch(stmt, binds, text_for_log, "declare", step["line"]), False
        if kind == "return":
            # RETURN ends the procedure — the debug stops here, just like
            # the real execution would (nothing is sent to the server)
            started = time.time()
            entry = self._record("return", step["line"], "SUCCESS",
                                 text_for_log, started, {}, None)
            self._echo("      RETURN — procedure execution finished.")
            return entry, True
        # atomically executed blocks may contain their own DECLARE:
        # those leave the batch's initial DECLARE to avoid duplication
        exclude = frozenset()
        if kind.endswith("_block"):
            ti0, ti1 = step["ti"]
            exclude = frozenset(n.upper() for n, _, _ in
                                scan_declares(self._sql, self._tokens, ti0, ti1))
        text, markers = rewrite(step["text"], self._error_msg,
                                self._env.get("@@ROWCOUNT") is not None)
        binds = [self._bind_for(m) for m in markers]
        entry = self._exec_batch(text, binds, text_for_log, kind, step["line"],
                                 exclude=exclude,
                                 session_mode=self._is_session_set(step))
        # a RETURN inside an atomic block ends the batch before the capture:
        # missing sentinel + RETURN in the text = the procedure returned
        returned = (not self._details[entry["step"]]["captured"]
                    and any(is_word(t, "RETURN") for t in scan(step["text"])))
        if returned:
            self._echo("      RETURN executed inside the block — execution finished "
                       "(variables assigned in this step were not captured).")
        return entry, returned

    def _run_one(self, step, emulate_catch, finalize):
        log_before = len(self._log)
        try:
            entry, returned = self._execute_step(step)
            if returned and finalize:
                self._finished = True
            return entry
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if len(self._log) == log_before:
                # nothing was recorded: infrastructure or internal failure —
                # never swallow it (a swallowed bug looks like a clean finish)
                self._echo(f"[FATAL] failure outside SQL execution: {exc}")
                raise
            error_entry = self._log[log_before]
            self._handle_step_error(step, emulate_catch, finalize)
            return error_entry

    def _handle_step_error(self, step, emulate_catch, finalize):
        if step["kind"].endswith("_block"):
            self._echo("      Hint: to pinpoint the exact statement inside the block, "
                       "use jump_to(n) + step_into().")
        catch_id = step.get("catch_id")
        catch_steps = (self._catches[catch_id]
                       if emulate_catch and catch_id is not None
                       and catch_id < len(self._catches) else [])
        if catch_steps:
            self._echo(f"[CATCH] error inside TRY #{catch_id + 1} — emulating "
                       f"{len(catch_steps)} step(s) of its CATCH block.")
            ok = self._emulate_catch(catch_steps)
            self._error_msg = self._error_number = self._error_line = None
            if not finalize:
                return
            if self._finished:      # RETURN or THROW inside the CATCH
                return
            if ok:
                # T-SQL semantics: after the CATCH handles the error, execution
                # CONTINUES after END CATCH — skip everything still inside the
                # failed TRY, nested TRY blocks included (catch_ids stack)
                skipped = 0
                while (self._pos < len(self._steps)
                       and catch_id in self._steps[self._pos].get("catch_ids", ())):
                    self._pos += 1
                    skipped += 1
                if skipped:
                    self._echo(f"[CATCH] {skipped} remaining step(s) of TRY #{catch_id + 1} "
                               "skipped; the debug continues after END CATCH.")
                if self._rolled_back and not self._warned_post_rollback:
                    self._warned_post_rollback = True
                    self._echo("[TRANSACTION] note: data effects before the error were rolled "
                               "back — following steps run against the post-rollback state "
                               "and may diverge from a real execution.")
            else:
                self._finished = True
        elif finalize:
            self._finished = self._stop_on_error

    def _emulate_catch(self, catch_steps):
        """Run the CATCH steps. Returns False if the CATCH itself failed."""
        for step in catch_steps:
            first = scan(step["text"])
            if first and is_word(first[0], "THROW"):
                started = time.time()
                self._record("throw", step["line"], "SUCCESS",
                             "[CATCH] " + step["text"], started, {}, None)
                self._echo("      THROW — the original error is re-raised; procedure aborts.")
                self._finished = True
                return True
            try:
                _, returned = self._execute_step(step, prefix="[CATCH] ")
            except KeyboardInterrupt:
                raise
            except Exception:
                return False
            if returned:
                self._echo("      RETURN inside the CATCH — procedure execution finished.")
                self._finished = True
                return True
        return True

    # -- interactive API ----------------------------------------------------
    def list_steps(self) -> None:
        """List the numbered steps without executing anything.

        The '*' marks the current cursor; indentation shows sub-steps created
        by step_into() inside IF/WHILE blocks. Numbers CHANGE after a
        step_into() expansion — re-list before jump_to()/run_until().
        """
        for n, step in enumerate(self._steps, start=1):
            mark = "*" if n == self._pos + 1 else " "
            indent = "  " * step.get("depth", 0)
            self._echo(f"{mark}[{n:>3}] line {step['line']:>4} | {step['kind']:<11} | {indent}"
                       + " ".join(step["text"][:90].split()))
        total_catch = sum(len(c) for c in self._catches)
        if total_catch:
            self._echo(f" ... + {total_catch} step(s) in {len(self._catches)} CATCH block(s) "
                       "(emulated after an error in the matching TRY)")

    def step(self) -> dict | None:
        """Run the next step and advance the cursor ("step over": whole IF/WHILE)."""
        if self._finished or self._pos >= len(self._steps):
            self._echo("Debug finished — all steps executed (or CATCH emulated).")
            return None
        current = self._steps[self._pos]
        self._pos += 1
        return self._run_one(current, emulate_catch=True, finalize=True)

    def step_into(self) -> object:
        """Step into the next step when it is an IF/WHILE block.

        Instead of running the whole block at once (as step() does), it
        evaluates the condition server-side with the current variables, picks
        the branch and queues its inner statements as individual steps — the
        next step() runs the first of them. For WHILE, Python drives the
        loop: the condition is re-evaluated after each iteration (guard:
        max_loop_iterations). On a regular step it behaves like step().
        """
        if self._finished or self._pos >= len(self._steps):
            self._echo("Debug finished — all steps executed (or CATCH emulated).")
            return None
        step = self._steps[self._pos]
        if step["kind"] not in ("if_block", "while_block"):
            return self.step()
        body_text = step["text"].upper()
        if step["is_loop"] and ("BREAK" in body_text or "CONTINUE" in body_text):
            self._echo("[WARNING] WHILE with BREAK/CONTINUE is not supported by step_into — "
                       "running the whole block (step over).")
            return self.step()
        log_before = len(self._log)
        try:
            if step["is_loop"]:
                return self._expand_while(step)
            return self._expand_if(step)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if len(self._log) == log_before or self._log[-1]["status"] != "ERROR":
                self._echo(f"[FATAL] failure outside SQL execution: {exc}")
                raise
            # the condition evaluation failed — same treatment the block
            # would get failing under step(): emulate its CATCH and move on
            if self._pos < len(self._steps) and self._steps[self._pos] is step:
                self._pos += 1
            self._handle_step_error(step, emulate_catch=True, finalize=True)
            return self._log[log_before]

    def run_step(self, n: int, emulate_catch: bool = False) -> dict | None:
        """Run ONLY step n (1-based), with the current variable state.

        Does not move the sequential cursor and — unlike step() — does NOT
        emulate the CATCH on failure unless emulate_catch=True. If the step
        depends on variables from earlier steps that never ran, build the
        state first with set_var().
        """
        if not 1 <= n <= len(self._steps):
            raise ValueError(f"Step {n} outside range 1..{len(self._steps)}.")
        return self._run_one(self._steps[n - 1], emulate_catch=emulate_catch, finalize=False)

    def jump_to(self, n: int) -> None:
        """Place the cursor at step n (1-based) WITHOUT running earlier steps.

        Careful: variables assigned by the skipped steps keep their current
        environment values (use set_var() to build them by hand), and step
        numbers change after a step_into() expansion — re-run list_steps()
        to get current numbers.
        """
        if not 1 <= n <= len(self._steps):
            raise ValueError(f"Step {n} outside range 1..{len(self._steps)}.")
        skipped = n - 1 - self._pos
        self._pos = n - 1
        self._finished = False
        if skipped > 0:
            self._echo(f"[WARNING] {skipped} step(s) skipped — their variable assignments did NOT run.")
        self._echo(f"Cursor at step {n}: " + " ".join(self._steps[n - 1]['text'][:90].split()))

    def set_var(self, name: str, value: object) -> None:
        """Manually assign an environment variable (e.g. before run_step)."""
        key = (name if name.startswith("@") else "@" + name).upper()
        if key != "@@ROWCOUNT" and key not in self._vars:
            known = ", ".join(self._vars[k]["name"] for k in self._vars)
            raise ValueError(f"Variable {name} does not exist in the procedure. Known: {known}")
        if key in self._vars and self._vars[key]["table"]:
            raise ValueError(f"{name} is a table variable — its content cannot be set "
                             "from the debugger.")
        self._env[key] = value
        self._echo(f"{name} = {_shorten(value)}")

    def set_log_level(self, level: str) -> None:
        """Change the log level mid-debug: 'simple' or 'full'."""
        if level.lower() not in ("simple", "full"):
            raise ValueError("Use 'simple' or 'full'.")
        self._log_level = level.lower()
        self._echo(f"Log level: {self._log_level}")

    def show_detail(self, step_no: int | None = None) -> dict | None:
        """Show one logged step in full: command, changed variables (untruncated),
        error (clean and raw), result sets and the executed SQL batch.

        step_no is a LOG entry number (the [ n] on the console), not a step
        number. Without an argument, shows the last entry.
        """
        if not self._log:
            self._echo("No step executed yet.")
            return None
        entry = self._log[step_no - 1] if step_no else self._log[-1]
        detail = self._details.get(entry["step"], {})
        self._echo(f"step {entry['step']} | file line {entry['line']} | "
                   f"{entry['kind']} | {entry['status']} | {entry['duration_s']}s")
        self._echo("-- command (original file):")
        self._echo(detail.get("text") or entry["command"])
        for k, v in (detail.get("changed") or {}).items():
            self._echo(f"-- changed: {k} = {v!r}")
        if entry["error"]:
            self._echo(f"-- error: {entry['error']}")
            raw = detail.get("raw_error")
            if raw and raw.strip() != entry["error"]:
                self._echo(f"-- raw driver error: {raw}")
        for n, rs in enumerate(detail.get("resultsets") or [], start=1):
            extra = " (truncated)" if rs["truncated"] else ""
            self._echo(f"-- result set {n}{extra}: {rs['columns']}")
            for r in rs["rows"]:
                self._echo(f"   {r}")
        if detail.get("batch"):
            self._echo("-- SQL batch executed (generated by the debugger):")
            self._echo(detail["batch"])
        return entry

    def last_results(self, step_no: int | None = None) -> list:
        """Result sets the procedure itself produced in one logged entry.

        Without an argument, uses the last executed entry. Returns a list of
        DataFrames (df.attrs["truncated"] marks a cut at max_result_rows), or
        of {"columns","rows","truncated"} dicts without pandas.
        """
        if not self._log:
            return []
        entry = self._log[step_no - 1] if step_no else self._log[-1]
        resultsets = self._details.get(entry["step"], {}).get("resultsets") or []
        try:
            import pandas as pd
        except ImportError:
            return resultsets
        frames = []
        for rs in resultsets:
            df = pd.DataFrame(rs["rows"], columns=rs["columns"])
            df.attrs["truncated"] = rs["truncated"]
            frames.append(df)
        return frames

    def _span_text(self, token_span):
        t0, t1 = token_span
        return self._sql[self._tokens[t0]["s"]:self._tokens[t1 - 1]["e"]]

    def _eval_condition(self, cond_text, line):
        """Evaluate an IF/WHILE condition server-side with the current variables."""
        text, markers = rewrite(cond_text, self._error_msg,
                                self._env.get("@@ROWCOUNT") is not None)
        binds = [self._bind_for(m) for m in markers]
        self._exec_batch(None, binds, f"condition: {cond_text}", "cond", line,
                         extra_capture=f"CASE WHEN {text} THEN 1 ELSE 0 END AS [__cond__]",
                         update_rowcount=False)
        result = bool(self._env.pop("__cond__", 0))
        self._echo(f"      -> condition = {result}")
        return result

    def _sub_steps(self, body_span, parent):
        """Slice a branch body into individual steps, inheriting the parent context.

        The ctx points at the debugger's real catches list (with the span
        registry): a BEGIN TRY nested inside the expanded branch registers
        its own CATCH exactly once, even across WHILE iterations.
        """
        b0, b1 = body_span
        ctx = {"catches": self._catches, "span_ids": self._span_ids}
        subs = split_steps(self._sql, self._tokens, b0, b1, ctx,
                           catch_stack=tuple(parent.get("catch_ids", ())))
        for sub in subs:
            sub["depth"] = parent.get("depth", 0) + 1
        return subs

    def _expand_if(self, step):
        chosen = None
        for branch in step["branches"]:
            if branch["cond"] is None:
                self._echo("      -> ELSE branch")
                chosen = branch
                break
            if self._eval_condition(" ".join(self._span_text(branch["cond"]).split()),
                                    step["line"]):
                chosen = branch
                break
        del self._steps[self._pos]
        if chosen is None:
            self._echo("No condition was true and there is no ELSE — the block does nothing.")
            return None
        subs = self._sub_steps(chosen["body"], step)
        self._steps[self._pos:self._pos] = subs
        self._echo(f"Block expanded into {len(subs)} sub-step(s) — step() runs the first one.")
        return subs

    def _expand_while(self, step):
        iteration = step.get("iteration", 1)
        branch = step["branches"][0]
        result = self._eval_condition(" ".join(self._span_text(branch["cond"]).split()),
                                      step["line"])
        del self._steps[self._pos]
        if not result:
            self._echo(f"WHILE condition is false — loop ended after {iteration - 1} iteration(s).")
            return None
        if iteration > self._max_loop_iterations:
            self._echo(f"[ABORTED] WHILE exceeded max_loop_iterations={self._max_loop_iterations}.")
            return None
        subs = self._sub_steps(branch["body"], step)
        next_round = dict(step)
        next_round["iteration"] = iteration + 1
        self._steps[self._pos:self._pos] = subs + [next_round]
        self._echo(f"Iteration {iteration}: {len(subs)} sub-step(s) queued; "
                   f"the WHILE re-evaluates afterwards.")
        return subs

    def run_all(self) -> object:
        """Run to the end (or until an error, with the CATCH emulated)."""
        while not self._finished and self._pos < len(self._steps):
            self.step()
        return self.log_df()

    def run_until(self, n: int) -> object:
        """Run up to step n (inclusive) — the 'breakpoint'.

        Step numbers change after step_into() expansions; re-run list_steps()
        for current numbers.
        """
        while not self._finished and self._pos < min(n, len(self._steps)):
            self.step()
        return self.log_df()

    def show_vars(self) -> dict:
        """Print and return the current variable state (OUTPUT params included).

        Table variables appear with a sentinel string — their content is not
        tracked across steps.
        """
        state = {}
        for k in self._vars:
            name = self._vars[k]["name"]
            if self._vars[k]["table"]:
                state[name] = "<table variable — content not tracked>"
                self._echo(f"{name:<20} = <table variable — content not tracked>")
            else:
                state[name] = self._env[k]
                self._echo(f"{name:<20} = {_shorten(self._env[k], 300)}")
        state["@@ROWCOUNT"] = self._env.get("@@ROWCOUNT")
        self._echo(f"{'@@ROWCOUNT':<20} = {_shorten(state['@@ROWCOUNT'], 300)}")
        return state

    def sql(self, query: str) -> object:
        """Ad-hoc query on the SAME session (sees uncommitted state).

        Rows are capped at 10,000. On failure the transaction is rolled back
        (a doomed Fabric transaction would poison every later step) and the
        error re-raised.
        """
        cur = self._ensure_connection()
        try:
            cur.execute(query)
        except Exception as exc:
            self._echo(f"[SQL] ad-hoc query failed: {_parse_sql_error(str(exc))[0]}")
            self._safe_rollback(announce=True)
            raise
        columns = [d[0] for d in cur.description] if cur.description else []
        raw_rows = cur.fetchmany(10_000) if columns else []
        if columns and len(raw_rows) == 10_000:
            self._echo("[SQL] result truncated at 10,000 rows.")
        while cur.nextset():
            pass
        try:
            import pandas as pd
            return pd.DataFrame([list(r) for r in raw_rows], columns=columns)
        except ImportError:
            return [dict(zip(columns, r)) for r in raw_rows]

    def rollback(self) -> None:
        """Undo all data effects so far but keep the session and variables.

        A mid-debug reset of the warehouse state — useful after inspecting
        the damage of a wrong step, or to re-run a phase from clean data.
        """
        self._safe_rollback()
        self._echo("ROLLBACK executed — data effects undone; captured variables kept.")

    def log_df(self) -> object:
        """Executed-step log as a DataFrame (or a list of dicts without pandas)."""
        try:
            import pandas as pd
            return pd.DataFrame(self._log)
        except ImportError:
            return self._log

    def close(self, commit: bool = False) -> object:
        """Close the session. Default: ROLLBACK — nothing persists in the Warehouse.

        Exception-safe: the connection is closed and released even when the
        final rollback/commit fails (dead session).
        """
        if self._conn is not None:
            try:
                if not self._autocommit:
                    try:
                        self._conn.commit() if commit else self._conn.rollback()
                        self._echo("COMMIT executed." if commit
                                   else "ROLLBACK executed — nothing persisted.")
                    except Exception as exc:
                        self._echo(f"[TRANSACTION] final {'COMMIT' if commit else 'ROLLBACK'} "
                                   f"FAILED — session likely dead: {_parse_sql_error(str(exc))[0]}")
            finally:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
                self._cursor = None
        return self.log_df()


def _shorten(value, limit=80):
    r = repr(value)
    return r if len(r) <= limit else r[:limit] + f"... ({len(r)} chars)"


def _parse_sql_error(text):
    """Extract (message, error_number) from a raw pyodbc error string.

    Joins every '[SQL Server]...' segment (multi-message errors keep all of
    them) and pulls the first error number, e.g. '(50000)'.
    """
    segments = re.split(r"\[SQL Server\]", text)[1:]
    messages = []
    for seg in segments:
        seg = re.sub(r"\s*\(\d+\)\s*\(SQL\w+\)[\s'\")]*$", "", seg.strip())
        seg = seg.strip(" ;'\")")
        if seg:
            messages.append(seg)
    number_match = re.search(r"\((\d+)\)\s*\(SQL", text)
    number = int(number_match.group(1)) if number_match else None
    if messages:
        return " | ".join(messages), number
    return text, number


def _extract_sql_error(exc):
    """Kept for backward compatibility: clean message only."""
    return _parse_sql_error(str(exc))[0]
