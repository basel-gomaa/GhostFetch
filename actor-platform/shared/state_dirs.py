"""Where per-account durable state lives, and how it is removed.

The Worker mounts <root>/<actor>/<social account id>/ into the containers of
Actors that declare persistent_state (a browser profile, for one of them). That
directory holds a logged-in browser profile, so it must go when the account it
belongs to goes: disconnecting the account or deleting the user (security review
M-04). The API removes it; the Worker creates it. Both use this one definition
of where it is, so the two cannot drift apart.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

STATE_DIR_ENV = "ACTOR_PLATFORM_STATE_DIR"


def state_root() -> Path:
    return Path(os.environ.get(STATE_DIR_ENV) or (Path.home() / ".actor-platform" / "state"))


def safe_segment(value: str) -> str:
    """One path segment, whatever was passed: `..` cannot walk the mount up."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or "").strip())
    cleaned = cleaned.strip(".") or "unknown"
    return cleaned[:128]


def remove_account_state(account_id: str) -> int:
    """Delete every Actor's state directory for one connected account. -> count.

    Never follows a link: a planted symlink at the account's path is removed as
    a link, and rmtree does not descend through links inside it either.
    """
    root = state_root()
    if not root.is_dir():
        return 0
    segment = safe_segment(account_id)
    removed = 0
    for actor_dir in root.iterdir():
        if not actor_dir.is_dir() or actor_dir.is_symlink():
            continue
        target = actor_dir / segment
        try:
            if target.is_symlink() or target.is_file():
                target.unlink()
                removed += 1
            elif target.is_dir():
                shutil.rmtree(target)
                removed += 1
        except OSError:
            continue
    return removed
