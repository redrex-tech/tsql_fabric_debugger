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
from .parser import parse_conditional, read_sql_file, skip_stmt
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
        t = tokens[i]
        if t["k"] == "w" and t["u"] in ("IF", "WHILE"):
            # a whole IF/ELSE chain (or WHILE) is ONE batch — splitting it
            # would run the ELSE branch unconditionally
            j, _, _ = parse_conditional(tokens, i, n)
        else:
            j = skip_stmt(tokens, i, n)
        if j == i:          # defensive: a stray level-0 token must not loop forever
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


def diff_logs(log_a, log_b):
    """Compare two execution logs (before/after a fix, @year=2015 vs 2016...).

    Entries are aligned by (line, kind) sequence; the result lists only the
    divergences: steps present on one side only, and aligned steps whose
    status, rows_affected or changed_vars differ. Returns a DataFrame (or a
    list of dicts without pandas).
    """
    import difflib

    def records(log):
        return log.to_dict("records") if hasattr(log, "to_dict") else list(log)

    ra, rb = records(log_a), records(log_b)
    ka = [(r.get("line"), r.get("kind")) for r in ra]
    kb = [(r.get("line"), r.get("kind")) for r in rb]
    out = []

    def row(change, a=None, b=None):
        src = a or b
        out.append({
            "change": change,
            "line": src.get("line"),
            "kind": src.get("kind"),
            "command": src.get("command"),
            "status_a": a.get("status") if a else None,
            "status_b": b.get("status") if b else None,
            "rows_a": a.get("rows_affected") if a else None,
            "rows_b": b.get("rows_affected") if b else None,
            "changed_vars_a": a.get("changed_vars") if a else None,
            "changed_vars_b": b.get("changed_vars") if b else None,
            "duration_a": a.get("duration_s") if a else None,
            "duration_b": b.get("duration_s") if b else None,
            "error_a": a.get("error") if a else None,
            "error_b": b.get("error") if b else None,
        })

    matcher = difflib.SequenceMatcher(None, ka, kb, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for a, b in zip(ra[i1:i2], rb[j1:j2]):
                if (a.get("status") != b.get("status")
                        or a.get("rows_affected") != b.get("rows_affected")
                        or a.get("changed_vars") != b.get("changed_vars")):
                    row("diverged", a, b)
        else:
            for a in ra[i1:i2]:
                row("only_in_a", a=a)
            for b in rb[j1:j2]:
                row("only_in_b", b=b)
    try:
        import pandas as pd
        return pd.DataFrame(out)
    except ImportError:
        return out
