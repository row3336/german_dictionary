"""Reconcile the initial A1 book-topic catalogue safely and idempotently.

The command is a dry-run by default.  ``--apply`` creates a WAL-safe backup,
then performs the whole reconciliation in one transaction.  It is intended for
the explicitly approved initial A1 catalogue; it refuses unexpected topic
levels/descriptions instead of overwriting later curation.

Examples::

    python3 -m scripts.reconcile_book_topics --db ./data/words.db
    python3 -m scripts.reconcile_book_topics --db ./data/words.db --apply
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from bot.common.db_safety import (
    SCHEMA_VERSION,
    backup_db_with_event,
    run_healthcheck,
)


MERGES = {"01": "1", "02": "4", "04": "6"}
DESCRIPTIONS = {
    "0": "Anfang",
    "03": "Im Deutschkurs",
    "011": "Wir gehen shoppen",
    "012": "Endlich Frühling",
    "1": "Willkommen!",
    "2": "Alte Heimat, neue Heimat",
    "3": "Häuser und Wohnungen",
    "4": "Familienleben",
    "5": "Der Tag und die Woche",
    "6": "Guten Appetit!",
    "7": "Arbeit und Beruf",
    "nom": "Nomen+",
    "verb": "Verben+",
}
EXPECTED_TOPICS = set(DESCRIPTIONS) | set(MERGES)
FINAL_TOPICS = set(DESCRIPTIONS)


class ReconciliationError(RuntimeError):
    """Raised when the DB does not match the approved reconciliation scope."""


def _topic_counts(conn):
    return {
        str(name): int(count)
        for name, count in conn.execute(
            "SELECT topic, COUNT(*) FROM words GROUP BY topic ORDER BY topic"
        )
    }


def _topic_rows(conn):
    return {
        str(name): {"description": str(description or ""), "level": str(level or "")}
        for name, description, level in conn.execute(
            "SELECT name, description, level FROM topics ORDER BY name"
        )
    }


def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _normalize_filter(raw):
    text = str(raw or "").strip()
    if not text or text.lower() in {"all", "*"}:
        return text
    tokens = []
    for chunk in text.replace(",", "+").split("+"):
        token = chunk.strip()
        if not token:
            continue
        token = MERGES.get(token, token)
        if token not in tokens:
            tokens.append(token)
    return "+".join(tokens)


def validate_topic_reconciliation(conn):
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version != SCHEMA_VERSION:
        raise ReconciliationError(
            f"schema v{version}; expected v{SCHEMA_VERSION} (run scripts.migrate_db first)"
        )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(topics)")}
    if not {"name", "description", "level"}.issubset(columns):
        raise ReconciliationError("topics table is missing required columns")

    rows = _topic_rows(conn)
    unknown = set(rows) - EXPECTED_TOPICS
    missing_final = FINAL_TOPICS - set(rows)
    if unknown:
        raise ReconciliationError(f"unexpected topics: {sorted(unknown)}")
    if missing_final:
        raise ReconciliationError(f"missing destination topics: {sorted(missing_final)}")

    conflicts = []
    for name, row in rows.items():
        if row["level"] not in {"", "A1"}:
            conflicts.append(f"{name}: unexpected level {row['level']!r}")
        desired = DESCRIPTIONS.get(name)
        if desired is not None and row["description"] not in {"", desired}:
            conflicts.append(
                f"{name}: description {row['description']!r} conflicts with {desired!r}"
            )
    if conflicts:
        raise ReconciliationError("; ".join(conflicts))


def _update_topic_filters(conn):
    changed = {"app_settings": 0, "user_settings": 0}
    for table in changed:
        if not _table_exists(conn, table):
            continue
        key_columns = "key, value" if table == "app_settings" else "user_id, key, value"
        rows = conn.execute(
            f"SELECT {key_columns} FROM {table} WHERE key = 'topic_filter'"
        ).fetchall()
        for row in rows:
            old = row[-1]
            new = _normalize_filter(old)
            if new == old:
                continue
            if table == "app_settings":
                conn.execute("UPDATE app_settings SET value=? WHERE key='topic_filter'", (new,))
            else:
                conn.execute(
                    "UPDATE user_settings SET value=? WHERE user_id=? AND key='topic_filter'",
                    (new, row[0]),
                )
            changed[table] += 1
    return changed


def reconcile_topics(conn):
    before_topics = _topic_rows(conn)
    before_counts = _topic_counts(conn)
    word_count = conn.execute("SELECT COUNT(*) FROM words").fetchone()[0]

    moved = {}
    for source, destination in MERGES.items():
        cursor = conn.execute("UPDATE words SET topic=? WHERE topic=?", (destination, source))
        moved[source] = {"destination": destination, "words": cursor.rowcount}

    jobs_changed = 0
    if _table_exists(conn, "vf_add_jobs"):
        for source, destination in MERGES.items():
            cursor = conn.execute(
                "UPDATE vf_add_jobs SET topic=? WHERE topic=?", (destination, source)
            )
            jobs_changed += cursor.rowcount

    filter_changes = _update_topic_filters(conn)
    conn.executemany("DELETE FROM topics WHERE name=?", ((name,) for name in MERGES))
    conn.executemany(
        "UPDATE topics SET description=?, level='A1' WHERE name=?",
        ((description, name) for name, description in DESCRIPTIONS.items()),
    )

    after_counts = _topic_counts(conn)
    after_word_count = conn.execute("SELECT COUNT(*) FROM words").fetchone()[0]
    remaining_sources = {
        name: after_counts.get(name, 0) for name in MERGES if after_counts.get(name, 0)
    }
    final_rows = _topic_rows(conn)
    if after_word_count != word_count:
        raise ReconciliationError(f"word count changed: {word_count} -> {after_word_count}")
    if remaining_sources:
        raise ReconciliationError(f"source topics still referenced: {remaining_sources}")
    if set(final_rows) != FINAL_TOPICS:
        raise ReconciliationError(
            f"final topic set mismatch: {sorted(final_rows)} != {sorted(FINAL_TOPICS)}"
        )
    for name, description in DESCRIPTIONS.items():
        if final_rows[name] != {"description": description, "level": "A1"}:
            raise ReconciliationError(f"final metadata mismatch for topic {name}")

    return {
        "word_count_before": word_count,
        "word_count_after": after_word_count,
        "topic_count_before": len(before_topics),
        "topic_count_after": len(final_rows),
        "word_counts_before": before_counts,
        "word_counts_after": after_counts,
        "moved": moved,
        "topic_filter_rows_changed": filter_changes,
        "vf_add_jobs_changed": jobs_changed,
        "topics_before": before_topics,
        "topics_after": final_rows,
    }


def reconcile_database(db_path, *, apply=False, dump_dir=None):
    """Validate and preview/apply the approved reconciliation; return a report."""
    db = Path(db_path)
    if not db.is_file():
        raise ReconciliationError(f"DB does not exist: {db}")

    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        validate_topic_reconciliation(conn)
        conn.execute("BEGIN IMMEDIATE")
        report = reconcile_topics(conn)
        if apply:
            conn.rollback()
        else:
            conn.rollback()
            report.update({"applied": False, "backup": None})
            return report
    finally:
        conn.close()

    backup_dir = Path(dump_dir) if dump_dir else db.resolve().parent / "backups"
    backup = backup_db_with_event(db, backup_dir, "book-topics")
    if backup is None:
        raise ReconciliationError("pre-apply backup was not created")

    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        validate_topic_reconciliation(conn)
        conn.execute("BEGIN IMMEDIATE")
        report = reconcile_topics(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    health = run_healthcheck(db)
    if not health["ok"]:
        raise ReconciliationError(f"post-apply healthcheck failed: {health}")
    report.update({"applied": True, "backup": str(backup), "healthcheck": health})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Path to a schema-v14 words.db")
    parser.add_argument("--apply", action="store_true", help="Apply after backup (default: dry-run)")
    parser.add_argument("--dump-dir", help="Backup directory (default: <db dir>/backups)")
    parser.add_argument("--report", help="Optional JSON report path")
    args = parser.parse_args()

    try:
        report = reconcile_database(args.db, apply=args.apply, dump_dir=args.dump_dir)
    except ReconciliationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
