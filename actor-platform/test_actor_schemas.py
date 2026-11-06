#!/usr/bin/env python3
"""Actor input schema tests.

An Actor may declare an input_schema.json. The platform reads it to reject bad
input at submission rather than letting a container fail seconds later with an
opaque exit 1.

The load-bearing claim, and the one most of these tests exist to protect: the
schema is an EARLY FILTER, not a replacement for the Actor's own validation. An
Actor can be started by `docker run` with no platform involved, so it stays
responsible for what it was given.

Mostly offline; two tests need Docker.

    python3 test_actor_schemas.py
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
VENV = REPO_ROOT / ".venv"
VENV_PYTHON = VENV / "bin" / "python"

if (
    VENV_PYTHON.is_file()
    and Path(sys.prefix).resolve() != VENV.resolve()
    and not os.environ.get("_ACTOR_PLATFORM_REEXEC")
):
    os.environ["_ACTOR_PLATFORM_REEXEC"] = "1"
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from api.auth import AuthPolicy, generate_key  # noqa: E402
from api.main import create_app  # noqa: E402
from catalog import actors as catalog  # noqa: E402
from jobqueue.base import open_queue  # noqa: E402
from registry.run_registry import RunRegistry  # noqa: E402
from shared.models import RunStatus  # noqa: E402

# The executor tests run real runs through the shared registry, as a direct CLI
# invocation genuinely does. Everything submitted over HTTP goes to a throwaway
# one instead: these tests deliberately create runs that are never executed, and
# a stranded run in shared storage is picked up later by another suite's Worker.
REGISTRY = RunRegistry()
EXECUTOR = REPO_ROOT / "worker" / "executor.py"

_SANDBOX = Path(tempfile.mkdtemp(prefix="actor-schema-tests-"))
atexit.register(shutil.rmtree, _SANDBOX, ignore_errors=True)

API_REGISTRY = RunRegistry(_SANDBOX / "runs")
QUEUE = open_queue(str(_SANDBOX / "queue"))

# Every suite runs against an ENFORCING policy and sends a real key. Turning
# auth off for the tests would leave the mode nobody actually deploys as the
# only one ever exercised.
API_KEY = generate_key()
AUTH = {"Authorization": f"Bearer {API_KEY}"}
CLIENT = TestClient(
    create_app(registry=API_REGISTRY, queue=QUEUE, auth=AuthPolicy.for_keys({API_KEY})),
    headers=AUTH,
)

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def submit(actor: str, payload):
    return CLIENT.post(f"/actors/{actor}/runs", json=payload)


def running_containers() -> set[str]:
    """Actor containers alive right now, so a test can prove none was started."""
    result = subprocess.run(
        ["docker", "ps", "--filter", "name=actor-", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    return {line for line in result.stdout.split() if line}


@contextmanager
def fake_actors(**schemas: str | None):
    """A throwaway actors/ tree, for cases the real Actors cannot express."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name, schema in schemas.items():
            actor = root / name
            actor.mkdir()
            (actor / "Dockerfile").write_text("FROM scratch\n")
            if schema is not None:
                (actor / "input_schema.json").write_text(schema)
        yield root


def raises(exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        return exc
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"expected {exc_type.__name__}, got {type(exc).__name__}: {exc}")
    raise AssertionError(f"expected {exc_type.__name__}, nothing was raised")


# --- discovery --------------------------------------------------------------


@test
def test_actors_describe_themselves():
    body = CLIENT.get("/actors").json()
    actors = {a["name"]: a for a in body["actors"]}

    for name in ("hello_world", "item_processor"):
        actor = actors[name]
        assert actor["description"], f"{name} has no description"
        assert actor["schema_error"] is None, actor["schema_error"]

        schema = actor["input_schema"]
        assert schema is not None, f"{name} declares no input schema"
        assert schema["type"] == "object"
        assert schema["required"], f"{name} declares no required fields"

    # The schema is the Actor's own file, passed through verbatim.
    on_disk = json.loads((REPO_ROOT / "actors" / "hello_world" / "input_schema.json").read_text())
    assert actors["hello_world"]["input_schema"] == on_disk


@test
def test_an_actor_without_a_schema_accepts_anything():
    """A schema is optional; requiring one would break every existing Actor."""
    with fake_actors(schemaless=None) as root:
        actor = catalog.find_actor("schemaless", root=root)
        assert actor.input_schema is None
        assert actor.schema_error is None
        # Whatever it is handed, the catalog has no opinion.
        for payload in ({}, {"anything": [1, 2, 3]}, {"deeply": {"nested": True}}):
            catalog.validate_input(actor, payload)


# --- validation at submission ----------------------------------------------


@test
def test_valid_input_creates_a_run_and_a_queue_job():
    QUEUE.purge()
    response = submit("hello_world", {"name": "Schema"})

    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]

    # All three must be true, not just the status code: the run is registered,
    # the job is queued, and the input was stored exactly as submitted.
    run = API_REGISTRY.get(run_id)
    assert run.status is RunStatus.PENDING
    assert run.actor == "hello_world"
    assert API_REGISTRY.get_input(run_id) == {"name": "Schema"}
    assert QUEUE.depth() == 1, "a valid submission must be queued"
    assert run_id in QUEUE.known_run_ids()

    QUEUE.purge()
    # Leave nothing stranded for a later Worker's reconcile sweep to adopt.
    API_REGISTRY.complete(
        run_id, status=RunStatus.PLATFORM_ERROR, error="dequeued by the test"
    )


@test
def test_a_missing_required_field_is_rejected_before_anything_happens():
    QUEUE.purge()
    before = len(API_REGISTRY.list(limit=200))

    containers_before = running_containers()

    response = submit("hello_world", {})
    assert response.status_code == 400, response.text
    containers_after = running_containers()

    body = response.json()
    assert body["error"] == "invalid actor input"
    assert any("'name' is a required property" in v["message"] for v in body["violations"]), body

    # The whole point: nothing was started, at any layer.
    assert QUEUE.depth() == 0, "a rejected input must never reach the queue"
    assert QUEUE.inflight() == 0
    assert len(API_REGISTRY.list(limit=200)) == before, "no run record may be created"
    assert containers_after == containers_before, (
        f"a container was started for a rejected input: {containers_after - containers_before}"
    )


@test
def test_wrong_types_are_rejected_with_the_offending_field_named():
    QUEUE.purge()
    response = submit("item_processor", {"count": "five"})
    assert response.status_code == 400

    body = response.json()
    violation = body["violations"][0]
    assert violation["path"] == "count", body
    assert "not of type 'integer'" in violation["message"]
    assert QUEUE.depth() == 0


@test
def test_out_of_range_values_are_rejected():
    QUEUE.purge()
    for payload, field in (
        ({"count": 0}, "count"),
        ({"count": 5000}, "count"),
        ({"count": 1, "delay_ms": -1}, "delay_ms"),
    ):
        response = submit("item_processor", payload)
        assert response.status_code == 400, f"{payload} was accepted"
        assert response.json()["violations"][0]["path"] == field, response.json()
    assert QUEUE.depth() == 0


@test
def test_a_misspelled_field_is_caught_rather_than_silently_ignored():
    """The failure this milestone is really for: a typo the Actor would ignore."""
    QUEUE.purge()
    response = submit("hello_world", {"nmae": "Basel"})
    assert response.status_code == 400

    messages = " ".join(v["message"] for v in response.json()["violations"])
    assert "required property" in messages, "the missing real field must be reported"
    assert "Additional properties" in messages, "the typo itself must be reported"
    assert QUEUE.depth() == 0


@test
def test_every_violation_is_reported_not_just_the_first():
    QUEUE.purge()
    response = submit("item_processor", {"count": 0, "delay_ms": "fast", "bogus": 1})
    assert response.status_code == 400

    paths = {v["path"] for v in response.json()["violations"]}
    assert {"count", "delay_ms", "(root)"} <= paths, paths
    assert QUEUE.depth() == 0


@test
def test_booleans_are_not_integers():
    """The Actors guard against this by hand; the schema must agree."""
    QUEUE.purge()
    response = submit("item_processor", {"count": True})
    assert response.status_code == 400, "True passed as an integer"
    assert response.json()["violations"][0]["path"] == "count"


@test
def test_null_is_allowed_where_the_actor_allows_it():
    QUEUE.purge()
    response = submit("item_processor", {"count": 2, "fail_at": None})
    assert response.status_code == 202, response.text
    QUEUE.purge()


@test
def test_unknown_fields_are_rejected_deliberately():
    """DECISION: unknown fields are REJECTED, via additionalProperties: false.

    The alternative -- ignoring them, as both Actors' own parsers do -- means a
    typo or a stale client field is silently dropped and the run does something
    other than what was asked. Rejecting turns that into an immediate 400. The
    cost is real and accepted: adding an input field to an Actor now requires
    updating its schema in the same commit, or valid requests start failing.
    """
    for actor in ("hello_world", "item_processor"):
        schema = json.loads((REPO_ROOT / "actors" / actor / "input_schema.json").read_text())
        assert schema.get("additionalProperties") is False, (
            f"{actor} does not declare the decision; unknown-field handling must not be accidental"
        )

    QUEUE.purge()
    response = submit("hello_world", {"name": "Basel", "surname": "Extra"})
    assert response.status_code == 400, "an unknown field must be refused, not ignored"

    messages = " ".join(v["message"] for v in response.json()["violations"])
    assert "surname" in messages, "the offending field must be named"
    assert QUEUE.depth() == 0


@test
def test_item_processor_structure_is_validated():
    """The second Actor's full input shape, accepted and rejected correctly."""
    QUEUE.purge()

    # Every optional field, all valid.
    full = {"count": 3, "fail_at": 2, "label": "Batch", "delay_ms": 10}
    accepted = submit("item_processor", full)
    assert accepted.status_code == 202, accepted.text
    run_id = accepted.json()["run_id"]
    assert API_REGISTRY.get_input(run_id) == full, "the input must be stored verbatim"
    QUEUE.purge()
    API_REGISTRY.complete(run_id, status=RunStatus.PLATFORM_ERROR, error="dequeued by the test")

    # Only `count` is required; the rest may be omitted entirely.
    minimal = submit("item_processor", {"count": 1})
    assert minimal.status_code == 202, minimal.text
    QUEUE.purge()
    API_REGISTRY.complete(
        minimal.json()["run_id"], status=RunStatus.PLATFORM_ERROR, error="dequeued by the test"
    )

    # Structurally wrong in each field, one at a time.
    for payload, field in (
        ({"count": [3]}, "count"),
        ({"count": 3, "label": ""}, "label"),
        ({"count": 3, "fail_at": "two"}, "fail_at"),
        ({"count": 3, "delay_ms": 1.5}, "delay_ms"),
    ):
        rejected = submit("item_processor", payload)
        assert rejected.status_code == 400, f"{payload} was accepted"
        assert rejected.json()["violations"][0]["path"] == field, rejected.json()
    assert QUEUE.depth() == 0


@test
def test_a_body_that_is_not_an_object_uses_the_same_error_shape():
    """Every input failure answers in one envelope.

    Declaring the body as a dict would make FastAPI reject a non-object with its
    own 422 `detail[]`, so this one kind of bad input would look different from
    every other kind.
    """
    QUEUE.purge()
    for body in ([1, 2, 3], "a string", 42, True):
        response = submit("hello_world", body)
        assert response.status_code == 400, f"{body!r} -> {response.status_code}"

        payload = response.json()
        assert payload["error"] == "invalid actor input", payload
        assert payload["violations"], "the envelope must carry violations like any other"
        assert "must be a JSON object" in payload["violations"][0]["message"]
    assert QUEUE.depth() == 0


# --- the API depends only on abstractions -----------------------------------


@test
def test_the_api_imports_no_execution_or_storage_implementation():
    """Checked by IMPORTING it, not by reading it.

    A source scan misses transitive imports, which is exactly what went wrong
    before: the API imported worker.executor for one helper and dragged the
    whole Docker execution module in behind it.
    """
    probe = (
        "import sys; sys.path.insert(0, '.')\n"
        "import api.main\n"
        "import json\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    loaded = set(json.loads(result.stdout))

    for banned in (
        "worker.executor",      # Docker execution
        "worker.consumer",
        "worker.reconciler",
        "jobqueue.redis_queue",  # a specific queue implementation
        "docker",
        "redis",
    ):
        assert banned not in loaded, f"the API pulled in {banned}"

    # No Actor implementation either: an Actor is a container, not an import.
    assert not [m for m in loaded if m.startswith("actors")], "an Actor module was imported"

    # And its own source names no implementation or shell-out.
    source = (REPO_ROOT / "api" / "main.py").read_text()
    for forbidden in ("import subprocess", "worker.executor", "redis", "file_queue"):
        assert forbidden not in source, f"api/main.py references {forbidden}"

    # jobqueue.file_queue IS loaded, and that is correct: the API asks
    # open_queue() for whichever queue is configured. It never names one.
    assert "jobqueue.base" in loaded
    assert "from jobqueue.base import" in source


# --- ordering of the checks -------------------------------------------------


@test
def test_actor_name_is_judged_before_input():
    """A bad name and a bad body must not be confused for one another."""
    QUEUE.purge()
    before = len(API_REGISTRY.list(limit=200))

    invalid = submit("_broken_probe", {})
    assert invalid.status_code == 400
    assert invalid.json()["error"] == "invalid actor name", invalid.json()

    unknown = submit("facebook-scraper", {})
    assert unknown.status_code == 404
    assert unknown.json()["error"] == "unknown actor", unknown.json()

    assert QUEUE.depth() == 0
    assert len(API_REGISTRY.list(limit=200)) == before


# --- broken schemas ---------------------------------------------------------


@test
def test_an_unreadable_schema_is_surfaced_not_ignored():
    """Silently treating a broken schema as "no schema" would switch validation
    off without anyone noticing."""
    with fake_actors(broken="{ this is not json") as root:
        actor = catalog.find_actor("broken", root=root)
        assert actor.input_schema is None
        assert actor.schema_error and "could not be read" in actor.schema_error

        # And it refuses to validate rather than waving input through.
        raises(catalog.InvalidActorSchema, catalog.validate_input, actor, {"anything": 1})


@test
def test_a_schema_that_is_not_a_valid_json_schema_is_surfaced():
    with fake_actors(bad='{"type": "obljekt", "properties": 5}') as root:
        actor = catalog.find_actor("bad", root=root)
        assert actor.input_schema is None
        assert actor.schema_error and "not a valid JSON Schema" in actor.schema_error
        raises(catalog.InvalidActorSchema, catalog.validate_input, actor, {})


@test
def test_a_broken_schema_does_not_break_the_listing():
    with fake_actors(healthy='{"type": "object"}', broken="not json") as root:
        listed = {a.name: a for a in catalog.list_actors(root=root)}
        assert set(listed) == {"healthy", "broken"}, "one bad Actor hid the others"
        assert listed["healthy"].input_schema == {"type": "object"}
        assert listed["broken"].schema_error


# --- the Actor is still the authority ---------------------------------------


@test
def test_the_actor_still_validates_its_own_input():
    """Bypass the API entirely: the Actor must still reject bad input itself.

    An Actor is an independent executable. If the platform's schema were the
    only check, running one directly -- which the contract explicitly allows --
    would run it on garbage.
    """
    result = subprocess.run(
        [sys.executable, str(EXECUTOR), "hello_world", "--input", "{}"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 1, f"expected an Actor failure, got {result.returncode}"

    doc = json.loads(result.stdout)
    assert doc["status"] == "FAILED"
    assert doc["exit_code"] == 1, "a bad input is the ACTOR failing, not the platform"

    logs = REGISTRY.get_logs(doc["run_id"])
    assert "is required" in logs, "the Actor's own message must still be what explains it"


@test
def test_a_schema_gap_is_still_caught_by_the_actor():
    """The schema is coarser than the Actor, on purpose.

    "   " satisfies minLength: 1 but is empty once trimmed. The Actor rejects
    it, which is exactly why its own validation has to stay.
    """
    actor = catalog.find_actor("hello_world")
    catalog.validate_input(actor, {"name": "   "})  # the schema is content with this

    result = subprocess.run(
        [sys.executable, str(EXECUTOR), "hello_world", "--input", '{"name": "   "}'],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 1, "the Actor accepted whitespace the platform let through"
    assert json.loads(result.stdout)["status"] == "FAILED"


def main() -> int:
    failures = 0
    print(f"running {len(TESTS)} actor schema tests\n")

    for fn in TESTS:
        name = fn.__name__.removeprefix("test_")
        try:
            fn()
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"  FAIL  {name}")
            print("        " + traceback.format_exc().strip().replace("\n", "\n        "))
        else:
            print(f"  ok    {name}")

    QUEUE.purge()
    print()
    print(f"{failures} of {len(TESTS)} tests FAILED" if failures else f"all {len(TESTS)} tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
