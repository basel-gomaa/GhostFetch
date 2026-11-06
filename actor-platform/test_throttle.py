#!/usr/bin/env python3
"""Sign-in and sign-up throttling, over HTTP (security review H-02).

    ACTOR_PLATFORM_DATABASE_URL=postgresql://... ACTOR_PLATFORM_SECRET_KEY=... python3 test_throttle.py

Needs a THROWAWAY database: it truncates users and the throttle table. No Docker.

  * one address cannot make more than N login / signup attempts per window
  * one account locks after N consecutive failures, even for the right password
  * an UNKNOWN name locks exactly like a real one, so nothing is revealed
  * a successful sign-in resets the count; an admin can unlock early
  * loopback is exempt from the per-address limit, never from the lockout
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

from api import throttle  # noqa: E402
from api.auth import AuthPolicy  # noqa: E402
from api.main import create_app  # noqa: E402
from api.users import Identity  # noqa: E402
from db.pool import pool  # noqa: E402
from jobqueue.file_queue import FileJobQueue  # noqa: E402
from registry.pg_registry import PgRunRegistry  # noqa: E402
from shared.crypto import KeyRing, generate_key  # noqa: E402

TESTS = []
KEYRING = KeyRing.from_env({"ACTOR_PLATFORM_SECRET_KEY": generate_key()})
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}
GOOD = "correct-horse-battery"


def test(fn):
    TESTS.append(fn)
    return fn


class Harness:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        with pool().connection() as conn:
            conn.execute("TRUNCATE users, runs, auth_throttle CASCADE")
        self.app = create_app(
            registry=PgRunRegistry(Path(self._tmp.name) / "runs"),
            queue=FileJobQueue(str(Path(self._tmp.name) / "queue")),
            auth=AuthPolicy.from_env({"ACTOR_PLATFORM_API_KEYS": "apk_throttle_operator"}),
            identity=Identity(KEYRING),
        )

    def client(self, address: str = "203.0.113.10") -> TestClient:
        return TestClient(self.app, client=(address, 50000))

    def signup(self, name: str, address: str = "testclient") -> str:
        c = self.client(address)
        r = c.post("/auth/signup", json={"email": f"{name}@gmail.com", "username": name,
                                         "password": GOOD}, headers=SAME_ORIGIN)
        assert r.status_code == 201, r.text
        return r.json()["id"]


def login(c: TestClient, who: str, password: str):
    return c.post("/auth/login", json={"identifier": who, "password": password},
                  headers=SAME_ORIGIN)


@test
def test_one_address_is_limited_on_login():
    h = Harness()
    c = h.client("203.0.113.20")
    for i in range(throttle.LOGIN_PER_IP):
        # Different (unknown) names, so the per-account lock is not what trips.
        assert login(c, f"nobody{i}", "x").status_code == 401
    r = login(c, "nobody-final", "x")
    assert r.status_code == 429, r.text
    assert int(r.headers["Retry-After"]) > 0, r.headers
    # Another address is unaffected.
    assert login(h.client("203.0.113.21"), "nobody", "x").status_code == 401


@test
def test_one_address_is_limited_on_signup():
    h = Harness()
    c = h.client("203.0.113.30")
    for i in range(throttle.SIGNUP_PER_IP):
        r = c.post("/auth/signup", json={"email": f"s{i}@gmail.com", "username": f"s{i}",
                                         "password": GOOD}, headers=SAME_ORIGIN)
        assert r.status_code == 201, r.text
    r = c.post("/auth/signup", json={"email": "spam@gmail.com", "username": "spam",
                                     "password": GOOD}, headers=SAME_ORIGIN)
    assert r.status_code == 429, r.text


@test
def test_an_account_locks_after_repeated_failures_even_for_the_right_password():
    h = Harness()
    h.signup("basel")
    for i in range(throttle.LOCK_AFTER):
        # A fresh address each time: this is the per-ACCOUNT lock, not the rate.
        assert login(h.client(f"198.51.100.{i + 1}"), "basel", "wrong").status_code == 401
    r = login(h.client("198.51.100.200"), "basel", GOOD)
    assert r.status_code == 429, f"a locked account still signed in: {r.text}"
    assert "try again" in r.json()["detail"], r.text


@test
def test_an_unknown_name_locks_exactly_like_a_real_one():
    h = Harness()
    h.signup("basel")
    real = unknown = None
    for i in range(throttle.LOCK_AFTER + 1):
        real = login(h.client(f"192.0.2.{i + 1}"), "basel", "wrong")
        unknown = login(h.client(f"192.0.2.{i + 101}"), "ghost", "wrong")
        assert real.status_code == unknown.status_code, (i, real.text, unknown.text)
    assert real.status_code == unknown.status_code == 429
    assert real.json()["detail"] == unknown.json()["detail"], "the messages differ"


@test
def test_a_successful_sign_in_resets_the_count():
    h = Harness()
    h.signup("basel")
    c = h.client("testclient")
    for _ in range(throttle.LOCK_AFTER - 1):
        assert login(c, "basel", "wrong").status_code == 401
    assert login(c, "basel", GOOD).status_code == 200
    for _ in range(throttle.LOCK_AFTER - 1):
        assert login(c, "basel", "wrong").status_code == 401
    assert login(c, "basel", GOOD).status_code == 200, "an earlier, reset run of failures still counted"


@test
def test_loopback_skips_the_rate_limit_but_not_the_lockout():
    h = Harness()
    h.signup("basel")
    c = h.client("127.0.0.1")
    for i in range(throttle.LOGIN_PER_IP + 5):
        assert login(c, f"nobody{i}", "x").status_code == 401, "loopback was rate limited"
    for _ in range(throttle.LOCK_AFTER):
        login(c, "basel", "wrong")
    assert login(c, "basel", GOOD).status_code == 429, "loopback bypassed the account lockout"


@test
def test_an_admin_can_unlock_early():
    h = Harness()
    target = h.signup("basel")
    admin_id = h.signup("boss")
    with pool().connection() as conn:
        conn.execute("UPDATE users SET is_admin = true WHERE id = %s", (admin_id,))
    for i in range(throttle.LOCK_AFTER):
        login(h.client(f"203.0.113.{100 + i}"), "basel", "wrong")
    assert login(h.client(), "basel", GOOD).status_code == 429

    admin = h.client("testclient")
    assert login(admin, "boss", GOOD).status_code == 200
    listed = {u["username"]: u for u in admin.get("/admin/users").json()["users"]}
    assert listed["basel"]["locked"] is True, listed["basel"]
    r = admin.post(f"/admin/users/{target}/unlock", headers=SAME_ORIGIN)
    assert r.status_code == 200 and r.json()["already"] is False, r.text
    assert login(h.client("203.0.113.250"), "basel", GOOD).status_code == 200


def main() -> int:
    failed = 0
    print(f"running {len(TESTS)} throttle tests\n")
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
