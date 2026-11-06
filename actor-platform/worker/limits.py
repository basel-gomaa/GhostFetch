"""Execution configuration: how much of the machine one run may take.

The Worker decides HOW MANY containers run at once and HOW BIG each one may be.
Both are operator policy, so they live here rather than being scattered through
the executor as bare defaults.

    WorkerConfig
      |-- concurrency        how many containers at once
      `-- ExecutionLimits    how much CPU / memory / time each one gets

Nothing here knows what an Actor does. limits_for() is keyed by name only so
that per-Actor overrides, when they arrive, are operator CONFIG rather than
code that has learned the difference between one Actor and another.

PER-RUN LIMITS (security review H-03, 2026-10-23). Every Actor now has a
`limits` section in config/actors.json -- a default and a maximum for the
wall-clock timeout, memory and CPUs, plus a process cap -- chosen from a review
of every past run (scripts/review_run_limits.py). A run may ask for different
values within the maximum; ActorLimits.resolve() is the single place a request
is clamped, and the Worker calls it again at execution time whatever the API
already checked, because a run record is not trusted to stay as written.

The RESOLVED limits are recorded on the run, so the console shows what a run
actually got. The Worker's own --cpus/--memory flags remain, as a host-wide
CEILING that no Actor default and no request can exceed.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Mapping

from shared.presentation import Icon, parse_icon, parse_label

# Conservative on purpose: one container at a time is exactly the behaviour
# every earlier milestone was built and measured against, so upgrading a
# deployment changes nothing until an operator asks it to. It is also the only
# safe default while limits are opt-in -- N unbounded containers can take the
# whole machine, and the platform has no way to know how big N should be.
DEFAULT_CONCURRENCY = 1

# How long to wait for in-flight runs when the Worker is asked to stop. A run
# that outlives it is left RUNNING with its lease held, which is precisely the
# crash case recovery already handles.
DEFAULT_SHUTDOWN_TIMEOUT_S = 60.0

# Where per-Actor policy is read from. A MISSING FILE IS NOT AN ERROR: every
# Actor then falls back to the defaults, which is exactly the behaviour that
# existed before per-Actor limits, so nothing has to be configured to run.
CONFIG_ENV = "ACTOR_CONFIG"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "actors.json"

# Keys a config document may carry. Anything else is REJECTED rather than
# ignored: a typo'd "concurrency" would otherwise read as "unlimited", which is
# the one mistake here that fails silently and dangerously.
_DOCUMENT_KEYS = frozenset({"global", "defaults", "actors", "_comment"})
_ACTOR_KEYS = frozenset({
    "max_concurrency", "note",
    # Session policy. It lives in the config file rather than in code because
    # no Actor may be NAMED in worker/, api/, jobqueue/ or registry/ -- a test
    # enforces that by grepping every module outside actors/ for site names,
    # which is why even this comment does not contain one.
    "session_cookies", "session_id_cookie", "session_note",
    "session_env", "session_out", "requires_social", "concurrency_scope", "login_url",
    # Whether a human-facing client should OFFER this Actor. Presentation, not
    # policy: a hidden Actor still runs, still resolves by name, and is still
    # reachable over the API -- the test fixtures need all three. It exists so
    # the console does not put "does it fail on purpose?" fixtures in front of
    # someone choosing a scraper.
    "hidden",
    # A platform listed in the picker but needing no connection, and the line
    # explaining why. Some sites serve their public data to anyone: there is no
    # session to capture, so requiring a connected account before a run would
    # block work that needs no credential at all.
    "session_optional", "platform_note",
    # Whether this Actor accumulates state that must outlive its container --
    # see ActorProfile.persistent_state. Opt-in, so an Actor that does not ask
    # for it gets no extra mount and the argv it has always had.
    "persistent_state", "state_note",
    # Whether this Actor's container exports a viewable display, so a person can
    # watch it work and intervene when the site asks for something only a human
    # can give. Opt-in: without it no port is published and the container argv
    # is byte-identical to what it has always been.
    "interactive_display", "display_note",
    # Whether this Actor attaches to a browser the OPERATOR runs on the host
    # rather than launching its own inside the container. Some sites refuse a
    # programmatically launched browser whatever it is told to look like, while
    # answering an ordinary one normally -- so the only way to collect is to
    # drive a real browser the person already has. Reaching it needs the host's
    # loopback, hence the network mode; naming it is the ACTOR's business, not
    # this module's.
    "host_network", "host_network_note",
    # Whether a person solving this Actor's challenge does so in a browser on
    # THEIR OWN machine. True only for an Actor that drives a browser the
    # operator is running; an Actor that launches its own inside the container
    # has no such window, and telling someone to "solve it there" sends them
    # looking for one that does not exist. Separate from host_network on
    # purpose: that one says how the container reaches the browser, this one
    # says where the PERSON should look.
    "drives_host_browser", "drives_host_browser_note",
    # Whether this Actor can PARK and ask a person to do something only a person
    # can do -- drag a slider, clear a check. It is what decides whether a
    # client offers "waiting for you" and an "I've solved it" override at all.
    # Separate from interactive_display: that says whether there is a window to
    # watch, this says whether anybody is ever asked. An Actor that cannot park
    # must never show either, because the prompt would be for a run that is not
    # waiting and an override that resumes nothing.
    "awaits_human", "awaits_human_note",
    # How a client should SHOW this Actor: a human label ("<site> Pages") and
    # a logo, plus the label of the PLATFORM it connects to where that differs
    # from the Actor's own. Presentation only -- see shared/presentation.py.
    "label", "platform_label", "icon",
    # Whether this Actor's Chrome runs with its own sandbox, which needs the
    # seccomp profile in deploy/seccomp/ (see shared/chrome_sandbox.py). Opt-in:
    # without it the container keeps Docker's default profile and the argv is
    # byte-identical to what it has always been.
    "chrome_sandbox", "chrome_sandbox_note",
    # The resource envelope of one run: default and maximum timeout, memory and
    # CPUs, a process cap and a /dev/shm size. See ActorLimits.
    "limits", "limits_note",
})
# Keys inside a "limits" section. Unknown ones are refused for the same reason
# as above: a typo'd "max_memory" must not quietly read as "no maximum".
_LIMIT_KEYS = frozenset({
    "timeout_minutes", "max_timeout_minutes",
    "memory_mb", "max_memory_mb",
    "cpus", "max_cpus",
    "pids", "shm_mb",
})
_GLOBAL_KEYS = frozenset({"max_concurrency", "note"})

# Docker's own size grammar: an integer with an optional b/k/m/g suffix.
_MEMORY_PATTERN = re.compile(r"(\d+)([bkmg])?$", re.IGNORECASE)

_SUFFIX_BYTES = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}

# Docker refuses a memory limit below 6MB.
MIN_MEMORY_BYTES = 6 * 1024**2


class LimitError(ValueError):
    """A limit that Docker could not honour, refused before any run starts."""


def _presentation(parse: Any, value: Any, where: str) -> Any:
    """A label or icon from config, with a bad entry reported as a config error."""
    try:
        return parse(value, where)
    except ValueError as exc:
        raise LimitError(str(exc)) from exc


def parse_memory(value: str) -> int:
    """-> bytes. Raises LimitError on anything Docker would reject.

    Validated here so a typo is a clear refusal at Worker startup rather than
    an opaque `docker run` failure minutes into someone's job.
    """
    match = _MEMORY_PATTERN.fullmatch(value.strip())
    if match is None:
        raise LimitError(
            f"invalid memory limit {value!r}: expected a number with an optional "
            "b/k/m/g suffix, e.g. '512m' or '2g'"
        )

    amount, suffix = match.groups()
    total = int(amount) * _SUFFIX_BYTES[(suffix or "b").lower()]

    if total < MIN_MEMORY_BYTES:
        raise LimitError(
            f"memory limit {value!r} is {total} bytes; docker's minimum is "
            f"{MIN_MEMORY_BYTES} bytes (6m)"
        )
    return total


@dataclass(frozen=True)
class ExecutionLimits:
    """What one container is allowed. Every field is optional.

    Unset means UNLIMITED. With no `limits` section in the config and no
    --cpus/--memory on the Worker, a run gets exactly the envelope every run
    before per-Actor limits got.

    timeout_s is enforced by the Worker, not by Docker: the executor's watcher
    stops the container (SIGTERM, then a kill after a grace period) once the
    run has been RUNNING that long, and records it as FAILED with a message
    naming the limit. It is the only field here that docker_args() does not
    translate into a flag.
    """

    cpus: float | None = None
    memory: str | None = None
    timeout_s: int | None = None
    pids: int | None = None
    shm: str | None = None

    def __post_init__(self) -> None:
        if self.cpus is not None and self.cpus <= 0:
            raise LimitError(f"cpu limit must be greater than 0, got {self.cpus}")
        if self.memory is not None:
            parse_memory(self.memory)  # raises LimitError on anything unusable
        if self.timeout_s is not None and self.timeout_s < 1:
            raise LimitError(f"timeout must be at least 1 second, got {self.timeout_s}")
        if self.pids is not None and self.pids < 16:
            raise LimitError(f"pids limit must be at least 16, got {self.pids}")
        if self.shm is not None:
            parse_memory(self.shm)

    @property
    def memory_mb(self) -> int | None:
        return None if self.memory is None else parse_memory(self.memory) // MB

    def docker_args(self) -> list[str]:
        """The `docker run` flags these limits translate to.

        --memory is ALWAYS paired with an equal --memory-swap. Docker's default
        is swap = 2 x memory, so a lone --memory lets a process quietly exceed
        its limit by swapping -- the limit would be advisory rather than
        enforced. Equal values give the container no swap at all, which is what
        makes an out-of-memory kill actually happen.
        """
        args: list[str] = []
        if self.cpus is not None:
            args += ["--cpus", f"{self.cpus:g}"]
        if self.memory is not None:
            args += ["--memory", self.memory, "--memory-swap", self.memory]
        # A process (and thread) cap. Without one a fork bomb, or a browser
        # leaking renderer processes, can exhaust the HOST's pid table and take
        # every other run -- and the Worker itself -- down with it.
        if self.pids is not None:
            args += ["--pids-limit", str(self.pids)]
        # Chrome keeps its renderer IPC in /dev/shm and crashes on Docker's
        # 64 MB default; the login browser has always set this for that reason.
        if self.shm is not None:
            args += ["--shm-size", self.shm]
        return args

    def describe(self) -> str:
        parts = [
            f"cpus={self.cpus:g}" if self.cpus is not None else "cpus=unlimited",
            f"memory={self.memory}" if self.memory is not None else "memory=unlimited",
        ]
        if self.timeout_s is not None:
            parts.append(f"timeout={self.timeout_s // 60}m{self.timeout_s % 60:02d}s")
        if self.pids is not None:
            parts.append(f"pids={self.pids}")
        return ", ".join(parts)


MB = 1024**2


@dataclass(frozen=True)
class RunOptions:
    """What ONE run asked for. Every field optional; None means "the default".

    Carried on the run record (timeout_s / memory_mb / cpus), so the API can
    accept it and the Worker can honour it without the two ever talking.
    """

    timeout_s: int | None = None
    memory_mb: int | None = None
    cpus: float | None = None

    @classmethod
    def from_run(cls, run: Any) -> RunOptions:
        def num(value: Any, kind: type) -> Any:
            # The record may have been written by a container (filesystem
            # registry) -- take a number or nothing, never a surprise type.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            return kind(value)
        return cls(
            timeout_s=num(getattr(run, "timeout_s", None), int),
            memory_mb=num(getattr(run, "memory_mb", None), int),
            cpus=num(getattr(run, "cpus", None), float),
        )


@dataclass(frozen=True)
class ActorLimits:
    """One Actor's `limits` section: defaults, maxima, and the fixed caps.

    All None -- an Actor with no section and no `defaults.limits` -- means no
    limit, i.e. exactly the old behaviour. Values are in the units a person
    types (minutes, MB); resolve() turns them into an ExecutionLimits.
    """

    timeout_minutes: int | None = None
    max_timeout_minutes: int | None = None
    memory_mb: int | None = None
    max_memory_mb: int | None = None
    cpus: float | None = None
    max_cpus: float | None = None
    pids: int | None = None
    shm_mb: int | None = None

    def __post_init__(self) -> None:
        for name in ("timeout_minutes", "max_timeout_minutes", "memory_mb",
                     "max_memory_mb", "pids", "shm_mb"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                raise LimitError(f"limits.{name} must be a positive integer, got {value!r}")
        for name in ("cpus", "max_cpus"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0):
                raise LimitError(f"limits.{name} must be a positive number, got {value!r}")
        for low, high in (("timeout_minutes", "max_timeout_minutes"),
                          ("memory_mb", "max_memory_mb"), ("cpus", "max_cpus")):
            a, b = getattr(self, low), getattr(self, high)
            if a is not None and b is not None and a > b:
                raise LimitError(f"limits.{low} ({a}) is above limits.{high} ({b})")
        if self.memory_mb is not None and self.memory_mb * MB < MIN_MEMORY_BYTES:
            raise LimitError("limits.memory_mb is below docker's 6 MB minimum")
        if self.pids is not None and self.pids < 16:
            raise LimitError(f"limits.pids must be at least 16, got {self.pids}")

    @classmethod
    def from_config(cls, block: Any, where: str) -> ActorLimits:
        if block is None:
            return cls()
        if not isinstance(block, dict):
            raise LimitError(f"{where}: 'limits' must be an object")
        bad = set(block) - _LIMIT_KEYS
        if bad:
            raise LimitError(f"{where}: unknown key(s) in limits: {', '.join(sorted(bad))}")
        try:
            return cls(**block)
        except LimitError as exc:
            raise LimitError(f"{where}: {exc}") from None

    @property
    def configured(self) -> bool:
        return any(getattr(self, f.name) is not None for f in fields(self))

    def problems(self, requested: RunOptions) -> list[str]:
        """Why a request is not acceptable, or [] -- for the API to answer 400.

        Over the maximum is refused rather than silently lowered at submission,
        so a caller is never surprised by a shorter run than they asked for.
        (The Worker still CLAMPS, because by then refusing is no longer an
        option and the record may not be what the API accepted.)
        """
        out: list[str] = []
        if requested.timeout_s is not None:
            if requested.timeout_s < 60:
                out.append("timeout_minutes must be at least 1")
            elif self.max_timeout_minutes and requested.timeout_s > self.max_timeout_minutes * 60:
                out.append(f"timeout_minutes may be at most {self.max_timeout_minutes} for this actor")
        if requested.memory_mb is not None:
            if requested.memory_mb * MB < MIN_MEMORY_BYTES or requested.memory_mb < 64:
                out.append("memory_mb must be at least 64")
            elif self.max_memory_mb and requested.memory_mb > self.max_memory_mb:
                out.append(f"memory_mb may be at most {self.max_memory_mb} for this actor")
        if requested.cpus is not None:
            if requested.cpus < 0.1:
                out.append("cpus must be at least 0.1")
            elif self.max_cpus and requested.cpus > self.max_cpus:
                out.append(f"cpus may be at most {self.max_cpus:g} for this actor")
        return out

    def resolve(self, requested: RunOptions | None = None,
                ceiling: ExecutionLimits | None = None) -> ExecutionLimits:
        """The limits one run actually gets. THE single clamp.

        request (if any) -> else this Actor's default -> never above this
        Actor's maximum -> never above the Worker's own --cpus/--memory.
        With nothing configured anywhere this returns `ceiling` unchanged,
        which is what keeps an unconfigured deployment byte-identical.
        """
        requested = requested or RunOptions()
        ceiling = ceiling or ExecutionLimits()

        def pick(asked: Any, default: Any, maximum: Any) -> Any:
            value = asked if asked is not None else default
            if value is not None and maximum is not None:
                value = min(value, maximum)
            return value

        timeout_s = pick(requested.timeout_s,
                         self.timeout_minutes * 60 if self.timeout_minutes else None,
                         self.max_timeout_minutes * 60 if self.max_timeout_minutes else None)
        if timeout_s is not None:
            timeout_s = max(60, int(timeout_s))

        memory_mb = pick(requested.memory_mb, self.memory_mb, self.max_memory_mb)
        if memory_mb is not None:
            # The floor guards a REQUEST; it is applied before the ceiling so an
            # operator's own --memory, however small, is still honoured exactly.
            memory_mb = max(64, int(memory_mb))
        if ceiling.memory is not None:
            cap = ceiling.memory_mb
            memory_mb = cap if memory_mb is None else min(memory_mb, cap)

        cpus = pick(requested.cpus, self.cpus, self.max_cpus)
        if ceiling.cpus is not None:
            cpus = ceiling.cpus if cpus is None else min(cpus, ceiling.cpus)

        if not self.configured and requested == RunOptions():
            return ceiling
        return ExecutionLimits(
            cpus=float(cpus) if cpus is not None else None,
            # Keep the operator's own spelling when the ceiling is what applies,
            # so a Worker started with --memory 256m still emits "256m".
            memory=(ceiling.memory if ceiling.memory is not None and memory_mb == ceiling.memory_mb
                    else (f"{memory_mb}m" if memory_mb is not None else None)),
            timeout_s=timeout_s,
            pids=self.pids if self.pids is not None else ceiling.pids,
            shm=f"{self.shm_mb}m" if self.shm_mb is not None else ceiling.shm,
        )

    def public(self) -> dict[str, Any]:
        """What a client may show and offer: defaults and maxima, nothing else."""
        return {
            "timeout_minutes": {"default": self.timeout_minutes, "max": self.max_timeout_minutes},
            "memory_mb": {"default": self.memory_mb, "max": self.max_memory_mb},
            "cpus": {"default": self.cpus, "max": self.max_cpus},
        }


@dataclass(frozen=True)
class ActorProfile:
    """Everything the Worker needs to know about running ONE Actor.

    max_concurrency of None means "bounded only by the global limit", which is
    the default and is exactly how every Actor behaved before per-Actor limits
    existed. That is what makes this addition non-breaking: an unconfigured
    Actor is unchanged.
    """

    max_concurrency: int | None = None
    execution: ExecutionLimits = field(default_factory=ExecutionLimits)
    # This Actor's limits section (defaults and maxima per run). `execution`
    # above is the Worker's host-wide ceiling; this is the per-Actor policy
    # applied beneath it -- see ActorLimits.resolve.
    limits: ActorLimits = field(default_factory=ActorLimits)

    # Which cookies prove a live session for this Actor's site, and which one
    # names the account. Empty means "this Actor needs no stored session",
    # which is every Actor unless its config says otherwise -- so adding these
    # changed nothing for hello_world or item_processor.
    session_cookies: tuple[str, ...] = ()
    session_id_cookie: str | None = None
    # The environment variable this Actor reads its session from. The platform
    # knows the NAME and passes the value by it; the value itself never enters
    # argv, the input document, the run record, the queue job or a log.
    session_env: str | None = None
    # Which connected-account PLATFORM a run of this Actor must name. Usually
    # the Actor's own name, but they are separate ideas: several Actors can read
    # one platform's session (a page scraper and a group scraper, say), and they
    # must all resolve to the same stored account rather than to one each.
    requires_social: str | None = None
    # WHAT must not run twice at once. None means the Actor as a whole, capped
    # by max_concurrency. "social_account" means one run per CONNECTED ACCOUNT,
    # which is the honest shape of the constraint: two scrapes on ONE session
    # race its write-back and make the account look automated, while thirty
    # people scraping on thirty sessions are not in each other's way at all.
    concurrency_scope: str | None = None
    # Whether this Actor writes a refreshed session back for the platform to
    # re-encrypt. Opt-in, so an Actor that does not gets no credential mount.
    session_out: bool = False
    # Whether a human-facing client should offer this Actor. NOT a permission:
    # it is never consulted when starting, resolving or executing a run, so
    # hiding one cannot break anything that already runs it by name.
    hidden: bool = False
    # How a client shows it (shared/presentation.py). Presentation only, like
    # `hidden`: never read when starting, resolving or executing a run.
    label: str | None = None
    icon: Icon | None = None
    # This Actor names a platform, but does not REQUIRE a connected account for
    # it. Without this, naming a platform is what makes the API demand one and
    # refuse the run -- which would be wrong for a site that serves its public
    # data anonymously.
    session_optional: bool = False
    # Whether this Actor needs a directory that SURVIVES its container, kept per
    # connected account.
    #
    # Everything else a run touches is disposable by design, and for an Actor
    # that drives a real browser that is a problem rather than a property: the
    # browser's own profile is what a site recognises on a return visit, and
    # rebuilding it per run means presenting as a new device every time. A
    # stored session alone cannot stand in for it -- it carries cookies, not the
    # local storage and databases where a site records that this device has
    # already been checked.
    #
    # Opt-in, and it does nothing without a connected account: state built while
    # acting as one account must never be given to another, so a run with no
    # account gets no mount rather than a shared one.
    persistent_state: bool = False
    # Whether this Actor's container publishes a viewable display.
    #
    # Some sites answer an automated client with a challenge that NO amount of
    # collector work can clear -- a slider a person has to drag. The honest
    # options are to fail the run or to let a person do it, and doing it needs
    # two things the platform can provide and the Actor cannot: a port the
    # display is reachable on, and somewhere to record that a run is waiting.
    #
    # Opt-in and inert by itself: it publishes a loopback port, nothing more.
    # An Actor that does not ask for one is completely unaffected.
    interactive_display: bool = False
    # Whether this Actor's container shares the HOST network namespace.
    #
    # MEASURED, and the reason is not performance: a browser this platform
    # launches is refused by some sites on sight -- identical machine, identical
    # address, identical browser binary -- while one the operator started and is
    # using normally is answered in full. Attaching to that browser means
    # reaching a port on the host's loopback, and host networking is the way to
    # do it that adds NO exposure: the browser's debug port stays bound to
    # 127.0.0.1 and is never published anywhere.
    #
    # The cost is real and bounded: this Actor's container loses network-
    # namespace isolation, and published ports stop meaning anything for it --
    # so it cannot also export a viewable display (see the check below). An
    # Actor that does not ask for this is completely unaffected.
    host_network: bool = False
    # Where a person goes to solve a challenge this Actor parks on. False means
    # the browser lives inside the container, so the only way to see it is a
    # viewable display (interactive_display); True means it is a browser on the
    # operator's own machine and they should look there.
    #
    # NOT derived from host_network, though the two travelled together while the
    # only reason to share the host namespace was to reach such a browser. They
    # answer different questions, and deriving one from the other would make the
    # console's advice a side effect of a networking decision.
    drives_host_browser: bool = False
    # Whether this Actor ever parks waiting for a person. False for all but one:
    # only an Actor that installs a challenge handler can be waiting, and a
    # client that offers the prompt regardless is describing a run that is not.
    awaits_human: bool = False
    # Whether this Actor's container gets deploy/seccomp/chrome-sandbox.json and
    # CHROME_SANDBOX=1, so the Chrome inside it keeps its own sandbox instead of
    # running with --no-sandbox. Only for an Actor that runs Chrome: the profile
    # lets a process create user/PID/network namespaces, which an Actor with no
    # browser has no use for. Every other Actor keeps Docker's default profile.
    chrome_sandbox: bool = False

    def __post_init__(self) -> None:
        if self.max_concurrency is not None and self.max_concurrency < 1:
            raise LimitError(
                f"max_concurrency must be at least 1, got {self.max_concurrency}"
            )
        if self.concurrency_scope not in (None, "social_account"):
            raise LimitError(
                f"unknown concurrency_scope {self.concurrency_scope!r}; "
                f"the only scope is 'social_account'"
            )
        if self.session_env and not self.session_cookies:
            # Otherwise the platform would inject a credential it has no way to
            # validate, and a logged-out jar would be indistinguishable from a
            # good one at every layer.
            raise LimitError(
                f"session_env {self.session_env!r} needs session_cookies to say "
                f"what a valid session looks like"
            )
        if self.host_network and self.interactive_display:
            # A published port means nothing in the host namespace -- the
            # container would bind the host's own port directly, which is
            # neither ephemeral nor loopback-scoped by us, and the API would
            # proxy to a port no run actually owns. Refuse the combination
            # rather than emit argv whose two halves contradict each other.
            raise LimitError(
                "host_network and interactive_display cannot both be set: "
                "published ports do not apply in the host network namespace"
            )
        if self.session_id_cookie and self.session_id_cookie not in self.session_cookies:
            # Otherwise the account id could be read from a jar that was never
            # accepted as a session -- the two must describe the same jar.
            raise LimitError(
                f"session_id_cookie {self.session_id_cookie!r} is not one of "
                f"session_cookies {list(self.session_cookies)}"
            )

    @property
    def per_key_limit(self) -> int | None:
        """How many runs may share one concurrency key. None means unlimited."""
        return 1 if self.concurrency_scope == "social_account" else None

    @property
    def needs_session(self) -> bool:
        return bool(self.session_cookies)

    def describe(self) -> str:
        bound = "global limit" if self.max_concurrency is None else str(self.max_concurrency)
        return f"concurrency={bound}, {self.execution.describe()}"


def _int_or_none(value: Any, where: str) -> int | None:
    if value is None:
        return None
    # bool is an int subclass; `true` would otherwise silently mean 1.
    if not isinstance(value, int) or isinstance(value, bool):
        raise LimitError(f"{where}: max_concurrency must be an integer or null, got {value!r}")
    return value


def load_actor_config(path: str | Path | None = None) -> tuple[int | None, ActorProfile, dict[str, ActorProfile]]:
    """Read config/actors.json. -> (global concurrency, defaults, per-actor).

    Returns (None, default profile, {}) when there is no file, so an
    unconfigured deployment behaves exactly as it did before.
    """
    if path is None:
        path = os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG_PATH
    path = Path(path)

    if not path.is_file():
        return None, ActorProfile(), {}

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LimitError(f"could not read actor config {path}: {exc}") from exc

    if not isinstance(document, dict):
        raise LimitError(f"{path}: the actor config must be a JSON object")

    unknown = set(document) - _DOCUMENT_KEYS
    if unknown:
        raise LimitError(f"{path}: unknown top-level key(s): {', '.join(sorted(unknown))}")

    def section(name: str, allowed: frozenset[str]) -> dict[str, Any]:
        block = document.get(name) or {}
        if not isinstance(block, dict):
            raise LimitError(f"{path}: '{name}' must be an object")
        bad = set(block) - allowed
        if bad:
            raise LimitError(f"{path}: unknown key(s) in '{name}': {', '.join(sorted(bad))}")
        return block

    global_block = section("global", _GLOBAL_KEYS)
    global_concurrency = _int_or_none(global_block.get("max_concurrency"), f"{path}: global")
    if global_concurrency is not None and global_concurrency < 1:
        raise LimitError(f"{path}: global max_concurrency must be at least 1")

    defaults_block = section("defaults", _ACTOR_KEYS)
    default_limits = ActorLimits.from_config(defaults_block.get("limits"), f"{path}: defaults")
    defaults = ActorProfile(
        max_concurrency=_int_or_none(defaults_block.get("max_concurrency"), f"{path}: defaults"),
        limits=default_limits,
    )

    actors_block = document.get("actors") or {}
    if not isinstance(actors_block, dict):
        raise LimitError(f"{path}: 'actors' must be an object")

    actors: dict[str, ActorProfile] = {}
    for name, entry in actors_block.items():
        if not isinstance(entry, dict):
            raise LimitError(f"{path}: actors.{name} must be an object")
        bad = set(entry) - _ACTOR_KEYS
        if bad:
            raise LimitError(f"{path}: unknown key(s) in actors.{name}: {', '.join(sorted(bad))}")
        cookies = entry.get("session_cookies") or []
        if not isinstance(cookies, list) or not all(isinstance(c, str) and c for c in cookies):
            raise LimitError(
                f"{path}: actors.{name}.session_cookies must be a list of cookie names"
            )
        actors[name] = ActorProfile(
            max_concurrency=_int_or_none(entry.get("max_concurrency"), f"{path}: actors.{name}"),
            session_cookies=tuple(cookies),
            session_id_cookie=entry.get("session_id_cookie"),
            session_env=entry.get("session_env"),
            session_out=bool(entry.get("session_out")),
            # Defaults to the Actor's own name, so the common case needs no
            # configuration and cannot be forgotten into a mismatch.
            requires_social=entry.get("requires_social")
            or (name if entry.get("session_env") else None),
            concurrency_scope=entry.get("concurrency_scope"),
            hidden=bool(entry.get("hidden")),
            session_optional=bool(entry.get("session_optional")),
            persistent_state=bool(entry.get("persistent_state")),
            interactive_display=bool(entry.get("interactive_display")),
            host_network=bool(entry.get("host_network")),
            drives_host_browser=bool(entry.get("drives_host_browser")),
            chrome_sandbox=bool(entry.get("chrome_sandbox")),
            # An Actor with no section of its own takes the defaults' section
            # WHOLE rather than field by field: a half-inherited section is a
            # maximum from one place and a default from another, which nobody
            # reading the file would predict.
            limits=(ActorLimits.from_config(entry["limits"], f"{path}: actors.{name}")
                    if "limits" in entry else default_limits),
            awaits_human=bool(entry.get("awaits_human")),
            label=_presentation(parse_label, entry.get("label"), f"{path}: actors.{name}"),
            icon=_presentation(parse_icon, entry.get("icon"), f"{path}: actors.{name}"),
        )

    return global_concurrency, defaults, actors


@dataclass(frozen=True)
class WorkerConfig:
    """One Worker's execution policy.

    Two bounds, both enforced: `concurrency` caps the containers this Worker
    runs at all, and each Actor's own max_concurrency caps how many of those
    may be that Actor. A run needs a slot from BOTH.
    """

    concurrency: int = DEFAULT_CONCURRENCY
    limits: ExecutionLimits = field(default_factory=ExecutionLimits)
    defaults: ActorProfile = field(default_factory=ActorProfile)
    actors: Mapping[str, ActorProfile] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise LimitError(f"concurrency must be at least 1, got {self.concurrency}")

    @classmethod
    def load(
        cls,
        *,
        path: str | Path | None = None,
        concurrency: int | None = None,
        limits: ExecutionLimits | None = None,
    ) -> WorkerConfig:
        """Build a config from the file, letting explicit arguments win.

        Precedence for the global bound: an explicit --concurrency, then the
        file's global.max_concurrency, then DEFAULT_CONCURRENCY. The flag wins
        because an operator typing a number on the command line means it.
        """
        file_concurrency, defaults, actors = load_actor_config(path)
        resolved = concurrency if concurrency is not None else file_concurrency
        return cls(
            concurrency=resolved if resolved is not None else DEFAULT_CONCURRENCY,
            limits=limits if limits is not None else ExecutionLimits(),
            defaults=defaults,
            actors=actors,
        )

    def limits_for(self, actor: str) -> ActorProfile:
        """THE single resolution point for one Actor's execution policy.

        Everything Actor-specific is decided here and nowhere else, which is
        what keeps the Worker generic: no Actor name appears in worker/, api/,
        jobqueue/ or registry/ -- only in the config file this reads.
        """
        profile = self.actors.get(actor, self.defaults)
        # `execution` is the Worker's host-wide ceiling; `limits` is the
        # Actor's own section. The executor resolves the two together per run.
        return ActorProfile(
            max_concurrency=profile.max_concurrency,
            execution=self.limits,
            limits=profile.limits,
            session_cookies=profile.session_cookies,
            session_id_cookie=profile.session_id_cookie,
            session_env=profile.session_env,
            session_out=profile.session_out,
            requires_social=profile.requires_social,
            concurrency_scope=profile.concurrency_scope,
            hidden=profile.hidden,
            session_optional=profile.session_optional,
            persistent_state=profile.persistent_state,
            interactive_display=profile.interactive_display,
            host_network=profile.host_network,
            drives_host_browser=profile.drives_host_browser,
            awaits_human=profile.awaits_human,
            label=profile.label,
            icon=profile.icon,
        )

    def describe_actors(self) -> str:
        if not self.actors:
            return "no per-actor limits"
        return ", ".join(
            f"{name}={p.max_concurrency if p.max_concurrency is not None else 'global'}"
            for name, p in sorted(self.actors.items())
        )

    def with_limits(self, **changes: object) -> WorkerConfig:
        """A copy with some limits changed. For tests and callers that tune one field."""
        return replace(self, limits=replace(self.limits, **changes))  # type: ignore[arg-type]
