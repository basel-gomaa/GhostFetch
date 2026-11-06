#!/usr/bin/env python3
"""The public platform API, v1.

    HTTP client -> FastAPI -> Queue -> Worker -> Actor Executor -> Docker -> Registry

THE API NEVER EXECUTES AN ACTOR. It validates the request, registers a PENDING
run, puts a job on the queue and returns. Docker is not imported here -- not
even transitively, since Actor discovery lives in the catalog rather than in the
execution module -- and nothing in this file can block on a container. If a
future change makes a request wait for a run to finish, the architecture has
been broken.

It depends on three abstractions and none of their storage:

    catalog.actors      what Actors exist
    RunRegistry         what runs exist and what they produced
    JobQueue            what work is outstanding

Filesystem paths, Docker and Redis are all absent from every response.

    uvicorn api.main:app --port 8000
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import contextlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import httpx  # noqa: E402
from fastapi import (  # noqa: E402
    Body, Depends, FastAPI, Query, Request, Response, WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse  # noqa: E402
from fastapi.security import HTTPBearer  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from api.auth import (  # noqa: E402
    SESSION_COOKIE_NAME,
    AuthNotConfigured,
    AuthPolicy,
    CredentialsInvalid,
    CredentialsMissing,
    InsufficientScope,
    Principal,
    Scope,
)

from api.security_headers import SecurityHeaders  # noqa: E402
from api.schemas import (  # noqa: E402
    ActorListResponse,
    ActorSummary,
    CollectorListResponse,
    CollectorStatus,
    ErrorBody,
    HealthResponse,
    MetricsResponse,
    QueueSnapshot,
    RunAccepted,
    RunCancelled,
    RunDetail,
    RunListResponse,
    RunSummary,
)
from observability.metrics import (  # noqa: E402
    METRICS_WINDOW,
    collector_health,
    platform_metrics,
)
from api.validation import install as install_validation  # noqa: E402
from shared import safe_fs  # noqa: E402
from shared.errors import redact  # noqa: E402
from shared.logging import event  # noqa: E402
from catalog import actors as catalog  # noqa: E402
from jobqueue.base import Job, JobQueue, QueueError, open_queue  # noqa: E402
if TYPE_CHECKING:
    from registry.pg_registry import PgRunRegistry
from registry.run_registry import (  # noqa: E402
    AmbiguousRunId,
    ArtifactCorrupt,
    ArtifactNotFound,
    CancelTooLate,
    RunIsLive,
    CorruptRunRecord,
    InvalidRunId,
    RegistryError,
    RunNotFound,
    RunRegistry,
    UnstorableInput,
)
from registry.dataset import COLLECTION_PATTERN  # noqa: E402
from shared.models import (  # noqa: E402
    ACTOR_NAME_PATTERN,
    ATTENTION_FILE,
    MAX_ACTOR_NAME_LENGTH,
    RESUME_FILE,
    RunStatus,
    TERMINAL_STATUSES,
)

MAX_LIST_LIMIT = 100
MAX_ITEM_SEQ = 2**63 - 1
DEFAULT_LIST_LIMIT = 20


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


# FAIR USE (security review M-09). How many runs one non-admin user may have
# PENDING or RUNNING at once, and how deep the whole queue may grow. 0 turns a
# cap off. Admins and operator keys are exempt from the per-user one.
MAX_ACTIVE_RUNS_PER_USER = _env_int("ACTOR_PLATFORM_MAX_ACTIVE_RUNS_PER_USER", 10)
MAX_QUEUE_DEPTH = _env_int("ACTOR_PLATFORM_MAX_QUEUE_DEPTH", 500)

ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorBody},
    401: {"model": ErrorBody},
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    503: {"model": ErrorBody},
}

# Declared so /docs offers an "Authorize" box and the OpenAPI schema says the
# API is protected. auto_error=False because this file owns the error shape.
BEARER_SCHEME = HTTPBearer(auto_error=False, description="An API key: 'Bearer apk_...'")


class PlatformError(Exception):
    """A failure to report to the client, in the API's own error shape.

    Not HTTPException: FastAPI renders that as {"detail": ...}, which nests the
    real reason a level down and gives "detail" two different meanings in one
    body. One handler below flattens this instead.
    """

    def __init__(self, status_code: int, error: str, detail: str = "", **extra: Any) -> None:
        # REDACT HERE, not at each call site. `detail` is very often str(exc),
        # and an exception's text is not a controlled string: a transport error
        # carries the URL it failed on with its query string, a subprocess error
        # carries its argv. Either can contain session material. This is the one
        # constructor every error response passes through, so redacting here is
        # the only placement that cannot be forgotten by the next call site.
        #
        # The `error` label is a fixed phrase chosen in this file, so it is left
        # alone; `extra` is structured data assembled by the caller from known
        # fields, and running the redactor over it would mangle values like a
        # platform name that merely looks credential-shaped.
        detail = redact(detail)
        super().__init__(f"{error}: {detail}")
        self.status_code = status_code
        self.body: dict[str, Any] = {"error": error, "detail": detail, **extra}
        # Extra response headers, e.g. Retry-After on a 429. Set by the caller.
        self.headers: dict[str, str] | None = None


# "... in /srv/actor-platform/storage/runs" -- the filesystem registry names its
# root in its messages, which is useful on a terminal and a disclosure in an
# HTTP body. Stripped here rather than in the registry, whose messages the CLI
# still wants whole.
_STORAGE_PATH = re.compile(r"\s+in\s+/\S*")


def _public(exc: Exception) -> str:
    """A registry message fit for a response body: no filesystem paths."""
    return _STORAGE_PATH.sub("", str(exc))


def _from_registry(exc: RegistryError) -> PlatformError:
    """Translate a registry failure into the response it deserves.

    Each case is distinct on purpose: a malformed id is the caller's mistake, a
    missing run is simply absent, and a corrupt record is the platform's fault
    and must not be reported as a client error.
    """
    if isinstance(exc, InvalidRunId):
        return PlatformError(400, "invalid run id", _public(exc))
    if isinstance(exc, UnstorableInput):
        return PlatformError(400, "invalid actor input", _public(exc))
    if isinstance(exc, AmbiguousRunId):
        return PlatformError(409, "ambiguous run id", _public(exc))
    if isinstance(exc, RunNotFound):
        return PlatformError(404, "run not found", _public(exc))
    if isinstance(exc, ArtifactNotFound):
        return PlatformError(404, "artifact not available", _public(exc))
    if isinstance(exc, (CorruptRunRecord, ArtifactCorrupt)):
        return PlatformError(500, "corrupt run record", _public(exc))
    return PlatformError(500, "registry error", _public(exc))


def require_same_origin(request: Request) -> None:
    """Refuse a cookie-authenticated write that did not come from this app.

    Checks `Sec-Fetch-Site` first (sent by every current browser and not
    forgeable by page JavaScript), falling back to comparing `Origin` with the
    request's own host for anything older. A request carrying NEITHER header is
    not a browser, so it cannot be a cross-site browser attack and is allowed --
    a script with a stolen cookie is a different problem, and one this check was
    never able to address.
    """
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if site:
        if site in {"same-origin", "none"}:
            return
        raise PlatformError(
            403, "cross-site request refused",
            f"a state-changing request must come from this application "
            f"(Sec-Fetch-Site was {site!r})",
        )

    origin = (request.headers.get("origin") or "").strip()
    if not origin:
        return
    from urllib.parse import urlparse

    if urlparse(origin).netloc != (request.headers.get("host") or ""):
        raise PlatformError(
            403, "cross-site request refused",
            f"Origin {origin!r} does not match this application",
        )


def identity_of(app):
    """The Identity this app was built with, or None.

    A websocket handler is not inside create_app's closure, so it reads the
    identity from the app it is serving rather than from a module global -- two
    apps in one process must not share one.
    """
    return getattr(app.state, "identity", None)


def create_app(
    registry: RunRegistry | None = None,
    queue: JobQueue | None = None,
    auth: AuthPolicy | None = None,
    identity=None,
) -> FastAPI:
    """Build the app. Every dependency is injectable so tests need no server.

    `identity` is an api.users.Identity, or None. None means NO accounts: the
    platform authenticates environment keys only and mounts no /auth routes,
    which is exactly how it behaved before accounts existed. That is deliberate
    -- it keeps a single-user deployment single-user, and it is why every
    pre-existing test still constructs this app unchanged.
    """
    registry = registry if registry is not None else RunRegistry()
    queue = queue if queue is not None else open_queue()
    auth = auth if auth is not None else AuthPolicy.from_env()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        queue.close()

    app = FastAPI(
        title="actor-platform",
        version="1.0.0",
        summary="Submit Actor runs asynchronously and inspect their results.",
        lifespan=lifespan,
    )

    @app.exception_handler(PlatformError)
    async def _render_error(_request: Request, exc: PlatformError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.body,
                            headers=exc.headers)

    # A body-size limit ahead of everything, and 422s that never echo input.
    # See api/validation.py for why neither can be left to the routes.
    install_validation(app)
    # CSP, framing, nosniff, referrer and (over HTTPS) HSTS on every response
    # (security review M-05). See api/security_headers.py.
    app.add_middleware(SecurityHeaders)

    @app.get("/ops", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> HTMLResponse:
        """The OPERATOR dashboard shell -- a THIN client, public like a login
        page. It holds no data and no key; every datum it shows comes from an
        auth-gated API call the browser makes with a key the operator pastes in.
        Serving one static asset is not moving platform logic into the frontend.

        Moved off "/" when the user console arrived. Thirty people arriving at
        the root of this service should meet the product, not an internal tool
        asking them to paste an API key they do not have.
        """
        ui = REPO_ROOT / "ui" / "index.html"
        if not ui.is_file():
            return HTMLResponse("<h1>dashboard not installed</h1>", status_code=404)
        return HTMLResponse(ui.read_text(encoding="utf-8"))

    # --- the console -------------------------------------------------------
    # A built React bundle: static files on the SAME ORIGIN as the API, which is
    # what lets the session cookie work with no CORS and no second service to
    # expose. Public, exactly as the operator dashboard is and for the same
    # reason -- it is a shell, and every datum in it comes from an auth-gated
    # call the browser makes afterwards.
    CONSOLE = REPO_ROOT / "ui" / "static"

    if (CONSOLE / "assets").is_dir():
        app.mount("/console/assets", StaticFiles(directory=CONSOLE / "assets"),
                  name="console-assets")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def console() -> HTMLResponse:
        index = CONSOLE / "index.html"
        if not index.is_file():
            # A clear instruction rather than a 404: the API is fine, the
            # frontend simply has not been built.
            return HTMLResponse(
                "<h1>console not built</h1>"
                "<p>Run <code>npm install &amp;&amp; npm run build</code> in "
                "<code>ui/console/</code>.</p>"
                "<p>The operator dashboard is at <a href='/ops'>/ops</a>.</p>",
                status_code=503,
            )
        return HTMLResponse(index.read_text(encoding="utf-8"))

    def identify(request: Request) -> Principal:
        """Turn the request's credentials into a caller, or refuse.

        THE ONE CHOKE POINT. Three credentials are accepted, in this order:

          1. the console's session cookie   -> a user
          2. a per-user API key             -> that user, with the key's scopes
          3. an environment key             -> the operator (see AuthPolicy)

        Cookie first because it is the console's own path and the common case.
        Order matters only in that a request presenting both should be treated
        as the signed-in user rather than as whoever's token was pasted.
        """
        if identity is not None:
            principal = identity.principal_for_cookie(
                request.cookies.get(SESSION_COOKIE_NAME)
            )
            if principal is not None:
                # A cookie is AMBIENT -- attached by the browser to any request
                # to this origin, including one a hostile page caused. So a
                # cookie-authenticated WRITE has to prove it came from this app.
                # Bearer-token requests skip this: a token is not ambient.
                if request.method not in ("GET", "HEAD", "OPTIONS"):
                    require_same_origin(request)
                return principal

            try:
                principal = identity.principal_for_key(
                    request.headers.get("authorization")
                )
            except CredentialsInvalid as exc:
                raise PlatformError(401, "invalid api key", str(exc)) from exc
            if principal is not None:
                return principal

        try:
            principal = auth.authenticate(request.headers.get("authorization"))
            if auth.enforced:
                _audit_operator_key(request)
            return principal
        except AuthNotConfigured as exc:
            # 503, not 401: the caller did nothing wrong and no key would help.
            raise PlatformError(503, "authentication not configured", str(exc)) from exc
        except CredentialsMissing as exc:
            raise PlatformError(401, "authentication required", str(exc)) from exc
        except CredentialsInvalid as exc:
            raise PlatformError(401, "invalid api key", str(exc)) from exc

    _operator_reads_logged: dict[str, float] = {}

    def _audit_operator_key(request: Request) -> None:
        """Record use of an environment operator key (security review M-07).

        An operator key is an admin over EVERY tenant's runs, so its use is
        written to the event log: every write, and reads at most once per key
        per ten minutes (the operator dashboard polls). The key is identified by
        a short sha256 fingerprint, never by its value. For integrations, a
        user's own API key (POST /me/api-keys) is the tenant-scoped alternative.
        """
        import hashlib  # noqa: PLC0415
        import time as _time  # noqa: PLC0415

        credential = (request.headers.get("authorization") or "").partition(" ")[2].strip()
        fingerprint = hashlib.sha256(credential.encode("utf-8")).hexdigest()[:8]
        if request.method in ("GET", "HEAD", "OPTIONS"):
            now = _time.monotonic()
            if now - _operator_reads_logged.get(fingerprint, -1e9) < 600:
                return
            _operator_reads_logged[fingerprint] = now
        # ALWAYS written, unlike event() (which is silent unless JSON logging
        # is on): an audit trail that is off by default is not one.
        print(f"[api] operator key {fingerprint} used: {request.method} "
              f"{request.url.path[:200]}", file=sys.stderr, flush=True)
        event("api", "operator_key_used", key=fingerprint, method=request.method,
              path=request.url.path[:200])

    def needs(scope: Scope):
        """Build the guard for one scope.

        Applied per route rather than as global middleware: a middleware has to
        decide by URL path, and a path it fails to match is an endpoint silently
        left open. A missing dependency here is visible in the route definition,
        and the scope it needs is stated next to what it does.
        """

        def guard(request: Request, _scheme=Depends(BEARER_SCHEME)) -> Principal:
            principal = identify(request)
            try:
                principal.require(scope)
            except InsufficientScope as exc:
                # 403, not 401: we know who this is, and a better credential is
                # not the answer -- a differently SCOPED one is.
                raise PlatformError(
                    403,
                    "insufficient scope",
                    str(exc),
                    required_scope=exc.required.value,
                    granted_scopes=sorted(s.value for s in exc.granted),
                ) from exc
            return principal

        return guard

    reads = [Depends(needs(Scope.READ))]
    runs = [Depends(needs(Scope.RUN))]

    if identity is not None:
        from api.accounts import build_router  # noqa: PLC0415 - avoids a cycle

        # TWO guards, because the account routes both read and write. A write
        # (minting or revoking a key) is an OPERATOR action and needs `run`: a
        # read-only key that could mint a key could mint itself a full one.
        app.include_router(build_router(
            identity, Depends(needs(Scope.READ)), Depends(needs(Scope.RUN))
        ))

        # Admin-only user management, behind the same two guards plus an admin
        # check inside: reading the user list needs `read`, changing a user
        # needs `run`, and both need an administrator.
        from api.admin import build_router as build_admin  # noqa: PLC0415

        app.include_router(build_admin(
            registry, Depends(needs(Scope.READ)), Depends(needs(Scope.RUN))
        ))

        # The websocket route needs to identify a caller from the session
        # cookie, and a websocket handler has no access to this closure. Stashed
        # on the app rather than made a module global, so two apps in one
        # process (which the tests do) cannot see each other's identity.
        app.state.identity = identity

        if identity.keyring is not None:
            from api.social import build_router as build_social  # noqa: PLC0415

            app.include_router(
                # Same split: disconnecting an account, starting a login
                # browser and storing a session are writes, so they need `run`.
                build_social(identity.keyring, Depends(needs(Scope.READ)),
                             Depends(needs(Scope.RUN)))
            )

    def _actor_policy(actor_name: str):
        """This Actor's declared policy, from config/actors.json.

        worker/limits.py is imported for its CONFIG READER, not for execution:
        it is stdlib-only, touches no Docker and starts no container. The API's
        isolation rule is about not importing the execution engine, and this is
        the file that holds "does this Actor need a connected account" -- which
        the API must know to refuse a run before creating it.
        """
        from worker.limits import WorkerConfig  # noqa: PLC0415

        try:
            return WorkerConfig.load().limits_for(actor_name)
        except Exception:  # noqa: BLE001 - a bad config must not 500 every submit
            from worker.limits import ActorProfile  # noqa: PLC0415

            return ActorProfile()

    def _connected_account(principal: Principal, actor_name: str) -> tuple[str | None, str | None]:
        """(social_account_id, concurrency_key) for this run, or refuse.

        Refusing HERE -- before registry.create() -- is the point: a user should
        not spend a run to discover their session is dead, and a queued run that
        can never be given a credential is worse than no run at all.
        """
        profile = _actor_policy(actor_name)
        platform = profile.requires_social
        if not platform or identity is None or not principal.user_id:
            return None, None
        from db.pool import pool  # noqa: PLC0415

        with pool().connection() as conn:
            row = conn.execute(
                "SELECT id, status FROM social_accounts WHERE user_id = %s AND platform = %s",
                (principal.user_id, platform),
            ).fetchone()

        # Named a platform, but does not NEED an account on it: some sites serve
        # their public data to anyone, so a user with no account must never be
        # refused. But a user who DID connect one must get it -- this used to
        # return before the lookup, so every such run went out anonymous even
        # with a connected account, and a Threads run fell back to the public
        # page's ~10 replies per post instead of the paginated reply query.
        if profile.session_optional:
            if row is None or row["status"] != "connected":
                return None, None
            account_id = str(row["id"])
            return account_id, f"social:{account_id}"

        if row is None:
            raise PlatformError(
                409, "social account not connected",
                f"connect a {platform} account before running this actor",
                platform=platform, reconnect=True,
            )
        if row["status"] != "connected":
            # 409, not 400: the REQUEST is well-formed; the account state is
            # wrong, and the fix is an action the user takes elsewhere.
            raise PlatformError(
                409, "social account needs reconnecting",
                f"your {platform} session is {row['status']}; reconnect it to run this actor",
                platform=platform, reconnect=True,
            )
        account_id = str(row["id"])
        # Opaque to the queue and the worker: they only ever compare it.
        return account_id, f"social:{account_id}"

    @contextlib.contextmanager
    def _run_quota(principal: Principal):
        """Refuse a user's run when they already have too many in flight (M-09).

        Counted and created under a per-user Postgres advisory lock, so two
        submissions racing each other cannot both slip under the cap. Admins
        are exempt, and so is every ownerless run (an operator key's): those
        are the deployment's own work, including the test suites' bursts.
        """
        if principal.is_admin or not principal.user_id or not MAX_ACTIVE_RUNS_PER_USER:
            yield
            return
        from db.pool import pool  # noqa: PLC0415

        key = f"run-quota:{principal.user_id}"
        with pool().connection() as conn:
            conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (key,))
            try:
                row = conn.execute(
                    "SELECT count(*) AS n FROM runs WHERE owner_id = %s "
                    "AND status IN ('PENDING', 'RUNNING')",
                    (principal.user_id,),
                ).fetchone()
                active = int(row["n"]) if row else 0
                if active >= MAX_ACTIVE_RUNS_PER_USER:
                    raise PlatformError(
                        429, "too many active runs",
                        f"you have {active} runs queued or running (the limit is "
                        f"{MAX_ACTIVE_RUNS_PER_USER}); wait for one to finish",
                    )
                conn.commit()
                yield
            finally:
                conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
                conn.commit()

    def _recipient_for(principal: Principal) -> str | None:
        """Where this user's report should go: the address they signed up with."""
        if identity is None or not principal.user_id:
            return None
        from api.users import get_user  # noqa: PLC0415

        user = get_user(principal.user_id)
        return user.email if user else None

    def _db_reachable() -> bool:
        """Is the account database answering? Never raises -- /health must be
        able to report a failure rather than become one."""
        try:
            from db.pool import healthy  # noqa: PLC0415

            return healthy()
        except Exception:  # noqa: BLE001
            return False

    def scoped(principal: Principal) -> RunRegistry:
        """The registry AS THIS CALLER SEES IT.

        Ownership is applied by handing the handler a narrowed object, not by
        asking each handler to remember a filter. A handler that never holds the
        unscoped registry cannot forget to scope it -- the same structural
        argument api/schemas.py makes about run_dir.

        Admins and operators get the unscoped registry, and so does a backend
        with no ownership at all (the filesystem one), which is what keeps a
        single-user deployment behaving exactly as before.
        """
        if principal.is_admin:
            return registry
        if not hasattr(registry, "for_owner"):
            # A backend with no ownership is fine only when there are no users
            # to keep apart. With accounts wired in, handing a user the unscoped
            # registry would show them everybody's runs -- so refuse instead.
            if identity is not None:
                raise PlatformError(
                    500, "misconfigured",
                    "accounts are enabled but the run registry cannot scope by owner",
                )
            return registry
        return registry.for_owner(principal.user_id)

    def reader(principal: Principal = Depends(needs(Scope.READ))) -> RunRegistry:
        return scoped(principal)

    def runner(principal: Principal = Depends(needs(Scope.RUN))) -> RunRegistry:
        return scoped(principal)

    def resolve(run_id: str, reg: RunRegistry | None = None) -> str:
        try:
            return (reg if reg is not None else registry).resolve(run_id)
        except RegistryError as exc:
            raise _from_registry(exc) from exc

    # --- actors ------------------------------------------------------------

    @app.get("/actors", response_model=ActorListResponse, dependencies=reads, tags=["actors"])
    def list_actors() -> ActorListResponse:
        """The Actors this platform can run, discovered rather than declared."""
        return ActorListResponse(
            actors=[
                ActorSummary(
                    name=actor.name,
                    description=actor.description,
                    input_schema=actor.input_schema,
                    schema_error=actor.schema_error,
                    # From config, not from the catalog: catalog/actors.py is
                    # deliberately ignorant of config/actors.json, and must stay
                    # so -- it is what resolves an Actor for a RUN, and a hidden
                    # Actor still has to run.
                    hidden=_actor_policy(actor.name).hidden,
                    # Same source as `hidden`, for the same reason: where a
                    # person solves this Actor's challenge is the ACTOR's fact,
                    # declared in config/actors.json. The console must not infer
                    # it from an actor name.
                    drives_host_browser=_actor_policy(actor.name).drives_host_browser,
                    limits=_actor_policy(actor.name).limits.public(),
                    # Same source as `hidden`: whether this Actor ever asks a
                    # person for help is the ACTOR's declared fact, and the
                    # console must not infer it from an Actor name.
                    awaits_human=_actor_policy(actor.name).awaits_human,
                    # How to SHOW it: the Actor's declared label and logo. The
                    # console draws these and names no site itself.
                    label=_actor_policy(actor.name).label,
                    icon=(_actor_policy(actor.name).icon.public()
                          if _actor_policy(actor.name).icon else None),
                )
                for actor in catalog.list_actors()
            ]
        )

    @app.post(
        "/actors/{actor_name}/runs",
        response_model=RunAccepted,
        status_code=202,
        responses=ERRORS,
        tags=["actors"],
    )
    def start_run(
        actor_name: str,
        response: Response,
        payload: Any = Body(default=None),
        principal: Principal = Depends(needs(Scope.RUN)),
        timeout_minutes: int | None = Query(
            default=None, description="wall-clock limit for this run; default and maximum per "
            "Actor are in GET /actors"),
        memory_mb: int | None = Query(default=None, description="memory limit for this run"),
        cpus: float | None = Query(default=None, description="CPU limit for this run"),
    ) -> RunAccepted:
        """Register a run and queue it. Returns before the Actor starts.

        202, not 200: the run is accepted, not completed. Poll GET /runs/{id}.

        The run's LIMITS ride as query parameters, never in the body: the body
        is the Actor's input and is validated against the Actor's own schema,
        which has no business knowing about the platform's resource envelope.
        """
        # The catalog's checks, not a copy of them, so the API and the executor
        # can never disagree about what a valid or installed Actor is.
        try:
            actor = catalog.find_actor(actor_name)
        except catalog.InvalidActorName as exc:
            raise PlatformError(400, "invalid actor name", str(exc)) from exc
        except catalog.UnknownActor as exc:
            raise PlatformError(404, "unknown actor", str(exc)) from exc

        payload = payload if payload is not None else {}

        # Typed as Any and checked here rather than declared as a dict: letting
        # FastAPI enforce the type would answer a non-object body with its own
        # 422 shape, so one kind of bad input would escape the error envelope
        # every other kind uses.
        if not isinstance(payload, dict):
            raise PlatformError(
                400,
                "invalid actor input",
                f"input must be a JSON object, got {type(payload).__name__}",
                violations=[
                    {
                        "path": "(root)",
                        "message": f"input must be a JSON object, got {type(payload).__name__}",
                    }
                ],
            )

        # Reject a bad input HERE, before a run exists and before a container is
        # built, rather than letting it fail inside Docker seconds later as an
        # opaque exit 1. The Actor still validates its own input -- it can be
        # started without the platform at all -- so this is an early filter, not
        # the only check.
        try:
            catalog.validate_input(actor, payload)
        except catalog.InvalidActorInput as exc:
            raise PlatformError(
                400, "invalid actor input", str(exc), violations=exc.violations
            ) from exc
        except catalog.InvalidActorSchema as exc:
            # The Actor is misconfigured, not the request. Refusing beats
            # accepting work the platform cannot validate as promised.
            raise PlatformError(500, "actor schema is invalid", str(exc)) from exc

        # THE RUN'S LIMITS. Over an Actor's maximum is REFUSED here rather than
        # quietly lowered, so nobody gets a shorter run than they asked for
        # without being told; the Worker clamps again regardless. What is
        # stored is the resolved value, so a PENDING run already shows the
        # limits it will get.
        from worker.limits import RunOptions  # noqa: PLC0415 - config reader, not the engine

        policy = _actor_policy(actor_name)
        options = RunOptions(
            timeout_s=timeout_minutes * 60 if timeout_minutes is not None else None,
            memory_mb=memory_mb,
            cpus=cpus,
        )
        problems = policy.limits.problems(options)
        if problems:
            raise PlatformError(
                400, "invalid run options", "; ".join(problems),
                violations=[{"path": "(query)", "message": m} for m in problems],
            )
        resolved = policy.limits.resolve(options)
        limits_kwargs = {
            "timeout_s": resolved.timeout_s,
            "memory_mb": resolved.memory_mb,
            "cpus": resolved.cpus,
        }

        # A FULL QUEUE IS REFUSED, not grown without bound (security review
        # M-09): every queued run is a directory, a row and a job, and nothing
        # else stops one caller filling the disk with them.
        if MAX_QUEUE_DEPTH:
            try:
                depth = queue.depth()
            except Exception:  # noqa: BLE001 - an unreadable depth must not block runs
                depth = 0
            if depth >= MAX_QUEUE_DEPTH:
                raise PlatformError(
                    503, "queue full",
                    f"{depth} runs are already waiting; try again when some have finished",
                )

        # OWNERSHIP IS SET AT CREATION and never afterwards. A run that was
        # born ownerless can only be adopted by guessing, so there is no path
        # that assigns one later -- the ~1802 imported runs stay ownerless and
        # admin-only, which is the honest answer for work done before accounts
        # existed.
        #
        # An operator key has no user_id, so a run it starts is ownerless too.
        # That is correct: it belongs to the deployment, not to a person.
        account_id, concurrency_key = _connected_account(principal, actor_name)

        try:
            if principal.user_id and hasattr(registry, "for_owner"):
                # for_owner() is the Postgres registry's; that one's create()
                # takes the ownership fields. The cast says what hasattr proved.
                with _run_quota(principal):
                    run = cast("PgRunRegistry", registry).create(
                        actor_name, payload,
                        owner_id=principal.user_id,
                        social_account_id=account_id,
                        concurrency_key=concurrency_key,
                        # Recorded HERE rather than carried in the input document,
                        # which is written to input.json and served over HTTP. The
                        # Actor has no business knowing who the report goes to.
                        report_email=_recipient_for(principal),
                        **limits_kwargs,
                    )
            else:
                run = registry.create(actor_name, payload, **limits_kwargs)
        except RegistryError as exc:
            raise _from_registry(exc) from exc

        try:
            # The concurrency key travels WITH the job, so a Worker can decline
            # to lease it without first taking it -- which is what keeps a job
            # whose session is busy plainly PENDING where the reconciler and
            # other Workers can still see it.
            queue.enqueue(Job(run_id=run.run_id, actor=actor_name, input=payload,
                              concurrency_key=concurrency_key))
        except QueueError as exc:
            # The run exists but nothing will ever pick it up. Close it out
            # rather than leaving a PENDING record waiting for ever -- the
            # reconciler would otherwise have to guess at it later.
            registry.complete(
                run.run_id,
                status=RunStatus.PLATFORM_ERROR,
                error=f"could not enqueue the run: {exc}",
            )
            raise PlatformError(503, "queue unavailable", str(exc)) from exc

        response.headers["Location"] = f"/runs/{run.run_id}"
        return RunAccepted(run_id=run.run_id, actor=run.actor, status=run.status.value)

    # --- runs --------------------------------------------------------------

    @app.get("/runs", response_model=RunListResponse, tags=["runs"])
    def list_runs(
        reg: RunRegistry = Depends(reader),
        actor: str | None = Query(
            default=None, max_length=MAX_ACTOR_NAME_LENGTH,
            pattern=f"^{ACTOR_NAME_PATTERN.pattern}$", description="only runs of this Actor"),
        status: RunStatus | None = Query(default=None, description="only runs in this state"),
        limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    ) -> RunListResponse:
        """Runs, NEWEST FIRST -- the Registry's documented ordering.

        Run ids are time-ordered to the microsecond, so that order is total and
        stable. There is no cursor: the Registry has no seek, and faking one by
        listing everything and slicing would be worse than not offering it.
        """
        try:
            runs = reg.list(actor=actor, status=status, limit=limit, skip_corrupt=True)
        except RegistryError as exc:
            raise _from_registry(exc) from exc

        rows = [RunSummary.from_run(run) for run in runs]
        return RunListResponse(runs=rows, count=len(rows))

    @app.get("/runs/{run_id}", response_model=RunDetail, responses=ERRORS,
              tags=["runs"])
    def get_run(run_id: str, reg: RunRegistry = Depends(reader)) -> RunDetail:
        try:
            return RunDetail.from_run(reg.get(resolve(run_id, reg)))
        except RegistryError as exc:
            raise _from_registry(exc) from exc

    @app.get("/runs/{run_id}/input", responses=ERRORS, tags=["runs"])
    def get_run_input(run_id: str, reg: RunRegistry = Depends(reader)) -> Any:
        """The input the run was started with, exactly as submitted."""
        try:
            return reg.get_input(resolve(run_id, reg))
        except RegistryError as exc:
            raise _from_registry(exc) from exc

    @app.get("/runs/{run_id}/output", responses=ERRORS, tags=["runs"])
    def get_run_output(run_id: str, reg: RunRegistry = Depends(reader)) -> Any:
        """Whatever the Actor produced.

        Returned verbatim, with no response_model: an Actor's output is any JSON
        value -- an object, an array, a number -- and the platform does not get
        to have an opinion about its shape.
        """
        # Resolved through the CALLER'S registry. An unscoped resolve here
        # expanded someone else's prefix into their full run id, which the
        # scoped read below then quoted back in its 404.
        resolved = resolve(run_id, reg)
        try:
            return reg.get_output(resolved)
        except ArtifactNotFound as exc:
            run = reg.get(resolved)
            # 404 with the reason attached: usually the run simply has not
            # finished, which is not the same as the run not existing.
            raise PlatformError(
                404,
                "no output",
                str(exc),
                status=run.status.value,
                run_error=run.error,
            ) from exc
        except RegistryError as exc:
            raise _from_registry(exc) from exc

    @app.get("/runs/{run_id}/items", responses=ERRORS, tags=["runs"])
    def list_run_items(
        run_id: str,
        reg: RunRegistry = Depends(reader),
        # The ingest side's own rule (registry/dataset.py), so a filter that
        # could never match is refused instead of reaching the database.
        collection: str | None = Query(
            default=None, max_length=64, pattern=f"^{COLLECTION_PATTERN.pattern}$",
            description="only items in this collection"),
        # seq is a Postgres bigint; a larger number is not a position anyone
        # was given.
        after: int = Query(
            default=0, ge=0, le=MAX_ITEM_SEQ,
            description="return items after this seq (from next_after)"),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> dict:
        """Items the Actor produced, in the order it produced them.

        Readable WHILE the run goes -- that is the point of items, as opposed to
        output.json, which exists only at the end. A run whose Actor writes no
        items simply has none: an empty page, not a 404.

        Paged by seq, which is stable: pass the response's `next_after` back as
        `after`. `next_after` is null when this page reached the end so far --
        on a running run, asking again later may still find more.
        """
        try:
            page = reg.get_items(resolve(run_id, reg), collection=collection,
                                 after=after, limit=limit)
        except RegistryError as exc:
            raise _from_registry(exc) from exc
        return {**page, "count": len(page["items"])}

    @app.get("/runs/{run_id}/artifacts", responses=ERRORS, tags=["runs"])
    def list_run_artifacts(run_id: str, reg: RunRegistry = Depends(reader)) -> dict:
        """The extra files this run produced -- a report, a PDF, a dashboard.

        Names and sizes only. Owner-scoped like every other run route, because
        a report is as much this user's data as the run it came from.
        """
        try:
            return {"artifacts": reg.list_artifacts(resolve(run_id, reg))}
        except RegistryError as exc:
            raise _from_registry(exc) from exc

    @app.get("/runs/{run_id}/artifacts/{name}", responses=ERRORS, tags=["runs"])
    def get_run_artifact(run_id: str, name: str, reg: RunRegistry = Depends(reader)):
        """One artifact, streamed back with a sensible content type.

        The NAME is matched against what the directory actually holds rather
        than joined onto a path and then checked, so traversal is not something
        this has to defend against -- "../../etc/passwd" is simply not in the
        listing.
        """
        from fastapi.responses import FileResponse, StreamingResponse  # noqa: PLC0415

        try:
            path = reg.artifact_path(resolve(run_id, reg), name)
        except RegistryError as exc:
            raise _from_registry(exc) from exc

        # SERVED FROM THE DESCRIPTOR THAT WAS CHECKED. FileResponse re-opens by
        # path when it sends, and the Actor may still be running: it could swap
        # the file for a link in between. Opened once, as a regular file only.
        try:
            fd = safe_fs.open_regular(path)
        except OSError as exc:
            raise PlatformError(404, "artifact not available",
                                f"run has no artifact named {name!r}") from exc
        handle = os.fdopen(fd, "rb")
        size = os.fstat(fd).st_size

        def chunks():
            with handle:
                while block := handle.read(64 * 1024):
                    yield block

        # inline for the things a browser can render (the dashboard, the PDF),
        # attachment for everything else. Never guessed from the caller's input.
        kinds = {".html": "text/html", ".pdf": "application/pdf",
                 ".txt": "text/plain; charset=utf-8", ".json": "application/json"}
        media_type = kinds.get(path.suffix.lower(), "application/octet-stream")
        # The same Content-Disposition FileResponse would have sent, built by
        # FileResponse itself, so no client sees a different header.
        disposition = FileResponse(path, media_type=media_type,
                                   filename=path.name).headers["content-disposition"]
        # An artifact is the Actor's file -- built from scraped pages -- served
        # on the API's own origin. It goes out as an attachment already; nosniff
        # and, for HTML, a sandbox CSP make sure that even a browser that renders
        # it anyway runs it in an opaque origin, with no access to the console's
        # cookie or same-origin API.
        headers = {"content-disposition": disposition, "content-length": str(size),
                   "x-content-type-options": "nosniff"}
        if media_type == "text/html":
            headers["content-security-policy"] = "sandbox allow-scripts allow-popups"
        return StreamingResponse(chunks(), media_type=media_type, headers=headers)

    @app.get(
        "/runs/{run_id}/logs",
        response_class=PlainTextResponse,
        responses=ERRORS,
        tags=["runs"],
    )
    def get_run_logs(run_id: str, reg: RunRegistry = Depends(reader)) -> str:
        """Everything the Actor printed, as text/plain.

        Not wrapped in JSON: logs are a stream, and the stdout/stderr contract
        the Actors write to should survive the trip to a client unaltered.
        """
        try:
            return reg.get_logs(resolve(run_id, reg))
        except RegistryError as exc:
            raise _from_registry(exc) from exc

    # --- operations --------------------------------------------------------

    @app.post(
        "/runs/{run_id}/cancel",
        response_model=RunCancelled,
        responses=ERRORS,
        tags=["runs"],
    )
    def cancel_run(run_id: str, reg: RunRegistry = Depends(runner)) -> RunCancelled:
        """Request cancellation of a run. An OPERATOR action (the `run` scope).

        Records the intent in the Registry, which is the single source of truth
        AND the channel the worker watches. The API never touches Docker: for a
        RUNNING run, the worker that owns the container sees the CANCELLED state
        and stops the container itself.

        Idempotent. Cancelling an already-CANCELLED run returns `already: true`;
        cancelling one that already finished otherwise is a 409 carrying its real
        terminal status, never a silent overwrite.
        """
        try:
            resolved = reg.resolve(run_id)
            before = reg.get(resolved).status.value
            run = reg.cancel(resolved)
        except (InvalidRunId, AmbiguousRunId) as exc:
            raise PlatformError(400, "invalid run id", _public(exc)) from exc
        except RunNotFound as exc:
            raise PlatformError(404, "unknown run", _public(exc)) from exc
        except CancelTooLate as exc:
            # The run finished before the cancel landed; its result stands.
            raise PlatformError(409, "run already finished", _public(exc)) from exc
        except RegistryError as exc:
            raise PlatformError(500, "could not cancel run", _public(exc)) from exc

        already = before == "CANCELLED"
        if not already:
            event("api", "run_cancel_requested", run_id=run.run_id,
                  actor=run.actor, previous=before)
        return RunCancelled(
            run_id=run.run_id, actor=run.actor, status=run.status.value, already=already
        )

    @app.delete("/runs/{run_id}", status_code=204, responses=ERRORS, tags=["runs"])
    def delete_run(run_id: str, reg: RunRegistry = Depends(runner)) -> Response:
        """Delete one of your FINISHED runs: record, items, logs, output and
        artifacts (security review M-04). A live run is a 409 -- cancel it
        first. Someone else's run is 404, like every other run route."""
        try:
            resolved = reg.resolve(run_id)
            run = reg.delete(resolved)
        except (InvalidRunId, AmbiguousRunId) as exc:
            raise PlatformError(400, "invalid run id", _public(exc)) from exc
        except RunNotFound as exc:
            raise PlatformError(404, "unknown run", _public(exc)) from exc
        except RunIsLive as exc:
            raise PlatformError(409, "run is still active", _public(exc)) from exc
        except RegistryError as exc:
            raise PlatformError(500, "could not delete run", _public(exc)) from exc
        event("api", "run_deleted", run_id=run.run_id, actor=run.actor)
        return Response(status_code=204)

    # ------------------------------------------------------------------
    # A run asking for help
    #
    # The real build could pause a run on a challenge only a person can clear
    # and stream its display to the console over VNC. The public demo build
    # ships no display or viewer; these two routes remain only as the read and
    # write side of the attention/resume marker files in a run's directory.
    #
    # Someone else's run is 404 here, exactly as it is everywhere else.
    # ------------------------------------------------------------------

    @app.get("/runs/{run_id}/attention", responses=ERRORS, tags=["runs"])
    def get_run_attention(run_id: str, reg: RunRegistry = Depends(reader)) -> Any:
        """What this run is waiting for a person to do, or null.

        Read from the run directory rather than the run record, because it is
        the ACTOR that knows -- and the run directory is already how an Actor
        and the platform exchange documents. It is also the only channel
        available while a run is in flight: log.txt is written once, when the
        container exits, so a paused run's logs do not exist yet.
        """
        try:
            run = reg.get(reg.resolve(run_id))
        except (InvalidRunId, AmbiguousRunId) as exc:
            raise PlatformError(400, "invalid run id", _public(exc)) from exc
        except RunNotFound as exc:
            raise PlatformError(404, "unknown run", _public(exc)) from exc
        marker = reg.run_dir(run.run_id) / ATTENTION_FILE
        try:
            # Regular file only: the run directory is bind-mounted into the
            # Actor's container, so a link planted there must not let this
            # route read a host file, nor a FIFO hang it. See shared/safe_fs.
            return json.loads(safe_fs.read_text(marker))
        except (OSError, ValueError):
            # Absent is the normal case and unreadable is a half-written file
            # we will see whole on the next poll. Neither is an error.
            return None

    @app.post("/runs/{run_id}/resume", responses=ERRORS, tags=["runs"])
    def resume_run(run_id: str, request: Request,
                   reg: RunRegistry = Depends(runner)) -> dict:
        """Tell a waiting run to try carrying on. An OPERATOR action.

        A HINT, NOT A VERDICT. The Actor re-checks the page before accepting it,
        for the same reason the login flow decides in the API rather than
        trusting its container: pressing the button early is the common case on
        a challenge, and a run sent back into a scrape it is still blocked on
        would fail for a reason nobody could see.
        """
        try:
            run = reg.get(reg.resolve(run_id))
        except (InvalidRunId, AmbiguousRunId) as exc:
            raise PlatformError(400, "invalid run id", _public(exc)) from exc
        except RunNotFound as exc:
            raise PlatformError(404, "unknown run", _public(exc)) from exc
        if run.status in TERMINAL_STATUSES:
            raise PlatformError(409, "run already finished",
                                f"this run is {run.status.value}")
        try:
            # Replaced, never opened through, for the same reason as the
            # attention marker -- and it matters more here: writing through a
            # link the container planted would TRUNCATE whatever host file it
            # pointed at. A planted link is simply replaced by the signal.
            safe_fs.write_text_atomic(reg.run_dir(run.run_id) / RESUME_FILE, "{}\n")
        except OSError as exc:
            # strerror only: the full exception names the run directory's host
            # path, which is not the caller's business.
            raise PlatformError(500, "could not signal the run",
                                exc.strerror or "write failed") from exc
        event("api", "run_resume_requested", run_id=run.run_id, actor=run.actor)
        return {"run_id": run.run_id, "actor": run.actor, "status": run.status.value}

    @app.get("/health", response_model=HealthResponse, tags=["ops"])
    def health(request: Request) -> HealthResponse:
        """Liveness. Deliberately the one endpoint that answers without a key.

        A load balancer or a person checking whether the platform is up should
        not need a credential, and an operator who forgot to configure keys has
        to be able to SEE that -- which is why `auth` is always reported.

        The operational counts are withheld from anonymous callers: `leased`
        staying high with nothing finishing means Workers are dying mid-run,
        which is exactly the sort of thing to tell an operator and not a
        stranger.
        """
        try:
            principal = identify(request)
        except Exception:  # noqa: BLE001 - any failure just means "not privileged"
            return HealthResponse(status="ok", auth=auth.mode.value)

        # Reachability is checked only for a privileged caller: it is one round
        # trip to the database, and an anonymous endpoint that performs one is a
        # free amplifier for anybody who wants to make the platform busy.
        db_ok = None if identity is None else _db_reachable()
        backend = ("chained" if identity is not None
                   else "disabled" if not auth.enforced else "env")

        try:
            return HealthResponse(
                # A database the platform NEEDS and cannot reach is degraded,
                # even though the API itself is answering: every authenticated
                # request is about to 503, and an operator needs to see the
                # cause rather than infer it from a wall of failures.
                status="ok" if db_ok is not False else "degraded",
                auth=auth.mode.value,
                scopes=principal.scope_names,
                queue=type(queue).__name__,
                queue_ok=True,
                queued=queue.depth(),
                leased=queue.inflight(),
                auth_backend=backend,
                db_ok=db_ok,
            )
        except QueueError:
            return HealthResponse(
                status="degraded",
                auth=auth.mode.value,
                scopes=principal.scope_names,
                queue=type(queue).__name__,
                queue_ok=False,
            )

    @app.get("/metrics", response_model=MetricsResponse, tags=["ops"])
    def metrics(
        reg: RunRegistry = Depends(reader),
        window: int = Query(
            METRICS_WINDOW, ge=1, le=5000,
            description="how many recent runs to roll up; bounds the cost as history grows",
        ),
    ) -> MetricsResponse:
        """Platform rollup: run counts by status and by collector, over a
        bounded newest-first window, plus the live queue depth. Read-only and
        derived -- it holds no state of its own.

        Rolled up over the CALLER'S runs: a user sees their own counts, an
        admin or operator the platform's. The queue depth stays global -- it is
        what explains why your own run is still queued, and it names no run.
        """
        m = platform_metrics(reg, window=window, queue=queue)
        return MetricsResponse(
            window=m.window,
            scanned=m.scanned,
            runs_by_status=m.by_status,
            runs_by_actor=m.by_actor,
            active_runs=m.active,
            queue=QueueSnapshot(queued=m.queued, leased=m.leased, ok=m.queue_ok),
        )

    @app.get("/collectors", response_model=CollectorListResponse, tags=["ops"])
    def collectors(
        reg: RunRegistry = Depends(reader),
        window: int = Query(METRICS_WINDOW, ge=1, le=5000),
    ) -> CollectorListResponse:
        """Per-collector health: last run, last success/failure, counts and
        success rate, derived from the same recent window. An Actor that has
        never run in the window simply does not appear.

        Owner-scoped like /metrics: each entry names a last run id, and another
        user's run id is not this caller's to be told.
        """
        entries = [CollectorStatus(**h.as_dict()) for h in collector_health(reg, window=window)]
        return CollectorListResponse(collectors=entries, count=len(entries), window=window)

    return app


def app_from_env():
    """The app `uvicorn api.main:app` gets, configured from the environment.

    Accounts, the console and the login browser all need a database and an
    encryption key. When both are present this wires them in; when they are not
    it builds exactly the app this module has always built -- environment API
    keys, no accounts -- so a single-user deployment is unchanged and nothing
    has to be passed on the command line either way.

    A key that is present but unusable is NOT tolerated. Falling back to "no
    accounts" would leave a console nobody can sign in to and an API answering
    normally, which is a far worse failure than refusing to start.
    """
    import os

    if not (os.environ.get("ACTOR_PLATFORM_DATABASE_URL") or "").strip():
        return create_app()

    from api.users import Identity
    from registry.pg_registry import PgRunRegistry
    from shared.crypto import KeyNotConfigured, KeyRing

    try:
        keyring = KeyRing.from_env()
    except KeyNotConfigured:
        # A database without a key means stored sessions could not be
        # encrypted, which is the one thing this platform must not do quietly.
        raise
    return create_app(registry=PgRunRegistry(), identity=Identity(keyring))


app = app_from_env()
