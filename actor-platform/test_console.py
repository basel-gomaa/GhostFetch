#!/usr/bin/env python3
"""The user console: served correctly, self-contained, and holding no secret.

    python3 test_console.py

The console is a BUILT bundle, unlike the operator dashboard's single hand-
written file, so these check the properties that survive a build rather than
the source: that it is served, that it pulls in no third-party code at runtime,
that it embeds no credential, and that it is a thin client -- every datum it
shows comes from an auth-gated call the browser makes afterwards.

Skipped with a stated reason when the bundle has not been built, because a
missing frontend must not read as a broken backend.
"""

from __future__ import annotations

import re
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from api.auth import AuthPolicy, generate_key  # noqa: E402
from api.main import create_app  # noqa: E402

STATIC = REPO_ROOT / "ui" / "static"
CLIENT = TestClient(create_app(auth=AuthPolicy.for_keys({generate_key()})))

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def bundle() -> str:
    """Every byte of built JavaScript and CSS, concatenated."""
    return "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in sorted((STATIC / "assets").glob("*"))
        if p.suffix in (".js", ".css")
    )


@test
def test_the_console_is_served_at_the_root_without_a_key():
    """It IS the login page. Requiring a credential to see it would be circular."""
    response = CLIENT.get("/")   # no Authorization header
    assert response.status_code == 200, response.status_code
    assert response.headers["content-type"].startswith("text/html")
    assert '<div id="root">' in response.text, response.text[:200]


@test
def test_its_assets_are_served_and_public():
    index = (STATIC / "index.html").read_text()
    referenced = re.findall(r'/console/assets/([A-Za-z0-9._-]+)', index)
    assert referenced, f"the built index references no assets: {index[:200]}"
    for name in referenced:
        response = CLIENT.get(f"/console/assets/{name}")
        assert response.status_code == 200, f"{name} -> {response.status_code}"


@test
def test_the_console_loads_no_external_code():
    """Everything is same-origin. A console that pulled a script from a CDN
    would put a third party inside the page that holds the session cookie."""
    index = (STATIC / "index.html").read_text()
    for pattern in ("http://", "https://", "//cdn", "integrity="):
        assert pattern not in index, f"the console page references {pattern!r}"


@test
def test_the_console_embeds_no_credential():
    """It is a shell. Anything that looks like a key in a public bundle IS one."""
    text = bundle()
    for marker in ("apk_", "sk-ant-", "ACTOR_PLATFORM_SECRET_KEY", "password="):
        assert marker not in text, f"the bundle contains {marker!r}"
    # ...and it never tries to read the session cookie, which is HttpOnly and
    # therefore unreadable anyway -- an attempt would mean someone had
    # misunderstood how this authenticates.
    assert "document.cookie" not in text, "the console reads document.cookie"


@test
def test_the_console_calls_the_real_endpoints():
    text = bundle()
    for path in ("/auth/login", "/auth/signup", "/social/login", "/social/accounts",
                 "/runs", "/actors", "/me/api-keys", "/items", "/admin/users",
                 "/admin/invites", "/auth/signup-options"):
        assert path in text, f"the console never calls {path}"


@test
def test_an_invite_link_keeps_its_token_out_of_server_logs():
    """The token rides in the FRAGMENT, which a browser never sends. A
    `?invite=` query string reached api.log in full (uvicorn's access log),
    where anyone reading the log could have used a pending invite."""
    text = bundle()
    assert "#invite=" in text, "the console no longer builds #invite= links"
    assert "?invite=" not in text, "an invite link puts its token in the query string"


@test
def test_the_console_sends_credentials_same_origin_only():
    """`credentials: same-origin` is what keeps the cookie off any other host if
    a path is ever made absolute by mistake."""
    text = bundle()
    assert "same-origin" in text, "the fetch wrapper does not pin credentials"


@test
def test_the_console_stops_polling_a_finished_run():
    """A page that polls a terminal run for ever is a load generator. The guard
    is the TERMINAL set, which must name every terminal status."""
    text = bundle()
    for status in ("SUCCEEDED", "FAILED", "PLATFORM_ERROR", "CANCELLED"):
        assert status in text, f"the console does not know about {status}"


@test
def test_the_operator_dashboard_is_still_reachable():
    """Moving it off "/" must not have removed it."""
    assert CLIENT.get("/ops").status_code == 200


def main() -> int:
    if not (STATIC / "index.html").is_file():
        print("the console is not built (run `npm install && npm run build` in "
              "ui/console/) - skipping", file=sys.stderr)
        return 0
    print(f"running {len(TESTS)} console tests\n")
    failed = 0
    for fn in TESTS:
        name = fn.__name__.removeprefix("test_")
        try:
            fn()
            print(f"  ok    {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
            print("        " + traceback.format_exc().strip().replace("\n", "\n        ")[:800])
    print(f"\n{'all ' + str(len(TESTS)) + ' passed' if not failed else f'{failed} FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
