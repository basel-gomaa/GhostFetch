"""The host must not trust what an Actor leaves in its own run directory.

    python3 test_run_dir_links.py

Offline: no database, no Docker. Drives the real registry, dataset reader,
executor helper, mailer and API routes over the filesystem backend -- which the
Postgres backend inherits every one of these methods from.

The run directory is bind-mounted READ-WRITE into the Actor's container. So
every file the platform later opens there may be something the container
planted, and three kinds of object turn an ordinary open() on the host into a
different operation:

    a symlink   resolved by the HOST: "write the logs" overwrites a host file
                with text the Actor chose; "serve the output" serves one
    a FIFO      open() blocks for ever; a worker or API thread just hangs
    a directory/device  same family; refused by the same check

Each test plants one of these pointing at a VICTIM file outside the run
directory, then asserts the victim is untouched and its contents never reach a
response. Each failed before shared/safe_fs.py existed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from email.message import EmailMessage
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from api.auth import AuthPolicy  # noqa: E402
from api.main import create_app  # noqa: E402
from jobqueue.file_queue import FileJobQueue  # noqa: E402
from registry.dataset import read_from  # noqa: E402
from registry.run_registry import ArtifactCorrupt, RunRegistry  # noqa: E402
from shared.models import (  # noqa: E402
    ATTENTION_FILE, DATASET_FILE, INPUT_FILE, LOG_FILE, OUTPUT_FILE, RESUME_FILE,
    RUN_FILE, SESSION_OUT_FILE, RunStatus,
)

TESTS = []
SECRET = "HOST-SECRET-do-not-serve"


def test(fn):
    TESTS.append(fn)
    return fn


class Box:
    """A registry, an app over it, one PENDING run, and a victim outside it."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.registry = RunRegistry(tmp / "runs")
        self.app = create_app(registry=self.registry,
                              queue=FileJobQueue(str(tmp / "queue")),
                              auth=AuthPolicy.disabled())
        self.client = TestClient(self.app)
        self.run = self.registry.create("hello_world", {"name": "legit"})
        self.dir = Path(self.run.run_dir)
        # JSON on purpose: a JSON victim is the one /input and /output would
        # parse and serve whole. JSONL-shaped too, so the item reader would
        # happily ingest it.
        self.victim = tmp / "victim.json"
        self.victim.write_text(json.dumps({"collection": "c", "key": "k", "data": SECRET}) + "\n")
        self.victim_dir = tmp / "victim_dir"
        self.victim_dir.mkdir()
        (self.victim_dir / "secret.txt").write_text(SECRET)

    def plant_link(self, name: str, target: Path | None = None) -> Path:
        path = self.dir / name
        if path.is_symlink() or path.exists():
            path.unlink()
        path.symlink_to(target or self.victim)
        return path

    def plant_fifo(self, name: str) -> Path:
        path = self.dir / name
        if path.exists():
            path.unlink()
        os.mkfifo(path)
        return path

    def victim_intact(self) -> bool:
        return SECRET in self.victim.read_text()


def returns_within(fn, seconds: float = 3.0):
    """Run fn in a thread; fail if it is still blocked after `seconds`. A FIFO
    opened for reading with nobody writing blocks for ever, so a hang IS the
    failure being tested for."""
    outcome: dict = {}

    def target():
        try:
            outcome["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), f"{getattr(fn, '__name__', fn)} hung on a FIFO"
    return outcome


# --- the positive control --------------------------------------------------------


@test
def test_an_ordinary_run_reads_back_exactly_as_written():
    """Without this, every refusal below could be a broken route."""
    b = Box()
    run_id = b.run.run_id
    (b.dir / OUTPUT_FILE).write_text(json.dumps({"ok": [1, 2, 3]}))
    (b.dir / DATASET_FILE).write_text('{"collection": "c", "key": "a", "data": 1}\n')
    (b.dir / "artifacts").mkdir()
    (b.dir / "artifacts" / "report.pdf").write_bytes(b"%PDF-1.4 legit")
    b.registry.write_logs(run_id, ["line one", "line two"])

    assert b.client.get(f"/runs/{run_id}/input").json() == {"name": "legit"}
    assert b.client.get(f"/runs/{run_id}/output").json() == {"ok": [1, 2, 3]}
    assert b.client.get(f"/runs/{run_id}/logs").text == "line one\nline two\n"
    assert [i["data"] for i in b.client.get(f"/runs/{run_id}/items").json()["items"]] == [1]
    r = b.client.get(f"/runs/{run_id}/artifacts/report.pdf")
    assert r.status_code == 200 and r.content == b"%PDF-1.4 legit", r.status_code
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-length"] == str(len(b"%PDF-1.4 legit"))
    assert 'filename="report.pdf"' in r.headers["content-disposition"], r.headers
    # The run record itself still round-trips through the atomic writer.
    assert b.registry.get(run_id).status is RunStatus.PENDING
    assert not (b.dir / RUN_FILE).is_symlink()


# --- writes: the worker must never write THROUGH a planted link ---------------------


@test
def test_writing_the_logs_never_writes_through_a_planted_link():
    """W1. The worst one: the log is the Actor's own stdout, so this was a host
    file overwrite with content the Actor chose."""
    b = Box()
    b.plant_link(LOG_FILE)
    b.registry.write_logs(b.run.run_id, ["ssh-ed25519 AAAA attacker-key"])
    assert b.victim_intact(), "write_logs overwrote a host file through log.txt"
    assert not (b.dir / LOG_FILE).is_symlink(), "the planted link survived"
    assert (b.dir / LOG_FILE).read_text() == "ssh-ed25519 AAAA attacker-key\n"


@test
def test_saving_the_run_record_never_writes_through_a_planted_link():
    """W2. run.json is saved on every update -- the Postgres backend too, as a
    breadcrumb -- via a temp file whose NAME used to be fixed and so plantable."""
    b = Box()
    b.plant_link(f".{RUN_FILE}.tmp")
    b.plant_link(RUN_FILE)
    b.registry._save(b.run)
    assert b.victim_intact(), "saving run.json wrote through a planted link"
    assert not (b.dir / RUN_FILE).is_symlink()


@test
def test_taking_the_run_lock_never_truncates_a_planted_link():
    """W3. open('.lock', 'w') truncated whatever the link pointed at."""
    b = Box()
    b.plant_link(".lock")
    b.registry.mark_running(b.run.run_id)  # an update, so it takes the lock
    assert b.victim_intact(), "taking the lock truncated a host file"
    assert b.registry.get(b.run.run_id).status is RunStatus.RUNNING
    # A FIFO or a directory at .lock must not wedge updates either.
    b2 = Box()
    b2.plant_fifo(".lock")
    out = returns_within(lambda: b2.registry.mark_running(b2.run.run_id))
    assert "error" not in out, out.get("error")


@test
def test_resume_never_writes_through_a_planted_link():
    b = Box()
    b.plant_link(RESUME_FILE)
    r = b.client.post(f"/runs/{b.run.run_id}/resume")
    assert r.status_code == 200, r.text
    assert b.victim_intact(), "resume wrote through a planted link"
    assert (b.dir / RESUME_FILE).read_text() == "{}\n"


# --- reads: nothing behind a link reaches a response ---------------------------------


@test
def test_input_output_and_logs_never_serve_a_linked_host_file():
    """W4/W5. A JSON host file behind output.json was served whole over /output,
    and the reconciler's get_input would requeue one as a fresh run's input."""
    b = Box()
    run_id = b.run.run_id
    for name, route in ((INPUT_FILE, "input"), (OUTPUT_FILE, "output"), (LOG_FILE, "logs")):
        b.plant_link(name)
        r = b.client.get(f"/runs/{run_id}/{route}")
        assert SECRET not in r.text, f"/{route} served the host file behind {name}"
        assert r.status_code == 500, f"/{route}: {r.status_code} {r.text}"
    try:
        b.registry.get_input(run_id)
    except ArtifactCorrupt:
        pass
    else:
        raise AssertionError("get_input followed a link (the reconciler path)")


@test
def test_the_item_reader_does_not_ingest_a_linked_host_file():
    """W6. dataset.jsonl is tailed while the run goes; a link there made a
    host JSONL file this run's items, readable over /items."""
    b = Box()
    path = b.plant_link(DATASET_FILE)
    result = read_from(path, final=True)
    assert result.items == [], f"ingested {len(result.items)} item(s) from a host file"
    assert any("symbolic link" in r for r in result.reasons), result.reasons
    page = b.client.get(f"/runs/{b.run.run_id}/items").json()
    assert SECRET not in json.dumps(page)


@test
def test_the_item_tailer_says_once_why_it_ingested_nothing():
    """A refused file must not look like a run that wrote no items -- and must
    not be logged on every one of the tailer's polls either."""
    from worker.dataset_ingest import DatasetIngestor  # noqa: PLC0415

    b = Box()
    b.plant_link(DATASET_FILE)
    lines: list[str] = []
    ingestor = DatasetIngestor(b.registry, b.run.run_id, log=lines.append)
    for _ in range(3):
        ingestor.pump(final=False)
    said = [line for line in lines if "symbolic link" in line]
    assert len(said) == 1, f"expected one warning, got {lines}"


@test
def test_a_linked_artifacts_directory_lists_and_serves_nothing():
    """W8. The hole in the first fix: with artifacts/ ITSELF a link, no entry
    inside it is a link and every one resolves inside the (linked) directory."""
    b = Box()
    b.plant_link("artifacts", b.victim_dir)
    r = b.client.get(f"/runs/{b.run.run_id}/artifacts")
    assert r.status_code == 200 and r.json()["artifacts"] == [], r.text
    r = b.client.get(f"/runs/{b.run.run_id}/artifacts/secret.txt")
    assert r.status_code == 404 and SECRET not in r.text, r.text


@test
def test_attention_never_reads_through_a_link():
    b = Box()
    b.plant_link(ATTENTION_FILE)
    r = b.client.get(f"/runs/{b.run.run_id}/attention")
    assert r.status_code == 200 and r.json() is None, r.text


# --- FIFOs: nothing may hang ----------------------------------------------------------


@test
def test_a_fifo_left_in_the_run_directory_hangs_nothing():
    """open() on a FIFO with no writer blocks for ever. Every reader of a
    container-writable path has to come back."""
    b = Box()
    run_id = b.run.run_id
    for name in (LOG_FILE, OUTPUT_FILE, INPUT_FILE, DATASET_FILE, ATTENTION_FILE):
        b.plant_fifo(name)
    returns_within(lambda: b.registry.get_logs(run_id))
    returns_within(lambda: b.registry.get_output(run_id))
    returns_within(lambda: b.registry.get_input(run_id))
    out = returns_within(lambda: read_from(b.dir / DATASET_FILE, final=True))
    assert out["value"].items == []
    returns_within(lambda: b.client.get(f"/runs/{run_id}/attention"))
    returns_within(lambda: b.client.get(f"/runs/{run_id}/logs"))
    # ...and writing the logs over a FIFO replaces it rather than blocking.
    returns_within(lambda: b.registry.write_logs(run_id, ["done"]))
    assert (b.dir / LOG_FILE).read_text() == "done\n"


# --- the session handout and the mailer -----------------------------------------------


@test
def test_the_session_handout_is_not_read_through_a_link():
    """W7. The scratch dir is mounted too; a link there would store a host
    file as this account's session and hand it to the account's next run."""
    from worker.executor import _read_handout  # noqa: PLC0415

    b = Box()
    scratch = Path(b._tmp.name) / "scratch"
    scratch.mkdir()
    (scratch / SESSION_OUT_FILE).symlink_to(b.victim)
    assert _read_handout(scratch, b.run.run_id) is None, "read the handout through a link"
    (scratch / SESSION_OUT_FILE).unlink()
    assert _read_handout(scratch, b.run.run_id) is None, "absent is the normal case"
    (scratch / SESSION_OUT_FILE).write_text('{"cookies": []}')
    assert _read_handout(scratch, b.run.run_id) == '{"cookies": []}'


@test
def test_the_report_mail_never_attaches_a_linked_file():
    """W10. The mailer attaches artifacts; a link must not mail a host file."""
    from delivery.mailer import _attach  # noqa: PLC0415

    b = Box()
    link = b.dir / "report.pdf"
    link.symlink_to(b.victim)
    message = EmailMessage()
    message.set_content("report")
    assert _attach(message, link) is False
    assert SECRET not in message.as_string()
    real = b.dir / "real.pdf"
    real.write_bytes(b"%PDF real")
    assert _attach(message, real) is True


def main() -> int:
    print(f"running {len(TESTS)} run-directory tests\n")
    failed = 0
    for fn in TESTS:
        name = fn.__name__.removeprefix("test_")
        try:
            fn()
            print(f"  ok    {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{'all ' + str(len(TESTS)) + ' tests passed' if not failed else f'{failed} FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    # A hung FIFO test leaves a daemon thread blocked in open(); exit hard so
    # the suite reports instead of waiting on it.
    code = main()
    sys.stdout.flush()
    os._exit(code)
