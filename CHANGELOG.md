# Changelog

## 0.1.0 — 2026-09-03

First release.

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
