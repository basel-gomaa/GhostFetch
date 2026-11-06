"""User administration: promote, demote, disable, enable -- and who may.

    ACTOR_PLATFORM_DATABASE_URL=postgresql://... \\
    ACTOR_PLATFORM_SECRET_KEY=... python3 test_admin_users.py

Needs a THROWAWAY database: it truncates users and runs between tests. No
Docker -- a live login browser is a row, and runs are seeded straight into the
registry.

What is asserted is the set of properties that make this safe to have at all:

  * only an administrator reaches any of it, and a read-scoped admin key can
    look but not change -- admin widens WHOSE data, never WHICH actions
  * a change takes effect on the target's NEXT request, with no re-login
  * disabling stops everything acting as the user, and keeps their history
  * re-enabling restores their keys but not the sessions that were revoked
  * nobody can lock the platform out of its own administration
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if not (os.environ.get("ACTOR_PLATFORM_DATABASE_URL") or "").strip():
    print("ACTOR_PLATFORM_DATABASE_URL must point at a THROWAWAY database "
          "(this truncates tables)", file=sys.stderr)
    raise SystemExit(2)

from fastapi.testclient import TestClient  # noqa: E402

from api.auth import AuthPolicy, Scope  # noqa: E402
from api.main import create_app  # noqa: E402
from api.users import Identity, create_user, mint_api_key  # noqa: E402
from db.pool import pool  # noqa: E402
from jobqueue.file_queue import FileJobQueue  # noqa: E402
from registry.pg_registry import PgRunRegistry  # noqa: E402
from shared.crypto import KeyRing, generate_key  # noqa: E402

TESTS = []
KEYRING = KeyRing.from_env({"ACTOR_PLATFORM_SECRET_KEY": generate_key()})
OPERATOR_KEY = "apk_operator_key_for_admin_tests"
OPERATOR = {"authorization": f"Bearer {OPERATOR_KEY}"}
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}
PASSWORD = "correct-horse-battery"
ROUTES_WRITE = ("promote", "demote", "disable", "enable")


def test(fn):
    TESTS.append(fn)
    return fn


def check(resp, code, label=""):
    assert resp.status_code == code, (
        f"{label}: expected {code}, got {resp.status_code} {resp.text[:300]}")


class User:
    def __init__(self, app, username: str, *, admin: bool = False):
        record = create_user(f"{username}@example.com", username, PASSWORD, is_admin=admin)
        self.id = record.id
        self.username = username
        self.app = app
        self.cookie = self.sign_in()
        read_key, _ = mint_api_key(self.id, scopes=frozenset({Scope.READ}))
        full_key, _ = mint_api_key(self.id)
        self.read = {"authorization": f"Bearer {read_key}"}
        self.full = {"authorization": f"Bearer {full_key}"}
        self.bearer = TestClient(app)

    def sign_in(self) -> TestClient:
        client = TestClient(self.app)
        r = client.post("/auth/login", json={"identifier": self.username,
                                             "password": PASSWORD}, headers=SAME_ORIGIN)
        check(r, 200, f"{self.username} signing in")
        return client


class World:
    def __init__(self):
        with pool().connection() as conn:
            conn.execute("TRUNCATE users, runs CASCADE")
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.registry = PgRunRegistry(tmp / "runs")
        self.app = create_app(
            registry=self.registry,
            queue=FileJobQueue(str(tmp / "queue")),
            auth=AuthPolicy.from_env({"ACTOR_PLATFORM_API_KEYS": OPERATOR_KEY}),
            identity=Identity(KEYRING),
        )
        self.anon = TestClient(self.app)
        self.root = User(self.app, "root", admin=True)
        self.alice = User(self.app, "alice")
        self.bob = User(self.app, "bob")

    def run_for(self, user: User, status: str) -> str:
        run = self.registry.create("hello_world", {"name": "x"}, owner_id=user.id)
        if status != "PENDING":
            with pool().connection() as conn:
                conn.execute("UPDATE runs SET status = %s, started_at = now() "
                             "WHERE run_id = %s", (status, run.run_id))
        return run.run_id

    def status(self, run_id: str) -> str:
        return self.registry.get(run_id).status.value

    def admin_post(self, user_id: str, action: str, *, client=None, headers=None):
        client = client or self.root.cookie
        return client.post(f"/admin/users/{user_id}/{action}",
                           headers=headers if headers is not None else SAME_ORIGIN)


# --- who may --------------------------------------------------------------------


@test
def test_nobody_but_an_administrator_reaches_any_admin_route():
    w = World()
    for label, client, headers in (("alice cookie", w.alice.cookie, SAME_ORIGIN),
                                   ("alice full key", w.alice.bearer, w.alice.full)):
        check(client.get("/admin/users", headers=headers), 403, f"{label} listing")
        check(client.get(f"/admin/users/{w.bob.id}", headers=headers), 403, f"{label} viewing")
        for action in ROUTES_WRITE:
            r = client.post(f"/admin/users/{w.bob.id}/{action}", headers=headers)
            check(r, 403, f"{label} {action}")
            assert r.json()["error"] == "admin only", r.json()
    # Not even on themselves: promoting yourself is the obvious attempt.
    check(w.alice.cookie.post(f"/admin/users/{w.alice.id}/promote", headers=SAME_ORIGIN),
          403, "alice promoting herself")
    for path in ("/admin/users", f"/admin/users/{w.bob.id}"):
        check(w.anon.get(path), 401, f"anonymous {path}")
    check(w.anon.post(f"/admin/users/{w.bob.id}/promote"), 401)
    assert not w.alice.cookie.get("/auth/me").json()["is_admin"]


@test
def test_an_admins_read_key_can_look_but_not_change():
    """Admin widens WHOSE data, never WHICH actions -- the PR #17 rule."""
    w = World()
    check(w.root.bearer.get("/admin/users", headers=w.root.read), 200)
    check(w.root.bearer.get(f"/admin/users/{w.bob.id}", headers=w.root.read), 200)
    for action in ROUTES_WRITE:
        r = w.root.bearer.post(f"/admin/users/{w.bob.id}/{action}", headers=w.root.read)
        check(r, 403, f"a read-scoped admin key: {action}")
        assert r.json()["error"] == "insufficient scope", r.json()


@test
def test_the_operator_key_bootstraps_the_first_admin():
    w = World()
    r = w.anon.post(f"/admin/users/{w.alice.id}/promote", headers=OPERATOR)
    check(r, 200, "operator promoting alice")
    assert r.json()["user"]["is_admin"] is True and r.json()["already"] is False


@test
def test_a_cross_site_cookie_write_is_refused():
    w = World()
    r = w.root.cookie.post(f"/admin/users/{w.bob.id}/disable",
                           headers={"sec-fetch-site": "cross-site"})
    check(r, 403, "a cross-site disable")
    check(w.bob.cookie.get("/auth/me"), 200, "bob still signed in")


# --- reading ---------------------------------------------------------------------


@test
def test_listing_and_viewing_users():
    w = World()
    body = w.root.cookie.get("/admin/users").json()
    assert {u["username"] for u in body["users"]} == {"root", "alice", "bob"}, body
    assert body["count"] == 3
    assert [u["username"] for u in w.root.cookie.get("/admin/users?q=ALI").json()["users"]] \
        == ["alice"], "search should be case-insensitive"
    # A LIKE wildcard the admin types is a character, not a pattern.
    assert w.root.cookie.get("/admin/users?q=%25").json()["users"] == []
    check(w.root.cookie.get("/admin/users?limit=0"), 422)

    w.run_for(w.bob, "SUCCEEDED")
    detail = w.root.cookie.get(f"/admin/users/{w.bob.id}").json()
    assert detail["username"] == "bob" and detail["runs"] == 1, detail
    assert detail["api_keys"] == 2 and detail["live_sessions"] == 1, detail
    assert "password" not in str(detail).lower(), "the hash must never be served"


@test
def test_a_bad_or_unknown_user_id_is_404_never_500():
    w = World()
    for bad in ("not-a-uuid", "'%3B--", str(uuid.uuid4())):
        check(w.root.cookie.get(f"/admin/users/{bad}"), 404, f"viewing {bad!r}")
        for action in ROUTES_WRITE:
            check(w.admin_post(bad, action), 404, f"{action} {bad!r}")


# --- promote / demote --------------------------------------------------------------


@test
def test_promotion_and_demotion_take_effect_on_the_next_request():
    """No re-login: every request rebuilds the caller from the database."""
    w = World()
    bobs = w.run_for(w.bob, "SUCCEEDED")
    check(w.alice.cookie.get(f"/runs/{bobs}"), 404, "before: bob's run is not hers")

    check(w.admin_post(w.alice.id, "promote"), 200)
    # The SAME cookie and the SAME key she already held.
    check(w.alice.cookie.get(f"/runs/{bobs}"), 200, "after promote, by cookie")
    check(w.alice.bearer.get(f"/runs/{bobs}", headers=w.alice.full), 200, "after, by key")
    assert w.alice.cookie.get("/auth/me").json()["is_admin"] is True
    check(w.alice.cookie.get("/admin/users"), 200, "she can now use the admin API")

    again = w.admin_post(w.alice.id, "promote")
    check(again, 200)
    assert again.json()["already"] is True, "a repeated promote is idempotent"

    check(w.admin_post(w.alice.id, "demote"), 200)
    check(w.alice.cookie.get(f"/runs/{bobs}"), 404, "after demote")
    check(w.alice.cookie.get("/admin/users"), 403, "admin API gone after demote")


# --- disable / enable ----------------------------------------------------------------


@test
def test_disabling_stops_everything_acting_as_the_user_and_keeps_their_history():
    w = World()
    pending = w.run_for(w.bob, "PENDING")
    running = w.run_for(w.bob, "RUNNING")
    done = w.run_for(w.bob, "SUCCEEDED")
    alices = w.run_for(w.alice, "PENDING")
    with pool().connection() as conn:
        conn.execute(
            "INSERT INTO social_accounts (user_id, platform, session_ciphertext, "
            "session_nonce, key_version, status) VALUES (%s,'linkedin','\\x00','\\x00',1,"
            "'connected')", (w.bob.id,))

    r = w.admin_post(w.bob.id, "disable")
    check(r, 200, "disabling bob")
    body = r.json()
    assert body["user"]["disabled"] is True and body["already"] is False, body
    assert body["runs_cancelled"] == 2, body
    # Demo build: there is no login browser to stop.
    assert body["login_browsers_stopped"] + body["login_browsers_unconfirmed"] == 0, body

    # Every credential he holds stops working on the very next request.
    check(w.bob.cookie.get("/runs"), 401, "bob's cookie")
    check(w.bob.bearer.get("/runs", headers=w.bob.full), 401, "bob's full key")
    check(w.bob.bearer.get("/runs", headers=w.bob.read), 401, "bob's read key")
    r = w.anon.post("/auth/login", json={"identifier": "bob", "password": PASSWORD},
                    headers=SAME_ORIGIN)
    check(r, 401, "bob signing in again")

    # Live work stopped; finished work, other users' work and his data kept.
    assert w.status(pending) == "CANCELLED" and w.status(running) == "CANCELLED"
    assert "administrator" in (w.registry.get(pending).error or "")
    assert w.status(done) == "SUCCEEDED", "a finished run must be left alone"
    assert w.status(alices) == "PENDING", "another user's run must be left alone"
    with pool().connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM social_accounts WHERE user_id=%s",
                            (w.bob.id,)).fetchone()["n"] == 1, "connected account kept"
        assert conn.execute("SELECT count(*) AS n FROM runs WHERE owner_id=%s",
                            (w.bob.id,)).fetchone()["n"] == 3, "run history kept"

    again = w.admin_post(w.bob.id, "disable")
    check(again, 200)
    assert again.json()["already"] is True and again.json()["runs_cancelled"] == 0


@test
def test_enabling_restores_keys_but_not_the_revoked_sessions():
    w = World()
    old_cookie = w.bob.cookie
    check(w.admin_post(w.bob.id, "disable"), 200)
    r = w.admin_post(w.bob.id, "enable")
    check(r, 200, "enabling bob")
    assert r.json()["user"]["disabled"] is False and r.json()["already"] is False

    check(old_cookie.get("/runs"), 401, "a cookie live at disable time must stay dead")
    check(w.bob.bearer.get("/runs", headers=w.bob.full), 200, "his key is restored")
    check(w.bob.sign_in().get("/runs"), 200, "a fresh sign-in works")
    assert w.admin_post(w.bob.id, "enable").json()["already"] is True


# --- lockout -------------------------------------------------------------------------


@test
def test_an_admin_cannot_demote_or_disable_themselves():
    w = World()
    check(w.admin_post(w.alice.id, "promote"), 200)  # so "last admin" is not the reason
    for action in ("demote", "disable"):
        r = w.admin_post(w.root.id, action)
        check(r, 409, f"root trying to {action} themselves")
        assert r.json()["error"] == "cannot change yourself", r.json()
    assert w.root.cookie.get("/auth/me").json()["is_admin"] is True


@test
def test_the_last_enabled_admin_cannot_be_removed():
    """By the operator key, which is not a user and so does not count."""
    w = World()
    for action in ("demote", "disable"):
        r = w.anon.post(f"/admin/users/{w.root.id}/{action}", headers=OPERATOR)
        check(r, 409, f"operator trying to {action} the only admin")
        assert r.json()["error"] == "last administrator", r.json()
    check(w.root.cookie.get("/admin/users"), 200, "root is still an admin")

    # With a second admin it is allowed, and the second is then protected.
    check(w.anon.post(f"/admin/users/{w.alice.id}/promote", headers=OPERATOR), 200)
    check(w.anon.post(f"/admin/users/{w.root.id}/demote", headers=OPERATOR), 200)
    check(w.anon.post(f"/admin/users/{w.alice.id}/disable", headers=OPERATOR), 409,
          "alice is now the last admin")
    # A DISABLED admin is not an admin anyone can use, so it does not count.
    check(w.anon.post(f"/admin/users/{w.root.id}/promote", headers=OPERATOR), 200)
    check(w.anon.post(f"/admin/users/{w.root.id}/disable", headers=OPERATOR), 200)
    check(w.anon.post(f"/admin/users/{w.alice.id}/demote", headers=OPERATOR), 409,
          "the only ENABLED admin left")


@test
def test_two_admins_demoting_each_other_at_once_cannot_both_win():
    """The guard and the write share a transaction under one lock."""
    import threading  # noqa: PLC0415

    from api.users import LastAdmin, set_admin  # noqa: PLC0415

    w = World()
    check(w.admin_post(w.alice.id, "promote"), 200)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def demote(user_id):
        barrier.wait()
        try:
            set_admin(user_id, False)
            outcomes.append("ok")
        except LastAdmin:
            outcomes.append("refused")

    threads = [threading.Thread(target=demote, args=(uid,))
               for uid in (w.root.id, w.alice.id)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(outcomes) == ["ok", "refused"], outcomes
    with pool().connection() as conn:
        n = conn.execute("SELECT count(*) AS n FROM users WHERE is_admin "
                         "AND disabled_at IS NULL").fetchone()["n"]
    assert n == 1, f"{n} enabled admins left"


def main() -> int:
    print(f"running {len(TESTS)} admin tests\n")
    failed = 0
    for fn in TESTS:
        name = fn.__name__.removeprefix("test_")
        try:
            fn()
            print(f"  ok    {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{'all ' + str(len(TESTS)) + ' tests passed' if not failed else f'{failed} FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
