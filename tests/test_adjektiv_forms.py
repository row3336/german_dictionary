import os
import sqlite3
import tempfile
import unittest

from bot.storage import add_word, init_db, select_due, update_word_fields


def _mk_db():
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.close()
    return tmp.name


def _add_adjektiv(db_path, german="schnell", russian="быстрый", komp="", supr=""):
    return add_word(
        db_path,
        {
            "id": "unique",
            "type": "adjektiv",
            "article": "",
            "german": german,
            "plural": "",
            "infinitive": "",
            "prateritum": "",
            "partizip2": "",
            "komparativ": komp,
            "superlativ": supr,
            "russian": russian,
            "ukrainian": "",
            "english": "",
            "topic": "1",
        },
    )


class AdjektivFormsTests(unittest.TestCase):
    def test_schema_contains_adjektiv_form_columns(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        with sqlite3.connect(db_path) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(words)").fetchall()}
        self.assertIn("komparativ", cols)
        self.assertIn("superlativ", cols)

    def test_add_word_persists_adjektiv_forms(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        entry = _add_adjektiv(db_path, german="schnell", russian="быстрый", komp="schneller", supr="am schnellsten")
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT komparativ, superlativ FROM words WHERE id = ?",
                (entry["id"],),
            ).fetchone()
        self.assertEqual(row[0], "schneller")
        self.assertEqual(row[1], "am schnellsten")

    def test_update_word_fields_updates_adjektiv_forms_only(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        entry = _add_adjektiv(db_path, german="klug", russian="умный")
        update_word_fields(
            db_path,
            entry["id"],
            {
                "komparativ": "klüger",
                "superlativ": "am klügsten",
                "unknown_field": "x",
            },
        )
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT german, komparativ, superlativ FROM words WHERE id = ?",
                (entry["id"],),
            ).fetchone()
        self.assertEqual(row[0], "klug")
        self.assertEqual(row[1], "klüger")
        self.assertEqual(row[2], "am klügsten")

    def test_select_due_returns_adjektiv_forms_in_entry(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        entry = _add_adjektiv(db_path, german="ruhig", russian="спокойный", komp="ruhiger", supr="am ruhigsten")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO quiz_events (user_id, word_id, mode, is_correct) VALUES (?, ?, 'ru_de', 1)",
                (1, entry["id"]),
            )
        due = select_due(db_path, user_id=1, mode="ru_de")
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["komparativ"], "ruhiger")
        self.assertEqual(due[0]["superlativ"], "am ruhigsten")


if __name__ == "__main__":
    unittest.main()
