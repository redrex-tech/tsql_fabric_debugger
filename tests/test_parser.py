# -*- coding: utf-8 -*-
from pathlib import Path

from tsql_fabric_debugger.parser import (
    find_procedure,
    parse_conditional,
    parse_params,
    procedure_body,
    rewrite,
    scan_declares,
    split_steps,
)
from tsql_fabric_debugger.scanner import scan

FIXTURE = (Path(__file__).parent / "fixtures" / "demo_proc.sql").read_text(encoding="utf-8")


def _parse():
    tokens = scan(FIXTURE)
    i_proc = find_procedure(tokens)
    name, params, i_as = parse_params(FIXTURE, tokens, i_proc)
    i0, i1 = procedure_body(tokens, i_as)
    ctx = {"catches": []}
    steps = split_steps(FIXTURE, tokens, i0, i1, ctx)
    return tokens, name, params, (i0, i1), steps, ctx["catches"][0] if ctx["catches"] else []


def test_header_name_and_params():
    _, name, params, _, _, _ = _parse()
    assert name == "dbo.p_demo"
    assert [p["name"] for p in params] == ["@n", "@label", "@result", "@err"]
    assert params[1]["default"] == "N'demo'"
    assert [p["output"] for p in params] == [False, False, True, True]


def test_step_slicing_and_catch():
    _, _, _, _, steps, catch = _parse()
    kinds = [s["kind"] for s in steps]
    assert kinds == ["stmt", "declare", "declare", "if_block", "if_block",
                     "while_block", "stmt", "stmt"]
    assert all(s["try"] for s in steps[3:])          # everything after the DECLAREs is inside TRY
    assert len(catch) == 2                            # SET @result / SET @err


def test_if_else_branch_structure():
    _, _, _, _, steps, _ = _parse()
    if_else = steps[4]                                # IF @n > 0 ... ELSE ...
    assert not if_else["is_loop"]
    assert len(if_else["branches"]) == 2
    assert if_else["branches"][0]["cond"] is not None
    assert if_else["branches"][1]["cond"] is None     # ELSE branch


def test_declare_inside_block_is_found():
    tokens, _, _, (i0, i1), _, _ = _parse()
    names = {n for n, _, _ in scan_declares(FIXTURE, tokens, i0, i1)}
    assert names == {"@i", "@total", "@bonus"}        # @bonus lives inside the IF


def test_rewrite_rowcount_and_error_message():
    text, markers = rewrite("SET @q = @@ROWCOUNT;", None, True)
    assert text == "SET @q = ?;" and markers == ["@@ROWCOUNT"]

    text, markers = rewrite("SET @e = CONCAT(N'x: ', ERROR_MESSAGE());", "boom", False)
    assert "?" in text and markers == ["ERROR_MESSAGE"]

    # never rewrite inside a string literal
    text, markers = rewrite("SET @s = N'use @@ROWCOUNT here';", None, True)
    assert markers == [] and "@@ROWCOUNT" in text


def test_while_has_no_else_chain():
    tokens = scan("WHILE @i < 3 BEGIN SET @i = @i + 1; END;")
    end, branches, is_loop = parse_conditional(tokens, 0, len(tokens))
    assert is_loop and len(branches) == 1
