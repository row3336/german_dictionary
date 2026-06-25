import os
import sqlite3
import tempfile
import unittest

from bot.storage import (
    _connect,
    backup_database,
    backup_db_before_migrate,
    init_db,
)


class DbBackupTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.src = os.path.join(self.dir, "words.db")
        init_db(self.src)
        with _connect(self.src) as conn:
            for i in range(15):
                conn.execute(
                    "INSERT INTO words (id, type, german, russian) VALUES (?, 'nomen', ?, ?)",
                    (f"id{i}", f"Wort{i}", f"слово{i}"),
                )

    def _count(self, path):
        conn = sqlite3.connect(path)
        try:
            return conn.execute("SELECT COUNT(*) FROM words").fetchone()[0]
        finally:
            conn.close()

    def test_backup_is_consistent_and_complete(self):
        dst = os.path.join(self.dir, "backup.db")
        result = backup_database(self.src, dst)
        self.assertEqual(str(result), dst)
        # all rows present in the snapshot
        self.assertEqual(self._count(dst), self._count(self.src))
        # snapshot passes integrity check
        conn = sqlite3.connect(dst)
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conn.close()

    def test_backup_missing_source_returns_none(self):
        missing = os.path.join(self.dir, "does-not-exist.db")
        self.assertIsNone(backup_database(missing, os.path.join(self.dir, "x.db")))

    def test_premigrate_snapshot_named_and_complete(self):
        dump_dir = os.path.join(self.dir, "backups")
        path = backup_db_before_migrate(self.src, dump_dir, schema_version=3)
        name = os.path.basename(str(path))
        self.assertTrue(name.startswith("words-premigrate-v3-"))
        self.assertTrue(name.endswith(".db"))
        self.assertEqual(self._count(str(path)), self._count(self.src))

    def test_premigrate_missing_source_returns_none(self):
        missing = os.path.join(self.dir, "nope.db")
        self.assertIsNone(backup_db_before_migrate(missing, self.dir))


if __name__ == "__main__":
    unittest.main()
