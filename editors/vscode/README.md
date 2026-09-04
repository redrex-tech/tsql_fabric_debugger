# T-SQL Fabric Debugger — VS Code extension

Step through **T-SQL stored procedures on the Microsoft Fabric Warehouse**
right in the editor: set breakpoints in the gutter, watch variables update,
step in / over / out — with **ROLLBACK at the end** (nothing is persisted).
No debug scripting.

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

Click the **T-SQL Fabric** icon in the Activity Bar (left edge) to open a view
that lists every `.sql` procedure and `.ipynb` notebook in the open folder —
click one to open it. A `.sql` row has an inline **debug** button (▷) that
starts a debug session for it. The view title has two buttons: **refresh** the
list, and the **gear** to open this plugin's settings.

## Use

1. Open the `.sql` file with the `CREATE PROCEDURE` (or pick it in the sidebar).
2. Click the gutter to set a breakpoint (condition and hit count supported via
   the breakpoint's context menu).
3. Press **F5**. If server/database are not set, you are prompted once; the
   parameter values come from the launch config (`params`).
4. Use the debug toolbar: continue, step over, step in, step out. Inspect
   variables in the **Variables** pane, evaluate T-SQL in the **Debug Console**
   (`@a * @b`, `CASE WHEN @x > 0 THEN 1 ELSE 0 END`), and turn on
   *break on CATCH-handled errors* in the **Breakpoints** pane.

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
npm run build       # bundle to dist/extension.js (npm run watch to iterate)
npm run package     # produce the .vsix
```

Press F5 in this folder (the "Run extension (dev)" config) to launch an
Extension Development Host with the extension loaded.

## License

MIT — see [LICENSE](LICENSE).
