#!/usr/bin/env python3
"""Import the existing filesystem run records into the database. One-shot, idempotent.

    python3 tools/import_runs.py --dry-run
    python3 tools/import_runs.py

Every run that existed before multi-tenancy is imported with `owner_id NULL`,
which means it belongs to nobody and is visible to admins only. That is the
honest answer: these runs were made by a single-user CLI and there is no user to
attribute them to. Guessing an owner would be worse than admitting none.

NOTHING IS MOVED OR DELETED. The run directories stay exactly where they are and
keep serving their artifacts; this only copies the metadata out of each run.json.
Safe to re-run: ON CONFLICT DO NOTHING, so an already-imported run is left as it
is rather than being reset to whatever the (now stale) run.json says.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from db.pool import pool  # noqa: E402
from registry.run_registry import RegistryError, RunRegistry  # noqa: E402
from shared.models import Run, is_valid_run_id  # noqa: E402

INSERT = """
INSERT INTO runs (run_id, owner_id, actor, status, created_at, started_at,
                  finished_at, duration_seconds, exit_code, error, run_dir,
                  log_lines, attempts, input)
VALUES (%(run_id)s, NULL, %(actor)s, %(status)s, %(created_at)s, %(started_at)s,
        %(finished_at)s, %(duration_seconds)s, %(exit_code)s, %(error)s,
        %(run_dir)s, %(log_lines)s, %(attempts)s, %(input)s)
ON CONFLICT (run_id) DO NOTHING
"""


def row_for(run: Run, run_dir: Path) -> dict:
    return {
        "run_id": run.run_id,
        "actor": run.actor,
        "status": run.status.value,
        # A record written before the registry existed may carry neither
        # created_at nor started_at; Run.from_dict already falls back, and an
        # empty string here must become NULL rather than a bad timestamp.
        "created_at": run.created_at or None,
        "started_at": run.started_at or None,
        "finished_at": run.finished_at or None,
        "duration_seconds": run.duration_seconds,
        "exit_code": run.exit_code,
        "error": run.error,
        "run_dir": str(run_dir),
        "log_lines": run.log_lines or 0,
        "attempts": run.attempts or 0,
        "input": json.dumps(run.input or {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Import filesystem runs into the database")
    parser.add_argument("--dry-run", action="store_true", help="report, change nothing")
    parser.add_argument("--root", default=None, help="override the runs directory")
    args = parser.parse_args()

    registry = RunRegistry(Path(args.root) if args.root else None)
    root = registry.root
    if not root.is_dir():
        print(f"no runs directory at {root}", file=sys.stderr)
        return 0

    found = imported = skipped = unreadable = 0
    rows: list[dict] = []

    for entry in sorted(p for p in root.iterdir() if p.is_dir()):
        record = entry / "run.json"
        if not record.is_file():
            # A directory without a record was never registered, so by the
            # platform's own definition it is not a run.
            continue
        found += 1
        try:
            # Through the registry's own reader, not json.loads on the path: the
            # record was writable by the run's container, so it is opened as a
            # regular file only, and one naming a different run is refused
            # rather than imported -- its run_dir is never taken from it.
            if not is_valid_run_id(entry.name):
                raise ValueError("not a run directory name")
            rows.append(row_for(registry._read_record(entry, entry.name), entry))
        except (OSError, ValueError, RegistryError) as exc:
            unreadable += 1
            print(f"  skipping {entry.name}: {exc}", file=sys.stderr)

    if args.dry_run:
        print(f"{found} run record(s) found, {len(rows)} importable, {unreadable} unreadable")
        return 0

    with pool().connection() as conn:
        with conn.transaction():
            for row in rows:
                imported += conn.execute(INSERT, row).rowcount
    skipped = len(rows) - imported

    print(
        f"imported {imported} run(s); {skipped} already present; "
        f"{unreadable} unreadable; {found} record(s) on disk"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
