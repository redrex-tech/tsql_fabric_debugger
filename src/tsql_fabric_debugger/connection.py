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


ACCESS_TOKEN_ENV = "FABRIC_TSQL_ACCESS_TOKEN"


def _get_token():
    # A caller (e.g. the VS Code extension) can hand us a database access token
    # via the environment so each short-lived process does not pay the Azure
    # CLI cold start again — the biggest part of "connect" latency.
    env_token = os.environ.get(ACCESS_TOKEN_ENV)
    if env_token:
        return env_token
    try:
        return notebookutils.credentials.getToken(TOKEN_SCOPE)  # noqa: F821
    except NameError:
        from azure.identity import AzureCliCredential
        return AzureCliCredential().get_token(TOKEN_SCOPE).token


def kill_orphan_sessions(server: str | None = None, database: str | None = None,
                         min_idle_seconds: int = 900, echo=print) -> list:
    """KILL leftover debugger sessions so their locks stop blocking everyone.

    A debugger process that dies without close() (SIGKILL, crashed kernel,
    killed test run) leaves its warehouse session sleeping with the debug
    transaction OPEN — schema locks included, which blocks OBJECT_DEFINITION
    and DDL for every other session until the server notices the dead TCP
    connection (minutes). This finds sessions tagged by this library
    (program_name 'tsql-fabric-debugger'), sleeping with an open transaction
    and idle for at least min_idle_seconds, and KILLs them: the server rolls
    their transaction back, releasing the locks.

    CAUTION: an interactive debug someone left paused looks exactly like an
    orphan. The default threshold (15 minutes) is a compromise — raise it on
    shared warehouses, or only run this when you know no one is mid-debug.
    Returns the killed session ids.
    """
    conn = connect(server, database, autocommit=True)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT session_id FROM sys.dm_exec_sessions "
            "WHERE program_name = 'tsql-fabric-debugger' "
            "  AND session_id <> @@SPID AND status = 'sleeping' "
            "  AND open_transaction_count > 0 "
            "  AND DATEDIFF(second, COALESCE(last_request_end_time, login_time), "
            "               SYSUTCDATETIME()) >= ?;", (int(min_idle_seconds),))
        orphans = [r[0] for r in cur.fetchall()]
        killed = []
        for sid in orphans:
            try:
                cur.execute(f"KILL {int(sid)};")
                killed.append(sid)
                echo(f"orphan debugger session {sid} killed — its transaction "
                     "was rolled back by the server.")
            except Exception as exc:
                echo(f"session {sid} not killed: {exc}")
        if not orphans:
            echo("no orphan debugger sessions found.")
        return killed
    finally:
        conn.close()


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
