"""How a client should SHOW an Actor or a platform: a label and an icon.

Presentation only, like `hidden`: nothing that starts, resolves or executes a
run ever reads these. They live in config/actors.json for the reason every
other per-site fact does -- no site may be named in the platform's code, the
console included, so a site's name and logo cannot be either.

An icon is ONE SVG path on a 24x24 grid plus an optional brand colour. Path
data only, never markup: it is drawn by the console as the `d` of a <path> it
builds itself, so there is nothing in it that could run. The grammar is
checked here anyway, so a bad entry fails when the config is loaded rather
than reaching a browser. No colour means "draw in the text colour", which is
how a black logo stays visible on a dark theme.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

MAX_LABEL_LENGTH = 40
MAX_PATH_LENGTH = 8000

# SVG path grammar: commands, numbers, separators. Nothing else.
_PATH = re.compile(r"[MmLlHhVvCcSsQqTtAaZz0-9.,\s+\-eE]+")
_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")
# `source` is attribution for whoever edits the file; it is not served.
_ICON_KEYS = frozenset({"d", "color", "source"})


@dataclass(frozen=True)
class Icon:
    d: str
    color: str | None = None

    def public(self) -> dict[str, Any]:
        return {"d": self.d, "color": self.color}


def parse_label(value: Any, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_LABEL_LENGTH:
        raise ValueError(f"{where}: label must be a non-empty string of at most "
                         f"{MAX_LABEL_LENGTH} characters")
    return value.strip()


def parse_icon(value: Any, where: str) -> Icon | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{where}: icon must be an object with 'd' and optional 'color'")
    bad = set(value) - _ICON_KEYS
    if bad:
        raise ValueError(f"{where}: unknown key(s) in icon: {', '.join(sorted(bad))}")
    d = value.get("d")
    if (not isinstance(d, str) or not d or len(d) > MAX_PATH_LENGTH
            or not _PATH.fullmatch(d) or d.lstrip()[:1] not in ("M", "m")):
        raise ValueError(f"{where}: icon.d must be SVG path data starting with a moveto")
    color = value.get("color")
    if color is not None and (not isinstance(color, str) or not _COLOR.fullmatch(color)):
        raise ValueError(f"{where}: icon.color must be a #rrggbb colour")
    return Icon(d=d, color=color)
