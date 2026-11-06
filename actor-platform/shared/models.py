"""The Actor <-> platform contract.

This module is the ONLY thing the platform and an Actor have to agree on. It
deliberately contains no scraping, no transport and no third-party imports, so
that every Actor image stays independent of the platform's own dependencies.

The contract has four parts:

    input   a JSON object the platform writes to ACTOR_INPUT_PATH before start
    output  a JSON value the Actor writes to ACTOR_OUTPUT_PATH before exit
    logs    anything the Actor prints to stdout / stderr, merged in write order
    status  the container's exit code (0 = succeeded, anything else = failed)

An Actor MAY also append items to DATASET_FILE while it runs (see below); that
part is optional and changes nothing for an Actor that does not.

Logs are captured over a single pty, so the two streams interleave exactly as
the Actor wrote them and an Actor sees isatty() == True. Two separate pipes
would race: measured, 7 of 10 runs spliced lines together.

Both paths live inside ACTOR_STORAGE_DIR, which the worker bind-mounts from a
per-run directory on the host. An Actor therefore needs no network, no
credentials and no knowledge of the platform to be run by it.

It also holds the vocabulary the platform uses to talk about runs -- statuses,
exit codes and the Run record itself -- because the executor and the registry
must agree on those exactly.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any

# --- Contract: names and locations -----------------------------------------
# Actors read these environment variables; the worker sets them. The defaults
# are baked into each Actor image so a container is also runnable by hand.

ENV_INPUT_PATH = "ACTOR_INPUT_PATH"
ENV_OUTPUT_PATH = "ACTOR_OUTPUT_PATH"
ENV_RUN_ID = "ACTOR_RUN_ID"

# Names of environment variables the Worker forwards from its own environment
# into every Actor container. This is how an Actor receives a credential the
# platform must never see: the value is read by the docker CLI from the Worker's
# environment and never passes through the input document, so it cannot reach
# the Registry, the queue, the API or a log.
ENV_FORWARD_LIST = "ACTOR_FORWARD_ENV"

# Where an Actor writes back a REFRESHED session, when it was given one.
#
# A site rotates its session cookies server-side while a scrape runs, so the jar
# the Actor ends with is newer than the one it started with. Throwing that away
# means every stored session decays until it dies -- which looks like the site
# blocking you, not like a bug.
ENV_SESSION_OUT = "ACTOR_SESSION_OUT"

# Where an Actor keeps state that must OUTLIVE its container.
#
# A run gets a fresh container and a fresh run directory, which is right for
# everything a run produces and wrong for the one thing a run accumulates: an
# Actor that drives a real browser builds up device-bound state -- the profile
# a site recognises across visits -- and rebuilding it every run means arriving
# as a brand new device every time, which is exactly what a site's anti-bot is
# looking for. This mount is that state's home, and it is per CONNECTED
# ACCOUNT, because state built while acting as one account must never be handed
# to another.
#
# It is NOT under ACTOR_STORAGE_DIR, for the reason ACTOR_SECRETS_DIR gives
# below: this directory holds authentication material, and the run tree is what
# the API serves.
ENV_STATE_DIR = "ACTOR_STATE_DIR"

# An operator must not be able to redirect an Actor's storage by "forwarding" a
# contract variable over the top of the one the executor sets.
RESERVED_ENV_NAMES = frozenset(
    {ENV_INPUT_PATH, ENV_OUTPUT_PATH, ENV_RUN_ID, ENV_FORWARD_LIST, ENV_SESSION_OUT,
     ENV_STATE_DIR}
)

ACTOR_STORAGE_DIR = "/actor/storage"

# A SECOND mount, and it is deliberately not under ACTOR_STORAGE_DIR.
#
# storage/runs/<run_id>/ is what GET /runs/{id}/input|output|logs serves from.
# A live session written in there would sit in the platform's public artifact
# tree -- one route addition away from being served, readable by anything that
# can list the directory, and surviving the container. So the credential mount
# is a worker-private directory outside the run tree entirely.
ACTOR_SECRETS_DIR = "/actor/secrets"
SESSION_OUT_FILE = "session.json"

# The durable per-account state mount (see ENV_STATE_DIR). A THIRD mount, and
# it differs from the other two in exactly one way that matters: it is not
# deleted when the run ends. Its host side is a worker-private directory keyed
# by Actor and connected account, so two accounts never share one and no
# account's state is reachable from the artifact tree.
ACTOR_STATE_DIR = "/actor/state"
DEFAULT_INPUT_PATH = f"{ACTOR_STORAGE_DIR}/input.json"
DEFAULT_OUTPUT_PATH = f"{ACTOR_STORAGE_DIR}/output.json"

# The port an Actor with a viewable display serves it on, inside the container.
# The SAME number the login browser uses, because it is the same stack (a VNC
# server on a virtual display, wrapped for the browser by websockify) and one
# number is easier to reason about than two. The HOST side is always ephemeral
# and always loopback -- see _container_args.
ACTOR_DISPLAY_PORT = 6080

# Marker files an interactive Actor and the platform exchange inside the run
# directory, which is already mounted read-write at ACTOR_STORAGE_DIR.
#
# A FILE, not a port or a socket: the mount already exists, it is already
# per-run, and it needs no second listening service inside a container that is
# driving a real browser. The Actor writes the first when it needs a person and
# polls for the second; the Worker does the mirror image. Neither ever blocks
# on the other being there.
ATTENTION_FILE = "attention.json"
RESUME_FILE = "resume.json"

# An OPTIONAL fifth part of the contract: items an Actor produces AS IT GOES.
#
# output.json is one document written at exit, so a forty-minute scrape shows
# nothing until minute forty and a run that dies at minute thirty-nine leaves
# nothing at all. An Actor that wants its results reachable while it runs
# appends them here instead, one JSON object per line:
#
#     {"collection": "<name>", "key": "<optional id>", "data": <any JSON>}
#
# The platform reads the file while the container runs and stores each line as
# an item of the run (see registry/dataset.py). `collection` and `key` are
# OPAQUE: the platform groups and returns by them and never interprets either,
# which is what keeps it ignorant of what any Actor scrapes.
#
# A FIXED NAME in the storage mount, not an environment variable, for the same
# reason as the two marker files above: the mount already exists, and a new
# variable would change the argv of every container whether or not its Actor
# ever writes a line. output.json is unaffected and remains the result.
DATASET_FILE = "dataset.jsonl"

# File names inside the host-side run directory.
INPUT_FILE = "input.json"
OUTPUT_FILE = "output.json"
LOG_FILE = "log.txt"
RUN_FILE = "run.json"


# --- Contract: what counts as an Actor name --------------------------------
# An Actor name is used twice, and both uses constrain it:
#
#   * as a Docker image path component -- Docker's reference grammar allows
#     only lowercase alphanumerics separated by ".", "_", "__" or runs of "-",
#     and rejects anything else with an opaque "invalid reference format"
#     raised deep inside `docker build`
#   * as a path segment under actors/ -- so "..", "/" and friends must never
#     reach a filesystem join
#
# Validating once, up front, turns both into a clear rejection before any work
# starts. The rule is generic: it encodes Docker's grammar, not any Actor.

MAX_ACTOR_NAME_LENGTH = 64

ACTOR_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:(?:\.|_{1,2}|-+)[a-z0-9]+)*")

ACTOR_NAME_RULE = (
    "an actor name must be lowercase alphanumerics, optionally separated by "
    "'.', '_', '__' or '-', starting and ending with a letter or digit, at most "
    f"{MAX_ACTOR_NAME_LENGTH} characters (e.g. 'my_actor', 'web-scraper')"
)


def is_valid_actor_name(name: str) -> bool:
    """True if `name` is safe to use as both an image tag and a path segment."""
    if not isinstance(name, str) or not name or len(name) > MAX_ACTOR_NAME_LENGTH:
        return False
    # fullmatch, not match: an anchored search would still accept a trailing
    # newline, which is exactly the kind of value a future API would pass in.
    return ACTOR_NAME_PATTERN.fullmatch(name) is not None


# --- Contract: what counts as a run id --------------------------------------
# Run ids are minted by the registry as <date>-<time>-<random>, which makes
# them sortable: lexicographic order is chronological order, and the suffix
# breaks same-second ties. They are also path segments under storage/runs/,
# so the same hardening as Actor names applies -- a run id that reaches the
# registry from an HTTP request must never be able to contain "..".

# The time field carries MICROSECONDS. Seconds are not enough to order runs:
# measured, 20,000 ids minted in a tight loop collapsed into ONE second-value
# and 62 millisecond-values, but 20,000 distinct microsecond-values -- so even
# milliseconds would leave a queue's ids ordered by their random suffix rather
# than by time. The 6-digit form is accepted for records written before this,
# and still sorts correctly: "-" is below every digit, so a legacy id sorts
# ahead of a microsecond id in the same second, which is also their true order.
#
# [0-9], not \d: Python's \d also matches Arabic-Indic and other Unicode
# digits, so "٢٠٢٦..." passed as a run id. Only the platform mints these, and
# only from ASCII digits; anything else in a path segment is not one of ours.
RUN_ID_PATTERN = re.compile(r"[0-9]{8}-[0-9]{6}(?:[0-9]{6})?-[0-9a-f]{6}")

RUN_ID_RULE = (
    "a run id looks like '20260902-073523481920-4cf822' "
    "(date-time-microseconds-suffix; the older 6-digit time form is still read)"
)


def is_valid_run_id(run_id: str) -> bool:
    """True if `run_id` is safe to use as a path segment under storage/runs/."""
    return isinstance(run_id, str) and bool(run_id) and RUN_ID_PATTERN.fullmatch(run_id) is not None


# The ONLY spelling of a UUID the platform accepts from a client: 36
# hex-and-dash characters. uuid.UUID() is far more forgiving -- it takes
# "urn:uuid:...", braces, upper case and no dashes -- and a string it accepted
# was then handed to Postgres as-is, where the uuid cast failed with a 500.
# Case is not part of the check: Postgres reads either, and a client that
# upper-cased an id it was given was always served.
UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def is_canonical_uuid(value: Any) -> bool:
    """True for a 36-character hyphenated UUID string, and nothing else."""
    return isinstance(value, str) and UUID_PATTERN.fullmatch(value) is not None


# --- Contract: run status ---------------------------------------------------


class RunStatus(str, Enum):
    """Where a run is in its lifecycle.

    PENDING and RUNNING are transient; the rest are terminal. FAILED and
    PLATFORM_ERROR are deliberately separate: one means the Actor ran and
    failed, the other means the platform could not run it. Collapsing them
    would lose the only signal a scheduler needs to decide whether a retry
    could possibly help.

    THERE IS NO TIMED_OUT. The platform does not bound how long an Actor may
    run: a scrape that legitimately takes six hours must be allowed to take six
    hours, and a status meaning "we killed it for being slow" would only ever
    be produced by a mechanism that must not exist. Verified before removal:
    none of the 1,448 stored records used it.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PLATFORM_ERROR = "PLATFORM_ERROR"
    # A run given up on before it could finish -- abandoned after too many
    # interrupted attempts, or stranded with no queued job. Distinct from
    # FAILED (the Actor ran and failed) and PLATFORM_ERROR (the platform could
    # not run it): CANCELLED means the platform STOPPED trying. There is no
    # user-facing cancel yet; this labels the outcomes recovery already produces
    # so they are visible as themselves rather than hiding inside PLATFORM_ERROR.
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES = frozenset(
    {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.PLATFORM_ERROR,
        RunStatus.CANCELLED,
    }
)


# --- Contract: process exit codes -------------------------------------------
# The worker's own exit code, not the Actor container's. Defined once here so
# the three-way distinction is a single documented fact rather than bare
# `return 1` / `return 2` scattered through the CLI.

EXIT_SUCCEEDED = 0
EXIT_ACTOR_FAILED = 1
EXIT_PLATFORM_ERROR = 2

_STATUS_EXIT_CODES = {
    RunStatus.SUCCEEDED: EXIT_SUCCEEDED,
    RunStatus.FAILED: EXIT_ACTOR_FAILED,
    RunStatus.PLATFORM_ERROR: EXIT_PLATFORM_ERROR,
    # The platform stopped trying rather than the Actor failing; a scheduler
    # reading the exit code should treat it like a platform-side give-up.
    RunStatus.CANCELLED: EXIT_PLATFORM_ERROR,
}


def worker_exit_code(status: RunStatus) -> int:
    """The process exit code that reports `status` to a caller or scheduler."""
    try:
        return _STATUS_EXIT_CODES[status]
    except KeyError:
        raise ValueError(f"{status.value} is not a terminal status") from None


# --- The run record ---------------------------------------------------------


@dataclass
class Run:
    """Everything the platform knows about one Actor execution.

    This is both the in-memory model and, via to_dict(), the on-disk run.json.
    The optional timestamps are genuinely unknown while a run is PENDING rather
    than being zero values.

    THE OUTPUT IS NOT HERE. It lives in output.json, and the record points at
    the run directory instead of copying it. Embedding it doubled the on-disk
    cost of every run (measured: an 88 KB result made a 91 KB record) and, worse,
    made list() parse every output just to render summaries that contain none of
    it. Records written before this still load -- from_dict ignores keys it does
    not know, so a legacy `output` field is simply dropped.
    """

    run_id: str
    actor: str
    status: RunStatus
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    exit_code: int | None = None
    error: str | None = None
    run_dir: str = ""
    log_lines: int = 0

    # --- Why it failed, structured. ADDITIVE, and `error` above is untouched.
    #
    # `error` stays exactly what it was -- a human sentence -- because the
    # dashboard renders it, the CLI prints it and every stored record already
    # has one. Widening that field into an object would have broken all three
    # to say something they were not asking for. This sits beside it instead:
    # shared.errors.RunError.to_dict(), carrying the category, whether a retry
    # could help, which stage failed and the Actor's or Docker's own narrower
    # code. A reader that knows nothing about it is unaffected; a reader that
    # does gets the machine-actionable half it could not previously have.
    #
    # A plain dict rather than the dataclass, for the reason this module gives
    # at the top: the Actor <-> platform contract must stay JSON, and a Run
    # round-trips through run.json and Postgres.
    error_info: dict[str, Any] | None = None

    # Whether a FAILED run still produced usable data.
    #
    # A forty-minute scrape that collected 640 of 1000 posts and then lost the
    # network has failed, and throwing those 640 away is a second failure the
    # platform inflicts on top of the first. The run stays FAILED -- it did not
    # do what was asked -- but this says the artifact is worth reading. False
    # for every run that produced nothing, which keeps the default honest.
    partial: bool = False
    # How many times this run has been STARTED. Above 1 means an earlier
    # attempt was abandoned -- a Worker died and the queue redelivered the job.
    attempts: int = 0
    input: dict[str, Any] = field(default_factory=dict)

    # --- Multi-tenancy. All optional, so a single-user deployment and every
    # record written before these existed are unchanged: from_dict() keeps only
    # known keys, so an old run.json still loads, and a new one still loads in
    # code that predates them.
    #
    # These are deliberately ABSENT from api/schemas.py. That module already
    # argues the point for run_dir -- a response model that cannot name a field
    # cannot leak it, however the internal record later changes.
    owner_id: str | None = None
    # Which stored social session this run was given. The concurrency key below
    # is derived from it; it is kept separately because a run may outlive the
    # account row it used.
    social_account_id: str | None = None
    # What must not run twice at once. None means "nothing to serialise on".
    # An opaque string, so the queue and the worker never need to know that the
    # thing being protected happens to be a social session.
    concurrency_key: str | None = None
    # The HOST port this run's viewable display is published on, for the Actors
    # that have one. Written by the Worker, which is the only component that may
    # ask Docker anything; the API needs it to proxy the display and must never
    # shell out to find it. None for every other run, which is nearly all of them.
    #
    # A port is not a secret and not an authorisation: it is bound to loopback,
    # and reaching it still requires an authenticated, ownership-checked route.
    display_port: int | None = None
    # THE LIMITS THIS RUN RAN UNDER (security review H-03). Written by the API
    # as what was REQUESTED, then overwritten by the Worker with what it
    # actually enforced once it has clamped the request -- so the record always
    # answers "how long was this run allowed to take, and how big could it get".
    # None means unlimited, which every run before per-run limits was.
    timeout_s: int | None = None
    memory_mb: int | None = None
    cpus: float | None = None
    # The highest memory use the Worker saw while the container ran, sampled
    # every few seconds. Recorded so the per-Actor defaults can be tightened
    # from measurements (scripts/review_run_limits.py) rather than guessed.
    peak_memory_mb: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Any) -> Run:
        """Rebuild a Run from a decoded run.json.

        Raises ValueError on anything that is not a usable record; the registry
        turns that into CorruptRunRecord. Unknown keys are ignored so a record
        written by a newer version still loads here.
        """
        if not isinstance(data, dict):
            raise ValueError(f"run record must be a JSON object, got {type(data).__name__}")

        missing = [key for key in ("run_id", "actor", "status") if not data.get(key)]
        if missing:
            raise ValueError(f"run record is missing required field(s): {', '.join(missing)}")

        known = {f.name for f in fields(cls)}
        kwargs = {key: value for key, value in data.items() if key in known}

        try:
            kwargs["status"] = RunStatus(kwargs["status"])
        except ValueError:
            raise ValueError(f"unknown run status {data['status']!r}") from None

        # Records written before the registry existed carry no created_at.
        # Falling back to started_at loads them rather than reading them as
        # corrupt; they were, after all, created when they started.
        if not kwargs.get("created_at"):
            kwargs["created_at"] = data.get("started_at") or ""

        return cls(**kwargs)

    @property
    def succeeded(self) -> bool:
        return self.status is RunStatus.SUCCEEDED

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES
