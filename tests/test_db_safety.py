import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bot.common.db_safety import (
    SCHEMA_VERSION,
    apply_migrations_safely,
    backup_database,
    dry_run_migration,
    get_user_version,
    log_db_event,
    run_healthcheck,
    set_user_version,
)
from bot.storage import _connect, init_db


def _add_words(db_path, n):
    with _connect(db_path) as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO words (id, type, german, russian) VALUES (?, 'nomen', ?, ?)",
                (f"id{i}", f"Wort{i}", f"слово{i}"),
            )


def _table_exists(db_path, name):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _event_kinds(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return [r[0] for r in conn.execute("SELECT kind FROM db_events ORDER BY id").fetchall()]
    finally:
        conn.close()


class UserVersionTest(unittest.TestCase):
    def test_roundtrip(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "words.db")
        init_db(db)
        self.assertEqual(get_user_version(db), 0)  # init_db must not touch the marker
        set_user_version(db, 7)
        self.assertEqual(get_user_version(db), 7)


class HealthcheckTest(unittest.TestCase):
    def test_ok_on_good_db(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "words.db")
        init_db(db)
        _add_words(db, 5)
        hc = run_healthcheck(db)
        self.assertTrue(hc["ok"])
        self.assertEqual(hc["integrity"], "ok")
        self.assertEqual(hc["fk_violations"], 0)
        self.assertEqual(hc["counts"]["words"], 5)


class DbEventsTest(unittest.TestCase):
    def test_log_creates_table_and_row(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "words.db")
        init_db(db)
        log_db_event(db, "healthcheck_fail", ok=False, detail={"x": 1})
        self.assertEqual(_event_kinds(db), ["healthcheck_fail"])


class ApplyMigrationsSafelyTest(unittest.TestCase):
    def test_fresh_db_no_backup_version_set(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "words.db")  # does not exist yet
        report = apply_migrations_safely(db, os.path.join(d, "backups"))
        self.assertTrue(report["fresh"])
        self.assertTrue(report["migrated"])
        self.assertIsNone(report["backup"])  # nothing to back up on a fresh install
        self.assertEqual(get_user_version(db), SCHEMA_VERSION)
        self.assertTrue(report["healthcheck"]["ok"])

    def test_existing_v0_backs_up_then_migrates(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "words.db")
        init_db(db)              # creates schema, leaves user_version = 0
        _add_words(db, 12)
        self.assertEqual(get_user_version(db), 0)

        report = apply_migrations_safely(db, os.path.join(d, "backups"))
        self.assertTrue(report["migrated"])
        self.assertFalse(report["fresh"])
        # pre-migration snapshot was taken and exists
        self.assertIsNotNone(report["backup"])
        self.assertTrue(Path(report["backup"]).exists())
        # version bumped, healthcheck passed, data preserved
        self.assertEqual(get_user_version(db), SCHEMA_VERSION)
        self.assertTrue(report["healthcheck"]["ok"])
        self.assertEqual(report["counts_before"]["words"], report["counts_after"]["words"])
        self.assertEqual(report["counts_after"]["words"], 12)
        # timeline recorded the migration
        self.assertIn("migration", _event_kinds(db))

    def test_rerun_is_idempotent_no_new_backup(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "words.db")
        backups = os.path.join(d, "backups")
        apply_migrations_safely(db, backups)          # fresh → v(SCHEMA_VERSION)
        report2 = apply_migrations_safely(db, backups)  # already current
        self.assertFalse(report2["migrated"])
        self.assertIsNone(report2["backup"])
        self.assertTrue(report2["healthcheck"]["ok"])


class BackupPreservesVersionTest(unittest.TestCase):
    def test_user_version_survives_backup(self):
        # dry-run correctness depends on the copy keeping the source's version marker
        d = tempfile.mkdtemp()
        src = os.path.join(d, "words.db")
        init_db(src)
        set_user_version(src, 3)
        dst = os.path.join(d, "copy.db")
        backup_database(src, dst)
        self.assertEqual(get_user_version(dst), 3)


class DryRunTest(unittest.TestCase):
    def test_dry_run_reports_ok_without_touching_live(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "words.db")
        init_db(db)            # live at v0
        _add_words(db, 9)

        report = dry_run_migration(db)
        self.assertTrue(report["dry_run"])
        self.assertTrue(report["ok"])
        self.assertTrue(report["migrated"])           # the copy would migrate v0 -> vN
        self.assertEqual(report["source_counts"]["words"], 9)
        self.assertEqual(report["counts_after"]["words"], 9)

        # live DB is untouched: version unchanged, data intact, no backups written next to it
        self.assertEqual(get_user_version(db), 0)
        self.assertEqual(run_healthcheck(db)["counts"]["words"], 9)
        self.assertFalse(os.path.exists(os.path.join(d, "backups")))

    def test_dry_run_rehearses_v3_to_v4_without_touching_live(self):
        # Simulate a real v3 DB (P6 tables absent, version marker 3) and confirm the
        # dry-run rehearses creating error_reports/feedback on the COPY only.
        d = tempfile.mkdtemp()
        db = os.path.join(d, "words.db")
        init_db(db)
        _add_words(db, 5)
        conn = sqlite3.connect(db)
        conn.execute("DROP TABLE error_reports")
        conn.execute("DROP TABLE feedback")
        conn.commit()
        conn.close()
        set_user_version(db, 3)

        report = dry_run_migration(db)
        self.assertTrue(report["ok"])
        self.assertTrue(report["migrated"])
        self.assertEqual(report["from_version"], 3)
        self.assertEqual(report["to_version"], SCHEMA_VERSION)
        # the copy got the new tables; words preserved
        self.assertIn("error_reports", report["counts_after"])
        self.assertIn("feedback", report["counts_after"])
        self.assertEqual(report["counts_after"]["words"], 5)

        # live DB is untouched: still v3, P6 tables still absent, data intact
        self.assertEqual(get_user_version(db), 3)
        self.assertFalse(_table_exists(db, "error_reports"))
        self.assertFalse(_table_exists(db, "feedback"))
        self.assertEqual(run_healthcheck(db)["counts"]["words"], 5)

    def test_dry_run_missing_source(self):
        d = tempfile.mkdtemp()
        report = dry_run_migration(os.path.join(d, "nope.db"))
        self.assertFalse(report["ok"])
        self.assertIn("error", report)


if __name__ == "__main__":
    unittest.main()
