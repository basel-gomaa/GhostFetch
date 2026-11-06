# actor-platform

An Apify-like platform for running self-contained **Actors** in Docker.

> **GhostFetch demo build.** The Actors here wrap demo collectors that return
> synthetic data, and *Connect account* stores a placeholder session instead of
> opening a real login browser. The platform itself (API, queue, Worker,
> registry, multi-tenancy, encryption, console) is the real code. Sections below
> that describe the login browser, the VNC viewer or a specific site's
> behaviour refer to the full build, which is not published.

**Status: Phase 2 — multi-tenant.** Runs are submitted over HTTP, queued, and
executed by a separate Worker process. People sign up, connect their own social
accounts, and see only their own runs.

```
HTTP client -> FastAPI -> Queue -> Worker -> Actor Executor -> Docker -> Run Registry
                  |                                                          |
                  `------------------ PostgreSQL ----------------------------'
                     users, sessions, api keys, credentials, run metadata, run items
```

Run ARTIFACTS (`input.json`, `output.json`, `log.txt`) still live on disk under
`storage/runs/<run_id>/` and are still served from there. Run METADATA moved to
the database — see [Multi-tenancy](#multi-tenancy) — and so do the optional
[items](#the-actor-contract) an Actor streams while it runs.

**The API never executes an Actor.** It validates, registers a PENDING run,
enqueues a job and returns `202` — so a submission costs milliseconds whether
or not a Worker is even running.

## The Actor contract

This is the whole interface. The platform knows nothing else about an Actor,
which is why the same worker will run a Facebook, YouTube or AI-agent Actor
without a line of change.

| | |
|---|---|
| **Input** | a JSON object at `$ACTOR_INPUT_PATH` (default `/actor/storage/input.json`) |
| **Output** | a JSON value at `$ACTOR_OUTPUT_PATH` (default `/actor/storage/output.json`) |
| **Logs** | anything printed to stdout / stderr |
| **Status** | the exit code — `0` succeeded, anything else failed |
| **Items** *(optional)* | JSON lines appended to `/actor/storage/dataset.jsonl` while running |

`/actor/storage` is a per-run directory bind-mounted from the host. The Actor
needs no network, no credentials and no platform code to run.

**Items are results you can read before the run ends.** `output.json` exists
only at exit, so a long scrape shows nothing until it finishes and a run that
dies late leaves nothing at all. An Actor that wants better appends one object
per line:

```json
{"collection": "comments", "key": "7429949831272992043:7430000000000000001", "data": {...}}
```

On Postgres the Worker tails the file every 2 s into the `run_items` table and
drains it after the container exits — before the run is marked terminal, and on
a failed run too. `GET /runs/{run_id}/items?collection=&after=&limit=` pages
them in production order, owner-scoped like every run route. On the filesystem
registry the same endpoint reads the file directly. Four rules, all in
[`registry/dataset.py`](registry/dataset.py):

- **`seq` is the line number**, so re-reading a file stores nothing twice, and
  `key` need not be unique. `collection` and `key` are opaque to the platform.
- **A line is read only once its newline arrives**, except on the final drain.
- **A malformed line is counted and skipped**, never fatal, and keeps its seq.
- **A retried attempt starts clean**: the previous attempt's file and rows go.

**Collections named `data#<n>` are BATCHES, and the console shows them.** One
convention the platform does give meaning to, and only in the console: an Actor
that delivers a big result in pieces streams each finished piece as
`data#1`, `data#2`, ..., one item per result row, and the run's **Data** tab
shows them as selectable sections **while the run is still going** (plus an
"All" view), each row expandable to its nested `comments`. Nothing else about a
collection name is interpreted - `posts` or `comments` streamed by another Actor
are not batches and the Data tab ignores them. The console fetches items with
`after=` paging and groups them itself; it never filters with `?collection=`,
because the `#` in `data#1` would start a URL fragment and the filter would
silently arrive as `collection=data`. The Facebook Actor uses this for GROUP
runs of more than 100 posts (see its `deliver_in_batches`).

A **fixed file name** rather than an environment variable, like
`attention.json`: a new variable would change every container's argv whether
or not its Actor ever writes an item. Ingestion never fails a run.

**Logs are one stream.** The container is given a single pty (`docker -t`), so
stdout and stderr share one file description and the kernel serialises the
writes. Without it they are two separate pipes that get demultiplexed and
re-merged, which measurably races: over 10 trials of an Actor interleaving 400
lines, **7/10 runs had lines spliced together** with the newline lost (e.g.
`38 err0 out`) and 3/10 had lines out of order. `log.txt` is the Actor's real
narrative, in the order it was written.

The one consequence: `isatty()` is **True** inside the container, so an Actor
that checks it may emit colour or progress-bar output. The platform has always
treated stdout and stderr as one merged `log.txt`, so nothing that was being
used is lost.

The contract lives in [`shared/models.py`](shared/models.py) as constants, so
the worker and the Actors cannot drift apart silently.

## Layout

```
actors/hello_world/     an Actor: actor.py + Dockerfile + its OWN requirements.in/.txt
actors/item_processor/  a second Actor, deliberately unlike the first
actors/facebook/        the first REAL Actor: reads posts from a public page
worker/executor.py      the Actor Executor (build image, run container, stream logs)
registry/run_registry.py the Run Registry: owns storage/runs/, nothing else writes there
cli/runs.py             the `runs` CLI: read-only inspection of past runs
api/main.py             the HTTP API: validates, registers, enqueues -- never executes
api/schemas.py          the wire format, separate from the internal Run record
api/auth.py             API key authentication, and the key generator
catalog/actors.py       Actor discovery: what Actors exist
jobqueue/               the queue abstraction + file and Redis implementations
worker/consumer.py      the queue consumer: dispatches jobs to execution slots
worker/reconciler.py    finds runs that are unfinished but absent from the queue
worker/limits.py        execution policy: concurrency, CPU, memory, timeout
shared/models.py        the contract: env var names, file names, RunStatus, Run
storage/runs/<run_id>/  one directory per run: input.json, output.json, log.txt, run.json
test_api_v1.py          public API v1 integration tests (needs Docker)
test_api_queue.py       API + queue tests (needs Docker; Worker runs as a subprocess)
test_delivery.py        at-least-once tests, incl. a real SIGKILL mid-run
test_reconciler.py      stranded-run tests
test_registry.py        registry tests (offline, no Docker)
test_runs_cli.py        CLI tests (offline, no Docker)
test_regression.py      end-to-end tests (needs Docker)
test_concurrency.py     concurrency, resource limits, timeout, crash recovery
```

The worker is split in three, so execution, persistence and policy stay
independent:

```
Worker
  |-- Actor Executor    docker build / docker run / log streaming
  |-- Run Registry      storage/runs/<run_id>/
  `-- Execution limits  how many at once, and how big each may be
```

Three Actors exist. Two are references that prove the contract generalises;
the third, `facebook`, is a **real** scraper adapted onto it. The worker runs all
three without a line of Actor-specific code:

| | `hello_world` | `item_processor` | `facebook` |
|---|---|---|---|
| Dependencies | none | `python-slugify` | `httpx[http2]` |
| Output | object | **array** | object with posts (+ optional comments) |
| Failure | bad input only | **mid-run** | dead session, page unavailable |
| Runtime | instant | seconds | **minutes to hours** |

## Running an Actor

```bash
python3 worker/executor.py hello_world --input '{"name": "Basel"}'
python3 worker/executor.py item_processor --input '{"count": 5, "fail_at": null}'
python3 worker/executor.py item_processor --input '{"count": 5, "fail_at": 3}'   # fails at item 3
```

The first returns `{"message": "Hello Basel"}`; the second an array of five
records; the third fails part way through, keeping its progress in `log.txt`
and writing no `output.json` — a partial result must never be readable as a
complete one.

Other forms:

```bash
python3 worker/executor.py hello_world --input-file ./input.json
python3 worker/executor.py hello_world --input '{"name": "Basel"}' --build
python3 worker/executor.py hello_world --input '{"name": "Basel"}' --cpus 0.5
```

The image is built automatically on first use, **and rebuilt whenever its
build context changes**; `--build` forces a rebuild.

Every image is labelled `org.actor-platform.context-hash` with a hash of its
build context (`actors/<name>/`):
each file's path, bytes and executable bit, and each symlink's target, skipping
`__pycache__` and `.pyc`. Before a run the worker hashes the context again
(about 20 ms) and rebuilds when the image is missing, unlabelled, or labelled
with a different hash. So a `sync_collector.sh`, a Dockerfile edit or a pinned
dependency bump reaches the next run with nobody rebuilding by hand. It used to
build only a MISSING image, and on 24 October that ran a mismatched Playwright
(every LinkedIn, X and Instagram launch failed) and pre-change Threads code.
A rebuild where nothing that is COPY'd changed is mostly layer cache.
`shared/image_freshness.py` is the one definition, used by the worker.

**If a REBUILD fails and an older image exists, the run goes ahead on the old
image**, so a Docker Hub rate limit or a proxy outage does not stop every run.
It is not silent: the worker logs a WARNING, emits a `stale_image` event, and
the run's own `log.txt` starts with `[platform] WARNING: this run used <image>,
which was built from older sources ...`. With no image at all a failed build
still fails the run, as before.

Not covered: a moving base-image tag such as `FROM python:3.13-slim`. The
context hash cannot see an upstream patch to it (a Dependabot bump to the
`FROM` line does change the hash). Pull base images (`docker build --pull`) to
pick those up.

**Worker exit codes:** `0` run succeeded · `1` run failed (the Actor's fault)
· `2` the platform could not start the run (no Docker, unknown Actor, bad JSON).
The distinction matters — a scheduler should retry a `2`, not a `1`.

## Running an Actor by hand

An Actor is an independent executable, so the platform is optional:

```bash
mkdir -p /tmp/run && echo '{"name":"Standalone"}' > /tmp/run/input.json
docker run --rm --user "$(id -u):$(id -g)" \
  -v /tmp/run:/actor/storage actor-platform/hello_world:latest
cat /tmp/run/output.json
```

Images can also be built through compose:

```bash
docker compose --profile build build hello_world
```

## The Run Registry

Past runs are inspectable as a normal Python component — no API needed:

```python
from registry.run_registry import RunRegistry

registry = RunRegistry()
registry.list(limit=10)                 # newest first
registry.list(actor="hello_world")      # filter by Actor or status
run = registry.get(run_id)              # the Run record
registry.get_input(run_id)              # input.json
registry.get_output(run_id)             # output.json  (strict: raises if absent)
registry.get_logs(run_id)               # log.txt
```

**Ordering is `run_id` descending — newest first.** That is a total, stable
order rather than a convention: run ids are
`<date>-<time-to-the-microsecond>-<random>`, so string order is chronological
order.

Microseconds, not seconds or milliseconds, because the timestamp has to do the
ordering on its own. Measured over 20,000 ids minted in a tight loop: **one**
distinct second-value, **62** distinct millisecond-values, **20,000** distinct
microsecond-values. At second resolution every run in the same second tied and
fell back to its random suffix — i.e. `list()` was arbitrary, not chronological.
The suffix now only breaks ties between concurrent processes.

Ids written before this change use a 6-digit time field. They stay valid and
still sort correctly, since `-` is below every digit.

**The record does not carry the output.** `run.json` holds metadata and points
at the run directory; `output.json` holds the result. Embedding it doubled every
run's on-disk cost and, worse, made `list()` parse every result to build
summaries containing none of it. Measured on a 150-post Facebook run:

| | run.json | `list(limit=10)` reads |
|---|---|---|
| embedded | 90,692 B | 99,577 B (9,958 B/run) |
| pointed at | **483 B** | **4,798 B (480 B/run)** |

A record is now a fixed ~480 B whatever the Actor produced, so listing cost no
longer scales with output size. Records written before this still load — unknown
keys are ignored — and `registry.get_output()` is the one way to read a result.

**A run is its `run.json`.** A directory without one was never registered, so
it is not a run. A record that exists but cannot be parsed raises
`CorruptRunRecord` rather than quietly vanishing from `list()` — pass
`skip_corrupt=True` to tolerate it deliberately.

**Statuses.** `PENDING → RUNNING → ` one of `SUCCEEDED`, `FAILED`,
`PLATFORM_ERROR`. Runs move forward only; a finished run cannot be rewritten.
There is no `TIMED_OUT`: the platform never ends a run for elapsed time.
`FAILED` (the Actor ran and failed) and `PLATFORM_ERROR` (the platform could not
run it) are deliberately distinct — that is the only signal telling a caller
whether a retry could help.

A run exists in the registry only once a container has actually started.
Pre-flight failures — bad Actor name, unknown Actor, malformed input, failed
build — exit `2` without creating anything.

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

export ACTOR_PLATFORM_API_KEYS="$(python3 api/auth.py)"   # mint and configure a key

.venv/bin/uvicorn api.main:app --port 8000     # terminal 1: the API
python3 worker/consumer.py                     # terminal 2: the Worker
```

**The API refuses to serve until you decide about authentication.** With no keys
configured and no explicit opt-out, every endpoint answers `503 authentication
not configured` — forgetting to set keys fails loudly rather than quietly
exposing an execution engine. To run open on purpose:

```bash
export ACTOR_PLATFORM_AUTH=off
```

Two processes, on purpose. The API stays up and responsive whether or not a
Worker is running; runs simply wait in the queue until one appears.

### Submit a run

```bash
curl -H "Authorization: Bearer $ACTOR_PLATFORM_API_KEYS" localhost:8000/actors
# {"actors":[{"name":"hello_world",
#             "description":"Greets someone by name...",
#             "input_schema":{"type":"object","required":["name"],...}}, ...]}

curl -X POST localhost:8000/actors/hello_world/runs \
     -H 'content-type: application/json' \
     -d '{"name": "Basel"}'
# 202 Accepted
# {"run_id":"20260902-...","actor":"hello_world","status":"PENDING"}
```

The body is the Actor's input, whatever shape that Actor expects. The response
comes back in milliseconds — it is an acknowledgement, not a result.

### Inspect it

```bash
curl localhost:8000/runs/20260902-...
# {"run_id":"...","actor":"hello_world","status":"SUCCEEDED","created_at":"...",
#  "started_at":"...","finished_at":"...","duration_seconds":0.47,
#  "exit_code":0,"attempts":1,"error":null,"log_lines":4}

curl localhost:8000/runs                      # newest first
curl "localhost:8000/runs?actor=hello_world&status=FAILED&limit=5"
```

### Get the result

```bash
curl localhost:8000/runs/20260902-.../input     # {"name":"Basel"}
curl localhost:8000/runs/20260902-.../output    # {"message":"Hello Basel"}
curl localhost:8000/runs/20260902-.../logs      # text/plain
```

`/output` returns whatever the Actor produced — an object from `hello_world`, an
array from `item_processor`. Until a run succeeds it returns `404` carrying the
run's status, so "not finished yet" is never confused with "no such run".

The same runs are visible from the CLI (`python3 cli/runs.py list`); both read
the one Registry.

### The API

| Endpoint | |
|---|---|
| `GET /actors` | Actors the platform can run, discovered from `actors/` |
| `POST /actors/{actor}/runs` | register + enqueue; `202` with the run id |
| `GET /runs` | recent runs, newest first (`?actor=`, `?status=`, `?limit=`) |
| `GET /runs/{run_id}` | run metadata |
| `GET /runs/{run_id}/input` | the input document |
| `GET /runs/{run_id}/output` | the Actor's result; `404` until there is one |
| `GET /runs/{run_id}/logs` | `text/plain` |
| `POST /runs/{run_id}/cancel` | request cancellation; `200` + final state, `409` if already finished |
| `GET /metrics` | run counts by status/collector + queue depth (`?window=N`) |
| `GET /collectors` | per-collector health: last run, success rate, counts |
| `GET /health` | liveness, queued and leased job counts |

Errors are always `{"error": "...", "detail": "..."}`:

| | |
|---|---|
| `400 invalid actor name` | the name could never denote an Actor |
| `400 invalid actor input` | the body does not match the Actor's declared schema; carries `violations` |
| `404 unknown actor` | well-formed, but not installed |
| `404 run not found` | no such run |
| `503 queue unavailable` | the run was registered but could not be queued, so it is closed as `PLATFORM_ERROR` rather than left waiting for ever |

Those first two stay distinct on purpose, and **neither creates a run or a job**.

**The API never executes an Actor.** It validates, registers a `PENDING` run,
enqueues a job and returns. It imports no Docker code — not even transitively,
since Actor discovery lives in `catalog/` rather than in the execution module —
and no response exposes a filesystem path, a container, or Redis. A test submits
a ~6s Actor and asserts the request returns in under a second.

Interactive docs are at `/docs`; the OpenAPI schema at `/openapi.json`.

## Authentication

API keys, presented as `Authorization: Bearer <key>`, each carrying what it may do.

```bash
python3 api/auth.py                # mint a key with every scope
python3 api/auth.py read           # mint a read-only entry

export ACTOR_PLATFORM_API_KEYS="apk_aaa:read,apk_bbb:read+run,apk_ccc"

curl -H 'Authorization: Bearer apk_aaa' localhost:8000/actors
```

### Scopes

| Scope | Grants |
|---|---|
| `read` | `GET /actors`, `GET /runs`, and a run's detail, input, output and logs |
| `run` | `POST /actors/{actor}/runs` |

Two, deliberately. The distinction that matters is observer versus operator:
reading a run is harmless, starting one spends CPU and reaches the network.
Finer grants — per-Actor, per-run — would need a real key store rather than an
environment variable.

`key:read+run` grants both; `key:*` and a **bare key with no suffix** grant
everything. That last one is why adding scopes changed no existing deployment:
a key configured before scopes existed still does exactly what it did.

An unknown scope name is **refused, not ignored** — the platform will not start
with `apk_x:admin` in its environment. Silently dropping it would hand out a key
with less access than intended and surface the failure somewhere far away.

**`401` and `403` mean different things here.** `401 authentication required` /
`invalid api key` means we do not know who you are. `403 insufficient scope`
means we do, and a better credential will not help — a differently *scoped* one
would. The `403` body names both sides:

```json
{
  "error": "insufficient scope",
  "detail": "this key does not have the 'run' scope (it has: read)",
  "required_scope": "run",
  "granted_scopes": ["read"]
}
```

| Mode | When | Behaviour |
|---|---|---|
| `enforced` | `ACTOR_PLATFORM_API_KEYS` is set | a valid key is required |
| `disabled` | `ACTOR_PLATFORM_AUTH=off` and no keys | open, deliberately |
| `unconfigured` | neither | every endpoint returns `503`, naming the fix |

Setting both keys and the opt-out is a mistake, and the stricter reading wins:
keys are enforced.

`GET /health` reports the scopes your key holds, which is the quickest way to
check what a key can actually do.

**`GET /health` is the one public endpoint.** A liveness probe should not need a
credential, and an operator who forgot to configure keys has to be able to see
that — so `auth` is always reported. Operational counts (`queued`, `leased`) are
withheld from anonymous callers; a queue depth is not a secret but it is not
something to hand to a stranger either. `/docs` and `/openapi.json` stay public:
they describe the API's shape, not its data.

Keys are compared with `secrets.compare_digest`, never `==` — a plain comparison
returns on the first differing byte, and that timing difference is enough to
recover a key one character at a time. A rejected credential is never echoed
back in the response, because an error that quotes the key is how a wrong key
becomes a written-down key.

**What this does not protect.** The Registry and Queue are files on disk, and
`cli/runs.py` reads them directly. Authentication guards the HTTP surface, not
the machine: anyone with filesystem access has everything regardless. There are
no users or roles, scopes are platform-wide rather than per-Actor, and revoking
a key means editing the environment and restarting.

## Secrets for Actors

An Actor may need a credential the platform must never see. The Worker forwards
named environment variables into every Actor container:

```bash
export FB_SESSION='c_user=...; xs=...'     # the secret, in the Worker's env
export ACTOR_FORWARD_ENV="FB_SESSION"      # the NAMES to forward
python3 worker/consumer.py
```

The value is passed as `docker run -e NAME` with **no** `=value`: the docker CLI
reads it from the Worker's own environment, so the secret never appears in the
container's argv where `ps` would show it to every user on the machine. The
platform knows the names and never the values.

**This is why credentials are not Actor input.** Anything submitted as input is
written to `input.json`, embedded in `run.json`, carried in the queue job and
served by `GET /runs/{id}/input` — four surfaces, all verified. Forwarded
environment reaches none of them.

Contract variables (`ACTOR_INPUT_PATH`, `ACTOR_OUTPUT_PATH`, `ACTOR_RUN_ID`)
cannot be forwarded over the top of the ones the executor sets.

It is deliberately not a secrets manager: no storage, no rotation, no per-Actor
scoping. Every Actor container gets the same forwarded names.

## Actor input schemas

An Actor may ship an `input_schema.json` — a plain JSON Schema document — beside
its `Dockerfile`. The platform reads it for two things: `GET /actors` becomes
self-describing, and a bad submission is rejected immediately instead of failing
inside a container seconds later as an opaque exit 1.

```bash
curl -X POST localhost:8000/actors/hello_world/runs \
     -H 'content-type: application/json' -d '{"nmae": "Basel"}'
```
```json
{
  "error": "invalid actor input",
  "detail": "input does not match the schema for 'hello_world': ...",
  "violations": [
    {"path": "(root)", "message": "'name' is a required property"},
    {"path": "(root)", "message": "Additional properties are not allowed ('nmae' was unexpected)"}
  ]
}
```

That typo is the case worth having this for: without a schema the Actor sees no
`name`, and the platform builds an image and starts a container just to discover
it. **Every** violation is reported, not the first.

**Unknown fields are REJECTED.** Both schemas declare `additionalProperties:
false`, and that is a decision rather than a default. Ignoring unknown fields —
which both Actors' own parsers do — means a typo or a stale client field is
silently dropped and the run quietly does something other than what was asked.
The cost is accepted and real: adding an input field to an Actor now requires
updating its schema in the same commit, or valid requests start failing. A test
asserts the declaration is present, so this can never become accidental.

Every input failure answers in the same envelope, including a body that is not
a JSON object at all — declaring the body as a dict would have let FastAPI
answer that one case with its own `422 detail[]` shape instead.

**A schema does NOT replace the Actor's own validation, and must never be read
that way.** An Actor is an independent executable — `docker run` starts one with
no platform involved — so it stays responsible for what it was given. The
platform's check is an early filter for callers, and it is deliberately coarser:
`"   "` satisfies `minLength: 1` and is still rejected by `hello_world`, which
trims first. Both checks exist, and there is a test for each.

**A schema is optional.** An Actor without one accepts any JSON object, so
adding this broke nothing. A schema that *cannot be parsed* is a different
matter: it is reported as `schema_error` on `GET /actors` and makes submissions
fail, because silently treating it as "no schema" would switch validation off
without anyone noticing.

**The API validates, and the Worker validates again.** The queue and the run
record are stores the API does not control once it has returned, and an Actor's
schema can tighten between submission and execution, so `execute_run` re-runs
the same catalog check before preparing a container. A refusal there is
recorded as `FAILED` -- the *input* failing, exactly as an Actor's own refusal
is -- never as `PLATFORM_ERROR`, so the semantic the system is built on holds.
The direct executor CLI (`worker/executor.py <actor>`) still runs whatever it
is given, which is how the tests prove each Actor validates its own input.

### Input limits and validation

Beyond each Actor's schema, the platform refuses some inputs whatever the
schema says, and bounds what an Actor can make it hold in memory. Every limit
is a refusal, never a silent clamp, and the configurable ones are environment
variables read by the API and the Worker:

| What | Limit | Configured by |
|---|---|---|
| Request body (any route, read before auth) | 1 MiB, else `413` | `ACTOR_PLATFORM_MAX_BODY_BYTES` |
| Run input: NUL / unpaired surrogates in any string or key | refused, `400` | -- |
| Run input nesting depth | 32 levels | -- |
| Integers in a schema | `5.0` and `1e3` are not integers | -- |
| `format: date-or-datetime` | a real date (or date + time), years 2000-2100 | -- |
| `x-date-window: {start, end}` | start may not be after end (a bare end date is end of day) | -- |
| A schema violation's message | 200 characters, so a value is never echoed whole | -- |
| Platform Actors' `target` | 2048 characters, no control characters | each `input_schema.json` |
| A queued job | 4 MiB, every field type-checked; a bad one is moved to `rejected/` (file) or `<queue>:rejected` (Redis) | -- |
| `output.json` / `input.json` read back | 64 MiB, else the run's output is reported corrupt | `ACTOR_PLATFORM_MAX_OUTPUT_BYTES` |
| `log.txt` kept by the Worker and served | 32 MiB, then a truncation note; one line is cut at 64 KiB | `ACTOR_PLATFORM_MAX_LOG_BYTES` |
| One dataset line | 4 MiB, nesting 100, no NUL / lone surrogates | -- |
| A session an Actor hands back | 4 MiB | -- |

422 responses keep FastAPI's shape (`{"detail": [{type, loc, msg}]}`) but never
carry the offending `input` -- the default echoed a signup's plaintext password.

**Not limited, deliberately:** run duration (long scrapes are the point) and the
`facebook_groups` Actor's documented `0 = no cap` on posts and comments, which a
date window bounds instead.

## The queue

```python
queue.enqueue(job)          # API side
queue.dequeue(timeout=1.0)  # Worker side
```

Two implementations behind one abstraction, chosen by `ACTOR_QUEUE_URL`:

- **file-backed** (default) — a directory of JSON files under `storage/queue/`.
  No server, so the platform runs with nothing installed and the tests are
  hermetic.
- **Redis** — set `ACTOR_QUEUE_URL=redis://127.0.0.1:6379/0`
  (`docker compose --profile queue up -d redis`).

The Worker never names an implementation; it calls `open_queue()`. The full API
test suite passes unchanged against either, which is what makes the abstraction
more than a promise.

**Ordering: FIFO.** With the single Worker this milestone builds, runs are
processed in submission order. Nothing promises ordering across concurrent
consumers.

**Delivery: at-least-once.** `dequeue()` *leases* a job rather than removing it;
the Worker acks once the run reaches a terminal state. A lease whose owner has
died is returned to the queue by `recover()`, so a Worker killed mid-run does
not strand its work — a replacement picks it up, `Run.attempts` becomes 2, and
the run finishes.

Abandonment is decided by **owner liveness**. Each lease records `<host>:<pid>`;
on the same host a dead pid is detected in seconds. The age-based
`--visibility-timeout` is **off by default** (`None`): Actors here may run for
hours, and any finite value would reclaim a healthy run's lease, kill its
container and restart it — the platform ending an Actor for elapsed time via the
queue. So when liveness is unknowable (another host, an unparseable owner) the
answer is "leave it alone" until an operator sets a timeout explicitly. On one
host, owner liveness is the whole story.

**The cost is duplicate delivery**, and consumers must be idempotent. A Worker
that finishes a run and dies before acking will see that job again;
`worker/consumer.py` checks the registry first and acks without re-running a
run that is already terminal. It also kills any container the dead Worker left
behind before retrying, so two containers never write one run directory.

**A job is given up on after `--max-attempts` (default 3)** — the run is closed
as `PLATFORM_ERROR` rather than retried for ever. Without a ceiling, a job that
reliably kills its Worker would be redelivered indefinitely.

## Stranded runs

Leases protect work that is still *in* the queue. They cannot help a run whose
job left the queue without the run finishing — the queue was purged, Redis was
flushed, the API died between registering a run and enqueueing it. The registry
then holds a run that is `PENDING` or `RUNNING` for ever while no Worker has any
reason to touch it. There is nothing to redeliver, so only a registry scan can
find it.

```
stranded == not terminal AND absent from the queue (waiting or leased)
```

That test is sound because of an invariant the consumer maintains: a Worker only
executes a job it has leased, and only acks once the run is terminal. So while a
run is `RUNNING`, its job is always somewhere in the queue.

The Worker sweeps at startup and every `--reconcile-interval` (300s; `0` disables
it). It also runs standalone:

```bash
python3 worker/reconciler.py --dry-run    # report, change nothing
python3 worker/reconciler.py              # requeue what is stranded
```

A `PENDING` run is simply re-enqueued. A `RUNNING` one is reset to `PENDING`
first — and any container it left behind is killed, so two containers never
write one run directory. `Run.attempts` carries forward, so the same
`--max-attempts` ceiling applies and a run that keeps stranding is eventually
closed as `PLATFORM_ERROR`.

**Nothing younger than `--reconcile-grace` (60s) is judged**, because "not in the
queue" is also what a perfectly healthy run looks like in the moment between
`registry.create()` and `queue.enqueue()`.

## Concurrency and resource limits

```bash
python3 worker/consumer.py --concurrency 6          # global cap; per-Actor caps live in config
```

| | default | |
|---|---|---|
| `--concurrency` | `global.max_concurrency` in the config, else `1` | containers this Worker may run at once, across all Actors |
| `--actor-config` | `$ACTOR_CONFIG` or `config/actors.json` | per-Actor policy file |
| `--cpus` | unlimited | host-wide CPU **ceiling** per container, e.g. `0.5`; no Actor default or request can exceed it |
| `--memory` | unlimited | host-wide memory **ceiling** per container, e.g. `512m` (minimum `6m`) |
| `--shutdown-timeout` | `60` | on **SIGINT/SIGTERM**, how long to wait for runs in flight |

### Per-run limits: timeout, memory, CPUs, processes

*Security review H-03, 2026-10-23.* Every Actor has a `limits` section in
`config/actors.json`:

```json
"facebook": { "limits": { "timeout_minutes": 120, "max_timeout_minutes": 360,
                          "memory_mb": 2048, "max_memory_mb": 4096,
                          "cpus": 2, "max_cpus": 4, "pids": 1024 } }
```

A run gets the **defaults** unless it asks for something else, **up to the
maximum**: the console's *Run options* card, or query parameters on
`POST /actors/{name}/runs?timeout_minutes=240&memory_mb=3072&cpus=2`. Asking
above the maximum is refused with 400, never quietly lowered. `GET /actors`
returns each Actor's `limits` so a client can offer them, and a run's record
(`GET /runs/{id}`) shows the limits it ran under and its sampled
`peak_memory_mb`. A `defaults.limits` section applies whole to any Actor
without its own.

**How the defaults were chosen.** From every past run (2,598 of them), not
guessed: the default timeout is at least 2.5x the Actor's longest successful
run, rounded up, and higher where a legitimately longer run is on record (a
48-minute Facebook group run gives a 120-minute default and a 6-hour maximum; a
TikTok run can park 15 minutes for a person to solve a slider, so it gets 60).
`scripts/review_run_limits.py` repeats the review on demand. Memory had never
been measured, so it is generous (browser Actors 3 GB), and every run now
records its peak so the numbers can be tightened from data.

**Why per Actor and per run rather than one number.** This README used to say
there was deliberately no timeout, because real scrapes run 17–50 minutes and
any platform-wide ceiling would kill real work. That still rules out ONE
number. But no limit at all let one stuck browser hold a slot and its memory
for ever, which on a shared deployment is a denial of service.

**Enforcement.** `ActorLimits.resolve()` is the single clamp: request, else the
Actor's default; never above the Actor's maximum; never above the Worker's
`--cpus/--memory`. The API checks at submission; the Worker clamps again at
execution, because a run record is not trusted to hold what the API accepted.
CPU, memory (`--memory` = `--memory-swap`, so there is no swap), `--pids-limit`
and, for browser Actors, `--shm-size` are Docker flags. The **timeout** is the
Worker's: the watcher thread that already handles cancellation stops the
container (`docker stop`, 30 s grace) once the run has been RUNNING that long,
and the run ends `FAILED` with *"timed out after N min (this run's limit; a run
of this actor may ask for up to M min)"*. Partial output and items are kept. An
exit 137 with peak memory near the limit is reported as *"ran out of memory"*.

The queue's visibility timeout stays off by default (`DEFAULT_VISIBILITY_TIMEOUT_S
= None`): a healthy hours-long run's lease must never be reclaimed by age.
`--shutdown-timeout` is unrelated: it bounds only how long an *interrupted*
Worker waits before leaving its runs for recovery.

### Container hardening

*Security review M-02.* Every Actor container runs with `--cap-drop ALL` and
`--security-opt no-new-privileges`, a pids
cap, and on the `ap-actor-net` bridge, which the Worker creates with
inter-container communication **off**: one run's container cannot reach
another's ports. Outbound internet and loopback-published display ports work
as before; an Actor declaring `host_network` still gets the host namespace.
There is deliberately **no read-only root filesystem**: every Actor writes its
session jar under `/tmp`, and a tmpfs there counts against the memory limit.

### Host isolation

*Security review N-01.* A container on a Docker bridge reaches the host through
the bridge's gateway (`172.19.0.1`, `172.20.0.1`) and through every other host
address, the LAN IP included. Anything listening on `0.0.0.0` is therefore one
`connect()` away from a compromised Actor dependency. Measured on the development machine before this fix: from both
bridges, a container could open the API (when it bound `0.0.0.0`), sshd on 22
and a web server on 80. Two layers close it.

1. **The API binds to `127.0.0.1` by default.** Nothing needs it anywhere else.
   The console is served by the API itself, Caddy proxies over loopback, and
   no container ever calls the API: Actors hand results back through files in
   their run directory. To use the console from another device on a
   trusted LAN, opt in with `ACTOR_PLATFORM_BIND_HOST=0.0.0.0 ./run-local.sh`.
   `run-local.sh` warns when you do. Public mode (`PUBLIC_DOMAIN`) always
   forces loopback.
2. **A host firewall rule keeps both bridges off every host port**
   (`deploy/host-firewall.sh`, run as root). Both networks are created with a
   fixed interface name, `ap-actor0` and `ap-login0`, rather than Docker's
   `br-<id>`, which changes whenever a network is recreated. The script adds
   its own chain and one jump at the top of `INPUT` per bridge:
   - replies to the host's own connections into containers are allowed;
   - every other packet is rejected.

   It touches no other rule, no chain policy, none of Docker's chains and not
   `FORWARD`. So containers keep outbound internet and DNS, and
   loopback-published ports keep working. `remove` takes it out exactly.

```bash
sudo deploy/host-firewall.sh apply                    # until reboot
sudo deploy/host-firewall.sh status
.venv/bin/python scripts/check_container_isolation.py # live check, no root needed
```

The rules last until reboot. `deploy/actor-platform-firewall.service`
re-applies them at boot from a root-owned copy of the script; the install
commands are in that file. `check_container_isolation.py` is the only real
proof. It starts a throwaway container on each bridge and tries every
non-loopback host port at every host address. Then it reports each one as
reachable, blocked or closed, and exits non-zero if anything is reachable.
`test_network_isolation.py` only proves what the code *asks* for.

**What this does not cover.**
- Containers can still reach *other* machines on your LAN (router admin page,
  NAS) through `FORWARD`.
- An Actor declaring `host_network: true` bypasses both layers. None does, and
  `test_tiktok.py` pins that.
- On a cloud VM, also block the metadata address (`169.254.169.254`) in
  `DOCKER-USER`.

### Chrome's sandbox: a seccomp profile for browser containers

`deploy/seccomp/chrome-sandbox.json` is Docker's default seccomp profile plus
the three operations Chrome's own sandbox needs (`clone`/`unshare` with
`CLONE_NEWUSER|NEWPID|NEWNET`, and `chroot`), generated by
`deploy/seccomp/make_chrome_profile.py` from the pinned default. An Actor that
declares `chrome_sandbox: true` in `config/actors.json` gets it, together with
`CHROME_SANDBOX=1`. No Actor in the demo build runs a browser, so none declares
it; `test_seccomp_profile.py` still checks the profile and the Worker's flag.

### Fair use: per-user quotas

*Security review M-09.* A non-admin user may have at most
`ACTOR_PLATFORM_MAX_ACTIVE_RUNS_PER_USER` (default 10) runs PENDING or RUNNING;
the next is refused with 429 and a message saying so. The count and the insert
happen under a per-user Postgres advisory lock, so two racing submissions
cannot both slip under the cap. The whole queue is capped at
`ACTOR_PLATFORM_MAX_QUEUE_DEPTH` (default 500, 503 beyond it). Admins and
operator keys are exempt. `0` turns either cap off.

### Per-Actor concurrency

A run needs a **global slot AND its Actor's slot** before its container starts.
Policy lives in `config/actors.json`, never in Actor code:

```json
{
  "global":   { "max_concurrency": 6 },
  "defaults": { "max_concurrency": null },
  "actors":   { "facebook": { "max_concurrency": 1 } }
}
```

`max_concurrency: null` (the default for any unconfigured Actor) means *bounded
only by the global limit* — exactly how every Actor behaved before per-Actor
limits existed, so the feature is purely additive. `WorkerConfig.limits_for(actor)`
is the single place policy is resolved; no Actor name appears anywhere in
`worker/`, `api/`, `jobqueue/` or `registry/` (asserted by an AST scan).

**The per-Actor limit is enforced by not leasing, not by holding.** `dequeue()`
takes an `eligible(actor)` predicate; a job whose Actor is already at its limit is
never leased, so it stays plainly `PENDING` in the queue where another Worker and
the reconciler can both still see it. This is what gives **fairness** for free: an
Actor at its cap is skipped, so jobs of *other* Actors behind it proceed, and when
a slot frees the oldest eligible job is taken next. No scheduler needed.

**Measured**, `global 6, facebook 1`: 3 `facebook`-shaped jobs enqueued *ahead*
of 6 uncapped jobs → peak **6** total, **1** capped, the uncapped Actor filled the
other 5; the capped jobs at the head of the queue did not block anything. Only 1
of the 3 was ever leased; the other 2 stayed `PENDING`.

**A slot is taken *before* a job is leased.**

```
   acquire GLOBAL slot
        |
   dequeue(eligible) -> LEASE     (nothing runnable? release the slot, loop)
        |
   take ACTOR slot
        |
   registry.mark_running          RUNNING recorded BEFORE the container exists
        |
   docker run  ->  complete  ->  ack
        |
   release ACTOR slot, release GLOBAL slot   (finally, always)
```

Taking the slot first keeps the invariant the reconciler's soundness rests on:
**a held lease means a live (or about-to-be-live) container.** Each job runs on
its own daemon thread; the semaphores, not the thread count, bound concurrency.

### The resource limits are enforced, and recorded

The limits a run actually ran under are written onto its record (`timeout_s`,
`memory_mb`, `cpus`, `peak_memory_mb`). `--memory` is always paired
with an equal `--memory-swap`, because Docker's default of swap = 2x memory lets
a process quietly exceed a lone `--memory`. Verified from **inside** the live
container (the kernel's view, not the daemon's record of what it was asked for):

```
--cpus 0.5 --memory 256m  ->  cpu.max         50000 100000
                              memory.max      268435456
                              memory.swap.max 0
```

Behaviourally too: `--memory 256m` completes, `--memory 12m` is OOM-killed with
exit `137`, every time — absolute values, so the same proof holds on a laptop and
a 1 TB server.

### Crashes and recovery

`mark_running` is recorded *before* `docker run`, so the dangerous window — a
container alive that the registry knows nothing about — **does not exist**.

| Crash point | Left behind | Who fixes it | Duplicate run? |
|---|---|---|---|
| lease taken, container not started | run `PENDING`, lease held by a dead pid | `recover()` sees the dead owner and requeues | No — the retry is the first execution |
| `RUNNING` recorded, container starting | run `RUNNING`, container maybe alive | `process_job` kills the container, `requeue()`s, executes | No — the kill precedes the retry |
| Actor finished, not yet acked | run terminal, lease abandoned | `process_job` sees a terminal run and acks it | No — the existing idempotency check |

Three independent mechanisms stop two containers ever running one run: the
**lease** is exclusive, the **consumer** refuses a non-`PENDING` run, and
**Docker** refuses a duplicate `--name`. Slots are in-memory, so a killed Worker's
slots die with the process and a replacement starts clean — a crash cannot leak a
slot. A Worker killed with three runs active leaves three `RUNNING` records and
three leases; a replacement finishes all three with `attempts == 2` and
byte-identical output. Reconciling *during* concurrent execution reclaims nothing
even with `grace=0`, because every active run holds a lease.

## Inspecting runs

```bash
python3 cli/runs.py list                                  # newest first
python3 cli/runs.py list --actor hello_world --status FAILED
python3 cli/runs.py list --all --json                     # machine-readable
python3 cli/runs.py show   20260902-0817                  # any unique prefix
python3 cli/runs.py logs   20260902-0817
python3 cli/runs.py output 20260902-0817
python3 cli/runs.py input  20260902-0817
```

```
RUN ID                  ACTOR           STATUS     EXIT  DURATION  CREATED
20260902-081758-b2dc6b  item_processor  FAILED     1     1.61s     2026-09-02 08:17:58
20260902-081758-759287  hello_world     SUCCEEDED  0     0.81s     2026-09-02 08:17:58
```

Run ids can be abbreviated to any **unique** prefix; an ambiguous one is
refused and lists the candidates rather than picking one.

**CLI exit codes are `0` and `2` only** — deliberately unlike the worker's.
`0` means the command worked, `2` means it could not (no such run, ambiguous
id, corrupt record, bad usage). There is no `1`, because in this platform that
means "the Actor failed", which a read-only command can never report about
itself: inspecting a FAILED run is a *successful* inspection. `runs output` is
the one nuance — it exits `2` when a run produced nothing, so
`runs output X > result.json` cannot silently write an empty file.

## Tests

```bash
python3 test_registry.py      # 28 registry tests, offline
python3 test_runs_cli.py      # 15 CLI tests, offline
python3 test_regression.py    # 16 end-to-end tests, needs Docker
python3 test_api_v1.py        # 12 API v1 tests, needs Docker
python3 test_actor_schemas.py # 20 input schema tests
python3 test_auth.py          # 25 authentication and scope tests
python3 test_facebook.py      # 12 Facebook Actor adapter tests, needs Docker
python3 test_api_queue.py     #  9 API + queue tests, needs Docker
python3 test_delivery.py      #  8 at-least-once tests, needs Docker
python3 test_reconciler.py    # 13 stranded-run tests, needs Docker
python3 test_concurrency.py   # 30 concurrency + per-Actor tests, needs Docker
python3 test_observability.py  # 16 observability tests, needs Docker
python3 test_cancellation.py   # 12 cancellation + race tests, needs Docker
python3 test_ui.py             #  8 dashboard serving/self-containment tests
python3 test_input_validation.py  # 32 input/queue/output boundary tests (1 uses Docker if present)
python3 test_input_validation_db.py  # 9 account-route validation tests, needs a throwaway DB
python3 test_accounts.py       # accounts, sessions and the demo connect, needs a throwaway DB
```

`test_concurrency.py` measures rather than infers: it samples live containers
to record a peak, and times a burst against its own measured serial cost, so it
can tell parallel execution from sequential execution instead of only checking
that everything eventually finished.

## Observability

The platform is inspectable through read-only endpoints and a structured event
stream — no new storage, all derived from the Registry the runs already live in.

**Per-run lifecycle and metadata** (`GET /runs`, `/runs/{id}`): every run carries
`status`, `created_at`/`started_at`/`finished_at`, `duration_seconds`,
`exit_code`, `error`, `attempts`, `log_lines`. States are `PENDING → RUNNING →`
one of `SUCCEEDED`, `FAILED`, `PLATFORM_ERROR`, `CANCELLED`. **`CANCELLED`** is
the platform *stopping trying* — a run abandoned after too many interrupted
attempts, or stranded with no queued job — kept distinct from `FAILED` (the Actor
ran and failed) and `PLATFORM_ERROR` (the platform could not run it). An
*interrupted* run is simply `RUNNING` with its lease held until recovery finishes
it; `attempts > 1` records that it was retried.

**Platform rollup** (`GET /metrics`): run counts by status and by collector, plus
live queue depth. Computed over a bounded, newest-first window (`?window=N`,
default 500) so cost stays flat as history grows — the response reports `window`
and `scanned` so the numbers are never mistaken for all-time totals.

```bash
curl -H "authorization: Bearer $KEY" localhost:8000/metrics
# {"window":500,"scanned":500,"runs_by_status":{"SUCCEEDED":420,"FAILED":70,...},
#  "runs_by_actor":{"facebook":{...}},"active_runs":3,"queue":{"queued":2,"leased":1,"ok":true}}
```

**Collector health** (`GET /collectors`): per-Actor last run, last success, last
failure, counts and success rate (`null` when nothing has finished — distinct
from a 0.0 rate). Derived from the same window.

```bash
curl -H "authorization: Bearer $KEY" localhost:8000/collectors
# {"collectors":[{"collector":"facebook","total":40,"by_status":{...},
#   "success_rate":0.9,"last_run":{...},"last_success_at":"...","last_failure_at":"..."}],...}
```

**Structured logging.** Set `ACTOR_LOG_FORMAT=json` and the worker emits one JSON
object per line to stderr for each lifecycle point — `job_picked_up`,
`job_running`, `job_finished`/`job_completed`, `job_reclaimed`, `job_cancelled` —
each keyed by `run_id` so a run's whole story can be grouped by a log processor.
Unset (the default), the existing human text is byte-for-byte unchanged. The
**Facebook collector is not touched** — the adapter already routes its logs to
stderr.

Everything here is auth-gated exactly like `/runs` (the `read` scope); only
`/health` answers anonymously.

## Dashboard

A first operational dashboard ships as a single self-contained page — vanilla
HTML/CSS/JS, no build step, no dependencies — served by the API itself:

```bash
python3 -m uvicorn api.main:app          # then open http://localhost:8000/
python3 worker/consumer.py               # a worker, so runs actually execute
```

Open `/`, paste an API key into the top bar (held in `sessionStorage`, sent only
as the `Authorization` header), and you get four views, all **read from the
API** — the page holds no platform logic and the Registry stays the source of
truth:

- **Overview** — health pill, queued/leased/active, and run counts by status
  over the metrics window.
- **Collectors** — per-collector last run, last success, last failure, success
  rate (`—` when nothing has finished, distinct from 0%).
- **Runs** — sortable, filterable by status and collector, with created/started/
  finished, duration and exit code.
- **Run detail** — full metadata, input, output, logs and error, with a
  **Cancel** button shown only for `PENDING`/`RUNNING`.

**Auto-refresh** polls the lists every ~5s and a run's detail every ~2s **only
while it is active** (a terminal run stops polling); polling pauses when the tab
is hidden. **Cancel is never optimistic** — it POSTs `/runs/{id}/cancel` and
re-renders whatever the API returns, so `200 already`, a `409` when completion
won the race, and a `403` for a read-only key each show the true outcome. The
shell is public (like a login page); every data call stays auth-gated, and the
page embeds no key.

## Cancellation

```bash
curl -X POST -H "authorization: Bearer $KEY" localhost:8000/runs/<run_id>/cancel
# {"run_id":"...","actor":"...","status":"CANCELLED","already":false}
```

Cancellation is **cooperative through the Registry**, which stays the single
source of truth *and* the channel for the intent. The API only records
`CANCELLED`; it never touches Docker (an architectural rule the tests enforce).
For a **running** container, the worker that owns it runs a watcher thread that
sees the `CANCELLED` state and issues the `docker kill` itself — so the container
is actually stopped, by the process that owns it, not merely marked done. It
works for all three targets:

| target | what happens |
|---|---|
| **queued** (not yet leased) | state → `CANCELLED`; the job stays in the queue but the worker sees a terminal run and acks it without ever starting a container |
| **leased / running** | state → `CANCELLED`; the worker's watcher stops the container (measured: gone in ~1s), then acks |
| **already finished** | `409`, carrying the real terminal status; the result is never overwritten |

It needs the **`run` scope** (an operator action, not a read).

**Idempotent and race-safe.** Cancelling an already-`CANCELLED` run returns
`already: true`. The completion-vs-cancel race is made deterministic by a
per-run advisory file lock (`fcntl.flock`) around the Registry's
read-modify-write: exactly one of "the worker completed it" and "the API
cancelled it" wins, the other is cleanly refused (`InvalidTransition` /
`CancelTooLate`), and the record is always one clean terminal state. Without the
lock, two concurrent writers corrupt `run.json` — verified by reverting it.
Nothing resurrects a cancelled run: it is terminal, so the reconciler never
scans it and the consumer acks its queued job without running it.

Structured events (`ACTOR_LOG_FORMAT=json`): `run_cancel_requested` (API),
`job_cancel_signalled` and `job_cancelled` (worker), all keyed by `run_id`.

## A run asking for help

The full build could pause a run on a challenge only a person can clear and
stream its browser to the console over noVNC. The demo build ships no display
or viewer. What remains is the generic half: an Actor may write
`attention.json` into its run directory, `GET /runs/{id}/attention` reads it,
and `POST /runs/{id}/resume` writes `resume.json` back. Both are
ownership-checked like every other run route and refuse links or special files
(`test_run_dir_links.py`).

## Multi-tenancy

Thirty people share one platform. Each signs up with an email address, username
and password, connects their own social account, and sees only their own runs.

```bash
docker compose --profile db up -d postgres     # the database
export ACTOR_PLATFORM_SECRET_KEY="$(python3 shared/crypto.py)"
export POSTGRES_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
export ACTOR_PLATFORM_DATABASE_URL="postgresql://actorplatform:$POSTGRES_PASSWORD@127.0.0.1:5433/actorplatform"
python3 db/migrate.py                          # apply the schema
python3 tools/import_runs.py                   # adopt existing run directories
```

`ACTOR_PLATFORM_SECRET_KEY` is not optional. It encrypts every stored social
session, and the platform refuses to start without it rather than quietly
writing credentials in the clear.

### Why a database, when the filesystem already worked

Because the questions multi-tenancy asks are queries, not directory scans.
Measured on this deployment's 1,835 run directories:

| | filesystem | postgres |
|---|---|---|
| newest 20 runs | 5.5 ms | 0.6 ms |
| filtered by one Actor (8 of 1,835 match) | **55.8 ms** | **0.3 ms** |
| one user's runs | — | 1.7 ms |
| counts by status over all history | — | 0.4 ms |

The second row is the one that matters: a non-selective filter has to scan
everything to fill one page, and **a per-user listing has exactly that shape**.
That cost would be paid on every poll of every console, forever.

The transition guard also got *stronger*. On the filesystem it is
read-check-write under an `fcntl` lock, "single-host by construction". In
Postgres it is one statement, with the allowed source states coming from the
same `_ALLOWED_TRANSITIONS` table:

```sql
UPDATE runs SET status = %s WHERE run_id = %s AND status = ANY(%s) RETURNING *
```

Zero rows returned is the refusal. **Verified: 25 concurrent
complete-vs-cancel races, exactly one writer winning every time**, the other
cleanly refused, and the record always in one terminal state.

`registry/pg_registry.py` SUBCLASSES `RunRegistry`, so the artifact half is
inherited rather than reimplemented. `run.json` is still written as a local
breadcrumb and is no longer read — the module's old claim that "a run IS its
run.json" stops being true here, so it says so.

### Ownership is a constructor, not a filter

```python
registry.for_owner(user_id)   # a view that carries the predicate on every read
```

A handler cannot forget to scope what it never holds — the same structural
argument `api/schemas.py` makes about `run_dir`. `api/main.py` hands each
handler an already-scoped registry through a dependency.

**A foreign run is `404`, never `403`.** A 403 confirms the id exists. This
covers PREFIX expansion too, which otherwise leaks existence through its error
message ("matches 2 runs" tells you two exist even when neither is yours).

The ~1,800 runs made before accounts existed are imported with `owner_id NULL`:
they belong to nobody and are visible to operators only. Nothing adopts them
later, because adoption would mean guessing.

### Three credentials, three treatments

| | stored as | why |
|---|---|---|
| password | argon2id | human-chosen, so low-entropy: the hash must be slow |
| API key | sha256 of a 256-bit random secret | nothing to brute-force; argon2 on every request would be a self-inflicted rate limit |
| console session | an opaque row id in an HMAC-signed cookie | **revocation** — logout, admin disable, a password change. A self-contained token cannot be revoked, only outlived |
| social session | AES-256-GCM ciphertext | see below |

An API key is `apk_<key_id>_<secret>`. The `key_id` half is public and indexed,
so authenticating is one indexed lookup plus one constant-time compare instead
of a walk over every key on the platform. It is **hex**, not base64url: the key
is split on the first `_`, and urlsafe emits `_` about 12% of the time at this
length — which made roughly one key in eight authenticate at the wrong split
point. Found by a test, not in production.

### Encrypted credentials

`shared/crypto.py` seals a captured social session with AES-256-GCM. The AAD is
`"<social_account_id>|<platform>"`, which binds a ciphertext to the row that
owns it: **copying one row's ciphertext into another's place fails to decrypt**
rather than silently letting one user scrape as another. The key lives in the
environment, never in the database — a dump alone must not be a compromise.
Every row records its `key_version`, so rotation is "add a key, re-seal in the
background, drop the old one" rather than a flag day.

**We never store anyone's social-media password.** There is no column for one
and there must never be one: it would turn a breach of this database into a
breach of thirty real accounts elsewhere. Only the session cookies the user
captured themselves are stored.

### Cross-site protection

A cookie is AMBIENT — the browser attaches it to any request to this origin,
including one a hostile page caused. So a cookie-authenticated **write** must
also prove it came from this app (`Sec-Fetch-Site`, falling back to `Origin`).
Bearer-token requests are exempt, because a token is not ambient and a hostile
page cannot cause one to be sent. Reads are never blocked, since a GET changes
nothing.

`Secure` on the session cookie is set whenever the request arrived over HTTPS
(directly, or through the documented proxy, whose `X-Forwarded-Proto` uvicorn
trusts from 127.0.0.1). `ACTOR_PLATFORM_SECURE_COOKIES=1` forces it, `=0` turns
it off. In public mode (`PUBLIC_DOMAIN=...`, which sets
`ACTOR_PLATFORM_REQUIRE_HTTPS=1`) sign-in and sign-up over plain HTTP are
refused outright, except from loopback; `ACTOR_PLATFORM_ALLOW_INSECURE_HTTP=1`
is the escape hatch for a trusted LAN. *Security review H-05.*

### Security headers

*Security review M-05.* Every response carries a Content-Security-Policy
(scripts only from this origin, websockets only back to this host, framing only
by this origin), `X-Frame-Options: SAMEORIGIN`, `nosniff`, a strict referrer
policy, and a Permissions-Policy; HTTPS responses also carry HSTS for a year.
`/ops` needs inline script and gets it, nothing else does.
A route's own header wins: the artifact route's sandboxing CSP for run-produced
HTML is stricter and is left alone. See `api/security_headers.py`.

### Sign-in throttling

*Security review H-02.* Per address: 20 login attempts per 10 minutes and 5
sign-ups per hour (`ACTOR_PLATFORM_LOGIN_PER_IP`, `ACTOR_PLATFORM_SIGNUPS_PER_IP`).
Per account: 10 consecutive failures (`ACTOR_PLATFORM_LOCK_AFTER_FAILURES`) lock
it for 15 minutes, doubling with each further lockout up to 24 hours; a
successful sign-in resets it. Both answer 429 with `Retry-After`. The account
counter is keyed by what was TYPED, so an unknown name locks exactly like a real
one and reveals nothing. Both checks run before argon2, so a refused attempt
costs a query rather than a hash. Loopback is exempt from the per-address limit
only: behind the proxy every real request carries the client's address. The
Users screen shows a *locked* badge and an *Unlock* action. Stored in Postgres
(migration 005) so a lockout survives a restart. There is no e-mail
verification or CAPTCHA; an admin can disable spam accounts.

### Who may sign up: invite-only by default

*Security re-review: open signup allowed unlimited distinct accounts, 5 per
address per hour.* An account can run scrapes that spend the operator's Claude
credits from the operator's IP, so sign-up is **invite-only** by default
(`api/invites.py`, migration 008). `ACTOR_PLATFORM_SIGNUP` picks the mode:

| value | who can create an account |
|---|---|
| `invite` (default) | someone with a single-use invite link from an admin |
| `open` | anyone (still 5 per address per hour) |
| `closed` | nobody, not even with an invite |

An unrecognised value means `invite`, with a warning: a typo makes sign-up
stricter, never open. Changing it takes a restart, like any setting.

- **The first account needs no invite** (except in `closed`): an empty users
  table is a fresh install, and there is no admin yet to make one. Only one
  person can be that first account; the check runs under an advisory lock. It is
  *not* an admin; promote it with the operator key, as before.
- **Making an invite:** Users screen → *Invites* → *Create invite link*, or
  `POST /admin/invites` (`{"note": "...", "days": 7}`, both optional). Needs an
  admin with the `run` scope. The link is shown **once**: only its sha256 is
  stored, so a lost link is revoked and replaced, not recovered.
- **Expiry:** `ACTOR_PLATFORM_INVITE_DAYS` (default 7, max 30), or `days` per
  invite.
- **Single use:** the invite is spent in the same transaction that creates the
  account. Two people with one link cannot both get in, and a signup that fails
  (email taken, say) does not use it up.
- **Revoking:** *Revoke* on the screen, or `DELETE /admin/invites/{id}`. Revoking
  a used invite changes nothing; disable the account instead.
- **The token stays out of server logs.** A link is `https://<host>/#invite=<token>`.
  The part after `#` is never sent to the server, so uvicorn's access log
  (`api.log`) never sees it. An earlier `?invite=` form was written there in
  full. The console reads the token and removes it from the address bar.
- Guessing tokens is pointless (256 bits) and throttled anyway: the sign-up rate
  limit runs before the invite check.

| Route | Who |
|---|---|
| `GET /auth/signup-options` | anyone; tells the form whether to ask for an invite |
| `GET /admin/invites` | admin, `read` |
| `POST /admin/invites` | admin, `run` |
| `DELETE /admin/invites/{id}` | admin, `run` |

### Administering users

`users.is_admin` and `users.disabled_at` used to be changeable only with SQL;
`api/admin.py` is the API for both. Every route needs an **administrator** (an
admin user's cookie or key, or the operator key from the environment) **and**
the usual scope: `read` to look, `run` to change. An admin's read-only key can
list users and change nothing; admin widens *whose* data a credential reaches,
never *which actions* it may take.

| Route | Does |
|---|---|
| `GET /admin/users?q=&limit=` | users, newest first; `q` matches email or username |
| `GET /admin/users/{id}` | one user, plus live sessions, active API keys and runs |
| `POST /admin/users/{id}/promote` / `demote` | `is_admin` on / off |
| `POST /admin/users/{id}/disable` / `enable` | `disabled_at` set / cleared |

```bash
# A fresh install has no admin: promote the first one with the operator key.
curl -X POST -H "Authorization: Bearer $OPERATOR_KEY" localhost:8000/admin/users/<id>/promote
```

- **Changes take effect on the target's next request.** Every request rebuilds
  the caller from the database, so nobody has to sign out and back in.
- **Disabling stops everything acting as the user**: their console sessions are
  revoked, their `PENDING`/`RUNNING` runs are cancelled (through the same path
  as `POST /runs/{id}/cancel`). Their run history and connected accounts are **kept**.
- **API keys are suspended, not revoked**: refused while the user is disabled,
  working again once enabled. Revoked sessions stay revoked, so a cookie that
  was live at the time does not come back.
- **No lockout.** An admin cannot demote or disable themselves, and no change
  may leave zero *enabled* admins. That count and the write share a transaction
  under an advisory lock, so two admins demoting each other at once cannot both
  succeed. The operator key is not a user and does not count.
- Every write is idempotent (`"already": true` on a repeat) and emits an
  `api user_promoted|user_demoted|user_disabled|user_enabled` event naming who
  did it.

### Which Actors need a session, and which cookies prove it

`config/actors.json`, not code:

```json
"facebook": {
  "session_cookies": ["c_user", "xs"],
  "session_id_cookie": "c_user"
}
```

**Both cookies are required**, and that is the subtle part: the identity cookie
survives a logout, so a jar carrying it without the authentication cookie looks
like a session and is not one — capturing on it stores a login that fails on its
first real use, hours later, as a scrape that hit a login wall.

It lives in the config file because **no Actor may be named in `worker/`,
`api/`, `jobqueue/` or `registry/`**. A test enforces that by grepping every
module outside `actors/` for site names, and it caught a first attempt that put
a `shared/fb_session.py` in the platform. `shared/social_session.py` takes the
names as an argument and knows nothing about any site.

### `/health` tells an operator which half is broken

`auth_backend` and `db_ok` are reported to privileged callers. A database the
platform needs but cannot reach is `degraded` with `db_ok: false` and
`auth: enforced` — not `unconfigured`, which would be a lie: the keys *are*
configured, they just cannot be checked. Authenticated endpoints then fail
closed with 503, never falling through to anonymous access.

An **empty users table is not a misconfiguration** — it is a fresh install, and
signup has to work: the first account needs no invite (see "Who may sign up").

### Tests

```bash
python3 test_facebook.py                     # includes the actor-boundary grep
python3 test_run_dir_links.py                # nothing an Actor plants is followed

export ACTOR_PLATFORM_DATABASE_URL=postgresql://...   # a THROWAWAY database
python3 test_registry_pg.py                  # the registry suite, on Postgres
python3 test_accounts.py                     # accounts, sessions, keys, tenancy
python3 test_authz.py                        # object-level authorization, A <-> B
python3 test_admin_users.py                  # promote/demote/disable/enable
python3 test_invites.py                      # invite-only signup, races included
```

`test_registry_pg.py` does not restate the registry's assertions — it imports
`test_registry.py`, swaps the class its fixture builds, and re-runs it. A
backend swap that needed the tests edited would not have been a swap. It skips
only the seven tests that plant a raw `run.json` and assert how it PARSES, since
a database backend has no such parsing; they are detected from the test source
rather than a hand-maintained list, and the behaviours two of them covered
(ordering, prefix ambiguity) are re-made against rows.

## Per-run credentials

`ACTOR_FORWARD_ENV` hands the same values to every Actor container. That is
right for one user and **wrong for thirty**: it means every run scrapes as
whoever's session the Worker happens to hold. A run now receives the session of
the person who submitted it.

```
API      run records WHICH account it will use (never the session itself)
Worker   reads that account's ciphertext, decrypts it, and hands the value to
         the docker CLI through the CHILD's environment
```

### The value still never enters argv

The executor keeps emitting `-e NAME` with no `=value`; what changed is only
where the value comes from — `subprocess.Popen(env=...)`, a per-child dict
applied at fork/exec.

**Do not "simplify" that into `os.environ[name] = value`.** The Worker is
threaded three ways — the consumer dispatches each job on its own thread, and
the executor adds a reader thread and a cancel watcher per run — so mutating the
process environment races, and the failure mode is **user A's session going into
user B's container**, with nothing in any log to show it. A test runs two
threads resolving 15 sessions each and asserts neither ever sees the other's.

Verified end to end against a real container: the secret **reaches** the
container (proven by echoing it back) and appears in **none** of `run.json`,
`input.json`, `output.json`, `log.txt`, any queue file, or the `runs` table.

Log lines are redacted before they are stored or echoed. No Actor prints its
session deliberately, but `log.txt` is served over HTTP by `GET /runs/{id}/logs`
and a traceback through a large vendored collector could carry a cookie header.

### The session write-back, and the bug it fixes

**Every stored session was going to decay to death.** A site rotates `xs`
server-side *during* a session and the collector writes the refreshed jar back
to `$FB_DATA_DIR/fb_auth.json` — which the Actor sets to a `mkdtemp` inside a
`--rm` container. The refresh was destroyed when the container exited, so a
working account died after a few weeks of scheduled runs, presenting as the site
blocking you rather than as a bug.

The Worker now bind-mounts a **private 0700 directory at `/actor/secrets`**,
outside `storage/runs/`, and sets `ACTOR_SESSION_OUT`. The Actor writes its
refreshed jar there; the Worker re-encrypts it into the account row and
`rmtree`s the directory in a `finally`, so a live session never outlives the
container that used it.

**Not `storage/runs/<id>/`** — that is the tree `GET /runs/{id}/…` serves from.
A live session written there would sit in the platform's public artifact tree,
one route addition away from being served.

**A logged-out jar is refused, on both sides.** A run that ended on a login wall
holds the identity cookie without the authentication cookie; storing it would
replace a good session with a dead one and turn one bounced run into "log in
again". The Actor checks before exporting, and the platform checks again —
because an Actor is not a trusted component.

### The third mount: state that outlives the container

A stored cookie jar is not the whole of an identity. An Actor that drives a real
browser also accumulates a **browser profile** — local storage, IndexedDB, the
device-bound cookies a site writes when it has checked you once — and that lives
on disk, not in a jar. Rebuilt per run, it means arriving as a brand new device
every time, which is precisely what a site's anti-bot is looking for.

So an Actor may declare `persistent_state: true`, and the Worker then bind-mounts

    ~/.actor-platform/state/<actor>/<social_account_id>/   ->   /actor/state

setting `ACTOR_STATE_DIR`. It differs from the credential mount above in exactly
one way, which is its whole purpose: **it is not deleted when the run ends.**

Four properties, each deliberate:

* **Per connected account, never per Actor.** State built while acting as one
  account must never be handed to another, so the mount is the account's own
  leaf directory — never a parent. Both path segments are sanitised to a single
  segment, so an account id cannot walk the mount up to the directory holding
  everyone's.
* **No account, no mount.** A run that names no connected account gets none,
  rather than a shared directory that every anonymous run would write into.
* **Outside `storage/`**, for the reason the credential mount gives above: this
  directory holds an authenticated browser profile, and `storage/runs/` is what
  the API serves. It is 0700, and so is the Actor directory above it, so another
  user on the host cannot even list which accounts are connected.
* **Serialised by `concurrency_scope`.** A browser profile directory tolerates
  exactly one live browser. The Actors that ask for persistent state are already
  capped at one run per connected account, which is what makes that safe.

Opt-in throughout: with the flag absent the container argv is byte-identical to
what it has always been, and no Actor that has not asked for it is affected.

### Failures are told apart

| what happened | reported as |
|---|---|
| the credential could not be produced (missing, undecryptable, disconnected) | `SessionUnavailable` → `PLATFORM_ERROR`. **Not** "your session expired": our injection broke, and telling the user otherwise costs them a real login |
| the Actor ran and found the session dead | recorded against the account, debounced |

An Actor reports a dead session by setting **`"session_failure": "<reason>"`**
at the top level of `output.json`. The field is generic -- the platform names no
site, so what "dead" looks like is the Actor's call. When it is present the
Worker records the failure against the account and does **not** store the
session the run ended with, which would both keep a dead jar and, as a positive
check, reset the counter just incremented. (Until 2026-09-21 nothing called the
recording hook at all, so no account was ever flagged.)

Only the **measured** signal (`"expired"`) expires an account on one occurrence;
any softer reason needs two in a row. A successful run is itself a positive check, so it
clears the failure counter and refreshes `last_verified_at`.

### Refusing before the run exists

`POST /actors/{name}/runs` returns **409** when the caller has no connected
account for the platform that Actor needs, or when theirs needs reconnecting —
**before** `registry.create()`. A queued run that can never be given a
credential is worse than no run: it occupies the queue, fails opaquely minutes
later, and costs a run to learn something we knew at submission. 409 rather than
400 because the request is well-formed; it is the account state that is wrong.

### Two configuration guards

`config/actors.json` declares `session_env`, `session_out` and
`requires_social`. The Worker **refuses to start** if `ACTOR_FORWARD_ENV`
declares a name that is also an Actor's `session_env` — the forwarded value
would be inherited by the child environment and either shadow the per-run
credential or, worse, be used when none was resolved. It checks the
DECLARATION, not the currently-set variables: a name that happens to be unset
today is still a misconfiguration, and exporting it tomorrow would silently hand
one person's session to everybody's runs.

`session_env` without `session_cookies` is also refused: the platform would be
injecting a credential it has no way to validate, and a logged-out jar would be
indistinguishable from a good one at every layer.

```bash
ACTOR_PLATFORM_DATABASE_URL=postgresql://...   # a THROWAWAY database
python3 test_secrets.py
```

## One run per connected account

The Facebook Actor used to carry `max_concurrency: 1`. That was the wrong shape
twice over.

**The real constraint is per SESSION, not per Actor.** The site rotates its
session cookie server-side during a run, so two scrapes on *the same* session
race the write-back and make the account look automated. Two scrapes on two
different people's sessions do not interact at all. Capping the Actor throttled
thirty users in order to serialise something that only ever applied within one
account.

**And it did not even do that.** `_Slots._actor_running` is an in-process dict,
so two `worker/consumer.py` processes on one host would both admit a Facebook
run — the guarantee the config comment claimed was not the one the code
provided.

So the cap is now `concurrency_scope: "social_account"`, enforced in two layers.

### Layer 1 — the eligibility predicate (fast path)

`Job` carries an opaque `concurrency_key`; the consumer's counter declines it
before the job is ever **leased**. That matters: a job it rejects stays plainly
`PENDING`, where another Worker and the reconciler can both still see it. The
alternative — leasing and handing back — burns leases and turns a saturated key
at the head of the queue into a spin.

The key is opaque all the way down. The queue and the executor only ever
*compare* it, so neither has to learn that the thing being serialised is a login.

### Layer 2 — the database lease (correctness)

A `PRIMARY KEY` on `session_leases.social_account_id`: a second `INSERT`
conflicts, so **Postgres decides**, not a check-then-act in application code.
This is what layer 1 cannot do, being one process.

Taken **before** `mark_running`, deliberately: a run that loses the lease has
changed nothing and its job goes straight back to the queue, rather than being
`RUNNING` and needing to be walked back.

A lease whose owner is provably dead is reclaimed, reusing the queue's own
`<host>:<pid>` convention and `owner_is_dead()` — otherwise one killed Worker
would strand an account for ever. Releasing is scoped to the releasing **run**,
so a late release after our lease was reaped cannot steal the one another run
has since taken.

### Losing the race must NACK, never ack

`ack` means *finished*: acking a job that never ran would silently discard it.
Letting `recover()` reclaim it instead would burn an attempt against
`MAX_ATTEMPTS` for work that never started, and three of those give up on the
run altogether. Hence the third verb. The same fix applies to the
previously-unreachable "lost the race for a slot" branch, which used to return
early and leave the job leased and invisible.

### The global ceiling was NOT raised

`global.max_concurrency` stays at 6, and that is a decision rather than an
oversight. Removing the per-Actor cap means thirty users can now queue thirty
scrapes and this is the only thing still bounding them — so the temptation is to
raise it to match. **There is no measurement to raise it against**: a scrape
container's peak RSS has never been recorded, and an unauthenticated probe exits
at the login wall in under a second, which measures nothing. Guessing high
OOM-kills the host. Users seeing "queued" is honest and is what the queue is for.

## Connecting an account

```
POST   /social/login               start a (demo) login for this user
GET    /social/login/{id}          ready / signed in?
POST   /social/login/{id}/capture  "I've finished logging in"
DELETE /social/login/{id}          close it
```

**Demo build.** In the full build these routes drove a per-user browser
container streamed into the console over noVNC, and `/capture` took the cookie
jar it produced. Here a login is an in-memory record that is signed in at once,
and `/capture` stores a **placeholder** jar: each cookie the platform's policy
requires (`session_cookies` in `config/actors.json`) with the value `"demo"`.

Everything after that is the real path. The jar is checked with the same
`is_logged_in` rule, sealed with AES-GCM under the account row's id as
associated data (so a ciphertext cannot be moved to another row), and stored in
`social_accounts`. A run of an Actor that `requires_social` is then accepted,
leases that account, and gets the decrypted jar injected as its `*_SESSION`
variable. Someone else's login is `404`, never `403`.

## The report

A run no longer stops at data. It produces an executive report — text, a PDF and
an HTML dashboard — analysed by Claude against the user's own brief, and e-mails
it to them.

```
collect  ->  process  ->  analyse  ->  render  ->  e-mail
(actor)     (actor)      (actor)      (actor)     (worker)
```

Verified end to end on a live scrape: 8 posts from a real page, one Claude call
with a custom brief, and a 3-page PDF, a 13 KB dashboard and a text report on
disk.

### The user's prompt is APPENDED, not substituted

The response is constrained by a `json_schema` and the report layer renders those
exact fields. A free-form prompt would still have to produce that shape, so the
only thing substitution could change is the content of fields the model did not
know it had to fill — which reads as an ignored prompt. Appending gives real
influence over emphasis while the report always renders.

The user's text is **delimited and labelled** as theirs. The data below it is
captions and comments written by strangers, and the model has to be able to tell
an operator's instruction from a comment that says "ignore your instructions".
That does not make injection impossible — nothing in a single prompt does — but
an unlabelled concatenation makes it easy.

An empty `ai_prompt` produces the byte-identical request this made before the
parameter existed, which is what keeps the CLI unchanged.

### Analysis cannot cost you a scrape

A scrape is minutes to hours; the analysis is two API calls. Losing the first
because the second failed would be absurd, so **every failure is caught** and
returned as `analysis.error` with the run still succeeding and its data intact.
Rendering fails separately from analysis, so a report that cannot be drawn does
not discard an analysis that succeeded.

### The whole pipeline layer is vendored, not reimplemented

`sync_collector.sh` now copies `processors/`, `ai/` and `reports/` alongside the
collector, and the drift test holds them to the same rule: **edited copies fail
the build**. Fix upstream and re-sync.

`reports/` is already platform-generic — it takes a `platform` string — so
vendoring it once is what lets the next six Actors reuse it instead of each
growing their own report layer.

**The text/PDF renderer is vendored under the Actor's own name.** Upstream it
is `reports/facebook_report.py`, which is Facebook-named only because Facebook
came first — so `instagram/vendor/reports/facebook_report.py` reads as the wrong
renderer having been shipped. `sync_collector.sh` renames it to
`<platform>_report.py`; the bytes are untouched and the drift test maps the name
back before comparing, then asserts the rename happened. Upstream also gained
the neutral aliases `format_combined_text_report` / `generate_combined_pdf`, so
an Actor's import line no longer names two platforms at once.

**`processors/` is the exception: an Actor vendors only its OWN pair.** Upstream
holds twelve (six platforms × page/comments) and each `actor.py` imports exactly
two, so the other ten were dead weight in every image — and an image carrying
another platform's reshaping code invites someone to import it. No processor
imports another, so two is the whole closure. The drift test asserts the
vendored set is **exactly** that pair, in both directions: a missing one breaks
the image, and an extra one means the trim has quietly stopped happening.

### Artifacts

`GET /runs/{id}/artifacts` and `/artifacts/{name}`, owner-scoped like every
other run route. **Report artifacts are named after the Actor** —
`tiktok_report.pdf`, `facebook_dashboard.html` — because they are fetched by
name and downloaded into one folder, where six Actors all writing `report.pdf`
give a person files whose names say nothing about which scrape produced them.
Each `actor.py` sets `ARTIFACT_PREFIX`, and its test fails on a bare
`report.pdf` / `report.txt` / `dashboard.html` string, since a revert here is
silent: the run still succeeds and the console still lists three artifacts. **The name is matched against what the directory actually
contains** rather than joined onto a path and then checked, so traversal is not
something the route has to sanitise — `../../etc/passwd` is simply not in the
listing.

### The WORKER sends the e-mail, not the Actor

Three reasons, not one: SMTP credentials would otherwise be handed to a
container running vendored third-party code; the recipient's address would have
to travel in `input.json`, which is served over HTTP; and delivery would be
unobservable — `report_sent_at` belongs beside the run, not inside it.

The recipient is the address the user signed up with, resolved at submission and
stored on the run. `report_sent_at` also makes delivery **once-only**: the queue
is at-least-once, so a finished job can arrive twice, and nobody wants the same
report twice.

**An unconfigured or unreachable mailer is not an error.** The artifacts are
already on disk and reachable through the API, so an unsent e-mail is a missing
convenience, not a lost result — a forty-minute scrape must never be reported as
failed because a mail server was down.

```bash
export ACTOR_PLATFORM_SMTP_USER=you@gmail.com
export ACTOR_PLATFORM_SMTP_PASSWORD='an app password, not your account password'
```

## The console

The user-facing app: sign up, connect an account, run a scrape with every option
as a field, watch it, and read the report. React + TypeScript, built to static
files the API serves from its own origin.

```bash
cd ui/console && npm install && npm run build   # -> ui/static/
python3 -m uvicorn api.main:app                 # serves it at /
```

`/` is the console. The operator dashboard moved to **`/ops`** — thirty people
arriving at the root of this service should meet the product, not an internal
tool asking for an API key they do not have.

### Same origin, no token in the browser

The console holds **no credential**. Authentication is the HttpOnly session
cookie, which its own JavaScript cannot read — so there is nothing in the bundle
to steal. Being same-origin is what makes that work with no CORS and no second
service to expose, and `Sec-Fetch-Site` (set by the browser, not by us) is the
whole CSRF story from the frontend's side: there is no token to thread through
and no way for the client to weaken it.

### The run form is generated from the Actor's schema

Not hand-written per Actor. The schema is what the API validates against, so a
form built from it cannot drift into offering a field the server rejects, and
**adding a field to an Actor makes it appear with no frontend change** — which
is what will make the next six platforms cheap.

Every option is a field, answerable in any order. The CLI asks these as a fixed
sequence of questions; a form does not have to, and answering out of order is
most of why someone wants a screen. Date inputs are inferred from the schema's
own `pattern`, so a new date field gets a picker for free. The post count
**disables itself** when a date range is set, because the schema says in prose
that a count cuts the window short — a form can enforce what prose can only ask.

### A bug the browser found that the tests did not

**Polling skipped the first read.** The visibility guard meant a screen mounting
while `document.hidden` never fetched its initial state at all. The first read
is now never skipped, and a tab returning to the front catches up immediately
instead of waiting out the interval.

## Current limitations

Known and deliberately deferred, not oversights:

| | |
|---|---|
| **Concurrency is per Worker, EXCEPT per-session** | Each Worker enforces its own global `--concurrency` and each Actor's `max_concurrency`, and nothing coordinates those *across* Workers. The one limit that IS cross-process is per connected account, which holds a real database lease — see [One run per connected account](#one-run-per-connected-account). |
| **One reconciler** | Two sweeping concurrently could both requeue the same run — harmless today (the consumer acks the duplicate without re-running) but unsound as a design. |
| **Liveness is host-local, and the visibility timeout is off** | A lease records `<host>:<pid>`; a dead pid is reclaimed in seconds on the same host. The age-based visibility timeout is disabled by default (so a healthy hours-long run is never reclaimed), which means a lease left by a dead process on **another** host is not reclaimed until an operator sets one or a heartbeat is added. Single-host today. |
| **No heartbeat** | A Worker that is wedged rather than dead looks healthy: its lease is fresh and its job is in the queue, so neither recovery nor reconciliation fires. |
| **Filesystem queue snapshot race** | `known_run_ids()` reads two directories in sequence. Under pathological churn a job can be missed, which would requeue a live run. Mitigated by a per-candidate re-check (96.5% reduction measured) and made harmless by the consumer's idempotency; the real fix is a single-directory queue layout. |
| **At-least-once, so duplicates happen** | A Worker that finishes and dies before acking will see the job again. Consumers must be idempotent; `worker/consumer.py` is. |
| **Scopes are coarse** | Two of them. No per-Actor or per-run grants. There are now users and per-user keys (revocable without a restart), but only two roles: a user, and an operator who sees everything. |
| **Local access bypasses auth** | `cli/runs.py` reads run directories from disk directly and does not know about ownership; authentication protects the HTTP surface only. Stored social sessions are safe regardless -- they are encrypted in the database, not on disk. |
| **Forwarded secrets are platform-wide** | `ACTOR_FORWARD_ENV` still hands every Actor container the same values, and there is no per-Actor scoping or rotation for them. Per-RUN credentials are separate and DO exist (see [Per-run credentials](#per-run-credentials)); the two mechanisms coexist, and the Worker refuses to start if they collide. |
| **CPU/RAM limits are per Worker, not per Actor** | `--cpus`/`--memory` apply to every container a Worker starts. Per-Actor **concurrency** is configured (`config/actors.json`); per-Actor CPU/RAM is not, deliberately — there are no production measurements to size it from. `WorkerConfig.limits_for(actor)` is where such profiles would attach. |
| **A reconcile sweep pauses dispatch** | The sweep runs on the dispatch thread, deliberately: its safety argument is that a live run holds a lease, and the one moment that is briefly untrue is between taking a slot and claiming a job. Sweeping there cannot land in that window — at the cost of no dequeues while it runs. |
| **No admission control** | `--concurrency` bounds containers, not the total CPU or memory they may claim. Four containers at `--cpus 4` can still oversubscribe the host; the operator sizes the two flags together. |
| **`run.json` embeds the input** | The input is small and bounded by the Actor's schema, so it stays in the record. The output no longer does. |

## Adding an Actor

1. `actors/<name>/` with `actor.py`, `Dockerfile` and its own `requirements.in`,
   locked into `requirements.txt` with `python3 scripts/image_locks.py lock <name>`
   (every package pinned with its hashes; the Dockerfile installs only that, with
   `pip install --require-hashes --no-deps -r requirements.txt`). Add the directory
   to an image entry in `.github/dependabot.yml`; `test_actor_images.py` checks
   all of this. The name doubles as a Docker image path component, so it must be lowercase
   alphanumerics separated by `.`, `_`, `__` or `-` (max 64 chars). The worker
   checks this before doing anything, so a bad name is rejected with exit `2`
   instead of failing later as `invalid reference format` inside the build.
2. Read the input path from `ACTOR_INPUT_PATH`, write the output path from
   `ACTOR_OUTPUT_PATH`, log to stdout, exit non-zero on failure. Validate your
   own input: the platform may not be involved.
3. Optionally add `input_schema.json` so callers get told what to send, and bad
   requests are refused before a container starts.
4. `python3 worker/executor.py <name> --input '{...}'` — no worker changes needed.

[`actors/hello_world/actor.py`](actors/hello_world/actor.py) is the reference
implementation; the contract handling is the top half of the file and the work
is the six lines in `run()`.


## The Facebook Actor

A real scraper — public Facebook pages and groups, posts and optionally comments,
with the full date-window machinery — running as a generic Actor. It is an
**adapter**, not a rewrite: the collector lives in a separate pipeline
repository and is vendored in verbatim.

```bash
# 1. vendor the collector into the build context (Docker can't COPY outside it)
./actors/facebook/sync_collector.sh [path-to-pipeline-repo]
docker build -t actor-platform/facebook:latest actors/facebook/

# 2. the session is forwarded BY NAME, never baked into the image or the input
export FB_SESSION="$(cat data/facebook/fb_auth.json)"   # any storage_state JSON
python3 worker/consumer.py --actor-config config/actors.json &   # ACTOR_FORWARD_ENV=FB_SESSION

# 3. submit through the generic API
curl -X POST localhost:8000/actors/facebook/runs \
     -H "authorization: Bearer $KEY" -H 'content-type: application/json' \
     -d '{"target":"nasa","start_date":"2026-08-01","end_date":"2026-08-31"}'
```

**Input** (`actors/facebook/input_schema.json`): `target` (required — a page name,
page URL, or group), `max_posts`, `start_date`/`end_date` (a window the run
*covers*, so omit `max_posts` when using it), `collect_comments`,
`max_comments_per_post`, `comment_sort`, `page_concurrency`.

**Output**: the collector's own document shape, passed through unchanged —
`page_information`, `posts`, and the full `date_window` accounting
(`stop_reason`, `complete`, `window_reached`, reach). Comments, when collected,
are attached to their post with their own accounting. The adapter invents no
second data model.

**Session** comes from `FB_SESSION` (a Playwright `storage_state` document or a
`c_user=...; xs=...` cookie string), forwarded through the platform's existing
by-name secrets mechanism. Verified: the value never reaches `run.json`,
`output.json`, `log.txt`, `input.json`, the queue job or the run directory.

**Exit codes** follow the platform contract exactly. `0` succeeded — including
**0 posts in a valid window**, which is a real answer, not a failure. `1` for an
Actor-level failure (dead session, page unavailable). `2` is the platform's, never
the Actor's. A scrape may run for hours -- up to this Actor's maximum run time
(*Per-run limits*) -- and keeps its concurrency slot for the whole time.

**Concurrency** is `max_concurrency: 1` in `config/actors.json`, enforced by the
platform — the scraper holds one authenticated session that two concurrent runs
would race. It is policy, not code: nothing in the vendored collector implements
a cap.

The boundary is enforced by tests: the vendored modules are byte-identical to the
pipeline repo (or the drift test fails), the adapter contains no scraping
machinery (AST-checked), and no platform module names an Actor (AST-checked). To
refresh the scraper, change it upstream and re-run `sync_collector.sh` — never
edit anything under `actors/facebook/vendor/`.
