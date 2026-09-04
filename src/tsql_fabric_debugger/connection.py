# -*- coding: utf-8 -*-
"""Authenticated connection to the Fabric Warehouse (Entra ID).

Two token chains, resolved automatically:
1. Inside a Fabric notebook: the notebook's own token (notebookutils).
2. Anywhere else (local machine, CI): AzureCliCredential — the same
   `az login` you already use. Requires the `[local]` extra (azure-identity)
   and the ODBC Driver 18 for SQL Server installed.

Server and database can be passed as parameters or through the
FABRIC_TSQL_SERVER and FABRIC_TSQL_DATABASE environment variables.
"""

import os
import struct

TOKEN_SCOPE = "https://database.windows.net/.default"
SERVER_ENV = "FABRIC_TSQL_SERVER"
DATABASE_ENV = "FABRIC_TSQL_DATABASE"


def _get_token():
    try:
        return notebookutils.credentials.getToken(TOKEN_SCOPE)  # noqa: F821
    except NameError:
        from azure.identity import AzureCliCredential
        return AzureCliCredential().get_token(TOKEN_SCOPE).token


def fetch_source(proc_name: str, server: str | None = None,
                 database: str | None = None) -> str:
    """Fetch the deployed source of a procedure straight from the warehouse.

    Opens a short-lived autocommit session, reads OBJECT_DEFINITION and closes.
    A name without schema resolves to dbo. Raises ValueError when the object
    does not exist or the caller lacks VIEW DEFINITION permission.
    """
    conn = connect(server, database, autocommit=True)
    try:
        cur = conn.cursor()
        cur.execute("SELECT OBJECT_DEFINITION(OBJECT_ID(?));", (proc_name,))
        row = cur.fetchone()
        source = row[0] if row else None
    finally:
        conn.close()
    if not source:
        raise ValueError(f"{proc_name}: source not available "
                         "(missing object or no VIEW DEFINITION permission).")
    return source


def connect(server: str | None = None, database: str | None = None,
            autocommit: bool = True, lock_timeout: int | None = None):
    """Open an authenticated (Entra ID) connection. One session = one debug.

    lock_timeout (seconds): when set, a statement that waits for a lock this
    long fails with SQL error 1222 instead of blocking forever. It does NOT
    prevent an orphaned session from holding locks — only the server can reap
    that — but it turns "hangs indefinitely behind an orphan" into an
    immediate, actionable error. Applies to the whole session.
    """
    import pyodbc

    server = server or os.environ.get(SERVER_ENV)
    database = database or os.environ.get(DATABASE_ENV)
    if not server or not database:
        raise ValueError(
            "Provide server and database (or set the "
            f"{SERVER_ENV} and {DATABASE_ENV} environment variables)."
        )

    token = _get_token()
    token_bytes = token.encode("utf-16-le")
    token_struct = struct.pack(f"<I{len(token_bytes)}s", len(token_bytes), token_bytes)
    SQL_COPT_SS_ACCESS_TOKEN = 1256

    # APP= makes the debugger identifiable in sys.dm_exec_sessions.program_name
    # during a blocking investigation; LoginTimeout bounds the connect phase
    # (step_timeout only covers command execution).
    conn = pyodbc.connect(
        f"DRIVER={{ODBC Driver 18 for SQL Server}};"
        f"SERVER={server};DATABASE={database};Encrypt=yes;"
        f"APP=tsql-fabric-debugger;LoginTimeout=30;",
        attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_struct},
        autocommit=autocommit,
    )
    if lock_timeout is not None:
        # a session option: must run outside pyodbc's parameterized path so it
        # persists for the session (sp_prepexec would revert it per batch)
        cur = conn.cursor()
        cur.execute(f"SET LOCK_TIMEOUT {int(lock_timeout) * 1000};")
        cur.close()
    return conn
