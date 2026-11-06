#!/usr/bin/env python3
"""Type-check the repository with mypy: the pipeline, then the platform.

    python3 scripts/type_check.py              # both halves
    python3 scripts/type_check.py pipeline     # or just one
    python3 scripts/type_check.py platform

TWO RUNS, ONE PER VIRTUALENV, because the two halves have separate dependency
sets (requirements.lock.txt and actor-platform/requirements.lock.txt) and mypy
can only see a package's types through the interpreter it is installed in.
Each run passes `--python-executable` for its own venv, so psycopg, FastAPI,
httpx and the rest are checked against their real annotations rather than
silently treated as Any. The platform run starts in actor-platform/, because
its modules import each other as top-level packages (`from shared import ...`).

Options shared by both runs live in [tool.mypy] in pyproject.toml. mypy itself
is NOT a project dependency: CI installs the pinned version on its own, and
locally any interpreter with that version importable will do.

Strictness is mypy's DEFAULT, on purpose -- the same gradual approach as
ruff's errors-only rule set: the tree passes it today, so CI enforces it from
the first commit, and stricter flags can be added one at a time later, each
with its own clean-up.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLATFORM = ROOT / "actor-platform"
CONFIG = ROOT / "pyproject.toml"

# The version CI installs; keep the two in step (.github/workflows/ci.yml).
MYPY_VERSION = "2.4.1"

# What each run checks, relative to the directory it runs in. Tests are plain
# scripts and are not checked yet; vendored copies are excluded in pyproject.
TARGETS = {
    "pipeline": (ROOT, ROOT / ".venv" / "bin" / "python",
                 ["collectors", "processors", "ai", "reports", "notifications",
                  "config.py", "main.py"]),
    "platform": (PLATFORM, PLATFORM / ".venv" / "bin" / "python",
                 ["api", "worker", "shared", "registry", "jobqueue",
                  "catalog", "db", "delivery"]),
}


def run(name: str) -> int:
    cwd, python, paths = TARGETS[name]
    if not python.exists():
        print(f"[type-check] {name}: no virtualenv at {python} -- create it from its "
              f"requirements.lock.txt first", file=sys.stderr)
        return 2
    command = [sys.executable, "-m", "mypy", "--config-file", str(CONFIG),
               "--python-executable", str(python), *paths]
    print(f"[type-check] {name}: mypy {' '.join(paths)}", flush=True)
    return subprocess.run(command, cwd=cwd).returncode


def main(argv: list[str]) -> int:
    try:
        import mypy.version
    except ImportError:
        print(f"[type-check] mypy is not importable by {sys.executable}; "
              f"install mypy=={MYPY_VERSION}", file=sys.stderr)
        return 2
    if mypy.version.__version__ != MYPY_VERSION:
        print(f"[type-check] warning: mypy {mypy.version.__version__} here, CI pins "
              f"{MYPY_VERSION}; results may differ", file=sys.stderr)

    names = argv or list(TARGETS)
    unknown = [n for n in names if n not in TARGETS]
    if unknown:
        print(f"[type-check] unknown target(s): {', '.join(unknown)}; "
              f"choose from {', '.join(TARGETS)}", file=sys.stderr)
        return 2
    codes = [run(n) for n in names]
    return max(codes)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
