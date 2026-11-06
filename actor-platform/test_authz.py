"""Object-level authorization (IDOR/BOLA) across the whole HTTP surface.

    ACTOR_PLATFORM_DATABASE_URL=postgresql://... \\
    ACTOR_PLATFORM_SECRET_KEY=... python3 test_authz.py

Needs a THROWAWAY database: it truncates users and runs between tests. No
Docker -- every run is seeded straight into the registry, so nothing executes.

test_accounts.py proves the scoping exists. This proves it HOLDS, route by
route, in both directions, for every way of naming a run (full id, prefix,
cookie, bearer key) -- because a boundary that is enforced on fifteen routes and
forgotten on the sixteenth is not a boundary. Each finding from the audit has a
test here that failed before its fix:

  F1  /runs/{prefix}/output expanded someone else's prefix and quoted the id
  F2  /metrics and /collectors rolled up every user's runs
  F3  a read-only key could mint itself a full key
  F4  a read-only key could delete connected accounts / start login browsers
  F5  a symlink in a run's artifacts directory was listed and served
  F6  /attention and /resume followed a symlink in the run directory
  F8  accounts + an unscoped registry showed every user every run
  F9  error bodies carried the storage path
  F10 a malformed id was a 500 rather than a 404
"""

from __future__ import annotations

import json
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
from registry.dataset import DatasetItem  # noqa: E402
from registry.pg_registry import PgRunRegistry  # noqa: E402
from registry.run_registry import RunRegistry  # noqa: E402
from shared.crypto import KeyRing, generate_key  # noqa: E402
from shared.models import (  # noqa: E402
    ATTENTION_FILE, INPUT_FILE, LOG_FILE, OUTPUT_FILE, RESUME_FILE,
)

TESTS = []
KEYRING = KeyRing.from_env({"ACTOR_PLATFORM_SECRET_KEY": generate_key()})
OPERATOR_KEY = "apk_operator_key_for_authz_tests"
OPERATOR = {"authorization": f"Bearer {OPERATOR_KEY}"}
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}
PASSWORD = "correct-horse-battery"

# Chosen ids with DISJOINT prefixes, so a prefix taken from one user's run names
# that run alone -- see test_accounts.seed_run for why minted ids cannot do this.
ALICE_RUN = "20260101-000001-aaaaaa"
BOB_RUN = "20260202-000002-bbbbbb"
BOB_TWINS = ("20260303-000003-cccccc", "20260303-000004-dddddd")

# Every read route that names a run. {id} is substituted per request.
READ_ROUTES = (
    "/runs/{id}",
    "/runs/{id}/input",
    "/runs/{id}/output",
    "/runs/{id}/items",
    "/runs/{id}/logs",
    "/runs/{id}/artifacts",
    "/runs/{id}/artifacts/report.txt",
    "/runs/{id}/attention",
)
WRITE_ROUTES = ("/runs/{id}/cancel", "/runs/{id}/resume")


def test(fn):
    TESTS.append(fn)
    return fn


def check(resp, code, label=""):
    codes = code if isinstance(code, tuple) else (code,)
    assert resp.status_code in codes, (
        f"{label}: expected {code}, got {resp.status_code} {resp.text[:300]}")


class User:
    """One account, reachable three ways: console cookie, read key, run key."""

    def __init__(self, app, username: str, *, admin: bool = False):
        self.cookie = TestClient(app)
        record = create_user(f"{username}@example.com", username, PASSWORD, is_admin=admin)
        self.id = record.id
        r = self.cookie.post("/auth/login", json={"identifier": username, "password": PASSWORD},
                             headers=SAME_ORIGIN)
        check(r, 200, f"{username} signing in")
        read_key, self.read_key_row = mint_api_key(self.id, scopes=frozenset({Scope.READ}))
        run_key, self.run_key_row = mint_api_key(self.id)
        self.read = {"authorization": f"Bearer {read_key}"}
        self.full = {"authorization": f"Bearer {run_key}"}
        self.bearer = TestClient(app)  # no cookie jar of its own in use

    def ways(self):
        """(label, client, headers) for every credential this user holds that
        may READ. The cookie path is what the console uses; the key path is
        what a script uses, and the two are separate code paths in identify()."""
        return (("cookie", self.cookie, {}),
                ("read key", self.bearer, self.read),
                ("full key", self.bearer, self.full))

    def writers(self):
        return (("cookie", self.cookie, SAME_ORIGIN), ("full key", self.bearer, self.full))


class World:
    def __init__(self):
        with pool().connection() as conn:
            conn.execute("TRUNCATE users, runs CASCADE")
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.registry = PgRunRegistry(self.tmp / "runs")
        self.app = create_app(
            registry=self.registry,
            queue=FileJobQueue(str(self.tmp / "queue")),
            auth=AuthPolicy.from_env({"ACTOR_PLATFORM_API_KEYS": OPERATOR_KEY}),
            identity=Identity(KEYRING),
        )
        self.anon = TestClient(self.app)
        self.alice = User(self.app, "alice")
        self.bob = User(self.app, "bob")
        self.admin = User(self.app, "root", admin=True)
        self.seed(ALICE_RUN, self.alice.id, "alice's")
        self.seed(BOB_RUN, self.bob.id, "bob's")

    def seed(self, run_id: str, owner_id: str, whose: str, status: str = "PENDING") -> Path:
        """A run with every kind of data a route can serve, all of it labelled
        with whose it is -- so a leak shows up as the WRONG label in a body."""
        run_dir = self.registry.root / run_id
        (run_dir / "artifacts").mkdir(parents=True)
        with pool().connection() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, owner_id, actor, status, created_at, run_dir) "
                "VALUES (%s,%s,'hello_world',%s,now(),%s)",
                (run_id, owner_id, status, str(run_dir)),
            )
        (run_dir / INPUT_FILE).write_text(json.dumps({"name": whose}))
        (run_dir / OUTPUT_FILE).write_text(json.dumps({"secret": f"{whose} output"}))
        (run_dir / LOG_FILE).write_text(f"{whose} log line\n")
        (run_dir / ATTENTION_FILE).write_text(json.dumps({"reason": f"{whose} attention"}))
        (run_dir / "artifacts" / "report.txt").write_text(f"{whose} report")
        self.registry.ingest_items(run_id, [DatasetItem(1, "posts", "k1", {"who": whose})])
        return run_dir

    def status_of(self, run_id: str) -> str:
        return self.registry.get(run_id).status.value


def world() -> World:
    return World()


# --- the two users ------------------------------------------------------------


@test
def test_each_user_reaches_everything_on_their_own_run():
    """The positive control. Without it, every 404 below could be a broken
    route rather than a working boundary."""
    w = world()
    for user, run_id, whose in ((w.alice, ALICE_RUN, "alice's"), (w.bob, BOB_RUN, "bob's")):
        for label, client, headers in user.ways():
            for route in READ_ROUTES:
                r = client.get(route.format(id=run_id), headers=headers)
                check(r, 200, f"{whose} {label} on {route}")
            assert whose in client.get(f"/runs/{run_id}/output", headers=headers).text
            assert whose in client.get(f"/runs/{run_id}/logs", headers=headers).text
        check(user.cookie.post(f"/runs/{run_id}/resume", headers=SAME_ORIGIN), 200,
              f"{whose} resuming their own run")
        check(user.bearer.post(f"/runs/{run_id}/cancel", headers=user.full), 200,
              f"{whose} cancelling their own run")
        assert w.status_of(run_id) == "CANCELLED"


@test
def test_neither_user_reaches_the_others_run_by_any_route_or_credential():
    """A -> B and B -> A, every read route, cookie and both key kinds. ABSENT
    (404), never forbidden: a 403 would confirm the id exists."""
    w = world()
    for user, theirs, their_name in ((w.alice, BOB_RUN, "bob's"), (w.bob, ALICE_RUN, "alice's")):
        for label, client, headers in user.ways():
            for route in READ_ROUTES:
                r = client.get(route.format(id=theirs), headers=headers)
                check(r, 404, f"{label} on {route} for {their_name} run")
                assert their_name not in r.text, f"{route} leaked {their_name} data"
                assert "forbidden" not in r.text.lower()
        for label, client, headers in user.writers():
            for route in WRITE_ROUTES:
                r = client.post(route.format(id=theirs), headers=headers)
                check(r, 404, f"{label} POST {route} on {their_name} run")
        assert w.status_of(theirs) == "PENDING", "a foreign cancel changed the run"
        assert not (w.registry.root / theirs / RESUME_FILE).exists(), \
            "a foreign resume signalled the run"
        listed = user.cookie.get("/runs").json()["runs"]
        assert theirs not in [r["run_id"] for r in listed]
        # A filter is not a way in: asking for the other user's runs by actor or
        # status still returns only your own.
        listed = user.cookie.get("/runs?actor=hello_world&status=PENDING").json()["runs"]
        assert theirs not in [r["run_id"] for r in listed]


@test
def test_a_prefix_on_any_run_route_reveals_nothing_about_someone_elses_runs():
    """F1. /output resolved the id through the UNSCOPED registry, so alice's
    prefix expanded to bob's FULL run id, which the 404 then quoted -- and a
    prefix matching several of bob's runs listed them all in a 409."""
    w = world()
    for twin in BOB_TWINS:
        w.seed(twin, w.bob.id, "bob's")
    root = str(w.registry.root)

    for route in READ_ROUTES:
        for prefix in (BOB_RUN[:8], BOB_RUN[:12], BOB_TWINS[0][:8]):
            r = w.alice.cookie.get(route.format(id=prefix))
            check(r, 404, f"alice probing {route} with {prefix!r}")
            for bob_id in (BOB_RUN, *BOB_TWINS):
                assert bob_id not in r.text, f"{route} named bob's run {bob_id}: {r.text}"
            assert root not in r.text, f"{route} disclosed the storage path: {r.text}"

    # ...while her own prefix still resolves on the very route that leaked, so
    # the refusal above is scoping and not a broken lookup.
    check(w.alice.cookie.get(f"/runs/{ALICE_RUN[:8]}/output"), 200, "her own prefix")


@test
def test_metrics_and_collectors_are_rolled_up_over_the_callers_runs_only():
    """F2. Both used to read every user's runs: counts per actor and status for
    the whole platform, and /collectors named the newest run id of each Actor
    -- whoever it belonged to."""
    w = world()
    for twin in BOB_TWINS:
        w.seed(twin, w.bob.id, "bob's")

    m = w.alice.cookie.get("/metrics")
    check(m, 200)
    assert m.json()["scanned"] == 1, f"alice's metrics counted other runs: {m.json()}"
    c = w.alice.cookie.get("/collectors")
    check(c, 200)
    assert ALICE_RUN in c.text
    for bob_id in (BOB_RUN, *BOB_TWINS):
        assert bob_id not in c.text, f"/collectors named bob's run {bob_id}"

    assert w.bob.read and w.bob.bearer.get("/metrics", headers=w.bob.read).json()["scanned"] == 3

    # Admin and operator still see the platform, which is what the pages are for.
    assert w.admin.cookie.get("/metrics").json()["scanned"] == 4
    assert w.anon.get("/metrics", headers=OPERATOR).json()["scanned"] == 4
    assert BOB_RUN in w.admin.cookie.get("/collectors").text or \
        BOB_TWINS[1] in w.admin.cookie.get("/collectors").text


# --- scope is not identity, and a key cannot grow itself -------------------------


@test
def test_a_read_only_key_cannot_mint_or_revoke_keys():
    """F3. POST /me/api-keys sat behind the READ guard and granted every scope
    by default, so a read key could mint itself a read+run key."""
    w = world()
    a = w.alice
    r = a.bearer.post("/me/api-keys", json={}, headers=a.read)
    check(r, 403, "a read key minting a key")
    r = a.bearer.post("/me/api-keys", json={"scopes": ["read"]}, headers=a.read)
    check(r, 403, "a read key minting even a read key")
    r = a.bearer.delete(f"/me/api-keys/{a.run_key_row}", headers=a.read)
    check(r, 403, "a read key revoking a key")
    check(a.bearer.get("/runs", headers=a.full), 200, "the key it tried to revoke still works")


@test
def test_a_new_key_is_never_wider_than_the_credential_that_minted_it():
    w = world()
    a = w.alice
    run_only, _ = mint_api_key(a.id, scopes=frozenset({Scope.RUN}))
    run_only_h = {"authorization": f"Bearer {run_only}"}

    r = a.bearer.post("/me/api-keys", json={"scopes": ["read", "run"]}, headers=run_only_h)
    check(r, 403, "a run-only key minting read+run")
    # No scopes asked for means "the same as mine", never "everything".
    r = a.bearer.post("/me/api-keys", json={}, headers=run_only_h)
    check(r, 201, "a run-only key minting with no scopes named")
    assert r.json()["scopes"] == ["run"], r.json()

    # A full key may narrow; the console session may mint anything (it holds all).
    r = a.bearer.post("/me/api-keys", json={"scopes": ["read"]}, headers=a.full)
    check(r, 201)
    assert r.json()["scopes"] == ["read"]
    r = a.cookie.post("/me/api-keys", json={}, headers=SAME_ORIGIN)
    check(r, 201, "the console minting a key")
    assert sorted(r.json()["scopes"]) == ["read", "run"]


@test
def test_a_read_only_key_cannot_change_social_accounts_or_start_a_login():
    """F4. Every social route sat behind READ, writes included."""
    w = world()
    a = w.alice
    some_session = str(uuid.uuid4())
    for method, path, body in (
        ("DELETE", "/social/accounts/linkedin", None),
        ("POST", "/social/login", {"platform": "linkedin"}),
        ("POST", f"/social/login/{some_session}/capture", None),
        ("DELETE", f"/social/login/{some_session}", None),
    ):
        r = a.bearer.request(method, path, json=body, headers=a.read)
        check(r, 403, f"a read key: {method} {path}")
        assert r.json()["error"] == "insufficient scope", r.json()

    # Reading stays open to a read key; a run key gets PAST the scope check
    # (404: nothing connected) rather than being refused by it.
    check(a.bearer.get("/social/accounts", headers=a.read), 200)
    check(a.bearer.delete("/social/accounts/linkedin", headers=a.full), 404)


@test
def test_an_operator_key_cannot_act_as_somebody_on_social_or_key_routes():
    """An operator key belongs to the deployment and has no user_id. It used to
    reach an INSERT with a NULL user and 500."""
    w = world()
    r = w.anon.post("/social/login", json={"platform": "linkedin"}, headers=OPERATOR)
    check(r, 403, "an operator key starting a login")
    r = w.anon.post("/me/api-keys", json={}, headers=OPERATOR)
    check(r, 403, "an operator key minting a user key")


# --- other users' keys and login sessions ------------------------------------------


@test
def test_another_users_key_and_login_session_cannot_be_reached_by_id():
    w = world()
    r = w.alice.cookie.delete(f"/me/api-keys/{w.bob.run_key_row}", headers=SAME_ORIGIN)
    check(r, 404, "alice revoking bob's key")
    check(w.bob.bearer.get("/runs", headers=w.bob.full), 200, "bob's key survived")
    assert w.bob.run_key_row not in w.alice.cookie.get("/me/api-keys").text

    # Bob starts a (demo) login; alice cannot see, capture or close it.
    r = w.bob.cookie.post("/social/login", json={"platform": "linkedin"}, headers=SAME_ORIGIN)
    check(r, 201, "bob starting a demo login")
    bobs = r.json()["id"]
    for method, path in (("GET", f"/social/login/{bobs}"),
                         ("POST", f"/social/login/{bobs}/capture"),
                         ("DELETE", f"/social/login/{bobs}")):
        r = w.alice.cookie.request(method, path, headers=SAME_ORIGIN)
        check(r, 404, f"alice {method} {path}")
    check(w.bob.cookie.get(f"/social/login/{bobs}"), 200, "alice's DELETE closed bob's login")


@test
def test_a_malformed_id_is_a_clean_refusal_never_a_500():
    """F10. A non-uuid key or login-session id reached a uuid cast in SQL."""
    w = world()
    a = w.alice
    for path in ("/social/login/not-a-uuid", "/social/login/'%3B--"):
        check(a.cookie.get(path), 404, path)
    check(a.cookie.delete("/me/api-keys/not-a-uuid", headers=SAME_ORIGIN), 404)
    for bad in ("not-a-run", "ZZZZ", "..%2F..%2Fetc", "%2Fetc%2Fpasswd"):
        for route in READ_ROUTES:
            r = a.cookie.get(route.format(id=bad))
            assert r.status_code in (400, 404), f"{route} with {bad!r}: {r.status_code}"
    check(a.cookie.get(f"/runs/{uuid.uuid4().hex[:6]}"), (400, 404))


# --- the filesystem under a run -----------------------------------------------------


@test
def test_a_symlink_in_the_artifacts_directory_is_neither_listed_nor_served():
    """F5. The directory is bind-mounted into the Actor's container; a link the
    container writes there must not become a download of a host file."""
    w = world()
    secret = w.tmp / "host-secret.txt"
    secret.write_text("HOST SECRET")
    artifacts = w.registry.root / ALICE_RUN / "artifacts"
    (artifacts / "leak.txt").symlink_to(secret)
    (artifacts / "etc").symlink_to("/etc/passwd")

    listed = w.alice.cookie.get(f"/runs/{ALICE_RUN}/artifacts").json()["artifacts"]
    assert [x["name"] for x in listed] == ["report.txt"], listed
    for name in ("leak.txt", "etc"):
        r = w.alice.cookie.get(f"/runs/{ALICE_RUN}/artifacts/{name}")
        check(r, 404, f"serving a symlinked artifact {name!r}")
        assert b"HOST SECRET" not in r.content and b"root:" not in r.content
    check(w.alice.cookie.get(f"/runs/{ALICE_RUN}/artifacts/report.txt"), 200)


@test
def test_attention_and_resume_do_not_follow_a_link_in_the_run_directory():
    """F6. resume WROTE through a planted link -- truncating any host file the
    API could write to -- and attention read through one."""
    w = world()
    run_dir = w.registry.root / ALICE_RUN

    outside = w.tmp / "outside.json"
    outside.write_text(json.dumps({"host": "file"}))
    (run_dir / ATTENTION_FILE).unlink()
    (run_dir / ATTENTION_FILE).symlink_to(outside)
    r = w.alice.cookie.get(f"/runs/{ALICE_RUN}/attention")
    check(r, 200)
    assert r.json() is None, f"attention followed a link: {r.json()}"

    victim = w.tmp / "victim.conf"
    victim.write_text("precious host config\n")
    (run_dir / RESUME_FILE).symlink_to(victim)
    r = w.alice.cookie.post(f"/runs/{ALICE_RUN}/resume", headers=SAME_ORIGIN)
    # The planted link is REPLACED by the real signal (never written through),
    # so the run can still be resumed and the host file is untouched.
    check(r, 200, "resuming with a planted link in place")
    assert victim.read_text() == "precious host config\n", "resume truncated a host file"
    assert not (run_dir / RESUME_FILE).is_symlink(), "the link survived"
    assert (run_dir / RESUME_FILE).read_text() == "{}\n"


# --- who is NOT a user ----------------------------------------------------------------


@test
def test_an_unauthenticated_request_is_refused_everywhere():
    w = world()
    reads = [r.format(id=ALICE_RUN) for r in READ_ROUTES] + [
        "/runs", "/actors", "/metrics", "/collectors", "/auth/me", "/me/api-keys",
        "/social/accounts", f"/social/login/{uuid.uuid4()}"]
    for path in reads:
        r = w.anon.get(path)
        check(r, 401, f"anonymous GET {path}")
        assert "alice" not in r.text
    for path in [r.format(id=ALICE_RUN) for r in WRITE_ROUTES] + [
            "/actors/hello_world/runs", "/me/api-keys", "/social/login"]:
        check(w.anon.post(path, json={}), 401, f"anonymous POST {path}")
    check(w.anon.get("/runs", headers={"authorization": "Bearer apk_nope_nope"}), 401)
    assert w.status_of(ALICE_RUN) == "PENDING"


@test
def test_admins_and_the_operator_see_and_act_on_every_users_runs():
    """Not a hole -- the documented model. Admin is a property of the account
    (or of a deployment key), never of a scope."""
    w = world()
    for label, client, headers in (("admin cookie", w.admin.cookie, SAME_ORIGIN),
                                   ("admin key", w.admin.bearer, w.admin.full),
                                   ("operator key", w.anon, OPERATOR)):
        listed = [r["run_id"] for r in client.get("/runs", headers=headers).json()["runs"]]
        assert {ALICE_RUN, BOB_RUN} <= set(listed), f"{label} saw {listed}"
        for run_id, whose in ((ALICE_RUN, "alice's"), (BOB_RUN, "bob's")):
            r = client.get(f"/runs/{run_id}/output", headers=headers)
            check(r, 200, f"{label} reading {whose} output")
            assert whose in r.text
    check(w.admin.cookie.post(f"/runs/{BOB_RUN}/cancel", headers=SAME_ORIGIN), 200)
    assert w.status_of(BOB_RUN) == "CANCELLED"

    # A read-scoped admin key is still held to its scope: admin widens WHOSE
    # data, never WHICH actions.
    check(w.admin.bearer.post(f"/runs/{ALICE_RUN}/cancel", headers=w.admin.read), 403)


@test
def test_the_client_cannot_choose_who_owns_a_run():
    """Ownership comes from the credential. An owner in the body or the query
    string is either refused or ignored -- never honoured."""
    w = world()
    r = w.alice.cookie.post("/actors/hello_world/runs",
                            json={"name": "x", "owner_id": w.bob.id}, headers=SAME_ORIGIN)
    if r.status_code == 202:
        assert w.registry.get(r.json()["run_id"]).owner_id == w.alice.id
    else:
        check(r, 400, "an owner_id in the input")
    r = w.alice.cookie.post(f"/actors/hello_world/runs?owner_id={w.bob.id}",
                            json={"name": "x"}, headers=SAME_ORIGIN)
    check(r, 202)
    assert w.registry.get(r.json()["run_id"]).owner_id == w.alice.id
    listed = w.alice.cookie.get(f"/runs?owner_id={w.bob.id}&user_id={w.bob.id}").json()["runs"]
    assert BOB_RUN not in [x["run_id"] for x in listed]
    assert w.bob.cookie.get("/runs").json()["runs"][0]["run_id"] == BOB_RUN


@test
def test_accounts_over_a_registry_that_cannot_scope_fail_closed():
    """F8. Accounts wired over the filesystem registry used to hand every user
    the unscoped registry -- i.e. everyone's runs."""
    with pool().connection() as conn:
        conn.execute("TRUNCATE users, runs CASCADE")
    tmp = tempfile.TemporaryDirectory()
    fs = RunRegistry(Path(tmp.name) / "runs")
    fs.create("hello_world", {"name": "someone else's"})
    app = create_app(registry=fs, queue=FileJobQueue(str(Path(tmp.name) / "q")),
                     auth=AuthPolicy.from_env({"ACTOR_PLATFORM_API_KEYS": OPERATOR_KEY}),
                     identity=Identity(KEYRING))
    user = User(app, "carol")
    r = user.cookie.get("/runs")
    check(r, 500, "a user over an unscopable registry")
    assert "someone else" not in r.text
    check(TestClient(app).get("/runs", headers=OPERATOR), 200, "the operator is unaffected")


@test
def test_error_bodies_carry_no_storage_path():
    """F9. The filesystem registry names its root in its messages."""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "runs"
    app = create_app(registry=RunRegistry(root), queue=FileJobQueue(str(Path(tmp.name) / "q")),
                     auth=AuthPolicy.disabled())
    client = TestClient(app)
    for path in ("/runs/20990101-000000-ffffff", "/runs/2099/logs", "/runs/2099/output",
                 "/runs/2099/attention"):
        r = client.get(path)
        check(r, 404, path)
        assert str(root) not in r.text and tmp.name not in r.text, r.text
    # The operation routes build their own errors rather than going through the
    # shared translator, so they are checked separately.
    for path in ("/runs/2099/cancel", "/runs/2099/resume"):
        r = client.post(path)
        check(r, 404, path)
        assert tmp.name not in r.text, r.text


def main() -> int:
    print(f"running {len(TESTS)} authorization tests\n")
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
