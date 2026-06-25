"""Apply all pending DB migrations to a words.db (idempotent).

Runs bot.storage.init_db(), which creates/updates the schema and migrates data:
- renames legacy SM2 settings keys to FSRS (preserving values),
- adds words.updated_at (backfilled to created_at for existing rows),
- plus any other schema/column migrations.

Usage on the server:
    python3 -m scripts.migrate_db --db ./data/words.db

Safe to run multiple times. Make a backup first; the script also reports
row counts before/after so you can confirm nothing was lost.
"""

import argparse
import sqlite3
import sys

from bot.storage import init_db


def _counts(db_path):
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            out = {}
            for table in ("words", "progress", "quiz_events", "app_settings", "learning_items"):
                try:
                    out[table] = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
                except sqlite3.Error:
                    out[table] = "-"
            return out
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {"error": str(exc)}


def _settings_keys(db_path, like):
    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT key FROM app_settings WHERE key LIKE ? ORDER BY key", (like,)
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def main():
    parser = argparse.ArgumentParser(description="Apply DB migrations (idempotent).")
    parser.add_argument("--db", required=True, help="Path to words.db")
    args = parser.parse_args()

    print(f"DB: {args.db}")
    before = _counts(args.db)
    print(f"before: {before}")
    print(f"  sm2_* keys before: {_settings_keys(args.db, 'sm2_%')}")

    init_db(args.db)
    print("migrations applied (init_db).")

    after = _counts(args.db)
    print(f"after:  {after}")
    print(f"  sm2_* keys after:  {_settings_keys(args.db, 'sm2_%')} (expect [])")
    print(f"  fsrs_* keys after: {_settings_keys(args.db, 'fsrs_%')}")

    if isinstance(before, dict) and isinstance(after, dict) and "error" not in before:
        for table in ("words", "progress", "quiz_events"):
            if before.get(table) != after.get(table):
                print(f"WARNING: {table} count changed {before.get(table)} -> {after.get(table)}")
                return 1
    print("OK: core row counts unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
