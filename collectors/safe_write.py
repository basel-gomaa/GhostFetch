"""Write a JSON file atomically, with a mode chosen here rather than by the umask.

    write_session_json(path, state)   # a session jar: 0600, readable by its owner only
    write_json(path, data, **kw)      # scraped or processed data: 0644

The ONE way a session jar or a data file is written, by the collectors, the
login and build_*_auth scripts and the processors alike.

WHY IT EXISTS (security review N-08, 2026-10-31). Each of those used to open
the file, fall back to deleting and recreating it on PermissionError, and then
`os.chmod(path, 0o666)` it -- 36 copies of that -- so the next OS user of a
shared checkout could overwrite it. That made every live session jar readable
AND writable by every account on the machine, and every scraped file
tamperable. Five more sites let Playwright write the jar
(`context.storage_state(path=...)`), which follows the umask: 0664 here. This
checkout is safe only because its home directory is 0700; installed in /srv or
/opt, any local account or service could have read six platforms' sessions.

How it writes: a temporary file in the same directory, created by mkstemp
(O_EXCL, so it is never something already there), set to the mode with fchmod
(so the umask cannot loosen it), written, fsynced, and renamed over the target.
The rename replaces whatever was at the name -- an old 0666 file, a file
another user owns, or a symlink -- without opening it, which also covers what
the old "PermissionError: delete and recreate" fallback was for.

Sharing a checkout between OS users is no longer supported by file modes. If
it is ever needed again, use a dedicated group and 0660, not world bits.

Standard library only, and Playwright-free, like collectors/daterange.py, so
every collector, script and processor can import it. It is vendored into every
Actor image beside daterange.py.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

SESSION_MODE = 0o600
DATA_MODE = 0o644


def write_text(path: str | os.PathLike[str], text: str, *, mode: int) -> None:
    """Replace `path` with `text`, the new file having exactly `mode`."""
    path = os.fspath(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{os.path.basename(path)}.", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path: str | os.PathLike[str], data: Any, *, mode: int = DATA_MODE,
               **dump_kwargs: Any) -> None:
    """`json.dump(data, fh, **dump_kwargs)` into `path`, atomically, at `mode`."""
    write_text(path, json.dumps(data, **dump_kwargs), mode=mode)


def write_session_json(path: str | os.PathLike[str], state: Any, **dump_kwargs: Any) -> None:
    """A session jar (cookies + storage): readable and writable by its owner only."""
    write_json(path, state, mode=SESSION_MODE, **dump_kwargs)
