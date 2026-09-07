# Changelog

## 0.3.1 — 2026-09-07

Hardening round (concurrency + privacy review):

- **`save_state()` privacy**: the docstring and the on-save message now warn
  that the JSON file is plain-text and may contain warehouse/production data —
  store it safely and delete when done.
- **Teardown robustness**: `dap.py` `_close_root` detaches the debugger before
  closing, so a `SIGTERM`/`SIGINT` arriving mid-`close()` cannot leave a
  half-torn-down session (idempotent even under `BaseException`).
- **Docs hygiene**: example procedure/parameter names generalized
  (`dbo.load_sales` / `@year`) across README, docstrings and the CHANGELOG —
  no internal/proprietary identifiers in the published package.

## 0.3.0 — 2026-09-04

Debugger parity with mainstream tools (pdb/debugpy, Chrome DevTools), driven
by a feature-gap analysis:

- **`step_out()`**: finish the current context and stop one level up — the
  remaining sub-steps of an expanded IF/WHILE (a WHILE stops at its
  re-evaluation step), or the whole active child debugger (OUTPUTs
  collected). Honors breakpoints on the way.
- **`eval(expr)`**: one-shot server-side evaluation of a T-SQL expression
  with the current variables — the single-use counterpart of `watch()`.
- **`stack()`**: the frame stack — procedures in the nested-EXEC chain plus
  the expanded-block frames (loop iteration included).
- **`stop_on_error="any"`**: `run_all()`/`run_until()`/`step_out()` also
  pause on errors a CATCH handled (after the CATCH emulation) — the
  "break on caught exceptions" of DevTools.
- **`break_at(..., hits=N, once=True)`**: hit-count breakpoints (fire from
  the Nth pass) and one-shot breakpoints. `breaks()` now returns
  `{line: {"condition", "hits", "once", "count"}}` (was `{line: condition}`).
- **Logpoints**: `log_at(line, expr)` echoes a value when a line executes,
  without ever stopping (`clear_logpoints()`, `logpoints()`). Fires inside
  auto-expanded blocks and on IF/WHILE headers.
- **DAP adapter**: `tsql-fabric-dap` speaks the Debug Adapter Protocol over
  stdio — debug the `.sql` visually from VS Code (via a DAP bridge
  extension), nvim-dap or any DAP client: gutter/conditional/hit-count
  breakpoints, step over/into/out, variables pane, hover/REPL evaluation,
  CATCH-handled-error exception filter. Never commits; disconnect rolls back.
  Result sets a step returns are printed to the Debug Console as a text table
  and emitted as a `tsqlFabricResultSet` custom event
  (`{line, columns, rows, truncated}`) for clients that render a grid.

Hardening from a 4-persona review (data engineer, data analyst, QA, DBA):
expression validation that blocks batch-breaking typos (comments, `;`,
unbalanced quotes) in watch/eval/logpoint/break conditions; broken breakpoint
conditions pause instead of crashing; `reset()` zeroes breakpoint hit
counters; a WHILE hitting `max_loop_iterations` pauses `run_all()` instead of
silently running post-loop steps on partial state; `eval()` failures no
longer pollute `ERROR_MESSAGE()`; logpoints are disarmed during CATCH
emulation.

Robustness against orphaned warehouse sessions (a debugger process killed
without close() leaves its transaction open, holding locks):

- **`kill_orphan_sessions(server, database, min_idle_seconds=900)`** (and
  `tsql-debug --kill-orphans [--min-idle N]`): KILL library-tagged sessions
  sleeping with an open transaction past the idle threshold, so the server
  rolls them back and releases their locks.
- The `tsql-debug` and `tsql-fabric-dap` entry points install a SIGTERM
  handler: a polite kill runs close()+ROLLBACK instead of orphaning the
  session (SIGKILL still needs the janitor above).


## 0.2.3 — 2026-09-04

Usability (driven by end-user feedback):

- **`proc_name=`**: debug a DEPLOYED procedure by name — no more manual
  `OBJECT_DEFINITION` boilerplate. `TSQLDebugger(proc_name="dbo.load_sales", ...)`
  and `run_procedure(proc_name=..., ...)` fetch the source straight from the
  warehouse on a short-lived session (a name without schema resolves to dbo).
  Exactly one of `sql_file`/`sql_text`/`proc_name` must be given.
- **`fetch_source(proc_name, server, database)`** is now public, for when you
  want the source text itself.

## 0.2.2 — 2026-09-04

Usability (driven by end-user feedback):

- **`run_all(into=True)`**: run the whole procedure the `step_into()` way —
  every `IF`/`WHILE` is expanded, so each branch taken and each loop
  iteration becomes its own logged step. Replaces the low-level
  `while not dbg._finished and dbg._pos < len(dbg._steps): dbg.step_into()`
  loop with a single call. Statements that are not blocks (and loops with
  `BREAK`/`CONTINUE`) run whole, exactly as `run_all()` already does. Fully
  backward compatible — the default is `into=False`.
- **`show_error()` / `last_error()`**: inspect a failure without scanning the
  log by hand. `last_error()` returns the most recent ERROR log entry (or
  `None`); `show_error()` runs `show_detail()` on it — replacing the
  `erro = next(e for e in dbg._log if e["status"] == "ERROR");
  dbg.show_detail(erro["step"])` idiom with a single call.

## 0.2.1 — 2026-09-04

Usability (driven by end-user feedback that the API was too low-level):

- **`summarize(log)`**: a one-glance verdict of a run — "OK, all N steps ran"
  or "FAILED at step X (line Y): <message>", noting when a CATCH handled it.
  Returns the facts as a dict (`ok`, `steps`, `error_step`, `error_line`,
  `error`, `handled`). Pairs with `run_procedure` for a simple "run and tell
  me what happened" flow, no step-by-step needed.
- **`find_step(contains=... | line=...)`**: locate a step by a text fragment
  or a file line, instead of hand-writing
  `next(i for i, s in enumerate(dbg._steps, 1) if ...)`.
- **`jump_to` and `run_until` now accept a text fragment or `line=<n>`**, not
  only a step number — `dbg.run_until("MAX(SEQREC)")`, `dbg.jump_to(line=40)`.
  Fully backward compatible (a number still works exactly as before).

## 0.2.0 — 2026-09-03

Productivity release — the items that turn a step executor into a debugger:

- **Nested EXEC step-into**: `step_into()` on `EXEC schema.proc ...` fetches
  the child's source from the warehouse and returns a child debugger that
  shares the parent's session/transaction; OUTPUT arguments and @@ROWCOUNT
  copy back on completion, and an unhandled child error propagates to the
  parent's CATCH exactly like the real EXEC. `abort_child()` discards.
  Dynamic SQL/sp_executesql/expression arguments fall back to step-over.

- **Breakpoints**: `break_at(line, condition=None)` stops `run_all()` BEFORE
  the matching step; file lines are stable across expansions, conditions run
  server-side with the current variables, and `run_all()` auto-expands
  IF/WHILE blocks that contain a breakpoint. `clear_breaks()`, `breaks()`.
- **Watches**: `watch(expr, name)` appends expressions to every capture
  batch — values echo after each step and via `watches()`. `unwatch()`.
- **State snapshots**: `save_state(path)` / `load_state(source)` serialize
  the variable environment (datetime/Decimal/bytes-safe JSON) — pair with
  `jump_to()` to resume a session another day.
- **Replay**: `reset()` rolls back, restores the pristine step plan and the
  initial parameter values, and replays from step 1 on a fresh connection.
- **`;`-less T-SQL**: statements now also split on the next statement-starting
  keyword at level 0, with legal mid-statement continuations respected
  (INSERT..SELECT, UPDATE..SET, WITH..consumer; MERGE never auto-splits).
  Legacy code without terminators debugs statement by statement.
- **Execution diffs**: `diff_logs(log_a, log_b)` aligns two runs by
  (line, kind) and reports only the divergences.
- **Large-value offload** (`offload_threshold`): strings above the threshold
  live in a server-side session temp table and are hydrated into variables
  per batch — uploaded once per change instead of re-sent on every step
  (graceful fallback if the endpoint lacks temp tables).
- **`lock_timeout`**: a session opened with `lock_timeout=<seconds>` fails a
  lock-blocked statement fast (error 1222) instead of hanging behind another
  session's lock — the anti-hang for orphaned-transaction locks (constructor,
  `run_script`, and `--lock-timeout` on the CLI). It bounds the wait; only
  the server can reap the orphan itself.
- **Memory bounds**: `history_batches=N` prunes old SUCCESS payloads
  (batch text/result sets) keeping the last N and every ERROR; `step_into`
  on WHILE now prunes the previous iteration's executed sub-steps, so long
  loops no longer grow the step list per iteration.
- **Test coverage**: a programmable fake pyodbc session (`tests/conftest.py`)
  drives the engine offline, adding 32 engine tests and bringing `engine.py`
  into the mutation-testing scope (previously scanner/parser only).

Post-implementation adversarial review (second pass) fixed: UNION/EXCEPT/
INTERSECT no longer split a statement (with or without ';'); a child ending
in error propagates to the parent CATCH instead of reporting SUCCESS;
breakpoints on a block's own header line stop before the block (and blocks
containing breakpoints auto-expand only for BODY lines); WHILE loops with
BREAK/CONTINUE containing a breakpoint stop before the loop instead of
silently running through; detached children cannot silently reconnect as
independent sessions; child guards on step_into/run_step/run_until; table
variables are rejected as EXEC arguments; the server-side offload table is
namespaced per debugger instance (parent/child same-named variables never
collide) and its creation state travels between parent and child; nested
loop pruning handles inner loops; reset()/close() detach an active child.

## 0.1.0 — 2026-09-03

First release.

Fixes from the four-lens pre-publish review (internal review —
data analyst, data engineer, DBA and developer/QA perspectives):

- **CLI**: exit-code computation no longer crashes on the base install
  (without pandas); quoted `--param` values force strings (leading zeros,
  literal "NULL"); strict int/float inference (no `1e5`/`nan`/`inf` floats);
  missing file returns exit 2 with a clean message; loose-script fallback
  warns and honors `--commit`; `--step-timeout` exposed; documented exit
  codes.
- **Engine correctness**: post-CATCH skip covers nested TRY blocks
  (catch-id stack); connection/internal failures are echoed as `[FATAL]` and
  re-raised instead of silently finishing; the error entry (not the CATCH's
  last entry) is returned to the caller; `DECLARE` and `RETURN` inside the
  emulated CATCH work; bare `THROW` in the CATCH aborts like the real
  re-raise; step_into condition failures route through the CATCH like
  step(); the full `ERROR_*()` family is emulated; session `SET` options
  persist (unparameterized batches); result sets produced before a failure
  are kept.
- **Parser**: `IF ... ELSE` without `;` before the ELSE; `COPY`/`GRANT`/
  `DENY`/`REVOKE`/`DBCC` end an IF condition; bodies without an outer
  `BEGIN...END` (incl. starting at `BEGIN TRY`) parse correctly; catch
  registration is idempotent across WHILE re-expansions; multi-encoding
  `.sql` reading (UTF-8/BOM, UTF-16 BOM, cp1252).
- **Session lifecycle (DBA)**: context-manager support (`with ... as dbg:`);
  exception-safe `close()`; public `rollback()`; Ctrl+C cancels the running
  statement server-side and rolls back; ad-hoc `sql()` errors roll back and
  are capped at 10k rows; transaction-control detection now covers string
  literals (dynamic SQL) and flags opaque `EXEC` calls; `autocommit=True`
  echoes a persistence warning; `APP=tsql-fabric-debugger` + `LoginTimeout`
  on the connection string.
- **Observability**: `rows_affected` is `None` when not measured (no more
  stale values); `post_rollback` column marks steps after a rollback;
  `show_detail()` prints untruncated changed variables and the raw driver
  error; `last_results()` DataFrames carry `attrs["truncated"]`;
  multi-message SQL errors are joined instead of truncated; loose scripts
  run inside a transaction with ROLLBACK by default and split via the
  library's scanner; CSV saving works without pandas and uses utf-8-sig.

- `TSQLDebugger`: interactive procedure debugging without touching the
  `.sql` — `step()`, `step_into()` (IF/WHILE statement by statement, with
  the condition evaluated server-side), `run_until()`, `jump_to()`,
  `run_step()`, `set_var()`, `show_vars()`, `sql()`, `show_detail()`,
  `last_results()`, `set_log_level()`.
- State preserved between steps (DECLARE + re-injection through pyodbc
  parameters + capture); `@@ROWCOUNT` and `ERROR_MESSAGE()` keep their
  cross-batch semantics.
- `BEGIN CATCH` emulated; DECLARE inside blocks stays visible (batch scope).
- Transaction with ROLLBACK by default; `close(commit=True)` is explicit.
- Entra ID authentication: Fabric notebook (notebookutils) or `az login`
  (AzureCliCredential).
- Batch mode (`run_procedure`), loose scripts (`run_script`) and a CLI
  (`tsql-debug`).
- Mutation testing with mutmut plus a snapshot/invariant hardening suite.

Semantics fixes from the pre-release gap analysis
(internal gap analysis):

- `RETURN` ends the debug — including when it runs inside an atomic block
  (detected by the missing capture).
- One CATCH block per `BEGIN TRY` (`catch_id` per step); after emulating the
  CATCH, the debug skips the rest of that TRY and continues after
  `END CATCH`, like T-SQL does.
- Table variables: declared in every batch, excluded from re-injection and
  capture, with a warning that their content does not survive across steps.
- `BEGIN TRAN` is no longer treated as a `BEGIN...END` block opener
  (procedures with explicit transactions used to break the parse) and inner
  `COMMIT`/`ROLLBACK`/`BEGIN TRAN`/`SAVE TRAN` raise a warning.
- Result sets produced by the procedure itself are captured
  (`last_results()`, `result_sets` column in the log) instead of discarded.
- `step_timeout` (seconds per step) in the constructor.
