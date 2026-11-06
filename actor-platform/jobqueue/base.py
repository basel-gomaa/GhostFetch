"""The job queue abstraction.

The API and the Worker are separate processes that must not know about each
other. The queue is the only thing between them:

    FastAPI  --enqueue-->  Queue  --dequeue-->  Worker

A Job is a POINTER, not a payload. The run already exists in the registry as
PENDING before anything is enqueued, so the registry -- never the job -- is the
source of truth for a run's input and status. The actor and input travel with
the job anyway because a queue message that cannot be read on its own is
miserable to debug.

ORDERING CONTRACT: FIFO. Jobs are dequeued in the order they were enqueued.
With the single Worker this milestone builds, that means runs are processed in
submission order. Nothing here promises ordering across concurrent consumers,
and callers must not assume it.

DELIVERY CONTRACT: at-least-once. dequeue() LEASES a job rather than deleting
it; the Worker must ack() once the run reaches a terminal state. A lease whose
owner has died is returned to the queue by recover(), so a Worker killed
mid-run does not strand the job.

The cost of at-least-once is that DUPLICATE DELIVERY IS NOW POSSIBLE -- a Worker
that finishes a run and dies before acking will see that job again. Consumers
must therefore be idempotent, and worker/consumer.py is: it checks the run's
status in the registry before executing anything, and acks without re-running a
run that is already terminal. Duplicate delivery is the price of never losing
work; silently running an Actor twice would not be.

The module name is `jobqueue`, not `queue`, on purpose: a top-level `queue`
package on sys.path would shadow the standard library's `queue` module for
every import in the process.
"""

from __future__ import annotations

import json
import os
import socket
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from shared.models import is_valid_actor_name, is_valid_run_id

QUEUE_URL_ENV = "ACTOR_QUEUE_URL"
DEFAULT_QUEUE_NAME = "actor-platform:runs"

# How long a lease may go unacked before it is assumed abandoned.
#
# NONE BY DEFAULT -- a lease does not expire on time at all, and that is the
# whole point. Actors here may legitimately run for hours (real scrapes are
# MEASURED at 17 to 50 minutes, backfills longer), and any finite value
# reclaims a HEALTHY run's lease: the redelivered job finds the run RUNNING,
# kills its container, requeues it and starts again -- the platform terminating
# an Actor for being slow, via the queue instead of the executor. The old 900s
# value did exactly that to most real scrapes.
#
# Owner liveness is therefore the only default signal, and it is the better
# one: a lease records "<host>:<pid>", so a crashed Worker's job is reclaimed
# in SECONDS on the same host rather than after a quarter of an hour.
#
# THE COST, which is real: a lease left by a dead process on ANOTHER host is
# now never reclaimed -- owner_is_dead() answers False when it cannot tell, and
# the reconciler will not touch it either, because a lease in inflight/ counts
# as being in the queue. The platform is single-host today; the fix when it is
# not is a lease heartbeat, not a timer. An operator can still set one.
DEFAULT_VISIBILITY_TIMEOUT_S: float | None = None

# How many times a job may be delivered before the run is given up on. Without
# a ceiling, a job that reliably kills its Worker would be retried for ever.
MAX_ATTEMPTS = 3

# What a queued job may look like, checked on the way OUT of the queue as well
# as in. The queue is a separate store (a directory, or Redis) that the API and
# the Worker trust each other through, so a message in it is data to validate,
# not a promise: a forged or corrupted one used to raise TypeError, ValueError
# or RecursionError out of from_json, which nothing caught -- the file-backed
# consumer crashed on it, and crashed again on restart, for ever.
MAX_JOB_BYTES = 4 * 1024 * 1024     # the API's body limit is 1 MiB; input is the bulk
MAX_JOB_ATTEMPTS_FIELD = 1000       # far past MAX_ATTEMPTS; anything larger is not a count
MAX_CONCURRENCY_KEY_LENGTH = 256    # "social:<uuid>" is 43


class QueueError(RuntimeError):
    """The queue could not be reached or used."""


def owner_id() -> str:
    """Identifies the process holding a lease: '<host>:<pid>'."""
    return f"{socket.gethostname()}:{os.getpid()}"


def owner_is_dead(owner: str) -> bool:
    """True only when the owner is PROVABLY gone.

    Answers False whenever it cannot tell -- a lease on another host, an
    unparseable owner, a process owned by another user. Reclaiming a job whose
    worker is actually alive would run the Actor twice, so the uncertain answer
    must be "leave it alone" and let the visibility timeout decide.
    """
    host, _, pid = owner.rpartition(":")
    if host != socket.gethostname():
        return False

    try:
        os.kill(int(pid), 0)
    except ValueError:
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        return False  # exists, just not ours
    return False


@dataclass
class Job:
    """One unit of work: run this already-registered run."""

    run_id: str
    actor: str
    input: dict[str, Any] = field(default_factory=dict)
    enqueued_at: str = ""
    attempts: int = 0
    # What this job must not run concurrently with. An OPAQUE string set by
    # whoever enqueued it: the queue never interprets it, it only hands it to
    # the eligibility predicate. Keeping it opaque is what stops the queue from
    # having to know that the thing being serialised is a social session.
    # Not a secret -- it is an identifier, and it is written to the queue file.
    concurrency_key: str | None = None
    # Set by dequeue() so ack() can find the exact lease this delivery created.
    # Never serialised: it identifies a delivery, not the work.
    receipt: str | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not self.enqueued_at:
            self.enqueued_at = (
                datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            )

    def to_json(self) -> str:
        data = asdict(self)
        data.pop("receipt", None)
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | bytes) -> Job:
        """Decode and VALIDATE one queued job. Raises QueueError and nothing else.

        Every field is checked against what the API would have written, so a
        job that names a path-shaped run id, an Actor no image could be built
        for, or a non-numeric attempt count is refused here -- before any of it
        reaches a slot table, a registry lookup or a `docker` argument. The
        message never quotes the payload: it is untrusted, and may be huge.
        """
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            if not isinstance(raw, str):
                raise QueueError(f"queued job must be text, got {type(raw).__name__}")
            if len(raw) > MAX_JOB_BYTES:
                raise QueueError(f"queued job is larger than {MAX_JOB_BYTES} bytes")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise QueueError(f"queued job is not valid JSON ({exc.msg})") from None

            if not isinstance(data, dict):
                raise QueueError(f"queued job must be an object, got {type(data).__name__}")

            missing = [key for key in ("run_id", "actor") if not data.get(key)]
            if missing:
                raise QueueError(f"queued job is missing {', '.join(missing)}")

            run_id, actor = data["run_id"], data["actor"]
            if not is_valid_run_id(run_id):
                raise QueueError("queued job has an invalid run_id")
            if not is_valid_actor_name(actor):
                raise QueueError("queued job has an invalid actor name")

            payload = data.get("input")
            if payload is None:
                payload = {}
            if not isinstance(payload, dict):
                raise QueueError("queued job input must be an object")

            enqueued_at = data.get("enqueued_at", "")
            if not isinstance(enqueued_at, str) or len(enqueued_at) > 64:
                raise QueueError("queued job has an invalid enqueued_at")

            attempts = data.get("attempts") or 0
            # bool is an int; True is not an attempt count.
            if (not isinstance(attempts, int) or isinstance(attempts, bool)
                    or not 0 <= attempts <= MAX_JOB_ATTEMPTS_FIELD):
                raise QueueError("queued job has an invalid attempts count")

            # Defaulted, so a job enqueued before this field existed still loads.
            key = data.get("concurrency_key")
            if key is not None and (not isinstance(key, str) or not key
                                    or len(key) > MAX_CONCURRENCY_KEY_LENGTH):
                raise QueueError("queued job has an invalid concurrency_key")
        except QueueError:
            raise
        except (RecursionError, ValueError, TypeError, AttributeError, UnicodeError) as exc:
            # Deep nesting, bad UTF-8, a value of a type nothing expected.
            raise QueueError(f"queued job could not be decoded ({type(exc).__name__})") from None

        return cls(
            run_id=run_id,
            actor=actor,
            input=payload,
            enqueued_at=enqueued_at,
            attempts=attempts,
            concurrency_key=key,
        )


class JobQueue(ABC):
    """What the API and the Worker are allowed to know about the queue.

    Seven methods. Priorities, delays and dead-letter routing are still out --
    adding them here would let Redis-shaped assumptions leak into the Worker --
    but ack/recover are not optional extras: without them "at-least-once" is
    just a claim.
    """

    @abstractmethod
    def enqueue(self, job: Job) -> None:
        """Add a job to the back of the queue."""

    @abstractmethod
    def dequeue(
        self,
        timeout: float = 0.0,
        *,
        eligible: Callable[[Job], bool] | None = None,
    ) -> Job | None:
        """Lease the oldest job, waiting up to `timeout` seconds.

        Returns None when the queue is still empty. timeout <= 0 means do not
        wait at all. The job is NOT removed -- it is leased, and stays leased
        until ack() or until recover() decides the lease was abandoned.

        `eligible(job) -> bool` filters WHICH job may be leased, and exists so
        a consumer that is at a concurrency limit does not have to take the job
        to find that out. It is handed the whole Job, not just the actor name,
        so a consumer can also decline on `concurrency_key` -- both backends
        already decode the full Job to read `.actor`, so this costs no I/O. A job it rejects is left plainly PENDING in
        the queue, where another Worker and the reconciler can both still see
        it -- which is what keeps the invariant that A HELD LEASE MEANS RUNNABLE
        WORK. The alternative, leasing and handing back, burns leases and turns
        a saturated Actor at the head of the queue into a spin.

        Returning None with a NON-EMPTY queue is therefore a normal outcome
        when a predicate is supplied: it means "nothing runnable right now",
        not "nothing here".

        The lease records only WHO took it and WHEN. How long a lease may go
        unacked is recover()'s policy, not a value frozen in at claim time.
        """

    @abstractmethod
    def ack(self, job: Job) -> bool:
        """Confirm a leased job is finished, dropping it for good.

        Returns False if the lease was already gone -- reclaimed while the run
        was still going, say -- which is worth logging but is not an error.
        """

    @abstractmethod
    def nack(self, job: Job) -> bool:
        """Hand a leased job back to the queue UNRUN. Returns False if the lease
        had already gone.

        Distinct from recover() in the one way that matters: it does NOT count
        an attempt, because nothing was attempted. It exists for the case where
        a Worker leases a job and only then discovers it may not run it -- the
        eligibility predicate is a fast in-process check, so a cross-process
        limit can still be found to be reached after the lease is taken.

        Acking such a job would silently DISCARD the run (ack means "finished",
        and nothing would ever pick it up again), and letting recover() reclaim
        it would burn an attempt against MAX_ATTEMPTS for work that never ran.
        Neither is acceptable, hence a third verb.
        """

    @abstractmethod
    def recover(
        self, *, visibility_timeout: float | None = DEFAULT_VISIBILITY_TIMEOUT_S
    ) -> int:
        """Return abandoned leases to the queue. Returns how many were reclaimed.

        A lease is abandoned when its owning process is provably dead, or when
        it has outlived `visibility_timeout`. `None` -- the default -- means
        leases never expire on time, so only a provably dead owner loses one;
        see the constant above for why a timer is the wrong tool here.

        Reclaimed jobs go to the FRONT of the queue -- they were enqueued first
        and have waited longest -- and their attempt count is incremented so a
        job that keeps killing its Worker can be given up on rather than
        retried for ever.
        """

    @abstractmethod
    def depth(self) -> int:
        """How many jobs are waiting. Leased jobs are NOT counted."""

    @abstractmethod
    def inflight(self) -> int:
        """How many jobs are leased and not yet acked."""

    @abstractmethod
    def known_run_ids(self) -> set[str]:
        """Every run this queue is holding work for, waiting or leased.

        The reconciler needs this to answer "is anything going to pick this run
        up?". A run that is not terminal and appears nowhere here is stranded:
        no Worker will ever touch it again.
        """

    def close(self) -> None:
        """Release any connection. Safe to call more than once."""

    def __len__(self) -> int:
        return self.depth()


def open_queue(url: str | None = None, *, name: str = DEFAULT_QUEUE_NAME) -> JobQueue:
    """Build the queue this deployment is configured for.

    Defaults to the file-backed queue so the platform runs with no infrastructure
    at all; set ACTOR_QUEUE_URL=redis://host:port to use Redis instead. The
    Worker and the API call this and never name an implementation.
    """
    url = url if url is not None else os.environ.get(QUEUE_URL_ENV, "")

    # Imported here so the redis package is only needed when Redis is used.
    if url.startswith(("redis://", "rediss://", "unix://")):
        from jobqueue.redis_queue import RedisJobQueue

        return RedisJobQueue(url, name=name)

    from jobqueue.file_queue import FileJobQueue

    return FileJobQueue(url or None, name=name)
