#!/usr/bin/env python3
"""Invite-only signup (api/invites.py), over HTTP and under concurrency.

    ACTOR_PLATFORM_DATABASE_URL=postgresql://... ACTOR_PLATFORM_SECRET_KEY=... \
        python3 test_invites.py

Needs a THROWAWAY database: it truncates users. No Docker.

What this pins:
  * invite is the DEFAULT, and an unknown mode means invite, never open
  * the first account on an empty install needs no invite -- and only ONE
    person can be that first account, however many race for it
  * an invite works once: two people with the same link cannot both get in
  * an invite is spent only when the account is actually created
  * expired and revoked invites are refused; a used one cannot be revoked away
  * only an admin with the `run` scope can make or revoke one
  * the token is never stored, listed or logged -- only its sha256
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if not (os.environ.get("ACTOR_PLATFORM_DATABASE_URL") or "").strip():
    print("ACTOR_PLATFORM_DATABASE_URL must point at a THROWAWAY database "
          "(this truncates tables)", file=sys.stderr)
    raise SystemExit(2)

os.environ.pop("ACTOR_PLATFORM_SIGNUP", None)  # the default is what is under test

from fastapi.testclient import TestClient  # noqa: E402

from api import invites  # noqa: E402
from api.auth import AuthPolicy, Scope  # noqa: E402
from api.main import create_app  # noqa: E402
from api.users import AccountError, Identity, create_user, mint_api_key  # noqa: E402
from db.pool import pool  # noqa: E402
from jobqueue.file_queue import FileJobQueue  # noqa: E402
from registry.pg_registry import PgRunRegistry  # noqa: E402
from shared.crypto import KeyRing, generate_key  # noqa: E402

TESTS = []
KEYRING = KeyRing.from_env({"ACTOR_PLATFORM_SECRET_KEY": generate_key()})
OPERATOR_KEY = "apk_operator_key_for_invite_tests"
OPERATOR_READ_KEY = "apk_operator_read_key_for_invite_tests"
OPERATOR = {"authorization": f"Bearer {OPERATOR_KEY}"}
OPERATOR_READ = {"authorization": f"Bearer {OPERATOR_READ_KEY}"}
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}
PASSWORD = "correct-horse-battery"


def test(fn):
    TESTS.append(fn)
    return fn


def check(resp, code, label=""):
    assert resp.status_code == code, (
        f"{label}: expected {code}, got {resp.status_code} {resp.text[:300]}")


class World:
    def __init__(self, mode: str | None = None):
        if mode is None:
            os.environ.pop("ACTOR_PLATFORM_SIGNUP", None)
        else:
            os.environ["ACTOR_PLATFORM_SIGNUP"] = mode
        with pool().connection() as conn:
            conn.execute("TRUNCATE users, runs, signup_invites CASCADE")
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.app = create_app(
            registry=PgRunRegistry(tmp / "runs"),
            queue=FileJobQueue(str(tmp / "queue")),
            auth=AuthPolicy.from_env({"ACTOR_PLATFORM_API_KEYS":
                                      f"{OPERATOR_KEY},{OPERATOR_READ_KEY}:read"}),
            identity=Identity(KEYRING),
        )
        self.anon = TestClient(self.app)

    def signup(self, name: str, invite: str | None = None, email: str | None = None):
        body = {"email": email or f"{name}@example.com", "username": name,
                "password": PASSWORD}
        if invite is not None:
            body["invite"] = invite
        return TestClient(self.app).post("/auth/signup", json=body, headers=SAME_ORIGIN)

    def invite(self, headers=OPERATOR, **body) -> str:
        r = self.anon.post("/admin/invites", json=body or None, headers=headers)
        check(r, 201, "creating an invite")
        return r.json()["token"]

    def with_users(self) -> None:
        """Get past the first-account bootstrap."""
        create_user("first@example.com", "first", PASSWORD)


# --- the mode -------------------------------------------------------------------


@test
def test_invite_is_the_default_and_a_typo_never_opens_signup():
    assert invites.signup_mode({}) == "invite"
    assert invites.signup_mode({"ACTOR_PLATFORM_SIGNUP": " OPEN "}) == "open"
    assert invites.signup_mode({"ACTOR_PLATFORM_SIGNUP": "closed"}) == "closed"
    for typo in ("opne", "yes", "1", "public"):
        assert invites.signup_mode({"ACTOR_PLATFORM_SIGNUP": typo}) == "invite", typo


@test
def test_the_first_account_needs_no_invite_and_is_not_an_admin():
    w = World()
    check(w.anon.get("/auth/signup-options"), 200)
    assert w.anon.get("/auth/signup-options").json() == {"signup": "invite",
                                                         "invite_required": False}
    r = w.signup("founder")
    check(r, 201, "first signup")
    assert r.json()["is_admin"] is False
    assert w.anon.get("/auth/signup-options").json()["invite_required"] is True


@test
def test_after_the_first_account_signup_needs_an_invite():
    w = World()
    w.with_users()
    for label, token in (("no invite", None), ("empty", ""), ("made up", "x" * 43)):
        r = w.signup("mallory", token)
        check(r, 403, label)
        assert r.json()["error"] == "sign-up not allowed", r.json()
    with pool().connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM users").fetchone()["n"] == 1


@test
def test_an_invite_works_once():
    w = World()
    w.with_users()
    token = w.invite(note="for sara")
    check(w.signup("sara", token), 201, "using the invite")
    r = w.signup("sara2", token)
    check(r, 403, "reusing it")
    assert "used, revoked or expired" in r.json()["detail"], r.json()
    rows = w.anon.get("/admin/invites", headers=OPERATOR).json()["invites"]
    assert [(i["status"], i["used_by"], i["note"]) for i in rows] == [("used", "sara", "for sara")]


@test
def test_an_invite_is_spent_only_when_the_account_is_created():
    w = World()
    w.with_users()
    token = w.invite()
    r = w.signup("taken", token, email="first@example.com")
    check(r, 400, "email already registered")
    rows = w.anon.get("/admin/invites", headers=OPERATOR).json()["invites"]
    assert rows[0]["status"] == "pending", rows
    check(w.signup("second", token), 201, "the same invite, now with a free email")


@test
def test_two_people_with_one_link_cannot_both_get_in():
    w = World()
    w.with_users()
    token = w.invite()
    outcomes: list[str] = []
    barrier = threading.Barrier(4)

    def attempt(i: int) -> None:
        barrier.wait()
        try:
            invites.signup(f"racer{i}@example.com", f"racer{i}", PASSWORD, token)
            outcomes.append("ok")
        except AccountError:
            outcomes.append("refused")

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(outcomes) == ["ok", "refused", "refused", "refused"], outcomes


@test
def test_only_one_person_can_be_the_first_account():
    World()
    outcomes: list[str] = []
    barrier = threading.Barrier(3)

    def attempt(i: int) -> None:
        barrier.wait()
        try:
            invites.signup(f"early{i}@example.com", f"early{i}", PASSWORD, None)
            outcomes.append("ok")
        except AccountError:
            outcomes.append("refused")

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(outcomes) == ["ok", "refused", "refused"], outcomes


@test
def test_expired_and_revoked_invites_are_refused():
    w = World()
    w.with_users()
    expired, revoked = w.invite(), w.invite()
    with pool().connection() as conn:
        conn.execute("UPDATE signup_invites SET created_at = now() - interval '9 days', "
                     "expires_at = now() - interval '1 second' WHERE token_hash = %s",
                     (hashlib.sha256(expired.encode()).digest(),))
    rows = {i["status"]: i["id"] for i in
            w.anon.get("/admin/invites", headers=OPERATOR).json()["invites"]}
    assert set(rows) == {"expired", "pending"}, rows
    r = w.anon.delete(f"/admin/invites/{rows['pending']}", headers=OPERATOR)
    check(r, 200, "revoking")
    assert r.json()["status"] == "revoked"
    check(w.signup("late", expired), 403, "expired invite")
    check(w.signup("cut", revoked), 403, "revoked invite")


@test
def test_revoking_a_used_invite_changes_nothing():
    w = World()
    w.with_users()
    token = w.invite()
    check(w.signup("kept", token), 201)
    invite_id = w.anon.get("/admin/invites", headers=OPERATOR).json()["invites"][0]["id"]
    r = w.anon.delete(f"/admin/invites/{invite_id}", headers=OPERATOR)
    check(r, 200)
    assert r.json()["status"] == "used", r.json()
    check(w.anon.delete("/admin/invites/not-a-uuid", headers=OPERATOR), 404)
    check(w.anon.delete("/admin/invites/00000000-0000-0000-0000-000000000000",
                        headers=OPERATOR), 404)


@test
def test_invites_expire_after_the_configured_days():
    w = World()
    w.with_users()
    r = w.anon.post("/admin/invites", json={"days": 2}, headers=OPERATOR)
    check(r, 201)
    with pool().connection() as conn:
        days = conn.execute("SELECT extract(epoch FROM expires_at - created_at) / 86400 AS d "
                            "FROM signup_invites").fetchone()["d"]
    assert 1.99 < float(days) < 2.01, days
    check(w.anon.post("/admin/invites", json={"days": 31}, headers=OPERATOR), 422, "too long")
    check(w.anon.post("/admin/invites", json={"token": "mine"}, headers=OPERATOR), 422,
          "a caller cannot choose the token")


@test
def test_only_an_admin_with_the_run_scope_can_make_or_revoke_one():
    w = World()
    alice = create_user("alice@example.com", "alice", PASSWORD)
    root = create_user("root@example.com", "root", PASSWORD, is_admin=True)
    alice_key, _ = mint_api_key(alice.id)
    root_read, _ = mint_api_key(root.id, scopes=frozenset({Scope.READ}))
    root_full, _ = mint_api_key(root.id)
    as_ = lambda key: {"authorization": f"Bearer {key}"}  # noqa: E731

    check(w.anon.get("/admin/invites"), 401, "anonymous list")
    check(w.anon.post("/admin/invites"), 401, "anonymous create")
    check(w.anon.get("/admin/invites", headers=as_(alice_key)), 403, "non-admin list")
    check(w.anon.post("/admin/invites", headers=as_(alice_key)), 403, "non-admin create")
    check(w.anon.post("/admin/invites", headers=as_(root_read)), 403, "admin read key")
    check(w.anon.post("/admin/invites", headers=OPERATOR_READ), 403, "operator read key")
    check(w.anon.get("/admin/invites", headers=as_(root_read)), 200, "admin read key lists")

    r = w.anon.post("/admin/invites", headers=as_(root_full))
    check(r, 201, "admin full key")
    invite_id = r.json()["id"]
    assert r.json()["created_by"] == "root", r.json()
    check(w.anon.delete(f"/admin/invites/{invite_id}", headers=as_(alice_key)), 403)
    check(w.anon.delete(f"/admin/invites/{invite_id}", headers=as_(root_read)), 403)
    r = w.anon.post("/admin/invites", headers=OPERATOR)
    assert r.json()["created_by"] is None, "the operator key is not a user"


@test
def test_the_token_is_never_stored_or_listed():
    w = World()
    w.with_users()
    r = w.anon.post("/admin/invites", json={"note": "n"}, headers=OPERATOR)
    token = r.json()["token"]
    assert len(token) >= 40, token
    listed = w.anon.get("/admin/invites", headers=OPERATOR).text
    assert token not in listed and "token" not in listed, listed
    with pool().connection() as conn:
        row = conn.execute("SELECT * FROM signup_invites").fetchone()
    assert bytes(row["token_hash"]) == hashlib.sha256(token.encode()).digest()
    assert all(token not in str(v) for v in row.values()), "the token is stored"


@test
def test_open_mode_needs_no_invite():
    w = World("open")
    w.with_users()
    check(w.signup("anyone"), 201)
    assert w.anon.get("/auth/signup-options").json() == {"signup": "open",
                                                         "invite_required": False}


@test
def test_closed_mode_refuses_everyone_even_with_an_invite():
    w = World("closed")
    check(w.signup("founder"), 403, "first account, closed")
    w.with_users()
    token = w.invite()
    check(w.signup("invited", token), 403, "invite, closed")
    assert w.anon.get("/auth/signup-options").json() == {"signup": "closed",
                                                         "invite_required": True}
    os.environ.pop("ACTOR_PLATFORM_SIGNUP", None)


def main() -> int:
    print(f"running {len(TESTS)} invite tests\n")
    failed = 0
    for fn in TESTS:
        name = fn.__name__.removeprefix("test_")
        try:
            fn()
            print(f"  ok    {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    os.environ.pop("ACTOR_PLATFORM_SIGNUP", None)
    print(f"\n{'all ' + str(len(TESTS)) + ' tests passed' if not failed else f'{failed} FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
