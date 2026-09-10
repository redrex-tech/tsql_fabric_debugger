# Changelog

## 0.4.0 — preview

Milestone: a complete Fabric debug + git + deploy environment (rolling out in
phases).

- **Deploy to Fabric** (phase 1): a rocket button on a procedure (Warehouse
  Procedures) or a `.sql` (Project Files) runs its `CREATE OR ALTER` against the
  warehouse — a committed write — after a **modal confirmation** and the
  production guard (`productionWarehouses`). Generating artifacts
  (*Export for Fabric deploy*) stays as the non-executing alternative.
- **Create Pull Request / Merge Request** (phase 2): a button in Project Files
  opens a PR/MR for the current branch → `tsqlFabric.git.baseBranch` via the
  provider's API — **GitHub** (built-in GitHub sign-in, no PAT), **GitLab** and
  **Bitbucket** (PAT stored in SecretStorage via *Set Git Provider Token*);
  Azure DevOps / unknown open the browser. Bridges GitLab/Bitbucket, which
  Fabric's native Git integration does not support.
- **Sync with Fabric** (phase 3): one command, pick a direction —
  **← Pull** downloads all procedures + notebooks into the folder, or
  **→ Deploy** publishes the folder's procedures (execute) and notebooks
  (updateDefinition) to the warehouse, after a confirmation + production guard.


## 0.3.8 — preview

- **Discoverability**: richer Marketplace keywords (transact-sql, mssql,
  microsoft fabric, fabric warehouse, synapse, breakpoints, stored procedure,
  notebook, etl, …) and a clearer description, so the extension shows up for
  more than just "t-sql".


## 0.3.7 — preview

- **Notebook round-trip fixed to Fabric's native format**: Fabric's
  `updateDefinition` only accepts the native `notebook-content.py` source, not
  `.ipynb` (verified against a live warehouse). So **Save Notebook to Project**
  now downloads the notebook as a git-friendly `.py` (into `fabric/notebooks/`,
  with a link comment on the first line), and **Update Notebook in Fabric**
  pushes that `.py` back (native `updateDefinition`) after a modal confirmation.
  The `.ipynb` "open notebook" action stays for read-only viewing.
  Project Files now has a **Fabric notebooks (.py)** group with the update
  button.


## 0.3.6 — preview

- **Notebook round-trip**:
  - **Save Notebook to Project** (inline button on a notebook in the Fabric
    Workspace view) downloads its `.ipynb` into `fabric/notebooks/`, stamping the
    Fabric identity (workspace/item id) into the notebook metadata so it can be
    updated back later — survives renames/moves.
  - **Update Notebook in Fabric** (inline button on a local `.ipynb` in Project
    Files) overwrites the linked cloud notebook with the local copy
    (`updateDefinition`), after a **modal confirmation**. Only the `.ipynb` part
    is replaced (other definition parts are preserved); the pushed copy is
    cleaned of the mapping metadata.


## 0.3.5 — preview

- **Settings organized into sections**: *Connection*, *Debugging*, and
  *Local Files & Git* — easier to find what to configure.
- **File organization** (`tsqlFabric.fileLayout`, default `schema-type`):
  pulled sources go to `procedures/<schema>/<name>.sql` and generated artifacts
  to `deploy/<schema>/<name>.sql` + `.ipynb`. Other layouts: `schema`, `type`,
  `flat`. Export now derives the schema/name from the `CREATE PROCEDURE`.
- **Commit & Push (Git)**: a dedicated button in the **Project Files** view
  title stages the Fabric folder, prompts for a message and pushes to the
  workspace's Git repository (your existing credentials, via the built-in Git
  extension). Optional `tsqlFabric.gitRemote` / `tsqlFabric.gitCommitMessage`.


## 0.3.4 — preview

- **Debug a deployed procedure with breakpoints**: **Open Source (edit & debug
  locally)** on a procedure in the Warehouse Procedures view fetches its source
  into a local `.sql` (in `tsqlFabric.localFolder`, default `fabric/`) and opens
  it — set breakpoints and press F5 (`program` mode). Read-only fetch; nothing
  is written to the warehouse.
- **Export for Fabric deploy**: an inline button on a `.sql` in the **Project
  Files** view (or right-click in the explorer) generates deployable artifacts —
  `<name>.sql` normalized to `CREATE OR ALTER PROCEDURE` and a
  `<name>.Deploy.ipynb` notebook whose cell (re)creates the procedure in the
  warehouse. Writes files only; **the extension never runs anything against the
  warehouse** — you upload/run them in Fabric.
- **Breakpoint-able line hints**: marks where a breakpoint can actually pause
  (statement starts, **including inside IF/ELSE/WHILE/CATCH**) — so you no
  longer place one on a `BEGIN`/`END`, blank/comment or continuation line where
  it never fires. Two styles via `tsqlFabric.breakpointLineHint`: **`label`**
  (default — a dim `◦ breakpoint` at the end of the line) or **`bar`** (a thin
  left-edge bar); `off` disables. Neither sits on the gutter click target, so
  setting breakpoints still works. Computed offline as you type.
- **Settings gear on all three views** (Warehouse Procedures / Fabric Workspace
  / Project Files), so it is reachable whichever section is open.


## 0.3.3 — preview

- **New branding**: the marketplace logo and the Activity Bar icon are now the
  RedRex T-Rex (red square logo; a monochrome `currentColor` silhouette for the
  Activity Bar that follows the theme). No functional changes.

## 0.3.2 — preview

- **All of a step's result sets are shown**: when one statement returns several
  result sets, they are stacked in the Result Set panel instead of the panel
  showing only the last one.
- **Export to CSV**: new command **T-SQL Fabric: Export Result Set to CSV**
  saves the current step's result set to a file (pick which one when a step
  produced several). Runs in the extension host — the webview stays
  script-free (RFC-4180 quoting, `NULL` → empty field).
- Tolerates lib/extension version skew: the grid accepts both the new
  `{line, sets}` and the legacy `{line, columns, rows, truncated}` event shape.

## 0.3.1 — preview

Hardening round (concurrency + privacy review):

- **Correct procedure list on warehouse switch**: the Warehouse Procedures
  cache is now keyed to the connected warehouse and guarded by a generation
  counter, so a slow list from a previous warehouse can no longer show its
  procedures under a newly selected one.
- **No duplicate `az` cold starts**: `getDatabaseToken` de-duplicates
  concurrent callers onto a single in-flight promise (the activation warm-up,
  the procedure tree and a starting debug session no longer each spawn `az`).
- **Cancellable notebook open**: opening a Fabric notebook is now cancellable —
  closing the progress notification aborts the Fabric polling/fetch instead of
  finishing in the background.
- **Result-set grid CSP**: the webview now declares an explicit
  `Content-Security-Policy` (`default-src 'none'`) as defense in depth (scripts
  were already disabled).
- **Docs hygiene**: example procedure/parameter names generalized
  (`dbo.load_sales` / `@year`).

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
