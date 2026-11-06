#!/usr/bin/env python3
"""At-least-once delivery tests.

The claim under test is that work is never lost: a Worker killed mid-run must
not strand its job. So one test really does SIGKILL a Worker while a container
is running, and the rest exercise the mechanics that make that recoverable --
leases, ack, recovery sweeps, idempotency and the give-up ceiling.

Needs Docker. Re-execs into .venv.

    python3 test_delivery.py
"""

from __future__ import annotations

import os
import subprocess
import sys
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

from jobqueue.base import Job, open_queue, owner_id, owner_is_dead  # noqa: E402
from jobqueue.file_queue import FileJobQueue  # noqa: E402
from registry.run_registry import RunRegistry  # noqa: E402
from shared.models import RunStatus  # noqa: E402

REGISTRY = RunRegistry()
CONSUMER = REPO_ROOT / "worker" / "consumer.py"

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


@contextmanager
def temp_queue(name: str):
    """A queue of its own, so a test never disturbs the real one."""
    queue = FileJobQueue(name=f"test-{name}")
    queue.purge()
    try:
        yield queue
    finally:
        queue.purge()


def enqueue_run(queue, actor: str, payload: dict):
    run = REGISTRY.create(actor, payload)
    queue.enqueue(Job(run_id=run.run_id, actor=actor, input=payload))
    return run.run_id


@contextmanager
def worker(queue: FileJobQueue, *, max_jobs: int = 1, drain: bool = False):
    """A Worker subprocess that is ALWAYS reaped.

    A leaked Worker is not a harmless test artefact: it keeps polling the same
    queue, so it silently steals the next run's job and the next test hangs
    waiting for its own Worker to do work that was already done. That happened.
    """
    process = start_worker(queue, max_jobs=max_jobs, drain=drain)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)


def start_worker(queue: FileJobQueue, *, max_jobs: int = 1, drain: bool = False):
    args = [
        sys.executable, str(CONSUMER),
        # BOTH are needed: --queue-url is only the root directory, so without
        # --queue-name the Worker would read the DEFAULT queue in that root.
        "--queue-url", str(queue.root.parent),
        "--queue-name", queue.name,
        # These Workers use a throwaway queue but the SHARED registry, so a
        # stranded-run sweep would requeue unrelated runs into this test's
        # queue and break its counts. Leasing is what is under test here;
        # reconciliation has its own suite.
        "--reconcile-interval", "0",
        "--max-jobs", str(max_jobs),
        "--poll-timeout", "0.2",
    ]
    if drain:
        args.append("--drain")
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for(run_id: str, predicate, *, seconds: float = 90.0, what: str = "condition"):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        run = REGISTRY.get(run_id)
        if predicate(run):
            return run
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never reached {what}; it is {REGISTRY.get(run_id).status.value}")


# --- the headline: a real crash ---------------------------------------------


@test
def test_a_killed_worker_does_not_lose_its_run():
    """SIGKILL a Worker mid-run; a new Worker must finish the job."""
    with temp_queue("crash") as queue:
        # delay_ms holds the container open long enough to kill the Worker
        # while it is genuinely mid-run rather than racing its startup.
        run_id = enqueue_run(queue, "item_processor", {"count": 8, "delay_ms": 700})

        with worker(queue) as first:
            wait_for(run_id, lambda r: r.status is RunStatus.RUNNING, what="RUNNING")
            first.kill()       # SIGKILL: no cleanup, no ack, no signal handler
            first.wait(timeout=30)

        # The record still claims RUNNING and the job is leased, not lost.
        stranded = REGISTRY.get(run_id)
        assert stranded.status is RunStatus.RUNNING, stranded.status
        assert stranded.attempts == 1, stranded.attempts
        assert queue.inflight() == 1, "the job vanished when the Worker died"
        assert queue.depth() == 0

        # A replacement Worker sweeps on startup, finds a lease whose owner is
        # dead, and retries it. --drain so it cannot outlive the job.
        with worker(queue, drain=True) as replacement:
            run = wait_for(run_id, lambda r: r.is_terminal, what="a terminal state")
            replacement.wait(timeout=90)

        assert run.status is RunStatus.SUCCEEDED, f"{run.status.value}: {run.error}"
        assert run.attempts == 2, f"expected a second attempt, got {run.attempts}"
        assert len(REGISTRY.get_output(run_id)) == 8
        assert queue.depth() == 0 and queue.inflight() == 0, "the job was not acked"


# --- the mechanics ----------------------------------------------------------


@test
def test_dequeue_leases_rather_than_deletes():
    with temp_queue("lease") as queue:
        queue.enqueue(Job(run_id="20260101-000001-aaaaaa", actor="hello_world"))
        assert (queue.depth(), queue.inflight()) == (1, 0)

        job = queue.dequeue()
        assert job is not None
        assert (queue.depth(), queue.inflight()) == (0, 1), "a dequeued job must still exist"
        assert job.receipt, "the delivery must be identifiable so it can be acked"

        assert queue.ack(job) is True
        assert (queue.depth(), queue.inflight()) == (0, 0)
        assert queue.ack(job) is False, "acking twice must report the lease was already gone"


@test
def test_recover_reclaims_only_abandoned_leases():
    with temp_queue("recover") as queue:
        queue.enqueue(Job(run_id="20260101-000001-aaaaaa", actor="hello_world"))
        job = queue.dequeue()

        # This process owns the lease and is very much alive.
        assert queue.recover() == 0, "a live owner's lease must not be reclaimed"
        assert queue.inflight() == 1

        # Re-point the lease at a pid that cannot exist.
        lease = queue.claimed / job.receipt
        owner_field = lease.name.split("__")[1]
        os.replace(lease, queue.claimed / lease.name.replace(owner_field, "nosuchhost~999999"))

        # A different host: liveness is unknowable, so the deadline decides.
        assert queue.recover() == 0, "an unknown owner must wait for the timeout"
        assert queue.recover(visibility_timeout=-1) == 1, "an expired lease must be reclaimed"
        assert (queue.depth(), queue.inflight()) == (1, 0)

        again = queue.dequeue()
        assert again.attempts == 1, "a redelivered job must count the attempt"


@test
def test_reclaimed_jobs_keep_their_place_in_the_queue():
    with temp_queue("fifo") as queue:
        queue.enqueue(Job(run_id="20260101-000001-aaaaaa", actor="hello_world"))
        first = queue.dequeue()
        queue.enqueue(Job(run_id="20260101-000002-bbbbbb", actor="hello_world"))

        lease = queue.claimed / first.receipt
        owner_field = lease.name.split("__")[1]
        os.replace(lease, queue.claimed / lease.name.replace(owner_field, "nosuchhost~999999"))
        assert queue.recover(visibility_timeout=-1) == 1

        # The reclaimed job waited longest, so it is served before the newer one.
        assert queue.dequeue().run_id == "20260101-000001-aaaaaa"
        assert queue.dequeue().run_id == "20260101-000002-bbbbbb"


@test
def test_owner_liveness_is_only_trusted_when_knowable():
    assert not owner_is_dead(owner_id()), "this process is alive"
    host = owner_id().rsplit(":", 1)[0]
    assert owner_is_dead(f"{host}:999999"), "a dead pid on this host is detectable"
    # Never guess about another host: reclaiming a live Worker's job would run
    # the same Actor twice.
    assert not owner_is_dead("someotherhost:1")
    assert not owner_is_dead("garbage")


# --- idempotency and giving up ----------------------------------------------


@test
def test_duplicate_delivery_does_not_run_the_actor_twice():
    """At-least-once means a finished run can be delivered again."""
    with temp_queue("duplicate") as queue:
        run_id = enqueue_run(queue, "hello_world", {"name": "Once"})
        with worker(queue, drain=True) as first:
            first.wait(timeout=90)

        done = wait_for(run_id, lambda r: r.is_terminal, what="a terminal state")
        assert done.status is RunStatus.SUCCEEDED
        finished_first, attempts_first = done.finished_at, done.attempts

        # Redeliver the very same job, as a reclaimed lease would.
        queue.enqueue(Job(run_id=run_id, actor="hello_world", input={"name": "Once"}, attempts=1))
        with worker(queue, drain=True) as second:
            second.wait(timeout=90)

        after = REGISTRY.get(run_id)
        assert after.finished_at == finished_first, "the Actor was run a second time"
        assert after.attempts == attempts_first, "a duplicate must not count as an attempt"
        assert after.status is RunStatus.SUCCEEDED
        assert queue.depth() == 0 and queue.inflight() == 0, "the duplicate was not acked"


@test
def test_a_job_is_given_up_on_after_max_attempts():
    """Without a ceiling, a job that kills its Worker retries for ever."""
    with temp_queue("giveup") as queue:
        run_id = enqueue_run(queue, "hello_world", {"name": "Doomed"})

        # Present it as a job that has already burned its attempts.
        queue.purge()
        queue.enqueue(Job(run_id=run_id, actor="hello_world", input={"name": "Doomed"}, attempts=3))

        with worker(queue, drain=True) as process:
            process.wait(timeout=90)

        run = REGISTRY.get(run_id)
        # CANCELLED, not PLATFORM_ERROR: the platform gave up retrying an
        # interrupted attempt, which is now labelled as itself.
        assert run.status is RunStatus.CANCELLED, run.status
        assert "abandoned after 3" in (run.error or ""), run.error
        assert run.attempts == 0, "it was never actually started"
        assert queue.depth() == 0 and queue.inflight() == 0, "the doomed job must be acked"


@test
def test_redis_behaves_identically_to_the_file_queue():
    """The lease/ack/recover cycle is a CONTRACT, not a file-queue detail.

    Skipped when no Redis is reachable, because the platform's default needs
    no server -- but when one is there, both implementations must agree.
    """
    try:
        from jobqueue.redis_queue import RedisJobQueue

        queue = RedisJobQueue("redis://127.0.0.1:6379/0", name="test-parity")
    except Exception as exc:  # noqa: BLE001
        print(f"        (skipped: no redis - {str(exc)[:60]})")
        return

    queue.purge()
    try:
        queue.enqueue(Job(run_id="20260101-000001-aaaaaa", actor="hello_world"))
        assert (queue.depth(), queue.inflight()) == (1, 0)

        job = queue.dequeue()
        assert (queue.depth(), queue.inflight()) == (0, 1), "dequeue must lease, not delete"
        assert queue.ack(job) is True
        assert (queue.depth(), queue.inflight()) == (0, 0)
        assert queue.ack(job) is False

        queue.enqueue(Job(run_id="20260101-000002-bbbbbb", actor="hello_world"))
        leased = queue.dequeue()
        assert queue.recover() == 0, "a live owner's lease must survive"

        queue._client.hset(queue.leases_key, leased.receipt, f"otherhost:1|{time.time()}")
        assert queue.recover() == 0, "an unknown owner must wait for the timeout"
        assert queue.recover(visibility_timeout=-1) == 1
        assert queue.dequeue().attempts == 1, "a redelivered job must count the attempt"
    finally:
        queue.purge()
        queue.close()


def main() -> int:
    failures = 0
    print(f"running {len(TESTS)} delivery tests\n")

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
