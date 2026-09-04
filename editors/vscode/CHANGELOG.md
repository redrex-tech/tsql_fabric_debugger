# Changelog

## 0.3.0 — preview

- **F5 follows the active file**: the generated launch.json config and a new
  **"Debug T-SQL procedure (current file)"** entry in the Run dropdown both use
  `${file}`, so F5 debugs the `.sql` you have open instead of a path pinned the
  first time. (A launch.json with an explicit `program` is still respected.)
- **Result-set grid**: rows a step returns open in a **Result Set** webview
  beside the editor (headers, `NULL` marked, theme-aware, `(truncated)` note)
  and are also printed to the Debug Console. Driven by the adapter's
  `tsqlFabricResultSet` custom event.
- **Production guard**: `tsqlFabric.productionWarehouses` flags warehouses (by
  name or endpoint substring) as production; debugging one pops a modal
  confirmation first, and the status bar shows an amber **Fabric (PROD)** badge.
- **Faster startup**: a database token is acquired once and passed to the
  Python processes, skipping the Azure CLI cold start on every connection
  (first connect ~4.7s → ~0.6s); warmed in the background on activation.
- **Safety**: default lockTimeout (30s) and optional stepTimeout on debug
  sessions; **Kill Orphan Debug Sessions** command; Check Setup now verifies
  the ODBC Driver 18.

- **Warehouse Procedures** view: browse deployed procedures by schema and
  debug one by clicking — the extension prompts for the input parameters.
- **Switch Warehouse**: saved connections, switch from the status bar.
- Debug UX: **Restart**, **Stop**, **logpoints** (diamond), exception details,
  **inline @variable values**, and Debug Console **autocomplete**.
- Recommends the **mssql** extension for IntelliSense/results grid.

- Clicking a notebook in **Fabric Workspace** now opens it **inside VS Code**
  (downloads its .ipynb); the ↗ button opens it in the Fabric web UI to run.

- **Brand logo**: the RedRex T-Rex on a red disc — the extension icon and the
  Activity Bar icon.

Friendly onboarding — no cryptic endpoints to paste:

- **Connect to Warehouse**: sign in with the Azure CLI and pick your
  **workspace → warehouse** from a list; the SQL endpoint and database name are
  filled in for you. (Status bar shows the connected warehouse.)
- **Fabric Workspace** view lists the **notebooks your account can see** in the
  connected workspace — click to open in the Fabric web UI.
- **Check Setup** command verifies Azure sign-in and that the Python package is
  importable, with actionable fixes.
- First-run welcome screens; hitting F5 without a warehouse offers the picker
  (or manual entry). The **Project Files** view keeps listing local `.sql`/
  `.ipynb`.


## 0.2.0 — preview

- **Activity Bar view "T-SQL Fabric"** listing the project's `.sql` procedures
  and `.ipynb` notebooks (click to open; a `.sql` has an inline debug button).
  The list refreshes as files are added or removed.
- **Settings button** (gear) in the view title opens the plugin's settings.


## 0.1.0 — preview

First scaffold. Debug a T-SQL Fabric procedure from the editor through the
`tsql-fabric-debugger` DAP adapter:

- `tsql-fabric` debug type: launch a `.sql` file (`program`) or a deployed
  procedure (`procName`); breakpoints on `.sql` (condition + hit count), step
  in / over / out, Variables pane, Debug Console evaluation, and a
  CATCH-handled-error exception filter.
- Settings for the default `server`/`database`, the Python interpreter, and an
  adapter-command override; interactive prompts fill in a missing endpoint.
- Adapter launched as `<python> -m tsql_fabric_debugger.dap`.

Preview limits inherited from the adapter: synchronous (no mid-run *pause*),
breakpoints apply to the launched source only, never commits (ROLLBACK on
disconnect).
