"""Written answers accept only the approved adjective degree pairs."""

import tempfile
import unittest
from pathlib import Path

from tests._telegram_stub import install

install()

from bot import storage
from bot import learning_store
from bot.common.adjective_forms import CREATE_TABLE_SQL
from bot.handlers.quiz import _check_ru_de_strict
from bot.study.generations import preserve_selected_entry, selected_entry


def accepts(entry, text):
    return _check_ru_de_strict(entry, text)[0]


class AdjectiveDegreePairsTest(unittest.TestCase):
    def test_hoch_primary_and_citation_superlative(self):
        entry = {"type": "adjektiv", "german": "hoch", "komparativ": "höher",
                 "superlativ": "am höchsten", "adjective_degree_pairs": [
                     {"comparative": "höher", "superlative": "am höchsten"},
                     {"comparative": "höher", "superlative": "der höchste"},
                 ]}
        for answer in ("hoch", "hoch höher", "hoch, höher, am höchsten",
                       "hoch, höher, der höchste"):
            self.assertTrue(accepts(entry, answer), answer)
        for answer in ("hoch, hoher", "hoch, der höchste",
                       "hoch, hoher, der höchste"):
            self.assertFalse(accepts(entry, answer), answer)

    def test_blass_and_ungesund_do_not_cross_pair_variants(self):
        cases = (
            ("blass", "blasser", "am blassesten", [
                ("blasser", "am blassesten"), ("blasser", "der blasseste"),
                ("blässer", "am blässesten"),
            ], ["blässer, am blassesten", "blasser, am blässesten"]),
            ("ungesund", "ungesünder", "am ungesündesten", [
                ("ungesünder", "am ungesündesten"),
                ("ungesunder", "am ungesundesten"),
            ], ["ungesünder, am ungesundesten", "ungesunder, am ungesündesten"]),
        )
        for base, primary_comp, primary_super, pairs, forbidden in cases:
            with self.subTest(base=base):
                entry = {"type": "adjektiv", "german": base,
                         "komparativ": primary_comp, "superlativ": primary_super,
                         "adjective_degree_pairs": [
                             {"comparative": comp, "superlative": sup} for comp, sup in pairs
                         ]}
                for comp, sup in pairs:
                    self.assertTrue(accepts(entry, f"{base}, {comp}"))
                    self.assertTrue(accepts(entry, f"{base}, {comp}, {sup}"))
                for suffix in forbidden:
                    self.assertFalse(accepts(entry, f"{base}, {suffix}"))

    def test_superlative_only_has_no_invented_comparative(self):
        entry = {"type": "adjektiv", "german": "innere", "komparativ": "",
                 "superlativ": "der innerste", "adjective_degree_pairs": [
                     {"comparative": "", "superlative": "der innerste"},
                     {"comparative": "", "superlative": "die innerste"},
                     {"comparative": "", "superlative": "das innerste"},
                 ]}
        self.assertTrue(accepts(entry, "innere, die innerste"))
        self.assertFalse(accepts(entry, "innere, innerer, die innerste"))

    def test_schema21_without_table_uses_scalar_and_table_snapshot_is_stable(self):
        root = Path(tempfile.mkdtemp(prefix="gd-adjective-pairs-"))
        db = root / "words.db"
        storage.init_db(str(db))
        card = storage.add_word(str(db), {"type": "adjektiv", "german": "hoch",
            "komparativ": "höher", "superlativ": "am höchsten", "russian": "высокий"})
        with storage._connect(str(db)) as conn:
            row = conn.execute("SELECT * FROM words WHERE id=?", (card["id"],)).fetchone()
            old = selected_entry(conn, 1, storage._row_to_entry(row))
        self.assertNotIn("adjective_degree_pairs", old)
        self.assertTrue(accepts(old, "hoch, höher, am höchsten"))
        self.assertFalse(accepts(old, "hoch, höher, der höchste"))

        with storage._connect(str(db)) as conn:
            conn.execute(CREATE_TABLE_SQL)
            conn.executemany(
                "INSERT INTO adjective_degree_variants(word_id, comparative, superlative) "
                "VALUES (?, ?, ?)",
                [(card["id"], "höher", "am höchsten"),
                 (card["id"], "höher", "der höchste")],
            )
            row = conn.execute("SELECT * FROM words WHERE id=?", (card["id"],)).fetchone()
            selected = selected_entry(conn, 1, storage._row_to_entry(row))
        self.assertTrue(accepts(selected, "hoch, höher, der höchste"))
        with storage._connect(str(db)) as conn:
            conn.execute("""INSERT INTO quiz_events(user_id,word_id,mode,is_correct)
                VALUES (1,?,'de_ru',1)""", (card["id"],))
            ordinary = storage.select_due(str(db), 1, "ru_de", conn=conn)
            self.assertEqual([row["id"] for row in ordinary], [card["id"]])
            ordinary_entry = selected_entry(conn, 1, ordinary[0])
        self.assertTrue(accepts(ordinary_entry, "hoch, höher, der höchste"))
        alias = storage.add_word(str(db), {"type": "adjektiv", "german": "groß",
            "komparativ": "größer", "superlativ": "am größten", "russian": "высокий"})
        with storage._connect(str(db)) as conn:
            conn.execute("""INSERT INTO adjective_degree_variants
                (word_id,comparative,superlative) VALUES (?,?,?)""",
                (alias["id"], "größer", "der größte"))
            group_id = conn.execute("INSERT INTO synonym_groups DEFAULT VALUES").lastrowid
            conn.executemany("INSERT INTO word_synonyms(word_id,group_id) VALUES (?,?)",
                             [(card["id"], group_id), (alias["id"], group_id)])
            selected_with_alias = selected_entry(conn, 1, storage._row_to_entry(
                conn.execute("SELECT * FROM words WHERE id=?", (card["id"],)).fetchone()))
        self.assertEqual(len(selected_with_alias["answer_aliases"]), 1)
        self.assertTrue(accepts(selected_with_alias, "groß, größer, der größte"))
        with storage._connect(str(db)) as conn:
            conn.execute("INSERT INTO learning_batches(id,user_id,target_qty) VALUES ('batch',1,1)")
            conn.execute("""INSERT INTO learning_items
                (user_id,word_id,batch_id,preview_done,next_due)
                VALUES (1,?,'batch',1,'2026-01-01T00:00:00Z')""", (card["id"],))
            learning_entry = learning_store.due_entries(conn, 1, "2026-01-02T00:00:00Z")[0]
        self.assertEqual(learning_entry["adjective_degree_pairs"], selected["adjective_degree_pairs"])
        self.assertTrue(accepts(learning_entry, "hoch, höher, der höchste"))
        refreshed = dict(selected, komparativ="hoher", superlativ="am hochsten",
                         adjective_degree_pairs=[{"comparative": "hoher", "superlative": "am hochsten"}])
        preserved = preserve_selected_entry(refreshed, selected)
        self.assertTrue(accepts(preserved, "hoch, höher, der höchste"))
        self.assertFalse(accepts(preserved, "hoch, hoher, am hochsten"))


if __name__ == "__main__":
    unittest.main()
