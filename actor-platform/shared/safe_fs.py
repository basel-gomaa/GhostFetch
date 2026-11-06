"""Opening files in a directory an Actor's container can write to.

A run directory is bind-mounted read-write into the Actor's container (and so
is the per-run session scratch directory). Whatever the platform later finds
there, the CONTAINER may have put there -- and three kinds of object turn an
ordinary open() on the host into something else:

    a symlink      resolved by the HOST process, so `log.txt -> ~/.ssh/
                   authorized_keys` makes "write the logs" overwrite a host
                   file with text the Actor chose, and `output.json ->
                   ~/.docker/config.json` makes GET /output serve it
    a FIFO         open() blocks until somebody opens the other end, which
                   nobody will: a worker or API thread hangs for ever
    a device node  Docker grants CAP_MKNOD by default, so a container can make
                   a node for a HOST disk and wait for the host to read it

Path.resolve() and "is it inside the directory?" checks do not cover the last
two, and race the first. So the rule here is about the OPENED object, checked
on the file descriptor rather than the name: O_NOFOLLOW so a link is never
followed, O_NONBLOCK so a FIFO cannot block the open, and fstat() so anything
that is not a regular file is refused before a byte is read or written.

Writes never open the existing name at all. They create a fresh file with a
random name (O_EXCL, so nothing planted can be reused) and os.replace() it into
place -- which swaps the directory ENTRY and never follows a link at the
destination. A planted `log.txt` link is simply replaced by the real log.

Standard library only, like the registry that uses it.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
import tempfile
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - not POSIX
    fcntl = None  # type: ignore[assignment]  # checked as `fcntl is None`


class NotRegularFile(OSError):
    """The name exists but is a link, FIFO, device or directory."""


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def open_regular(path: Path | str, flags: int = os.O_RDONLY, mode: int = 0o644) -> int:
    """An fd for `path`, guaranteed to be a regular file. Raises NotRegularFile.

    FileNotFoundError still propagates as itself, so "absent" stays
    distinguishable from "tampered with".
    """
    try:
        fd = os.open(path, flags | _NOFOLLOW | _NONBLOCK, mode)
    except OSError as exc:
        # ELOOP is what O_NOFOLLOW reports for a link; some platforms say
        # EMLINK. Either way the name is not a file we will open.
        if exc.errno in (errno.ELOOP, errno.EMLINK) or (
                os.path.islink(path) and not isinstance(exc, FileNotFoundError)):
            raise NotRegularFile(f"{Path(path).name} is a symbolic link") from exc
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise NotRegularFile(f"{Path(path).name} is not a regular file")
        if _NONBLOCK and fcntl is not None:
            # Only there to stop a FIFO blocking the open. A regular file never
            # blocks, but leave the descriptor in ordinary blocking mode anyway.
            fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~_NONBLOCK)
    except BaseException:
        os.close(fd)
        raise
    return fd


class FileTooLarge(OSError):
    """A file larger than the reader agreed to hold in memory."""


def read_bytes(path: Path | str, max_bytes: int | None = None) -> bytes:
    """A regular file's bytes. With `max_bytes`, refuse rather than read more.

    The run directory's files are the Actor's, so their size is too: without a
    bound, one 20 GB output.json was read whole by the worker and the API.
    """
    with os.fdopen(open_regular(path), "rb") as handle:
        if max_bytes is None:
            return handle.read()
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise FileTooLarge(f"{Path(path).name} is larger than {max_bytes} bytes")
    return data


def read_text(path: Path | str, encoding: str = "utf-8", max_bytes: int | None = None) -> str:
    return read_bytes(path, max_bytes).decode(encoding)


def read_head(path: Path | str, max_bytes: int) -> tuple[bytes, bool]:
    """Up to `max_bytes` of a regular file, and whether there was more."""
    with os.fdopen(open_regular(path), "rb") as handle:
        data = handle.read(max_bytes + 1)
    return data[:max_bytes], len(data) > max_bytes


def write_text_atomic(path: Path | str, text: str, encoding: str = "utf-8") -> None:
    """Replace `path` with `text` without ever opening what is there now."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)  # mkstemp makes 0600; keep what write_text produced
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def open_lock(path: Path | str):
    """A file object to flock(), never truncating and never following a link.

    A lock file's CONTENT is irrelevant, so if the name has been replaced by
    something that is not a regular file, the entry is removed and recreated
    rather than refusing -- refusing would let a container stop its own run's
    record from ever being updated.
    """
    try:
        return os.fdopen(open_regular(path, os.O_RDWR | os.O_CREAT), "r+")
    except (NotRegularFile, IsADirectoryError):
        # Removes the link / FIFO / node / directory ITSELF, never a target:
        # unlink does not follow, and rmtree refuses to descend through links.
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)
        return os.fdopen(open_regular(path, os.O_RDWR | os.O_CREAT | os.O_EXCL), "r+")
