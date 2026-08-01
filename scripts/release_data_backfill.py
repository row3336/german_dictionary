"""Apply the reviewed 2026-08-01 dictionary release payload safely.

Dry-run is the default. ``--apply`` first performs the same complete preview,
creates a WAL-safe backup, and then commits topics, explicit word-field patches,
and synonym pairs in one transaction. Every field uses an old-or-new precondition:
the reviewed old value may be changed, the reviewed new value is an idempotent
no-op, and any third value aborts the whole run.

Production order::

    python3 -m scripts.migrate_db --db ./data/words.db --dry-run
    python3 -m scripts.migrate_db --db ./data/words.db
    python3 -m scripts.release_data_backfill --db ./data/words.db
    python3 -m scripts.release_data_backfill --db ./data/words.db --apply
"""

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from bot.common.db_safety import (
    SCHEMA_VERSION,
    backup_db_with_event,
    log_db_event,
    run_healthcheck,
)
from scripts.reconcile_book_topics import (
    DESCRIPTIONS,
    MERGES,
    ReconciliationError,
    reconcile_topics,
    validate_topic_reconciliation,
)


DEFAULT_PAYLOAD = Path(__file__).with_name("release_data_20260801.json")
ALLOWED_WORD_FIELDS = {
    "type",
    "article",
    "german",
    "plural",
    "infinitive",
    "prateritum",
    "partizip2",
    "russian",
    "ukrainian",
    "english",
    "komparativ",
    "superlativ",
    "example",
    "preposition_type",
    "case_type",
    "conjugation_praesens",
    "conjugation_praeteritum",
    "cefr_level",
}


class ReleaseDataError(RuntimeError):
    """Raised when the payload or target database is outside the approved scope."""


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_payload(path=DEFAULT_PAYLOAD):
    """Load and structurally validate a release payload."""
    payload_path = Path(path)
    if not payload_path.is_file():
        raise ReleaseDataError(f"payload does not exist: {payload_path}")
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseDataError(f"cannot read payload {payload_path}: {exc}") from exc
    if payload.get("format_version") != 1:
        raise ReleaseDataError(f"unsupported payload format: {payload.get('format_version')!r}")
    if payload.get("topics") != {"merges": MERGES, "descriptions": DESCRIPTIONS}:
        raise ReleaseDataError("payload topic catalogue does not match the reviewed implementation")

    word_patches = payload.get("word_patches")
    synonym_pairs = payload.get("synonym_pairs")
    if not isinstance(word_patches, list) or not word_patches:
        raise ReleaseDataError("payload word_patches must be a non-empty list")
    if not isinstance(synonym_pairs, list):
        raise ReleaseDataError("payload synonym_pairs must be a list")

    seen_ids = set()
    for patch in word_patches:
        if not isinstance(patch, dict):
            raise ReleaseDataError(f"invalid word patch: {patch!r}")
        word_id = str(patch.get("id") or "")
        fields = patch.get("fields")
        if not word_id or word_id in seen_ids:
            raise ReleaseDataError(f"missing or duplicate word patch ID: {word_id!r}")
        seen_ids.add(word_id)
        if not isinstance(fields, dict) or not fields:
            raise ReleaseDataError(f"word patch {word_id} has no fields")
        unknown = set(fields) - ALLOWED_WORD_FIELDS
        if unknown:
            raise ReleaseDataError(f"word patch {word_id} has forbidden fields: {sorted(unknown)}")
        for field, change in fields.items():
            if not isinstance(change, dict) or set(change) != {"old", "new"}:
                raise ReleaseDataError(f"word patch {word_id}.{field} must contain only old/new")
            if not isinstance(change["old"], str) or not isinstance(change["new"], str):
                raise ReleaseDataError(f"word patch {word_id}.{field} values must be strings")
            if change["old"] == change["new"]:
                raise ReleaseDataError(f"word patch {word_id}.{field} is not a change")

    seen_synonym_ids = set()
    for pair in synonym_pairs:
        if not isinstance(pair, dict):
            raise ReleaseDataError(f"invalid synonym pair: {pair!r}")
        word_ids = pair.get("word_ids")
        labels = pair.get("labels")
        if not isinstance(word_ids, list) or len(word_ids) != 2 or len(set(word_ids)) != 2:
            raise ReleaseDataError(f"invalid synonym pair IDs: {word_ids!r}")
        if not isinstance(labels, list) or len(labels) != 2:
            raise ReleaseDataError(f"invalid synonym pair labels: {labels!r}")
        overlap = seen_synonym_ids.intersection(word_ids)
        if overlap:
            raise ReleaseDataError(f"word appears in multiple payload synonym pairs: {sorted(overlap)}")
        seen_synonym_ids.update(word_ids)

    try:
        expected_count = int(payload.get("source", {}).get("word_count", -1))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReleaseDataError("invalid source word count") from exc
    if expected_count <= 0 or len(seen_ids) > expected_count:
        raise ReleaseDataError("invalid source word count")
    return payload


def _table_counts(conn):
    counts = {}
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    for (table,) in rows:
        escaped = table.replace('"', '""')
        counts[table] = int(conn.execute(f'SELECT COUNT(*) FROM "{escaped}"').fetchone()[0])
    return counts


def _validate_target(conn, payload):
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version != SCHEMA_VERSION:
        raise ReleaseDataError(
            f"schema v{version}; expected v{SCHEMA_VERSION}. Run scripts.migrate_db first"
        )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(words)")}
    missing = ALLOWED_WORD_FIELDS - columns
    if missing:
        raise ReleaseDataError(f"words table misses required columns: {sorted(missing)}")
    word_count = int(conn.execute("SELECT COUNT(*) FROM words").fetchone()[0])
    expected = int(payload["source"]["word_count"])
    if word_count != expected:
        raise ReleaseDataError(f"word count {word_count}; expected reviewed baseline {expected}")
    validate_topic_reconciliation(conn)


def _plan_word_updates(conn, payload):
    updates = []
    conflicts = []
    already_current = 0
    fields_already_current = 0
    for patch in payload["word_patches"]:
        word_id = patch["id"]
        fields = patch["fields"]
        selected = sorted(fields)
        quoted = ", ".join(f'"{field}"' for field in selected)
        row = conn.execute(f"SELECT {quoted} FROM words WHERE id=?", (word_id,)).fetchone()
        if row is None:
            conflicts.append({"id": word_id, "label": patch.get("label", ""), "error": "missing ID"})
            continue
        changed = {}
        for field, actual in zip(selected, row):
            expected_old = fields[field]["old"]
            expected_new = fields[field]["new"]
            actual = str(actual or "")
            if actual == expected_new:
                fields_already_current += 1
            elif actual == expected_old:
                changed[field] = expected_new
            else:
                conflicts.append(
                    {
                        "id": word_id,
                        "label": patch.get("label", ""),
                        "field": field,
                        "expected_old": expected_old,
                        "expected_new": expected_new,
                        "actual": actual,
                    }
                )
        if changed:
            updates.append((word_id, patch.get("label", ""), changed))
        elif not any(conflict.get("id") == word_id for conflict in conflicts):
            already_current += 1
    if conflicts:
        preview = json.dumps(conflicts[:20], ensure_ascii=False, sort_keys=True)
        suffix = f" (+{len(conflicts) - 20} more)" if len(conflicts) > 20 else ""
        raise ReleaseDataError(f"word precondition conflicts: {preview}{suffix}")
    return updates, already_current, fields_already_current


def _apply_word_updates(conn, payload):
    updates, already_current, fields_already_current = _plan_word_updates(conn, payload)
    changed_fields = 0
    has_updated_at = "updated_at" in {row[1] for row in conn.execute("PRAGMA table_info(words)")}
    for word_id, _label, fields in updates:
        columns = sorted(fields)
        assignments = [f'"{field}"=?' for field in columns]
        values = [fields[field] for field in columns]
        if has_updated_at:
            assignments.append("updated_at=CURRENT_TIMESTAMP")
        cursor = conn.execute(
            f"UPDATE words SET {', '.join(assignments)} WHERE id=?",
            (*values, word_id),
        )
        if cursor.rowcount != 1:
            raise ReleaseDataError(f"word update lost stable ID {word_id}")
        changed_fields += len(fields)
    return {
        "payload_words": len(payload["word_patches"]),
        "words_changed": len(updates),
        "words_already_current": already_current,
        "fields_changed": changed_fields,
        "fields_already_current": fields_already_current,
    }


def _apply_synonym_pairs(conn, payload):
    created = 0
    already_current = 0
    details = []
    for pair in payload["synonym_pairs"]:
        word_ids = pair["word_ids"]
        found = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT w.id, s.group_id FROM words w "
                "LEFT JOIN word_synonyms s ON s.word_id=w.id "
                "WHERE w.id IN (?, ?)",
                word_ids,
            )
        }
        if set(found) != set(word_ids):
            raise ReleaseDataError(f"synonym pair has missing word: {pair}")
        groups = [found[word_id] for word_id in word_ids]
        if groups[0] is not None and groups[0] == groups[1]:
            already_current += 1
            details.append({"labels": pair["labels"], "status": "already_current", "group_id": groups[0]})
            continue
        if groups != [None, None]:
            raise ReleaseDataError(
                f"synonym precondition conflict for {pair['labels']}: existing groups {groups}"
            )
        cursor = conn.execute("INSERT INTO synonym_groups DEFAULT VALUES")
        group_id = int(cursor.lastrowid)
        conn.executemany(
            "INSERT INTO word_synonyms(word_id, group_id) VALUES (?, ?)",
            ((word_id, group_id) for word_id in word_ids),
        )
        created += 1
        details.append({"labels": pair["labels"], "status": "created", "group_id": group_id})
    return {"groups_created": created, "groups_already_current": already_current, "details": details}


def _assert_count_changes(before, after, report):
    allowed = {
        "topics": report["topics"]["topic_count_after"] - report["topics"]["topic_count_before"],
        "synonym_groups": report["synonyms"]["groups_created"],
        "word_synonyms": report["synonyms"]["groups_created"] * 2,
    }
    problems = []
    for table in sorted(set(before) | set(after)):
        delta = after.get(table, 0) - before.get(table, 0)
        expected = allowed.get(table, 0)
        if delta != expected:
            problems.append(f"{table}: delta {delta}, expected {expected}")
    if problems:
        raise ReleaseDataError("unexpected row-count changes: " + "; ".join(problems))


def _run_transaction(conn, payload):
    before = _table_counts(conn)
    topics = reconcile_topics(conn)
    words = _apply_word_updates(conn, payload)
    synonyms = _apply_synonym_pairs(conn, payload)
    after = _table_counts(conn)
    report = {"topics": topics, "words": words, "synonyms": synonyms}
    _assert_count_changes(before, after, report)
    report["table_counts_before"] = before
    report["table_counts_after"] = after
    return report


def _preview(db, payload):
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        _validate_target(conn, payload)
        conn.execute("BEGIN IMMEDIATE")
        report = _run_transaction(conn, payload)
        conn.rollback()
        return report
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _report_has_changes(report):
    return any(
        (
            report["words"]["words_changed"],
            report["synonyms"]["groups_created"],
            report["topics"]["topic_count_after"] - report["topics"]["topic_count_before"],
            sum(item["words"] for item in report["topics"]["moved"].values()),
            report["topics"]["vf_add_jobs_changed"],
            sum(report["topics"]["topic_filter_rows_changed"].values()),
            sum(
                before != report["topics"]["topics_after"].get(name)
                for name, before in report["topics"]["topics_before"].items()
                if name in report["topics"]["topics_after"]
            ),
        )
    )


def apply_release_data(db_path, *, payload_path=DEFAULT_PAYLOAD, apply=False, dump_dir=None):
    """Preview or apply the release payload and return a machine-readable report."""
    db = Path(db_path)
    if not db.is_file():
        raise ReleaseDataError(f"DB does not exist: {db}")
    payload = load_payload(payload_path)
    report = _preview(db, payload)
    report.update(
        {
            "release": payload["release"],
            "payload": str(Path(payload_path).resolve()),
            "payload_sha256": _file_sha256(payload_path),
            "applied": False,
            "noop": not _report_has_changes(report),
            "backup": None,
        }
    )
    if not apply:
        return report
    if report["noop"]:
        health = run_healthcheck(db)
        if not health["ok"]:
            raise ReleaseDataError(f"idempotent healthcheck failed: {health}")
        report.update({"applied": True, "healthcheck": health})
        return report

    backup_dir = Path(dump_dir) if dump_dir else db.resolve().parent / "backups"
    backup = backup_db_with_event(db, backup_dir, "release-data-20260801")
    if backup is None:
        raise ReleaseDataError("pre-apply backup was not created")

    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        _validate_target(conn, payload)
        conn.execute("BEGIN IMMEDIATE")
        report = _run_transaction(conn, payload)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    health = run_healthcheck(db)
    if not health["ok"]:
        raise ReleaseDataError(f"post-apply healthcheck failed; restore {backup}: {health}")
    report.update(
        {
            "release": payload["release"],
            "payload": str(Path(payload_path).resolve()),
            "payload_sha256": _file_sha256(payload_path),
            "applied": True,
            "noop": False,
            "backup": str(backup),
            "healthcheck": health,
        }
    )
    log_db_event(
        db,
        "release_data_backfill",
        ok=True,
        detail={
            "release": report["release"],
            "payload_sha256": report["payload_sha256"],
            "backup": report["backup"],
            "words": report["words"],
            "synonyms": {
                "groups_created": report["synonyms"]["groups_created"],
                "groups_already_current": report["synonyms"]["groups_already_current"],
            },
        },
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Path to a schema-v14 words.db")
    parser.add_argument("--payload", default=str(DEFAULT_PAYLOAD), help="Release payload JSON")
    parser.add_argument("--apply", action="store_true", help="Apply after backup (default: dry-run)")
    parser.add_argument("--dump-dir", help="Backup directory (default: <db dir>/backups)")
    parser.add_argument("--report", help="Optional JSON report path")
    args = parser.parse_args()
    try:
        report = apply_release_data(
            args.db,
            payload_path=args.payload,
            apply=args.apply,
            dump_dir=args.dump_dir,
        )
    except (ReleaseDataError, ReconciliationError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.report:
        output = Path(args.report)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
