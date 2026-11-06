"""The Run Registry, backed by Postgres.

Same public surface as RunRegistry, and it SUBCLASSES it -- so the artifact half
(`run_dir`, `_artifact_path`, `get_input`, `get_output`, `get_logs`) is not
reimplemented here at all, it is inherited. Only the metadata half is overridden:

    metadata  ->  postgres   (this file)
    artifacts ->  storage/runs/<run_id>/{input,output,log}.txt   (INHERITED)

WHY MOVE METADATA AT ALL. Multi-tenancy asks questions a directory scan cannot
answer at a sensible cost. Measured on this deployment's 1802 run directories:
filtering by a single Actor takes 56 ms on the filesystem and 0.3 ms here,
because only 8 of 1802 match and a non-selective filter has to scan everything
to fill one page. A per-user listing has exactly that shape -- each of thirty
users owns a small fraction -- so the filesystem cost is paid on every page load
of the console, forever. "Which sessions are busy" and "which accounts need
reconnecting" are not directory scans in any form.

WHAT GETS BETTER, not just different: the state-transition guard. On the
filesystem it is read-check-write under an fcntl lock, which the module's own
docstring notes is "single-host by construction". Here it is ONE statement --

    UPDATE runs SET status=%s WHERE run_id=%s AND status = ANY(%s) RETURNING *

-- where the allowed source statuses come from the SAME _ALLOWED_TRANSITIONS
table. Zero rows returned is the refusal. No lock file, and correct across
processes and hosts. The completion-versus-cancel race is decided by Postgres.

run.json IS STILL WRITTEN, and is no longer read. The registry's own docstring
says "a run IS its run.json"; that stops being true here, so it is said plainly
rather than left for a reader to discover. The file remains as a local
breadcrumb -- it makes a run directory self-describing to anyone poking around
with `cat`, and it is what tools/import_runs.py reads.

OWNERSHIP IS A CONSTRUCTOR, NOT A FILTER. `registry.for_owner(user_id)` returns
a view that carries the predicate on every read. A handler cannot forget to
apply it, because it never holds the unscoped object -- the same structural
argument api/schemas.py already makes about run_dir.
"""

from __future__ import annotations

import builtins
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from db.pool import pool
from registry.dataset import DatasetItem
from registry.run_registry import (
    _ALLOWED_TRANSITIONS,
    _IMMUTABLE_FIELDS,
    _PREFIX_PATTERN,
    _iso,
    _now,
    AmbiguousRunId,
    InvalidRunId,
    InvalidTransition,
    CancelTooLate,
    RegistryError,
    RunIsLive,
    RunNotFound,
    RunRegistry,
)
from shared.models import (
    RUN_ID_RULE,
    Run,
    RunStatus,
    is_valid_actor_name,
    is_valid_run_id,
)

# Every column of `runs`, in the order Run's fields are named. One place, so a
# schema change breaks loudly here instead of silently dropping a field.
_COLUMNS = (
    "run_id, owner_id, actor, status, social_account_id, concurrency_key, "
    "created_at, started_at, finished_at, duration_seconds, exit_code, error, "
    "run_dir, log_lines, attempts, input, ai_prompt, report_email, report_sent_at, "
    "display_port, timeout_s, memory_mb, cpus, peak_memory_mb, error_info, partial"
)

# Fields a caller may set through update(). Deliberately NOT everything in the
# table: owner_id is set once at creation, and report_sent_at is delivery's.
_UPDATABLE = frozenset({
    "status", "started_at", "finished_at", "duration_seconds", "exit_code",
    "error", "log_lines", "attempts", "input", "social_account_id",
    "concurrency_key", "ai_prompt", "report_email", "report_sent_at",
    "display_port", "timeout_s", "memory_mb", "cpus", "peak_memory_mb",
    "error_info", "partial",
})


def _sources_for(target: RunStatus) -> list[str]:
    """Which statuses a run may be in for `target` to be a legal next step.

    Derived from _ALLOWED_TRANSITIONS rather than restated, so the SQL guard and
    the filesystem guard can never disagree about the lifecycle.
    """
    return [
        source.value
        for source, allowed in _ALLOWED_TRANSITIONS.items()
        if target in allowed
    ]


def _ts(value: Any) -> str | None:
    """A database timestamp as the ISO string Run carries."""
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return _iso(moment.astimezone(timezone.utc))
    return str(value)


class PgRunRegistry(RunRegistry):
    """Run metadata in Postgres; artifacts still on disk, via the base class."""

    def __init__(self, root: Path | str | None = None, *, owner_id: str | None = None,
                 unscoped: bool = True) -> None:
        super().__init__(root)
        # None + unscoped means "see everything" (worker, reconciler, admin).
        # A view for a user carries their id and unscoped=False; the two are
        # separate so `for_owner(None)` cannot silently become "see everything".
        self._owner_id = owner_id
        self._unscoped = unscoped

    # --- scoping ------------------------------------------------------------

    def for_owner(self, owner_id: str | None) -> PgRunRegistry:
        """A view restricted to one user's runs.

        `owner_id=None` yields a view that can see NOTHING, which is the right
        answer for an authenticated caller who owns no runs -- not "everything".
        Ask for the unscoped registry explicitly if that is what you want.
        """
        return PgRunRegistry(self.root, owner_id=owner_id, unscoped=False)

    def _scope(self, params: list[Any]) -> str:
        """The ownership predicate, and its parameter, or '' when unscoped."""
        if self._unscoped:
            return ""
        if self._owner_id is None:
            return " AND FALSE"
        params.append(self._owner_id)
        return " AND owner_id = %s"

    # --- reads --------------------------------------------------------------

    def _row_to_run(self, row: dict[str, Any]) -> Run:
        return Run(
            run_id=row["run_id"],
            actor=row["actor"],
            status=RunStatus(row["status"]),
            created_at=_ts(row["created_at"]) or "",
            started_at=_ts(row["started_at"]),
            finished_at=_ts(row["finished_at"]),
            duration_seconds=row["duration_seconds"],
            exit_code=row["exit_code"],
            error=row["error"],
            # Derived, never read back: the artifact half (run_dir(), get_output,
            # ...) already resolves paths from root + id, and a stored value can
            # have been imported from a run.json the Actor's container rewrote.
            run_dir=str(self.root / row["run_id"]),
            log_lines=row["log_lines"] or 0,
            attempts=row["attempts"] or 0,
            input=row["input"] or {},
            owner_id=str(row["owner_id"]) if row["owner_id"] else None,
            social_account_id=(
                str(row["social_account_id"]) if row["social_account_id"] else None
            ),
            concurrency_key=row["concurrency_key"],
            display_port=row["display_port"],
            timeout_s=row["timeout_s"],
            memory_mb=row["memory_mb"],
            cpus=row["cpus"],
            peak_memory_mb=row["peak_memory_mb"],
            # Both are absent from rows written before migration 006, and both
            # have a defined meaning when absent -- see the migration.
            error_info=row.get("error_info"),
            partial=bool(row.get("partial")),
        )

    def get(self, run_id: str) -> Run:
        # Validate BEFORE querying. Parameterised SQL is injection-safe either
        # way, but the base class promises InvalidRunId for a malformed id and a
        # caller distinguishes "you asked wrongly" from "it is not there".
        # Dropping this turned `get("../../etc")` into a plain RunNotFound.
        if not is_valid_run_id(run_id):
            raise InvalidRunId(f"invalid run id {run_id!r}: {RUN_ID_RULE}")
        params: list[Any] = [run_id]
        scope = self._scope(params)
        row = self._fetchone(
            f"SELECT {_COLUMNS} FROM runs WHERE run_id = %s{scope}", params
        )
        if row is None:
            # A run this caller may not see is reported as ABSENT, never as
            # forbidden. A 403 confirms the id exists, which is itself a leak.
            raise RunNotFound(f"no run with id '{run_id}'")
        return self._row_to_run(row)

    def exists(self, run_id: str) -> bool:
        if not is_valid_run_id(run_id):
            return False
        params: list[Any] = [run_id]
        scope = self._scope(params)
        return self._fetchone(f"SELECT 1 AS x FROM runs WHERE run_id = %s{scope}", params) is not None

    def resolve(self, run_id_or_prefix: str) -> str:
        """Expand a unique run id prefix. Owner-scoped, which matters.

        An unscoped prefix search leaks other users' runs through the ERROR:
        AmbiguousRunId names the matches, and even the choice between "no match"
        and "several" tells a caller whether ids beginning that way exist.
        """
        if is_valid_run_id(run_id_or_prefix) and self.exists(run_id_or_prefix):
            return run_id_or_prefix

        if not run_id_or_prefix or not _PREFIX_PATTERN.fullmatch(run_id_or_prefix):
            raise InvalidRunId(f"invalid run id {run_id_or_prefix!r}: {RUN_ID_RULE}")

        params: list[Any] = [run_id_or_prefix]
        scope = self._scope(params)
        rows = self._fetchall(
            f"SELECT run_id FROM runs WHERE run_id LIKE %s || '%%'{scope} "
            f"ORDER BY run_id DESC LIMIT 6",
            params,
        )
        matches = [r["run_id"] for r in rows]
        if not matches:
            raise RunNotFound(f"no run matching '{run_id_or_prefix}'")
        if len(matches) > 1:
            shown = ", ".join(matches[:5]) + (" ..." if len(matches) > 5 else "")
            raise AmbiguousRunId(f"'{run_id_or_prefix}' matches {len(matches)} runs: {shown}")
        return matches[0]

    def list(
        self,
        *,
        actor: str | None = None,
        status: RunStatus | str | None = None,
        limit: int | None = None,
        skip_corrupt: bool = False,  # noqa: ARG002 - kept for interface parity
    ) -> list[Run]:
        """Runs, newest first. `skip_corrupt` is accepted and meaningless here:
        a row either parses or the schema is broken, so there is nothing to
        tolerate. It stays in the signature so callers need not know which
        backend they hold."""
        clauses: list[str] = []
        params: list[Any] = []
        if actor is not None:
            clauses.append("actor = %s")
            params.append(actor)
        if status is not None:
            clauses.append("status = %s")
            params.append(RunStatus(status).value)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else " WHERE TRUE"
        where += self._scope(params)
        sql = f"SELECT {_COLUMNS} FROM runs{where} ORDER BY run_id DESC"
        if limit is not None:
            sql += " LIMIT %s"
            params.append(limit)
        return [self._row_to_run(r) for r in self._fetchall(sql, params)]

    # --- writes -------------------------------------------------------------

    def create(
        self,
        actor: str,
        input_payload: dict[str, Any] | None = None,
        *,
        owner_id: str | None = None,
        social_account_id: str | None = None,
        concurrency_key: str | None = None,
        ai_prompt: str | None = None,
        report_email: str | None = None,
        timeout_s: int | None = None,
        memory_mb: int | None = None,
        cpus: float | None = None,
    ) -> Run:
        """Register a PENDING run and seed its input document.

        The extra arguments are keyword-only with defaults, so the base
        signature `create(actor, input_payload)` still works unchanged -- which
        is what lets the worker's tests and the single-user path stay as they
        are.
        """
        if not is_valid_actor_name(actor):
            raise RegistryError(f"invalid actor name {actor!r}")
        payload = input_payload or {}
        if not isinstance(payload, dict):
            raise RegistryError(f"run input must be a JSON object, got {type(payload).__name__}")

        run_id = self.new_run_id()
        directory = self._seed_input(run_id, payload)

        run = Run(
            run_id=run_id,
            actor=actor,
            status=RunStatus.PENDING,
            created_at=_iso(_now()),
            run_dir=str(directory),
            input=payload,
            owner_id=owner_id,
            social_account_id=social_account_id,
            concurrency_key=concurrency_key,
            timeout_s=timeout_s,
            memory_mb=memory_mb,
            cpus=cpus,
        )
        try:
            with pool().connection() as conn:
                conn.execute(
                    "INSERT INTO runs (run_id, owner_id, actor, status, social_account_id, "
                    "concurrency_key, created_at, run_dir, input, ai_prompt, report_email, "
                    "timeout_s, memory_mb, cpus) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (run_id, owner_id, actor, RunStatus.PENDING.value, social_account_id,
                     concurrency_key, run.created_at, run.run_dir, json.dumps(payload),
                     ai_prompt, report_email, timeout_s, memory_mb, cpus),
                )
        except BaseException:
            # No row, so no run: the directory must not outlive the refusal.
            self._discard(directory)
            raise
        self._save(run)  # breadcrumb only; nothing reads it back
        return run

    def update(self, run_id: str, /, **changes: Any) -> Run:
        """Apply field changes, enforcing the lifecycle in ONE statement.

        The guard is the WHERE clause, so there is no read-check-write window
        for a concurrent writer to slip through and no lock file to maintain.
        Zero rows updated means the guard refused; the reason is then determined
        by re-reading, which is off the hot path.
        """
        resolved = self.resolve(run_id)

        immutable = set(changes) & _IMMUTABLE_FIELDS
        if immutable:
            raise RegistryError(
                f"cannot change {', '.join(sorted(immutable))} on run '{run_id}'"
            )
        unknown = set(changes) - _UPDATABLE
        if unknown:
            raise RegistryError(f"unknown run field(s): {', '.join(sorted(unknown))}")

        assignments: list[str] = []
        params: list[Any] = []
        for key, value in changes.items():
            if key == "status":
                value = RunStatus(value).value
            elif key == "input":
                value = json.dumps(value or {})
            elif key == "error_info":
                # jsonb, so it must arrive as JSON text rather than as a dict.
                # None stays None: the column is nullable and "no classification"
                # is a real answer, distinct from an empty object.
                value = json.dumps(value) if value is not None else None
            assignments.append(f"{key} = %s")
            params.append(value)

        if "status" in changes:
            target = RunStatus(changes["status"])
            guard, guard_params = "status = ANY(%s)", [_sources_for(target)]
        else:
            # No status change: the run must still be live. Mirrors the base
            # class refusing to modify a terminal run.
            guard = "status = ANY(%s)"
            guard_params = [[RunStatus.PENDING.value, RunStatus.RUNNING.value]]

        params.append(resolved)
        params.extend(guard_params)
        row = self._fetchone(
            f"UPDATE runs SET {', '.join(assignments)} WHERE run_id = %s AND {guard} "
            f"RETURNING {_COLUMNS}",
            params,
            write=True,
        )
        if row is None:
            current = self.get(resolved)  # raises RunNotFound if it truly vanished
            if "status" in changes:
                raise InvalidTransition(
                    f"run '{run_id}' cannot move from {current.status.value} "
                    f"to {RunStatus(changes['status']).value}"
                )
            raise InvalidTransition(
                f"run '{run_id}' is {current.status.value} and can no longer be modified"
            )

        run = self._row_to_run(row)
        self._save(run)
        return run

    def delete(self, run_id: str) -> Run:
        """Delete a finished run this view can see. One guarded statement:
        the WHERE clause both scopes it to the owner and refuses a live run,
        so there is no check-then-delete window. Items and the session lease
        row go with it (ON DELETE CASCADE)."""
        resolved = self.resolve(run_id)
        params: list[Any] = [resolved]
        scope = self._scope(params)
        with pool().connection() as conn:
            row = conn.execute(
                f"DELETE FROM runs WHERE run_id = %s{scope} "
                f"AND status NOT IN ('PENDING', 'RUNNING') RETURNING {_COLUMNS}",
                params,
            ).fetchone()
        if row is None:
            current = self.get(resolved)  # RunNotFound if this view cannot see it
            raise RunIsLive(f"run '{resolved}' is {current.status.value}; cancel it first")
        self._remove_dir(resolved)
        return self._row_to_run(row)

    def cancel(self, run_id: str, reason: str = "cancelled by request") -> Run:
        """CANCELLED, idempotently and atomically against a live completion.

        Same three outcomes as the filesystem registry, but the race is settled
        by the WHERE clause instead of by a lock: exactly one of "the worker
        completed it" and "the API cancelled it" updates a row.
        """
        resolved = self.resolve(run_id)
        finished = _now()

        row = self._fetchone(
            "UPDATE runs SET status = 'CANCELLED', error = %s, finished_at = %s, "
            "duration_seconds = CASE WHEN started_at IS NULL THEN NULL "
            "  ELSE round(EXTRACT(EPOCH FROM (%s::timestamptz - started_at))::numeric, 3) END "
            "WHERE run_id = %s AND status = ANY(%s) "
            f"RETURNING {_COLUMNS}",
            [reason, _iso(finished), _iso(finished), resolved,
             [RunStatus.PENDING.value, RunStatus.RUNNING.value]],
            write=True,
        )
        if row is not None:
            run = self._row_to_run(row)
            self._save(run)
            return run

        current = self.get(resolved)
        if current.status is RunStatus.CANCELLED:
            return current  # idempotent: cancelling twice is not an error
        raise CancelTooLate(resolved, current.status)

    # --- items ------------------------------------------------------------------

    # Items live in run_items, copied there from dataset.jsonl by the worker's
    # tailer while the run goes. The file stays on disk as the Actor wrote it.
    STORES_ITEMS = True

    def reset_items(self, run_id: str) -> None:
        super().reset_items(run_id)
        with pool().connection() as conn:
            conn.execute("DELETE FROM run_items WHERE run_id = %s", (run_id,))

    # builtins.list: a bare `list` here would name the list() method above.
    def ingest_items(self, run_id: str, items: builtins.list[DatasetItem]) -> int:
        """Batch insert, idempotent on (run_id, seq). -> rows actually inserted.

        UNSCOPED ON PURPOSE: only the worker writes items, and it holds the
        unscoped registry. There is no route by which a user supplies them.
        """
        if not items:
            return 0
        rows = [(run_id, item.seq, item.collection, item.key, Jsonb(item.data))
                for item in items]
        with pool().connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO run_items (run_id, seq, collection, item_key, data) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (run_id, seq) DO NOTHING",
                    rows,
                )
                # psycopg 3 sums rowcount across executemany's statements, and a
                # conflicting row counts 0 -- so this is exactly "how many new".
                return max(cur.rowcount, 0)

    def get_items(
        self,
        run_id: str,
        *,
        collection: str | None = None,
        after: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        # Ownership first: get() carries this view's owner predicate, so a run
        # that is not the caller's is RunNotFound here exactly as everywhere else.
        self.get(run_id)
        params: list[Any] = [run_id, after]
        where = "run_id = %s AND seq > %s"
        if collection:
            where += " AND collection = %s"
            params.append(collection)
        params.append(limit + 1)
        rows = self._fetchall(
            f"SELECT seq, collection, item_key, data FROM run_items WHERE {where} "
            "ORDER BY seq LIMIT %s",
            params,
        )
        more = len(rows) > limit
        page = [
            {"seq": row["seq"], "collection": row["collection"],
             "key": row["item_key"], "data": row["data"]}
            for row in rows[:limit]
        ]
        return {"items": page, "next_after": page[-1]["seq"] if more and page else None}

    # --- plumbing -----------------------------------------------------------

    def _fetchone(self, sql: str, params: Any = None, *, write: bool = False):
        with pool().connection() as conn:
            return conn.execute(sql, params).fetchone()

    def _fetchall(self, sql: str, params: Any = None):
        with pool().connection() as conn:
            return conn.execute(sql, params).fetchall()


def open_registry(root: Path | str | None = None) -> RunRegistry:
    """The registry this deployment is configured for.

    Mirrors jobqueue.base.open_queue: callers ask for "the registry" and never
    name an implementation. Falls back to the filesystem when no database is
    configured, so the single-user CLI path and every existing test keep working
    with nothing set.
    """
    import os

    if (os.environ.get("ACTOR_PLATFORM_DATABASE_URL") or "").strip():
        return PgRunRegistry(root)
    return RunRegistry(root)
