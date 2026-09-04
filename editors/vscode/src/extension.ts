// T-SQL Fabric Debugger — VS Code extension.
//
// This is thin glue: VS Code's debug UI speaks the Debug Adapter Protocol, and
// the tsql-fabric-debugger Python package ships a DAP server (tsql-fabric-dap /
// `python -m tsql_fabric_debugger.dap`). The extension (1) fills in launch
// defaults and prompts for anything missing, and (2) tells VS Code how to spawn
// the adapter. All the debugging logic lives in the Python engine.

import * as vscode from "vscode";

const TYPE = "tsql-fabric";

export function activate(context: vscode.ExtensionContext): void {
  const filesProvider = new ProjectFilesProvider();

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

    // The gear button in the view title: open Settings filtered to this plugin.
    vscode.commands.registerCommand("tsqlFabric.openSettings", () => {
      void vscode.commands.executeCommand(
        "workbench.action.openSettings",
        "@ext:redrex-tech.tsql-fabric-debugger-vscode",
      );
    }),
    vscode.commands.registerCommand("tsqlFabric.refreshFiles", () => {
      filesProvider.refresh();
    }),
    // Inline debug button on a .sql item: start a debug session for it.
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

  // Keep the tree fresh as .sql/.ipynb files come and go.
  const watcher = vscode.workspace.createFileSystemWatcher("**/*.{sql,ipynb}");
  watcher.onDidCreate(() => filesProvider.refresh());
  watcher.onDidDelete(() => filesProvider.refresh());
  context.subscriptions.push(watcher);
}

export function deactivate(): void {
  /* nothing to clean up: each session owns its own adapter process */
}

// ---------------------------------------------------------------------------
// Sidebar tree: the project's .sql procedures and .ipynb notebooks.
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
      // group node -> its files
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
// Configuration: defaults, settings fallback, and interactive prompts.
// ---------------------------------------------------------------------------
class TsqlFabricConfigurationProvider
  implements vscode.DebugConfigurationProvider
{
  // Called with an empty config when the user hits F5 without a launch.json —
  // synthesize one for the active .sql file.
  async resolveDebugConfiguration(
    _folder: vscode.WorkspaceFolder | undefined,
    config: vscode.DebugConfiguration,
    _token?: vscode.CancellationToken,
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

    // A file OR a deployed procedure name — not both, at least one.
    if (!config.program && !config.procName) {
      config.program = "${file}";
    }

    config.server = config.server || cfg.get<string>("server") || "";
    config.database = config.database || cfg.get<string>("database") || "";

    config.server = await ensureValue(
      config.server,
      "Fabric Warehouse SQL endpoint",
      "xxxx.datawarehouse.fabric.microsoft.com",
    );
    if (!config.server) {
      return undefined; // user cancelled
    }
    config.database = await ensureValue(
      config.database,
      "Warehouse name",
      "my_warehouse",
    );
    if (!config.database) {
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
      // e.g. "tsql-fabric-dap" (installed console script) — support extra args
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

// Best-effort: reuse the interpreter the user already picked in the Python
// extension, so `pip install tsql-fabric-debugger` into that env just works.
function pythonFromPythonExtension(): string | undefined {
  const ext = vscode.extensions.getExtension("ms-python.python");
  const api = ext?.exports as
    | { settings?: { getExecutionDetails?: () => { execCommand?: string[] } } }
    | undefined;
  const cmd = api?.settings?.getExecutionDetails?.().execCommand;
  return cmd && cmd.length > 0 ? cmd[0] : undefined;
}
