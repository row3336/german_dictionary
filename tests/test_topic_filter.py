"""spec #8 — topic-filter subsystem tests (sort helper first; UI/toggle added later)."""

import unittest

from bot.common import topics as T
from bot.common.topics import sorted_topics


class TopicSortTest(unittest.TestCase):
    def test_canonical_order(self):
        # leading-zero numerics, then plain numerics, then alphabetics
        names = ["verb", "7", "01", "0", "nom", "03", "1", "04", "011", "2", "02", "012"]
        self.assertEqual(
            sorted_topics(names),
            ["0", "01", "02", "03", "04", "011", "012", "1", "2", "7", "nom", "verb"],
        )

    def test_new_numeric_slots_by_value_not_lexically(self):
        # 05 sits between 04 and 011 (value 5, not lexical), 8 after 7
        out = sorted_topics(["04", "011", "05", "7", "8"])
        self.assertEqual(out, ["04", "05", "011", "7", "8"])

    def test_new_alpha_joins_alphabetics_at_end(self):
        out = sorted_topics(["verb", "food", "7", "nom"])
        self.assertEqual(out, ["7", "food", "nom", "verb"])

    def test_stable_and_idempotent(self):
        names = ["012", "0", "verb"]
        once = sorted_topics(names)
        self.assertEqual(once, sorted_topics(once))


class FilterLogicTest(unittest.TestCase):
    NAMES = ["01", "03", "7", "nom", "verb"]

    def test_resolve_all_expands(self):
        self.assertEqual(T.resolve_active("all", self.NAMES), set(self.NAMES))

    def test_resolve_list_intersects_known(self):
        # stale token 'gone' dropped; only known kept
        self.assertEqual(T.resolve_active("01+03+gone", self.NAMES), {"01", "03"})

    def test_toggle_add_uses_canonical_order(self):
        new, rejected = T.toggle("03+01", "7", self.NAMES)
        self.assertFalse(rejected)
        self.assertEqual(new, "01+03+7")  # canonical order in serialized string

    def test_toggle_remove(self):
        new, rejected = T.toggle("01+03", "03", self.NAMES)
        self.assertFalse(rejected)
        self.assertEqual(new, "01")

    def test_toggle_last_active_rejected(self):
        new, rejected = T.toggle("01", "01", self.NAMES)
        self.assertIsNone(new)
        self.assertTrue(rejected)

    def test_manual_full_selection_becomes_all(self):
        # adding the final missing topic collapses to 'all'
        raw = "+".join(n for n in self.NAMES if n != "verb")
        new, rejected = T.toggle(raw, "verb", self.NAMES)
        self.assertFalse(rejected)
        self.assertEqual(new, "all")

    def test_toggle_off_from_all(self):
        new, rejected = T.toggle("all", "7", self.NAMES)
        self.assertFalse(rejected)
        self.assertEqual(set(T.filter_tokens(new)), set(self.NAMES) - {"7"})


class AddInputParseTest(unittest.TestCase):
    def test_bare_name(self):
        self.assertEqual(T.parse_topic_input("05"), ("05", ""))

    def test_space_separator(self):
        self.assertEqual(T.parse_topic_input("05 Essen und Trinken"), ("05", "Essen und Trinken"))

    def test_comma_dot_dash_separators(self):
        self.assertEqual(T.parse_topic_input("05, Essen"), ("05", "Essen"))
        self.assertEqual(T.parse_topic_input("05..Essen"), ("05", "Essen"))
        self.assertEqual(T.parse_topic_input("05 - Essen"), ("05", "Essen"))
        self.assertEqual(T.parse_topic_input("05 -, . Essen"), ("05", "Essen"))

    def test_internal_punctuation_preserved(self):
        self.assertEqual(T.parse_topic_input("07 Essen, Trinken."), ("07", "Essen, Trinken."))

    def test_alpha_name(self):
        self.assertEqual(T.parse_topic_input("verb Verben"), ("verb", "Verben"))

    def test_empty(self):
        self.assertEqual(T.parse_topic_input("   "), ("", ""))


if __name__ == "__main__":
    unittest.main()
