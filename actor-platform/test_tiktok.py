#!/usr/bin/env python3
"""TikTok Actor integration tests.

The claim under test is that the HARDEST scraper still fits the platform
unchanged. This one needs a headed browser on a virtual display, real Chrome
rather than bundled Chromium, and a session whose value includes a solved
CAPTCHA's token -- and none of that reached the platform: it is an image
detail, a config entry and this file.

WHAT THESE TESTS DO NOT CLAIM: that a scrape returns data. TikTok fingerprints
its data endpoints and serves empty 200 bodies to automated clients on
datacenter IPs regardless of session, so on a hosted machine zero videos is a
legitimate outcome rather than a failure. These test the ADAPTER -- input,
output, exit codes, session handling, boundaries -- which is what is actually
in our control.

    TIKTOK_SESSION='sessionid=...' python3 test_tiktok.py
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
_SANDBOX = Path(tempfile.mkdtemp(prefix="actor-tiktok-tests-"))
atexit.register(shutil.rmtree, _SANDBOX, ignore_errors=True)

REGISTRY = RunRegistry(_SANDBOX / "runs")
QUEUE = open_queue(str(_SANDBOX / "queue"))

API_KEY = generate_key()
AUTH = {"Authorization": f"Bearer {API_KEY}"}
CLIENT = TestClient(
    create_app(registry=REGISTRY, queue=QUEUE, auth=AuthPolicy.for_keys({API_KEY})),
    headers=AUTH,
)

SESSION = os.environ.get("TIKTOK_SESSION", "")
TARGET = os.environ.get("TIKTOK_TEST_TARGET", "nasa")
ACTOR_DIR = REPO_ROOT / "actors" / "tiktok"
IMAGE = "actor-platform/tiktok:latest"

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def submit(payload: dict):
    return CLIENT.post("/actors/tiktok/runs", json=payload)


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
        args += ["-e", "TIKTOK_SESSION"]
        env["TIKTOK_SESSION"] = session
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
def test_comments_are_collected_unless_turned_off():
    """A run started without touching the box used to collect posts only.

    The schema defaulted collect_comments to false, so a form left alone (or
    an API call that omitted it) skipped the comment stage silently, even with
    max_comments_per_post set. Same rule as the Facebook Actor: on unless the
    caller says false.
    """
    schema = json.loads((ACTOR_DIR / "input_schema.json").read_text())
    assert schema["properties"]["collect_comments"]["default"] is True
    module = load_actor("tiktok_default_actor")
    assert module.parse_settings({"target": "nasa"})["collect_comments"] is True
    off = module.parse_settings({"target": "nasa", "collect_comments": False})
    assert off["collect_comments"] is False


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
        ({"target": TARGET, "mode": "sounds"}, "not an allowed mode"),
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

    code, _, stderr, _ = run_container({"target": TARGET, "mode": "sounds"})
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
    assert "[tiktok]" in stderr, "the Actor's own logs are missing from stderr"
    assert "[tiktok]" not in stdout, "an Actor log line reached stdout"


@test
def test_a_container_run_returns_the_demo_dataset():
    """Demo build: the vendored collector returns synthetic data, so a run
    succeeds with no session at all and its result carries posts and their
    comments in the collector's own shape."""
    if not image_exists():
        return
    code, _, stderr, output = run_container({"target": TARGET, "max_posts": 3,
                                             "analyse": False})
    assert code == 0, f"the demo run failed with {code}: {stderr[-500:]}"
    assert output is not None and output["status"] == "success", output
    assert output["post_count"] == 3, output["post_count"]
    assert output["posts"][0]["comments"], "the demo posts carry no comments"


@test
def test_the_result_preserves_the_collectors_own_document_shape():
    """The Actor must not invent a second Instagram data model."""
    module = load_actor("tt_actor")

    posts_doc = {
        "platform": "TikTok", "scraper": "posts", "status": "success",
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

    assert result["platform"] == "TikTok"
    assert result["date_window"] == posts_doc["date_window"]
    assert result["page_information"] == posts_doc["page_information"]
    assert result["status"] == "success"
    assert result["post_count"] == 1
    assert result["posts"][0]["comments"] == [{"comment_id": "c1", "text": "hi"}]
    assert result["comment_count"] == 1


@test
def test_posts_and_comments_are_streamed_as_items_the_platform_can_parse():
    """Items land beside output.json, one parseable line each, naming their post."""
    import tempfile  # noqa: PLC0415

    from registry.dataset import read_from  # noqa: PLC0415

    module = load_actor("tt_actor_items")
    comments_doc = {"posts": [
        {"post_url": "https://www.tiktok.com/@nasa/video/7429949831272992043",
         "comments": [{"username": "a", "comment": "hi", "reply_comments": [{"comment": "re"}]},
                      {"username": "b", "comment": "yo", "reply_comments": []}]},
        {"post_url": "https://www.tiktok.com/@nasa/video/7000000000000000001", "comments": []},
    ]}
    with tempfile.TemporaryDirectory() as tmp:
        saved = os.environ.get("ACTOR_OUTPUT_PATH")
        os.environ["ACTOR_OUTPUT_PATH"] = os.path.join(tmp, "output.json")
        try:
            assert module.push_items("posts", [("7429949831272992043", {"likes": 3})]) == 1
            assert module.push_items("comments", module.comment_items(comments_doc)) == 2
            assert module.push_items("comments", []) == 0, "nothing to write is not a line"
        finally:
            if saved is None:
                os.environ.pop("ACTOR_OUTPUT_PATH", None)
            else:
                os.environ["ACTOR_OUTPUT_PATH"] = saved

        parsed = read_from(Path(tmp) / "dataset.jsonl")
        assert parsed.malformed == 0, parsed.reasons
        items = [(i.collection, i.key) for i in parsed.items]
        assert items == [("posts", "7429949831272992043"),
                         ("comments", "7429949831272992043"),
                         ("comments", "7429949831272992043")], items
        first = parsed.items[1].data
        assert first["video_id"] == "7429949831272992043" and first["comment"] == "hi"
        assert first["reply_comments"] == [{"comment": "re"}], "replies stay nested"


@test
def test_an_empty_but_valid_window_is_not_reported_as_a_failure():
    """0 posts in a valid range is the collector answering truthfully."""
    module = load_actor("tt_actor2")

    assert "no_posts_found" not in module.FAILURE_STATUSES
    for failing in ("login_wall", "profile_unavailable", "hashtag_unavailable",
                    "error"):
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
    module = load_actor("tt_actor3")
    assert module.PLATFORM == "TikTok"

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
def test_tiktok_is_capped_at_one_run_per_connected_account():
    """Policy in configuration, enforced by the platform -- not a lock inside
    the scraper, and not a cap on the Actor as a whole."""
    from worker.limits import WorkerConfig

    config = WorkerConfig.load(path=REPO_ROOT / "config" / "actors.json")
    profile = config.limits_for("tiktok")
    assert profile.concurrency_scope == "social_account", profile.concurrency_scope
    assert profile.per_key_limit == 1, profile.per_key_limit
    assert profile.max_concurrency is None, profile.max_concurrency
    assert profile.session_env == "TIKTOK_SESSION"
    assert profile.session_out is True, "the refreshed session must come back"

    for path in (ACTOR_DIR / "vendor" / "collectors" / "tiktok").glob("*.py"):
        assert "max_concurrency" not in path.read_text().lower(), (
            f"{path.name} implements its own concurrency cap"
        )


@test
def test_the_session_policy_names_no_credential_as_the_account_id():
    """session_id_cookie is DISPLAYED. It must never be the session cookie.

    The platform requires session_id_cookie to be one of session_cookies, and
    the only cookie here that proves a session is the credential itself -- so
    the only safe answer is to name none, and show a connected account with no
    id rather than publish the session token as one.
    """
    from shared.platform_policy import policy_for, reset_cache

    reset_cache()
    policy = policy_for("tiktok")
    assert policy.session_cookies == ("sessionid",), policy.session_cookies
    assert policy.session_id_cookie is None, (
        f"session_id_cookie is {policy.session_id_cookie!r}; a credential must not be "
        "shown as the account's visible id"
    )
    assert policy.connectable


@test
def test_the_actor_appears_in_the_catalog_with_its_schema():
    response = CLIENT.get("/actors")
    assert response.status_code == 200
    actors = {a["name"]: a for a in response.json()["actors"]}
    assert "tiktok" in actors, list(actors)

    schema = actors["tiktok"].get("input_schema")
    assert schema and schema.get("required") == ["target"], schema
    assert "start_date" in schema["properties"] and "end_date" in schema["properties"]
    assert "mode" in schema["properties"]
    # Facebook-only options must not have been copied across.
    assert "comment_sort" not in schema["properties"]
    assert "page_concurrency" not in schema["properties"]
    # It is a real scraper, so it is OFFERED -- unlike the test fixtures.
    assert actors["tiktok"].get("hidden") is False

    body = response.text
    for leak in ("/actors/", "Dockerfile", "vendor", str(REPO_ROOT)):
        assert leak not in body, f"the catalog leaked {leak!r}"


# --- the persistent browser profile -----------------------------------------
#
# WHAT THESE ARE ABOUT. The collector keeps a persistent Playwright profile so
# a device the site has already checked stays checked. In a container that only
# works if the profile lives on a mount that OUTLIVES the container -- which it
# did not: it sat in the run's mkdtemp and died with it, so every run arrived
# as a brand new device. These cover the mount, its isolation, and the two
# things about it that would be a security failure rather than a bug.


def _state_bits():
    """The platform pieces under test, imported here rather than at module
    scope so the rest of the file still runs if the worker cannot be imported."""
    from shared.models import ACTOR_STATE_DIR, ENV_STATE_DIR
    from worker.executor import _container_args, _persistent_state_dir, _state_dir_for
    from worker.limits import load_actor_config
    from shared.models import Run

    return (ACTOR_STATE_DIR, ENV_STATE_DIR, _container_args, _persistent_state_dir,
            _state_dir_for, load_actor_config, Run)


def _run_for(account: str | None, actor: str = "tiktok"):
    from shared.models import Run, RunStatus

    return Run(run_id="20260101-000001-aaaaaa", actor=actor, status=RunStatus.PENDING,
               created_at="2026-01-01T00:00:00Z", run_dir="/tmp/probe",
               social_account_id=account)


@test
def test_a_connections_browser_profile_path_is_deterministic_and_isolated():
    """The same account resolves to the same directory; two never share one."""
    _, _, _, _persistent_state_dir, _state_dir_for, load_actor_config, _ = _state_bits()

    root = Path(tempfile.mkdtemp(dir=_SANDBOX))
    original = os.environ.get("ACTOR_PLATFORM_STATE_DIR")
    os.environ["ACTOR_PLATFORM_STATE_DIR"] = str(root)
    try:
        first = _persistent_state_dir("tiktok", "acct-1")
        again = _persistent_state_dir("tiktok", "acct-1")
        other = _persistent_state_dir("tiktok", "acct-2")
        assert first == again, f"the same account moved: {first} != {again}"
        assert first != other, "two accounts resolved to ONE profile directory"
        assert other.parent == first.parent, "accounts should be siblings under the actor"

        # Run 2 finds what run 1 left. This is the whole point of the mount.
        (first / "Cookies").write_text("device state from run 1")
        assert (_persistent_state_dir("tiktok", "acct-1") / "Cookies").read_text() == (
            "device state from run 1"), "run 2 did not see run 1's profile"

        # An account id is not a path. It arrives from outside the worker.
        escaped = _persistent_state_dir("tiktok", "../../etc")
        assert escaped.parent == first.parent, f"traversal escaped the root: {escaped}"

        # 0700: another user on the host must not be able to read a profile or
        # even list which accounts are connected.
        assert oct(first.stat().st_mode)[-3:] == "700", oct(first.stat().st_mode)
        assert oct(first.parent.stat().st_mode)[-3:] == "700"

        # OUTSIDE the artifact tree, which is what the API serves.
        assert "storage/runs" not in str(first), first

        # No account, no mount -- a shared directory would hand one user's
        # logged-in profile to another user's run.
        _, _, actors = load_actor_config(str(REPO_ROOT / "config" / "actors.json"))
        profile = actors["tiktok"]
        assert profile.persistent_state, "this Actor should ask for persistent state"
        assert _state_dir_for(_run_for("acct-1"), profile) is not None
        assert _state_dir_for(_run_for(None), profile) is None, (
            "a run with no connected account was given a state mount")
        assert _state_dir_for(_run_for("acct-1"), actors["facebook"]) is None, (
            "an Actor that never asked for persistent state was given a mount")
    finally:
        if original is None:
            os.environ.pop("ACTOR_PLATFORM_STATE_DIR", None)
        else:
            os.environ["ACTOR_PLATFORM_STATE_DIR"] = original


@test
def test_the_container_gets_exactly_one_scoped_profile_mount():
    """The argv mounts ONE account's directory, and nothing without it."""
    ACTOR_STATE_DIR, ENV_STATE_DIR, _container_args, _, _, _, _ = _state_bits()

    host = Path("/host/state/tiktok/acct-1")
    args = _container_args("tiktok", "20260101-000001-aaaaaa", Path("/tmp/probe"),
                           state_dir=host)
    assert f"{host}:{ACTOR_STATE_DIR}" in args, args
    assert f"{ENV_STATE_DIR}={ACTOR_STATE_DIR}" in args, args

    # The mount is the LEAF, never a parent: mounting the actor directory would
    # put every connected account's profile inside one run's container.
    mounts = [a for i, a in enumerate(args) if i and args[i - 1] == "-v"]
    assert all(not m.startswith("/host/state:") and not m.startswith("/host/state/tiktok:")
               for m in mounts), mounts
    assert not any(m.startswith("/:") or m.startswith(f"{os.path.expanduser('~')}:")
                   for m in mounts), f"a mount is far too broad: {mounts}"

    # And with no state directory the argv is byte-identical to the old one --
    # every other Actor is untouched by this.
    plain = _container_args("tiktok", "20260101-000001-aaaaaa", Path("/tmp/probe"))
    trimmed = [a for a in args if a not in (f"{host}:{ACTOR_STATE_DIR}",
                                            f"{ENV_STATE_DIR}={ACTOR_STATE_DIR}")]
    # -v and -e each lost their value above; drop the now-dangling flags.
    while len(trimmed) > len(plain):
        for i, a in enumerate(trimmed):
            if a in ("-v", "-e") and (i + 1 >= len(trimmed) or trimmed[i + 1] in ("-v", "-e")
                                      or trimmed[i + 1].startswith("--")):
                del trimmed[i]
                break
        else:
            break
    assert trimmed == plain, f"the no-mount argv changed:\n{trimmed}\n{plain}"


@test
def test_the_profile_survives_the_container_and_never_leaks_into_output_or_logs():
    """Two container runs over one mount: it persists, and stays private."""
    if not image_exists():
        return

    ACTOR_STATE_DIR = _state_bits()[0]
    host = Path(tempfile.mkdtemp(dir=_SANDBOX))
    host.chmod(0o777)   # the container runs as this user; keep it simple here

    def run_once(payload: dict):
        workdir = Path(tempfile.mkdtemp(dir=_SANDBOX))
        (workdir / "input.json").write_text(json.dumps(payload))
        workdir.chmod(0o777)
        done = subprocess.run(
            ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
             "-v", f"{workdir}:/actor/storage",
             "-v", f"{host}:{ACTOR_STATE_DIR}",
             "-e", f"ACTOR_STATE_DIR={ACTOR_STATE_DIR}",
             IMAGE],
            capture_output=True, text=True, timeout=180,
        )
        output = None
        if (workdir / "output.json").is_file():
            output = json.loads((workdir / "output.json").read_text())
        return done, output

    # An input the Actor rejects: it exercises the mount and then exits in a
    # second, instead of spending a minute being challenged by the live site.
    first, _ = run_once({"target": ""})
    profile = host / "browser-profile"
    assert profile.is_dir(), (
        f"the container did not create its profile on the mount: {first.stderr[-600:]}")
    assert "new, will be created" in first.stderr, first.stderr[-600:]

    # THE CONTAINER IS GONE (--rm) AND THE PROFILE IS STILL HERE. That is the
    # limitation this change exists to remove.
    canary = "CANARY-PROFILE-CONTENT-b3f9"
    (profile / "Cookies").write_text(canary)

    second, output = run_once({"target": ""})
    assert (profile / "Cookies").read_text() == canary, "run 2 destroyed run 1's profile"
    assert "reusing existing state" in second.stderr, second.stderr[-600:]

    # The profile holds a logged-in browser's cookies and local storage. None
    # of it may reach the run's output or its log -- both are served over HTTP.
    assert canary not in (second.stdout + second.stderr), "profile contents were LOGGED"
    assert canary not in json.dumps(output or {}), "profile contents reached the output"
    assert str(host) not in json.dumps(output or {}), "the host state path reached the output"


# --- the platform stayed generic --------------------------------------------


@test
def test_the_host_namespace_is_opt_in_and_excludes_a_published_port():
    """NO Actor shares the host network now, and the mechanism stays guarded.

    TikTok used to. A browser the platform LAUNCHES was served HTTP 403 on a
    VIDEO page while a browser the operator had started returned every comment,
    so the Actor attached to that one -- and reaching it meant the host's
    loopback. That was a COMMENT-stage problem, and comments are now off
    (/api/comment/list answers with an empty 200 in every automated browser
    measured). POSTS were never affected: a launched HEADLESS browser on the
    durable per-account profile returns a full item_list, measured live.

    So the flag is off, and the cost of leaving it on was concrete: with
    TIKTOK_CDP_URL forwarded and no Chrome on 9222, every run died on
    `connect ECONNREFUSED 127.0.0.1:9222` after passing robots, session and
    profile setup -- which reads as a broken Actor rather than as a missing
    browser nobody meant to require.

    The MECHANISM is still tested rather than deleted, because turning it back
    on must stay safe: host networking adds no exposure only while the debug
    port stays bound to 127.0.0.1 and is published nowhere. Binding it to the
    docker bridge or to 0.0.0.0 would publish an UNAUTHENTICATED control
    interface for a logged-in browser to everything that can reach it.
    """
    _, _, _container_args, _, _, _, _ = _state_bits()
    from worker.limits import ActorProfile, LimitError, WorkerConfig

    run_dir = Path("/tmp/probe")
    plain = _container_args("tiktok", "20260101-000001-aaaaaa", run_dir)
    off = _container_args("tiktok", "20260101-000001-aaaaaa", run_dir,
                          host_network=False)
    assert plain == off, "the default is not the same as asking for none"
    assert "--network" not in plain, f"a network mode appears unasked: {plain}"

    shared = _container_args("tiktok", "20260101-000001-aaaaaa", run_dir,
                             host_network=True)
    assert shared[shared.index("--network") + 1] == "host", shared
    # Nothing is published: a port mapping has no meaning in this namespace,
    # and emitting one would claim an isolation this container does not have.
    assert "-p" not in shared, f"a port is published in the host namespace: {shared}"

    config = WorkerConfig.load(path=str(REPO_ROOT / "config" / "actors.json"))
    for name in ("tiktok", "facebook", "instagram", "linkedin", "x", "threads"):
        assert config.limits_for(name).host_network is False, (
            f"{name} asks for the host network namespace; nothing needs it now, "
            "and TikTok's reason for it (attaching to the operator's browser to "
            "collect comments) is gone")

    # AND NOTHING FORWARDS THE CDP VARIABLE, which is the other half of the same
    # arrangement: host networking only ever existed so that address resolved.
    # Forwarding it with no browser listening is what failed every run.
    run_local = (REPO_ROOT / "run-local.sh").read_text(encoding="utf-8")
    forwarded = [line for line in run_local.splitlines()
                 if line.startswith("export ACTOR_FORWARD_ENV=")]
    assert forwarded, "ACTOR_FORWARD_ENV is no longer exported"
    assert "TIKTOK_CDP" not in forwarded[0], forwarded[0]

    # The two are refused TOGETHER rather than silently producing argv whose
    # halves contradict each other: the container would bind the host's own
    # port directly, and the API would proxy to a port no run owns.
    try:
        ActorProfile(host_network=True, interactive_display=True)
    except LimitError:
        pass
    else:
        raise AssertionError("host_network + interactive_display was accepted")


@test
def test_a_display_outside_loopback_is_refused_rather_than_recorded():
    """Reading the port back must never accept a routable binding.

    The API proxies whatever port the record names. A display bound to 0.0.0.0
    would be reachable from off-host by anyone who guessed it, so the Worker
    refuses to record one -- rather than recording it and relying on the
    proxy's authentication to make up the difference.
    """
    import subprocess as sp
    from worker import executor

    real = sp.run
    answers = {
        "127.0.0.1:32768": 32768,     # the normal case
        "[::1]:32769": 32769,         # loopback over v6
        "0.0.0.0:32770": None,        # REFUSED
        "192.168.1.5:32771": None,    # REFUSED
        "": None,                     # container not up yet
    }
    try:
        for stdout, expected in answers.items():
            sp.run = lambda *a, **k: sp.CompletedProcess(
                a[0], 0 if stdout else 1, stdout=stdout, stderr="")
            executor.subprocess.run = sp.run
            got = executor.display_port("20260101-000001-aaaaaa")
            assert got == expected, f"{stdout!r} -> {got}, expected {expected}"
    finally:
        sp.run = real
        executor.subprocess.run = real


class _FakeDiagnosis:
    reason = "a verification page is being shown"
    error_code = "TIKTOK_CHALLENGE"


@test
def test_reply_count_sits_beside_comment_count():
    """`comment_count` is TOP-LEVEL comments; `reply_count` is what nests under
    them. A live run printed "96 unique replies" in its log and
    `comment_count: 86` in its result - ten replies-to-comments, counted in one
    and not the other, which read as ten lost. Both numbers now appear, and
    they add up to what the log says."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("tiktok_reply_actor", ACTOR_DIR / "actor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    posts_doc = {
        "platform": "TikTok", "scraper": "posts", "status": "success",
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



@test
def test_a_partial_comment_stage_is_not_reported_as_a_whole_one():
    """A video with good metrics and failed comments must READ as partial.

    `status` on a comment record was set to "success" unconditionally at the end
    of the scrape, so a video reporting 20,000 comments that yielded none was
    filed identically to one whose comments were all collected - and the keys
    this adapter copied into output.json could not express the difference either.
    The exit code deliberately stays with the POSTS document (a comment failure
    must not fail a good post scrape, and FAILURE_STATUSES is read against the
    posts status alone), so saying it in the result is the whole mechanism.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("tiktok_partial_actor",
                                                  ACTOR_DIR / "actor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    posts_doc = {
        "platform": "TikTok", "scraper": "posts", "status": "success",
        "page_information": {"page_name": "t", "page_url": "https://example.com/t"},
        "posts": [{"post_url": "https://example.com/p/1"},
                  {"post_url": "https://example.com/p/2"}],
    }
    comments_doc = {"posts": [
        {"post_url": "https://example.com/p/1", "video_id": "1",
         "status": "success", "comments_status": "success",
         "replies_status": "success", "collection_complete": True,
         "requested_comments": 2, "comments_scraped": 2,
         "comment_pages_collected": 1, "stop_reason": "requested_met",
         "comments": [{"comment_id": "a"}, {"comment_id": "b"}]},
        {"post_url": "https://example.com/p/2", "video_id": "2",
         "status": "success", "comments_status": "failed",
         "replies_status": "failed", "collection_complete": False,
         "requested_comments": 20, "comments_scraped": 0,
         "comment_responses": 6, "comment_pages_collected": 0,
         "stop_reason": "fetch_failed", "failure_reason": "fetch_failed",
         "comments": []},
    ]}

    result = module.build_result(posts_doc, comments_doc)

    # The per-stage keys have to REACH output.json. The copy list filters by
    # `if key in record`, so a key it does not name is silently absent rather
    # than an error - which is how `stop_reason` existed for weeks and never
    # once appeared in a result.
    good = result["posts"][0]["comment_collection"]
    bad = result["posts"][1]["comment_collection"]
    assert good["comments_status"] == "success", good
    assert bad["comments_status"] == "failed", bad
    assert bad["stop_reason"] == "fetch_failed", bad
    assert bad["collection_complete"] is False, bad
    assert bad["requested_comments"] == 20, bad

    stage = result["comment_collection_summary"]
    assert stage["complete"] is False, stage
    assert stage["comments_status"] == {"success": 1, "failed": 1}, stage
    # NAMED, not counted: a reader chasing a short result wants to know which
    # video fell short and why, and the reasons differ per video.
    assert [v["video_id"] for v in stage["incomplete_videos"]] == ["2"], stage
    assert stage["incomplete_videos"][0]["stop_reason"] == "fetch_failed", stage

    summary = module.summarise(result)
    assert summary["comments_partial"] == 1, summary

    # A run whose comments all worked says nothing extra - an ordinary result is
    # unchanged, so this cannot become noise on every successful scrape.
    whole = module.build_result(posts_doc, {"posts": [comments_doc["posts"][0]]})
    assert whole["comment_collection_summary"]["complete"] is True
    assert "comments_partial" not in module.summarise(whole)


@test
def test_the_reply_cap_is_something_the_platform_can_actually_ask_for():
    """Replies had NO input field, only TIKTOK_REPLIES_PER_COMMENT.

    That env var is read at import, so the only way to change the reply budget
    was to rebuild or re-run the image with a different environment - the
    platform could not express it per run at all, while `max_comments_per_post`
    beside it could. The cap is PER COMMENT, which is the part worth stating in
    the schema: a single comment can carry thousands of replies and the rest of
    the batch waits behind it.
    """
    schema = json.loads((ACTOR_DIR / "input_schema.json").read_text())
    props = schema["properties"]
    field = props["max_replies_per_comment"]
    assert field["type"] == "integer", field
    # 0 is MEANINGFUL - "collect the comments but none of their replies" - so
    # the floor is 0 here, unlike every other cap in this schema.
    assert field["minimum"] == 0, field
    assert field["maximum"] == 200, field
    assert "each comment" in field["description"].lower(), field["description"]

    # The stale notice is gone: the console used to tell everyone comments were
    # unavailable and that "the three comment fields are ignored" - of which
    # there were two, and they were honoured.
    assert "COMMENTS ARE CURRENTLY UNAVAILABLE" not in schema["description"]
    assert "three comment fields" not in schema["description"]

    import importlib.util

    spec = importlib.util.spec_from_file_location("tiktok_cap_actor",
                                                  ACTOR_DIR / "actor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    settings = module.parse_settings({"target": "nasa", "collect_comments": True,
                                   "max_replies_per_comment": 5})
    assert settings["max_replies_per_comment"] == 5, settings
    # Unset stays unset, so the collector's own default survives rather than
    # being overwritten by a number this adapter invented.
    assert module.parse_settings({"target": "nasa"})["max_replies_per_comment"] is None

    # ...and the collector accepts it under that name, which is the join that
    # a schema change cannot verify on its own.
    import inspect
    import sys
    sys.path.insert(0, str(ACTOR_DIR / "vendor"))
    try:
        from collectors.tiktok import collect_profile_comments
        params = inspect.signature(collect_profile_comments).parameters
        assert "max_replies_per_comment" in params, list(params)
    finally:
        sys.path.remove(str(ACTOR_DIR / "vendor"))



@test
def test_the_collector_is_pointed_at_scratch_before_anything_imports_it():
    """DATA_DIR IS FROZEN AT IMPORT TIME, so the order inside main() is load-bearing.

    collectors/tiktok/common.py computes DATA_DIR - and STORAGE_STATE_FILE under
    it - from TIKTOK_DATA_DIR at IMPORT time. The Actor points that at a scratch
    directory, so ANY import of the collector before that line freezes the
    image's default and nothing later can move it.

    MEASURED: 7a714c7 put a collector import inside configure_logging(), nine
    lines above the assignment, and run 20261003-152946496031 failed with
    `installed a session with 57 cookie(s)` followed immediately by `No usable
    /actor/data/tiktok/tiktok_auth.json found` - install_session had written the
    jar to scratch while the collector was reading the frozen default. One bug,
    two messages that look unrelated.

    Tested two ways, because each catches what the other misses: the OUTCOME in a
    clean interpreter, and the ORDER in main(), which is what actually regressed.
    """
    import ast
    import shutil
    import subprocess
    import sys

    # -- 1. the outcome, in a fresh interpreter so nothing is already imported --
    probe = (
        "import importlib.util, os, sys\n"
        f"sys.path.insert(0, {str(ACTOR_DIR / 'vendor')!r})\n"
        f"spec = importlib.util.spec_from_file_location('a', {str(ACTOR_DIR / 'actor.py')!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "work = m.point_collector_at_scratch()\n"
        "m.configure_logging()\n"          # the import that used to be too early
        "from collectors.tiktok import common\n"
        "print(work); print(common.DATA_DIR); print(common.STORAGE_STATE_FILE)\n"
    )
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                          text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    work, data_dir, jar = done.stdout.strip().splitlines()[:3]
    try:
        assert data_dir == work, f"collector reads {data_dir}, run writes {work}"
        # The jar install_session writes and the jar require_auth_file reads have
        # to be the same file - that equality IS the bug this guards.
        assert jar == os.path.join(work, "tiktok_auth.json"), jar
        assert "/actor/data" not in jar, f"froze the image default: {jar}"
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # -- 2. the order in main(), which is the thing that regressed -------------
    tree = ast.parse((ACTOR_DIR / "actor.py").read_text(encoding="utf-8"))
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "main" in funcs and "point_collector_at_scratch" in funcs

    def imports_collector(node) -> bool:
        for sub in ast.walk(node):
            if isinstance(sub, ast.ImportFrom) and (sub.module or "").startswith("collectors"):
                return True
            if isinstance(sub, ast.Import) and any(
                    a.name.startswith("collectors") for a in sub.names):
                return True
        return False

    # Every helper that reaches the collector, so a future one is covered too -
    # the broken call was configure_logging(), which looks innocent from main().
    reaching = {name for name, fn in funcs.items() if imports_collector(fn)}
    assert "configure_logging" in reaching, (
        "configure_logging no longer imports the collector - if the pin moved, "
        "point this test at whatever imports it now"
    )

    def first_touch(body) -> int | None:
        for i, stmt in enumerate(body):
            if imports_collector(stmt):
                return i
            for sub in ast.walk(stmt):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                        and sub.func.id in reaching):
                    return i
        return None

    def index_of_call(body, name) -> int | None:
        for i, stmt in enumerate(body):
            for sub in ast.walk(stmt):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                        and sub.func.id == name):
                    return i
        return None

    pointed = index_of_call(funcs["main"].body, "point_collector_at_scratch")
    touched = first_touch(funcs["main"].body)
    assert pointed is not None, "main() never points the collector at scratch"
    assert touched is not None, "no collector use found in main() - test is stale"
    assert pointed < touched, (
        f"main() reaches the collector at statement {touched} but only points it "
        f"at scratch at {pointed} - DATA_DIR will freeze to the image default"
    )


def main() -> int:
    failures = 0
    print(f"running {len(TESTS)} TikTok Actor tests")
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
