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
    parse_exec_call,
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
                 step_timeout: int | None = None, max_result_rows: int = 50,
                 offload_threshold: int = 200_000, history_batches: int | None = None,
                 lock_timeout: int | None = None, echo=print):
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
        self._lock_timeout = lock_timeout            # seconds to wait for a lock (None = forever)
        self._max_result_rows = max_result_rows      # rows captured per procedure result set
        self._offload_threshold = offload_threshold  # chars; bigger strings live server-side
        self._history_batches = history_batches      # keep batch text for the last N entries
        self._echo = echo
        self._watches = {}        # name -> expression, appended to every capture
        self._watch_values = {}   # name -> last captured value
        self._watch_seq = 0
        self._breaks = {}         # file line -> condition (None = unconditional)
        self._break_resume = None
        self._offload_synced = {} # var key -> value currently stored server-side
        self._state_table_ok = False
        self._offload_disabled = False
        self._pruned_upto = 0
        self._owns_connection = True   # False for a child adopted into a parent session
        self._child = None             # active child debugger (nested EXEC step-into)
        self._child_call = None        # (exec_step, output_map, assign_var)
        # namespace for the server-side state table: a nested-EXEC child shares
        # the session (and the table) — same-named variables must not collide
        self._state_ns = f"{id(self) & 0xFFFFFF:06x}"
        self._conn = None
        self._cursor = None
        self._log = []
        self._details = {}   # log step -> {"text","batch","resultsets","captured","changed","raw_error"}
        self._error_msg = None
        self._error_number = None
        self._error_line = None
        self._propagated_error = None   # (msg, number, line) when the proc ends in error
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
        i0, i1 = procedure_body(sql_text, tokens, i_as)

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

        # pristine snapshots for reset(): the step list mutates on step_into
        # expansions, and the pending defaults are consumed on first connect
        self._initial_steps = list(self._steps)
        self._initial_env = dict(self._env)
        self._initial_pending = list(self._pending_defaults)

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
        if self._conn is None and not self._owns_connection:
            raise RuntimeError("This child debugger was detached from its parent "
                               "session and cannot reconnect on its own.")
        if self._conn is None:
            self._conn = connect(self._server, self._database, autocommit=self._autocommit,
                                 lock_timeout=self._lock_timeout)
            if self._step_timeout:
                self._conn.timeout = self._step_timeout   # per-command timeout, in seconds
            self._cursor = self._conn.cursor()
            if self._pending_defaults:
                pending, self._pending_defaults = self._pending_defaults, []
                assigns = ", ".join(f"{n} = ({d})" for n, d in pending)
                self._exec_batch(f"SELECT {assigns}", [], f"defaults: {assigns}", "params", 0)
        return self._cursor

    def _safe_rollback(self, announce=False) -> bool:
        """Rollback that never raises. Returns True only when it really ran —
        callers must not announce success otherwise."""
        if self._autocommit or self._conn is None:
            return False
        try:
            self._conn.rollback()
            self._rolled_back = True
            # the rollback also undoes the server-side state table used for
            # offloaded large values — force re-creation on the next batch
            self._state_table_ok = False
            self._offload_synced.clear()
            if announce:
                self._echo("[TRANSACTION] error inside a Fabric transaction — ROLLBACK "
                           "executed (data effects of previous steps undone; captured "
                           "VARIABLES keep their values).")
            return True
        except Exception:
            self._echo("[TRANSACTION] rollback FAILED — the session is likely dead; "
                       "close() and start a new debugger.")
            return False

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
        # very large strings live in a server-side session table and are
        # HYDRATED into the variable instead of re-uploaded on every batch
        offloaded = [k for k in to_inject if self._is_offloaded(k)]
        to_inject = [k for k in to_inject if k not in set(offloaded)]
        parts = [declare + ";"] if outer else []
        values = []
        if to_inject:
            parts.append("SELECT " + ", ".join(f"{self._vars[k]['name']} = ?" for k in to_inject) + ";")
            values = [self._env[k] for k in to_inject]
        for k in offloaded:
            parts.append(f"SELECT {self._vars[k]['name']} = (SELECT [value] FROM "
                         f"#tsqldbg_state WHERE [name] = N'{self._state_ns}:{k}');")
        if stmt_text is not None:
            parts.append(stmt_text + "\n;")
        capture = f"SELECT '{_SENTINEL}' AS [{_SENTINEL}], @@ROWCOUNT AS [__rowcount__]"
        if scalars:
            capture += ", " + ", ".join(f"{self._vars[k]['name']} AS [{k}]" for k in scalars)
        for wname, wexpr in self._watches.items():
            capture += f", ({wexpr}) AS [__watch__{wname}]"
        if extra_capture:
            capture += ", " + extra_capture
        parts.append(capture + ";")
        return "\n".join(parts), values + stmt_binds

    def _is_offloaded(self, key):
        value = self._env.get(key)
        return (not self._offload_disabled
                and isinstance(value, str) and len(value) > self._offload_threshold)

    def _sync_offloaded(self, cur, exclude=frozenset()):
        """Push changed large values to the server-side state table (once per
        change, instead of once per step). Falls back to plain parameter
        injection if the endpoint rejects session temp tables."""
        keys = [k for k in self._vars
                if k not in exclude and not self._vars[k]["table"] and self._is_offloaded(k)]
        stale = [k for k in keys if self._offload_synced.get(k) != self._env[k]]
        if not stale:
            return
        try:
            import pyodbc
            if not self._state_table_ok:
                cur.execute("CREATE TABLE #tsqldbg_state ([name] VARCHAR(200) NOT NULL, "
                            "[value] NVARCHAR(MAX));")
                self._state_table_ok = True
                self._offload_synced.clear()
            for k in stale:
                nskey = f"{self._state_ns}:{k}"
                cur.setinputsizes([(pyodbc.SQL_WVARCHAR, 0, 0)])
                cur.execute(f"DELETE FROM #tsqldbg_state WHERE [name] = N'{nskey}'; "
                            f"INSERT INTO #tsqldbg_state ([name], [value]) VALUES (N'{nskey}', ?);",
                            (self._env[k],))
                cur.setinputsizes(None)
                while cur.nextset():
                    pass
                self._offload_synced[k] = self._env[k]
        except Exception as exc:
            self._offload_disabled = True
            self._state_table_ok = False
            self._echo("[NOTICE] large-value offload unavailable on this endpoint "
                       f"({_parse_sql_error(str(exc))[0]}) — falling back to per-step "
                       "parameter injection.")

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
            self._sync_offloaded(cur, exclude)
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
                        elif col.startswith("__watch__"):
                            self._watch_values[col[len("__watch__"):]] = val
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
            if captured and self._watches and kind not in ("cond", "params"):
                for wname in self._watches:
                    self._echo(f"      ?? {wname} = {_shorten(self._watch_values.get(wname))}")
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
            if self._watches:
                self._echo("      Hint: watch expressions run inside every batch — if the "
                           "error points at one of them, unwatch() it and retry.")
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
        if self._history_batches is not None:
            # bound memory on long sessions: drop the heavy payloads of old
            # SUCCESS entries (ERROR entries keep everything for diagnosis)
            cutoff = entry["step"] - self._history_batches
            while self._pruned_upto < cutoff:
                self._pruned_upto += 1
                old = self._details.get(self._pruned_upto)
                if old is not None and self._log[self._pruned_upto - 1]["status"] != "ERROR":
                    old["batch"] = None
                    old["resultsets"] = []
                    old["changed"] = {}
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
            # the ERROR entry may not be the first new one: a lazy first
            # connection logs the pending-defaults entry before the statement
            error_entry = next((e for e in reversed(self._log[log_before:])
                                if e["status"] == "ERROR"), None)
            if error_entry is None:
                # no SQL error was recorded: infrastructure or internal
                # failure — never swallow it (a swallowed bug looks like a
                # clean finish)
                self._echo(f"[FATAL] failure outside SQL execution: {exc}")
                raise
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
            outcome = self._emulate_catch(catch_steps)   # "ok"|"return"|"throw"|"failed"
            escaped = (self._error_msg, self._error_number, self._error_line)
            self._error_msg = self._error_number = self._error_line = None
            if not finalize:
                # run_step(): the isolated execution must not end the
                # sequential debug, whatever happened inside the CATCH
                return
            if outcome == "return":  # the CATCH handled the error and returned
                self._finished = True
                return
            if outcome in ("throw", "failed"):
                # THROW re-raises / the CATCH itself failed: the error escapes
                # this procedure — a parent (nested EXEC) must see it
                self._finished = True
                self._propagated_error = escaped
                return
            if outcome == "ok":
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
        elif finalize:
            # no CATCH: the error is unhandled — it would abort the procedure
            # and propagate to a caller (nested-EXEC parent)
            self._finished = self._stop_on_error
            if self._finished:
                self._propagated_error = (self._error_msg, self._error_number,
                                          self._error_line)

    def _emulate_catch(self, catch_steps):
        """Run the CATCH steps without touching the debug lifecycle.

        Returns "ok" (CATCH completed — execution continues after END CATCH),
        "return" (RETURN inside the CATCH — clean end), "throw" (THROW
        re-raises the error) or "failed" (the CATCH itself raised). The
        CALLER decides what that means for _finished and error propagation,
        so an isolated run_step() never kills the sequential session.
        """
        for step in catch_steps:
            first = scan(step["text"])
            if first and is_word(first[0], "THROW"):
                started = time.time()
                self._record("throw", step["line"], "SUCCESS",
                             "[CATCH] " + step["text"], started, {}, None)
                self._echo("      THROW — the original error is re-raised; procedure aborts.")
                return "throw"
            try:
                _, returned = self._execute_step(step, prefix="[CATCH] ")
            except KeyboardInterrupt:
                raise
            except Exception:
                return "failed"
            if returned:
                self._echo("      RETURN inside the CATCH — procedure execution finished.")
                return "return"
        return "ok"

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

    def _child_done(self) -> bool:
        c = self._child
        return c is not None and (c._finished or c._pos >= len(c._steps))

    def step(self) -> dict | None:
        """Run the next step and advance the cursor ("step over": whole IF/WHILE).

        When a child debugger (nested EXEC step-into) is active, step() first
        requires it to finish; the completing call copies the child's OUTPUT
        values back and records the EXEC step.
        """
        if self._child is not None:
            if not self._child_done():
                self._echo("[CHILD] a child debugger is active — finish it first "
                           "(child.run_all()) or discard it with abort_child().")
                return None
            return self._finish_child()
        if self._finished or self._pos >= len(self._steps):
            self._echo("Debug finished — all steps executed (or CATCH emulated).")
            return None
        current = self._steps[self._pos]
        self._pos += 1
        try:
            return self._run_one(current, emulate_catch=True, finalize=True)
        except BaseException:
            # _run_one only re-raises on fatal/interrupt — the step did not
            # complete, so keep the cursor on it for a retry after recovery
            self._pos -= 1
            raise

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
        if self._child is not None:
            self._echo("[CHILD] a child debugger is active — finish it first "
                       "(child.run_all()) or abort_child().")
            return None
        step = self._steps[self._pos]
        if step["kind"] == "stmt" and step["text"].lstrip()[:4].upper() in ("EXEC", "EXECU"):
            return self._step_into_exec(step)
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
            error_entry = next((e for e in reversed(self._log[log_before:])
                                if e["status"] == "ERROR"), None)
            if error_entry is None:
                self._echo(f"[FATAL] failure outside SQL execution: {exc}")
                raise
            # the condition evaluation failed — same treatment the block
            # would get failing under step(): emulate its CATCH and move on
            if self._pos < len(self._steps) and self._steps[self._pos] is step:
                self._pos += 1
            self._handle_step_error(step, emulate_catch=True, finalize=True)
            return error_entry

    # -- nested EXEC step-into ----------------------------------------------
    def _step_into_exec(self, step):
        """Step INTO a child stored procedure called by a plain EXEC.

        Fetches the child's source from the warehouse (same session — it sees
        even uncommitted definitions), maps the call arguments onto the
        child's parameters, and hands back a CHILD TSQLDebugger that shares
        this session and transaction. Debug it (child.step()/run_all()); when
        it finishes, the parent's next step() copies the OUTPUT values back
        and records the EXEC step. Falls back to step over when the call is
        dynamic (EXEC(@sql)/sp_executesql), the source is unavailable, or an
        argument is not a literal/variable.
        """
        call = parse_exec_call(step["text"])
        if call is None:
            self._echo("[CHILD] dynamic EXEC / sp_executesql — cannot step into; stepping over.")
            return self.step()
        cur = self._ensure_connection()
        try:
            cur.execute("SELECT OBJECT_DEFINITION(OBJECT_ID(?));", (call["proc"],))
            row = cur.fetchone()
            source = row[0] if row else None
            while cur.nextset():
                pass
        except Exception as exc:
            self._echo(f"[CHILD] could not fetch {call['proc']} source "
                       f"({_parse_sql_error(str(exc))[0]}); stepping over.")
            return self.step()
        if not source:
            self._echo(f"[CHILD] source of {call['proc']} not available "
                       "(missing object or no VIEW DEFINITION permission); stepping over.")
            return self.step()
        try:
            child_params, output_map = self._map_exec_args(call, source)
        except ValueError as exc:
            self._echo(f"[CHILD] {exc}; stepping over.")
            return self.step()
        try:
            child = TSQLDebugger(
                sql_text=source, params=child_params,
                server=self._server, database=self._database,
                autocommit=self._autocommit, log_level=self._log_level,
                stop_on_error=self._stop_on_error, preview_chars=self._preview_chars,
                max_loop_iterations=self._max_loop_iterations,
                step_timeout=self._step_timeout, max_result_rows=self._max_result_rows,
                offload_threshold=self._offload_threshold,
                history_batches=self._history_batches,
                lock_timeout=self._lock_timeout,
                echo=lambda m: self._echo("    » " + str(m)),
            )
        except ValueError as exc:
            self._echo(f"[CHILD] cannot parse {call['proc']} ({exc}); stepping over.")
            return self.step()
        child._adopt_connection(self._conn, self._cursor, self._state_table_ok)
        self._child = child
        self._child_call = (step, output_map, call["assign_var"])
        self._echo(f"[CHILD] stepping into {call['proc']} — it SHARES this session and "
                   "transaction (a child error rolls back everything). Debug it with "
                   "child.step()/run_all(); the parent's next step() collects the OUTPUTs.")
        return child

    def _map_exec_args(self, call, source):
        """Match EXEC arguments to the child's parameters.

        Returns (child_params, output_map) where output_map pairs the child
        parameter key with the parent variable key for each OUTPUT argument.
        """
        tokens = scan(source)
        i_proc = find_procedure(tokens)
        if i_proc is None:
            raise ValueError("child source has no CREATE PROCEDURE")
        _, child_defs, _ = parse_params(source, tokens, i_proc)
        by_name = {p["name"].upper(): p for p in child_defs}
        child_params = {}
        output_map = []
        for pos, arg in enumerate(call["args"]):
            if arg["name"] is not None:
                target = by_name.get(arg["name"].upper())
                if target is None:
                    raise ValueError(f"argument {arg['name']} does not exist on the child")
            elif pos < len(child_defs):
                target = child_defs[pos]
            else:
                raise ValueError("more positional arguments than child parameters")
            value_text = (arg["value"] or "").strip()
            if value_text.startswith("@"):
                parent_key = value_text.upper()
                if parent_key not in self._env:
                    raise ValueError(f"argument variable {value_text} is unknown to the parent")
                if parent_key in self._vars and self._vars[parent_key]["table"]:
                    raise ValueError(f"{value_text} is a table variable — its content is "
                                     "not tracked and would reach the child empty")
                child_params[target["name"]] = self._env[parent_key]
                if arg["output"]:
                    output_map.append((target["name"].upper(), parent_key))
            else:
                ok, value = eval_literal(value_text or None)
                if not ok and value_text != "":
                    raise ValueError(f"argument {value_text!r} is not a literal or variable")
                child_params[target["name"]] = value
                if arg["output"]:
                    raise ValueError("OUTPUT argument must be a variable")
        return child_params, output_map

    def _adopt_connection(self, conn, cursor, state_table_ok=False):
        """Attach this debugger to an existing session (nested EXEC child)."""
        self._conn = conn
        self._cursor = cursor
        self._owns_connection = False
        self._state_table_ok = state_table_ok
        if self._pending_defaults:
            pending, self._pending_defaults = self._pending_defaults, []
            assigns = ", ".join(f"{n} = ({d})" for n, d in pending)
            self._exec_batch(f"SELECT {assigns}", [], f"defaults: {assigns}", "params", 0)

    def _finish_child(self):
        """Collect the finished child and record the EXEC step.

        A child that completed normally hands its OUTPUT values back. A child
        that ended in an UNHANDLED error (no CATCH, THROW, or a failed CATCH)
        propagates that error to the parent — exactly like the real EXEC
        would — so the parent's own CATCH gets emulated.
        """
        child = self._child
        exec_step, output_map, assign_var = self._child_call
        self._child = None
        self._child_call = None
        # session-level bookkeeping travels both ways
        if child._rolled_back:
            self._rolled_back = True
            self._state_table_ok = False
            self._offload_synced.clear()
        elif child._state_table_ok:
            self._state_table_ok = True     # the child may have created the table
        child._conn = None      # detach WITHOUT closing the shared session
        child._cursor = None
        started = time.time()
        propagated = child._propagated_error
        if propagated is not None and propagated[0] is not None:
            # real T-SQL: the child's unhandled error reaches the parent at
            # the EXEC — OUTPUT values are NOT copied back
            self._error_msg, self._error_number, _ = propagated
            self._error_line = exec_step["line"]
            entry = self._record("exec", exec_step["line"], "ERROR",
                                 "[CHILD failed] " + exec_step["text"], started,
                                 {}, self._error_msg)
            if self._pos < len(self._steps) and self._steps[self._pos] is exec_step:
                self._pos += 1
            self._handle_step_error(exec_step, emulate_catch=True, finalize=True)
            return entry
        changed = {}
        for child_key, parent_key in output_map:
            self._env[parent_key] = child._env.get(child_key)
            changed[self._vars[parent_key]["name"]] = self._env[parent_key]
        if "@@ROWCOUNT" in child._env:
            self._env["@@ROWCOUNT"] = child._env["@@ROWCOUNT"]
        if assign_var:
            key = assign_var.upper()
            if key in self._env:
                self._env[key] = 0
                self._echo(f"[CHILD] return value of the child is not observable — "
                           f"{assign_var} defaulted to 0 (T-SQL success).")
        entry = self._record("exec", exec_step["line"], "SUCCESS",
                             "[CHILD done] " + exec_step["text"], started, changed, None)
        if self._pos < len(self._steps) and self._steps[self._pos] is exec_step:
            self._pos += 1
        return entry

    def abort_child(self) -> None:
        """Discard an active child debugger without collecting its OUTPUTs.

        The shared transaction keeps whatever the child already executed —
        rollback() if you want that undone too. The EXEC step stays pending.
        """
        if self._child is None:
            self._echo("No active child debugger.")
            return
        self._child._conn = None
        self._child._cursor = None
        self._child = None
        self._child_call = None
        self._echo("Child discarded — the EXEC step is still pending "
                   "(step() runs it whole, step_into() re-enters).")

    def run_step(self, n: int, emulate_catch: bool = False) -> dict | None:
        """Run ONLY step n (1-based), with the current variable state.

        Does not move the sequential cursor and — unlike step() — does NOT
        emulate the CATCH on failure unless emulate_catch=True. If the step
        depends on variables from earlier steps that never ran, build the
        state first with set_var().
        """
        if self._child is not None:
            self._echo("[CHILD] a child debugger is active — finish it first "
                       "(child.run_all()) or abort_child().")
            return None
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

    # -- watches ------------------------------------------------------------
    def watch(self, expr: str, name: str | None = None) -> str:
        """Track a T-SQL expression after every step, without typing sql().

        Example: dbg.watch("(SELECT COUNT(*) FROM stg.movements)", "stg_rows").
        The expression is appended to every capture batch — if it references
        a missing object, the NEXT step fails with that error (unwatch() it).
        Returns the watch name.
        """
        if not expr or not expr.strip():
            raise ValueError("Empty watch expression.")
        toks = scan(expr)
        if sum(1 for t in toks if t.get("u") == "(") != sum(1 for t in toks if t.get("u") == ")"):
            raise ValueError("Unbalanced parentheses in watch expression.")
        if name is None:
            self._watch_seq += 1
            name = f"w{self._watch_seq}"
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("Watch names must be simple identifiers.")
        self._watches[name] = expr
        self._echo(f"watch {name}: {expr}")
        return name

    def unwatch(self, name: str | None = None) -> None:
        """Remove one watch by name, or all of them when called without arguments."""
        if name is None:
            self._watches.clear()
            self._watch_values.clear()
            self._echo("All watches removed.")
            return
        self._watches.pop(name, None)
        self._watch_values.pop(name, None)
        self._echo(f"watch {name} removed.")

    def watches(self) -> dict:
        """Last captured value of every watch: {name: value}."""
        return dict(self._watch_values)

    # -- breakpoints --------------------------------------------------------
    def break_at(self, line: int, condition: str | None = None) -> None:
        """Stop run_all() BEFORE executing any step at this FILE line.

        File lines are stable across step_into() expansions (unlike step
        numbers). An optional T-SQL condition is evaluated server-side with
        the current variables: dbg.break_at(42, "@code = 31000").

        The line must hold a statement or a block header (IF/WHILE line) —
        a breakpoint on a BEGIN/END/blank line never matches any step.
        run_all() auto-expands blocks whose BODY contains a breakpoint.
        """
        self._breaks[line] = condition
        self._echo(f"breakpoint at line {line}"
                   + (f" when {condition}" if condition else ""))

    def clear_breaks(self, line: int | None = None) -> None:
        """Remove the breakpoint at one line, or all of them."""
        if line is None:
            self._breaks.clear()
            self._echo("All breakpoints removed.")
        else:
            self._breaks.pop(line, None)
            self._echo(f"breakpoint at line {line} removed.")

    def breaks(self) -> dict:
        """Registered breakpoints: {line: condition | None}."""
        return dict(self._breaks)

    # -- state snapshots ----------------------------------------------------
    def save_state(self, path: str | None = None) -> dict:
        """Serialize the current variable state (JSON-safe) — pair with
        load_state() + jump_to() to resume tomorrow without replaying steps."""
        payload = {"procedure": self.proc_name,
                   "vars": {k: _encode_value(v) for k, v in self._env.items()}}
        if path:
            import json
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            self._echo(f"State saved to: {path}")
        return payload

    def load_state(self, source) -> None:
        """Restore variables from save_state() output (a dict or a file path)."""
        import json
        if isinstance(source, str):
            with open(source, encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = source
        if data.get("procedure") != self.proc_name:
            self._echo(f"[WARNING] state was saved from {data.get('procedure')!r}, "
                       f"this debugger runs {self.proc_name!r}.")
        restored = 0
        for key, encoded in data.get("vars", {}).items():
            if key == "@@ROWCOUNT" or key in self._vars:
                self._env[key] = _decode_value(encoded)
                restored += 1
            else:
                self._echo(f"[WARNING] unknown variable in state, skipped: {key}")
        self._echo(f"{restored} variable(s) restored.")

    # -- replay -------------------------------------------------------------
    def reset(self) -> None:
        """Discard the session and replay from the start.

        Rolls back and closes the current connection, restores the pristine
        step plan (undoing step_into expansions), the initial variable values
        (params + defaults) and an empty log. Watches and breakpoints are
        kept. The next step() opens a fresh session.
        """
        if self._child is not None:
            self.abort_child()
        self.close(commit=False)
        self._steps = list(self._initial_steps)
        self._env = dict(self._initial_env)
        self._pending_defaults = list(self._initial_pending)
        self._pos = 0
        self._finished = False
        self._log = []
        self._details = {}
        self._pruned_upto = 0
        self._error_msg = self._error_number = self._error_line = None
        self._propagated_error = None
        self._rolled_back = False
        self._warned_post_rollback = False
        self._break_resume = None
        self._watch_values.clear()
        self._offload_synced.clear()
        self._state_table_ok = False
        self._echo("Session reset — replay starts from step 1 on a fresh connection.")

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
            if parent.get("loop_key") is not None:
                sub["loop_key"] = parent["loop_key"]   # keep loop pruning effective
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
        # prune the PREVIOUS iteration's executed sub-steps: a long loop must
        # not grow the step list unboundedly (the log keeps the history)
        loop_key = step["ti"]
        prune_from = self._pos
        while prune_from > 0:
            lk = self._steps[prune_from - 1].get("loop_key")
            if lk is not None and loop_key[0] <= lk[0] and lk[1] <= loop_key[1]:
                prune_from -= 1     # this iteration's subs, nested loops included
            else:
                break
        if prune_from < self._pos:
            del self._steps[prune_from:self._pos]
            self._pos = prune_from
        if not result:
            self._echo(f"WHILE condition is false — loop ended after {iteration - 1} iteration(s).")
            return None
        if iteration > self._max_loop_iterations:
            self._echo(f"[ABORTED] WHILE exceeded max_loop_iterations={self._max_loop_iterations}.")
            return None
        subs = self._sub_steps(branch["body"], step)
        for sub in subs:
            sub["loop_key"] = loop_key
        next_round = dict(step)
        next_round["iteration"] = iteration + 1
        self._steps[self._pos:self._pos] = subs + [next_round]
        self._echo(f"Iteration {iteration}: {len(subs)} sub-step(s) queued; "
                   f"the WHILE re-evaluates afterwards.")
        return subs

    def run_all(self) -> object:
        """Run to the end — or until an error (CATCH emulated) or a breakpoint.

        Breakpoints (break_at) stop BEFORE the matching step executes; calling
        run_all() again resumes past the one it stopped at.
        """
        while not self._finished and self._pos < len(self._steps):
            if self._child is not None:
                if not self._child_done():
                    self._echo("[CHILD] a child debugger is active — finish it first "
                               "(child.run_all()) or abort_child().")
                    return self.log_df()
                self._finish_child()
                continue
            step = self._steps[self._pos]
            if (self._breaks and step["kind"] in ("if_block", "while_block")
                    and self._break_resume != self._pos
                    and step["line"] not in self._breaks):
                # a breakpoint INSIDE a block only exists as a step after the
                # block is expanded — auto step_into blocks that contain one
                # (a breakpoint on the block's OWN line is handled below)
                end_line = self._sql.count("\n", 0, step["e"]) + 1
                if any(step["line"] < bl <= end_line for bl in self._breaks):
                    body_text = step["text"].upper()
                    if step.get("is_loop") and ("BREAK" in body_text
                                                or "CONTINUE" in body_text):
                        # step_into cannot expand this loop — stop before it
                        # instead of silently running through the breakpoint
                        self._break_resume = self._pos
                        self._echo(f"[BREAK] stopped BEFORE the WHILE at line "
                                   f"{step['line']} — it contains a breakpoint but "
                                   "BREAK/CONTINUE prevents expansion; step() runs "
                                   "it whole.")
                        return self.log_df()
                    self.step_into()
                    continue
            if self._breaks and self._break_resume != self._pos:
                condition = self._breaks.get(step["line"], "__no_break__")
                if condition != "__no_break__":
                    hit = True if condition is None else \
                        self._eval_condition(condition, step["line"])
                    if hit:
                        self._break_resume = self._pos
                        self._echo(f"[BREAK] stopped BEFORE line {step['line']} "
                                   f"(step {self._pos + 1})"
                                   + (f" — condition {condition} is true" if condition else "")
                                   + ". step()/run_all() to continue.")
                        return self.log_df()
            self._break_resume = None
            self.step()
        return self.log_df()

    def run_until(self, n: int) -> object:
        """Run up to step n (inclusive) — the 'breakpoint'.

        Step numbers change after step_into() expansions; re-run list_steps()
        for current numbers.
        """
        while not self._finished and self._pos < min(n, len(self._steps)):
            if self._child is not None and not self._child_done():
                self._echo("[CHILD] a child debugger is active — finish it first "
                           "(child.run_all()) or abort_child().")
                return self.log_df()
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
        Announces success only when a rollback actually ran.
        """
        if self._autocommit:
            self._echo("[TRANSACTION] autocommit=True — there is nothing to roll back; "
                       "every step already persisted.")
            return
        if self._conn is None:
            self._echo("No open session — nothing to roll back.")
            return
        if self._safe_rollback():
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
        if self._child is not None:
            # closing the parent kills the shared session — detach the child
            # so the parent is never left blocked on an unusable child
            self._child._conn = None
            self._child._cursor = None
            self._child = None
            self._child_call = None
        if self._conn is not None:
            if not self._owns_connection:
                # a child never closes the session it borrowed from the parent
                self._conn = None
                self._cursor = None
                self._echo("Child detached — the parent session stays open.")
                return self.log_df()
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


def _encode_value(value):
    """JSON-safe encoding for the T-SQL types that cross the pyodbc boundary."""
    import base64
    import datetime
    import decimal
    if isinstance(value, datetime.datetime):
        return {"__dt__": value.isoformat()}
    if isinstance(value, datetime.date):
        return {"__d__": value.isoformat()}
    if isinstance(value, datetime.time):
        return {"__t__": value.isoformat()}
    if isinstance(value, decimal.Decimal):
        return {"__dec__": str(value)}
    if isinstance(value, (bytes, bytearray)):
        return {"__b64__": base64.b64encode(bytes(value)).decode("ascii")}
    return value


def _decode_value(encoded):
    import base64
    import datetime
    import decimal
    if isinstance(encoded, dict):
        if "__dt__" in encoded:
            return datetime.datetime.fromisoformat(encoded["__dt__"])
        if "__d__" in encoded:
            return datetime.date.fromisoformat(encoded["__d__"])
        if "__t__" in encoded:
            return datetime.time.fromisoformat(encoded["__t__"])
        if "__dec__" in encoded:
            return decimal.Decimal(encoded["__dec__"])
        if "__b64__" in encoded:
            return base64.b64decode(encoded["__b64__"])
    return encoded


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
