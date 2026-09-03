# -*- coding: utf-8 -*-
"""CLI: `tsql-debug file.sql` runs a whole procedure with a step-by-step log.

Examples:
    tsql-debug prd_load.sql --server X.datawarehouse.fabric.microsoft.com \\
        --database my_warehouse --param @year=2015
    tsql-debug load.sql --param @year=2024 --commit --csv log.csv --log-level full

For interactive debugging (step/step_into/show_vars), use the TSQLDebugger
class in a notebook, IPython or `python -i`.
"""

import argparse
import sys

from . import __version__
from .parser import find_procedure
from .runner import run_procedure, run_script
from .scanner import scan


def _parse_param(raw):
    """'@name=value' -> ('@name', value) with int/float/NULL inferred."""
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"Invalid parameter: {raw!r} (use @name=value)")
    name, value = raw.split("=", 1)
    if value.upper() == "NULL":
        return name, None
    for cast in (int, float):
        try:
            return name, cast(value)
        except ValueError:
            pass
    return name, value


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="tsql-debug",
        description="Runs a Fabric Warehouse T-SQL procedure step by step, "
                    "with ROLLBACK at the end by default.",
    )
    ap.add_argument("sql_file", help=".sql file with the CREATE PROCEDURE (or a loose script)")
    ap.add_argument("--server", help="Warehouse SQL endpoint (or env FABRIC_TSQL_SERVER)")
    ap.add_argument("--database", help="Warehouse name (or env FABRIC_TSQL_DATABASE)")
    ap.add_argument("--param", action="append", type=_parse_param, default=[],
                    metavar="@NAME=VALUE", help="test value for a parameter (repeatable)")
    ap.add_argument("--commit", action="store_true",
                    help="persist the effects (default: ROLLBACK at the end)")
    ap.add_argument("--csv", metavar="FILE", help="save the execution log as CSV")
    ap.add_argument("--log-level", choices=["simple", "full"], default="simple")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args(argv)

    with open(args.sql_file, encoding="utf-8") as f:
        sql_text = f.read()

    if find_procedure(scan(sql_text)) is not None:
        df = run_procedure(sql_text=sql_text, params=dict(args.param),
                           server=args.server, database=args.database,
                           commit=args.commit, save_csv=args.csv,
                           log_level=args.log_level)
    else:
        df = run_script(sql_text=sql_text, server=args.server, database=args.database)
        if args.csv and hasattr(df, "to_csv"):
            df.to_csv(args.csv, index=False)

    errors = (df["status"] == "ERROR").sum() if hasattr(df, "__getitem__") else 0
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
