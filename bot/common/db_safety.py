"""Database safety subsystem: WAL-safe backups, schema versioning, healthcheck,
the db_events timeline, and a safe migration orchestrator.

Primitives here depend only on stdlib (sqlite3/json/pathlib/datetime). The single
orchestrator `apply_migrations_safely` lazily imports `storage.init_db` — a
one-way edge (storage never imports this module), so there is no cycle.

See docs/multiuser.md §0.2 (protocol) and §0.4 (irreversible-step registry).
Migration rule: ADD-only (CREATE TABLE / ADD COLUMN) so an older/rolled-back
build still opens a newer DB. Bump SCHEMA_VERSION whenever init_db's schema grows.
"""

import json
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
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
SCHEMA_VERSION = 13

# Tables whose row counts we snapshot before/after a migration (must not shrink).
_COUNT_TABLES = (
    "words",
    "progress",
    "quiz_events",
    "learning_items",
    "form_stats",
    "users",
    "word_synonyms",
    "user_word_media",
    "error_reports",
    "feedback",
    "conjugation_progress",
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
    finally:
        conn.close()
    return {
        "ok": integrity == "ok" and len(fk_rows) == 0,
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


def apply_migrations_safely(db_path, dump_dir):
    """Safe wrapper around init_db: backup → migrate → healthcheck → log.

    Backup + version bump + event are gated on an actual version change, so the
    defensive `init_db` calls scattered through storage stay cheap (schema-only).
    If the pre-migration backup can't be taken, it raises BEFORE touching the
    schema (never migrate without a rollback point). Returns a report dict.
    """
    from ..storage import init_db  # lazy: one-way edge, avoids any import ordering issue

    db = Path(db_path)
    fresh = not db.exists()
    pre = 0 if fresh else get_user_version(db_path)
    needs = pre < SCHEMA_VERSION

    counts_before = None
    backup_path = None
    if needs and not fresh:
        counts_before = table_counts(db_path)
        backup_path = backup_db_before_migrate(db_path, dump_dir, pre)  # raises → abort before schema

    init_db(db_path)  # apply schema/indexes/migrations (idempotent)

    if needs:
        set_user_version(db_path, SCHEMA_VERSION)

    health = run_healthcheck(db_path)
    report = {
        "fresh": fresh,
        "migrated": needs,
        "from_version": pre,
        "to_version": SCHEMA_VERSION,
        "backup": str(backup_path) if backup_path else None,
        "counts_before": counts_before,
        "counts_after": health["counts"],
        "healthcheck": health,
    }

    if needs:
        log_db_event(
            db_path,
            "migration",
            ok=health["ok"],
            detail={
                "from": pre,
                "to": SCHEMA_VERSION,
                "backup": report["backup"],
                "counts_before": counts_before,
                "counts_after": health["counts"],
                "healthcheck": {k: health[k] for k in ("ok", "integrity", "fk_violations")},
            },
        )
    elif not health["ok"]:
        log_db_event(db_path, "healthcheck_fail", ok=False, detail=health)

    return report


def dry_run_migration(db_path):
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
        report = apply_migrations_safely(str(copy), str(Path(tmpdir) / "backups"))
        report["dry_run"] = True
        report["source_counts"] = table_counts(db_path)
        report["ok"] = bool(report["healthcheck"]["ok"])
        return report
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
