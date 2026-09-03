# -*- coding: utf-8 -*-
"""Snapshot and invariant tests for mutation testing (mutmut).

The functional suite covers the main behavior; these tests pin down the
EXACT behavior of the scanner and the parser — token positions, branch
spans, step texts — so that fine-grained mutations (swapped operator,
shifted index, inverted condition) do not survive.
"""
from pathlib import Path

import pytest

from tsql_fabric_debugger.parser import (
    eval_literal,
    extract_declares,
    find_procedure,
    find_try_catch_end,
    parse_conditional,
    parse_params,
    procedure_body,
    rewrite,
    scan_declares,
    skip_block,
    skip_stmt,
    split_steps,
)
from tsql_fabric_debugger.scanner import scan

FIXTURE = (Path(__file__).parent / "fixtures" / "demo_proc.sql").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# scanner — exact snapshot and invariants
# ---------------------------------------------------------------------------
def test_scanner_exact_snapshot():
    snippet = ("SET @x=N'a;''b' + [c;]] d] -- end\n"
               "/* x /* y */ z */ SELECT 12.5, \"q id\", @@ROWCOUNT#t_1$")
    expected = [
        ("w", 0, 3, "SET"),
        ("w", 4, 6, "@X"),
        ("p", 6, 7, "="),
        ("w", 7, 8, "N"),
        ("str", 8, 15, None),
        ("p", 16, 17, "+"),
        ("brk", 18, 26, None),
        ("w", 52, 58, "SELECT"),
        ("num", 59, 63, None),
        ("p", 63, 64, ","),
        ("brk", 65, 71, None),
        ("p", 71, 72, ","),
        ("w", 73, 88, "@@ROWCOUNT#T_1$"),
    ]
    got = [(t["k"], t["s"], t["e"], t.get("u")) for t in scan(snippet)]
    assert got == expected


def test_scanner_snapshot_with_uppercase_and_mid_text_comment():
    # capital 'X' in the source, a newline BEFORE the '--' and two lines
    # after it: catches mutants in find()/rfind() and in the whitespace set
    snippet = "X1\nY2 -- cmt\nZ3\nW4"
    got = [(t["k"], t["s"], t["e"], t.get("u")) for t in scan(snippet)]
    assert got == [
        ("w", 0, 2, "X1"),
        ("w", 3, 5, "Y2"),
        ("w", 13, 15, "Z3"),
        ("w", 16, 18, "W4"),
    ]


def test_parse_params_paren_right_after_the_type():
    # header ')' glued to the type (no default/OUTPUT before it): the parser
    # must hand control back without swallowing the AS
    sql = "CREATE PROCEDURE p (@a INT) AS BEGIN SET @a = 1; END"
    tokens = scan(sql)
    name, params, i_as = parse_params(sql, tokens, find_procedure(tokens))
    assert name == "p"
    assert [(p["name"], p["type"], p["default"], p["output"]) for p in params] == [
        ("@a", "INT", None, False),
    ]
    i0, i1 = procedure_body(tokens, i_as)
    assert sql[tokens[i0]["s"]:tokens[i1 - 1]["e"]] == "SET @a = 1;"


def test_scanner_invariants_over_corpus():
    corpus = [
        FIXTURE,
        "SELECT 'unterminated string",
        "SELECT [unterminated bracket",
        'SELECT "unterminated quote',
        "a--\nb/*c*/d'e''f'[g]]h]123.4x @v #t ##g $z\n\t;()=",
    ]
    for sql in corpus:
        tokens = scan(sql)
        prev_end = 0
        for t in tokens:
            # ordered, non-overlapping, inside the text
            assert 0 <= t["s"] < t["e"] <= len(sql)
            assert t["s"] >= prev_end
            prev_end = t["e"]
            if t["k"] == "w":
                assert t["u"] == sql[t["s"]:t["e"]].upper()
            if t["k"] == "str":
                assert sql[t["s"]] == "'"
            if t["k"] == "p":
                assert t["e"] - t["s"] == 1 and t["u"] == sql[t["s"]]


# ---------------------------------------------------------------------------
# parser — header in every variant
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("sql, name", [
    ("CREATE PROCEDURE dbo.p1 AS BEGIN SET NOCOUNT ON; END", "dbo.p1"),
    ("CREATE OR ALTER PROCEDURE p2 AS BEGIN SET NOCOUNT ON; END", "p2"),
    ("CREATE PROC p3 AS BEGIN SET NOCOUNT ON; END", "p3"),
    ("-- comment\nCREATE PROCEDURE [sch].[p4] AS BEGIN SET NOCOUNT ON; END", "[sch].[p4]"),
])
def test_find_procedure_variants(sql, name):
    tokens = scan(sql)
    i = find_procedure(tokens)
    assert i is not None
    got_name, _, _ = parse_params(sql, tokens, i)
    assert got_name == name


@pytest.mark.parametrize("sql", [
    "SELECT 1;",
    "ALTER TABLE t ADD c INT;",
    "SET @x = 'CREATE PROCEDURE p AS';",   # inside a string it does not count
])
def test_find_procedure_no_match(sql):
    assert find_procedure(scan(sql)) is None


def test_parse_params_full_snapshot():
    tokens = scan(FIXTURE)
    _, params, _ = parse_params(FIXTURE, tokens, find_procedure(tokens))
    assert [(p["name"], p["type"], p["default"], p["output"]) for p in params] == [
        ("@n", "INT", None, False),
        ("@label", "NVARCHAR(50)", "N'demo'", False),
        ("@result", "INT", None, True),
        ("@err", "NVARCHAR(MAX)", None, True),
    ]


def test_parse_params_with_header_parens():
    sql = ("CREATE PROCEDURE p (@a INT = 5, @b NVARCHAR(10) OUTPUT) AS "
           "BEGIN SET @a = 1; END")
    tokens = scan(sql)
    name, params, i_as = parse_params(sql, tokens, find_procedure(tokens))
    assert name == "p"
    assert [(p["name"], p["type"], p["default"], p["output"]) for p in params] == [
        ("@a", "INT", "5", False),
        ("@b", "NVARCHAR(10)", None, True),
    ]
    # body located correctly after the AS
    i0, i1 = procedure_body(tokens, i_as)
    body = sql[tokens[i0]["s"]:tokens[i1 - 1]["e"]]
    assert body == "SET @a = 1;"


def test_procedure_body_missing_begin_or_end():
    with pytest.raises(ValueError, match="BEGIN"):
        procedure_body(scan("CREATE PROCEDURE p AS SELECT 1"), 0)
    with pytest.raises(ValueError, match="END of procedure body"):
        procedure_body(scan("CREATE PROCEDURE p AS BEGIN SELECT 1;"), 0)


# ---------------------------------------------------------------------------
# slicing — exact snapshot of the fixture's steps
# ---------------------------------------------------------------------------
def _parse_fixture():
    tokens = scan(FIXTURE)
    _, _, i_as = parse_params(FIXTURE, tokens, find_procedure(tokens))
    i0, i1 = procedure_body(tokens, i_as)
    ctx = {"catches": []}
    steps = split_steps(FIXTURE, tokens, i0, i1, ctx)
    return tokens, steps, ctx["catches"][0] if ctx["catches"] else [], (i0, i1)


def test_split_steps_snapshot_kind_line_and_text():
    _, steps, catch, _ = _parse_fixture()
    assert [(s["kind"], s["line"]) for s in steps] == [
        ("stmt", 8), ("declare", 10), ("declare", 11), ("if_block", 14),
        ("if_block", 19), ("while_block", 29), ("stmt", 35), ("stmt", 36),
    ]
    assert steps[0]["text"] == "SET NOCOUNT ON"
    assert steps[1]["text"] == "DECLARE @i INT = 0"
    assert steps[7]["text"] == "SET @result = @total"
    assert [s["try"] for s in steps] == [False, False, False, True, True, True, True, True]
    assert [(s["kind"], s["line"]) for s in catch] == [("stmt", 39), ("stmt", 40)]
    assert catch[1]["text"] == "SET @err = CONCAT(N'Error in p_demo: ', ERROR_MESSAGE())"


def test_if_else_branches_with_exact_spans():
    tokens, steps, _, _ = _parse_fixture()

    def span_text(span):
        return " ".join(FIXTURE[tokens[span[0]]["s"]:tokens[span[1] - 1]["e"]].split())

    if_else = steps[4]
    assert not if_else["is_loop"]
    conds = [None if b["cond"] is None else span_text(b["cond"]) for b in if_else["branches"]]
    bodies = [span_text(b["body"]) for b in if_else["branches"]]
    assert conds == ["@n > 0", None]
    assert bodies == ["DECLARE @bonus INT = 100; SET @total = @bonus;", "SET @total = -1;"]

    loop = steps[5]
    assert loop["is_loop"] and len(loop["branches"]) == 1
    assert span_text(loop["branches"][0]["cond"]) == "@i < 3"
    assert span_text(loop["branches"][0]["body"]) == "SET @i = @i + 1; SET @total = @total + @i;"


def test_else_if_chain_and_branch_without_begin():
    sql = ("IF @a = 1 SET @x = 10; "
           "ELSE IF @a = 2 BEGIN SET @x = 20; END "
           "ELSE SET @x = 30;")
    tokens = scan(sql)
    end, branches, is_loop = parse_conditional(tokens, 0, len(tokens))
    assert not is_loop and end == len(tokens)
    assert len(branches) == 3

    def span_text(span):
        return " ".join(sql[tokens[span[0]]["s"]:tokens[span[1] - 1]["e"]].split())

    assert span_text(branches[0]["cond"]) == "@a = 1"
    assert span_text(branches[0]["body"]) == "SET @x = 10;"
    assert span_text(branches[1]["cond"]) == "@a = 2"
    assert span_text(branches[1]["body"]) == "SET @x = 20;"
    assert branches[2]["cond"] is None
    assert span_text(branches[2]["body"]) == "SET @x = 30;"


def test_skip_stmt_ignores_inner_delimiters():
    # ';' inside parens and CASE...END do not terminate the statement
    sql = "SET @x = (SELECT CASE WHEN a=1 THEN 2 ELSE 3 END FROM t); SET @y = 1;"
    tokens = scan(sql)
    end = skip_stmt(tokens, 0, len(tokens))
    assert sql[tokens[0]["s"]:tokens[end - 1]["e"]].endswith("FROM t);")


def test_skip_block_with_nesting():
    sql = "BEGIN BEGIN SET @a = 1; END SET @b = 2; END SET @c = 3;"
    tokens = scan(sql)
    end = skip_block(tokens, 0, len(tokens))
    assert sql[tokens[end]["s"]:].startswith("SET @c")


def test_find_try_catch_end_errors():
    tokens = scan("BEGIN TRY SET @a = 1; END CATCH")
    with pytest.raises(ValueError, match="expected"):
        find_try_catch_end(tokens, 2, len(tokens), "TRY")
    tokens = scan("BEGIN TRY SET @a = 1;")
    with pytest.raises(ValueError, match="TRY not found"):
        find_try_catch_end(tokens, 2, len(tokens), "TRY")
    # happy path with the exact END position
    sql = "BEGIN TRY SET @a = 1; END TRY BEGIN CATCH SET @b = 2; END CATCH"
    tokens = scan(sql)
    end_idx = find_try_catch_end(tokens, 2, len(tokens), "TRY")
    assert sql[tokens[end_idx]["s"]:tokens[end_idx + 1]["e"]] == "END TRY"


# ---------------------------------------------------------------------------
# declares, literals and rewriting
# ---------------------------------------------------------------------------
def test_scan_declares_snapshot():
    tokens, _, _, (i0, i1) = _parse_fixture()
    assert scan_declares(FIXTURE, tokens, i0, i1) == [
        ("@i", "INT", "0"),
        ("@total", "INT", "0"),
        ("@bonus", "INT", "100"),
    ]


def test_extract_declares_multiple_variables():
    assert extract_declares(
        "DECLARE @a INT = f(@z), @b NVARCHAR(10), @c DATETIME2(6) = SYSUTCDATETIME();"
    ) == [
        ("@a", "INT", "f(@z)"),
        ("@b", "NVARCHAR(10)", None),
        ("@c", "DATETIME2(6)", "SYSUTCDATETIME()"),
    ]


@pytest.mark.parametrize("expr, expected", [
    ("NULL", (True, None)),
    ("null", (True, None)),
    ("N'demo'", (True, "demo")),
    ("'it''s'", (True, "it's")),
    ("42", (True, 42)),
    ("-7", (True, -7)),
    ("3.14", (True, 3.14)),
    ("-0.5", (True, -0.5)),
    ("GETDATE()", (False, None)),
    ("N'a' + N'b'", (False, None)),
    (None, (False, None)),
])
def test_eval_literal_every_form(expr, expected):
    assert eval_literal(expr) == expected


def test_rewrite_snapshot_and_order():
    text, markers = rewrite(
        "SET @q = @@ROWCOUNT; SET @e = ERROR_MESSAGE(); SET @r = @@ROWCOUNT;",
        "boom", True)
    assert text == "SET @q = ?; SET @e = ?; SET @r = ?;"
    assert markers == ["@@ROWCOUNT", "ERROR_MESSAGE", "@@ROWCOUNT"]


def test_rewrite_disabled_without_context():
    # no @@ROWCOUNT in the environment and no captured error: nothing rewritten
    original = "SET @q = @@ROWCOUNT; SET @e = ERROR_MESSAGE();"
    text, markers = rewrite(original, None, False)
    assert text == original and markers == []


def test_rewrite_requires_parens_on_error_message():
    text, markers = rewrite("SET @e = ERROR_MESSAGE", "boom", False)
    assert markers == [] and "ERROR_MESSAGE" in text
