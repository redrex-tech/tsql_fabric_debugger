# -*- coding: utf-8 -*-
"""Batch execution: run everything at once, no interaction.

For when you don't need to stop midway — validating a whole procedure with
test parameters (run_procedure, ROLLBACK by default) or executing a loose
script batch by batch (run_script, also ROLLBACK by default).
"""

import re
import time

from .connection import connect
from .engine import TSQLDebugger
from .parser import read_sql_file, skip_stmt
from .scanner import scan

GO_PATTERN = re.compile(r"^\s*GO\s*\d*\s*$", flags=re.IGNORECASE | re.MULTILINE)


def count_errors(log) -> int:
    """Number of ERROR entries in a log (DataFrame or list of dicts)."""
    rows = log.to_dict("records") if hasattr(log, "to_dict") else log
    return sum(1 for r in rows if r.get("status") == "ERROR")


def save_log_csv(log, path: str, echo=print) -> None:
    """Persist a log to CSV — with pandas when available, stdlib csv otherwise.

    utf-8-sig so Excel renders accented NVARCHAR values correctly.
    """
    if hasattr(log, "to_csv"):
        log.to_csv(path, index=False, encoding="utf-8-sig")
    else:
        import csv
        rows = list(log)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            if rows:
                writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
    echo(f"Log saved to: {path}")


def split_script(sql_text: str) -> list:
    """Split a loose script into batches.

    On GO lines when present; otherwise per statement using the library's own
    scanner — a ';' inside a string literal or comment never splits.
    """
    if GO_PATTERN.search(sql_text):
        return [b.strip() for b in GO_PATTERN.split(sql_text) if b.strip()]
    tokens = scan(sql_text)
    parts = []
    i, n = 0, len(tokens)
    while i < n:
        j = skip_stmt(tokens, i, n)
        if j == i:          # defensive: a stray level-0 ELSE must not loop forever
            i += 1
            continue
        segment = sql_text[tokens[i]["s"]:tokens[j - 1]["e"]].strip()
        if segment and segment != ";":
            parts.append(segment)
        i = j
    return parts


def run_procedure(sql_file=None, sql_text=None, params=None, server=None, database=None,
                  commit=False, save_csv=None, **kwargs):
    """Run the whole procedure step by step and return the log. ROLLBACK by default.

    Same engine as TSQLDebugger, without interaction. Use save_csv to persist
    the log (works with or without pandas) and commit=True to keep the
    effects in the Warehouse.
    """
    dbg = TSQLDebugger(sql_file=sql_file, sql_text=sql_text, params=params,
                       server=server, database=database, **kwargs)
    try:
        dbg.run_all()
    finally:
        df = dbg.close(commit=commit)
    if save_csv:
        save_log_csv(df, save_csv)
    return df


def run_script(sql_file=None, sql_text=None, server=None, database=None,
               stop_on_error=True, commit=False, echo=print):
    """Execute a loose script (no CREATE PROCEDURE) batch by batch.

    Runs inside a transaction with ROLLBACK at the end by default — pass
    commit=True to persist. Splits on GO when present, otherwise per
    statement via the library's scanner. No variable preservation across
    batches — for that, the script must be a procedure and go through
    TSQLDebugger.
    """
    if sql_text is None:
        sql_text = read_sql_file(sql_file)
    batches = split_script(sql_text)
    echo(f"{len(batches)} batch(es) found.")

    conn = connect(server, database, autocommit=False)
    cursor = conn.cursor()
    log = []
    try:
        for i, stmt in enumerate(batches, start=1):
            preview = " ".join(stmt[:500].split())
            started = time.time()
            try:
                cursor.execute(stmt)
                # first-statement rowcount only: a multi-statement GO batch
                # reports the first DML's count (limitation of the driver API)
                rows = cursor.rowcount
                while cursor.nextset():
                    pass
                log.append({"step": i, "status": "SUCCESS", "rows_affected": rows,
                            "duration_s": round(time.time() - started, 3),
                            "command": preview, "error": None})
            except Exception as exc:
                log.append({"step": i, "status": "ERROR", "rows_affected": None,
                            "duration_s": round(time.time() - started, 3),
                            "command": preview, "error": str(exc)})
                echo(f"[ERROR] batch {i}/{len(batches)}: {exc}")
                if stop_on_error:
                    break
    finally:
        try:
            conn.commit() if commit else conn.rollback()
            echo("COMMIT executed." if commit else "ROLLBACK executed — nothing persisted.")
        except Exception:
            echo("[TRANSACTION] final rollback/commit FAILED — session likely dead.")
        conn.close()
    try:
        import pandas as pd
        return pd.DataFrame(log)
    except ImportError:
        return log
