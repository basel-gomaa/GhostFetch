"""HTTP models for the public API.

Deliberately separate from shared.models.Run. The internal record carries things
a client has no business seeing -- run_dir is a path on the platform's disk --
and it will keep growing as the engine does. Mapping through explicit models
means the wire format changes only when someone changes it on purpose.

That separation is structural, not a convention: RunDetail has no run_dir field,
so no future edit to Run can leak one. The previous version stripped it with a
pop(), which works right up until someone forgets.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from shared.models import Run


class Violation(BaseModel):
    """One way a submitted input failed the Actor's schema."""

    path: str = Field(description="where in the document, or '(root)'")
    message: str


class ErrorBody(BaseModel):
    """Every failure the API reports, in one shape."""

    error: str = Field(description="short, stable machine-readable reason")
    detail: str = Field(default="", description="human-readable explanation")
    violations: list[Violation] | None = Field(
        default=None, description="present on input validation failures; ALL of them, not the first"
    )


class ActorSummary(BaseModel):
    """An Actor, described by itself.

    `input_schema` is the Actor's own input_schema.json, passed through
    verbatim, so a client can learn what to send without reading the source.
    """

    name: str = Field(description="the Actor's name, as used in run URLs")
    description: str | None = Field(default=None, description="what the Actor does")
    input_schema: dict[str, Any] | None = Field(
        default=None, description="JSON Schema for this Actor's input; null if it declares none"
    )
    schema_error: str | None = Field(
        default=None,
        description="set when the Actor ships a schema that cannot be used, so a broken "
        "deployment is visible instead of silently disabling validation",
    )
    hidden: bool = Field(
        default=False,
        description="a human-facing client should not OFFER this Actor. Presentation only: "
        "it still lists here, still resolves by name and still runs, because the platform's "
        "own test fixtures are hidden and every suite depends on running them",
    )
    drives_host_browser: bool = Field(
        default=False,
        description="this Actor drives a browser on the OPERATOR's machine rather than one "
        "inside its container, so a challenge it parks on is solved in that person's own "
        "window. Presentation only: it tells a client where to send someone, and telling "
        "them to look at a browser that does not exist is the failure this prevents",
    )
    label: str | None = Field(
        default=None,
        description="how a client should name this Actor to a person (\"... Pages\"); null "
        "means use `name`. Presentation only, from config/actors.json",
    )
    icon: dict[str, Any] | None = Field(
        default=None,
        description="the Actor's logo: {d, color} -- one SVG path on a 24x24 grid and an "
        "optional #rrggbb colour (null: draw in the text colour). Path data only, never "
        "markup. Presentation only, from config/actors.json",
    )
    limits: dict[str, Any] | None = Field(
        default=None,
        description="per-run limits a client may offer: timeout_minutes, memory_mb and cpus, "
        "each {default, max}. A run may ask for any value up to max via query parameters on "
        "POST /actors/{name}/runs; null values mean unlimited",
    )
    awaits_human: bool = Field(
        default=False,
        description="this Actor can pause and ask a person to do something only a person "
        "can do. A client should offer a waiting prompt and a resume control only for "
        "these: for any other Actor the prompt describes a run that is not waiting, and "
        "the control resumes nothing",
    )


class ActorListResponse(BaseModel):
    actors: list[ActorSummary]


class RunAccepted(BaseModel):
    """The answer to a submission: the run is registered, not finished."""

    run_id: str
    actor: str
    status: str = Field(description="always PENDING - execution has not started")


class RunDetail(BaseModel):
    """One run's metadata.

    No input or output: both have their own endpoints, and a large Actor result
    would otherwise be re-sent on every status poll.
    """

    run_id: str
    actor: str
    status: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    exit_code: int | None = Field(
        default=None, description="the Actor container's exit code; 0 on success"
    )
    attempts: int = Field(
        default=0, description="times this run has been started; above 1 means it was retried"
    )
    error: str | None = None
    # The same failure as `error`, in the shape a machine can act on:
    # {"code", "message", "retryable", "stage", "detail", ...} -- see
    # shared/errors.py. ADDED beside `error`, never replacing it: a client that
    # only knows `error` is unaffected, and `code` is what lets one that knows
    # this decide whether trying again could possibly help.
    #
    # Deliberately typed as a free dict rather than a nested model. The shape
    # belongs to shared/errors.py, which the Actor contract also uses, and
    # restating it here would create two definitions that can drift.
    error_info: dict[str, Any] | None = Field(
        default=None,
        description="structured cause of failure: code, message, retryable, stage",
    )
    partial: bool = Field(
        default=False,
        description="the run failed but still produced usable data; fetch its output",
    )
    log_lines: int = 0
    timeout_s: int | None = Field(
        default=None, description="wall-clock limit this run runs under; null = unlimited"
    )
    memory_mb: int | None = Field(default=None, description="memory limit; null = unlimited")
    cpus: float | None = Field(default=None, description="CPU limit; null = unlimited")
    peak_memory_mb: int | None = Field(
        default=None, description="highest memory use sampled while the run's container ran"
    )

    @classmethod
    def from_run(cls, run: Run) -> RunDetail:
        return cls(
            run_id=run.run_id,
            actor=run.actor,
            status=run.status.value,
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            duration_seconds=run.duration_seconds,
            exit_code=run.exit_code,
            attempts=run.attempts,
            error=run.error,
            error_info=run.error_info,
            partial=run.partial,
            log_lines=run.log_lines,
            timeout_s=run.timeout_s,
            memory_mb=run.memory_mb,
            cpus=run.cpus,
            peak_memory_mb=run.peak_memory_mb,
        )


class RunSummary(BaseModel):
    """A row in a listing: enough to decide which run to look at."""

    run_id: str
    actor: str
    status: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    exit_code: int | None = None
    # The CATEGORY only, not the whole error. A listing is for deciding which
    # run to open; the message, stage and retry guidance live in RunDetail. One
    # short string per row is what lets an operator see "six RATE_LIMITED and
    # one ACTOR_ERROR" without fetching seven records.
    error_code: str | None = Field(
        default=None, description="failure category, when the run failed and was classified"
    )
    partial: bool = Field(
        default=False, description="the run failed but still produced usable data"
    )

    @classmethod
    def from_run(cls, run: Run) -> RunSummary:
        return cls(
            run_id=run.run_id,
            actor=run.actor,
            status=run.status.value,
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            duration_seconds=run.duration_seconds,
            exit_code=run.exit_code,
            error_code=(run.error_info or {}).get("code"),
            partial=run.partial,
        )


class RunListResponse(BaseModel):
    runs: list[RunSummary]
    count: int = Field(description="how many rows this response carries")


class RunCancelled(BaseModel):
    """The result of a cancel request: the run's state after it was applied."""

    run_id: str
    actor: str
    status: str = Field(description="CANCELLED, or the terminal state if it was already done")
    already: bool = Field(
        description="true when the run was already CANCELLED before this request"
    )


class HealthResponse(BaseModel):
    """Liveness. The ONLY endpoint that answers without a key.

    Operational counts are withheld from anonymous callers -- a queue depth is
    not a secret, but it is not something to hand out either. `auth` is always
    reported, because an operator has to be able to see that the platform came
    up unconfigured.
    """

    status: str
    auth: str = Field(description="enforced, disabled, or unconfigured")
    scopes: list[str] | None = Field(
        default=None, description="what the presented key may do; null when anonymous"
    )
    queue: str | None = Field(default=None, description="the queue implementation in use")
    queue_ok: bool | None = None
    queued: int | None = Field(default=None, description="jobs waiting")
    leased: int | None = Field(
        default=None, description="jobs taken by a Worker and not yet acknowledged"
    )
    auth_backend: str | None = Field(
        default=None,
        description="where credentials are checked: database, env, chained or disabled",
    )
    db_ok: bool | None = Field(
        default=None,
        description="whether the account database is reachable; null when there is none",
    )


class QueueSnapshot(BaseModel):
    queued: int | None = None
    leased: int | None = None
    ok: bool | None = None


class MetricsResponse(BaseModel):
    """Platform rollup over a bounded, newest-first window of runs.

    `window` names the scope so these are never mistaken for all-time totals;
    the live queue counts are exact and separate from that window.
    """

    window: int = Field(description="how many recent runs were read")
    scanned: int = Field(description="how many runs actually existed in that window")
    runs_by_status: dict[str, int]
    runs_by_actor: dict[str, dict[str, int]]
    active_runs: int = Field(description="PENDING + RUNNING within the window")
    queue: QueueSnapshot


class CollectorStatus(BaseModel):
    """One Actor's health, derived from recent runs."""

    collector: str
    total: int
    by_status: dict[str, int]
    success_rate: float | None = Field(
        default=None, description="SUCCEEDED / finished; null when nothing has finished"
    )
    last_run: dict[str, Any]
    last_success_at: str | None = None
    last_failure_at: str | None = None


class CollectorListResponse(BaseModel):
    collectors: list[CollectorStatus]
    count: int
    window: int = Field(description="the run window these were derived from")


class OutputUnavailable(BaseModel):
    """A 404 from /output that explains itself: there may simply be no result yet."""

    error: str
    detail: str
    status: str = Field(description="the run's status, which is usually the reason")
    run_error: str | None = None


AnyJson = Any
