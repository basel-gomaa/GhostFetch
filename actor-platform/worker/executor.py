#!/usr/bin/env python3
"""The Actor execution engine.

Takes an Actor name plus a JSON input and produces a completed run:

    input JSON -> run directory -> docker container -> output JSON + logs

The worker is deliberately ignorant of two things. It does not know what any
Actor does -- only the contract in shared/models.py, which is why the same code
path runs every Actor unchanged. And it does not know how a run is stored --
that belongs to the Run Registry:

    Worker
      |-- Actor Executor    this module: docker build / docker run / logs
      `-- Run Registry      registry/run_registry.py: storage/runs/<run_id>/

Usage:

    python worker/executor.py hello_world --input '{"name": "Basel"}'
    python worker/executor.py hello_world --input-file ./my_input.json
    python worker/executor.py hello_world --input '{"name": "Basel"}' --build
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from registry.run_registry import (  # noqa: E402
    DEFAULT_MAX_LOG_BYTES,
    MAX_LOG_BYTES_ENV,
    log_truncation_note,
    size_limit,
    ArtifactError,
    InvalidTransition,
    RegistryError,
    RunRegistry,
)
from catalog import actors as catalog  # noqa: E402
from worker.limits import (  # noqa: E402
    ActorProfile,
    ExecutionLimits,
    LimitError,
    RunOptions,
)
from shared.errors import (  # noqa: E402
    CONTAINER_OOM,
    ErrorCategory,
    RunError,
    category_for_actor_status,
    classify_container_exit,
    parse_retry_after,
)
from shared.chrome_sandbox import docker_args as chrome_sandbox_args  # noqa: E402
from shared.logging import event, write_line  # noqa: E402
from shared.display_token import ENV_DISPLAY_TOKEN, display_token  # noqa: E402
from shared.state_dirs import state_root  # noqa: E402
from shared import safe_fs  # noqa: E402
from shared import docker_net  # noqa: E402
from shared import image_freshness  # noqa: E402
from worker.dataset_ingest import DatasetIngestor  # noqa: E402
from worker.run_secrets import SecretResolver, SessionBusy  # noqa: E402
from shared.models import (  # noqa: E402
    ACTOR_SECRETS_DIR,
    ACTOR_DISPLAY_PORT,
    ACTOR_STATE_DIR,
    ACTOR_STORAGE_DIR,
    DEFAULT_INPUT_PATH,
    DEFAULT_OUTPUT_PATH,
    ENV_INPUT_PATH,
    ENV_OUTPUT_PATH,
    ENV_FORWARD_LIST,
    ENV_RUN_ID,
    ENV_SESSION_OUT,
    ENV_STATE_DIR,
    EXIT_PLATFORM_ERROR,
    INPUT_FILE,
    RESERVED_ENV_NAMES,
    RUN_FILE,
    SESSION_OUT_FILE,
    Run,
    RunStatus,
    worker_exit_code,
)

ACTORS_DIR = catalog.ACTORS_DIR
IMAGE_PREFIX = "actor-platform"

__all__ = ["ExecutionLimits"]

# EVERY RUN HAS A WALL-CLOCK LIMIT (security review H-03, 2026-10-23).
#
# This used to say there was deliberately no timeout, because real scrapes are
# measured at 17 to 50 minutes and a platform-wide number would kill real work.
# That reasoning still holds for a SINGLE number, which is why the limit is per
# Actor and per run instead: config/actors.json gives each Actor a default and a
# maximum chosen from a review of every past run (the longest successful run
# was 48 minutes; that Actor's default is 120), and a run can ask for more up to
# the maximum. Without any limit, one stuck browser holds a worker slot -- and
# its memory -- for ever, which on a shared deployment is a denial of service.
#
# The watcher thread enforces it: past the limit it STOPS the container
# (SIGTERM, then a kill after STOP_GRACE_S), and the run is recorded as FAILED
# with a message naming the limit, so it never reads as a crash. Anything the
# Actor already wrote -- output, dataset items -- is kept, as for any failed run.
#
# kill_container() is separate and unchanged: it is RECOVERY, removing a
# container a dead Worker left orphaned.
STOP_GRACE_S = 30

# Every Actor container joins this network instead of Docker's default bridge
# (security review M-02). It is created with inter-container communication
# OFF, so one run's container cannot reach another's ports -- including a
# run's viewable display -- while outbound internet and loopback-published ports
# work exactly as before. An Actor declaring host_network still gets the host.
ACTOR_NETWORK = docker_net.ACTOR_NETWORK
# Its host-side interface. Fixed (not Docker's br-<id>) so deploy/host-firewall.sh
# can refuse connections from these containers to the host itself (N-01).
ACTOR_BRIDGE = docker_net.ACTOR_BRIDGE

# How often the Worker samples a running container's memory use. The peak is
# recorded on the run so the per-Actor memory defaults can be tuned from data.
MEMORY_SAMPLE_INTERVAL_S = 5.0

# How long to wait for a killed container's NAME to become free again. `docker
# run --rm` removes the container asynchronously, so a fast retry can otherwise
# collide with the corpse of the attempt it just killed.
NAME_RELEASE_TIMEOUT_S = 15.0
NAME_POLL_INTERVAL_S = 0.1

# How often the cancel-watcher checks whether its run has been marked CANCELLED.
# Small enough to feel responsive, large enough to be a trivial read load.
CANCEL_POLL_INTERVAL_S = 0.5


class WorkerError(RuntimeError):
    """A failure of the platform itself, as opposed to a failing Actor run."""


class InvalidActorName(WorkerError):
    """The name could never denote an Actor."""


class UnknownActor(WorkerError):
    """A well-formed name, but no such Actor is installed."""


class RunAlreadyClaimed(WorkerError):
    """Something else moved this run out of PENDING first.

    Not a failure of the run -- a failure of THIS attempt to claim it, which
    under concurrency is an ordinary outcome rather than an error. The caller
    should drop its delivery and leave the winner alone.
    """


# --- helpers ---------------------------------------------------------------


def log(message: str) -> None:
    """Human-facing progress goes to stderr, so stdout carries only the run
    result JSON and `worker.py ... | jq` works."""
    write_line(f"[worker] {message}")


def image_tag(actor: str) -> str:
    return f"{IMAGE_PREFIX}/{actor}:latest"


def actor_dir(actor: str) -> Path:
    """Resolve an Actor to its build context.

    The lookup itself belongs to the catalog, which the API also uses -- one
    definition of what an Actor is, rather than two that can drift.

    The exceptions ARE re-raised rather than passed through, and that is
    load-bearing: main() catches WorkerError to return EXIT_PLATFORM_ERROR, so
    a raw CatalogError escaping here would silently stop an invalid Actor name
    exiting 2.
    """
    try:
        return catalog.find_actor(actor).path
    except catalog.InvalidActorName as exc:
        raise InvalidActorName(str(exc)) from exc
    except catalog.UnknownActor as exc:
        raise UnknownActor(str(exc)) from exc


def require_docker() -> None:
    if shutil.which("docker") is None:
        raise WorkerError("docker is not installed or not on PATH")
    probe = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        raise WorkerError(f"cannot reach the docker daemon: {probe.stderr.strip()}")


# --- build -----------------------------------------------------------------


def build_image(actor: str, context_hash: str | None = None) -> None:
    """Build the Actor's image, streaming the build log through.

    The image is labelled with `context_hash` -- the hash of the context taken
    BEFORE the build started. If a file changes mid-build the label is behind
    the context, and the next run sees a mismatch and rebuilds: self-correcting.
    """
    tag = image_tag(actor)
    context = actor_dir(actor)
    if context_hash is None:
        context_hash = image_freshness.context_hash(context)
    log(f"building {tag} from {context} (context {image_freshness.short(context_hash)})")

    result = subprocess.run(
        ["docker", "build", *image_freshness.build_args(context_hash), "-t", tag, str(context)]
    )
    if result.returncode != 0:
        raise WorkerError(f"docker build failed for actor '{actor}' (exit {result.returncode})")

    log(f"built {tag}")


# One lock per Actor, so a cold start under concurrency builds each image once.
# Without it N threads all pass the staleness check and all run `docker
# build` for the same tag: wasted work, and N interleaved build logs on one
# stderr. Per-actor rather than global, so two DIFFERENT Actors still build in
# parallel -- the lock exists to stop duplicate work, not to serialise the
# Worker.
_BUILD_LOCKS: dict[str, threading.Lock] = {}
_BUILD_LOCKS_GUARD = threading.Lock()


def _build_lock(actor: str) -> threading.Lock:
    with _BUILD_LOCKS_GUARD:
        return _BUILD_LOCKS.setdefault(actor, threading.Lock())


def image_staleness(actor: str) -> tuple[str, str | None, bool]:
    """(the context's hash now, why the image is stale or None, does it exist).

    See shared/image_freshness.py. A seam of its own so tests can drive the
    decision without Docker.
    """
    return image_freshness.staleness(image_tag(actor), actor_dir(actor))


def ensure_image(actor: str, *, force_build: bool) -> str | None:
    """Make sure the Actor's image is built from its sources AS THEY ARE NOW.

    An image is rebuilt when it is missing, carries no context-hash label, or
    carries a different one -- not only when it is missing, which is how stale
    code used to run for weeks after a fix. Double-checked under a per-Actor
    lock: the second thread re-tests inside it and finds the image the first one
    built, rather than building it again.

    Returns None normally. When a REBUILD fails but an older image exists, the
    run goes ahead on that image (an operator's choice: a registry outage must
    not stop every run) and this returns a note for the run's own log, so
    nobody reading that run mistakes it for one on current code. With no image
    at all a failed build raises, as it always did.
    """
    tag = image_tag(actor)

    def current() -> tuple[str, str | None, bool]:
        want, reason, exists = image_staleness(actor)
        if force_build:
            reason = reason or "rebuild forced"
        if reason is None:
            log(f"reusing image {tag} (context {image_freshness.short(want)})")
        return want, reason, exists

    if current()[1] is None:
        return None

    with _build_lock(actor):
        want, reason, exists = current()
        if reason is None:
            return None
        if exists:
            log(f"image {tag} is stale ({reason}): rebuilding")
        try:
            build_image(actor, want)
        except WorkerError as exc:
            # No image to fall back on, or the image is CURRENT and the build
            # was only forced (--build): fail, exactly as before. Falling back
            # there would label an up-to-date image "older sources".
            if not exists or reason == "rebuild forced":
                raise
            log(f"WARNING: {exc}; running the OLD image {tag}, built from older sources")
            event("worker", "stale_image", actor=actor, image=tag, reason=reason, error=str(exc))
            return image_freshness.stale_note(tag, reason, str(exc))
        return None


# --- run -------------------------------------------------------------------


def forwarded_env_names(environ: dict[str, str] | None = None) -> list[str]:
    """Which environment variables to hand every Actor container.

    Named in ACTOR_FORWARD_ENV, resolved from the Worker's own environment. This
    is the platform's whole answer to secrets, and it is deliberately small: it
    knows the NAMES, never the values, and no value is written down anywhere.

    Actor-agnostic on purpose. Every Actor container gets the same list, so no
    Actor's name or credential shape appears anywhere in the platform.
    """
    env = environ if environ is not None else dict(os.environ)

    names: list[str] = []
    for raw in env.get(ENV_FORWARD_LIST, "").split(","):
        name = raw.strip()
        if not name or name in names:
            continue
        if name in RESERVED_ENV_NAMES:
            log(f"refusing to forward {name}: it is part of the Actor contract")
            continue
        if name not in env:
            log(f"{name} is listed in {ENV_FORWARD_LIST} but is not set - not forwarded")
            continue
        names.append(name)

    return names


_network_lock = threading.Lock()
_network_ready = False


def ensure_actor_network() -> str:
    """Create ACTOR_NETWORK once: inter-container traffic off, fixed bridge name.

    Idempotent and cached per process; the work is shared/docker_net.py's,
    which the login manager uses for its own bridge too. A pre-existing network
    with the wrong options is recreated when nothing is attached to it, and
    otherwise used as it is with a warning -- refusing every run because of a
    network a person made would be worse than one that is merely less strict.
    Raises WorkerError if Docker cannot provide it at all: a run must not
    silently fall back to the shared default bridge.
    """
    global _network_ready
    with _network_lock:
        if _network_ready:
            return ACTOR_NETWORK
        try:
            docker_net.ensure_isolated_bridge(ACTOR_NETWORK, ACTOR_BRIDGE, warn=log)
        except docker_net.NetworkError as exc:
            raise WorkerError(str(exc)) from exc
        _network_ready = True
        return ACTOR_NETWORK


def stop_container(run_id: str, grace_s: int = STOP_GRACE_S) -> bool:
    """SIGTERM the run's container, then kill it after `grace_s`. -> stopped?

    Gentler than kill_container(): an Actor that handles SIGTERM gets a moment
    to flush what it has. Used for the wall-clock limit, where the run did
    nothing wrong except take too long.
    """
    result = subprocess.run(
        ["docker", "stop", "-t", str(grace_s), f"actor-{run_id}"],
        capture_output=True, text=True,
    )
    return result.returncode == 0


_MEM_UNITS = {"b": 1, "kb": 1000, "kib": 1024, "mb": 1000**2, "mib": 1024**2,
              "gb": 1000**3, "gib": 1024**3, "tb": 1000**4, "tib": 1024**4}


def parse_mem_usage(text: str) -> int | None:
    """'123.4MiB / 2GiB' (docker stats MemUsage) -> 123 (MB used), or None."""
    used = text.split("/")[0].strip()
    match = re.fullmatch(r"([\d.]+)\s*([A-Za-z]+)", used)
    if not match:
        return None
    unit = _MEM_UNITS.get(match.group(2).lower())
    if unit is None:
        return None
    try:
        return int(float(match.group(1)) * unit // 1024**2)
    except ValueError:
        return None


def container_memory_mb(run_id: str) -> int | None:
    """Current memory use of the run's container in MB, or None if unknown."""
    probe = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", f"actor-{run_id}"],
        capture_output=True, text=True, timeout=20,
    )
    if probe.returncode != 0:
        return None
    return parse_mem_usage(probe.stdout.strip())


def display_port(run_id: str) -> int | None:
    """The host port this run's viewable display was published on, or None.

    Loopback is REQUIRED, not merely expected: a display published on a routable
    address would be reachable from off-host by anyone who guessed the port,
    which is exactly the authorisation-by-obscurity the proxy exists to avoid.
    A non-loopback binding is refused rather than recorded.

    None while the container is still starting -- `docker port` has nothing to
    say until it is up -- so the caller polls rather than treating one empty
    answer as an absence.
    """
    probe = subprocess.run(
        ["docker", "port", f"actor-{run_id}", str(ACTOR_DISPLAY_PORT)],
        capture_output=True, text=True,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        return None
    for line in probe.stdout.split():
        host, _, port = line.rpartition(":")
        if host in ("127.0.0.1", "[::1]") and port.isdigit():
            return int(port)
    log(f"run {run_id} published its display outside loopback ({probe.stdout.strip()!r}) "
        "- refusing to record it")
    return None


def _mount_spec(source: Path, target: str) -> str:
    """A read-only `--mount` value. CSV, because that is what docker parses:
    a storage root containing a comma or a quote is quoted, not split."""
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="").writerow(
        ["type=bind", f"source={source}", f"target={target}", "readonly"])
    return buffer.getvalue()


def _is_regular_file(path: Path) -> bool:
    """True for a regular file that is not a symlink. Never raises."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _container_args(
    actor: str,
    run_id: str,
    run_dir: Path,
    limits: ExecutionLimits | None = None,
    secret_names: Sequence[str] = (),
    secrets_dir: Path | None = None,
    state_dir: Path | None = None,
    interactive_display: bool = False,
    host_network: bool = False,
    network: str | None = None,
    chrome_sandbox: bool = False,
) -> list[str]:
    """Build the `docker run` command for one run.

    EVERY container is hardened the way the login browser always was (security
    review M-02): all Linux capabilities dropped and no privilege escalation
    through setuid binaries. No Actor needs either -- they fetch pages, write
    files into their own mounts and, for some, drive a browser that runs with
    its own sandbox off inside the container -- and these two flags turn a
    compromised parser dependency into a far smaller problem. They are present
    whatever else is passed, so the argv diff between "no limits" and "limits"
    is still exactly the resource flags.

    `network` names the bridge to join (the executor passes ACTOR_NETWORK);
    None leaves Docker's default, which only callers building argv by hand use.

    limits=None emits exactly the argv this produced before resource control
    existed, which is what makes 'unlimited by default' a real guarantee
    rather than a claim: an operator who sets no limits gets the container
    they have always got. The same holds for secret_names=() and
    secrets_dir=None: a run with no per-run credential gets the old argv.

    `secret_names` are emitted as `-e NAME` with NO value, exactly as the
    platform-wide forwarded names are -- the difference is only WHERE the value
    comes from, and the value never appears here either way.

    `state_dir=None` likewise emits the old argv: the durable state mount is
    opt-in per Actor and, even then, only for a run that names a connected
    account.
    """
    args = [
        "docker",
        "run",
        "--rm",
        # -t gives the container ONE pty for stdout and stderr instead of two
        # separate pipes that docker demultiplexes and the worker re-merges.
        # Measured over 10 trials of an Actor interleaving 400 lines: without
        # it, 7/10 runs had lines SPLICED TOGETHER with the newline lost (e.g.
        # "38 err0 out") and 3/10 had lines out of order; with it, 0/10 on both.
        # The kernel serialises writes to a shared pty, so ordering is a
        # guarantee rather than a race.
        #
        # The cost, accepted deliberately: isatty() is now True inside the
        # container, so an Actor may choose to emit colour or progress-bar
        # output. In exchange, log.txt is the Actor's real narrative. The
        # platform never distinguished stdout from stderr anyway -- log.txt has
        # always been one merged file -- so nothing that was being used is lost.
        "-t",
        "--name",
        f"actor-{run_id}",
        "-v",
        f"{run_dir}:{ACTOR_STORAGE_DIR}",
        "-e",
        f"{ENV_INPUT_PATH}={DEFAULT_INPUT_PATH}",
        "-e",
        f"{ENV_OUTPUT_PATH}={DEFAULT_OUTPUT_PATH}",
        "-e",
        f"{ENV_RUN_ID}={run_id}",
    ]

    # THE RUN RECORD AND THE INPUT ARE READ-ONLY INSIDE THE CONTAINER. Both sit
    # in the directory mounted read-write above, and the registry reads run.json
    # back: a container that rewrote it could reset its own attempts, rename its
    # Actor, point the API's display proxy at another port, or -- before the
    # registry stopped trusting the field -- choose which host path the NEXT
    # attempt mounts. A read-only bind over each file makes the record the
    # platform's alone while the container lives: writes fail with EROFS, and a
    # rename or unlink over a mount point fails with EBUSY. The host still
    # replaces run.json atomically; a bind follows the inode, not the name.
    #
    # --mount, not -v: -v CREATES a missing source as a directory, and these
    # are only added for files that exist as regular files (never a link the
    # container planted between attempts), so a directory with neither -- a
    # probe, a test -- gets exactly the argv it always did.
    for name in (RUN_FILE, INPUT_FILE):
        if _is_regular_file(run_dir / name):
            args += ["--mount", _mount_spec(run_dir / name, f"{ACTOR_STORAGE_DIR}/{name}")]

    # Secrets are passed as "-e NAME" with NO value: the docker CLI reads it
    # from this process's environment. Writing "-e NAME=value" would put the
    # secret in the container's argv, where `ps` shows it to every user on the
    # machine. The names are logged; the values are never touched.
    forwarded = forwarded_env_names()
    for name in forwarded:
        args += ["-e", name]
    if forwarded:
        log(f"forwarding {len(forwarded)} environment variable(s) to the actor: {', '.join(forwarded)}")

    # Per-RUN credentials. Same "-e NAME" form and the same guarantee; the value
    # is supplied to the docker CLI through its own environment (see _execute's
    # `env` argument), so it is not in this argv and `ps` cannot show it.
    # De-duplicated against the forwarded names so a name is never passed twice.
    per_run = [n for n in secret_names if n not in set(forwarded)]
    for name in per_run:
        args += ["-e", name]
    if per_run:
        log(f"run {run_id} receives {len(per_run)} per-run credential(s): {', '.join(per_run)}")

    # The write-back mount, when this Actor refreshes its session. OUTSIDE the
    # run directory on purpose: storage/runs/<id>/ is what GET /runs/{id}/... is
    # served from, and a live session must never sit in the public artifact tree.
    if secrets_dir is not None:
        args += ["-v", f"{secrets_dir}:{ACTOR_SECRETS_DIR}"]
        args += ["-e", f"{ENV_SESSION_OUT}={ACTOR_SECRETS_DIR}/{SESSION_OUT_FILE}"]

    # The DURABLE mount: state this Actor keeps across runs, scoped to one
    # connected account. Unlike the two mounts above it is not deleted when the
    # run ends -- that is the entire point of it.
    #
    # ONE ACCOUNT'S DIRECTORY, NEVER A PARENT OF IT. The host path is built by
    # _persistent_state_dir from the Actor name and the account id, both
    # sanitised to a single path segment, so this mount can only ever expose
    # the state of the account the run is already acting as.
    if state_dir is not None:
        args += ["-v", f"{state_dir}:{ACTOR_STATE_DIR}"]
        args += ["-e", f"{ENV_STATE_DIR}={ACTOR_STATE_DIR}"]

    # THE VIEWABLE DISPLAY, for an Actor that declared one. Published to host
    # LOOPBACK on an EPHEMERAL port, exactly as the login browser's is: an
    # unguessable port is not an authorisation boundary, so the only way to it
    # is an authenticated, ownership-checked route in the API. Nothing about it
    # is reachable from off-host.
    #
    # Nothing else here changes. With the flag absent this block emits no
    # argument at all and the argv is byte-identical to what it has always been.
    if interactive_display:
        args += ["-p", f"127.0.0.1:0:{ACTOR_DISPLAY_PORT}"]

    # THE HOST NETWORK NAMESPACE, for an Actor that declared it needs one.
    #
    # It exists so a container can reach a service on the host's LOOPBACK -- a
    # browser the operator is running and using normally, which some sites
    # answer in full while refusing any browser this platform launches. The
    # alternative wirings are worse: binding that service to the docker bridge
    # or to 0.0.0.0 would publish an unauthenticated control interface to
    # everything that can reach the address, where host networking publishes
    # nothing at all.
    #
    # ActorProfile refuses to combine this with interactive_display, because a
    # published port has no meaning in this namespace. Opt-in: without it not
    # one argument changes.
    if host_network:
        args += ["--network", "host"]
    elif network:
        args += ["--network", network]

    # Match the host user so files written into the mounted run directory stay
    # readable and deletable by whoever started the run.
    if hasattr(os, "getuid"):
        args += ["--user", f"{os.getuid()}:{os.getgid()}"]

    args += ["--cap-drop", "ALL", "--security-opt", "no-new-privileges"]

    # CHROME'S OWN SANDBOX, for an Actor that declared chrome_sandbox: the
    # seccomp profile that lets Chrome create its namespaces, and the flag that
    # tells the browser to keep its sandbox on (shared/chrome_sandbox.py). It
    # replaces Docker's default seccomp profile for THIS container only; the
    # capabilities, no-new-privileges, user and network above are untouched.
    # Absent, not one argument changes.
    if chrome_sandbox:
        args += chrome_sandbox_args()

    # CPU and memory last, so they read as a distinct block in `ps` output and
    # in the test that asserts the unlimited default is byte-identical.
    if limits is not None:
        resource_args = limits.docker_args()
        if resource_args:
            args += resource_args
            log(f"run {run_id} limited to {limits.describe()}")

    args.append(image_tag(actor))
    return args


# One line of Actor output longer than this is cut. Generous: a JSON blob
# printed on one line is legitimate, a gigabyte without a newline is not.
MAX_LOG_LINE_CHARS = 64 * 1024
# A forwarded value shorter than this is not redacted: it is too likely to be
# an ordinary word or number, and replacing it everywhere would wreck the log
# while protecting nothing a credential would look like.
MIN_REDACTED_LENGTH = 8


def _redactions(secrets: Mapping[str, str]) -> list[str]:
    """Every value that must never reach a run's log.

    The per-run secrets, AND the operator's forwarded variables -- an API key
    passed to every Actor turned up in log.txt whenever a traceback printed the
    environment or a client's headers, and log.txt is served over HTTP.
    """
    values = [value for value in secrets.values() if value]
    for name in forwarded_env_names():
        value = os.environ.get(name, "")
        if len(value) >= MIN_REDACTED_LENGTH:
            values.append(value)
    # Longest first, so a value that contains another is replaced whole.
    return sorted(set(values), key=len, reverse=True)


def _execute(
    command: list[str],
    container_name: str,
    prefix: str = "",
    env: Mapping[str, str] | None = None,
    redact: Sequence[str] = (),
) -> tuple[int | None, list[str]]:
    """Run the container to completion, streaming its output. -> (exit code, logs).

    WAITS FOR AS LONG AS THE ACTOR TAKES. There is no deadline here by design;
    see the note at the top of this module.

    `prefix` tags the echoed lines. With several runs sharing one stderr,
    unprefixed output is an unreadable interleave of N narratives; the stored
    log.txt is per-run and never carries it.

    `env` is the CHILD's environment, passed to Popen and applied at fork/exec.
    It is how a per-run secret reaches the docker CLI without entering argv.
    IT MUST NOT BECOME os.environ[name] = value: this Worker is threaded three
    ways, so mutating the process environment races, and the failure mode is one
    user's session going into another user's container.

    `redact` are values to strip from every line BEFORE it is stored or echoed.
    log.txt is served over HTTP by GET /runs/{id}/logs, and while no Actor
    prints its session deliberately, a traceback through a large vendored
    collector could carry a cookie header. Cheap insurance on a public route.
    """
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=dict(env) if env is not None else None,
    )

    lines: list[str] = []
    # The Actor decides how much it prints, and every line used to be kept in
    # this list for the whole run -- in the ONE worker process every run
    # shares. Bounded now: a line is cut at MAX_LOG_LINE_CHARS, and past the
    # log limit lines are still echoed (an operator watching sees everything)
    # but no longer kept, with one note saying how many were dropped.
    budget = size_limit(MAX_LOG_BYTES_ENV, DEFAULT_MAX_LOG_BYTES)
    kept = 0
    dropped = 0

    def pump() -> None:
        nonlocal kept, dropped
        assert process.stdout is not None
        # readline(n) rather than iteration: one newline-free stream must not
        # become one unbounded string.
        while True:
            line = process.stdout.readline(MAX_LOG_LINE_CHARS + 1)
            if not line:
                break
            if len(line) > MAX_LOG_LINE_CHARS and not line.endswith("\n"):
                # Swallow the rest of this over-long line, a bounded chunk at a time.
                rest = line
                while rest and not rest.endswith("\n"):
                    rest = process.stdout.readline(MAX_LOG_LINE_CHARS + 1)
                line = line[:MAX_LOG_LINE_CHARS] + " [line truncated]"
            # A pty terminates lines with \r\n. text=True already folds that to
            # \n, but strip defensively so a bare \r can never survive into the
            # stored log.
            line = line.rstrip("\r\n")
            # Redact BEFORE the line is kept or echoed, so a secret never
            # reaches log.txt, the terminal, or the list handed to the caller.
            for secret in redact:
                if secret and secret in line:
                    line = line.replace(secret, "[redacted]")
            if kept + len(line) + 1 <= budget:
                lines.append(line)
                kept += len(line) + 1
            else:
                dropped += 1
            # The prefix is display only -- `lines` is what reaches log.txt.
            write_line(f"  {prefix}| {line}")

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()

    exit_code = process.wait()

    reader.join(timeout=5)
    if dropped:
        lines.append(f"{log_truncation_note(budget)} {dropped} further line(s) not kept")
    return exit_code, lines


# The output.json field an Actor sets when it found its session dead. Generic
# on purpose: the platform names no site, so the Actor decides what "dead"
# looks like and says so here. "expired" is the confident reason and flips the
# account at once; any other non-empty string needs two runs in a row.
SESSION_FAILURE_FIELD = "session_failure"


def session_failure(registry: RunRegistry, run_id: str) -> str | None:
    """The reason the Actor gave for a dead session, or None. Never raises."""
    try:
        output = registry.get_output(run_id)
    except Exception:  # noqa: BLE001 - no output is simply no verdict
        return None
    reason = output.get(SESSION_FAILURE_FIELD) if isinstance(output, dict) else None
    if not isinstance(reason, str) or not reason.strip():
        return None
    return reason.strip()[:64]


def _produced_items(registry: RunRegistry, run_id: str) -> bool:
    """Whether this run streamed any dataset items before it ended.

    The platform's ONE Actor-agnostic measure of "there is data here". It does
    not know what a post is and must not learn: DATASET_FILE is the generic
    as-it-goes channel, so a line in it means the Actor produced something real,
    whatever the something was. Never raises -- a failure to read the dataset
    must not change how the run itself is classified.
    """
    try:
        return bool(registry.get_items(run_id, limit=1).get("items"))
    except Exception:  # noqa: BLE001 - absent or unreadable is simply "no items"
        return False


# Where an Actor may state how long to wait before retrying.
#
# Three places, because three shapes already exist in the wild: a top-level
# field is the obvious one; `retry_after` is what a passthrough of the header's
# own name looks like; and `reliability` is the block the platform's most
# instrumented Actor already publishes its error code and retryability in, so
# the delay belongs beside them rather than in a fourth place.
#
# The VALUE may be a number or the raw header string -- parse_retry_after takes
# either, so an Actor that did its own arithmetic and one that passed the header
# straight through both arrive at the same answer.
_RETRY_AFTER_FIELDS = ("retry_after_seconds", "retry_after")


def _declared_retry_after(output: dict[str, Any]) -> float | None:
    """How long the Actor said to wait, in seconds, or None. Never raises."""
    for source in (output, output.get("reliability")):
        if not isinstance(source, dict):
            continue
        for field in _RETRY_AFTER_FIELDS:
            if field in source:
                parsed = parse_retry_after(source[field])
                if parsed is not None:
                    return parsed
    return None


def _actor_declared_error(
    registry: RunRegistry, run_id: str
) -> tuple[ErrorCategory, str, str, float | None] | None:
    """The Actor's OWN verdict from its output document, mapped to a category.

    An Actor sees what the platform cannot: that the page was private, the jar
    hit a login wall, the upstream answered 429. It already says so in the
    `status` field of its result (and, for a dead session, in `session_failure`).
    Reading it here is what turns "exited with code 1" into something a caller
    can act on, WITHOUT the platform learning any site's vocabulary -- the
    mapping is on generic words, in shared/errors.py.

    Returns (category, detail, message, retry_after) or None when the Actor
    said nothing recognisable. Never raises.
    """
    try:
        output = registry.get_output(run_id)
    except Exception:  # noqa: BLE001 - no output is simply no verdict
        return None
    if not isinstance(output, dict):
        return None

    # `status` first, then the generic session verdict. A dead session is an
    # AUTH_ERROR even when the Actor's status word was something vaguer,
    # because reconnecting is the actual fix and the caller needs to be told.
    mapped = category_for_actor_status(output.get("status"))
    if mapped is None:
        reason = output.get(SESSION_FAILURE_FIELD)
        if isinstance(reason, str) and reason.strip():
            mapped = category_for_actor_status(reason) or (
                ErrorCategory.AUTH_ERROR,
                "SESSION_FAILED",
            )
    if mapped is None:
        return None

    category, detail = mapped
    message = output.get("error")
    if not isinstance(message, str) or not message.strip():
        message = f"the actor reported {detail.lower().replace('_', ' ')}"
    return category, detail, message, _declared_retry_after(output)


def _classify(
    registry: RunRegistry,
    run_id: str,
    *,
    exit_code: int | None,
    actor: str | None = None,
    attempt: int | None = None,
    log_tail: str = "",
) -> tuple[RunStatus, str | None, RunError | None, bool]:
    """Decide a finished container's outcome.

    This is execution POLICY and so stays in the worker; the registry only
    supplies the artifact. An exit code of 0 with no readable output is still
    a failed run -- the Actor claimed success and produced nothing.

    -> (status, message, structured error, partial)

    The status half is unchanged from before, deliberately: SUCCEEDED / FAILED /
    PLATFORM_ERROR mean exactly what they always meant, and every existing
    reader keeps working. What is new is the second half -- WHICH KIND of
    failure, and whether the run left usable data behind.

    The order matters. A container-level failure is read FIRST, because the
    Actor cannot have reported anything if it never ran: an image that is
    missing, a daemon that is down, an entrypoint that is not executable. Only a
    plain non-zero exit -- the Actor ran and said it failed -- is then refined
    by the Actor's own verdict.
    """
    partial = _produced_items(registry, run_id)

    if exit_code == 0:
        try:
            # Read to CHECK, not to keep: an exit code of 0 with no readable
            # output is still a failed run, but the value belongs in output.json.
            registry.get_output(run_id)
        except ArtifactError as exc:
            # The Actor claimed success and produced nothing readable. That is
            # the Actor's contract broken, not the platform's -- ACTOR_ERROR,
            # and not retryable, because it will do it again.
            error = RunError(
                code=ErrorCategory.ACTOR_ERROR,
                message=str(exc),
                stage="output",
                actor=actor,
                attempt=attempt,
                detail="MALFORMED_OUTPUT",
            )
            return RunStatus.FAILED, error.message, error, partial
        # NEVER partial on success. `partial` means "failed, but left data worth
        # reading" (Run.partial, api/schemas.py), and the console shows it as a
        # PARTIAL badge with "This run did not finish". Every Actor that streams
        # items as it goes produced items on its SUCCESSFUL runs too, so passing
        # `partial` through here flagged each of those as unfinished.
        return RunStatus.SUCCEEDED, None, None, False

    category, detail, message = classify_container_exit(exit_code, log_tail)

    # The container itself failed, so the Actor never got to have an opinion.
    # PLATFORM_ERROR is the right STATUS for both of these categories: a missing
    # image and an unreachable daemon are equally "we could not run it", and the
    # category is what separates the permanent one from the retryable one.
    if category is not ErrorCategory.ACTOR_ERROR:
        error = RunError(
            code=category,
            message=message,
            stage="container",
            actor=actor,
            attempt=attempt,
            detail=detail,
        )
        return RunStatus.PLATFORM_ERROR, error.message, error, partial

    # The Actor ran and exited non-zero. Let it say why, if it did.
    retry_after: float | None = None
    declared = _actor_declared_error(registry, run_id)
    if declared is not None:
        category, detail, message, retry_after = declared

    error = RunError(
        code=category,
        message=message,
        stage="actor",
        actor=actor,
        attempt=attempt,
        detail=detail,
        # Only ever what the UPSTREAM said. The platform never invents a delay:
        # a guessed number presented in the same field as a real Retry-After
        # would be indistinguishable from one, and a caller acting on it would
        # be obeying us rather than the site.
        retry_after_seconds=retry_after,
    )
    return RunStatus.FAILED, error.message, error, partial


def _abort(registry: RunRegistry, run_id: str, reason: str) -> None:
    """Close out a run the platform could not finish.

    Without this, an unexpected failure would leave the record claiming RUNNING
    for ever -- a lie on disk that the registry would faithfully report.
    """
    try:
        if not registry.get(run_id).is_terminal:
            registry.complete(run_id, status=RunStatus.PLATFORM_ERROR, error=reason)
    except RegistryError:
        # Already unrecoverable; never mask the original failure with this one.
        pass


def name_exists(name: str) -> bool:
    """True while anything -- running or dead -- still holds this container NAME.

    Takes the full name rather than a run id so callers that do not name their
    containers `actor-<run_id>` can reuse it. The login-browser containers are
    the reason: they are not runs, must not be given a run directory, and yet
    need exactly this check and the wait below.
    """
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def container_exists(run_id: str) -> bool:
    """True while anything -- running or dead -- still holds this run's name."""
    return name_exists(f"actor-{run_id}")


def wait_for_name_release(name: str) -> bool:
    """Block until `name` is free, or NAME_RELEASE_TIMEOUT_S passes.

    Returns True if the name came free. See kill_container() for why this is
    insurance rather than a fix for anything observed: measured on docker 29.7.2
    the name was already free every time (6/6 trials, 0ms of waiting), so the
    loop normally costs one `docker ps`.
    """
    deadline = time.monotonic() + NAME_RELEASE_TIMEOUT_S
    while name_exists(name):
        if time.monotonic() >= deadline:
            return False
        time.sleep(NAME_POLL_INTERVAL_S)
    return True


def kill_container(run_id: str) -> bool:
    """Stop any container still running for `run_id`, and wait for its NAME.

    A Worker killed mid-run leaves its container alive: `docker run` is a child
    process, but the CONTAINER is not. Without this, reclaiming the run starts a
    second container writing the same mounted run directory as the first.
    Returns False when there was nothing to kill, which is the normal case.

    The wait afterwards is INSURANCE, not a fix for anything observed here.
    `docker rm --rm` is documented as removing the container on exit but not as
    doing so before `docker kill` returns, and a name still held when the retry
    starts means `name "actor-<id>" is already in use` -- the run fails as
    PLATFORM_ERROR instead of being retried. MEASURED on docker 29.7.2: `docker
    kill` took ~330ms and the name was already free every time (6/6 trials, 0ms
    of extra waiting), so on this version the loop never spins. It costs one
    `docker ps` when that holds and covers the case where it does not. Giving
    up after NAME_RELEASE_TIMEOUT_S is deliberate: a run that then fails loudly
    on the collision beats one that quietly starts a second container.
    """
    result = subprocess.run(
        ["docker", "kill", f"actor-{run_id}"], capture_output=True, text=True
    )
    if result.returncode != 0:
        return False

    if not wait_for_name_release(f"actor-{run_id}"):
        log(f"container actor-{run_id} was killed but its name is still taken")

    return True


def _preflight(actor: str, *, force_build: bool) -> list[str]:
    """Everything that must be true before a container can start.

    Returns lines for the top of the run's own log -- today only the warning
    that the run is going ahead on an out-of-date image (see ensure_image).
    """
    actor_dir(actor)
    require_docker()
    note = ensure_image(actor, force_build=force_build)
    return [note] if note else []


def _session_scratch(run_id: str) -> Path:
    """A worker-private directory for one run's credential exchange.

    0700, and deliberately NOT under storage/runs/: that tree is what the API
    serves artifacts from. Created per run and removed in a finally, so a live
    session never outlives the container that used it.
    """
    root = Path(os.environ.get("ACTOR_PLATFORM_SESSION_DIR") or (Path.home() / ".actor-platform" / "sessions"))
    directory = root / run_id
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


MAX_HANDOUT_BYTES = 4 * 1024 * 1024


def _read_handout(scratch: Path, run_id: str) -> str | None:
    """The refreshed session the Actor handed back, or None.

    The scratch directory is mounted into the container, so session.json is
    whatever the container left there -- possibly a link to a host file, which
    would then be stored as this account's "session" and handed to its next
    run. Read only as a regular file; anything else is refused and logged.
    """
    try:
        # Bounded: a session is a cookie jar of a few KB, and the Actor wrote it.
        return safe_fs.read_text(scratch / SESSION_OUT_FILE, max_bytes=MAX_HANDOUT_BYTES)
    except FileNotFoundError:
        return None  # the Actor did not refresh it; the normal case
    except OSError as exc:
        log(f"run {run_id}: could not read the refreshed session: {exc}")
        return None


def _safe_segment(value: str) -> str:
    """One path segment, whatever was passed.

    An Actor name and an account id both reach this from outside the worker, so
    neither is interpolated into a path raw: `..` must not be able to walk the
    mount up to a parent directory holding every account's state.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or "").strip())
    cleaned = cleaned.strip(".") or "unknown"
    return cleaned[:128]


def _persistent_state_dir(actor: str, account_id: str) -> Path:
    """The durable, per-account state directory for one Actor. 0700.

    OUTSIDE storage/, for the same reason _session_scratch is: this holds
    authentication material -- a browser profile carries the cookies and local
    storage of a logged-in account -- and storage/runs/ is the tree the API
    serves. It sits beside the session scratch instead, under the worker's own
    private root, and ACTOR_PLATFORM_STATE_DIR moves it for a deployment that
    keeps such things elsewhere (a mounted volume, say).

    KEYED BY ACTOR **AND** ACCOUNT. Sharing one directory between accounts
    would hand one user's logged-in browser profile to another user's run,
    which is the worst failure this whole path could have.
    """
    # One definition of where this lives, shared with the API, which removes
    # it when the account is disconnected or its user deleted.
    directory = state_root() / _safe_segment(actor) / _safe_segment(account_id)
    directory.mkdir(parents=True, exist_ok=True)
    # 0700 on the leaf AND on the per-Actor parent: another user on the host
    # must not be able to list which accounts are connected, let alone read a
    # profile.
    for path in (directory, directory.parent, directory.parent.parent):
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass  # a pre-existing directory we do not own; the leaf is what matters
    return directory


def _state_dir_for(run: Run, profile: "ActorProfile | None") -> Path | None:
    """The durable mount for one run, or None. -> a per-account directory.

    Two conditions, and the second is the security one: the Actor must ask for
    persistent state, AND the run must name a connected account to scope it to.
    A run with no account gets NO mount rather than a shared directory, because
    state accumulated while acting as one account must never be handed to
    another -- and an unscoped directory is exactly how that would happen.
    """
    if not (profile and profile.persistent_state and run.social_account_id):
        return None
    return _persistent_state_dir(run.actor, run.social_account_id)


def _run_prepared(
    run: Run,
    *,
    registry: RunRegistry,
    limits: ExecutionLimits,
    resolver: "SecretResolver | None" = None,
    profile: "ActorProfile | None" = None,
    platform_notes: Sequence[str] = (),
) -> Run:
    """Execute a PENDING run whose image is ready. The one place Docker runs.

    `platform_notes` are the platform's own lines, put at the TOP of the run's
    stored log, ahead of the Actor's -- so a warning about this run (e.g. it ran
    on an out-of-date image) is where whoever reads the run will see it.

    Both entry points below funnel through here, so the queued path and the
    direct path cannot drift apart.

    ORDERING GUARANTEE: mark_running is recorded BEFORE the container exists.
    So the dangerous window -- a container alive that the registry knows
    nothing about -- does not occur, and a Worker that dies mid-run always
    leaves a record saying RUNNING for recovery to find. Do not reorder these.
    """
    log(f"run {run.run_id} | actor={run.actor} | storage={run.run_dir}")

    # CLAIM THE RUN FIRST, and deliberately OUTSIDE the guard below.
    #
    # PENDING -> RUNNING is the registry's own claim, and its transition guard
    # is what settles which of two simultaneous deliveries owns the run. The
    # loser must fail HARMLESSLY. Inside the try below it would instead be
    # caught as an unexpected failure and _abort would mark the WINNER's live
    # run PLATFORM_ERROR -- one duplicate delivery killing a healthy run.

    # THE SESSION LEASE, taken BEFORE the run is claimed, for the same reason:
    # a run that loses it here has changed NOTHING, so its job can go straight
    # back to the queue unrun. Taken after mark_running it would already be
    # RUNNING and would have to be walked back.
    if resolver is not None and not resolver.acquire_session(run):
        raise SessionBusy(
            f"another run is already using the session for run {run.run_id}"
        )

    try:
        registry.mark_running(run.run_id)
        event("worker", "job_running", run_id=run.run_id, actor=run.actor)
    except InvalidTransition as exc:
        if resolver is not None:
            resolver.release_session(run)
        raise RunAlreadyClaimed(
            f"run {run.run_id} was claimed by another executor: {exc}"
        ) from exc

    # Declared before the try so the finally can always read them, whatever
    # failed and however early.
    secrets: Mapping[str, str] = {}
    scratch: Path | None = None
    refreshed: str | None = None

    try:
        # THE LIMITS THIS RUN GETS, resolved here whatever the API accepted:
        # the record may have been written by something other than the API (a
        # requeue, an import, a container rewriting run.json in filesystem
        # mode), so the clamp to the Actor's maximum happens again at the last
        # moment it can. Recorded on the run so the console shows what was
        # actually enforced, not what was asked for.
        effective = (profile.limits.resolve(RunOptions.from_run(run), ceiling=limits)
                     if profile is not None else limits)
        if (effective.timeout_s, effective.memory_mb, effective.cpus) != (
                run.timeout_s, run.memory_mb, run.cpus):
            try:
                registry.update(run.run_id, timeout_s=effective.timeout_s,
                                memory_mb=effective.memory_mb, cpus=effective.cpus)
            except RegistryError as exc:
                log(f"run {run.run_id}: could not record its limits: {exc}")

        # WHAT THIS RUN IS GIVEN. Resolved before the container starts, so a
        # missing or undecryptable credential fails the run cleanly rather than
        # half way in. A resolver failure is a PLATFORM error (see _abort
        # below), not the Actor's, and specifically not "your session expired".
        #
        # INSIDE the try, and that is load-bearing: the lease is already held by
        # now, so a failure out here would strand it AND leave the run RUNNING
        # for ever. The finally releases it on every path.
        if resolver is not None and profile is not None:
            secrets = resolver.for_run(run, profile)

        # THE VIEWER'S TOKEN, for an Actor that exports a display. Passed like
        # any per-run credential -- by name in argv, value in the child's
        # environment -- and derived rather than stored (shared/display_token).
        if profile is not None and profile.interactive_display:
            token = display_token(run.run_id)
            if token:
                secrets = {**secrets, ENV_DISPLAY_TOKEN: token}
            else:
                log(f"run {run.run_id}: no ACTOR_PLATFORM_SECRET_KEY, so its viewer will "
                    "refuse every connection")

        # Only Actors that declare session_out get a credential mount at all.
        scratch = _session_scratch(run.run_id) if (profile and profile.session_out) else None

        # And only those that declare persistent_state get the durable one --
        # and only when the run names a connected account to scope it to. A run
        # with no account gets NO mount rather than a shared directory: state
        # accumulated while acting as one account must never be handed to
        # another, and an unscoped directory is exactly how that happens.
        state = _state_dir_for(run, profile)
        if state is not None:
            # The PATH and the account id, never the contents. This directory
            # holds a logged-in browser profile.
            log(f"run {run.run_id} keeps persistent state for account "
                f"{run.social_account_id} at {state}")

        # A daemon watcher stops the container if the run is cancelled WHILE it
        # runs. The API only records CANCELLED in the registry (it must not touch
        # Docker); the worker owns the container, so the worker is what turns that
        # intent into a `docker kill`. The watcher kills only AFTER seeing
        # CANCELLED, so by the time _execute returns the state is already terminal.
        stop_watch = threading.Event()

        # The SAME thread also publishes the display port, for an Actor that has
        # one. It is already polling at exactly the cadence that needs -- the
        # port cannot be read until the container is up, and there is no event
        # to wait on -- and a second thread would duplicate the loop, the
        # registry error handling and the shutdown handshake for one integer.
        interactive = bool(profile and profile.interactive_display)
        host_net = bool(profile and profile.host_network)
        published = not interactive
        network = None if host_net else ensure_actor_network()

        # THE SAME THREAD ENFORCES THE WALL-CLOCK LIMIT, for the same reason it
        # publishes the port: it is already awake every half second. Measured
        # from when the container starts, not from submission -- time spent
        # queued behind other runs is not this run's fault.
        timed_out = threading.Event()
        container_started = time.monotonic()

        def _watch_for_cancel() -> None:
            nonlocal published
            while not stop_watch.wait(CANCEL_POLL_INTERVAL_S):
                try:
                    if (effective.timeout_s and not timed_out.is_set()
                            and time.monotonic() - container_started > effective.timeout_s):
                        timed_out.set()
                        log(f"run {run.run_id} reached its {effective.timeout_s // 60}-minute "
                            "limit - stopping its container")
                        event("worker", "job_timed_out", run_id=run.run_id, actor=run.actor,
                              timeout_s=effective.timeout_s)
                        stop_container(run.run_id)
                        return
                    if registry.get(run.run_id).status is RunStatus.CANCELLED:
                        log(f"run {run.run_id} was cancelled - stopping its container")
                        event("worker", "job_cancel_signalled", run_id=run.run_id, actor=run.actor)
                        kill_container(run.run_id)
                        return
                    if not published:
                        port = display_port(run.run_id)
                        if port is not None:
                            registry.update(run.run_id, display_port=port)
                            published = True
                            log(f"run {run.run_id} display is viewable (host port {port})")
                            event("worker", "run_display_published",
                                  run_id=run.run_id, actor=run.actor)
                except RegistryError:
                    continue  # a transient read failure is not a reason to stop watching

        watcher = threading.Thread(target=_watch_for_cancel, name=f"cancel-{run.run_id[-6:]}",
                                   daemon=True)

        # PEAK MEMORY, sampled on its own thread because `docker stats` takes a
        # second or so and the watcher above must stay responsive to cancels.
        # A run shorter than the first sample records nothing, which is honest.
        peak: dict[str, int | None] = {"mb": None}

        def _sample_memory() -> None:
            delay = 2.0
            while not stop_watch.wait(delay):
                delay = MEMORY_SAMPLE_INTERVAL_S
                try:
                    used = container_memory_mb(run.run_id)
                except Exception:  # noqa: BLE001 - a missed sample is not an error
                    continue
                if used is not None and (peak["mb"] is None or used > peak["mb"]):
                    peak["mb"] = used

        sampler = threading.Thread(target=_sample_memory, name=f"mem-{run.run_id[-6:]}",
                                   daemon=True)

        # ITEMS: a requeued run re-executes in this same directory, so the
        # previous attempt's lines go first or they would be stored twice. Then,
        # for a backend that keeps items outside the file, tail it while the
        # container runs so results are reachable before the run ends.
        ingestor = None
        try:
            registry.reset_items(run.run_id)
            if registry.STORES_ITEMS:
                ingestor = DatasetIngestor(registry, run.run_id, log=log).start()
        except Exception as exc:  # noqa: BLE001 - items never fail a run
            log(f"run {run.run_id}: item ingestion unavailable: {type(exc).__name__}: {exc}")

        try:
            watcher.start()
            sampler.start()
            command = _container_args(
                # The registry's path for this id, never the record's run_dir
                # field: the record is the run's own, and the mount is the one
                # thing it must never get to choose.
                run.actor, run.run_id, registry.run_dir(run.run_id), effective,
                secret_names=list(secrets),
                secrets_dir=scratch,
                state_dir=state,
                interactive_display=interactive,
                host_network=host_net,
                network=network,
                chrome_sandbox=bool(profile and profile.chrome_sandbox),
            )
            exit_code, lines = _execute(
                command, f"actor-{run.run_id}", prefix=f"{run.run_id[-6:]} ",
                # The child's environment, NOT this process's. See _execute.
                env={**os.environ, **secrets} if secrets else None,
                redact=_redactions(secrets),
            )
        finally:
            stop_watch.set()
            watcher.join(timeout=5)
            sampler.join(timeout=5)
            # BEFORE the run is marked terminal, and on every path -- a crashed
            # or cancelled run's partial items are real data too.
            if ingestor is not None:
                try:
                    ingestor.stop_and_drain()
                except Exception as exc:  # noqa: BLE001
                    log(f"run {run.run_id}: could not finish storing items: {exc}")

        registry.write_logs(run.run_id, [*(platform_notes or ()), *lines])

        # Read the refreshed session BEFORE the finally removes the directory.
        # Held as a local rather than acted on here: this function owns Docker
        # and the registry, and knows nothing about encryption or accounts.
        if scratch is not None:
            refreshed = _read_handout(scratch, run.run_id)

        # CANCELLATION WON: the watcher (or a cancel that landed as the actor
        # finished) already moved the run to CANCELLED. Leave it -- do not overwrite
        # a terminal state with a completion result.
        current = registry.get(run.run_id)
        if current.status is RunStatus.CANCELLED:
            event("worker", "job_cancelled", run_id=run.run_id, actor=run.actor,
                  exit_code=exit_code, duration_seconds=current.duration_seconds)
            return current

        # The LAST few log lines are the haystack for container-level failures:
        # docker writes "Unable to find image ..." and "Cannot connect to the
        # Docker daemon" onto the same stream the Actor's own output rides, and
        # a missing image produces nothing else at all. Bounded to the tail so a
        # forty-minute scrape's log is never scanned in full.
        log_tail = "\n".join(lines[-40:])
        status, error, error_info, partial = _classify(
            registry,
            run.run_id,
            exit_code=exit_code,
            actor=run.actor,
            attempt=run.attempts or None,
            log_tail=log_tail,
        )
        if timed_out.is_set():
            # Said plainly, with the way out, so it never reads as a crash. The
            # stop it took shows up as a SIGTERM/SIGKILL exit, which on its own
            # would be classified as the container being killed -- but this run
            # hit ITS OWN configured limit, which is what TIMEOUT means.
            ceiling = profile.limits.max_timeout_minutes if profile is not None else None
            status = RunStatus.FAILED
            # timed_out is only ever set by the watcher when a limit exists.
            limit_min = (effective.timeout_s or 0) // 60
            error = (f"timed out after {limit_min} min (this run's limit"
                     + (f"; a run of this actor may ask for up to {ceiling} min" if ceiling else "")
                     + ")")
            error_info = RunError(ErrorCategory.TIMEOUT, error, retryable=False,
                                  stage="run", actor=run.actor,
                                  attempt=run.attempts or None, detail="RUN_TIMEOUT")
        elif (exit_code == 137 and effective.memory_mb and peak["mb"]
              and peak["mb"] >= 0.9 * effective.memory_mb):
            # Docker's own OOMKilled flag is not read here, but a SIGKILL with
            # the measured peak at the limit is the same finding.
            error = (f"killed: ran out of memory (peak {peak['mb']} MB of a "
                     f"{effective.memory_mb} MB limit)")
            error_info = RunError(ErrorCategory.PLATFORM_ERROR, error,
                                  stage=error_info.stage if error_info else "run",
                                  actor=run.actor, attempt=run.attempts or None,
                                  detail=CONTAINER_OOM)
        if error_info is not None:
            event("worker", "job_failed", run_id=run.run_id, actor=run.actor,
                  severity="error", stage=error_info.stage,
                  error_code=error_info.code.value, error_detail=error_info.detail,
                  retryable=error_info.retryable, partial=partial,
                  attempt=run.attempts or None)
        try:
            completed = registry.complete(
                run.run_id,
                status=status,
                exit_code=exit_code,
                error=error,
                error_info=error_info.to_dict() if error_info is not None else None,
                partial=partial,
                peak_memory_mb=peak["mb"],
            )
        except InvalidTransition:
            # The narrow tie: a cancel committed between our get() above and this
            # complete(). cancel() holds the run lock for its whole check-and-set,
            # so it either fully won (state is CANCELLED) or fully lost; here it
            # won. Honour it rather than forcing the completion.
            completed = registry.get(run.run_id)
            event("worker", "job_cancelled", run_id=run.run_id, actor=run.actor,
                  exit_code=exit_code, duration_seconds=completed.duration_seconds)
            return completed
        event("worker", "job_completed", run_id=run.run_id, actor=run.actor,
              status=completed.status.value, exit_code=exit_code,
              duration_seconds=completed.duration_seconds)

        # THE ACTOR FOUND ITS SESSION DEAD. Recorded against the account
        # (debounced by the resolver), and the jar it ended with is NOT stored:
        # absorbing it would both keep a dead session and, being a positive
        # check, reset the very failure counter just incremented.
        failure = session_failure(registry, run.run_id)
        if failure and resolver is not None:
            refreshed = None
            try:
                resolver.report_auth_failure(completed, failure)
            except Exception as exc:  # noqa: BLE001 - never fails the run
                log(f"run {run.run_id}: could not record the session failure: {exc}")

        # DELIVERY IS AFTER COMPLETION, and outside the run's own success. The
        # data is already recorded and reachable through the API, so an
        # unreachable mail server is a missing convenience rather than a lost
        # result -- reporting a forty-minute scrape as failed because of it
        # would be absurd. Never raises; see delivery/mailer.
        if completed.succeeded and resolver is not None:
            try:
                artifacts = [
                    registry.artifact_path(run.run_id, a["name"])
                    for a in registry.list_artifacts(run.run_id)
                ]
                resolver.deliver_report(completed, artifacts)
            except Exception as exc:  # noqa: BLE001
                log(f"run {run.run_id}: could not deliver the report: {exc}")

        return completed
    except BaseException as exc:
        _abort(registry, run.run_id, f"{type(exc).__name__}: {exc}")
        if isinstance(exc, Exception):
            raise WorkerError(f"run {run.run_id} aborted: {type(exc).__name__}: {exc}") from exc
        raise  # Ctrl-C and friends stay themselves
    finally:
        # The lease goes back whatever happened, including on a crash path.
        if resolver is not None:
            resolver.release_session(run)
        # A site rotates its session cookies mid-run, so the jar the Actor ends
        # with is newer than the one it began with. Storing it is what stops
        # every session slowly decaying to death -- which presents as the site
        # blocking you rather than as a bug. Never fatal: the run's own result
        # stands whatever happens here.
        if refreshed and resolver is not None:
            try:
                resolver.absorb_refreshed(run, refreshed)
            except Exception as exc:  # noqa: BLE001
                log(f"run {run.run_id}: could not store the refreshed session: {exc}")
        # The credential directory goes, whatever happened. A live session must
        # not outlive the container that used it.
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


def run_actor(
    actor: str,
    payload: dict[str, Any],
    *,
    force_build: bool = False,
    limits: ExecutionLimits | None = None,
    registry: RunRegistry | None = None,
) -> Run:
    """Create a run and execute it immediately. The direct/CLI path.

    Pre-flight happens BEFORE the run is created, so a platform-start failure
    still leaves no record at all: the run never started.
    """
    registry = registry if registry is not None else RunRegistry()
    limits = limits if limits is not None else ExecutionLimits()

    notes = _preflight(actor, force_build=force_build)
    run = registry.create(actor, payload)
    return _run_prepared(run, registry=registry, limits=limits, platform_notes=notes)


def _input_rejection(registry: RunRegistry, run: Run) -> str | None:
    """Why this run's stored input is not valid for its Actor, or None.

    An Actor the catalog cannot find, or whose schema is broken, is left to
    _preflight, which already reports that as the platform's failure.
    """
    try:
        actor = catalog.find_actor(run.actor)
    except catalog.CatalogError:
        return None
    payload: Any = run.input
    if not payload:
        # Records written before the input was copied into them keep it only
        # in input.json -- the reconciler reads it the same way.
        try:
            payload = registry.get_input(run.run_id)
        except RegistryError:
            payload = {}
    try:
        catalog.validate_input(actor, payload if payload is not None else {})
    except catalog.InvalidActorInput as exc:
        return str(exc)[:500]
    except catalog.InvalidActorSchema:
        return None
    return None


def execute_run(
    run_id: str,
    *,
    registry: RunRegistry | None = None,
    force_build: bool = False,
    limits: ExecutionLimits | None = None,
    resolver: "SecretResolver | None" = None,
    profile: "ActorProfile | None" = None,
) -> Run:
    """Execute a run that ALREADY exists as PENDING. The queued path.

    The API creates the run and returns its id long before this is called, so
    unlike run_actor there is a record to answer for: a pre-flight failure here
    is a PLATFORM_ERROR against this run, not a refusal to create one.
    """
    registry = registry if registry is not None else RunRegistry()
    limits = limits if limits is not None else ExecutionLimits()
    run = registry.get(run_id)

    if run.status is not RunStatus.PENDING:
        raise WorkerError(f"run {run_id} is {run.status.value}, not PENDING - refusing to re-run it")

    # THE WORKER CHECKS THE INPUT AGAIN. The API validated it at submission,
    # but the queue and the record are separate stores the API does not
    # control once it has returned, and an Actor's schema can tighten between
    # submission and execution. One check, the catalog's, so the two
    # boundaries cannot disagree about what is valid.
    rejection = _input_rejection(registry, run)
    if rejection is not None:
        log(f"run {run_id}: input refused by the worker: {rejection}")
        event("worker", "input_rejected", run_id=run_id, actor=run.actor)
        registry.complete(run_id, status=RunStatus.FAILED,
                          error=f"input refused by the worker: {rejection}")
        return registry.get(run_id)

    try:
        notes = _preflight(run.actor, force_build=force_build)
    except BaseException as exc:
        _abort(registry, run_id, f"{type(exc).__name__}: {exc}")
        raise

    return _run_prepared(
        run, registry=registry, limits=limits, resolver=resolver, profile=profile,
        platform_notes=notes,
    )


# --- cli -------------------------------------------------------------------


def load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.input_file:
        raw = Path(args.input_file).read_text(encoding="utf-8")
        source = args.input_file
    else:
        raw = args.input or "{}"
        source = "--input"

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkerError(f"input from {source} is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise WorkerError(f"input from {source} must be a JSON object")

    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="worker",
        description="Run an Actor in its own Docker container.",
    )
    parser.add_argument("actor", help="actor name, i.e. a directory under actors/")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--input", help="input as an inline JSON object")
    source.add_argument("--input-file", help="path to a JSON file holding the input")
    parser.add_argument(
        "--build",
        action="store_true",
        help="rebuild the image even if it already exists",
    )
    parser.add_argument("--cpus", type=float, help="cpu limit for the container, e.g. 0.5")
    parser.add_argument("--memory", help="memory limit for the container, e.g. 512m")
    return parser.parse_args(argv)


def _cli_view(run: Run, registry: RunRegistry) -> dict[str, Any]:
    """The CLI's answer: the record, plus the artifact the record points at.

    A person running one Actor from a terminal wants to see what it produced,
    and the record no longer carries it. Composing the two HERE keeps that
    convenience without putting the output back into storage -- the same split
    the API makes between RunDetail and GET /runs/{id}/output.
    """
    view = run.to_dict()
    try:
        view["output"] = registry.get_output(run.run_id)
    except RegistryError:
        view["output"] = None
    return view


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    registry = RunRegistry()

    try:
        payload = load_payload(args)
        result = run_actor(
            args.actor,
            payload,
            force_build=args.build,
            limits=ExecutionLimits(cpus=args.cpus, memory=args.memory),
            registry=registry,
        )
    except (WorkerError, RegistryError, LimitError) as exc:
        # The platform could not produce a run at all.
        print(f"[worker] ERROR: {exc}", file=sys.stderr, flush=True)
        return EXIT_PLATFORM_ERROR

    log(f"run {result.run_id} {result.status.value} in {result.duration_seconds}s")
    print(json.dumps(_cli_view(result, registry), indent=2, ensure_ascii=False))
    return worker_exit_code(result.status)


if __name__ == "__main__":
    sys.exit(main())
