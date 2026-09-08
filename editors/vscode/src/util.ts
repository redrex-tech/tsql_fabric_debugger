// Pure helpers, free of the `vscode` API so they can be unit- and
// mutation-tested without an Extension Host.

// Coerce a value typed in a parameter prompt, matching the launch.json params
// semantics: quoted = string; NULL = null; a strict integer that round-trips
// as a JS number -> number (bigger stays a string so BIGINT is exact); a
// strict decimal -> number; leading-zero forms (codes like "00123") and
// anything else stay strings.
export function coerceParam(raw: string): unknown {
  const s = raw.trim();
  if (s.toUpperCase() === "NULL") {
    return null;
  }
  if (
    s.length >= 2 &&
    s[0] === s[s.length - 1] &&
    (s[0] === "'" || s[0] === '"')
  ) {
    return s.slice(1, -1);
  }
  if (/^[+-]?(0|[1-9]\d*)$/.test(s)) {
    const n = Number(s);
    return Number.isSafeInteger(n) ? n : s;
  }
  if (/^[+-]?(0|[1-9]\d*|)\.\d+$/.test(s)) {
    return parseFloat(s);
  }
  return raw;
}

export interface IntrospectOk {
  ok: unknown;
}
export interface IntrospectErr {
  error: string;
}

// Decide what the introspection CLI returned. The CLI prints a JSON array on
// success and {"error": "..."} on failure (exiting 1), so the JSON in stdout
// wins even when the process reported a non-zero exit; stderr is the last
// resort. hasError is true when the child exited non-zero.
export function parseIntrospectResult(
  stdout: string,
  hasError: boolean,
  stderr: string,
): IntrospectOk | IntrospectErr {
  let data: unknown;
  try {
    data = JSON.parse(stdout);
  } catch {
    /* not JSON — data stays undefined */
  }
  if (data && typeof data === "object" && "error" in data) {
    return { error: String((data as { error: unknown }).error) };
  }
  if (hasError) {
    return { error: (stderr || "introspection failed").trim() };
  }
  if (data === undefined) {
    return { error: `unexpected output: ${stdout.slice(0, 200)}` };
  }
  return { ok: data };
}

export interface VarMatch {
  name: string;
  start: number;
  end: number;
}

// Find @variable / @@variable spans in a line of T-SQL, for inline values.
export function matchVariables(text: string): VarMatch[] {
  const out: VarMatch[] = [];
  for (const m of text.matchAll(/@{1,2}\w+/g)) {
    const start = m.index ?? 0;
    out.push({ name: m[0], start, end: start + m[0].length });
  }
  return out;
}

// The T-SQL expressions to evaluate inside a VS Code logpoint message: the
// content of each {…} placeholder. Text outside braces is literal.
export function logpointExpressions(message: string): string[] {
  const out: string[] = [];
  for (const m of message.matchAll(/\{([^}]+)\}/g)) {
    out.push(m[1].trim());
  }
  return out;
}

// Decide whether a warehouse (by name or SQL endpoint) is one the user flagged
// as production. Matching is case-insensitive substring against each pattern,
// tested against both the database name and the server endpoint, so a pattern
// like "prod" or "xxxx.datawarehouse" catches either. Empty/blank patterns are
// ignored so a stray "" in the list never flags everything as production.
export function isProductionTarget(
  server: string,
  database: string,
  patterns: string[],
): boolean {
  const hay = `${database}\n${server}`.toLowerCase();
  return patterns.some((p) => {
    const needle = p.trim().toLowerCase();
    return needle.length > 0 && hay.includes(needle);
  });
}

// ---------------------------------------------------------------------------
// Result-set payloads (from the adapter's tsqlFabricResultSet event) and CSV
// export. Kept here (vscode-free) so the data logic is unit/mutation-tested.
// ---------------------------------------------------------------------------
export interface ResultSet {
  columns: string[];
  rows: unknown[][];
  truncated: boolean;
}
export interface ResultSetPayload {
  line: number;
  sets: ResultSet[];
}

// The adapter may send the newer {line, sets:[...]} shape or the older
// {line, columns, rows, truncated} single-set shape — accept both so an
// extension/lib version skew never drops the grid. Returns undefined when the
// body is not a recognizable result-set payload.
export function normalizePayload(body: unknown): ResultSetPayload | undefined {
  const b = body as Record<string, unknown>;
  if (!b || typeof b.line !== "number") {
    return undefined;
  }
  if (Array.isArray(b.sets)) {
    return { line: b.line, sets: b.sets as ResultSet[] };
  }
  if (Array.isArray(b.columns) && Array.isArray(b.rows)) {
    return {
      line: b.line,
      sets: [
        {
          columns: b.columns as string[],
          rows: b.rows as unknown[][],
          truncated: Boolean(b.truncated),
        },
      ],
    };
  }
  return undefined;
}

// RFC-4180-ish CSV: quote a field when it holds a comma, quote, or newline,
// doubling embedded quotes; NULL (null/undefined) becomes an empty field.
export function csvCell(v: unknown): string {
  if (v === null || v === undefined) {
    return "";
  }
  const s = String(v);
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function toCsv(set: ResultSet): string {
  const lines = [set.columns.map(csvCell).join(",")];
  for (const row of set.rows) {
    lines.push(row.map(csvCell).join(","));
  }
  return lines.join("\r\n");
}

// ---------------------------------------------------------------------------
// Fabric round-trip: normalize a procedure for redeploy, and generate a
// deploy notebook. Pure (vscode-free) so it's unit/mutation-tested.
// ---------------------------------------------------------------------------

// Normalize the first `CREATE [OR ALTER] PROC[EDURE]` to a canonical
// `CREATE OR ALTER PROCEDURE`, so the exported script is idempotent to redeploy.
// Source with no CREATE PROCEDURE is returned unchanged.
export function toCreateOrAlter(sql: string): string {
  return sql.replace(
    /\bCREATE\s+(?:OR\s+ALTER\s+)?PROC(?:EDURE)?\b/i,
    "CREATE OR ALTER PROCEDURE",
  );
}

// A safe file base for a procedure: "schema.name" with unsafe chars collapsed
// to "_". Brackets are stripped; an empty schema yields just the name.
export function procFileBase(schema: string, name: string): string {
  const clean = (s: string) =>
    s
      .replace(/[[\]]/g, "")
      .replace(/[^\w.-]+/g, "_")
      .replace(/^_+|_+$/g, "");
  const s = clean(schema);
  const n = clean(name) || "procedure";
  return s ? `${s}.${n}` : n;
}

function nbSource(lines: string[]): string[] {
  // nbformat: each line keeps its trailing newline except the last
  return lines.map((l, i) => (i < lines.length - 1 ? l + "\n" : l));
}

// Build a Fabric (Python) notebook whose code cell (re)creates the procedure
// in the warehouse — a CREATE OR ALTER, safe to re-run. The T-SQL is kept
// visible in the cell as a triple-quoted string; the user fills SERVER/DATABASE
// and runs it in Fabric. Returns the .ipynb JSON.
export function buildDeployNotebook(sqlText: string, procLabel: string): string {
  const ddl = toCreateOrAlter(sqlText)
    .replace(/\\/g, "\\\\")
    .replace(/"""/g, '\\"\\"\\"');
  const md = [
    `# Deploy \`${procLabel}\``,
    "",
    "Run this notebook in Microsoft Fabric to (re)create the procedure in the",
    "warehouse. It runs a `CREATE OR ALTER PROCEDURE`, so it is safe to re-run.",
  ];
  const code = [
    "# %pip install tsql-fabric-debugger   # uncomment on the first run",
    "from tsql_fabric_debugger import connect",
    "",
    'SERVER = "<your warehouse SQL endpoint>"',
    'DATABASE = "<your warehouse name>"',
    "",
    'DDL = """',
    ...ddl.split("\n"),
    '"""',
    "",
    "conn = connect(SERVER, DATABASE, autocommit=True)",
    "conn.cursor().execute(DDL)",
    `print("deployed: ${procLabel}")`,
  ];
  const nb = {
    cells: [
      { cell_type: "markdown", metadata: {}, source: nbSource(md) },
      {
        cell_type: "code",
        metadata: {},
        execution_count: null,
        outputs: [],
        source: nbSource(code),
      },
    ],
    metadata: {
      language_info: { name: "python" },
      kernelspec: {
        display_name: "Python 3",
        language: "python",
        name: "python3",
      },
    },
    nbformat: 4,
    nbformat_minor: 5,
  };
  return JSON.stringify(nb, null, 1);
}
