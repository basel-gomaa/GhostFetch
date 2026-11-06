#!/usr/bin/env python3
"""Accounts, sessions, API keys and cross-site protection, over HTTP.

    ACTOR_PLATFORM_DATABASE_URL=postgresql://... python3 test_accounts.py

Needs a THROWAWAY database: it truncates users between tests. No Docker.

What is actually being asserted here is not "signup works" -- it is the set of
properties that stop thirty users becoming one:

  * a foreign run is ABSENT, not forbidden (a 403 confirms it exists)
  * a signed-up user is never an operator, whatever scopes their key carries
  * logout revokes SERVER-SIDE, so a kept copy of the cookie is worthless
  * a cookie-authenticated WRITE must prove it came from this app
  * a bearer token is exempt from that, because it is not ambient
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

from api.auth import AuthPolicy, SESSION_COOKIE_NAME  # noqa: E402
from api.main import create_app  # noqa: E402
from api.users import Identity  # noqa: E402
from db.pool import pool  # noqa: E402
from jobqueue.file_queue import FileJobQueue  # noqa: E402
from registry.pg_registry import PgRunRegistry  # noqa: E402
from shared.crypto import KeyRing, generate_key  # noqa: E402

TESTS = []
KEYRING = KeyRing.from_env({"ACTOR_PLATFORM_SECRET_KEY": generate_key()})
OPERATOR_KEY = "apk_operator_key_for_tests"

# A browser's own request carries this; a cross-site one carries a different
# value. Every write below sends it, so the CSRF check is exercised as a real
# browser would exercise it rather than being bypassed by omission.
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}


def test(fn):
    TESTS.append(fn)
    return fn


class Harness:
    def __init__(self, tmp: str):
        with pool().connection() as conn:
            conn.execute("TRUNCATE users, runs CASCADE")
        self.registry = PgRunRegistry(Path(tmp) / "runs")
        self.app = create_app(
            registry=self.registry,
            queue=FileJobQueue(str(Path(tmp) / "queue")),
            auth=AuthPolicy.from_env({"ACTOR_PLATFORM_API_KEYS": OPERATOR_KEY}),
            identity=Identity(KEYRING),
        )
        self.client = TestClient(self.app)

    def signup(self, username="basel", email=None, password="correct-horse-battery"):
        c = TestClient(self.app)  # its own cookie jar, so users stay separate
        r = c.post("/auth/signup", json={
            "email": email or f"{username}@gmail.com",
            "username": username, "password": password}, headers=SAME_ORIGIN)
        assert r.status_code == 201, r.text
        return c, r.json()


def harness():
    tmp = tempfile.TemporaryDirectory()
    h = Harness(tmp.name)
    h._tmp = tmp  # keep it alive for the test's lifetime
    return h


def seed_run(registry, run_id: str, owner_id: str) -> str:
    """Insert a run row with a CHOSEN id.

    Minted ids are microseconds apart and share a long prefix, so a prefix taken
    from one user's run also prefixes the other's -- which makes a leak test
    pass for the wrong reason (it resolves to the caller's OWN run and returns
    200). Choosing the ids is the only way to make the prefix name exactly one.
    """
    with pool().connection() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, owner_id, actor, status, created_at, run_dir) "
            "VALUES (%s,%s,'hello_world','PENDING',now(),%s)",
            (run_id, owner_id, str(registry.root / run_id)),
        )
    return run_id


def check(resp, code, label=""):
    assert resp.status_code == code, f"{label}: expected {code}, got {resp.status_code} {resp.text}"


# --- accounts ---------------------------------------------------------------


@test
def test_signup_signs_you_in_and_sets_an_httponly_cookie():
    h = harness()
    c, body = h.signup()
    assert body["username"] == "basel" and body["is_admin"] is False
    cookie = c.cookies.get(SESSION_COOKIE_NAME)
    assert cookie, "no session cookie was set"
    assert "." in cookie, "the cookie must be signed, not a bare row id"
    # HttpOnly means page JavaScript cannot read it, so an XSS cannot steal it.
    raw = [v for k, v in c.post("/auth/login", json={
        "identifier": "basel", "password": "correct-horse-battery"},
        headers=SAME_ORIGIN).headers.raw if k == b"set-cookie"]
    assert any(b"httponly" in v.lower() for v in raw), raw
    check(c.get("/auth/me"), 200)
    assert c.get("/auth/me").json()["email"] == "basel@gmail.com"


@test
def test_duplicate_signup_is_refused_case_insensitively():
    h = harness()
    h.signup(username="basel", email="Basel@Gmail.com")
    c = TestClient(h.app)
    r = c.post("/auth/signup", json={"email": "basel@gmail.com", "username": "other",
                                     "password": "correct-horse-battery"}, headers=SAME_ORIGIN)
    check(r, 400, "same email in another case")
    assert "already registered" in r.text


@test
def test_a_bad_password_says_nothing_about_whether_the_account_exists():
    h = harness()
    h.signup()
    c = TestClient(h.app)
    real = c.post("/auth/login", json={"identifier": "basel", "password": "wrong-wrong-wrong"},
                  headers=SAME_ORIGIN)
    ghost = c.post("/auth/login", json={"identifier": "nobody", "password": "wrong-wrong-wrong"},
                   headers=SAME_ORIGIN)
    check(real, 401); check(ghost, 401)
    assert real.json() == ghost.json(), "the two failures must be indistinguishable"


@test
def test_logout_revokes_server_side_not_just_the_cookie():
    h = harness()
    c, _ = h.signup()
    stolen = c.cookies.get(SESSION_COOKIE_NAME)
    check(c.post("/auth/logout", headers=SAME_ORIGIN), 204)
    check(c.get("/auth/me"), 401, "after logout")
    # Someone who kept a copy of the cookie must not still be signed in.
    thief = TestClient(h.app)
    thief.cookies.set(SESSION_COOKIE_NAME, stolen)
    check(thief.get("/auth/me"), 401, "a kept copy of a revoked cookie")


@test
def test_a_forged_cookie_is_rejected():
    h = harness()
    c, _ = h.signup()
    real = c.cookies.get(SESSION_COOKIE_NAME)
    session_id = real.rpartition(".")[0]
    forged = TestClient(h.app)
    forged.cookies.set(SESSION_COOKIE_NAME, f"{session_id}.{'0' * 32}")
    check(forged.get("/auth/me"), 401, "wrong signature")
    forged.cookies.set(SESSION_COOKIE_NAME, session_id)
    check(forged.get("/auth/me"), 401, "unsigned")


# --- cross-site protection --------------------------------------------------


@test
def test_a_cookie_authenticated_write_must_come_from_this_app():
    h = harness()
    c, _ = h.signup()
    hostile = c.post("/me/api-keys", json={"label": "x"},
                     headers={"sec-fetch-site": "cross-site"})
    check(hostile, 403, "cross-site write")
    assert "cross-site request refused" in hostile.text
    check(c.post("/me/api-keys", json={"label": "x"}, headers=SAME_ORIGIN), 201)


@test
def test_reads_are_not_blocked_by_the_cross_site_check():
    """A GET changes nothing, so refusing it would break ordinary navigation."""
    h = harness()
    c, _ = h.signup()
    check(c.get("/auth/me", headers={"sec-fetch-site": "cross-site"}), 200)


@test
def test_a_bearer_token_is_exempt_because_it_is_not_ambient():
    h = harness()
    c, _ = h.signup()
    key = c.post("/me/api-keys", json={"label": "ci"}, headers=SAME_ORIGIN).json()["key"]
    bare = TestClient(h.app)  # no cookies at all
    r = bare.post("/actors/hello_world/runs", json={"name": "x"},
                  headers={"authorization": f"Bearer {key}", "sec-fetch-site": "cross-site"})
    assert r.status_code != 403, f"a token request must not be CSRF-refused: {r.text}"


# --- API keys ---------------------------------------------------------------


@test
def test_an_api_key_is_shown_once_and_then_unrecoverable():
    h = harness()
    c, _ = h.signup()
    created = c.post("/me/api-keys", json={"label": "ci"}, headers=SAME_ORIGIN).json()
    key = created["key"]
    assert key.startswith("apk_")
    listed = c.get("/me/api-keys").json()
    assert len(listed) == 1 and listed[0]["key_id"] == created["key_id"]
    assert all("key" not in row for row in listed), "the listing must never carry the secret"

    other = TestClient(h.app)
    check(other.get("/auth/me", headers={"authorization": f"Bearer {key}"}), 200)
    check(c.delete(f"/me/api-keys/{created['id']}", headers=SAME_ORIGIN), 204)
    check(other.get("/auth/me", headers={"authorization": f"Bearer {key}"}), 401, "revoked key")


@test
def test_a_key_can_be_narrower_than_the_console_session():
    h = harness()
    c, _ = h.signup()
    key = c.post("/me/api-keys", json={"label": "ro", "scopes": ["read"]},
                 headers=SAME_ORIGIN).json()["key"]
    ro = TestClient(h.app)
    check(ro.get("/runs", headers={"authorization": f"Bearer {key}"}), 200)
    denied = ro.post("/actors/hello_world/runs", json={"name": "x"},
                     headers={"authorization": f"Bearer {key}"})
    check(denied, 403, "a read-only key starting a run")
    assert denied.json()["required_scope"] == "run"


# --- tenancy ----------------------------------------------------------------


@test
def test_one_users_runs_are_invisible_to_another():
    h = harness()
    a, ua = h.signup(username="alice")
    b, ub = h.signup(username="bob")

    mine = h.registry.create("hello_world", {}, owner_id=ua["id"])
    theirs = h.registry.create("hello_world", {}, owner_id=ub["id"])

    assert [r["run_id"] for r in a.get("/runs").json()["runs"]] == [mine.run_id]
    # ABSENT, not forbidden: a 403 would confirm the id exists.
    r = a.get(f"/runs/{theirs.run_id}")
    check(r, 404, "reading another user's run")
    assert "forbidden" not in r.text.lower()
    check(a.get(f"/runs/{theirs.run_id}/logs"), 404, "another user's logs")
    check(a.post(f"/runs/{theirs.run_id}/cancel", headers=SAME_ORIGIN), 404,
          "cancelling another user's run")


@test
def test_a_signed_up_user_is_never_an_operator():
    """The line that keeps thirty users out of the ownerless historical runs."""
    h = harness()
    a, ua = h.signup(username="alice")
    h.registry.create("hello_world", {})  # ownerless, as every imported run is

    assert a.get("/runs").json()["runs"] == [], "a user must not see ownerless runs"
    operator = TestClient(h.app)
    listed = operator.get("/runs", headers={"authorization": f"Bearer {OPERATOR_KEY}"})
    check(listed, 200)
    assert len(listed.json()["runs"]) == 1, "the operator key must still see everything"


@test
def test_a_run_you_start_belongs_to_you_and_only_you():
    """The round trip the whole milestone exists for: submit, then see it --
    and be the only one who can."""
    h = harness()
    a, ua = h.signup(username="alice")
    b, _ = h.signup(username="bob")

    started = a.post("/actors/hello_world/runs", json={"name": "Basel"}, headers=SAME_ORIGIN)
    check(started, 202, "starting a run")
    run_id = started.json()["run_id"]

    assert h.registry.get(run_id).owner_id == ua["id"], "the run was created ownerless"
    assert [r["run_id"] for r in a.get("/runs").json()["runs"]] == [run_id]
    assert b.get("/runs").json()["runs"] == [], "bob can see alice's run"
    check(b.get(f"/runs/{run_id}"), 404, "bob reading alice's run")
    check(a.get(f"/runs/{run_id}"), 200, "alice reading her own run")
    check(a.get(f"/runs/{run_id}/input"), 200, "alice reading her own input")
    assert a.get(f"/runs/{run_id}/input").json() == {"name": "Basel"}


@test
def test_a_prefix_cannot_be_used_to_probe_for_other_peoples_runs():
    """Prefix expansion leaks existence through its ERROR if left unscoped:
    'matches 2 runs' tells you two exist even when neither is yours."""
    h = harness()
    a, ua = h.signup(username="alice")
    b, ub = h.signup(username="bob")
    hers = seed_run(h.registry, "20260101-000001-aaaaaa", ua["id"])
    his = seed_run(h.registry, "20260202-000002-bbbbbb", ub["id"])

    check(a.get(f"/runs/{his[:8]}"), 404, "a prefix naming only bob's run")
    # ...while a prefix naming only her own still resolves, so the refusal above
    # is scoping and not a broken query.
    check(a.get(f"/runs/{hers[:8]}"), 200, "a prefix naming only her run")
    check(a.get(f"/runs/{hers}"), 200, "her own run in full")


@test
def test_a_run_needing_a_connection_you_do_not_have_is_refused_up_front():
    """409 BEFORE the run is created.

    A queued run that can never be given a credential is worse than no run: it
    occupies the queue, fails opaquely minutes later, and costs the user a run
    to learn something we knew at submission time. 409 rather than 400 because
    the REQUEST is well-formed -- it is the account state that is wrong, and the
    fix is an action taken elsewhere.
    """
    h = harness()
    a, ua = h.signup(username="alice")

    r = a.post("/actors/facebook/runs", json={"target": "nasa"}, headers=SAME_ORIGIN)
    check(r, 409, "no connected account")
    body = r.json()
    assert body["error"] == "social account not connected", body
    assert body.get("platform") == "facebook" and body.get("reconnect") is True, body
    assert h.registry.list() == [], "a refused submission must create no run"

    # An account that exists but has EXPIRED is refused too, and says so
    # differently -- the user needs to reconnect, not to connect.
    with pool().connection() as conn:
        conn.execute(
            "INSERT INTO social_accounts (user_id, platform, session_ciphertext, "
            "session_nonce, status) VALUES (%s,'facebook','\\x00','\\x00','expired')",
            (ua["id"],))
    r = a.post("/actors/facebook/runs", json={"target": "nasa"}, headers=SAME_ORIGIN)
    check(r, 409, "expired account")
    assert r.json()["error"] == "social account needs reconnecting", r.json()

    # ...and once connected, the run is accepted and carries the account.
    with pool().connection() as conn:
        conn.execute("UPDATE social_accounts SET status='connected' WHERE user_id=%s",
                     (ua["id"],))
    ok = a.post("/actors/facebook/runs", json={"target": "nasa"}, headers=SAME_ORIGIN)
    check(ok, 202, "with a connected account")
    run = h.registry.get(ok.json()["run_id"])
    assert run.social_account_id, "the run does not name the account it will use"
    assert run.concurrency_key == f"social:{run.social_account_id}", run.concurrency_key


@test
def test_the_demo_connect_stores_a_sealed_placeholder_and_unlocks_runs():
    """Demo build: Connect starts no browser. The login is signed in at once and
    capture stores a placeholder jar through the real encrypt-and-store path,
    so a run that needs an account is accepted afterwards."""
    import json

    from shared.crypto import session_aad

    h = harness()
    a, ua = h.signup(username="alice")
    r = a.post("/social/login", json={"platform": "facebook"}, headers=SAME_ORIGIN)
    check(r, 201, "starting a demo login")
    assert r.json()["demo"] is True and "vnc_path" not in r.json(), r.json()
    sid = r.json()["id"]

    status = a.get(f"/social/login/{sid}").json()
    assert status["ready"] and status["signed_in"], status

    r = a.post(f"/social/login/{sid}/capture", headers=SAME_ORIGIN)
    check(r, 200, "capturing the demo session")
    assert r.json()["status"] == "connected", r.json()
    check(a.get(f"/social/login/{sid}"), 404, "a captured login is forgotten")
    assert [x["platform"] for x in a.get("/social/accounts").json()] == ["facebook"]

    with pool().connection() as conn:
        row = conn.execute(
            "SELECT id, session_ciphertext, session_nonce, key_version FROM social_accounts "
            "WHERE user_id=%s", (ua["id"],)).fetchone()
    jar = json.loads(KEYRING.open_(bytes(row["session_ciphertext"]), bytes(row["session_nonce"]),
                                   session_aad(str(row["id"]), "facebook"), row["key_version"]))
    names = {c["name"]: c["value"] for c in jar["cookies"]}
    assert set(names) >= {"c_user", "xs"} and names["xs"] == "demo", names

    check(a.post("/actors/facebook/runs", json={"target": "lumen"}, headers=SAME_ORIGIN),
          202, "a run after the demo connect")
    # A platform that needs no account has nothing to connect.
    check(a.post("/social/login", json={"platform": "facebook_groups"}, headers=SAME_ORIGIN),
          400, "connecting a platform that needs no account")


@test
def test_an_optional_session_uses_your_account_when_you_have_one():
    """session_optional means "never refused", NOT "never given an account".

    The API used to return before looking the account up, so a Threads run went
    out anonymous even for a user with a connected account -- and got the public
    page's handful of replies per post instead of the paginated reply query.
    Without an account (or with a dead one) the run must still be accepted and
    simply carry none.
    """
    h = harness()
    a, ua = h.signup(username="alice")
    body = {"target": "lumen", "mode": "profile"}

    r = a.post("/actors/threads/runs", json=body, headers=SAME_ORIGIN)
    check(r, 202, "no account: still accepted")
    assert h.registry.get(r.json()["run_id"]).social_account_id is None

    with pool().connection() as conn:
        conn.execute(
            "INSERT INTO social_accounts (user_id, platform, session_ciphertext, "
            "session_nonce, status) VALUES (%s,'threads','\\x00','\\x00','expired')",
            (ua["id"],))
    r = a.post("/actors/threads/runs", json=body, headers=SAME_ORIGIN)
    check(r, 202, "expired account: runs anonymously, not refused")
    assert h.registry.get(r.json()["run_id"]).social_account_id is None

    with pool().connection() as conn:
        conn.execute("UPDATE social_accounts SET status='connected' WHERE user_id=%s",
                     (ua["id"],))
    r = a.post("/actors/threads/runs", json=body, headers=SAME_ORIGIN)
    check(r, 202, "connected account")
    run = h.registry.get(r.json()["run_id"])
    assert run.social_account_id, "a connected account was not attached to the run"


@test
def test_an_actor_needing_no_connection_is_unaffected():
    h = harness()
    a, _ = h.signup(username="alice")
    check(a.post("/actors/hello_world/runs", json={"name": "Basel"}, headers=SAME_ORIGIN),
          202, "hello_world needs no account")


# --- reports ----------------------------------------------------------------


@test
def test_a_report_is_reachable_only_by_the_run_owner():
    """A report is as much this user's data as the run it came from."""
    h = harness()
    a, ua = h.signup(username="alice")
    b, _ = h.signup(username="bob")

    run = h.registry.create("hello_world", {}, owner_id=ua["id"])
    artifacts = Path(run.run_dir) / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "report.pdf").write_bytes(b"%PDF-1.4 pretend")
    (artifacts / "dashboard.html").write_text("<h1>dashboard</h1>")

    listed = a.get(f"/runs/{run.run_id}/artifacts")
    check(listed, 200)
    assert sorted(x["name"] for x in listed.json()["artifacts"]) == [
        "dashboard.html", "report.pdf"], listed.json()

    served = a.get(f"/runs/{run.run_id}/artifacts/dashboard.html")
    check(served, 200)
    assert b"dashboard" in served.content
    assert served.headers["content-type"].startswith("text/html")
    assert a.get(f"/runs/{run.run_id}/artifacts/report.pdf"
                 ).headers["content-type"] == "application/pdf"

    check(b.get(f"/runs/{run.run_id}/artifacts"), 404, "bob listing alice's report")
    check(b.get(f"/runs/{run.run_id}/artifacts/report.pdf"), 404, "bob fetching it")


@test
def test_items_are_reachable_only_by_the_run_owner():
    """Items are the run's scraped data, so they are scoped exactly like output."""
    from worker.dataset_ingest import DatasetIngestor  # noqa: PLC0415

    h = harness()
    a, ua = h.signup(username="alice")
    b, _ = h.signup(username="bob")

    run = h.registry.create("hello_world", {}, owner_id=ua["id"])
    h.registry.dataset_path(run.run_id).write_text(
        "".join(f'{{"collection": "c", "key": "k{i}", "data": {i}}}\n' for i in range(3)),
        encoding="utf-8")
    DatasetIngestor(h.registry, run.run_id, log=lambda _m: None).stop_and_drain()

    page = a.get(f"/runs/{run.run_id}/items?limit=2")
    check(page, 200)
    body = page.json()
    assert [i["data"] for i in body["items"]] == [0, 1] and body["next_after"] == 2, body
    assert body["count"] == 2, body
    rest = a.get(f"/runs/{run.run_id}/items?after=2").json()
    assert [i["data"] for i in rest["items"]] == [2] and rest["next_after"] is None, rest

    check(b.get(f"/runs/{run.run_id}/items"), 404, "bob reading alice's items")
    check(a.get(f"/runs/{run.run_id}/items?limit=0"), 422, "a zero page size")


@test
def test_an_artifact_name_cannot_reach_a_file_outside_the_artifacts_directory():
    """The name is matched against the LISTING rather than joined onto a path,
    so traversal is not something this route has to sanitise.

    Note what is NOT asserted: that a name containing ".." returns 404. A bare
    ".." is normalised away by the HTTP client before the request is ever sent,
    so it lands on GET /runs/{id} -- the caller's own run, which they may read
    anyway. Asserting a status code there would be testing the client's URL
    handling. The property that matters is the one below: no name serves a file
    the artifacts directory does not contain.
    """
    h = harness()
    a, ua = h.signup(username="alice")
    run = h.registry.create("hello_world", {}, owner_id=ua["id"])
    artifacts = Path(run.run_dir) / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "report.txt").write_text("the only artifact")
    secret = Path(run.run_dir) / "input.json"
    assert secret.is_file(), "expected the run to have an input document to try to reach"

    for hostile in ("..%2F..%2Finput.json", "..%2Finput.json", ".hidden",
                    "report.txt%2F..%2F..%2Finput.json", "%2Fetc%2Fpasswd"):
        r = a.get(f"/runs/{run.run_id}/artifacts/{hostile}")
        assert r.status_code == 404, f"{hostile!r} returned {r.status_code}"
        assert b"root:" not in r.content, f"{hostile!r} served a system file"

    # The legitimate name still works, so the refusals above are not the route
    # being broken.
    ok = a.get(f"/runs/{run.run_id}/artifacts/report.txt")
    check(ok, 200)
    assert b"the only artifact" in ok.content


@test
def test_a_run_records_where_its_report_should_go():
    """The recipient is on the RUN, never in input.json -- which is served over
    HTTP and read by an Actor that has no business knowing it."""
    h = harness()
    a, ua = h.signup(username="alice", email="alice@gmail.com")
    started = a.post("/actors/hello_world/runs", json={"name": "Basel"}, headers=SAME_ORIGIN)
    check(started, 202)
    run_id = started.json()["run_id"]

    with pool().connection() as conn:
        row = conn.execute("SELECT report_email, report_sent_at FROM runs WHERE run_id=%s",
                           (run_id,)).fetchone()
    assert row["report_email"] == "alice@gmail.com", row
    assert row["report_sent_at"] is None

    # ...and it is NOT in the input document the Actor and the API both see.
    assert a.get(f"/runs/{run_id}/input").json() == {"name": "Basel"}


def main() -> int:
    print(f"running {len(TESTS)} account tests\n")
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
