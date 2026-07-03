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

    def test_select_all(self):
        self.assertEqual(T.select_all("01", self.NAMES), "all")
        self.assertIsNone(T.select_all("all", self.NAMES))  # no-op when already all


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


class RenderTest(unittest.TestCase):
    TOPICS = [("01", "Zahlen"), ("7", ""), ("verb", "Verben")]

    def test_text_has_codeblock_marks_and_dash(self):
        text = T.render_text("en", "01", self.TOPICS)
        self.assertIn("```text", text)
        self.assertIn("[✅] [01] Zahlen", text)   # active
        self.assertIn("[  ] [7] —", text)          # inactive + no description
        self.assertIn("[  ] [verb] Verben", text)

    def test_text_localized_surrounding(self):
        # surrounding text switches with interface lang (closes #6)
        self.assertIn("Фильтр тем", T.render_text("ru", "01", self.TOPICS))
        self.assertIn("Фільтр тем", T.render_text("uk", "01", self.TOPICS))

    def test_keyboard_rows_of_five_plus_all(self):
        names = ["01", "02", "03", "04", "05", "06", "07"]
        layout = T.keyboard_layout("all", names)
        # 7 topics -> rows of 5 => [5,2] then a trailing 'all' row
        self.assertEqual([len(r) for r in layout], [5, 2, 1])
        self.assertEqual(layout[-1][0][1], T.CB_ALL)  # (text, callback_data, style)

    def test_keyboard_style_only_on_active(self):
        layout = T.keyboard_layout("01", ["01", "03"])
        flat = {text: style for row in layout for text, _data, style in row}
        self.assertEqual(flat["01"], "success")
        self.assertIsNone(flat["03"])
        self.assertIsNone(flat["all"])  # not all -> neutral

    def test_keyboard_all_green_when_filter_all(self):
        layout = T.keyboard_layout("all", ["01", "03"])
        flat = {text: style for row in layout for text, _data, style in row}
        self.assertEqual(flat["all"], "success")
        self.assertEqual(flat["01"], "success")

    def test_callback_data_prefixes(self):
        layout = T.keyboard_layout("01", ["01", "verb"])
        datas = [data for row in layout for _t, data, _s in row]
        self.assertIn("topicf:t:01", datas)
        self.assertIn("topicf:t:verb", datas)
        self.assertIn("topicf:all", datas)


if __name__ == "__main__":
    unittest.main()
