#!/usr/bin/env python3
"""The queue consumer: the Worker's second entry point.

    Queue job -> registry PENDING->RUNNING -> Actor Executor -> Docker
              -> registry SUCCEEDED / FAILED

It contains NO Docker logic of its own. Everything below `execute_run` is the
same executor the direct CLI path uses, so the queued and direct paths cannot
diverge. This module only decides which run to execute next.

    python3 worker/consumer.py                     # run until interrupted
    python3 worker/consumer.py --max-jobs 3        # process 3 jobs, then exit
    python3 worker/consumer.py --once             # process one job if any is waiting
    python3 worker/consumer.py --concurrency 4    # up to 4 containers at once

CONCURRENCY: a slot is taken BEFORE a job is leased.

    acquire execution slot   (semaphore, --concurrency permits)
      -> dequeue = LEASE     (nothing waiting? release the slot and loop)
      -> mark_running        recorded BEFORE the container exists
      -> docker run
      -> complete            terminal status
      -> ack                 lease released
      -> release slot        (finally, always)

That order is the whole design. Leasing first and then waiting for a slot would
create leases with no container behind them, burning their visibility timeout
on work that has not started. Taking the slot first keeps the invariant the
reconciler's soundness already rests on: A HELD LEASE MEANS A LIVE CONTAINER.

Each job runs on its own daemon thread; the semaphore, not the thread count, is
what bounds concurrency. Daemon threads on purpose -- a stop request waits a
bounded time for runs to finish and then exits regardless, which is exactly the
crash case that recovery already handles. A non-daemon pool would join at
interpreter exit and make --shutdown-timeout a lie.

Exit codes describe the CONSUMER, not the runs it processed. A consumer that
executes a failing Actor has done its job correctly and exits 0; a failing
Actor is reported through the run's own status, exactly as everywhere else.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobqueue.base import (  # noqa: E402
    DEFAULT_QUEUE_NAME,
    DEFAULT_VISIBILITY_TIMEOUT_S,
    MAX_ATTEMPTS,
    Job,
    JobQueue,
    QueueError,
    open_queue,
)
from registry.run_registry import RegistryError, RunNotFound, RunRegistry  # noqa: E402
from shared.logging import event, write_line  # noqa: E402
from shared.models import ENV_FORWARD_LIST, Run, RunStatus  # noqa: E402
from worker.reconciler import DEFAULT_GRACE_S, reconcile  # noqa: E402
from worker.limits import (  # noqa: E402
    ActorProfile,
    DEFAULT_SHUTDOWN_TIMEOUT_S,
    ExecutionLimits,
    LimitError,
    WorkerConfig,
)
from worker.run_secrets import (  # noqa: E402
    SecretResolver,
    SessionBusy,
    SessionUnavailable,
    resolver_from_env,
)
from worker.executor import (  # noqa: E402
    RunAlreadyClaimed,
    WorkerError,
    execute_run,
    kill_container,
)

POLL_TIMEOUT_S = 1.0

# How often to sweep for leases abandoned by a Worker that died. Also runs once
# at startup, which is when it matters most.
RECOVER_INTERVAL_S = 30.0

# How often to look for runs that are unfinished but absent from the queue.
RECONCILE_INTERVAL_S = 300.0

# RETENTION (security review M-04). OFF unless ACTOR_PLATFORM_RUN_RETENTION_DAYS
# is set: deleting people's results is not something to switch on silently.
# When set, finished runs older than that many days are deleted once a day --
# record, items, logs and artifacts. Ownerless runs (operator-key runs and runs
# imported from before accounts existed) are kept unless
# ACTOR_PLATFORM_RUN_RETENTION_INCLUDE_OWNERLESS=1.
RETENTION_INTERVAL_S = 24 * 3600.0


def retention_days() -> int | None:
    raw = (os.environ.get("ACTOR_PLATFORM_RUN_RETENTION_DAYS") or "").strip()
    try:
        days = int(raw)
    except ValueError:
        return None
    return days if days > 0 else None


def retention_sweep(registry, now=None) -> int:
    """Delete finished runs older than the retention period. -> how many."""
    days = retention_days()
    if days is None:
        return 0
    include_ownerless = (os.environ.get("ACTOR_PLATFORM_RUN_RETENTION_INCLUDE_OWNERLESS")
                         or "").strip().lower() in {"1", "true", "yes", "on"}
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    removed = 0
    for run in registry.list(skip_corrupt=True):
        if not run.is_terminal or not (run.owner_id or include_ownerless):
            continue
        stamp = run.finished_at or run.created_at
        try:
            finished = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            continue
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        if finished < cutoff:
            try:
                registry.delete(run.run_id)
                removed += 1
            except Exception:  # noqa: BLE001 - one bad run must not stop the sweep
                continue
    return removed


def log(message: str) -> None:
    write_line(f"[consumer] {message}")


def process_job(
    job: Job,
    *,
    queue: JobQueue,
    registry: RunRegistry,
    limits: ExecutionLimits | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    force_build: bool = False,
    resolver: SecretResolver | None = None,
    profile: ActorProfile | None = None,
) -> Run | None:
    """Execute one leased job, then ack it. Never raises.

    The queue delivers at-least-once, so this has to be IDEMPOTENT: the same
    job can arrive twice if a Worker finished a run and died before acking.
    The registry -- not the job -- decides what still needs doing.
    """
    limits = limits if limits is not None else ExecutionLimits()
    log(f"picked up run {job.run_id} (actor={job.actor}, attempt {job.attempts + 1})")
    event("consumer", "job_picked_up", run_id=job.run_id, actor=job.actor,
          attempt=job.attempts + 1)

    try:
        run = registry.get(job.run_id)
    except RunNotFound:
        # Nothing to execute and nothing to retry; retrying would only replay
        # the same dead end for ever.
        log(f"run {job.run_id} does not exist - dropping the job")
        queue.ack(job)
        return None
    except RegistryError as exc:
        # Might be transient, so do NOT ack: let the lease expire and retry.
        log(f"run {job.run_id} could not be read ({exc}) - leaving it queued")
        return None

    # 1. Duplicate delivery. The work is already done; running the Actor again
    #    would double its side effects for no gain.
    if run.is_terminal:
        log(f"run {job.run_id} is already {run.status.value} - acking duplicate delivery")
        queue.ack(job)
        return run

    # 1b. A job that disagrees with its run about WHICH Actor it is. The slot
    #     table and the Actor's policy (session, state mount, network) were
    #     chosen from job.actor, but the image is run.actor -- so a mismatch is
    #     a forged or corrupted message, and running it would hand one Actor's
    #     policy to another's container. Dropped, not run: the record is the
    #     trusted copy, and the reconciler requeues a PENDING run with no job.
    if run.actor != job.actor:
        log(f"run {job.run_id}: queued job names actor {job.actor!r} but the run is "
            f"{run.actor!r} - dropping the job as tampered")
        event("consumer", "job_rejected", run_id=job.run_id, actor=run.actor,
              reason="actor_mismatch")
        queue.ack(job)
        return None

    # 2. A job that keeps killing its Worker must not be retried for ever.
    if job.attempts >= max_attempts:
        log(f"run {job.run_id} abandoned after {job.attempts} attempt(s)")
        event("consumer", "job_cancelled", run_id=job.run_id, actor=job.actor,
              reason="max_attempts", attempts=job.attempts)
        try:
            # CANCELLED, not PLATFORM_ERROR: the Actor never got to fail on its
            # own merits -- the platform stopped retrying an attempt that kept
            # being interrupted. Labelling it distinctly is what makes
            # "interrupted and given up on" visible in the metrics rather than
            # indistinguishable from a platform fault.
            registry.complete(
                job.run_id,
                status=RunStatus.CANCELLED,
                error=f"abandoned after {job.attempts} interrupted attempt(s)",
            )
        except RegistryError as exc:
            log(f"could not close out run {job.run_id}: {exc}")
        queue.ack(job)
        return None

    # 3. An orphan: a previous attempt died mid-run, so the record still claims
    #    RUNNING while nothing is running. Reset it before executing.
    if run.status is RunStatus.RUNNING:
        log(f"run {job.run_id} was left RUNNING by an interrupted attempt - reclaiming it")
        event("consumer", "job_reclaimed", run_id=job.run_id, actor=job.actor,
              reason="interrupted", attempt=job.attempts)
        if kill_container(job.run_id):
            log(f"killed the container the previous attempt left running")
        try:
            registry.requeue(job.run_id, f"reclaimed after {job.attempts} interrupted attempt(s)")
        except RegistryError as exc:
            log(f"could not reclaim run {job.run_id}: {exc}")
            return None

    try:
        run = execute_run(
            job.run_id, registry=registry, limits=limits, force_build=force_build,
            resolver=resolver, profile=profile,
        )
    except SessionBusy as exc:
        # ANOTHER RUN HOLDS THIS SESSION. Not a failure of anything -- the
        # in-process counter declined jobs like this before they were leased,
        # so reaching here means a cross-PROCESS race, which is exactly what
        # the database lease exists to settle.
        #
        # NACK, never ack. ack means "finished" and would silently discard the
        # run; recover() would eventually redeliver it but would burn an attempt
        # against MAX_ATTEMPTS for work that never started, and three of those
        # give up on it altogether.
        log(f"run {job.run_id} is waiting for its session - returning it to the queue")
        event("consumer", "job_session_busy", run_id=job.run_id, actor=job.actor)
        _return_unrun(queue, job)
        return None
    except SessionUnavailable as exc:
        # OUR failure, not the user's and not the Actor's: the credential could
        # not be produced, so nothing ran. The executor has already recorded a
        # PLATFORM_ERROR, and the distinction matters -- reporting this as an
        # auth failure would tell someone to redo an interactive login for a
        # session that is probably fine.
        log(f"run {job.run_id} could not be given its credential: {exc}")
        event("consumer", "job_no_credential", run_id=job.run_id, actor=job.actor,
              reason=str(exc))
        queue.ack(job)
        return None
    except RunAlreadyClaimed as exc:
        # Someone else won the PENDING -> RUNNING claim, so this delivery has
        # nothing to do. Ack it: the winner owns its own lease and will finish
        # the run. Nothing was changed, so there is nothing to undo.
        log(f"run {job.run_id} is already being executed - dropping this delivery ({exc})")
        queue.ack(job)
        return None
    except (WorkerError, RegistryError) as exc:
        # The executor has already recorded a terminal PLATFORM_ERROR against
        # the run, so this delivery IS finished -- ack it. Retrying could not
        # help: a terminal run cannot be re-run.
        log(f"run {job.run_id} could not be executed: {exc}")
        queue.ack(job)
        return None

    if not queue.ack(job):
        # The lease was reclaimed while the run was still going, so the job is
        # queued again. Harmless -- the duplicate will see a terminal run and
        # ack -- but it means the visibility timeout is too short for this run.
        log(f"run {job.run_id} finished but its lease had already been reclaimed")

    log(f"run {run.run_id} finished {run.status.value} in {run.duration_seconds}s")
    event("consumer", "job_finished", run_id=run.run_id, actor=run.actor,
          status=run.status.value, exit_code=run.exit_code,
          duration_seconds=run.duration_seconds, attempts=run.attempts)
    return run


class _Slots:
    """The two bounds a run must satisfy before its container starts.

        global slot   AND   this Actor's slot

    Both, always. The global bound protects the machine; the per-Actor bound
    protects whatever the Actor itself cannot share -- typically one
    authenticated session that two concurrent runs would race.

    An Actor with no configured limit gets no second semaphore at all, so it is
    bounded only by the global one. That is what makes per-Actor limits purely
    additive: an unconfigured Actor behaves exactly as it did before they
    existed.

    The active set is not bookkeeping for its own sake -- it is how the Worker
    can SAY what it is running, which is the difference between a saturated
    Worker and a wedged one in the log.
    """

    def __init__(
        self,
        size: int,
        limit_for: Callable[[str], int | None],
        key_limit_for: Callable[[str], int | None] | None = None,
    ) -> None:
        self.size = size
        self._limit_for = limit_for
        # How many runs may share one concurrency KEY, per Actor. None (the
        # default) means the key is ignored entirely, which is what every
        # pre-existing caller gets.
        self._key_limit_for = key_limit_for or (lambda _actor: None)
        self._free = threading.Semaphore(size)
        self._lock = threading.Lock()
        self._active: set[str] = set()
        self._actor_running: dict[str, int] = {}
        # Keyed by the job's opaque concurrency_key. A SECOND counter beside
        # _actor_running rather than a replacement: an Actor can be bounded
        # both as a whole and per key, and the two answer different questions.
        self._key_running: dict[str, int] = {}

    # --- global ------------------------------------------------------------

    def acquire(self, timeout: float) -> bool:
        """Take a GLOBAL slot, or return False if none came free in time."""
        return self._free.acquire(timeout=max(timeout, 0.0))

    def release_global(self) -> None:
        self._free.release()

    # --- per actor ---------------------------------------------------------
    # A COUNTER under the existing lock, not a second semaphore. The dispatcher
    # needs a non-blocking "is there room?" that reserves nothing (the queue's
    # eligibility predicate) as well as a reservation, and a counter gives both
    # without keeping two structures in step.

    def actor_has_room(self, actor: str) -> bool:
        """Could this Actor start another run right now? PURE -- reserves nothing.

        This is the queue's eligibility predicate, which is what stops a job
        whose Actor is at its limit from ever being LEASED: it stays plainly
        PENDING where another Worker and the reconciler can both see it.
        """
        limit = self._limit_for(actor)
        if limit is None:
            return True
        with self._lock:
            return self._actor_running.get(actor, 0) < limit

    def may_run(self, job: Job) -> bool:
        """The queue's eligibility predicate: may this job be leased right now?

        PURE -- reserves nothing. The single seam every concurrency rule goes
        through, so the dispatcher's call site does not change as rules are
        added.
        """
        return self.actor_has_room(job.actor) and self.key_has_room(job)

    # --- per concurrency key ------------------------------------------------
    # The same counter-under-one-lock shape as the per-Actor bound above. What
    # differs is the QUESTION: an Actor bound asks "how much of this work at
    # once", a key bound asks "how much of it against ONE shared resource".
    # For a scraper the resource is a login: two runs on one session race its
    # server-side rotation, while thirty runs on thirty sessions do not
    # interact at all. Bounding the Actor to express that throttled everyone.

    def key_has_room(self, job: Job) -> bool:
        """Could this job's key take another run? PURE -- reserves nothing."""
        if not job.concurrency_key:
            return True
        limit = self._key_limit_for(job.actor)
        if limit is None:
            return True
        with self._lock:
            return self._key_running.get(job.concurrency_key, 0) < limit

    def take_key(self, job: Job) -> bool:
        """Reserve one of this key's slots. Never blocks."""
        if not job.concurrency_key:
            return True
        limit = self._key_limit_for(job.actor)
        if limit is None:
            return True
        with self._lock:
            running = self._key_running.get(job.concurrency_key, 0)
            if running >= limit:
                return False
            self._key_running[job.concurrency_key] = running + 1
            return True

    def release_key(self, key: str | None) -> None:
        if not key:
            return
        with self._lock:
            remaining = self._key_running.get(key, 0) - 1
            if remaining > 0:
                self._key_running[key] = remaining
            else:
                # Dropped rather than left at zero: keys are per social account
                # and unbounded in number, so keeping every one ever seen would
                # grow this dict for the life of the process.
                self._key_running.pop(key, None)

    def take_actor(self, actor: str) -> bool:
        """Reserve one of this Actor's slots. Never blocks.

        Cannot fail in practice: only the dispatcher thread reserves, worker
        threads only ever release, so between actor_has_room() saying yes and
        this call the count can only have fallen. Checked anyway, because a
        silent over-subscription is the one bug this class exists to prevent.
        """
        limit = self._limit_for(actor)
        if limit is None:
            return True
        with self._lock:
            running = self._actor_running.get(actor, 0)
            if running >= limit:
                return False
            self._actor_running[actor] = running + 1
            return True

    def release_actor(self, actor: str) -> None:
        with self._lock:
            self._actor_running[actor] = max(0, self._actor_running.get(actor, 0) - 1)

    def actor_running(self, actor: str) -> int:
        with self._lock:
            return self._actor_running.get(actor, 0)

    # --- bookkeeping -------------------------------------------------------

    def claim(self, run_id: str) -> None:
        with self._lock:
            self._active.add(run_id)

    def release(
        self,
        run_id: str | None = None,
        actor: str | None = None,
        key: str | None = None,
    ) -> None:
        """Hand back everything this run held. Always called from a finally."""
        if actor is not None:
            self.release_actor(actor)
        self.release_key(key)
        if run_id is not None:
            with self._lock:
                self._active.discard(run_id)
        self._free.release()

    @property
    def active(self) -> set[str]:
        with self._lock:
            return set(self._active)

    def busy(self) -> int:
        with self._lock:
            return len(self._active)

    def drain(self, timeout: float | None) -> set[str]:
        """Wait for active runs to finish. -> whatever is still going after `timeout`.

        `None` waits indefinitely, which is what a Worker that has simply
        finished dispatching must do: those runs are its own work.
        """
        deadline = None if timeout is None else time.monotonic() + max(timeout, 0.0)
        while self.busy() and (deadline is None or time.monotonic() < deadline):
            time.sleep(0.05)
        return self.active


def _return_unrun(queue: JobQueue, job: Job) -> None:
    """Hand a leased job back to the queue without counting an attempt."""
    try:
        queue.nack(job)
    except QueueError as exc:
        # Not fatal: the lease still exists, so recover() will reclaim it. That
        # costs an attempt, which is why nack is tried first.
        log(f"could not return run {job.run_id} to the queue: {exc}")


def _refuse_session_env_collision(config: WorkerConfig) -> None:
    """A forwarded name must not also be an Actor's per-run session variable.

    Reads the DECLARED names out of the environment variable rather than asking
    forwarded_env_names(), which returns only those currently set. A declared
    name that happens to be unset right now is still a misconfiguration: export
    it tomorrow and the platform silently starts handing one person's session to
    everybody's runs. Refusing on the declaration catches it at boot.
    """
    declared = {
        name.strip()
        for name in (os.environ.get(ENV_FORWARD_LIST) or "").replace(",", " ").split()
        if name.strip()
    }
    if not declared:
        return
    forwarded = declared
    for actor, profile in config.actors.items():
        name = getattr(profile, "session_env", None)
        if name and name in forwarded:
            raise LimitError(
                f"{ENV_FORWARD_LIST} names {name!r}, which is also the per-run "
                f"session variable for actor '{actor}'. A platform-wide value "
                f"would be handed to runs belonging to other people. Remove it "
                f"from {ENV_FORWARD_LIST}."
            )


def consume(
    queue: JobQueue,
    *,
    registry: RunRegistry,
    config: WorkerConfig | None = None,
    resolver: SecretResolver | None = None,
    max_jobs: int | None = None,
    poll_timeout: float = POLL_TIMEOUT_S,
    drain: bool = False,
    max_attempts: int = MAX_ATTEMPTS,
    visibility_timeout: float | None = DEFAULT_VISIBILITY_TIMEOUT_S,
    reconcile_interval: float = RECONCILE_INTERVAL_S,
    reconcile_grace: float = DEFAULT_GRACE_S,
    shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_S,
    stop: threading.Event | None = None,
) -> int:
    """Dispatch jobs until stopped, or until max_jobs have been DISPATCHED.

    A long-running Worker WAITS on an empty queue -- that is its job. `drain`
    is for scripts and tests that mean "process whatever is queued, then stop":
    without it, asking for N jobs on an empty queue blocks for ever, which is
    the opposite of what --once promises. Under concurrency `drain` also means
    "and then wait for what is already running".

    max_jobs counts jobs handed to a slot, not jobs finished. Counting
    completions would mean the loop could not stop dequeuing until the last one
    landed, so a --max-jobs 4 Worker at concurrency 4 would start a fifth.
    """
    stop = stop if stop is not None else threading.Event()
    config = config if config is not None else WorkerConfig()

    # Per-run credentials, when this deployment has a database and a key.
    # NullSecretResolver otherwise, which is byte-identical to the behaviour
    # that existed before per-run credentials -- so a single-user deployment
    # needs no configuration and cannot accidentally acquire them.
    resolver = resolver if resolver is not None else resolver_from_env(config)

    # REFUSE TO START if a platform-wide forwarded name collides with an
    # Actor's per-run session variable. Otherwise the forwarded value would be
    # inherited by the child environment and either shadow the per-run one or,
    # worse, be used when no per-run credential was resolved -- putting one
    # user's session into another user's run. Loud at boot, in the same spirit
    # as LimitError refusing a bad memory string before anything runs.
    _refuse_session_env_collision(config)
    # limits_for() is THE single resolution point for Actor policy, so the
    # scheduler asks it rather than reading config itself. No Actor name
    # reaches this module.
    slots = _Slots(
        config.concurrency,
        lambda actor: config.limits_for(actor).max_concurrency,
        lambda actor: config.limits_for(actor).per_key_limit,
    )
    dispatched = 0

    # Sweep before taking anything: if THIS Worker is replacing one that died,
    # its abandoned jobs are what should be picked up first.
    try:
        reclaimed = queue.recover(visibility_timeout=visibility_timeout)
        if reclaimed:
            log(f"reclaimed {reclaimed} job(s) abandoned by a previous worker")
    except QueueError as exc:
        log(f"startup recovery sweep failed: {exc}")
    last_recover = time.monotonic()

    # Leases cover work still IN the queue. Reconciling covers the rest: a run
    # whose job vanished has nothing to redeliver, and only a registry scan can
    # find it.
    #
    # It runs on the DISPATCH thread, so a sweep never overlaps this Worker's
    # own dequeue. That is the point: the sweep's whole safety argument is that
    # a live run holds a lease, and the one moment that is briefly untrue is
    # between acquiring a slot and claiming a job. Sweeping here cannot land in
    # that window. The cost is that dispatch pauses for the length of a sweep.
    def sweep_stranded() -> None:
        if reconcile_interval <= 0:
            return
        try:
            report = reconcile(
                registry, queue, grace=reconcile_grace, max_attempts=max_attempts
            )
            if report.stranded:
                log(report.summary())
        except (QueueError, RegistryError) as exc:
            log(f"reconcile sweep failed: {exc}")

    sweep_stranded()
    last_reconcile = time.monotonic()
    # 0 means "due now": the first retention sweep runs as the Worker starts.
    last_retention = 0.0

    def dispatch(job: Job) -> None:
        """One job, on its own thread. BOTH slots are released whatever happens."""
        try:
            profile = config.limits_for(job.actor)
            process_job(
                job,
                queue=queue,
                registry=registry,
                limits=profile.execution,
                max_attempts=max_attempts,
                resolver=resolver,
                profile=profile,
            )
        finally:
            slots.release(job.run_id, job.actor, job.concurrency_key)

    while not stop.is_set() and (max_jobs is None or dispatched < max_jobs):
        if time.monotonic() - last_recover >= RECOVER_INTERVAL_S:
            try:
                reclaimed = queue.recover(visibility_timeout=visibility_timeout)
                if reclaimed:
                    log(f"reclaimed {reclaimed} abandoned job(s)")
            except QueueError as exc:
                log(f"recovery sweep failed: {exc}")
            last_recover = time.monotonic()

        if reconcile_interval > 0 and time.monotonic() - last_reconcile >= reconcile_interval:
            sweep_stranded()
            last_reconcile = time.monotonic()

        if time.monotonic() - last_retention >= RETENTION_INTERVAL_S:
            last_retention = time.monotonic()
            try:
                removed = retention_sweep(registry)
                if removed:
                    log(f"retention: deleted {removed} run(s) older than the retention period")
            except Exception as exc:  # noqa: BLE001 - housekeeping never stops the loop
                log(f"retention sweep failed: {exc}")

        # THE SLOT COMES FIRST, and the whole design rests on it: a job is not
        # leased until there is a container's worth of room for it. Timing out
        # rather than blocking keeps the recovery and reconcile timers ticking
        # while every slot is busy.
        if not slots.acquire(timeout=poll_timeout):
            continue

        # The predicate is what enforces the per-Actor bound: a job whose
        # Actor is already at its limit is never leased, so it stays PENDING in
        # the queue instead of being held by a Worker that cannot run it.
        try:
            job = queue.dequeue(timeout=poll_timeout, eligible=slots.may_run)
        except QueueError as exc:
            log(f"queue error: {exc}")
            slots.release_global()
            if stop.wait(poll_timeout):
                break
            continue

        if job is None:
            slots.release_global()
            # None now means "nothing RUNNABLE", which is not the same as
            # "nothing here": a queue full of jobs for a saturated Actor
            # returns None every time. Draining on that would exit with work
            # outstanding, so the queue itself has to be empty.
            if drain and queue.depth() == 0:
                break
            continue  # loop so the stop flag and the timers are honoured

        # A duplicate delivery of a run THIS Worker is already executing. The
        # registry's transition guard would catch it a layer down, but only
        # after a second container had been prepared; catching it here means
        # the second delivery costs nothing. Ack it -- the delivery that is
        # actually running owns its own lease.
        if job.run_id in slots.active:
            log(f"run {job.run_id} is already running here - acking duplicate delivery")
            try:
                queue.ack(job)
            except QueueError as exc:
                log(f"could not ack duplicate delivery of run {job.run_id}: {exc}")
            slots.release_global()
            continue

        # Unreachable in practice -- the eligibility predicate already declined
        # such a job -- but never guessed at: over-subscribing is the one
        # failure this must not have.
        #
        # NACK, do not just drop it. The job is LEASED by now, so returning
        # early without handing it back leaves it invisible until recover()
        # reclaims it -- which burns an attempt against MAX_ATTEMPTS for work
        # that never ran, and after three of those the run is given up on.
        if not slots.take_actor(job.actor):
            log(f"run {job.run_id} lost the race for a {job.actor} slot - returning it")
            _return_unrun(queue, job)
            slots.release_global()
            continue

        if not slots.take_key(job):
            log(f"run {job.run_id} lost the race for its session - returning it")
            _return_unrun(queue, job)
            slots.release_actor(job.actor)
            slots.release_global()
            continue

        slots.claim(job.run_id)
        dispatched += 1
        try:
            # Daemon: a stop request must not be able to hang on a 300s run.
            threading.Thread(
                target=dispatch, args=(job,), name=f"run-{job.run_id[-6:]}", daemon=True
            ).start()
        except RuntimeError as exc:
            # Out of threads. Do NOT ack -- the lease is still held, so recovery
            # redelivers this job rather than it being silently dropped.
            log(f"could not start a thread for run {job.run_id}: {exc}")
            slots.release(job.run_id, job.actor, job.concurrency_key)
            dispatched -= 1

        if slots.busy() >= slots.size:
            log(f"all {slots.size} slot(s) busy - later jobs are waiting in the queue")

    # HOW LONG TO WAIT DEPENDS ON WHY WE STOPPED, and conflating the two
    # abandons long runs.
    #
    # Finished dispatching (--max-jobs reached, or --drain on an empty queue):
    # wait INDEFINITELY. Those runs are this Worker's own work and there is no
    # deadline on an Actor -- bounding it here at 60s meant a Worker exited
    # from under any run longer than a minute, whose container was then killed
    # and re-run on redelivery. That is the platform ending an Actor for
    # elapsed time, reintroduced through the back door. MEASURED: a 90s run
    # under --max-jobs 1 was abandoned at 60s and left RUNNING.
    #
    # Asked to stop (SIGINT/SIGTERM): bound it, because an operator needs the
    # process to actually go away. What is left behind is exactly the crash
    # case, which recovery already handles.
    interrupted = stop.is_set()
    grace = shutdown_timeout if interrupted else None
    if slots.busy():
        waiting = "indefinitely" if grace is None else f"up to {grace:.0f}s"
        log(f"waiting {waiting} for {slots.busy()} run(s) still in flight")

    still_running = slots.drain(grace)
    if still_running:
        # Their leases are still held and their records still say RUNNING, which
        # is exactly what a crashed Worker leaves behind -- so recovery handles
        # them. Say so rather than exiting quietly on a half-finished run.
        log(
            f"{len(still_running)} run(s) still going after {shutdown_timeout:.0f}s: "
            f"{', '.join(sorted(still_running))} - leaving them for recovery"
        )

    return dispatched


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="consumer", description="Execute Actor runs taken from the queue."
    )
    parser.add_argument("--queue-url", help="queue location (default: $ACTOR_QUEUE_URL)")
    parser.add_argument(
        "--queue-name", default=DEFAULT_QUEUE_NAME, help="which queue to consume from"
    )
    parser.add_argument("--registry-root", help="run storage root (default: storage/runs)")
    parser.add_argument(
        "--max-jobs", type=int, help="stop dequeuing after dispatching this many jobs"
    )
    parser.add_argument(
        "--once", action="store_true", help="process a single job, then exit (implies --drain)"
    )
    parser.add_argument(
        "--drain",
        action="store_true",
        help="exit as soon as the queue is empty instead of waiting for more work",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        help=(
            "how many Actor containers may run at once, across all Actors "
            "(default: global.max_concurrency from the actor config, else 1). "
            "Per-Actor limits are configured in that file, not here."
        ),
    )
    parser.add_argument(
        "--actor-config",
        help="path to the per-Actor policy file (default: $ACTOR_CONFIG or config/actors.json)",
    )
    parser.add_argument(
        "--cpus", type=float, help="cpu limit per container, e.g. 0.5 (default: unlimited)"
    )
    parser.add_argument(
        "--memory",
        help="memory limit per container, e.g. 512m (default: unlimited; minimum 6m)",
    )
    parser.add_argument(
        "--shutdown-timeout",
        type=float,
        default=DEFAULT_SHUTDOWN_TIMEOUT_S,
        help=(
            "seconds to wait for in-flight runs when stopping "
            f"(default {DEFAULT_SHUTDOWN_TIMEOUT_S:.0f}); anything still going is "
            "left for recovery"
        ),
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=MAX_ATTEMPTS,
        help=f"give up on a run after this many interrupted attempts (default {MAX_ATTEMPTS})",
    )
    parser.add_argument(
        "--reconcile-interval",
        type=float,
        default=RECONCILE_INTERVAL_S,
        help="seconds between stranded-run sweeps; 0 disables them",
    )
    parser.add_argument(
        "--reconcile-grace",
        type=float,
        default=DEFAULT_GRACE_S,
        help=(
            "seconds a run must be unfinished before the sweep may call it stranded "
            f"(default {DEFAULT_GRACE_S:.0f})"
        ),
    )
    parser.add_argument(
        "--visibility-timeout",
        type=float,
        default=DEFAULT_VISIBILITY_TIMEOUT_S,
        help="seconds before a lease is assumed abandoned when liveness is unknown",
    )
    parser.add_argument(
        "--poll-timeout",
        type=float,
        default=POLL_TIMEOUT_S,
        help="how long to wait for a job before checking whether to stop",
    )
    args = parser.parse_args(argv)
    if args.once:
        args.max_jobs = 1
        # "then exit" must mean it, even when nothing is queued.
        args.drain = True
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        # Refuse an unusable limit BEFORE opening anything: a typo should cost
        # a second at startup, not a container failing five minutes in.
        config = WorkerConfig.load(
            path=args.actor_config,
            concurrency=args.concurrency,
            limits=ExecutionLimits(cpus=args.cpus, memory=args.memory),
        )
    except LimitError as exc:
        print(f"[consumer] ERROR: {exc}", file=sys.stderr, flush=True)
        return 2

    try:
        queue = open_queue(args.queue_url, name=args.queue_name)
    except QueueError as exc:
        print(f"[consumer] ERROR: {exc}", file=sys.stderr, flush=True)
        return 2

    # open_registry, NOT RunRegistry: the API picks its backend the same way, and
    # the two MUST agree. While this said RunRegistry() the Worker wrote statuses
    # to run.json files that a Postgres-backed API never read -- so a run that had
    # succeeded, produced its report and e-mailed it still showed PENDING in the
    # console, forever, with nothing in any log to say why.
    # Imported HERE, not at module scope: pg_registry pulls in psycopg, and a
    # single-user Worker with no database configured should not need it loaded.
    # api/main.py::app_from_env does the same for the same reason.
    from registry.pg_registry import open_registry  # noqa: PLC0415

    registry = open_registry(args.registry_root)

    stop = threading.Event()

    def request_stop(signum, _frame) -> None:
        # Set a flag rather than dying: a run already inside Docker gets to
        # finish and record its result instead of being orphaned as RUNNING.
        log(
            f"signal {signal.Signals(signum).name} received - no new jobs; "
            f"waiting up to {args.shutdown_timeout:.0f}s for runs in flight"
        )
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    log(
        f"waiting for jobs on {type(queue).__name__} (depth {queue.depth()}) | "
        f"concurrency={config.concurrency} | per-actor: {config.describe_actors()} | "
        f"{config.limits.describe()}"
    )
    try:
        processed = consume(
            queue,
            registry=registry,
            config=config,
            max_jobs=args.max_jobs,
            poll_timeout=args.poll_timeout,
            drain=args.drain,
            max_attempts=args.max_attempts,
            visibility_timeout=args.visibility_timeout,
            reconcile_interval=args.reconcile_interval,
            reconcile_grace=args.reconcile_grace,
            shutdown_timeout=args.shutdown_timeout,
            stop=stop,
        )
    finally:
        queue.close()

    log(f"stopped after dispatching {processed} job(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
