# -*- coding: utf-8 -*-
"""Batch execution: run everything at once, no interaction.

For when you don't need to stop midway — validating a whole procedure with
test parameters (run_procedure, ROLLBACK by default) or executing a loose
script batch by batch (run_script).
"""

import re
import time

from .connection import connect
from .engine import TSQLDebugger

GO_PATTERN = re.compile(r"^\s*GO\s*\d*\s*$", flags=re.IGNORECASE | re.MULTILINE)


def run_procedure(sql_file=None, sql_text=None, params=None, server=None, database=None,
                  commit=False, save_csv=None, **kwargs):
    """Run the whole procedure step by step and return the log. ROLLBACK by default.

    Same engine as TSQLDebugger, without interaction. Use save_csv to persist
    the log and commit=True to keep the effects in the Warehouse.
    """
    dbg = TSQLDebugger(sql_file=sql_file, sql_text=sql_text, params=params,
                       server=server, database=database, **kwargs)
    try:
        dbg.run_all()
    finally:
        df = dbg.close(commit=commit)
    if save_csv:
        try:
            df.to_csv(save_csv, index=False)
            print(f"Log saved to: {save_csv}")
        except AttributeError:
            pass
    return df


def run_script(sql_file=None, sql_text=None, server=None, database=None,
               stop_on_error=True, echo=print):
    """Execute a loose script (no CREATE PROCEDURE) batch by batch.

    Splits on GO when present; otherwise per statement (sqlparse, with a ';'
    fallback). No variable preservation across batches — for that, the script
    must be a procedure and go through TSQLDebugger.
    """
    if sql_text is None:
        with open(sql_file, encoding="utf-8") as f:
            sql_text = f.read()
    if GO_PATTERN.search(sql_text):
        batches = [b.strip() for b in GO_PATTERN.split(sql_text) if b.strip()]
    else:
        try:
            import sqlparse
            batches = [b.strip() for b in sqlparse.split(sql_text) if b.strip()]
        except ImportError:
            batches = [b.strip() for b in sql_text.split(";") if b.strip()]
    echo(f"{len(batches)} batch(es) found.")

    conn = connect(server, database, autocommit=True)
    cursor = conn.cursor()
    log = []
    try:
        for i, stmt in enumerate(batches, start=1):
            preview = " ".join(stmt[:500].split())
            started = time.time()
            try:
                cursor.execute(stmt)
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
        conn.close()
    try:
        import pandas as pd
        return pd.DataFrame(log)
    except ImportError:
        return log
