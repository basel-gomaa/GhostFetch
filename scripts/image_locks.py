#!/usr/bin/env python3
"""Lock, and audit, the Python packages INSIDE each image.

    python3 scripts/image_locks.py lock [image ...]           # requirements.in -> requirements.txt
    python3 scripts/image_locks.py audit [--build] [image ...]  # what a built image really holds
    python3 scripts/image_locks.py list

An image is every actor-platform/actors/<name>/ with a Dockerfile. No names
means all of them.

WHY THIS EXISTS. The two lock files the audits check (requirements.lock.txt and
actor-platform/requirements.lock.txt) describe the developers' virtualenvs, not
the images. The images installed loose ranges (`anthropic>=0.40`), the login
browser's pins were written into its Dockerfile, and the Playwright base image
brought packages of its own -- so on 30 October the login browser shipped
starlette 0.41.3 (7 advisories) and three Actor images filelock/virtualenv
(24), while every audit passed.

LOCK. Each image has a requirements.in (what it depends on) and a
requirements.txt (pip-compile's lock of it: every package, pinned, with its
hashes), and its Dockerfile installs the lock with --require-hashes. The lock
is resolved INSIDE the image's own base image, because the bases differ:
python:3.13-slim is Python 3.13 and the Playwright image is Ubuntu's 3.12, and
an environment marker resolved on the wrong one picks the wrong packages. The
output is pip-compile's standard format, which Dependabot recognises as an
.in/.txt pair and keeps up to date.

AUDIT. Freezes the packages a BUILT image holds (with the image's own Python,
reading installed metadata, so no pip is needed), then checks two things:
  1. they are exactly the lock plus pip -- anything else came from somewhere
     no lock or audit describes (a base image, a stray `pip install`);
  2. pip-audit finds no advisory in them, pip included.
--build builds each image first, labelled the way the worker labels it
(shared/image_freshness.py), so the result is also a fresh image the worker
will use. Actor contexts need their collectors vendored first
(actors/<name>/sync_collector.sh <repo root>).

OS packages (apt: Chrome, websockify, supervisor) are out of scope: pip-audit
reads Python metadata only, and the interpreter checked is the image's own
/usr/local one, which never sees Debian's.

Needs Docker. `audit` needs pip-audit importable by the Python running this
script (CI: `pip install pip-audit`).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLATFORM = ROOT / "actor-platform"

# The pip-tools that resolves every lock. Pinned so two people re-locking the
# same requirements.in get the same file.
PIP_TOOLS_VERSION = "7.6.2"

# The options every lock is compiled with. They land in the lock's own header,
# which is where Dependabot reads them back from when it updates the file.
COMPILE_OPTIONS = ["--generate-hashes", "--allow-unsafe", "--strip-extras"]

# pip-compile, with ONE function replaced. pip-tools 7.6.2 (the latest release)
# reads each file's hash from PyPI's JSON API, but builds that URL as
# https://pypi.org/<name>/json -- a path PyPI no longer serves (404) -- instead
# of the documented https://pypi.org/pypi/<name>/json. It then falls back,
# silently, to DOWNLOADING every file of every pin to hash it: every wheel for
# every platform, ~100 each for pillow and pydantic-core. MEASURED: one image
# was still hashing after 10 minutes; with the right URL a two-package lock
# takes 6 s. The output is unchanged -- same hashes, same header, which still
# reads `pip-compile ...`, so Dependabot treats the file as pip-compile's.
PIP_COMPILE = (
    "import sys\n"
    "from piptools.repositories import pypi\n"
    "pypi._get_true_base_from_index_url = lambda url: 'https://pypi.org/pypi/'\n"
    "from piptools.scripts.compile import cli\n"
    "sys.argv = ['pip-compile', *sys.argv[1:]]\n"
    "cli()\n"
)

# In the image but deliberately not in its lock: the installer itself.
NOT_LOCKED = {"pip"}

FREEZE = (
    "import importlib.metadata as m\n"
    "for d in m.distributions():\n"
    "    n = d.metadata['Name']\n"
    "    if n: print(f'{n}=={d.version}')\n"
)

PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)")
FROM = re.compile(r"^FROM\s+(?:--\S+\s+)*(\S+)", re.M)


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class Image:
    name: str
    context: Path
    tag: str

    @property
    def lock_file(self) -> Path:
        return self.context / "requirements.txt"

    @property
    def base(self) -> str:
        found = FROM.search((self.context / "Dockerfile").read_text())
        if not found:
            raise SystemExit(f"{self.context / 'Dockerfile'}: no FROM line")
        return found.group(1)


def images() -> list[Image]:
    found = [Image(p.name, p, f"actor-platform/{p.name}:latest")
             for p in sorted((PLATFORM / "actors").iterdir()) if (p / "Dockerfile").is_file()]
    return found


def select(names: list[str]) -> list[Image]:
    known = {i.name: i for i in images()}
    unknown = [n for n in names if n not in known]
    if unknown:
        raise SystemExit(f"unknown image(s): {', '.join(unknown)}; choose from {', '.join(known)}")
    return [known[n] for n in names] if names else list(known.values())


def locked(path: Path) -> dict[str, str]:
    """{normalized name: version} for every pin in a lock file."""
    pins = {}
    for line in path.read_text().splitlines():
        m = PIN.match(line.strip())
        if m:
            pins[normalize(m.group(1))] = m.group(2)
    return pins


# ------------------------------------------------------------------ lock


def lock(image: Image) -> int:
    """Re-resolve requirements.in into requirements.txt inside the image's base."""
    if not (image.context / "requirements.in").is_file():
        print(f"[lock] {image.name}: no requirements.in", file=sys.stderr)
        return 1
    print(f"[lock] {image.name}: resolving in {image.base}", flush=True)
    # Root inside a throwaway container (pip needs it in the Playwright image),
    # so the file is handed back to whoever ran this.
    script = (
        f"pip install -q pip-tools=={PIP_TOOLS_VERSION} && "
        f'python3 -c "$LOCK_COMPILER" -q {" ".join(COMPILE_OPTIONS)} '
        f"-o requirements.txt requirements.in && "
        f"chown {os.getuid()}:{os.getgid()} requirements.txt"
    )
    return subprocess.run([
        "docker", "run", "--rm", "--entrypoint", "sh",
        "-v", f"{image.context}:/w", "-w", "/w",
        "-e", "PIP_BREAK_SYSTEM_PACKAGES=1", "-e", "PIP_ROOT_USER_ACTION=ignore",
        "-e", "PIP_DISABLE_PIP_VERSION_CHECK=1", "-e", "PIP_TIMEOUT=60", "-e", "PIP_RETRIES=10",
        # NOT a PIP_* name: pip reads every PIP_* variable as one of its own
        # options, and PIP_COMPILE is its --compile flag.
        "-e", f"LOCK_COMPILER={PIP_COMPILE}",
        image.base, "-c", script,
    ]).returncode


# ----------------------------------------------------------------- audit


def build(image: Image) -> bool:
    sys.path.insert(0, str(PLATFORM))
    from shared import image_freshness  # stdlib only

    label = image_freshness.build_args(image_freshness.context_hash(image.context))
    print(f"[audit] {image.name}: building {image.tag}", flush=True)
    return subprocess.run(["docker", "build", "-q", "-t", image.tag, *label,
                           str(image.context)], stdout=subprocess.DEVNULL).returncode == 0


def freeze(image: Image) -> dict[str, str] | None:
    run = subprocess.run(["docker", "run", "--rm", "--network", "none",
                          "--entrypoint", "python3", image.tag, "-c", FREEZE],
                         capture_output=True, text=True)
    if run.returncode != 0:
        print(f"[audit] {image.name}: cannot read {image.tag}: {run.stderr.strip()}",
              file=sys.stderr)
        return None
    rows = (line.split("==", 1) for line in run.stdout.splitlines() if "==" in line)
    return {normalize(n): v for n, v in rows}


def drift(frozen: dict[str, str], pins: dict[str, str]) -> list[str]:
    """How the image differs from its lock, one line per package."""
    lines = []
    for name in sorted(set(frozen) | set(pins)):
        have, want = frozen.get(name), pins.get(name)
        if name in NOT_LOCKED or have == want:
            continue
        if want is None:
            lines.append(f"{name}=={have} is in the image but not in its lock")
        elif have is None:
            lines.append(f"{name}=={want} is locked but not in the image")
        else:
            lines.append(f"{name} is {have} in the image, {want} in the lock")
    return lines


def pip_audit(image: Image, frozen: dict[str, str]) -> bool:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write("".join(f"{n}=={v}\n" for n, v in sorted(frozen.items())))
        path = fh.name
    try:
        # Every version is exact, so nothing needs resolving: --no-deps and
        # --disable-pip audit the list as given, with no virtualenv.
        return subprocess.run([sys.executable, "-m", "pip_audit", "--no-deps", "--disable-pip",
                               "--progress-spinner", "off", "-r", path]).returncode == 0
    finally:
        os.unlink(path)


def audit(image: Image, do_build: bool) -> bool:
    if do_build and not build(image):
        print(f"[audit] {image.name}: FAIL, the build failed", file=sys.stderr)
        return False
    frozen = freeze(image)
    if frozen is None:
        return False
    ok = True
    problems = drift(frozen, locked(image.lock_file))
    if problems:
        ok = False
        print(f"[audit] {image.name}: FAIL, the image is not its lock:", file=sys.stderr)
        for line in problems:
            print(f"          {line}", file=sys.stderr)
    print(f"[audit] {image.name}: {len(frozen)} package(s), pip-audit:", flush=True)
    if not pip_audit(image, frozen):
        ok = False
        print(f"[audit] {image.name}: FAIL, pip-audit found advisories", file=sys.stderr)
    return ok


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_lock = sub.add_parser("lock", help="re-resolve requirements.in into requirements.txt")
    p_lock.add_argument("images", nargs="*")
    p_audit = sub.add_parser("audit", help="check what a built image holds")
    p_audit.add_argument("--build", action="store_true", help="build each image first")
    p_audit.add_argument("images", nargs="*")
    sub.add_parser("list", help="the images this script knows")
    args = parser.parse_args(argv)

    if args.command == "list":
        for image in images():
            print(f"{image.name:16} {image.tag:40} {image.base}")
        return 0
    if args.command == "lock":
        return max([lock(i) for i in select(args.images)], default=0)

    try:
        import pip_audit  # noqa: F401
    except ImportError:
        print(f"[audit] pip-audit is not importable by {sys.executable}; pip install pip-audit",
              file=sys.stderr)
        return 2
    failed = [i.name for i in select(args.images) if not audit(i, args.build)]
    print(f"\n[audit] {'FAILED: ' + ', '.join(failed) if failed else 'every image matches its lock and is clean'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
