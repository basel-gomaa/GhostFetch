"""Who may create an account: open signup, invite-only, or closed.

    ACTOR_PLATFORM_SIGNUP=invite   the default. A new account needs a single-use
                                   invite an administrator created.
    ACTOR_PLATFORM_SIGNUP=open     anyone may sign up (still 5 per address per
                                   hour, api/throttle.py).
    ACTOR_PLATFORM_SIGNUP=closed   nobody may sign up; invites are refused too.

WHY INVITE-ONLY IS THE DEFAULT. The security re-review found that open signup
allows unlimited distinct accounts, at 5 per IP per hour, and email
verification and CAPTCHA were deliberately left out. An account here can run
scrapes that spend the operator's Claude credits and use the operator's IP
address, so "anyone on the internet" is the wrong default for who gets one.

THE FIRST ACCOUNT NEEDS NO INVITE, in every mode except `closed`. An empty users
table is a fresh install (README: "signup has to work"), and with no users there
is no admin to make an invite. The emptiness check runs under an advisory lock,
so two people racing to be first cannot both get in without an invite. Note the
first account is NOT made an admin: promote it as before, with the operator key
(POST /admin/users/{id}/promote), which can also create invites itself.

An invite is a 256-bit random token, shown ONCE to the admin who made it. Only
its sha256 is stored (like an API key), it expires (ACTOR_PLATFORM_INVITE_DAYS,
default 7), and it is spent in the SAME transaction that creates the account:
if the account cannot be created (email taken, say), the invite is not used up,
and two people with the same link cannot both get in.

The mode is read on every request rather than at import, so one process (the
tests) can exercise each mode.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from api.users import (
    AccountError,
    User,
    hash_password,
    insert_user,
    validate_signup,
)
from db.pool import expect_row, pool

log = logging.getLogger("api.invites")

SIGNUP_ENV = "ACTOR_PLATFORM_SIGNUP"
MODES = ("invite", "open", "closed")
DEFAULT_MODE = "invite"

INVITE_DAYS_ENV = "ACTOR_PLATFORM_INVITE_DAYS"
DEFAULT_INVITE_DAYS = 7
MAX_INVITE_DAYS = 30
TOKEN_BYTES = 32

# Serialises signups, so "is this the first account?" and the insert that
# answers it happen as one step. Signups are rare and rate-limited; nothing
# else waits on this lock.
_SIGNUP_LOCK = 0x7369676E  # "sign"

_warned: set[str] = set()


class SignupRefused(AccountError):
    """This signup is not allowed under the current mode."""


def signup_mode(env: dict[str, str] | None = None) -> str:
    """The configured mode. An unrecognised value means `invite`, with a
    warning: a typo must make signup STRICTER, never quietly open it."""
    raw = ((env if env is not None else os.environ).get(SIGNUP_ENV) or "").strip().lower()
    if not raw:
        return DEFAULT_MODE
    if raw in MODES:
        return raw
    if raw not in _warned:
        _warned.add(raw)
        log.warning("%s=%r is not one of %s; using %r", SIGNUP_ENV, raw,
                    "/".join(MODES), DEFAULT_MODE)
    return DEFAULT_MODE


def invite_days(env: dict[str, str] | None = None) -> int:
    raw = ((env if env is not None else os.environ).get(INVITE_DAYS_ENV) or "").strip()
    try:
        days = int(raw) if raw else DEFAULT_INVITE_DAYS
    except ValueError:
        days = DEFAULT_INVITE_DAYS
    return min(max(days, 1), MAX_INVITE_DAYS)


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def has_users() -> bool:
    with pool().connection() as conn:
        return expect_row(conn.execute("SELECT EXISTS (SELECT 1 FROM users) AS e").fetchone(),
                          "SELECT EXISTS")["e"]


def invite_required() -> bool:
    """What the sign-up form needs to know. Reveals only whether an install
    is fresh, and on a fresh install anyone may sign up anyway."""
    mode = signup_mode()
    return mode == "closed" or (mode == "invite" and has_users())


# --- signup -------------------------------------------------------------------

REFUSED_INVITE = "this invite link is not valid: it may have been used, revoked or expired"


def signup(email: str, username: str, password: str, invite: str | None) -> User:
    """Create an account if the mode allows it. Raises SignupRefused, or the
    usual AccountError subclasses (EmailTaken, ...)."""
    email, username = email.strip(), username.strip()
    validate_signup(email, username, password)
    mode = signup_mode()
    token = (invite or "").strip()
    # Before the lock: argon2 is slow on purpose and must not hold it.
    password_hash = hash_password(password)

    with pool().connection() as conn:  # one transaction: commit or rollback
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SIGNUP_LOCK,))
        first = not expect_row(conn.execute("SELECT EXISTS (SELECT 1 FROM users) AS e").fetchone(),
                               "SELECT EXISTS")["e"]

        if mode == "closed":
            raise SignupRefused("sign-up is closed on this platform")
        invite_id = None
        if mode == "invite" and not first:
            if not token:
                raise SignupRefused("sign-up on this platform is by invitation; "
                                    "ask an administrator for an invite link")
            # Spent HERE, inside the transaction. If insert_user raises below,
            # the rollback un-spends it.
            row = conn.execute(
                "UPDATE signup_invites SET used_at = now() "
                "WHERE token_hash = %s AND used_at IS NULL AND revoked_at IS NULL "
                "  AND expires_at > now() RETURNING id",
                (_hash(token),),
            ).fetchone()
            if row is None:
                raise SignupRefused(REFUSED_INVITE)
            invite_id = row["id"]

        user = insert_user(conn, email, username, password_hash)
        if invite_id is not None:
            conn.execute("UPDATE signup_invites SET used_by = %s WHERE id = %s",
                         (user.id, invite_id))
    return user


# --- administration -------------------------------------------------------------


_ONE = ("SELECT i.*, c.username AS created_by_username, u.username AS used_by_username "
        "FROM signup_invites i "
        "LEFT JOIN users c ON c.id = i.created_by "
        "LEFT JOIN users u ON u.id = i.used_by WHERE i.id = %s")


def create_invite(created_by: str | None, note: str | None = None,
                  days: int | None = None) -> tuple[dict[str, Any], str]:
    """-> (the stored row, the token). The token exists only in this return
    value; it cannot be read back later."""
    days = min(max(days, 1), MAX_INVITE_DAYS) if days else invite_days()
    token = secrets.token_urlsafe(TOKEN_BYTES)
    with pool().connection() as conn:
        invite_id = expect_row(conn.execute(
            "INSERT INTO signup_invites (token_hash, note, created_by, expires_at) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (_hash(token), (note or "").strip() or None, created_by,
             _now() + timedelta(days=days)),
        ).fetchone(), "signup_invites RETURNING id")["id"]
        row = conn.execute(_ONE, (invite_id,)).fetchone()
    return _public(expect_row(row, "the invite just inserted")), token


def _status(row: dict[str, Any]) -> str:
    if row["used_at"] is not None:
        return "used"
    if row["revoked_at"] is not None:
        return "revoked"
    if row["expires_at"] <= _now():
        return "expired"
    return "pending"


def _public(row: dict[str, Any]) -> dict[str, Any]:
    """An invite as an admin sees it. Never the token, never its hash."""
    return {
        "id": str(row["id"]),
        "note": row["note"],
        "status": _status(row),
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "used_at": row["used_at"],
        "created_by": row.get("created_by_username"),
        "used_by": row.get("used_by_username"),
    }


def list_invites(limit: int = 100) -> list[dict[str, Any]]:
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT i.*, c.username AS created_by_username, u.username AS used_by_username "
            "FROM signup_invites i "
            "LEFT JOIN users c ON c.id = i.created_by "
            "LEFT JOIN users u ON u.id = i.used_by "
            "ORDER BY i.created_at DESC, i.id LIMIT %s",
            (limit,),
        ).fetchall()
    return [_public(r) for r in rows]


def revoke_invite(invite_id: str) -> dict[str, Any] | None:
    """Revoke an unused invite. -> the invite afterwards, or None if there is
    no such invite. Revoking a used or already-revoked one changes nothing:
    an account made with it is not undone (disable the user for that)."""
    with pool().connection() as conn:
        conn.execute(
            "UPDATE signup_invites SET revoked_at = now() "
            "WHERE id = %s AND used_at IS NULL AND revoked_at IS NULL",
            (invite_id,),
        )
        row = conn.execute(_ONE, (invite_id,)).fetchone()
    return _public(row) if row else None
