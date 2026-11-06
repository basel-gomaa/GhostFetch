#!/usr/bin/env python3
"""hello_world Actor.

The reference implementation of the Actor contract: read a JSON object from the
path the platform points at, do the work, write a JSON result back, log to
stdout, exit 0 on success and non-zero on failure.

Nothing here is hardcoded -- neither the name nor the file locations. Run it
under the worker, or by hand:

    ACTOR_INPUT_PATH=in.json ACTOR_OUTPUT_PATH=out.json python actor.py

Standard library only, on purpose: an Actor image should carry only what its own
job needs.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

# Kept in sync with shared/models.py. Duplicated rather than imported so the
# image has no dependency on the platform's source tree.
DEFAULT_INPUT_PATH = "/actor/storage/input.json"
DEFAULT_OUTPUT_PATH = "/actor/storage/output.json"


def log(message: str) -> None:
    """Actor logs are just stdout; the worker captures and stores them."""
    print(f"[hello_world] {message}", flush=True)


def read_input(path: str) -> dict[str, Any]:
    """Load the run's input document, or fail with an actionable message."""
    if not os.path.exists(path):
        raise ValueError(f"no input file at {path}")

    with open(path, encoding="utf-8") as handle:
        raw = handle.read().strip()

    if not raw:
        raise ValueError(f"input file {path} is empty")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"input file {path} is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"input must be a JSON object, got {type(payload).__name__}")

    return payload


def write_output(path: str, payload: Any) -> None:
    """Write the run's result where the platform will read it back."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """The Actor's actual work. Everything else in this file is the contract."""
    name = payload.get("name")

    if not isinstance(name, str) or not name.strip():
        raise ValueError('input field "name" is required and must be a non-empty string')

    return {"message": f"Hello {name.strip()}"}


def main() -> int:
    input_path = os.environ.get("ACTOR_INPUT_PATH", DEFAULT_INPUT_PATH)
    output_path = os.environ.get("ACTOR_OUTPUT_PATH", DEFAULT_OUTPUT_PATH)
    run_id = os.environ.get("ACTOR_RUN_ID", "local")

    log(f"run {run_id} starting")
    log(f"reading input from {input_path}")

    try:
        payload = read_input(input_path)
        result = run(payload)
    except ValueError as exc:
        # A bad input is the Actor's fault to report, not to crash on: a clear
        # message on stderr plus a non-zero exit is what the worker records.
        print(f"[hello_world] ERROR: {exc}", file=sys.stderr, flush=True)
        return 1

    write_output(output_path, result)
    log(f"wrote output to {output_path}")
    log("run finished successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
