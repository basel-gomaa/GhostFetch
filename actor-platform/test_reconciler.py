#!/usr/bin/env python3
"""Reconciler tests: runs that nothing will ever pick up.

Leases protect work that is still IN the queue. These cover the other half --
a run whose job vanished entirely, which no amount of redelivery can fix
because there is nothing left to redeliver.

Every test uses its own registry and queue, so the real storage is untouched.
Only the end-to-end test needs Docker.

    python3 test_reconciler.py
"""

from __future__ import annotations

import os
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

from jobqueue.base import Job  # noqa: E402
from jobqueue.file_queue import FileJobQueue  # noqa: E402
from registry.run_registry import RunRegistry  # noqa: E402
from shared.models import RunStatus  # noqa: E402
from worker.reconciler import find_stranded, reconcile  # noqa: E402

CONSUMER = REPO_ROOT / "worker" / "consumer.py"

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


@contextmanager
def platform():
    """A registry and a queue of their own."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        yield RunRegistry(root / "runs"), FileJobQueue(root / "queue")


def submitted(registry, queue, actor="hello_world", payload=None, *, enqueue=True):
    """A run registered exactly as the API registers one."""
    payload = payload if payload is not None else {"name": "Stranded"}
    run = registry.create(actor, payload)
    if enqueue:
        queue.enqueue(Job(run_id=run.run_id, actor=actor, input=payload))
    return run


# --- what counts as stranded ------------------------------------------------


@test
def test_a_pending_run_with_no_job_is_stranded():
    with platform() as (registry, queue):
        orphan = submitted(registry, queue, enqueue=False)

        stranded, young = find_stranded(registry, queue, grace=0)
        assert [r.run_id for r in stranded] == [orphan.run_id], stranded
        assert young == []

        report = reconcile(registry, queue, grace=0)
        assert report.requeued == [orphan.run_id], report
        assert queue.depth() == 1, "the run must be back on the queue"
        assert queue.known_run_ids() == {orphan.run_id}
        assert registry.get(orphan.run_id).status is RunStatus.PENDING


@test
def test_a_running_run_with_no_job_is_stranded_and_reset():
    """Nothing is running it: a Worker only executes a job it has leased."""
    with platform() as (registry, queue):
        orphan = submitted(registry, queue, enqueue=False)
        registry.mark_running(orphan.run_id)
        assert registry.get(orphan.run_id).attempts == 1

        report = reconcile(registry, queue, grace=0)
        assert report.requeued == [orphan.run_id]

        run = registry.get(orphan.run_id)
        assert run.status is RunStatus.PENDING, "a stranded RUNNING run must become runnable"
        assert run.started_at is None
        assert "stranded" in (run.error or "")
        assert run.attempts == 1, "the abandoned attempt is remembered"

        job = queue.dequeue()
        assert job.attempts == 1, "the requeued job must carry the attempt count forward"


@test
def test_a_queued_run_is_left_alone():
    with platform() as (registry, queue):
        waiting = submitted(registry, queue)

        stranded, _ = find_stranded(registry, queue, grace=0)
        assert stranded == [], "a run with a queued job is not stranded"

        report = reconcile(registry, queue, grace=0)
        assert report.stranded == 0
        assert queue.depth() == 1, "it must not be enqueued twice"


@test
def test_a_leased_run_is_left_alone():
    """The dangerous false positive: a run being executed RIGHT NOW."""
    with platform() as (registry, queue):
        run = submitted(registry, queue)
        queue.dequeue()                       # a Worker holds the lease
        registry.mark_running(run.run_id)
        assert (queue.depth(), queue.inflight()) == (0, 1)

        stranded, _ = find_stranded(registry, queue, grace=0)
        assert stranded == [], "a leased job is still in the queue - never requeue it"
        assert reconcile(registry, queue, grace=0).stranded == 0
        assert registry.get(run.run_id).status is RunStatus.RUNNING


@test
def test_terminal_runs_are_never_touched():
    with platform() as (registry, queue):
        done = submitted(registry, queue, enqueue=False)
        registry.mark_running(done.run_id)
        registry.complete(done.run_id, status=RunStatus.SUCCEEDED, exit_code=0)

        failed = submitted(registry, queue, enqueue=False)
        registry.mark_running(failed.run_id)
        registry.complete(failed.run_id, status=RunStatus.FAILED, exit_code=1, error="nope")

        assert find_stranded(registry, queue, grace=0) == ([], [])
        assert reconcile(registry, queue, grace=0).stranded == 0
        assert queue.depth() == 0, "a finished run must never be resurrected"


@test
def test_a_young_run_is_not_judged_yet():
    """"Not queued yet" is also what a healthy run looks like for a moment."""
    with platform() as (registry, queue):
        fresh = submitted(registry, queue, enqueue=False)

        stranded, young = find_stranded(registry, queue, grace=60)
        assert stranded == []
        assert [r.run_id for r in young] == [fresh.run_id]

        report = reconcile(registry, queue, grace=60)
        assert report.stranded == 0
        assert report.skipped_young == [fresh.run_id]
        assert queue.depth() == 0


@test
def test_a_run_out_of_attempts_is_given_up_on():
    with platform() as (registry, queue):
        doomed = submitted(registry, queue, enqueue=False)
        for _ in range(3):
            registry.mark_running(doomed.run_id)
            registry.requeue(doomed.run_id, "abandoned")
        assert registry.get(doomed.run_id).attempts == 3

        report = reconcile(registry, queue, grace=0, max_attempts=3)
        assert report.abandoned == [doomed.run_id], report
        assert report.requeued == []

        run = registry.get(doomed.run_id)
        assert run.status is RunStatus.CANCELLED
        assert "stranded after 3" in (run.error or ""), run.error
        assert queue.depth() == 0, "a run given up on must not be requeued"


@test
def test_dry_run_changes_nothing():
    with platform() as (registry, queue):
        orphan = submitted(registry, queue, enqueue=False)
        registry.mark_running(orphan.run_id)
        before = registry.get(orphan.run_id).to_dict()

        report = reconcile(registry, queue, grace=0, dry_run=True)
        assert report.requeued == [orphan.run_id], "it must still REPORT the problem"
        assert queue.depth() == 0, "dry run must not enqueue"
        assert registry.get(orphan.run_id).to_dict() == before, "dry run must not edit the record"


# --- case 2: no duplicate execution -----------------------------------------


@test
def test_a_stranded_running_run_has_its_container_killed():
    """Resetting a RUNNING run is only safe if its container is stopped first.

    The container outlives the Worker that started it, so without this the
    retry's container and the abandoned one write the same run directory at the
    same time.
    """
    with platform() as (registry, queue):
        orphan = submitted(registry, queue, enqueue=False)
        registry.mark_running(orphan.run_id)

        # Stand in for the container the dead Worker left behind.
        name = f"actor-{orphan.run_id}"
        subprocess.run(
            ["docker", "run", "-d", "--name", name, "--entrypoint", "sleep",
             "actor-platform/hello_world:latest", "120"],
            capture_output=True, text=True, check=True,
        )
        try:
            alive = subprocess.run(
                ["docker", "ps", "--filter", f"name={name}", "--format", "{{.Names}}"],
                capture_output=True, text=True,
            ).stdout.strip()
            assert alive == name, "the stand-in container did not start"

            reconcile(registry, queue, grace=0)

            still = subprocess.run(
                ["docker", "ps", "--filter", f"name={name}", "--format", "{{.Names}}"],
                capture_output=True, text=True,
            ).stdout.strip()
            assert still == "", "the abandoned container is still running - two would now write one run dir"
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)


# --- case 4: racing a live Worker -------------------------------------------


@test
def test_reconciling_while_a_worker_runs_creates_no_duplicate():
    """Sweep continuously, with NO grace period, while runs are executing.

    grace=0 removes the safety margin on purpose: if the queue snapshot and the
    registry scan can disagree, this is where it shows.
    """
    with platform() as (registry, queue):
        runs = [submitted(registry, queue, "hello_world", {"name": f"Race{i}"}) for i in range(4)]

        worker = subprocess.Popen(
            [
                sys.executable, str(CONSUMER),
                "--queue-url", str(queue.root.parent),
                "--registry-root", str(registry.root),
                "--reconcile-interval", "0",     # only THIS test's sweeps run
                "--drain", "--max-jobs", "8", "--poll-timeout", "0.2",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        sweeps = duplicated = 0
        try:
            while worker.poll() is None:
                report = reconcile(registry, queue, grace=0)
                duplicated += len(report.requeued)
                sweeps += 1
                time.sleep(0.01)
        finally:
            if worker.poll() is None:
                worker.kill()
            worker.wait(timeout=30)

        assert sweeps > 5, f"only {sweeps} sweeps - the race window was never exercised"
        assert duplicated == 0, f"{duplicated} live run(s) were wrongly requeued"

        for run in runs:
            final = registry.get(run.run_id)
            assert final.status is RunStatus.SUCCEEDED, f"{final.run_id}: {final.status.value}"
            assert final.attempts == 1, (
                f"run {final.run_id} was executed {final.attempts} times - the reconciler "
                "created a duplicate"
            )
        assert (queue.depth(), queue.inflight()) == (0, 0), "leftover jobs after everything finished"


# --- case 6: running it twice -----------------------------------------------


@test
def test_repeated_reconciliation_is_idempotent():
    """A second sweep must find nothing left to do and enqueue nothing more."""
    with platform() as (registry, queue):
        pending = submitted(registry, queue, enqueue=False)
        running = submitted(registry, queue, enqueue=False)
        registry.mark_running(running.run_id)

        first = reconcile(registry, queue, grace=0)
        assert sorted(first.requeued) == sorted([pending.run_id, running.run_id])
        assert queue.depth() == 2
        after_first = {r: registry.get(r).to_dict() for r in first.requeued}

        for sweep in range(3):
            report = reconcile(registry, queue, grace=0)
            assert report.stranded == 0, f"sweep {sweep + 2} found work again: {report.summary()}"
            assert queue.depth() == 2, f"sweep {sweep + 2} enqueued a duplicate job"
            for run_id, before in after_first.items():
                assert registry.get(run_id).to_dict() == before, f"sweep {sweep + 2} edited {run_id}"

        # And each run is queued exactly once, not merely counted correctly.
        ids = [queue.dequeue().run_id for _ in range(2)]
        assert sorted(ids) == sorted([pending.run_id, running.run_id]), ids
        assert queue.depth() == 0


@test
def test_repeated_reconciliation_of_an_abandoned_run_is_idempotent():
    with platform() as (registry, queue):
        doomed = submitted(registry, queue, enqueue=False)
        for _ in range(3):
            registry.mark_running(doomed.run_id)
            registry.requeue(doomed.run_id, "abandoned")

        first = reconcile(registry, queue, grace=0, max_attempts=3)
        assert first.abandoned == [doomed.run_id]
        record = registry.get(doomed.run_id).to_dict()

        second = reconcile(registry, queue, grace=0, max_attempts=3)
        assert second.stranded == 0, "a run already given up on must not be revisited"
        assert registry.get(doomed.run_id).to_dict() == record
        assert queue.depth() == 0


# --- end to end -------------------------------------------------------------


@test
def test_a_stranded_run_is_recovered_and_completed():
    """The whole point: a run whose job was lost still finishes."""
    with platform() as (registry, queue):
        run = submitted(registry, queue, "item_processor", {"count": 3})

        # Lose the job the way a purge or a flushed Redis would.
        queue.purge()
        assert queue.known_run_ids() == set()
        assert registry.get(run.run_id).status is RunStatus.PENDING

        result = subprocess.run(
            [
                sys.executable, str(CONSUMER),
                "--queue-url", str(queue.root.parent),
                "--registry-root", str(registry.root),
                # grace 0: the run was created moments ago, and the default
                # 60s window exists precisely to avoid judging runs that young.
                "--reconcile-grace", "0",
                "--drain", "--max-jobs", "2", "--poll-timeout", "0.2",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, result.stderr

        final = registry.get(run.run_id)
        assert final.status is RunStatus.SUCCEEDED, f"{final.status.value}: {final.error}"
        assert len(registry.get_output(run.run_id)) == 3
        assert queue.depth() == 0 and queue.inflight() == 0
        assert "stranded" in result.stderr, "the Worker should say why it picked this up"


def main() -> int:
    failures = 0
    print(f"running {len(TESTS)} reconciler tests\n")

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

    print()
    print(f"{failures} of {len(TESTS)} tests FAILED" if failures else f"all {len(TESTS)} tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
