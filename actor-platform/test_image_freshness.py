#!/usr/bin/env python3
"""Images are rebuilt when their build context changes, offline.

    python3 test_image_freshness.py

No Docker, no database. What is pinned:
  * the context hash is stable, sees every change that can reach an image (a
    byte, the executable bit, a symlink target, a file added, removed or
    renamed) and ignores __pycache__ / .pyc, which test runs churn;
  * ensure_image rebuilds a missing, unlabelled or changed image and reuses a
    current one without building;
  * a failed REBUILD with an old image present runs the old image and says so
    -- in the worker log, as an event, and at the top of the run's own log --
    while a failed first build still fails the run;
  * the login image follows the same rule;
  * run-local.sh uses the same label and the same hash definition.

The live half -- a real image labelled with the real hash, an edit rebuilding
it -- needs Docker and is in test_concurrency.py.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared import image_freshness as freshness  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def tree() -> Path:
    root = Path(tempfile.mkdtemp())
    (root / "vendor" / "collectors").mkdir(parents=True)
    (root / "Dockerfile").write_text("FROM scratch\n")
    (root / "actor.py").write_text("print('hi')\n")
    (root / "entrypoint.sh").write_text("#!/bin/sh\n")
    (root / "vendor" / "collectors" / "x.py").write_text("X = 1\n")
    return root


@test
def test_the_hash_is_stable_and_sees_every_change_that_reaches_an_image():
    root = tree()
    base = freshness.context_hash(root)
    assert base.startswith("v1:") and base == freshness.context_hash(root)

    def changed(mutate, undo) -> bool:
        mutate()
        try:
            return freshness.context_hash(root) != base
        finally:
            undo()
            assert freshness.context_hash(root) == base, "undo did not restore the tree"

    f = root / "vendor" / "collectors" / "x.py"
    assert changed(lambda: f.write_text("X = 2\n"), lambda: f.write_text("X = 1\n")), "a byte"
    e = root / "entrypoint.sh"
    assert changed(lambda: e.chmod(0o755), lambda: e.chmod(0o644)), "the executable bit"
    new = root / "vendor" / "collectors" / "y.py"
    assert changed(lambda: new.write_text(""), new.unlink), "an added file"
    assert changed(lambda: f.rename(new), lambda: new.rename(f)), "a rename"
    saved = f.read_text()
    assert changed(f.unlink, lambda: f.write_text(saved)), "a removed file"
    link = root / "link"
    assert changed(lambda: link.symlink_to("actor.py"), link.unlink), "an added symlink"
    link.symlink_to("actor.py")
    base = freshness.context_hash(root)
    assert changed(lambda: (link.unlink(), link.symlink_to("Dockerfile")),
                   lambda: (link.unlink(), link.symlink_to("actor.py"))), "a symlink target"


@test
def test_the_hash_ignores_bytecode_that_test_runs_churn():
    root = tree()
    base = freshness.context_hash(root)
    for d in (root / "__pycache__", root / "vendor" / "collectors" / "__pycache__"):
        d.mkdir()
        (d / "actor.cpython-313.pyc").write_bytes(os.urandom(64))
    (root / "stray.pyc").write_bytes(os.urandom(8))
    assert freshness.context_hash(root) == base


@test
def test_the_hash_is_independent_of_where_the_context_lives():
    a, b = tree(), tree()
    assert freshness.context_hash(a) == freshness.context_hash(b)


# --- the worker's decision ------------------------------------------------


class Fakes:
    """ensure_image with Docker replaced: a staleness answer and a build result."""

    def __init__(self, reason, exists, build_ok=True):
        from worker import executor
        self.ex = executor
        self.reason, self.exists, self.build_ok = reason, exists, build_ok
        self.builds, self.events = [], []

    def __enter__(self):
        ex = self.ex
        self.saved = ex.image_staleness, ex.build_image, ex.event

        def build(actor, context_hash=None):
            self.builds.append(context_hash)
            if not self.build_ok:
                raise ex.WorkerError(f"docker build failed for actor '{actor}' (exit 1)")
            self.reason, self.exists = None, True

        ex.image_staleness = lambda actor: ("v1:now", self.reason, self.exists)
        ex.build_image = build
        ex.event = lambda *a, **k: self.events.append((a, k))
        return self

    def __exit__(self, *exc):
        self.ex.image_staleness, self.ex.build_image, self.ex.event = self.saved
        self.ex._BUILD_LOCKS.pop("hello_world", None)


@test
def test_a_current_image_is_reused_and_nothing_is_built():
    with Fakes(None, True) as f:
        assert f.ex.ensure_image("hello_world", force_build=False) is None
        assert f.builds == []


@test
def test_a_missing_unlabelled_or_changed_image_is_built_with_the_current_hash():
    for reason, exists in ((freshness.MISSING, False), (freshness.UNLABELLED, True),
                           (freshness.CHANGED, True)):
        with Fakes(reason, exists) as f:
            assert f.ex.ensure_image("hello_world", force_build=False) is None
            assert f.builds == ["v1:now"], (reason, f.builds)


@test
def test_force_build_rebuilds_a_current_image():
    with Fakes(None, True) as f:
        f.ex.ensure_image("hello_world", force_build=True)
        assert f.builds == ["v1:now"]


@test
def test_a_failed_rebuild_runs_the_old_image_and_says_so():
    with Fakes(freshness.CHANGED, True, build_ok=False) as f:
        note = f.ex.ensure_image("hello_world", force_build=False)
        assert note and "older sources" in note and "context changed" in note, note
        assert "exit 1" in note, note
        assert [a for a, _ in f.events] == [("worker", "stale_image")], f.events
        assert f.events[0][1]["reason"] == freshness.CHANGED


@test
def test_a_failed_first_build_still_fails():
    with Fakes(freshness.MISSING, False, build_ok=False) as f:
        try:
            f.ex.ensure_image("hello_world", force_build=False)
        except f.ex.WorkerError:
            pass
        else:
            raise AssertionError("a run started with no image at all")
        assert f.events == []


@test
def test_a_failed_forced_build_of_a_current_image_still_fails():
    """--build on an up-to-date image: there is nothing stale to warn about, so
    a failed build is an error, as it always was -- not an "older sources" note."""
    with Fakes(None, True, build_ok=False) as f:
        try:
            f.ex.ensure_image("hello_world", force_build=True)
        except f.ex.WorkerError:
            pass
        else:
            raise AssertionError("a failed --build was swallowed")
        assert f.events == []


@test
def test_preflight_hands_the_note_to_the_run_and_nothing_otherwise():
    from worker import executor as ex
    saved = ex.require_docker, ex.ensure_image
    ex.require_docker = lambda: None
    try:
        ex.ensure_image = lambda actor, force_build: None
        assert ex._preflight("hello_world", force_build=False) == []
        ex.ensure_image = lambda actor, force_build: "[platform] WARNING: stale"
        assert ex._preflight("hello_world", force_build=False) == ["[platform] WARNING: stale"]
    finally:
        ex.require_docker, ex.ensure_image = saved


@test
def test_the_note_is_the_first_line_of_the_runs_stored_log():
    """Through the REAL _run_prepared, with only Docker replaced."""
    from registry.run_registry import RunRegistry
    from worker import executor as ex
    from worker.limits import ExecutionLimits

    reg = RunRegistry(Path(tempfile.mkdtemp()) / "runs")
    run = reg.create("hello_world", {"name": "x"})
    saved = ex._execute, ex.ensure_actor_network, ex.container_memory_mb
    ex._execute = lambda command, name, **kw: (0, ["actor line 1", "actor line 2"])
    ex.ensure_actor_network = lambda: "ap-actor-net"
    ex.container_memory_mb = lambda run_id: None
    try:
        ex._run_prepared(run, registry=reg, limits=ExecutionLimits(),
                         platform_notes=["[platform] WARNING: stale"])
        stored = reg.get_logs(run.run_id).splitlines()
        assert stored[:3] == ["[platform] WARNING: stale", "actor line 1", "actor line 2"], stored

        plain = reg.create("hello_world", {"name": "y"})
        ex._run_prepared(plain, registry=reg, limits=ExecutionLimits())
        assert reg.get_logs(plain.run_id).splitlines()[:2] == ["actor line 1", "actor line 2"]
    finally:
        ex._execute, ex.ensure_actor_network, ex.container_memory_mb = saved


@test
def test_every_build_in_the_platform_carries_the_label():
    """A `docker build` that forgets the label makes an image that reads as
    stale for ever -- one rebuild per run."""
    import re
    for rel in ("worker/executor.py",):
        text = (REPO_ROOT / rel).read_text()
        for call in re.findall(r'\["docker", "build"[^\]]*\]', text):
            assert "build_args" in call, f"{rel}: {call}"


def main() -> int:
    os.environ.pop("ACTOR_LOG_FORMAT", None)
    print(f"running {len(TESTS)} image-freshness tests\n")
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"  ok    {fn.__name__.removeprefix('test_')}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {fn.__name__.removeprefix('test_')}: {type(exc).__name__}: {exc}")
    print(f"\n{'all ' + str(len(TESTS)) + ' tests passed' if not failed else f'{failed} FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
