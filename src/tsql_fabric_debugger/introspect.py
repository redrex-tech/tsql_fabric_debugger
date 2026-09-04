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

from .connection import connect


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


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="tsql-fabric-introspect",
        description="List warehouse objects as JSON (for tooling).")
    ap.add_argument("what", choices=["procedures"], help="what to list")
    ap.add_argument("--server", help="SQL endpoint (or env FABRIC_TSQL_SERVER)")
    ap.add_argument("--database", help="warehouse (or env FABRIC_TSQL_DATABASE)")
    ap.add_argument("--lock-timeout", type=int, default=30, metavar="SECONDS")
    args = ap.parse_args(argv)
    try:
        result = list_procedures(args.server, args.database, args.lock_timeout)
    except Exception as exc:                       # emit a machine-readable error
        json.dump({"error": str(exc)}, sys.stdout)
        return 1
    json.dump(result, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
