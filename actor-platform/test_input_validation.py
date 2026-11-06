#!/usr/bin/env python3
"""Input validation at every boundary, not just at the HTTP edge.

    python3 test_input_validation.py

Offline apart from ONE test that starts a real container when Docker is
available (it skips, and says so, when it is not). No database: the
account-route checks that need one live in test_input_validation_db.py.

Each test drives the REAL boundary -- the API over its ASGI app, the queue over
its files, the worker's own functions, the registry over a real run directory
-- and asserts the outcome an attacker would care about (nothing stored, nothing
queued, nothing executed, nothing echoed), not merely that a helper returned
False.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from contextlib import contextmanager, redirect_stderr
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
if (
    VENV_PYTHON.is_file()
    and Path(sys.prefix).resolve() != (REPO_ROOT / ".venv").resolve()
    and not os.environ.get("_ACTOR_PLATFORM_REEXEC")
):
    os.environ["_ACTOR_PLATFORM_REEXEC"] = "1"
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])

sys.path.insert(0, str(REPO_ROOT))

import warnings  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)

from fastapi.testclient import TestClient  # noqa: E402

from api.auth import AuthPolicy, generate_key  # noqa: E402
from api.main import create_app  # noqa: E402
from api.validation import DEFAULT_MAX_BODY_BYTES  # noqa: E402
from catalog import actors as catalog  # noqa: E402
from jobqueue.base import Job, QueueError  # noqa: E402
from jobqueue.file_queue import FileJobQueue  # noqa: E402
from registry.dataset import parse_line, read_from  # noqa: E402
from registry.run_registry import (  # noqa: E402
    ArtifactCorrupt,
    CorruptRunRecord,
    RunRegistry,
)
from shared.models import (  # noqa: E402
    INPUT_FILE,
    LOG_FILE,
    OUTPUT_FILE,
    RUN_FILE,
    RunStatus,
    is_canonical_uuid,
    is_valid_run_id,
)
from worker import consumer, executor  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


@contextmanager
def env(**values):
    """Set environment variables for the block, restoring them afterwards."""
    saved = {name: os.environ.get(name) for name in values}
    try:
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class Box:
    """A throwaway registry, file queue and API over them, with a real key."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="input-validation-"))
        self.registry = RunRegistry(self.tmp / "runs")
        self.queue = FileJobQueue(str(self.tmp / "queue"))
        key = generate_key()
        self.client = TestClient(
            create_app(registry=self.registry, queue=self.queue,
                       auth=AuthPolicy.for_keys({key})),
            headers={"Authorization": f"Bearer {key}"},
        )

    def runs(self) -> list[str]:
        root = self.tmp / "runs"
        return sorted(p.name for p in root.iterdir()) if root.is_dir() else []

    def post_raw(self, path: str, body: bytes):
        return self.client.post(path, content=body,
                                headers={"content-type": "application/json"})

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def assert_nothing_created(box: Box) -> None:
    assert box.runs() == [], f"a run directory was left behind: {box.runs()}"
    assert box.queue.depth() == 0, "a job was queued for a refused request"


# --- request size and error bodies ------------------------------------------


@test
def test_an_oversized_body_is_refused_before_it_is_parsed():
    box = Box()
    try:
        big = b'{"name":"' + b"a" * (DEFAULT_MAX_BODY_BYTES + 10) + b'"}'
        response = box.post_raw("/actors/hello_world/runs", big)
        assert response.status_code == 413, response.status_code
        assert response.json()["error"] == "request too large"
        assert_nothing_created(box)
    finally:
        box.close()


@test
def test_a_chunked_body_with_no_length_is_bounded_too():
    """No Content-Length to check: the limit has to be counted on the stream."""
    box = Box()
    try:
        def stream():
            yield b'{"name":"'
            for _ in range(DEFAULT_MAX_BODY_BYTES // 8192 + 4):
                yield b"a" * 8192
            yield b'"}'

        response = box.client.post("/actors/hello_world/runs", content=stream(),
                                    headers={"content-type": "application/json"})
        assert response.status_code == 413, response.status_code
        assert_nothing_created(box)
    finally:
        box.close()


@test
def test_the_limit_is_configurable_and_a_normal_body_still_passes():
    with env(ACTOR_PLATFORM_MAX_BODY_BYTES="64"):
        box = Box()
    try:
        assert box.client.post("/actors/hello_world/runs",
                               json={"name": "x"}).status_code == 202
        assert box.client.post("/actors/hello_world/runs",
                               json={"name": "x" * 100}).status_code == 413
    finally:
        box.close()


@test
def test_a_validation_error_never_echoes_the_input():
    """FastAPI's default 422 copies `input` into the body. Never here."""
    box = Box()
    try:
        marker = "do-not-echo-" + "z" * 50
        response = box.client.get(f"/runs?limit={marker}")
        assert response.status_code == 422
        body = response.json()
        assert marker not in response.text, body
        assert all(set(error) <= {"type", "loc", "msg"} for error in body["detail"]), body
        # Malformed JSON: also 422, also without the input.
        response = box.post_raw("/actors/hello_world/runs", b'{"name": "' + marker.encode())
        assert response.status_code in (400, 422), response.status_code
        assert marker not in response.text, response.text
    finally:
        box.close()


@test
def test_a_schema_violation_message_is_bounded():
    box = Box()
    try:
        response = box.client.post("/actors/facebook/runs",
                                   json={"target": "n", "ai_prompt": "q" * 50_000})
        assert response.status_code == 400
        assert len(response.text) < 2_000, f"{len(response.text)} bytes echoed back"
        assert_nothing_created(box)
    finally:
        box.close()


# --- run input -----------------------------------------------------------------


@test
def test_unstorable_strings_are_refused_and_leave_no_orphan_run():
    """NUL and lone surrogates used to 500 AFTER mkdir, orphaning the run dir."""
    box = Box()
    try:
        for raw in (b'{"name": "a\\u0000b"}', b'{"name": "\\ud800"}',
                    b'{"name": "ok", "\\u0000": 1}'):
            response = box.post_raw("/actors/hello_world/runs", raw)
            assert response.status_code == 400, (raw, response.status_code, response.text)
            assert response.json()["error"] == "invalid actor input"
        assert_nothing_created(box)
    finally:
        box.close()


@test
def test_a_registry_that_cannot_store_an_input_leaves_nothing_behind():
    """The registry's own guard, for a caller that skipped the catalog."""
    from registry.run_registry import UnstorableInput  # noqa: PLC0415

    box = Box()
    try:
        try:
            box.registry.create("hello_world", {"name": "\ud800"})
        except UnstorableInput:
            pass
        else:
            raise AssertionError("an unencodable input was stored")
        assert box.runs() == [], box.runs()
    finally:
        box.close()


@test
def test_deeply_nested_input_is_refused():
    box = Box()
    try:
        nested: object = "x"
        for _ in range(40):
            nested = [nested]
        response = box.client.post("/actors/hello_world/runs",
                                   json={"name": "x", "extra": nested})
        assert response.status_code == 400, response.text
        assert "nested" in response.text
        # Far past Python's recursion limit: the JSON parser itself gives up,
        # and that must still be a client error, never a 500.
        deep = b'{"name": "x", "e": ' + b"[" * 50_000 + b"]" * 50_000 + b"}"
        response = box.post_raw("/actors/hello_world/runs", deep)
        assert 400 <= response.status_code < 500, response.status_code
        assert_nothing_created(box)
    finally:
        box.close()


@test
def test_floats_are_not_integers():
    box = Box()
    try:
        for value in (5.0, 1e3, 2.5):
            response = box.client.post("/actors/facebook/runs",
                                       json={"target": "nasa", "max_posts": value})
            assert response.status_code == 400, (value, response.text)
        assert_nothing_created(box)
    finally:
        box.close()


@test
def test_integer_bounds_and_edges():
    box = Box()
    try:
        for value in (0, -1, 5001, 2**63, -(2**63) - 1, 10**100):
            response = box.client.post("/actors/facebook/runs",
                                       json={"target": "nasa", "max_posts": value})
            assert response.status_code == 400, (value, response.status_code)
        for value in (1, 5000):
            assert box.client.post("/actors/facebook/runs", json={
                "target": "nasa", "max_posts": value}).status_code == 202, value
        # The test fixtures carried unbounded fields too.
        assert box.client.post("/actors/item_processor/runs",
                               json={"count": 1, "fail_at": 10**9}).status_code == 400
        assert box.client.post("/actors/item_processor/runs",
                               json={"count": 1, "label": "l" * 201}).status_code == 400
        assert box.client.post("/actors/hello_world/runs",
                               json={"name": "n" * 201}).status_code == 400
    finally:
        box.close()


@test
def test_dates_must_be_real_dates_in_a_sane_range_and_in_order():
    box = Box()
    try:
        bad = [
            {"start_date": "2026-99-99"},                     # impossible
            {"start_date": "2026-02-30"},                     # impossible
            {"start_date": "\u0662\u0660\u0662\u0666-01-01"},  # non-ASCII digits
            {"start_date": "2026-01-01\n"},                   # trailing newline
            {"end_date": "9999-12-31"},                       # overflowed X's window
            {"start_date": "1970-01-01"},                     # before any target site
            {"start_date": "2026-01-01 25:00"},               # impossible time
            {"start_date": "2026-03-01", "end_date": "2026-02-01"},  # inverted
            {"start_date": "2026-01-01", "end_date": 20260101},       # wrong type
        ]
        for extra in bad:
            response = box.client.post("/actors/x/runs", json={"target": "nasa", **extra})
            assert response.status_code == 400, (extra, response.status_code)
        good = [
            {"start_date": "2026-01-01", "end_date": "2026-02-01"},
            {"start_date": "2026-01-01 08:30", "end_date": "2026-01-01"},  # same day
            {"start_date": "2026-01-01T08:30:00"},
        ]
        for extra in good:
            response = box.client.post("/actors/x/runs", json={"target": "nasa", **extra})
            assert response.status_code == 202, (extra, response.text)
    finally:
        box.close()


@test
def test_a_target_is_bounded_and_free_of_control_characters():
    box = Box()
    try:
        for target in ("a" * 2049, "nasa\nINJECTED log line", "nasa\r\n", "a\x1b[31m",
                       "a\x7f", ""):
            for actor in ("facebook", "facebook_groups", "instagram", "linkedin",
                          "threads", "tiktok", "x"):
                response = box.client.post(f"/actors/{actor}/runs", json={"target": target})
                assert response.status_code == 400, (actor, repr(target)[:30])
        assert_nothing_created(box)
    finally:
        box.close()


@test
def test_every_actor_still_accepts_a_legitimate_input():
    """The regression guard: tightening must not break a real request."""
    box = Box()
    try:
        legitimate = {
            "facebook": [
                {"target": "nasa"},
                {"target": "https://www.facebook.com/groups/123456789012345",
                 "start_date": "2026-08-01", "end_date": "2026-08-31",
                 "max_comments_per_post": 20, "comment_sort": "newest",
                 "page_concurrency": 3, "analyse": False,
                 "ai_prompt": "focus on complaints"},
                {"target": "https://www.facebook.com/profile.php?id=100064"},
            ],
            "facebook_groups": [{"target": "groups/123456789012345", "max_posts": 0,
                                 "max_comments_per_post": 0}],
            "instagram": [{"target": "nasa", "mode": "profile", "max_posts": 10}],
            "linkedin": [{"target": "pwc", "mode": "company", "deep_comments": True}],
            "threads": [{"target": "lumen", "mode": "tag"}],
            "tiktok": [{"target": "@nasa", "max_replies_per_comment": 0}],
            "x": [{"target": "#space", "mode": "hashtag", "start_date": "2026-01-01"}],
            "hello_world": [{"name": "Ünïcødé ☃"}],
            "item_processor": [{"count": 3, "fail_at": None, "label": "L", "delay_ms": 0}],
        }
        installed = {a.name for a in catalog.list_actors()}
        assert set(legitimate) <= installed, set(legitimate) - installed
        for actor, inputs in legitimate.items():
            for payload in inputs:
                response = box.client.post(f"/actors/{actor}/runs", json=payload)
                assert response.status_code == 202, (actor, payload, response.text)
        assert box.queue.depth() == sum(len(v) for v in legitimate.values())
    finally:
        box.close()


# --- identifiers ---------------------------------------------------------------


@test
def test_shell_metacharacter_actor_names_never_get_anywhere():
    box = Box()
    try:
        for name in ("a;rm -rf /", "$(id)", "`id`", "a|b", "a&&b", "..", "../hello_world",
                     "hello_world;id", "HELLO_WORLD", "a" * 65, "x%0Ab", "%2e%2e", "a%00b"):
            response = box.client.post(f"/actors/{name}/runs", json={"name": "x"})
            assert response.status_code in (400, 404, 405), (name, response.status_code)
            assert "Dockerfile" not in response.text and "/actors/" not in response.json().get(
                "detail", ""), response.text
        assert_nothing_created(box)
    finally:
        box.close()


@test
def test_an_unknown_actor_message_names_no_server_path():
    box = Box()
    try:
        response = box.client.post("/actors/no_such_actor/runs", json={})
        assert response.status_code == 404
        assert str(REPO_ROOT) not in response.text and "/" not in response.json()["detail"]
    finally:
        box.close()


@test
def test_malformed_run_ids_are_client_errors_never_500():
    box = Box()
    try:
        box.registry.create("hello_world", {"name": "x"})
        hostile = ["../../etc/passwd", "..%2f..%2fetc", "%2e%2e", "20261016-000000000000-zzzzzz",
                   "\u0662" * 8 + "-000000-aaaaaa", "a" * 500, "%00", "20261016-000000-aaaaaa;id"]
        for run_id in hostile:
            for suffix in ("", "/output", "/logs", "/artifacts", "/items", "/input"):
                response = box.client.get(f"/runs/{run_id}{suffix}")
                assert response.status_code in (400, 404, 409), (run_id, suffix,
                                                                 response.status_code)
            response = box.client.post(f"/runs/{run_id}/cancel")
            assert response.status_code in (400, 404, 405, 409), (run_id, response.status_code)
        # Unicode digits are not run-id digits any more.
        assert not is_valid_run_id("\u0662" * 8 + "-000000-aaaaaa")
    finally:
        box.close()


@test
def test_query_parameters_are_bounded():
    box = Box()
    try:
        run = box.registry.create("hello_world", {"name": "x"})
        for query in ("after=-1", f"after={2**63}", f"after={10**30}", "after=abc",
                      "collection=a%00b", "collection=../x", "collection=" + "c" * 65,
                      "limit=0", "limit=1001"):
            response = box.client.get(f"/runs/{run.run_id}/items?{query}")
            assert response.status_code == 422, (query, response.status_code)
        for query in ("actor=a%00b", "actor=../x", "actor=" + "a" * 65, "actor=A;B",
                      "status=NOPE", "limit=101"):
            response = box.client.get(f"/runs?{query}")
            assert response.status_code == 422, (query, response.status_code)
        assert box.client.get(f"/runs/{run.run_id}/items?collection=data%231&after=5"
                              ).status_code == 200
        assert box.client.get("/runs?actor=hello_world&limit=5").status_code == 200
    finally:
        box.close()


@test
def test_uuid_checks_take_only_the_hyphenated_form():
    assert is_canonical_uuid("0f8fad5b-d9cb-469f-a165-70867728950e")
    assert is_canonical_uuid("0F8FAD5B-D9CB-469F-A165-70867728950E")
    for value in ("urn:uuid:0f8fad5b-d9cb-469f-a165-70867728950e",
                  "{0f8fad5b-d9cb-469f-a165-70867728950e}",
                  "0f8fad5bd9cb469fa16570867728950e", "0f8fad5b-d9cb-469f-a165-70867728950e\n",
                  "", None, 5, "../x"):
        assert not is_canonical_uuid(value), value


# --- artifacts and the display proxy -----------------------------------------


@test
def test_an_html_artifact_is_sandboxed_and_nothing_is_sniffed():
    box = Box()
    try:
        run = box.registry.create("hello_world", {"name": "x"})
        artifacts = Path(run.run_dir) / "artifacts"
        artifacts.mkdir()
        (artifacts / "dashboard.html").write_text("<script>fetch('/me/api-keys')</script>")
        (artifacts / "report.pdf").write_bytes(b"%PDF-1.4")
        html = box.client.get(f"/runs/{run.run_id}/artifacts/dashboard.html")
        assert html.status_code == 200
        assert html.headers["x-content-type-options"] == "nosniff"
        assert html.headers["content-security-policy"].startswith("sandbox"), html.headers
        assert html.headers["content-disposition"].startswith("attachment")
        pdf = box.client.get(f"/runs/{run.run_id}/artifacts/report.pdf")
        assert pdf.headers["x-content-type-options"] == "nosniff"
        for name in ("..%2Frun.json", "%2e%2e%2f%2e%2e%2fetc%2fpasswd", ".hidden", "run.json"):
            assert box.client.get(f"/runs/{run.run_id}/artifacts/{name}").status_code == 404
    finally:
        box.close()


@test
def test_a_finished_runs_display_port_is_not_proxied():
    """The port is ephemeral; after the run it may belong to someone else."""
    box = Box()
    try:
        run = box.registry.create("hello_world", {"name": "x"})
        box.registry.update(run.run_id, status=RunStatus.RUNNING, display_port=9)
        box.registry.complete(run.run_id, status=RunStatus.SUCCEEDED)
        response = box.client.get(f"/runs/{run.run_id}/vnc/vnc.html")
        assert response.status_code == 404, response.status_code
    finally:
        box.close()


# --- the run record is the platform's, not the container's ---------------------


@test
def test_a_tampered_run_record_cannot_choose_its_own_directory():
    """V1: run.json rewritten by the container with run_dir "/"."""
    box = Box()
    try:
        run = box.registry.create("hello_world", {"name": "x"})
        record = Path(run.run_dir) / RUN_FILE
        data = json.loads(record.read_text())
        data["run_dir"] = "/"
        record.write_text(json.dumps(data))
        reread = box.registry.get(run.run_id)
        assert reread.run_dir == str(box.registry.run_dir(run.run_id)), reread.run_dir

        data["run_id"] = "20200101-000000000000-aaaaaa"
        record.write_text(json.dumps(data))
        try:
            box.registry.get(run.run_id)
        except CorruptRunRecord:
            pass
        else:
            raise AssertionError("a record naming another run was accepted")
    finally:
        box.close()


@test
def test_the_container_gets_the_record_and_input_read_only():
    box = Box()
    try:
        run = box.registry.create("hello_world", {"name": "x"})
        directory = box.registry.run_dir(run.run_id)
        args = executor._container_args("hello_world", run.run_id, directory)
        mounts = [args[i + 1] for i, arg in enumerate(args) if arg == "--mount"]
        for name in (RUN_FILE, INPUT_FILE):
            assert any(f"source={directory / name}," in m and m.endswith(",readonly")
                       and f"target=/actor/storage/{name}" in m for m in mounts), mounts
        assert f"{directory}:/actor/storage" in args

        # A planted link is not bind-mounted (docker would follow it on the host).
        (directory / INPUT_FILE).unlink()
        (directory / INPUT_FILE).symlink_to("/etc/passwd")
        args = executor._container_args("hello_world", run.run_id, directory)
        assert not any("/etc/passwd" in a or f"{INPUT_FILE}," in a for a in args), args
    finally:
        box.close()


@test
def test_a_real_container_cannot_rewrite_its_run_record():
    """End to end, when Docker is here: the write fails and the host file holds."""
    if shutil.which("docker") is None or subprocess.run(
            ["docker", "image", "inspect", "actor-platform/hello_world:latest"],
            capture_output=True).returncode != 0:
        print("        (skipped: docker or the hello_world image is not available)")
        return
    box = Box()
    try:
        run = box.registry.create("hello_world", {"name": "x"})
        directory = box.registry.run_dir(run.run_id)
        before = (directory / RUN_FILE).read_bytes()
        program = (
            "import json, os\n"
            "results = {}\n"
            "for name in ('run.json', 'input.json'):\n"
            "    path = '/actor/storage/' + name\n"
            "    try:\n"
            "        open(path, 'w').write('{\"run_dir\": \"/\"}')\n"
            "        results[name] = 'WROTE'\n"
            "    except OSError as exc:\n"
            "        results[name] = type(exc).__name__\n"
            "    try:\n"
            "        os.replace('/actor/storage/x', path)\n"
            "    except OSError as exc:\n"
            "        pass\n"
            "    try:\n"
            "        os.unlink(path); results[name + ':unlink'] = 'UNLINKED'\n"
            "    except OSError as exc:\n"
            "        results[name + ':unlink'] = type(exc).__name__\n"
            "open('/actor/storage/output.json', 'w').write(json.dumps(results))\n"
        )
        args = executor._container_args("hello_world", run.run_id, directory)
        image = args.pop()
        args = [a for a in args if a != "-t"] + ["--entrypoint", "python", image, "-c", program]
        done = subprocess.run(args, capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr[-1000:]
        results = json.loads((directory / OUTPUT_FILE).read_text())
        assert "WROTE" not in results.values() and "UNLINKED" not in results.values(), results
        assert (directory / RUN_FILE).read_bytes() == before
        assert box.registry.get(run.run_id).run_dir == str(directory)
    finally:
        subprocess.run(["docker", "rm", "-f", f"actor-{run.run_id}"], capture_output=True)
        box.close()


# --- the queue -------------------------------------------------------------------


GOOD_RUN_ID = "20261016-120000000000-abcdef"
TAMPERED = [
    b"not json at all",
    b"[1, 2, 3]",
    b'"a string"',
    b"{}",
    b'{"run_id": 5, "actor": "hello_world"}',
    b'{"run_id": "../../etc/passwd", "actor": "hello_world"}',
    b'{"run_id": "' + GOOD_RUN_ID.encode() + b'", "actor": "a;rm -rf /"}',
    b'{"run_id": "' + GOOD_RUN_ID.encode() + b'", "actor": "hello_world", "attempts": "x"}',
    b'{"run_id": "' + GOOD_RUN_ID.encode() + b'", "actor": "hello_world", "attempts": -1}',
    b'{"run_id": "' + GOOD_RUN_ID.encode() + b'", "actor": "hello_world", "attempts": true}',
    b'{"run_id": "' + GOOD_RUN_ID.encode() + b'", "actor": "hello_world", "input": [1]}',
    b'{"run_id": "' + GOOD_RUN_ID.encode() + b'", "actor": "hello_world", '
    b'"concurrency_key": ["a"]}',
    b'{"run_id": "' + GOOD_RUN_ID.encode() + b'", "actor": "hello_world", '
    b'"concurrency_key": "' + b"k" * 300 + b'"}',
    b'{"run_id": "' + GOOD_RUN_ID.encode() + b'", "actor": "hello_world", "input": '
    + b"[" * 100_000 + b"]" * 100_000 + b"}",
    b"\xff\xfe not utf-8",
]


@test
def test_tampered_job_messages_raise_only_queue_errors():
    for raw in TAMPERED:
        try:
            Job.from_json(raw)
        except QueueError as exc:
            assert len(str(exc)) < 200, str(exc)  # never the payload
        else:
            raise AssertionError(f"accepted {raw[:60]!r}")
    job = Job.from_json(json.dumps({"run_id": GOOD_RUN_ID, "actor": "hello_world",
                                    "input": {"name": "x"}, "attempts": 2}))
    assert job.attempts == 2 and job.input == {"name": "x"}


@test
def test_the_file_queue_sets_bad_messages_aside_and_serves_the_next_good_one():
    """Both dequeue paths: plain, and the filtered one the consumer uses."""
    for eligible in (None, lambda job: True):
        tmp = Path(tempfile.mkdtemp())
        try:
            queue = FileJobQueue(str(tmp))
            queue.enqueue(Job(run_id=GOOD_RUN_ID, actor="hello_world"))
            queue._ensure()
            for index, raw in enumerate(TAMPERED):
                # Named to sort AHEAD of the good job, so it is met first.
                (queue.pending / f"00000000-{index:03d}.json").write_bytes(raw)
            job = queue.dequeue(timeout=0, eligible=eligible)
            assert job is not None and job.run_id == GOOD_RUN_ID, job
            assert queue.depth() == 0, queue.depth()
            rejected = sorted((queue.root / "rejected").iterdir())
            assert len(rejected) == len(TAMPERED), len(rejected)
            assert queue.dequeue(timeout=0, eligible=eligible) is None
            # recover() and known_run_ids() meet a bad lease without raising.
            (queue.claimed / "bad__host~1__0.json").write_bytes(b"[[[")
            queue.recover(visibility_timeout=0)
            assert queue.known_run_ids() == {GOOD_RUN_ID}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


@test
def test_the_redis_queue_sets_bad_messages_aside_too():
    """Against an in-memory stand-in for the four Redis commands it uses."""
    from jobqueue.redis_queue import RedisJobQueue  # noqa: PLC0415

    class FakeRedis:
        def __init__(self):
            self.lists: dict[str, list[str]] = {}
            self.hashes: dict[str, dict[str, str]] = {}

        def lrange(self, key, start, end):
            return list(self.lists.get(key, []))

        def lrem(self, key, count, value):
            items = self.lists.get(key, [])
            if value in items:
                items.remove(value)
                return 1
            return 0

        def lpush(self, key, value):
            self.lists.setdefault(key, []).insert(0, value)

        def rpush(self, key, value):
            self.lists.setdefault(key, []).append(value)

        def hset(self, key, field, value):
            self.hashes.setdefault(key, {})[field] = value

        def hget(self, key, field):
            return self.hashes.get(key, {}).get(field)

        def hdel(self, key, field):
            self.hashes.get(key, {}).pop(field, None)

        def lmove(self, source, target, *_):
            items = self.lists.get(source, [])
            if not items:
                return None
            value = items.pop()
            self.lpush(target, value)
            return value

    queue = RedisJobQueue.__new__(RedisJobQueue)
    queue.name, queue.url = "q", "redis://fake"
    queue.inflight_key, queue.leases_key, queue.rejected_key = "q:inflight", "q:leases", "q:rejected"
    queue._client = FakeRedis()
    good = Job(run_id=GOOD_RUN_ID, actor="hello_world").to_json()
    queue._client.lists["q"] = [good] + [raw.decode("utf-8", "replace") for raw in TAMPERED]

    job = queue.dequeue(timeout=0, eligible=lambda job: True)
    assert job is not None and job.run_id == GOOD_RUN_ID
    assert len(queue._client.lists["q:rejected"]) == len(TAMPERED)
    assert queue._client.lists["q"] == []

    # The unfiltered path leases first; a bad payload then leaves inflight.
    queue._client.lists["q"] = ["{oops"]
    assert queue.dequeue(timeout=0) is None
    assert queue._client.lists["q:inflight"] == [good]  # only the good lease remains
    assert "{oops" in queue._client.lists["q:rejected"]


class _AckRecorder:
    def __init__(self):
        self.acked, self.nacked = [], []

    def ack(self, job):
        self.acked.append(job.run_id)
        return True

    def nack(self, job):
        self.nacked.append(job.run_id)
        return True


@test
def test_a_job_that_disagrees_with_its_run_about_the_actor_is_never_run():
    box = Box()
    executed = []
    original = consumer.execute_run
    consumer.execute_run = lambda *a, **k: executed.append(a)
    try:
        run = box.registry.create("hello_world", {"name": "x"})
        queue = _AckRecorder()
        job = Job(run_id=run.run_id, actor="item_processor", input={"count": 1})
        with redirect_stderr(io.StringIO()):
            assert consumer.process_job(job, queue=queue, registry=box.registry) is None
        assert executed == [], "a tampered job reached execute_run"
        assert queue.acked == [run.run_id]
        assert box.registry.get(run.run_id).status is RunStatus.PENDING
    finally:
        consumer.execute_run = original
        box.close()


@test
def test_the_worker_revalidates_the_stored_input_and_runs_no_container():
    """The record and queue are not the API's to vouch for once it returns."""
    box = Box()
    called = []
    original = executor._preflight
    executor._preflight = lambda *a, **k: called.append(a)
    try:
        # Created straight through the registry: what a tampered record, a
        # direct caller or a since-tightened schema looks like to the worker.
        run = box.registry.create("hello_world", {"name": "n" * 500})
        with redirect_stderr(io.StringIO()):
            result = executor.execute_run(run.run_id, registry=box.registry)
        assert called == [], "a container was prepared for an invalid input"
        assert result.status is RunStatus.FAILED, result.status
        assert "input refused by the worker" in (result.error or ""), result.error
    finally:
        executor._preflight = original
        box.close()


# --- what an Actor produces ------------------------------------------------------


@test
def test_dataset_lines_that_cannot_be_stored_are_skipped_and_ingest_continues():
    deep = b'{"collection": "c", "data": ' + b"[" * 5000 + b"]" * 5000 + b"}"
    bad = [
        deep,
        b'{"collection": "c", "data": "a\\u0000b"}',
        b'{"collection": "c", "data": {"\\ud800": 1}}',
        b'{"collection": "c", "data": ' + b"[" * 150 + b"1" + b"]" * 150 + b"}",
        b'{"collection": "../x", "data": 1}',
        b'{"collection": "c", "key": {"k": 1}, "data": 1}',
    ]
    for index, raw in enumerate(bad, start=1):
        item, reason = parse_line(raw, index)
        assert item is None and reason, (index, reason)
        assert "\u0000" not in reason and len(reason) < 200

    tmp = Path(tempfile.mkdtemp())
    try:
        path = tmp / "dataset.jsonl"
        path.write_bytes(b"\n".join(bad + [b'{"collection": "c", "key": "k", "data": {"ok": 1}}'])
                         + b"\n")
        result = read_from(path)
        assert [item.data for item in result.items] == [{"ok": 1}], result.items
        assert result.malformed == len(bad)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def test_an_oversized_output_is_refused_and_an_oversized_log_is_cut():
    box = Box()
    try:
        run = box.registry.create("hello_world", {"name": "x"})
        directory = box.registry.run_dir(run.run_id)
        (directory / OUTPUT_FILE).write_text(json.dumps({"x": "y" * 5000}))
        (directory / LOG_FILE).write_text("line\n" * 2000)
        with env(ACTOR_PLATFORM_MAX_OUTPUT_BYTES="1000", ACTOR_PLATFORM_MAX_LOG_BYTES="500"):
            try:
                box.registry.get_output(run.run_id)
            except ArtifactCorrupt as exc:
                assert "larger than 1000" in str(exc)
            else:
                raise AssertionError("an oversized output.json was read whole")
            assert box.client.get(f"/runs/{run.run_id}/output").status_code == 500
            logs = box.client.get(f"/runs/{run.run_id}/logs")
            assert logs.status_code == 200 and len(logs.text) < 700, len(logs.text)
            assert "log truncated" in logs.text
        # Not UTF-8 is not a crash either.
        (directory / LOG_FILE).write_bytes(b"\xff\xfe ok\n")
        assert box.client.get(f"/runs/{run.run_id}/logs").status_code == 200
    finally:
        box.close()


@test
def test_the_worker_keeps_a_bounded_log_and_cuts_endless_lines():
    program = (
        "import sys\n"
        "for i in range(500):\n"
        "    print(f'line {i}')\n"
        "sys.stdout.write('x' * 200000 + '\\n')\n"
        "print('after the long line')\n"
    )
    with env(ACTOR_PLATFORM_MAX_LOG_BYTES="2000"), redirect_stderr(io.StringIO()):
        code, lines = executor._execute([sys.executable, "-c", program], "log-cap-probe")
    assert code == 0
    assert sum(len(line) + 1 for line in lines[:-1]) <= 2000, sum(map(len, lines))
    assert "log truncated" in lines[-1] and "further line(s)" in lines[-1], lines[-1]

    with redirect_stderr(io.StringIO()):
        code, lines = executor._execute(
            [sys.executable, "-c", "print('x' * 200000); print('next')"], "line-cap-probe")
    assert lines[0].endswith("[line truncated]") and len(lines[0]) < 70_000
    assert lines[1] == "next", lines


@test
def test_forwarded_operator_secrets_are_redacted_from_actor_logs():
    secret = "sk-ant-fake-key-0123456789"
    with env(ACTOR_FORWARD_ENV="FAKE_PROVIDER_KEY,SHORT_ONE", FAKE_PROVIDER_KEY=secret,
             SHORT_ONE="abc"), redirect_stderr(io.StringIO()) as sink:
        redact = executor._redactions({"FB_SESSION": "session-value-123"})
        assert secret in redact and "session-value-123" in redact
        assert "abc" not in redact, "a short value would wreck the log"
        _, lines = executor._execute(
            [sys.executable, "-c", f"print('Authorization: {secret}'); print('abc')"],
            "redact-probe", redact=redact)
    assert lines == ["Authorization: [redacted]", "abc"], lines
    assert secret not in sink.getvalue()


def main() -> int:
    print(f"running {len(TESTS)} input-validation tests\n")
    failed = 0
    for fn in TESTS:
        name = fn.__name__.removeprefix("test_")
        try:
            fn()
            print(f"  ok    {name}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}")
            print("        " + traceback.format_exc().strip().replace("\n", "\n        "))
    print(f"\n{'all ' + str(len(TESTS)) + ' tests passed' if not failed else f'{failed} FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
