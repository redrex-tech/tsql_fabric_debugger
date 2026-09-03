# -*- coding: utf-8 -*-
from tsql_fabric_debugger.scanner import is_punct, is_word, scan


def test_ignores_semicolon_inside_string():
    tokens = scan("SET @x = N'a ; b';")
    strings = [t for t in tokens if t["k"] == "str"]
    semicolons = [t for t in tokens if is_punct(t, ";")]
    assert len(strings) == 1
    assert len(semicolons) == 1  # only the real terminator


def test_doubled_quotes_escaped_in_string():
    sql = "SELECT 'it''s ok'"
    tokens = scan(sql)
    s = next(t for t in tokens if t["k"] == "str")
    assert sql[s["s"]:s["e"]] == "'it''s ok'"


def test_line_and_nested_block_comments():
    tokens = scan("SELECT 1 -- comment ; SET\n/* a /* b */ c */ FROM t")
    words = [t["u"] for t in tokens if t["k"] == "w"]
    assert words == ["SELECT", "FROM", "T"]


def test_variable_and_rowcount_as_single_tokens():
    tokens = scan("SET @qty = @@ROWCOUNT")
    assert any(is_word(t, "@QTY") for t in tokens)
    assert any(is_word(t, "@@ROWCOUNT") for t in tokens)


def test_brackets_as_single_tokens():
    tokens = scan("SELECT [col;umn] FROM [db1].[sales_2015].tab")
    brackets = [t for t in tokens if t["k"] == "brk"]
    assert len(brackets) == 3
    assert not any(is_punct(t, ";") for t in tokens)
