#!/usr/bin/env python3
"""API key authentication tests.

The platform is an execution engine reachable over HTTP: anything that can call
it can start containers. These cover the three auth modes, the guard on every
endpoint, and the handful of ways this kind of thing is usually got wrong.

Offline apart from one real-server test.

    python3 test_auth.py
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
VENV = REPO_ROOT / ".venv"
VENV_PYTHON = VENV / "bin" / "python"

if (
    VENV_PYTHON.is_file()
    and Path(sys.prefix).resolve() != VENV.resolve()
    and not os.environ.get("_ACTOR_PLATFORM_REEXEC")
):
    os.environ["_ACTOR_PLATFORM_REEXEC"] = "1"
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api.auth import (  # noqa: E402
    ALL_SCOPES,
    API_KEYS_ENV,
    AUTH_MODE_ENV,
    AuthMode,
    AuthPolicy,
    Scope,
    generate_key,
    parse_scopes,
)
from api.main import create_app  # noqa: E402
from jobqueue.base import open_queue  # noqa: E402
from registry.run_registry import RunRegistry  # noqa: E402
from shared.models import RunStatus  # noqa: E402

_SANDBOX = Path(tempfile.mkdtemp(prefix="actor-auth-tests-"))
atexit.register(shutil.rmtree, _SANDBOX, ignore_errors=True)

REGISTRY = RunRegistry(_SANDBOX / "runs")
QUEUE = open_queue(str(_SANDBOX / "queue"))

KEY = generate_key()
OTHER_KEY = generate_key()
AUTH = {"Authorization": f"Bearer {KEY}"}

# Endpoints that answer without a credential, and why.
PUBLIC_PATHS = {
    "/",                # the CONSOLE shell: public like a login page, because
                        # it IS the login page. It carries no data and no
                        # credential; every data call its JS makes is auth-gated.
    "/ops",             # the operator dashboard shell, public for the same
                        # reason and asserted in test_ui.py.
    "/health",          # liveness: a probe must not need a key
    "/openapi.json",    # the API's shape, which is not data
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
}

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def client(policy: AuthPolicy, **kwargs) -> TestClient:
    return TestClient(create_app(registry=REGISTRY, queue=QUEUE, auth=policy), **kwargs)


ENFORCING = AuthPolicy.for_keys({KEY, OTHER_KEY})

# Keys that can do exactly one thing each.
READ_KEY = generate_key()
RUN_KEY = generate_key()
SCOPED = AuthPolicy(
    entries=(
        (READ_KEY, frozenset({Scope.READ})),
        (RUN_KEY, frozenset({Scope.RUN})),
        (KEY, ALL_SCOPES),
    ),
    mode=AuthMode.ENFORCED,
)

READ_ONLY = {"Authorization": f"Bearer {READ_KEY}"}
RUN_ONLY = {"Authorization": f"Bearer {RUN_KEY}"}

# Every read endpoint, and the one write.
READ_URLS = [
    "/actors",
    "/runs",
    "/runs/20260101-000001-aaaaaa",
    "/runs/20260101-000001-aaaaaa/input",
    "/runs/20260101-000001-aaaaaa/output",
    "/runs/20260101-000001-aaaaaa/logs",
]
RUN_URL = "/actors/hello_world/runs"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- the three modes --------------------------------------------------------


@test
def test_unconfigured_refuses_everything_but_says_so():
    """Forgetting to configure keys must fail loudly, not serve openly."""
    c = client(AuthPolicy())

    response = c.get("/actors")
    assert response.status_code == 503, response.text
    assert response.json()["error"] == "authentication not configured"
    # 503 rather than 401: no key would help, so it is the platform's fault.
    assert response.status_code != 401

    # And an operator can still see WHY, without a credential they do not have.
    health = c.get("/health").json()
    assert health["status"] == "ok"
    assert health["auth"] == "unconfigured"
    assert API_KEYS_ENV in c.get("/actors").json()["detail"], "the fix must be named"


@test
def test_enforced_requires_a_valid_key():
    c = client(ENFORCING)

    anonymous = c.get("/actors")
    assert anonymous.status_code == 401, anonymous.text
    assert anonymous.json()["error"] == "authentication required"

    wrong = c.get("/actors", headers={"Authorization": "Bearer apk_not-a-real-key"})
    assert wrong.status_code == 401
    assert wrong.json()["error"] == "invalid api key"

    assert c.get("/actors", headers=AUTH).status_code == 200


@test
def test_disabled_is_open_and_admits_it():
    c = client(AuthPolicy.disabled())
    assert c.get("/actors").status_code == 200, "an explicit opt-out must actually open it"
    assert c.get("/health").json()["auth"] == "disabled"


@test
def test_keys_beat_the_opt_out():
    """Configuring both is a mistake; the safe reading of a mistake is stricter."""
    policy = AuthPolicy.from_env({API_KEYS_ENV: KEY, AUTH_MODE_ENV: "off"})
    assert policy.mode is AuthMode.ENFORCED
    assert client(policy).get("/actors").status_code == 401


# --- how the credential is presented ---------------------------------------


@test
def test_only_a_bearer_header_is_accepted():
    c = client(ENFORCING)
    for header, why in (
        (KEY, "a bare key with no scheme"),
        (f"Basic {KEY}", "the wrong scheme"),
        (f"Token {KEY}", "another wrong scheme"),
        ("Bearer", "a scheme with no credential"),
        ("Bearer ", "a scheme with an empty credential"),
    ):
        response = c.get("/actors", headers={"Authorization": header})
        assert response.status_code == 401, f"{why} was accepted"


@test
def test_the_scheme_is_case_insensitive():
    """RFC 7235 says the scheme is case-insensitive; clients vary."""
    c = client(ENFORCING)
    for scheme in ("Bearer", "bearer", "BEARER", "BeArEr"):
        assert c.get("/actors", headers={"Authorization": f"{scheme} {KEY}"}).status_code == 200


@test
def test_a_key_in_the_query_string_is_not_accepted():
    """Query strings end up in access logs, browser history and referrers."""
    c = client(ENFORCING)
    assert c.get(f"/actors?token={KEY}").status_code == 401
    assert c.get(f"/actors?api_key={KEY}").status_code == 401


@test
def test_every_configured_key_works_independently():
    c = client(ENFORCING)
    for key in (KEY, OTHER_KEY):
        assert c.get("/actors", headers={"Authorization": f"Bearer {key}"}).status_code == 200


# --- the guard covers everything -------------------------------------------


@test
def test_every_endpoint_is_guarded_unless_deliberately_public():
    """The test that survives someone adding an endpoint later.

    A route added without the auth dependency is an open door, and nothing else
    here would notice. This walks the app's own route table instead of a list
    someone has to remember to update.
    """
    app = create_app(registry=REGISTRY, queue=QUEUE, auth=ENFORCING)
    c = TestClient(app)

    checked = 0
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", set()) or set()
        if not path or path in PUBLIC_PATHS:
            continue

        url = path.replace("{actor_name}", "hello_world").replace(
            "{run_id}", "20260101-000001-aaaaaa"
        )
        for method in sorted(methods & {"GET", "POST"}):
            response = c.request(method, url, json={} if method == "POST" else None)
            assert response.status_code == 401, (
                f"{method} {path} answered {response.status_code} without a key - it is unguarded"
            )
            checked += 1

    assert checked >= 7, f"only {checked} endpoints were checked; the walk found too few"


@test
def test_health_is_public_but_withholds_operational_detail():
    c = client(ENFORCING)

    anonymous = c.get("/health").json()
    assert anonymous["status"] == "ok", "liveness must not need a key"
    assert anonymous["auth"] == "enforced"
    assert anonymous["queued"] is None, "queue depth was handed to an anonymous caller"
    assert anonymous["leased"] is None
    assert anonymous["queue"] is None

    privileged = c.get("/health", headers=AUTH).json()
    assert privileged["queued"] is not None and privileged["leased"] is not None
    assert privileged["queue_ok"] is True


# --- the security-relevant consequences ------------------------------------


@test
def test_an_unauthenticated_submission_starts_nothing():
    """A 401 must not leave a run or a job behind."""
    QUEUE.purge()
    before = len(REGISTRY.list(limit=200))
    c = client(ENFORCING)

    response = c.post("/actors/hello_world/runs", json={"name": "Intruder"})
    assert response.status_code == 401

    assert QUEUE.depth() == 0, "an unauthenticated request queued work"
    assert len(REGISTRY.list(limit=200)) == before, "an unauthenticated request created a run"


@test
def test_a_rejected_key_is_never_echoed_back():
    """A rejection that quotes the credential writes it into logs and trackers."""
    c = client(ENFORCING)
    secret = "apk_this-value-must-not-come-back"

    response = c.get("/actors", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 401

    body = json.dumps(response.json())
    assert secret not in body, f"the rejected key was echoed: {body}"
    assert secret not in json.dumps(dict(response.headers))
    # Nor should a VALID key ever appear in a response.
    assert KEY not in body


@test
def test_keys_are_compared_in_constant_time():
    """`==` on a secret leaks it one byte at a time through timing.

    Asserted structurally: a timing measurement would be flaky, while the
    presence of the right primitive is not.
    """
    source = (REPO_ROOT / "api" / "auth.py").read_text()
    assert "secrets.compare_digest" in source, "key comparison must be constant-time"
    assert "== key" not in source and "key ==" not in source

    policy = AuthPolicy.for_keys({KEY})
    assert policy.matches(KEY)
    assert not policy.matches(KEY[:-1])          # a prefix must not pass
    assert not policy.matches(KEY + "x")
    assert not policy.matches("")


@test
def test_generated_keys_are_strong_and_identifiable():
    keys = {generate_key() for _ in range(200)}
    assert len(keys) == 200, "generated keys collided"
    for key in list(keys)[:5]:
        assert key.startswith("apk_"), "a leaked key should be recognisable as one"
        assert len(key) > 40


# --- scopes -----------------------------------------------------------------


@test
def test_a_read_only_key_cannot_start_a_run():
    """The distinction scopes exist for: watching is not operating."""
    QUEUE.purge()
    before = len(REGISTRY.list(limit=200))
    c = client(SCOPED)

    assert c.get("/actors", headers=READ_ONLY).status_code == 200

    refused = c.post(RUN_URL, json={"name": "Observer"}, headers=READ_ONLY)
    assert refused.status_code == 403, refused.text

    body = refused.json()
    assert body["error"] == "insufficient scope"
    assert body["required_scope"] == "run"
    assert body["granted_scopes"] == ["read"], body

    assert QUEUE.depth() == 0, "a read-only key queued work"
    assert len(REGISTRY.list(limit=200)) == before, "a read-only key created a run"


@test
def test_a_run_only_key_cannot_read():
    """Submitting is not the same as being allowed to see the results."""
    c = client(SCOPED)

    accepted = c.post(RUN_URL, json={"name": "Submitter"}, headers=RUN_ONLY)
    assert accepted.status_code == 202, accepted.text
    run_id = accepted.json()["run_id"]

    for url in ("/actors", "/runs", f"/runs/{run_id}", f"/runs/{run_id}/output"):
        response = c.get(url, headers=RUN_ONLY)
        assert response.status_code == 403, f"{url} answered {response.status_code}"
        assert response.json()["required_scope"] == "read"

    QUEUE.purge()
    REGISTRY.complete(run_id, status=RunStatus.PLATFORM_ERROR, error="dequeued by the test")


@test
def test_every_endpoint_declares_the_scope_it_needs():
    """A read key must reach every read endpoint and no write one.

    Walks the same routes the guard test walks, so an endpoint given the wrong
    scope is caught rather than assumed correct.
    """
    c = client(SCOPED)

    for url in READ_URLS:
        response = c.get(url, headers=READ_ONLY)
        # 404 is fine -- the run id is a placeholder. 403 is not: it would mean
        # a read endpoint is demanding something other than `read`.
        assert response.status_code != 403, f"{url} refused a read key"
        assert response.status_code != 401, f"{url} refused a valid key"

    assert c.post(RUN_URL, json={}, headers=READ_ONLY).status_code == 403

    # And the all-scopes key reaches everything.
    for url in READ_URLS:
        assert c.get(url, headers=AUTH).status_code != 403


@test
def test_a_key_with_both_scopes_can_do_both():
    QUEUE.purge()
    c = client(SCOPED)

    assert c.get("/actors", headers=AUTH).status_code == 200
    submitted = c.post(RUN_URL, json={"name": "Both"}, headers=AUTH)
    assert submitted.status_code == 202

    run_id = submitted.json()["run_id"]
    assert c.get(f"/runs/{run_id}", headers=AUTH).status_code == 200

    QUEUE.purge()
    REGISTRY.complete(run_id, status=RunStatus.PLATFORM_ERROR, error="dequeued by the test")


@test
def test_a_key_with_no_scope_suffix_gets_everything():
    """Backward compatibility: this is what every key granted before scopes."""
    policy = AuthPolicy.from_env({API_KEYS_ENV: KEY})
    assert policy.scopes_for(KEY) == ALL_SCOPES

    c = client(policy)
    assert c.get("/actors", headers=AUTH).status_code == 200
    QUEUE.purge()
    submitted = c.post(RUN_URL, json={"name": "Legacy"}, headers=AUTH)
    assert submitted.status_code == 202, "adding scopes must not narrow an existing key"
    QUEUE.purge()
    REGISTRY.complete(
        submitted.json()["run_id"], status=RunStatus.PLATFORM_ERROR, error="dequeued by the test"
    )


@test
def test_scopes_are_parsed_from_the_environment():
    policy = AuthPolicy.from_env(
        {API_KEYS_ENV: "apk_reader:read, apk_both:read+run ,apk_star:*,apk_bare"}
    )
    assert policy.scopes_for("apk_reader") == frozenset({Scope.READ})
    assert policy.scopes_for("apk_both") == ALL_SCOPES
    assert policy.scopes_for("apk_star") == ALL_SCOPES, "'*' must mean everything"
    assert policy.scopes_for("apk_bare") == ALL_SCOPES
    assert policy.scopes_for("apk_unknown") is None


@test
def test_an_unknown_scope_is_refused_not_ignored():
    """A typo must not quietly produce a key with less access than intended."""
    for text in ("admin", "read+delete", "READ+bogus"):
        try:
            parse_scopes(text)
        except ValueError as exc:
            assert "valid scopes are" in str(exc), exc
        else:
            raise AssertionError(f"{text!r} was accepted as a scope")

    try:
        AuthPolicy.from_env({API_KEYS_ENV: "apk_x:admin"})
    except ValueError:
        pass
    else:
        raise AssertionError("a bad scope in the environment must stop the platform starting")


@test
def test_insufficient_scope_is_403_not_401():
    """The two rejections mean different things and must not be conflated."""
    c = client(SCOPED)

    unknown_key = c.post(RUN_URL, json={}, headers={"Authorization": "Bearer apk_nope"})
    assert unknown_key.status_code == 401, "an unknown key is an authentication failure"

    known_key = c.post(RUN_URL, json={"name": "x"}, headers=READ_ONLY)
    assert known_key.status_code == 403, "a known key without the scope is an authorisation failure"

    # A 403 must not leak the credential either.
    assert READ_KEY not in json.dumps(known_key.json())


@test
def test_disabled_auth_grants_every_scope():
    c = client(AuthPolicy.disabled())
    QUEUE.purge()
    assert c.get("/actors").status_code == 200
    submitted = c.post(RUN_URL, json={"name": "Open"})
    assert submitted.status_code == 202, "an opt-out must not leave writes blocked"
    QUEUE.purge()
    REGISTRY.complete(
        submitted.json()["run_id"], status=RunStatus.PLATFORM_ERROR, error="dequeued by the test"
    )


@test
def test_health_reports_what_the_key_may_do():
    c = client(SCOPED)
    assert c.get("/health", headers=READ_ONLY).json()["scopes"] == ["read"]
    assert c.get("/health", headers=RUN_ONLY).json()["scopes"] == ["run"]
    assert c.get("/health", headers=AUTH).json()["scopes"] == ["read", "run"]
    assert c.get("/health").json()["scopes"] is None, "anonymous callers have no scopes"


# --- over a real server -----------------------------------------------------


@test
def test_authentication_holds_over_real_http():
    """Through uvicorn and a socket, configured the way an operator would."""
    port = free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api.main:app", "--port", str(port),
         "--log-level", "warning"],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, API_KEYS_ENV: KEY},
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.25)
        else:
            raise AssertionError("the API server never came up")

        assert httpx.get(f"{base}/health").json()["auth"] == "enforced"
        assert httpx.get(f"{base}/actors").status_code == 401
        assert httpx.get(f"{base}/actors", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert httpx.get(f"{base}/actors", headers=AUTH).status_code == 200

        blocked = httpx.post(f"{base}/actors/hello_world/runs", json={"name": "Intruder"})
        assert blocked.status_code == 401, "an unauthenticated run was accepted over the wire"
    finally:
        server.terminate()
        server.wait(timeout=30)


def main() -> int:
    failures = 0
    print(f"running {len(TESTS)} authentication tests\n")

    for fn in TESTS:
        name = fn.__name__.removeprefix("test_")
        try:
            fn()
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"  FAIL  {name}")
            print("        " + traceback.format_exc().strip().replace("\n", "\n        "))
        else:
            print(f"  ok    {name}")

    QUEUE.purge()
    print()
    print(f"{failures} of {len(TESTS)} tests FAILED" if failures else f"all {len(TESTS)} tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
