"""The connection pool, and where the connection string comes from.

One pool per process, created lazily. The API and each Worker are separate
processes and each gets its own; nothing is shared across a fork.

CONNECTING IS NOT REQUIRED AT IMPORT TIME. `pool()` opens on first use and the
platform starts whether or not the database is up, because "the database is
down" must surface as a 503 from a running API that can say so -- not as an
import error that stops the process before it can answer anything at all.
"""

from __future__ import annotations

import os
import threading
from typing import Any

import atexit

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

DSN_ENV = "ACTOR_PLATFORM_DATABASE_URL"

# Matches docker-compose.yml, including the 5433 host port chosen because this
# machine already runs an unrelated postgres on 5432.
DEFAULT_DSN = "postgresql://actorplatform:actorplatform@127.0.0.1:5433/actorplatform"

# Every connection returns rows as dicts. Saying so in the TYPE, not only in
# the row_factory kwarg, is what lets a type checker see `row["id"]` as valid
# (psycopg's documented pattern: connection_class=Connection[DictRow]).
DictPool = ConnectionPool[Connection[DictRow]]

_POOL: DictPool | None = None
_LOCK = threading.Lock()


class DatabaseUnavailable(RuntimeError):
    """The database could not be reached.

    Kept distinct from a query error on purpose: this one means "try again
    later" and must become a 503, never a fall-through to unauthenticated
    access. See api/auth.py for why failing closed here is load-bearing.
    """


def dsn(environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    return (env.get(DSN_ENV) or "").strip() or DEFAULT_DSN


def pool() -> DictPool:
    """The process-wide pool, opened on first use.

    `open=False` then `open(wait=...)` rather than opening in the constructor:
    psycopg warns about implicit opening, and being explicit keeps "the pool
    exists" and "the database answered" as two separate facts.
    """
    global _POOL
    if _POOL is not None:
        return _POOL
    with _LOCK:
        if _POOL is None:
            _POOL = ConnectionPool(
                dsn(),
                connection_class=Connection[DictRow],
                min_size=1,
                max_size=int(os.environ.get("ACTOR_PLATFORM_DB_POOL_MAX", "8")),
                # FAIL, do not hang. Without an explicit connect_timeout psycopg
                # waits indefinitely on an unreachable server, so a stopped
                # database turns every CLI tool and test into a silent hang with
                # no output at all -- which is exactly how it presented the first
                # time it happened.
                kwargs={"row_factory": dict_row, "connect_timeout": 5},
                timeout=10,
                open=False,
            )
            _POOL.open()
            # Without this the pool's background workers outlive the main
            # thread and psycopg prints "couldn't stop thread ... within 5.0
            # seconds" four times on the way out of every short-lived CLI.
            atexit.register(close_pool)
    return _POOL


def close_pool() -> None:
    """Release the pool. Safe to call more than once; used by tests."""
    global _POOL
    with _LOCK:
        if _POOL is not None:
            _POOL.close()
            _POOL = None


def healthy(timeout: float = 2.0) -> bool:
    """Can we reach the database right now? Never raises.

    /health calls this, so it must answer even when everything is broken.
    """
    try:
        with pool().connection(timeout=timeout) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001 - any failure is the same answer here
        return False


def expect_row(row: dict[str, Any] | None, what: str) -> dict[str, Any]:
    """The row a query is GUARANTEED to return (RETURNING, SELECT EXISTS, a
    row this transaction just locked), as a dict rather than dict | None.

    fetchone() is typed Optional because a query in general can match
    nothing; at these call sites it cannot, and indexing None would fail
    anyway. This fails the same way, but says which row was missing.
    """
    if row is None:
        raise LookupError(f"expected a row and got none: {what}")
    return row


def query(sql: str, params: Any = None) -> list[dict[str, Any]]:
    """Run a read and return all rows as dicts. A convenience, not a layer."""
    with pool().connection() as conn:
        return conn.execute(sql, params).fetchall()


def one(sql: str, params: Any = None) -> dict[str, Any] | None:
    """Run a read expected to match at most one row."""
    with pool().connection() as conn:
        return conn.execute(sql, params).fetchone()


def execute(sql: str, params: Any = None) -> int:
    """Run a write and return the affected row count."""
    with pool().connection() as conn:
        return conn.execute(sql, params).rowcount
