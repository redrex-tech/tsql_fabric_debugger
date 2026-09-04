import { describe, expect, it } from "vitest";
import {
  coerceParam,
  isProductionTarget,
  logpointExpressions,
  matchVariables,
  parseIntrospectResult,
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
