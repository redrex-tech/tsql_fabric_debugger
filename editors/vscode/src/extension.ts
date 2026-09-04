// T-SQL Fabric Debugger — VS Code extension.
//
// Thin glue over the tsql-fabric-debugger Python package: VS Code's debug UI
// speaks DAP, the package ships the adapter (`python -m tsql_fabric_debugger.dap`).
// The extension fills in launch config, spawns the adapter, and adds a friendly
// front door — connect to a warehouse by picking it from a list, browse the
// workspace's notebooks, and debug the project's .sql files.

import * as vscode from "vscode";
import {
  FabricAuthError,
  findWorkspaceForServer,
  getNotebookIpynb,
  getToken,
  listNotebooks,
  listParameters,
  listProcedures,
  listWarehouses,
  listWorkspaces,
  notebookUrl,
  type NotebookItem,
  type Procedure,
} from "./fabric";

const TYPE = "tsql-fabric";
const WS_KEY = "tsqlFabric.workspaceId";
const WS_NAME_KEY = "tsqlFabric.workspaceName";

export function activate(context: vscode.ExtensionContext): void {
  const filesProvider = new ProjectFilesProvider();
  const fabricProvider = new FabricWorkspaceProvider(context);
  const proceduresProvider = new ProceduresProvider();
  const status = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Left,
    100,
  );
  status.command = "tsqlFabric.switchWarehouse";
  context.subscriptions.push(status);
  refreshStatus(status);

  context.subscriptions.push(
    vscode.debug.registerDebugConfigurationProvider(
      TYPE,
      new TsqlFabricConfigurationProvider(),
    ),
    vscode.debug.registerDebugAdapterDescriptorFactory(
      TYPE,
      new TsqlFabricAdapterFactory(),
    ),
    vscode.window.registerTreeDataProvider("tsqlFabricFiles", filesProvider),
    vscode.window.registerTreeDataProvider(
      "tsqlFabricWorkspace",
      fabricProvider,
    ),
    vscode.window.registerTreeDataProvider(
      "tsqlFabricProcedures",
      proceduresProvider,
    ),

    vscode.commands.registerCommand("tsqlFabric.refreshProcedures", () => {
      proceduresProvider.refresh();
    }),
    vscode.commands.registerCommand(
      "tsqlFabric.debugProcedure",
      async (item?: ProcNode) => {
        if (!item?.proc) {
          return;
        }
        const name = `${item.proc.schema}.${item.proc.name}`;
        const params = await promptForParams(name);
        if (params === undefined) {
          return; // cancelled
        }
        await vscode.debug.startDebugging(undefined, {
          type: TYPE,
          request: "launch",
          name: `Debug ${name}`,
          procName: name,
          params,
          stopOnEntry: true,
        });
      },
    ),
    vscode.commands.registerCommand("tsqlFabric.openSettings", () => {
      void vscode.commands.executeCommand(
        "workbench.action.openSettings",
        "@ext:redrex-tech.tsql-fabric-debugger-vscode",
      );
    }),
    vscode.commands.registerCommand("tsqlFabric.refreshFiles", () => {
      filesProvider.refresh();
    }),
    vscode.commands.registerCommand("tsqlFabric.refreshWorkspace", () => {
      fabricProvider.refresh();
    }),
    vscode.commands.registerCommand("tsqlFabric.connect", () =>
      connectToWarehouse(context, status, fabricProvider, proceduresProvider),
    ),
    vscode.commands.registerCommand("tsqlFabric.switchWarehouse", () =>
      switchWarehouse(context, status, fabricProvider, proceduresProvider),
    ),
    vscode.commands.registerCommand("tsqlFabric.checkSetup", () => checkSetup()),
    // Click a notebook -> open it inside VS Code (download its .ipynb source).
    vscode.commands.registerCommand(
      "tsqlFabric.openNotebook",
      (item?: NotebookNode) => openNotebookInEditor(context, item?.notebook),
    ),
    // The inline button -> open in the Fabric web UI (to actually run it).
    vscode.commands.registerCommand(
      "tsqlFabric.openInFabric",
      (item?: NotebookNode) => {
        if (item?.notebook) {
          void vscode.env.openExternal(
            vscode.Uri.parse(
              notebookUrl(item.notebook.workspaceId, item.notebook.id),
            ),
          );
        }
      },
    ),
    vscode.commands.registerCommand(
      "tsqlFabric.debugFile",
      async (item?: FileNode) => {
        const uri = item?.resourceUri;
        if (!uri) {
          return;
        }
        const folder = vscode.workspace.getWorkspaceFolder(uri);
        await vscode.debug.startDebugging(folder, {
          type: TYPE,
          request: "launch",
          name: `Debug ${uriBasename(uri)}`,
          program: uri.fsPath,
          params: {},
          stopOnEntry: true,
        });
      },
    ),
  );

  // Inline values: show @variable values next to the code while stopped.
  context.subscriptions.push(
    vscode.languages.registerInlineValuesProvider("sql", {
      provideInlineValues(document, viewport) {
        const out: vscode.InlineValue[] = [];
        for (let line = viewport.start.line; line <= viewport.end.line; line++) {
          const text = document.lineAt(line).text;
          for (const m of text.matchAll(/@{1,2}\w+/g)) {
            const start = m.index ?? 0;
            const range = new vscode.Range(
              line,
              start,
              line,
              start + m[0].length,
            );
            out.push(new vscode.InlineValueVariableLookup(range, m[0], false));
          }
        }
        return out;
      },
    }),
  );

  const watcher = vscode.workspace.createFileSystemWatcher("**/*.{sql,ipynb}");
  watcher.onDidCreate(() => filesProvider.refresh());
  watcher.onDidDelete(() => filesProvider.refresh());
  context.subscriptions.push(watcher);

  vscode.workspace.onDidChangeConfiguration(
    (e) => {
      if (e.affectsConfiguration("tsqlFabric")) {
        refreshStatus(status);
        proceduresProvider.refresh();
        fabricProvider.refresh();
      }
    },
    null,
    context.subscriptions,
  );
}

export function deactivate(): void {
  /* nothing to clean up: each session owns its own adapter process */
}

function refreshStatus(status: vscode.StatusBarItem): void {
  const db = vscode.workspace
    .getConfiguration("tsqlFabric")
    .get<string>("database");
  if (db) {
    status.text = `$(database) Fabric: ${db}`;
    status.tooltip = "T-SQL Fabric — click to switch warehouse";
  } else {
    status.text = "$(plug) Fabric: connect";
    status.tooltip = "T-SQL Fabric — click to connect to a warehouse";
  }
  status.show();
}

// ---------------------------------------------------------------------------
// Onboarding: connect to a warehouse by picking it, and check the setup.
// ---------------------------------------------------------------------------
async function connectToWarehouse(
  context: vscode.ExtensionContext,
  status: vscode.StatusBarItem,
  fabricProvider: FabricWorkspaceProvider,
  proceduresProvider: ProceduresProvider,
): Promise<void> {
  try {
    await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: "T-SQL Fabric" },
      async (progress) => {
        progress.report({ message: "Signing in via Azure CLI…" });
        const token = await getToken();

        progress.report({ message: "Loading workspaces…" });
        const workspaces = await listWorkspaces(token);
        if (workspaces.length === 0) {
          throw new Error("No Fabric workspaces are visible to your account.");
        }
        const ws = await vscode.window.showQuickPick(
          workspaces.map((w) => ({ label: w.displayName, id: w.id })),
          { title: "Fabric workspace", placeHolder: "Pick a workspace" },
        );
        if (!ws) {
          return;
        }

        progress.report({ message: "Loading warehouses…" });
        const warehouses = await listWarehouses(token, ws.id);
        if (warehouses.length === 0) {
          throw new Error(`No warehouses in "${ws.label}".`);
        }
        const wh = await vscode.window.showQuickPick(
          warehouses.map((w) => ({
            label: w.displayName,
            detail: w.connectionString,
            connectionString: w.connectionString,
          })),
          {
            title: "Warehouse",
            placeHolder: "Pick a warehouse to debug against",
          },
        );
        if (!wh) {
          return;
        }

        const cfg = vscode.workspace.getConfiguration("tsqlFabric");
        // Global by default: it shows in the (default) User tab of Settings and
        // applies in every folder — most people debug one warehouse.
        const target = vscode.ConfigurationTarget.Global;
        await cfg.update("server", wh.connectionString, target);
        await cfg.update("database", wh.label, target);
        await context.workspaceState.update(WS_KEY, ws.id);
        await context.workspaceState.update(WS_NAME_KEY, ws.label);
        // remember it in the saved-connections list for quick switching
        saveConnection(context, {
          label: `${wh.label} — ${ws.label}`,
          server: wh.connectionString,
          database: wh.label,
          workspaceId: ws.id,
          workspaceName: ws.label,
        });
      },
    );
    refreshStatus(status);
    fabricProvider.refresh();
    proceduresProvider.refresh();
    const db = vscode.workspace
      .getConfiguration("tsqlFabric")
      .get<string>("database");
    if (db) {
      void vscode.window.showInformationMessage(
        `T-SQL Fabric: connected to ${db}.`,
      );
    }
  } catch (err) {
    reportError(err);
  }
}

// -- saved connections ------------------------------------------------------
interface SavedConnection {
  label: string;
  server: string;
  database: string;
  workspaceId?: string;
  workspaceName?: string;
}
const CONNECTIONS_KEY = "tsqlFabric.savedConnections";

function saveConnection(
  context: vscode.ExtensionContext,
  conn: SavedConnection,
): void {
  const all = context.globalState.get<SavedConnection[]>(CONNECTIONS_KEY, []);
  const rest = all.filter(
    (c) => !(c.server === conn.server && c.database === conn.database),
  );
  void context.globalState.update(CONNECTIONS_KEY, [conn, ...rest].slice(0, 20));
}

async function switchWarehouse(
  context: vscode.ExtensionContext,
  status: vscode.StatusBarItem,
  fabricProvider: FabricWorkspaceProvider,
  proceduresProvider: ProceduresProvider,
): Promise<void> {
  const saved = context.globalState.get<SavedConnection[]>(CONNECTIONS_KEY, []);
  const items: (vscode.QuickPickItem & { conn?: SavedConnection })[] = saved.map(
    (c) => ({ label: c.label, detail: `${c.database} — ${c.server}`, conn: c }),
  );
  items.push({
    label: "$(plug) Connect to a new warehouse…",
    conn: undefined,
  });
  const pick = await vscode.window.showQuickPick(items, {
    title: "Switch warehouse",
    placeHolder: "Pick a saved connection, or connect to a new one",
  });
  if (!pick) {
    return;
  }
  if (!pick.conn) {
    await connectToWarehouse(context, status, fabricProvider, proceduresProvider);
    return;
  }
  const cfg = vscode.workspace.getConfiguration("tsqlFabric");
  await cfg.update("server", pick.conn.server, vscode.ConfigurationTarget.Global);
  await cfg.update(
    "database",
    pick.conn.database,
    vscode.ConfigurationTarget.Global,
  );
  await context.workspaceState.update(WS_KEY, pick.conn.workspaceId);
  await context.workspaceState.update(WS_NAME_KEY, pick.conn.workspaceName);
  refreshStatus(status);
  fabricProvider.refresh();
  proceduresProvider.refresh();
}

async function checkSetup(): Promise<void> {
  const out = vscode.window.createOutputChannel("T-SQL Fabric");
  out.show(true);
  out.appendLine("Checking setup…\n");

  try {
    await getToken();
    out.appendLine("✓ Azure sign-in (az) — OK");
  } catch (err) {
    out.appendLine(`✗ Azure sign-in — ${(err as Error).message}`);
  }

  const cfg = vscode.workspace.getConfiguration("tsqlFabric");
  const python =
    cfg.get<string>("pythonPath") || pythonFromPythonExtension() || "python3";
  const { execFile } = await import("node:child_process");
  await new Promise<void>((resolve) => {
    execFile(
      python,
      ["-c", "import tsql_fabric_debugger as t; print(t.__version__)"],
      { timeout: 20000 },
      (err, stdout, stderr) => {
        if (err) {
          out.appendLine(
            `✗ Python package — '${python}' cannot import tsql_fabric_debugger.\n` +
              `  Fix: pip install tsql-fabric-debugger  (into that interpreter)\n` +
              `  ${String(stderr || err).trim()}`,
          );
        } else {
          out.appendLine(
            `✓ Python package — tsql-fabric-debugger ${stdout.trim()} (${python})`,
          );
        }
        resolve();
      },
    );
  });

  const server = cfg.get<string>("server");
  out.appendLine(
    server
      ? `✓ Warehouse — ${cfg.get<string>("database")} (${server})`
      : "• Warehouse — not connected yet (run “T-SQL Fabric: Connect to Warehouse”)",
  );
  out.appendLine("\nDone.");
}

async function openNotebookInEditor(
  context: vscode.ExtensionContext,
  notebook?: NotebookItem,
): Promise<void> {
  if (!notebook) {
    return;
  }
  try {
    const uri = await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: `T-SQL Fabric: opening "${notebook.displayName}"…`,
      },
      async () => {
        const token = await getToken();
        const ipynb = await getNotebookIpynb(
          token,
          notebook.workspaceId,
          notebook.id,
        );
        const dir = vscode.Uri.joinPath(context.globalStorageUri, "notebooks");
        await vscode.workspace.fs.createDirectory(dir);
        const safe = notebook.displayName.replace(/[^\w.\- ]+/g, "_");
        const file = vscode.Uri.joinPath(dir, `${safe}.ipynb`);
        await vscode.workspace.fs.writeFile(file, Buffer.from(ipynb, "utf8"));
        return file;
      },
    );
    await vscode.commands.executeCommand("vscode.open", uri);
  } catch (err) {
    reportError(err);
  }
}

function reportError(err: unknown): void {
  const msg = err instanceof Error ? err.message : String(err);
  if (err instanceof FabricAuthError) {
    void vscode.window
      .showErrorMessage(`T-SQL Fabric: ${msg}`, "Open Terminal")
      .then((pick) => {
        if (pick === "Open Terminal") {
          const term = vscode.window.createTerminal("az login");
          term.show();
          term.sendText("az login", false);
        }
      });
  } else {
    void vscode.window.showErrorMessage(`T-SQL Fabric: ${msg}`);
  }
}

// ---------------------------------------------------------------------------
// Configuration: defaults, settings fallback, interactive prompts.
// ---------------------------------------------------------------------------
class TsqlFabricConfigurationProvider
  implements vscode.DebugConfigurationProvider
{
  async resolveDebugConfiguration(
    _folder: vscode.WorkspaceFolder | undefined,
    config: vscode.DebugConfiguration,
  ): Promise<vscode.DebugConfiguration | undefined | null> {
    const cfg = vscode.workspace.getConfiguration("tsqlFabric");

    if (!config.type && !config.request && !config.name) {
      const editor = vscode.window.activeTextEditor;
      if (!editor || editor.document.languageId !== "sql") {
        void vscode.window.showErrorMessage(
          "T-SQL Fabric: open a .sql file (with a CREATE PROCEDURE) to debug.",
        );
        return undefined;
      }
      config.type = TYPE;
      config.request = "launch";
      config.name = "Debug T-SQL procedure";
      config.program = "${file}";
      config.stopOnEntry = true;
    }

    if (!config.program && !config.procName) {
      config.program = "${file}";
    }

    config.server = config.server || cfg.get<string>("server") || "";
    config.database = config.database || cfg.get<string>("database") || "";

    // Not connected yet? Offer the friendly picker instead of a raw input box.
    if (!config.server || !config.database) {
      const pick = await vscode.window.showWarningMessage(
        "T-SQL Fabric: no warehouse is configured.",
        "Connect to Warehouse",
        "Enter manually",
      );
      if (pick === "Connect to Warehouse") {
        await vscode.commands.executeCommand("tsqlFabric.connect");
        config.server = cfg.get<string>("server") || "";
        config.database = cfg.get<string>("database") || "";
      } else if (pick === "Enter manually") {
        config.server = await ensureValue(
          config.server,
          "Fabric Warehouse SQL endpoint",
          "xxxx.datawarehouse.fabric.microsoft.com",
        );
        config.database = await ensureValue(
          config.database,
          "Warehouse name",
          "my_warehouse",
        );
      }
    }
    if (!config.server || !config.database) {
      return undefined;
    }

    if (config.params === undefined) {
      config.params = {};
    }
    return config;
  }
}

async function ensureValue(
  current: string,
  label: string,
  placeholder: string,
): Promise<string> {
  if (current) {
    return current;
  }
  const value = await vscode.window.showInputBox({
    prompt: `T-SQL Fabric: ${label}`,
    placeHolder: placeholder,
    ignoreFocusOut: true,
  });
  return value ?? "";
}

// ---------------------------------------------------------------------------
// Adapter: how to start the DAP server for a session.
// ---------------------------------------------------------------------------
class TsqlFabricAdapterFactory
  implements vscode.DebugAdapterDescriptorFactory
{
  createDebugAdapterDescriptor(
    _session: vscode.DebugSession,
    _executable: vscode.DebugAdapterExecutable | undefined,
  ): vscode.ProviderResult<vscode.DebugAdapterDescriptor> {
    const cfg = vscode.workspace.getConfiguration("tsqlFabric");
    const explicit = cfg.get<string>("adapterCommand");
    if (explicit) {
      const [command, ...args] = explicit.split(/\s+/);
      return new vscode.DebugAdapterExecutable(command, args);
    }
    const python =
      cfg.get<string>("pythonPath") || pythonFromPythonExtension() || "python3";
    return new vscode.DebugAdapterExecutable(python, [
      "-m",
      "tsql_fabric_debugger.dap",
    ]);
  }
}

function pythonFromPythonExtension(): string | undefined {
  const ext = vscode.extensions.getExtension("ms-python.python");
  const api = ext?.exports as
    | { settings?: { getExecutionDetails?: () => { execCommand?: string[] } } }
    | undefined;
  const cmd = api?.settings?.getExecutionDetails?.().execCommand;
  return cmd && cmd.length > 0 ? cmd[0] : undefined;
}

function resolvePython(): string {
  return (
    vscode.workspace.getConfiguration("tsqlFabric").get<string>("pythonPath") ||
    pythonFromPythonExtension() ||
    "python3"
  );
}

// Ask for the procedure's INPUT parameter values before debugging. Returns a
// params object, {} if the procedure takes none, or undefined if cancelled.
async function promptForParams(
  procName: string,
): Promise<Record<string, unknown> | undefined> {
  const cfg = vscode.workspace.getConfiguration("tsqlFabric");
  const server = cfg.get<string>("server");
  const database = cfg.get<string>("database");
  if (!server || !database) {
    return {};
  }
  let params: import("./fabric").ProcParameter[];
  try {
    params = await listParameters(resolvePython(), server, database, procName);
  } catch {
    return {}; // introspection failed — debug with no params (they stay NULL)
  }
  // Ask only for pure inputs; OUTPUT params (INOUT in INFORMATION_SCHEMA)
  // stay NULL and are filled by the procedure.
  const inputs = params.filter((p) => p.mode === "IN");
  const values: Record<string, unknown> = {};
  for (const p of inputs) {
    const raw = await vscode.window.showInputBox({
      title: `Debug ${procName}`,
      prompt: `${p.name} (${p.type}) — leave empty for NULL`,
      ignoreFocusOut: true,
    });
    if (raw === undefined) {
      return undefined; // cancelled
    }
    if (raw !== "") {
      values[p.name] = coerceParam(raw);
    }
  }
  return values;
}

// Match the launch.json params semantics: strict int/float, quoted = string,
// NULL/empty = omit (stays NULL); otherwise a string.
function coerceParam(raw: string): unknown {
  const s = raw.trim();
  if (s.toUpperCase() === "NULL") {
    return null;
  }
  if (s.length >= 2 && s[0] === s[s.length - 1] && (s[0] === "'" || s[0] === '"')) {
    return s.slice(1, -1);
  }
  if (/^[+-]?\d+$/.test(s)) {
    return parseInt(s, 10);
  }
  if (/^[+-]?(\d+\.\d*|\.\d+)$/.test(s)) {
    return parseFloat(s);
  }
  return raw;
}

// ---------------------------------------------------------------------------
// Sidebar: the connected warehouse's deployed procedures, grouped by schema.
// Click one to start a debug session (via procName).
// ---------------------------------------------------------------------------
class ProcNode extends vscode.TreeItem {
  constructor(
    label: string,
    state: vscode.TreeItemCollapsibleState,
    public readonly proc?: Procedure,
  ) {
    super(label, state);
    if (proc) {
      this.iconPath = new vscode.ThemeIcon("symbol-method");
      this.contextValue = "procedure";
      this.tooltip = `${proc.schema}.${proc.name} — click to debug`;
      this.command = {
        command: "tsqlFabric.debugProcedure",
        title: "Debug",
        arguments: [this],
      };
    } else {
      this.iconPath = new vscode.ThemeIcon("symbol-namespace");
      this.contextValue = "schema";
    }
  }
}

class ProceduresProvider implements vscode.TreeDataProvider<ProcNode> {
  private readonly _onDidChange = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this._onDidChange.event;
  private _cache: Procedure[] | undefined;

  refresh(): void {
    this._cache = undefined;
    this._onDidChange.fire();
  }

  getTreeItem(e: ProcNode): vscode.TreeItem {
    return e;
  }

  async getChildren(element?: ProcNode): Promise<ProcNode[]> {
    const cfg = vscode.workspace.getConfiguration("tsqlFabric");
    const server = cfg.get<string>("server");
    const database = cfg.get<string>("database");
    if (!server || !database) {
      return [
        new ProcNode(
          "Not connected — run “Connect to Warehouse”.",
          vscode.TreeItemCollapsibleState.None,
        ),
      ];
    }
    const procs = await this.load(server, database);
    if (procs === null) {
      return [
        new ProcNode(
          "Could not list procedures (see notification).",
          vscode.TreeItemCollapsibleState.None,
        ),
      ];
    }
    if (element) {
      return procs
        .filter((p) => p.schema === element.label)
        .map(
          (p) =>
            new ProcNode(p.name, vscode.TreeItemCollapsibleState.None, p),
        );
    }
    const schemas = [...new Set(procs.map((p) => p.schema))].sort();
    return schemas.map(
      (s) => new ProcNode(s, vscode.TreeItemCollapsibleState.Collapsed),
    );
  }

  private async load(
    server: string,
    database: string,
  ): Promise<Procedure[] | null> {
    if (this._cache) {
      return this._cache;
    }
    try {
      this._cache = await listProcedures(resolvePython(), server, database);
      return this._cache;
    } catch (err) {
      void vscode.window.showErrorMessage(
        `T-SQL Fabric: could not list procedures — ${(err as Error).message}`,
      );
      return null;
    }
  }
}

// ---------------------------------------------------------------------------
// Sidebar: local project files (.sql / .ipynb).
// ---------------------------------------------------------------------------
function uriBasename(uri: vscode.Uri): string {
  const parts = uri.path.split("/");
  return parts[parts.length - 1];
}

class FileNode extends vscode.TreeItem {
  constructor(
    label: string,
    collapsibleState: vscode.TreeItemCollapsibleState,
    public readonly resourceUri?: vscode.Uri,
    kind?: "sql" | "notebook",
  ) {
    super(label, collapsibleState);
    if (resourceUri) {
      this.resourceUri = resourceUri;
      this.tooltip = resourceUri.fsPath;
      this.command = {
        command: "vscode.open",
        title: "Open",
        arguments: [resourceUri],
      };
      this.iconPath = new vscode.ThemeIcon(
        kind === "sql" ? "database" : "notebook",
      );
      this.contextValue = kind === "sql" ? "sqlFile" : "notebookFile";
    } else {
      this.iconPath = new vscode.ThemeIcon("folder");
      this.contextValue = "group";
    }
  }
}

class ProjectFilesProvider implements vscode.TreeDataProvider<FileNode> {
  private readonly _onDidChange = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this._onDidChange.event;

  refresh(): void {
    this._onDidChange.fire();
  }
  getTreeItem(element: FileNode): vscode.TreeItem {
    return element;
  }
  async getChildren(element?: FileNode): Promise<FileNode[]> {
    if (element) {
      const kind = element.label === "Procedures (.sql)" ? "sql" : "notebook";
      return this.files(kind);
    }
    const groups: FileNode[] = [];
    if ((await this.files("sql")).length > 0) {
      groups.push(
        new FileNode(
          "Procedures (.sql)",
          vscode.TreeItemCollapsibleState.Expanded,
        ),
      );
    }
    if ((await this.files("notebook")).length > 0) {
      groups.push(
        new FileNode(
          "Notebooks (.ipynb)",
          vscode.TreeItemCollapsibleState.Expanded,
        ),
      );
    }
    return groups;
  }
  private async files(kind: "sql" | "notebook"): Promise<FileNode[]> {
    const glob = kind === "sql" ? "**/*.sql" : "**/*.ipynb";
    const uris = await vscode.workspace.findFiles(
      glob,
      "**/{node_modules,.venv,.git,dist,__pycache__}/**",
      500,
    );
    uris.sort((a, b) => a.path.localeCompare(b.path));
    return uris.map(
      (uri) =>
        new FileNode(
          workspaceRelative(uri),
          vscode.TreeItemCollapsibleState.None,
          uri,
          kind,
        ),
    );
  }
}

function workspaceRelative(uri: vscode.Uri): string {
  const folder = vscode.workspace.getWorkspaceFolder(uri);
  if (!folder) {
    return uriBasename(uri);
  }
  const rel = uri.path.slice(folder.uri.path.length).replace(/^\//, "");
  return rel || uriBasename(uri);
}

// ---------------------------------------------------------------------------
// Sidebar: the connected Fabric workspace's notebooks (respects permissions).
// ---------------------------------------------------------------------------
class NotebookNode extends vscode.TreeItem {
  constructor(
    label: string,
    public readonly notebook?: NotebookItem,
  ) {
    super(label, vscode.TreeItemCollapsibleState.None);
    if (notebook) {
      this.iconPath = new vscode.ThemeIcon("notebook");
      this.contextValue = "fabricNotebook";
      this.tooltip = "Open in VS Code (the ↗ button opens it in Fabric)";
      this.command = {
        command: "tsqlFabric.openNotebook",
        title: "Open in VS Code",
        arguments: [this],
      };
    }
  }
}

class FabricWorkspaceProvider
  implements vscode.TreeDataProvider<NotebookNode>
{
  private readonly _onDidChange = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this._onDidChange.event;

  constructor(private readonly context: vscode.ExtensionContext) {}

  refresh(): void {
    this._onDidChange.fire();
  }
  getTreeItem(e: NotebookNode): vscode.TreeItem {
    return e;
  }
  async getChildren(): Promise<NotebookNode[]> {
    let wsId = this.context.workspaceState.get<string>(WS_KEY);
    const wsName = this.context.workspaceState.get<string>(WS_NAME_KEY);
    const server = vscode.workspace
      .getConfiguration("tsqlFabric")
      .get<string>("server");
    if (!wsId && !server) {
      return [new NotebookNode("Not connected — run “Connect to Warehouse”.")];
    }
    try {
      const token = await getToken();
      // Configured via Settings (no explicit Connect)? Discover the workspace
      // from the SQL endpoint so notebooks still show.
      if (!wsId && server) {
        wsId = await findWorkspaceForServer(token, server);
        if (!wsId) {
          return [
            new NotebookNode(
              "Connected by settings — workspace not found for this endpoint.",
            ),
          ];
        }
        await this.context.workspaceState.update(WS_KEY, wsId);
      }
      if (!wsId) {
        return [new NotebookNode("Not connected — run “Connect to Warehouse”.")];
      }
      const notebooks = await listNotebooks(token, wsId);
      if (notebooks.length === 0) {
        return [
          new NotebookNode(`No notebooks in ${wsName ?? "this workspace"}.`),
        ];
      }
      return notebooks.map((n) => new NotebookNode(n.displayName, n));
    } catch (err) {
      return [new NotebookNode(`Error: ${(err as Error).message}`)];
    }
  }
}
