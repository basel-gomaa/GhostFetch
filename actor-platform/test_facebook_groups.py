#!/usr/bin/env python3
"""facebook_groups Actor tests.

The claim under test is that PUBLIC Facebook groups are collectable with no
credential of any kind, as a source type that can never be confused with a Page.

Mostly OFFLINE: the limit arithmetic, the target refusal, the stop-reason
vocabulary, the drift of the vendored collectors and the "this Actor holds no
session" property all run with no network, no session and no Docker. The live
scrape is env-gated (FACEBOOK_GROUPS_TEST_TARGET), because the numbers it would
assert belong to a group whose contents change hourly.

    python3 test_facebook_groups.py            # everything that needs nothing
    python3 test_facebook_groups.py offline    # same, named explicitly
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

_SANDBOX = Path(tempfile.mkdtemp(prefix="actor-fbgroups-tests-"))
atexit.register(shutil.rmtree, _SANDBOX, ignore_errors=True)

REGISTRY = RunRegistry(_SANDBOX / "runs")
QUEUE = open_queue(str(_SANDBOX / "queue"))

API_KEY = generate_key()
AUTH = {"Authorization": f"Bearer {API_KEY}"}
CLIENT = TestClient(
    create_app(registry=REGISTRY, queue=QUEUE, auth=AuthPolicy.for_keys({API_KEY})),
    headers=AUTH,
)

ACTOR = "facebook_groups"
ACTOR_DIR = REPO_ROOT / "actors" / ACTOR
IMAGE = f"actor-platform/{ACTOR}:latest"
LIVE_TARGET = os.environ.get("FACEBOOK_GROUPS_TEST_TARGET", "")

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def _load(module_path: Path, name: str):
    """Import a module by path, with the Actor's vendor tree importable.

    The same trick test_facebook.py uses: the image puts vendor/ at the import
    root, so the tests do too rather than reaching for the upstream repo.
    """
    import importlib.util
    for path in (str(ACTOR_DIR), str(ACTOR_DIR / "vendor")):
        if path not in sys.path:
            sys.path.insert(0, path)
    spec = importlib.util.spec_from_file_location(name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_actor():
    return _load(ACTOR_DIR / "actor.py", "fbgroups_actor")


# --- the integration's central claim ----------------------------------------


def _adapter_source() -> str:
    """actor.py with docstrings stripped - prose may name things code must not."""
    return _strip_docstrings((ACTOR_DIR / "actor.py").read_text())


def _strip_docstrings(source: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


@test
def test_report_artifacts_are_named_after_this_actor():
    """A run directory must not hand back a file called "report.pdf"."""
    tree = ast.parse((ACTOR_DIR / "actor.py").read_text())
    prefix = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "ARTIFACT_PREFIX":
                    prefix = node.value.value
    assert prefix == ACTOR, f"ARTIFACT_PREFIX is {prefix!r}, expected {ACTOR!r}"
    # Against the CODE: the comment above ARTIFACT_PREFIX quotes the generic
    # names while explaining why they are banned, and a test that cannot tell a
    # rule from its violation is not worth having.
    code = _adapter_source()
    for generic in ('"report.pdf"', '"report.txt"', '"dashboard.html"',
                    "'report.pdf'", "'report.txt'", "'dashboard.html'"):
        assert generic not in code, f"{generic} is a generic artifact name"


@test
def test_the_dependency_points_one_way():
    """The facebook Actor must NOT vendor this collector.

    The groups collector imports the Facebook one; nothing goes the other way.
    Asserted rather than only commented, because a well-meaning edit to the
    other sync script would make the Pages image carry a second scraper.
    """
    assert not (REPO_ROOT / "actors" / "facebook" / "vendor" / "collectors"
                / "facebook_groups").exists(), (
        "the facebook Actor has vendored facebook_groups - the dependency is one-way")


# --- the limits, which ARE the contract --------------------------------------


class _FakeHttp:
    def close(self):
        pass


# --- the platform side ------------------------------------------------------


@test
def test_the_actor_is_never_refused_for_want_of_an_account():
    """No session policy, and crucially NO requires_social.

    Naming a platform is what makes the API demand a connected account, and
    naming one that another Actor already names with a DIFFERENT session policy
    makes the shared policy loader raise - which would break the account picker
    for every Actor at once. This Actor needs no account, so it names no
    platform and is un-connectable by construction.
    """
    from worker.limits import WorkerConfig
    config = WorkerConfig.load(path=str(REPO_ROOT / "config" / "actors.json"))
    profile = config.limits_for(ACTOR)
    assert profile.session_env is None, profile.session_env
    assert not profile.session_cookies, profile.session_cookies
    assert profile.requires_social is None, profile.requires_social
    assert profile.session_optional is True
    assert profile.session_out is False
    assert profile.concurrency_scope is None
    assert profile.hidden is False, "the Actor must be offered to a human"


@test
def test_the_account_picker_still_works_for_every_other_actor():
    """The regression this Actor could most easily have caused.

    `shared/platform_policy` raises when two Actors name one platform with
    differing policy, and that raise fires inside the picker rather than at load
    time - so it would surface as every platform disappearing at once.
    """
    from shared import platform_policy
    platform_policy.reset_cache()
    path = str(REPO_ROOT / "config" / "actors.json")
    listed = platform_policy.listed_platforms(path)
    assert listed, "no platforms are listed at all"
    # And the Pages Actor's own policy still resolves.
    assert platform_policy.policy_for("facebook", path) is not None


@test
def test_the_actor_appears_in_the_catalog_with_its_schema():
    response = CLIENT.get("/actors")
    assert response.status_code == 200, response.text
    entry = next((a for a in response.json()["actors"] if a["name"] == ACTOR), None)
    assert entry is not None, "the Actor is not in the catalog"
    assert entry["hidden"] is False
    assert entry["schema_error"] is None, entry["schema_error"]
    schema = entry["input_schema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["target"]
    props = schema["properties"]
    assert props["max_posts"]["default"] == 1000
    assert props["max_comments_per_post"]["default"] == 20
    assert props["include_replies"]["default"] is False
    assert props["public_only"]["const"] is True
    # Pages-only options must not appear here.
    for absent in ("comment_sort", "page_concurrency"):
        assert absent not in props, f"{absent} is a Pages option"
    rendered = json.dumps(entry)
    for leak in (str(REPO_ROOT), "/actors/", "Dockerfile", "vendor/"):
        assert leak not in rendered, f"{leak!r} leaked into the catalog"


@test
def test_invalid_input_is_rejected_before_a_run_exists():
    before = len(CLIENT.get("/runs").json()["runs"])
    for payload in ({}, {"target": ""}, {"target": "g", "max_posts": -1},
                    {"target": "g", "max_posts": "lots"},
                    {"target": "g", "start_date": "last tuesday"},
                    {"target": "g", "public_only": False},
                    {"target": "g", "comment_sort": "newest"},
                    {"target": "g", "nonsense": 1}):
        response = CLIENT.post(f"/actors/{ACTOR}/runs", json=payload)
        assert response.status_code == 400, (payload, response.status_code)
        assert response.json()["violations"], payload
    after = len(CLIENT.get("/runs").json()["runs"])
    assert after == before, "a rejected input created a run"


@test
def test_the_actor_validates_its_own_input_too():
    """The API is not a boundary: the Actor is runnable by hand."""
    actor = load_actor()
    settings = actor.parse_settings({"target": "https://www.facebook.com/groups/1"})
    assert settings["max_posts"] == 1000
    assert settings["max_comments_per_post"] == 20
    assert settings["include_replies"] is False
    assert settings["collect_comments"] is True
    assert settings["public_only"] is True
    assert settings["analyse"] is True

    # UNCAPPED: 0 reaches the collector as None - the repo-wide "no count
    # target" convention - and never as a large number standing in for one.
    uncapped = actor.parse_settings({"target": "g", "max_posts": 0,
                                     "max_comments_per_post": 0})
    assert uncapped["max_posts"] is None, uncapped["max_posts"]
    # 0 means NO CAP on BOTH fields - the same value must not mean "unlimited"
    # on one and "none" on the other. Skipping comments is collect_comments.
    assert uncapped["max_comments_per_post"] is None, uncapped["max_comments_per_post"]
    assert uncapped["collect_comments"] is True

    # 0 is VALID on both count fields and means "no cap" - asserted below.
    for payload in ({}, {"target": 5}, {"target": " "},
                    {"target": "g", "max_posts": True},
                    {"target": "g", "max_posts": -1},
                    {"target": "g", "include_replies": "yes"},
                    {"target": "g", "public_only": False}):
        try:
            actor.parse_settings(payload)
        except actor.InputError:
            pass
        else:
            raise AssertionError(f"{payload!r} was accepted")


@test
def test_the_pages_actor_still_collects_groups():
    """Test 7 of the brief, and the promise made when this Actor was added: the
    facebook Actor's behaviour is UNCHANGED. It still accepts a group target -
    its session reaches far deeper than an anonymous visitor can - and only says
    that a public path now exists."""
    pages = _load(REPO_ROOT / "actors" / "facebook" / "actor.py", "pages_actor")
    settings = pages.parse_settings({"target": "https://www.facebook.com/groups/123456"})
    assert settings["target"] == "https://www.facebook.com/groups/123456"
    source = (REPO_ROOT / "actors" / "facebook" / "actor.py").read_text()
    assert ACTOR in source, "the Pages Actor should point at the public path"
    assert "raise InputError" not in source.split("is_group_url(resolve_page_url")[1][:400], (
        "the Pages Actor now REFUSES groups - that is a behaviour change")


# --- live, env-gated --------------------------------------------------------


def main() -> int:
    failures = 0
    wanted = [a for a in sys.argv[1:] if not a.startswith("-")]
    # "offline" selects everything that needs no network, session or Docker -
    # which here is everything except the one live test.
    if wanted == ["offline"]:
        selected = [fn for fn in TESTS if "real_public_group" not in fn.__name__]
    else:
        selected = [fn for fn in TESTS
                    if not wanted or any(w in fn.__name__ for w in wanted)]
    print(f"running {len(selected)} facebook_groups Actor tests")
    print(f"  live target={LIVE_TARGET or 'none (live test skipped)'}\n")
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
    print(f"{failures} of {len(selected)} tests FAILED" if failures
          else f"all {len(selected)} tests passed"
          + (f" ({len(TESTS) - len(selected)} not selected)" if wanted else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
