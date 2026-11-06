#!/usr/bin/env python3
"""One runner for every test script in the repository.

    python3 scripts/run_tests.py                    # offline group (no services)
    python3 scripts/run_tests.py --group db         # needs a THROWAWAY Postgres
    python3 scripts/run_tests.py --group docker     # needs a Docker daemon
    python3 scripts/run_tests.py --group all
    python3 scripts/run_tests.py --list
    python3 scripts/run_tests.py test_demo_pipeline actor-platform/test_auth

The tests are plain scripts (`python3 test_x.py`), each with its own runner
that exits non-zero on any failure -- that is how they were written and how
the READMEs say to run them, so this does not convert them to pytest. It finds
them, runs each in a subprocess from the right directory with the right
environment, and reports one table and one exit code: the merge gate CI needs.

Groups, by what a suite needs to RUN rather than by what it tests:
  offline  no network, no database, no Docker
  db       a throwaway Postgres in ACTOR_PLATFORM_DATABASE_URL (migrated by
           this runner) and ACTOR_PLATFORM_SECRET_KEY
  docker   a Docker daemon; builds the small fixture images itself

Every suite not named below is treated as `docker`, the most demanding group,
so a new test is never silently skipped by CI's offline job.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLATFORM = ROOT / "actor-platform"

# Suites are named by their path relative to the repo root: root suites are
# `test_x`, platform suites `actor-platform/test_x`.
P = "actor-platform/"
GROUPS: dict[str, set[str]] = {
    "offline": {
        # pipeline (repo root)
        "test_demo_pipeline", "test_safe_write",
        # platform
        "test_auth", "test_console", "test_observability", "test_registry",
        "test_run_dir_links", "test_runs_cli", "test_ui", "test_security_headers",
        "test_actor_images", "test_errors", "test_network_isolation",
        "test_seccomp_profile",
        "test_image_freshness", "test_site_names", "test_presentation",
    },
    "db": {
        "test_accounts", "test_admin_users", "test_authz", "test_registry_pg",
        "test_secrets", "test_items", "test_input_validation_db", "test_throttle",
        "test_quotas", "test_run_deletion", "test_invites", "test_profile",
    },
}


def discover() -> dict[str, Path]:
    """name -> path. Root suites are `test_x`; platform suites `actor-platform/test_x`."""
    found: dict[str, Path] = {}
    for base, prefix in ((ROOT, ""), (PLATFORM, P)):
        for path in sorted(base.glob("test_*.py")):
            found[prefix + path.stem] = path
    return found


def group_of(name: str) -> str:
    platform = name.startswith(P)
    stem = name[len(P):] if platform else name
    for group, members in GROUPS.items():
        # A root suite can only be offline; a platform suite of the same name
        # (none today) would fall through to its own group.
        if stem in members and (platform or group == "offline"):
            return group
    return "docker"


def interpreter_for(path: Path) -> str:
    venv = path.parent / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def env_for(group: str) -> dict[str, str]:
    env = dict(os.environ)
    if group != "db":
        # api/main.py builds its app at import time: a non-DB suite must not
        # see a database URL, or it silently becomes a DB suite.
        env.pop("ACTOR_PLATFORM_DATABASE_URL", None)
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def migrate(env: dict[str, str]) -> None:
    subprocess.run([interpreter_for(PLATFORM / "x.py"), "db/migrate.py"],
                   cwd=PLATFORM, env=env, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("names", nargs="*", help="specific suites, e.g. test_auth")
    parser.add_argument("--group", default="offline",
                        choices=["offline", "db", "docker", "all"])
    parser.add_argument("--exclude", nargs="*", default=[], help="suites to leave out")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--timeout", type=int, default=1800, help="seconds per suite")
    args = parser.parse_args()

    suites = discover()
    if args.list:
        for name, path in suites.items():
            print(f"{group_of(name):8} {path.relative_to(ROOT)}")
        return 0

    if args.names:
        missing = [n for n in args.names if n not in suites]
        if missing:
            print(f"unknown suite(s): {', '.join(missing)}", file=sys.stderr)
            return 2
        chosen = args.names
    elif args.group == "all":
        chosen = list(suites)
    else:
        chosen = [n for n in suites if group_of(n) == args.group]

    chosen = [n for n in chosen if n not in set(args.exclude)]

    if any(group_of(n) == "db" for n in chosen):
        if not os.environ.get("ACTOR_PLATFORM_DATABASE_URL"):
            print("db suites need ACTOR_PLATFORM_DATABASE_URL pointing at a THROWAWAY "
                  "database, and ACTOR_PLATFORM_SECRET_KEY", file=sys.stderr)
            return 2
        migrate(env_for("db"))

    results: list[tuple[str, int, float]] = []
    for name in chosen:
        path = suites[name]
        started = time.monotonic()
        print(f"\n=== {name} ({group_of(name)}) ===", flush=True)
        try:
            code = subprocess.run([interpreter_for(path), path.name], cwd=path.parent,
                                  env=env_for(group_of(name)), timeout=args.timeout).returncode
        except subprocess.TimeoutExpired:
            code = 124
        results.append((name, code, time.monotonic() - started))

    print("\n" + "-" * 60)
    for name, code, seconds in results:
        print(f"{'ok  ' if code == 0 else 'FAIL'}  {name:32} {seconds:7.1f}s  exit {code}")
    failed = [r for r in results if r[1] != 0]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
