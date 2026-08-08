"""
Lakebase (Databricks-managed Postgres) connection helper.

Used only for best-effort logging of tool calls (see _log_query in
weather_mcp_server.py) so the dashboard app has recent agent
queries/predictions to display - none of the weather tools themselves
depend on Lakebase, so the MCP server works with zero Lakebase setup too.

Same pattern as prior Day 2/3 homeworks: connects using a single
LAKEBASE_URL (a standard Postgres connection URL, e.g.
postgresql://role:password@host:5432/databricks_postgres?sslmode=require)
pointing at a native Postgres role with a static, non-expiring password.
"""

import base64
import os
from contextlib import contextmanager

import psycopg2
from databricks.sdk import WorkspaceClient
from psycopg2.extras import RealDictCursor
from sqlalchemy import create_engine

_SCOPE = os.environ.get("LAKEBASE_SECRET_SCOPE", "database")
_KEY = os.environ.get("LAKEBASE_SECRET_KEY", "lakebase-url")


def _lakebase_url() -> str:
    """Fetch and decode the Lakebase connection URL. Falls back to a plain
    LAKEBASE_URL env var (set by .env for local dev) if no Databricks
    secret scope is reachable."""
    env_url = os.environ.get("LAKEBASE_URL")
    if env_url:
        return env_url
    w = WorkspaceClient()
    secret = w.secrets.get_secret(scope=_SCOPE, key=_KEY)
    return base64.b64decode(secret.value).decode("utf-8")


@contextmanager
def get_connection():
    """Yield a raw psycopg2 connection with a RealDictCursor factory."""
    conn = psycopg2.connect(_lakebase_url(), cursor_factory=RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()


def get_engine():
    """Return a SQLAlchemy engine for Lakebase."""
    return create_engine(_lakebase_url())


def run_query(sql: str, params: tuple | dict | None = None) -> list[dict]:
    """Run a read query against Lakebase and return rows as list[dict]."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def run_write(sql: str, params: tuple | dict | None = None) -> int:
    """Run an INSERT/UPDATE/DELETE against Lakebase, return affected row count."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            return cur.rowcount


def ensure_weather_query_log_table() -> None:
    """Create the weather_query_log table (and its index) if they don't exist yet."""
    run_write(
        """
        CREATE TABLE IF NOT EXISTS weather_query_log (
            id SERIAL PRIMARY KEY,
            user_email VARCHAR(255),
            tool_name VARCHAR(50) NOT NULL,
            location VARCHAR(255),
            query_date DATE,
            result_summary TEXT,
            status VARCHAR(20) NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
        """
    )
    run_write(
        "CREATE INDEX IF NOT EXISTS idx_weather_query_log_created_at "
        "ON weather_query_log(created_at DESC)"
    )
