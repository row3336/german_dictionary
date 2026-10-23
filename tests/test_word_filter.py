"""P2 quiz word-filter resolver + CEFR filtering in the query layer
(spec_topics_levels.md §3). Covers mode exclusivity, the 'all six levels = no
restriction' rule, book-level topic scoping, empty-cefr hiding, and per-user
isolation. select_due's own scheduling is not re-tested here — learning_pick_new_words
is the simplest query that just applies the topic/cefr WHERE clauses.
"""

import os
import tempfile
import unittest

from bot import storage
from bot.app_config import TOPIC_FILTER_KEY
from bot.common import word_filter as wf

U = 1000


def _add(db, german, wtype, russian, topic, cefr=""):
    entry = {"german": german, "type": wtype, "russian": russian, "topic": topic, "cefr_level": cefr}
    if wtype == "verb":
        entry["reflexive_case"] = "none"
        entry["infinitive"] = german
    if wtype == "nomen":
        entry["article"] = "das"
    storage.add_word(db, entry)


class WordFilterResolverTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        storage.init_db(self.db)
        self._old = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = self.db
        self.addCleanup(self._restore)
        storage.register_or_update_user(self.db, U, "u", "U")
        # topic 1 -> A1 (Haus A1, Buch A2), topic 2 -> B1 (reisen B1), topic 3 -> A1 (Auto, no cefr)
        _add(self.db, "Haus", "nomen", "дом", "1", "A1")
        _add(self.db, "Buch", "nomen", "книга", "1", "A2")
        _add(self.db, "reisen", "verb", "ехать", "2", "B1")
        _add(self.db, "Auto", "nomen", "авто", "3", "")
        storage.set_topic_level(self.db, "1", "A1")
        storage.set_topic_level(self.db, "2", "B1")
        storage.set_topic_level(self.db, "3", "A1")

    def _restore(self):
        if self._old is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = self._old

    # --- defaults & mode switching ---

    def test_default_mode_is_topics_a1_all(self):
        self.assertEqual(wf.get_filter_mode(U), "topics")
        self.assertEqual(wf.get_active_book_level(U), "A1")
        qf = wf.resolve_quiz_filter(U)
        self.assertEqual(qf.mode, "topics")
        self.assertIsNone(qf.cefr_levels)
        # 'all' of A1 -> topics 1 and 3 (both A1), not topic 2 (B1)
        self.assertEqual(set(qf.topics), {"1", "3"})

    def test_set_and_get_active_book_level(self):
        wf.set_active_book_level(U, "B1")
        self.assertEqual(wf.get_active_book_level(U), "B1")
        qf = wf.resolve_quiz_filter(U)
        self.assertEqual(set(qf.topics), {"2"})
        with self.assertRaises(ValueError):
            wf.set_active_book_level(U, "ZZ")

    def test_topic_selection_scoped_to_level(self):
        # selecting topic 1 while on A1 -> just topic 1
        storage.set_user_setting(self.db, U, TOPIC_FILTER_KEY, "1")
        qf = wf.resolve_quiz_filter(U)
        self.assertEqual(qf.topics, ["1"])
        # A stale selection must remain empty rather than broaden to all topics.
        storage.set_user_setting(self.db, U, TOPIC_FILTER_KEY, "2")  # 2 is B1, level is A1
        qf = wf.resolve_quiz_filter(U)
        self.assertEqual(qf.topics, [])

    # --- CEFR mode ---

    def test_cefr_all_six_means_no_restriction(self):
        wf.set_filter_mode(U, "cefr")
        qf = wf.resolve_quiz_filter(U)
        self.assertEqual(qf.mode, "cefr")
        self.assertIsNone(qf.topics)
        self.assertIsNone(qf.cefr_levels)  # default = all six -> no restriction

    def test_cefr_subset(self):
        wf.set_filter_mode(U, "cefr")
        for lvl in ("A2", "B1", "B2", "C1", "C2"):
            storage.toggle_user_level(self.db, U, lvl)  # leave only A1
        qf = wf.resolve_quiz_filter(U)
        self.assertEqual(qf.cefr_levels, ["A1"])

    def test_mode_is_exclusive(self):
        # a CEFR subset must NOT leak into topics mode, and vice-versa
        for lvl in ("A2", "B1", "B2", "C1", "C2"):
            storage.toggle_user_level(self.db, U, lvl)  # user_levels now just A1
        # still in topics mode -> cefr_levels stays None
        qf = wf.resolve_quiz_filter(U)
        self.assertEqual(qf.mode, "topics")
        self.assertIsNone(qf.cefr_levels)

    # --- query-layer integration (learning_pick_new_words) ---

    def test_pick_topics_mode_filters_by_level(self):
        qf = wf.resolve_quiz_filter(U)  # topics A1 all -> {1,3}
        picked = storage.learning_pick_new_words(
            self.db, user_id=U, limit=50, topics=qf.topics, cefr_levels=qf.cefr_levels
        )
        germans = {w["german"] for w in picked}
        self.assertEqual(germans, {"Haus", "Buch", "Auto"})  # topic 2 'reisen' excluded

    def test_pick_cefr_subset_hides_empty_and_other_levels(self):
        wf.set_filter_mode(U, "cefr")
        for lvl in ("A1", "B1", "B2", "C1", "C2"):
            storage.toggle_user_level(self.db, U, lvl)  # leave only A2
        qf = wf.resolve_quiz_filter(U)
        picked = storage.learning_pick_new_words(
            self.db, user_id=U, limit=50, topics=qf.topics, cefr_levels=qf.cefr_levels
        )
        germans = {w["german"] for w in picked}
        self.assertEqual(germans, {"Buch"})  # only the A2 word; empty-cefr 'Auto' hidden

    def test_pick_cefr_all_shows_everything_including_empty(self):
        wf.set_filter_mode(U, "cefr")  # all six -> no restriction
        qf = wf.resolve_quiz_filter(U)
        picked = storage.learning_pick_new_words(
            self.db, user_id=U, limit=50, topics=qf.topics, cefr_levels=qf.cefr_levels
        )
        germans = {w["german"] for w in picked}
        self.assertEqual(germans, {"Haus", "Buch", "reisen", "Auto"})  # incl. empty-cefr Auto

    # --- empty-state counter mirrors the resolver ---

    def test_count_words_for_filter(self):
        self.assertEqual(wf.count_words_for_filter(U), 3)  # topics A1 all -> Haus,Buch,Auto
        wf.set_filter_mode(U, "cefr")
        for lvl in ("A1", "A2", "B2", "C1", "C2"):
            storage.toggle_user_level(self.db, U, lvl)  # leave only B1
        self.assertEqual(wf.count_words_for_filter(U), 1)  # just 'reisen'

    def test_per_user_isolation(self):
        other = 2000
        storage.register_or_update_user(self.db, other, "o", "O")
        wf.set_filter_mode(U, "cefr")
        wf.set_active_book_level(other, "B1")
        self.assertEqual(wf.get_filter_mode(other), "topics")  # unaffected by U
        self.assertEqual(wf.get_active_book_level(U), "A1")     # unaffected by other


if __name__ == "__main__":
    unittest.main()
