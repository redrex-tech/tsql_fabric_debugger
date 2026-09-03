# -*- coding: utf-8 -*-
"""CLI tests — pure paths, no warehouse (would have caught the exit-code CRITICAL)."""
import pytest

from tsql_fabric_debugger.cli import _parse_param, main
from tsql_fabric_debugger.runner import count_errors


@pytest.mark.parametrize("raw, expected", [
    ("@year=2015", ("@year", 2015)),
    ("@rate=0.5", ("@rate", 0.5)),
    ("@n=-7", ("@n", -7)),
    ("@x=NULL", ("@x", None)),
    ("@x=null", ("@x", None)),
    ("@code=00123", ("@code", "00123")),          # leading zeros survive
    ("@lot=1e5", ("@lot", "1e5")),                # no scientific-notation floats
    ("@w=nan", ("@w", "nan")),                    # nan/inf never become floats
    ("@w=inf", ("@w", "inf")),
    ("@name=Ana Maria", ("@name", "Ana Maria")),
    ("@q='007'", ("@q", "007")),                  # quotes force string
    ('@q="NULL"', ("@q", "NULL")),                # quoted NULL is the string
    ("@empty=''", ("@empty", "")),
])
def test_parse_param(raw, expected):
    assert _parse_param(raw) == expected


def test_parse_param_without_equals_fails():
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_param("@nope")


def test_count_errors_works_without_pandas_shape():
    # the base install returns a LIST of dicts — this is the CRITICAL path
    log = [{"status": "SUCCESS"}, {"status": "ERROR"}, {"status": "ERROR"}]
    assert count_errors(log) == 2
    assert count_errors([]) == 0


def test_count_errors_with_dataframe():
    pd = pytest.importorskip("pandas")
    df = pd.DataFrame([{"status": "SUCCESS"}, {"status": "ERROR"}])
    assert count_errors(df) == 1


def test_main_missing_file_returns_2(capsys):
    rc = main(["/nonexistent/proc.sql"])
    assert rc == 2
    assert "file not found" in capsys.readouterr().err
