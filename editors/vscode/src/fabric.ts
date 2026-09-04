// Microsoft Fabric REST discovery, so the user picks a warehouse from a list
// instead of pasting a cryptic SQL endpoint. Auth reuses the Azure CLI login
// the Python library already requires (AzureCliCredential) — one `az login`.

import { execFile } from "node:child_process";

const FABRIC_API = "https://api.fabric.microsoft.com/v1";
const FABRIC_RESOURCE = "https://api.fabric.microsoft.com";

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
