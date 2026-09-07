// Microsoft Fabric REST discovery, so the user picks a warehouse from a list
// instead of pasting a cryptic SQL endpoint. Auth reuses the Azure CLI login
// the Python library already requires (AzureCliCredential) — one `az login`.

import { execFile } from "node:child_process";
import { parseIntrospectResult } from "./util";

const FABRIC_API = "https://api.fabric.microsoft.com/v1";
const FABRIC_RESOURCE = "https://api.fabric.microsoft.com";
const DATABASE_RESOURCE = "https://database.windows.net/";
export const ACCESS_TOKEN_ENV = "FABRIC_TSQL_ACCESS_TOKEN";

// Cache a warehouse (database) access token so each short-lived Python process
// we spawn reuses it instead of paying the Azure CLI cold start again — the
// biggest part of "connect" latency. The token is passed to Python via env.
let dbTokenCache: { token: string; expiresAt: number } | undefined;
// De-duplicate concurrent acquisitions: several callers (activation warm-up,
// the procedure tree, a starting debug session) can ask at once before the
// cache is populated. Without this they would each spawn a separate `az`
// cold start (thundering herd); instead they share one in-flight promise.
let dbTokenInFlight: Promise<string> | undefined;

export function getDatabaseToken(azPath = "az"): Promise<string> {
  if (dbTokenCache && dbTokenCache.expiresAt - Date.now() > 120_000) {
    return Promise.resolve(dbTokenCache.token);
  }
  if (dbTokenInFlight) {
    return dbTokenInFlight;
  }
  const p = new Promise<string>((resolve, reject) => {
    execFile(
      azPath,
      ["account", "get-access-token", "--resource", DATABASE_RESOURCE, "--output", "json"],
      { timeout: 30000 },
      (err, stdout, stderr) => {
        if (err) {
          reject(new Error(String(stderr || err).trim()));
          return;
        }
        try {
          const j = JSON.parse(stdout) as {
            accessToken: string;
            expires_on?: number;
          };
          const expiresAt = j.expires_on
            ? j.expires_on * 1000
            : Date.now() + 50 * 60 * 1000;
          dbTokenCache = { token: j.accessToken, expiresAt };
          resolve(j.accessToken);
        } catch {
          reject(new Error("could not parse the Azure CLI token"));
        }
      },
    );
  });
  // Clear the in-flight slot once settled so a later call can retry/refresh.
  // Use then(clear, clear) — not finally — so this cleanup branch handles the
  // rejection too and never surfaces as an unhandled rejection (the returned
  // `p` is what callers await and handle).
  dbTokenInFlight = p;
  const clear = () => {
    if (dbTokenInFlight === p) {
      dbTokenInFlight = undefined;
    }
  };
  p.then(clear, clear);
  return p;
}

// Build a child-process environment carrying the database token when we can
// get it, so Python skips the az cold start; falls back to Python's own auth.
export async function envWithToken(): Promise<Record<string, string>> {
  const env: Record<string, string> = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (v !== undefined) {
      env[k] = v;
    }
  }
  try {
    env[ACCESS_TOKEN_ENV] = await getDatabaseToken();
  } catch {
    /* no token — Python falls back to its own auth chain */
  }
  return env;
}

export interface Procedure {
  schema: string;
  name: string;
}

export interface ProcParameter {
  name: string; // @-prefixed
  type: string;
  mode: string; // "IN" | "OUT" | "INOUT"
}

async function runIntrospect(python: string, args: string[]): Promise<unknown> {
  const env = await envWithToken();
  return new Promise((resolve, reject) => {
    execFile(
      python,
      ["-m", "tsql_fabric_debugger.introspect", ...args],
      { timeout: 60000, maxBuffer: 8 * 1024 * 1024, env },
      (err, stdout, stderr) => {
        const r = parseIntrospectResult(stdout, err != null, String(stderr || err || ""));
        if ("error" in r) {
          reject(new Error(r.error));
        } else {
          resolve(r.ok);
        }
      },
    );
  });
}

export async function killOrphanSessions(
  python: string,
  server: string,
  database: string,
  minIdleSeconds: number,
): Promise<number[]> {
  const r = (await runIntrospect(python, [
    "kill-orphans",
    "--server",
    server,
    "--database",
    database,
    "--min-idle",
    String(minIdleSeconds),
  ])) as { killed: number[] };
  return r.killed ?? [];
}

export async function listParameters(
  python: string,
  server: string,
  database: string,
  procName: string,
): Promise<ProcParameter[]> {
  return (await runIntrospect(python, [
    "parameters",
    "--proc",
    procName,
    "--server",
    server,
    "--database",
    database,
  ])) as ProcParameter[];
}

// List the warehouse's deployed procedures by spawning the library's
// introspection CLI (same Python interpreter the adapter uses).
export async function listProcedures(
  python: string,
  server: string,
  database: string,
): Promise<Procedure[]> {
  return (await runIntrospect(python, [
    "procedures",
    "--server",
    server,
    "--database",
    database,
  ])) as Procedure[];
}

export interface Workspace {
  id: string;
  displayName: string;
}

export interface Warehouse {
  id: string;
  displayName: string;
  connectionString: string; // the SQL endpoint pyodbc connects to
}

export interface NotebookItem {
  id: string;
  displayName: string;
  workspaceId: string;
}

export class FabricAuthError extends Error {}

// Get an access token from the Azure CLI. Kept dependency-free (no azure-identity
// in the extension) and consistent with how the library authenticates.
export function getToken(azPath = "az"): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(
      azPath,
      [
        "account",
        "get-access-token",
        "--resource",
        FABRIC_RESOURCE,
        "--query",
        "accessToken",
        "--output",
        "tsv",
      ],
      { timeout: 30000 },
      (err, stdout, stderr) => {
        if (err) {
          reject(
            new FabricAuthError(
              /not.*logged in|az login|ENOENT/i.test(String(stderr || err))
                ? "Not signed in to Azure. Run `az login` in a terminal, then try again."
                : `Azure CLI failed: ${String(stderr || err).trim()}`,
            ),
          );
          return;
        }
        const token = stdout.trim();
        token
          ? resolve(token)
          : reject(new FabricAuthError("Azure CLI returned an empty token."));
      },
    );
  });
}

async function api<T>(token: string, path: string): Promise<T[]> {
  const out: T[] = [];
  let url: string | undefined = `${FABRIC_API}${path}`;
  // the Fabric list APIs page with continuationToken/Uri
  while (url) {
    const resp = await fetch(url, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!resp.ok) {
      const body = await resp.text();
      throw new Error(`Fabric API ${resp.status}: ${body.slice(0, 200)}`);
    }
    const json = (await resp.json()) as {
      value?: T[];
      continuationUri?: string;
    };
    if (json.value) {
      out.push(...json.value);
    }
    url = json.continuationUri;
  }
  return out;
}

export async function listWorkspaces(token: string): Promise<Workspace[]> {
  const rows = await api<Workspace>(token, "/workspaces");
  return rows.sort((a, b) => a.displayName.localeCompare(b.displayName));
}

export async function listWarehouses(
  token: string,
  workspaceId: string,
): Promise<Warehouse[]> {
  const rows = await api<{
    id: string;
    displayName: string;
    properties?: { connectionString?: string };
  }>(token, `/workspaces/${workspaceId}/warehouses`);
  return rows
    .map((w) => ({
      id: w.id,
      displayName: w.displayName,
      connectionString: w.properties?.connectionString ?? "",
    }))
    .sort((a, b) => a.displayName.localeCompare(b.displayName));
}

export async function listNotebooks(
  token: string,
  workspaceId: string,
): Promise<NotebookItem[]> {
  const rows = await api<{ id: string; displayName: string }>(
    token,
    `/workspaces/${workspaceId}/items?type=Notebook`,
  );
  return rows
    .map((n) => ({ id: n.id, displayName: n.displayName, workspaceId }))
    .sort((a, b) => a.displayName.localeCompare(b.displayName));
}

export function notebookUrl(workspaceId: string, itemId: string): string {
  return `https://app.fabric.microsoft.com/groups/${workspaceId}/synapsenotebooks/${itemId}`;
}

// Download a notebook's .ipynb source from Fabric (getDefinition). Handles both
// the synchronous (200) and long-running (202 + poll) shapes of the API.
export async function getNotebookIpynb(
  token: string,
  workspaceId: string,
  itemId: string,
  signal?: AbortSignal,
): Promise<string> {
  const headers = { Authorization: `Bearer ${token}` };
  const resp = await fetch(
    `${FABRIC_API}/workspaces/${workspaceId}/items/${itemId}/getDefinition?format=ipynb`,
    { method: "POST", headers, signal },
  );

  let result: {
    definition?: { parts?: { path: string; payload: string }[] };
  };
  if (resp.status === 200) {
    result = (await resp.json()) as typeof result;
  } else if (resp.status === 202) {
    const location = resp.headers.get("Location");
    if (!location) {
      throw new Error("Fabric getDefinition: missing operation Location.");
    }
    for (let i = 0; i < 30; i++) {
      await new Promise((r) => setTimeout(r, 1500));
      if (signal?.aborted) {
        throw new Error("cancelled");
      }
      const op = await fetch(location, { headers, signal });
      const status = ((await op.json()) as { status?: string }).status;
      if (status === "Succeeded") {
        break;
      }
      if (status === "Failed") {
        throw new Error("Fabric getDefinition operation failed.");
      }
    }
    const res = await fetch(`${location}/result`, { headers, signal });
    if (!res.ok) {
      throw new Error(`Fabric getDefinition result: ${res.status}`);
    }
    result = (await res.json()) as typeof result;
  } else {
    throw new Error(
      `Fabric getDefinition: ${resp.status} ${(await resp.text()).slice(0, 200)}`,
    );
  }

  const part = result.definition?.parts?.find((p) =>
    p.path.endsWith(".ipynb"),
  );
  if (!part) {
    throw new Error("Fabric getDefinition: no .ipynb part in the response.");
  }
  return Buffer.from(part.payload, "base64").toString("utf8");
}

// Find which workspace owns a warehouse with the given SQL endpoint. Lets the
// notebook list work when server/database were set in Settings directly,
// without going through "Connect to Warehouse".
export async function findWorkspaceForServer(
  token: string,
  server: string,
): Promise<string | undefined> {
  const target = server.trim().toLowerCase();
  for (const ws of await listWorkspaces(token)) {
    const whs = await listWarehouses(token, ws.id).catch(() => []);
    if (whs.some((w) => w.connectionString.trim().toLowerCase() === target)) {
      return ws.id;
    }
  }
  return undefined;
}
