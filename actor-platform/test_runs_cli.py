#!/usr/bin/env python3
"""`runs` CLI tests.

Offline: the CLI is driven in-process against a throwaway registry, so no
Docker, no Actor images and no touching the real storage/runs/.

    python3 test_runs_cli.py
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import traceback
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cli.runs import main  # noqa: E402
from registry.run_registry import RunRegistry  # noqa: E402
from shared.models import RunStatus  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


@contextmanager
def temp_registry():
    with tempfile.TemporaryDirectory() as tmp:
        yield RunRegistry(Path(tmp) / "runs")


def cli(registry: RunRegistry, *argv: str) -> tuple[int, str, str]:
    """Run the CLI in-process, keeping the two streams apart."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv), registry=registry)
    return code, out.getvalue(), err.getvalue()


def make_run(registry: RunRegistry, actor: str, payload: dict, *, output=None, logs=()) -> str:
    """A finished run, complete with the artifacts a container would leave."""
    run = registry.create(actor, payload)
    registry.mark_running(run.run_id)
    if logs:
        registry.write_logs(run.run_id, list(logs))
    if output is not None:
        (Path(run.run_dir) / "output.json").write_text(json.dumps(output))
        registry.complete(run.run_id, status=RunStatus.SUCCEEDED, exit_code=0)
    else:
        registry.complete(
            run.run_id, status=RunStatus.FAILED, exit_code=1, error="actor exited with code 1"
        )
    return run.run_id


def plant(registry: RunRegistry, run_id: str, body: str) -> None:
    directory = registry.root / run_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run.json").write_text(body)


# --- list -------------------------------------------------------------------


@test
def test_list_empty_registry():
    with temp_registry() as reg:
        code, out, _ = cli(reg, "list")
        assert code == 0, "an empty registry is not an error"
        assert "no runs found" in out


@test
def test_list_shows_runs_newest_first():
    with temp_registry() as reg:
        for run_id in ("20260101-000001-aaaaaa", "20260103-120000-bbbbbb", "20260102-090000-cccccc"):
            plant(reg, run_id, json.dumps(
                {"run_id": run_id, "actor": "hello_world", "status": "SUCCEEDED",
                 "created_at": "2026-01-01T00:00:01Z", "exit_code": 0}))

        code, out, _ = cli(reg, "list")
        assert code == 0
        assert "RUN ID" in out and "ACTOR" in out and "STATUS" in out

        order = [line.split()[0] for line in out.splitlines() if line.startswith("2026")]
        assert order == sorted(order, reverse=True), order
        assert order[0] == "20260103-120000-bbbbbb", "newest first"
        assert "3 run(s)" in out


@test
def test_list_json_is_machine_readable():
    with temp_registry() as reg:
        run_id = make_run(reg, "hello_world", {"name": "Basel"}, output={"message": "Hello Basel"})
        code, out, _ = cli(reg, "list", "--json")
        assert code == 0

        docs = json.loads(out)  # the whole point: parses with nothing stripped
        assert isinstance(docs, list) and len(docs) == 1
        assert docs[0]["run_id"] == run_id
        assert docs[0]["status"] == "SUCCEEDED"
        # A listing carries no outputs: that duplication is what the record
        # stopped doing. `runs output <id>` is how you get one.
        assert "output" not in docs[0]


@test
def test_list_filters():
    with temp_registry() as reg:
        ok = make_run(reg, "hello_world", {}, output={"message": "hi"})
        bad = make_run(reg, "item_processor", {})

        assert len(json.loads(cli(reg, "list", "--json")[1])) == 2
        assert [d["run_id"] for d in json.loads(cli(reg, "list", "--actor", "hello_world", "--json")[1])] == [ok]
        assert [d["run_id"] for d in json.loads(cli(reg, "list", "--status", "FAILED", "--json")[1])] == [bad]
        assert len(json.loads(cli(reg, "list", "--limit", "1", "--json")[1])) == 1

        code, out, _ = cli(reg, "list", "--limit", "1")
        assert "limit 1" in out, "a truncated listing must say so"


@test
def test_list_rejects_an_unknown_status():
    with temp_registry() as reg:
        try:
            cli(reg, "list", "--status", "EXPLODED")
        except SystemExit as exc:
            assert exc.code == 2, exc.code
        else:
            raise AssertionError("an invalid --status should be refused")


# --- show / logs / output / input -------------------------------------------


@test
def test_show_a_successful_run():
    with temp_registry() as reg:
        run_id = make_run(reg, "hello_world", {"name": "Basel"}, output={"message": "Hello Basel"})
        code, out, _ = cli(reg, "show", run_id)

        assert code == 0
        for expected in (run_id, "hello_world", "SUCCEEDED", '"name": "Basel"', "Hello Basel"):
            assert expected in out, f"missing {expected!r}"


@test
def test_show_a_failed_run_explains_the_missing_output():
    with temp_registry() as reg:
        run_id = make_run(reg, "item_processor", {"count": 3, "fail_at": 2})
        code, out, _ = cli(reg, "show", run_id)

        assert code == 0, "inspecting a failed run is a SUCCESSFUL inspection"
        assert "FAILED" in out
        assert "actor exited with code 1" in out
        assert "the run did not succeed" in out, "a bare 'null' output would read as a bug"


@test
def test_show_json():
    with temp_registry() as reg:
        run_id = make_run(reg, "hello_world", {}, output=[1, 2, 3])
        code, out, _ = cli(reg, "show", run_id, "--json")
        assert code == 0
        assert json.loads(out)["run_id"] == run_id


@test
def test_logs_and_input_and_output():
    with temp_registry() as reg:
        run_id = make_run(
            reg, "item_processor", {"count": 2},
            output=[{"index": 1}], logs=["[item_processor] item 1/2", "[item_processor] done"],
        )

        code, out, _ = cli(reg, "logs", run_id)
        assert code == 0 and "item 1/2" in out and "done" in out

        code, out, _ = cli(reg, "output", run_id)
        assert code == 0 and json.loads(out) == [{"index": 1}]

        code, out, _ = cli(reg, "input", run_id)
        assert code == 0 and json.loads(out) == {"count": 2}


@test
def test_output_of_a_failed_run_is_non_zero():
    """`runs output X > result.json` must not silently write an empty file."""
    with temp_registry() as reg:
        run_id = make_run(reg, "item_processor", {"count": 3, "fail_at": 2})
        code, out, err = cli(reg, "output", run_id)

        assert code == 2, f"expected 2, got {code}"
        assert out == "", "nothing may reach stdout when there is no output"
        assert "produced no output.json" in err
        assert "FAILED" in err, "the reason must be explained on stderr"


# --- run id handling --------------------------------------------------------


@test
def test_a_unique_prefix_resolves():
    with temp_registry() as reg:
        run_id = make_run(reg, "hello_world", {}, output={"ok": True})
        code, out, _ = cli(reg, "show", run_id[:18], "--json")
        assert code == 0
        assert json.loads(out)["run_id"] == run_id


@test
def test_an_ambiguous_prefix_is_refused():
    with temp_registry() as reg:
        for suffix in ("aaaaaa", "bbbbbb"):
            run_id = f"20260101-000001-{suffix}"
            plant(reg, run_id, json.dumps(
                {"run_id": run_id, "actor": "hello_world", "status": "SUCCEEDED",
                 "created_at": "2026-01-01T00:00:01Z"}))

        code, out, err = cli(reg, "show", "20260101-000001")
        assert code == 2
        assert out == ""
        assert "matches 2 runs" in err, err
        assert "aaaaaa" in err and "bbbbbb" in err, "it must show WHICH runs"


@test
def test_missing_and_hostile_run_ids():
    with temp_registry() as reg:
        make_run(reg, "hello_world", {}, output={})
        before = sorted(p.name for p in reg.root.iterdir())

        for command in ("show", "logs", "output", "input"):
            code, out, err = cli(reg, command, "20260101-000000-abcdef")
            assert code == 2, f"{command}: expected 2, got {code}"
            assert out == "" and "ERROR" in err

            code, _, err = cli(reg, command, "../../etc")
            assert code == 2, f"{command}: traversal must be refused"
            assert "invalid run id" in err

        assert sorted(p.name for p in reg.root.iterdir()) == before, "lookups created something"


# --- corrupt records --------------------------------------------------------


@test
def test_corrupt_record_is_reported_with_a_way_forward():
    with temp_registry() as reg:
        healthy = make_run(reg, "hello_world", {}, output={"ok": True})
        plant(reg, "20260101-000001-aaaaaa", "{broken")

        code, out, err = cli(reg, "list")
        assert code == 2
        assert "20260101-000001-aaaaaa" in err, "name the bad record"
        assert "--skip-corrupt" in err, "and say how to get past it"

        code, out, _ = cli(reg, "list", "--skip-corrupt")
        assert code == 0 and healthy in out


# --- exit codes -------------------------------------------------------------


@test
def test_the_cli_never_exits_1():
    """Exit 1 means 'the Actor failed'; a read-only command cannot mean that."""
    with temp_registry() as reg:
        failed = make_run(reg, "item_processor", {"count": 2, "fail_at": 1})
        plant(reg, "20260101-000001-aaaaaa", "{broken")

        seen = {
            cli(reg, "list", "--skip-corrupt")[0],
            cli(reg, "show", failed)[0],
            cli(reg, "logs", failed)[0],
            cli(reg, "output", failed)[0],
            cli(reg, "input", failed)[0],
            cli(reg, "list")[0],
            cli(reg, "show", "20260101-000000-abcdef")[0],
        }
        assert seen <= {0, 2}, f"unexpected exit codes: {seen}"
        assert 1 not in seen


def main_() -> int:
    failures = 0
    print(f"running {len(TESTS)} runs-CLI tests\n")

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
    print(f"{failures} of {len(TESTS)} tests FAILED" if failures else f"all {len(TESTS)} tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main_())
