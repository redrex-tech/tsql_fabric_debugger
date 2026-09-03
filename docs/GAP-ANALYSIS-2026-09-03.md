# Functional completeness analysis — 0.1.0 (2026-09-03)

Tech-lead review of what the library still needs to cover real-world
debugging of Data Warehouse T-SQL procedures. Based on a full read of the
source (`scanner.py`, `parser.py`, `engine.py`, `runner.py`,
`connection.py`, `cli.py`).

## Overall assessment

The core is solid: a correct scanner (strings, nested comments, brackets), a
parser with token spans for control flow, and an engine that solves the
central problem (state across batches) with safe parameterized re-injection,
`setinputsizes` for NVARCHAR(MAX)/UTF-8 and ROLLBACK by default. What's
missing isn't quality — it's coverage of what real procedures do.

> **Status (2026-09-03, same day):** all 6 MUST items were implemented and
> validated (58 offline tests + 6 integration tests against a real
> Warehouse). Implementing item 4 revealed and fixed an extra bug:
> `BEGIN TRAN` was counted as a block opener and broke parsing of procedures
> with explicit transactions. Item 3 went beyond plan: besides the right
> CATCH, execution continues after `END CATCH` (real T-SQL semantics).
> Details in the CHANGELOG.

## MUST — 0.1.x (remove silent semantic divergence) — ✅ DONE

| # | Gap | Scenario | Effort |
|---|-----|----------|--------|
| 1 | **Table variables break the harness** — `DECLARE @t TABLE` enters the capture as a scalar → syntax error on every step; and its content would die across batches | Procedure accumulating keys in a `@table TABLE` | S (warn) / M (handle) |
| 2 | **`RETURN` does not end the debug** — a guard clause `IF @x IS NULL RETURN;` runs and the debug keeps executing code the real execution would never reach | Guard clauses at the top of the procedure | S |
| 3 | **Multiple TRY/CATCH: the wrong CATCH is emulated** — `ctx["catch"]` was overwritten; an error in phase 1 emulated the last phase's CATCH | One TRY/CATCH per load phase (a DW staple) | M |
| 4 | **Inner `COMMIT`/`BEGIN TRAN` defeats rollback-by-default** — a COMMIT inside a step commits the debugger's transaction, silently breaking the core promise | Procedure managing its own transaction | S (detect/warn) |
| 5 | **Intermediate result sets discarded** — `_exec_batch` called `nextset()` without fetching; the procedure's diagnostic SELECTs were invisible | Validation counts / samples mid-procedure | M |
| 6 | **No per-step timeout** — `cursor.timeout` was never set; an unfiltered UPDATE hangs the session holding locks | Big fact table, wrong predicate | S |

## SHOULD — 0.2.0 (turn it into a real debugger)

- **Line and conditional breakpoints** (`run_until` by index is fragile:
  `_expand_*` mutates `self._steps`; a file line is stable; a condition
  reuses `_eval_condition`). — M
- **Expression watch** — infrastructure ready (`extra_capture`); missing the
  `watch()/unwatch()` API. — S
- **Full ERROR_*() family in the emulated CATCH** — only `ERROR_MESSAGE()` is
  rewritten; `ERROR_NUMBER()/LINE()/PROCEDURE()` come back NULL in error
  logging. — S/M
- **Save/restore of variable state** (`_env` → JSON) + pairs with
  `jump_to`. — S
- **Session reset/replay** — requires keeping the immutable parsed step
  list. — M
- **Slicing without `;`** — legacy T-SQL without terminators becomes one
  giant step; also cut on `STMT_START` keywords at level 0. — M
- **#temp tested and documented** — works through the single session, but a
  post-error ROLLBACK drops a `#temp` created inside the transaction; needs
  tests + docs. — M
- **Step-into for nested `EXEC`** — fetch the source from
  `sys.sql_modules`, sub-debugger on the same cursor. — L

## NICE — backlog

Execution diffs (compare two `log_df`s); BREAK/CONTINUE in WHILE
`step_into`; nested TRY inside a branch (paired with MUST 3); a DAP adapter
for VS Code; a fixture covering MERGE/CTE (they already work in practice).

## Mapped technical risks

1. An identifier equal to a keyword without brackets (`end` as a column)
   confuses block counting → silent mis-slicing.
2. The IF-condition end heuristic (first `STMT_START` token outside parens)
   can cut an exotic condition at the wrong place.
3. Post-error: the ROLLBACK wipes data but `_env` keeps the variables —
   mixed state; deserves a louder warning.
4. The emulated CATCH runs in a fresh transaction (the real one would run in
   the doomed one).
5. DATETIME2(7) round-trip loses precision (Python datetime = microseconds).
6. Re-injecting a large NVARCHAR(MAX) on every step has a cost in
   dynamic-SQL-heavy procedures.
7. `@@TRANCOUNT`/`XACT_STATE()` see the debugger's transaction.
8. The `SQL_VARIANT` type fallback doesn't exist on the Fabric Warehouse →
   confusing server error instead of a clear parse error.
9. Files with multiple objects: only the first procedure is considered.

## Recommended roadmap

- **0.1.x**: the 6 MUSTs + document table variables, `RETURN`, `#temp` and
  inner transactions under "Practical limits". Criterion: nothing may *lie*
  to the user.
- **0.2.0**: conditional breakpoints, watch, save/restore, ERROR_*(),
  reset/replay, `;`-less slicing, official `#temp` support.
- **0.3+**: nested EXEC, execution diffs, BREAK/CONTINUE, DAP.
