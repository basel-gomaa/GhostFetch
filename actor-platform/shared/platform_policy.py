"""Per-PLATFORM policy: what a session looks like, and where people sign in.

The platform must not know which cookies any site uses or what its login page
is, so all of it is configuration. It lives in config/actors.json beside the
Actor that needs it -- one file rather than two that can disagree -- and is
read from here by everything that needs it: the API deciding whether a captured
jar is a real session, the Worker deciding whether a refreshed one is worth
storing, and the login manager deciding where to point the browser.

    "<actor-name>": {
      "requires_social": "<platform>",
      "session_cookies": ["<identity-cookie>", "<auth-cookie>"],
      "session_id_cookie": "<identity-cookie>",
      "login_url": "https://<the site>/login"
    }

The example is a placeholder rather than a real site, and so is every other one
in this package: a test greps every module outside actors/ for site names, so
that no platform module -- comments included -- can come to depend on which
sites happen to be installed.

POLICY IS KEYED BY PLATFORM, DECLARED ON THE ACTOR. Those are not the same
thing, and the difference will matter: several Actors can read one platform's
session (a page scraper and a group scraper, say) and they must all resolve to
the SAME rule, or one of them would accept a jar the other rejects. So two
Actors naming one platform with DIFFERENT rules is refused rather than resolved
arbitrarily -- an arbitrary answer here is a security decision made by
dictionary ordering.

This module is deliberately dependency-free and names no site.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from shared.presentation import Icon, parse_icon, parse_label

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "actors.json"


class PolicyError(RuntimeError):
    """The configuration cannot answer for this platform."""


@dataclass(frozen=True)
class PlatformPolicy:
    """Everything the platform knows about one connectable site."""

    platform: str
    # Every one required, and non-empty. See shared/social_session for why more
    # than one name is usually needed.
    session_cookies: tuple[str, ...]
    # Which of them names the account, for display. Never a credential.
    session_id_cookie: str | None
    # Where the login browser starts. None means this platform cannot be
    # connected interactively.
    login_url: str | None
    # Shown instead of a Connect action when this platform is listed but not
    # connectable -- some sites serve their public data to anyone, so there is
    # no session to capture and an enabled button would be a lie.
    note: str | None = None
    # How a client SHOWS it (shared/presentation.py). Excluded from equality:
    # two Actors that agree on the security policy must not be refused because
    # one of them spells the label differently.
    label: str | None = field(default=None, compare=False)
    icon: Icon | None = field(default=None, compare=False)

    @property
    def connectable(self) -> bool:
        return bool(self.login_url and self.session_cookies)


@dataclass(frozen=True)
class ListedPlatform:
    """One row of the platform picker: a name, whether it can be connected,
    and -- when it cannot -- why it does not need to be."""

    name: str
    connectable: bool
    note: str | None = None
    label: str | None = None
    icon: Icon | None = None


def _load(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise PolicyError(f"{path}: {exc}") from exc
    actors = document.get("actors")
    return actors if isinstance(actors, dict) else {}


@lru_cache(maxsize=8)
def _policies(path_str: str) -> dict[str, PlatformPolicy]:
    found: dict[str, PlatformPolicy] = {}
    for actor, entry in _load(Path(path_str)).items():
        if not isinstance(entry, dict):
            continue
        platform = entry.get("requires_social")
        if not platform:
            continue
        try:
            # The PLATFORM's label where the Actor's own names one product
            # of it (the site's name, for an Actor labelled "<site> Pages").
            label = parse_label(entry.get("platform_label") or entry.get("label"),
                                f"actors.{actor}")
            icon = parse_icon(entry.get("icon"), f"actors.{actor}")
        except ValueError as exc:
            raise PolicyError(str(exc)) from exc
        policy = PlatformPolicy(
            platform=platform,
            session_cookies=tuple(entry.get("session_cookies") or ()),
            session_id_cookie=entry.get("session_id_cookie"),
            login_url=entry.get("login_url"),
            note=entry.get("platform_note"),
            label=label,
            icon=icon,
        )
        existing = found.get(platform)
        if existing is not None and existing != policy:
            # Refused, not resolved. Picking one would make a security rule
            # depend on which Actor happened to be read first.
            raise PolicyError(
                f"actors disagree about the '{platform}' session policy "
                f"(actor '{actor}' differs from an earlier one); make them identical"
            )
        found[platform] = policy
    return found


def policy_for(platform: str, path: Path | str | None = None) -> PlatformPolicy:
    """The policy for one platform. Raises if it is not configured."""
    policies = _policies(str(path or DEFAULT_CONFIG))
    if platform not in policies:
        raise PolicyError(f"no session policy is configured for platform {platform!r}")
    return policies[platform]


def connectable_platforms(path: Path | str | None = None) -> list[str]:
    """Platforms a user can connect interactively, in a stable order."""
    policies = _policies(str(path or DEFAULT_CONFIG))
    return sorted(name for name, p in policies.items() if p.connectable)


def listed_platforms(path: Path | str | None = None) -> list[ListedPlatform]:
    """Every configured platform, connectable or not, in a stable order.

    Wider than connectable_platforms() on purpose. A platform whose site serves
    its public data anonymously has no session to capture, but leaving it out of
    the picker entirely reads as "not supported" -- so it is listed, marked
    un-connectable, and carries its own explanation from configuration. Keeping
    the explanation in config is what stops a site name reaching this module.
    """
    policies = _policies(str(path or DEFAULT_CONFIG))
    return [
        ListedPlatform(name=name, connectable=p.connectable, note=p.note,
                       label=p.label, icon=p.icon)
        for name, p in sorted(policies.items())
    ]


def reset_cache() -> None:
    """Forget the parsed configuration. For tests that rewrite the file."""
    _policies.cache_clear()
