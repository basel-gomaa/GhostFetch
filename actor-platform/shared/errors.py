"""The platform's error vocabulary: one category set, one shape, one redactor.

WHY THIS EXISTS. The platform could already say WHETHER a run failed and, via
RunStatus, whose fault it was in the broadest sense -- FAILED means the Actor
ran and failed, PLATFORM_ERROR means we could not run it. What it could not say
is WHAT KIND of failure, and that is the fact a caller actually needs, because
it is the one that decides whether trying again could possibly help. "actor
exited with code 1" is true of a bad URL, a dead session, a private page, a
rate limit and a parser bug alike, and those five want five different responses.

This module is deliberately in shared/ and deliberately dependency-free, for
the same reason shared/models.py is: an Actor image must be able to import the
vocabulary without importing the platform.

IT ADDS NO STATUS. RunStatus is unchanged, and in particular this module does
NOT reintroduce TIMED_OUT -- shared/models.py argues that case and the argument
still holds. TIMEOUT here is a CATEGORY, describing why a run ended, and it is
only ever produced by an actual configured limit being exceeded. A long scrape
that is merely slow produces no timeout of any kind, because nothing bounds it.

THE CATEGORY IS NOT THE MESSAGE. A category is for a machine deciding what to
do next; the message is for a person deciding what to fix. Both are required.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ErrorCategory(str, Enum):
    """What kind of failure this was.

    Ordered from "the caller can fix it" to "we do not know". The str mixin is
    what lets a category be written straight into JSON and compared to a plain
    string, exactly as RunStatus does.
    """

    # The caller supplied something unusable: a missing target, a malformed
    # URL, a count out of range, a date window that is not one.
    INPUT_ERROR = "INPUT_ERROR"

    # A credential problem: expired session, invalid token, permission denied
    # at the identity layer. The message must say what needs reconnecting.
    AUTH_ERROR = "AUTH_ERROR"

    # The target cannot be reached AS ASKED: private, deleted, region-locked,
    # behind a login wall. Distinct from AUTH_ERROR because supplying a
    # credential is not necessarily the fix, and distinct from INPUT_ERROR
    # because the input was well-formed.
    ACCESS_ERROR = "ACCESS_ERROR"

    # The upstream said, explicitly, to slow down: 429, a quota response, a
    # documented rate-limit body. Never inferred from slowness.
    RATE_LIMITED = "RATE_LIMITED"

    # Temporary and plausibly self-healing: a reset connection, a DNS blip, an
    # upstream 5xx. The only category besides RATE_LIMITED that is retryable
    # by default.
    TRANSIENT_ERROR = "TRANSIENT_ERROR"

    # OUR fault: the worker, Docker, storage, the queue, a bad actor config.
    # Pairs with RunStatus.PLATFORM_ERROR but is not the same thing -- the
    # status says the platform could not run the Actor, this says why.
    PLATFORM_ERROR = "PLATFORM_ERROR"

    # The Actor ran and broke on its own logic: a parser that met a shape it
    # did not expect, a normalisation failure, a dependency blowing up. A
    # traceback belongs in the run log; a summary belongs in the message.
    ACTOR_ERROR = "ACTOR_ERROR"

    # An actual CONFIGURED limit was exceeded. See the module docstring: this
    # is not a general "took too long".
    TIMEOUT = "TIMEOUT"

    # Nothing above fits. Never a silent success, never a guess dressed up as
    # a diagnosis -- an unclassifiable failure is still a failure, and saying
    # so honestly is the point of having this member at all.
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


# Whether trying the SAME request again could plausibly succeed, per category.
#
# Only two are retryable, and both because the upstream itself said so: a rate
# limit names a time to wait, and a transient fault is by definition the kind
# that passes. Everything else repeats its own failure -- a bad URL is bad on
# every attempt, a private page stays private, and a parser bug is
# deterministic. PLATFORM_ERROR is false here on purpose even though some
# platform faults are transient: the specific transient ones (a Docker daemon
# blip, say) set retryable explicitly, and defaulting the whole category to
# true would have the platform retry its own misconfiguration three times.
_RETRYABLE_BY_DEFAULT = {
    ErrorCategory.INPUT_ERROR: False,
    ErrorCategory.AUTH_ERROR: False,
    ErrorCategory.ACCESS_ERROR: False,
    ErrorCategory.RATE_LIMITED: True,
    ErrorCategory.TRANSIENT_ERROR: True,
    ErrorCategory.PLATFORM_ERROR: False,
    ErrorCategory.ACTOR_ERROR: False,
    ErrorCategory.TIMEOUT: False,
    ErrorCategory.UNKNOWN_ERROR: False,
}


def retryable_by_default(category: ErrorCategory) -> bool:
    """Whether `category` may be retried when nothing says otherwise."""
    return _RETRYABLE_BY_DEFAULT[category]


# --- Secret redaction -------------------------------------------------------
#
# An exception's text is not a controlled string. A requests error carries the
# URL it failed on, query string included; a subprocess error carries the argv
# it ran; an Actor's own traceback can carry whatever it was holding. Any of
# those can contain a session cookie, and all of them end up in a log line or a
# run record. Redacting at the boundary -- once, here, on the way in -- is the
# only place that covers every path, because there is no path to a stored error
# that does not go through this module.
#
# The patterns are deliberately about SHAPE rather than about any site: a rule
# naming c_user or li_at would protect exactly the sites someone remembered.

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # A CREDENTIAL HEADER takes the whole rest of the line.
    #
    # Not the next token -- the whole line. These headers are entirely made of
    # secret: "Cookie: c_user=1; xs=2; sb=3" is three secrets separated by
    # semicolons, and a rule that stopped at the first separator would redact
    # c_user and publish xs. "Authorization: Bearer <jwt>" has a SPACE between
    # the scheme and the token, so stopping at whitespace redacts the word
    # "Bearer" and publishes the token. Both were real, and both are why this
    # rule is deliberately greedy: nothing after one of these names on one line
    # is ever worth keeping.
    (
        re.compile(
            r"(?im)^(.*?\b(?:set-)?cookie|.*?\b(?:proxy-)?authorization)\b(\s*[:=]\s*).*$"
        ),
        r"\1\2[REDACTED]",
    ),
    # A NAMED credential elsewhere: key=value / key: value. The value runs to
    # the next separator, quote or whitespace, because unlike a header this
    # appears mid-sentence and the rest of the line is usually the diagnosis.
    (
        re.compile(
            r"(?i)\b("
            r"token|access[_-]?token|refresh[_-]?token|id[_-]?token|"
            r"api[_-]?key|apikey|secret|password|passwd|pwd|"
            r"session|sessionid|session[_-]?id|sessionkey|"
            r"credential|csrf|auth|bearer"
            r")\b(\s*[:=]\s*)([^\s,;&\"\'}\])]+)"
        ),
        r"\1\2[REDACTED]",
    ),
    # A bare Bearer/Basic value, which carries no key= to match on.
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [REDACTED]"),
    # A JWT, wherever it appears. Recognised by SHAPE -- three base64url
    # segments joined by dots, starting with the "eyJ" that is base64 for '{"'.
    # The high-entropy rule below misses these because each segment on its own
    # can be under the length threshold and the dots break the word boundary.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?"), "[REDACTED]"),
    # A query string on any URL. Session material rides in query strings far
    # more often than it rides in a named field, and a query string is never
    # load-bearing in an error message -- the path already says what failed.
    (re.compile(r"(\?)[^\s\"\'<>]{1,4096}"), r"\1[REDACTED]"),
)

# Long opaque blobs that look like credentials wherever they appear. Applied
# after the rules above so a value already replaced is not re-scanned.
_HIGH_ENTROPY = re.compile(r"\b[A-Za-z0-9_-]{40,}\b")

MAX_MESSAGE_CHARS = 2000


def redact(text: Any) -> str:
    """Return `text` with anything credential-shaped replaced.

    Never raises and never returns None: this runs on the failure path, where a
    second exception would replace a real diagnosis with a meaningless one.
    Over-redaction is the deliberate bias -- a message missing a query string is
    a small loss, a message carrying a live session cookie is an incident.
    """
    if text is None:
        return ""
    try:
        out = str(text)
    except Exception:  # noqa: BLE001 - a __str__ that throws must not win
        return "<unprintable>"

    for pattern, replacement in _REDACTIONS:
        try:
            out = pattern.sub(replacement, out)
        except Exception:  # noqa: BLE001
            continue
    try:
        out = _HIGH_ENTROPY.sub("[REDACTED]", out)
    except Exception:  # noqa: BLE001
        pass

    if len(out) > MAX_MESSAGE_CHARS:
        out = out[: MAX_MESSAGE_CHARS - 3] + "..."
    return out


# --- Retry-After ------------------------------------------------------------
#
# RFC 9110 gives this header TWO forms, and a parser that knows only the first
# silently ignores the second:
#
#     Retry-After: 120
#     Retry-After: Wed, 21 Oct 2026 07:28:00 GMT
#
# Both are in live use -- the delta-seconds form is far more common, but CDNs
# and rate limiters in front of these sites emit the date form too. Reading
# only the integer form means falling back to a guessed backoff while the
# upstream was telling us exactly how long to wait, which is the whole reason
# the field exists.
#
# A value is PARSED HERE rather than in each collector so that one grammar
# serves everything, and so that whatever an Actor puts in its output -- an
# already-computed number or the raw header string it was handed -- arrives at
# the same answer.

# A day. Past this the number is not guidance any more, it is a bug or a
# misconfigured proxy, and honouring it would park a run for a week. Clamping
# rather than rejecting keeps the signal ("wait a long time") while bounding it.
MAX_RETRY_AFTER_S = 86400.0


def parse_retry_after(value: Any, *, now: float | None = None) -> float | None:
    """Seconds to wait, from either RFC 9110 form. None if it says nothing.

    Accepts a number as well as a header string, because an Actor may have
    computed the delay itself rather than passing the header through.

    Never raises, and never returns a negative: a date already in the past
    means "you may retry now", which is 0.0, not a negative wait. `now` is
    injectable so the date form is testable without freezing the clock.
    """
    if value is None or isinstance(value, bool):
        return None

    # Already a number: an Actor that did its own arithmetic.
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds != seconds or seconds in (float("inf"), float("-inf")):
            return None  # NaN / infinity are not a wait
        return max(0.0, min(seconds, MAX_RETRY_AFTER_S))

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None

    # delta-seconds. Checked first because it is the common form and because
    # the date parser below accepts some bare numbers in ways we do not want.
    try:
        return max(0.0, min(float(int(text)), MAX_RETRY_AFTER_S))
    except ValueError:
        pass

    # HTTP-date. parsedate_to_datetime handles IMF-fixdate and the two obsolete
    # formats RFC 9110 still requires a recipient to accept.
    try:
        from email.utils import parsedate_to_datetime  # noqa: PLC0415

        when = parsedate_to_datetime(text)
    except Exception:  # noqa: BLE001 - a malformed date is simply no guidance
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        # RFC 9110 requires GMT; a date with no zone is read as UTC rather than
        # as local time, which would be wrong by the host's offset.
        from datetime import timezone as _tz  # noqa: PLC0415

        when = when.replace(tzinfo=_tz.utc)

    reference = time.time() if now is None else now
    return max(0.0, min(when.timestamp() - reference, MAX_RETRY_AFTER_S))


# --- The structured error ---------------------------------------------------


@dataclass(frozen=True)
class RunError:
    """Why one run failed, in the shape everything downstream reads.

    Frozen because an error is a finding, not a workspace: once the executor has
    decided what happened, nothing further down should be quietly amending it.

    `code` is the machine's field and `message` the person's. `detail` carries
    the Actor's or Docker's OWN narrower code (LOGIN_WALL, CONTAINER_OOM) so
    that mapping into a category never destroys the more specific thing that was
    known -- section 11 of the brief, and the reason a generic category set does
    not flatten every Actor into the same vocabulary.
    """

    code: ErrorCategory
    message: str
    retryable: bool | None = None
    retry_after_seconds: float | None = None
    stage: str | None = None
    actor: str | None = None
    attempt: int | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        # Redact once, here, so no construction path can store a raw message.
        object.__setattr__(self, "message", redact(self.message))
        if self.retryable is None:
            object.__setattr__(self, "retryable", retryable_by_default(self.code))

    def to_dict(self) -> dict[str, Any]:
        """The JSON form. Omits what is unknown rather than sending nulls."""
        data: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "retryable": bool(self.retryable),
        }
        for key in ("retry_after_seconds", "stage", "actor", "attempt", "detail"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data

    @classmethod
    def from_dict(cls, data: Any) -> RunError | None:
        """Rebuild from a stored dict, or None if it is not one.

        Tolerant on purpose: this reads records written by other versions, and
        an unreadable error field must not make a whole run record unloadable.
        """
        if not isinstance(data, dict) or not data.get("code"):
            return None
        try:
            code = ErrorCategory(data["code"])
        except ValueError:
            code = ErrorCategory.UNKNOWN_ERROR
        retry_after = data.get("retry_after_seconds")
        attempt = data.get("attempt")
        return cls(
            code=code,
            message=str(data.get("message") or ""),
            retryable=bool(data["retryable"]) if "retryable" in data else None,
            retry_after_seconds=float(retry_after) if isinstance(retry_after, (int, float)) else None,
            stage=data.get("stage") or None,
            actor=data.get("actor") or None,
            attempt=int(attempt) if isinstance(attempt, int) else None,
            detail=data.get("detail") or None,
        )


# --- Container failures -----------------------------------------------------
#
# Docker's own vocabulary, kept as `detail` rather than promoted to a category:
# these all mean "the platform could not run the Actor", which IS the category,
# and flattening them into it would throw away the only part an operator can
# act on. "exit code 137" and "the image is missing" both fail a run and want
# completely different responses.
#
# WHY NOT JUST exit_code != 0. Docker distinguishes ITS OWN failures from the
# Actor's by reserving a small range: 125 means the docker CLI itself failed,
# 126 that the entrypoint was not executable, 127 that it was not found. Those
# three are never the Actor's fault -- the Actor never ran. Above that, 128+N
# is "killed by signal N", which is how an OOM kill (137 = 128 + SIGKILL) looks
# from outside. Reading exit codes as a flat "non-zero is the Actor's problem"
# reports our own broken image as a scraping failure.

IMAGE_NOT_FOUND = "IMAGE_NOT_FOUND"
CONTAINER_START_FAILED = "CONTAINER_START_FAILED"
CONTAINER_EXITED = "CONTAINER_EXITED"
CONTAINER_OOM = "CONTAINER_OOM"
CONTAINER_KILLED = "CONTAINER_KILLED"
CONTAINER_RUNTIME_ERROR = "CONTAINER_RUNTIME_ERROR"

# Docker's reserved range: the container never ran the Actor's code.
DOCKER_CLI_FAILED = 125
ENTRYPOINT_NOT_EXECUTABLE = 126
ENTRYPOINT_NOT_FOUND = 127

# 128 + signal number, as reported for a container killed by a signal.
SIGNAL_EXIT_BASE = 128
SIGKILL = 9
SIGTERM = 15
SIGSEGV = 11

# Substrings Docker puts in stderr when it could not produce a container at
# all. Matched case-insensitively against `docker run` stderr; each is a
# documented daemon message rather than a guess.
_IMAGE_MISSING_MARKERS = (
    "no such image",
    "unable to find image",
    "image not found",
    "pull access denied",
    "manifest unknown",
)
_DAEMON_MARKERS = (
    "cannot connect to the docker daemon",
    "is the docker daemon running",
    "connection refused",
)


def classify_container_exit(
    exit_code: int | None,
    stderr: str = "",
    *,
    oom_killed: bool | None = None,
) -> tuple[ErrorCategory, str, str]:
    """Read a finished container's exit code as (category, detail, message).

    `oom_killed` is Docker's OWN verdict, from `docker inspect .State.OOMKilled`
    when the caller looked. It is believed over the exit code, because 137 is
    ambiguous by construction: an OOM kill and a deliberate `docker kill` are
    the same number. Passing None means "not checked", and then 137 is reported
    as CONTAINER_KILLED -- the honest reading, since the platform does kill
    containers itself during cancellation and recovery.

    Returns a PLATFORM_ERROR category for every failure that happened to the
    container rather than inside the Actor. A plain non-zero exit is the one
    case that is the Actor's own: it ran, and it said it failed.
    """
    haystack = (stderr or "").lower()

    if exit_code is None:
        return (
            ErrorCategory.PLATFORM_ERROR,
            CONTAINER_START_FAILED,
            "the container produced no exit status",
        )

    if any(marker in haystack for marker in _IMAGE_MISSING_MARKERS):
        return (
            ErrorCategory.PLATFORM_ERROR,
            IMAGE_NOT_FOUND,
            "the actor image is not available to this worker",
        )

    if any(marker in haystack for marker in _DAEMON_MARKERS):
        # The ONE container failure that is retryable: a daemon that was not
        # reachable this second may be reachable next second, and nothing about
        # the run itself is wrong.
        return (
            ErrorCategory.TRANSIENT_ERROR,
            CONTAINER_START_FAILED,
            "the docker daemon could not be reached",
        )

    if exit_code == DOCKER_CLI_FAILED:
        return (
            ErrorCategory.PLATFORM_ERROR,
            CONTAINER_START_FAILED,
            "docker could not start the container",
        )
    if exit_code in (ENTRYPOINT_NOT_EXECUTABLE, ENTRYPOINT_NOT_FOUND):
        return (
            ErrorCategory.PLATFORM_ERROR,
            CONTAINER_RUNTIME_ERROR,
            "the actor image has no runnable entrypoint",
        )

    if exit_code == SIGNAL_EXIT_BASE + SIGKILL:
        if oom_killed:
            return (
                ErrorCategory.PLATFORM_ERROR,
                CONTAINER_OOM,
                "the container was killed after running out of memory",
            )
        return (
            ErrorCategory.PLATFORM_ERROR,
            CONTAINER_KILLED,
            "the container was killed (SIGKILL)",
        )
    if exit_code == SIGNAL_EXIT_BASE + SIGTERM:
        return (
            ErrorCategory.PLATFORM_ERROR,
            CONTAINER_KILLED,
            "the container was terminated (SIGTERM)",
        )
    if exit_code == SIGNAL_EXIT_BASE + SIGSEGV:
        return (
            ErrorCategory.ACTOR_ERROR,
            CONTAINER_RUNTIME_ERROR,
            "the actor process crashed (SIGSEGV)",
        )

    # The ordinary case, and the only one that is the Actor's own doing: it ran
    # its logic and exited non-zero. The Actor's output.json is what says more,
    # and the caller overlays it -- see actor_error_from_output.
    return (
        ErrorCategory.ACTOR_ERROR,
        CONTAINER_EXITED,
        f"the actor exited with code {exit_code}",
    )


# --- Actor-declared errors --------------------------------------------------
#
# An Actor knows things the platform cannot see: that a page was private, that
# a jar hit a login wall, that the upstream answered 429. It says so in its
# output document, and this is where that becomes a category without the
# platform having to learn any site's vocabulary.
#
# The mapping is on the GENERIC words an Actor uses, not on Actor names. A new
# Actor that says "login_wall" is classified correctly the day it is written,
# and no file here has to change -- which is the same rule config/actors.json
# follows about never naming an Actor inside worker/, api/ or registry/.

_ACTOR_STATUS_CATEGORIES: dict[str, tuple[ErrorCategory, str]] = {
    # Access: the target exists but will not be served as asked.
    "login_wall": (ErrorCategory.ACCESS_ERROR, "LOGIN_WALL"),
    "authwall": (ErrorCategory.ACCESS_ERROR, "AUTHWALL"),
    "private": (ErrorCategory.ACCESS_ERROR, "PRIVATE_RESOURCE"),
    "private_group": (ErrorCategory.ACCESS_ERROR, "PRIVATE_GROUP"),
    "private_profile": (ErrorCategory.ACCESS_ERROR, "PRIVATE_RESOURCE"),
    "hashtag_unavailable": (ErrorCategory.ACCESS_ERROR, "RESOURCE_UNAVAILABLE"),
    "page_unavailable": (ErrorCategory.ACCESS_ERROR, "RESOURCE_UNAVAILABLE"),
    "profile_unavailable": (ErrorCategory.ACCESS_ERROR, "PROFILE_UNAVAILABLE"),
    "unavailable": (ErrorCategory.ACCESS_ERROR, "RESOURCE_UNAVAILABLE"),
    "not_found": (ErrorCategory.ACCESS_ERROR, "RESOURCE_UNAVAILABLE"),
    "deleted": (ErrorCategory.ACCESS_ERROR, "RESOURCE_UNAVAILABLE"),
    "access_denied": (ErrorCategory.ACCESS_ERROR, "ACCESS_DENIED"),
    "forbidden": (ErrorCategory.ACCESS_ERROR, "ACCESS_DENIED"),
    "region_blocked": (ErrorCategory.ACCESS_ERROR, "REGION_RESTRICTED"),
    # Auth: a credential is missing, dead, or refused.
    "auth_error": (ErrorCategory.AUTH_ERROR, "AUTH_FAILED"),
    "session_expired": (ErrorCategory.AUTH_ERROR, "SESSION_EXPIRED"),
    "expired": (ErrorCategory.AUTH_ERROR, "SESSION_EXPIRED"),
    "no_session": (ErrorCategory.AUTH_ERROR, "SESSION_MISSING"),
    "invalid_credentials": (ErrorCategory.AUTH_ERROR, "INVALID_CREDENTIALS"),
    # The upstream said to slow down.
    "rate_limited": (ErrorCategory.RATE_LIMITED, "RATE_LIMITED"),
    "quota_exceeded": (ErrorCategory.RATE_LIMITED, "QUOTA_EXCEEDED"),
    "throttled": (ErrorCategory.RATE_LIMITED, "RATE_LIMITED"),
    # A challenge is an access control, not a fault to retry around. It is
    # reported, never solved -- see the brief's section 12 and the platform's
    # own rule about never defeating a restriction.
    "captcha": (ErrorCategory.ACCESS_ERROR, "CHALLENGE_REQUIRED"),
    "challenge": (ErrorCategory.ACCESS_ERROR, "CHALLENGE_REQUIRED"),
    "blocked": (ErrorCategory.ACCESS_ERROR, "BLOCKED"),
    # The site's own robots.txt refuses this path. An ACCESS_ERROR because the
    # resource is what cannot be fetched, and never retryable: the refusal is a
    # policy, so a second attempt is the same request made twice.
    "robots_disallowed": (ErrorCategory.ACCESS_ERROR, "ROBOTS_DISALLOWED"),
    # Transient.
    "timeout": (ErrorCategory.TRANSIENT_ERROR, "UPSTREAM_TIMEOUT"),
    "network_error": (ErrorCategory.TRANSIENT_ERROR, "NETWORK_ERROR"),
    "upstream_error": (ErrorCategory.TRANSIENT_ERROR, "UPSTREAM_ERROR"),
    "fetch_failed": (ErrorCategory.TRANSIENT_ERROR, "FETCH_FAILED"),
    "transport_error": (ErrorCategory.TRANSIENT_ERROR, "TRANSPORT_ERROR"),
    # The caller's fault.
    "invalid_input": (ErrorCategory.INPUT_ERROR, "INVALID_INPUT"),
    "invalid_target": (ErrorCategory.INPUT_ERROR, "INVALID_TARGET"),
    "invalid_url": (ErrorCategory.INPUT_ERROR, "INVALID_URL"),
    "unsupported_target": (ErrorCategory.INPUT_ERROR, "UNSUPPORTED_TARGET"),
}


# DELIBERATELY ABSENT: "no_posts_found", "no_comments", "success", "ok".
#
# The first two are the ones worth saying out loud. A scrape that ran correctly
# and found nothing is a SUCCESSFUL run with an empty result, not a failure --
# a hashtag with no posts and a private profile are different answers and a
# caller has to be able to tell them apart. Mapping them here would relabel
# every legitimately empty result as an error, which is the specific way an
# error taxonomy makes a system less honest rather than more.


def category_for_actor_status(status: str | None) -> tuple[ErrorCategory, str] | None:
    """Map an Actor's own status/error word to (category, detail), or None."""
    if not status or not isinstance(status, str):
        return None
    return _ACTOR_STATUS_CATEGORIES.get(status.strip().lower())
