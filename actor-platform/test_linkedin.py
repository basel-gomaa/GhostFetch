#!/usr/bin/env python3
"""LinkedIn Actor integration tests.

The claim under test is not "LinkedIn works". It is that a THIRD real scraper
fits the platform without the platform learning anything new -- and this one is
the awkward case, because it is shaped differently from the other two: its post
feed has no out-of-browser tier at all, its comment stage needs no session, and
its cookie jar carries nothing that names the account.

These hit the live network. A session in LINKEDIN_SESSION unlocks the post feed;
without one the Actor fails at the auth wall, which is itself one of the things
tested.

    LINKEDIN_SESSION='li_at=...; JSESSIONID=...' python3 test_linkedin.py
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
_SANDBOX = Path(tempfile.mkdtemp(prefix="actor-linkedin-tests-"))
atexit.register(shutil.rmtree, _SANDBOX, ignore_errors=True)

REGISTRY = RunRegistry(_SANDBOX / "runs")
QUEUE = open_queue(str(_SANDBOX / "queue"))

API_KEY = generate_key()
AUTH = {"Authorization": f"Bearer {API_KEY}"}
CLIENT = TestClient(
    create_app(registry=REGISTRY, queue=QUEUE, auth=AuthPolicy.for_keys({API_KEY})),
    headers=AUTH,
)

SESSION = os.environ.get("LINKEDIN_SESSION", "")
TARGET = os.environ.get("LINKEDIN_TEST_TARGET", "pwc")
ACTOR_DIR = REPO_ROOT / "actors" / "linkedin"
IMAGE = "actor-platform/linkedin:latest"

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def submit(payload: dict):
    return CLIENT.post("/actors/linkedin/runs", json=payload)


def image_exists() -> bool:
    return subprocess.run(
        ["docker", "image", "inspect", IMAGE],
        capture_output=True, text=True,
    ).returncode == 0


def run_container(payload: dict, *, session: str | None = None, seconds: int = 300):
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
        args += ["-e", "LINKEDIN_SESSION"]
        env["LINKEDIN_SESSION"] = session
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
        ({"target": TARGET, "mode": "newsletter"}, "not an allowed mode"),
        # Facebook-only options must NOT have been copied across.
        ({"target": TARGET, "comment_sort": "newest"}, "comment_sort is not a LinkedIn option"),
        ({"target": TARGET, "page_concurrency": 4}, "page_concurrency is not a LinkedIn option"),
        ({"target": TARGET, "deep_comments": "yes"}, "deep_comments must be a boolean"),
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

    code, _, stderr, _ = run_container({"target": TARGET, "mode": "newsletter"})
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
    assert "[linkedin]" in stderr, "the Actor's own logs are missing from stderr"
    assert "[linkedin]" not in stdout, "an Actor log line reached stdout"


@test
def test_the_result_preserves_the_collectors_own_document_shape():
    """The Actor must not invent a second LinkedIn data model."""
    module = load_actor("li_actor")

    posts_doc = {
        "platform": "LinkedIn", "scraper": "posts", "status": "success",
        "page_information": {"page_name": "NASA", "page_url": "https://linkedin.com/nasa",
                             "username": "nasa", "followers_count": 100},
        "posts": [{"post_url": "https://linkedin.com/p/abc/", "post_date": "2026-08-01T00:00:00Z",
                   "caption": "hello", "likes": 3}],
        "date_window": {"start": "2026-08-01", "end": "2026-08-31", "complete": True,
                        "stop_reason": "window_cleared", "window_reached": True},
    }
    comments_doc = {"posts": [{
        "post_url": "https://linkedin.com/p/abc/",
        "comments": [{"comment_id": "c1", "text": "hi"}],
    }]}

    result = module.build_result(posts_doc, comments_doc)

    assert result["platform"] == "LinkedIn"
    assert result["date_window"] == posts_doc["date_window"]
    assert result["page_information"] == posts_doc["page_information"]
    assert result["status"] == "success"
    assert result["post_count"] == 1
    assert result["posts"][0]["comments"] == [{"comment_id": "c1", "text": "hi"}]
    assert result["comment_count"] == 1


@test
def test_an_empty_but_valid_window_is_not_reported_as_a_failure():
    """0 posts in a valid range is the collector answering truthfully."""
    module = load_actor("li_actor2")

    assert "no_posts_found" not in module.FAILURE_STATUSES
    for failing in ("authwall", "profile_unavailable", "hashtag_unavailable",
                    "error", "transport_error"):
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
    string alone -- LinkedIn has no shares metric -- so an adapter that let the
    default stand would render a report describing a metric that does not exist.
    """
    module = load_actor("li_actor3")
    assert module.PLATFORM == "LinkedIn"

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
def test_linkedin_is_capped_at_one_run_per_connected_account():
    """Policy in configuration, enforced by the platform -- not a lock inside
    the scraper, and not a cap on the Actor as a whole."""
    from worker.limits import WorkerConfig

    config = WorkerConfig.load(path=REPO_ROOT / "config" / "actors.json")
    profile = config.limits_for("linkedin")
    assert profile.concurrency_scope == "social_account", profile.concurrency_scope
    assert profile.per_key_limit == 1, profile.per_key_limit
    assert profile.max_concurrency is None, profile.max_concurrency
    assert profile.session_env == "LINKEDIN_SESSION"
    assert profile.session_out is True, "the refreshed session must come back"

    for path in (ACTOR_DIR / "vendor" / "collectors" / "linkedin").glob("*.py"):
        assert "max_concurrency" not in path.read_text().lower(), (
            f"{path.name} implements its own concurrency cap"
        )


@test
def test_the_session_policy_names_no_credential_as_the_account_id():
    """Only the cookie that proves being logged IN may define a live session.

    The jar also carries JSESSIONID -- the CSRF token, echoed into a header on
    every API request -- and it is tempting to require both. It must not be:
    a jar that reached the auth wall KEEPS JSESSIONID and loses li_at, so
    listing both would accept a dead session as a live one and let the
    write-back overwrite a good stored session with it.

    And there is no account-identifying cookie here at all, so session_id_cookie
    is absent: a connected account shows no id, which is the platform declining
    to invent one.
    """
    from shared.platform_policy import policy_for, reset_cache

    reset_cache()
    policy = policy_for("linkedin")
    assert policy.session_cookies == ("li_at",), policy.session_cookies
    assert "JSESSIONID" not in policy.session_cookies, (
        "JSESSIONID survives the auth wall; requiring it would accept a dead session"
    )
    assert policy.session_id_cookie is None, (
        f"session_id_cookie is {policy.session_id_cookie!r}; this jar names no account"
    )
    assert policy.connectable


@test
def test_the_actor_appears_in_the_catalog_with_its_schema():
    response = CLIENT.get("/actors")
    assert response.status_code == 200
    actors = {a["name"]: a for a in response.json()["actors"]}
    assert "linkedin" in actors, list(actors)

    schema = actors["linkedin"].get("input_schema")
    assert schema and schema.get("required") == ["target"], schema
    assert "start_date" in schema["properties"] and "end_date" in schema["properties"]
    assert "mode" in schema["properties"]
    assert "deep_comments" in schema["properties"]
    # Facebook-only options must not have been copied across.
    assert "comment_sort" not in schema["properties"]
    assert "page_concurrency" not in schema["properties"]
    # It is a real scraper, so it is OFFERED -- unlike the test fixtures.
    assert actors["linkedin"].get("hidden") is False

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

    spec = importlib.util.spec_from_file_location("linkedin_reply_actor", ACTOR_DIR / "actor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    posts_doc = {
        "platform": "LinkedIn", "scraper": "posts", "status": "success",
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
    print(f"running {len(TESTS)} LinkedIn Actor tests")
    print(f"  target={TARGET}  session={'yes' if SESSION else 'no (anonymous)'}"
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
