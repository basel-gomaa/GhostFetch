"""Input validation on the routes that only exist with accounts (a database).

    ACTOR_PLATFORM_DATABASE_URL=postgresql://... python3 test_input_validation_db.py

Needs a THROWAWAY database: it truncates users and runs between tests. No
Docker. The offline half lives in test_input_validation.py.

These bodies are read BEFORE anyone is authenticated (signup, login), or carry
values that go straight into SQL (identifiers, platform names, key ids), so the
two failures under test are the ones that matter on a public port: a response
that echoes a password back, and a value that reaches Postgres and turns into a
500.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if not (os.environ.get("ACTOR_PLATFORM_DATABASE_URL") or "").strip():
    print("ACTOR_PLATFORM_DATABASE_URL must point at a THROWAWAY database "
          "(this truncates tables)", file=sys.stderr)
    raise SystemExit(2)

import warnings  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)

# This suite creates its users through /auth/signup and is not about who may
# sign up; invite-only signup (the default) is covered by test_invites.py.
os.environ["ACTOR_PLATFORM_SIGNUP"] = "open"

from fastapi.testclient import TestClient  # noqa: E402

from db.pool import pool  # noqa: E402
from test_authz import (  # noqa: E402 - the same two-user world, reused
    ALICE_RUN, BOB_RUN, SAME_ORIGIN, World, check,
)

TESTS = []
PASSWORD = "correct-horse-battery-staple"


def test(fn):
    TESTS.append(fn)
    return fn


def run_count() -> int:
    with pool().connection() as conn:
        return conn.execute("SELECT count(*) AS n FROM runs").fetchone()["n"]


# --- bodies read before authentication ------------------------------------------


@test
def test_a_signup_error_never_echoes_the_password():
    w = World()
    anon = TestClient(w.app)
    secret = "MyRealPassword-123456"
    cases = [
        {"email": "new@example.com", "password": secret},                      # missing field
        {"email": "new@example.com", "username": "new", "password": secret,
         "is_admin": True},                                                     # extra field
        {"email": "e" * 250 + "@x.io", "username": "new", "password": secret},  # long email
        {"email": "new@example.com", "username": 5, "password": secret},       # wrong type
    ]
    for body in cases:
        r = anon.post("/auth/signup", json=body, headers=SAME_ORIGIN)
        check(r, 422, json.dumps(body)[:60])
        assert secret not in r.text, f"the password was echoed: {r.text[:200]}"
        assert '"input"' not in r.text, r.text[:200]
    # An over-long password is refused before it is hashed, and not echoed.
    r = anon.post("/auth/signup", headers=SAME_ORIGIN, json={
        "email": "new@example.com", "username": "new", "password": "p" * 1025})
    check(r, 422, "1025-character password")
    assert "p" * 100 not in r.text
    # "is_admin" in the body never made anyone an admin, and still does not.
    with pool().connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM users WHERE username='new'"
                            ).fetchone()["n"] == 0


@test
def test_unstorable_characters_are_422_not_a_database_500():
    w = World()
    anon = TestClient(w.app)
    for body in ({"identifier": "alice\u0000", "password": PASSWORD},
                 {"identifier": "\ud800", "password": PASSWORD},
                 {"identifier": "alice", "password": "pw\u0000pw"}):
        r = anon.post("/auth/login", headers=SAME_ORIGIN,
                      content=json.dumps(body).encode(),
                      )
        check(r, 422, repr(body)[:50])
    r = anon.post("/auth/signup", headers=SAME_ORIGIN, json={
        "email": "x\u0000@example.com", "username": "nul", "password": PASSWORD})
    check(r, 422, "NUL in an email")


@test
def test_legitimate_signup_and_login_still_work():
    w = World()
    anon = TestClient(w.app)
    r = anon.post("/auth/signup", headers=SAME_ORIGIN, json={
        "email": "Carol.Ünïcode@Example.com", "username": "carol", "password": PASSWORD})
    check(r, (200, 201), "signup")
    r = anon.post("/auth/login", headers=SAME_ORIGIN,
                  json={"identifier": "carol", "password": PASSWORD})
    check(r, 200, "login by username")


# --- account and social routes ---------------------------------------------------


@test
def test_api_key_requests_are_bounded():
    w = World()
    alice = w.alice
    for body in ({"label": "l" * 101}, {"scopes": ["read"] * 9}, {"label": "a\u0000"},
                 {"label": "ok", "unexpected": 1}, {"scopes": "read"}):
        r = alice.bearer.post("/me/api-keys", headers=alice.full, json=body)
        check(r, 422, json.dumps(body)[:40])
    r = alice.bearer.post("/me/api-keys", headers=alice.full,
                          json={"label": "nightly script", "scopes": ["read"]})
    check(r, (200, 201), "a normal key")


@test
def test_malformed_ids_reach_no_sql_cast():
    """uuid.UUID() took urn:uuid: and braces; Postgres then failed the cast."""
    w = World()
    alice = w.alice
    key_id = alice.read_key_row  # mint_api_key returns (plaintext, row id)
    for spelled in (f"urn:uuid:{key_id}", f"{{{key_id}}}", key_id.replace("-", ""),
                    "not-a-uuid", "%00"):
        r = alice.bearer.delete(f"/me/api-keys/{spelled}", headers=alice.full)
        check(r, 404, spelled)
    for spelled in (f"urn:uuid:{key_id}", "{00000000-0000-0000-0000-000000000000}", "%00"):
        r = alice.bearer.get(f"/social/login/{spelled}", headers=alice.full)
        check(r, (403, 404), spelled)
        assert r.status_code != 500


@test
def test_a_platform_name_is_a_config_key_or_nothing():
    w = World()
    alice = w.alice
    for name in ("../x", "a%00b", "FACEBOOK", "x" * 33, "fb;id"):
        r = alice.bearer.delete(f"/social/accounts/{name}", headers=alice.full)
        check(r, 404, name)
    for body in ({"platform": "../x"}, {"platform": "a\u0000"}, {"platform": ""},
                 {"platform": "facebook", "extra": 1}):
        r = alice.bearer.post("/social/login", headers=alice.full, json=body)
        check(r, 422, json.dumps(body))


# --- runs over Postgres ----------------------------------------------------------


@test
def test_an_unstorable_run_input_creates_no_row_and_no_directory():
    w = World()
    alice = w.alice
    before = run_count()
    for raw in (b'{"name": "a\\u0000b"}', b'{"name": "\\ud800"}'):
        r = alice.bearer.post("/actors/hello_world/runs", content=raw,
                              headers={**alice.full, "content-type": "application/json"})
        check(r, 400, raw.decode())
    assert run_count() == before
    assert sorted(p.name for p in w.registry.root.iterdir()) == sorted([ALICE_RUN, BOB_RUN])


@test
def test_item_paging_accepts_the_whole_bigint_range_and_nothing_past_it():
    w = World()
    alice = w.alice
    r = alice.bearer.get(f"/runs/{ALICE_RUN}/items?after={2**63 - 1}", headers=alice.full)
    check(r, 200, "after = max bigint")
    r = alice.bearer.get(f"/runs/{ALICE_RUN}/items?after={2**63}", headers=alice.full)
    check(r, 422, "after past bigint")
    r = alice.bearer.get(f"/runs/{ALICE_RUN}/items?collection=a%00b", headers=alice.full)
    check(r, 422, "NUL collection")
    r = alice.bearer.get("/runs?actor=a%00b", headers=alice.full)
    check(r, 422, "NUL actor filter")


@test
def test_identifier_manipulation_never_reaches_another_users_run():
    """Format validation is not authorization: a well-formed id that is not
    yours is 404, and no spelling of an id turns into someone else's run."""
    w = World()
    bob = w.bob
    spellings = [ALICE_RUN, ALICE_RUN[:8], ALICE_RUN.upper(), f" {ALICE_RUN}",
                 ALICE_RUN + "/", ALICE_RUN.replace("0", "٠"), f"{ALICE_RUN}%00",
                 f"../{ALICE_RUN}", f"..%2f{ALICE_RUN}"]
    for spelled in spellings:
        for route in ("/runs/{id}", "/runs/{id}/output", "/runs/{id}/logs", "/runs/{id}/items"):
            r = bob.bearer.get(route.format(id=spelled), headers=bob.full)
            assert r.status_code in (400, 404, 307, 422), (spelled, route, r.status_code)
            assert "alice" not in r.text, (spelled, route, r.text[:200])
        r = bob.bearer.post(f"/runs/{spelled}/cancel", headers=bob.full)
        assert r.status_code in (400, 404, 405, 422), (spelled, r.status_code)
    assert w.status_of(ALICE_RUN) == "PENDING"
    listed = bob.bearer.get("/runs?actor=hello_world", headers=bob.full).json()["runs"]
    assert [run["run_id"] for run in listed] == [BOB_RUN], listed


def main() -> int:
    print(f"running {len(TESTS)} database input-validation tests\n")
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
