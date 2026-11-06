#!/usr/bin/env python3
"""Apply the numbered SQL migrations in db/migrations/.

    python3 db/migrate.py           # apply everything outstanding
    python3 db/migrate.py --status  # what is applied, what is pending

Numbered files rather than Alembic. At this size the whole migration history is
readable in one sitting, and a generated-diff tool would obscure the schema
comments -- which is where the reasoning lives.

APPLIED MIGRATIONS ARE FROZEN. Each file's sha256 is recorded, and a file that
changes after being applied is a hard error rather than a silent no-op: two
deployments would otherwise have different schemas while both reporting the same
version. Fix a mistake with a NEW migration.

Each migration runs inside ONE transaction together with the row recording it,
so a failure half way leaves neither the schema change nor the claim that it was
made. Postgres has transactional DDL, which is what makes that possible.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from db.pool import pool  # noqa: E402

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     text PRIMARY KEY,
    checksum    text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(RuntimeError):
    """The migration history and the files on disk disagree."""


def discover() -> list[Path]:
    """Every migration, in version order.

    Sorted by filename, which is why they are zero-padded: `010_` must come
    after `009_`, and string order gives that only with the padding.
    """
    if not MIGRATIONS_DIR.is_dir():
        return []
    return sorted(p for p in MIGRATIONS_DIR.glob("*.sql") if not p.name.startswith("."))


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def applied() -> dict[str, str]:
    with pool().connection() as conn:
        conn.execute(BOOTSTRAP)
        rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    return {r["version"]: r["checksum"] for r in rows}


def pending() -> list[Path]:
    done = applied()
    out = []
    for path in discover():
        version = path.stem
        if version not in done:
            out.append(path)
        elif done[version] != checksum(path):
            raise MigrationError(
                f"migration {version} was applied but its file has changed since "
                f"(recorded {done[version][:12]}, on disk {checksum(path)[:12]}). "
                f"Applied migrations are frozen -- add a new one instead."
            )
    return out


def migrate() -> list[str]:
    """Apply everything outstanding. Returns the versions applied."""
    done: list[str] = []
    for path in pending():
        version = path.stem
        print(f"[migrate] applying {version}", file=sys.stderr, flush=True)
        # One transaction for the DDL AND the record of it, so a failure cannot
        # leave the schema changed but unrecorded (or the reverse).
        with pool().connection() as conn:
            with conn.transaction():
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                    (version, checksum(path)),
                )
        done.append(version)
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply database migrations")
    parser.add_argument("--status", action="store_true", help="show state and exit")
    args = parser.parse_args()

    try:
        if args.status:
            done = applied()
            for path in discover():
                mark = "applied" if path.stem in done else "PENDING"
                print(f"  {mark:>7}  {path.name}")
            return 0

        versions = migrate()
        print(f"[migrate] {'applied ' + ', '.join(versions) if versions else 'already up to date'}",
              file=sys.stderr)
        return 0
    except MigrationError as exc:
        print(f"[migrate] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"[migrate] failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
