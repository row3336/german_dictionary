"""Database safety subsystem: WAL-safe backups, schema versioning, healthcheck,
the db_events timeline, and a safe migration orchestrator.

Primitives here depend only on stdlib (sqlite3/json/pathlib/datetime). The
explicit dispatcher lazily imports `storage.init_db` through a one-way edge
(storage never imports this module), so there is no cycle. Ordinary startup uses
a separate blocker and never confirms destructive steps implicitly.

See docs/multiuser.md §0.2 (protocol) and §0.4 (irreversible-step registry).
Additive normalization remains compatible through v15. Versioned destructive
steps run only through the explicit dispatcher after dry-run and snapshot.
"""

import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Current schema level. 1 = single-user baseline (state at v1.0-single-user).
# 2 = P2 (+ user_settings). 3 = P4 (+ user_word_media).
# 4 = P6 (+ error_reports, + feedback).
# 5 = #18 (+ form_stats.*_active weak-episode flags).
# 6 = spec #8 (+ topics registry table, seeded DISTINCT from words.topic).
# 7 = spec #1 (+ words.preposition_type, + words.case_type; both DEFAULT '').
# 8 = spec #3 (+ words.conjugation_praesens, + words.conjugation_praeteritum;
#     + conjugation_progress table; columns DEFAULT '').
# 9 = spec #7 (+ user_word_media.russian/ukrainian/english; per-user personal
#     translation overrides; all DEFAULT '').
# 10 = spec #2 (+ vf_add_jobs, + vf_add_items: persisted VF-add batch job so a
#      restart resumes the slow-drip parse instead of starting over).
# 11 = spec #2 (+ vf_add_items.word_id: links a recheck item to the existing word
#      it verifies, so the diff apply updates that word).
# 12 = word-level CEFR (+ words.cefr_level, DEFAULT ''): per-word A1..C2 parsed from
#      verbformen.de, feeding the level-aware topic filters.
# 13 = VF recheck (+ words.vf_checked_at, DEFAULT ''): timestamp of last successful
#      recheck so an 'unchecked' rescan skips already-verified words.
# 14 = topic book-level (+ topics.level, DEFAULT ''): per-topic A1..C2 level, independent
#      of words.cefr_level, groups the "book topics" filter. '' = unassigned (hidden from
#      that filter until set). Migration is column-only; A1 backfill is a deliberate step.
# 15 = VF separable status (+ words.is_separable, DEFAULT NULL): NULL means VF
#      has not produced a reliable answer; 0/1 are reviewed non-separable/separable.
# 16 = separate phrase direction modes. This is a destructive DATA migration and
#      is never run by ordinary bot startup.
# 17 = remove weak-form modes, schema, settings, progress, and history. This is a
#      destructive schema/data migration and is never run by ordinary startup.
# 18-21 = study core, reflexive identity, learning requests, linked Perfekt JSON.
# 22 = scalar words.hilfsverb (hat/ist/wechsel); converts and drops schema21 JSON.
ADDITIVE_SCHEMA_VERSION = 15
SCHEMA_VERSION = 22


class DestructiveMigrationRequired(RuntimeError):
    """Raised when ordinary startup reaches a migration that needs confirmation."""

# Tables whose row counts we snapshot before/after a migration (must not shrink).
_COUNT_TABLES = (
    "words",
    "progress",
    "quiz_events",
    "learning_items",
    "users",
    "user_settings",
    "app_settings",
    "word_synonyms",
    "user_word_media",
    "error_reports",
    "feedback",
    "conjugation_progress",
    "transaction_log",
    "study_actions",
    "study_deliveries",
    "conjugation_admissions",
    "conjugation_blocks",
    "conjugation_passes",
    "conjugation_answers",
    "study_word_generations",
    "study_reset_operations",
    "learning_requests",
)

# Keep the db_events timeline bounded (it's tiny, but never let it grow forever).
_DB_EVENTS_KEEP = 1000


def backup_database(src_path, dst_path):
    """Make a CONSISTENT snapshot of the SQLite DB via the online backup API.

    A plain file copy (shutil.copy2) can miss transactions still sitting in the
    `-wal` sidecar under WAL mode → a stale/incomplete backup. `conn.backup()`
    copies a transactionally-consistent image including WAL content, regardless
    of journal mode. Returns the destination Path, or None if src is missing.
    """
    src = Path(src_path)
    if not src.exists():
        return None
    dst = Path(dst_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(src)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return dst


def backup_db_before_migrate(db_path, dump_dir, schema_version=None):
    """Snapshot the DB right before a schema migration (rollback point).

    Distinct from the daily backup: a point-in-time copy taken the instant before
    a risky structural change, so a bad migration can be rolled back without
    losing fresh data. Matches the `words-*.db` glob → auto-cleaned at 14 days.
    Returns the backup Path, or None if there's no DB yet (fresh install).
    """
    src = Path(db_path)
    if not src.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    ver = f"-v{schema_version}" if schema_version is not None else ""
    dst = Path(dump_dir) / f"words-premigrate{ver}-{stamp}.db"
    return backup_database(src, dst)


def backup_db_before_destructive_migrate(db_path, dump_dir, schema_version):
    """Persistent snapshot for a destructive step, outside normal cleanup globs."""
    src = Path(db_path)
    if not src.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    dst = Path(dump_dir) / f"snapshot-premigrate-v{int(schema_version)}-{stamp}.db"
    return backup_database(src, dst)


def backup_db_with_event(db_path, dump_dir, event):
    """Point-in-time backup tagged with an event name (spec #2/#4).

    Taken before a non-migration data change (e.g. a VF-add batch apply). The
    `event` is baked into the filename now so a future off-site backup-bot can
    ship these tagged snapshots. Returns the backup Path, or None if no DB yet.
    """
    src = Path(db_path)
    if not src.exists():
        return None
    tag = re.sub(r"[^A-Za-z0-9_-]+", "_", str(event or "event")).strip("_") or "event"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    Path(dump_dir).mkdir(parents=True, exist_ok=True)
    dst = Path(dump_dir) / f"words-preapply-{tag}-{stamp}.db"
    return backup_database(src, dst)


def get_user_version(db_path):
    """Read PRAGMA user_version (schema level marker). 0 = fresh / pre-versioning."""
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def set_user_version(db_path, version):
    """Set PRAGMA user_version (int, not bindable → inlined safely)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"PRAGMA user_version = {int(version)}")
        conn.commit()
    finally:
        conn.close()


def _table_counts(conn):
    """Row counts for key tables (skips tables that don't exist yet)."""
    out = {}
    for table in _COUNT_TABLES:
        try:
            out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            pass
    return out


def table_counts(db_path):
    """Row counts for key tables, opening/closing its own connection."""
    conn = sqlite3.connect(db_path)
    try:
        return _table_counts(conn)
    finally:
        conn.close()


def _connection_fingerprint(conn):
    """Hash the logical state visible to an already-open SQLite connection."""
    digest = hashlib.sha256()
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    digest.update(f"user_version={version}\n".encode())
    for statement in conn.iterdump():
        digest.update(statement.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def database_fingerprint(db_path):
    """Hash a transactionally consistent logical dump, including user_version."""
    conn = sqlite3.connect(db_path)
    try:
        return _connection_fingerprint(conn)
    finally:
        conn.close()


def run_healthcheck(db_path):
    """Structural integrity check. Returns a dict; ok=True means no problems.

    - integrity_check: detects corruption / structural damage.
    - foreign_key_check: detects orphaned FK rows.
    - counts: current row counts (for before/after comparison).
    """
    conn = sqlite3.connect(db_path)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        counts = _table_counts(conn)
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        required = {"study_actions", "study_deliveries", "conjugation_admissions",
                    "conjugation_blocks", "conjugation_passes", "conjugation_answers"}
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(required - tables) if version >= 18 else []
        if version >= 19:
            missing.extend(sorted({"study_word_generations", "study_reset_operations"} - tables))
            for table, column in (("words", "reflexive_case"), ("conjugation_passes", "prompt_snapshot_json"), ("quiz_events", "study_generation"), ("conjugation_admissions", "study_generation")):
                if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
                    missing.append(table + "." + column)
        if version >= 20:
            missing.extend(sorted({'learning_requests'} - tables))
            for column in ('user_id', 'word_id', 'status', 'request_id', 'queued_at',
                           'consumed_at', 'word_generation', 'learning_lifecycle'):
                if column not in {r[1] for r in conn.execute('PRAGMA table_info(learning_requests)')}:
                    missing.append('learning_requests.' + column)
        word_columns = {r[1] for r in conn.execute("PRAGMA table_info(words)")}
        if version == 21 and "perfekt_variants_json" not in word_columns:
            missing.append("words.perfekt_variants_json")
        if version >= 22:
            if "hilfsverb" not in word_columns:
                missing.append("words.hilfsverb")
            if "perfekt_variants_json" in word_columns:
                missing.append("retired words.perfekt_variants_json still present")
        if version >= 18:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(learning_items)")}
            missing.extend("learning_items." + c for c in
                           ("recovery_remaining", "first_due_pending") if c not in columns)
    finally:
        conn.close()
    return {
        "ok": integrity == "ok" and len(fk_rows) == 0 and not missing,
        "missing_schema": missing,
        "integrity": integrity,
        "fk_violations": len(fk_rows),
        "counts": counts,
    }


def log_db_event(db_path, kind, ok=True, detail=None):
    """Append one row to the db_events timeline (creates the table on demand).

    The timeline answers "when did something change / break, so which backup
    date to restore". Tiny + pruned to the newest _DB_EVENTS_KEEP rows.
    kind: 'migration' | 'healthcheck_fail' | 'backup' | ...
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS db_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                kind TEXT NOT NULL,
                ok INTEGER NOT NULL DEFAULT 1,
                detail TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            "INSERT INTO db_events (kind, ok, detail) VALUES (?, ?, ?)",
            (str(kind), 1 if ok else 0, json.dumps(detail or {}, ensure_ascii=False)),
        )
        conn.execute(
            """
            DELETE FROM db_events WHERE id <= (
                SELECT MAX(id) FROM db_events
            ) - ?
            """,
            (_DB_EVENTS_KEEP,),
        )
        conn.commit()
    finally:
        conn.close()


_DIRECTION_BASE_MODES = ("ru_de", "uk_de", "en_de", "de_ru", "de_uk", "de_en")


def _payload_references_deleted_phrase_state(payload, affected_words, deleted_event_ids, old_progress):
    """Match decoded rollback structures, never raw JSON substrings."""
    affected_words = set(affected_words)
    deleted_event_ids = {int(value) for value in deleted_event_ids}
    old_progress = {(int(uid), str(wid), str(mode)) for uid, wid, mode in old_progress}

    def _walk(value, inherited_word_id=None, inherited_user_id=None):
        if isinstance(value, list):
            return any(_walk(item, inherited_word_id, inherited_user_id) for item in value)
        if not isinstance(value, dict):
            return False

        word_id = str(value.get("word_id") or inherited_word_id or "")
        try:
            user_id = int(value.get("user_id") or inherited_user_id or 0)
        except (TypeError, ValueError):
            user_id = 0

        for key in ("event_id", "quiz_event_id"):
            try:
                if int(value.get(key) or 0) in deleted_event_ids:
                    return True
            except (TypeError, ValueError):
                pass

        if word_id in affected_words:
            mode = str(value.get("mode") or "")
            if (user_id, word_id, mode) in old_progress:
                return True
            modes = {str(item) for item in (value.get("modes") or [])}
            if any((user_id, word_id, item) in old_progress for item in modes):
                return True
            progress = value.get("progress")
            if isinstance(progress, dict) and any(
                (user_id, word_id, str(item)) in old_progress for item in progress
            ):
                return True
            # All events of an affected phrase are deleted, including learning.
            if isinstance(value.get("event_counts"), dict) and value["event_counts"]:
                return True

        return any(_walk(item, word_id, user_id) for item in value.values())

    return _walk(payload)


def _migrate_phrase_modes_v16(conn, today=None):
    """Reset old phrase direction history and seed exact phrase progress."""
    current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current_version != ADDITIVE_SCHEMA_VERSION:
        raise RuntimeError(
            f"phrase_modes_v16 requires exact schema v{ADDITIVE_SCHEMA_VERSION}, "
            f"got v{current_version}"
        )
    migration_day = today or date.today()
    due = (migration_day + timedelta(days=1)).isoformat()
    placeholders = ",".join("?" for _ in _DIRECTION_BASE_MODES)
    affected_rows = conn.execute(
        f"""
        SELECT DISTINCT w.id
        FROM words w
        WHERE LOWER(TRIM(w.type)) = 'phrase'
          AND (
            EXISTS (
                SELECT 1 FROM progress p
                WHERE p.word_id = w.id AND p.mode IN ({placeholders})
            )
            OR EXISTS (
                SELECT 1 FROM quiz_events e
                WHERE e.word_id = w.id AND e.mode IN ({placeholders})
            )
          )
        """,
        (*_DIRECTION_BASE_MODES, *_DIRECTION_BASE_MODES),
    ).fetchall()
    affected = {str(row[0]) for row in affected_rows}
    if not affected:
        return {
            "affected_phrases": 0,
            "replaced_progress": 0,
            "deleted_events": 0,
            "deleted_rollback_rows": 0,
        }

    word_marks = ",".join("?" for _ in affected)
    old_progress_rows = conn.execute(
        f"""
        SELECT user_id, word_id, mode
        FROM progress
        WHERE word_id IN ({word_marks}) AND mode IN ({placeholders})
        ORDER BY user_id, word_id, mode
        """,
        (*sorted(affected), *_DIRECTION_BASE_MODES),
    ).fetchall()
    old_progress = [(int(row[0]), str(row[1]), str(row[2])) for row in old_progress_rows]
    event_rows = conn.execute(
        f"SELECT id FROM quiz_events WHERE word_id IN ({word_marks}) ORDER BY id",
        tuple(sorted(affected)),
    ).fetchall()
    deleted_event_ids = [int(row[0]) for row in event_rows]

    rollback_ids = []
    for row in conn.execute("SELECT id, payload FROM transaction_log ORDER BY id").fetchall():
        try:
            payload = json.loads(row[1])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if _payload_references_deleted_phrase_state(
            payload, affected, deleted_event_ids, old_progress
        ):
            rollback_ids.append(int(row[0]))

    if rollback_ids:
        marks = ",".join("?" for _ in rollback_ids)
        conn.execute(f"DELETE FROM transaction_log WHERE id IN ({marks})", rollback_ids)
    conn.execute(
        f"DELETE FROM quiz_events WHERE word_id IN ({word_marks})",
        tuple(sorted(affected)),
    )
    conn.execute(
        f"DELETE FROM progress WHERE word_id IN ({word_marks}) AND mode IN ({placeholders})",
        (*sorted(affected), *_DIRECTION_BASE_MODES),
    )
    conn.executemany(
        """
        INSERT INTO progress (user_id, word_id, mode, interval, repetitions, ease, due)
        VALUES (?, ?, ?, 1, 1, 2.5, ?)
        ON CONFLICT(user_id, word_id, mode) DO NOTHING
        """,
        [(uid, word_id, f"phrase_{mode}", due) for uid, word_id, mode in old_progress],
    )
    return {
        "affected_phrases": len(affected),
        "replaced_progress": len(old_progress),
        "deleted_events": len(deleted_event_ids),
        "deleted_rollback_rows": len(rollback_ids),
    }


_REMOVED_WEAK_SETTING_KEYS = ("weak_drill_gap", "weak_drill_repeats")


def _table_exists(conn, table_name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    ).fetchone()
    return bool(row)


def _weak_removal_counts(conn):
    """Count every removed weak entity and its preserved non-weak counterpart."""
    form_stats = (
        int(conn.execute("SELECT COUNT(*) FROM form_stats").fetchone()[0])
        if _table_exists(conn, "form_stats")
        else 0
    )
    counts = {"form_stats": form_stats}
    for table, column, prefix in (
        ("progress", "mode", "weak_*"),
        ("quiz_events", "mode", "weak_*"),
        ("transaction_log", "action", "quiz:weak_*"),
    ):
        total = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        weak = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} GLOB ?",
                (prefix,),
            ).fetchone()[0]
        )
        counts[f"weak_{table}"] = weak
        counts[f"non_weak_{table}"] = total - weak
    placeholders = ",".join("?" for _ in _REMOVED_WEAK_SETTING_KEYS)
    for table in ("user_settings", "app_settings"):
        total = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        weak = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE key IN ({placeholders})",
                _REMOVED_WEAK_SETTING_KEYS,
            ).fetchone()[0]
        )
        counts[f"weak_{table}"] = weak
        counts[f"non_weak_{table}"] = total - weak
    return counts


def _migrate_remove_weak_v17(conn):
    """Remove weak schema and data while preserving every ordinary row verbatim."""
    current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current_version != 16:
        raise RuntimeError(f"remove_weak_v17 requires exact schema v16, got v{current_version}")

    before = _weak_removal_counts(conn)
    quiz_indexes = [
        row[0]
        for row in conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name='quiz_events' AND sql IS NOT NULL "
            "ORDER BY name"
        ).fetchall()
    ]

    conn.execute("ALTER TABLE quiz_events RENAME TO quiz_events_v17_old")
    conn.execute(
        """
        CREATE TABLE quiz_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            word_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            is_correct INTEGER NOT NULL,
            prev_is_correct INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        INSERT INTO quiz_events
            (id, user_id, word_id, mode, is_correct, prev_is_correct, created_at)
        SELECT id, user_id, word_id, mode, is_correct, prev_is_correct, created_at
        FROM quiz_events_v17_old
        WHERE mode NOT GLOB 'weak_*'
        ORDER BY id
        """
    )
    conn.execute("DROP TABLE quiz_events_v17_old")
    for sql in quiz_indexes:
        conn.execute(sql)

    conn.execute("DELETE FROM progress WHERE mode GLOB 'weak_*'")
    conn.execute("DELETE FROM transaction_log WHERE action GLOB 'quiz:weak_*'")
    placeholders = ",".join("?" for _ in _REMOVED_WEAK_SETTING_KEYS)
    conn.execute(
        f"DELETE FROM user_settings WHERE key IN ({placeholders})",
        _REMOVED_WEAK_SETTING_KEYS,
    )
    conn.execute(
        f"DELETE FROM app_settings WHERE key IN ({placeholders})",
        _REMOVED_WEAK_SETTING_KEYS,
    )
    conn.execute("DROP TABLE IF EXISTS form_stats")

    after = _weak_removal_counts(conn)
    for name, count in after.items():
        if name.startswith("weak_") or name == "form_stats":
            if count != 0:
                raise RuntimeError(f"weak removal left {name} rows: {count}")
        elif count != before[name]:
            raise RuntimeError(
                f"weak removal changed preserved {name}: {before[name]} -> {count}"
            )
    return {
        "counts_before": before,
        "counts_after": after,
        "deleted_progress": before["weak_progress"],
        "deleted_events": before["weak_quiz_events"],
        "deleted_transaction_rows": before["weak_transaction_log"],
        "deleted_user_settings": before["weak_user_settings"],
        "deleted_app_settings": before["weak_app_settings"],
        "deleted_form_stats": before["form_stats"],
    }


def _validate_destructive_counts(before, after, steps):
    phrase_step = steps.get("phrase_modes_v16") or {}
    weak_step = steps.get("remove_weak_v17") or {}
    allowed = {
        "progress": int(before.get("progress", 0)) - int(weak_step.get("deleted_progress", 0)),
        "quiz_events": (
            int(before.get("quiz_events", 0))
            - int(phrase_step.get("deleted_events", 0))
            - int(weak_step.get("deleted_events", 0))
        ),
        "transaction_log": (
            int(before.get("transaction_log", 0))
            - int(phrase_step.get("deleted_rollback_rows", 0))
            - int(weak_step.get("deleted_transaction_rows", 0))
        ),
        "user_settings": (
            int(before.get("user_settings", 0))
            - int(weak_step.get("deleted_user_settings", 0))
        ),
        "app_settings": (
            int(before.get("app_settings", 0))
            - int(weak_step.get("deleted_app_settings", 0))
        ),
    }
    for table, old_count in before.items():
        if table in {"study_actions", "study_deliveries", "conjugation_admissions",
                     "conjugation_blocks", "conjugation_passes", "conjugation_answers"}:
            continue  # v18 creates these; only admissions may receive intentional seed rows.
        expected = allowed.get(table, old_count)
        if after.get(table) != expected:
            raise RuntimeError(
                f"unexpected {table} count after migration: "
                f"{old_count} -> {after.get(table)}, expected {expected}"
            )


def _finalize_additive_normalization(db_path, source_version):
    """Record the add-only schema normalization as an exact transition to v15."""
    if source_version >= ADDITIVE_SCHEMA_VERSION:
        return None
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if current != source_version:
            raise RuntimeError(
                f"source DB version changed during additive normalization: "
                f"expected v{source_version}, got v{current}"
            )
        conn.execute(f"PRAGMA user_version = {ADDITIVE_SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"from": source_version, "to": ADDITIVE_SCHEMA_VERSION}


def _apply_migration_dispatcher(db_path, today=None, expected_fingerprint=None):
    """Apply pending steps to a disposable copy or an already-snapshotted DB."""
    from ..storage import init_db

    pre = get_user_version(db_path)
    if pre > SCHEMA_VERSION:
        raise RuntimeError(f"database schema v{pre} is newer than supported v{SCHEMA_VERSION}")
    if pre == SCHEMA_VERSION:
        health = run_healthcheck(db_path)
        return {
            "fresh": False, "migrated": False, "from_version": pre,
            "to_version": pre, "backup": None, "counts_before": health["counts"],
            "counts_after": health["counts"], "healthcheck": health, "steps": {},
        }

    before = table_counts(db_path)
    steps = {}
    if pre < ADDITIVE_SCHEMA_VERSION:
        init_db(db_path, _additive_only=True)  # add-only schema through v15; version marker stays unchanged
        steps["additive_to_v15"] = _finalize_additive_normalization(db_path, pre)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        locked_fingerprint = _connection_fingerprint(conn)
        if expected_fingerprint and locked_fingerprint != expected_fingerprint:
            raise RuntimeError(
                "source DB changed after destructive migration dry-run; run a new dry-run"
            )
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if current not in {ADDITIVE_SCHEMA_VERSION, 16, 17, 18, 19, 20, 21}:
            raise RuntimeError(
                f"migration dispatcher requires v{ADDITIVE_SCHEMA_VERSION}, v16, v17, v18, v19, v20, or v21, "
                f"got v{current}"
            )
        if current == ADDITIVE_SCHEMA_VERSION:
            steps["phrase_modes_v16"] = _migrate_phrase_modes_v16(conn, today=today)
            conn.execute("PRAGMA user_version = 16")
            current = 16
        if current == 16:
            steps["remove_weak_v17"] = _migrate_remove_weak_v17(conn)
            conn.execute("PRAGMA user_version = 17")
            current = 17
        if current == 17:
            from ..study.schema_v18 import migrate
            steps["study_v18"] = migrate(conn)
            conn.execute("PRAGMA user_version = 18")
            current = 18
        if current == 18:
            from ..study.schema_v19 import migrate as migrate_v19
            steps["reflexive_v19"] = migrate_v19(conn)
            conn.execute("PRAGMA user_version = 19")
            current = 19
        if current == 19:
            from ..study.schema_v20 import migrate as migrate_v20
            steps["learning_requests_v20"] = migrate_v20(conn)
            conn.execute("PRAGMA user_version = 20")
            current = 20
        if current == 20:
            from ..study.schema_v21 import migrate as migrate_v21
            steps["perfekt_v21"] = migrate_v21(conn)
            conn.execute("PRAGMA user_version = 21")
            current = 21
        if current == 21:
            from ..study.schema_v22 import migrate as migrate_v22
            steps["hilfsverb_v22"] = migrate_v22(conn)
            conn.execute("PRAGMA user_version = 22")
        counts_in_transaction = _table_counts(conn)
        _validate_destructive_counts(before, counts_in_transaction, steps)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or fk_rows:
            raise RuntimeError(
                f"healthcheck failed inside migration transaction: integrity={integrity}, "
                f"fk_violations={len(fk_rows)}"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    health = run_healthcheck(db_path)
    if not health["ok"]:
        raise RuntimeError(f"healthcheck failed after migration: {health}")
    return {
        "fresh": False,
        "migrated": True,
        "from_version": pre,
        "to_version": SCHEMA_VERSION,
        "backup": None,
        "counts_before": before,
        "counts_after": health["counts"],
        "healthcheck": health,
        "steps": steps,
        "source_fingerprint": locked_fingerprint,
    }


def prepare_storage_safely(db_path, dump_dir):
    """Prepare fresh/current storage; block every pending destructive path."""
    from ..storage import init_db

    db = Path(db_path)
    if not db.exists():
        init_db(db_path)
        set_user_version(db_path, SCHEMA_VERSION)
        health = run_healthcheck(db_path)
        return {
            "fresh": True, "migrated": True, "from_version": 0,
            "to_version": SCHEMA_VERSION, "backup": None, "counts_before": None,
            "counts_after": health["counts"], "healthcheck": health, "steps": {},
        }
    pre = get_user_version(db_path)
    if pre < SCHEMA_VERSION:
        raise DestructiveMigrationRequired(
            f"DB schema v{pre} requires confirmed destructive migration to v{SCHEMA_VERSION}; "
            "run scripts.migrate_db --dry-run, then scripts.migrate_db"
        )
    if pre > SCHEMA_VERSION:
        raise RuntimeError(f"database schema v{pre} is newer than supported v{SCHEMA_VERSION}")
    health = run_healthcheck(db_path)
    return {
        "fresh": False, "migrated": False, "from_version": pre,
        "to_version": pre, "backup": None, "counts_before": health["counts"],
        "counts_after": health["counts"], "healthcheck": health, "steps": {},
    }


def apply_destructive_migrations_safely(db_path, dump_dir, today=None):
    """Dry-run, persist snapshot, then dispatch confirmed destructive steps."""
    db = Path(db_path)
    if not db.exists():
        return prepare_storage_safely(db_path, dump_dir)
    pre = get_user_version(db_path)
    if pre == SCHEMA_VERSION:
        return prepare_storage_safely(db_path, dump_dir)
    dry_report = dry_run_migration(db_path, today=today)
    if not dry_report.get("ok"):
        raise RuntimeError(f"destructive migration dry-run failed: {dry_report}")
    backup_path = backup_db_before_destructive_migrate(db_path, dump_dir, pre)
    if backup_path is None:
        raise RuntimeError("pre-migration snapshot was not created")
    report = _apply_migration_dispatcher(
        db_path,
        today=today,
        expected_fingerprint=dry_report.get("source_fingerprint"),
    )
    report["backup"] = str(backup_path)
    report["dry_run"] = dry_report
    log_db_event(
        db_path,
        "migration",
        ok=True,
        detail={
            "from": pre,
            "to": SCHEMA_VERSION,
            "backup": str(backup_path),
            "steps": report["steps"],
            "counts_before": report["counts_before"],
            "counts_after": report["counts_after"],
        },
    )
    return report


def apply_migrations_safely(db_path, dump_dir, today=None):
    """Explicit migration API retained for callers outside normal startup."""
    return apply_destructive_migrations_safely(db_path, dump_dir, today=today)


def dry_run_migration(db_path, today=None):
    """Run the migration on a throwaway COPY of the live DB — never touches live.

    The copy is a consistent snapshot (backup API, incl. the user_version header),
    so the migration runs against the REAL data and catches data-specific issues
    that synthetic tests miss. Returns a report (same shape as
    apply_migrations_safely) + `source_counts` + `ok`. Temp files are cleaned up.
    Apply for real only if `ok` is True (docs/multiuser.md §0.2 item 4).
    """
    src = Path(db_path)
    if not src.exists():
        return {"ok": False, "dry_run": True, "error": "source DB does not exist"}
    tmpdir = tempfile.mkdtemp(prefix="vocab-dryrun-")
    try:
        copy = Path(tmpdir) / "words.db"
        backup_database(src, copy)  # consistent copy of live data (preserves user_version)
        from ..study.schema_v22 import UnsupportedLegacyPerfekt
        try:
            report = _apply_migration_dispatcher(str(copy), today=today)
        except UnsupportedLegacyPerfekt as exc:
            # Documented refusal: never default unsupported annotations.
            return {"ok": False, "dry_run": True, "error": str(exc), "unsupported": exc.cases}
        report["dry_run"] = True
        report["source_counts"] = table_counts(db_path)
        report["ok"] = bool(report["healthcheck"]["ok"])
        return report
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
