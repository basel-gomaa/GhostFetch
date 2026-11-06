#!/usr/bin/env python3
"""item_processor Actor.

A second reference Actor, structurally different from hello_world in every way
the contract allows, to prove the platform is not shaped around one Actor:

    * it has a real third-party dependency (python-slugify) doing the work
    * its successful output is a JSON ARRAY, not an object
    * it can fail PART WAY THROUGH, after emitting real progress

What it does: turns a count into that many processed records, logging each one
as it goes. If `fail_at` names an item, processing raises there -- the completed
items stay in the log, and no output file is written, because a partial result
must never be mistaken for a whole one.

Input:

    {"count": 5, "fail_at": null, "label": "Sample Item", "delay_ms": 0}

`delay_ms` pauses before each item. It defaults to 0 and exists so the platform's
own tests can hold a run open long enough to stage a Worker crash.

Output (success only):

    [{"index": 1, "title": "Sample Item #1", "slug": "sample-item-1"}, ...]

Run it under the worker, or by hand:

    ACTOR_INPUT_PATH=in.json ACTOR_OUTPUT_PATH=out.json python actor.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

# The dependency under test. It is imported at module scope on purpose: if the
# image's dependency layer did not install, the Actor fails loudly at startup
# rather than half way through a run.
from slugify import slugify

# Kept in sync with shared/models.py. Duplicated rather than imported so the
# image has no dependency on the platform's source tree.
DEFAULT_INPUT_PATH = "/actor/storage/input.json"
DEFAULT_OUTPUT_PATH = "/actor/storage/output.json"

MAX_COUNT = 1000
MAX_DELAY_MS = 60_000
DEFAULT_LABEL = "Sample Item"


class InputError(ValueError):
    """The run was never viable: the input document is wrong."""


class ProcessingError(RuntimeError):
    """The run started, produced real progress, and then failed."""


def log(message: str) -> None:
    """Actor logs are just stdout; the worker captures and stores them."""
    print(f"[item_processor] {message}", flush=True)


def read_input(path: str) -> dict[str, Any]:
    """Load the run's input document, or fail with an actionable message."""
    if not os.path.exists(path):
        raise InputError(f"no input file at {path}")

    with open(path, encoding="utf-8") as handle:
        raw = handle.read().strip()

    if not raw:
        raise InputError(f"input file {path} is empty")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InputError(f"input file {path} is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise InputError(f"input must be a JSON object, got {type(payload).__name__}")

    return payload


def write_output(path: str, payload: Any) -> None:
    """Write the run's result where the platform will read it back."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def parse_settings(payload: dict[str, Any]) -> tuple[int, int | None, str, int]:
    """Validate the input up front, before any item is processed."""
    count = payload.get("count")
    # bool is an int subclass; True would otherwise pass as count == 1.
    if not isinstance(count, int) or isinstance(count, bool):
        raise InputError('input field "count" is required and must be an integer')
    if not 1 <= count <= MAX_COUNT:
        raise InputError(f'input field "count" must be between 1 and {MAX_COUNT}, got {count}')

    fail_at = payload.get("fail_at")
    if fail_at is not None:
        if not isinstance(fail_at, int) or isinstance(fail_at, bool):
            raise InputError('input field "fail_at" must be an integer or null')
        if fail_at < 1:
            raise InputError(f'input field "fail_at" must be 1 or greater, got {fail_at}')

    label = payload.get("label", DEFAULT_LABEL)
    if not isinstance(label, str) or not label.strip():
        raise InputError('input field "label" must be a non-empty string')

    delay_ms = payload.get("delay_ms", 0)
    if not isinstance(delay_ms, int) or isinstance(delay_ms, bool):
        raise InputError('input field "delay_ms" must be an integer')
    if not 0 <= delay_ms <= MAX_DELAY_MS:
        raise InputError(f'input field "delay_ms" must be between 0 and {MAX_DELAY_MS}')

    return count, fail_at, label.strip(), delay_ms


def process_item(index: int, label: str) -> dict[str, Any]:
    """One unit of work. slugify() is what makes the dependency load-bearing."""
    title = f"{label} #{index}"
    return {"index": index, "title": title, "slug": slugify(title)}


def run(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Process every item in order, failing at `fail_at` if it is set."""
    count, fail_at, label, delay_ms = parse_settings(payload)
    log(f"processing {count} item(s), label={label!r}, fail_at={fail_at}, delay_ms={delay_ms}")

    results: list[dict[str, Any]] = []
    for index in range(1, count + 1):
        if index == fail_at:
            # Deliberate mid-run failure: the work already done is in the log.
            log(f"partial progress: {len(results)} of {count} item(s) completed before failure")
            raise ProcessingError(f"deliberate failure while processing item {index} of {count}")

        if delay_ms:
            # Lets a test hold a run open long enough to stage a Worker crash.
            time.sleep(delay_ms / 1000)

        record = process_item(index, label)
        results.append(record)
        log(f"item {index}/{count} -> {record['slug']}")

    log(f"processed {len(results)} item(s)")
    return results


def main() -> int:
    input_path = os.environ.get("ACTOR_INPUT_PATH", DEFAULT_INPUT_PATH)
    output_path = os.environ.get("ACTOR_OUTPUT_PATH", DEFAULT_OUTPUT_PATH)
    run_id = os.environ.get("ACTOR_RUN_ID", "local")

    log(f"run {run_id} starting")
    log(f"reading input from {input_path}")

    try:
        payload = read_input(input_path)
        results = run(payload)
    except (InputError, ProcessingError) as exc:
        # Both are the Actor's own failure to report: a clear message on stderr
        # plus a non-zero exit. No output file is written, so the platform can
        # never read a partial array as a complete result.
        print(f"[item_processor] ERROR: {exc}", file=sys.stderr, flush=True)
        return 1

    write_output(output_path, results)
    log(f"wrote {len(results)} record(s) to {output_path}")
    log("run finished successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
