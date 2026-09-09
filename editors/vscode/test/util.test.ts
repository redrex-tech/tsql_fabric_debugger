import { describe, expect, it } from "vitest";
import {
  coerceParam,
  csvCell,
  isProductionTarget,
  logpointExpressions,
  matchVariables,
  normalizePayload,
  parseIntrospectResult,
  parseProcName,
  procFileBase,
  stampNotebookLink,
  readNotebookLink,
  stripNotebookLink,
  artifactPath,
  toCreateOrAlter,
  toCsv,
  buildDeployNotebook,
} from "../src/util";

describe("coerceParam", () => {
  it("maps NULL (any case) to null", () => {
    expect(coerceParam("NULL")).toBeNull();
    expect(coerceParam("null")).toBeNull();
    expect(coerceParam("  Null ")).toBeNull();
  });

  it("strips a single/double-quoted string", () => {
    expect(coerceParam("'00123'")).toBe("00123");
    expect(coerceParam('"abc"')).toBe("abc");
    expect(coerceParam("'  keep spaces  '")).toBe("  keep spaces  ");
  });

  it("parses a plain integer", () => {
    expect(coerceParam("2015")).toBe(2015);
    expect(coerceParam("-7")).toBe(-7);
    expect(coerceParam("0")).toBe(0);
  });

  it("keeps leading-zero codes as strings", () => {
    expect(coerceParam("00123")).toBe("00123");
    expect(coerceParam("007")).toBe("007");
  });

  it("keeps a BIGINT beyond 2^53 exact, as a string", () => {
    expect(coerceParam("12345678901234567")).toBe("12345678901234567");
    expect(coerceParam("9007199254740991")).toBe(9007199254740991); // MAX_SAFE
    expect(coerceParam("9007199254740993")).toBe("9007199254740993"); // > MAX_SAFE
  });

  it("parses decimals", () => {
    expect(coerceParam("19.90")).toBeCloseTo(19.9);
    expect(coerceParam("-0.5")).toBeCloseTo(-0.5);
    expect(coerceParam(".5")).toBeCloseTo(0.5);
  });

  it("leaves non-numeric, non-quoted text as the raw string", () => {
    expect(coerceParam("2015-01-01")).toBe("2015-01-01");
    expect(coerceParam("1e5")).toBe("1e5");
    expect(coerceParam("1,5")).toBe("1,5");
    expect(coerceParam("hello")).toBe("hello");
  });

  it("only strips quotes when BOTH ends are the SAME quote and len >= 2", () => {
    expect(coerceParam("'")).toBe("'"); // single char: not stripped
    expect(coerceParam("''")).toBe(""); // exactly two quotes -> empty
    expect(coerceParam("'a\"")).toBe("'a\""); // mismatched quote ends
    expect(coerceParam("xabcx")).toBe("xabcx"); // same ends but not quotes
    expect(coerceParam("(abc)")).toBe("(abc)"); // same-type-ish but not a quote
  });

  it("anchors the decimal regex at both ends and needs a fractional digit", () => {
    expect(coerceParam("x1.5")).toBe("x1.5"); // junk before -> not a number
    expect(coerceParam("1.5x")).toBe("1.5x"); // junk after -> not a number
    expect(coerceParam("1.55")).toBe(1.55); // two fractional digits kept
    expect(coerceParam("12.5")).toBe(12.5); // multi-digit integer part
    expect(coerceParam("1.")).toBe("1."); // no fractional digit -> string
  });
});

describe("parseIntrospectResult", () => {
  it("returns ok data from clean JSON on success", () => {
    const r = parseIntrospectResult('[{"schema":"dbo","name":"p"}]', false, "");
    expect(r).toEqual({ ok: [{ schema: "dbo", name: "p" }] });
  });

  it("returns the {error} from stdout even on non-zero exit", () => {
    const r = parseIntrospectResult('{"error":"Login timeout"}', true, "runpy warning");
    expect(r).toEqual({ error: "Login timeout" });
  });

  it("falls back to stderr only when there is no JSON error", () => {
    const r = parseIntrospectResult("not json", true, "boom on stderr");
    expect(r).toEqual({ error: "boom on stderr" });
  });

  it("reports unexpected output when exit is 0 but stdout is not JSON", () => {
    const r = parseIntrospectResult("garbage", false, "");
    expect("error" in r && r.error.startsWith("unexpected output:")).toBe(true);
  });

  it("has a default error message when stderr is empty", () => {
    const r = parseIntrospectResult("not json", true, "");
    expect(r).toEqual({ error: "introspection failed" });
  });

  it("handles a JSON primitive without the 'error' in <primitive> crash", () => {
    // JSON.parse("5") -> 5; "error" in 5 would throw without the object check
    expect(parseIntrospectResult("5", false, "")).toEqual({ ok: 5 });
    expect(parseIntrospectResult("true", false, "")).toEqual({ ok: true });
  });

  it("does not treat a plain array (no error key) as an error", () => {
    expect(parseIntrospectResult("[1,2,3]", false, "")).toEqual({ ok: [1, 2, 3] });
  });

  it("trims the stderr error message", () => {
    expect(parseIntrospectResult("x", true, "  boom  ")).toEqual({ error: "boom" });
  });

  it("truncates unexpected output to 200 chars", () => {
    const long = "z".repeat(250);
    const r = parseIntrospectResult(long, false, "");
    expect("error" in r && r.error).toBe(`unexpected output: ${"z".repeat(200)}`);
  });
});

describe("matchVariables", () => {
  it("finds @var and @@var with correct spans", () => {
    expect(matchVariables("SET @out = @out + 1")).toEqual([
      { name: "@out", start: 4, end: 8 },
      { name: "@out", start: 11, end: 15 },
    ]);
    expect(matchVariables("SELECT @@ROWCOUNT")).toEqual([
      { name: "@@ROWCOUNT", start: 7, end: 17 },
    ]);
  });

  it("returns [] when there is no variable", () => {
    expect(matchVariables("SELECT 1")).toEqual([]);
  });
});

describe("logpointExpressions", () => {
  it("extracts each {expr}, trimmed, in order", () => {
    expect(logpointExpressions("i={@i} fat={@fat}")).toEqual(["@i", "@fat"]);
    expect(logpointExpressions("{ @a + @b } rest")).toEqual(["@a + @b"]);
  });

  it("returns [] for plain text with no braces", () => {
    expect(logpointExpressions("reached here")).toEqual([]);
  });
});

describe("isProductionTarget", () => {
  it("matches a substring of the database name (case-insensitive)", () => {
    expect(isProductionTarget("srv", "WH_Prod", ["prod"])).toBe(true);
    expect(isProductionTarget("srv", "wh_dev", ["prod"])).toBe(false);
  });
  it("matches against the server endpoint too", () => {
    expect(
      isProductionTarget("prod.datawarehouse.fabric.microsoft.com", "wh", [
        "prod.datawarehouse",
      ]),
    ).toBe(true);
  });
  it("ignores blank patterns so an empty entry never flags everything", () => {
    expect(isProductionTarget("srv", "anything", [""])).toBe(false);
    expect(isProductionTarget("srv", "anything", ["  "])).toBe(false);
  });
  it("trims surrounding spaces off a pattern before matching", () => {
    // "wh_prod" has no spaces; only a trimmed " prod " matches it
    expect(isProductionTarget("srv", "wh_prod", [" prod "])).toBe(true);
  });
  it("returns false with no patterns", () => {
    expect(isProductionTarget("srv", "db", [])).toBe(false);
  });
  it("matches any one of several patterns", () => {
    expect(isProductionTarget("srv", "staging", ["prod", "staging"])).toBe(true);
  });
});

describe("normalizePayload", () => {
  it("passes through the new {line, sets} shape", () => {
    const body = { line: 5, sets: [{ columns: ["a"], rows: [[1]], truncated: false }] };
    expect(normalizePayload(body)).toEqual(body);
  });
  it("wraps the legacy {line, columns, rows, truncated} shape into one set", () => {
    expect(
      normalizePayload({ line: 3, columns: ["a", "b"], rows: [[1, 2]], truncated: true }),
    ).toEqual({
      line: 3,
      sets: [{ columns: ["a", "b"], rows: [[1, 2]], truncated: true }],
    });
  });
  it("defaults a missing legacy truncated flag to false", () => {
    const p = normalizePayload({ line: 1, columns: ["a"], rows: [] });
    expect(p?.sets[0].truncated).toBe(false);
  });
  it("returns undefined for an unrecognizable body", () => {
    expect(normalizePayload(undefined)).toBeUndefined();
    expect(normalizePayload({})).toBeUndefined();
    expect(normalizePayload({ line: "x" })).toBeUndefined();
    expect(normalizePayload({ line: 1 })).toBeUndefined(); // no sets, no columns/rows
  });
  it("rejects a non-number line even when sets are present", () => {
    expect(
      normalizePayload({ line: "x", sets: [{ columns: [], rows: [], truncated: false }] }),
    ).toBeUndefined();
  });
  it("needs BOTH columns and rows for the legacy shape (not either)", () => {
    expect(normalizePayload({ line: 1, columns: ["a"] })).toBeUndefined();
    expect(normalizePayload({ line: 1, rows: [[1]] })).toBeUndefined();
  });
});

describe("csvCell", () => {
  it("leaves plain values unquoted", () => {
    expect(csvCell("abc")).toBe("abc");
    expect(csvCell(42)).toBe("42");
  });
  it("maps NULL (null/undefined) to an empty field", () => {
    expect(csvCell(null)).toBe("");
    expect(csvCell(undefined)).toBe("");
  });
  it("quotes fields with comma, quote or newline and doubles quotes", () => {
    expect(csvCell("a,b")).toBe('"a,b"');
    expect(csvCell('he said "hi"')).toBe('"he said ""hi"""');
    expect(csvCell("line1\nline2")).toBe('"line1\nline2"');
    expect(csvCell("carriage\rreturn")).toBe('"carriage\rreturn"');
  });
});

describe("toCsv", () => {
  it("builds a header + CRLF-joined rows", () => {
    const csv = toCsv({
      columns: ["id", "name"],
      rows: [
        [1, "Ann"],
        [2, "O'Brien, Jr"],
        [3, null],
      ],
      truncated: false,
    });
    expect(csv).toBe('id,name\r\n1,Ann\r\n2,"O\'Brien, Jr"\r\n3,');
  });
  it("emits just the header for an empty result set", () => {
    expect(toCsv({ columns: ["a", "b"], rows: [], truncated: false })).toBe("a,b");
  });
});

describe("toCreateOrAlter", () => {
  it("converts CREATE PROCEDURE to CREATE OR ALTER PROCEDURE", () => {
    expect(toCreateOrAlter("CREATE PROCEDURE dbo.p AS SELECT 1")).toBe(
      "CREATE OR ALTER PROCEDURE dbo.p AS SELECT 1",
    );
  });
  it("handles the PROC abbreviation and case/space variance", () => {
    expect(toCreateOrAlter("create   proc dbo.p as x")).toBe(
      "CREATE OR ALTER PROCEDURE dbo.p as x",
    );
  });
  it("is a no-op when already CREATE OR ALTER", () => {
    expect(toCreateOrAlter("CREATE OR ALTER PROCEDURE dbo.p AS x")).toBe(
      "CREATE OR ALTER PROCEDURE dbo.p AS x",
    );
  });
  it("requires a word boundary: RECREATE PROCEDURE is not matched", () => {
    expect(toCreateOrAlter("RECREATE PROCEDURE dbo.p AS x")).toBe(
      "RECREATE PROCEDURE dbo.p AS x",
    );
  });
  it("requires whitespace between CREATE and PROC", () => {
    expect(toCreateOrAlter("CREATEPROCEDURE dbo.p")).toBe("CREATEPROCEDURE dbo.p");
  });
  it("only rewrites the first CREATE PROCEDURE", () => {
    const s = "CREATE PROCEDURE dbo.a AS SELECT 'CREATE PROCEDURE dbo.b'";
    expect(toCreateOrAlter(s)).toBe(
      "CREATE OR ALTER PROCEDURE dbo.a AS SELECT 'CREATE PROCEDURE dbo.b'",
    );
  });
  it("leaves source without a CREATE PROCEDURE unchanged", () => {
    expect(toCreateOrAlter("SELECT 1;")).toBe("SELECT 1;");
  });
});

describe("procFileBase", () => {
  it("joins schema.name", () => {
    expect(procFileBase("dbo", "p_demo")).toBe("dbo.p_demo");
  });
  it("strips brackets and collapses unsafe chars", () => {
    expect(procFileBase("[my schema]", "[weird/name]")).toBe(
      "my_schema.weird_name",
    );
  });
  it("drops an empty schema", () => {
    expect(procFileBase("", "p")).toBe("p");
  });
  it("falls back to 'procedure' for an empty name", () => {
    expect(procFileBase("dbo", "")).toBe("dbo.procedure");
  });
  it("trims leading/trailing underscores produced by unsafe chars", () => {
    // "/dbo/" → "_dbo_" → trimmed to "dbo"; "/p/" → "p"
    expect(procFileBase("/dbo/", "/p/")).toBe("dbo.p");
  });
  it("trims a leading-only and a trailing-only underscore", () => {
    // exercises each side of /^_+|_+$/ independently
    expect(procFileBase("_dbo", "p_")).toBe("dbo.p");
  });
});

describe("buildDeployNotebook", () => {
  it("produces valid ipynb JSON with the DDL as CREATE OR ALTER", () => {
    const nb = JSON.parse(
      buildDeployNotebook("CREATE PROCEDURE dbo.p AS SELECT 1", "dbo.p"),
    );
    expect(nb.nbformat).toBe(4);
    expect(nb.cells).toHaveLength(2);
    expect(nb.cells[0].cell_type).toBe("markdown");
    expect(nb.cells[1].cell_type).toBe("code");
    // markdown mentions the procedure and that it is safe to re-run
    const mdText = nb.cells[0].source.join("");
    expect(mdText).toContain("Deploy");
    expect(mdText).toContain("dbo.p");
    expect(mdText).toContain("CREATE OR ALTER PROCEDURE");
    // code cell: pip line, placeholders, DDL, connect, confirmation
    const codeText = nb.cells[1].source.join("");
    expect(codeText).toContain("%pip install tsql-fabric-debugger");
    expect(codeText).toContain("from tsql_fabric_debugger import connect");
    expect(codeText).toContain('SERVER = "<your warehouse SQL endpoint>"');
    expect(codeText).toContain('DATABASE = "<your warehouse name>"');
    expect(codeText).toContain("CREATE OR ALTER PROCEDURE dbo.p");
    expect(codeText).toContain("connect(SERVER, DATABASE, autocommit=True)");
    expect(codeText).toContain("conn.cursor().execute(DDL)");
    expect(codeText).toContain('print("deployed: dbo.p")');
    // python notebook metadata
    expect(nb.metadata.language_info.name).toBe("python");
    expect(nb.metadata.kernelspec.name).toBe("python3");
    expect(nb.metadata.kernelspec.language).toBe("python");
    expect(nb.nbformat_minor).toBe(5);
    // nbformat source: every line keeps a trailing newline except the last
    for (const cell of nb.cells) {
      const src: string[] = cell.source;
      expect(src.length).toBeGreaterThan(1);
      src.forEach((l: string, i: number) => {
        expect(l.endsWith("\n")).toBe(i < src.length - 1);
      });
    }
  });
  it("escapes a triple-quote in the SQL so the Python string stays valid", () => {
    const nb = JSON.parse(
      buildDeployNotebook('CREATE PROC dbo.p AS SELECT """x"""', "dbo.p"),
    );
    const codeText = nb.cells[1].source.join("");
    expect(codeText).not.toContain('SELECT """x"""');
    expect(codeText).toContain('\\"\\"\\"');
  });
});

describe("parseProcName", () => {
  it("parses schema.name", () => {
    expect(parseProcName("CREATE PROCEDURE sales.load_orders AS x")).toEqual({
      schema: "sales",
      name: "load_orders",
    });
  });
  it("defaults schema to dbo when unqualified", () => {
    expect(parseProcName("CREATE OR ALTER PROC p_demo AS x")).toEqual({
      schema: "dbo",
      name: "p_demo",
    });
  });
  it("strips brackets and tolerates whitespace around the dot", () => {
    expect(parseProcName("create   proc [my schema] . [weird] as x")).toEqual({
      schema: "my schema",
      name: "weird",
    });
  });
  it("returns undefined without a CREATE PROCEDURE", () => {
    expect(parseProcName("SELECT 1;")).toBeUndefined();
  });
});

describe("artifactPath", () => {
  it("schema-type: kind/schema/name (default)", () => {
    expect(artifactPath("procedures", "sales", "load_orders", "schema-type")).toBe(
      "procedures/sales/load_orders",
    );
    expect(artifactPath("deploy", "dbo", "p", "schema-type")).toBe("deploy/dbo/p");
  });
  it("schema: schema/name", () => {
    expect(artifactPath("deploy", "dbo", "p", "schema")).toBe("dbo/p");
  });
  it("type: kind/schema.name", () => {
    expect(artifactPath("procedures", "dbo", "p", "type")).toBe("procedures/dbo.p");
  });
  it("flat: schema.name", () => {
    expect(artifactPath("deploy", "dbo", "p", "flat")).toBe("dbo.p");
  });
  it("falls back to the default layout for an unknown value", () => {
    expect(artifactPath("procedures", "dbo", "p", "weird")).toBe(
      "procedures/dbo/p",
    );
  });
  it("sanitizes and defaults empty segments", () => {
    expect(artifactPath("deploy", "[a/b]", "", "schema-type")).toBe(
      "deploy/a_b/procedure",
    );
  });
});

describe("notebook link (.py round-trip)", () => {
  const py = "# Fabric notebook source\n\n# CELL ****\n\nprint(1)\n";

  it("stamps the link on the first line, keeping the source", () => {
    const out = stampNotebookLink(py, { workspaceId: "ws1", itemId: "it1", displayName: "nb_x" });
    expect(out.split("\n")[0]).toBe(
      '# tsqlFabric-link: {"workspaceId":"ws1","itemId":"it1","displayName":"nb_x"}',
    );
    expect(out).toContain("# Fabric notebook source");
    expect(out).toContain("print(1)");
  });

  it("reads the link back", () => {
    const out = stampNotebookLink(py, { workspaceId: "ws1", itemId: "it1" });
    expect(readNotebookLink(out)).toEqual({
      workspaceId: "ws1",
      itemId: "it1",
      displayName: undefined,
    });
  });

  it("returns undefined when there is no link or it is malformed", () => {
    expect(readNotebookLink(py)).toBeUndefined();
    expect(readNotebookLink("# tsqlFabric-link: not json")).toBeUndefined();
    expect(
      readNotebookLink('# tsqlFabric-link: {"workspaceId":"w"}'),
    ).toBeUndefined(); // missing itemId
  });

  it("strips the link so the pushed copy is the pristine source", () => {
    const stamped = stampNotebookLink(py, { workspaceId: "ws1", itemId: "it1" });
    expect(stripNotebookLink(stamped)).toBe(py);
  });

  it("re-stamping replaces the existing link (no duplicates)", () => {
    const once = stampNotebookLink(py, { workspaceId: "a", itemId: "b" });
    const twice = stampNotebookLink(once, { workspaceId: "c", itemId: "d" });
    expect(twice.match(/tsqlFabric-link/g)).toHaveLength(1);
    expect(readNotebookLink(twice)).toEqual({ workspaceId: "c", itemId: "d", displayName: undefined });
  });
});
