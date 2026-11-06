#!/usr/bin/env python3
"""Public API v1 integration tests.

Drives the HTTP surface for real. The Worker is always a separate OS process,
because the central claim is that the API and the execution engine are
decoupled -- calling the executor in-process would prove nothing.

Needs Docker. Re-execs into .venv for FastAPI.

    python3 test_api_v1.py
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
from contextlib import contextmanager
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

from api.auth import API_KEYS_ENV, AuthPolicy, generate_key  # noqa: E402
from api.main import create_app  # noqa: E402
from jobqueue.base import QUEUE_URL_ENV, open_queue  # noqa: E402
from registry.run_registry import RUNS_ROOT_ENV, RunRegistry  # noqa: E402

# A SANDBOX, not the shared queue and run store -- following the same reasoning
# test_actor_schemas.py records for the same reason, one scope wider.
#
# This suite purges its queue between tests and runs real jobs through real
# Worker subprocesses. Against the DEFAULT queue that is destructive: run-local
# .sh starts a long-lived Worker on exactly that queue, so running these tests
# beside a live deployment DELETES ITS PENDING WORK -- and, worse, the live
# Worker dequeues this suite's jobs and resolves them against ITS registry
# (Postgres, where they do not exist), so the jobs vanish and every run here
# sits at PENDING until the test times out. Measured: 8 of 12 tests failed that
# way, with nothing in the output pointing at the real cause.
_SANDBOX = Path(tempfile.mkdtemp(prefix="api-v1-tests-"))
atexit.register(shutil.rmtree, _SANDBOX, ignore_errors=True)

REGISTRY = RunRegistry(_SANDBOX / "runs")
QUEUE = open_queue(str(_SANDBOX / "queue"))
# Every suite runs against an ENFORCING policy and sends a real key. Turning
# auth off for the tests would leave the mode nobody actually deploys as the
# only one ever exercised.
API_KEY = generate_key()
AUTH = {"Authorization": f"Bearer {API_KEY}"}
CLIENT = TestClient(
    create_app(registry=REGISTRY, queue=QUEUE, auth=AuthPolicy.for_keys({API_KEY})),
    headers=AUTH,
)
CONSUMER = REPO_ROOT / "worker" / "consumer.py"

TERMINAL = {"SUCCEEDED", "FAILED", "PLATFORM_ERROR"}

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def submit(actor: str, payload: dict | None = None):
    return CLIENT.post(f"/actors/{actor}/runs", json=payload if payload is not None else {})


def _worker_env() -> dict[str, str]:
    """The child's environment, with the database deliberately removed.

    consumer.py picks its registry with open_registry(), which returns the
    Postgres one whenever ACTOR_PLATFORM_DATABASE_URL is set -- ignoring
    --registry-root entirely. The API above is built on a filesystem registry,
    so a Worker that quietly switched backends would write statuses somewhere
    this suite never reads. Inheriting a DSN from the operator's shell is easy
    and would look exactly like the Worker not running at all.
    """
    env = dict(os.environ)
    env.pop("ACTOR_PLATFORM_DATABASE_URL", None)
    return env


def start_worker(max_jobs: int = 1, *, drain: bool = True):
    args = [
        sys.executable,
        str(CONSUMER),
        "--max-jobs",
        str(max_jobs),
        "--poll-timeout",
        "0.2",
        # Point the Worker at the SAME sandbox the API above was built on. A
        # Worker on a different queue never sees these jobs, and one on a
        # different registry cannot resolve them -- both leave every run at
        # PENDING, which is what the flags exist to prevent.
        "--queue-url",
        str(_SANDBOX / "queue"),
        "--registry-root",
        str(_SANDBOX / "runs"),
    ]
    if drain:
        args.append("--drain")
    return subprocess.Popen(
        args, env=_worker_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


@contextmanager
def worker(max_jobs: int = 1, *, drain: bool = True):
    """A Worker that is always reaped: a leaked one steals the next test's job."""
    process = start_worker(max_jobs, drain=drain)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=30)


def run_worker(max_jobs: int = 1) -> None:
    with worker(max_jobs) as process:
        process.wait(timeout=300)


def wait_for_terminal(run_id: str, seconds: float = 180.0) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        doc = CLIENT.get(f"/runs/{run_id}").json()
        if doc["status"] in TERMINAL:
            return doc
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} never finished; it is {doc['status']}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- 1 ----------------------------------------------------------------------


@test
def test_list_actors():
    response = CLIENT.get("/actors")
    assert response.status_code == 200, response.text

    body = response.json()
    names = [a["name"] for a in body["actors"]]
    assert {"hello_world", "item_processor"} <= set(names), names

    # Discovered, not declared: every name must be a real Actor directory.
    # An ALLOWLIST, so a new field is a deliberate contract change rather than
    # something that leaked. `hidden`, `drives_host_browser` and `awaits_human`
    # are PRESENTATION facts a client needs and none reveals anything about the
    # host: the first says do not offer this Actor, the second says where a
    # person goes to solve a challenge it parks on, the third says this Actor
    # can pause and ask a person at all, so a waiting prompt and a resume
    # control may be shown. `limits` is the per-run default and maximum a
    # client may offer (timeout, memory, CPUs): policy the user is meant to
    # see, and nothing about the host's own resources.
    # `label` and `icon` say how to SHOW an Actor (its name for people, its
    # logo); the icon is path data and a colour, never markup.
    allowed = {"name", "description", "input_schema", "schema_error", "hidden",
               "drives_host_browser", "awaits_human", "limits", "label", "icon"}
    for actor in body["actors"]:
        assert set(actor) <= allowed, f"the listing leaks internals: {set(actor) - allowed}"
        assert isinstance(actor["hidden"], bool), f"hidden must be a bool: {actor}"
        icon = actor.get("icon")
        if icon is not None:
            assert set(icon) == {"d", "color"}, f"the icon carries more than drawing data: {icon}"
            assert "<" not in icon["d"] and ">" not in icon["d"], "an icon carried markup"
        # The point is that no filesystem detail escapes, not that the entry is
        # bare: Actors describe themselves with a description and input schema.
        # A path, not any slash. The old check rejected every "/" outside the
        # schema, which a description may legitimately contain (a URL, say);
        # what must never appear is a FILESYSTEM path or a Docker detail.
        rendered = json.dumps({k: v for k, v in actor.items() if k != "input_schema"})
        assert "path" not in actor, f"the catalog exposed a path field: {actor}"
        for leak in (str(REPO_ROOT), "/actors/", "Dockerfile", "docker", "vendor/"):
            assert leak not in rendered, f"a {leak!r} leaked: {rendered[:200]}"
        assert (REPO_ROOT / "actors" / actor["name"] / "Dockerfile").is_file()

    # `hidden` is PRESENTATION, not access control, and the assertion has to be
    # that way round: it tells a human-facing client not to OFFER an Actor, and
    # the listing must keep carrying it -- filtering here would break the two
    # fixtures above, which are hidden AND are what every suite runs. The value
    # must also come from config/actors.json rather than the schema default; a
    # silent fall back to False is how the fixtures end up offered as scrapers.
    by_name = {a["name"]: a for a in body["actors"]}
    for fixture in ("hello_world", "item_processor"):
        assert by_name[fixture]["hidden"] is True, f"{fixture} lost its hidden flag: {by_name[fixture]}"


# --- 2 ----------------------------------------------------------------------


@test
def test_submit_returns_a_run_id_without_executing():
    QUEUE.purge()
    started = time.monotonic()
    response = submit("hello_world", {"name": "Basel"})
    elapsed = time.monotonic() - started

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["run_id"] and body["actor"] == "hello_world"
    assert body["status"] == "PENDING", body
    assert response.headers.get("location") == f"/runs/{body['run_id']}"

    # The decisive evidence is not the clock: the run has no start time, so no
    # container was launched, and the job is still waiting for a Worker.
    detail = CLIENT.get(f"/runs/{body['run_id']}").json()
    assert detail["status"] == "PENDING"
    assert detail["started_at"] is None, "the API executed the Actor - it must only enqueue"
    assert QUEUE.depth() == 1, f"the job was not queued (depth {QUEUE.depth()})"
    assert elapsed < 1.0, f"POST took {elapsed:.2f}s - it looks like it waited"

    run_worker(1)


# --- 3 ----------------------------------------------------------------------


@test
def test_lifecycle_pending_running_succeeded():
    QUEUE.purge()
    # delay_ms holds the run open ~2s so RUNNING cannot slip between two polls.
    run_id = submit("item_processor", {"count": 4, "delay_ms": 500}).json()["run_id"]
    assert CLIENT.get(f"/runs/{run_id}").json()["status"] == "PENDING"

    seen: list[str] = []
    with worker(1) as process:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            status = CLIENT.get(f"/runs/{run_id}").json()["status"]
            if not seen or seen[-1] != status:
                seen.append(status)
            if status in TERMINAL:
                break
            time.sleep(0.02)
        process.wait(timeout=180)

    assert seen == ["PENDING", "RUNNING", "SUCCEEDED"], f"lifecycle was {seen}"

    doc = CLIENT.get(f"/runs/{run_id}").json()
    assert doc["exit_code"] == 0
    assert doc["started_at"] and doc["finished_at"] and doc["created_at"]
    assert doc["attempts"] == 1
    assert doc["error"] is None
    assert "run_dir" not in doc, "the API must not expose filesystem paths"


# --- 4 ----------------------------------------------------------------------


@test
def test_input_output_and_logs():
    QUEUE.purge()
    payload = {"name": "Artifacts"}
    run_id = submit("hello_world", payload).json()["run_id"]
    run_worker(1)
    wait_for_terminal(run_id)

    assert CLIENT.get(f"/runs/{run_id}/input").json() == payload

    output = CLIENT.get(f"/runs/{run_id}/output")
    assert output.status_code == 200
    assert output.json() == {"message": "Hello Artifacts"}

    logs = CLIENT.get(f"/runs/{run_id}/logs")
    assert logs.status_code == 200
    assert logs.headers["content-type"].startswith("text/plain"), logs.headers["content-type"]
    assert "[hello_world]" in logs.text
    try:
        json.loads(logs.text)
    except json.JSONDecodeError:
        pass  # correct: logs are a stream, not a JSON document
    else:
        raise AssertionError("logs must not be wrapped in JSON")


# --- 5 ----------------------------------------------------------------------


@test
def test_array_output_is_returned_as_an_array():
    QUEUE.purge()
    run_id = submit("item_processor", {"count": 5, "fail_at": None}).json()["run_id"]
    run_worker(1)

    doc = wait_for_terminal(run_id)
    assert doc["status"] == "SUCCEEDED", doc

    output = CLIENT.get(f"/runs/{run_id}/output").json()
    assert isinstance(output, list), f"expected an ARRAY, got {type(output).__name__}"
    assert len(output) == 5
    assert output[0]["slug"] == "sample-item-1"


# --- 6 ----------------------------------------------------------------------


@test
def test_actor_failure_is_reported_as_an_actor_failure():
    QUEUE.purge()
    run_id = submit("item_processor", {"count": 5, "fail_at": 3}).json()["run_id"]
    run_worker(1)

    doc = wait_for_terminal(run_id)
    assert doc["status"] == "FAILED", doc
    assert doc["status"] != "PLATFORM_ERROR", "an Actor failure is not a platform failure"
    assert doc["exit_code"] == 1, doc
    assert doc["error"], "a failed run must record why"

    missing = CLIENT.get(f"/runs/{run_id}/output")
    assert missing.status_code == 404
    assert missing.json()["status"] == "FAILED", "the 404 must explain itself"

    logs = CLIENT.get(f"/runs/{run_id}/logs").text
    assert "partial progress: 2 of 5" in logs, "the Actor's own diagnosis must survive"
    assert logs.strip().splitlines()[-1].startswith("[item_processor] ERROR")


# --- 7 ----------------------------------------------------------------------


@test
def test_invalid_actor_name_is_rejected():
    QUEUE.purge()
    before = len(REGISTRY.list(limit=100))

    response = submit("_broken_probe", {})
    assert response.status_code == 400, response.text
    assert response.json()["error"] == "invalid actor name"

    assert QUEUE.depth() == 0, "an invalid name must never reach the queue"
    assert len(REGISTRY.list(limit=100)) == before, "no run record may be created"


# --- 8 ----------------------------------------------------------------------


@test
def test_unknown_actor_is_a_different_failure():
    QUEUE.purge()
    before = len(REGISTRY.list(limit=100))

    response = submit("facebook-scraper", {})
    assert response.status_code == 404, response.text
    assert response.json()["error"] == "unknown actor"
    # A well-formed name that is not installed is NOT the same as a bad name.
    assert response.json()["error"] != "invalid actor name"

    assert QUEUE.depth() == 0
    assert len(REGISTRY.list(limit=100)) == before


# --- 9 ----------------------------------------------------------------------


@test
def test_ten_rapid_submissions_all_complete():
    QUEUE.purge()
    count = 10

    run_ids = []
    for index in range(count):
        response = submit("hello_world", {"name": f"Rapid{index}"})
        assert response.status_code == 202, response.text
        run_ids.append(response.json()["run_id"])

    assert len(set(run_ids)) == count, "run ids collided"
    assert QUEUE.depth() == count, f"expected {count} queued, got {QUEUE.depth()}"

    run_worker(count)

    # No ordering assumed: only that every run finishes with its own result.
    for index, run_id in enumerate(run_ids):
        doc = wait_for_terminal(run_id)
        assert doc["status"] == "SUCCEEDED", f"{run_id}: {doc}"
        assert CLIENT.get(f"/runs/{run_id}/output").json() == {"message": f"Hello Rapid{index}"}, (
            "a run was executed with another run's input"
        )

    # Scoped to THIS batch, not to global emptiness: the Worker's startup
    # reconcile sweep can legitimately requeue an unrelated stranded run, and a
    # test asserting the whole queue is empty would then fail on correct
    # platform behaviour. It happened.
    left = QUEUE.known_run_ids() & set(run_ids)
    assert not left, f"jobs from this batch were never acknowledged: {left}"

    listed = CLIENT.get(f"/runs?limit={count}").json()
    assert listed["count"] == count
    ids = [row["run_id"] for row in listed["runs"]]
    assert ids == sorted(ids, reverse=True), "the Registry's newest-first ordering must hold"


# --- 10 ---------------------------------------------------------------------


@test
def test_the_api_works_with_no_worker_running():
    QUEUE.purge()
    response = submit("hello_world", {"name": "Deferred"})
    assert response.status_code == 202, "the API must not depend on a Worker being up"
    run_id = response.json()["run_id"]

    # Long enough that a coupled design would certainly have finished.
    time.sleep(3)

    doc = CLIENT.get(f"/runs/{run_id}").json()
    assert doc["status"] == "PENDING", f"something executed the run with no Worker: {doc}"
    assert doc["started_at"] is None
    assert QUEUE.depth() == 1, "the job left the queue with no Worker running"
    assert CLIENT.get("/health").json()["queued"] == 1

    run_worker(1)

    doc = wait_for_terminal(run_id)
    assert doc["status"] == "SUCCEEDED", doc
    assert CLIENT.get(f"/runs/{run_id}/output").json() == {"message": "Hello Deferred"}


# --- the architectural rule -------------------------------------------------


@test
def test_submitting_a_slow_actor_still_returns_immediately():
    """FastAPI must not become the execution engine.

    The Actor here takes ~6s. If the request were doing the work, the response
    could not possibly beat it.
    """
    QUEUE.purge()
    slow = {"count": 12, "delay_ms": 500}

    started = time.monotonic()
    response = submit("item_processor", slow)
    elapsed = time.monotonic() - started
    run_id = response.json()["run_id"]

    assert response.status_code == 202
    assert elapsed < 1.0, f"POST took {elapsed:.2f}s for a ~6s Actor - the API is executing it"
    assert CLIENT.get(f"/runs/{run_id}").json()["started_at"] is None

    with worker(1) as process:
        doc = wait_for_terminal(run_id)
        process.wait(timeout=180)

    assert doc["status"] == "SUCCEEDED"
    assert doc["duration_seconds"] > elapsed * 3, (
        f"the run took {doc['duration_seconds']}s but the request returned in "
        f"{elapsed:.2f}s - the work happened outside the request, as it must"
    )


# --- over a real server -----------------------------------------------------


@test
def test_end_to_end_over_real_http():
    """The same flow through uvicorn and a socket, not the in-process client."""
    QUEUE.purge()
    port = free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api.main:app", "--port", str(port),
         "--log-level", "warning"],
        cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        # The sandbox, by environment: `uvicorn api.main:app` takes no
        # arguments, so this is the only way to keep a REAL server off the
        # shared queue and run store that a live deployment is using.
        env={
            **_worker_env(),
            API_KEYS_ENV: API_KEY,
            QUEUE_URL_ENV: str(_SANDBOX / "queue"),
            RUNS_ROOT_ENV: str(_SANDBOX / "runs"),
        },
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

        assert "hello_world" in [a["name"] for a in httpx.get(f"{base}/actors", headers=AUTH).json()["actors"]]

        response = httpx.post(f"{base}/actors/hello_world/runs", json={"name": "OverHTTP"}, headers=AUTH)
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        assert httpx.get(f"{base}/runs/{run_id}", headers=AUTH).json()["status"] == "PENDING"

        run_worker(1)

        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            doc = httpx.get(f"{base}/runs/{run_id}", headers=AUTH).json()
            if doc["status"] in TERMINAL:
                break
            time.sleep(0.05)

        assert doc["status"] == "SUCCEEDED", doc
        assert httpx.get(f"{base}/runs/{run_id}/output", headers=AUTH).json() == {"message": "Hello OverHTTP"}
        assert "[hello_world]" in httpx.get(f"{base}/runs/{run_id}/logs", headers=AUTH).text
    finally:
        server.terminate()
        server.wait(timeout=30)


def main() -> int:
    failures = 0
    print(f"running {len(TESTS)} API v1 tests\n")

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
