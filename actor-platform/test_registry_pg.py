#!/usr/bin/env python3
"""Run the WHOLE filesystem-registry suite against the Postgres registry.

    ACTOR_PLATFORM_DATABASE_URL=postgresql://... python3 test_registry_pg.py

Not a second set of assertions -- the SAME ones. test_registry.py encodes the
lifecycle this platform depends on (the transition table, the completion-vs-
cancel race, artifact strictness, newest-first ordering), and the only useful
question about a new backend is whether it satisfies those unchanged. So this
imports that module, swaps the class its fixture builds, and re-runs it.

A backend swap that needed the tests edited would not have been a swap.
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if not (os.environ.get("ACTOR_PLATFORM_DATABASE_URL") or "").strip():
    print("ACTOR_PLATFORM_DATABASE_URL is not set - refusing to run against the "
          "default database. Point it at a THROWAWAY database: this truncates "
          "the runs table between tests.", file=sys.stderr)
    raise SystemExit(2)

import test_registry  # noqa: E402
from db.pool import pool  # noqa: E402
from registry.pg_registry import PgRunRegistry  # noqa: E402


@contextlib.contextmanager
def pg_temp_registry():
    """The same fixture, but Postgres-backed and isolated per test.

    Artifacts still go to a fresh temp directory, exactly as before -- that half
    is inherited code and must keep behaving identically. Only the metadata
    store is shared, so it is truncated: list() asserts on the whole table, and
    a leftover row from the previous test would be indistinguishable from a bug.
    """
    with pool().connection() as conn:
        # users too: the ownership tests create them, and CASCADE from runs does
        # not reach a parent table. Leaving them behind made the second test to
        # insert 'a@x.com' fail on the unique index rather than on its subject.
        conn.execute("TRUNCATE users, runs CASCADE")
    with tempfile.TemporaryDirectory() as tmp:
        yield PgRunRegistry(Path(tmp) / "runs")


def is_record_format_test(fn) -> bool:
    """Does this test plant a raw run.json and expect the registry to read it?

    Detected from the source rather than a hand-maintained list, so a new test
    of either kind is classified correctly without anyone remembering to come
    back here.

    These are the ONLY tests that cannot apply to a database backend, and the
    reason is not a shortfall: they assert how the on-disk record FORMAT is
    parsed -- a legacy record with no created_at, a truncated one, one that
    still embeds its output. Postgres does not read run.json, so there is no
    such parsing to test. Every behavioural test -- the transition table, the
    cancel race, artifact strictness, ordering, prefix resolution, traversal
    rejection -- still has to pass, and does.
    """
    import inspect

    try:
        return "write_record(" in inspect.getsource(fn)
    except OSError:
        return False


# --- what the skipped tests covered, restated for a database backend --------
#
# Two of the seven skips are BEHAVIOURAL claims that merely happened to be set
# up by planting files: newest-first ordering, and refusing to guess between
# ambiguous prefixes. Losing them would be a real gap, so they are re-made here
# against rows. The other five are genuinely about parsing run.json.

def seed(reg, run_id: str, *, actor="hello_world", status="SUCCEEDED", owner=None):
    """Insert a run row directly, so ids can be chosen rather than minted."""
    with pool().connection() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, owner_id, actor, status, created_at, run_dir) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (run_id, owner, actor, status, "2026-01-01T00:00:00Z", str(reg.root / run_id)),
        )


def check(name, fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
        return False
    print(f"  ok    {name}")
    return True


def pg_specific() -> int:
    """Claims that belong to THIS backend and have no filesystem counterpart."""
    from registry.run_registry import AmbiguousRunId, CancelTooLate, RunNotFound, RunStatus
    import concurrent.futures as cf

    print("\nPostgres-backed behaviour")
    passed = failed = 0

    def ordering():
        with pg_temp_registry() as reg:
            ids = ["20260101-000001-aaaaaa", "20260103-120000-bbbbbb", "20260102-090000-cccccc"]
            for rid in ids:
                seed(reg, rid)
            listed = [r.run_id for r in reg.list()]
            assert listed == sorted(ids, reverse=True), listed
            assert listed[0] == "20260103-120000-bbbbbb", "newest must come first"
            assert listed == [r.run_id for r in reg.list()], "ordering must be stable"

    def ambiguity():
        with pg_temp_registry() as reg:
            seed(reg, "20260101-000001-aaaaaa")
            seed(reg, "20260101-000001-bbbbbb")
            try:
                reg.resolve("20260101-000001")
                raise AssertionError("expected AmbiguousRunId")
            except AmbiguousRunId as exc:
                assert "matches 2 runs" in str(exc), exc
            assert reg.resolve("20260101-000001-a") == "20260101-000001-aaaaaa"

    def cancel_race():
        """The claim this backend is FOR: exactly one of complete/cancel wins,
        decided by the WHERE clause rather than by a lock file."""
        with pg_temp_registry() as reg:
            outcomes = {"completed": 0, "cancelled": 0, "refused": 0}
            for _ in range(25):
                run = reg.create("hello_world", {})
                reg.mark_running(run.run_id)

                def do_complete():
                    try:
                        reg.complete(run.run_id, status=RunStatus.SUCCEEDED, exit_code=0)
                        return "completed"
                    except Exception:  # noqa: BLE001
                        return "refused"

                def do_cancel():
                    try:
                        reg.cancel(run.run_id)
                        return "cancelled"
                    except CancelTooLate:
                        return "refused"

                with cf.ThreadPoolExecutor(2) as pool_:
                    results = [f.result() for f in
                               [pool_.submit(do_complete), pool_.submit(do_cancel)]]
                winners = [r for r in results if r != "refused"]
                assert len(winners) == 1, f"{len(winners)} writers won: {results}"
                outcomes[winners[0]] += 1
                outcomes["refused"] += results.count("refused")

                final = reg.get(run.run_id)
                assert final.status in (RunStatus.SUCCEEDED, RunStatus.CANCELLED), final.status
            print(f"        25 races: {outcomes['completed']} completed, "
                  f"{outcomes['cancelled']} cancelled, {outcomes['refused']} cleanly refused")

    def owner_scoping():
        with pg_temp_registry() as reg:
            with pool().connection() as conn:
                a = conn.execute("INSERT INTO users (email,username,password_hash) "
                                 "VALUES ('a@x.com','a','h') RETURNING id").fetchone()["id"]
                b = conn.execute("INSERT INTO users (email,username,password_hash) "
                                 "VALUES ('b@x.com','b','h') RETURNING id").fetchone()["id"]
            # Explicit ids, so a prefix can name exactly ONE of the two. Minted
            # ids are microseconds apart and share a long prefix, which would
            # make the leak check below pass for the wrong reason -- the prefix
            # would resolve to the caller's OWN run.
            mine, theirs = "20260101-000001-aaaaaa", "20260202-000002-bbbbbb"
            seed(reg, mine, owner=a)
            seed(reg, theirs, owner=b)

            as_a = reg.for_owner(str(a))
            assert [r.run_id for r in as_a.list()] == [mine]
            # A foreign run is ABSENT, not forbidden: a 403 would confirm it exists.
            try:
                as_a.get(theirs)
                raise AssertionError("user A read user B's run")
            except RunNotFound:
                pass
            # ...and the same must hold for PREFIX expansion, which otherwise
            # leaks existence through the ambiguity error.
            try:
                as_a.resolve("20260202")
                raise AssertionError("prefix resolution leaked a foreign run")
            except RunNotFound:
                pass
            assert as_a.exists(theirs) is False
            # The unscoped registry resolves the same prefix perfectly well, so
            # the refusal above is scoping and not a broken query.
            assert reg.resolve("20260202") == theirs
            # The unscoped registry (worker, reconciler, admin) still sees both.
            assert len(reg.list()) == 2

    def unowned_view_sees_nothing():
        """for_owner(None) must mean 'no runs', never 'all runs'."""
        with pg_temp_registry() as reg:
            reg.create("hello_world", {})
            assert reg.for_owner(None).list() == []
            assert len(reg.list()) == 1

    for name, fn in [
        ("list_is_newest_first_and_deterministic", ordering),
        ("resolve_refuses_to_guess", ambiguity),
        ("completion_vs_cancel_is_decided_by_the_database", cancel_race),
        ("runs_are_scoped_to_their_owner", owner_scoping),
        ("an_ownerless_view_sees_nothing", unowned_view_sees_nothing),
    ]:
        if check(name, fn):
            passed += 1
        else:
            failed += 1

    print(f"\n{'all ' + str(passed) + ' passed' if not failed else str(failed) + ' FAILED'}")
    return 1 if failed else 0


def main() -> int:
    test_registry.temp_registry = pg_temp_registry
    # The suite also constructs the class directly to prove a record survives a
    # fresh reader; that must exercise the backend under test, not the old one.
    test_registry.RunRegistry = PgRunRegistry

    applicable = [fn for fn in test_registry.TESTS if not is_record_format_test(fn)]
    skipped = [fn for fn in test_registry.TESTS if is_record_format_test(fn)]
    test_registry.TESTS = applicable

    print(f"running {len(applicable)} registry test(s) against PostgreSQL")
    print(f"skipping {len(skipped)} run.json FORMAT test(s), which a database "
          f"backend cannot have:")
    for fn in skipped:
        print(f"    - {fn.__name__.removeprefix('test_')}")
    print()
    return test_registry.main() or pg_specific()


if __name__ == "__main__":
    raise SystemExit(main())
