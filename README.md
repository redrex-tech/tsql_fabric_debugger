# tsql-fabric-debugger

Step-by-step debugger for T-SQL stored procedures on the **Microsoft Fabric
Warehouse** — without changing a single line of your `.sql`.

> This README is the quick tour. The deep dive — why Fabric has no native
> debugger, how the state engine works inside, the full API reference, and
> usage from a local machine, a Fabric notebook or AI agents/MCP — lives in
> [`docs/DOCUMENTATION.md`](docs/DOCUMENTATION.md).

## Why

The Fabric Warehouse has no T-SQL debugger: no breakpoints, no watch, no way
to run half a procedure. A whole procedure is a single statement to any SQL
client, and `DECLARE` variables die at the end of each batch — so "run one
piece at a time" doesn't work naively.

This library solves that by parsing the procedure **in memory**: it slices
the body into steps and preserves variable state between them. Each step runs
as one batch on the same session:

```sql
DECLARE <all variables>;
SELECT @a = ?, @b = ?, ...;        -- state re-injection (pyodbc parameters)
<original statement, untouched>;
SELECT '__hcap__', @@ROWCOUNT, @a, @b, ...;   -- capture of the new state
```

`@@ROWCOUNT` and `ERROR_MESSAGE()` are rewritten to preserve cross-batch
semantics, and the procedure's `BEGIN CATCH` is emulated when a step fails.
Everything runs inside a transaction with **ROLLBACK at the end by
default** — nothing persists in the Warehouse unless you ask for it.

## Installation

```bash
pip install tsql-fabric-debugger            # Fabric notebook
pip install "tsql-fabric-debugger[local]"   # local machine (az login)
pip install "tsql-fabric-debugger[all]"     # + pandas and sqlparse
```

Outside Fabric you need the **ODBC Driver 18 for SQL Server** installed and
a valid `az login`.

## IDE debugging (DAP)

`tsql-fabric-dap` speaks the Debug Adapter Protocol over stdio: point any DAP
client (VS Code, nvim-dap, ...) at it and debug the `.sql` visually — gutter
breakpoints, step over/into/out, variables pane, hover evaluation. Never
commits; disconnect rolls back.

Rows a step returns are surfaced two ways: printed as a text table to the
Debug Console, and emitted as a `tsqlFabricResultSet` custom DAP event
(`{line, columns, rows, truncated}`) so a client can render them in a grid —
the VS Code extension shows a **Result Set** panel beside the editor.

A **VS Code extension** that wires this up (F5 on a `.sql`, no launch.json
needed) lives in [`editors/vscode/`](editors/vscode/) — preview/MVP.

## Interactive usage

```python
from tsql_fabric_debugger import TSQLDebugger

dbg = TSQLDebugger(
    "prd_load.sql",                        # the original .sql, untouched
    params={"@year": 2015},                # test values
    server="<endpoint>.datawarehouse.fabric.microsoft.com",
    database="my_warehouse",
)
```

No local `.sql`? Debug the **deployed** procedure by name — the source is
fetched straight from the warehouse (`OBJECT_DEFINITION`):

```python
dbg = TSQLDebugger(proc_name="pck_am.prd_crgodsrec",
                   params={"@numAnoRef": 2015}, server=..., database=...)
```

Prefer the context-manager form — it guarantees ROLLBACK + close even when an
exception interrupts the session, so no orphan transaction is left holding
locks on the warehouse:

```python
with TSQLDebugger("prd_load.sql", params={"@year": 2015},
                  server=..., database=...) as dbg:
    dbg.run_all()
```

```python

dbg.list_steps()      # list the numbered steps, without executing
dbg.step()            # run the next step ("step over": whole IF/WHILE)
dbg.step_into()       # step into an IF/WHILE: evaluates the condition
                      #   server-side, picks the branch, yields sub-steps
dbg.step_out()        # finish the current block/child and stop one level up
dbg.run_all(into=True)   # run to the end the step_into() way: every IF/WHILE
                         #   expanded, each loop iteration its own logged step
child = dbg.step_into()   # on an `EXEC dbo.child ...` step: fetches the child's
                          #   source from the warehouse and returns a CHILD
                          #   debugger sharing this session; debug it, then the
                          #   parent's next step() collects the OUTPUT values
                          #   (an unhandled child error reaches the parent CATCH)
dbg.run_until(15)     # run up to step 15 (breakpoint)
dbg.run_until("MAX(SEQREC)")   # ...or up to the step whose command has that text
dbg.jump_to(line=40)  # ...or position by file line; find_step() returns the number
dbg.show_vars()       # state of every variable (OUTPUT params included)
dbg.eval("@a * @b")   # evaluate one T-SQL expression with the CURRENT variables
dbg.stack()           # where am I? procedures + expanded blocks + iteration
dbg.sql("SELECT COUNT(*) FROM dbo.movements")   # query on the SAME session
dbg.jump_to(17)       # move the cursor without running earlier steps
dbg.set_var("@sqlSrc", "...")                   # build state by hand
dbg.run_step(17)      # run ONLY step 17 (does not move the cursor)
dbg.set_log_level("full")   # full command + SQL batch on errors
dbg.show_detail()     # last step in full (command + error + batch)
dbg.show_error()      # the step that FAILED, in full — the one-call idiom
                      #   after a failed run_all() (last_error() for the dict)
dbg.last_results()    # result sets the procedure itself produced
dbg.watch("(SELECT COUNT(*) FROM stg.movements)", "stg")   # tracked every step
dbg.log_at(8, "@fat")               # logpoint: print the value there, never stop
dbg.clear_logpoints()               # remove one logpoint (by line) or all
dbg.break_at(42, "@code = 31000")   # run_all() stops there when it's true
dbg.break_at(8, hits=4)             # ...or from the 4th pass on (once=True: fire once)
dbg.save_state("st.json")           # variables snapshot (JSON) ...
dbg.load_state("st.json")           # ... resume tomorrow with jump_to()
dbg.reset()           # rollback + replay from step 1 on a fresh session
dbg.close()           # ROLLBACK and close (commit=True to persist)

from tsql_fabric_debugger import diff_logs
diff_logs(log_2015, log_2016)       # divergences between two runs
```

Each step shows the line in the original file, the variables that changed
and, on errors, the clean SQL Server message:

```
[  5] line   19 | ok   | SELECT @offset = ISNULL(MAX(id), 0) FROM dbo.movements
      -> @offset = 184230
[  6] line   21 | ok   | condition: @year < 2018
      -> condition = True
```

## Batch mode and CLI

```python
from tsql_fabric_debugger import run_procedure, summarize

log = run_procedure("prd_load.sql", params={"@year": 2015},
                    server=..., database=..., save_csv="log.csv")
summarize(log)   # "OK — all N steps ran" or "FAILED at step X (line Y): <msg>"
```

`summarize(log)` is the "just tell me what happened" verdict — it prints one
line and returns the facts (`ok`, `error_step`, `error_line`, `error`,
`handled`), so you rarely need the step-by-step API for a quick check.

```bash
tsql-debug prd_load.sql --param @year=2015 \
    --server <endpoint> --database my_warehouse --csv log.csv
```

Server and database can also come from the `FABRIC_TSQL_SERVER` and
`FABRIC_TSQL_DATABASE` environment variables. Files without a
`CREATE PROCEDURE` go through `run_script()` — split on `GO` lines when
present, otherwise per statement via the library's own scanner (a `;` inside
a string never splits) — also inside a transaction with ROLLBACK by default.
On the CLI, `--param` values can be quoted (`--param @code='00123'`) to force
a string and keep leading zeros; unquoted values infer int/float strictly
(no scientific notation, no `nan`/`inf`). Exit codes: 0 = clean, 1 = at
least one step recorded an ERROR (even if the procedure's CATCH handled it),
2 = usage/file error. `.sql` files may be UTF-8 (with or without BOM),
UTF-16 with BOM (SSMS default) or cp1252.

### Constructor parameters

| Parameter | Default | Purpose |
|---|---|---|
| `params` | `{}` | test values for procedure parameters (`{"@year": 2015}`) |
| `autocommit` | `False` | `True` = every step persists immediately (a warning is echoed; `close()` undoes nothing) |
| `log_level` | `"simple"` | `"full"` prints whole commands, untruncated variables and the SQL batch on errors |
| `stop_on_error` | `True` | stop the sequential run on an unhandled error (`False`: continue past errors; `"any"`: also pause on CATCH-handled errors) |
| `step_timeout` | `None` | per-step query timeout in seconds (`None` = unlimited) |
| `lock_timeout` | `None` | seconds to wait for a lock before failing (error 1222) instead of hanging behind another session — the anti-hang for orphaned-transaction locks; does not prevent the orphan, only bounds the wait |
| `max_result_rows` | `50` | rows captured per result set the procedure produces |
| `max_loop_iterations` | `1000` | guard for `step_into()` on WHILE loops |
| `preview_chars` | `500` | command truncation in the log's `command` column |
| `offload_threshold` | `200_000` | strings above this length are kept in a server-side session table and hydrated per batch (uploaded once per change) instead of re-sent on every step |
| `history_batches` | `None` | keep the heavy per-step payloads (batch text, result sets) only for the last N entries — ERROR entries always keep everything |
| `echo` | `print` | console output sink |

### Log columns

`log_df()` / `--csv` (procedure mode): `step`, `line` (file line), `kind`
(`stmt`/`declare`/`if_block`/`while_block`/`cond`/`exec`/`eval`/`return`/`throw`/`params`),
`status` (`SUCCESS`/`ERROR`/`REGISTERED`), `rows_affected` (only for captured
steps; `None` otherwise), `duration_s`, `command` (truncated preview),
`changed_vars` (truncated — `show_detail()` has the full values),
`result_sets`, `post_rollback` (True for steps that ran after an
error-triggered rollback), `error`. The script mode (`run_script`) logs a
smaller schema: `step`, `status`, `rows_affected`, `duration_s`, `command`,
`error`.

## Security

- **Entra ID** authentication only: the notebook token on Fabric,
  `AzureCliCredential` elsewhere. No passwords, ever.
- Execution inside a transaction with ROLLBACK by default;
  `close(commit=True)` is an explicit decision.
- Variable re-injection through pyodbc parameters — no value concatenation
  into SQL.
- Only debug files you trust: the `.sql` **is** code executed under your
  identity, and rollback-by-default is a convenience, not a security
  boundary (a `COMMIT` hidden in dynamic SQL persists — the parser warns
  about literal and string-embedded transaction control, and flags `EXEC`
  calls it cannot see into).
- Console output and CSV logs contain **real data** from the warehouse
  (variable values, result-set rows) — treat them like the data itself.

## Operational notes (read before debugging a shared warehouse)

- **Required permissions**: the debugger does NOT `EXECUTE` the procedure —
  it runs the body's statements directly under your identity. You need
  SELECT/INSERT/UPDATE/DELETE on every object the procedure touches
  (ownership chaining does not apply), and row-level security/column masks
  apply to *you*, which can make results diverge from a real execution.
- **Long transactions hold locks**: the debug session keeps one transaction
  open from the first step until `close()`. Locks from completed steps are
  retained the whole time — an interactive session parked for an hour blocks
  concurrent writers and DDL on the touched tables. Debug in a dev
  warehouse/schema, use the `with` form, and `close()` as soon as you are
  done. `step_timeout` bounds a *running* statement only.
- The session identifies itself as `tsql-fabric-debugger` in
  `sys.dm_exec_sessions.program_name`.
- Instances are **not thread-safe** (one session, one shared environment).
- Memory: the full text of every executed batch is kept for `show_detail()`;
  very long sessions over procedures with multi-MB dynamic SQL grow
  accordingly.

## Semantics preserved

- `RETURN` ends the debug wherever it appears — top level, guard clause
  inside an `IF`, or inside the emulated `CATCH` — just as it would end the
  real execution.
- Each `BEGIN TRY` gets **its own** emulated `CATCH`; after the CATCH handles
  the error, the debug skips the rest of that TRY (nested TRY blocks
  included) and continues after `END CATCH` — the same T-SQL semantics. A
  `THROW` inside the CATCH aborts the debug like the real re-raise would.
- The full `ERROR_*()` family works in the emulated CATCH: `ERROR_MESSAGE()`,
  `ERROR_NUMBER()`, `ERROR_PROCEDURE()`, `ERROR_LINE()` (the file line of the
  failing step), plus `ERROR_SEVERITY()`/`ERROR_STATE()` as RAISERROR-style
  defaults (16/1) — the driver does not expose the real ones.
- `DECLARE` inside the CATCH (the classic `DECLARE @msg = ERROR_MESSAGE();`)
  is emulated correctly.
- `IF x SET a = 1 ELSE SET a = 2` — no `;` before the `ELSE` — parses and
  steps correctly.
- Bodies without an outer `BEGIN...END` (bare statements, or starting
  straight at `BEGIN TRY`) are supported.
- Session `SET` options (`NOCOUNT`, `XACT_ABORT`, ...) run unparameterized so
  they persist for the following steps, as they would in a real execution.
- `SELECT`s produced by the procedure itself (diagnostics, samples) are
  captured and displayed (`last_results()`), not discarded — including the
  ones produced before a step failed.
- Procedures with inner `COMMIT`/`BEGIN TRAN` raise a parse-time warning —
  including transaction keywords spotted **inside string literals** (dynamic
  SQL); `EXEC` calls are flagged as opaque.
- After an error-triggered rollback, the debug warns that following steps run
  against post-rollback data, and marks them with `post_rollback=True` in the
  log.
- `step_timeout=<seconds>` in the constructor bounds every step; Ctrl+C
  cancels the running statement server-side and rolls back.

## Practical limits

- **Table variables**: `DECLARE @t TABLE (...)` is declared in every batch
  (references compile) and triggers a warning, but its **content does not
  survive across steps** — step over the block that fills and consumes it,
  or use `#temp`.
- `step_into` on a `WHILE` with `BREAK`/`CONTINUE` falls back to atomic mode
  (with a warning).
- `GOTO`, cursors and `WAITFOR` are out of scope.
- A single failing statement keeps the previous variable values (same T-SQL
  semantics); when an atomic block fails, use `jump_to(n)` + `step_into()`
  to pinpoint the exact statement. After an error, the ROLLBACK undoes the
  data effects of earlier steps, but the captured variables remain.

## Tests

```bash
pytest                       # unit (offline, no warehouse)
FABRIC_TSQL_SERVER=... FABRIC_TSQL_DATABASE=... pytest -m integration
```

### Mutation testing

Suite quality is measured with [mutmut](https://mutmut.readthedocs.io/):

```bash
pip install "tsql-fabric-debugger[dev]"
mutmut run        # ~1,700 mutants over scanner.py and parser.py
mutmut results    # survivors; `mutmut show <id>` prints the diff
```

The scope covers `scanner`, `parser` **and `engine`** — the engine is
exercised offline through a programmable fake pyodbc session
(`tests/conftest.py`), so its logic (state capture, error routing, CATCH
emulation, breakpoints, nested EXEC, offload) is mutation-tested without a
warehouse. `connection`/`runner`/`cli` stay out (thin driver/warehouse
glue). `tests/test_mutation_hardening.py` pins exact parser/scanner behavior
and `tests/test_engine_offline.py` drives the engine; the remaining
survivors are dominated by equivalent mutants.

## License

MIT © RedRex
