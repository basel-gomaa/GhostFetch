#!/usr/bin/env python3
"""The reconciler: finds runs that nothing will ever pick up.

At-least-once delivery protects work that is IN the queue. It cannot protect a
run whose job left the queue without the run finishing -- the queue was purged,
Redis was flushed, the API died between registering a run and enqueueing it, or
a lease was reclaimed and then lost. The registry then holds a run that is
PENDING or RUNNING for ever while no Worker has any reason to touch it.

    stranded == not terminal AND absent from the queue (waiting or leased)

That test is sound because of an invariant the consumer maintains: a Worker only
executes a job it has leased, and only acks after the run is terminal. So while
a run is RUNNING its job is always somewhere in the queue. If it is not, nobody
is running it.

WHAT IT DOES NOT DO: kill anything on a hunch. A run younger than the grace
period is left alone, because "not in the queue yet" is also what a run looks
like for the few milliseconds between registry.create() and queue.enqueue().

    python3 worker/reconciler.py --dry-run     # report, change nothing
    python3 worker/reconciler.py               # re-queue what is stranded
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobqueue.base import (  # noqa: E402
    DEFAULT_QUEUE_NAME,
    MAX_ATTEMPTS,
    Job,
    JobQueue,
    QueueError,
    open_queue,
)
from registry.run_registry import RegistryError, RunRegistry  # noqa: E402
from shared.logging import write_line  # noqa: E402
from shared.models import Run, RunStatus  # noqa: E402
from worker.executor import kill_container  # noqa: E402

# A run must be at least this old before it can be called stranded. Anything
# less races the gap between registering a run and enqueueing its job.
DEFAULT_GRACE_S = 60.0

# Cap the scan so a registry with a long history stays cheap to reconcile.
DEFAULT_SCAN_LIMIT = 500


def log(message: str) -> None:
    write_line(f"[reconciler] {message}")


@dataclass
class ReconcileReport:
    scanned: int = 0
    requeued: list[str] = field(default_factory=list)
    abandoned: list[str] = field(default_factory=list)
    skipped_young: list[str] = field(default_factory=list)

    @property
    def stranded(self) -> int:
        return len(self.requeued) + len(self.abandoned)

    def summary(self) -> str:
        return (
            f"scanned {self.scanned} unfinished run(s): "
            f"{len(self.requeued)} requeued, {len(self.abandoned)} abandoned, "
            f"{len(self.skipped_young)} too young to judge"
        )


def _age_seconds(run: Run) -> float:
    """How long since this run last showed signs of life."""
    stamp = run.started_at or run.created_at
    if not stamp:
        return float("inf")
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    return (datetime.now(timezone.utc) - moment).total_seconds()


def find_stranded(
    registry: RunRegistry,
    queue: JobQueue,
    *,
    grace: float = DEFAULT_GRACE_S,
    limit: int = DEFAULT_SCAN_LIMIT,
) -> tuple[list[Run], list[Run]]:
    """-> (stranded runs, runs too young to judge). Changes nothing."""
    # Snapshot the QUEUE first. Taken the other way round, a job leased between
    # the two reads would look absent and a healthy run would be called
    # stranded; this ordering can only ever be over-inclusive in the safe
    # direction, and the age check plus the re-read in reconcile() cover that.
    queued = queue.known_run_ids()

    stranded: list[Run] = []
    young: list[Run] = []

    for status in (RunStatus.PENDING, RunStatus.RUNNING):
        for run in registry.list(status=status, limit=limit, skip_corrupt=True):
            if run.run_id in queued:
                continue
            (young if _age_seconds(run) < grace else stranded).append(run)

    return stranded, young


def reconcile(
    registry: RunRegistry,
    queue: JobQueue,
    *,
    grace: float = DEFAULT_GRACE_S,
    limit: int = DEFAULT_SCAN_LIMIT,
    max_attempts: int = MAX_ATTEMPTS,
    dry_run: bool = False,
) -> ReconcileReport:
    """Put stranded runs back on the queue, or give up on them."""
    stranded, young = find_stranded(registry, queue, grace=grace, limit=limit)
    report = ReconcileReport(
        scanned=len(stranded) + len(young), skipped_young=[r.run_id for r in young]
    )

    for candidate in stranded:
        # Re-read: the snapshot is a moment old, and a run that finished in the
        # meantime must not be resurrected.
        try:
            run = registry.get(candidate.run_id)
        except RegistryError as exc:
            log(f"run {candidate.run_id} could not be re-read: {exc}")
            continue

        if run.is_terminal:
            continue

        # Re-check the QUEUE too, per candidate. known_run_ids() reads two lists
        # and a job moving between them can slip through a single scan -- proven,
        # not theorised: under a pathological churn a job that existed on disk
        # was missed. One miss is cheap to survive; requiring the SAME job to be
        # missed by two independent scans makes a false positive vanishingly
        # unlikely, and the consumer's idempotency covers even that.
        try:
            if run.run_id in queue.known_run_ids():
                continue
        except QueueError as exc:
            log(f"could not re-check the queue for run {run.run_id}: {exc}")
            continue

        if run.attempts >= max_attempts:
            report.abandoned.append(run.run_id)
            log(f"run {run.run_id} stranded after {run.attempts} attempt(s) - giving up")
            if not dry_run:
                try:
                    registry.complete(
                        run.run_id,
                        # CANCELLED: stranded and given up on, not a platform
                        # fault while running. See the consumer's abandonment path.
                        status=RunStatus.CANCELLED,
                        error=f"stranded after {run.attempts} attempt(s) with no queued job",
                    )
                except RegistryError as exc:
                    log(f"could not close out run {run.run_id}: {exc}")
            continue

        report.requeued.append(run.run_id)
        log(f"run {run.run_id} ({run.status.value}, attempt {run.attempts}) is stranded - requeueing")

        if dry_run:
            continue

        try:
            if run.status is RunStatus.RUNNING:
                # Its container may still be alive with nothing watching it.
                # Two containers writing one run directory is exactly what the
                # consumer's orphan handling exists to prevent.
                if kill_container(run.run_id):
                    log(f"killed the container run {run.run_id} left behind")
                registry.requeue(run.run_id, "stranded: no queued job, requeued by the reconciler")

            payload = registry.get_input(run.run_id) if run.input == {} else run.input
        except RegistryError as exc:
            log(f"could not prepare run {run.run_id} for requeueing: {exc}")
            report.requeued.remove(run.run_id)
            continue

        try:
            queue.enqueue(
                Job(
                    run_id=run.run_id,
                    actor=run.actor,
                    input=payload if isinstance(payload, dict) else {},
                    attempts=run.attempts,
                    # Carried over, or a requeued run would skip its per-session
                    # slot and lean on the database lease alone.
                    concurrency_key=run.concurrency_key,
                )
            )
        except QueueError as exc:
            log(f"could not requeue run {run.run_id}: {exc}")
            report.requeued.remove(run.run_id)

    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="reconciler",
        description="Requeue runs that are unfinished but absent from the queue.",
    )
    parser.add_argument("--queue-url", help="queue location (default: $ACTOR_QUEUE_URL)")
    parser.add_argument("--queue-name", default=DEFAULT_QUEUE_NAME, help="which queue to reconcile")
    parser.add_argument("--registry-root", help="run storage root (default: storage/runs)")
    parser.add_argument(
        "--grace",
        type=float,
        default=DEFAULT_GRACE_S,
        help=f"seconds a run must be unfinished before it counts (default {DEFAULT_GRACE_S:.0f})",
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_SCAN_LIMIT, help="max runs to scan")
    parser.add_argument(
        "--max-attempts", type=int, default=MAX_ATTEMPTS, help="give up beyond this many attempts"
    )
    parser.add_argument("--dry-run", action="store_true", help="report without changing anything")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        queue = open_queue(args.queue_url, name=args.queue_name)
    except QueueError as exc:
        print(f"[reconciler] ERROR: {exc}", file=sys.stderr, flush=True)
        return 2

    registry = RunRegistry(args.registry_root) if args.registry_root else RunRegistry()

    try:
        report = reconcile(
            registry,
            queue,
            grace=args.grace,
            limit=args.limit,
            max_attempts=args.max_attempts,
            dry_run=args.dry_run,
        )
    except (QueueError, RegistryError) as exc:
        print(f"[reconciler] ERROR: {exc}", file=sys.stderr, flush=True)
        return 2
    finally:
        queue.close()

    log(("dry run: " if args.dry_run else "") + report.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
