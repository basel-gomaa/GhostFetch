"""Structured, correlate-by-run-id platform events.

The platform already logs progress as human text to stderr ("[consumer] picked
up run ..."). That is fine to read and miserable to CORRELATE: answering "what
happened to run X?" means grepping free-form strings. This adds a second,
OPT-IN rendering of the same lifecycle events as one JSON object per line,
always carrying `run_id`, so a log processor can group a run's whole story.

    ACTOR_LOG_FORMAT=json    -> one JSON object per line on stderr
    (anything else / unset)  -> the existing human text, byte-for-byte

It is deliberately tiny and dependency-free. It does NOT replace the modules'
own log() helpers or reach into any Actor -- an Actor's own logging is its own
concern and is untouched. This is for the PLATFORM's own lifecycle points (a job
picked up, started, finished, reclaimed), which is where correlation actually
matters.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any

LOG_FORMAT_ENV = "ACTOR_LOG_FORMAT"

# One lock for every line the platform writes to stderr, whichever module
# writes it.
_WRITE_LOCK = threading.Lock()


def write_line(text: str, stream=None) -> None:
    """Write `text` plus its newline as ONE write, then flush.

    `print()` writes the text and the newline separately. That is harmless on a
    line-buffered stream, but under PYTHONUNBUFFERED=1 each becomes its own OS
    write, and a second thread's line can land between them:
    `[consumer] all 1 slot(s) busy{"event": "job_picked_up", ...}`. Anything
    reading one JSON object per line then drops that event. The Worker logs
    from several threads at once (job threads, the container-output pump, the
    dataset ingestor), so every platform log line goes through here.
    """
    target = stream if stream is not None else sys.stderr
    with _WRITE_LOCK:
        target.write(text + "\n")
        target.flush()


def json_logging_enabled() -> bool:
    return os.environ.get(LOG_FORMAT_ENV, "").strip().lower() == "json"


def event(
    component: str,
    event_name: str,
    *,
    run_id: str | None = None,
    stream=None,
    **fields: Any,
) -> None:
    """Emit one structured platform event.

    A no-op for the log CONTENT unless ACTOR_LOG_FORMAT=json -- callers keep
    their own human log() line, and this rides alongside it only when a machine
    is meant to read the stream. Every field is optional except the component
    and the event name; `run_id` is promoted to a top-level key because it is
    the correlation handle the whole point rests on.

    Never raises: an observability call must not be able to fail a run. A field
    that will not serialise is coerced to its repr rather than blowing up.
    """
    if not json_logging_enabled():
        return

    record: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        "component": component,
        "event": event_name,
    }
    if run_id is not None:
        record["run_id"] = run_id
    record.update(fields)

    try:
        line = json.dumps(record, ensure_ascii=False, default=repr)
    except (TypeError, ValueError):
        line = json.dumps(
            {"component": component, "event": event_name, "run_id": run_id,
             "error": "unserialisable event fields"},
            ensure_ascii=False,
        )

    write_line(line, stream)
