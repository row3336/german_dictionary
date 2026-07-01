"""One-off maintenance: apply a reviewed translations file (produced by
scripts.generate_translations_review) into the DB's ukrainian/english columns.

This is the ONLY step of the two that touches the DB. Run it only after the
review file has been checked. Backs up the DB file first (plain copy, next to
the original, timestamped) so the previous state is always recoverable.

Entries with a null *_new value (failed generation, or dropped during review)
are skipped for that column — the existing DB value is left untouched.

Usage (from repo root, server or local):
    python3 -m scripts.apply_translations_review --db ./data/words.db \
        --review ./claude_docs/translations_review.json
"""

import argparse
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="./data/words.db")
    parser.add_argument("--review", default="./claude_docs/translations_review.json")
    args = parser.parse_args()

    db_path = Path(args.db)
    review_path = Path(args.review)
    entries = json.loads(review_path.read_text(encoding="utf-8"))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = db_path.with_name(f"{db_path.stem}.pre_translations_apply.{stamp}{db_path.suffix}")
    shutil.copy2(db_path, backup_path)
    print(f"Backup: {backup_path}")

    conn = sqlite3.connect(db_path)
    updated_uk = 0
    updated_en = 0
    skipped = 0
    with conn:
        for entry in entries:
            uk = entry.get("ukrainian_new")
            en = entry.get("english_new")
            if uk is None and en is None:
                skipped += 1
                continue
            if uk is not None:
                conn.execute("UPDATE words SET ukrainian = ? WHERE id = ?", (uk, entry["id"]))
                updated_uk += 1
            if en is not None:
                conn.execute("UPDATE words SET english = ? WHERE id = ?", (en, entry["id"]))
                updated_en += 1
    conn.close()

    print(f"Updated ukrainian: {updated_uk}, updated english: {updated_en}, skipped (both null): {skipped}")
    print("Done.")


if __name__ == "__main__":
    main()
