#!/usr/bin/env python3
"""Per-run limits and fair-use quotas, over HTTP (security review H-03, M-09).

    ACTOR_PLATFORM_DATABASE_URL=postgresql://... ACTOR_PLATFORM_SECRET_KEY=... python3 test_quotas.py

Needs a THROWAWAY database: it truncates users and runs. No Docker -- runs are
queued and never executed, which is exactly the state a quota counts.

  * GET /actors offers each Actor's limits (default and max) to clients
  * a run gets its Actor's defaults unless it asks, and the record shows them
  * asking above an Actor's maximum is REFUSED (400), never silently lowered
  * a user with too many runs in flight is refused (429); an admin is not
  * a full queue is refused (503), whoever asks
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if not (os.environ.get("ACTOR_PLATFORM_DATABASE_URL") or "").strip():
    print("ACTOR_PLATFORM_DATABASE_URL must point at a THROWAWAY database "
          "(this truncates tables)", file=sys.stderr)
    raise SystemExit(2)

# This suite creates its users through /auth/signup and is not about who may
# sign up; invite-only signup (the default) is covered by test_invites.py.
os.environ["ACTOR_PLATFORM_SIGNUP"] = "open"

from fastapi.testclient import TestClient  # noqa: E402

import api.main as api_main  # noqa: E402
from api.auth import AuthPolicy  # noqa: E402
from api.main import create_app  # noqa: E402
from api.users import Identity  # noqa: E402
from db.pool import pool  # noqa: E402
from jobqueue.file_queue import FileJobQueue  # noqa: E402
from registry.pg_registry import PgRunRegistry  # noqa: E402
from shared.crypto import KeyRing, generate_key  # noqa: E402
from worker.limits import WorkerConfig  # noqa: E402

TESTS = []
KEYRING = KeyRing.from_env({"ACTOR_PLATFORM_SECRET_KEY": generate_key()})
OPERATOR_KEY = "apk_operator_key_for_quota_tests"
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}
ACTOR = "hello_world"


def test(fn):
    TESTS.append(fn)
    return fn


class Harness:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        with pool().connection() as conn:
            conn.execute("TRUNCATE users, runs CASCADE")
        self.registry = PgRunRegistry(Path(self._tmp.name) / "runs")
        self.queue = FileJobQueue(str(Path(self._tmp.name) / "queue"))
        self.app = create_app(
            registry=self.registry,
            queue=self.queue,
            auth=AuthPolicy.from_env({"ACTOR_PLATFORM_API_KEYS": OPERATOR_KEY}),
            identity=Identity(KEYRING),
        )

    def user(self, name: str) -> tuple[TestClient, str]:
        c = TestClient(self.app)
        r = c.post("/auth/signup", json={"email": f"{name}@gmail.com", "username": name,
                                         "password": "correct-horse-battery"}, headers=SAME_ORIGIN)
        assert r.status_code == 201, r.text
        return c, r.json()["id"]

    def operator(self) -> TestClient:
        return TestClient(self.app, headers={"Authorization": f"Bearer {OPERATOR_KEY}"})


def start(client: TestClient, query: str = "") -> object:
    return client.post(f"/actors/{ACTOR}/runs{query}", json={"name": "quota"}, headers=SAME_ORIGIN)


class caps:
    """Temporarily set the API's quota constants (read at call time)."""

    def __init__(self, per_user: int, depth: int):
        self.values = (per_user, depth)

    def __enter__(self):
        self.saved = (api_main.MAX_ACTIVE_RUNS_PER_USER, api_main.MAX_QUEUE_DEPTH)
        api_main.MAX_ACTIVE_RUNS_PER_USER, api_main.MAX_QUEUE_DEPTH = self.values

    def __exit__(self, *exc):
        api_main.MAX_ACTIVE_RUNS_PER_USER, api_main.MAX_QUEUE_DEPTH = self.saved


# --- limits -----------------------------------------------------------------


@test
def test_actors_offer_their_limits():
    h = Harness()
    c, _ = h.user("basel")
    actors = {a["name"]: a for a in c.get("/actors").json()["actors"]}
    configured = WorkerConfig.load().limits_for(ACTOR).limits
    got = actors[ACTOR]["limits"]
    assert got["timeout_minutes"] == {"default": configured.timeout_minutes,
                                      "max": configured.max_timeout_minutes}, got
    assert got["memory_mb"]["max"] == configured.max_memory_mb, got
    # Every scraper the console offers has a section of its own.
    for name in ("facebook", "facebook_groups", "instagram", "linkedin", "threads", "tiktok", "x"):
        if name in actors:
            assert actors[name]["limits"]["timeout_minutes"]["default"], name


@test
def test_a_run_gets_its_actors_defaults_and_the_record_shows_them():
    h = Harness()
    c, _ = h.user("basel")
    r = start(c)
    assert r.status_code == 202, r.text
    detail = c.get(f"/runs/{r.json()['run_id']}").json()
    configured = WorkerConfig.load().limits_for(ACTOR).limits
    assert detail["timeout_s"] == configured.timeout_minutes * 60, detail
    assert detail["memory_mb"] == configured.memory_mb, detail


@test
def test_a_run_may_ask_for_other_limits_within_the_maximum():
    h = Harness()
    c, _ = h.user("basel")
    configured = WorkerConfig.load().limits_for(ACTOR).limits
    r = start(c, f"?timeout_minutes={configured.max_timeout_minutes}&memory_mb=256&cpus=0.5")
    assert r.status_code == 202, r.text
    detail = c.get(f"/runs/{r.json()['run_id']}").json()
    assert (detail["timeout_s"], detail["memory_mb"], detail["cpus"]) == (
        configured.max_timeout_minutes * 60, 256, 0.5), detail


@test
def test_asking_above_the_maximum_is_refused_not_lowered():
    h = Harness()
    c, _ = h.user("basel")
    configured = WorkerConfig.load().limits_for(ACTOR).limits
    for query in (f"?timeout_minutes={configured.max_timeout_minutes + 1}",
                  f"?memory_mb={configured.max_memory_mb + 1}",
                  "?memory_mb=10", "?timeout_minutes=0", "?cpus=99"):
        r = start(c, query)
        assert r.status_code == 400, f"{query}: {r.status_code} {r.text}"
        assert r.json()["error"] == "invalid run options", r.text
    with pool().connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM runs").fetchone()["n"] == 0, \
            "a refused request still created a run"


# --- quotas -----------------------------------------------------------------


@test
def test_a_user_with_too_many_active_runs_is_refused():
    h = Harness()
    c, _ = h.user("basel")
    other, _ = h.user("nasr")
    with caps(per_user=2, depth=0):
        assert start(c).status_code == 202
        assert start(c).status_code == 202
        r = start(c)
        assert r.status_code == 429, r.text
        assert "2 runs queued or running" in r.json()["detail"], r.text
        # One user's backlog never blocks another's.
        assert start(other).status_code == 202


@test
def test_a_finished_run_frees_its_slot():
    h = Harness()
    c, _ = h.user("basel")
    with caps(per_user=1, depth=0):
        first = start(c).json()["run_id"]
        assert start(c).status_code == 429
        c.post(f"/runs/{first}/cancel", headers=SAME_ORIGIN)
        assert start(c).status_code == 202, "a cancelled run still counted against the quota"


@test
def test_admins_and_operator_keys_are_exempt():
    h = Harness()
    c, uid = h.user("basel")
    with pool().connection() as conn:
        conn.execute("UPDATE users SET is_admin = true WHERE id = %s", (uid,))
    op = h.operator()
    with caps(per_user=1, depth=0):
        for _ in range(3):
            assert start(c).status_code == 202, "an admin was held to the user quota"
            assert start(op).status_code == 202, "an operator key was held to the user quota"


@test
def test_a_full_queue_is_refused_for_everyone():
    h = Harness()
    op = h.operator()
    with caps(per_user=0, depth=2):
        assert start(op).status_code == 202
        assert start(op).status_code == 202
        r = start(op)
        assert r.status_code == 503, r.text
        assert r.json()["error"] == "queue full", r.text


def main() -> int:
    failed = 0
    print(f"running {len(TESTS)} quota tests\n")
    for fn in TESTS:
        name = fn.__name__.removeprefix("test_")
        try:
            fn()
            print(f"  ok    {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{failed} FAILED" if failed else "\nall passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
