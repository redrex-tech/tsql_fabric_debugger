# -*- coding: utf-8 -*-
"""tsql-fabric-debugger — step-by-step debugger for T-SQL procedures on Fabric Warehouse.

The Fabric Warehouse has no T-SQL debugger: no breakpoints, no watch, no way
to run half a procedure. This library solves that without touching the .sql:
it parses the procedure in memory, slices the body into steps and preserves
variable state between them (DECLARE + re-injection + statement + capture
batch). Everything runs inside a transaction with ROLLBACK by default.

Quick start:

    from tsql_fabric_debugger import TSQLDebugger
    dbg = TSQLDebugger("prd_load.sql", params={"@year": 2015},
                       server="<endpoint>.datawarehouse.fabric.microsoft.com",
                       database="my_warehouse")
    dbg.list_steps(); dbg.step(); dbg.step_into(); dbg.show_vars()
    dbg.close()   # ROLLBACK

Batch mode: run_procedure(...). Scripts without CREATE PROCEDURE: run_script(...).
"""

from .connection import connect, fetch_source
from .engine import TSQLDebugger
from .runner import diff_logs, run_procedure, run_script, summarize

__version__ = "0.3.0"
__all__ = ["TSQLDebugger", "connect", "fetch_source", "diff_logs", "run_procedure", "run_script", "summarize", "__version__"]
