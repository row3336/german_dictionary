"""spec #3 — apply script for the hand-filled conjugation file.

Covers the pure conversion/validation core (block_to_text, build_updates) and the
DB write (apply_updates). The file-format itself is authored by hand; here we only
guarantee the loader validates + writes correctly.
"""

import os
import sqlite3
import tempfile
import unittest

from bot.storage import init_db
from scripts.apply_conjugation_fill import apply_updates, block_to_text, build_updates

FORMS_P = ["gehe", "gehst", "geht", "gehen", "geht", "gehen"]
PRONOUN_KEYS = ["ich", "du", "er/sie/es", "wir", "ihr", "sie/Sie"]


def _block(forms):
    return dict(zip(PRONOUN_KEYS, forms))


class BlockToTextTest(unittest.TestCase):
    def test_full_block(self):
        self.assertEqual(
            block_to_text(_block(FORMS_P)),
            "ich gehe\ndu gehst\ner/sie/es geht\nwir gehen\nihr geht\nsie/Sie gehen",
        )

    def test_all_empty_is_blank(self):
        self.assertEqual(block_to_text(_block(["", "", "", "", "", ""])), "")
        self.assertEqual(block_to_text(None), "")

    def test_partial_block_keeps_missing_blank(self):
        # partial -> text with a missing form; build_updates will reject it as invalid
        txt = block_to_text(_block(["gehe", "", "geht", "gehen", "geht", "gehen"]))
        self.assertTrue(txt.startswith("ich gehe"))
        self.assertIn("\ndu \n", "\n" + txt + "\n")  # 'du' line carries no form


class BuildUpdatesTest(unittest.TestCase):
    def test_valid_regular_and_modal(self):
        doc = {"verbs": [
            {"id": "w1", "infinitive": "gehen",
             "conjugation_praesens": _block(FORMS_P), "conjugation_praeteritum": None},
            {"id": "w2", "infinitive": "können",
             "conjugation_praesens": _block(["kann", "kannst", "kann", "können", "könnt", "können"]),
             "conjugation_praeteritum": _block(["konnte", "konntest", "konnte", "konnten", "konntet", "konnten"])},
        ]}
        updates, errors, empty = build_updates(doc)
        self.assertEqual(errors, [])
        self.assertEqual(empty, 0)
        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[0][0], "w1")
        self.assertTrue(updates[1][3].startswith("ich konnte"))  # praeteritum text

    def test_empty_skipped(self):
        doc = {"verbs": [
            {"id": "w1", "infinitive": "abfangen",
             "conjugation_praesens": _block(["", "", "", "", "", ""]), "conjugation_praeteritum": None},
        ]}
        updates, errors, empty = build_updates(doc)
        self.assertEqual((len(updates), len(errors), empty), (0, 0, 1))

    def test_partial_is_error(self):
        doc = {"verbs": [
            {"id": "w1", "infinitive": "gehen",
             "conjugation_praesens": _block(["gehe", "", "geht", "gehen", "geht", "gehen"]),
             "conjugation_praeteritum": None},
        ]}
        updates, errors, empty = build_updates(doc)
        self.assertEqual(len(updates), 0)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][1], "praesens")


class ApplyUpdatesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO words (id, type, german, infinitive, russian) VALUES ('w1','verb','gehen','gehen','идти')")
        conn.commit()
        conn.close()

    def test_writes_praesens(self):
        doc = {"verbs": [
            {"id": "w1", "infinitive": "gehen",
             "conjugation_praesens": _block(FORMS_P), "conjugation_praeteritum": None},
        ]}
        updates, errors, _ = build_updates(doc)
        self.assertEqual(errors, [])
        apply_updates(self.db, updates)
        conn = sqlite3.connect(self.db)
        val = conn.execute("SELECT conjugation_praesens FROM words WHERE id='w1'").fetchone()[0]
        conn.close()
        self.assertEqual(val, "ich gehe\ndu gehst\ner/sie/es geht\nwir gehen\nihr geht\nsie/Sie gehen")


if __name__ == "__main__":
    unittest.main()
