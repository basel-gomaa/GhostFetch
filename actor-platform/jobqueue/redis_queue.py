"""A queue backed by Redis lists.

LPUSH onto the head and pop from the tail gives FIFO, which is the ordering the
JobQueue contract promises. Redis is confined to this file: nothing else in the
platform imports the redis package or knows the queue is a list.

At-least-once is the standard reliable-queue shape. dequeue() does not pop --
it uses (B)LMOVE to shift the payload ATOMICALLY from the pending list to an
inflight list, so a Worker that dies between the two can never make the job
vanish. A lease (owner + deadline) is recorded beside it, ack() removes it, and
recover() moves abandoned payloads back.

    <name>            pending payloads, FIFO
    <name>:inflight   leased payloads, not yet acked
    <name>:leases     payload -> "<owner>|<claimed at>"
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from jobqueue.base import (
    DEFAULT_QUEUE_NAME,
    DEFAULT_VISIBILITY_TIMEOUT_S,
    Job,
    JobQueue,
    QueueError,
    owner_id,
    owner_is_dead,
)


logger = logging.getLogger(__name__)

# How long a FILTERED dequeue sleeps between peeks. Only used when an
# `eligible` predicate rules BLMOVE out; the unfiltered path still blocks.
POLL_INTERVAL_S = 0.05


def _text(value: bytes | str) -> str:
    """A Redis reply as str. The client is built with decode_responses=True,
    so this is already str at run time; redis-py's annotations cannot say so."""
    return value.decode() if isinstance(value, bytes) else value


def _text_or_none(value: bytes | str | None) -> str | None:
    return None if value is None else _text(value)


class RedisJobQueue(JobQueue):
    def __init__(self, url: str, *, name: str = DEFAULT_QUEUE_NAME) -> None:
        try:
            import redis
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise QueueError(
                "the redis package is required for ACTOR_QUEUE_URL=redis://... "
                "(pip install redis), or unset it to use the file queue"
            ) from exc

        self.name = name
        self.inflight_key = f"{name}:inflight"
        self.leases_key = f"{name}:leases"
        self.rejected_key = f"{name}:rejected"
        self.url = url
        try:
            self._client = redis.Redis.from_url(url, decode_responses=True)
            self._client.ping()
        except Exception as exc:  # redis raises several unrelated types
            raise QueueError(f"cannot reach redis at {url}: {exc}") from exc

    # --- producer ----------------------------------------------------------

    def enqueue(self, job: Job) -> None:
        try:
            self._client.lpush(self.name, job.to_json())
        except Exception as exc:
            raise QueueError(f"could not enqueue to redis: {exc}") from exc

    # --- malformed messages -------------------------------------------------

    def _quarantine(self, source: str, payload: str, reason: Exception) -> None:
        """Move one unreadable payload from `source` to <name>:rejected. Never raises.

        LREM first: if it removes nothing, another client already handled the
        payload and it is not ours to move. Logged by reason and size only --
        the payload is untrusted and may be large.
        """
        try:
            if self._client.lrem(source, 1, payload):
                self._client.lpush(self.rejected_key, payload)
            self._client.hdel(self.leases_key, payload)
        except Exception:  # noqa: BLE001 - quarantine is best-effort
            return
        logger.warning("queue %s: moved a malformed job (%d bytes) to %s: %s",
                       self.name, len(payload or ""), self.rejected_key, reason)

    # --- consumer ----------------------------------------------------------

    def _claim_eligible(self, eligible: Callable[[Job], bool]) -> str | None:
        """Claim the oldest payload the caller accepts.

        LMOVE cannot filter, so this peeks with LRANGE and claims with
        LREM -- the same by-payload idiom ack() already uses. LREM returning 1
        means THIS client removed it and owns the claim; 0 means another client
        got there first, so the candidate is simply skipped. That return value
        is what makes a non-atomic peek-then-claim safe.
        """
        # The oldest end: enqueue LPUSHes onto the head, so the tail is oldest.
        payloads = [_text(p) for p in self._client.lrange(self.name, 0, -1)]
        for payload in reversed(payloads):
            try:
                peeked = Job.from_json(payload)
            except QueueError as exc:
                # Moved aside rather than skipped: skipped, it was re-peeked on
                # every poll for ever.
                self._quarantine(self.name, payload, exc)
                continue
            if not eligible(peeked):
                continue
            if not self._client.lrem(self.name, 1, payload):
                continue  # another client claimed it between the peek and here
            self._client.lpush(self.inflight_key, payload)
            return payload
        return None

    def dequeue(
        self, timeout: float = 0.0, *, eligible: Callable[[Job], bool] | None = None
    ) -> Job | None:
        try:
            if eligible is not None:
                # BLMOVE cannot express a predicate, so a filtered dequeue polls.
                deadline = time.monotonic() + max(timeout, 0.0)
                payload = None
                while True:
                    payload = self._claim_eligible(eligible)
                    if payload is not None or time.monotonic() >= deadline:
                        break
                    time.sleep(POLL_INTERVAL_S)
            elif timeout <= 0:
                # The blocking form treats a 0 timeout as "wait for ever",
                # which is the opposite of what timeout=0 means here.
                payload = _text_or_none(
                    self._client.lmove(self.name, self.inflight_key, "RIGHT", "LEFT"))
            else:
                # timeout stays a float: BLMOVE takes fractional seconds, and
                # int() would turn 0.5 into 0, which Redis reads as "for ever".
                # redis-py annotates it as int.
                payload = _text_or_none(self._client.blmove(
                    self.name, self.inflight_key, timeout, "RIGHT", "LEFT"  # type: ignore[arg-type]
                ))
        except QueueError:
            raise
        except Exception as exc:
            raise QueueError(f"could not dequeue from redis: {exc}") from exc

        if not payload:
            return None

        # Recorded after the atomic move, so a crash in between leaves a
        # lease-less inflight payload -- which recover() treats as abandoned.
        self._client.hset(self.leases_key, payload, f"{owner_id()}|{time.time()}")

        try:
            job = Job.from_json(payload)
        except QueueError as exc:
            # Leased already, so it leaves the inflight list for rejected; a
            # malformed message is a dropped delivery, never a stuck consumer.
            self._quarantine(self.inflight_key, payload, exc)
            return None
        job.receipt = payload
        return job

    def ack(self, job: Job) -> bool:
        if job.receipt is None:
            raise QueueError(f"run {job.run_id} was not leased by this queue - nothing to ack")

        try:
            removed = self._client.lrem(self.inflight_key, 1, job.receipt)
            self._client.hdel(self.leases_key, job.receipt)
        except Exception as exc:
            raise QueueError(f"could not ack: {exc}") from exc

        return bool(removed)

    def nack(self, job: Job) -> bool:
        """Return a leased payload to the queue UNRUN, without counting an attempt.

        The by-payload LREM is ack()'s idiom; the LPUSH back onto the head is
        recover()'s. Head, not tail: this job was next in line and did not get
        its turn, so it keeps its place rather than going to the back.
        """
        if job.receipt is None:
            raise QueueError(f"run {job.run_id} was not leased by this queue - nothing to nack")

        try:
            removed = self._client.lrem(self.inflight_key, 1, job.receipt)
            if not removed:
                return False  # already reclaimed or acked
            self._client.hdel(self.leases_key, job.receipt)
            self._client.lpush(self.name, job.receipt)
        except Exception as exc:
            raise QueueError(f"could not nack: {exc}") from exc

        return True

    def recover(
        self, *, visibility_timeout: float | None = DEFAULT_VISIBILITY_TIMEOUT_S
    ) -> int:
        try:
            payloads = [_text(p) for p in self._client.lrange(self.inflight_key, 0, -1)]
        except Exception as exc:
            raise QueueError(f"could not inspect inflight jobs: {exc}") from exc

        now = time.time()
        reclaimed = 0

        for payload in payloads:
            lease = _text_or_none(self._client.hget(self.leases_key, payload))
            if lease is None:
                abandoned = True  # leased but never recorded, or already cleared
            else:
                owner, _, claimed_at = lease.rpartition("|")
                try:
                    abandoned = (
                        owner_is_dead(owner)
                        or (
                            visibility_timeout is not None
                            and (now - float(claimed_at)) > visibility_timeout
                        )
                    )
                except ValueError:
                    abandoned = True

            if not abandoned:
                continue

            try:
                job = Job.from_json(payload)
            except QueueError as exc:
                self._quarantine(self.inflight_key, payload, exc)
                continue

            job.attempts += 1

            # Requeue BEFORE clearing the lease: crashing in between duplicates
            # a delivery, which the consumer tolerates, whereas the other order
            # would lose the job, which at-least-once must not.
            # RPUSH is the tail, which is the end (B)LMOVE serves from next.
            self._client.rpush(self.name, job.to_json())
            self._client.lrem(self.inflight_key, 1, payload)
            self._client.hdel(self.leases_key, payload)

            reclaimed += 1

        return reclaimed

    # --- introspection -----------------------------------------------------

    def depth(self) -> int:
        try:
            return int(self._client.llen(self.name))
        except Exception as exc:
            raise QueueError(f"could not measure the redis queue: {exc}") from exc

    def inflight(self) -> int:
        try:
            return int(self._client.llen(self.inflight_key))
        except Exception as exc:
            raise QueueError(f"could not measure inflight jobs: {exc}") from exc

    def known_run_ids(self) -> set[str]:
        """Every run held here, waiting or leased.

        Both lists are read inside a MULTI/EXEC so the answer is a genuine
        point-in-time snapshot. Read separately, a job moving between the lists
        could fall through the gap, and the reconciler would then call a live run
        stranded and queue it a second time.
        """
        try:
            pipe = self._client.pipeline(transaction=True)
            pipe.lrange(self.name, 0, -1)
            pipe.lrange(self.inflight_key, 0, -1)
            waiting, leased = pipe.execute()
            payloads = list(waiting) + list(leased)
        except Exception as exc:
            raise QueueError(f"could not list queued runs: {exc}") from exc

        found: set[str] = set()
        for payload in payloads:
            try:
                found.add(Job.from_json(payload).run_id)
            except QueueError:
                continue
        return found

    def purge(self) -> int:
        removed = self.depth() + self.inflight()
        self._client.delete(self.name, self.inflight_key, self.leases_key)
        return removed

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
