#!/usr/bin/env python3
"""End-to-end regression suite.

Drives the real worker against real Docker and asserts the behaviour every
earlier milestone established, then checks those runs are visible through the
Run Registry.

Needs Docker and both Actor images (they are built on demand).

    python3 test_regression.py
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import traceback
from contextlib import redirect_stderr
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from registry.run_registry import ArtifactNotFound, RunRegistry  # noqa: E402
from shared.models import RunStatus  # noqa: E402
from worker.executor import _container_args, _execute, forwarded_env_names  # noqa: E402

WORKER = REPO_ROOT / "worker" / "executor.py"
REGISTRY = RunRegistry()

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def run_worker(*args: str) -> tuple[int, str, str]:
    """Invoke the worker exactly as a user would; keep the streams separate."""
    proc = subprocess.run(
        [sys.executable, str(WORKER), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    return proc.returncode, proc.stdout, proc.stderr


def run_count() -> int:
    root = REGISTRY.root
    return len([p for p in root.iterdir() if (p / "run.json").is_file()]) if root.is_dir() else 0


# --- the four Actor executions ---------------------------------------------


@test
def test_hello_world_success():
    code, out, err = run_worker("hello_world", "--input", '{"name": "Basel"}')
    assert code == 0, f"expected exit 0, got {code}\n{err}"

    doc = json.loads(out)  # stdout must be pure JSON
    assert doc["status"] == "SUCCEEDED"
    assert doc["exit_code"] == 0
    assert doc["output"] == {"message": "Hello Basel"}
    assert doc["error"] is None
    assert doc["created_at"], "run record must carry created_at"

    # Logs go to stderr, never polluting the machine-readable stream.
    assert "[worker]" in err and "[worker]" not in out
    assert "[hello_world]" in err

    directory = Path(doc["run_dir"])
    for artifact in ("input.json", "output.json", "log.txt", "run.json"):
        assert (directory / artifact).is_file(), f"missing {artifact}"


@test
def test_hello_world_actor_failure():
    code, out, err = run_worker("hello_world", "--input", "{}")
    assert code == 1, f"an Actor failure must be exit 1, got {code}"

    doc = json.loads(out)
    assert doc["status"] == "FAILED"
    assert doc["exit_code"] == 1
    assert doc["output"] is None
    assert "is required" in REGISTRY.get_logs(doc["run_id"])

    directory = Path(doc["run_dir"])
    assert not (directory / "output.json").exists(), "a failed run must write no output"


@test
def test_item_processor_success():
    code, out, _ = run_worker("item_processor", "--input", '{"count": 5, "fail_at": null}')
    assert code == 0, f"expected exit 0, got {code}"

    doc = json.loads(out)
    assert doc["status"] == "SUCCEEDED"
    assert isinstance(doc["output"], list), f"output must be an ARRAY, got {type(doc['output'])}"
    assert len(doc["output"]) == 5
    assert doc["output"][0] == {
        "index": 1,
        "title": "Sample Item #1",
        "slug": "sample-item-1",
    }, "the third-party dependency must still be doing the work"


@test
def test_item_processor_mid_run_failure():
    code, out, _ = run_worker("item_processor", "--input", '{"count": 5, "fail_at": 3}')
    assert code == 1, f"a mid-run Actor failure must be exit 1, got {code}"

    doc = json.loads(out)
    assert doc["status"] == "FAILED"
    assert doc["output"] is None

    logs = REGISTRY.get_logs(doc["run_id"])
    assert "item 1/5" in logs and "item 2/5" in logs, "partial progress must be preserved"
    assert "partial progress: 2 of 5" in logs
    assert "item 3/5" not in logs, "it must have failed AT item 3"


# --- the three platform-start failures --------------------------------------


@test
def test_invalid_actor_name():
    before = run_count()
    code, out, err = run_worker("_broken_probe", "--input", "{}")

    assert code == 2, f"a platform-start failure must be exit 2, got {code}"
    assert "invalid actor name" in err
    assert "[worker] building" not in err, "no build may be attempted for a bad name"
    assert out == "", "no run means no run JSON on stdout"
    assert run_count() == before, "a rejected run must leave no record"


@test
def test_unknown_actor():
    before = run_count()
    code, out, err = run_worker("facebook-scraper", "--input", "{}")

    assert code == 2, f"expected exit 2, got {code}"
    assert "unknown actor" in err
    assert "invalid actor name" not in err, "a valid-but-absent name is a different failure"
    assert out == ""
    assert run_count() == before


@test
def test_malformed_input_json():
    before = run_count()
    code, out, err = run_worker("hello_world", "--input", '{"name": ,}')

    assert code == 2, f"expected exit 2, got {code}"
    assert "not valid JSON" in err
    assert out == ""
    assert run_count() == before


# --- secret forwarding ------------------------------------------------------


@test
def test_secrets_are_forwarded_by_name_never_by_value():
    """An Actor needs credentials the platform must never see.

    The value goes in as `-e NAME` with no `=value`, so the docker CLI reads it
    from the Worker's own environment. Writing `-e NAME=value` would put the
    secret in the container's argv, where `ps` shows it to every user on the box.
    """
    from worker.executor import forwarded_env_names

    env = {
        "ACTOR_FORWARD_ENV": "A_SECRET, NOT_SET_ANYWHERE ,ACTOR_RUN_ID",
        "A_SECRET": "the-value-that-must-not-appear",
    }
    assert forwarded_env_names(env) == ["A_SECRET"], forwarded_env_names(env)

    previous = dict(os.environ)
    try:
        os.environ.update(env)
        args = _container_args("hello_world", "20260101-000001-aaaaaa", Path("/tmp/probe"))
    finally:
        os.environ.clear()
        os.environ.update(previous)

    assert "A_SECRET" in args, "the name must be passed so docker resolves it"
    assert not any("the-value-that-must-not-appear" in arg for arg in args), (
        "the secret VALUE reached the container's argv"
    )
    # A contract variable must not be overridable by "forwarding" one over it.
    assert "ACTOR_RUN_ID" not in forwarded_env_names(env)


@test
def test_a_forwarded_secret_reaches_the_container_and_nothing_else():
    secret = "forwarded-secret-9f3a"
    env = {**os.environ, "ACTOR_FORWARD_ENV": "A_SECRET", "A_SECRET": secret}

    result = subprocess.run(
        ["docker", "run", "--rm", "-e", "A_SECRET", "--entrypoint", "python",
         "actor-platform/hello_world:latest", "-c",
         "import os; print(os.environ.get('A_SECRET'))"],
        capture_output=True, text=True, env=env, timeout=300,
    )
    assert result.returncode == 0, result.stderr
    assert secret in result.stdout, "the Actor did not receive the forwarded value"

    # And the platform wrote it nowhere: forwarding touches no run record.
    code, out, _ = run_worker("hello_world", "--input", '{"name": "Forwarded"}')
    assert code == 0
    doc = json.loads(out)
    assert secret not in json.dumps(doc), "a forwarded secret reached the run record"
    assert secret not in REGISTRY.get_logs(doc["run_id"]), "a forwarded secret reached the log"


# --- repository layout ------------------------------------------------------


@test
def test_no_module_shadows_its_own_package():
    """A package must not contain a module of the same name.

    worker/worker.py did, and it broke imports in a way that depended on the
    working directory: running `python worker/consumer.py` puts worker/ on
    sys.path, so a bare `import worker` found the MODULE and `worker.worker`
    failed with "'worker' is not a package". Renaming to worker/executor.py
    fixed it; this keeps anyone from reintroducing the shape anywhere.
    """
    offenders = [
        f"{d.name}/{d.name}.py"
        for d in REPO_ROOT.iterdir()
        if d.is_dir() and not d.name.startswith((".", "__")) and (d / f"{d.name}.py").is_file()
    ]
    assert not offenders, f"module shadows its own package: {offenders}"


# --- log capture integrity --------------------------------------------------


@test
def test_container_gets_one_pty_for_both_streams():
    """The structural guard, and the reliable one.

    Without -t, docker hands the container two separate pipes, demultiplexes
    them and the worker re-merges them -- two writers, one pipe, no ordering or
    atomicity. With it, both fds are one pty and the kernel serialises writes.
    Asserted on the arguments because it is deterministic: the behavioural test
    below only catches the bug probabilistically.
    """
    args = _container_args("hello_world", "20260101-000001-aaaaaa", Path("/tmp/probe"))
    assert "-t" in args, (
        "docker -t is missing: stdout and stderr will race and splice together"
    )


@test
def test_log_capture_survives_heavy_interleaving():
    """The measured bug, reproduced at the volume that actually triggers it.

    An Actor writing one stderr line at the end (as item_processor does) almost
    never races. Alternating 200 stdout/stderr pairs does: measured over 10
    trials WITHOUT the fix, 7/10 runs had lines spliced together with the
    newline lost (e.g. "38 err0 out") and 3/10 had lines out of order.

    This drives the worker's own capture path, so it tests the code that broke.
    """
    count = 200
    program = (
        "import sys\n"
        f"for i in range({count}):\n"
        "    print(f'{i} out', flush=True)\n"
        "    print(f'{i} err', file=sys.stderr, flush=True)\n"
    )
    # Same flags the worker uses, with the entrypoint swapped for a generator.
    command = [
        arg for arg in _container_args("hello_world", "20260101-000001-aaaaaa", Path("/tmp/probe"))
        if arg != "actor-platform/hello_world:latest"
    ] + ["--entrypoint", "python", "actor-platform/hello_world:latest", "-c", program]

    expected = [f"{i} {stream}" for i in range(count) for stream in ("out", "err")]
    well_formed = re.compile(r"\d+ (out|err)")

    for attempt in range(3):
        sink = io.StringIO()
        with redirect_stderr(sink):  # _execute streams every line; keep it quiet
            _, lines = _execute(command, "log-capture-probe")

        lines = [ln for ln in lines if ln.strip()]

        spliced = [ln for ln in lines if not well_formed.fullmatch(ln)]
        assert not spliced, f"attempt {attempt}: spliced lines: {spliced[:3]}"

        assert len(lines) == len(expected), (
            f"attempt {attempt}: {len(lines)} lines, expected {len(expected)} - lines were lost"
        )
        assert lines == expected, (
            f"attempt {attempt}: {sum(1 for a, b in zip(expected, lines) if a != b)} lines misordered"
        )


@test
def test_actor_stderr_lands_after_its_stdout():
    """The same guarantee seen through a real Actor run, end to end."""
    code, out, _ = run_worker("item_processor", "--input", '{"count": 40, "fail_at": 40}')
    assert code == 1
    lines = [ln for ln in REGISTRY.get_logs(json.loads(out)["run_id"]).splitlines() if ln.strip()]

    assert all(ln.startswith("[item_processor]") for ln in lines), "a spliced line lost its prefix"
    items = [int(ln.split("item ")[1].split("/")[0]) for ln in lines if " -> sample-item" in ln]
    assert items == list(range(1, 40)), f"progress out of order: {items[:8]}"
    assert lines[-1].startswith("[item_processor] ERROR"), f"stderr overtook stdout: {lines[-1]!r}"
    assert "\r" not in REGISTRY.get_logs(json.loads(out)["run_id"]), "raw CR reached log.txt"


# --- registry visibility ----------------------------------------------------


@test
def test_runs_are_visible_in_the_registry():
    code, out, _ = run_worker("hello_world", "--input", '{"name": "Registry"}')
    assert code == 0
    doc = json.loads(out)

    run = REGISTRY.get(doc["run_id"])
    assert run.actor == "hello_world"
    assert run.status is RunStatus.SUCCEEDED
    # They agree on everything the RECORD carries, and stdout adds exactly one
    # thing: the output, which the record deliberately no longer embeds.
    assert set(doc) - set(run.to_dict()) == {"output"}, sorted(set(doc) - set(run.to_dict()))
    assert {k: v for k, v in doc.items() if k != "output"} == run.to_dict()
    assert doc["output"] == REGISTRY.get_output(run.run_id)

    listed = REGISTRY.list(limit=1)
    assert listed and listed[0].run_id == doc["run_id"], "newest run must list first"

    assert REGISTRY.get_input(run.run_id) == {"name": "Registry"}
    assert REGISTRY.get_output(run.run_id) == {"message": "Hello Registry"}
    assert "[hello_world]" in REGISTRY.get_logs(run.run_id)


@test
def test_failed_runs_are_inspectable_too():
    code, out, _ = run_worker("item_processor", "--input", '{"count": 4, "fail_at": 2}')
    assert code == 1
    doc = json.loads(out)

    run = REGISTRY.get(doc["run_id"])
    assert run.status is RunStatus.FAILED
    assert run.exit_code == 1
    assert run.error, "a failed run must record why"

    # Input and logs survive; output legitimately does not, and says so.
    assert REGISTRY.get_input(run.run_id) == {"count": 4, "fail_at": 2}
    assert "partial progress: 1 of 4" in REGISTRY.get_logs(run.run_id)
    try:
        REGISTRY.get_output(run.run_id)
    except ArtifactNotFound:
        pass
    else:
        raise AssertionError("a failed run must not report an output")


@test
def test_both_actors_ran_through_one_worker():
    """The whole point of the contract: one executor, two unrelated Actors."""
    recent = REGISTRY.list(limit=40)
    actors = {r.actor for r in recent}
    assert {"hello_world", "item_processor"} <= actors, actors

    shapes = {}
    for run in recent:
        if run.status is RunStatus.SUCCEEDED and run.actor not in shapes:
            shapes[run.actor] = type(REGISTRY.get_output(run.run_id)).__name__
    assert shapes.get("hello_world") == "dict", shapes
    assert shapes.get("item_processor") == "list", shapes


def main() -> int:
    failures = 0
    print(f"running {len(TESTS)} end-to-end regression tests\n")

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
    sys.exit(main())
