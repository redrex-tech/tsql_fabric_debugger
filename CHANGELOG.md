# Changelog

## 0.1.0 — 2026-09-03

First release.

Fixes from the four-lens pre-publish review (`docs/REVIEW-2026-09-03.md` —
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
- Mutation testing with mutmut (`scanner`/`parser` scope, ~85% of mutants
  caught) plus a snapshot/invariant hardening suite.

Semantics fixes from the pre-release gap analysis
(`docs/GAP-ANALYSIS-2026-09-03.md`):

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
