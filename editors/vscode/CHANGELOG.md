# Changelog

## 0.3.0 — preview

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
