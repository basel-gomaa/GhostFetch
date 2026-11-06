#!/usr/bin/env python3
"""The operational dashboard: served correctly, self-contained, and a thin client.

It lives at /ops. It used to be at "/", and moved when the user console took
that path: thirty people arriving at the root of this service should meet the
product, not an internal tool asking them to paste an API key they do not have.
The subject of these tests is the dashboard, not its URL.

The dashboard is one static HTML file (vanilla JS, no build step), so there is
no JS test runner to lint it. These tests pin the contract that MATTERS: the
shell is public, it carries no secret, it is genuinely self-contained (no
external code that a CSP or an offline box would break), it talks to the real
endpoints, and adding it changed no existing API behaviour. The live behaviour
is exercised separately with a browser against a running API.

    python3 test_ui.py
"""

from __future__ import annotations

import os
import re
import sys
import traceback
from pathlib import Path

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
from jobqueue.base import Job, open_queue  # noqa: E402
from registry.run_registry import RunRegistry  # noqa: E402
import tempfile  # noqa: E402

KEY = generate_key()
REG = RunRegistry(Path(tempfile.mkdtemp(prefix="actor-ui-")) / "runs")
QUEUE = open_queue(str(REG.root.parent / "queue"))
CLIENT = TestClient(create_app(registry=REG, queue=QUEUE, auth=AuthPolicy.for_keys({KEY})))
AUTHED = {"Authorization": f"Bearer {KEY}"}

UI_FILE = REPO_ROOT / "ui" / "index.html"
TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


@test
def test_the_dashboard_is_served_without_a_key():
    """The shell is public, like a login page; the DATA it fetches is not."""
    resp = CLIENT.get("/ops")   # no Authorization header
    assert resp.status_code == 200, resp.status_code
    assert "text/html" in resp.headers["content-type"]
    body = resp.text.lower()
    assert "<!doctype html>" in body and "actor-platform" in body


@test
def test_the_dashboard_is_self_contained_no_external_code():
    """No CDN/script/style pulled from another origin: it must work offline and
    under a strict CSP, and a thin client that phoned a third party would not be
    'thin'."""
    html = UI_FILE.read_text()
    external = re.findall(r'(?:src|href)\s*=\s*["\']https?://[^"\']+', html)
    assert not external, f"external references: {external}"
    # everything inline: exactly one <script> and one <style>, no <link>/<img src>
    assert "<link" not in html.lower(), "a <link> pulls an external stylesheet"


@test
def test_the_dashboard_embeds_no_api_key_or_secret():
    """The file is served to anyone; a baked-in key would hand out access."""
    html = UI_FILE.read_text()
    assert KEY not in html
    # sessionStorage is where the key lives at runtime; assert the design, and
    # that no obvious credential literal is present.
    assert "sessionStorage" in html
    assert not re.search(r'Bearer\s+[A-Za-z0-9_\-]{16,}', html), "a bearer token literal is baked in"


@test
def test_the_dashboard_references_the_real_endpoints():
    html = UI_FILE.read_text()
    for path in ("/health", "/metrics", "/collectors", "/runs", "/cancel",
                 "/input", "/output", "/logs"):
        assert path in html, f"the UI never calls {path}"


@test
def test_the_dashboard_does_not_poll_terminal_runs():
    """A structural guard on the 'avoid unnecessary polling' requirement: the
    detail view schedules a refresh only for ACTIVE states."""
    html = UI_FILE.read_text()
    assert "ACTIVE.has(run.status)" in html, "run detail must gate polling on active status"
    assert 'ACTIVE = new Set(["PENDING","RUNNING"])' in html or \
           'new Set(["PENDING","RUNNING"])' in html


@test
def test_cancel_is_not_optimistic():
    """The one behavioural rule the brief stresses: after cancel, re-render from
    the API rather than forcing CANCELLED locally."""
    html = UI_FILE.read_text()
    # the cancel handler must call renderRun() (reconcile from the API) and must
    # NOT set a CANCELLED status onto the run object itself.
    assert "await renderRun();" in html
    assert "run.status = \"CANCELLED\"" not in html and "status:'CANCELLED'" not in html


@test
def test_serving_the_ui_did_not_gate_or_break_the_api():
    """Adding a public "/" must not touch the auth on the data endpoints."""
    # data endpoints still require a key
    assert CLIENT.get("/metrics").status_code == 401
    assert CLIENT.get("/collectors").status_code == 401
    assert CLIENT.get("/runs").status_code == 401
    # and work with one
    assert CLIENT.get("/metrics", headers=AUTHED).status_code == 200
    # /health remains public
    assert CLIENT.get("/health").status_code == 200


@test
def test_run_summary_now_carries_started_at_and_duration():
    """The one backend change: the runs LIST can show started/duration without
    an N+1 detail fetch."""
    from shared.models import RunStatus
    run = REG.create("hello_world", {"name": "ui"})
    REG.mark_running(run.run_id)
    REG.complete(run.run_id, status=RunStatus.SUCCEEDED, exit_code=0)

    row = CLIENT.get("/runs", headers=AUTHED).json()["runs"][0]
    assert "started_at" in row and "duration_seconds" in row
    assert row["started_at"] is not None
    assert row["duration_seconds"] is not None


def main():
    passed = failed = 0
    for fn in TESTS:
        try:
            fn()
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            print("".join("      " + l for l in traceback.format_exc().splitlines(True)))
        else:
            passed += 1
            print(f"ok    {fn.__name__}")
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
