#!/usr/bin/env python3
"""API + Queue tests: the asynchronous flow, end to end.

    HTTP -> FastAPI -> Queue -> Worker (separate OS process) -> Docker -> Registry

The Worker is a real subprocess, not a function call, because the claim under
test is that the API and the Worker are DECOUPLED. Running the executor
in-process would prove nothing.

Needs Docker and the venv (FastAPI). Re-execs itself into .venv if started with
another interpreter.

    python3 test_api_queue.py
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
VENV = REPO_ROOT / ".venv"
VENV_PYTHON = VENV / "bin" / "python"

# Re-exec into the venv, which has FastAPI. The test is sys.prefix, NOT the
# path of sys.executable: .venv/bin/python is a symlink to the system
# interpreter, so comparing resolved executables finds them equal and never
# re-execs. The env sentinel stops an exec loop if the venv is somehow broken.
if (
    VENV_PYTHON.is_file()
    and Path(sys.prefix).resolve() != VENV.resolve()
    and not os.environ.get("_ACTOR_PLATFORM_REEXEC")
):
    os.environ["_ACTOR_PLATFORM_REEXEC"] = "1"
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from api.auth import AuthPolicy, generate_key  # noqa: E402
from api.main import create_app  # noqa: E402
from jobqueue.base import open_queue  # noqa: E402
from registry.run_registry import RunRegistry  # noqa: E402
from shared.models import EXIT_ACTOR_FAILED, EXIT_PLATFORM_ERROR, RunStatus, worker_exit_code  # noqa: E402

# Its own registry and queue. These tests assert exact queue depths, so sharing
# storage with other suites makes them fail on other people's residue -- and on
# correct platform behaviour, since a Worker's reconcile sweep legitimately
# adopts stranded runs from anywhere.
_SANDBOX = Path(tempfile.mkdtemp(prefix="actor-queue-tests-"))
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

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def submit(actor: str, payload: dict | None = None):
    return CLIENT.post(f"/actors/{actor}/runs", json=payload if payload is not None else {})


def run_consumer(max_jobs: int = 1, *, background: bool = False, concurrency: int | None = None):
    """Start the Worker as its own OS process."""
    cmd = [
        sys.executable, str(CONSUMER),
        "--queue-url", str(_SANDBOX / "queue"),
        "--registry-root", str(_SANDBOX / "runs"),
        "--max-jobs", str(max_jobs), "--poll-timeout", "0.2",
    ]
    if concurrency is not None:
        cmd += ["--concurrency", str(concurrency)]
    if background:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300)


def wait_for_terminal(run_id: str, seconds: float = 120.0) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        doc = CLIENT.get(f"/runs/{run_id}").json()
        if doc["status"] in {"SUCCEEDED", "FAILED", "PLATFORM_ERROR"}:
            return doc
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} never reached a terminal state: {doc['status']}")


# --- 1 --------------------------------------------------------------------


@test
def test_submit_returns_without_running_the_actor():
    QUEUE.purge()

    started = time.monotonic()
    response = submit("hello_world", {"name": "Basel"})
    elapsed = time.monotonic() - started

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "PENDING", body
    assert body["run_id"]
    assert response.headers.get("location") == f"/runs/{body['run_id']}"

    # The decisive evidence is not the clock: the run has no start time at all,
    # so no container was launched, and the job is still waiting.
    run = REGISTRY.get(body["run_id"])
    assert run.status is RunStatus.PENDING
    assert run.started_at is None, "the API started the Actor - it must only enqueue"
    assert run.exit_code is None
    assert QUEUE.depth() == 1, f"job was not queued (depth {QUEUE.depth()})"

    # Corroborating: a real run takes ~1s including container startup.
    assert elapsed < 1.0, f"POST took {elapsed:.2f}s - it looks like it waited for the Actor"

    run_consumer(1)  # leave the queue clean for the next test


# --- 2 --------------------------------------------------------------------


@test
def test_pending_running_succeeded_observed_through_the_api():
    QUEUE.purge()
    # delay_ms holds the run open for ~2s so RUNNING cannot be missed between
    # two polls. Observing it must not depend on how fast the machine is.
    run_id = submit("item_processor", {"count": 4, "delay_ms": 500}).json()["run_id"]
    assert CLIENT.get(f"/runs/{run_id}").json()["status"] == "PENDING"

    worker = run_consumer(1, background=True)
    seen: list[str] = []
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            status = CLIENT.get(f"/runs/{run_id}").json()["status"]
            if not seen or seen[-1] != status:
                seen.append(status)
            if status in {"SUCCEEDED", "FAILED", "PLATFORM_ERROR"}:
                break
            time.sleep(0.02)
    finally:
        worker.wait(timeout=120)

    assert seen == ["PENDING", "RUNNING", "SUCCEEDED"], f"lifecycle was {seen}"

    doc = CLIENT.get(f"/runs/{run_id}").json()
    assert doc["exit_code"] == 0
    assert doc["started_at"] and doc["finished_at"], doc
    assert "run_dir" not in doc, "the API must not expose filesystem paths"

    assert len(CLIENT.get(f"/runs/{run_id}/output").json()) == 4
    assert CLIENT.get(f"/runs/{run_id}/input").json() == {"count": 4, "delay_ms": 500}
    assert "[item_processor]" in CLIENT.get(f"/runs/{run_id}/logs").text


# --- 3 --------------------------------------------------------------------


@test
def test_second_actor_runs_through_the_same_queue():
    QUEUE.purge()
    run_id = submit("item_processor", {"count": 5, "fail_at": None}).json()["run_id"]
    run_consumer(1)

    doc = wait_for_terminal(run_id)
    assert doc["status"] == "SUCCEEDED", doc
    assert doc["exit_code"] == 0

    output = CLIENT.get(f"/runs/{run_id}/output").json()
    assert isinstance(output, list), f"expected an ARRAY, got {type(output).__name__}"
    assert len(output) == 5
    assert output[0]["slug"] == "sample-item-1", "the Actor's dependency must still run"


# --- 4 --------------------------------------------------------------------


@test
def test_actor_failure_stays_an_actor_failure():
    QUEUE.purge()
    run_id = submit("item_processor", {"count": 5, "fail_at": 3}).json()["run_id"]
    run_consumer(1)

    doc = wait_for_terminal(run_id)
    assert doc["status"] == "FAILED", doc
    assert doc["status"] != "PLATFORM_ERROR", "an Actor failure must not become a platform failure"
    assert doc["exit_code"] == 1, doc

    # The exit-code semantics survive the trip through the queue.
    assert worker_exit_code(RunStatus.FAILED) == EXIT_ACTOR_FAILED == 1
    assert worker_exit_code(RunStatus.PLATFORM_ERROR) == EXIT_PLATFORM_ERROR == 2

    output = CLIENT.get(f"/runs/{run_id}/output")
    assert output.status_code == 404
    assert output.json()["status"] == "FAILED", "404 must explain WHY there is no output"

    logs = CLIENT.get(f"/runs/{run_id}/logs").text
    assert "partial progress: 2 of 5" in logs
    assert logs.strip().splitlines()[-1].startswith("[item_processor] ERROR")


# --- 5 --------------------------------------------------------------------


@test
def test_invalid_actor_name_is_rejected_before_queueing():
    QUEUE.purge()
    before = len(REGISTRY.list(limit=100))

    response = submit("_broken_probe", {})
    assert response.status_code == 400, response.text
    assert response.json()["error"] == "invalid actor name"

    assert QUEUE.depth() == 0, "an invalid name must never reach the queue"
    assert len(REGISTRY.list(limit=100)) == before, "no run record may be created"


# --- 6 --------------------------------------------------------------------


@test
def test_unknown_actor_is_reported_as_unknown():
    QUEUE.purge()
    before = len(REGISTRY.list(limit=100))

    response = submit("facebook-scraper", {})
    assert response.status_code == 404, response.text
    assert response.json()["error"] == "unknown actor"
    # A syntactically valid name is a DIFFERENT failure from an invalid one.
    assert response.json()["error"] != "invalid actor name"

    assert QUEUE.depth() == 0
    assert len(REGISTRY.list(limit=100)) == before


# --- 7 --------------------------------------------------------------------


@test
def test_many_rapid_submissions_are_all_queued_and_processed():
    """The queue contract is FIFO, and with one Worker that means submission order."""
    QUEUE.purge()
    count = 5

    run_ids = [submit("hello_world", {"name": f"Rapid{i}"}).json()["run_id"] for i in range(count)]
    assert len(set(run_ids)) == count, "run ids collided"
    assert QUEUE.depth() == count, f"expected {count} queued, got {QUEUE.depth()}"
    assert all(CLIENT.get(f"/runs/{r}").json()["status"] == "PENDING" for r in run_ids)

    run_consumer(count)

    docs = [wait_for_terminal(r) for r in run_ids]
    assert all(d["status"] == "SUCCEEDED" for d in docs), [d["status"] for d in docs]
    for index, run_id in enumerate(run_ids):
        assert CLIENT.get(f"/runs/{run_id}/output").json() == {"message": f"Hello Rapid{index}"}, (
            "a run was executed with another run's input"
        )

    # FIFO is the documented guarantee, and it is about the order jobs are
    # TAKEN, not the order they finish. Start times only line up with it when
    # exactly one run executes at a time -- a Worker with several slots takes
    # jobs in FIFO order and then starts them within milliseconds of each
    # other, so their recorded start times interleave. Re-run pinned to one
    # slot, which is the configuration the claim is actually about.
    QUEUE.purge()
    ordered = [submit("hello_world", {"name": f"Ordered{i}"}).json()["run_id"] for i in range(4)]
    run_consumer(4, concurrency=1)
    starts = [wait_for_terminal(r)["started_at"] for r in ordered]
    assert starts == sorted(starts), f"jobs were not processed FIFO: {starts}"
    assert QUEUE.depth() == 0


# --- 8 --------------------------------------------------------------------


@test
def test_a_run_stays_pending_until_a_worker_appears():
    """The decoupling proof: with no Worker running, nothing executes."""
    QUEUE.purge()
    run_id = submit("hello_world", {"name": "Deferred"}).json()["run_id"]

    # No consumer is running. Give it long enough that a coupled design would
    # certainly have finished -- a real run takes about a second.
    time.sleep(3)

    doc = CLIENT.get(f"/runs/{run_id}").json()
    assert doc["status"] == "PENDING", f"something executed the run without a Worker: {doc}"
    assert doc["started_at"] is None
    assert QUEUE.depth() == 1, "the job left the queue with no Worker running"

    run_consumer(1)

    doc = wait_for_terminal(run_id)
    assert doc["status"] == "SUCCEEDED", doc
    assert CLIENT.get(f"/runs/{run_id}/output").json() == {"message": "Hello Deferred"}
    assert QUEUE.depth() == 0


@test
def test_once_terminates_on_an_empty_queue():
    """--once says "then exit", so it must not block waiting for work.

    It did: --max-jobs 1 on an empty queue waited for ever, which hangs any
    script or test that drains a queue it believes is already empty.
    """
    QUEUE.purge()
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(CONSUMER), "--once", "--poll-timeout", "0.2",
         "--queue-url", str(_SANDBOX / "queue"), "--registry-root", str(_SANDBOX / "runs")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr
    assert elapsed < 15, f"--once blocked for {elapsed:.1f}s on an empty queue"
    # "dispatching", not "processing": --max-jobs counts jobs handed to an
    # execution slot, since a concurrent Worker cannot wait for completions
    # before deciding whether to dequeue again.
    assert "stopped after dispatching 0 job(s)" in result.stderr, result.stderr


def main() -> int:
    failures = 0
    print(f"running {len(TESTS)} API + queue tests\n")

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
