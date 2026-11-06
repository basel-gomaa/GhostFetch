#!/usr/bin/env python3
"""Session jars are written 0600 and data files 0644, and nothing goes back (offline).

    python3 test_safe_write.py

Security review N-08 (2026-10-31): 36 places chmodded what they wrote 0666 so
another OS user could overwrite it, and five more let Playwright write a
session jar at whatever the umask allowed. All of them now go through
collectors/safe_write.py. Two kinds of check:

  * the helper itself: exact modes under a permissive umask, the rename that
    replaces an old 0666 file or a symlink without touching it, and no
    half-written file or stray temp file when a write fails;
  * the SOURCE, because a regression here is silent -- a file written 0666
    works perfectly. So the collectors, scripts and processors are grepped for
    any way back to it.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from collectors import safe_write  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


class Scratch:
    """A temp directory, under a umask that would grant everything."""

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        self._umask = os.umask(0)
        return Path(self._tmp.name)

    def __exit__(self, *exc):
        os.umask(self._umask)
        self._tmp.cleanup()


# ------------------------------------------------------------------ helper


@test
def test_a_session_jar_is_0600_and_data_is_0644_whatever_the_umask():
    with Scratch() as d:
        safe_write.write_session_json(d / "x" / "x_auth.json", {"cookies": []}, indent=2)
        safe_write.write_json(d / "x" / "x_posts_raw.json", {"posts": ["é"]}, ensure_ascii=False)
        assert mode(d / "x" / "x_auth.json") == 0o600, oct(mode(d / "x" / "x_auth.json"))
        assert mode(d / "x" / "x_posts_raw.json") == 0o644
        assert json.loads((d / "x" / "x_auth.json").read_text()) == {"cookies": []}
        assert (d / "x" / "x_posts_raw.json").read_text(encoding="utf-8") == '{"posts": ["é"]}'


@test
def test_an_old_world_writable_file_is_replaced_not_rewritten():
    with Scratch() as d:
        jar = d / "tiktok_auth.json"
        jar.write_text("{}")
        jar.chmod(0o666)
        inode = jar.stat().st_ino
        safe_write.write_session_json(jar, {"cookies": [1]})
        assert mode(jar) == 0o600
        assert jar.stat().st_ino != inode, "the old file was written through, not replaced"
        assert json.loads(jar.read_text()) == {"cookies": [1]}


@test
def test_a_symlink_at_the_target_is_replaced_never_followed():
    with Scratch() as d:
        victim = d / "victim.txt"
        victim.write_text("untouched")
        link = d / "fb_auth.json"
        link.symlink_to(victim)
        safe_write.write_session_json(link, {"cookies": []})
        assert victim.read_text() == "untouched"
        assert not link.is_symlink() and mode(link) == 0o600


@test
def test_a_failed_write_keeps_the_old_file_and_leaves_no_temp_file():
    with Scratch() as d:
        jar = d / "ig_auth.json"
        safe_write.write_session_json(jar, {"cookies": ["old"]})
        real_replace = os.replace

        def boom(*_a, **_k):
            raise OSError("disk full")

        os.replace = boom
        try:
            safe_write.write_session_json(jar, {"cookies": ["new"]})
            raise AssertionError("the failure was swallowed")
        except OSError:
            pass
        finally:
            os.replace = real_replace
        assert json.loads(jar.read_text()) == {"cookies": ["old"]}
        assert sorted(p.name for p in d.iterdir()) == ["ig_auth.json"], list(d.iterdir())


# ------------------------------------------------------------------ source


def sources() -> list[Path]:
    files = [*ROOT.glob("collectors/**/*.py"), *ROOT.glob("processors/*.py"), ROOT / "main.py"]
    return [p for p in files if p.name != "safe_write.py" and "__pycache__" not in p.parts]


def code_lines(path: Path):
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        code = line.split("#", 1)[0]
        if code.strip():
            yield n, code


@test
def test_nothing_is_made_world_readable_or_writable_again():
    """No chmod that grants anything to others, no Playwright path= writer
    (it follows the umask), and no session file opened for writing by hand."""
    world = re.compile(r"chmod\([^)]*0o[0-7]*[1-7]\s*\)")      # any 'other' bit
    pw_path = re.compile(r"storage_state\(\s*path\s*=")
    by_hand = re.compile(r"open\(\s*(STORAGE_STATE_FILE|[^,]*_auth\.json[^,]*)\s*,\s*['\"][wa]")
    found = []
    for path in sources():
        for n, code in code_lines(path):
            for rx, why in ((world, "grants 'other' access"), (pw_path, "Playwright path= writer"),
                            (by_hand, "session file opened for writing by hand")):
                if rx.search(code):
                    found.append(f"{path.relative_to(ROOT)}:{n}: {why}: {code.strip()}")
    assert not found, "\n" + "\n".join(found)


@test
def test_every_session_writer_and_data_saver_uses_the_helper():
    """Each script or module that writes a jar or an output document names
    the helper, so a new copy of the old open/chmod dance stands out."""
    missing = []
    for path in sources():
        text = path.read_text(encoding="utf-8")
        if re.search(r"^def (save|_save|_write_state|save_output|save_json|save_storage_state)\(",
                     text, re.M) and "safe_write" not in text and "write_session_json" not in text \
                and "write_json" not in text:
            missing.append(str(path.relative_to(ROOT)))
    assert not missing, f"writers that bypass collectors/safe_write.py: {missing}"


def main() -> int:
    failed = 0
    print(f"running {len(TESTS)} safe_write tests\n")
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
