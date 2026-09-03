# tsql-fabric-debugger

Step-by-step debugger for T-SQL stored procedures on the **Microsoft Fabric
Warehouse** — without changing a single line of your `.sql`.

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

## Interactive usage

```python
from tsql_fabric_debugger import TSQLDebugger

dbg = TSQLDebugger(
    "prd_load.sql",                        # the original .sql, untouched
    params={"@year": 2015},                # test values
    server="<endpoint>.datawarehouse.fabric.microsoft.com",
    database="my_warehouse",
)

dbg.list_steps()      # list the numbered steps, without executing
dbg.step()            # run the next step ("step over": whole IF/WHILE)
dbg.step_into()       # step into an IF/WHILE: evaluates the condition
                      #   server-side, picks the branch, yields sub-steps
dbg.run_until(15)     # run up to step 15 (breakpoint)
dbg.show_vars()       # state of every variable (OUTPUT params included)
dbg.sql("SELECT COUNT(*) FROM dbo.movements")   # query on the SAME session
dbg.jump_to(17)       # move the cursor without running earlier steps
dbg.set_var("@sqlSrc", "...")                   # build state by hand
dbg.run_step(17)      # run ONLY step 17 (does not move the cursor)
dbg.set_log_level("full")   # full command + SQL batch on errors
dbg.show_detail()     # last step in full (command + error + batch)
dbg.last_results()    # result sets the procedure itself produced
dbg.close()           # ROLLBACK and close (commit=True to persist)
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
from tsql_fabric_debugger import run_procedure
df_log = run_procedure("prd_load.sql", params={"@year": 2015},
                       server=..., database=..., save_csv="log.csv")
```

```bash
tsql-debug prd_load.sql --param @year=2015 \
    --server <endpoint> --database my_warehouse --csv log.csv
```

Server and database can also come from the `FABRIC_TSQL_SERVER` and
`FABRIC_TSQL_DATABASE` environment variables. Files without a
`CREATE PROCEDURE` go through `run_script()` (split on `GO`/`;`).

## Security

- **Entra ID** authentication only: the notebook token on Fabric,
  `AzureCliCredential` elsewhere. No passwords, ever.
- Execution inside a transaction with ROLLBACK by default;
  `close(commit=True)` is an explicit decision.
- Variable re-injection through pyodbc parameters — no value concatenation
  into SQL.

## Semantics preserved

- `RETURN` (including guard clauses inside an `IF`) ends the debug, just as
  it would end the real execution.
- Each `BEGIN TRY` gets **its own** emulated `CATCH`; after the CATCH handles
  the error, the debug skips the rest of that TRY and continues after
  `END CATCH` — the same T-SQL semantics.
- `SELECT`s produced by the procedure itself (diagnostics, samples) are
  captured and displayed (`last_results()`), not discarded.
- Procedures with inner `COMMIT`/`BEGIN TRAN` raise a parse-time warning: an
  inner COMMIT persists data even with the debugger's default ROLLBACK.
- `step_timeout=<seconds>` in the constructor bounds every step — an
  unfiltered UPDATE won't hang the session holding locks.

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

The scope covers the pure modules (`scanner`/`parser`), which the offline
suite fully exercises — `engine`/`connection`/`runner`/`cli` are excluded
because they need a real Warehouse (env-gated integration tests), and
mutants there would survive for lack of an environment, not lack of a test.
The tests in `tests/test_mutation_hardening.py` pin the exact behavior
(token positions, branch spans, step texts) and keep ~85% of mutants caught;
the remainder is dominated by equivalent mutants.

## License

MIT © RedRex
