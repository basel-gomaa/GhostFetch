#!/usr/bin/env python3
"""Run Registry tests.

Offline: no Docker, no network, no Actor images. Every test runs against a
throwaway registry in a temp directory, so the real storage/runs/ is never
touched.

    python3 test_registry.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import traceback
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from registry.run_registry import (  # noqa: E402
    AmbiguousRunId,
    ArtifactCorrupt,
    ArtifactNotFound,
    CorruptRunRecord,
    InvalidRunId,
    InvalidTransition,
    RegistryError,
    RunNotFound,
    RunRegistry,
)
from shared.models import (  # noqa: E402
    is_valid_run_id,
    EXIT_ACTOR_FAILED,
    EXIT_PLATFORM_ERROR,
    EXIT_SUCCEEDED,
    RunStatus,
    worker_exit_code,
)

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


@contextmanager
def temp_registry():
    with tempfile.TemporaryDirectory() as tmp:
        yield RunRegistry(Path(tmp) / "runs")


def raises(exc_type, fn, *args, **kwargs):
    """Assert a call raises, and hand back the exception for message checks."""
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        return exc
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"expected {exc_type.__name__}, got {type(exc).__name__}: {exc}")
    raise AssertionError(f"expected {exc_type.__name__}, nothing was raised")


def write_record(registry: RunRegistry, run_id: str, data: dict | str) -> Path:
    """Plant a raw run.json, for legacy and corruption cases."""
    directory = registry.root / run_id
    directory.mkdir(parents=True, exist_ok=True)
    body = data if isinstance(data, str) else json.dumps(data, indent=2)
    (directory / "run.json").write_text(body, encoding="utf-8")
    return directory


# --- 1. create / register ---------------------------------------------------


@test
def test_create_registers_a_pending_run():
    with temp_registry() as reg:
        run = reg.create("hello_world", {"name": "Basel"})

        assert run.status is RunStatus.PENDING, run.status
        assert run.actor == "hello_world"
        assert run.created_at, "created_at must be stamped at creation"
        assert run.input == {"name": "Basel"}
        assert reg.exists(run.run_id)

        # Unknown until the run actually runs -- not zero-filled.
        assert run.started_at is None
        assert run.finished_at is None
        assert run.exit_code is None
        assert run.duration_seconds is None

        directory = Path(run.run_dir)
        assert (directory / "input.json").is_file()
        assert (directory / "run.json").is_file()
        # Not yet produced, so not yet present.
        assert not (directory / "output.json").exists()
        assert not (directory / "log.txt").exists()
        assert json.loads((directory / "input.json").read_text()) == {"name": "Basel"}
        assert not list(directory.glob(".*tmp")), "atomic write left a temp file behind"


@test
def test_create_rejects_bad_input():
    with temp_registry() as reg:
        raises(RegistryError, reg.create, "Bad Name", {})
        raises(RegistryError, reg.create, "hello_world", ["not", "an", "object"])


# --- 2. update / transitions ------------------------------------------------


@test
def test_pending_running_succeeded():
    with temp_registry() as reg:
        run = reg.create("hello_world", {"name": "Basel"})
        assert run.status is RunStatus.PENDING

        run = reg.mark_running(run.run_id)
        assert run.status is RunStatus.RUNNING
        assert run.started_at, "mark_running must stamp started_at"

        run = reg.complete(run.run_id, status=RunStatus.SUCCEEDED, exit_code=0)
        assert run.status is RunStatus.SUCCEEDED
        assert run.exit_code == 0
        assert run.error is None
        assert run.finished_at, "complete must stamp finished_at"
        assert run.duration_seconds is not None and run.duration_seconds >= 0
        assert run.is_terminal


@test
def test_pending_running_failed():
    with temp_registry() as reg:
        run = reg.create("item_processor", {"count": 5, "fail_at": 3})
        reg.mark_running(run.run_id)
        run = reg.complete(
            run.run_id,
            status=RunStatus.FAILED,
            exit_code=1,
            error="actor exited with code 1",
        )

        assert run.status is RunStatus.FAILED
        assert run.exit_code == 1
        assert run.error == "actor exited with code 1"
        assert run.is_terminal


@test
def test_platform_error_is_its_own_terminal_state():
    with temp_registry() as reg:
        run = reg.create("hello_world", {})
        reg.mark_running(run.run_id)
        run = reg.complete(run.run_id, status=RunStatus.PLATFORM_ERROR, error="docker vanished")

        assert run.status is RunStatus.PLATFORM_ERROR
        # The whole point: not collapsed into FAILED.
        assert run.status is not RunStatus.FAILED
        assert worker_exit_code(run.status) == EXIT_PLATFORM_ERROR


@test
def test_illegal_transitions_are_refused():
    with temp_registry() as reg:
        run = reg.create("hello_world", {})

        # PENDING cannot jump straight to SUCCEEDED: it never ran.
        raises(InvalidTransition, reg.update, run.run_id, status=RunStatus.SUCCEEDED)

        reg.mark_running(run.run_id)
        # RUNNING -> RUNNING is meaningless and stays refused...
        raises(InvalidTransition, reg.update, run.run_id, status=RunStatus.RUNNING)
        # ...but RUNNING -> PENDING is the RECOVERY edge that at-least-once
        # delivery needs: a Worker died, so the run must become runnable again.
        assert reg.requeue(run.run_id, "worker died").status is RunStatus.PENDING

        reg.mark_running(run.run_id)
        reg.complete(run.run_id, status=RunStatus.SUCCEEDED, exit_code=0)

        # A finished run is finished: no re-running, no quiet field edits.
        raises(InvalidTransition, reg.update, run.run_id, status=RunStatus.RUNNING)
        raises(InvalidTransition, reg.update, run.run_id, error="rewriting history")


@test
def test_update_guards_fields():
    with temp_registry() as reg:
        run = reg.create("hello_world", {})
        raises(RegistryError, reg.update, run.run_id, run_id="20260101-000000-aaaaaa")
        raises(RegistryError, reg.update, run.run_id, created_at="whenever")
        raises(RegistryError, reg.update, run.run_id, nonsense=1)


# --- 3. retrieve ------------------------------------------------------------


@test
def test_get_round_trips_through_disk():
    with temp_registry() as reg:
        run = reg.create("item_processor", {"count": 2})
        reg.mark_running(run.run_id)
        (Path(run.run_dir) / "output.json").write_text('[{"index": 1}]')
        written = reg.complete(run.run_id, status=RunStatus.SUCCEEDED, exit_code=0)

        # A brand-new registry object: proves the record, not memory, is the truth.
        fetched = RunRegistry(reg.root).get(run.run_id)
        assert fetched.to_dict() == written.to_dict()
        assert RunRegistry(reg.root).get_output(run.run_id) == [{"index": 1}]


@test
def test_legacy_records_without_created_at_still_load():
    """The 22 records written before the registry existed must not read as corrupt."""
    with temp_registry() as reg:
        write_record(
            reg,
            "20260101-101010-abc123",
            {
                "run_id": "20260101-101010-abc123",
                "actor": "hello_world",
                "status": "SUCCEEDED",
                "exit_code": 0,
                "started_at": "2026-01-01T10:10:10Z",
                "finished_at": "2026-01-01T10:10:11Z",
                "duration_seconds": 0.4,
                "output": {"message": "Hello Basel"},
                "error": None,
                "run_dir": "/somewhere",
                "log_lines": 4,
                "input": {"name": "Basel"},
            },
        )
        run = reg.get("20260101-101010-abc123")
        assert run.status is RunStatus.SUCCEEDED
        assert run.created_at == "2026-01-01T10:10:10Z", "should fall back to started_at"


# --- 4. list ----------------------------------------------------------------


@test
def test_list_is_newest_first_and_deterministic():
    with temp_registry() as reg:
        # Hand-written ids across distinct seconds, so chronological ordering is
        # actually exercised rather than depending on how fast the loop runs.
        ids = ["20260101-000001-aaaaaa", "20260103-120000-bbbbbb", "20260102-090000-cccccc"]
        for run_id in ids:
            write_record(
                reg,
                run_id,
                {"run_id": run_id, "actor": "hello_world", "status": "SUCCEEDED",
                 "created_at": run_id},
            )

        listed = [r.run_id for r in reg.list()]
        assert listed == sorted(ids, reverse=True), listed
        assert listed[0] == "20260103-120000-bbbbbb", "newest must come first"
        assert listed == [r.run_id for r in reg.list()], "ordering must be stable"


@test
def test_list_filters_and_limit():
    with temp_registry() as reg:
        a = reg.create("hello_world", {})
        b = reg.create("item_processor", {})
        c = reg.create("hello_world", {})
        reg.mark_running(c.run_id)
        reg.complete(c.run_id, status=RunStatus.FAILED, exit_code=1, error="nope")

        assert len(reg.list()) == 3
        assert {r.run_id for r in reg.list(actor="hello_world")} == {a.run_id, c.run_id}
        assert [r.run_id for r in reg.list(actor="item_processor")] == [b.run_id]
        assert [r.run_id for r in reg.list(status=RunStatus.FAILED)] == [c.run_id]
        assert [r.run_id for r in reg.list(status="PENDING")] == sorted(
            [a.run_id, b.run_id], reverse=True
        )
        assert len(reg.list(limit=2)) == 2
        assert reg.list(actor="nobody") == []


@test
def test_list_on_empty_registry():
    with temp_registry() as reg:
        assert reg.list() == [], "a registry with no runs lists nothing, it does not fail"
        assert not reg.root.exists(), "listing must not create the root directory"


@test
def test_list_ignores_directories_that_are_not_runs():
    with temp_registry() as reg:
        run = reg.create("hello_world", {})
        (reg.root / "not-a-run").mkdir(parents=True, exist_ok=True)
        assert [r.run_id for r in reg.list()] == [run.run_id]


# --- 5. artifacts -----------------------------------------------------------


@test
def test_artifact_retrieval():
    with temp_registry() as reg:
        run = reg.create("item_processor", {"count": 2, "fail_at": None})
        reg.mark_running(run.run_id)

        # Stand in for the container writing its result.
        (Path(run.run_dir) / "output.json").write_text('[{"index": 1}, {"index": 2}]')
        reg.write_logs(run.run_id, ["[item_processor] item 1/2", "[item_processor] item 2/2"])

        assert reg.get_input(run.run_id) == {"count": 2, "fail_at": None}
        assert reg.get_output(run.run_id) == [{"index": 1}, {"index": 2}]

        logs = reg.get_logs(run.run_id)
        assert "item 1/2" in logs and "item 2/2" in logs
        assert reg.get(run.run_id).log_lines == 2, "write_logs must record the line count"


@test
def test_missing_or_broken_artifacts_are_explicit():
    with temp_registry() as reg:
        run = reg.create("hello_world", {})

        # A failed run genuinely has no output; that must not look like null.
        exc = raises(ArtifactNotFound, reg.get_output, run.run_id)
        assert "produced no output.json" in str(exc), exc
        raises(ArtifactNotFound, reg.get_logs, run.run_id)

        (Path(run.run_dir) / "output.json").write_text("   ")
        assert "empty output.json" in str(raises(ArtifactCorrupt, reg.get_output, run.run_id))

        (Path(run.run_dir) / "output.json").write_text("{not json")
        assert "not valid JSON" in str(raises(ArtifactCorrupt, reg.get_output, run.run_id))


# --- 6. missing run ---------------------------------------------------------


@test
def test_missing_run_is_explicit_and_creates_nothing():
    with temp_registry() as reg:
        reg.create("hello_world", {})  # so the root exists
        before = sorted(p.name for p in reg.root.iterdir())

        ghost = "20260101-000000-abcdef"
        raises(RunNotFound, reg.get, ghost)
        raises(RunNotFound, reg.get_input, ghost)
        raises(RunNotFound, reg.get_output, ghost)
        raises(RunNotFound, reg.get_logs, ghost)
        raises(RunNotFound, reg.update, ghost, error="x")
        assert not reg.exists(ghost)

        after = sorted(p.name for p in reg.root.iterdir())
        assert before == after, f"a missing-run lookup created something: {set(after) - set(before)}"


@test
def test_run_id_traversal_is_rejected():
    with temp_registry() as reg:
        for hostile in ["../../etc", "..", "not-a-run-id", "20260101-000000-ABCDEF", ""]:
            raises(InvalidRunId, reg.get, hostile)
            raises(InvalidRunId, reg.run_dir, hostile)
            assert reg.exists(hostile) is False
        assert not reg.root.exists(), "rejected ids must not create directories"


# --- 7. corrupt records -----------------------------------------------------


@test
def test_corrupt_records_fail_loudly():
    cases = {
        "malformed JSON": "{this is not json",
        "not an object": '["a", "list"]',
        "missing actor": '{"run_id": "20260101-000001-aaaaaa", "status": "SUCCEEDED"}',
        "missing status": '{"run_id": "20260101-000001-aaaaaa", "actor": "hello_world"}',
        "unknown status": ('{"run_id": "20260101-000001-aaaaaa", "actor": "hello_world",'
                           ' "status": "EXPLODED"}'),
    }
    for label, body in cases.items():
        with temp_registry() as reg:
            write_record(reg, "20260101-000001-aaaaaa", body)
            exc = raises(CorruptRunRecord, reg.get, "20260101-000001-aaaaaa")
            assert "20260101-000001-aaaaaa" in str(exc), f"{label}: error must name the run"


@test
def test_list_refuses_to_hide_a_corrupt_record():
    with temp_registry() as reg:
        healthy = reg.create("hello_world", {})
        write_record(reg, "20260101-000001-aaaaaa", "{broken")

        # Strict by default: better to fail than to under-report silently.
        raises(CorruptRunRecord, reg.list)

        # Opt out only on purpose.
        assert [r.run_id for r in reg.list(skip_corrupt=True)] == [healthy.run_id]


# --- exit-code distinction --------------------------------------------------


@test
def test_exit_codes_stay_three_way():
    assert worker_exit_code(RunStatus.SUCCEEDED) == EXIT_SUCCEEDED == 0
    assert worker_exit_code(RunStatus.FAILED) == EXIT_ACTOR_FAILED == 1
    # There is no TIMED_OUT: the platform never ends a run for taking too long.
    assert not hasattr(RunStatus, "TIMED_OUT")
    assert worker_exit_code(RunStatus.PLATFORM_ERROR) == EXIT_PLATFORM_ERROR == 2

    # An Actor failure and a platform failure must never report the same code.
    assert worker_exit_code(RunStatus.FAILED) != worker_exit_code(RunStatus.PLATFORM_ERROR)

    # Non-terminal statuses have no exit code to give.
    for status in (RunStatus.PENDING, RunStatus.RUNNING):
        try:
            worker_exit_code(status)
        except ValueError:
            continue
        raise AssertionError(f"{status.value} should have no exit code")


# --- run id ordering --------------------------------------------------------


@test
def test_run_ids_are_ordered_under_load():
    """Ids minted in quick succession must still sort into creation order.

    With second-resolution ids they did not: every run in the same second tied
    on the timestamp and was ordered by its random suffix, i.e. arbitrarily.
    Measured: 20,000 ids in a tight loop produced ONE distinct second and 62
    distinct milliseconds, but 20,000 distinct microseconds.
    """
    ids = [RunRegistry.new_run_id() for _ in range(2000)]

    assert len(set(ids)) == len(ids), f"{len(ids) - len(set(ids))} duplicate ids"
    assert ids == sorted(ids), "ids must sort into the order they were minted"
    for run_id in ids[:50]:
        assert is_valid_run_id(run_id), run_id


@test
def test_list_returns_rapid_runs_in_creation_order():
    """The user-facing guarantee: `runs list` is chronological, not arbitrary.

    Ten runs created back-to-back land in the same second. Under the old id
    format their order came from the random suffix, so this ordering held only
    by luck (1 chance in 10!).
    """
    with temp_registry() as reg:
        created = [reg.create("hello_world", {"n": i}).run_id for i in range(10)]

        listed = [run.run_id for run in reg.list()]
        assert listed == list(reversed(created)), (
            "list() must return newest-first in true creation order\n"
            f"  created: {created}\n  listed : {list(reversed(listed))}"
        )


@test
def test_legacy_run_ids_remain_valid_and_sort_correctly():
    """The 115 runs recorded before this change must stay readable."""
    legacy, modern = "20260902-083534-a3d1fb", "20260902-083534123456-a3d1fb"

    assert is_valid_run_id(legacy), "old records would become unreachable"
    assert is_valid_run_id(modern)

    # Same second: "-" sorts below every digit, so the legacy id comes first --
    # which is also its true order, since all legacy runs predate the change.
    assert sorted([modern, legacy]) == [legacy, modern]
    # A later legacy second still sorts after an earlier microsecond id.
    assert sorted(["20260902-083535-aaaaaa", modern]) == [modern, "20260902-083535-aaaaaa"]

    with temp_registry() as reg:
        write_record(reg, legacy, {"run_id": legacy, "actor": "hello_world",
                                   "status": "SUCCEEDED", "created_at": "2026-09-02T08:35:34Z"})
        assert reg.get(legacy).run_id == legacy
        assert reg.resolve(legacy[:18]) == legacy


@test
def test_the_record_does_not_carry_the_output():
    """The output lives in output.json; the record points at the directory.

    Embedding it doubled every run's on-disk cost and made list() parse every
    result to render summaries that contain none of it.
    """
    with temp_registry() as reg:
        run = reg.create("item_processor", {"count": 2})
        reg.mark_running(run.run_id)
        (Path(run.run_dir) / "output.json").write_text('[{"index": 1}, {"index": 2}]')
        finished = reg.complete(run.run_id, status=RunStatus.SUCCEEDED, exit_code=0)

        record = json.loads((Path(run.run_dir) / "run.json").read_text())
        assert "output" not in record, "run.json embeds the output again"
        assert "output" not in finished.to_dict()

        # And it is still reachable, by the one route that reads the artifact.
        assert reg.get_output(run.run_id) == [{"index": 1}, {"index": 2}]


@test
def test_a_legacy_record_that_embeds_output_still_loads():
    """Records written before this change carry an `output` key; it is ignored."""
    with temp_registry() as reg:
        write_record(
            reg,
            "20260101-101010-abc123",
            {
                "run_id": "20260101-101010-abc123",
                "actor": "hello_world",
                "status": "SUCCEEDED",
                "created_at": "2026-01-01T10:10:10Z",
                "exit_code": 0,
                "output": {"message": "from an older version"},
            },
        )
        run = reg.get("20260101-101010-abc123")
        assert run.status is RunStatus.SUCCEEDED
        assert not hasattr(run, "output"), "the model should no longer have the field"


# --- prefix resolution ------------------------------------------------------


@test
def test_resolve_expands_a_unique_prefix():
    with temp_registry() as reg:
        run = reg.create("hello_world", {})

        assert reg.resolve(run.run_id) == run.run_id, "a full id must resolve to itself"
        assert reg.resolve(run.run_id[:18]) == run.run_id
        assert reg.resolve(run.run_id[:9]) == run.run_id


@test
def test_resolve_refuses_to_guess():
    with temp_registry() as reg:
        for suffix in ("aaaaaa", "bbbbbb"):
            write_record(
                reg,
                f"20260101-000001-{suffix}",
                {"run_id": f"20260101-000001-{suffix}", "actor": "hello_world",
                 "status": "SUCCEEDED", "created_at": "2026-01-01T00:00:01Z"},
            )

        exc = raises(AmbiguousRunId, reg.resolve, "20260101-000001")
        assert "matches 2 runs" in str(exc)
        # Naming the candidates is the difference between a dead end and a hint.
        assert "aaaaaa" in str(exc) and "bbbbbb" in str(exc)

        # Enough characters to disambiguate resolves fine.
        assert reg.resolve("20260101-000001-a") == "20260101-000001-aaaaaa"


@test
def test_resolve_rejects_hostile_prefixes():
    with temp_registry() as reg:
        reg.create("hello_world", {})
        for hostile in ["../../etc", "..", "", "zzz", "20260101/000001", "'; rm -rf /"]:
            raises(InvalidRunId, reg.resolve, hostile)
        raises(RunNotFound, reg.resolve, "19990101")


# --- items: the optional dataset part of the contract ------------------------
#
# Every test goes through DatasetIngestor, which is a no-op copy on the
# filesystem backend and a real one on Postgres -- so the SAME assertions hold
# for both, and test_registry_pg.py re-runs them against the database.


def _write_dataset(reg: RunRegistry, run_id: str, lines: list, *, terminate=True) -> None:
    body = "\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines)
    reg.dataset_path(run_id).write_text(body + ("\n" if terminate else ""), encoding="utf-8")


def _drain(reg: RunRegistry, run_id: str) -> dict:
    from worker.dataset_ingest import DatasetIngestor  # noqa: PLC0415
    return DatasetIngestor(reg, run_id, log=lambda _m: None).stop_and_drain()


@test
def test_items_come_back_in_production_order_and_page_by_seq():
    with temp_registry() as reg:
        run = reg.create("hello_world", {})
        _write_dataset(reg, run.run_id, [
            {"collection": "posts", "key": f"p{i}", "data": {"n": i}} for i in range(1, 6)
        ])
        _drain(reg, run.run_id)

        first = reg.get_items(run.run_id, limit=2)
        assert [i["seq"] for i in first["items"]] == [1, 2], first
        assert first["items"][0] == {"seq": 1, "collection": "posts", "key": "p1",
                                     "data": {"n": 1}}, first
        assert first["next_after"] == 2, first

        rest = reg.get_items(run.run_id, after=first["next_after"], limit=10)
        assert [i["seq"] for i in rest["items"]] == [3, 4, 5], rest
        assert rest["next_after"] is None, "the last page must say there is no more"


@test
def test_items_filter_by_collection_without_interpreting_it():
    with temp_registry() as reg:
        run = reg.create("hello_world", {})
        _write_dataset(reg, run.run_id, [
            {"collection": "posts", "data": 1},
            {"collection": "comments", "key": "a:1", "data": 2},
            {"collection": "comments", "key": "a:1", "data": 3},  # keys need not be unique
        ])
        _drain(reg, run.run_id)
        got = reg.get_items(run.run_id, collection="comments")
        assert [(i["seq"], i["data"]) for i in got["items"]] == [(2, 2), (3, 3)], got


@test
def test_a_malformed_line_is_skipped_and_keeps_its_seq():
    """One bad line must not cost the lines after it, nor shift their identity."""
    with temp_registry() as reg:
        run = reg.create("hello_world", {})
        _write_dataset(reg, run.run_id, [
            {"collection": "x", "data": "first"},
            "{not json",
            {"data": "no collection"},
            {"collection": "../etc", "data": "bad collection"},
            {"collection": "x"},  # no data
            {"collection": "x", "data": "sixth"},
        ])
        stats = _drain(reg, run.run_id)
        got = reg.get_items(run.run_id)["items"]
        assert [(i["seq"], i["data"]) for i in got] == [(1, "first"), (6, "sixth")], got
        if reg.STORES_ITEMS:
            assert stats["malformed"] == 4, stats


@test
def test_ingesting_twice_stores_nothing_twice():
    with temp_registry() as reg:
        run = reg.create("hello_world", {})
        _write_dataset(reg, run.run_id, [{"collection": "x", "data": i} for i in range(3)])
        _drain(reg, run.run_id)
        second = _drain(reg, run.run_id)  # a restarted worker reads from the top
        assert second["inserted"] == 0, second
        assert len(reg.get_items(run.run_id)["items"]) == 3


@test
def test_a_half_written_line_waits_for_its_newline_until_the_final_read():
    from registry.dataset import read_from  # noqa: PLC0415
    from worker.dataset_ingest import DatasetIngestor  # noqa: PLC0415

    with temp_registry() as reg:
        run = reg.create("hello_world", {})
        path = reg.dataset_path(run.run_id)
        path.write_text('{"collection": "x", "data": 1}\n{"collection": "x", "da',
                        encoding="utf-8")
        live = read_from(path)
        assert [i.seq for i in live.items] == [1], "a partial line must not be read mid-write"
        assert live.malformed == 0, "nor counted as malformed"

        with path.open("a", encoding="utf-8") as fh:
            fh.write('ta": 2}')  # the Actor finishes the line but exits without \n
        ingestor = DatasetIngestor(reg, run.run_id, log=lambda _m: None)
        ingestor.pump(final=False)
        ingestor.stop_and_drain()
        assert [i["data"] for i in reg.get_items(run.run_id)["items"]] == [1, 2]


@test
def test_a_new_attempt_discards_the_previous_attempts_items():
    with temp_registry() as reg:
        run = reg.create("hello_world", {})
        _write_dataset(reg, run.run_id, [{"collection": "x", "data": "old"}])
        _drain(reg, run.run_id)
        reg.reset_items(run.run_id)
        assert reg.get_items(run.run_id)["items"] == []
        assert not reg.dataset_path(run.run_id).exists()


@test
def test_a_run_with_no_items_has_an_empty_page_not_an_error():
    with temp_registry() as reg:
        run = reg.create("hello_world", {})
        _drain(reg, run.run_id)
        assert reg.get_items(run.run_id) == {"items": [], "next_after": None}
        raises(RunNotFound, reg.get_items, "20990101-000000000000-abcdef")


def main() -> int:
    failures = 0
    print(f"running {len(TESTS)} registry tests\n")

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
    if failures:
        print(f"{failures} of {len(TESTS)} tests FAILED")
    else:
        print(f"all {len(TESTS)} tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
