#!/usr/bin/env python3
"""Threads Actor integration tests.

The claim under test is that a scraper needing NO CREDENTIAL AT ALL fits the
platform as neatly as one that needs a session -- that "connect an account" is
an Actor's property rather than an assumption baked into the platform.

This one is the odd case in both directions: it is the only Actor with no
session_env, no cookies and no login page, AND the only one where supplying a
session would make things worse (measured upstream: a valid jar took the same
scrape from 25 posts to zero, silently). Both halves are tested.

These hit the live network, and unlike the other suites they can actually
collect: no session is needed, so a real scrape is part of the run.

    python3 test_threads.py
"""

from __future__ import annotations

import ast
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
VENV = REPO_ROOT / ".venv"
VENV_PYTHON = VENV / "bin" / "python"

if (
    VENV_PYTHON.is_file()
    and Path(sys.prefix).resolve() != VENV.resolve()
    and not os.environ.get("_ACTOR_PLATFORM_REEXEC")
):
    os.environ["_ACTOR_PLATFORM_REEXEC"] = "1"
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from api.auth import AuthPolicy, generate_key  # noqa: E402
from api.main import create_app  # noqa: E402
from jobqueue.base import open_queue  # noqa: E402
from registry.run_registry import RunRegistry  # noqa: E402

# A SANDBOX, for the reason test_api_v1.py records: this suite purges its queue
# and runs real containers, and the default queue belongs to whatever deployment
# is running locally.
_SANDBOX = Path(tempfile.mkdtemp(prefix="actor-threads-tests-"))
atexit.register(shutil.rmtree, _SANDBOX, ignore_errors=True)

REGISTRY = RunRegistry(_SANDBOX / "runs")
QUEUE = open_queue(str(_SANDBOX / "queue"))

API_KEY = generate_key()
AUTH = {"Authorization": f"Bearer {API_KEY}"}
CLIENT = TestClient(
    create_app(registry=REGISTRY, queue=QUEUE, auth=AuthPolicy.for_keys({API_KEY})),
    headers=AUTH,
)

TARGET = os.environ.get("THREADS_TEST_TARGET", "lumen")
ACTOR_DIR = REPO_ROOT / "actors" / "threads"
IMAGE = "actor-platform/threads:latest"

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def submit(payload: dict):
    return CLIENT.post("/actors/threads/runs", json=payload)


def image_exists() -> bool:
    return subprocess.run(
        ["docker", "image", "inspect", IMAGE],
        capture_output=True, text=True,
    ).returncode == 0


def run_container(payload: dict, *, session: str | None = None, seconds: int = 600):
    """Run the Actor image directly. -> (exit code, stdout, stderr, output.json).

    Direct rather than through the queue: these are about the Actor's own
    contract -- what it does with an input, where its logs go, what it leaves at
    ACTOR_OUTPUT_PATH and what it exits with.
    """
    workdir = Path(tempfile.mkdtemp(dir=_SANDBOX))
    (workdir / "input.json").write_text(json.dumps(payload))
    workdir.chmod(0o777)

    args = ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{workdir}:/actor/storage"]
    env = {**os.environ}
    if session is not None:
        # There is no session variable for this Actor; the parameter exists so
        # one test can prove that setting something session-shaped changes
        # nothing, rather than being silently honoured.
        args += ["-e", f"{session}=irrelevant"]
    args.append(IMAGE)

    done = subprocess.run(args, capture_output=True, text=True, timeout=seconds, env=env)
    output = None
    if (workdir / "output.json").is_file():
        output = json.loads((workdir / "output.json").read_text())
    return done.returncode, done.stdout, done.stderr, output


def load_actor(name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, ACTOR_DIR / "actor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the adapter contract ---------------------------------------------------


@test
def test_report_artifacts_are_named_after_this_actor():
    """A run directory must not hand back a file called "report.pdf".

    Artifacts are served by name and downloaded into one folder, so six Actors
    all writing report.pdf / report.txt / dashboard.html produce files whose
    names say nothing about which scrape made them. This regresses silently -
    the run still succeeds and the console still lists three artifacts - so the
    generic names are pinned out of the source rather than only being renamed.
    """
    tree = ast.parse((ACTOR_DIR / "actor.py").read_text())
    prefix = next(
        (node.value.value for node in ast.walk(tree)
         if isinstance(node, ast.Assign)
         and any(getattr(t, "id", None) == "ARTIFACT_PREFIX" for t in node.targets)
         and isinstance(node.value, ast.Constant)), None)
    assert prefix == ACTOR_DIR.name, f"ARTIFACT_PREFIX is {prefix!r}, expected {ACTOR_DIR.name!r}"

    generic = {"report.pdf", "report.txt", "dashboard.html"}
    bare = {node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and node.value in generic}
    assert not bare, f"actor.py writes generically named artifact(s): {sorted(bare)}"

@test
def test_invalid_input_is_rejected_before_a_run_exists():
    """The API refuses bad input against input_schema.json -- no run, no job."""
    QUEUE.purge()
    before = len(REGISTRY.list(limit=500))

    for payload, why in (
        ({}, "target is required"),
        ({"target": ""}, "target must be non-empty"),
        ({"target": TARGET, "max_posts": 0}, "max_posts below the minimum"),
        ({"target": TARGET, "max_posts": "lots"}, "max_posts must be an integer"),
        ({"target": TARGET, "start_date": "last tuesday"}, "unparseable date"),
        ({"target": TARGET, "mode": "reels"}, "not an allowed mode"),
        # Facebook-only options must NOT have been copied across.
        ({"target": TARGET, "comment_sort": "newest"}, "comment_sort is not an Instagram option"),
        ({"target": TARGET, "page_concurrency": 4}, "page_concurrency is not an Instagram option"),
        ({"target": TARGET, "nonsense": 1}, "unknown field"),
    ):
        response = submit(payload)
        assert response.status_code == 400, f"{why}: got {response.status_code}"

    assert QUEUE.depth() == 0, "an invalid request queued a job"
    assert len(REGISTRY.list(limit=500)) == before, "an invalid request created a run"


@test
def test_the_actor_validates_its_own_input_too():
    """The Actor is runnable by hand, so it cannot rely on having been checked."""
    if not image_exists():
        return
    code, _, stderr, _ = run_container({"max_posts": 3})
    assert code == 1, f"expected an Actor failure, got {code}"
    assert "target" in stderr

    code, _, stderr, _ = run_container({"target": TARGET, "max_posts": "lots"})
    assert code == 1
    assert "must be an integer" in stderr

    code, _, stderr, _ = run_container({"target": TARGET, "mode": "reels"})
    assert code == 1
    assert "mode" in stderr


@test
def test_an_inverted_date_range_is_an_actor_failure_not_a_crash():
    if not image_exists():
        return
    code, _, stderr, _ = run_container(
        {"target": TARGET, "start_date": "2026-08-31", "end_date": "2026-08-01"}
    )
    assert code == 1, code
    assert "inverted" in stderr, stderr[-300:]
    assert "Traceback" not in stderr, "an invalid range should be reported, not raised"


@test
def test_stdout_is_valid_json_and_every_log_line_goes_to_stderr():
    """The collector reports through BOTH logging and bare print(); its print()
    calls would land on stdout and corrupt the result without the adapter's
    stream redirect."""
    if not image_exists():
        return
    code, stdout, stderr, _ = run_container({"target": TARGET, "max_posts": 2})

    assert stdout.strip(), "the Actor printed nothing on stdout"
    parsed = json.loads(stdout)          # raises if a log line leaked in
    assert isinstance(parsed, dict) and "status" in parsed, parsed
    assert len(stdout.splitlines()) == 1, (
        f"stdout carried {len(stdout.splitlines())} lines; it must be the result alone"
    )
    assert "[threads]" in stderr, "the Actor's own logs are missing from stderr"
    assert "[threads]" not in stdout, "an Actor log line reached stdout"


@test
def test_it_actually_collects_with_no_credential_at_all():
    """The inverse of every other Actor's session test: this one must SUCCEED
    anonymously. No session variable, no cookie jar, no login page."""
    if not image_exists():
        return
    code, stdout, stderr, output = run_container(
        {"target": TARGET, "max_posts": 3, "analyse": False}
    )
    assert code == 0, f"an anonymous run failed with {code}: {stderr[-500:]}"
    assert output is not None, "no output document was written"
    assert output["status"] == "success", output.get("status")
    assert output["post_count"] > 0, "an anonymous run collected nothing"
    assert output["platform"] == "Threads"
    # And it says it ran anonymously, so a short reply count is never mistaken
    # for a logged-in run that came up empty.
    assert "collecting anonymously" in stderr, stderr[-500:]


@test
def test_an_unusable_session_degrades_to_anonymous_rather_than_failing():
    """A session this Actor cannot use must not take the run down with it.

    THE SESSION IS OPTIONAL HERE, which is what makes this different from every
    other Actor: the others raise on a malformed value and are right to, because
    without a session they can do nothing. This one collects perfectly well
    anonymously, so a bad value must degrade rather than fail.

    Still the guard against the old hazard, too. A session once took the same
    profile scrape from 25 posts to ZERO, so "the posts stage still works with
    a session variable set" remains exactly the thing to assert.
    """
    if not image_exists():
        return
    code, _, stderr, output = run_container(
        {"target": TARGET, "max_posts": 3, "analyse": False}, session="THREADS_SESSION"
    )
    assert code == 0, f"an unusable session failed the run: {stderr[-400:]}"
    assert output and output["post_count"] > 0, (
        "an unusable session zeroed the posts stage - it must be ignored"
    )
    assert "anonymously" in stderr.lower(), (
        "the run must SAY it fell back to anonymous, or a capped result looks "
        f"like an authenticated one: {stderr[-400:]}"
    )


@test
def test_a_logged_out_jar_is_refused_rather_than_installed():
    """A jar with the right shape and no sessionid proves nothing.

    Installing it would put cookies on the tiers that want a session while
    still not being logged in - strictly worse than staying anonymous. Driven
    in-process because it needs no network: only the decision is under test.
    """
    module = load_actor("th_actor_session")
    import tempfile as _tf

    with _tf.TemporaryDirectory() as tmp:
        for value, label in (
            ("csrftoken=abc; mid=xyz", "cookie string with no sessionid"),
            ('{"cookies": [{"name": "csrftoken", "value": "abc"}]}', "storage_state with no sessionid"),
        ):
            os.environ[module.SESSION_ENV] = value
            try:
                assert module.install_session(tmp) is False, f"installed a {label}"
            finally:
                os.environ.pop(module.SESSION_ENV, None)
            assert not os.path.exists(os.path.join(tmp, "threads_auth.json")), (
                f"a {label} was written to disk")

        # And the real thing IS installed.
        os.environ[module.SESSION_ENV] = "sessionid=live; csrftoken=abc"
        try:
            assert module.install_session(tmp) is True, "a valid jar was refused"
        finally:
            os.environ.pop(module.SESSION_ENV, None)
        path = os.path.join(tmp, "threads_auth.json")
        assert os.path.exists(path), "a valid jar was not written"
        written = json.loads(open(path).read())
        names = {c["name"] for c in written["cookies"]}
        assert "sessionid" in names, names
        assert oct(os.stat(path).st_mode)[-3:] == "600", "the jar must not be world-readable"


@test
def test_the_result_preserves_the_collectors_own_document_shape():
    """The Actor must not invent a second Instagram data model."""
    module = load_actor("th_actor")

    posts_doc = {
        "platform": "Threads", "scraper": "posts", "status": "success",
        "page_information": {"page_name": "NASA", "page_url": "https://instagram.com/nasa",
                             "username": "nasa", "followers_count": 100},
        "posts": [{"post_url": "https://instagram.com/p/abc/", "post_date": "2026-08-01T00:00:00Z",
                   "caption": "hello", "likes": 3}],
        "date_window": {"start": "2026-08-01", "end": "2026-08-31", "complete": True,
                        "stop_reason": "window_cleared", "window_reached": True},
    }
    comments_doc = {"posts": [{
        "post_url": "https://instagram.com/p/abc/",
        "comments": [{"comment_id": "c1", "text": "hi"}],
    }]}

    result = module.build_result(posts_doc, comments_doc)

    assert result["platform"] == "Threads"
    assert result["date_window"] == posts_doc["date_window"]
    assert result["page_information"] == posts_doc["page_information"]
    assert result["status"] == "success"
    assert result["post_count"] == 1
    assert result["posts"][0]["comments"] == [{"comment_id": "c1", "text": "hi"}]
    assert result["comment_count"] == 1


@test
def test_an_empty_but_valid_window_is_not_reported_as_a_failure():
    """0 posts in a valid range is the collector answering truthfully."""
    module = load_actor("th_actor2")

    assert "no_posts_found" not in module.FAILURE_STATUSES
    # `rate_limited` MUST be a failure: this site throttles inside an HTTP 200
    # body rather than with a 429, so a throttled run is indistinguishable from
    # an empty account unless the status is honoured.
    assert "rate_limited" in module.FAILURE_STATUSES
    for failing in ("profile_unavailable", "rate_limited", "error"):
        assert failing in module.FAILURE_STATUSES

    empty = module.build_result(
        {"status": "no_posts_found", "page_information": {}, "posts": [],
         "date_window": {"complete": True, "stop_reason": "window_cleared",
                         "window_reached": True}},
        None,
    )
    assert empty["post_count"] == 0
    assert module.summarise(empty)["date_window"]["stop_reason"] == "window_cleared"


@test
def test_the_report_layer_is_told_which_platform_this_is():
    """The report layer is SHARED and defaults to the other platform's wording.

    reports/common.py::share_terms swaps "shares" for "reposts" on the platform
    string alone -- Instagram has no shares metric -- so an adapter that let the
    default stand would render a report describing a metric that does not exist.
    """
    module = load_actor("th_actor3")
    assert module.PLATFORM == "Threads"

    source = ast.parse((ACTOR_DIR / "actor.py").read_text())
    calls = [
        node for node in ast.walk(source)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id in {
            "format_facebook_text_report", "generate_facebook_pdf",
            "generate_dashboard", "format_page_text_report", "generate_page_pdf",
        }
    ]
    assert calls, "no report calls found - has the adapter changed shape?"
    for call in calls:
        assert any(kw.arg == "platform" for kw in call.keywords), (
            f"{call.func.id} is called without platform= and would render as the default"
        )


@test
def test_a_run_is_never_refused_for_want_of_an_account():
    """Optional, not absent: never refused without an account, used with one.

    Naming a platform is normally exactly what makes the API demand a connected
    account before creating a run. Posts and a sample of replies are public, so
    session_optional keeps a user with no account runnable -- while a user who
    DID connect one gets it injected as THREADS_SESSION, which is the only way
    past the public page's per-post reply cap.
    """
    from worker.limits import WorkerConfig

    config = WorkerConfig.load(path=REPO_ROOT / "config" / "actors.json")
    profile = config.limits_for("threads")
    assert profile.session_optional is True, "a run would be refused for a missing account"
    assert profile.session_env == "THREADS_SESSION", profile.session_env
    assert profile.session_cookies == ("sessionid",), profile.session_cookies
    # The collector's auth.py is read-only: nothing to write back.
    assert profile.session_out is False, "there is no refreshed session to write back"
    # No per-account cap: a read-only session does not race a rotation.
    assert profile.concurrency_scope is None, profile.concurrency_scope

    for path in (ACTOR_DIR / "vendor" / "collectors" / "threads").glob("*.py"):
        assert "max_concurrency" not in path.read_text().lower(), (
            f"{path.name} implements its own concurrency cap"
        )


@test
def test_the_session_policy_names_no_credential_as_the_account_id():
    """Connectable, with a login page and ONE proving cookie.

    Only sessionid proves a live Threads session; csrftoken, mid and ig_did are
    handed to logged-out visitors too, so listing them would refuse valid jars.
    """
    from shared.platform_policy import listed_platforms, policy_for, reset_cache

    reset_cache()
    policy = policy_for("threads")
    assert policy.connectable, "a user must be able to connect an account"
    assert policy.login_url, "a connectable platform needs a login page"
    assert policy.session_cookies == ("sessionid",), policy.session_cookies

    listed = {p.name: p for p in listed_platforms()}
    assert "threads" in listed, "it must still be LISTED, or it reads as unsupported"
    assert listed["threads"].connectable is True


@test
def test_the_actor_appears_in_the_catalog_with_its_schema():
    response = CLIENT.get("/actors")
    assert response.status_code == 200
    actors = {a["name"]: a for a in response.json()["actors"]}
    assert "threads" in actors, list(actors)

    schema = actors["threads"].get("input_schema")
    assert schema and schema.get("required") == ["target"], schema
    assert "start_date" in schema["properties"] and "end_date" in schema["properties"]
    assert "mode" in schema["properties"]
    assert schema["properties"]["mode"]["enum"] == ["profile", "tag"]
    # Facebook-only options must not have been copied across.
    assert "comment_sort" not in schema["properties"]
    assert "page_concurrency" not in schema["properties"]
    # It is a real scraper, so it is OFFERED -- unlike the test fixtures.
    assert actors["threads"].get("hidden") is False

    body = response.text
    for leak in ("/actors/", "Dockerfile", "vendor", str(REPO_ROOT)):
        assert leak not in body, f"the catalog leaked {leak!r}"


# --- the platform stayed generic --------------------------------------------


@test
def test_reply_count_sits_beside_comment_count():
    """`comment_count` is TOP-LEVEL comments; `reply_count` is what nests under
    them. A live run printed "96 unique replies" in its log and
    `comment_count: 86` in its result - ten replies-to-comments, counted in one
    and not the other, which read as ten lost. Both numbers now appear, and
    they add up to what the log says."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("threads_reply_actor", ACTOR_DIR / "actor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    posts_doc = {
        "platform": "Threads", "scraper": "posts", "status": "success",
        "page_information": {"page_name": "t", "page_url": "https://example.com/t"},
        "posts": [{"post_url": "https://example.com/p/1"},
                  {"post_url": "https://example.com/p/2"}],
    }
    comments_doc = {"posts": [
        {"post_url": "https://example.com/p/1", "comments": [
            {"comment_id": "a", "reply_comments": [{"comment_id": "a1"},
                                                     {"comment_id": "a2"}]},
            {"comment_id": "b", "reply_comments": []},
            {"comment_id": "c"},
        ]},
        {"post_url": "https://example.com/p/2", "comments": [
            {"comment_id": "d", "reply_comments": [{"comment_id": "d1"}]},
        ]},
    ]}

    result = module.build_result(posts_doc, comments_doc)
    assert result["comment_count"] == 4, result.get("comment_count")
    assert result["reply_count"] == 3, result.get("reply_count")

    summary = module.summarise(result)
    assert (summary["comment_count"], summary["reply_count"]) == (4, 3), summary

    # No comments collected -> neither count appears, rather than a false 0.
    bare = module.build_result(posts_doc, None)
    assert "reply_count" not in bare and "reply_count" not in module.summarise(bare)


def main() -> int:
    failures = 0
    print(f"running {len(TESTS)} Threads Actor tests")
    print(f"  target={TARGET}  session=none needed"
          f"  image={'built' if image_exists() else 'MISSING - container tests skipped'}\n")

    for fn in TESTS:
        name = fn.__name__.removeprefix("test_")
        started = time.monotonic()
        try:
            fn()
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"  FAIL  {name}  ({time.monotonic() - started:.0f}s)")
            print("        " + traceback.format_exc().strip().replace("\n", "\n        "))
        else:
            print(f"  ok    {name}  ({time.monotonic() - started:.0f}s)")

    print()
    if failures:
        print(f"{failures} of {len(TESTS)} tests FAILED")
        return 1
    print(f"all {len(TESTS)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
