"""The Run Registry: the one component that owns storage/runs/<run_id>/.

Every run leaves four artifacts on disk:

    storage/runs/<run_id>/
    |-- input.json     what the run was asked to do
    |-- output.json    what the Actor produced (successful runs only)
    |-- log.txt        everything the Actor printed
    `-- run.json       the run record itself -- the Run model, serialised

Before this existed the executor opened all four by hand, which meant nothing
else could inspect a past run without re-implementing that. The registry turns
them into an API, and is deliberately independent of Docker: it has no idea how
a run is executed, only how one is recorded.

    Worker
      |-- Actor Executor    docker build / docker run / log streaming
      `-- Run Registry      this module

A run IS its run.json. A directory without one was never registered, so it is
not a run: list() ignores it and get() reports it missing. Corruption means the
file is there but unreadable, which is always raised rather than skipped -- a
registry that quietly drops records it cannot parse is worse than one that
admits it is broken.
"""

from __future__ import annotations

import builtins
import json
import os
import re
import sys
import uuid
from contextlib import contextmanager

try:
    import fcntl  # POSIX advisory locks; the platform is Linux (see README)
except ImportError:  # pragma: no cover - non-POSIX degrades to no locking
    fcntl = None  # type: ignore[assignment]  # checked as `fcntl is None`
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from registry.dataset import DatasetItem, read_from  # noqa: E402
from shared import safe_fs  # noqa: E402
from shared.models import (  # noqa: E402
    DATASET_FILE,
    INPUT_FILE,
    LOG_FILE,
    OUTPUT_FILE,
    RUN_FILE,
    RUN_ID_RULE,
    Run,
    RunStatus,
    is_valid_actor_name,
    is_valid_run_id,
)

DEFAULT_ROOT = REPO_ROOT / "storage" / "runs"

# The sibling of jobqueue.base.QUEUE_URL_ENV, and it exists for the same reason:
# a CHILD PROCESS has no other way to be told where runs live. consumer.py has
# taken --registry-root since the beginning, but `uvicorn api.main:app` takes no
# arguments at all, so an API server spawned by a test or a supervisor could
# only ever use DEFAULT_ROOT -- which is how a suite ended up sharing a run
# store, and a queue, with a live deployment.
RUNS_ROOT_ENV = "ACTOR_RUNS_ROOT"

# A prefix is any leading fragment of a run id. Constraining the charset here
# is what keeps "../.." from ever reaching the directory scan in resolve().
_PREFIX_PATTERN = re.compile(r"[0-9a-f-]{1,28}")


# --- errors -----------------------------------------------------------------


class RegistryError(RuntimeError):
    """Base class: anything the registry refuses to do."""


class RunNotFound(RegistryError):
    """No run with that id has been registered."""


class InvalidRunId(RegistryError):
    """The id is not a well-formed run id, so it cannot name a run."""


class AmbiguousRunId(RegistryError):
    """A run id prefix matched more than one run."""


class UnstorableInput(RegistryError):
    """A run input that cannot be written as UTF-8 JSON (NUL is fine in JSON
    text but not in Postgres; an unpaired surrogate is not UTF-8 at all)."""


class CorruptRunRecord(RegistryError):
    """run.json exists but is not a usable run record."""


class InvalidTransition(RegistryError):
    """The requested status change is not legal from the run's current state."""


class RunIsLive(RegistryError):
    """A PENDING or RUNNING run cannot be deleted; cancel it first."""


class CancelTooLate(RegistryError):
    """Cancellation lost the race: the run had already reached a DIFFERENT
    terminal state. Distinct from InvalidTransition so the API can render it as
    a clear 409 ("already SUCCEEDED") rather than a generic bad-transition."""

    def __init__(self, run_id: str, status: RunStatus) -> None:
        self.status = status
        super().__init__(
            f"run '{run_id}' cannot be cancelled: it already finished as {status.value}"
        )


class ArtifactError(RegistryError):
    """Base class for problems with input.json / output.json / log.txt."""


class ArtifactNotFound(ArtifactError):
    """The artifact was never written."""


class ArtifactCorrupt(ArtifactError):
    """The artifact exists but could not be read as what it claims to be."""


# --- size limits on what an Actor wrote ---------------------------------------
# input.json, output.json and log.txt sit in the Actor's mount, so their size is
# the Actor's to choose. Read whole and unbounded, one huge file was held in
# memory by the worker (classifying the run) and by the API (serving it).
# Generous defaults -- the largest real output measured is a few MB -- and
# configurable for a deployment that genuinely produces more.
MAX_DOCUMENT_BYTES_ENV = "ACTOR_PLATFORM_MAX_OUTPUT_BYTES"
DEFAULT_MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
MAX_LOG_BYTES_ENV = "ACTOR_PLATFORM_MAX_LOG_BYTES"
DEFAULT_MAX_LOG_BYTES = 32 * 1024 * 1024


def size_limit(env: str, default: int) -> int:
    """A positive byte limit from the environment, or the default."""
    try:
        value = int((os.environ.get(env) or "").strip() or default)
    except ValueError:
        return default
    return value if value > 0 else default


def log_truncation_note(limit: int) -> str:
    return f"[log truncated: it is larger than {limit} bytes]"


# --- lifecycle rules --------------------------------------------------------
# A run moves forward only. Terminal states accept nothing, so a finished run
# can never be silently rewritten by a late or duplicated update.

_ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset(
        {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.PLATFORM_ERROR,
         RunStatus.CANCELLED}
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.PLATFORM_ERROR,
            RunStatus.CANCELLED,
            # Back to PENDING is the RECOVERY edge, added for at-least-once
            # delivery: a Worker that died mid-run leaves the record claiming
            # RUNNING for ever, and the redelivered job has to be able to reset
            # it. Go through requeue(), which records why.
            RunStatus.PENDING,
        }
    ),
}

# Identity and location are set once, at creation, and are never updatable.
_IMMUTABLE_FIELDS = frozenset({"run_id", "created_at", "run_dir"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    """Timestamps carry milliseconds so they can corroborate the run order.

    Run ids are microsecond-resolution, so two runs a few hundred microseconds
    apart list in the correct order -- but with second-resolution timestamps
    their recorded created_at was identical, leaving nothing in the record to
    show why one preceded the other. datetime.fromisoformat reads both forms,
    so records written before this still parse.
    """
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RunRegistry:
    """Create, update, retrieve and list runs backed by storage/runs/."""

    def __init__(self, root: Path | str | None = None) -> None:
        # Explicit argument wins, then $ACTOR_RUNS_ROOT, then the default --
        # the same precedence open_queue() applies to its url.
        if root is None:
            root = os.environ.get(RUNS_ROOT_ENV) or None
        self.root = Path(root) if root is not None else DEFAULT_ROOT

    # --- identity ----------------------------------------------------------

    @staticmethod
    def new_run_id() -> str:
        """Mint a sortable run id: <date>-<time>-<random>.

        Lexicographic order is chronological order, which is what makes list()
        orderable without reading every record's timestamps.

        The timestamp is microsecond-resolution because second-resolution ids
        left same-second runs ordered by their random suffix -- i.e. arbitrarily
        -- which a queue creating many runs per second would hit constantly.
        The suffix remains only as a tie-breaker between processes.
        """
        return f"{_now():%Y%m%d-%H%M%S%f}-{uuid.uuid4().hex[:6]}"

    @contextmanager
    def _locked(self, run_id: str):
        """Serialise the read-modify-write of one run's record.

        update() reads run.json, checks the transition, then writes -- and the
        worker (completing a run) and the API (cancelling it) can do that to the
        SAME run at once. Without a lock both read RUNNING, both pass the guard,
        and one write is silently lost: exactly the completion-vs-cancel race
        cancellation has to resolve deterministically. A POSIX advisory lock on
        a per-run .lock file makes the whole RMW atomic on this host. Locking is
        per-run, so unrelated runs never contend; the file persists (unlinking
        it would reopen the race). Single-host by construction, which is the
        platform's scope. If fcntl is unavailable the lock is a no-op and the
        old best-effort behaviour stands -- documented, not hidden.
        """
        if fcntl is None:
            yield
            return
        directory = self.root / run_id
        directory.mkdir(parents=True, exist_ok=True)
        # Not open(..., "w"): the run directory is the Actor's mount, so ".lock"
        # may be a link the container planted, and "w" would truncate its
        # target on the host. See shared/safe_fs.py.
        handle = safe_fs.open_lock(directory / ".lock")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def run_dir(self, run_id: str) -> Path:
        """Where a run's artifacts live. Never creates anything."""
        if not is_valid_run_id(run_id):
            raise InvalidRunId(f"invalid run id {run_id!r}: {RUN_ID_RULE}")
        return self.root / run_id

    def resolve(self, run_id_or_prefix: str) -> str:
        """Expand a unique run id PREFIX into the full run id.

        Run ids are 22 characters, which is unusable to type. A full id takes
        the fast path and never scans; a prefix scans and insists on exactly
        one match, because silently picking one of several would attach the
        caller's next command to an arbitrary run.
        """
        if is_valid_run_id(run_id_or_prefix) and self.exists(run_id_or_prefix):
            return run_id_or_prefix

        if not run_id_or_prefix or not _PREFIX_PATTERN.fullmatch(run_id_or_prefix):
            raise InvalidRunId(f"invalid run id {run_id_or_prefix!r}: {RUN_ID_RULE}")

        matches = [
            entry.name
            for entry in sorted(self.root.iterdir(), reverse=True)
            if entry.is_dir()
            and entry.name.startswith(run_id_or_prefix)
            and (entry / RUN_FILE).is_file()
        ] if self.root.is_dir() else []

        if not matches:
            raise RunNotFound(f"no run matching '{run_id_or_prefix}' in {self.root}")
        if len(matches) > 1:
            shown = ", ".join(matches[:5]) + (" ..." if len(matches) > 5 else "")
            raise AmbiguousRunId(
                f"'{run_id_or_prefix}' matches {len(matches)} runs: {shown}"
            )
        return matches[0]

    def exists(self, run_id: str) -> bool:
        try:
            return (self.run_dir(run_id) / RUN_FILE).is_file()
        except InvalidRunId:
            return False

    # --- persistence -------------------------------------------------------

    def _read_record(self, path: Path, run_id: str) -> Run:
        """Parse one run.json. Every failure mode is named, never guessed at."""
        record_path = path / RUN_FILE
        try:
            raw = safe_fs.read_text(record_path)
        except OSError as exc:
            raise CorruptRunRecord(f"run '{run_id}': cannot read {RUN_FILE}: {exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CorruptRunRecord(
                f"run '{run_id}': {RUN_FILE} is not valid JSON: {exc}"
            ) from exc

        try:
            run = Run.from_dict(data)
        except ValueError as exc:
            raise CorruptRunRecord(f"run '{run_id}': {exc}") from exc

        # run.json lives in the directory the Actor's container mounts
        # read-write, so its contents are the CONTAINER's word, not the
        # registry's. Identity and location therefore come from where the
        # record was found, never from what it says: a record naming another
        # run is refused, and its run_dir is replaced outright. Trusting it let
        # a container write `"run_dir": "/"`, have the reconciler requeue the
        # run, and get the host's root mounted read-write on the next attempt.
        if run.run_id != run_id:
            raise CorruptRunRecord(
                f"run '{run_id}': {RUN_FILE} names a different run - refusing it"
            )
        run.run_dir = str(self.root / run_id)
        return run

    def _save(self, run: Run) -> Run:
        """Write run.json atomically.

        Temp file plus os.replace, because a registry whose own interruption
        can corrupt the record it exists to protect is not a registry.
        """
        directory = self.root / run.run_id
        directory.mkdir(parents=True, exist_ok=True)

        # A fresh temp file, never a fixed name: a fixed ".run.json.tmp" in a
        # directory the Actor can write to is a name it can plant a link at.
        safe_fs.write_text_atomic(directory / RUN_FILE, run.to_json() + "\n")
        return run

    # --- create / update ---------------------------------------------------

    def create(
        self,
        actor: str,
        input_payload: dict[str, Any] | None = None,
        *,
        timeout_s: int | None = None,
        memory_mb: int | None = None,
        cpus: float | None = None,
    ) -> Run:
        """Register a new PENDING run and seed its input document.

        The limit arguments are what the caller REQUESTED; the Worker clamps
        them against the Actor's maximum and records what it enforced.
        """
        if not is_valid_actor_name(actor):
            raise RegistryError(f"invalid actor name {actor!r}")

        payload = input_payload or {}
        if not isinstance(payload, dict):
            raise RegistryError(f"run input must be a JSON object, got {type(payload).__name__}")

        run_id = self.new_run_id()
        directory = self._seed_input(run_id, payload)
        try:
            return self._save(
                Run(
                    run_id=run_id,
                    actor=actor,
                    status=RunStatus.PENDING,
                    created_at=_iso(_now()),
                    run_dir=str(directory),
                    input=payload,
                    timeout_s=timeout_s,
                    memory_mb=memory_mb,
                    cpus=cpus,
                )
            )
        except BaseException:
            self._discard(directory)
            raise

    def _seed_input(self, run_id: str, payload: dict[str, Any]) -> Path:
        """Create the run's directory with its input.json, or nothing at all.

        The input is ENCODED before the directory exists, so an input that
        cannot be stored is refused without leaving a half-made run behind --
        which is what used to happen: the directory was created, the UTF-8 write
        raised, and the request ended in a 500 with an orphan in storage/runs.
        """
        try:
            data = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        except (ValueError, TypeError, RecursionError) as exc:
            raise UnstorableInput(
                f"run input cannot be stored ({type(exc).__name__})") from None
        directory = self.root / run_id
        directory.mkdir(parents=True, exist_ok=True)
        try:
            (directory / INPUT_FILE).write_bytes(data)
        except BaseException:
            self._discard(directory)
            raise
        return directory

    @staticmethod
    def _discard(directory: Path) -> None:
        """Remove a run directory this process just created. Never raises."""
        import shutil  # noqa: PLC0415

        shutil.rmtree(directory, ignore_errors=True)

    def update(self, run_id: str, /, **changes: Any) -> Run:
        """Apply field changes to a run, enforcing the lifecycle rules.

        run_id is positional-only so that every Run field name stays usable as
        a keyword. Without the "/", update(rid, run_id=...) would collide with
        this parameter and raise TypeError instead of the intended refusal.
        """
        # The whole read-modify-write is under the run lock, so a concurrent
        # writer (the classic case: the worker completing while the API cancels)
        # cannot read the same pre-state and clobber this write. Everything that
        # mutates a run funnels through here, so one lock covers them all.
        with self._locked(self.resolve(run_id)):
            run = self.get(run_id)

            unknown = set(changes) & _IMMUTABLE_FIELDS
            if unknown:
                raise RegistryError(f"cannot change {', '.join(sorted(unknown))} on run '{run_id}'")

            known = set(run.to_dict())
            invalid = set(changes) - known
            if invalid:
                raise RegistryError(f"unknown run field(s): {', '.join(sorted(invalid))}")

            if "status" in changes:
                target = RunStatus(changes["status"])
                allowed = _ALLOWED_TRANSITIONS.get(run.status, frozenset())
                if target not in allowed:
                    raise InvalidTransition(
                        f"run '{run_id}' cannot move from {run.status.value} to {target.value}"
                    )
                changes["status"] = target
            elif run.is_terminal:
                raise InvalidTransition(
                    f"run '{run_id}' is {run.status.value} and can no longer be modified"
                )

            for key, value in changes.items():
                setattr(run, key, value)

            return self._save(run)

    def mark_running(self, run_id: str) -> Run:
        """PENDING -> RUNNING, stamping the start time and counting the attempt."""
        run = self.get(run_id)
        return self.update(
            run_id,
            status=RunStatus.RUNNING,
            started_at=_iso(_now()),
            attempts=run.attempts + 1,
        )

    def requeue(self, run_id: str, reason: str) -> Run:
        """RUNNING -> PENDING: an attempt was abandoned, so let it be retried.

        Only for recovery. A run reaches this state when its Worker died
        without recording an outcome, so the record still claims RUNNING while
        nothing is running. started_at is cleared because the next attempt will
        stamp its own; `attempts` preserves the history that there WAS one.
        """
        run = self.get(run_id)
        if run.status is not RunStatus.RUNNING:
            raise InvalidTransition(
                f"run '{run_id}' is {run.status.value}, not RUNNING - nothing to requeue"
            )
        return self.update(run_id, status=RunStatus.PENDING, started_at=None, error=reason)

    def complete(
        self,
        run_id: str,
        *,
        status: RunStatus,
        exit_code: int | None = None,
        error: str | None = None,
        peak_memory_mb: int | None = None,
        error_info: dict[str, Any] | None = None,
        partial: bool | None = None,
    ) -> Run:
        """Move a run to a terminal status, stamping the finish time.

        Takes no output: the Actor already wrote output.json into the run
        directory, and the record points at that rather than copying it.

        `error_info` is the structured form of the same failure `error`
        describes in a sentence, and `partial` says whether the run left usable
        data behind. Both are optional and both default to leaving the record
        alone, so a caller that predates them -- the CLI, a test, any older
        worker -- completes a run exactly as it always did.
        """
        if status not in _ALLOWED_TRANSITIONS.get(RunStatus.RUNNING, frozenset()):
            raise InvalidTransition(f"{status.value} is not a terminal status")

        finished = _now()
        run = self.get(run_id)

        duration: float | None = None
        if run.started_at:
            try:
                started = datetime.fromisoformat(run.started_at.replace("Z", "+00:00"))
                duration = round((finished - started).total_seconds(), 3)
            except ValueError:
                duration = None

        changes: dict[str, Any] = {
            "status": status,
            "exit_code": exit_code,
            "error": error,
            "finished_at": _iso(finished),
            "duration_seconds": duration,
        }
        # Only sent when the caller had something to say. update() rejects an
        # unknown field, so passing None here would still write a null over
        # whatever a previous attempt had recorded.
        if error_info is not None:
            changes["error_info"] = error_info
        if partial is not None:
            changes["partial"] = partial
        if peak_memory_mb is not None:
            changes["peak_memory_mb"] = peak_memory_mb

        return self.update(run_id, **changes)

    # --- retrieve ----------------------------------------------------------

    def get(self, run_id: str) -> Run:
        """Retrieve one run's record. Never creates anything on disk."""
        directory = self.run_dir(run_id)
        if not (directory / RUN_FILE).is_file():
            raise RunNotFound(f"no run with id '{run_id}' in {self.root}")
        return self._read_record(directory, run_id)

    def list(
        self,
        *,
        actor: str | None = None,
        status: RunStatus | str | None = None,
        limit: int | None = None,
        skip_corrupt: bool = False,
    ) -> list[Run]:
        """Registered runs, NEWEST FIRST.

        Ordering is run_id descending, which is a total, deterministic and
        stable order: run ids are <date>-<time>-<random>, so string order is
        chronological order and the random suffix breaks same-second ties.

        A corrupt record raises by default rather than vanishing from the
        listing; pass skip_corrupt=True to tolerate it deliberately.
        """
        if not self.root.is_dir():
            return []

        wanted_status = RunStatus(status) if status is not None else None
        found: list[Run] = []

        for entry in sorted(self.root.iterdir(), key=lambda p: p.name, reverse=True):
            if not entry.is_dir() or not (entry / RUN_FILE).is_file():
                continue

            try:
                run = self._read_record(entry, entry.name)
            except CorruptRunRecord:
                if skip_corrupt:
                    continue
                raise

            if actor is not None and run.actor != actor:
                continue
            if wanted_status is not None and run.status is not wanted_status:
                continue

            found.append(run)
            if limit is not None and len(found) >= limit:
                break

        return found

    def cancel(self, run_id: str, reason: str = "cancelled by request") -> Run:
        """Move a run to CANCELLED. Idempotent, and safe against a live finish.

        The whole check-and-set is under the run lock, so it is atomic against a
        worker completing the same run:

          * already CANCELLED   -> return it unchanged (idempotent: cancelling
                                   twice is not an error)
          * any OTHER terminal  -> CancelTooLate; completion won the race, and
                                   its result must NOT be overwritten
          * PENDING / RUNNING   -> set CANCELLED with the reason

        Cancelling does NOT itself touch Docker: for a RUNNING run the worker
        that owns the container watches for this state and stops it. The
        registry is the source of truth AND the channel for the intent.
        """
        resolved = self.resolve(run_id)
        with self._locked(resolved):
            run = self.get(resolved)
            if run.status is RunStatus.CANCELLED:
                return run
            if run.is_terminal:
                raise CancelTooLate(resolved, run.status)

            finished = _now()
            duration = None
            if run.started_at:
                try:
                    started = datetime.fromisoformat(run.started_at.replace("Z", "+00:00"))
                    duration = round((finished - started).total_seconds(), 3)
                except ValueError:
                    duration = None

            run.status = RunStatus.CANCELLED
            run.error = reason
            run.finished_at = _iso(finished)
            run.duration_seconds = duration
            return self._save(run)

    # --- deletion (security review M-04) -----------------------------------

    def delete(self, run_id: str) -> Run:
        """Delete a FINISHED run: its record, its items and its directory.

        A live run is refused (RunIsLive) rather than deleted from under its
        container: cancel it first. Returns the record as it was. The directory
        is removed without following links -- it is the container's mount, and
        anything it planted there is removed as itself, never as its target.
        """
        resolved = self.resolve(run_id)
        with self._locked(resolved):
            run = self.get(resolved)
            if not run.is_terminal:
                raise RunIsLive(f"run '{resolved}' is {run.status.value}; cancel it first")
            self._delete_record(resolved)
        self._remove_dir(resolved)
        return run

    def _delete_record(self, run_id: str) -> None:
        """The filesystem record lives in the directory, which goes next."""

    def _remove_dir(self, run_id: str) -> None:
        import shutil  # noqa: PLC0415

        directory = self.run_dir(run_id)
        if directory.is_symlink():
            directory.unlink()
        elif directory.exists():
            shutil.rmtree(directory, ignore_errors=True)

        # --- artifacts ---------------------------------------------------------

    def _artifact_path(self, run_id: str, filename: str) -> Path:
        # "Does this run exist?" goes through exists() rather than testing for
        # run.json inline, so a subclass that keeps metadata elsewhere inherits
        # the artifact half unchanged. run_dir() is still called first, so an
        # invalid id remains InvalidRunId rather than becoming RunNotFound.
        directory = self.run_dir(run_id)
        if not self.exists(run_id):
            raise RunNotFound(f"no run with id '{run_id}' in {self.root}")
        return directory / filename

    def _read_json_artifact(self, run_id: str, filename: str, missing: str) -> Any:
        path = self._artifact_path(run_id, filename)
        try:
            # Never follows a link and never opens a FIFO or device: the run
            # directory is the Actor's mount, and a link here would otherwise
            # serve a HOST file over /input or /output -- or, via the
            # reconciler's get_input, hand one to a fresh container as input.
            raw = safe_fs.read_text(
                path, max_bytes=size_limit(MAX_DOCUMENT_BYTES_ENV, DEFAULT_MAX_DOCUMENT_BYTES)
            ).strip()
        except FileNotFoundError:
            raise ArtifactNotFound(missing) from None
        except safe_fs.NotRegularFile as exc:
            raise ArtifactCorrupt(f"{filename} is not a regular file: {exc}") from exc
        except safe_fs.FileTooLarge as exc:
            raise ArtifactCorrupt(str(exc)) from exc
        except UnicodeDecodeError:
            raise ArtifactCorrupt(f"{filename} is not UTF-8 text") from None
        if not raw:
            raise ArtifactCorrupt(f"actor wrote an empty {filename}")

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ArtifactCorrupt(f"{filename} is not valid JSON: {exc}") from exc
        except RecursionError:
            raise ArtifactCorrupt(f"{filename} is nested too deeply to read") from None

    def get_input(self, run_id: str) -> Any:
        """The input document the run was started with."""
        return self._read_json_artifact(run_id, INPUT_FILE, f"run has no {INPUT_FILE}")

    def get_output(self, run_id: str) -> Any:
        """What the Actor produced.

        Strict on purpose: a failed run has no output.json, and raising keeps
        that distinguishable from an Actor that legitimately produced null.
        This is the ONLY way to read a run's output -- the record does not
        carry one.
        """
        return self._read_json_artifact(run_id, OUTPUT_FILE, f"actor produced no {OUTPUT_FILE}")

    def get_logs(self, run_id: str) -> str:
        """Everything the Actor printed, as raw text."""
        path = self._artifact_path(run_id, LOG_FILE)
        limit = size_limit(MAX_LOG_BYTES_ENV, DEFAULT_MAX_LOG_BYTES)
        try:
            # The head, not the whole file, and never an error for size: a log
            # is the one artifact whose first megabytes are still useful. Bytes
            # that are not UTF-8 (an Actor's binary output) are replaced rather
            # than failing the read.
            data, more = safe_fs.read_head(path, limit)
            text = data.decode("utf-8", errors="replace")
            return text + ("\n" + log_truncation_note(limit) + "\n" if more else "")
        except FileNotFoundError:
            raise ArtifactNotFound(f"run has no {LOG_FILE}") from None
        except safe_fs.NotRegularFile as exc:
            raise ArtifactCorrupt(f"{LOG_FILE} is not a regular file: {exc}") from exc

    # Where an Actor puts files that are RESULTS but are not the result
    # document -- a PDF, an HTML dashboard, a rendered report. Inside the run
    # directory (so it is bind-mounted and cleaned up with the run) and in a
    # named subdirectory, so serving it can be whitelisted to one place rather
    # than to "anything under the run".
    ARTIFACTS_SUBDIR = "artifacts"

    # `list` here would name the RunRegistry.list METHOD defined above, not the
    # built-in, so annotations after it spell the type as builtins.list.
    def list_artifacts(self, run_id: str) -> builtins.list[dict[str, Any]]:
        """The extra files this run produced. [] when it produced none."""
        directory = self._artifact_path(run_id, self.ARTIFACTS_SUBDIR)
        # The DIRECTORY must not be a link either. Checking only its entries
        # left a hole: with "artifacts -> /etc", no entry is a link and every
        # one resolves inside the (linked) directory, so /etc/passwd listed.
        if directory.is_symlink() or not directory.is_dir():
            return []
        found = []
        for entry in sorted(directory.iterdir()):
            # A LINK IS NOT AN ARTIFACT. This directory is bind-mounted into
            # the Actor's container, and is_file() follows links -- so without
            # this, a link written there pointing at /etc/shadow, or at the
            # platform's own secret key, would be listed and then served from
            # the API host to whoever owns the run.
            if entry.is_symlink() or entry.name.startswith("."):
                continue
            if entry.is_file():
                found.append({"name": entry.name, "bytes": entry.stat().st_size})
        return found

    def artifact_path(self, run_id: str, name: str) -> Path:
        """One artifact, by name. Raises rather than escaping the directory.

        THE NAME IS A NAME, not a path. It is compared against what the
        directory actually contains rather than being joined and then checked,
        so "../../etc/passwd", an absolute path and a symlink pointing outside
        are all simply not in the listing. Sanitising a caller-supplied path is
        a game this does not have to play.
        """
        for entry in self.list_artifacts(run_id):
            if entry["name"] == name:
                directory = self._artifact_path(run_id, self.ARTIFACTS_SUBDIR)
                path = directory / name
                # Belt and braces against a link appearing between the listing
                # and here. Compared against the RUN DIRECTORY's real location,
                # never the artifacts directory's own resolve(): if that is a
                # link, both sides of the comparison would follow it and agree.
                expected = self.run_dir(run_id).resolve() / self.ARTIFACTS_SUBDIR
                if (directory.is_symlink() or path.is_symlink()
                        or path.resolve().parent != expected):
                    break
                return path
        raise ArtifactNotFound(f"run has no artifact named {name!r}")

    # --- items (the optional dataset part of the contract) -------------------

    # Whether this backend keeps items somewhere OTHER than dataset.jsonl, and so
    # needs the worker to copy them across while a run is going. The filesystem
    # registry does not: the file the Actor appends to already is its store, so
    # there is nothing to ingest and the worker starts no tailer for it.
    STORES_ITEMS = False

    def dataset_path(self, run_id: str) -> Path:
        """Where this run's Actor appends items. May not exist."""
        return self._artifact_path(run_id, DATASET_FILE)

    def reset_items(self, run_id: str) -> None:
        """Discard a PREVIOUS attempt's items before a new attempt starts.

        A requeued run re-executes in the same run directory, and the Actor
        starts again from nothing -- so without this the second attempt would
        append after the first one's lines and every item it re-produces would
        be stored twice. A run's items are its latest attempt's.
        """
        path = self.dataset_path(run_id)
        # is_symlink() too: is_file() is False for a DANGLING link, which would
        # otherwise survive into the next attempt. unlink() removes the link
        # itself, never its target.
        if path.is_symlink() or path.exists():
            path.unlink()

    def ingest_items(self, run_id: str, items: builtins.list[DatasetItem]) -> int:
        """Store items read from the dataset file. -> how many were new.

        A no-op here (see STORES_ITEMS). A backend that stores items elsewhere
        must make this IDEMPOTENT on (run_id, seq): the worker may hand the same
        item over twice after a restart, and that must not duplicate it.
        """
        return 0

    def get_items(
        self,
        run_id: str,
        *,
        collection: str | None = None,
        after: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """A page of this run's items, in the order the Actor produced them.

        -> {"items": [...], "next_after": <seq to pass as `after`> | None}.
        Readable while the run is still going. Here it is read straight from the
        file, so it is always exactly as current as the Actor's last write.
        """
        path = self.dataset_path(run_id)
        page: list[dict[str, Any]] = []
        offset, next_seq = 0, 1
        more = False
        while True:
            chunk = read_from(path, offset, next_seq, final=True, max_items=1000)
            for item in chunk.items:
                if item.seq <= after or (collection and item.collection != collection):
                    continue
                if len(page) == limit:
                    more = True
                    break
                page.append(item.to_dict())
            if more or chunk.offset == offset:
                break
            offset, next_seq = chunk.offset, chunk.next_seq
        return {"items": page, "next_after": page[-1]["seq"] if more and page else None}

    def write_logs(self, run_id: str, lines: builtins.list[str]) -> int:
        """Persist a run's captured output and record how many lines it was.

        The log FILE is always written; the log_lines COUNT is a field on the
        record. If the run went terminal while the container was being drained
        -- the container was cancelled mid-run, say -- updating the count would
        trip the terminal-write guard. That must not raise: the outcome
        (CANCELLED) is already correct and the logs are already on disk, so a
        raise here would only mask the real result behind a spurious
        "could not be executed". The count is skipped in that case.
        """
        path = self._artifact_path(run_id, LOG_FILE)
        # Replaced, never opened: the container has exited, but whatever it
        # left at log.txt may be a link -- and writing THROUGH it would put the
        # Actor's own output into any host file the worker can write.
        safe_fs.write_text_atomic(path, "\n".join(lines) + ("\n" if lines else ""))
        try:
            self.update(run_id, log_lines=len(lines))
        except InvalidTransition:
            pass  # the run finished mid-write; the log file stands on its own
        return len(lines)
