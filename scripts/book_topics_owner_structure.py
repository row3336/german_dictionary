"""Explicit plan/apply/rollback CLI for the owner book structure (PONS chapters, GSV).

Default is a read-only plan. ``--apply`` and ``--rollback`` reserve the database
(BEGIN IMMEDIATE), write a verified backup to a new file, then run the whole
operation in one transaction. A matching receipt makes ``--apply`` a no-op.
The operator stops the bot beforehand; nothing runs at startup.

Examples::

    python3 -m scripts.book_topics_owner_structure --db ./data/words.db
    python3 -m scripts.book_topics_owner_structure --db ./data/words.db --apply --backup ./pre.db
"""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.common import book_topic_structure as structure
from bot.common.db_safety import _connection_fingerprint, run_healthcheck
from scripts.backfill_existing_a1_filters import open_database, verified_backup

MANIFEST = Path(__file__).resolve().with_name("pons_chapter_topics_v1.json")


def read_manifest(path=MANIFEST):
    return structure.load_manifest(Path(path).read_text(encoding="utf-8"))


def plan_file(db_path, manifest_path=MANIFEST):
    manifest = read_manifest(manifest_path)
    conn = open_database(db_path)
    try:
        conn.execute("BEGIN")
        return structure.plan(conn, manifest)
    finally:
        conn.close()


def _write(db_path, manifest_path, backup_path, operation):
    manifest = read_manifest(manifest_path)
    conn = open_database(db_path, readonly=False)
    committed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        if operation is structure.apply:
            report = structure.plan(conn, manifest)
            if report["state"] == "already_applied":
                conn.rollback()
                return report
        backup = verified_backup(db_path, backup_path, _connection_fingerprint(conn))
        report = operation(conn, manifest)
        conn.commit()
        committed = True
    except Exception:
        if not committed:
            conn.rollback()
        raise
    finally:
        conn.close()
    health = run_healthcheck(db_path)
    if not health["ok"]:
        raise structure.TopicStructureError("post-commit healthcheck failed; restore the backup")
    return {**report, "backup": backup, "healthcheck": {"ok": True, "integrity": health["integrity"]}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="existing SQLite database")
    parser.add_argument("--manifest", default=str(MANIFEST), help="reviewed manifest (digest is pinned)")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="apply after a verified backup")
    action.add_argument("--rollback", action="store_true", help="reverse an applied receipt after a backup")
    parser.add_argument("--backup", help="new pre-write snapshot file (required with --apply/--rollback)")
    args = parser.parse_args(argv)
    if (args.apply or args.rollback) and not args.backup:
        parser.error("--backup is required with --apply/--rollback")
    try:
        if args.apply:
            report = _write(args.db, args.manifest, args.backup, structure.apply)
        elif args.rollback:
            report = _write(args.db, args.manifest, args.backup, structure.rollback)
        else:
            report = plan_file(args.db, args.manifest)
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"state": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
