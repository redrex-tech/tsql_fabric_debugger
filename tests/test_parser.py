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
    i0, i1 = procedure_body(FIXTURE, tokens, i_as)
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


# ---------------------------------------------------------------------------
# fixes from the pre-publish four-lens review
# ---------------------------------------------------------------------------
from tsql_fabric_debugger.parser import scan_transaction_controls  # noqa: E402


def test_else_without_semicolon_before_it():
    sql = "IF @n = 1 SET @x = N'one' ELSE SET @x = N'other';"
    tokens = scan(sql)
    end, branches, is_loop = parse_conditional(tokens, 0, len(tokens))
    assert len(branches) == 2

    def span_text(span):
        return " ".join(sql[tokens[span[0]]["s"]:tokens[span[1] - 1]["e"]].split())

    assert span_text(branches[0]["body"]) == "SET @x = N'one'"     # ELSE not swallowed
    assert branches[1]["cond"] is None
    assert span_text(branches[1]["body"]) == "SET @x = N'other';"


def test_copy_into_ends_the_if_condition():
    sql = "IF @full = 1 COPY INTO stg.t FROM 'https://x' WITH (FILE_TYPE = 'CSV');"
    tokens = scan(sql)
    end, branches, _ = parse_conditional(tokens, 0, len(tokens))
    def span_text(span):
        return " ".join(sql[tokens[span[0]]["s"]:tokens[span[1] - 1]["e"]].split())
    assert span_text(branches[0]["cond"]) == "@full = 1"
    assert span_text(branches[0]["body"]).startswith("COPY INTO stg.t")


def test_nested_try_steps_carry_the_full_catch_stack():
    sql = """
    CREATE PROCEDURE p AS
    BEGIN
        BEGIN TRY
            SET @a = 1;
            BEGIN TRY
                SET @b = 1;
            END TRY
            BEGIN CATCH
                SET @b = -1;
            END CATCH
            SET @c = 1;
        END TRY
        BEGIN CATCH
            SET @a = -1;
        END CATCH
    END
    """
    tokens = scan(sql)
    _, _, i_as = parse_params(sql, tokens, find_procedure(tokens))
    i0, i1 = procedure_body(FIXTURE, tokens, i_as)
    ctx = {"catches": []}
    steps = split_steps(sql, tokens, i0, i1, ctx)
    assert [s["text"] for s in steps] == ["SET @a = 1", "SET @b = 1", "SET @c = 1"]
    assert steps[0]["catch_ids"] == (0,)
    assert steps[1]["catch_ids"] == (0, 1)     # inside BOTH trys
    assert steps[2]["catch_ids"] == (0,)
    assert len(ctx["catches"]) == 2


def test_transaction_controls_including_dynamic_sql_and_exec():
    sql = """
    CREATE PROCEDURE p AS
    BEGIN
        SET @sql = N'UPDATE t SET a = 1; COMMIT;';
        EXEC (@sql);
        EXEC dbo.child_proc;
    END
    """
    tokens = scan(sql)
    _, _, i_as = parse_params(sql, tokens, find_procedure(tokens))
    i0, i1 = procedure_body(sql, tokens, i_as)
    controls, exec_lines = scan_transaction_controls(sql, tokens, i0, i1)
    assert any("string literal" in desc for desc, _ in controls)   # COMMIT inside N'...'
    assert len(exec_lines) == 2


def test_read_sql_file_common_encodings(tmp_path):
    from tsql_fabric_debugger.parser import read_sql_file
    text = "SELECT N'ação';"
    for name, encoding in [("u8.sql", "utf-8"), ("u8b.sql", "utf-8-sig"),
                           ("u16.sql", "utf-16"), ("cp.sql", "cp1252")]:
        p = tmp_path / name
        p.write_bytes(text.encode(encoding))
        assert read_sql_file(p) == text


def test_go_as_identifier_does_not_truncate_bare_body():
    from tsql_fabric_debugger.parser import _is_batch_separator
    # `go` as a column alias must NOT end a bare body; a real GO line must
    sql = ("CREATE PROCEDURE p AS\n"
           "SELECT uf FROM dbo.states go WHERE go.uf = @uf;\n"
           "SELECT 2;\n"
           "GO\n"
           "SELECT N'next object';")
    tokens = scan(sql)
    i_as = next(i for i, t in enumerate(tokens) if t.get("u") == "AS") + 1
    i0, i1 = procedure_body(sql, tokens, i_as)
    body = sql[tokens[i0]["s"]:tokens[i1 - 1]["e"]]
    assert "go.uf = @uf" in body and body.endswith("SELECT 2;")
    assert "next object" not in body

    go_tokens = [t for t in tokens if t.get("u") == "GO"]
    assert [_is_batch_separator(sql, t) for t in go_tokens] == [False, False, True]


def test_semicolonless_statements_split_correctly():
    from tsql_fabric_debugger.parser import skip_stmt
    # legacy T-SQL without ';': statements split on the next starter keyword
    sql = "SET @a = 1 SET @b = 2 SELECT 1"
    tokens = scan(sql)
    j1 = skip_stmt(tokens, 0, len(tokens))
    assert sql[tokens[0]["s"]:tokens[j1 - 1]["e"]] == "SET @a = 1"

    # INSERT..SELECT and UPDATE..SET are ONE statement each
    sql = "INSERT INTO t SELECT a FROM x UPDATE t2 SET b = 1"
    tokens = scan(sql)
    j1 = skip_stmt(tokens, 0, len(tokens))
    assert sql[tokens[0]["s"]:tokens[j1 - 1]["e"]] == "INSERT INTO t SELECT a FROM x"
    j2 = skip_stmt(tokens, j1, len(tokens))
    assert sql[tokens[j1]["s"]:tokens[j2 - 1]["e"]] == "UPDATE t2 SET b = 1"

    # a CTE's consumer belongs to the WITH
    sql = "WITH c AS (SELECT 1 AS a) SELECT * FROM c SET @x = 1"
    tokens = scan(sql)
    j1 = skip_stmt(tokens, 0, len(tokens))
    assert sql[tokens[0]["s"]:tokens[j1 - 1]["e"]] == "WITH c AS (SELECT 1 AS a) SELECT * FROM c"

    # MERGE never auto-splits (T-SQL requires its ';')
    sql = ("MERGE t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.a = s.a "
           "WHEN NOT MATCHED THEN INSERT (a) VALUES (s.a); SELECT 1")
    tokens = scan(sql)
    j1 = skip_stmt(tokens, 0, len(tokens))
    assert sql[tokens[0]["s"]:tokens[j1 - 1]["e"]].endswith("VALUES (s.a);")
