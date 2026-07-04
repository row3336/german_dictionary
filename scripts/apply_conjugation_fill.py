"""One-off maintenance: apply a hand-filled conjugation file (spec #3) into the
DB's words.conjugation_praesens / conjugation_praeteritum columns.

The fill file is produced for manual entry (see claude_docs/conjugation_fill.json):
each verb carries pronoun-keyed Präsens (and, for the closed list, Präteritum)
blocks. This script converts those to the canonical 6-line text, validates every
non-empty block with the SAME validator the bot uses (bot.common.conjugation), and
writes the valid ones.

Review-first: run WITHOUT --apply to validate + print a report only (no DB touch).
Re-run WITH --apply to back up the DB (plain timestamped copy) and write.

A block whose 6 forms are all empty is skipped (fill gradually). A partially filled
block (some forms missing) is an ERROR — it is reported and never written.

Usage (from repo root):
    python3 -m scripts.apply_conjugation_fill --db ./data/words.db \
        --file ./claude_docs/conjugation_fill.json          # dry-run report
    python3 -m scripts.apply_conjugation_fill --db ./data/words.db \
        --file ./claude_docs/conjugation_fill.json --apply   # back up + write
"""

import argparse
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from bot.common.conjugation import PRONOUNS, parse_conjugation


def block_to_text(block):
    """Pronoun-keyed dict -> canonical text, or '' if all forms are blank."""
    if not block:
        return ""
    forms = {p: str(block.get(p, "") or "").strip() for p in PRONOUNS}
    if not any(forms.values()):
        return ""
    return "\n".join(f"{p} {forms[p]}" for p in PRONOUNS)


def build_updates(doc):
    """Validate every filled block. Returns (updates, errors, empty_count).

    updates: list of (id, infinitive, praesens_text, praeteritum_text)
    errors:  list of (infinitive, which, message) for partial/invalid blocks
    """
    updates = []
    errors = []
    empty = 0
    for entry in doc.get("verbs", []):
        wid = entry.get("id")
        inf = entry.get("infinitive", "")
        praesens_raw = block_to_text(entry.get("conjugation_praesens"))
        praeteritum_raw = block_to_text(entry.get("conjugation_praeteritum"))

        praesens = ""
        praeteritum = ""
        ok = True
        if praesens_raw:
            try:
                praesens = parse_conjugation(praesens_raw)
            except ValueError as exc:
                errors.append((inf, "praesens", str(exc).splitlines()[0]))
                ok = False
        if praeteritum_raw:
            try:
                praeteritum = parse_conjugation(praeteritum_raw)
            except ValueError as exc:
                errors.append((inf, "praeteritum", str(exc).splitlines()[0]))
                ok = False

        if not ok:
            continue
        if not praesens and not praeteritum:
            empty += 1
            continue
        updates.append((wid, inf, praesens, praeteritum))
    return updates, errors, empty


def apply_updates(db_path, updates):
    """Write validated blocks. Präsens is only overwritten when present (a filled
    Präteritum-only entry never blanks an existing Präsens)."""
    conn = sqlite3.connect(db_path)
    with conn:
        for wid, _inf, praesens, praeteritum in updates:
            if praesens:
                conn.execute("UPDATE words SET conjugation_praesens = ? WHERE id = ?", (praesens, wid))
            if praeteritum:
                conn.execute("UPDATE words SET conjugation_praeteritum = ? WHERE id = ?", (praeteritum, wid))
    conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="./data/words.db")
    parser.add_argument("--file", default="./claude_docs/conjugation_fill.json")
    parser.add_argument("--apply", action="store_true", help="Back up the DB and write (default: dry-run report only)")
    args = parser.parse_args()

    doc = json.loads(Path(args.file).read_text(encoding="utf-8"))
    updates, errors, empty = build_updates(doc)

    print(f"Verbs in file: {len(doc.get('verbs', []))}")
    print(f"Ready to write: {len(updates)} | still empty: {empty} | errors: {len(errors)}")
    for inf, which, msg in errors:
        print(f"  ERROR  {inf} [{which}]: {msg}")

    if not args.apply:
        print("\nDry run (no DB changes). Re-run with --apply to back up and write.")
        return

    if errors:
        print("\nRefusing to apply while there are errors — fix them first.")
        return

    db_path = Path(args.db)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = db_path.with_name(f"{db_path.stem}.pre_conjugation_apply.{stamp}{db_path.suffix}")
    shutil.copy2(db_path, backup_path)
    print(f"Backup: {backup_path}")

    apply_updates(db_path, updates)
    print(f"Wrote conjugation for {len(updates)} verbs. Done.")


if __name__ == "__main__":
    main()
