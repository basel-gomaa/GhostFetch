#!/usr/bin/env python3
"""Deleting runs and users, and retention (security review M-04).

    ACTOR_PLATFORM_DATABASE_URL=postgresql://... ACTOR_PLATFORM_SECRET_KEY=... python3 test_run_deletion.py

Needs a THROWAWAY database: it truncates users and runs. No Docker.

  * an owner can delete a FINISHED run: row, items and directory all go
  * someone else's run is 404, and a live run is 409 (cancel it first)
  * deleting a user removes their runs, files and stored browser profiles
  * disconnecting an account removes its browser profile
  * retention is OFF unless configured; when on, it deletes only old,
    finished, owned runs
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if not (os.environ.get("ACTOR_PLATFORM_DATABASE_URL") or "").strip():
    print("ACTOR_PLATFORM_DATABASE_URL must point at a THROWAWAY database "
          "(this truncates tables)", file=sys.stderr)
    raise SystemExit(2)

STATE = Path(tempfile.mkdtemp(prefix="actor-state-"))
os.environ["ACTOR_PLATFORM_STATE_DIR"] = str(STATE)

# This suite creates its users through /auth/signup and is not about who may
# sign up; invite-only signup (the default) is covered by test_invites.py.
os.environ["ACTOR_PLATFORM_SIGNUP"] = "open"

from fastapi.testclient import TestClient  # noqa: E402

from api.auth import AuthPolicy  # noqa: E402
from api.main import create_app  # noqa: E402
from api.users import Identity  # noqa: E402
from db.pool import pool  # noqa: E402
from jobqueue.file_queue import FileJobQueue  # noqa: E402
from registry.pg_registry import PgRunRegistry  # noqa: E402
from shared.crypto import KeyRing, generate_key  # noqa: E402
from shared.models import RunStatus  # noqa: E402
from worker import consumer  # noqa: E402

TESTS = []
KEYRING = KeyRing.from_env({"ACTOR_PLATFORM_SECRET_KEY": generate_key()})
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}


def test(fn):
    TESTS.append(fn)
    return fn


class Harness:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        with pool().connection() as conn:
            conn.execute("TRUNCATE users, runs, auth_throttle CASCADE")
        self.registry = PgRunRegistry(Path(self._tmp.name) / "runs")
        self.app = create_app(
            registry=self.registry,
            queue=FileJobQueue(str(Path(self._tmp.name) / "queue")),
            auth=AuthPolicy.from_env({"ACTOR_PLATFORM_API_KEYS": "apk_deletion_operator"}),
            identity=Identity(KEYRING),
        )

    def user(self, name: str, admin: bool = False) -> tuple[TestClient, str]:
        c = TestClient(self.app)
        r = c.post("/auth/signup", json={"email": f"{name}@gmail.com", "username": name,
                                         "password": "correct-horse-battery"}, headers=SAME_ORIGIN)
        assert r.status_code == 201, r.text
        uid = r.json()["id"]
        if admin:
            with pool().connection() as conn:
                conn.execute("UPDATE users SET is_admin = true WHERE id = %s", (uid,))
        return c, uid

    def finished_run(self, owner: str, *, days_old: int = 0, status=RunStatus.SUCCEEDED) -> str:
        run = self.registry.create("hello_world", {"name": "x"}, owner_id=owner)
        (self.registry.run_dir(run.run_id) / "output.json").write_text("{}")
        if status is not RunStatus.PENDING:
            self.registry.mark_running(run.run_id)
            if status is not RunStatus.RUNNING:
                self.registry.complete(run.run_id, status=status, exit_code=0)
        if days_old:
            with pool().connection() as conn:
                conn.execute("UPDATE runs SET finished_at = now() - make_interval(days => %s) "
                             "WHERE run_id = %s", (days_old, run.run_id))
        with pool().connection() as conn:
            conn.execute("INSERT INTO run_items (run_id, seq, collection, data) "
                         "VALUES (%s, 1, 'default', '{}'::jsonb)", (run.run_id,))
        return run.run_id

    def exists(self, run_id: str) -> bool:
        with pool().connection() as conn:
            return conn.execute("SELECT 1 AS x FROM runs WHERE run_id = %s",
                                (run_id,)).fetchone() is not None


@test
def test_an_owner_deletes_a_finished_run_completely():
    h = Harness()
    c, uid = h.user("basel")
    run_id = h.finished_run(uid)
    directory = h.registry.run_dir(run_id)
    assert directory.is_dir()
    r = c.delete(f"/runs/{run_id}", headers=SAME_ORIGIN)
    assert r.status_code == 204, r.text
    assert not h.exists(run_id), "the row survived"
    assert not directory.exists(), "the directory survived"
    with pool().connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM run_items WHERE run_id = %s",
                            (run_id,)).fetchone()["n"] == 0, "items survived"
    assert c.get(f"/runs/{run_id}").status_code == 404


@test
def test_someone_elses_run_is_404_and_untouched():
    h = Harness()
    _, alice = h.user("alice")
    bob, _ = h.user("bob")
    run_id = h.finished_run(alice)
    assert bob.delete(f"/runs/{run_id}", headers=SAME_ORIGIN).status_code == 404
    assert h.exists(run_id) and h.registry.run_dir(run_id).is_dir()


@test
def test_a_live_run_is_refused_until_cancelled():
    h = Harness()
    c, uid = h.user("basel")
    run_id = h.finished_run(uid, status=RunStatus.RUNNING)
    r = c.delete(f"/runs/{run_id}", headers=SAME_ORIGIN)
    assert r.status_code == 409, r.text
    assert h.exists(run_id)
    c.post(f"/runs/{run_id}/cancel", headers=SAME_ORIGIN)
    assert c.delete(f"/runs/{run_id}", headers=SAME_ORIGIN).status_code == 204


@test
def test_a_cross_site_delete_is_refused():
    h = Harness()
    c, uid = h.user("basel")
    run_id = h.finished_run(uid)
    r = c.delete(f"/runs/{run_id}", headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403, r.text
    assert h.exists(run_id)


@test
def test_deleting_a_user_removes_everything_that_was_theirs():
    h = Harness()
    admin, _ = h.user("boss", admin=True)
    _, uid = h.user("basel")
    runs = [h.finished_run(uid) for _ in range(2)]
    with pool().connection() as conn:
        account = conn.execute(
            "INSERT INTO social_accounts (user_id, platform, status, session_ciphertext, "
            "session_nonce) VALUES (%s, 'tiktok', 'connected', '\\x00'::bytea, "
            "'\\x00'::bytea) RETURNING id", (uid,)
        ).fetchone()["id"]
    profile = STATE / "tiktok" / str(account)
    profile.mkdir(parents=True)
    (profile / "Cookies").write_text("secret")

    r = admin.delete(f"/admin/users/{uid}", headers=SAME_ORIGIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["runs_deleted"] == 2 and body["browser_profiles_removed"] == 1, body
    assert not any(h.exists(x) for x in runs), "runs survived their owner"
    assert not profile.exists(), "a logged-in browser profile survived its owner"
    with pool().connection() as conn:
        assert conn.execute("SELECT 1 AS x FROM users WHERE id = %s", (uid,)).fetchone() is None


@test
def test_an_admin_must_be_demoted_before_deletion_and_cannot_delete_themselves():
    h = Harness()
    admin, admin_id = h.user("boss", admin=True)
    _, other = h.user("deputy", admin=True)
    assert admin.delete(f"/admin/users/{admin_id}", headers=SAME_ORIGIN).status_code == 409
    assert admin.delete(f"/admin/users/{other}", headers=SAME_ORIGIN).status_code == 409


@test
def test_disconnecting_an_account_removes_its_browser_profile():
    h = Harness()
    c, uid = h.user("basel")
    with pool().connection() as conn:
        account = conn.execute(
            "INSERT INTO social_accounts (user_id, platform, status, session_ciphertext, "
            "session_nonce) VALUES (%s, 'tiktok', 'connected', '\\x00'::bytea, "
            "'\\x00'::bytea) RETURNING id", (uid,)
        ).fetchone()["id"]
    profile = STATE / "tiktok" / str(account)
    profile.mkdir(parents=True)
    assert c.delete("/social/accounts/tiktok", headers=SAME_ORIGIN).status_code == 204
    assert not profile.exists(), "disconnect left the logged-in profile on disk"


@test
def test_retention_is_off_unless_configured_and_spares_what_it_should():
    h = Harness()
    _, uid = h.user("basel")
    old = h.finished_run(uid, days_old=40)
    recent = h.finished_run(uid, days_old=5)
    live = h.finished_run(uid, status=RunStatus.RUNNING)
    ownerless = h.registry.create("hello_world", {"name": "x"})
    h.registry.mark_running(ownerless.run_id)
    h.registry.complete(ownerless.run_id, status=RunStatus.SUCCEEDED, exit_code=0)
    with pool().connection() as conn:
        conn.execute("UPDATE runs SET finished_at = now() - interval '400 days' "
                     "WHERE run_id = %s", (ownerless.run_id,))

    os.environ.pop("ACTOR_PLATFORM_RUN_RETENTION_DAYS", None)
    assert consumer.retention_sweep(h.registry) == 0, "retention ran while switched off"
    os.environ["ACTOR_PLATFORM_RUN_RETENTION_DAYS"] = "30"
    try:
        assert consumer.retention_sweep(h.registry) == 1
    finally:
        os.environ.pop("ACTOR_PLATFORM_RUN_RETENTION_DAYS", None)
    assert not h.exists(old), "the old run survived"
    assert h.exists(recent) and h.exists(live), "retention removed too much"
    assert h.exists(ownerless.run_id), "an ownerless run was removed without opting in"


def main() -> int:
    failed = 0
    print(f"running {len(TESTS)} deletion tests\n")
    for fn in TESTS:
        name = fn.__name__.removeprefix("test_")
        try:
            fn()
            print(f"  ok    {name}")
        except Exception as exc:  # noqa: BLE001
            import traceback
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{failed} FAILED" if failed else "\nall passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
