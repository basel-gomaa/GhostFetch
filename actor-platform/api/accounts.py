"""Sign-up, sign-in, and personal API keys.

Three public routes and the rest behind a session. They are mounted only when
create_app() is given an Identity, so a deployment with no database has no
account endpoints at all rather than endpoints that fail confusingly.

WHY THE COOKIE IS NOT ENOUGH ON ITS OWN. A cookie is AMBIENT: the browser
attaches it to any request to this origin, including one triggered by another
site. `SameSite=Lax` blocks that for cross-site POSTs from a form, but it is a
browser-side default rather than a guarantee this server has made, and it does
not cover every case. So every state-changing request authenticated BY COOKIE
must also prove it came from this app -- see require_same_origin below. Requests
authenticated by BEARER TOKEN need none of this: a token is not ambient, so a
hostile page cannot cause one to be sent.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from api import invites, throttle
from api.auth import Principal, Scope
from api.main import require_same_origin
from api.validation import SafeText
from api.users import (
    SESSION_COOKIE_NAME,
    SESSION_TTL,
    AccountError,
    Identity,
    User,
    authenticate_password,
    avatar_version,
    close_session,
    delete_avatar,
    get_avatar,
    get_user,
    list_api_keys,
    mint_api_key,
    open_session,
    revoke_api_key,
    set_avatar,
    set_display_name,
)
from shared.models import is_canonical_uuid

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _env(name: str) -> str:
    import os

    return (os.environ.get(name) or "").strip().lower()


# SECURE WHENEVER THE REQUEST CAME OVER HTTPS (security review H-05). It used
# to be off unless ACTOR_PLATFORM_SECURE_COOKIES=1, which a deployment behind
# TLS could easily forget. Now the request's own scheme decides -- behind the
# documented proxy uvicorn trusts X-Forwarded-Proto from 127.0.0.1, so a public
# request reads as https -- and the variable remains as an explicit override
# either way. Plain-HTTP localhost keeps working, because browsers refuse a
# Secure cookie over plain HTTP and the user would appear never to log in.
def _secure_cookies(request: Request | None = None) -> bool:
    forced = _env("ACTOR_PLATFORM_SECURE_COOKIES")
    if forced in _TRUE:
        return True
    if forced in _FALSE:
        return False
    return request is not None and request.url.scheme == "https"


def _require_https(request: Request) -> None:
    """In public mode, refuse to sign anyone in over plain HTTP.

    ACTOR_PLATFORM_REQUIRE_HTTPS=1 is set by run-local.sh when PUBLIC_DOMAIN is,
    so a public deployment that is somehow reached over http:// (a direct port,
    a misrouted proxy) fails loudly instead of sending a password and a session
    cookie in the clear. Loopback stays allowed for the operator on the box;
    ACTOR_PLATFORM_ALLOW_INSECURE_HTTP=1 is the escape hatch for a trusted LAN.
    """
    if _env("ACTOR_PLATFORM_REQUIRE_HTTPS") not in _TRUE:
        return
    if _env("ACTOR_PLATFORM_ALLOW_INSECURE_HTTP") in _TRUE:
        return
    if request.url.scheme == "https":
        return
    if (request.client.host if request.client else "") in _LOOPBACK:
        return
    from api.main import PlatformError  # noqa: PLC0415

    raise PlatformError(400, "https required",
                        "this platform must be used over https://; plain http is refused")


# LENGTHS ARE CAPPED HERE, ahead of api/users.py's own rules, because these
# bodies are read before anyone is authenticated: an unbounded password is
# hashed with argon2 whatever its size, even for an unknown user (the dummy hash
# that keeps timing flat). The caps sit above every real value -- 254 is the
# longest valid email address, and users.py still enforces its own, tighter,
# username rule with its own message. Unknown fields are refused rather than
# ignored, so a misspelled field is an error and not a silent no-op.
MAX_EMAIL_LENGTH = 254
MAX_USERNAME_LENGTH = 64
MAX_PASSWORD_LENGTH = 1024
MAX_KEY_LABEL_LENGTH = 100


class SignupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: SafeText = Field(max_length=MAX_EMAIL_LENGTH, description="Where reports are sent.")
    username: SafeText = Field(max_length=MAX_USERNAME_LENGTH)
    password: SafeText = Field(max_length=MAX_PASSWORD_LENGTH)
    # Required unless ACTOR_PLATFORM_SIGNUP=open or this is the first account
    # (api/invites.py). A token is ~43 characters; anything far longer is not one.
    invite: SafeText | None = Field(default=None, max_length=200)


class SignupOptions(BaseModel):
    signup: str            # "invite", "open" or "closed"
    invite_required: bool  # whether the form must ask for an invite


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identifier: SafeText = Field(max_length=MAX_EMAIL_LENGTH,
                                 description="Email address or username.")
    password: SafeText = Field(max_length=MAX_PASSWORD_LENGTH)


class UserResponse(BaseModel):
    id: str
    email: str
    username: str
    is_admin: bool
    # The profile. display_name is what the person chose to be called (None:
    # show the username); avatar_version changes whenever the photo does and is
    # None when there is none, so the console can use it as a cache key.
    display_name: str | None = None
    avatar_version: str | None = None


def user_response(user: User) -> UserResponse:
    stamp = avatar_version(user.id)
    return UserResponse(id=user.id, email=user.email, username=user.username,
                        is_admin=user.is_admin, display_name=user.display_name,
                        avatar_version=stamp.isoformat() if stamp else None)


class ProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A generous raw cap; api/users.py collapses whitespace and enforces the
    # real 50-character rule with its own message. null or "" clears it.
    display_name: SafeText | None = Field(
        default=None, max_length=200,
        description="What the console calls you. Your username, which you sign in "
                    "with, does not change.",
    )


class ApiKeyResponse(BaseModel):
    id: str
    key_id: str
    scopes: list[str]
    label: str | None = None
    created_at: Any = None
    last_used_at: Any = None


class NewApiKeyResponse(ApiKeyResponse):
    # Returned exactly once, on creation. Only its sha256 is stored, so this
    # value is unrecoverable afterwards -- by us as much as by anyone else.
    key: str


class CreateApiKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: SafeText | None = Field(default=None, max_length=MAX_KEY_LABEL_LENGTH)
    # There are two scopes; a few more entries is a typo, hundreds is not a key.
    scopes: list[SafeText] | None = Field(
        default=None, max_length=8,
        description="Defaults to every scope. Narrow it for a script.",
    )


def build_router(identity: Identity, current_user, current_operator=None) -> APIRouter:
    """The account routes. `current_user` is main.py's read guard;
    `current_operator` (the `run` scope) guards minting and revoking keys.
    Defaults to the read guard so an older caller keeps working."""
    from api.main import PlatformError

    current_operator = current_operator if current_operator is not None else current_user

    def _user_of(principal: Principal) -> str:
        """Keys belong to a user. An operator key has none to manage."""
        if not principal.user_id:
            raise PlatformError(403, "requires a user account",
                                "api keys belong to a signed-in user, not an operator key")
        return principal.user_id

    router = APIRouter(tags=["accounts"])

    def _issue_session(response: Response, request: Request, user_id: str) -> None:
        session_id = open_session(user_id, user_agent=request.headers.get("user-agent"))
        response.set_cookie(
            SESSION_COOKIE_NAME,
            identity.signer.sign(session_id),
            max_age=int(SESSION_TTL.total_seconds()),
            httponly=True,          # unreadable to page JavaScript, so XSS cannot exfiltrate it
            secure=_secure_cookies(request),
            samesite="lax",
            path="/",
        )

    def _refuse(refusal) -> None:
        exc = PlatformError(
            429, "too many attempts",
            f"{refusal.reason}; try again in {max(1, round(refusal.retry_after / 60))} minute(s)",
            retry_after=refusal.retry_after,
        )
        exc.headers = {"Retry-After": str(refusal.retry_after)}
        raise exc

    @router.get("/auth/signup-options", response_model=SignupOptions)
    def signup_options() -> SignupOptions:
        """What the sign-up form should show. Public: the form is shown before
        anyone is signed in. The enforcement is in /auth/signup, not here."""
        return SignupOptions(signup=invites.signup_mode(),
                             invite_required=invites.invite_required())

    @router.post("/auth/signup", response_model=UserResponse, status_code=201)
    def signup(body: SignupRequest, request: Request, response: Response) -> UserResponse:
        """Create an account and sign in.

        Public, necessarily: it is how the first account comes to exist. An
        empty users table is a fresh install, not a misconfiguration. Rate
        limited per address (api/throttle.py), so it cannot be scripted into
        thousands of accounts -- and, unless ACTOR_PLATFORM_SIGNUP=open, it
        needs an administrator's invite (api/invites.py). The rate check comes
        first, so guessing invite tokens is throttled like everything else.
        """
        _require_https(request)
        refusal = throttle.check_rate(request, "signup")
        if refusal:
            _refuse(refusal)
        try:
            user = invites.signup(body.email, body.username, body.password, body.invite)
        except invites.SignupRefused as exc:
            raise PlatformError(403, "sign-up not allowed", str(exc)) from exc
        except AccountError as exc:
            raise PlatformError(400, "cannot create account", str(exc)) from exc
        _issue_session(response, request, user.id)
        return user_response(user)

    @router.post("/auth/login", response_model=UserResponse)
    def login(body: LoginRequest, request: Request, response: Response) -> UserResponse:
        # Both checks BEFORE argon2 runs: a refused attempt costs a query, not
        # a deliberately expensive hash.
        _require_https(request)
        refusal = throttle.check_rate(request, "login") or throttle.check_locked(body.identifier)
        if refusal:
            _refuse(refusal)
        user = authenticate_password(body.identifier, body.password)
        if user is None:
            throttle.record_failure(body.identifier)
            # ONE message for every failure. Saying "no such user" would let
            # anyone enumerate who has an account here.
            raise PlatformError(401, "authentication required",
                                "that email/username and password do not match")
        throttle.record_success(body.identifier)
        _issue_session(response, request, user.id)
        return user_response(user)

    @router.post("/auth/logout", status_code=204)
    def logout(request: Request, response: Response) -> Response:
        """Revoke this session server-side AND clear the cookie.

        Both halves matter: clearing only the cookie would leave a session that
        still authenticates anyone who kept a copy of it.
        """
        require_same_origin(request)
        session_id = identity.signer.unsign(request.cookies.get(SESSION_COOKIE_NAME))
        if session_id:
            close_session(session_id)
        response.delete_cookie(SESSION_COOKIE_NAME, path="/", httponly=True,
                               secure=_secure_cookies(request), samesite="lax")
        response.status_code = 204
        return response

    @router.get("/auth/me", response_model=UserResponse)
    def me(principal: Principal = current_user) -> UserResponse:
        user = get_user(principal.user_id) if principal.user_id else None
        if user is None:
            raise PlatformError(401, "authentication required", "not signed in")
        return user_response(user)

    # --- profile: display name and photo ---------------------------------
    # Writes take the `run` guard and the same-origin check, exactly like the
    # API-key routes: a read-only key must not be able to change who you appear
    # to be. Only ever the caller's own profile; there is no user id to pass.

    def _own(principal: Principal) -> User:
        user = get_user(_user_of(principal))
        if user is None:
            raise PlatformError(401, "authentication required", "not signed in")
        return user

    @router.patch("/me/profile", response_model=UserResponse)
    def update_profile(body: ProfileRequest, request: Request,
                       principal: Principal = current_operator) -> UserResponse:
        require_same_origin(request)
        user = _own(principal)
        try:
            user = set_display_name(user.id, body.display_name)
        except AccountError as exc:
            raise PlatformError(400, "invalid profile", str(exc)) from exc
        return user_response(user)

    @router.put("/me/avatar", response_model=UserResponse)
    async def upload_avatar(request: Request,
                            principal: Principal = current_operator) -> UserResponse:
        """The raw image as the request body. Its TYPE is read from its bytes,
        not from Content-Type, and only PNG, JPEG and WebP are kept."""
        require_same_origin(request)
        user = _own(principal)
        try:
            set_avatar(user.id, await request.body())
        except AccountError as exc:
            raise PlatformError(400, "invalid photo", str(exc)) from exc
        return user_response(user)

    @router.get("/me/avatar", include_in_schema=False)
    def read_avatar(v: str | None = None, principal: Principal = current_user) -> Response:
        found = get_avatar(principal.user_id) if principal.user_id else None
        if found is None:
            raise PlatformError(404, "no photo", "this account has no profile photo")
        data, content_type = found
        # A versioned URL (?v=<avatar_version>) never changes content, so it can
        # be cached hard; an unversioned one is revalidated. `private` either
        # way: it belongs to one signed-in person.
        cache = "private, max-age=31536000, immutable" if v else "private, no-cache"
        return Response(content=data, media_type=content_type,
                        headers={"Cache-Control": cache,
                                 "Content-Disposition": 'inline; filename="avatar"'})

    @router.delete("/me/avatar", response_model=UserResponse)
    def remove_avatar(request: Request,
                      principal: Principal = current_operator) -> UserResponse:
        require_same_origin(request)
        user = _own(principal)
        delete_avatar(user.id)
        return user_response(user)

    @router.get("/me/api-keys", response_model=list[ApiKeyResponse])
    def list_keys(principal: Principal = current_user) -> list[ApiKeyResponse]:
        # An operator key belongs to no user, so it has no keys of its own: the
        # same empty list the query returned when it was handed a NULL user id.
        if not principal.user_id:
            return []
        return [ApiKeyResponse(id=str(r["id"]), key_id=r["key_id"], scopes=list(r["scopes"]),
                               label=r["label"], created_at=r["created_at"],
                               last_used_at=r["last_used_at"])
                for r in list_api_keys(principal.user_id)]

    @router.post("/me/api-keys", response_model=NewApiKeyResponse, status_code=201)
    def create_key(body: CreateApiKeyRequest, request: Request,
                   principal: Principal = current_operator) -> NewApiKeyResponse:
        require_same_origin(request)
        user_id = _user_of(principal)
        try:
            requested = frozenset(Scope(s) for s in body.scopes) if body.scopes else None
        except ValueError as exc:
            raise PlatformError(400, "invalid scope", str(exc)) from exc

        # NEVER WIDER THAN THE CALLER. A key may only hand on what it holds:
        # otherwise any key that can reach this route can mint itself a broader
        # one, and scopes stop meaning anything. No scopes asked for means "the
        # same as mine", not "everything".
        scopes = requested if requested is not None else principal.scopes
        if not scopes <= principal.scopes:
            raise PlatformError(
                403, "insufficient scope",
                "a new key cannot have scopes the requesting credential does not",
                requested_scopes=sorted(s.value for s in scopes),
                granted_scopes=principal.scope_names,
            )

        key, row_id = mint_api_key(user_id, scopes=scopes, label=body.label)
        rows = {str(r["id"]): r for r in list_api_keys(user_id)}
        row = rows[row_id]
        return NewApiKeyResponse(
            id=row_id, key_id=row["key_id"], scopes=list(row["scopes"]), label=row["label"],
            created_at=row["created_at"], last_used_at=None, key=key,
        )

    @router.delete("/me/api-keys/{key_id}", status_code=204)
    def revoke_key(key_id: str, request: Request,
                   principal: Principal = current_operator) -> Response:
        require_same_origin(request)
        user_id = _user_of(principal)
        # Not a uuid is not one of yours: 404, rather than the 500 a uuid cast
        # error inside the query would produce.
        if not _is_uuid(key_id) or not revoke_api_key(user_id, key_id):
            raise PlatformError(404, "no such api key", f"no key {key_id} on this account")
        return Response(status_code=204)

    return router


def _is_uuid(value: str) -> bool:
    # The hyphenated form only. uuid.UUID() also takes "urn:uuid:...", braces
    # and bare hex, and such a string reached the uuid cast and failed as a 500.
    return is_canonical_uuid(value)
