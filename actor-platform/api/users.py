"""Accounts, passwords, console sessions and per-user API keys.

The database half of authentication. api/auth.py stays dependency-free and
injectable -- it knows about scopes and Principals and nothing about storage --
so everything that needs a connection lives here and is composed in api/main.py.

THREE CREDENTIALS, THREE TREATMENTS, and the differences are deliberate:

  password    argon2id. Human-chosen, therefore low-entropy and guessable, so
              the hash has to be deliberately slow. Never recoverable: we cannot
              tell a user their password, only let them set a new one.

  API key     sha256 of a 256-bit random secret. NOT argon2 -- there is no
              low-entropy guess to slow down, and a slow hash on every API
              request would be a self-inflicted rate limit. Split into a public
              `key_id` and a secret half so a lookup is one indexed row plus one
              constant-time compare, instead of a walk over every key.

  session     an opaque row id in an HMAC-signed cookie. Not a JWT: the whole
              point is REVOCATION -- logout, admin disable, a password change --
              and a self-contained token cannot be revoked, only outlived.

WE NEVER STORE A SOCIAL-MEDIA PASSWORD. A user's connected account is
represented here only by the session cookies they captured themselves,
encrypted by shared/crypto.py. There is no column for a social-platform
password and there must never be one: it would turn a breach of this database
into a breach of thirty real accounts elsewhere.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from api.auth import (
    ALL_SCOPES,
    SESSION_COOKIE_NAME,
    CredentialsInvalid,
    Principal,
    Scope,
)
from db.pool import expect_row, pool
from shared.crypto import KeyRing

# --- policy -----------------------------------------------------------------

SESSION_TTL = timedelta(days=14)

# Long enough to matter, short enough that people will actually use it. Length
# is the only password rule enforced: composition rules ("one symbol!") push
# people toward predictable substitutions and are not worth the friction.
MIN_PASSWORD_LENGTH = 10

# 2-32 chars. The trailing class is REQUIRED, not optional: making it optional
# let a single character through, which the error message already denied.
USERNAME_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,30}[a-zA-Z0-9]")
# Deliberately permissive. A stricter pattern rejects addresses that are legal
# and deliverable, and the only real proof an address works is sending to it.
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

KEY_PREFIX = "apk_"
KEY_ID_BYTES = 6      # 12 hex chars; the public, indexed handle
KEY_SECRET_BYTES = 32  # 256 bits; why sha256 rather than argon2 is enough

_hasher = PasswordHasher()


class AccountError(RuntimeError):
    """The account operation cannot be performed as asked."""


class EmailTaken(AccountError):
    pass


class UsernameTaken(AccountError):
    pass


@dataclass(frozen=True)
class User:
    id: str
    email: str
    username: str
    is_admin: bool
    disabled: bool
    # What the person chose to be called; None means "use the username".
    # Absent on a row read before migration 009, hence .get().
    display_name: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> User:
        return cls(
            id=str(row["id"]),
            email=row["email"],
            username=row["username"],
            is_admin=bool(row["is_admin"]),
            disabled=row["disabled_at"] is not None,
            display_name=row.get("display_name"),
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- passwords --------------------------------------------------------------


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError, Exception):  # noqa: BLE001
        # Any failure is the same answer. argon2 raises several distinct types
        # and telling them apart in a response would leak whether an account
        # exists.
        return False


def validate_signup(email: str, username: str, password: str) -> None:
    if not EMAIL_RE.fullmatch(email.strip()):
        raise AccountError("that does not look like an email address")
    if not USERNAME_RE.fullmatch(username.strip()):
        raise AccountError(
            "a username is 2-32 characters: letters, digits, dot, dash or underscore, "
            "starting and ending with a letter or digit"
        )
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AccountError(f"a password must be at least {MIN_PASSWORD_LENGTH} characters")


# --- accounts ---------------------------------------------------------------


def create_user(email: str, username: str, password: str, *, is_admin: bool = False) -> User:
    email, username = email.strip(), username.strip()
    validate_signup(email, username, password)

    with pool().connection() as conn:
        return insert_user(conn, email, username, hash_password(password), is_admin=is_admin)


def insert_user(conn, email: str, username: str, password_hash: str, *,
                is_admin: bool = False) -> User:
    """Insert on a connection the caller owns, so the insert can share a
    transaction with something that must roll back with it (api/invites.py
    spends an invite in the same one). Inputs are already validated."""
    # Checked before inserting so the caller gets "that email is taken"
    # rather than a constraint name. The UNIQUE indexes are still what makes
    # it correct under a race; this is only for the message.
    if conn.execute("SELECT 1 FROM users WHERE email = %s", (email,)).fetchone():
        raise EmailTaken("that email address is already registered")
    if conn.execute("SELECT 1 FROM users WHERE username = %s", (username,)).fetchone():
        raise UsernameTaken("that username is taken")
    row = conn.execute(
        "INSERT INTO users (email, username, password_hash, is_admin) "
        "VALUES (%s,%s,%s,%s) RETURNING *",
        (email, username, password_hash, is_admin),
    ).fetchone()
    return User.from_row(row)


def authenticate_password(identifier: str, password: str) -> User | None:
    """Sign in by email or username. None on any failure, without saying which.

    The password is verified even when no such user exists, against a throwaway
    hash. Skipping it would make a missing account return measurably faster than
    a wrong password, which is how an attacker enumerates who has an account.
    """
    identifier = identifier.strip()
    row = None
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = %s OR username = %s", (identifier, identifier)
        ).fetchone()

    if row is None:
        verify_password(
            "$argon2id$v=19$m=65536,t=3,p=4$"
            "c29tZXNhbHRzb21lc2FsdA$RdescudvJCsgt3ub+b+dWRWJTmaaJObG", password
        )
        return None
    if not verify_password(row["password_hash"], password):
        return None
    user = User.from_row(row)
    return None if user.disabled else user


def get_user(user_id: str) -> User | None:
    with pool().connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
    return User.from_row(row) if row else None


# --- administration -----------------------------------------------------------
#
# Promote, demote, disable, enable. The columns always existed; until these
# functions the only way to change them was SQL. Nothing here checks WHO is
# asking -- that is api/admin.py's job -- but the one rule that must hold under
# concurrency, "there is always an enabled admin", lives here, in the same
# transaction as the write it guards.

# Serialises every change that could reduce the number of enabled admins. A
# transaction-scoped advisory lock rather than a table lock: it blocks only other
# admin changes, never sign-ins or runs, and is released by commit or rollback.
_ADMIN_CHANGE_LOCK = 0x61646D6E  # "admn"


class NoSuchUser(AccountError):
    pass


class LastAdmin(AccountError):
    """The change would leave no enabled administrator."""


def list_users(q: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Users, newest first. `q` matches a substring of email or username."""
    sql = ("SELECT id, email, username, display_name, is_admin, disabled_at, created_at "
           "FROM users")
    params: list[Any] = []
    if q:
        sql += " WHERE email ILIKE %s OR username ILIKE %s"
        # Escaped so a '%' or '_' the admin types is a character, not a pattern.
        needle = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params += [needle, needle]
    sql += " ORDER BY created_at DESC, id LIMIT %s"
    params.append(limit)
    with pool().connection() as conn:
        return conn.execute(sql, params).fetchall()


def user_detail(user_id: str) -> dict[str, Any] | None:
    """One user plus what is currently able to act as them."""
    with pool().connection() as conn:
        return conn.execute(
            "SELECT u.id, u.email, u.username, u.display_name, u.is_admin, u.disabled_at, "
            "  u.created_at, "
            "  (SELECT count(*) FROM user_sessions s WHERE s.user_id = u.id "
            "     AND s.revoked_at IS NULL AND s.expires_at > now()) AS live_sessions, "
            "  (SELECT count(*) FROM api_keys k WHERE k.user_id = u.id "
            "     AND k.revoked_at IS NULL) AS api_keys, "
            "  (SELECT count(*) FROM runs r WHERE r.owner_id = u.id) AS runs "
            "FROM users u WHERE u.id = %s",
            (user_id,),
        ).fetchone()


def _set_flag(user_id: str, column: str, target_sql: str, unchanged_sql: str,
              removes_admin: bool) -> tuple[User, bool]:
    """The one write path for both flags. -> (user after, whether it changed).

    `removes_admin` marks a change that could take an ENABLED ADMIN away
    (demoting one, or disabling one). Those take the advisory lock and refuse
    to leave zero enabled admins -- counted inside the same transaction, after
    the lock, so two admins demoting each other at once cannot both succeed.
    """
    with pool().connection() as conn:  # one transaction: commit or rollback
        if removes_admin:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ADMIN_CHANGE_LOCK,))
        row = conn.execute("SELECT * FROM users WHERE id = %s FOR UPDATE",
                           (user_id,)).fetchone()
        if row is None:
            raise NoSuchUser("no such user")
        if expect_row(conn.execute(f"SELECT {unchanged_sql} AS same FROM users WHERE id = %s",
                                   (user_id,)).fetchone(), "users (unchanged check)")["same"]:
            return User.from_row(row), False  # idempotent: already in that state

        target_is_enabled_admin = row["is_admin"] and row["disabled_at"] is None
        if removes_admin and target_is_enabled_admin:
            others = expect_row(conn.execute(
                "SELECT count(*) AS n FROM users "
                "WHERE is_admin AND disabled_at IS NULL AND id <> %s",
                (user_id,),
            ).fetchone(), "SELECT count(*)")["n"]
            if others == 0:
                raise LastAdmin("this is the last enabled administrator; "
                                "promote another user first")

        updated = conn.execute(
            f"UPDATE users SET {column} = {target_sql} WHERE id = %s RETURNING *",
            (user_id,),
        ).fetchone()
    return User.from_row(expect_row(updated, "users (locked above)")), True


def set_admin(user_id: str, value: bool) -> tuple[User, bool]:
    return _set_flag(
        user_id, "is_admin", "TRUE" if value else "FALSE",
        "is_admin" if value else "NOT is_admin",
        removes_admin=not value,
    )


def set_disabled(user_id: str, value: bool) -> tuple[User, bool]:
    """Disable or re-enable. Disabling also REVOKES every console session, so
    re-enabling later does not revive a cookie that was live at the time. That
    runs just after the flag commits, which is safe: from the commit on, every
    session lookup already refuses a disabled account, so there is no window in
    which a revoked-to-be cookie works. API keys are deliberately left alone: they are
    refused while the account is disabled (every key lookup checks it) and work
    again once it is enabled, which is what makes disabling reversible."""
    user, changed = _set_flag(
        user_id, "disabled_at", "now()" if value else "NULL",
        "disabled_at IS NOT NULL" if value else "disabled_at IS NULL",
        removes_admin=value,
    )
    if value:
        # Idempotent, so it runs even on an already-disabled account: a session
        # minted in a race just before the disable is still closed.
        close_all_sessions(user_id)
    return user, changed


# --- console sessions -------------------------------------------------------


class SessionSigner:
    """Signs the session cookie.

    Its key is DERIVED from the platform secret rather than being the platform
    secret, so the value that signs cookies is not the value that decrypts
    stored social sessions. One compromise should not be two.
    """

    INFO = "actor-platform/console-session-cookie/v1"

    def __init__(self, keyring: KeyRing) -> None:
        self._key = keyring.derive(self.INFO)

    def sign(self, session_id: str) -> str:
        mac = hmac.new(self._key, session_id.encode(), hashlib.sha256).hexdigest()[:32]
        return f"{session_id}.{mac}"

    def unsign(self, cookie: str | None) -> str | None:
        """The session id inside a cookie, or None if it was not signed by us."""
        if not cookie or "." not in cookie:
            return None
        session_id, _, mac = cookie.rpartition(".")
        expected = hmac.new(self._key, session_id.encode(), hashlib.sha256).hexdigest()[:32]
        return session_id if hmac.compare_digest(mac, expected) else None


def open_session(user_id: str, *, user_agent: str | None = None) -> str:
    with pool().connection() as conn:
        row = conn.execute(
            "INSERT INTO user_sessions (user_id, expires_at, user_agent) "
            "VALUES (%s,%s,%s) RETURNING id",
            (user_id, _now() + SESSION_TTL, (user_agent or "")[:500] or None),
        ).fetchone()
    return str(expect_row(row, "user_sessions RETURNING id")["id"])


def session_user(session_id: str) -> User | None:
    """The user behind a live session, refreshing last_seen_at.

    Every condition is in the WHERE clause -- not revoked, not expired, not a
    disabled account -- so there is no way to check three things and forget one.
    """
    with pool().connection() as conn:
        row = conn.execute(
            "UPDATE user_sessions s SET last_seen_at = now() "
            "FROM users u WHERE s.id = %s AND s.user_id = u.id "
            "  AND s.revoked_at IS NULL AND s.expires_at > now() "
            "  AND u.disabled_at IS NULL "
            "RETURNING u.*",
            (session_id,),
        ).fetchone()
    return User.from_row(row) if row else None


def close_session(session_id: str) -> bool:
    with pool().connection() as conn:
        return conn.execute(
            "UPDATE user_sessions SET revoked_at = now() "
            "WHERE id = %s AND revoked_at IS NULL",
            (session_id,),
        ).rowcount > 0


def close_all_sessions(user_id: str) -> int:
    """Sign a user out everywhere. What a password change must call."""
    with pool().connection() as conn:
        return conn.execute(
            "UPDATE user_sessions SET revoked_at = now() "
            "WHERE user_id = %s AND revoked_at IS NULL",
            (user_id,),
        ).rowcount


# --- API keys ---------------------------------------------------------------


def _split_key(presented: str) -> tuple[str, str] | None:
    """`apk_<key_id>_<secret>` -> (key_id, secret)."""
    if not presented.startswith(KEY_PREFIX):
        return None
    key_id, _, secret = presented[len(KEY_PREFIX):].partition("_")
    return (key_id, secret) if key_id and secret else None


def mint_api_key(user_id: str, *, scopes: frozenset[Scope] | None = None,
                 label: str | None = None) -> tuple[str, str]:
    """Create a key. Returns (the key to SHOW ONCE, its row id).

    The plaintext is returned and never stored: only sha256(secret) goes to the
    database, so a database dump yields no usable key. This is why the console
    can show a key exactly once and never again.
    """
    # token_HEX, not token_urlsafe: the key is `apk_<key_id>_<secret>` and is
    # split on the first "_", so a key_id containing one would silently split in
    # the wrong place. urlsafe emits "_" about 12% of the time at this length,
    # which made roughly one key in eight fail to authenticate. Hex has no
    # separator character in its alphabet, so the split cannot be ambiguous.
    key_id = secrets.token_hex(KEY_ID_BYTES)
    secret = secrets.token_urlsafe(KEY_SECRET_BYTES)
    granted = sorted(s.value for s in (scopes or ALL_SCOPES))

    with pool().connection() as conn:
        row = conn.execute(
            "INSERT INTO api_keys (key_id, user_id, secret_hash, scopes, label) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (key_id, user_id, hashlib.sha256(secret.encode()).digest(), granted, label),
        ).fetchone()
    return f"{KEY_PREFIX}{key_id}_{secret}", str(expect_row(row, "api_keys RETURNING id")["id"])


def list_api_keys(user_id: str) -> list[dict[str, Any]]:
    with pool().connection() as conn:
        return conn.execute(
            "SELECT id, key_id, scopes, label, created_at, last_used_at "
            "FROM api_keys WHERE user_id = %s AND revoked_at IS NULL "
            "ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()


def revoke_api_key(user_id: str, key_row_id: str) -> bool:
    with pool().connection() as conn:
        return conn.execute(
            "UPDATE api_keys SET revoked_at = now() "
            "WHERE id = %s AND user_id = %s AND revoked_at IS NULL",
            (key_row_id, user_id),
        ).rowcount > 0


class DbAuthPolicy:
    """Authenticates a per-user API key. Satisfies the same shape as AuthPolicy.

    Returns None rather than raising when the credential is not one of ours, so
    it can sit in a chain ahead of the environment-key policy.
    """

    def authenticate_or_none(self, authorization: str | None) -> Principal | None:
        if not authorization:
            return None
        scheme, _, credential = authorization.partition(" ")
        if scheme.strip().lower() != "bearer":
            return None

        parts = _split_key(credential.strip())
        if parts is None:
            return None
        key_id, secret = parts

        with pool().connection() as conn:
            row = conn.execute(
                "SELECT k.id, k.secret_hash, k.scopes, u.id AS user_id, u.is_admin "
                "FROM api_keys k JOIN users u ON u.id = k.user_id "
                "WHERE k.key_id = %s AND k.revoked_at IS NULL AND u.disabled_at IS NULL",
                (key_id,),
            ).fetchone()
            if row is None:
                return None
            # Constant-time, as the environment policy is. The indexed lookup
            # above narrows to one row; this is what actually authenticates.
            if not hmac.compare_digest(
                hashlib.sha256(secret.encode()).digest(), bytes(row["secret_hash"])
            ):
                raise CredentialsInvalid("the API key is not valid")
            conn.execute("UPDATE api_keys SET last_used_at = now() WHERE id = %s", (row["id"],))

        return Principal(
            scopes=frozenset(Scope(s) for s in row["scopes"]),
            user_id=str(row["user_id"]),
            # A signed-up user is NOT an operator, whatever their key's scopes.
            # This is the line that keeps thirty users out of each other's runs.
            is_admin=bool(row["is_admin"]),
        )


# --- the bundle the API wires in -------------------------------------------


class Identity:
    """Everything the API needs to know who a caller is, in one injectable object.

    create_app() takes this or None. None means the platform runs exactly as it
    did before accounts existed -- environment keys only -- which is what keeps
    every pre-existing test passing untouched and lets the single-user
    deployment stay single-user.
    """

    def __init__(self, keyring: KeyRing) -> None:
        self.signer = SessionSigner(keyring)
        self.keys = DbAuthPolicy()
        # Kept so the social router can seal a captured session with the same
        # ring the worker opens it with. One ring per process, passed rather
        # than re-read, so a mid-run key rotation cannot make the two disagree.
        self.keyring = keyring

    def principal_for_cookie(self, cookie: str | None) -> Principal | None:
        session_id = self.signer.unsign(cookie)
        if session_id is None:
            return None
        user = session_user(session_id)
        if user is None:
            return None
        # A console session gets both scopes: it IS the user, driving the app
        # they signed up for. Scopes exist to restrict API KEYS, which are
        # handed to scripts and can be narrowed.
        return Principal(scopes=ALL_SCOPES, user_id=user.id, is_admin=user.is_admin)

    def principal_for_key(self, authorization: str | None) -> Principal | None:
        return self.keys.authenticate_or_none(authorization)


# --- profile ----------------------------------------------------------------
# The editable half of an account: a display name and a photo. Neither one is an
# identity -- sign-in, ownership and every authorization check use the id and
# the username, which editing a profile never touches.

MAX_DISPLAY_NAME_LENGTH = 50
MAX_AVATAR_BYTES = 256 * 1024

# Sniffed from the bytes, never taken from a header. Raster formats only: an SVG
# is a document that can carry script, and it is served from this origin.
_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)


def normalize_display_name(value: str | None) -> str | None:
    """The stored form of a display name, or AccountError.

    Surrounding and repeated whitespace collapses; empty means "unset". Control
    characters and bidirectional overrides are refused: a name is shown beside
    other text, and an override could make it read as something else."""
    if value is None:
        return None
    name = " ".join(value.split())
    if not name:
        return None
    if len(name) > MAX_DISPLAY_NAME_LENGTH:
        raise AccountError(f"a display name is at most {MAX_DISPLAY_NAME_LENGTH} characters")
    for ch in name:
        cat = unicodedata.category(ch)
        if cat == "Cc" or ch in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069":
            raise AccountError("a display name cannot contain control or direction characters")
    return name


def set_display_name(user_id: str, display_name: str | None) -> User:
    name = normalize_display_name(display_name)
    with pool().connection() as conn:
        row = conn.execute(
            "UPDATE users SET display_name = %s WHERE id = %s RETURNING *", (name, user_id),
        ).fetchone()
    if row is None:
        raise NoSuchUser("no such user")
    return User.from_row(row)


def sniff_image_type(data: bytes) -> str | None:
    """'image/png' / 'image/jpeg' / 'image/webp' from the bytes, or None."""
    for signature, content_type in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return content_type
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def set_avatar(user_id: str, data: bytes) -> datetime:
    """Store a photo; returns its version stamp. AccountError if it is not one."""
    if not data:
        raise AccountError("the photo is empty")
    if len(data) > MAX_AVATAR_BYTES:
        raise AccountError(f"the photo is larger than {MAX_AVATAR_BYTES // 1024} KB")
    content_type = sniff_image_type(data)
    if content_type is None:
        raise AccountError("the photo must be a PNG, JPEG or WebP image")
    with pool().connection() as conn:
        row = expect_row(conn.execute(
            "INSERT INTO user_avatars (user_id, image, content_type, updated_at) "
            "VALUES (%s, %s, %s, clock_timestamp()) "
            "ON CONFLICT (user_id) DO UPDATE SET image = EXCLUDED.image, "
            "  content_type = EXCLUDED.content_type, updated_at = EXCLUDED.updated_at "
            "RETURNING updated_at",
            (user_id, data, content_type),
        ).fetchone(), "user_avatars (upsert)")
    return row["updated_at"]


def get_avatar(user_id: str) -> tuple[bytes, str] | None:
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT image, content_type FROM user_avatars WHERE user_id = %s", (user_id,),
        ).fetchone()
    return (bytes(row["image"]), row["content_type"]) if row else None


def avatar_version(user_id: str) -> datetime | None:
    """When the photo last changed (a cache key for the console), or None."""
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT updated_at FROM user_avatars WHERE user_id = %s", (user_id,),
        ).fetchone()
    return row["updated_at"] if row else None


def delete_avatar(user_id: str) -> bool:
    with pool().connection() as conn:
        return conn.execute(
            "DELETE FROM user_avatars WHERE user_id = %s", (user_id,),
        ).rowcount > 0
