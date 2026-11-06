#!/usr/bin/env python3
"""How Actors and platforms are SHOWN: labels and logos from config.

    python3 test_presentation.py

Offline: reads config only. What matters is not that a logo appears but that
the field cannot become a way in -- an icon is path data drawn by the console,
so anything that is not path data is refused when the config is LOADED rather
than reaching a browser -- and that a presentation fact can never change a
security decision.
"""

from __future__ import annotations

import json
import sys
import tempfile
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.platform_policy import PolicyError, listed_platforms, reset_cache  # noqa: E402
from shared.presentation import parse_icon, parse_label  # noqa: E402
from worker.limits import LimitError, load_actor_config  # noqa: E402

CONFIG = REPO_ROOT / "config" / "actors.json"
SQUARE = "M0 0h24v24H0z"
TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def refused(fn, *args) -> bool:
    try:
        fn(*args)
    except ValueError:
        return True
    return False


@test
def test_an_icon_is_path_data_and_nothing_else():
    assert parse_icon({"d": SQUARE}, "t").public() == {"d": SQUARE, "color": None}
    assert parse_icon({"d": SQUARE, "color": "#0A66C2", "source": "x"}, "t").public() == {
        "d": SQUARE, "color": "#0A66C2"}, "`source` is attribution and is not served"
    for bad, why in (({"d": "<script>alert(1)</script>"}, "markup"),
                     ({"d": 'M0 0" onload="alert(1)'}, "an attribute break-out"),
                     ({"d": "url(javascript:alert(1))"}, "a url"),
                     ({"d": "L1 1"}, "no initial moveto"),
                     ({"d": ""}, "empty"),
                     ({"d": "M" + "1 " * 5000}, "too long"),
                     ({"d": SQUARE, "color": "red;background:url(x)"}, "a colour that is CSS"),
                     ({"d": SQUARE, "color": "#12345"}, "a short colour"),
                     ({"d": SQUARE, "href": "x"}, "an unknown key"),
                     ("M0 0", "not an object")):
        assert refused(parse_icon, bad, "t"), f"accepted {why}: {bad!r}"


@test
def test_a_label_is_short_plain_text():
    assert parse_label("  Some Site ", "t") == "Some Site"
    assert parse_label(None, "t") is None
    for bad in ("", "   ", "x" * 41, 7):
        assert refused(parse_label, bad, "t"), f"accepted {bad!r}"


@test
def test_a_bad_icon_fails_when_the_config_loads():
    """Not when a browser draws it."""
    doc = json.loads(CONFIG.read_text(encoding="utf-8"))
    doc["actors"]["hello_world"]["icon"] = {"d": "<svg/>"}
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "actors.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        try:
            load_actor_config(path)
        except LimitError as exc:
            assert "icon" in str(exc), exc
        else:
            raise AssertionError("a config with markup in an icon loaded")


@test
def test_every_offered_actor_has_a_label_and_a_logo():
    """The console shows these instead of raw names, so a visible Actor without
    them would be the one plain-text row in a list of logos."""
    _, _, actors = load_actor_config(CONFIG)
    for name, profile in actors.items():
        if profile.hidden:
            continue
        assert profile.label, f"{name} has no label"
        assert profile.icon, f"{name} has no icon"


@test
def test_every_listed_platform_has_a_label_and_a_logo():
    reset_cache()
    for platform in listed_platforms(CONFIG):
        assert platform.label and platform.icon, f"{platform.name} lacks a label or icon"


@test
def test_presentation_never_changes_the_security_policy_comparison():
    """Two Actors naming one platform must agree on its SESSION policy, and a
    disagreement is refused. A different label or logo is not a disagreement."""
    doc = json.loads(CONFIG.read_text(encoding="utf-8"))
    actors = doc["actors"]
    # Any Actor that names a platform will do; none is named here.
    original = next(n for n, e in actors.items() if e.get("requires_social"))
    platform = actors[original]["requires_social"]
    twin = dict(actors[original])
    twin["label"] = "Another label"
    twin["icon"] = {"d": SQUARE}
    actors[original + "_twin"] = twin
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "actors.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        reset_cache()
        try:
            names = [p.name for p in listed_platforms(path)]
        except PolicyError as exc:
            raise AssertionError(f"a label difference was read as a policy conflict: {exc}")
        finally:
            reset_cache()
    assert names.count(platform) == 1


def main() -> int:
    print(f"running {len(TESTS)} presentation tests\n")
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"  ok    {fn.__name__.removeprefix('test_')}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {fn.__name__.removeprefix('test_')}")
            traceback.print_exc()
    print(f"\n{'all passed' if not failed else f'{failed} FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
