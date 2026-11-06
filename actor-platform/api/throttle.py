"""Rate limits and lockout for the public auth routes (security review H-02).

POST /auth/login and /auth/signup are public, and every login attempt runs
argon2 -- deliberately expensive -- so without a limit a handful of clients can
both guess passwords and pin the CPU. Two independent mechanisms:

  PER ADDRESS   a request rate on each route (login 20 / 10 min, signup
                5 / hour). Counts every attempt, successful or not.
  PER ACCOUNT   consecutive failed logins against one identifier. After
                LOCK_AFTER failures it is locked for 15 minutes, doubling with
                each further lockout up to 24 hours. A successful login resets
                it; an admin can clear it from the Users screen.

Both answer 429 with Retry-After and a message that says nothing about whether
the account exists: the per-account counter is keyed by what was TYPED, so an
unknown name locks exactly like a real one.

LOOPBACK IS EXEMPT FROM THE PER-ADDRESS LIMIT, and only from that one. Behind
the documented reverse proxy every request arrives with the client's real
address (uvicorn trusts X-Forwarded-For from 127.0.0.1), so loopback is the
operator's own machine -- and the in-process test client, which signs up
hundreds of accounts per test run. The per-account lockout applies to everyone.

Stored in Postgres (migration 005) so it survives a restart and holds across
API processes. Never fails a request because the throttle itself failed: a
database error here lets the attempt through, since locking everyone out on a
transient error would be its own denial of service.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass

from db.pool import pool

log = logging.getLogger("api.throttle")


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


LOGIN_PER_IP = _int_env("ACTOR_PLATFORM_LOGIN_PER_IP", 20)
LOGIN_WINDOW_S = 10 * 60
SIGNUP_PER_IP = _int_env("ACTOR_PLATFORM_SIGNUPS_PER_IP", 5)
SIGNUP_WINDOW_S = 60 * 60
LOCK_AFTER = _int_env("ACTOR_PLATFORM_LOCK_AFTER_FAILURES", 10)
LOCK_BASE_S = 15 * 60
LOCK_MAX_S = 24 * 60 * 60

EXEMPT_ADDRESSES = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


@dataclass(frozen=True)
class Refusal:
    retry_after: int
    reason: str


def client_address(request) -> str:
    client = getattr(request, "client", None)
    return (client.host if client and client.host else "unknown")[:64]


def _key_for(identifier: str) -> str:
    return "user:" + identifier.strip().lower()[:254]


def check_rate(request, route: str) -> Refusal | None:
    """Count one request from this address on `route`; refuse past the limit."""
    limit, window = ((LOGIN_PER_IP, LOGIN_WINDOW_S) if route == "login"
                     else (SIGNUP_PER_IP, SIGNUP_WINDOW_S))
    address = client_address(request)
    if not limit or address in EXEMPT_ADDRESSES:
        return None
    try:
        with pool().connection() as conn:
            row = conn.execute(
                """
                INSERT INTO auth_throttle (key, hits, window_start) VALUES (%s, 1, now())
                ON CONFLICT (key) DO UPDATE SET
                  hits = CASE WHEN auth_throttle.window_start < now() - make_interval(secs => %s)
                              THEN 1 ELSE auth_throttle.hits + 1 END,
                  window_start = CASE WHEN auth_throttle.window_start < now() - make_interval(secs => %s)
                              THEN now() ELSE auth_throttle.window_start END
                RETURNING hits,
                  extract(epoch FROM auth_throttle.window_start + make_interval(secs => %s) - now())
                    AS remaining
                """,
                (f"ip:{route}:{address}", window, window, window),
            ).fetchone()
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        log.warning("auth throttle unavailable (%s); letting the attempt through", type(exc).__name__)
        return None
    if row and row["hits"] > limit:
        return Refusal(max(1, math.ceil(float(row["remaining"] or 1))),
                       f"too many {route} attempts from this address")
    return None


def check_locked(identifier: str) -> Refusal | None:
    """Is this identifier locked out right now?"""
    try:
        with pool().connection() as conn:
            row = conn.execute(
                "SELECT extract(epoch FROM locked_until - now()) AS remaining FROM auth_throttle "
                "WHERE key = %s AND locked_until > now()",
                (_key_for(identifier),),
            ).fetchone()
    except Exception as exc:  # noqa: BLE001
        log.warning("auth throttle unavailable (%s); letting the attempt through", type(exc).__name__)
        return None
    if row:
        return Refusal(max(1, math.ceil(float(row["remaining"]))),
                       "too many failed sign-in attempts for this account")
    return None


def record_failure(identifier: str) -> None:
    """One more failed login; lock the identifier once LOCK_AFTER is reached."""
    if not LOCK_AFTER:
        return
    try:
        with pool().connection() as conn:
            conn.execute(
                """
                INSERT INTO auth_throttle (key, failures) VALUES (%s, 1)
                ON CONFLICT (key) DO UPDATE SET failures = auth_throttle.failures + 1
                """,
                (_key_for(identifier),),
            )
            conn.execute(
                """
                UPDATE auth_throttle SET
                  locked_until = now() + make_interval(secs => LEAST(%s, %s * power(2, lockouts))),
                  lockouts = lockouts + 1,
                  failures = 0
                WHERE key = %s AND failures >= %s
                """,
                (LOCK_MAX_S, LOCK_BASE_S, _key_for(identifier), LOCK_AFTER),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not record a failed login (%s)", type(exc).__name__)


def record_success(identifier: str) -> None:
    try:
        with pool().connection() as conn:
            conn.execute("DELETE FROM auth_throttle WHERE key = %s", (_key_for(identifier),))
    except Exception:  # noqa: BLE001
        pass


def unlock_user(username: str, email: str) -> int:
    """Clear lockouts for a user (both ways they might have typed it). -> rows cleared."""
    with pool().connection() as conn:
        cur = conn.execute("DELETE FROM auth_throttle WHERE key = ANY(%s)",
                           ([_key_for(username), _key_for(email)],))
        return cur.rowcount or 0


def locked_identifiers(username: str, email: str) -> bool:
    try:
        with pool().connection() as conn:
            row = conn.execute(
                "SELECT 1 AS x FROM auth_throttle WHERE key = ANY(%s) AND locked_until > now()",
                ([_key_for(username), _key_for(email)],),
            ).fetchone()
        return row is not None
    except Exception:  # noqa: BLE001
        return False
