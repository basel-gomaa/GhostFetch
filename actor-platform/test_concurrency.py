#!/usr/bin/env python3
"""Worker concurrency and resource control.

The claim under test is CONTROLLED, BOUNDED, RECOVERABLE concurrent execution:

    * at most --concurrency containers run at once, and the rest wait
    * --cpus / --memory are enforced by the KERNEL, not recorded as metadata
    * an Actor is NEVER killed for taking too long, by the executor or the queue
    * a per-Actor limit holds behind the global one
    * a Worker killed with several runs active loses nothing and duplicates
      nothing
    * the reconciler stays safe while runs are active

Timing and live container counts are measured rather than inferred: a suite
that only checked final statuses could not tell parallel execution from
sequential execution, which is the whole point.

Needs Docker. Re-execs into .venv. Runs in its own sandbox registry and queue --
a shared registry is what previously let one suite's stranded runs be requeued
into another's.

    python3 test_concurrency.py
"""

from __future__ import annotations

import atexit
import dataclasses
import json
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
from jobqueue.base import Job, open_queue  # noqa: E402
from jobqueue.file_queue import FileJobQueue  # noqa: E402
from registry.run_registry import RunRegistry  # noqa: E402
from shared.models import RunStatus  # noqa: E402
from worker.executor import (  # noqa: E402
    _container_args,
    container_exists,
    WorkerError,
    ensure_image,
    kill_container,
)
from worker.limits import (  # noqa: E402
    ActorProfile,
    ExecutionLimits,
    LimitError,
    WorkerConfig,
    load_actor_config,
)
from worker.reconciler import reconcile  # noqa: E402

_SANDBOX = Path(tempfile.mkdtemp(prefix="actor-concurrency-tests-"))
atexit.register(shutil.rmtree, _SANDBOX, ignore_errors=True)

REGISTRY = RunRegistry(_SANDBOX / "runs")
QUEUE = open_queue(str(_SANDBOX / "queue"))

API_KEY = generate_key()
CLIENT = TestClient(
    create_app(registry=REGISTRY, queue=QUEUE, auth=AuthPolicy.for_keys({API_KEY})),
    headers={"Authorization": f"Bearer {API_KEY}"},
)

CONSUMER = REPO_ROOT / "worker" / "consumer.py"
ACTOR = "item_processor"

# ~2.5s of wall clock with no CPU to speak of: the deterministic unit of work
# this suite times things in. Not a new Actor -- delay_ms already exists so a
# run can be held open long enough to stage a crash.
SLOW = {"count": 1, "delay_ms": 2000}
QUICK = {"count": 1}

TERMINAL = {"SUCCEEDED", "FAILED", "PLATFORM_ERROR"}

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


# --- harness ---------------------------------------------------------------


def start_worker(*, concurrency: int, max_jobs: int | None = None, drain: bool = True,
                 cpus: float | None = None, memory: str | None = None,
                 actor_config: str | None = None) -> subprocess.Popen:
    args = [
        sys.executable, str(CONSUMER),
        "--queue-url", str(_SANDBOX / "queue"),
        "--registry-root", str(_SANDBOX / "runs"),
        # This suite runs its own reconciler tests explicitly; a background
        # sweep would move work under the tests that are measuring it.
        "--reconcile-interval", "0",
        "--concurrency", str(concurrency),
        "--poll-timeout", "0.2",
    ]
    if max_jobs is not None:
        args += ["--max-jobs", str(max_jobs)]
    if drain:
        args.append("--drain")
    if actor_config is not None:
        args += ["--actor-config", actor_config]
    if cpus is not None:
        args += ["--cpus", str(cpus)]
    if memory is not None:
        args += ["--memory", memory]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@contextmanager
def worker(**kwargs):
    """A Worker subprocess that is ALWAYS reaped.

    A leaked Worker keeps polling the same queue and silently steals the next
    test's job, hanging it. That has happened before; it is not theoretical.
    """
    process = start_worker(**kwargs)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=60)


def enqueue(payload: dict, count: int = 1) -> list[str]:
    ids = []
    for _ in range(count):
        run = REGISTRY.create(ACTOR, payload)
        QUEUE.enqueue(Job(run_id=run.run_id, actor=ACTOR, input=payload))
        ids.append(run.run_id)
    return ids


def containers(run_ids=None) -> set[str]:
    """Live actor containers, optionally narrowed to a set of runs.

    Narrowing matters: another suite running at the same time would otherwise
    inflate this suite's peak and fail an assertion about its own concurrency.
    """
    result = subprocess.run(
        ["docker", "ps", "--filter", "name=actor-", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    live = {line for line in result.stdout.split() if line}
    if run_ids is None:
        return live
    return live & {f"actor-{r}" for r in run_ids}


def wait_for(run_id: str, predicate, *, seconds: float = 180.0, what: str = "condition"):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        run = REGISTRY.get(run_id)
        if predicate(run):
            return run
        time.sleep(0.05)
    raise AssertionError(
        f"run {run_id} never reached {what}; it is {REGISTRY.get(run_id).status.value}"
    )


def wait_all_terminal(run_ids, *, seconds: float = 300.0) -> list:
    return [
        wait_for(r, lambda run: run.is_terminal, seconds=seconds, what="a terminal status")
        for r in run_ids
    ]


def inspect_container(name: str, fmt: str, *, seconds: float = 90.0) -> list[str] | None:
    """Poll `docker inspect` until the container appears. -> the fields, or None."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        probe = subprocess.run(
            ["docker", "inspect", name, "--format", fmt], capture_output=True, text=True
        )
        if probe.returncode == 0 and probe.stdout.strip():
            return probe.stdout.split()
        time.sleep(0.05)
    return None


def cgroup_in_container(name: str, path: str) -> str | None:
    """Read a cgroup file from INSIDE the live container -- the kernel's own view.

    `docker inspect` reports what the daemon was ASKED for. This reports what
    the kernel is actually enforcing on the process, which is the difference
    between a limit and a note about a limit.
    """
    probe = subprocess.run(
        ["docker", "exec", name, "cat", path], capture_output=True, text=True
    )
    return probe.stdout.strip() if probe.returncode == 0 else None


class Sampler:
    """Counts live containers in the background. Records the PEAK, which is the
    only number that can prove a bound was never exceeded."""

    def __init__(self, run_ids, interval: float = 0.05) -> None:
        self.run_ids = run_ids
        self.interval = interval
        self.peak = 0
        self.samples = 0
        self.queue_depth_while_saturated = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            live = len(containers(self.run_ids))
            self.peak = max(self.peak, live)
            self.samples += 1
            if live >= 2:
                self.queue_depth_while_saturated = max(
                    self.queue_depth_while_saturated, QUEUE.depth()
                )
            self._stop.wait(self.interval)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)


# --- the measured burst -----------------------------------------------------
# One real run of 8 jobs at concurrency 4, measured once and asserted from four
# angles. Re-running it per assertion would quadruple the suite's wall clock for
# no extra information.

_BURST: dict | None = None
BURST_JOBS = 8
BURST_CONCURRENCY = 4


def burst() -> dict:
    global _BURST
    if _BURST is not None:
        return _BURST

    QUEUE.purge()
    run_ids = enqueue(SLOW, BURST_JOBS)

    started = time.monotonic()
    with Sampler(run_ids) as sampler:
        with worker(concurrency=BURST_CONCURRENCY, max_jobs=BURST_JOBS) as process:
            runs = wait_all_terminal(run_ids)
            process.wait(timeout=120)
    elapsed = time.monotonic() - started

    # What one run costs on this machine, so "parallel" is judged against
    # measured serial cost rather than a number guessed in advance.
    unit = max(r.duration_seconds or 0 for r in runs)

    _BURST = {
        "run_ids": run_ids,
        "runs": runs,
        "elapsed": elapsed,
        "peak": sampler.peak,
        "samples": sampler.samples,
        "depth_while_busy": sampler.queue_depth_while_saturated,
        "unit": unit,
    }
    return _BURST


# --- 1: the concurrency limit ----------------------------------------------


@test
def test_active_containers_never_exceed_the_concurrency_limit():
    b = burst()
    assert b["samples"] > 20, f"only {b['samples']} samples - the measurement is not credible"
    assert b["peak"] <= BURST_CONCURRENCY, (
        f"{b['peak']} containers ran at once with --concurrency {BURST_CONCURRENCY}"
    )


# --- 2: it is really parallel ----------------------------------------------


@test
def test_execution_is_parallel_not_sequential():
    """Timing, not status. Eight 2.5s runs finish in far under eight 2.5s runs."""
    b = burst()
    serial = BURST_JOBS * b["unit"]
    ceiling = serial * 0.75
    assert b["elapsed"] < ceiling, (
        f"{BURST_JOBS} runs took {b['elapsed']:.1f}s; sequential would be ~{serial:.1f}s "
        f"and this is not distinguishable from it"
    )
    assert b["peak"] >= 2, "never saw two containers at once - nothing ran in parallel"


# --- 3: the rest wait -------------------------------------------------------


@test
def test_jobs_wait_in_the_queue_for_a_slot():
    """8 jobs and 4 slots means 4 stay queued -- the Worker must not drain the
    queue into unlimited processes."""
    b = burst()
    assert b["depth_while_busy"] > 0, (
        "the queue was never non-empty while containers were running - "
        "every job was leased up front rather than waiting for a slot"
    )


# --- 15: everything finished -----------------------------------------------


@test
def test_every_job_in_the_burst_completes():
    b = burst()
    for run in b["runs"]:
        assert run.status is RunStatus.SUCCEEDED, f"{run.run_id}: {run.status.value} {run.error}"
        assert run.attempts == 1, f"run {run.run_id} was started {run.attempts} times"
    assert QUEUE.depth() == 0 and QUEUE.inflight() == 0, "jobs were left in the queue"


# --- 4: CPU is enforced by the kernel ---------------------------------------


@test
def test_the_cpu_limit_reaches_the_container_and_the_kernel():
    QUEUE.purge()
    run_id = enqueue({"count": 1, "delay_ms": 6000})[0]
    name = f"actor-{run_id}"

    with worker(concurrency=1, max_jobs=1, cpus=0.5):
        fields = inspect_container(name, "{{.HostConfig.NanoCpus}}")
        cpu_max = cgroup_in_container(name, "/sys/fs/cgroup/cpu.max") if fields else None
        wait_for(run_id, lambda r: r.is_terminal, what="a terminal status")

    assert fields is not None, "the container was never observed running"
    assert fields[0] == "500000000", f"docker was asked for {fields[0]} nanocpus, not 0.5 cpu"

    # The daemon's record above says what it was ASKED for. This says what the
    # kernel is doing: quota/period, so 0.5 cpu is 50000 out of every 100000us.
    assert cpu_max is not None, "could not read cpu.max from inside the container"
    quota, period = cpu_max.split()
    assert quota != "max", "the container's cgroup has NO cpu limit - it was only recorded"
    assert int(quota) / int(period) == 0.5, f"cgroup allows {quota}/{period}, not 0.5 cpu"


# --- 5: memory is enforced by the kernel ------------------------------------


@test
def test_the_memory_limit_reaches_the_container_and_the_kernel():
    QUEUE.purge()
    run_id = enqueue({"count": 1, "delay_ms": 6000})[0]
    name = f"actor-{run_id}"

    with worker(concurrency=1, max_jobs=1, memory="256m"):
        fields = inspect_container(name, "{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}")
        mem_max = cgroup_in_container(name, "/sys/fs/cgroup/memory.max") if fields else None
        swap_max = cgroup_in_container(name, "/sys/fs/cgroup/memory.swap.max") if fields else None
        run = wait_for(run_id, lambda r: r.is_terminal, what="a terminal status")

    assert fields is not None, "the container was never observed running"
    assert fields[0] == "268435456", f"docker was asked for {fields[0]} bytes, not 256m"
    # Equal to --memory, so the container gets NO swap. A lone --memory leaves
    # swap at 2x the limit and the "limit" can simply be swapped past.
    assert fields[1] == "268435456", f"memory-swap is {fields[1]} - the limit can be swapped past"

    assert mem_max == "268435456", f"the cgroup's memory.max is {mem_max}, not 256m"
    assert swap_max == "0", f"the cgroup allows {swap_max} bytes of swap, so the limit is soft"
    assert run.status is RunStatus.SUCCEEDED, f"256m should be ample: {run.error}"


@test
def test_a_memory_limit_actually_kills_a_container_that_exceeds_it():
    """The behavioural half: a limit below what the runtime needs must KILL it.

    12m is an ABSOLUTE limit, so this proves the same thing on a laptop and on
    a 1TB server -- it does not depend on how much memory the host has. The
    pair matters: the run above succeeds at 256m, this one dies at 12m, and
    only the two together show the number is doing something.
    """
    QUEUE.purge()
    run_id = enqueue(QUICK)[0]

    with worker(concurrency=1, max_jobs=1, memory="12m"):
        run = wait_for(run_id, lambda r: r.is_terminal, what="a terminal status")

    assert run.status is not RunStatus.SUCCEEDED, (
        "the actor completed under a 12m limit - the limit was not enforced"
    )
    # 137 = 128 + SIGKILL: the kernel's OOM killer, not a tidy exit.
    assert run.exit_code == 137, f"expected an OOM kill (137), got exit {run.exit_code}"
    assert not containers([run_id]), "the OOM-killed container was left behind"


# --- 6 and 7: the wall-clock limit is PER RUN, never a platform-wide guess ---


@test
def test_a_run_past_its_own_time_limit_is_stopped_and_says_so():
    """Behavioural. A run that asked for 1 minute but needs ~3 is stopped near
    the minute mark (plus the SIGTERM grace), recorded FAILED -- never
    PLATFORM_ERROR -- with an error naming the limit, and leaves no container.

    This replaced a test that pinned the ABSENCE of any timeout. Its concern
    still stands and is what the test below keeps guarding: a single platform-
    wide number would kill real 50-minute scrapes. Limits are therefore per
    Actor (config/actors.json) and per run, and a long run asks for more."""
    QUEUE.purge()
    payload = {"count": 6, "delay_ms": 30000}            # ~180s if left alone
    run = REGISTRY.create(ACTOR, payload, timeout_s=60)   # but it asked for 60s
    QUEUE.enqueue(Job(run_id=run.run_id, actor=ACTOR, input=payload))

    started = time.monotonic()
    with worker(concurrency=1, max_jobs=1):
        done = wait_for(run.run_id, lambda r: r.is_terminal, seconds=300, what="a terminal status")
    elapsed = time.monotonic() - started

    assert done.status is RunStatus.FAILED, f"{done.status.value}: {done.error}"
    assert "timed out after 1 min" in (done.error or ""), done.error
    assert done.timeout_s == 60, f"the record does not show the enforced limit: {done.timeout_s}"
    # 60s limit + up to 30s of SIGTERM grace + startup; the run itself needed 180s.
    assert elapsed < 150, f"took {elapsed:.0f}s - the limit was not enforced"
    assert not containers([run.run_id]), "the timed-out container was left behind"
    assert QUEUE.depth() == 0 and QUEUE.inflight() == 0


@test
def test_a_request_can_never_exceed_the_actors_maximum():
    """The clamp is the Worker's, whatever the record says: a run record is not
    trusted to hold what the API accepted (filesystem mode lets the container
    rewrite run.json), so an absurd request is cut to the Actor's maximum."""
    from worker.limits import ActorLimits, RunOptions

    limits = ActorLimits(timeout_minutes=10, max_timeout_minutes=30, memory_mb=512,
                         max_memory_mb=1024, cpus=1, max_cpus=1, pids=256)
    got = limits.resolve(RunOptions(timeout_s=10**9, memory_mb=10**9, cpus=64.0))
    assert (got.timeout_s, got.memory_mb, got.cpus, got.pids) == (1800, 1024, 1.0, 256), got
    # And the Worker's own --memory/--cpus stay a ceiling above every Actor.
    capped = limits.resolve(None, ExecutionLimits(cpus=0.5, memory="256m"))
    assert (capped.cpus, capped.memory) == (0.5, "256m"), capped
    # Nothing configured anywhere: exactly the old, unlimited envelope.
    assert ActorLimits().resolve(None, ExecutionLimits()) == ExecutionLimits()


@test
def test_an_actor_that_runs_far_longer_than_the_old_ceiling_is_not_killed():
    """Behavioural. The old platform-wide default killed at 300s; this proves a
    run inside its OWN limit sails past a comparable ceiling untouched. 90s
    rather than 300s to keep the suite usable."""
    QUEUE.purge()
    run_id = enqueue({"count": 3, "delay_ms": 30000})[0]     # ~90s

    started = time.monotonic()
    with worker(concurrency=1, max_jobs=1):
        run = wait_for(run_id, lambda r: r.is_terminal, seconds=300, what="a terminal status")
    elapsed = time.monotonic() - started

    assert elapsed > 60, f"the run only took {elapsed:.0f}s - it did not test anything"
    assert run.status is RunStatus.SUCCEEDED, f"{run.status.value}: {run.error}"
    assert run.duration_seconds > 60, run.duration_seconds
    assert QUEUE.depth() == 0 and QUEUE.inflight() == 0


@test
def test_a_live_lease_is_never_reclaimed_however_old_it_is():
    """The other half of the long-run guarantee, and the one that actually bit.

    Removing the executor's timeout is not enough: the queue used to reclaim any
    lease older than 900s, so a healthy 2-hour run had its container killed and
    restarted at the 15-minute mark. Proved here in milliseconds by AGEING a
    lease rather than earning one -- a live owner must keep its job whatever the
    clock says."""
    queue = FileJobQueue(_SANDBOX / "queue", name="lease-age")
    queue.purge()
    queue.enqueue(Job(run_id="20260101-000001000000-aaaaaa", actor=ACTOR, input={}))
    job = queue.dequeue()
    assert job is not None and queue.inflight() == 1

    # Rewrite the lease's claim time to a year ago. The owner (this process) is
    # very much alive.
    lease = queue.claimed / job.receipt
    stem, owner, _ = FileJobQueue._parse_lease(lease.name)
    aged = int(time.time()) - 365 * 24 * 3600
    lease.rename(queue.claimed / f"{stem}__{owner.replace(':', '~')}__{aged}.json")

    assert queue.recover() == 0, "a year-old lease with a LIVE owner was reclaimed"
    assert queue.inflight() == 1, "the live run lost its job"

    # An explicit timeout still works for an operator who wants one...
    assert queue.recover(visibility_timeout=60) == 1, "an explicit timeout was ignored"
    queue.purge()


# --- 8 and 9: a Worker killed with several runs active ----------------------


@test
def test_a_worker_killed_with_three_runs_active_loses_nothing():
    QUEUE.purge()
    run_ids = enqueue({"count": 1, "delay_ms": 25000}, 3)

    first = start_worker(concurrency=3, max_jobs=3)
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if len(containers(run_ids)) == 3:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("never saw three containers running at once")
        time.sleep(2)  # let all three get properly under way
        first.kill()
        first.wait(timeout=60)
    finally:
        if first.poll() is None:
            first.kill()

    for run_id in run_ids:
        run = REGISTRY.get(run_id)
        assert run.status is RunStatus.RUNNING, f"{run_id} is {run.status.value}"
        assert run.attempts == 1, run.attempts
    assert QUEUE.inflight() == 3, f"{QUEUE.inflight()} leases survived the crash, expected 3"
    assert QUEUE.depth() == 0

    _CRASHED["run_ids"] = run_ids


_CRASHED: dict = {}


@test
def test_a_replacement_worker_recovers_all_three_without_duplicating_them():
    run_ids = _CRASHED.get("run_ids")
    assert run_ids, "the crash test did not run"

    with worker(concurrency=3, max_jobs=3) as replacement:
        runs = wait_all_terminal(run_ids, seconds=300)
        replacement.wait(timeout=300)

    for run in runs:
        assert run.status is RunStatus.SUCCEEDED, f"{run.run_id}: {run.status.value} {run.error}"
        assert run.attempts == 2, f"expected a second attempt, got {run.attempts}"
        # One run, one result. Two containers writing one run directory would
        # show up here as a doubled array.
        assert REGISTRY.get_output(run.run_id) == [
            {"index": 1, "title": "Sample Item #1", "slug": "sample-item-1"}
        ], "the output looks like two containers wrote it"

    assert QUEUE.depth() == 0 and QUEUE.inflight() == 0, "the jobs were not acked"
    assert not containers(run_ids), "a container was left behind"


# --- 10: the reconciler while runs are active -------------------------------


@test
def test_the_reconciler_reclaims_nothing_while_runs_are_active():
    """Swept with grace=0, which strips away the age protection entirely and
    leaves only the real invariant: an active run holds a lease."""
    QUEUE.purge()
    finished = enqueue(QUICK)[0]
    with worker(concurrency=1, max_jobs=1):
        wait_for(finished, lambda r: r.is_terminal, what="a terminal status")
    finished_before = REGISTRY.get(finished)

    active = enqueue({"count": 1, "delay_ms": 20000}, 4)
    with worker(concurrency=4, max_jobs=4) as process:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and len(containers(active)) < 4:
            time.sleep(0.05)
        assert len(containers(active)) == 4, "never saw four containers at once"

        report = reconcile(REGISTRY, QUEUE, grace=0.0)
        peak_during = len(containers(active))

        runs = wait_all_terminal(active, seconds=300)
        process.wait(timeout=300)

    assert report.requeued == [], f"a live run was requeued: {report.requeued}"
    assert report.abandoned == [], f"a live run was abandoned: {report.abandoned}"
    assert peak_during <= 4, f"the sweep let {peak_during} containers run"

    for run in runs:
        assert run.status is RunStatus.SUCCEEDED, f"{run.run_id}: {run.status.value} {run.error}"
        assert run.attempts == 1, f"run {run.run_id} was executed {run.attempts} times"

    after = REGISTRY.get(finished)
    assert after.to_dict() == finished_before.to_dict(), "the sweep modified a terminal run"
    assert QUEUE.depth() == 0 and QUEUE.inflight() == 0


# --- 11: duplicate delivery under concurrency -------------------------------


@test
def test_a_job_delivered_twice_is_executed_once():
    QUEUE.purge()
    run = REGISTRY.create(ACTOR, SLOW)
    job = Job(run_id=run.run_id, actor=ACTOR, input=SLOW)
    QUEUE.enqueue(job)
    QUEUE.enqueue(job)
    assert QUEUE.depth() == 2

    with Sampler([run.run_id]) as sampler:
        with worker(concurrency=4, max_jobs=2) as process:
            finished = wait_for(
                run.run_id, lambda r: r.is_terminal, what="a terminal status"
            )
            process.wait(timeout=120)

    assert finished.status is RunStatus.SUCCEEDED, f"{finished.status.value}: {finished.error}"
    assert finished.attempts == 1, (
        f"the run was started {finished.attempts} times - both deliveries executed it"
    )
    assert sampler.peak <= 1, f"{sampler.peak} containers ran for ONE run"
    assert REGISTRY.get_output(run.run_id) == [
        {"index": 1, "title": "Sample Item #1", "slug": "sample-item-1"}
    ]
    assert QUEUE.depth() == 0 and QUEUE.inflight() == 0, "a delivery was not acked"


# --- 12: nothing is left behind ---------------------------------------------


@test
def test_no_container_from_this_suite_survives():
    every_run = [r.run_id for r in REGISTRY.list(limit=500)]
    assert every_run, "no runs were recorded"
    leaked = [r for r in every_run if container_exists(r)]
    assert not leaked, f"{len(leaked)} container(s) leaked: {leaked[:5]}"


# --- 13: the API stays asynchronous -----------------------------------------


@test
def test_twelve_rapid_api_submissions_are_all_accepted_and_bounded():
    QUEUE.purge()
    count = 12

    started = time.monotonic()
    responses = [CLIENT.post(f"/actors/{ACTOR}/runs", json=QUICK) for _ in range(count)]
    submit_time = time.monotonic() - started

    assert all(r.status_code == 202 for r in responses), [r.status_code for r in responses]
    run_ids = [r.json()["run_id"] for r in responses]
    assert len(set(run_ids)) == count, "run ids collided"

    # The API registers and enqueues; it must never wait for Docker. 12 runs of
    # ~1s each could not possibly have been executed in this window.
    assert submit_time < 2.0, f"{count} submissions took {submit_time:.1f}s - the API is executing"
    assert all(r.json()["status"] == "PENDING" for r in responses)
    assert QUEUE.depth() == count, f"{QUEUE.depth()} jobs queued, expected {count}"

    with Sampler(run_ids) as sampler:
        with worker(concurrency=3, max_jobs=count) as process:
            runs = wait_all_terminal(run_ids, seconds=300)
            process.wait(timeout=300)

    assert sampler.peak <= 3, f"{sampler.peak} containers ran with --concurrency 3"
    for run in runs:
        assert run.status is RunStatus.SUCCEEDED, f"{run.run_id}: {run.status.value} {run.error}"
    assert QUEUE.depth() == 0 and QUEUE.inflight() == 0


# --- 14: authentication is untouched ----------------------------------------


@test
def test_authentication_is_still_enforced():
    QUEUE.purge()
    before = len(REGISTRY.list(limit=500))

    anonymous = TestClient(
        create_app(registry=REGISTRY, queue=QUEUE, auth=AuthPolicy.for_keys({API_KEY}))
    )
    response = anonymous.post(f"/actors/{ACTOR}/runs", json=QUICK)

    assert response.status_code == 401, response.status_code
    assert QUEUE.depth() == 0, "an unauthenticated request queued a job"
    assert len(REGISTRY.list(limit=500)) == before, "an unauthenticated request created a run"


# --- 15/structural: the unlimited default is byte-identical -----------------


@test
def test_no_limits_means_exactly_the_command_line_that_came_before():
    args = Path("/tmp/probe")
    unlimited = _container_args(ACTOR, "20260101-000001-aaaaaa", args)
    explicit = _container_args(ACTOR, "20260101-000001-aaaaaa", args, None)
    assert unlimited == explicit

    for flag in ("--cpus", "--memory", "--memory-swap"):
        assert flag not in unlimited, f"{flag} appears with no limits configured"

    limited = _container_args(
        ACTOR, "20260101-000001-aaaaaa", args, ExecutionLimits(cpus=0.5, memory="256m")
    )
    assert [a for a in limited if a not in unlimited] == [
        "--cpus", "0.5", "--memory", "256m", "--memory-swap", "256m",
    ]
    # The image is always last, or docker reads the flags as the actor's argv.
    assert limited[-1].endswith(f"/{ACTOR}:latest")


@test
def test_every_container_is_hardened_and_isolated():
    """Security review M-02: every Actor container drops all capabilities,
    cannot gain privileges, and -- given the executor's network -- joins the
    isolated actor bridge rather than Docker's shared default one. The pids and
    /dev/shm caps come from the limits, like CPU and memory."""
    import worker.executor as executor_module

    plain = _container_args(ACTOR, "20260101-000001-aaaaaa", Path("/tmp/probe"))
    i = plain.index("--cap-drop")
    assert plain[i + 1] == "ALL", plain
    j = plain.index("--security-opt")
    assert plain[j + 1] == "no-new-privileges", plain

    joined = _container_args(ACTOR, "20260101-000001-aaaaaa", Path("/tmp/probe"),
                             network=executor_module.ACTOR_NETWORK)
    assert joined[joined.index("--network") + 1] == executor_module.ACTOR_NETWORK, joined
    host = _container_args(ACTOR, "20260101-000001-aaaaaa", Path("/tmp/probe"),
                           host_network=True, network=executor_module.ACTOR_NETWORK)
    assert host.count("--network") == 1 and host[host.index("--network") + 1] == "host", host

    capped = _container_args(ACTOR, "20260101-000001-aaaaaa", Path("/tmp/probe"),
                             ExecutionLimits(pids=256, shm="1024m"))
    assert capped[capped.index("--pids-limit") + 1] == "256", capped
    assert capped[capped.index("--shm-size") + 1] == "1024m", capped

    # And the network the executor creates really has inter-container traffic off.
    executor_module.ensure_actor_network()
    probe = subprocess.run(
        ["docker", "network", "inspect", executor_module.ACTOR_NETWORK, "--format",
         '{{index .Options "com.docker.network.bridge.enable_icc"}}|'
         '{{index .Options "com.docker.network.bridge.name"}}'],
        capture_output=True, text=True,
    )
    # The FIXED interface name is what deploy/host-firewall.sh matches (N-01).
    assert probe.stdout.strip() == f"false|{executor_module.ACTOR_BRIDGE}", probe.stdout + probe.stderr


@test
def test_an_unusable_limit_is_refused_before_anything_starts():
    for kwargs in ({"cpus": 0}, {"cpus": -1}, {"memory": "5m"}, {"memory": "big"}):
        try:
            ExecutionLimits(**kwargs)
        except LimitError:
            continue
        raise AssertionError(f"{kwargs} was accepted")

    try:
        WorkerConfig(concurrency=0)
    except LimitError:
        pass
    else:
        raise AssertionError("concurrency 0 was accepted")

    probe = subprocess.run(
        [sys.executable, str(CONSUMER), "--memory", "3m", "--drain",
         "--queue-url", str(_SANDBOX / "queue"), "--registry-root", str(_SANDBOX / "runs")],
        capture_output=True, text=True,
    )
    assert probe.returncode == 2, f"a bad limit exited {probe.returncode}, expected 2"
    assert "minimum" in probe.stderr


# --- 16: the build stampede -------------------------------------------------


def _stampede(initial_reason: str, exists: bool) -> list[str]:
    """Four threads want one image that needs building; return the builds run."""
    import worker.executor as executor

    builds: list[str] = []
    lock = threading.Lock()
    state = {"reason": initial_reason, "exists": exists}

    def fake_build(actor: str, context_hash: str | None = None) -> None:
        with lock:
            builds.append(actor)
        time.sleep(0.2)          # a real build is not instantaneous
        state["reason"], state["exists"] = None, True

    original_build, original_stale = executor.build_image, executor.image_staleness
    executor.build_image = fake_build
    executor.image_staleness = lambda actor: ("v1:test", state["reason"], state["exists"])
    try:
        threads = [
            threading.Thread(target=ensure_image, args=("hello_world",), kwargs={"force_build": False})
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    finally:
        executor.build_image, executor.image_staleness = original_build, original_stale
        executor._BUILD_LOCKS.pop("hello_world", None)
    return builds


@test
def test_a_cold_image_is_built_once_however_many_threads_want_it():
    """Without the per-actor lock every thread passes the staleness check and
    every thread runs `docker build` for the same tag."""
    builds = _stampede("missing", exists=False)
    assert builds == ["hello_world"], f"the image was built {len(builds)} times"


@test
def test_a_stale_image_is_rebuilt_once_however_many_threads_want_it():
    """The same lock covers a REBUILD: an image whose sources changed is rebuilt
    by the first thread, and the others find it current inside the lock."""
    builds = _stampede("context changed", exists=True)
    assert builds == ["hello_world"], f"the image was rebuilt {len(builds)} times"


@test
def test_a_changed_build_context_rebuilds_its_image():
    """LIVE: the 24 October failure, end to end. An image used to be built only
    when missing, so a fix to its sources never reached it.

    On a scratch copy of hello_world under its own tag, so no real image is
    touched: the image is labelled with the context hash; an unchanged context
    reuses it; an edit rebuilds it and the run shows the new code; a rebuild
    that fails runs the old image and the run's log says so first; and with no
    image at all a failed build is still a refusal to run."""
    import worker.executor as ex
    from shared import image_freshness as freshness

    actor = "imagefreshtest"
    ctx = _SANDBOX / "contexts" / actor
    shutil.copytree(REPO_ROOT / "actors" / "hello_world", ctx,
                    ignore=shutil.ignore_patterns("__pycache__"))
    tag = ex.image_tag(actor)
    saved = ex.actor_dir
    ex.actor_dir = lambda a: ctx if a == actor else saved(a)

    def image_id() -> str:
        return subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", tag],
                              capture_output=True, text=True).stdout.strip()

    def run(name: str):
        r = ex.run_actor(actor, {"name": name}, registry=REGISTRY)
        return r, REGISTRY.get_logs(r.run_id)

    try:
        first, _ = run("one")
        assert first.status is RunStatus.SUCCEEDED, first
        assert freshness.image_label(tag) == (True, freshness.context_hash(ctx))
        built = image_id()

        again, _ = run("two")
        assert again.status is RunStatus.SUCCEEDED and image_id() == built, \
            "an unchanged context rebuilt its image"

        code = ctx / "actor.py"
        code.write_text(code.read_text().replace(
            "from typing import Any\n",
            "from typing import Any\n\nprint('[hello_world] edited-marker', flush=True)\n", 1))
        edited, logs = run("three")
        assert edited.status is RunStatus.SUCCEEDED, edited
        assert image_id() != built, "an edited context did not rebuild its image"
        assert "edited-marker" in logs, "the run did not use the rebuilt image"
        assert freshness.image_label(tag) == (True, freshness.context_hash(ctx))

        (ctx / "Dockerfile").write_text((ctx / "Dockerfile").read_text() + "RUN false\n")
        stale, logs = run("four")
        assert stale.status is RunStatus.SUCCEEDED, "a failed rebuild did not fall back"
        assert logs.splitlines()[0].startswith("[platform] WARNING:"), logs[:300]
        assert "edited-marker" in logs, "the fallback was not the previous image"

        subprocess.run(["docker", "image", "rm", "-f", tag], capture_output=True)
        try:
            run("five")
        except WorkerError:
            pass
        else:
            raise AssertionError("a run started with no image and a failing build")
    finally:
        ex.actor_dir = saved
        subprocess.run(["docker", "image", "rm", "-f", tag], capture_output=True)


# --- 17: killing a container leaves nothing behind --------------------------


@test
def test_kill_container_leaves_no_container_holding_the_name():
    """The postcondition recovery depends on: once kill_container returns True,
    nothing holds that run's name, so the retry can start its own container.

    This does NOT prove the async-removal race is handled, because that race
    does not reproduce here -- measured on docker 29.7.2, `docker kill` took
    ~330ms and the name was already free on return, 6 times out of 6. What it
    does prove is that container_exists() really tracks a container's lifetime
    and that kill_container() removes one; the bounded wait inside it is
    insurance for a Docker that behaves differently, not a demonstrated fix."""
    QUEUE.purge()
    run_id = enqueue({"count": 1, "delay_ms": 30000})[0]

    with worker(concurrency=1, max_jobs=1) as process:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not containers([run_id]):
            time.sleep(0.05)
        assert containers([run_id]), "the container never started"
        assert container_exists(run_id), "container_exists cannot see a live container"

        assert kill_container(run_id) is True
        # No sleep: the postcondition must hold the moment it returns.
        assert not container_exists(run_id), "the name is still taken after kill_container"
        assert not containers([run_id]), "the container is still running"

        process.kill()
        process.wait(timeout=60)

    assert kill_container(run_id) is False, "killing a gone container reported success"


# --- per-Actor concurrency ---------------------------------------------------
# The acceptance scenario, with item_processor standing in for the Actor that
# must not run twice. It is capped at 1 while hello_world is uncapped, which is
# exactly the facebook/hello_world shape the real config uses.

CAPPED = ACTOR                # item_processor, capped at 1 below
UNCAPPED = "hello_world"      # bounded only by the global limit

_PER_ACTOR_CONFIG = _SANDBOX / "per-actor.json"
_PER_ACTOR_CONFIG.write_text(json.dumps({
    "global": {"max_concurrency": 6},
    "actors": {CAPPED: {"max_concurrency": 1}},
}))


def enqueue_actor(actor: str, payload: dict, count: int = 1) -> list[str]:
    ids = []
    for _ in range(count):
        run = REGISTRY.create(actor, payload)
        QUEUE.enqueue(Job(run_id=run.run_id, actor=actor, input=payload))
        ids.append(run.run_id)
    return ids


class ActorSampler:
    """Peaks per Actor as well as overall. Both bounds have to be watched at
    once, because satisfying either alone proves nothing."""

    def __init__(self, owners: dict[str, str], interval: float = 0.04) -> None:
        self.owners = owners                      # run_id -> actor
        self.interval = interval
        self.peak_total = 0
        self.peak_by_actor: dict[str, int] = {}
        self.samples = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            mine = [n[len("actor-"):] for n in containers(self.owners)]
            self.peak_total = max(self.peak_total, len(mine))
            for actor in set(self.owners.values()):
                live = sum(1 for r in mine if self.owners.get(r) == actor)
                self.peak_by_actor[actor] = max(self.peak_by_actor.get(actor, 0), live)
            self.samples += 1
            self._stop.wait(self.interval)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)


_ACCEPTANCE: dict = {}


def acceptance() -> dict:
    """THE acceptance scenario, measured once: global 6, capped Actor 1.

    The capped jobs are enqueued FIRST and therefore sit at the head of the
    queue. If the limit were enforced by holding leases rather than by not
    taking them, they would block everything behind them.
    """
    if _ACCEPTANCE:
        return _ACCEPTANCE

    QUEUE.purge()
    owners: dict[str, str] = {}
    for run_id in enqueue_actor(CAPPED, {"count": 1, "delay_ms": 2000}, 3):
        owners[run_id] = CAPPED
    for run_id in enqueue_actor(UNCAPPED, {"name": "Basel"}, 6):
        owners[run_id] = UNCAPPED

    started = time.monotonic()
    with ActorSampler(owners) as sampler:
        with worker(concurrency=6, max_jobs=9, actor_config=str(_PER_ACTOR_CONFIG)) as process:
            runs = wait_all_terminal(list(owners), seconds=300)
            process.wait(timeout=300)

    _ACCEPTANCE.update({
        "owners": owners, "runs": runs, "sampler": sampler,
        "elapsed": time.monotonic() - started,
    })
    return _ACCEPTANCE


@test
def test_a_capped_actor_never_runs_more_than_its_limit():
    a = acceptance()
    peak = a["sampler"].peak_by_actor.get(CAPPED, 0)
    assert a["sampler"].samples > 20, "too few samples to believe"
    assert peak == 1, f"{peak} {CAPPED} containers ran at once with max_concurrency 1"


@test
def test_the_global_limit_still_holds_alongside_the_per_actor_one():
    a = acceptance()
    assert a["sampler"].peak_total <= 6, f"{a['sampler'].peak_total} containers ran with global 6"


@test
def test_a_capped_actor_does_not_block_the_rest_of_the_capacity():
    """The fairness claim: three capped jobs at the HEAD of the queue must not
    stop uncapped work behind them from using the free slots."""
    a = acceptance()
    peak = a["sampler"].peak_by_actor.get(UNCAPPED, 0)
    assert peak >= 2, (
        f"only {peak} {UNCAPPED} container(s) ever ran - the capped Actor at the head "
        "of the queue blocked the others instead of being skipped"
    )


@test
def test_every_run_in_the_acceptance_scenario_completes():
    a = acceptance()
    for run in a["runs"]:
        assert run.status is RunStatus.SUCCEEDED, f"{run.run_id}: {run.status.value} {run.error}"
        assert run.attempts == 1, f"run {run.run_id} was started {run.attempts} times"
    assert QUEUE.depth() == 0 and QUEUE.inflight() == 0


@test
def test_a_capped_actors_queued_jobs_are_never_leased():
    """The mechanism, not just the outcome. A job the Worker cannot run must
    stay PENDING in the queue -- visible to another Worker and to the
    reconciler -- rather than being leased and held."""
    QUEUE.purge()
    run_ids = enqueue_actor(CAPPED, {"count": 1, "delay_ms": 8000}, 3)

    with worker(concurrency=6, max_jobs=3, actor_config=str(_PER_ACTOR_CONFIG)) as process:
        deadline = time.monotonic() + 120
        observed = None
        while time.monotonic() < deadline:
            if len(containers(run_ids)) == 1:
                observed = (QUEUE.depth(), QUEUE.inflight())
                break
            time.sleep(0.05)
        assert observed is not None, "never saw the one permitted container"
        runs = wait_all_terminal(run_ids, seconds=300)
        process.wait(timeout=300)

    depth, inflight = observed
    assert inflight == 1, f"{inflight} jobs were leased while only one could run"
    assert depth == 2, f"{depth} jobs waiting; the other two should still be PENDING"
    for run in runs:
        assert run.status is RunStatus.SUCCEEDED, f"{run.run_id}: {run.error}"


@test
def test_slots_are_released_after_a_failure_not_only_after_success():
    """A failing Actor must hand back both slots. Two waves through a limit of
    1: if the first wave leaked, the second would never start."""
    QUEUE.purge()
    failing = enqueue_actor(CAPPED, {"count": 5, "fail_at": 2}, 2)
    succeeding = enqueue_actor(CAPPED, {"count": 1}, 2)

    with worker(concurrency=6, max_jobs=4, actor_config=str(_PER_ACTOR_CONFIG)) as process:
        runs = wait_all_terminal(failing + succeeding, seconds=300)
        process.wait(timeout=300)

    by_id = {r.run_id: r for r in runs}
    for run_id in failing:
        assert by_id[run_id].status is RunStatus.FAILED, by_id[run_id].status.value
        assert by_id[run_id].exit_code == 1
    for run_id in succeeding:
        assert by_id[run_id].status is RunStatus.SUCCEEDED, (
            f"a run after two failures never started - a failed run leaked its slot"
        )
    assert QUEUE.depth() == 0 and QUEUE.inflight() == 0


@test
def test_an_unconfigured_actor_is_bounded_only_by_the_global_limit():
    """The default has to be real, or adding a config file would silently
    throttle every Actor that has no entry."""
    _, defaults, actors = load_actor_config(_PER_ACTOR_CONFIG)
    assert defaults.max_concurrency is None
    assert UNCAPPED not in actors

    config = WorkerConfig.load(path=_PER_ACTOR_CONFIG)
    assert config.limits_for(UNCAPPED).max_concurrency is None
    assert config.limits_for(CAPPED).max_concurrency == 1
    assert config.concurrency == 6, "global.max_concurrency was not read"

    a = acceptance()
    assert a["sampler"].peak_by_actor.get(UNCAPPED, 0) > 1, (
        "an Actor with no entry was throttled anyway"
    )


@test
def test_the_actor_config_is_validated_rather_than_guessed_at():
    missing = _SANDBOX / "nope.json"
    assert load_actor_config(missing) == (None, ActorProfile(), {}), (
        "a missing config must mean 'no limits', not an error"
    )

    for bad, why in (
        ({"actors": {"x": {"concurrency": 2}}}, "a typo'd key would read as unlimited"),
        ({"actrs": {}}, "an unknown top-level key"),
        ({"actors": {"x": {"max_concurrency": 0}}}, "a limit of zero could never run"),
        ({"actors": {"x": {"max_concurrency": "two"}}}, "a non-integer limit"),
        ({"global": {"max_concurrency": True}}, "bool is an int subclass"),
    ):
        path = _SANDBOX / "bad.json"
        path.write_text(json.dumps(bad))
        try:
            load_actor_config(path)
        except LimitError:
            continue
        raise AssertionError(f"accepted {bad} - {why}")

    # --concurrency beats the file; the file beats the built-in default.
    assert WorkerConfig.load(path=_PER_ACTOR_CONFIG, concurrency=2).concurrency == 2
    assert WorkerConfig.load(path=missing).concurrency == 1


@test
def test_the_real_config_caps_facebook_per_connected_account():
    """The shipped policy, not a test fixture: this is the file the Worker reads.

    It used to be max_concurrency: 1 -- one run of the Actor at a time. The real
    constraint is one run per SESSION: the site rotates its session cookie
    server-side during a run, so two scrapes on the same session race the
    write-back, while two scrapes on two different people's sessions do not
    interact. Capping the Actor throttled every user to serialise something that
    only ever applied within one account.
    """
    config = WorkerConfig.load(path=REPO_ROOT / "config" / "actors.json")
    profile = config.limits_for("facebook")
    assert profile.concurrency_scope == "social_account", (
        "facebook must be capped per connected account"
    )
    assert profile.per_key_limit == 1, profile.per_key_limit
    assert profile.max_concurrency is None, (
        "the Actor as a whole must NOT be capped -- different accounts run at once"
    )


# --- runner -----------------------------------------------------------------


def main() -> int:
    print(f"sandbox: {_SANDBOX}\n")
    passed = failed = 0
    for fn in TESTS:
        name = fn.__name__
        started = time.monotonic()
        try:
            fn()
        except Exception:
            failed += 1
            print(f"FAIL  {name}  ({time.monotonic() - started:.1f}s)")
            print("".join("      " + l for l in traceback.format_exc().splitlines(True)))
        else:
            passed += 1
            print(f"ok    {name}  ({time.monotonic() - started:.1f}s)")

    b = _BURST
    if b:
        print(
            f"\nmeasured: {BURST_JOBS} runs at concurrency {BURST_CONCURRENCY} in "
            f"{b['elapsed']:.1f}s (sequential would be ~{BURST_JOBS * b['unit']:.1f}s); "
            f"peak {b['peak']} container(s) over {b['samples']} samples"
        )
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
