"""A queue backed by a directory of files.

The default, because it needs no server: the platform runs end to end with
nothing installed, and the tests are hermetic. It is also what proves the
abstraction is not Redis-shaped -- the Worker cannot tell which one it has.

FIFO comes from the filenames: each job is stored as
<enqueue-timestamp>-<run_id>.json, and microsecond timestamps sort into
enqueue order for the same reason run ids do.

At-least-once works through two directories. dequeue() atomically renames a job
from pending/ into inflight/, encoding the LEASE IN THE FILENAME:

    pending/20260902-101500123456-<run_id>.json
    inflight/20260902-101500123456-<run_id>__host~4242__1756800000.json
                                   ^^^^^^^^^^^^^^^  ^^^^^^^^^^
                                   owner            claimed at

so claiming is one atomic os.replace rather than a write that could be
interrupted half way. ack() unlinks the file; recover() renames abandoned ones
back to pending/ under their ORIGINAL name, which restores their place in FIFO
order automatically.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from typing import Callable

from jobqueue.base import (
    DEFAULT_QUEUE_NAME,
    DEFAULT_VISIBILITY_TIMEOUT_S,
    Job,
    MAX_JOB_BYTES,
    JobQueue,
    QueueError,
    owner_id,
    owner_is_dead,
)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = REPO_ROOT / "storage" / "queue"

POLL_INTERVAL_S = 0.05


class FileJobQueue(JobQueue):
    def __init__(self, root: Path | str | None = None, *, name: str = DEFAULT_QUEUE_NAME) -> None:
        base = Path(root) if root else DEFAULT_ROOT
        # The queue name becomes a directory, so it must not escape the root.
        self.name = name.replace(":", "-").replace("/", "-")
        self.root = base / self.name
        self.pending = self.root / "pending"
        self.claimed = self.root / "inflight"

    def _ensure(self) -> None:
        self.pending.mkdir(parents=True, exist_ok=True)
        self.claimed.mkdir(parents=True, exist_ok=True)

    # --- lease naming ------------------------------------------------------

    @staticmethod
    def _lease_name(stem: str) -> str:
        # ":" is legal in a Linux filename but awkward everywhere else.
        return f"{stem}__{owner_id().replace(':', '~')}__{int(time.time())}.json"

    @staticmethod
    def _parse_lease(filename: str) -> tuple[str, str, float] | None:
        """-> (original stem, owner, claimed-at epoch), or None if unreadable."""
        if not filename.endswith(".json"):
            return None
        parts = filename[: -len(".json")].rsplit("__", 2)
        if len(parts) != 3:
            return None
        stem, owner, deadline = parts
        try:
            return stem, owner.replace("~", ":"), float(deadline)
        except ValueError:
            return None

    # --- producer ----------------------------------------------------------

    def enqueue(self, job: Job) -> None:
        self._ensure()
        stamp = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S%f}"
        target = self.pending / f"{stamp}-{job.run_id}.json"

        tmp = self.pending / f".{target.name}.tmp"
        tmp.write_text(job.to_json(), encoding="utf-8")
        # Atomic: a reader never sees a half-written job.
        os.replace(tmp, target)

    # --- malformed messages -------------------------------------------------

    @staticmethod
    def _read_job(path: Path) -> str:
        """A job file's text, size-bounded. Raises OSError, QueueError."""
        with path.open("rb") as handle:
            raw = handle.read(MAX_JOB_BYTES + 1)
        if len(raw) > MAX_JOB_BYTES:
            raise QueueError(f"queued job is larger than {MAX_JOB_BYTES} bytes")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise QueueError("queued job is not UTF-8 text") from None

    def _quarantine(self, path: Path, reason: Exception) -> None:
        """Move a message this queue cannot read to rejected/. Never raises.

        Kept, not deleted, so an operator can see what arrived; logged by name
        and reason only, never by content, which is untrusted.
        """
        rejected = self.root / "rejected"
        try:
            rejected.mkdir(parents=True, exist_ok=True)
            os.replace(path, rejected / f"{int(time.time() * 1000)}-{path.name}")
        except OSError:
            return  # claimed or moved by someone else; theirs to deal with
        logger.warning("queue %s: moved malformed job %s to rejected/: %s",
                       self.name, path.name, reason)

    # --- consumer ----------------------------------------------------------

    def _claim_oldest(self, eligible: Callable[[Job], bool] | None = None) -> Job | None:
        """Claim the oldest job, skipping any the caller will not accept.

        With a predicate the payload is READ BEFORE the claim, because what the
        decision needs -- the actor's name, the concurrency key -- is in the
        payload. Reading first
        is already safe: if another Worker claims the file in between, the
        os.replace below fails and the loop moves on, which is exactly what it
        did before. The cost is up to one read per pending job when the head of
        the queue is all saturated Actors.
        """
        if not self.pending.is_dir():
            return None

        for entry in sorted(self.pending.iterdir()):
            if entry.name.startswith(".") or entry.suffix != ".json":
                continue

            if eligible is not None:
                try:
                    peeked = Job.from_json(self._read_job(entry))
                except OSError:
                    continue  # claimed by someone else between listing and reading
                except QueueError as exc:
                    # Not a job this queue could have written. Moved aside
                    # rather than skipped: skipped, it was re-read on every poll
                    # for ever, and nothing ever said why.
                    self._quarantine(entry, exc)
                    continue
                if not eligible(peeked):
                    continue

            lease = self.claimed / self._lease_name(entry.name[: -len(".json")])
            try:
                os.replace(entry, lease)
            except OSError:
                continue  # someone else claimed it first

            try:
                raw = self._read_job(lease)
            except OSError as exc:
                raise QueueError(f"could not read leased job {lease.name}: {exc}") from exc
            except QueueError as exc:
                self._quarantine(lease, exc)
                continue

            try:
                job = Job.from_json(raw)
            except QueueError as exc:
                self._quarantine(lease, exc)
                continue  # one bad message must not stop the next good one
            job.receipt = lease.name
            return job

        return None

    def dequeue(
        self, timeout: float = 0.0, *, eligible: Callable[[Job], bool] | None = None
    ) -> Job | None:
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            job = self._claim_oldest(eligible)
            if job is not None:
                return job
            if time.monotonic() >= deadline:
                return None
            time.sleep(POLL_INTERVAL_S)

    def ack(self, job: Job) -> bool:
        if not job.receipt:
            raise QueueError(f"run {job.run_id} was not leased by this queue - nothing to ack")

        lease = self.claimed / job.receipt
        try:
            lease.unlink()
        except FileNotFoundError:
            return False  # already reclaimed; the caller may want to know
        return True

    def nack(self, job: Job) -> bool:
        """Move a lease back to pending WITHOUT counting an attempt.

        Deliberately not recover()'s code path: that increments `attempts`,
        which exists to give up on a job that keeps killing its Worker. Nothing
        ran here, so counting it would retire a perfectly good run after three
        collisions with a busy session.

        Same write-before-unlink ordering as recover(), and for the same reason:
        crashing between the two duplicates a delivery (which the consumer
        tolerates), while the other order would lose the job outright.
        """
        if not job.receipt:
            raise QueueError(f"run {job.run_id} was not leased by this queue - nothing to nack")

        lease = self.claimed / job.receipt
        parsed = self._parse_lease(job.receipt)
        stem = parsed[0] if parsed else job.receipt[: -len(".json")]

        if not lease.exists():
            return False  # already reclaimed or acked; nothing to hand back

        try:
            raw = lease.read_text(encoding="utf-8")
        except OSError:
            return False

        self._ensure()
        target = self.pending / f"{stem}.json"
        tmp = self.pending / f".{target.name}.tmp"
        tmp.write_text(raw, encoding="utf-8")
        os.replace(tmp, target)
        lease.unlink(missing_ok=True)
        return True

    def recover(
        self, *, visibility_timeout: float | None = DEFAULT_VISIBILITY_TIMEOUT_S
    ) -> int:
        """Put abandoned leases back at the front of the queue.

        visibility_timeout=None (the default) means a lease never expires on
        AGE -- only a provably dead owner loses one. That is what lets an Actor
        run for hours without the queue reclaiming its live job.
        """
        if not self.claimed.is_dir():
            return 0

        now = time.time()
        reclaimed = 0

        for lease in sorted(self.claimed.iterdir()):
            if lease.name.startswith(".") or lease.suffix != ".json":
                continue

            parsed = self._parse_lease(lease.name)
            if parsed is None:
                # A lease we cannot read is a lease we cannot trust; treat the
                # job as abandoned rather than leaving it stuck for ever.
                stem, abandoned = lease.name[: -len(".json")], True
            else:
                stem, owner, claimed_at = parsed
                expired = (
                    visibility_timeout is not None
                    and (now - claimed_at) > visibility_timeout
                )
                abandoned = owner_is_dead(owner) or expired

            if not abandoned:
                continue

            try:
                job = Job.from_json(self._read_job(lease))
            except OSError:
                lease.unlink(missing_ok=True)
                continue
            except QueueError as exc:
                self._quarantine(lease, exc)
                continue

            job.attempts += 1

            # Write the job back BEFORE dropping the lease. Crashing between the
            # two duplicates a delivery, which the consumer tolerates; the other
            # order would lose the job outright, which at-least-once must not.
            self._ensure()
            target = self.pending / f"{stem}.json"
            tmp = self.pending / f".{target.name}.tmp"
            tmp.write_text(job.to_json(), encoding="utf-8")
            os.replace(tmp, target)
            lease.unlink(missing_ok=True)

            reclaimed += 1

        return reclaimed

    # --- introspection -----------------------------------------------------

    def depth(self) -> int:
        if not self.pending.is_dir():
            return 0
        return sum(
            1
            for p in self.pending.iterdir()
            if p.suffix == ".json" and not p.name.startswith(".")
        )

    def inflight(self) -> int:
        if not self.claimed.is_dir():
            return 0
        return sum(
            1
            for p in self.claimed.iterdir()
            if p.suffix == ".json" and not p.name.startswith(".")
        )

    def known_run_ids(self) -> set[str]:
        """Every run held here, waiting or leased.

        The two directories are read pending -> inflight -> PENDING AGAIN, and
        the results unioned. A job is always in exactly one of them (both moves
        are a single atomic os.replace), but a sequential scan can still straddle
        a move: dequeue takes pending -> inflight, which this order already
        catches, while recover() takes inflight -> pending, which the second
        pending read catches. Missing a job would make the reconciler call a live
        run stranded and queue it twice.
        """
        found: set[str] = set()
        for directory in (self.pending, self.claimed, self.pending):
            if not directory.is_dir():
                continue
            for entry in directory.iterdir():
                if entry.suffix != ".json" or entry.name.startswith("."):
                    continue
                try:
                    # Read the payload rather than parsing the filename: the
                    # run id is data, and a filename is a naming convention.
                    found.add(Job.from_json(self._read_job(entry)).run_id)
                except (OSError, QueueError):
                    continue
        return found

    def purge(self) -> int:
        """Drop every job, waiting or leased. For tests and operator cleanup."""
        removed = self.depth() + self.inflight()
        for directory in (self.pending, self.claimed):
            if directory.is_dir():
                for entry in directory.iterdir():
                    entry.unlink(missing_ok=True)
        return removed
