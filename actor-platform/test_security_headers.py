#!/usr/bin/env python3
"""Browser-hardening headers and the cookie's Secure flag (review M-05, H-05).

    python3 test_security_headers.py

Offline: no database, no Docker. The cookie rules are tested on the functions
that decide them, because the console's cookie is only issued with a database.

  * every response carries CSP / frame / nosniff / referrer / permissions
  * HSTS only over HTTPS
  * only /ops and the noVNC viewers may run inline script
  * a route's own CSP (the artifact sandbox) is never overwritten
  * Secure follows the request's scheme, and the env var still overrides it
  * in public mode, plain-HTTP sign-in is refused except from loopback
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent
VENV = REPO_ROOT / ".venv"
VENV_PYTHON = VENV / "bin" / "python"
if (VENV_PYTHON.is_file() and Path(sys.prefix).resolve() != VENV.resolve()
        and not os.environ.get("_ACTOR_PLATFORM_REEXEC")):
    os.environ["_ACTOR_PLATFORM_REEXEC"] = "1"
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])
sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from api.auth import AuthPolicy, generate_key  # noqa: E402
from api.main import create_app  # noqa: E402
from jobqueue.base import open_queue  # noqa: E402
from registry.run_registry import RunRegistry  # noqa: E402

KEY = generate_key()
REG = RunRegistry(Path(tempfile.mkdtemp(prefix="actor-headers-")) / "runs")
APP = create_app(registry=REG, queue=open_queue(str(REG.root.parent / "queue")),
                 auth=AuthPolicy.for_keys({KEY}))
HTTP = TestClient(APP)
HTTPS = TestClient(APP, base_url="https://testserver")
AUTHED = {"Authorization": f"Bearer {KEY}"}
TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


@test
def test_every_response_is_hardened():
    for path in ("/", "/ops", "/health", "/actors", "/does-not-exist"):
        r = HTTP.get(path, headers=AUTHED)
        h = r.headers
        csp = h.get("content-security-policy", "")
        assert "frame-ancestors 'self'" in csp, (path, csp)
        assert "object-src 'none'" in csp, (path, csp)
        assert h.get("x-frame-options") == "SAMEORIGIN", (path, dict(h))
        assert h.get("x-content-type-options") == "nosniff", path
        assert h.get("referrer-policy") == "strict-origin-when-cross-origin", path
        assert "camera=()" in h.get("permissions-policy", ""), path


@test
def test_hsts_only_over_https():
    assert "strict-transport-security" not in HTTP.get("/health").headers
    hsts = HTTPS.get("/health").headers.get("strict-transport-security", "")
    assert hsts.startswith("max-age=31536000"), hsts


@test
def test_only_the_pages_that_need_it_may_run_inline_script():
    console = HTTP.get("/").headers["content-security-policy"]
    assert "script-src 'self';" in console, console
    assert "unsafe-inline" not in console.split("script-src")[1].split(";")[0], console
    ops = HTTP.get("/ops").headers["content-security-policy"]
    assert "script-src 'self' 'unsafe-inline'" in ops, ops


@test
def test_websockets_are_allowed_back_to_this_host_only():
    csp = HTTP.get("/", headers={"host": "platform.example.com"}).headers["content-security-policy"]
    assert "ws://platform.example.com" in csp and "wss://platform.example.com" in csp, csp
    assert " ws: " not in csp and "wss: " not in csp, f"a wildcard websocket source: {csp}"
    # A hostile Host header cannot inject policy.
    evil = HTTP.get("/", headers={"host": "x; script-src *"}).headers["content-security-policy"]
    assert "script-src *" not in evil, evil


@test
def test_a_routes_own_csp_wins():
    from api.security_headers import SecurityHeaders

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-security-policy", b"sandbox")]})
        await send({"type": "http.response.body", "body": b""})

    r = TestClient(SecurityHeaders(app)).get("/runs/x/artifacts/report.html")
    assert r.headers["content-security-policy"] == "sandbox", r.headers
    assert r.headers.get("x-content-type-options") == "nosniff"


def _request(scheme: str, host: str):
    return SimpleNamespace(url=SimpleNamespace(scheme=scheme), client=SimpleNamespace(host=host))


class env:
    def __init__(self, **values):
        self.values = values

    def __enter__(self):
        self.saved = {k: os.environ.get(k) for k in self.values}
        for k, v in self.values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@test
def test_the_cookie_is_secure_whenever_the_request_was_https():
    from api.accounts import _secure_cookies

    with env(ACTOR_PLATFORM_SECURE_COOKIES=None):
        assert _secure_cookies(_request("https", "203.0.113.5")) is True
        assert _secure_cookies(_request("http", "127.0.0.1")) is False
    with env(ACTOR_PLATFORM_SECURE_COOKIES="1"):
        assert _secure_cookies(_request("http", "127.0.0.1")) is True
    with env(ACTOR_PLATFORM_SECURE_COOKIES="0"):
        assert _secure_cookies(_request("https", "203.0.113.5")) is False


@test
def test_public_mode_refuses_plain_http_sign_in():
    from api.accounts import _require_https
    from api.main import PlatformError

    def refused(request) -> bool:
        try:
            _require_https(request)
            return False
        except PlatformError as exc:
            assert exc.status_code == 400 and exc.body["error"] == "https required"
            return True

    with env(ACTOR_PLATFORM_REQUIRE_HTTPS=None, ACTOR_PLATFORM_ALLOW_INSECURE_HTTP=None):
        assert not refused(_request("http", "203.0.113.5")), "refused outside public mode"
    with env(ACTOR_PLATFORM_REQUIRE_HTTPS="1", ACTOR_PLATFORM_ALLOW_INSECURE_HTTP=None):
        assert refused(_request("http", "203.0.113.5")), "plain http accepted in public mode"
        assert not refused(_request("https", "203.0.113.5"))
        assert not refused(_request("http", "127.0.0.1")), "the operator's own box was refused"
    with env(ACTOR_PLATFORM_REQUIRE_HTTPS="1", ACTOR_PLATFORM_ALLOW_INSECURE_HTTP="1"):
        assert not refused(_request("http", "203.0.113.5"))


def main() -> int:
    failed = 0
    print(f"running {len(TESTS)} security-header tests\n")
    for fn in TESTS:
        name = fn.__name__.removeprefix("test_")
        try:
            fn()
            print(f"  ok    {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{failed} FAILED" if failed else "\nall passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
