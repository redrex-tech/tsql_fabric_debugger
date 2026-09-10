# T-SQL Fabric Debugger

**Debug your T-SQL stored procedures on the Microsoft Fabric Warehouse — with real breakpoints, step-by-step execution, and variable inspection — right inside VS Code.** Then version them in Git (any provider) and deploy back to Fabric, without leaving the editor.

Fabric's Warehouse has no built-in debugger. This extension gives you one: it slices a procedure into steps and runs them one at a time on a single session, so you can pause, look at variables, and see the rows each statement returns. Every debug run ends in **ROLLBACK** — nothing is written unless you explicitly deploy.

> **At a glance:** breakpoints · step in/over/out · watch variables · result-set grid · pull/edit/deploy procedures & notebooks · commit, push & open a PR to GitHub/GitLab/Bitbucket/Azure DevOps.
>
> **Status: preview.** The debugger is synchronous (no *pause* mid-run — use breakpoints); a debug session never commits.

---

## New here? (30-second primer)

If some of these words are new, this is for you:

- **Warehouse** — a Fabric database you query with T-SQL.
- **Stored procedure** — saved T-SQL code (a `CREATE PROCEDURE …`) that runs on the warehouse.
- **Debugging** — running that code **one line at a time**, pausing to inspect variable values, instead of running it all at once and guessing what happened.
- **Breakpoint** — a red dot you click in the left margin; execution **pauses** there so you can look around.

You don't need to write any scripts to debug — the extension does the wiring.

---

## Quick start (your first debug in ~5 minutes)

1. **Install the prerequisites** (once) — see [Requirements](#requirements) below: Python + the `tsql-fabric-debugger` package, the ODBC driver, and `az login`.
2. Click the **T-SQL Fabric** icon (the dinosaur 🦖) in the Activity Bar on the left.
3. Click **Connect to Warehouse** → sign in → pick your **workspace → warehouse** from the list. The endpoint and database fill in automatically.
4. Open the **Warehouse Procedures** view, hover a procedure, and click **▷ Debug this procedure**. Enter any input parameters when asked.
5. Execution **pauses on the first line**. Use the debug toolbar:
   - **Step Over (F10)** — run the next statement and pause again.
   - **Continue (F5)** — run until the next breakpoint (or the end).
   - Watch values in the **Variables** pane; see returned rows in the **Result Set** grid.

That's it — you're debugging a real procedure, safely (it rolls back at the end).

> **Prefer editing locally first?** Click **Open Source (edit & debug locally)** on a procedure to save it as a `.sql` in your project, set breakpoints in it, and press **F5**.

---

## Requirements

1. **Python 3.10+** with the debugger package installed in the interpreter you use:
   ```bash
   pip install tsql-fabric-debugger
   ```
   Point the extension at it with `tsqlFabric.pythonPath` if it isn't your default interpreter.
2. **ODBC Driver 18 for SQL Server** — [download](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server).
3. **Azure CLI signed in** — run `az login` in a terminal. The extension authenticates through it (Entra ID); **no passwords or tokens are stored**.

Not sure what's missing? Run **T-SQL Fabric: Check Setup** — it verifies each piece and tells you how to fix it.

---

## The sidebar (three views)

Click the 🦖 icon in the Activity Bar:

| View | What it shows | What you can do |
| --- | --- | --- |
| **Warehouse Procedures** | Every deployed procedure, grouped by schema | ▷ Debug · 📄 Open source locally · 🚀 Deploy |
| **Fabric Workspace** | Notebooks your account can see | Open in VS Code · ↗ open in Fabric · ⬇ Save to project · 🔄 Sync |
| **Project Files** | Local `.sql` / `.ipynb` / Fabric `.py` in your folder | ▷ Debug · ☁ Export · 🚀 Deploy · ☁ Update notebook · commit/push · 🔀 PR |

The **status bar** shows the connected warehouse — click it to switch warehouses. It turns **amber (PROD)** when the warehouse is flagged as production.

The **gear ⚙** at the top of every view opens the settings.

---

## Debugging in depth

### Where execution pauses

The debugger stops **at the start of a statement**. Two things surprise people:

- **It pauses on the first line even without a breakpoint.** That's *stop on entry* — normal debugger behavior, so you can inspect before anything runs. From there, **Step Over (F10)** goes line by line; **Continue (F5)** runs to the next breakpoint or the end. (Set `"stopOnEntry": false` in `launch.json` to skip it.)
- **Breakpoints only fire on runnable statement lines.** A breakpoint on `BEGIN`/`END`, a blank line, a comment, or a continuation line of a multi-line statement never triggers. The extension marks the lines that *do* accept a breakpoint with a dim **`◦ breakpoint`** hint at the end of the line (change or disable it with `tsqlFabric.breakpointLineHint`). Statements **inside** `IF`/`WHILE`/`CATCH` blocks are breakpoint-able too.

### What you get while paused

- **Variables** pane — every `@variable` and its current value.
- **Result Set grid** — when a step returns rows (a `SELECT`), they open in a grid beside the editor (and print to the Debug Console). Multiple result sets stack; a `(truncated)` note shows when the row count was capped. Export one with **Export Result Set to CSV**.
- **Debug Console (REPL)** — type a T-SQL expression to evaluate with the current variables: `@a * @b`, `CASE WHEN @x > 0 THEN 1 ELSE 0 END`.
- **Conditional & hit-count breakpoints** — right-click a breakpoint to add a condition or a hit count.
- **Logpoints** — a breakpoint that logs a value without stopping (the diamond).
- **Break on CATCH-handled errors** — toggle in the Breakpoints pane to pause on errors your `TRY/CATCH` swallows.

### Two ways to start a debug

- **From the sidebar** — ▷ on a procedure (deployed) or on a `.sql` (local file).
- **F5** — debugs the **file you have open**. If it keeps running the *same* file, your `launch.json` pinned a `program` path — set it to `"${file}"`, or pick **"Debug T-SQL procedure (current file)"** from the Run dropdown.

### launch.json (optional — advanced)

Auto-generated on first F5, or write it yourself:

```jsonc
{
  "type": "tsql-fabric",
  "request": "launch",
  "name": "Debug T-SQL procedure",
  "program": "${file}",            // OR "procName": "dbo.load_sales"
  "params": { "@year": 2015 },
  "server": "xxxx.datawarehouse.fabric.microsoft.com",
  "database": "my_warehouse",
  "stopOnEntry": true,
  // optional engine knobs:
  "logLevel": "simple",
  "maxLoopIterations": 1000,
  "stepTimeout": null,
  "lockTimeout": null
}
```

`program` (a local `.sql`) and `procName` (a deployed procedure fetched from the warehouse) are mutually exclusive — give exactly one.

---

## Fabric CI/CD — with any Git provider

A complete loop without leaving VS Code, working with **GitHub, GitLab, Bitbucket, or Azure DevOps**. This matters because Fabric's *native* Git integration only supports GitHub and Azure DevOps — for **GitLab/Bitbucket, this extension is the bridge**.

1. **Pull** — *Sync with Fabric → Pull* downloads every procedure (`.sql`) and notebook (native `.py`) into your repo folder, organized by `tsqlFabric.fileLayout`. (Or one at a time: *Open Source* / *Save Notebook to Project*.)
2. **Debug & edit** locally.
3. **Commit & Push** — *Commit & Push Fabric folder* stages the folder and pushes with your existing Git credentials.
4. **Create PR / MR** — opens a pull/merge request for the current branch → `tsqlFabric.git.baseBranch` via the provider's API. GitHub uses the built-in sign-in (no token); GitLab/Bitbucket use a token you set once with *Set Git Provider Token*; Azure DevOps opens the browser.
5. **Deploy** — *Deploy to Fabric* runs a procedure's `CREATE OR ALTER` on the warehouse; *Update Notebook in Fabric* republishes a notebook; *Sync with Fabric → Deploy* does the whole folder. **Every warehouse write asks for confirmation** and honors the production guard.

Provider tokens are stored in VS Code **SecretStorage**, never in settings.

---

## Safety on a shared / production warehouse

A debug session opens a **real transaction**. Nothing is committed (ROLLBACK on disconnect), **but** while you're paused after an `INSERT`/`UPDATE`/DDL it holds **locks** — other writers wait until you continue or the session ends. And *Deploy* is a **committed write**. Guardrails:

- **`tsqlFabric.productionWarehouses`** — list names/endpoint substrings you treat as production (e.g. `["wh_prod", "prod.datawarehouse"]`). Debugging or deploying against a match pops a **modal confirmation**, and the status bar shows an amber **PROD** badge.
- **`tsqlFabric.lockTimeout`** (default **30s**) — a paused step gives up a lock instead of blocking others forever.
- **`tsqlFabric.stepTimeout`** — cap how long one step may run.
- **Kill Orphan Debug Sessions** (Command Palette) — reap sessions left open by a crashed debugger.
- Prefer a **dev/test warehouse** for heavy or destructive procedures.

---

## Settings reference

Open with the **⚙** in any view, or `Cmd/Ctrl+,` → search `tsqlFabric`. Grouped into **Connection**, **Debugging**, and **Local Files & Git**.

| Setting | Default | What it does |
| --- | --- | --- |
| `tsqlFabric.server` | `""` | Warehouse SQL endpoint (filled by *Connect*) |
| `tsqlFabric.database` | `""` | Warehouse name |
| `tsqlFabric.pythonPath` | `""` | Interpreter with the package (empty = Python extension's, else `python3`) |
| `tsqlFabric.adapterCommand` | `""` | Override the adapter command |
| `tsqlFabric.lockTimeout` | `30` | Seconds a step waits on a lock before failing |
| `tsqlFabric.stepTimeout` | `0` | Seconds a step may run (`0` = no limit) |
| `tsqlFabric.productionWarehouses` | `[]` | Warehouses to guard as production |
| `tsqlFabric.breakpointLineHint` | `label` | `label` / `bar` / `off` — how breakpoint-able lines are marked |
| `tsqlFabric.localFolder` | `fabric` | Project subfolder for pulled/generated files |
| `tsqlFabric.fileLayout` | `schema-type` | `schema-type` / `schema` / `type` / `flat` — folder layout |
| `tsqlFabric.git.baseBranch` | `main` | Target branch for *Create Pull Request* |

Provider tokens are set via the **Set Git Provider Token** command (stored securely), not as settings.

---

## Troubleshooting (FAQ)

**Breakpoints don't stop anywhere.** They only fire on statement-start lines — look for the `◦ breakpoint` hint. `BEGIN`/`END`, blank, comment, and continuation lines don't take a breakpoint. Debugging a *deployed* procedure (via ▷) has no file to click in — use **Open Source** first, then F5.

**It stopped on the first line and I hadn't set a breakpoint.** That's *stop on entry*. Use **Step Over (F10)** to go line by line, or set `"stopOnEntry": false`.

**F5 keeps running the same file.** Your `launch.json` has a fixed `program`. Set it to `"${file}"`, or pick **"Debug T-SQL procedure (current file)"** in the Run dropdown.

**`AADSTS50078 … multi-factor authentication has expired`** (or notebooks/procedures won't list). Your Azure sign-in lapsed — run `az login` again in a terminal. If your org uses a work account with no Azure subscription, `az login --allow-no-subscriptions`.

**"ODBC Driver 18 … not found".** Install [ODBC Driver 18 for SQL Server](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server) and reload.

**Can I put breakpoints in a notebook?** No — notebooks are Python; the T-SQL debugger targets **procedures**. Debug the procedures your notebooks call.

**Deploy failed / I don't want to touch production.** Point at a dev warehouse, and add your real one to `tsqlFabric.productionWarehouses` so every write asks first.

---

## Works well alongside the mssql extension

This extension does the **debugging**. For IntelliSense, an ad-hoc query editor, and a results grid for plain queries, add Microsoft's **mssql** (`ms-mssql.mssql`) — they complement each other on the same warehouse.

## How it works

```
VS Code debug UI  ──DAP over stdio──►  python -m tsql_fabric_debugger.dap
                                              │
                                        the debug engine ──►  Fabric Warehouse
                                        (one session, ROLLBACK on disconnect)
```

The extension is a thin front end over the [`tsql-fabric-debugger`](https://github.com/redrex-tech/tsql_fabric_debugger) Python package, which ships the debug engine and a Debug Adapter Protocol (DAP) server. VS Code's debug UI talks to that server; the extension wires them together and fills in the launch configuration. Everything — breakpoints, variable capture, step-into, CATCH emulation — is the Python engine.

## Development

```bash
npm install
npm run build        # bundle to dist/extension.js (npm run watch to iterate)
npm run package      # produce the .vsix
npm test             # unit tests (vitest) for the pure logic in src/util.ts
npm run mutation     # mutation testing (Stryker)
```

Press F5 in this folder (the "Run extension (dev)" config) to launch an Extension Development Host with the extension loaded.

## License

MIT — see [LICENSE](LICENSE).
