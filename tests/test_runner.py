# -*- coding: utf-8 -*-
"""Runner tests — script splitting and CSV persistence, no warehouse."""
from tsql_fabric_debugger.runner import save_log_csv, split_script


def test_split_on_go_lines():
    parts = split_script("SELECT 1\nGO\nSELECT 2\ngo 3\nSELECT 3")
    assert parts == ["SELECT 1", "SELECT 2", "SELECT 3"]


def test_split_by_statement_respects_string_literals():
    parts = split_script("SET @x = N'a ; b'; SELECT 1; -- c ; d\nSELECT 2;")
    assert parts == ["SET @x = N'a ; b';", "SELECT 1;", "SELECT 2;"]


def test_split_keeps_blocks_together():
    parts = split_script("BEGIN SET @a = 1; SET @b = 2; END; SELECT 1;")
    assert len(parts) == 2
    assert parts[0].startswith("BEGIN") and parts[0].endswith("END;")


def test_save_csv_without_pandas(tmp_path):
    log = [{"step": 1, "status": "SUCCESS", "error": None},
           {"step": 2, "status": "ERROR", "error": "boom; caiu"}]
    path = tmp_path / "log.csv"
    save_log_csv(log, str(path), echo=lambda *_: None)
    content = path.read_text(encoding="utf-8-sig")
    assert content.splitlines()[0] == "step,status,error"
    assert "boom; caiu" in content


def test_split_glues_blockless_if_else_chain():
    # the ELSE must never become its own (unconditionally executed) batch
    parts = split_script("IF @a = 1 SET @x = 1 ELSE IF @a = 2 SET @x = 2 "
                         "ELSE SET @x = 3; SELECT 1;")
    assert len(parts) == 2
    assert parts[0].count("ELSE") == 2 and parts[0].endswith("SET @x = 3;")
    assert parts[1] == "SELECT 1;"
