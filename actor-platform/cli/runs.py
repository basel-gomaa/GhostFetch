#!/usr/bin/env python3
"""`runs` -- inspect past Actor executions.

A read-only client of the Run Registry. It executes nothing and writes nothing;
everything it shows comes from registry/run_registry.py, which is the point:
this is the first consumer of that API rather than its author.

    python3 cli/runs.py list
    python3 cli/runs.py list --actor hello_world --status FAILED
    python3 cli/runs.py show 20260902-081758
    python3 cli/runs.py logs 20260902-081758
    python3 cli/runs.py output 20260902-081758

Run ids may be abbreviated to any unique prefix.

EXIT CODES differ deliberately from the worker's. Here 0 means the command
worked and 2 means it could not (no such run, corrupt record, bad usage).
There is no exit 1: in this platform that means "the Actor failed", which a
read-only inspection command can never report about itself. Inspecting a FAILED
run is a successful inspection.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from registry.run_registry import (  # noqa: E402
    ArtifactError,
    CorruptRunRecord,
    RegistryError,
    RunRegistry,
)
from shared.models import Run, RunStatus  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 2

DEFAULT_LIMIT = 20


def fail(message: str, *, hint: str | None = None) -> int:
    print(f"[runs] ERROR: {message}", file=sys.stderr)
    if hint:
        print(f"[runs] {hint}", file=sys.stderr)
    return EXIT_ERROR


def _stamp(value: str | None) -> str:
    """ISO timestamps are for machines; drop the T and Z for the table."""
    return value.replace("T", " ").replace("Z", "") if value else "-"


def _duration(value: float | None) -> str:
    return f"{value:.2f}s" if value is not None else "-"


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


# --- commands ---------------------------------------------------------------


def cmd_list(args: argparse.Namespace, registry: RunRegistry) -> int:
    """Show recent runs, newest first."""
    try:
        runs = registry.list(
            actor=args.actor,
            status=args.status,
            limit=args.limit,
            skip_corrupt=args.skip_corrupt,
        )
    except CorruptRunRecord as exc:
        return fail(str(exc), hint="re-run with --skip-corrupt to list the rest")

    if args.json:
        print(_dump([run.to_dict() for run in runs]))
        return EXIT_OK

    if not runs:
        print("no runs found")
        return EXIT_OK

    rows = [
        (
            run.run_id,
            run.actor,
            run.status.value,
            "-" if run.exit_code is None else str(run.exit_code),
            _duration(run.duration_seconds),
            _stamp(run.created_at),
        )
        for run in runs
    ]
    headers = ("RUN ID", "ACTOR", "STATUS", "EXIT", "DURATION", "CREATED")
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]

    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip())
    for row in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())

    print()
    shown = f"{len(runs)} run(s)"
    if args.limit is not None and len(runs) == args.limit:
        shown += f" (limit {args.limit}; use --limit to see more)"
    print(shown)
    return EXIT_OK


def _show_human(run: Run, output: Any, produced: bool) -> None:
    fields: list[tuple[str, str]] = [
        ("run_id", run.run_id),
        ("actor", run.actor),
        ("status", run.status.value),
        ("exit_code", "-" if run.exit_code is None else str(run.exit_code)),
        ("created_at", _stamp(run.created_at)),
        ("started_at", _stamp(run.started_at)),
        ("finished_at", _stamp(run.finished_at)),
        ("duration", _duration(run.duration_seconds)),
        ("log_lines", str(run.log_lines)),
        ("run_dir", run.run_dir),
    ]
    if run.error:
        fields.append(("error", run.error))

    width = max(len(label) for label, _ in fields)
    for label, value in fields:
        print(f"{label.ljust(width)}  {value}")

    print(f"\ninput\n{_dump(run.input)}")
    if not produced:
        # Say why rather than printing a bare null: for a failed run this is
        # the expected state, not a missing field.
        reason = "the run did not succeed" if run.status is not RunStatus.SUCCEEDED else "none"
        print(f"\noutput\n({reason})")
    else:
        print(f"\noutput\n{_dump(output)}")


def cmd_show(args: argparse.Namespace, registry: RunRegistry) -> int:
    """Show one run's full record."""
    run = registry.get(registry.resolve(args.run_id))
    if args.json:
        # Metadata only, matching the record and the API's run detail. The
        # output has its own command, and re-sending it on every inspection is
        # what this record stopped doing.
        print(run.to_json())
        return EXIT_OK

    try:
        output, produced = registry.get_output(run.run_id), True
    except RegistryError:
        output, produced = None, False
    _show_human(run, output, produced)
    return EXIT_OK


def cmd_logs(args: argparse.Namespace, registry: RunRegistry) -> int:
    """Print everything the Actor printed."""
    run_id = registry.resolve(args.run_id)
    try:
        sys.stdout.write(registry.get_logs(run_id))
    except ArtifactError as exc:
        return fail(str(exc), hint=f"run {run_id} is {registry.get(run_id).status.value}")
    return EXIT_OK


def cmd_output(args: argparse.Namespace, registry: RunRegistry) -> int:
    """Print what the Actor produced."""
    run_id = registry.resolve(args.run_id)
    try:
        output = registry.get_output(run_id)
    except ArtifactError as exc:
        run = registry.get(run_id)
        # Non-zero on purpose: `runs output X > result.json` must not quietly
        # write an empty file when the run produced nothing.
        return fail(str(exc), hint=f"run {run_id} is {run.status.value}: {run.error or 'no error recorded'}")
    print(_dump(output))
    return EXIT_OK


def cmd_input(args: argparse.Namespace, registry: RunRegistry) -> int:
    """Print the input the run was started with."""
    run_id = registry.resolve(args.run_id)
    try:
        print(_dump(registry.get_input(run_id)))
    except ArtifactError as exc:
        return fail(str(exc))
    return EXIT_OK


# --- cli --------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="runs",
        description="Inspect past Actor runs. Run ids may be abbreviated to a unique prefix.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="list runs, newest first")
    listing.add_argument("--actor", help="only runs of this Actor")
    listing.add_argument(
        "--status", choices=[s.value for s in RunStatus], help="only runs in this state"
    )
    listing.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT, help=f"max rows (default {DEFAULT_LIMIT})"
    )
    listing.add_argument("--all", action="store_true", help="no limit")
    listing.add_argument(
        "--skip-corrupt", action="store_true", help="ignore records that cannot be read"
    )
    listing.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    listing.set_defaults(handler=cmd_list)

    for name, handler, help_text in (
        ("show", cmd_show, "show one run's full record"),
        ("logs", cmd_logs, "print a run's captured output"),
        ("output", cmd_output, "print what a run produced"),
        ("input", cmd_input, "print the input a run was given"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("run_id", help="full run id or a unique prefix")
        if name == "show":
            cmd.add_argument("--json", action="store_true", help="emit JSON")
        cmd.set_defaults(handler=handler)

    args = parser.parse_args(argv)
    if getattr(args, "all", False):
        args.limit = None
    return args


def main(argv: list[str] | None = None, registry: RunRegistry | None = None) -> int:
    args = parse_args(argv)
    registry = registry if registry is not None else RunRegistry()

    try:
        return args.handler(args, registry)
    except RegistryError as exc:
        return fail(str(exc))
    except BrokenPipeError:
        # `runs logs X | head` closes the pipe early; that is not an error.
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
