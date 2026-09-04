# tsql-fabric-debugger — Technical Documentation

Everything the README summarizes, in depth: why this tool needs to exist,
how it works inside, every configuration knob, the full API, and how to use
it locally, in a Fabric notebook, and from AI agents / MCP setups.

- [1. Why the Fabric Warehouse has no debugger](#1-why-the-fabric-warehouse-has-no-debugger)
- [2. How the debugger works](#2-how-the-debugger-works)
- [3. Configuration](#3-configuration)
- [4. API reference](#4-api-reference)
- [5. Usage: local machine](#5-usage-local-machine)
- [6. Usage: Fabric notebook](#6-usage-fabric-notebook)
- [7. Usage: AI agents and MCP](#7-usage-ai-agents-and-mcp)
- [8. Limits and troubleshooting](#8-limits-and-troubleshooting)

---

## 1. Why the Fabric Warehouse has no debugger

Debugging T-SQL was never great, but it used to exist: SQL Server
Management Studio shipped a T-SQL debugger (breakpoints, step, locals) until
SSMS 17 — Microsoft removed it in SSMS 18, even for on-premises SQL Server.
The official guidance since then is "use PRINT and try/catch", which is
exactly the workflow this library replaces.

On the **Fabric Warehouse** the situation is stricter, for architectural
reasons:

- The warehouse engine is a **distributed query processor** (the Polaris
  engine). A statement is compiled into a distributed plan and executed
  across compute nodes; there is no single-threaded execution context an
  interactive debugger could attach to.
- The old SSMS debugger worked by attaching to the database engine through a
  dedicated debugging protocol with engine-side hooks. That surface simply
  does not exist in the Fabric SQL endpoint.
- A stored procedure arrives at the server as **one unit**. Clients get
  result sets and messages back — there is no protocol to pause between
  statements, inspect a variable, or resume.

Two T-SQL language facts make naive workarounds fail:

1. **Variables are batch-scoped.** If you cut a procedure into pieces and
   run them one at a time, every `DECLARE` dies at the end of its batch. The
   second piece cannot see `@total` from the first.
2. **Control flow spans statements.** `IF`/`WHILE`/`TRY/CATCH` structure
   cannot be executed piecemeal without something re-implementing the
   control flow.

So "just run half the proc" doesn't work, and "sprinkle PRINTs and re-run
the whole thing" costs a full execution per hypothesis — painful when a load
procedure takes minutes and mutates state. This library exists to give
Fabric the debugger the platform cannot provide natively — from the client
side, with no server-side installation and no change to the `.sql` file.

## 2. How the debugger works

### 2.1 Pipeline overview

```
 .sql file ──► scanner ──► parser ──► steps ──► engine ──► Fabric Warehouse
              (tokens)   (structure)  (plan)   (batches)     (one session)
```

Four modules, each with one job:

| Module | Job |
|---|---|
| `scanner.py` | Tokenize T-SQL without ever mistaking content inside strings, comments or `[brackets]` for code |
| `parser.py` | Extract the procedure (name, parameters, body) and slice the body into control-flow-aware steps |
| `engine.py` | Execute steps one batch at a time while preserving variable state, emulating CATCH, driving step-into |
| `connection.py` / `runner.py` / `cli.py` | Entra ID connections, batch mode, command line |

### 2.2 The scanner

`scan(sql)` walks the text once and emits tokens `{"k": kind, "s": start,
"e": end, "u": UPPER}` with kinds `w` (word), `str`, `brk` (`[..]`/`".."`),
`num`, `p` (punctuation). Strings honor `''` escapes, block comments nest
(`/* /* */ */`), brackets honor `]]`. Everything downstream operates on
tokens, which is why a `;` or a `COMMIT` inside a string literal never
confuses the parser — the classic failure mode of regex-based SQL tools.

### 2.3 The parser

`parse_params` reads the header (name, each parameter's type, default and
OUTPUT flag — with or without wrapping parentheses). `procedure_body`
locates the body and accepts all three legal shapes: a `BEGIN...END`
wrapper, a body starting straight at `BEGIN TRY`, or a bare statement list.

`split_steps` slices the body into **steps**:

- A plain statement ends at a level-0 `;` (parentheses and
  `BEGIN`/`CASE`/`END` depths are tracked; `BEGIN TRAN` is *not* a block
  opener). A level-0 `ELSE` also ends a statement — that is what makes
  `IF x SET a = 1 ELSE SET a = 2` (no `;` before the ELSE) parse correctly.
- An `IF`/`WHILE` becomes a single **block step** that additionally records
  its branch structure: for each branch, the token span of the condition and
  of the body. That structure is what `step_into()` consumes later.
- `BEGIN TRY` is unwrapped: its statements become normal steps, and the
  matching `BEGIN CATCH` block is parsed into a separate list. Every step
  carries `catch_ids` — the stack of enclosing TRYs — so an error knows both
  *which* CATCH to emulate and *how far* to skip afterwards.
- `RETURN` becomes its own step kind so the engine can stop where the real
  execution would stop.
- Every `DECLARE`, at any nesting depth, is discoverable
  (`scan_declares`) — T-SQL variable scope is the batch, not the block, so a
  variable declared inside an `IF` must remain visible to later steps.

### 2.4 The state engine

The core problem: **variables die between batches**. The engine solves it by
making Python the keeper of the "stack frame". Every step executes as a
batch shaped like this:

```sql
DECLARE @p1 INT, @v1 NVARCHAR(MAX), ...;      -- every known variable
SELECT @p1 = ?, @v1 = ?;                       -- re-inject current values
<original statement, byte-for-byte untouched>;
SELECT '__hcap__' AS [__hcap__],               -- capture sentinel
       @@ROWCOUNT AS [__rowcount__],
       @p1 AS [@P1], @v1 AS [@V1], ...;
```

Key details:

- **Re-injection is parameterized** (`?` placeholders). No value is ever
  concatenated into SQL — no escaping bugs, no injection surface. Strings
  bind as `nvarchar(max)` via `setinputsizes` (Fabric's UTF-8 collation
  rejects the legacy `ntext` type pyodbc would otherwise pick for long
  strings).
- **The capture is the last result set**, marked with a sentinel column so
  result sets produced by the statement itself (diagnostic SELECTs) can be
  fetched and kept separately instead of discarded.
- **`@@ROWCOUNT` semantics survive**: it is captured immediately after the
  statement inside the same batch, and any *reference* to `@@ROWCOUNT` in
  the next step is rewritten to a bound parameter carrying that value.
- **The ERROR_*() family survives** the same way: inside an emulated CATCH,
  `ERROR_MESSAGE()`, `ERROR_NUMBER()`, `ERROR_PROCEDURE()` and
  `ERROR_LINE()` are rewritten to parameters carrying what Python captured
  from the driver exception (`ERROR_SEVERITY()`/`ERROR_STATE()` fall back to
  16/1 — the driver does not expose them).
- **Statements declared inside the step** (a `DECLARE` inside an atomic
  block) are excluded from the batch prologue so the declaration is not
  duplicated — but still captured, since after the statement they exist in
  the batch.
- **Table variables** are declared in every batch (so references compile)
  but never injected or captured — there is no scalar round-trip for them,
  and the debugger warns that their content does not survive across steps.
- **Session `SET` options** (`SET NOCOUNT ON`, `SET XACT_ABORT ON`, ...)
  execute in an *unparameterized* batch: pyodbc routes parameterized batches
  through `sp_prepexec`, where SET options revert at batch end; direct
  execution makes them persist for the session, as a real run would.

### 2.5 CATCH emulation

A step that fails raises a pyodbc exception. The engine records the error,
rolls back (a failed Fabric transaction is doomed — nothing else can run in
it), then emulates the T-SQL rules:

1. The CATCH block **of the failing TRY** runs, step by step, with the
   ERROR_*() rewrites active. `DECLARE`, `RETURN` and bare `THROW` inside
   the CATCH behave as they would in a real run (`RETURN`/`THROW` end the
   debug).
2. Execution then **continues after `END CATCH`**: every remaining step
   whose `catch_ids` stack contains the failed TRY — nested TRY blocks
   included — is skipped.
3. Because the rollback wiped the data effects of *earlier* steps (Fabric
   dooms the whole transaction), the log marks every subsequent entry with
   `post_rollback=True` and the console warns that following results may
   diverge from a real execution. This is the one place where emulation and
   reality necessarily differ; the debugger's job is to make it loud.

### 2.6 step_into: real granularity inside blocks

`step()` runs an `IF`/`WHILE` as one atomic block. `step_into()` gives
statement-level granularity:

- For an `IF`: each branch condition is evaluated **server-side**, with the
  current variables, as `CASE WHEN <condition> THEN 1 ELSE 0 END` appended
  to a capture batch (logged as a `cond` entry that does not disturb
  `@@ROWCOUNT`). The chosen branch's body is sliced into sub-steps and
  spliced into the step queue.
- For a `WHILE`: Python drives the loop — evaluate the condition, queue one
  iteration's sub-steps plus a re-check step, repeat, bounded by
  `max_loop_iterations`. (`BREAK`/`CONTINUE` in the body fall back to atomic
  mode with a warning.)
- A nested `BEGIN TRY` inside an expanded branch registers its own CATCH —
  exactly once, even across WHILE iterations (span-keyed registration).

### 2.7 Stepping into a nested EXEC

`step_into()` on a plain `EXEC [@ret =] schema.proc [args]` statement builds a
**child debugger**:

1. The child's source is fetched from the warehouse on the same session
   (`OBJECT_DEFINITION` — it even sees procedures created uncommitted in this
   transaction; you need VIEW DEFINITION permission).
2. Call arguments are matched to the child's parameters (positional and
   named; literals and parent variables — T-SQL allows nothing else in EXEC
   arguments), and defaults fill the gaps.
3. The child `TSQLDebugger` ADOPTS the parent's session and transaction — it
   never opens or closes a connection of its own — and is returned to you.
   Debug it exactly like the parent (`child.step()`, `child.step_into()` for
   grandchildren, `child.run_all()`).
4. When the child finishes, the parent's next `step()` collects it: OUTPUT
   arguments copy back into the parent's variables, `@@ROWCOUNT` carries
   over, and the EXEC step is logged (`kind="exec"`). A child that ended in
   an **unhandled error** (no CATCH, a `THROW`, or a failing CATCH)
   propagates instead: the EXEC records as ERROR and the parent's own CATCH
   is emulated — the same thing the real EXEC would do.

While a child is active the parent refuses to move (`step`/`step_into`/
`run_step`/`run_all`/`run_until` all point you back to the child);
`abort_child()` discards it. Dynamic calls (`EXEC(@sql)`, `sp_executesql`),
unavailable sources and expression arguments fall back to a plain step-over
with a notice. Remember the shared transaction: a child error rolls back the
parent's data effects too (announced, and marked via `post_rollback`).

### 2.8 Transaction model

The session opens with `autocommit=False`: one transaction from the first
step until `close()`. `close()` (and the context-manager exit) rolls back by
default — `commit=True` is an explicit decision. Corollaries worth knowing:

- Locks acquired by completed steps are held until `close()` — debug on a
  dev warehouse, and close as soon as you're done.
- A `COMMIT` *inside* the procedure commits the debugger's transaction and
  escapes the rollback guarantee. The parser warns about literal transaction
  keywords, about the same keywords spotted inside string literals (dynamic
  SQL), and flags `EXEC` calls as opaque.
- `autocommit=True` flips the model: every step persists immediately (the
  constructor warns; `close()` undoes nothing).

## 3. Configuration

### 3.1 Constructor

```python
TSQLDebugger(
    sql_file=None,            # path to the .sql — OR —
    sql_text=None,            # the T-SQL as a string
    params=None,              # {"@year": 2015} — test values for parameters
    server=None,              # SQL endpoint; falls back to $FABRIC_TSQL_SERVER
    database=None,            # warehouse name; falls back to $FABRIC_TSQL_DATABASE
    autocommit=False,         # True = every step persists immediately (warned)
    log_level="simple",       # "full" = whole commands, values, batch on error
    stop_on_error=True,       # stop the sequential run on an unhandled error
    preview_chars=500,        # command truncation in the log
    max_loop_iterations=1000, # step_into guard for WHILE loops
    step_timeout=None,        # seconds per step (None = unlimited)
    max_result_rows=50,       # rows kept per procedure-produced result set
    echo=print,               # console sink — pass any callable
)
```

Parameter handling: keys in `params` are case-insensitive and the leading
`@` is optional. Header defaults fill parameters you don't pass — simple
literals (`N'...'`, numbers, `NULL`) are evaluated in Python; expressions
are evaluated server-side on first connect. Parameters with neither a value
nor a default stay `NULL` (with a warning). `OUTPUT` parameters are ordinary
debug variables — inspect them at any step.

### 3.2 Environment variables

| Variable | Meaning |
|---|---|
| `FABRIC_TSQL_SERVER` | SQL endpoint, e.g. `xxxx.datawarehouse.fabric.microsoft.com` |
| `FABRIC_TSQL_DATABASE` | Warehouse (or SQL-endpoint database) name |

### 3.3 Connection details

- Driver: **ODBC Driver 18 for SQL Server**; `Encrypt=yes`, no
  `TrustServerCertificate`.
- Authentication is **Entra ID only**, resolved automatically: inside a
  Fabric notebook, the notebook's own token
  (`notebookutils.credentials.getToken`); anywhere else,
  `azure.identity.AzureCliCredential` — your `az login`. No password
  support, by design.
- The session identifies itself as `APP=tsql-fabric-debugger`
  (`sys.dm_exec_sessions.program_name`), and `LoginTimeout=30` bounds the
  connect phase.
- The connection is **lazy**: constructing a `TSQLDebugger` only parses; the
  session opens on the first executed step (which also applies pending
  parameter defaults, once).

### 3.4 File encodings

`.sql` files are read as UTF-8 (with or without BOM), UTF-16 when a BOM says
so (the SSMS default), and cp1252 as the last resort — legacy files with
accents just work.

### 3.5 CLI flags

```
tsql-debug FILE.sql [--server S] [--database D]
           [--param @NAME=VALUE]...    # quote the value ('00123') to force string
           [--commit] [--csv FILE] [--log-level simple|full]
           [--step-timeout SECONDS] [--version]
```

Unquoted `--param` values infer types strictly: integers without leading
zeros, decimals like `1.5` — never scientific notation, `nan` or `inf`;
`NULL` (any case) means SQL NULL; anything else stays a string. Exit codes:
`0` clean, `1` at least one step recorded an ERROR (even if the procedure's
own CATCH handled it), `2` usage/file error. A file without a
`CREATE PROCEDURE` falls back to loose-script mode with a warning
(`--param`/`--log-level` don't apply there; `--commit` does).

## 4. API reference

Everything importable from `tsql_fabric_debugger`:

### 4.1 `TSQLDebugger`

Navigation:

| Method | Behavior |
|---|---|
| `list_steps()` | Print the numbered step plan without executing. `*` marks the cursor; indentation marks sub-steps from `step_into()`. Numbers change after an expansion — re-list before using them. |
| `step()` | Run the next step ("step over": a whole `IF`/`WHILE` at once) and advance the cursor. Returns the log entry — on failure, the *error* entry, after emulating the CATCH. |
| `step_into()` | Enter the next step when it is an `IF`/`WHILE` (see §2.6); otherwise identical to `step()`. |
| `run_all()` | Run to the end, or until an unhandled error. Returns `log_df()`. |
| `run_until(n)` | Run up to step `n` inclusive — the breakpoint idiom. |
| `jump_to(n)` | Move the cursor to step `n` without executing anything before it (warned: skipped assignments did not run — use `set_var`). |
| `run_step(n, emulate_catch=False)` | Run *only* step `n` with the current environment; the cursor does not move. |

State:

| Method | Behavior |
|---|---|
| `show_vars()` | Print and return every variable's current value (OUTPUT params included; table variables appear with a sentinel string). |
| `set_var(name, value)` | Manually set a variable (rejects table variables). The value is re-injected from the next batch on. |
| `sql(query)` | Ad-hoc query **on the same session** — sees uncommitted state. Capped at 10,000 rows; on failure the transaction is rolled back and the error re-raised. |
| `rollback()` | Undo all data effects so far, keep the session and the captured variables. Truthful: refuses under autocommit and reports a dead session. |
| `save_state(path=None)` / `load_state(source)` | Snapshot/restore the variable environment as JSON (datetime, Decimal and bytes round-trip). Pair with `jump_to()` to resume a long debug another day without replaying the steps. |
| `reset()` | Roll back, close, restore the pristine step plan (undoing expansions) and the initial parameter values, clear the log — the next step() replays from step 1 on a fresh session. Watches and breakpoints survive. |

Watches and breakpoints:

| Method | Behavior |
|---|---|
| `watch(expr, name=None)` | Track a T-SQL expression after every step — it is appended to each capture batch (e.g. `"(SELECT COUNT(*) FROM stg.t)"`). Values echo per step and are returned by `watches()`. A watch that references a dropped object fails the next step — `unwatch()` it. |
| `unwatch(name=None)` | Remove one watch, or all of them. |
| `break_at(line, condition=None)` | Stop `run_all()` BEFORE any step at this **file** line (stable across expansions, unlike step numbers). The optional condition is T-SQL, evaluated server-side with the current variables. `run_all()` auto-expands IF/WHILE blocks that contain a breakpoint line, so loop-body breakpoints just work. Resuming `run_all()` continues past the stop. |
| `clear_breaks(line=None)` / `breaks()` | Remove/inspect breakpoints. |

Nested EXEC:

| Method | Behavior |
|---|---|
| `step_into()` on an EXEC step | Returns a **child debugger** sharing the session (see §2.7): debug the called procedure statement by statement. Falls back to step-over for dynamic SQL, missing sources or expression arguments. |
| `abort_child()` | Discard an active child without collecting OUTPUTs; the EXEC step stays pending (step() runs it whole, step_into() re-enters). |

Inspection:

| Method | Behavior |
|---|---|
| `log_df()` | The execution log as a DataFrame (list of dicts without pandas). Columns: `step`, `line` (file line), `kind`, `status`, `rows_affected` (None when not measured), `duration_s`, `command`, `changed_vars` (truncated), `result_sets`, `post_rollback`, `error`. |
| `show_detail(step_no=None)` | One LOG entry in full: untruncated command and changed values, the clean and the raw driver error, captured result sets, and the exact SQL batch the debugger executed. |
| `last_results(step_no=None)` | Result sets the procedure itself produced in that entry, as DataFrames (`df.attrs["truncated"]` marks the `max_result_rows` cut). |
| `set_log_level(level)` | Switch `"simple"`/`"full"` mid-debug. |

Lifecycle:

| Method | Behavior |
|---|---|
| `close(commit=False)` | Rollback (default) or commit, then close — exception-safe: the connection is released even if the final rollback fails. Returns `log_df()`. |
| `with TSQLDebugger(...) as dbg:` | Context manager — `close(commit=False)` guaranteed on exit, even on exceptions. The recommended form. |

### 4.2 Module functions

| Function | Behavior |
|---|---|
| `run_procedure(sql_file/sql_text, params, server, database, commit=False, save_csv=None, **kwargs)` | Construct, `run_all()`, `close()` in a try/finally, optionally save the CSV. `**kwargs` forward to the constructor (`log_level`, `step_timeout`, ...). |
| `run_script(sql_file/sql_text, server, database, stop_on_error=True, commit=False)` | Loose scripts (no `CREATE PROCEDURE`): split on `GO` lines, else per statement via the scanner; executed batch-by-batch inside a transaction, ROLLBACK by default. No variable preservation. |
| `connect(server, database, autocommit=True)` | A raw authenticated pyodbc connection with the library's auth chain — useful for your own tooling. |
| `runner.split_script(sql_text)` | The batch splitter, importable on its own. |
| `runner.save_log_csv(log, path)` | CSV persistence that works with or without pandas (utf-8-sig). |
| `runner.count_errors(log)` | ERROR-entry count for either log shape. |
| `diff_logs(log_a, log_b)` | Align two execution logs by (line, kind) and report only the divergences — different status/rows/variables, and steps present on one side only. |
| `parser.read_sql_file(path)` | The multi-encoding file reader. |

The lower layers (`scanner.scan`, `parser.split_steps`, ...) are importable
and stable enough to build tooling on, but the supported public surface is
the list above.

## 5. Usage: local machine

One-time setup (macOS shown; Linux analogous with apt/yum + Microsoft's
repo):

```bash
brew tap microsoft/mssql-release https://github.com/Microsoft/homebrew-mssql-release
HOMEBREW_ACCEPT_EULA=Y brew install msodbcsql18
pip install "tsql-fabric-debugger[local,pandas]"
az login          # the same identity you use for Fabric
```

Set the endpoint once:

```bash
export FABRIC_TSQL_SERVER="<endpoint>.datawarehouse.fabric.microsoft.com"
export FABRIC_TSQL_DATABASE="my_warehouse"
```

Then debug from any REPL (`python -i`, IPython, VS Code interactive):

```python
from tsql_fabric_debugger import TSQLDebugger

with TSQLDebugger("prd_load.sql", params={"@year": 2015}) as dbg:
    dbg.list_steps()
    dbg.run_until(14)
    dbg.show_vars()
    dbg.sql("SELECT COUNT(*) FROM stg.movements")   # uncommitted state
    dbg.step_into()                                  # enter the IF/ELSE chain
    dbg.step()
# exiting the with-block rolled everything back

# batch mode / CI:
#   tsql-debug prd_load.sql --param @year=2015 --csv log.csv
```

The endpoint string is in the Fabric portal: warehouse → settings → *SQL
connection string*.

## 6. Usage: Fabric notebook

Inside a Fabric Python notebook, authentication is automatic — the debugger
uses the notebook's own Entra token; no `az login`, no secrets:

```python
%pip install tsql-fabric-debugger

from tsql_fabric_debugger import TSQLDebugger

dbg = TSQLDebugger(
    "/lakehouse/default/Files/prd_load.sql",   # or builtin Files path
    params={"@year": 2015},
    server="<endpoint>.datawarehouse.fabric.microsoft.com",
    database="my_warehouse",
)
dbg.step()
dbg.show_vars()
dbg.close()
```

Notebook-specific notes:

- Cell-by-cell interaction maps naturally: one cell per `step()` /
  `show_vars()` / `sql()` call, and the console output renders under the
  cell.
- **Re-running the constructor cell without `close()`** leaves the previous
  session's transaction open (holding locks) until the kernel dies. Use the
  `with` form inside a single cell, or make `dbg.close()` a habit before
  re-creating.
- The procedure source can come from anywhere — Lakehouse Files, a mounted
  repo, or a string: `TSQLDebugger(sql_text=ddl_string, ...)` pairs well
  with fetching the deployed definition from
  `sys.sql_modules`/`OBJECT_DEFINITION()` when the file and the warehouse
  have drifted.

## 7. Usage: AI agents and MCP

The debugger was built to be **drivable by software**, not only by a human
at a prompt — every interaction is a method call with structured returns,
and the console stream is an injectable callable. That makes it a natural
tool for AI coding agents (Claude Code, Copilot Workspace, custom
LangChain/AutoGen agents) and for MCP (Model Context Protocol) setups.

### 7.1 Agent-friendly design

- `echo=` accepts any callable: an agent can capture the console narrative
  into a buffer and feed it to the model instead of stdout.
- Every method returns data (log entries, dicts, DataFrames) — nothing
  meaningful lives only in printed text.
- Batch mode (`run_procedure`, or the `tsql-debug` CLI with `--csv` and
  meaningful exit codes) suits non-interactive agent loops: run, read the
  log, form a hypothesis, edit, run again.
- Rollback-by-default makes agent-driven exploration safe: an agent can
  execute a suspicious step to *observe* it without persisting anything.

A minimal agent loop:

```python
from tsql_fabric_debugger import TSQLDebugger

console = []
with TSQLDebugger(sql_text=proc_source, params=test_params,
                  echo=console.append, step_timeout=120) as dbg:
    log = dbg.run_all()
errors = [e for e in (log.to_dict("records") if hasattr(log, "to_dict") else log)
          if e["status"] == "ERROR"]
# hand `errors`, `console`, and dbg-captured variables to the model
```

### 7.2 Alongside a Fabric MCP server

If your agent already talks to the warehouse through an MCP server (one that
exposes `execute_sql` / metadata tools for Fabric), the two compose — this
library was itself debugged that way during development:

- The **MCP server** answers questions: schemas, row counts, the deployed
  procedure text (`SELECT definition FROM sys.sql_modules WHERE object_id =
  OBJECT_ID('dbo.prd_load')`).
- The **debugger** answers *behavior*: which branch ran, what `@sql` looked
  like before the `EXEC`, which statement raised, what the variables held at
  that moment.

A productive agent recipe: fetch the deployed source via MCP, feed it to
`TSQLDebugger(sql_text=...)`, reproduce the failure under rollback, and use
the MCP's read tools to inspect reference data on a *separate* session while
the debug session holds its own transaction. (Remember the two sessions have
different visibility: the MCP session will not see the debugger's
uncommitted writes — use `dbg.sql()` for those.)

### 7.3 Exposing the debugger AS an MCP server

The API maps one-to-one onto MCP tools if you want interactive debugging as
a first-class agent capability. A sketch with the official `mcp` package
(FastMCP):

```python
from mcp.server.fastmcp import FastMCP
from tsql_fabric_debugger import TSQLDebugger

mcp = FastMCP("tsql-debugger")
sessions: dict[str, TSQLDebugger] = {}

@mcp.tool()
def open_debug(session_id: str, sql_text: str, params: dict,
               server: str, database: str) -> str:
    console = []
    dbg = TSQLDebugger(sql_text=sql_text, params=params,
                       server=server, database=database, echo=console.append)
    dbg._console = console          # keep the narrative with the session
    sessions[session_id] = dbg
    return "\n".join(console)

@mcp.tool()
def step(session_id: str) -> dict:
    dbg = sessions[session_id]
    entry = dbg.step()
    return {"entry": entry, "console": dbg._console[-20:]}

@mcp.tool()
def show_vars(session_id: str) -> dict:
    return sessions[session_id].show_vars()

@mcp.tool()
def close_debug(session_id: str, commit: bool = False) -> str:
    sessions.pop(session_id).close(commit=commit)
    return "closed"
```

Wrap the remaining methods (`step_into`, `run_until`, `sql`,
`show_detail`, ...) the same way. Operational cautions for such a server:
one debugger per session id (instances are not thread-safe), always expose
`close`, and consider an idle reaper that closes abandoned sessions — an
abandoned debug session is an open transaction holding locks on the
warehouse. Authentication follows the server's identity (its `az login` or
managed identity), so scope that identity to a dev warehouse.

## 8. Limits and troubleshooting

Documented limits (see also the README):

- Table variable **content** does not cross steps (declared everywhere,
  warned once).
- `step_into` on a `WHILE` containing `BREAK`/`CONTINUE` falls back to
  atomic execution.
- `GOTO`, cursors and `WAITFOR` are out of scope.
- After an error, the rollback wipes earlier data effects while variables
  survive — subsequent steps carry `post_rollback=True` and a warning.
- `ERROR_SEVERITY()`/`ERROR_STATE()` in an emulated CATCH return 16/1;
  `ERROR_LINE()` returns the *file* line of the failing step.
- `;`-less legacy T-SQL is supported: statements also split on the next
  statement-starting keyword at level 0 (INSERT..SELECT, UPDATE..SET and
  WITH..consumer stay whole; MERGE still requires its `;`, as T-SQL does).

Common issues:

| Symptom | Cause / fix |
|---|---|
| `Provide server and database...` | Neither arguments nor `FABRIC_TSQL_*` env vars set. |
| `[FATAL] failure outside SQL execution: ...` on the first step | Connection/auth problem — token expired (`az login`), wrong endpoint, missing ODBC driver. The debugger refuses to swallow these. |
| `Data source name not found` / driver errors | ODBC Driver 18 not installed (see §5). |
| Steps hang forever | No `step_timeout` set and a heavy statement — set one; remember locks held by the open transaction can also block *other* sessions. |
| `[TRANSACTION] rollback FAILED — session likely dead` | The service killed the idle session (long pause). `close()` and start a new debugger; nothing was persisted. |
| Values look right but the real run differs | Check the warnings: inner `COMMIT`/dynamic SQL, RLS applying to *your* identity, `post_rollback` marks, table variables. |
