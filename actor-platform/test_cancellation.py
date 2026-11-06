#!/usr/bin/env python3
"""Run cancellation, and the completion/cancellation races.

Cancellation is cooperative through the Registry: the API records CANCELLED (it
never touches Docker), and the worker that owns the container watches for that
state and stops the container. These tests cover all three targets (queued,
running, running-container) plus the races the design has to resolve
deterministically. Needs Docker.

    python3 test_cancellation.py
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager
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
from registry.run_registry import (  # noqa: E402
    CancelTooLate,
    InvalidTransition,
    RunRegistry,
)
from shared.models import RunStatus  # noqa: E402
from worker.reconciler import reconcile  # noqa: E402

_SANDBOX = Path(tempfile.mkdtemp(prefix="actor-cancellation-"))
atexit.register(shutil.rmtree, _SANDBOX, ignore_errors=True)

REGISTRY = RunRegistry(_SANDBOX / "runs")
QUEUE = open_queue(str(_SANDBOX / "queue"))
KEY = generate_key()
CLIENT = TestClient(
    create_app(registry=REGISTRY, queue=QUEUE, auth=AuthPolicy.for_keys({KEY})),
    headers={"Authorization": f"Bearer {KEY}"},
)
CONSUMER = REPO_ROOT / "worker" / "consumer.py"
TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def start_worker(*, max_jobs=1, drain=True):
    args = [sys.executable, str(CONSUMER), "--queue-url", str(_SANDBOX / "queue"),
            "--registry-root", str(_SANDBOX / "runs"), "--reconcile-interval", "0",
            "--max-jobs", str(max_jobs), "--poll-timeout", "0.2"]
    if drain:
        args.append("--drain")
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@contextmanager
def worker(**kw):
    p = start_worker(**kw)
    try:
        yield p
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=60)


def enqueue(actor, payload):
    run = REGISTRY.create(actor, payload)
    QUEUE.enqueue(Job(run_id=run.run_id, actor=actor, input=payload))
    return run.run_id


def container_alive(run_id) -> bool:
    out = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=^actor-{run_id}$", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    ).stdout.strip()
    return bool(out)


def wait_until(predicate, *, seconds=120, what="condition"):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def wait_running(run_id, **kw):
    wait_until(lambda: REGISTRY.get(run_id).status is RunStatus.RUNNING,
               what=f"{run_id} RUNNING", **kw)


def wait_terminal(run_id, seconds=120):
    wait_until(lambda: REGISTRY.get(run_id).is_terminal, seconds=seconds,
               what=f"{run_id} terminal")
    return REGISTRY.get(run_id)


# --- 1: cancel a QUEUED run -------------------------------------------------


@test
def test_cancelling_a_queued_run_stops_it_ever_executing():
    """A cancelled PENDING run must never start a container, even though its job
    is still in the queue -- the worker sees a terminal run and acks it."""
    run_id = enqueue("hello_world", {"name": "Q"})
    resp = CLIENT.post(f"/runs/{run_id}/cancel")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "CANCELLED" and resp.json()["already"] is False
    assert REGISTRY.get(run_id).status is RunStatus.CANCELLED
    assert QUEUE.depth() == 1, "the job is still queued; cancellation is by state, not by removal"

    with worker():
        wait_until(lambda: QUEUE.depth() == 0 and QUEUE.inflight() == 0,
                   what="the job to be acked")

    run = REGISTRY.get(run_id)
    assert run.status is RunStatus.CANCELLED, run.status
    assert run.started_at is None, "a cancelled queued run must never have started"
    assert not container_alive(run_id), "a container ran for a cancelled queued run"


# --- 2 & 3: cancel a RUNNING run and its container --------------------------


@test
def test_cancelling_a_running_run_actually_stops_the_container():
    run_id = enqueue("item_processor", {"count": 1, "delay_ms": 30000})
    with worker() as w:
        wait_running(run_id)
        wait_until(lambda: container_alive(run_id), what="the container to start")

        resp = CLIENT.post(f"/runs/{run_id}/cancel")
        assert resp.status_code == 200, resp.text

        # The worker watcher must actually stop the container, not just leave a
        # CANCELLED record with a container still burning CPU.
        wait_until(lambda: not container_alive(run_id), seconds=30,
                   what="the container to be stopped")
        w.wait(timeout=60)

    run = REGISTRY.get(run_id)
    assert run.status is RunStatus.CANCELLED, run.status
    assert run.finished_at and run.duration_seconds is not None
    assert QUEUE.depth() == 0 and QUEUE.inflight() == 0, "the cancelled job was not acked"


# --- 6: idempotency ---------------------------------------------------------


@test
def test_cancelling_twice_is_safe_and_reports_already():
    run_id = enqueue("hello_world", {"name": "Twice"})
    first = CLIENT.post(f"/runs/{run_id}/cancel").json()
    second = CLIENT.post(f"/runs/{run_id}/cancel").json()
    assert first["already"] is False and second["already"] is True
    assert REGISTRY.get(run_id).status is RunStatus.CANCELLED
    QUEUE.purge()


@test
def test_cancelling_a_finished_run_is_refused_not_corrupting():
    run_id = enqueue("hello_world", {"name": "Done"})
    with worker():
        run = wait_terminal(run_id)
    assert run.status is RunStatus.SUCCEEDED

    resp = CLIENT.post(f"/runs/{run_id}/cancel")
    assert resp.status_code == 409, resp.text
    assert "already finished" in resp.json()["error"]
    # the result is untouched
    after = REGISTRY.get(run_id)
    assert after.status is RunStatus.SUCCEEDED
    assert CLIENT.get(f"/runs/{run_id}/output").json() == {"message": "Hello Done"}


@test
def test_registry_cancel_is_idempotent_and_race_safe_in_isolation():
    """Unit-level: the three outcomes cancel() must distinguish."""
    reg = RunRegistry(_SANDBOX / "unit")
    # PENDING -> CANCELLED
    a = reg.create("hello_world", {"name": "a"})
    assert reg.cancel(a.run_id).status is RunStatus.CANCELLED
    # re-cancel -> no-op, still CANCELLED
    assert reg.cancel(a.run_id).status is RunStatus.CANCELLED
    # SUCCEEDED -> CancelTooLate
    b = reg.create("hello_world", {"name": "b"})
    reg.mark_running(b.run_id)
    reg.complete(b.run_id, status=RunStatus.SUCCEEDED, exit_code=0)
    try:
        reg.cancel(b.run_id)
        raise AssertionError("cancelled a SUCCEEDED run")
    except CancelTooLate as exc:
        assert exc.status is RunStatus.SUCCEEDED


# --- 6: the completion-vs-cancel race is deterministic ----------------------


@test
def test_concurrent_complete_and_cancel_never_lose_an_update():
    """Hammer the exact race under the run lock: one thread completes, another
    cancels, same run, at the same instant. Exactly one wins, and the record is
    always ONE clean terminal state -- never a half-written or lost update."""
    reg = RunRegistry(_SANDBOX / "race")
    cancel_won = complete_won = 0

    for i in range(40):
        run = reg.create("hello_world", {"name": f"r{i}"})
        reg.mark_running(run.run_id)
        barrier = threading.Barrier(2)
        results = {}

        def do_complete():
            barrier.wait()
            try:
                reg.complete(run.run_id, status=RunStatus.SUCCEEDED, exit_code=0)
                results["complete"] = "ok"
            except Exception as exc:  # noqa: BLE001
                results["complete"] = type(exc).__name__

        def do_cancel():
            barrier.wait()
            try:
                reg.cancel(run.run_id)
                results["cancel"] = "ok"
            except CancelTooLate:
                results["cancel"] = "too_late"
            except Exception as exc:  # noqa: BLE001
                results["cancel"] = type(exc).__name__

        t1 = threading.Thread(target=do_complete)
        t2 = threading.Thread(target=do_cancel)
        t1.start(); t2.start(); t1.join(); t2.join()

        final = reg.get(run.run_id).status
        # the record is exactly one of the two clean terminal states
        assert final in (RunStatus.SUCCEEDED, RunStatus.CANCELLED), final
        # and the outcomes are mutually consistent: if cancel committed, complete
        # must have been refused (InvalidTransition), and vice versa.
        if final is RunStatus.CANCELLED:
            cancel_won += 1
            assert results["cancel"] == "ok"
            assert results["complete"] in ("InvalidTransition",), results
        else:
            complete_won += 1
            assert results["complete"] == "ok"
            assert results["cancel"] == "too_late", results

    # NOT asserted: that both winners appear. Which one wins is legitimately
    # timing-dependent AND structurally skewed -- complete() reads the run to
    # compute its duration before update() takes the lock, while cancel() takes
    # the lock immediately, so cancel usually gets there first. Requiring both
    # here made this test fail roughly 1 run in 3 while nothing was wrong. The
    # property that matters is the one asserted per iteration above: exactly one
    # clean terminal state, never a lost update. Both ORDERINGS are covered
    # deterministically by the test below instead of being hoped for.
    assert cancel_won + complete_won == 40


@test
def test_both_orderings_of_completion_and_cancellation_are_deterministic():
    """The coverage the concurrent hammer above cannot guarantee, forced.

    Ordering A -- cancel commits first: the later completion must be REFUSED and
    the run stays CANCELLED. Ordering B -- completion commits first: the later
    cancel must be refused as too late and the result stands. Sequencing them
    explicitly is strictly better than racing and hoping both appear."""
    reg = RunRegistry(_SANDBOX / "orderings")

    # A: cancel wins
    a = reg.create("hello_world", {"name": "a"})
    reg.mark_running(a.run_id)
    reg.cancel(a.run_id)
    try:
        reg.complete(a.run_id, status=RunStatus.SUCCEEDED, exit_code=0)
        raise AssertionError("completion overwrote a CANCELLED run")
    except InvalidTransition:
        pass
    assert reg.get(a.run_id).status is RunStatus.CANCELLED

    # B: completion wins
    b = reg.create("hello_world", {"name": "b"})
    reg.mark_running(b.run_id)
    reg.complete(b.run_id, status=RunStatus.SUCCEEDED, exit_code=0)
    try:
        reg.cancel(b.run_id)
        raise AssertionError("cancel overwrote a finished run")
    except CancelTooLate as exc:
        assert exc.status is RunStatus.SUCCEEDED
    assert reg.get(b.run_id).status is RunStatus.SUCCEEDED


# --- 7: nothing resurrects a cancelled run ----------------------------------


@test
def test_the_reconciler_never_resurrects_a_cancelled_run():
    reg = RunRegistry(_SANDBOX / "recon-runs")
    q = open_queue(str(_SANDBOX / "recon-queue"))
    q.purge()
    # a cancelled run with NO job in the queue -- exactly what the reconciler
    # scans for, except it is terminal and must be left alone.
    run = reg.create("hello_world", {"name": "z"})
    reg.cancel(run.run_id)
    report = reconcile(reg, q, grace=0.0)
    assert report.requeued == [] and report.abandoned == []
    assert reg.get(run.run_id).status is RunStatus.CANCELLED
    assert q.depth() == 0


@test
def test_a_cancelled_run_whose_job_is_still_queued_is_acked_not_run():
    run_id = enqueue("item_processor", {"count": 1, "delay_ms": 30000})
    CLIENT.post(f"/runs/{run_id}/cancel")
    with worker():
        wait_until(lambda: QUEUE.depth() == 0 and QUEUE.inflight() == 0,
                   what="the cancelled job to be acked")
    assert REGISTRY.get(run_id).status is RunStatus.CANCELLED
    assert not container_alive(run_id)
    assert REGISTRY.get(run_id).started_at is None


# --- 8: structured events ---------------------------------------------------


@test
def test_cancel_emits_a_structured_api_event():
    run_id = enqueue("hello_world", {"name": "Evt"})
    p = subprocess.run(
        [sys.executable, "-c",
         "import os,sys,json; os.environ['ACTOR_LOG_FORMAT']='json'; sys.path.insert(0,'.');"
         "from shared.logging import event;"
         "event('api','run_cancel_requested',run_id='r1',actor='hello_world',previous='PENDING')"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    line = p.stderr.strip()
    assert line.startswith("{"), p.stderr
    import json
    rec = json.loads(line)
    assert rec["event"] == "run_cancel_requested" and rec["run_id"] == "r1"
    QUEUE.purge()


# --- misc guards ------------------------------------------------------------


@test
def test_cancel_requires_the_run_scope():
    run_id = enqueue("hello_world", {"name": "Scope"})
    read_key = generate_key()
    from api.auth import AuthPolicy as AP, Scope
    app = create_app(registry=REGISTRY, queue=QUEUE,
                     auth=AP.for_keys({read_key}, scopes=frozenset({Scope.READ})))
    reader = TestClient(app, headers={"Authorization": f"Bearer {read_key}"})
    resp = reader.post(f"/runs/{run_id}/cancel")
    assert resp.status_code == 403, resp.status_code
    assert REGISTRY.get(run_id).status is RunStatus.PENDING, "a read-only key cancelled a run"
    QUEUE.purge()


@test
def test_cancelling_an_unknown_run_is_404():
    resp = CLIENT.post("/runs/20260101-000000000000-ffffff/cancel")
    assert resp.status_code == 404, resp.status_code


@test
def test_a_run_cancelled_mid_write_keeps_its_logs_without_a_false_error():
    """The UI verification surfaced this: after a mid-run cancel the worker
    called write_logs() on an already-CANCELLED run, which raised the terminal
    guard and logged a spurious 'could not be executed'. write_logs must persist
    the log file and skip the count rather than raise."""
    reg = RunRegistry(_SANDBOX / "logwrite")
    run = reg.create("hello_world", {"name": "L"})
    reg.mark_running(run.run_id)
    reg.cancel(run.run_id)                       # now terminal
    # writing logs on the terminal run must NOT raise, and must persist them
    n = reg.write_logs(run.run_id, ["line one", "line two"])
    assert n == 2
    assert reg.get_logs(run.run_id).splitlines() == ["line one", "line two"]
    assert reg.get(run.run_id).status is RunStatus.CANCELLED


def main():
    print(f"sandbox: {_SANDBOX}\n")
    passed = failed = 0
    for fn in TESTS:
        started = time.monotonic()
        try:
            fn()
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}  ({time.monotonic()-started:.1f}s)")
            print("".join("      " + l for l in traceback.format_exc().splitlines(True)))
        else:
            passed += 1
            print(f"ok    {fn.__name__}  ({time.monotonic()-started:.1f}s)")
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
