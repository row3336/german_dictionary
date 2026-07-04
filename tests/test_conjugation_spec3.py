"""spec #3 — conjugation storage layer (validator + carousel selection + progress).

Migration itself is in test_migrations.py::ConjugationColumnsMigrationTest.
Card rendering / handler flow are covered in their own files.
"""

import os
import sqlite3
import tempfile
import unittest

from bot.common.conjugation import (
    PRONOUNS,
    is_valid_conjugation,
    parse_conjugation,
    takes_praeteritum,
)
from bot.storage import (
    init_db,
    next_conjugation_word,
    record_conjugation_shown,
    set_word_conjugation,
)

GOOD = "ich gehe\ndu gehst\ner/sie/es geht\nwir gehen\nihr geht\nsie/Sie gehen"


class ValidatorTest(unittest.TestCase):
    def test_canonical_accepted_and_normalized(self):
        # extra surrounding blank lines + wide spacing collapse to canonical form
        messy = "\n\nich    gehe\ndu gehst\ner/sie/es geht \nwir gehen\nihr geht\nsie/Sie gehen\n"
        self.assertEqual(parse_conjugation(messy), GOOD)
        self.assertTrue(is_valid_conjugation(GOOD))

    def test_wrong_line_count_rejected(self):
        five = "ich a\ndu b\ner/sie/es c\nwir d\nihr e"
        seven = GOOD + "\nsie/Sie extra"
        self.assertFalse(is_valid_conjugation(five))
        self.assertFalse(is_valid_conjugation(seven))
        with self.assertRaises(ValueError):
            parse_conjugation(five)

    def test_wrong_pronoun_order_rejected(self):
        swapped = "du a\nich b\ner/sie/es c\nwir d\nihr e\nsie/Sie f"
        self.assertFalse(is_valid_conjugation(swapped))

    def test_missing_pronoun_rejected(self):
        no_pron = "gehe\ngehst\ngeht\ngehen\ngeht\ngehen"
        self.assertFalse(is_valid_conjugation(no_pron))

    def test_blank_form_rejected(self):
        # pronoun present but no form after it
        blank = "ich\ndu b\ner/sie/es c\nwir d\nihr e\nsie/Sie f"
        self.assertFalse(is_valid_conjugation(blank))

    def test_pronoun_constants_order(self):
        self.assertEqual(PRONOUNS, ("ich", "du", "er/sie/es", "wir", "ihr", "sie/Sie"))

    def test_praeteritum_closed_list(self):
        for inf in ("können", "müssen", "wollen", "sollen", "dürfen", "mögen", "werden", "sein", "haben"):
            self.assertTrue(takes_praeteritum(inf))
        self.assertTrue(takes_praeteritum("  Sein "))  # trimmed + case-insensitive
        for inf in ("gehen", "machen", "lernen", ""):
            self.assertFalse(takes_praeteritum(inf))


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.db = os.path.join(self.d, "words.db")
        init_db(self.db)

    def _add_verb(self, wid, infinitive):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO words (id, type, german, infinitive, russian, topic) VALUES (?, 'verb', ?, ?, ?, '1')",
            (wid, infinitive, infinitive, infinitive),
        )
        conn.commit()
        conn.close()

    def _set_progress(self, user_id, wid, mode, repetitions):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO progress (user_id, word_id, mode, interval, repetitions, ease, due) "
            "VALUES (?, ?, ?, 3, ?, 2.5, '2020-01-01')",
            (user_id, wid, mode, repetitions),
        )
        conn.commit()
        conn.close()

    def _set_conj_progress(self, user_id, wid, seen_count, last_seen_at):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO conjugation_progress (user_id, word_id, seen_count, last_seen_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, wid, seen_count, last_seen_at),
        )
        conn.commit()
        conn.close()


class SetConjugationTest(_DbCase):
    def test_writes_and_validates(self):
        self._add_verb("w1", "gehen")
        set_word_conjugation(self.db, "w1", GOOD)
        conn = sqlite3.connect(self.db)
        praesens, praet = conn.execute(
            "SELECT conjugation_praesens, conjugation_praeteritum FROM words WHERE id='w1'"
        ).fetchone()
        conn.close()
        self.assertEqual(praesens, GOOD)
        self.assertEqual(praet, "")

    def test_garbage_rejected(self):
        self._add_verb("w1", "gehen")
        with self.assertRaises(ValueError):
            set_word_conjugation(self.db, "w1", "not a conjugation")

    def test_empty_clears(self):
        self._add_verb("w1", "gehen")
        set_word_conjugation(self.db, "w1", GOOD)
        set_word_conjugation(self.db, "w1", "")
        conn = sqlite3.connect(self.db)
        praesens = conn.execute("SELECT conjugation_praesens FROM words WHERE id='w1'").fetchone()[0]
        conn.close()
        self.assertEqual(praesens, "")


class CarouselSelectionTest(_DbCase):
    def test_eligibility_gates(self):
        # w1: eligible (verb, reps>=2 in ru_de, conj set)
        # w2: reps=1 -> excluded; w3: no conjugation -> excluded; w4: reps>=2 only in de_ru -> excluded
        for wid, inf in (("w1", "gehen"), ("w2", "sein"), ("w3", "haben"), ("w4", "machen")):
            self._add_verb(wid, inf)
            if wid != "w3":
                set_word_conjugation(self.db, wid, GOOD)
        self._set_progress(7, "w1", "ru_de", 2)
        self._set_progress(7, "w2", "ru_de", 1)
        self._set_progress(7, "w3", "ru_de", 5)
        self._set_progress(7, "w4", "de_ru", 5)

        picks = set()
        for _ in range(6):
            nxt = next_conjugation_word(self.db, 7, "ru_de")
            self.assertIsNotNone(nxt)
            picks.add(nxt["id"])
            record_conjugation_shown(self.db, 7, nxt["id"])
        self.assertEqual(picks, {"w1"})  # only w1 ever eligible

    def test_empty_pool_returns_none(self):
        self._add_verb("w1", "gehen")
        set_word_conjugation(self.db, "w1", GOOD)
        self._set_progress(7, "w1", "ru_de", 2)
        # different user has no progress -> nothing
        self.assertIsNone(next_conjugation_word(self.db, 999, "ru_de"))
        # right user, wrong (language) mode -> nothing
        self.assertIsNone(next_conjugation_word(self.db, 7, "uk_de"))

    def test_language_dependent_mode(self):
        self._add_verb("w1", "gehen")
        set_word_conjugation(self.db, "w1", GOOD)
        self._set_progress(7, "w1", "uk_de", 2)  # uk user's L1>De mode
        self.assertIsNone(next_conjugation_word(self.db, 7, "ru_de"))
        got = next_conjugation_word(self.db, 7, "uk_de")
        self.assertEqual(got["id"], "w1")

    def test_per_user_isolation(self):
        self._add_verb("w1", "gehen")
        self._add_verb("w2", "sein")
        set_word_conjugation(self.db, "w1", GOOD)
        set_word_conjugation(self.db, "w2", GOOD)
        self._set_progress(7, "w1", "ru_de", 2)
        self._set_progress(8, "w2", "ru_de", 2)
        self.assertEqual(next_conjugation_word(self.db, 7, "ru_de")["id"], "w1")
        self.assertEqual(next_conjugation_word(self.db, 8, "ru_de")["id"], "w2")

    def test_unseen_first(self):
        # w1 already seen recently, w2 never seen -> w2 (unseen) must come first
        self._add_verb("w1", "gehen")
        self._add_verb("w2", "sein")
        set_word_conjugation(self.db, "w1", GOOD)
        set_word_conjugation(self.db, "w2", GOOD)
        self._set_progress(7, "w1", "ru_de", 2)
        self._set_progress(7, "w2", "ru_de", 2)
        self._set_conj_progress(7, "w1", seen_count=3, last_seen_at="2020-01-01 00:00:00")
        self.assertEqual(next_conjugation_word(self.db, 7, "ru_de")["id"], "w2")

    def test_weighted_rotation_learned_less_often(self):
        # Both seen; w1 seen many times (heavier weight), w2 seen once.
        # Same elapsed time favours the lighter (less-seen) w2 via elapsed/(1+seen).
        self._add_verb("w1", "gehen")
        self._add_verb("w2", "sein")
        set_word_conjugation(self.db, "w1", GOOD)
        set_word_conjugation(self.db, "w2", GOOD)
        self._set_progress(7, "w1", "ru_de", 2)
        self._set_progress(7, "w2", "ru_de", 2)
        self._set_conj_progress(7, "w1", seen_count=9, last_seen_at="2020-01-01 00:00:00")
        self._set_conj_progress(7, "w2", seen_count=1, last_seen_at="2020-01-01 00:00:00")
        self.assertEqual(next_conjugation_word(self.db, 7, "ru_de")["id"], "w2")

    def test_least_recently_seen_first_when_equal_weight(self):
        self._add_verb("w1", "gehen")
        self._add_verb("w2", "sein")
        set_word_conjugation(self.db, "w1", GOOD)
        set_word_conjugation(self.db, "w2", GOOD)
        self._set_progress(7, "w1", "ru_de", 2)
        self._set_progress(7, "w2", "ru_de", 2)
        # equal seen_count; w1 seen longer ago -> higher elapsed -> comes first
        self._set_conj_progress(7, "w1", seen_count=2, last_seen_at="2020-01-01 00:00:00")
        self._set_conj_progress(7, "w2", seen_count=2, last_seen_at="2024-01-01 00:00:00")
        self.assertEqual(next_conjugation_word(self.db, 7, "ru_de")["id"], "w1")


class RecordShownTest(_DbCase):
    def test_increments_and_stamps(self):
        self._add_verb("w1", "gehen")
        record_conjugation_shown(self.db, 7, "w1")
        record_conjugation_shown(self.db, 7, "w1")
        conn = sqlite3.connect(self.db)
        seen, last = conn.execute(
            "SELECT seen_count, last_seen_at FROM conjugation_progress WHERE user_id=7 AND word_id='w1'"
        ).fetchone()
        conn.close()
        self.assertEqual(seen, 2)
        self.assertIsNotNone(last)


if __name__ == "__main__":
    unittest.main()
