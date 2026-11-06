#!/usr/bin/env python3
"""Observability: the read-model, structured events, and the CANCELLED state.

This suite is about whether the platform can be OBSERVED -- can you see a job's
lifecycle, correlate its events by id, and read accurate per-collector health?
It exercises real runs through the worker (needs Docker) plus offline unit checks
of the derivation and the log format.

    python3 test_observability.py
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
from observability.metrics import collector_health, platform_metrics  # noqa: E402
from registry.run_registry import RunRegistry  # noqa: E402
from shared.models import RunStatus  # noqa: E402

_SANDBOX = Path(tempfile.mkdtemp(prefix="actor-observability-"))
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


def start_worker(*, max_jobs=1, concurrency=1, drain=True, json_logs=False):
    args = [
        sys.executable, str(CONSUMER),
        "--queue-url", str(_SANDBOX / "queue"),
        "--registry-root", str(_SANDBOX / "runs"),
        "--reconcile-interval", "0",
        "--concurrency", str(concurrency),
        "--max-jobs", str(max_jobs), "--poll-timeout", "0.2",
    ]
    if drain:
        args.append("--drain")
    env = {**os.environ}
    if json_logs:
        env["ACTOR_LOG_FORMAT"] = "json"
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, env=env)


@contextmanager
def worker(**kwargs):
    p = start_worker(**kwargs)
    try:
        yield p
    finally:
        # LET IT EXIT ON ITS OWN; kill only a worker that hangs. Every caller
        # gives it exactly --max-jobs jobs under --drain, so it stops by itself.
        # Killing it as soon as a test SEES a terminal status is a race: the
        # worker records that status first and acks the queue job a moment
        # later, so a kill in between strands an unacked lease. The next
        # worker's startup recovery puts that lease back at the FRONT of the
        # queue, spends its single --max-jobs slot acking the finished run, and
        # exits -- leaving the new test's run PENDING until it times out. Seen in
        # CI as "never observed RUNNING: ['PENDING']" after 120 s.
        # communicate() rather than wait(): stderr is a pipe, and an unread pipe
        # that fills would block the very exit being waited for.
        try:
            p.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate(timeout=60)


def enqueue(actor, payload):
    run = REGISTRY.create(actor, payload)
    QUEUE.enqueue(Job(run_id=run.run_id, actor=actor, input=payload))
    return run.run_id


def wait_terminal(run_id, seconds=180):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        run = REGISTRY.get(run_id)
        if run.is_terminal:
            return run
        time.sleep(0.05)
    raise AssertionError(f"{run_id} never finished; it is {REGISTRY.get(run_id).status.value}")


# --- job lifecycle & metadata over the API ----------------------------------


@test
def test_a_normal_job_exposes_its_full_lifecycle_and_metadata():
    run_id = enqueue("hello_world", {"name": "Obs"})

    # PENDING is observable before a Worker touches it.
    d = CLIENT.get(f"/runs/{run_id}").json()
    assert d["status"] == "PENDING" and d["started_at"] is None

    with worker():
        run = wait_terminal(run_id)

    d = CLIENT.get(f"/runs/{run_id}").json()
    for field in ("run_id", "actor", "status", "created_at", "started_at",
                  "finished_at", "duration_seconds", "exit_code", "attempts"):
        assert field in d, f"metadata missing {field}"
    assert d["status"] == "SUCCEEDED" and d["exit_code"] == 0
    assert d["started_at"] and d["finished_at"] and d["duration_seconds"] >= 0
    assert d["actor"] == "hello_world"


@test
def test_a_failed_job_carries_its_error_and_the_right_status():
    run_id = enqueue("item_processor", {"count": 3, "fail_at": 2})
    with worker():
        wait_terminal(run_id)
    d = CLIENT.get(f"/runs/{run_id}").json()
    assert d["status"] == "FAILED", d["status"]
    assert d["exit_code"] == 1
    assert d["error"], "a failed run must record why"


# --- state transitions ------------------------------------------------------


@test
def test_state_transitions_are_observed_in_order():
    """PENDING -> RUNNING -> a terminal status, seen by polling the read API."""
    run_id = enqueue("item_processor", {"count": 1, "delay_ms": 1500})
    seen = []
    with worker():
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            st = CLIENT.get(f"/runs/{run_id}").json()["status"]
            if not seen or seen[-1] != st:
                seen.append(st)
            if st in ("SUCCEEDED", "FAILED", "PLATFORM_ERROR", "CANCELLED"):
                break
            time.sleep(0.03)
    assert seen[0] == "PENDING", seen
    assert "RUNNING" in seen, f"never observed RUNNING: {seen}"
    assert seen[-1] == "SUCCEEDED", seen
    # forward-only: no status repeats after moving on
    assert len(seen) == len(set(seen)), f"a state was revisited: {seen}"


# --- CANCELLED: interrupted-then-given-up is visible as itself ---------------


@test
def test_an_abandoned_run_is_observable_as_cancelled_not_platform_error():
    """A job delivered past --max-attempts is CANCELLED (the platform stopped
    trying), which the metrics must count separately from a platform fault."""
    run_id = enqueue("hello_world", {"name": "Doomed"})
    # Hand it back already at the attempt ceiling.
    QUEUE.purge()
    QUEUE.enqueue(Job(run_id=run_id, actor="hello_world", input={"name": "Doomed"}, attempts=3))
    with worker():
        run = wait_terminal(run_id)
    assert run.status is RunStatus.CANCELLED, run.status
    assert "abandoned" in (run.error or "")
    assert RunStatus.CANCELLED in __import__("shared.models", fromlist=["TERMINAL_STATUSES"]).TERMINAL_STATUSES


# --- concurrent jobs are each observable ------------------------------------


@test
def test_concurrent_jobs_are_each_individually_observable():
    ids = [enqueue("item_processor", {"count": 1, "delay_ms": 1000}) for _ in range(4)]
    with worker(max_jobs=4, concurrency=4):
        runs = [wait_terminal(i) for i in ids]
    assert all(r.status is RunStatus.SUCCEEDED for r in runs)
    # each has its own distinct metadata
    assert len({r.run_id for r in runs}) == 4
    assert all(r.duration_seconds is not None for r in runs)


# --- long-running job stays observable as RUNNING ---------------------------


@test
def test_a_long_running_job_is_observable_as_running_throughout():
    run_id = enqueue("item_processor", {"count": 1, "delay_ms": 3000})
    with worker():
        # It must show RUNNING for a sustained stretch, not flicker.
        deadline = time.monotonic() + 60
        running_seen = 0
        while time.monotonic() < deadline:
            if CLIENT.get(f"/runs/{run_id}").json()["status"] == "RUNNING":
                running_seen += 1
                if running_seen >= 3:
                    break
            time.sleep(0.2)
        assert running_seen >= 3, "the run was not observably RUNNING for long"
        wait_terminal(run_id)


# --- shutdown while running: not lost, and observable -----------------------


@test
def test_a_run_interrupted_by_shutdown_is_recovered_and_visible():
    run_id = enqueue("item_processor", {"count": 1, "delay_ms": 20000})
    first = start_worker(max_jobs=1, drain=False)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if REGISTRY.get(run_id).status is RunStatus.RUNNING:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("never reached RUNNING")
        time.sleep(1)
        first.kill(); first.wait(timeout=30)
    finally:
        if first.poll() is None:
            first.kill()

    # Interrupted: still RUNNING with its lease intact -- observable, not lost.
    interrupted = REGISTRY.get(run_id)
    assert interrupted.status is RunStatus.RUNNING
    assert QUEUE.inflight() == 1

    with worker():
        run = wait_terminal(run_id)
    assert run.status is RunStatus.SUCCEEDED
    assert run.attempts == 2, "the recovery attempt must be visible in the metadata"


# --- structured logging correlates by run id --------------------------------


@test
def test_structured_events_are_emitted_and_correlate_by_run_id():
    run_id = enqueue("hello_world", {"name": "Structured"})
    p = start_worker(json_logs=True)
    try:
        wait_terminal(run_id)
        _, stderr = p.communicate(timeout=60)
    finally:
        if p.poll() is None:
            p.kill()

    events = []
    for line in stderr.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    assert events, "no JSON events were emitted under ACTOR_LOG_FORMAT=json"

    mine = [e for e in events if e.get("run_id") == run_id]
    names = {e["event"] for e in mine}
    assert "job_picked_up" in names, names
    assert "job_running" in names, names
    assert "job_finished" in names or "job_completed" in names, names
    # every event carries the correlation handle and a component
    assert all("component" in e and "event" in e for e in mine)


# Child process for the interleaving test below. Two threads log at once, the
# way a job thread and the consumer's main loop do: one writes structured
# events, the other writes human log() lines.
_INTERLEAVE_CHILD = r"""
import sys, threading
sys.path.insert(0, sys.argv[1])
from shared.logging import event
from worker.consumer import log as consumer_log
from worker.executor import log as executor_log

N = int(sys.argv[2])
gate = threading.Barrier(3)

def events():
    gate.wait()
    for i in range(N):
        event("consumer", "job_picked_up", run_id=f"r{i}", attempt=1)

def humans(fn, tag):
    gate.wait()
    for i in range(N):
        fn(f"{tag} line {i} - later jobs are waiting in the queue")

threads = [threading.Thread(target=events),
           threading.Thread(target=humans, args=(consumer_log, "c")),
           threading.Thread(target=humans, args=(executor_log, "e"))]
for t in threads:
    t.start()
for t in threads:
    t.join()
"""


@test
def test_concurrent_log_lines_never_glue_onto_an_event():
    """Every log line is written whole, even with stderr unbuffered.

    `print()` writes the text and the newline separately. Under
    PYTHONUNBUFFERED=1 (which scripts/run_tests.py sets and the Worker inherits)
    those are two OS writes, so another thread's line could land between them:
    `[consumer] all 1 slot(s) busy{"event": "job_picked_up", ...}`. A JSON-lines
    reader then lost the event. Measured before the fix: 2-6% of events lost.
    """
    n = 3000
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "ACTOR_LOG_FORMAT": "json"}
    proc = subprocess.run(
        [sys.executable, "-c", _INTERLEAVE_CHILD, str(REPO_ROOT), str(n)],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]

    events, humans, garbled = 0, {"[consumer]": 0, "[worker]": 0}, []
    for line in proc.stderr.splitlines():
        if line.startswith("{"):
            try:
                record = json.loads(line)
            except ValueError:
                garbled.append(line)
                continue
            if record.get("event") == "job_picked_up":
                events += 1
            else:
                garbled.append(line)
            continue
        prefix = line.split(" ", 1)[0]
        if prefix in humans and line.endswith("later jobs are waiting in the queue"):
            humans[prefix] += 1
        else:
            garbled.append(line)

    assert not garbled, f"{len(garbled)} glued line(s), e.g. {garbled[0][:200]!r}"
    assert events == n, f"{n - events} of {n} event(s) lost"
    assert humans == {"[consumer]": n, "[worker]": n}, humans


@test
def test_human_logging_is_unchanged_when_json_is_not_requested():
    run_id = enqueue("hello_world", {"name": "Human"})
    p = start_worker(json_logs=False)
    try:
        wait_terminal(run_id)
        _, stderr = p.communicate(timeout=60)
    finally:
        if p.poll() is None:
            p.kill()
    # Human text still there; no JSON event lines leaked in.
    assert "[consumer]" in stderr
    json_lines = [l for l in stderr.splitlines() if l.strip().startswith('{"ts"')]
    assert not json_lines, f"JSON events emitted without being asked: {json_lines[:2]}"


# --- the read model: platform metrics ---------------------------------------


@test
def test_platform_metrics_roll_up_by_status_and_actor():
    ok = enqueue("hello_world", {"name": "M"})
    bad = enqueue("item_processor", {"count": 2, "fail_at": 1})
    with worker(max_jobs=2, concurrency=2):
        wait_terminal(ok); wait_terminal(bad)

    m = platform_metrics(REGISTRY, window=500, queue=QUEUE)
    # every status has a row, present or zero
    assert set(m.by_status) == {s.value for s in RunStatus}
    assert m.by_status["SUCCEEDED"] >= 1 and m.by_status["FAILED"] >= 1
    assert "hello_world" in m.by_actor and "item_processor" in m.by_actor
    assert m.by_actor["hello_world"]["SUCCEEDED"] >= 1
    assert m.by_actor["item_processor"]["FAILED"] >= 1
    # live queue counts are exact and separate from the window
    assert m.queue_ok is True and m.queued == 0

    body = CLIENT.get("/metrics").json()
    assert body["runs_by_status"]["SUCCEEDED"] >= 1
    assert body["queue"]["ok"] is True


@test
def test_metrics_window_is_bounded_and_honest_about_scope():
    """The window caps cost and the response says what it read, so the numbers
    are never mistaken for all-time totals."""
    m = platform_metrics(REGISTRY, window=2)
    assert m.window == 2
    assert m.scanned <= 2, "the window was not honoured"
    total = sum(m.by_status.values())
    assert total == m.scanned, "counts must sum to what was actually read"


# --- the read model: collector health ---------------------------------------


@test
def test_collector_health_reports_last_run_success_failure_and_rate():
    # a clean sandbox so the rates are exactly what this test produced
    reg = RunRegistry(_SANDBOX / "health-runs")
    q = open_queue(str(_SANDBOX / "health-queue"))

    def submit(actor, payload):
        run = reg.create(actor, payload)
        q.enqueue(Job(run_id=run.run_id, actor=actor, input=payload))
        return run.run_id

    good = [submit("hello_world", {"name": f"G{i}"}) for i in range(3)]
    bad = submit("hello_world", {})  # missing name -> the Actor fails

    args = [sys.executable, str(CONSUMER), "--queue-url", str(_SANDBOX / "health-queue"),
            "--registry-root", str(_SANDBOX / "health-runs"), "--reconcile-interval", "0",
            "--concurrency", "2", "--max-jobs", "4", "--drain", "--poll-timeout", "0.2"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if all(reg.get(i).is_terminal for i in [*good, bad]):
                break
            time.sleep(0.05)
        proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()

    health = {h.name: h for h in collector_health(reg, window=500)}
    hw = health["hello_world"]
    assert hw.total == 4, hw.total
    assert hw.by_status["SUCCEEDED"] == 3 and hw.by_status["FAILED"] == 1
    assert hw.success_rate == 0.75, hw.success_rate
    assert hw.last_success_at is not None
    assert hw.last_failure_at is not None
    assert hw.last_run_id is not None and hw.last_status in ("SUCCEEDED", "FAILED")


@test
def test_success_rate_is_none_when_nothing_has_finished():
    """None, not 0.0: 'no finished runs' and 'all failed' are different facts."""
    reg = RunRegistry(_SANDBOX / "pending-only")
    reg.create("hello_world", {"name": "never run"})
    health = {h.name: h for h in collector_health(reg, window=500)}
    assert health["hello_world"].success_rate is None
    assert health["hello_world"].by_status["PENDING"] == 1


@test
def test_collectors_endpoint_matches_the_read_model():
    body = CLIENT.get("/collectors?window=500").json()
    assert body["window"] == 500 and body["count"] == len(body["collectors"])
    for c in body["collectors"]:
        assert {"collector", "total", "by_status", "success_rate", "last_run"} <= set(c)


# --- FIFO / path-leak behavior still holds ----------------------------------


@test
def test_fifo_start_order_under_a_single_slot_is_preserved():
    """The existing guarantee, re-checked here because observability touched the
    consumer: with one slot, jobs START in submission order."""
    QUEUE.purge()
    ids = [enqueue("hello_world", {"name": f"FIFO{i}"}) for i in range(4)]
    with worker(max_jobs=4, concurrency=1):
        runs = [wait_terminal(i) for i in ids]
    starts = [r.started_at for r in runs]
    assert starts == sorted(starts), f"not FIFO: {starts}"


@test
def test_no_run_is_executed_with_another_runs_input():
    """The path-leak check: each run's output reflects ITS OWN input."""
    QUEUE.purge()
    ids = [enqueue("hello_world", {"name": f"Leak{i}"}) for i in range(5)]
    with worker(max_jobs=5, concurrency=3):
        for i in ids:
            wait_terminal(i)
    for index, run_id in enumerate(ids):
        out = CLIENT.get(f"/runs/{run_id}/output").json()
        assert out == {"message": f"Hello Leak{index}"}, out


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
