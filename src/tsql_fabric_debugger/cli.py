# -*- coding: utf-8 -*-
"""CLI: `tsql-debug file.sql` runs a whole procedure with a step-by-step log.

Examples:
    tsql-debug prd_load.sql --server X.datawarehouse.fabric.microsoft.com \\
        --database my_warehouse --param @year=2015
    tsql-debug load.sql --param @code='00123' --commit --csv log.csv --log-level full

For interactive debugging (step/step_into/show_vars), use the TSQLDebugger
class in a notebook, IPython or `python -i`.
"""

import argparse
import re
import sys

from . import __version__
from .parser import find_procedure, read_sql_file
from .runner import count_errors, run_procedure, run_script, save_log_csv
from .scanner import scan

_INT_RE = re.compile(r"-?(0|[1-9]\d*)")
_FLOAT_RE = re.compile(r"-?(0|[1-9]\d*)\.\d+")


def _parse_param(raw):
    """'@name=value' -> ('@name', value).

    Quoted values ('...' or "...") are always strings — the way to keep
    leading zeros, pass the literal word NULL, or force '1e5' as text.
    Unquoted: NULL -> None; strict int/float (no leading zeros, no
    scientific notation, no nan/inf); everything else stays a string.
    """
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"Invalid parameter: {raw!r} (use @name=value)")
    name, value = raw.split("=", 1)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return name, value[1:-1]
    if value.upper() == "NULL":
        return name, None
    if _INT_RE.fullmatch(value):
        return name, int(value)
    if _FLOAT_RE.fullmatch(value):
        return name, float(value)
    return name, value


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="tsql-debug",
        description="Runs a Fabric Warehouse T-SQL procedure step by step, "
                    "with ROLLBACK at the end by default.",
        epilog="Exit codes: 0 = no step errored; 1 = at least one step recorded an "
               "ERROR (even when the procedure's own CATCH handled it); "
               "2 = usage or file error.",
    )
    ap.add_argument("sql_file", help=".sql file with the CREATE PROCEDURE (or a loose script)")
    ap.add_argument("--server", help="Warehouse SQL endpoint (or env FABRIC_TSQL_SERVER)")
    ap.add_argument("--database", help="Warehouse name (or env FABRIC_TSQL_DATABASE)")
    ap.add_argument("--param", action="append", type=_parse_param, default=[],
                    metavar="@NAME=VALUE",
                    help="test value for a parameter (repeatable); quote the value "
                         "('00123') to force a string")
    ap.add_argument("--commit", action="store_true",
                    help="persist the effects (default: ROLLBACK at the end)")
    ap.add_argument("--csv", metavar="FILE", help="save the execution log as CSV")
    ap.add_argument("--log-level", choices=["simple", "full"], default="simple")
    ap.add_argument("--step-timeout", type=int, metavar="SECONDS",
                    help="per-step query timeout")
    ap.add_argument("--lock-timeout", type=int, metavar="SECONDS",
                    help="fail a lock-blocked step fast instead of hanging")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args(argv)

    try:
        sql_text = read_sql_file(args.sql_file)
    except FileNotFoundError:
        print(f"tsql-debug: file not found: {args.sql_file}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"tsql-debug: cannot read {args.sql_file}: {exc}", file=sys.stderr)
        return 2

    if find_procedure(scan(sql_text)) is not None:
        df = run_procedure(sql_text=sql_text, params=dict(args.param),
                           server=args.server, database=args.database,
                           commit=args.commit, save_csv=args.csv,
                           log_level=args.log_level, step_timeout=args.step_timeout,
                           lock_timeout=args.lock_timeout)
    else:
        print("[WARNING] no CREATE PROCEDURE found — running as a loose script "
              "(--param and --log-level do not apply on this path).")
        df = run_script(sql_text=sql_text, server=args.server, database=args.database,
                        commit=args.commit, lock_timeout=args.lock_timeout)
        if args.csv:
            save_log_csv(df, args.csv)

    return 1 if count_errors(df) else 0


if __name__ == "__main__":
    sys.exit(main())
