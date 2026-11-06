"""Connecting a social account (DEMO BUILD: no real login happens).

    GET    /social/accounts              what this user has connected
    GET    /social/platforms             the platform picker
    DELETE /social/accounts/{platform}   disconnect (and forget the session)
    POST   /social/login                 start a (demo) login
    GET    /social/login/{id}            is the user signed in yet?
    POST   /social/login/{id}/capture    "I've finished logging in"
    DELETE /social/login/{id}            close it

In the real build, POST /social/login started a per-user browser container
that the user drove through a noVNC viewer, and /capture took the session
cookies it produced. The public GhostFetch demo ships neither the login
browser nor the VNC proxy. A login here is an in-memory record that is
"signed in" at once, and /capture stores a PLACEHOLDER session (each required
cookie name with the value "demo") through the same encrypt-and-store path a
real session took, so the rest of the platform - run gating, secret
injection, disconnect - behaves exactly as it did.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from api.auth import Principal
from db.pool import expect_row, pool
from shared.crypto import KeyRing, session_aad
from shared.platform_policy import PolicyError, listed_platforms, policy_for
from shared.models import is_canonical_uuid
from shared.social_session import account_id, cookies_of, describe, is_logged_in

# Platform names are config keys: short, lowercase, no punctuation beyond '_'.
PLATFORM_PATTERN = r"^[a-z0-9_]{1,32}$"

logger = logging.getLogger(__name__)

# How long an unfinished demo login lingers before it is forgotten.
DEMO_LOGIN_TTL = timedelta(minutes=15)


class SocialAccount(BaseModel):
    platform: str
    status: str
    external_id: str | None = None
    connected_at: object = None
    last_verified_at: object = None
    expired_reason: str | None = None
    needs_reconnect: bool = False


class LoginStarted(BaseModel):
    id: str
    platform: str
    expires_at: object
    # Always true in this build: there is no browser to drive.
    demo: bool = True


class PlatformOption(BaseModel):
    """One row of the platform picker."""

    name: str
    connectable: bool
    note: str | None = None
    # How to show it: a label and a logo ({d, color}), from config. Path data
    # only, never markup -- see shared/presentation.py.
    label: str | None = None
    icon: dict[str, Any] | None = None


class StartLogin(BaseModel):
    """Defined at MODULE level, and it has to be.

    This file uses `from __future__ import annotations`, so every annotation is
    a string that FastAPI resolves against the module's globals. A model
    declared inside build_router() is not there, so FastAPI cannot tell it is a
    body model and silently reads it as a query parameter -- which surfaces as a
    422 "field required: query.body" for a request that was perfectly correct.
    """

    model_config = ConfigDict(extra="forbid")

    # A platform name as config/actors.json spells them. policy_for() is still
    # what decides whether it is one; this only stops anything else reaching it.
    platform: str = Field(pattern=PLATFORM_PATTERN)


class LoginStatus(BaseModel):
    id: str
    status: str
    ready: bool
    signed_in: bool
    cookie_names: list[str] = []
    expires_at: object = None


def build_router(keyring: KeyRing, current_user, current_operator=None) -> APIRouter:
    """`current_user` guards the reads; `current_operator` (the `run` scope)
    guards every route that changes something -- deleting a stored session,
    starting a login browser, storing a new session. Defaults to the read guard
    only so an older caller keeps working, never to something looser."""
    from api.main import PlatformError, require_same_origin

    current_operator = current_operator if current_operator is not None else current_user

    router = APIRouter(tags=["social"])

    def _account_holder(principal: Principal) -> str:
        """The user this request acts for, or a refusal. An operator key belongs
        to the deployment, not a person, so it has no accounts to connect."""
        if not principal.user_id:
            raise PlatformError(403, "requires a user account",
                                "social accounts belong to a signed-in user, not an operator key")
        return principal.user_id

    def _owned(session_id: str, principal: Principal) -> DemoLogin:
        # 404 for someone else's session, exactly as for one that does not
        # exist. A 403 would confirm it exists.
        session = (_LOGINS.get(session_id, principal.user_id)
                   if principal.user_id and _is_uuid(session_id) else None)
        if session is None:
            raise PlatformError(404, "no such login session",
                                f"no login session {session_id} on this account")
        return session

    # --- accounts -----------------------------------------------------------

    @router.get("/social/accounts", response_model=list[SocialAccount])
    def list_accounts(principal: Principal = current_user) -> list[SocialAccount]:
        with pool().connection() as conn:
            rows = conn.execute(
                "SELECT platform, status, external_id, connected_at, last_verified_at, "
                "expired_reason FROM social_accounts WHERE user_id = %s ORDER BY platform",
                (principal.user_id,),
            ).fetchall()
        return [
            SocialAccount(
                platform=r["platform"], status=r["status"], external_id=r["external_id"],
                connected_at=r["connected_at"], last_verified_at=r["last_verified_at"],
                expired_reason=r["expired_reason"],
                needs_reconnect=r["status"] != "connected",
            )
            for r in rows
        ]

    @router.get("/social/platforms", response_model=list[PlatformOption])
    def list_platforms() -> list[PlatformOption]:
        """Every platform the picker should show. Public shape, not user data.

        Wider than "platforms that can be connected", and objects rather than
        names, because a site that serves its public data anonymously has no
        session to capture: omitting it reads as "not supported", while showing
        it with an enabled Connect button would be a lie. It is listed with
        connectable=false and its own explanation instead.
        """
        return [
            PlatformOption(name=p.name, connectable=p.connectable, note=p.note,
                           label=p.label, icon=p.icon.public() if p.icon else None)
            for p in listed_platforms()
        ]

    @router.delete("/social/accounts/{platform}", status_code=204)
    def disconnect(platform: str, request: Request, principal: Principal = current_operator):
        require_same_origin(request)
        if not re.fullmatch(PLATFORM_PATTERN, platform):
            # Not a name config could hold, so not one this user has connected:
            # the same 404 as an unconnected platform, without reaching the
            # database (where a NUL in the text parameter was a 500).
            raise PlatformError(404, "not connected", "no such account on this profile")
        with pool().connection() as conn:
            # DELETED, not marked revoked: the row's whole content is a live
            # login, and "disconnect" has to mean the ciphertext is gone. Runs
            # that used it keep their history -- the reference is ON DELETE SET
            # NULL rather than cascading.
            gone = conn.execute(
                "DELETE FROM social_accounts WHERE user_id = %s AND platform = %s RETURNING id",
                (principal.user_id, platform),
            ).fetchall()
        if not gone:
            raise PlatformError(404, "not connected", f"no {platform} account on this profile")
        # AND THE BROWSER PROFILE KEPT FOR IT (security review M-04): an Actor
        # with persistent state holds a logged-in profile per account on disk,
        # and "disconnect" must mean that is gone too.
        from shared.state_dirs import remove_account_state  # noqa: PLC0415

        for row in gone:
            remove_account_state(str(row["id"]))
        from fastapi import Response

        return Response(status_code=204)

    # --- the (demo) login --------------------------------------------------

    @router.post("/social/login", response_model=LoginStarted, status_code=201)
    def start_login(body: StartLogin, request: Request,
                    principal: Principal = current_operator) -> LoginStarted:
        require_same_origin(request)
        user_id = _account_holder(principal)
        try:
            policy = policy_for(body.platform)
        except PolicyError as exc:
            raise PlatformError(400, "unknown platform", str(exc)) from exc
        if not policy.connectable:
            raise PlatformError(400, "not connectable",
                                f"{body.platform} needs no account to collect from")
        session = _LOGINS.start(user_id, body.platform)
        return LoginStarted(id=session.id, platform=session.platform,
                            expires_at=session.expires_at)

    @router.get("/social/login/{session_id}", response_model=LoginStatus)
    def login_status(session_id: str, principal: Principal = current_user) -> LoginStatus:
        session = _owned(session_id, principal)
        required = list(policy_for(session.platform).session_cookies)
        # Signed in at once: there is no browser and nothing to wait for.
        return LoginStatus(id=session.id, status="ready", ready=True, signed_in=True,
                           cookie_names=required, expires_at=session.expires_at)

    @router.post("/social/login/{session_id}/capture", response_model=SocialAccount)
    def capture(session_id: str, request: Request,
                principal: Principal = current_operator) -> SocialAccount:
        """Store a placeholder session for this platform (demo build)."""
        require_same_origin(request)
        session = _owned(session_id, principal)

        policy = policy_for(session.platform)
        state = demo_storage_state(policy.session_cookies, policy.session_id_cookie)
        jar = cookies_of(state)

        # The same rule a real capture had to pass. A placeholder that failed it
        # would be a bug in this file, not a user error.
        if not is_logged_in(jar, policy.session_cookies):
            raise PlatformError(
                400, "not signed in yet",
                "that does not look like a finished login yet -- check for a "
                "two-factor or security prompt in the browser, then try again",
                cookie_count=len(jar),
            )

        import json

        document = json.dumps(state)
        external = account_id(jar, policy.session_id_cookie)

        with pool().connection() as conn:
            row = conn.execute(
                "INSERT INTO social_accounts (user_id, platform, external_id, "
                "  session_ciphertext, session_nonce, key_version, status, last_verified_at) "
                "VALUES (%s,%s,%s,'\\x00','\\x00',1,'connected',now()) "
                "ON CONFLICT (user_id, platform) DO UPDATE SET "
                "  external_id = EXCLUDED.external_id, status='connected', "
                "  connected_at = now(), last_verified_at = now(), expired_at = NULL, "
                "  expired_reason = NULL, consecutive_auth_failures = 0 "
                "RETURNING id",
                (principal.user_id, session.platform, external),
            ).fetchone()
            account = str(expect_row(row, "social_accounts RETURNING id")["id"])
            # Sealed only once the row id exists: the AAD binds the ciphertext
            # to THAT row, so it cannot be moved to another one.
            ciphertext, nonce, version = keyring.seal(
                document, session_aad(account, session.platform)
            )
            conn.execute(
                "UPDATE social_accounts SET session_ciphertext=%s, session_nonce=%s, "
                "key_version=%s WHERE id=%s",
                (ciphertext, nonce, version, account),
            )

        # The COUNT and the account number, never the contents.
        logger.info("user %s connected %s: %s", principal.user_id, session.platform,
                    describe(state, policy.session_cookies, policy.session_id_cookie))
        _LOGINS.forget(session.id)

        return SocialAccount(platform=session.platform, status="connected",
                             external_id=external, needs_reconnect=False)

    @router.delete("/social/login/{session_id}", status_code=204)
    def close_login(session_id: str, request: Request, principal: Principal = current_operator):
        require_same_origin(request)
        session = _owned(session_id, principal)
        _LOGINS.forget(session.id)
        from fastapi import Response

        return Response(status_code=204)

    return router


def _is_uuid(value: str) -> bool:
    # The hyphenated form only. uuid.UUID() also takes "urn:uuid:...", braces
    # and bare hex, and such a string reached the uuid cast and failed as a 500.
    return is_canonical_uuid(value)


# --- demo login sessions -----------------------------------------------------


@dataclass
class DemoLogin:
    id: str
    user_id: str
    platform: str
    expires_at: datetime


class DemoLogins:
    """Unfinished demo logins, in memory. Nothing secret is held here."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, DemoLogin] = {}

    def _expire(self) -> None:
        now = datetime.now(timezone.utc)
        for key in [k for k, s in self._sessions.items() if s.expires_at < now]:
            del self._sessions[key]

    def start(self, user_id: str, platform: str) -> DemoLogin:
        session = DemoLogin(str(uuid.uuid4()), user_id, platform,
                            datetime.now(timezone.utc) + DEMO_LOGIN_TTL)
        with self._lock:
            self._expire()
            self._sessions[session.id] = session
        return session

    def get(self, session_id: str, user_id: str) -> DemoLogin | None:
        with self._lock:
            self._expire()
            session = self._sessions.get(session_id)
        return session if session and session.user_id == user_id else None

    def forget(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)


_LOGINS = DemoLogins()


def demo_storage_state(cookie_names, id_cookie: str | None = None) -> dict[str, Any]:
    """A Playwright-shaped storage state carrying placeholder cookies.

    Every cookie the platform requires is present with the value "demo", so the
    platform's own logged-in rule accepts it. It is useless against the real
    site, which is the point: no credential ever exists in this build.
    """
    names = list(dict.fromkeys([*cookie_names, *([id_cookie] if id_cookie else [])]))
    return {
        "cookies": [{"name": name, "value": "demo" if name != id_cookie else "100000000000000",
                     "domain": "example.invalid", "path": "/", "expires": -1,
                     "httpOnly": True, "secure": True, "sameSite": "Lax"}
                    for name in names],
        "origins": [],
    }
