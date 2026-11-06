"""A read-model over the Run Registry: platform metrics and collector health.

Pure derivation. It adds NO storage, NO background process and NO write path --
it reads the records the registry already keeps and rolls them up. Two questions
it answers that no single run can:

    platform_metrics()   how are runs going overall? counts by status and actor.
    collector_health()   how is each Actor behaving? last run, last success /
                         failure, success rate, counts.

BOUNDED BY DESIGN. list() scans every record dir, and there are thousands, so a
naive all-time rollup would get slower for ever. Both functions read a capped,
newest-first WINDOW (default METRICS_WINDOW) and say so in their output, so the
numbers are honestly "the last N runs", never silently-partial all-time totals.
Live queue depth, when a queue is supplied, is exact and separate.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from registry.run_registry import RunRegistry  # noqa: E402
from shared.models import RunStatus, TERMINAL_STATUSES  # noqa: E402

# How many recent runs a rollup reads. Bounds the cost as history grows; the
# result names the window so a reader is never misled about its scope.
METRICS_WINDOW = 500

# Every status, so a rollup always has a row for each -- a 0 is information
# ("nothing failed"), a missing key is ambiguous.
_ALL_STATUSES = [s.value for s in RunStatus]

_ACTIVE = {RunStatus.PENDING.value, RunStatus.RUNNING.value}
_SUCCESS = RunStatus.SUCCEEDED.value


def _zeroed_counts() -> dict[str, int]:
    return {status: 0 for status in _ALL_STATUSES}


@dataclass
class PlatformMetrics:
    window: int                       # how many runs were read
    scanned: int                      # how many actually existed to read (<= window)
    by_status: dict[str, int]
    by_actor: dict[str, dict[str, int]]
    active: int                       # PENDING + RUNNING in the window
    queued: int | None = None         # live queue depth, if a queue was given
    leased: int | None = None
    queue_ok: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "scanned": self.scanned,
            "runs_by_status": self.by_status,
            "runs_by_actor": self.by_actor,
            "active_runs": self.active,
            "queue": {"queued": self.queued, "leased": self.leased, "ok": self.queue_ok},
        }


@dataclass
class CollectorHealth:
    name: str
    total: int = 0
    by_status: dict[str, int] = field(default_factory=_zeroed_counts)
    last_run_id: str | None = None
    last_run_at: str | None = None
    last_status: str | None = None
    last_success_at: str | None = None
    last_failure_at: str | None = None

    @property
    def finished(self) -> int:
        return sum(self.by_status[s] for s in (RunStatus.SUCCEEDED.value,
                                               RunStatus.FAILED.value,
                                               RunStatus.PLATFORM_ERROR.value,
                                               RunStatus.CANCELLED.value))

    @property
    def success_rate(self) -> float | None:
        """SUCCEEDED / finished, or None when nothing has finished yet.

        None rather than 0.0 on purpose: "no finished runs" and "everything
        failed" are different facts, and a 0.0 would conflate them.
        """
        done = self.finished
        return round(self.by_status[_SUCCESS] / done, 4) if done else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "collector": self.name,
            "total": self.total,
            "by_status": self.by_status,
            "success_rate": self.success_rate,
            "last_run": {"run_id": self.last_run_id, "at": self.last_run_at,
                         "status": self.last_status},
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
        }


def _finished_at(run) -> str | None:
    return run.finished_at or run.created_at


def platform_metrics(
    registry: RunRegistry, *, window: int = METRICS_WINDOW, queue=None
) -> PlatformMetrics:
    runs = registry.list(limit=window, skip_corrupt=True)

    by_status = _zeroed_counts()
    by_actor: dict[str, dict[str, int]] = {}
    active = 0
    for run in runs:
        status = run.status.value
        by_status[status] += 1
        by_actor.setdefault(run.actor, _zeroed_counts())[status] += 1
        if status in _ACTIVE:
            active += 1

    metrics = PlatformMetrics(
        window=window, scanned=len(runs), by_status=by_status,
        by_actor=by_actor, active=active,
    )

    if queue is not None:
        try:
            metrics.queued = queue.depth()
            metrics.leased = queue.inflight()
            metrics.queue_ok = True
        except Exception:  # noqa: BLE001 - a broken queue is a datum, not a crash
            metrics.queue_ok = False

    return metrics


def collector_health(
    registry: RunRegistry, *, window: int = METRICS_WINDOW
) -> list[CollectorHealth]:
    """Per-Actor health from the recent window, one entry per Actor SEEN.

    Runs come newest-first, so the FIRST time an Actor is seen is its last run;
    the first SUCCEEDED / (FAILED|PLATFORM_ERROR|CANCELLED) seen are its last
    success / failure. Later (older) rows only add to the counts.
    """
    runs = registry.list(limit=window, skip_corrupt=True)

    health: dict[str, CollectorHealth] = {}
    failure_states = {RunStatus.FAILED.value, RunStatus.PLATFORM_ERROR.value,
                      RunStatus.CANCELLED.value}

    for run in runs:  # newest first
        entry = health.setdefault(run.actor, CollectorHealth(name=run.actor))
        status = run.status.value
        entry.total += 1
        entry.by_status[status] += 1

        if entry.last_run_id is None:            # newest -> this is the last run
            entry.last_run_id = run.run_id
            entry.last_run_at = _finished_at(run)
            entry.last_status = status
        if status == _SUCCESS and entry.last_success_at is None:
            entry.last_success_at = _finished_at(run)
        if status in failure_states and entry.last_failure_at is None:
            entry.last_failure_at = _finished_at(run)

    return sorted(health.values(), key=lambda h: h.name)
