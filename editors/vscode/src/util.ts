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
