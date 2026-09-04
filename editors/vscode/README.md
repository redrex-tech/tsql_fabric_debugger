# T-SQL Fabric Debugger — VS Code extension

Step through **T-SQL stored procedures on the Microsoft Fabric Warehouse**
right in the editor: set breakpoints in the gutter, watch variables update,
step in / over / out, and **inspect the rows each step returns** in a grid —
all with **ROLLBACK at the end** (nothing is persisted). No debug scripting.
A **production guard** asks for confirmation before you debug against a
warehouse you flagged as production.

It is a thin front end over the [`tsql-fabric-debugger`](https://github.com/redrex-tech/tsql_fabric_debugger)
Python package, which ships the debug engine and a Debug Adapter Protocol
server. VS Code's debug UI talks to that server; this extension just wires
them together and fills in the launch configuration.

> **Status: preview / MVP.** The adapter is synchronous (there is no *pause*
> mid-run — use breakpoints), line breakpoints apply to the launched `.sql`
> only, and the session **never commits**.

## Requirements

1. **Python 3.10+** with the package installed into the interpreter you use:

   ```bash
   pip install tsql-fabric-debugger
   ```

   Verify the adapter is importable: `python -m tsql_fabric_debugger.dap`
   (it waits on stdin — press Ctrl+C).

2. **ODBC Driver 18 for SQL Server** on your machine.
3. A signed-in **Azure CLI** (`az login`) — the library authenticates through
   it (Entra ID). No passwords or tokens are stored.

## First run

1. Click the **T-SQL Fabric** icon in the Activity Bar. The **Fabric
   Workspace** view greets you with **Connect to Warehouse** and **Check
   setup**.
2. **Connect to Warehouse** — signs you in via `az login`, then lets you pick
   your **workspace → warehouse** from a list. The cryptic SQL endpoint and the
   database name are filled in for you; the connected warehouse shows in the
   status bar. (Prefer typing them? Set `tsqlFabric.server`/`database` in
   Settings — the gear button in the view.)
3. **Check setup** verifies the Azure sign-in and that the Python package is
   importable, and tells you how to fix whatever is missing.

Once connected, the **Fabric Workspace** view lists the notebooks your account
can see in that workspace — click one to open it in the Fabric web UI.

`tsqlFabric.pythonPath` *(optional)* — the interpreter that has the package.
Empty reuses the one picked by the Python extension, else `python3` on PATH.

## The T-SQL Fabric sidebar

Click the **T-SQL Fabric** icon in the Activity Bar (left edge) for three views:

- **Warehouse Procedures** — every deployed procedure, grouped by schema.
  Click one (or its ▷ button) to debug it: the extension asks for the input
  parameter values, then starts the session — no name typing, no launch.json.
- **Fabric Workspace** — the notebooks your account can see; click to open in
  VS Code (↗ opens in the Fabric web UI).
- **Project Files** — the local `.sql`/`.ipynb` in the open folder.

The status bar shows the connected warehouse; click it to **switch** between
saved connections or connect to a new one. When the connected warehouse is
flagged as production (see below) the status bar turns amber and reads
**Fabric (PROD)**.

### Result-set grid

When a step runs a `SELECT` (or any statement that returns rows), the rows are
shown two ways: printed as a text table in the **Debug Console**, and opened in
a **Result Set** grid beside the editor (columns as headers, `NULL` marked,
theme-aware, refreshed as each new result set arrives). A `(truncated)` note
appears when the engine capped the number of rows. No configuration needed.

### Works well alongside the mssql extension

This extension does the *debugging*. For IntelliSense, a results grid and
ad-hoc query editing, install Microsoft's **mssql** extension
(`ms-mssql.mssql`) — the two complement each other on the same warehouse.

## Debugging safely on a shared / production warehouse

A debug session opens a **real transaction** and executes statements as you
step. Nothing is committed (ROLLBACK on disconnect), **but** while you are
paused after an `INSERT`/`UPDATE`/DDL the transaction holds **locks** on those
tables — other sessions writing them wait until you continue or the session is
killed. Guardrails:

- **`tsqlFabric.lockTimeout`** (default **30s**) caps how long a step waits on
  a lock before failing, so a paused debug cannot block others indefinitely.
- **`tsqlFabric.stepTimeout`** (default off) caps how long a single step runs.
- **T-SQL Fabric: Kill Orphan Debug Sessions** (Command Palette) reaps the
  library's sessions left sleeping with an open transaction — for when a debug
  process died without disconnecting.
- **`tsqlFabric.productionWarehouses`** — list the warehouse names or endpoint
  substrings you treat as production (e.g. `["wh_prod", "prod.datawarehouse"]`).
  Starting a debug session against a match pops a **modal confirmation** first,
  and the status bar shows an amber **Fabric (PROD)** badge so you always know
  where you are pointing. Matching is a case-insensitive substring test against
  both the warehouse name and the SQL endpoint.
- Prefer a **dev/test warehouse** when debugging heavy load procedures.

## Use

1. Open the `.sql` file with the `CREATE PROCEDURE` (or pick it in the sidebar).
2. Click the gutter to set a breakpoint (condition and hit count supported via
   the breakpoint's context menu).
3. Press **F5**. It debugs the **file you have open** (`program: "${file}"`).
   If server/database are not set, you are prompted once; the parameter values
   come from the launch config (`params`). If F5 keeps running the *same* file,
   your `launch.json` has a fixed `program` path — change it to `"${file}"`, or
   pick **"Debug T-SQL procedure (current file)"** from the Run dropdown.
4. Use the debug toolbar: continue, step over, step in, step out. Inspect
   variables in the **Variables** pane, evaluate T-SQL in the **Debug Console**
   (`@a * @b`, `CASE WHEN @x > 0 THEN 1 ELSE 0 END`), and turn on
   *break on CATCH-handled errors* in the **Breakpoints** pane. Any rows a step
   returns open in the **Result Set** grid beside the editor.

### launch.json

Auto-generated on first F5, or add it yourself:

```jsonc
{
  "type": "tsql-fabric",
  "request": "launch",
  "name": "Debug T-SQL procedure",
  "program": "${file}",            // OR "procName": "pck_am.prd_crgodsrec"
  "params": { "@numAnoRef": 2015 },
  "server": "xxxx.datawarehouse.fabric.microsoft.com",
  "database": "my_warehouse",
  "stopOnEntry": true,
  // optional engine knobs:
  "logLevel": "simple",
  "maxLoopIterations": 1000,
  "historyBatches": null,
  "stepTimeout": null,
  "lockTimeout": null
}
```

`program` (a local `.sql`) and `procName` (a deployed procedure fetched from
the warehouse) are mutually exclusive — give one.

## How it works

```
VS Code debug UI  ──DAP over stdio──►  python -m tsql_fabric_debugger.dap
                                              │
                                        the debug engine ──►  Fabric Warehouse
                                        (one session, ROLLBACK on disconnect)
```

The extension launches the adapter with `<python> -m tsql_fabric_debugger.dap`
(or the command in `tsqlFabric.adapterCommand`). Everything else — breakpoints,
variable capture, step-into, CATCH emulation — is the Python engine.

## Development

```bash
npm install
npm run build        # bundle to dist/extension.js (npm run watch to iterate)
npm run package      # produce the .vsix
npm test             # unit tests (vitest) for the pure logic in src/util.ts
npm run mutation     # mutation testing (Stryker) — 100% score enforced
```

The pure, `vscode`-free logic (parameter coercion, introspection-output
parsing, variable matching, logpoint expressions) lives in `src/util.ts` and
is unit- and mutation-tested (`test/util.test.ts`, `stryker.conf.json`).

Press F5 in this folder (the "Run extension (dev)" config) to launch an
Extension Development Host with the extension loaded.

## License

MIT — see [LICENSE](LICENSE).
