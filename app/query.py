from __future__ import annotations

import re
import sqlite3
import time

import sqlglot
from sqlglot import exp

from .models import QueryResult


FORBIDDEN_FUNCTIONS = {"load_extension", "readfile", "writefile"}
DENIED_ACTIONS = {
    sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_INDEX, sqlite3.SQLITE_CREATE_TEMP_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER, sqlite3.SQLITE_CREATE_TEMP_VIEW,
    sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_VIEW,
    sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_INDEX, sqlite3.SQLITE_DROP_TEMP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_TRIGGER, sqlite3.SQLITE_DROP_TEMP_VIEW,
    sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_VIEW,
    sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
    sqlite3.SQLITE_PRAGMA, sqlite3.SQLITE_TRANSACTION,
}


def validate_select(conn: sqlite3.Connection, sql: str) -> exp.Expression:
    try:
        statements = sqlglot.parse(sql, read="sqlite")
    except Exception as exc:
        raise ValueError(f"Invalid SQL: {exc}") from exc
    if len(statements) != 1 or statements[0] is None:
        raise ValueError("Exactly one SQL statement is allowed")
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union)) and not tree.find(exp.Select):
        raise ValueError("Only SELECT queries and CTEs are allowed")
    forbidden = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.Command, exp.Transaction, exp.Attach, exp.Pragma)
    if any(tree.find(kind) for kind in forbidden):
        raise ValueError("Only read-only SELECT queries are allowed")
    funcs = {f.sql_name().lower() for f in tree.find_all(exp.Func)}
    if funcs & FORBIDDEN_FUNCTIONS:
        raise ValueError("Disallowed SQL function")
    allowed = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    referenced = {t.name for t in tree.find_all(exp.Table) if t.name not in ctes}
    unknown = referenced - allowed
    if unknown:
        raise ValueError(f"Unknown table(s): {', '.join(sorted(unknown))}")
    return tree


def execute_select(conn: sqlite3.Connection, sql: str, row_limit: int = 200, timeout: float = 1.0) -> QueryResult:
    tree = validate_select(conn, sql)
    started = time.monotonic()
    conn.set_progress_handler(lambda: 1 if time.monotonic()-started > timeout else 0, 1000)
    def authorize(action, arg1, arg2, database, trigger):
        if action in DENIED_ACTIONS:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in FORBIDDEN_FUNCTIONS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    conn.set_authorizer(authorize)
    try:
        plan = [" | ".join(str(x) for x in r) for r in conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()]
        cursor = conn.execute(f"SELECT * FROM ({sql.rstrip().rstrip(';')}) AS guarded_query LIMIT ?",(row_limit+1,))
        rows = cursor.fetchall()
        columns = [x[0] for x in cursor.description or []]
    except sqlite3.OperationalError as exc:
        raise ValueError(f"Query failed: {exc}") from exc
    finally:
        conn.set_progress_handler(None, 0)
        conn.set_authorizer(None)
    tables = sorted({t.name for t in tree.find_all(exp.Table)})
    return QueryResult(columns=columns, rows=[list(r) for r in rows[:row_limit]], plan=plan, tables=tables, join_count=sum(1 for _ in tree.find_all(exp.Join)), truncated=len(rows)>row_limit)
