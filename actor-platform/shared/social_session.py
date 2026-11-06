"""What counts as a usable social-media session, and how to read one.

A captured browser session is a Playwright `storage_state` document: a cookie
jar plus origins. The platform has to answer one question about it -- IS THIS
STILL A LOGIN? -- at three moments: when a login container says the user has
finished, when the API decides whether to store what the container returned,
and when a worker decides whether a refreshed jar is worth writing back.

THE PLATFORM DOES NOT KNOW WHICH COOKIES ANY SITE USES, and must not learn.
`config/actors.json` already carries per-Actor policy for exactly this reason,
and the cookie names live there as `session_cookies`. So this module takes the
required names as an argument and has no site-specific knowledge at all -- the
same rule worker/limits.py states: no Actor is named in platform code.

THE ONE SUBTLETY, and it is why a jar is checked against SEVERAL names rather
than one: a site's identity cookie often OUTLIVES its authentication cookie. A
jar carrying the first and not the second looks like a session and is not one,
so capturing on that rule stores a login that fails on its first real use --
with the failure surfacing hours later as a scrape that hit a login wall. Every
name in the list must be present AND non-empty.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Sequence


def is_logged_in(
    cookies: Iterable[Mapping[str, Any]] | None, required: Sequence[str]
) -> bool:
    """True when this jar carries every required cookie with a non-empty value.

    An empty `required` means nothing can be proved, so the answer is False:
    a missing configuration must not read as "everything is fine".
    """
    if not required:
        return False
    jar = list(cookies or [])
    return all(
        any(c.get("name") == name and c.get("value") for c in jar) for name in required
    )


def account_id(
    cookies: Iterable[Mapping[str, Any]] | None, id_cookie: str | None
) -> str | None:
    """The site's own account identifier, for display. None when absent.

    Not a credential -- it is typically the number in a public profile URL --
    so it is safe to store beside the ciphertext and to show a user so they can
    tell which of their accounts is connected.
    """
    if not id_cookie:
        return None
    for cookie in cookies or []:
        if cookie.get("name") == id_cookie and cookie.get("value"):
            return str(cookie["value"])
    return None


def cookies_of(state: Any) -> list[dict[str, Any]]:
    """The cookie list out of a storage_state document, defensively.

    Accepts the parsed document or raw JSON, because the same value arrives as
    text from a container's HTTP response and as a dict from Playwright.
    Returns [] for anything unusable rather than raising: every caller's next
    step is is_logged_in(), which answers False for [], and False is also the
    right answer for a malformed document.
    """
    if isinstance(state, (str, bytes)):
        try:
            state = json.loads(state)
        except (ValueError, TypeError):
            return []
    if not isinstance(state, dict):
        return []
    cookies = state.get("cookies")
    return [c for c in cookies if isinstance(c, dict)] if isinstance(cookies, list) else []


def storage_state_is_usable(state: Any, required: Sequence[str]) -> bool:
    """The whole check a caller normally wants: is this document a live session?"""
    return is_logged_in(cookies_of(state), required)


def describe(state: Any, required: Sequence[str], id_cookie: str | None = None) -> str:
    """A log line about a session that contains NO PART of it.

    Every path that handles a session wants to say something about it, and this
    is the only safe thing to say. A session cookie must never reach a log:
    log.txt is served over HTTP by GET /runs/{id}/logs.
    """
    jar = cookies_of(state)
    uid = account_id(jar, id_cookie)
    return (
        f"{len(jar)} cookie(s), "
        f"{'logged in' if is_logged_in(jar, required) else 'NOT logged in'}"
        f"{f', account {uid}' if uid else ''}"
    )
