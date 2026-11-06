#!/usr/bin/env python3
"""Facebook Actor integration tests.

The claim under test is not "Facebook works". It is that a real, slow,
network-heavy Actor fits the platform WITHOUT the platform knowing anything
about it: same API, same queue, same leases, same crash recovery, same
concurrency, same failure classification as hello_world.

These hit the live network. A session in FB_SESSION unlocks the paginated feed;
without one the tests still run against real Facebook, just with the two or
three posts it server-renders.

    FB_SESSION='c_user=...; xs=...' python3 test_facebook.py
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
from contextlib import contextmanager
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
from shared.models import RunStatus  # noqa: E402

_SANDBOX = Path(tempfile.mkdtemp(prefix="actor-facebook-tests-"))
atexit.register(shutil.rmtree, _SANDBOX, ignore_errors=True)

REGISTRY = RunRegistry(_SANDBOX / "runs")
QUEUE = open_queue(str(_SANDBOX / "queue"))

API_KEY = generate_key()
AUTH = {"Authorization": f"Bearer {API_KEY}"}
CLIENT = TestClient(
    create_app(registry=REGISTRY, queue=QUEUE, auth=AuthPolicy.for_keys({API_KEY})),
    headers=AUTH,
)

CONSUMER = REPO_ROOT / "worker" / "consumer.py"
SESSION = os.environ.get("FB_SESSION", "")
TARGET = os.environ.get("FB_TEST_TARGET", "nasa")
TERMINAL = {"SUCCEEDED", "FAILED", "PLATFORM_ERROR"}

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def worker_env() -> dict[str, str]:
    """The Worker's environment. The session is forwarded BY NAME."""
    env = {**os.environ, "ACTOR_FORWARD_ENV": "FB_SESSION"}
    if SESSION:
        env["FB_SESSION"] = SESSION
    return env


def start_worker(max_jobs: int = 1, *, drain: bool = True) -> subprocess.Popen:
    args = [
        sys.executable, str(CONSUMER),
        "--queue-url", str(_SANDBOX / "queue"),
        "--registry-root", str(_SANDBOX / "runs"),
        "--reconcile-interval", "0",
        "--max-jobs", str(max_jobs), "--poll-timeout", "0.2",
    ]
    if drain:
        args.append("--drain")
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=worker_env())


@contextmanager
def worker(max_jobs: int = 1, *, drain: bool = True):
    process = start_worker(max_jobs, drain=drain)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=60)


def submit(payload: dict):
    return CLIENT.post("/actors/facebook/runs", json=payload)


def wait_for_terminal(run_id: str, seconds: float = 900.0) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        doc = CLIENT.get(f"/runs/{run_id}").json()
        if doc["status"] in TERMINAL:
            return doc
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} never finished; it is {doc['status']}")


def running_actor_containers() -> set[str]:
    result = subprocess.run(
        ["docker", "ps", "--filter", "name=actor-", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    return {line for line in result.stdout.split() if line}


# --- the adapter contract ---------------------------------------------------
# These test the ADAPTER, not the scraper. The scraper has its own suite in the
# pipeline repository (test_daterange.py drives its paging loops through fake
# transports) and is vendored in verbatim -- re-testing its internals here would
# fork the very thing this integration exists not to fork.

ACTOR_DIR = REPO_ROOT / "actors" / "facebook"


def run_container(payload: dict, *, session: str | None = None, seconds: int = 300):
    """Run the Actor image directly. -> (exit code, stdout, stderr, output.json).

    Direct rather than through the queue, because these tests are about the
    Actor's own contract: what it does with an input, where its logs go, what
    it leaves at ACTOR_OUTPUT_PATH and what it exits with.
    """
    workdir = Path(tempfile.mkdtemp(dir=_SANDBOX))
    (workdir / "input.json").write_text(json.dumps(payload))
    workdir.chmod(0o777)

    args = ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{workdir}:/actor/storage"]
    env = {**os.environ}
    if session is not None:
        args += ["-e", "FB_SESSION"]
        env["FB_SESSION"] = session
    args.append("actor-platform/facebook:latest")

    done = subprocess.run(args, capture_output=True, text=True, timeout=seconds, env=env)
    output = None
    if (workdir / "output.json").is_file():
        output = json.loads((workdir / "output.json").read_text())
    return done.returncode, done.stdout, done.stderr, output


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
        ({"target": "nasa", "max_posts": 0}, "max_posts below the minimum"),
        ({"target": "nasa", "max_posts": "lots"}, "max_posts must be an integer"),
        ({"target": "nasa", "start_date": "last tuesday"}, "unparseable date"),
        ({"target": "nasa", "comment_sort": "best"}, "not an allowed sort"),
        ({"target": "nasa", "nonsense": 1}, "unknown field"),
    ):
        response = submit(payload)
        assert response.status_code == 400, f"{why}: got {response.status_code}"

    assert QUEUE.depth() == 0, "an invalid request queued a job"
    assert len(REGISTRY.list(limit=500)) == before, "an invalid request created a run"


@test
def test_the_actor_validates_its_own_input_too():
    """The Actor is runnable by hand, so it cannot rely on having been checked."""
    code, _, stderr, _ = run_container({"max_posts": 3})
    assert code == 1, f"expected an Actor failure, got {code}"
    assert "target" in stderr

    code, _, stderr, _ = run_container({"target": "nasa", "max_posts": "lots"})
    assert code == 1
    assert "must be an integer" in stderr


@test
def test_an_inverted_date_range_is_an_actor_failure_not_a_crash():
    code, stdout, stderr, _ = run_container(
        {"target": "nasa", "start_date": "2026-08-31", "end_date": "2026-08-01"}
    )
    assert code == 1, code
    assert "inverted" in stderr, stderr[-300:]
    assert "Traceback" not in stderr, "an invalid range should be reported, not raised"


@test
def test_stdout_is_valid_json_and_every_log_line_goes_to_stderr():
    """Measured, not assumed: the collector reports through BOTH logging and
    bare print(), and its print() calls landed on stdout until the adapter
    redirected the stream."""
    code, stdout, stderr, _ = run_container({"target": "nasa", "max_posts": 2})

    assert stdout.strip(), "the Actor printed nothing on stdout"
    parsed = json.loads(stdout)          # raises if a log line leaked in
    assert isinstance(parsed, dict) and "status" in parsed, parsed

    assert len(stdout.splitlines()) == 1, (
        f"stdout carried {len(stdout.splitlines())} lines; it must be the result alone"
    )
    assert "[facebook]" in stderr, "the Actor's own logs are missing from stderr"
    assert "[facebook]" not in stdout, "an Actor log line reached stdout"


@test
def test_a_container_run_returns_the_demo_dataset():
    """Demo build: the vendored collector returns synthetic data, so a run
    succeeds with no session at all and its result carries posts and their
    comments in the collector's own shape."""
    code, _, stderr, output = run_container({"target": "nasa", "max_posts": 3,
                                             "analyse": False})
    assert code == 0, f"the demo run failed with {code}: {stderr[-500:]}"
    assert output is not None and output["status"] == "success", output
    assert output["post_count"] == 3, output["post_count"]
    assert output["posts"][0]["comments"], "the demo posts carry no comments"


@test
def test_the_result_preserves_the_collectors_own_document_shape():
    """The Actor must not invent a second Facebook data model: page_information,
    posts and date_window are the collector's own, passed through."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("fb_actor", ACTOR_DIR / "actor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    posts_doc = {
        "platform": "Facebook", "scraper": "posts", "status": "success",
        "page_information": {"page_name": "NASA", "page_url": "https://facebook.com/nasa",
                             "followers": 100, "page_type": "page"},
        "posts": [{"post_url": "https://facebook.com/p/1", "post_date": "2026-08-01T00:00:00Z",
                   "caption": "hello", "likes": 3}],
        "date_window": {"start": "2026-08-01", "end": "2026-08-31", "complete": True,
                        "stop_reason": "window_cleared", "window_reached": True,
                        "oldest_seen": "2026-08-01T00:00:00Z"},
    }
    comments_doc = {"posts": [{
        "post_url": "https://facebook.com/p/1",
        "comments": [{"comment_id": "c1", "text": "hi"}],
        "stop_reason": "source_exhausted", "collection_complete": True,
        "reported_comment_count": 1, "comment_sort": "relevant",
    }]}

    result = module.build_result(posts_doc, comments_doc)

    # The date-window accounting survives INTACT -- this is the behaviour the
    # brief singles out, and an adapter that summarised it away would destroy
    # the distinction between window_cleared and page_budget.
    assert result["date_window"] == posts_doc["date_window"]
    assert result["page_information"] == posts_doc["page_information"]
    assert result["status"] == "success"
    assert result["post_count"] == 1
    # Comments are attached to their post WITH their accounting.
    assert result["posts"][0]["comments"] == [{"comment_id": "c1", "text": "hi"}]
    assert result["posts"][0]["comment_collection"]["stop_reason"] == "source_exhausted"
    assert result["comment_count"] == 1

    # An empty window is a SUCCESS, not a failure.
    assert "no_posts_found" not in module.FAILURE_STATUSES
    for failing in ("login_wall", "page_unavailable", "error", "fetch_failed"):
        assert failing in module.FAILURE_STATUSES


@test
def test_an_empty_but_valid_window_is_not_reported_as_a_failure():
    """The specific misclassification the brief warns about: 0 posts in a valid
    range is the collector answering truthfully, not a fetch failure."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("fb_actor2", ACTOR_DIR / "actor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert "no_posts_found" not in module.FAILURE_STATUSES
    empty = module.build_result(
        {"status": "no_posts_found", "page_information": {}, "posts": [],
         "date_window": {"complete": True, "stop_reason": "window_cleared",
                         "window_reached": True}},
        None,
    )
    assert empty["post_count"] == 0
    assert empty["date_window"]["complete"] is True
    assert module.summarise(empty)["date_window"]["stop_reason"] == "window_cleared"


@test
def test_facebook_is_capped_at_one_run_per_connected_account():
    """Policy, in configuration, enforced by the platform -- not a lock inside
    the scraper.

    The cap USED to be max_concurrency: 1, i.e. one run of this Actor at a time.
    That was the wrong shape twice over. The real constraint is that the site
    rotates its session cookie server-side during a run, so two scrapes on the
    SAME session race the write-back -- which says nothing about two scrapes on
    two different people's sessions. Capping the Actor throttled every user to
    serialise a constraint that only ever applied within one account, and did
    not even achieve that, since the counter enforcing it was per worker
    PROCESS.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from worker.limits import WorkerConfig

    config = WorkerConfig.load(path=REPO_ROOT / "config" / "actors.json")
    profile = config.limits_for("facebook")
    assert profile.concurrency_scope == "social_account", profile.concurrency_scope
    assert profile.per_key_limit == 1, profile.per_key_limit
    # The Actor as a whole is NOT capped: different accounts may run at once.
    assert profile.max_concurrency is None, profile.max_concurrency

    # And nothing in the scraper implements it.
    for path in (ACTOR_DIR / "vendor" / "collectors" / "facebook").glob("*.py"):
        text = path.read_text().lower()
        assert "max_concurrency" not in text, f"{path.name} implements its own concurrency cap"


@test
def test_the_actor_appears_in_the_catalog_with_its_schema():
    response = CLIENT.get("/actors")
    assert response.status_code == 200
    actors = {a["name"]: a for a in response.json()["actors"]}
    assert "facebook" in actors, list(actors)

    schema = actors["facebook"].get("input_schema")
    assert schema and schema.get("required") == ["target"], schema
    assert "start_date" in schema["properties"] and "end_date" in schema["properties"]
    # No filesystem or Docker detail may leak through the API.
    body = response.text
    for leak in ("/actors/", "Dockerfile", "vendor", str(REPO_ROOT)):
        assert leak not in body, f"the catalog leaked {leak!r}"


# --- 12: the platform stayed generic ----------------------------------------


# --- batched delivery (offline) ----------------------------------------------
# These drive actor.run() IN PROCESS with the collector's two entry points
# faked, so they need no Docker, no network and no session. What is under test
# is the ADAPTER's batching, not the scraper.

def _load_actor():
    """Import actor.py the way the image does: vendor/ on the path."""
    for path in (str(ACTOR_DIR), str(ACTOR_DIR / "vendor")):
        if path not in sys.path:
            sys.path.insert(0, path)
    import importlib
    import actor  # noqa: PLC0415
    return importlib.reload(actor)


def _fake_run(target: str, n_posts: int, *, comments: bool = True):
    """Run the Actor on a fake collector. -> (result, dataset rows, comment calls)."""
    actor = _load_actor()
    import collectors.facebook as fb  # noqa: PLC0415

    posts = [{"post_url": f"https://www.facebook.com/groups/1/posts/{i}/",
              "post_date": f"2026-08-01T00:00:00+00:00".replace(
                  "T00:00", f"T{i // 60 % 24:02d}:{i % 60:02d}"),
              # Likes deliberately UNRELATED to date, or engagement order and
              # newest-first would coincide and the order check prove nothing.
              "caption": f"post {i}", "likes": (i * 37) % 101} for i in range(n_posts)]
    # The collector returns posts RANKED BY ENGAGEMENT, not by date.
    posts.sort(key=lambda p: -p["likes"])
    calls: list[list[str]] = []

    def fake_posts(target, num_posts=None, window=None, concurrency=None):
        return {"status": "success", "page_information": {"page_url": target},
                "posts": [dict(p) for p in posts]}

    def fake_comments(urls, **kwargs):
        calls.append(list(urls))
        return {"posts": [{"post_url": u, "status": "success",
                           "comments": [{"comment": f"on {u}", "reply_comments": []}],
                           "stop_reason": "source_exhausted"} for u in urls]}

    saved = (fb.collect_page_posts, fb.collect_page_comments)
    fb.collect_page_posts, fb.collect_page_comments = fake_posts, fake_comments
    out_dir = Path(tempfile.mkdtemp(dir=_SANDBOX))
    old_env = os.environ.get("ACTOR_OUTPUT_PATH")
    os.environ["ACTOR_OUTPUT_PATH"] = str(out_dir / "output.json")
    try:
        settings = actor.parse_settings({"target": target,
                                         "collect_comments": comments,
                                         "analyse": False})
        result, code = actor.run(settings)
    finally:
        fb.collect_page_posts, fb.collect_page_comments = saved
        if old_env is None:
            os.environ.pop("ACTOR_OUTPUT_PATH", None)
        else:
            os.environ["ACTOR_OUTPUT_PATH"] = old_env
    assert code == 0, code
    dataset = out_dir / "dataset.jsonl"
    lines = dataset.read_text().splitlines() if dataset.is_file() else []
    # Through the PLATFORM'S OWN parser, the one the worker ingests with. The
    # first live run through the worker skipped every batch item ("invalid
    # 'collection'": `#` was not allowed), which no test here could see while
    # it only json.loads-ed the file.
    from registry.dataset import parse_line  # noqa: PLC0415
    for seq, line in enumerate(lines, 1):
        item, reason = parse_line(line.encode("utf-8"), seq)
        assert item is not None, f"the worker would skip this item: {reason}"
    rows = [json.loads(line) for line in lines]
    return result, rows, calls


GROUP = "https://www.facebook.com/groups/123456789012345"


@test
def test_a_big_group_run_streams_batches_of_100_with_their_own_comments():
    result, rows, calls = _fake_run(GROUP, 230)
    by = {}
    for row in rows:
        by.setdefault(row["collection"], []).append(row)
    assert list(by) == ["data#1", "data#2", "data#3"], list(by)
    assert [len(v) for v in by.values()] == [100, 100, 30]
    # Comments were fetched ONE call per batch, with exactly that batch's posts.
    assert [len(c) for c in calls] == [100, 100, 30], [len(c) for c in calls]
    for name, call in zip(by, calls):
        assert [r["key"] for r in by[name]] == call
    # Every streamed post carries ITS OWN comments, not a neighbour's.
    for row in rows:
        comments = row["data"]["comments"]
        assert comments and comments[0]["comment"] == f"on {row['key']}", row["key"]
    # Batches follow the SCRAPE order - newest first - not the engagement
    # ranking the collector returns (ranked, data#N was a tail of posts with no
    # comments at all). data#1 holds the newest 100.
    dates = [r["data"]["post_date"] for r in rows]
    assert dates == sorted(dates, reverse=True), "batches are not newest-first"
    # `replies` is the batch's replies-to-comments, beside its top-level
    # `comments` - the same split as the result's comment_count/reply_count.
    # This fake collector nests none, hence 0.
    assert result["batches"] == [
        {"name": "data#1", "posts": 100, "comments": 100, "replies": 0},
        {"name": "data#2", "posts": 100, "comments": 100, "replies": 0},
        {"name": "data#3", "posts": 30, "comments": 30, "replies": 0}]


@test
def test_the_batched_output_json_matches_the_streamed_batches():
    result, rows, _ = _fake_run(GROUP, 230)
    assert result["post_count"] == 230 and result["comment_count"] == 230
    streamed = {r["key"]: r["data"] for r in rows}
    for post in result["posts"]:
        assert streamed[post["post_url"]]["comments"] == post["comments"]


@test
def test_a_small_group_and_any_page_are_not_batched_at_all():
    for target, n in ((GROUP, 100), (GROUP, 80), ("https://www.facebook.com/nasa", 230)):
        result, rows, calls = _fake_run(target, n)
        assert rows == [], f"{target} x{n} streamed {len(rows)} item(s)"
        assert "batches" not in result, target
        assert len(calls) == 1, f"{target} x{n}: {len(calls)} comment call(s)"


@test
def test_a_big_group_without_comments_still_batches_its_posts():
    result, rows, calls = _fake_run(GROUP, 150, comments=False)
    assert calls == []
    assert [r["collection"] for r in rows].count("data#1") == 100
    assert [r["collection"] for r in rows].count("data#2") == 50
    assert all("comments" not in r["data"] for r in rows)
    assert "comment_count" not in result


@test
def test_comments_are_collected_unless_turned_off_batch():
    # A form left untouched sends no collect_comments at all; that run must
    # still carry comments into its CSV/JSON download.
    actor = _load_actor()
    page = "https://www.facebook.com/nasa"
    assert actor.parse_settings({"target": page})["collect_comments"] is True
    assert actor.parse_settings({"target": page, "collect_comments": False})["collect_comments"] is False
    schema = json.loads((ACTOR_DIR / "input_schema.json").read_text())
    assert schema["properties"]["collect_comments"]["default"] is True


def _main_with_status(status: str, target: str = GROUP) -> tuple[int, bool, str]:
    """Run actor.main() with the scrape faked to end on `status`.
    -> (exit code, whether a session was exported, stderr)."""
    import contextlib
    import io
    actor = _load_actor()
    work = Path(tempfile.mkdtemp(dir=_SANDBOX))
    (work / "input.json").write_text(json.dumps({"target": target, "analyse": False}))
    session_out = work / "session_out.json"
    env = {"ACTOR_INPUT_PATH": str(work / "input.json"),
           "ACTOR_OUTPUT_PATH": str(work / "output.json"),
           actor.SESSION_OUT_ENV: str(session_out)}
    saved_env = {k: os.environ.get(k) for k in env}
    saved = (actor.install_session, actor.run)

    def fake_install(data_dir):
        # Fake values: the guard checks the cookie NAMES, and a walled jar
        # still carries both, which is exactly why names are not enough.
        jar = {"cookies": [{"name": "c_user", "value": "1"}, {"name": "xs", "value": "x"}]}
        Path(data_dir, "fb_auth.json").write_text(json.dumps(jar))
        return True

    actor.install_session = fake_install
    actor.run = lambda settings: ({"status": status, "posts": [], "post_count": 0},
                                  actor.EXIT_ACTOR_FAILED if status in actor.FAILURE_STATUSES
                                  else actor.EXIT_OK)
    os.environ.update(env)
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = actor.main()
    finally:
        actor.install_session, actor.run = saved
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    output = json.loads((work / "output.json").read_text())
    assert (output.get("session_failure") == "login_wall") == (status == "login_wall"), \
        f"session_failure wrong for status={status}: {output.get('session_failure')!r}"
    return code, session_out.is_file(), err.getvalue()


@test
def test_the_result_says_which_tier_served_the_run():
    """Which tier ran is a fact the result should carry, not a log to re-read.

    On the default path the in-page fetch() and DOM tiers are skipped by
    construction, so a result cannot otherwise distinguish "the cheap tier
    carried everything" from "the browser had to save it".
    """
    actor = _load_actor()
    posts_doc = {"status": "success", "page_information": {}, "posts":
                 [{"post_url": "u1"}],
                 "tiers": {"served_by": ["ssr_bootstrap", "http_replay"],
                           "browser_used": False}}
    comments_doc = {"posts": [{"post_url": "u1", "comments": [],
                               "comment_tier": "http_replay"}]}
    result = actor.build_result(posts_doc, comments_doc)
    assert result["tiers"] == {"feed": ["ssr_bootstrap", "http_replay"],
                               "comments": ["http_replay"],
                               "browser_used": False}, result["tiers"]

    # One post that needed the navigating scraper makes the whole run a
    # browser run -- that is the case worth noticing.
    comments_doc["posts"].append({"post_url": "u2", "comment_tier": "navigated"})
    assert actor.build_result(posts_doc, comments_doc)["tiers"]["browser_used"] is True

    # A collector that recorded nothing adds no block, so old results and the
    # no-comments path are byte-identical.
    assert "tiers" not in actor.build_result({"posts": []}, None)


@test
def test_a_walled_run_does_not_export_its_session():
    """A walled jar passes the cookie-name guard, and exporting it both stored a
    dead session and reset the account's failure counter (measured 2026-09-21:
    five walled runs, every one exported)."""
    code, exported, stderr = _main_with_status("login_wall")
    assert code == 1, code
    assert not exported, "a login-walled run exported its session"
    assert "hit the login wall" in stderr
    code, exported, _ = _main_with_status("success")
    assert code == 0 and exported, "a healthy run must still export"


@test
def test_a_display_name_target_is_refused_with_advice():
    """A name with spaces is not a URL; it used to fail once per date chunk."""
    actor = _load_actor()
    try:
        actor.parse_settings({"target": "كمبوند المقصد العاصمة الإدارية"})
    except actor.InputError as exc:
        assert "URL" in str(exc), exc
    else:
        raise AssertionError("a display name was accepted as a target")
    for ok in ("nasa", "https://www.facebook.com/groups/123456789012345",
               "facebook.com/Myfegypt"):
        actor.parse_settings({"target": ok})



SSRF_TARGETS = [
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata
    "http://127.0.0.1:8000/runs",                 # the API on loopback
    "http://172.17.0.1:8000/",                    # the API through the bridge
    "http://localhost/",
    "http://[::1]/",                              # IPv6 loopback
    "http://[::ffff:127.0.0.1]/",                 # IPv4-mapped IPv6
    "http://0x7f000001/",                         # hex IPv4
    "http://2130706433/",                         # decimal IPv4
    "http://0177.0.0.1/",                         # octal IPv4
    "https://facebook.com.evil.example/",         # suffix trick
    "https://evilfacebook.com/",                  # no dot before the domain
    "https://evil.example/?facebook.com/groups/1",  # domain in the query
    "https://evil.example/facebook.com/groups/1",   # domain in the path
    "https://user@facebook.com@evil.example/",     # userinfo
    "https://www.facebook.com@evil.example/",
    "https://facebook.com\\@evil.example/",        # backslash parser differential
    "https://www.facebook.com:8080/nasa",          # odd port
    "file:///etc/passwd",
    "javascript:alert(1)",
    "ftp://www.facebook.com/nasa",
    "//evil.example/x",                            # scheme-relative
]


@test
def test_reply_count_sits_beside_comment_count():
    """`comment_count` is TOP-LEVEL comments; `reply_count` is what nests under
    them. A live run printed "96 unique replies" in its log and
    `comment_count: 86` in its result - ten replies-to-comments, counted in one
    and not the other, which read as ten lost. Both numbers now appear, and
    they add up to what the log says."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("facebook_reply_actor", ACTOR_DIR / "actor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    posts_doc = {
        "platform": "Facebook", "scraper": "posts", "status": "success",
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
    print(f"running {len(TESTS)} Facebook Actor tests"
          + (f", filtered by {sys.argv[1:]}" if len(sys.argv) > 1 else ""))
    print(f"  target={TARGET}  session={'yes' if SESSION else 'no (anonymous)'}\n")

    # `python3 test_facebook.py batch` runs only tests whose name contains
    # "batch" - the offline ones, which need no Docker, network or session.
    wanted = [a for a in sys.argv[1:] if not a.startswith("-")]
    selected = [fn for fn in TESTS
                if not wanted or any(w in fn.__name__ for w in wanted)]
    for fn in selected:
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

    QUEUE.purge()
    print()
    # Count what RAN - a filtered run claiming "all 16 passed" after running 4
    # is exactly the kind of summary that hides a skipped test.
    print(f"{failures} of {len(selected)} tests FAILED" if failures
          else f"all {len(selected)} tests passed"
          + (f" ({len(TESTS) - len(selected)} not selected)" if wanted else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
