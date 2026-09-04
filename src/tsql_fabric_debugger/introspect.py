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


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="tsql-fabric-introspect",
        description="List warehouse objects as JSON (for tooling).")
    ap.add_argument("what", choices=["procedures", "parameters", "kill-orphans"],
                    help="what to list, or the orphan-session janitor")
    ap.add_argument("--proc", help="schema.proc (required for parameters)")
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
