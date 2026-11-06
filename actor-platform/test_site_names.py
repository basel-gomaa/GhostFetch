#!/usr/bin/env python3
"""The platform names no site (offline: reads files only, no Docker).

    python3 test_site_names.py

The platform is site-agnostic: everything a site needs lives under `actors/`
or in `config/actors.json`, and nothing in `api/`, `worker/`, `shared/`, ...
may know one exists. Two checks hold it to that, for every site:

  * no platform module's CODE names the site or its session cookies
    (docstrings stripped, so a module may explain itself);
  * no `.py` file outside `actors/` mentions it at all, docstrings included.

These used to live in each Actor's own suite, which needs Docker, so they ran
only nightly. #95 then named three sites in a docstring and the nightly was red
for a week with nobody told, while every pull request stayed green. Here they
run in the offline group, i.e. on every pull request.
"""

from __future__ import annotations

import ast
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# word lists per site, copied from the Actor suites that used to hold them:
#   code -- what a platform module's code (docstrings stripped) must not contain
#   text -- what no .py file outside actors/ may contain, docstrings included
SITES: dict[str, dict[str, tuple[str, ...]]] = {
    "facebook": {
        "code": ("facebook", "fb_session", "fb_dtsg", "c_user"),
        "text": ("facebook",),
    },
    "instagram": {
        "code": ("instagram", "ig_session", "sessionid", "ds_user_id"),
        "text": ("instagram",),
    },
    "linkedin": {
        "code": ("linkedin", "linkedin_session", "li_at", "jsessionid"),
        "text": ("linkedin",),
    },
    # NOT the bare word "threads": it is the ordinary programming term, and the
    # Worker legitimately says "Daemon threads on purpose" and "Out of threads".
    # A real leak would carry one of these instead.
    "threads": {
        "code": ("threads.com", "threads_session", "barcelona"),
        "text": ("threads.com", "threads.net", "threads_session", "threads_auth", "barcelona"),
    },
    "tiktok": {
        "code": ("tiktok", "tiktok_session", "x-bogus", "mstoken"),
        "text": ("tiktok",),
    },
    # NOT the bare platform name: "x" is a single letter and appears inside
    # ordinary identifiers everywhere. The distinctive strings are what a leak
    # would actually look like.
    "x": {
        "code": ("x_session", "auth_token", "twid", "x.com"),
        "text": ("x_session", "auth_token", "x.com"),
    },
}

# Actor directories that need no row of their own.
COVERED_BY = {"facebook_groups": "facebook"}  # the "facebook" row matches it
FIXTURES = {"hello_world", "item_processor"}  # no site behind them

# The platform modules whose code is checked: the union of the lists the six
# Actor suites carried.
PLATFORM_MODULES = (
    "api/main.py", "api/schemas.py", "api/auth.py", "api/social.py",
    "catalog/actors.py",
    "worker/executor.py", "worker/consumer.py", "worker/reconciler.py", "worker/limits.py",
    "registry/run_registry.py",
    "shared/models.py", "shared/platform_policy.py",
    "cli/runs.py",
    "jobqueue/base.py", "jobqueue/file_queue.py", "jobqueue/redis_queue.py",
)

# Pruned from the text check: where sites are allowed to be named, and
# third-party code.
SKIP_DIRS = {"actors", ".venv", "node_modules", "__pycache__", ".git"}

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def code_without_docstrings(path: Path) -> str:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree).lower()


def platform_files() -> list[Path]:
    """Every .py file outside actors/ that is not a test."""
    found = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(".py") and not name.startswith("test_"):
                found.append(Path(dirpath) / name)
    return sorted(found)


@test
def test_no_platform_module_names_a_site_in_its_code():
    missing = [m for m in PLATFORM_MODULES if not (ROOT / m).is_file()]
    assert not missing, f"listed platform modules no longer exist: {missing} -- update PLATFORM_MODULES"
    offences = []
    for module in PLATFORM_MODULES:
        code = code_without_docstrings(ROOT / module)
        for site, words in SITES.items():
            offences += [f"{module}: {word!r} ({site})" for word in words["code"] if word in code]
    assert not offences, "the platform learned about a site:\n  " + "\n  ".join(offences)


@test
def test_no_file_outside_actors_mentions_a_site():
    files = platform_files()
    assert len(files) > 20, f"only {len(files)} platform files found -- is the walk broken?"
    offences = []
    for path in files:
        text = path.read_text(errors="replace").lower()
        for site, words in SITES.items():
            offences += [f"{path.relative_to(ROOT)}: {word!r} ({site})"
                         for word in words["text"] if word in text]
    assert not offences, "site names outside actors/:\n  " + "\n  ".join(offences)


@test
def test_every_actor_is_covered():
    actors = {p.name for p in (ROOT / "actors").iterdir() if (p / "Dockerfile").is_file()}
    uncovered = actors - set(SITES) - set(COVERED_BY) - FIXTURES
    assert not uncovered, (
        f"Actors with no site-name guard: {sorted(uncovered)} -- add a row to SITES, "
        "or to COVERED_BY / FIXTURES if no new site is behind it")
    stale = (set(SITES) | set(COVERED_BY) | FIXTURES) - actors
    assert not stale, f"names here with no Actor directory: {sorted(stale)}"
    assert set(COVERED_BY.values()) <= set(SITES)


def main() -> int:
    failed = 0
    print(f"running {len(TESTS)} site-name tests\n")
    for fn in TESTS:
        name = fn.__name__.removeprefix("test_")
        try:
            fn()
            print(f"  ok    {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{failed} FAILED" if failed else "\nall passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
