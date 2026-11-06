#!/usr/bin/env python3
"""Items through the REAL executor path: tailed while the container runs.

    ACTOR_PLATFORM_DATABASE_URL=postgresql://.../<throwaway> python3 test_items.py

The registry tests prove the file is parsed and stored correctly. What they
cannot prove is the wiring: that _run_prepared resets a previous attempt,
tails the file WHILE the container is alive, and drains it before the run is
marked terminal -- on a failed run too. So each test drives _run_prepared with
the container replaced by a local process that writes the run directory the
way an Actor would. No Docker, no network.

The database is TRUNCATED: point this at a throwaway one, never a deployment.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if not (os.environ.get("ACTOR_PLATFORM_DATABASE_URL") or "").strip():
    print("ACTOR_PLATFORM_DATABASE_URL must point at a THROWAWAY database "
          "(this suite truncates tables)")
    sys.exit(2)

from db.pool import pool  # noqa: E402
from registry.pg_registry import PgRunRegistry  # noqa: E402
from shared.models import RunStatus  # noqa: E402
from worker import executor  # noqa: E402
from worker.limits import ExecutionLimits  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


# The stand-in Actor. Writes one item, waits until the test has SEEN that item
# through the registry (proving it was stored while this process was alive),
# then writes a second item with no trailing newline and exits.
FAKE_ACTOR = textwrap.dedent("""
    import json, os, sys, time
    run_dir, exit_code = sys.argv[1], int(sys.argv[2])
    with open(os.path.join(run_dir, "dataset.jsonl"), "a") as fh:
        fh.write(json.dumps({"collection": "c", "key": "one", "data": 1}) + "\\n")
        fh.flush()
    deadline = time.time() + 30
    while not os.path.exists(os.path.join(run_dir, "seen")) and time.time() < deadline:
        time.sleep(0.1)
    with open(os.path.join(run_dir, "dataset.jsonl"), "a") as fh:
        fh.write(json.dumps({"collection": "c", "key": "two", "data": 2}))
    if exit_code == 0:
        with open(os.path.join(run_dir, "output.json"), "w") as fh:
            json.dump({"ok": True}, fh)
    sys.exit(exit_code)
""")


def run_with_fake_container(reg, run, exit_code):
    """_run_prepared, with docker swapped for FAKE_ACTOR. Watches for item 1."""
    import threading

    seen_while_running = threading.Event()

    def watch():
        deadline = time.time() + 20
        while time.time() < deadline:
            if reg.get_items(run.run_id)["items"]:
                seen_while_running.set()
                (Path(run.run_dir) / "seen").write_text("1")
                return
            time.sleep(0.2)
        (Path(run.run_dir) / "seen").write_text("timeout")

    original = executor._container_args
    executor._container_args = lambda *a, **k: [
        sys.executable, "-c", FAKE_ACTOR, str(run.run_dir), str(exit_code)]
    watcher = threading.Thread(target=watch, daemon=True)
    try:
        watcher.start()
        finished = executor._run_prepared(run, registry=reg, limits=ExecutionLimits())
    finally:
        executor._container_args = original
        watcher.join(timeout=25)
    return finished, seen_while_running.is_set()


def fresh_registry(tmp):
    with pool().connection() as conn:
        conn.execute("TRUNCATE users, runs CASCADE")
    return PgRunRegistry(Path(tmp) / "runs")


@test
def test_items_are_stored_while_the_run_goes_and_drained_before_it_ends():
    with tempfile.TemporaryDirectory() as tmp:
        reg = fresh_registry(tmp)
        run = reg.create("hello_world", {})
        finished, live = run_with_fake_container(reg, run, exit_code=0)

        assert finished.status is RunStatus.SUCCEEDED, (finished.status, finished.error)
        assert live, "item 1 must be readable from the database while the Actor is running"
        items = reg.get_items(run.run_id)["items"]
        assert [(i["key"], i["data"]) for i in items] == [("one", 1), ("two", 2)], (
            "the unterminated last line must be taken on the final drain", items)


@test
def test_a_failed_run_keeps_the_items_it_produced():
    with tempfile.TemporaryDirectory() as tmp:
        reg = fresh_registry(tmp)
        run = reg.create("hello_world", {})
        finished, _ = run_with_fake_container(reg, run, exit_code=3)

        assert finished.status is RunStatus.FAILED, finished.status
        assert [i["data"] for i in reg.get_items(run.run_id)["items"]] == [1, 2]


@test
def test_a_retried_run_does_not_keep_the_previous_attempts_items():
    with tempfile.TemporaryDirectory() as tmp:
        reg = fresh_registry(tmp)
        run = reg.create("hello_world", {})
        # What an interrupted first attempt left behind, file and rows both.
        reg.dataset_path(run.run_id).write_text(
            json.dumps({"collection": "c", "key": "stale", "data": 0}) + "\n")
        from worker.dataset_ingest import DatasetIngestor  # noqa: PLC0415
        DatasetIngestor(reg, run.run_id, log=lambda _m: None).stop_and_drain()
        assert reg.get_items(run.run_id)["items"], "precondition: stale item stored"

        finished, _ = run_with_fake_container(reg, run, exit_code=0)
        keys = [i["key"] for i in reg.get_items(run.run_id)["items"]]
        assert keys == ["one", "two"], keys


@test
def test_a_run_whose_actor_writes_no_items_is_unchanged():
    with tempfile.TemporaryDirectory() as tmp:
        reg = fresh_registry(tmp)
        run = reg.create("hello_world", {})
        original = executor._container_args
        executor._container_args = lambda *a, **k: [
            sys.executable, "-c",
            f"import json; json.dump({{'ok': 1}}, open({str(Path(run.run_dir) / 'output.json')!r}, 'w'))"]
        try:
            finished = executor._run_prepared(run, registry=reg, limits=ExecutionLimits())
        finally:
            executor._container_args = original
        assert finished.status is RunStatus.SUCCEEDED, (finished.status, finished.error)
        assert reg.get_items(run.run_id) == {"items": [], "next_after": None}
        assert not reg.dataset_path(run.run_id).exists(), "no file must be created for it"


def main() -> int:
    print(f"running {len(TESTS)} item ingestion tests\n")
    failed = 0
    for fn in TESTS:
        name = fn.__name__.removeprefix("test_")
        try:
            fn()
            print(f"  ok    {name}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}")
            print("        " + traceback.format_exc().strip().replace("\n", "\n        "))
    print(f"\n{'all ' + str(len(TESTS)) + ' tests passed' if not failed else f'{failed} FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
