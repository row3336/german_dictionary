"""P8b — security invariants (lockdown tests).

The audit (SQL/command/secrets/access) is mostly verification; this locks the one
guard worth pinning in code: update_word_fields only ever writes allowlisted
columns, so a crafted field key can't inject into the SQL structure.
"""

import os
import sqlite3
import tempfile
import unittest

from bot.storage import get_word_by_id, init_db, update_word_fields


def _mk_db():
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.close()
    init_db(tmp.name)
    conn = sqlite3.connect(tmp.name)
    conn.execute("INSERT INTO words (id, type, german, russian) VALUES ('w1','nomen','Haus','дом')")
    conn.commit()
    conn.close()
    return tmp.name


class UpdateWordFieldsAllowlistTest(unittest.TestCase):
    def setUp(self):
        self.db = _mk_db()
        self.addCleanup(lambda: os.unlink(self.db))

    def test_only_allowlisted_fields_written(self):
        update_word_fields(self.db, "w1", {"german": "Haus2", "russian": "дом2"})
        w = get_word_by_id(self.db, "w1")
        self.assertEqual(w["german"], "Haus2")
        self.assertEqual(w["russian"], "дом2")

    def test_unknown_and_injection_keys_ignored(self):
        # A non-allowlisted key (incl. a SQL-injection attempt and 'id') must be
        # silently dropped — never reaching the SET clause — and not corrupt data.
        update_word_fields(self.db, "w1", {
            "german": "Haus3",
            "id": "hacked",                         # not allowlisted → ignored
            "german = 'x'; DROP TABLE words; --": "y",  # injection attempt → ignored
        })
        w = get_word_by_id(self.db, "w1")
        self.assertEqual(w["id"], "w1")             # id unchanged
        self.assertEqual(w["german"], "Haus3")      # allowlisted field applied
        # words table still intact (DROP did not run)
        conn = sqlite3.connect(self.db)
        try:
            self.assertIsNotNone(
                conn.execute("SELECT 1 FROM words WHERE id='w1'").fetchone()
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
