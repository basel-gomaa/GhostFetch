"""Is an image built from the build context as it is NOW?

An image used to be built only when it was missing, so once it existed nothing
ever rebuilt it. A re-synced collector, a Dockerfile edit or a pinned
dependency bump left the old image running: on 24 October that meant a
mismatched Playwright (every launch of three browser Actors failed) and another
Actor running pre-change code, and a security fix inside an image did not take
effect until somebody rebuilt it by hand. (No site is named here: the platform
names none outside actors/, and each Actor's suite checks that.)

So every image is built with a label holding a hash of its build context, and a
run compares that label with the context's hash as it is now. A mismatch
rebuilds. Rebuilding when nothing that is COPY'd changed is mostly Docker's
layer cache, so it is cheap; hashing a context is ~20 ms.

What the hash covers, per entry in sorted order: the relative path, whether it
is a file or a symlink, the executable bit (COPY keeps the mode, e.g. an
entrypoint script) and the bytes or the link target. It SKIPS __pycache__ and
.pyc: test runs rewrite them constantly, so hashing them would rebuild on every
run, and Python ignores a stale .pyc next to its .py anyway.

Used by the worker (Actor images), the login manager and run-local.sh (the
login-browser image), so all three agree on what "stale" means:

    python -m shared.image_freshness <build context>   # prints the hash
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

LABEL = "org.actor-platform.context-hash"
SCHEME = "v1"

SKIP_DIRS = frozenset({"__pycache__"})
SKIP_SUFFIXES = (".pyc",)

MISSING = "missing"
UNLABELLED = "unlabelled"
CHANGED = "context changed"


def _entries(root: Path):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune in place so os.walk never descends into a skipped directory.
        # A symlink to a directory shows up in dirnames; it is an entry, not a
        # tree to walk, so it is moved to the file list.
        here = Path(dirpath)
        keep = []
        for name in dirnames:
            if name in SKIP_DIRS:
                continue
            if (here / name).is_symlink():
                filenames.append(name)
            else:
                keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            if name.endswith(SKIP_SUFFIXES):
                continue
            yield here / name


def context_hash(root: Path | str) -> str:
    """A stable hash of everything in a build context that can reach an image."""
    root = Path(root)
    digest = hashlib.sha256()
    for path in sorted(_entries(root), key=lambda p: p.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            kind, body = b"L", os.readlink(path).encode()
        elif stat.S_ISREG(info.st_mode):
            kind, body = b"F", path.read_bytes()
        else:
            continue  # a FIFO or socket cannot be COPY'd into an image
        executable = b"x" if info.st_mode & stat.S_IXUSR else b"-"
        for part in (rel.encode(), kind, executable, body):
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)
    return f"{SCHEME}:{digest.hexdigest()}"


def image_label(tag: str) -> tuple[bool, str | None]:
    """(exists, label). The label is None when the image has none."""
    probe = subprocess.run(
        ["docker", "image", "inspect", "--format", f'{{{{index .Config.Labels "{LABEL}"}}}}', tag],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        return False, None
    value = probe.stdout.strip()
    # A missing key prints "<no value>"; an image with no labels at all, "".
    return True, (value if value and value != "<no value>" else None)


def staleness(tag: str, context: Path | str) -> tuple[str, str | None, bool]:
    """(the context's hash now, why the image is stale or None, does it exist)."""
    want = context_hash(context)
    exists, have = image_label(tag)
    if not exists:
        return want, MISSING, False
    if have is None:
        return want, UNLABELLED, True
    if have != want:
        return want, CHANGED, True
    return want, None, True


def build_args(hash_value: str) -> list[str]:
    return ["--label", f"{LABEL}={hash_value}"]


def short(hash_value: str) -> str:
    """For log lines: the scheme and enough of the digest to tell two apart."""
    return hash_value[: len(SCHEME) + 1 + 12]


def stale_note(tag: str, reason: str, detail: str) -> str:
    """The line a run's own log carries when it ran on an image that is out of date."""
    return (f"[platform] WARNING: this run used {tag}, which was built from older "
            f"sources ({reason}); rebuilding it failed: {detail}. "
            f"Fixes made since that build are not in this run.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m shared.image_freshness <build context>", file=sys.stderr)
        raise SystemExit(2)
    print(context_hash(sys.argv[1]))
