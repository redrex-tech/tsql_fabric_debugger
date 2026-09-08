# -*- coding: utf-8 -*-
"""Warehouse introspection: list the deployed procedures, so tooling (the VS
Code extension's object explorer) can offer "debug this procedure" without the
user typing a name.

Usable as a library (`list_procedures(...)`) or as a small JSON CLI the
extension spawns:

    python -m tsql_fabric_debugger.introspect procedures --server S --database D

which prints a JSON array of {"schema", "name"} to stdout.
"""

import argparse
import json
import sys

from .connection import connect, kill_orphan_sessions


def list_procedures(server=None, database=None, lock_timeout=30):
    """Deployed stored procedures as [{"schema", "name"}], schema then name.

    Reads INFORMATION_SCHEMA.ROUTINES on a short-lived session (autocommit,
    read-only — nothing is written). A short lock_timeout keeps it from
    hanging behind an orphaned schema lock.
    """
    conn = connect(server, database, autocommit=True, lock_timeout=lock_timeout)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT ROUTINE_SCHEMA, ROUTINE_NAME "
            "FROM INFORMATION_SCHEMA.ROUTINES "
            "WHERE ROUTINE_TYPE = 'PROCEDURE' "
            "ORDER BY ROUTINE_SCHEMA, ROUTINE_NAME;")
        rows = cur.fetchall()
    finally:
        conn.close()
    return [{"schema": r[0], "name": r[1]} for r in rows]


def list_parameters(proc_name, server=None, database=None, lock_timeout=30):
    """Input parameters of a procedure as [{"name", "type", "mode"}] in order.

    proc_name is "schema.proc" (no schema means dbo); [bracketed] identifiers
    are accepted. mode is "IN" for inputs and "INOUT" for OUTPUT parameters
    (INFORMATION_SCHEMA.PARAMETERS) — OUTPUT params are included so tooling can
    skip them when prompting for test values.
    """
    parts = [p.strip().strip("[]") for p in proc_name.split(".")]
    if len(parts) >= 2:
        schema, name = parts[-2], parts[-1]     # ignore an optional db prefix
    else:
        schema, name = "dbo", parts[0]
    conn = connect(server, database, autocommit=True, lock_timeout=lock_timeout)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT PARAMETER_NAME, DATA_TYPE, PARAMETER_MODE "
            "FROM INFORMATION_SCHEMA.PARAMETERS "
            "WHERE SPECIFIC_SCHEMA = ? AND SPECIFIC_NAME = ? "
            "  AND PARAMETER_NAME <> '' "
            "ORDER BY ORDINAL_POSITION;", (schema, name))
        rows = cur.fetchall()
    finally:
        conn.close()
    return [{"name": r[0], "type": r[1], "mode": r[2]} for r in rows]


def steppable_lines(sql_text):
    """Line numbers a breakpoint can actually pause on for this procedure.

    Pure, OFFLINE parse — no server, token or connection: the debugger slices
    the procedure body into steps in memory, and a breakpoint pauses at the
    start line of a statement. Lines without a statement (BEGIN/END, blank,
    comments, a CREATE PROCEDURE header, continuation lines of a multi-line
    statement) are not returned. Tooling (the VS Code extension) uses this to
    show, before debugging, which lines a breakpoint will bind to.

    Statements INSIDE IF/WHILE blocks and CATCH handlers are breakpoint-able
    too (the engine expands blocks and honors inner breakpoints), so this
    recurses through each block's branch bodies and every CATCH body — not just
    the top-level steps. Returns a sorted list of 1-based line numbers.
    """
    from .engine import TSQLDebugger
    from .parser import split_steps
    dbg = TSQLDebugger(sql_text=sql_text, server=None, database=None,
                       echo=lambda *_: None)
    lines = set()

    def walk(steps):
        for step in steps:
            lines.add(step["line"])
            if step["kind"].endswith("_block"):
                for branch in step["branches"]:
                    b0, b1 = branch["body"]
                    # fresh ctx: re-parsing a branch must not mutate dbg state
                    ctx = {"catches": [], "span_ids": {}}
                    walk(split_steps(dbg._sql, dbg._tokens, b0, b1, ctx))
                    for catch in ctx["catches"]:    # a TRY nested in the block
                        walk(catch)

    walk(dbg._steps)
    for catch in dbg._catches:                       # top-level CATCH bodies
        walk(catch)
    return sorted(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="tsql-fabric-introspect",
        description="List warehouse objects as JSON (for tooling).")
    ap.add_argument("what",
                    choices=["procedures", "parameters", "kill-orphans",
                             "steppable-lines", "fetch-source"],
                    help="what to list, the orphan-session janitor, the "
                         "breakpoint-able lines of a .sql (offline), or a "
                         "deployed procedure's source")
    ap.add_argument("--proc", help="schema.proc (required for parameters)")
    ap.add_argument("--file", help="path to a .sql (steppable-lines); "
                                   "omit to read the SQL from stdin")
    ap.add_argument("--server", help="SQL endpoint (or env FABRIC_TSQL_SERVER)")
    ap.add_argument("--database", help="warehouse (or env FABRIC_TSQL_DATABASE)")
    ap.add_argument("--lock-timeout", type=int, default=30, metavar="SECONDS")
    ap.add_argument("--min-idle", type=int, default=900, metavar="SECONDS",
                    help="idle threshold for kill-orphans")
    args = ap.parse_args(argv)
    try:
        if args.what == "kill-orphans":
            killed = kill_orphan_sessions(args.server, args.database,
                                          min_idle_seconds=args.min_idle,
                                          echo=lambda *_: None)
            result = {"killed": killed}
        elif args.what == "steppable-lines":
            sql = (open(args.file, encoding="utf-8", errors="replace").read()
                   if args.file else sys.stdin.read())
            result = {"lines": steppable_lines(sql)}
        elif args.what == "fetch-source":
            if not args.proc:
                raise ValueError("--proc is required for fetch-source")
            from .connection import fetch_source
            result = {"source": fetch_source(args.proc, args.server,
                                             args.database)}
        elif args.what == "parameters":
            if not args.proc:
                raise ValueError("--proc is required for parameters")
            result = list_parameters(args.proc, args.server, args.database,
                                     args.lock_timeout)
        else:
            result = list_procedures(args.server, args.database, args.lock_timeout)
    except Exception as exc:                       # emit a machine-readable error
        json.dump({"error": str(exc)}, sys.stdout)
        return 1
    json.dump(result, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
