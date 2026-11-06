#!/usr/bin/env python3
"""The editable profile: a display name and a photo, over HTTP.

    ACTOR_PLATFORM_DATABASE_URL=postgresql://... python3 test_profile.py

Needs a THROWAWAY database: it truncates users between tests. No Docker.

The properties that matter, rather than "the name saves":

  * a display name is NOT an identity: you still sign in with your username,
    you cannot sign in with the display name, and two people may share one
  * a photo's TYPE comes from its bytes, never its header, and only raster
    types are kept (an SVG is a document that can run script on this origin)
  * you only ever read or change your OWN profile -- there is no id to pass
  * profile writes are guarded like every other write: the `run` scope, and a
    cookie write must come from this app
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

# Users are created through /auth/signup; who may sign up is test_invites.py's job.
os.environ["ACTOR_PLATFORM_SIGNUP"] = "open"

from fastapi.testclient import TestClient  # noqa: E402

from api.auth import AuthPolicy  # noqa: E402
from api.main import create_app  # noqa: E402
from api.users import MAX_AVATAR_BYTES, Identity  # noqa: E402
from db.pool import pool  # noqa: E402
from jobqueue.file_queue import FileJobQueue  # noqa: E402
from registry.pg_registry import PgRunRegistry  # noqa: E402
from shared.crypto import KeyRing, generate_key  # noqa: E402

TESTS = []
KEYRING = KeyRing.from_env({"ACTOR_PLATFORM_SECRET_KEY": generate_key()})
OPERATOR_KEY = "apk_operator_key_for_profile_tests"
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}
PASSWORD = "correct-horse-battery"

# The smallest real headers of each accepted type. The server only sniffs the
# signature, so these are enough to exercise it.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 40
JPEG = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF\x00" + b"\x00" * 40
WEBP = b"RIFF" + (40).to_bytes(4, "little") + b"WEBPVP8 " + b"\x00" * 32
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def test(fn):
    TESTS.append(fn)
    return fn


class Harness:
    def __init__(self, tmp: str):
        with pool().connection() as conn:
            conn.execute("TRUNCATE users, runs CASCADE")
        self.app = create_app(
            registry=PgRunRegistry(Path(tmp) / "runs"),
            queue=FileJobQueue(str(Path(tmp) / "queue")),
            auth=AuthPolicy.from_env({"ACTOR_PLATFORM_API_KEYS": OPERATOR_KEY}),
            identity=Identity(KEYRING),
        )
        self.anon = TestClient(self.app)

    def signup(self, username: str) -> tuple[TestClient, dict]:
        c = TestClient(self.app)  # its own cookie jar
        r = c.post("/auth/signup", json={"email": f"{username}@example.test",
                                         "username": username, "password": PASSWORD},
                   headers=SAME_ORIGIN)
        assert r.status_code == 201, r.text
        return c, r.json()


def harness() -> Harness:
    tmp = tempfile.TemporaryDirectory()
    h = Harness(tmp.name)
    h._tmp = tmp  # type: ignore[attr-defined]
    return h


def check(resp, code, label=""):
    assert resp.status_code == code, f"{label}: expected {code}, got {resp.status_code} {resp.text}"


def put_photo(c: TestClient, data: bytes, content_type: str = "image/png", headers=None):
    return c.put("/me/avatar", content=data,
                 headers={"content-type": content_type, **(headers or SAME_ORIGIN)})


# --- display name -------------------------------------------------------------


@test
def test_a_new_account_has_no_profile_yet():
    h = harness()
    c, body = h.signup("basel")
    assert body["display_name"] is None and body["avatar_version"] is None, body
    me = c.get("/auth/me").json()
    assert me["display_name"] is None and me["avatar_version"] is None, me


@test
def test_the_display_name_changes_and_the_username_does_not():
    h = harness()
    c, _ = h.signup("basel")
    r = c.patch("/me/profile", json={"display_name": "  Basel   Gomaa "}, headers=SAME_ORIGIN)
    check(r, 200, "set name")
    assert r.json()["display_name"] == "Basel Gomaa", "whitespace is collapsed"
    assert r.json()["username"] == "basel", "the username must not change"
    me = c.get("/auth/me").json()
    assert (me["display_name"], me["username"]) == ("Basel Gomaa", "basel"), me


@test
def test_you_sign_in_with_the_username_never_the_display_name():
    h = harness()
    c, _ = h.signup("basel")
    check(c.patch("/me/profile", json={"display_name": "Captain"}, headers=SAME_ORIGIN), 200)
    fresh = TestClient(h.app)
    check(fresh.post("/auth/login", json={"identifier": "Captain", "password": PASSWORD},
                     headers=SAME_ORIGIN), 401, "display name as login")
    ok = fresh.post("/auth/login", json={"identifier": "basel", "password": PASSWORD},
                    headers=SAME_ORIGIN)
    check(ok, 200, "username login")
    assert ok.json()["display_name"] == "Captain", "sign-in answers with the profile too"


@test
def test_two_people_may_share_a_display_name():
    """It is a label, not an identity, so it is deliberately not unique."""
    h = harness()
    a, _ = h.signup("alice")
    b, _ = h.signup("bob")
    for c in (a, b):
        check(c.patch("/me/profile", json={"display_name": "Sam"}, headers=SAME_ORIGIN), 200)


@test
def test_an_empty_name_clears_it():
    h = harness()
    c, _ = h.signup("basel")
    check(c.patch("/me/profile", json={"display_name": "Basel"}, headers=SAME_ORIGIN), 200)
    for cleared in ("", "   ", None):
        r = c.patch("/me/profile", json={"display_name": cleared}, headers=SAME_ORIGIN)
        check(r, 200, f"clear with {cleared!r}")
        assert r.json()["display_name"] is None, r.json()


@test
def test_a_bad_display_name_is_refused_and_nothing_changes():
    h = harness()
    c, _ = h.signup("basel")
    check(c.patch("/me/profile", json={"display_name": "Basel"}, headers=SAME_ORIGIN), 200)
    for bad, why in (("x" * 51, "too long"),
                     ("Basel\x07", "control character"),
                     ("Basel‮evil", "right-to-left override"),
                     ("x" * 201, "over the raw cap")):
        r = c.patch("/me/profile", json={"display_name": bad}, headers=SAME_ORIGIN)
        assert r.status_code in (400, 422), f"{why}: {r.status_code} {r.text}"
    assert c.get("/auth/me").json()["display_name"] == "Basel", "a refused write changed it"
    # Fifty characters exactly is fine, and so is a non-Latin name.
    check(c.patch("/me/profile", json={"display_name": "x" * 50}, headers=SAME_ORIGIN), 200)
    r = c.patch("/me/profile", json={"display_name": "باسل جمعة"}, headers=SAME_ORIGIN)
    check(r, 200, "arabic name")
    assert r.json()["display_name"] == "باسل جمعة"


@test
def test_unknown_profile_fields_are_refused():
    """You cannot rename yourself through the profile, or promote yourself."""
    h = harness()
    c, _ = h.signup("basel")
    for field in ("username", "is_admin", "email"):
        r = c.patch("/me/profile", json={"display_name": "B", field: "x"}, headers=SAME_ORIGIN)
        check(r, 422, f"extra field {field}")
    me = c.get("/auth/me").json()
    assert me["username"] == "basel" and me["is_admin"] is False


# --- photo ---------------------------------------------------------------------


@test
def test_a_photo_is_stored_and_served_back_to_its_owner():
    h = harness()
    c, _ = h.signup("basel")
    r = put_photo(c, PNG)
    check(r, 200, "upload")
    version = r.json()["avatar_version"]
    assert version, r.json()
    got = c.get(f"/me/avatar?v={version}")
    check(got, 200, "read back")
    assert got.content == PNG
    assert got.headers["content-type"] == "image/png"
    assert got.headers["x-content-type-options"] == "nosniff"
    assert "private" in got.headers["cache-control"]
    assert "immutable" in got.headers["cache-control"], "a versioned URL can be cached hard"
    assert "immutable" not in c.get("/me/avatar").headers["cache-control"]


@test
def test_the_type_comes_from_the_bytes_not_the_header():
    h = harness()
    c, _ = h.signup("basel")
    for data, declared, actual in ((PNG, "image/jpeg", "image/png"),
                                   (JPEG, "image/png", "image/jpeg"),
                                   (WEBP, "application/octet-stream", "image/webp")):
        check(put_photo(c, data, declared), 200, f"{actual} declared as {declared}")
        assert c.get("/me/avatar").headers["content-type"] == actual


@test
def test_only_raster_images_of_a_sane_size_are_accepted():
    h = harness()
    c, _ = h.signup("basel")
    check(put_photo(c, PNG), 200)
    for data, declared, why in ((SVG, "image/svg+xml", "an SVG can carry script"),
                                (b"<html><script>1</script>", "image/png", "HTML claiming PNG"),
                                (b"", "image/png", "empty"),
                                (b"GIF89a" + b"\x00" * 20, "image/gif", "not an accepted type"),
                                (PNG + b"\x00" * MAX_AVATAR_BYTES, "image/png", "too large")):
        check(put_photo(c, data, declared), 400, why)
    assert c.get("/me/avatar").content == PNG, "a refused upload replaced the photo"


@test
def test_a_new_photo_changes_the_version():
    h = harness()
    c, _ = h.signup("basel")
    first = put_photo(c, PNG).json()["avatar_version"]
    second = put_photo(c, JPEG).json()["avatar_version"]
    assert first and second and first != second, (first, second)
    assert c.get("/auth/me").json()["avatar_version"] == second


@test
def test_removing_the_photo():
    h = harness()
    c, _ = h.signup("basel")
    check(put_photo(c, PNG), 200)
    r = c.delete("/me/avatar", headers=SAME_ORIGIN)
    check(r, 200, "remove")
    assert r.json()["avatar_version"] is None
    check(c.get("/me/avatar"), 404, "read after remove")
    check(c.delete("/me/avatar", headers=SAME_ORIGIN), 200, "removing nothing is fine")


@test
def test_you_only_ever_see_your_own_photo():
    h = harness()
    a, _ = h.signup("alice")
    b, _ = h.signup("bob")
    check(put_photo(a, PNG), 200)
    check(b.get("/me/avatar"), 404, "bob has none, and must not get alice's")
    check(put_photo(b, JPEG), 200)
    assert a.get("/me/avatar").content == PNG and b.get("/me/avatar").content == JPEG


@test
def test_deleting_a_user_removes_their_photo():
    h = harness()
    c, me = h.signup("basel")
    check(put_photo(c, PNG), 200)
    with pool().connection() as conn:
        conn.execute("DELETE FROM users WHERE id = %s", (me["id"],))
        left = conn.execute("SELECT count(*) AS n FROM user_avatars").fetchone()["n"]
    assert left == 0, "the photo outlived its owner"


# --- who may write ---------------------------------------------------------------


@test
def test_profile_routes_need_a_signed_in_user():
    h = harness()
    check(h.anon.patch("/me/profile", json={"display_name": "x"}), 401, "anon patch")
    check(h.anon.put("/me/avatar", content=PNG), 401, "anon upload")
    check(h.anon.get("/me/avatar"), 401, "anon read")
    check(h.anon.delete("/me/avatar"), 401, "anon delete")
    # An operator key belongs to nobody, so it has no profile to change.
    op = {"Authorization": f"Bearer {OPERATOR_KEY}"}
    check(h.anon.patch("/me/profile", json={"display_name": "x"}, headers=op), 403, "operator")
    check(h.anon.put("/me/avatar", content=PNG, headers=op), 403, "operator upload")


@test
def test_a_cross_site_cookie_write_is_refused():
    h = harness()
    c, _ = h.signup("basel")
    hostile = {"sec-fetch-site": "cross-site"}
    check(c.patch("/me/profile", json={"display_name": "pwned"}, headers=hostile), 403, "patch")
    check(put_photo(c, PNG, headers=hostile), 403, "upload")
    check(c.delete("/me/avatar", headers=hostile), 403, "delete")
    assert c.get("/auth/me").json()["display_name"] is None, "a cross-site write landed"


@test
def test_a_read_only_key_cannot_change_the_profile():
    h = harness()
    c, _ = h.signup("basel")
    made = c.post("/me/api-keys", json={"label": "ro", "scopes": ["read"]}, headers=SAME_ORIGIN)
    check(made, 201, "mint read-only key")
    ro = {"Authorization": f"Bearer {made.json()['key']}"}
    check(h.anon.patch("/me/profile", json={"display_name": "x"}, headers=ro), 403, "ro patch")
    check(h.anon.put("/me/avatar", content=PNG, headers=ro), 403, "ro upload")
    check(h.anon.get("/auth/me", headers=ro), 200, "but it can still read")


@test
def test_admins_see_the_display_name_beside_the_username():
    h = harness()
    admin, root = h.signup("root")
    with pool().connection() as conn:
        conn.execute("UPDATE users SET is_admin = true WHERE id = %s", (root["id"],))
    c, _ = h.signup("alice")
    check(c.patch("/me/profile", json={"display_name": "Alice Liddell"}, headers=SAME_ORIGIN), 200)
    users = {u["username"]: u for u in admin.get("/admin/users").json()["users"]}
    assert users["alice"]["display_name"] == "Alice Liddell", users["alice"]
    assert users["root"]["display_name"] is None


def main() -> int:
    print(f"running {len(TESTS)} profile tests\n")
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
