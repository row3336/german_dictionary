"""Apply all pending DB migrations to a words.db — SAFELY (idempotent).

Uses bot.common.db_safety.apply_migrations_safely():
- snapshots the DB BEFORE any schema change (rollback point),
- runs bot.storage.init_db() (creates/updates schema, migrates data),
- runs a healthcheck (integrity_check + foreign_key_check + row counts),
- records the migration in the db_events timeline.

Usage on the server:
    python3 -m scripts.migrate_db --db ./data/words.db [--dump-dir ./data/backups]

Safe to run multiple times. The pre-migration backup + reported counts let you
confirm nothing was lost, and roll back from the snapshot if needed.
"""

import argparse
import sys
from pathlib import Path

from bot.common.db_safety import apply_migrations_safely


def main():
    parser = argparse.ArgumentParser(description="Apply DB migrations safely (idempotent).")
    parser.add_argument("--db", required=True, help="Path to words.db")
    parser.add_argument(
        "--dump-dir",
        default=None,
        help="Where to write the pre-migration backup (default: <db dir>/backups)",
    )
    args = parser.parse_args()

    dump_dir = args.dump_dir or str(Path(args.db).resolve().parent / "backups")
    print(f"DB: {args.db}")
    print(f"backup dir: {dump_dir}")

    report = apply_migrations_safely(args.db, dump_dir)

    print(f"version: v{report['from_version']} -> v{report['to_version']} "
          f"(migrated={report['migrated']}, fresh={report['fresh']})")
    if report["backup"]:
        print(f"pre-migration backup: {report['backup']}")
    if report["counts_before"] is not None:
        print(f"counts before: {report['counts_before']}")
    print(f"counts after:  {report['counts_after']}")

    health = report["healthcheck"]
    print(f"healthcheck: ok={health['ok']} integrity={health['integrity']} "
          f"fk_violations={health['fk_violations']}")

    if not health["ok"]:
        print("ERROR: healthcheck FAILED — investigate; restore from the pre-migration backup if needed.")
        return 1

    # Guard: core tables must not shrink across a migration.
    before, after = report["counts_before"], report["counts_after"]
    if before:
        for table in ("words", "progress", "quiz_events"):
            if table in before and before.get(table) != after.get(table):
                print(f"WARNING: {table} count changed {before.get(table)} -> {after.get(table)}")
                return 1
    print("OK: schema applied, healthcheck passed, core counts unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
