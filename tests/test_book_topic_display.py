"""Book-topic presentation against retained, synthetic filter snapshots."""

from copy import deepcopy
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from _telegram_stub import install

install()

from bot.common import word_filter as wf
from bot.common.settings import _topic_filter_banner
from bot.common.topic_display import format_topic_selection, topic_labels
from bot.handlers import word_filter_ui as ui


class BookTopicDisplayTest(unittest.TestCase):
    def setUp(self):
        root = os.environ.get("GD_TEST_STAND")
        if not root:
            raise RuntimeError("set GD_TEST_STAND to the retained session stand")
        self.root = Path(tempfile.mkdtemp(prefix="topic-display-", dir=root))
        self.conn = sqlite3.connect(self.root / "synthetic.db")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE user_settings(user_id INTEGER, key TEXT, value TEXT,
                                       PRIMARY KEY(user_id,key));
            CREATE TABLE user_levels(user_id INTEGER, level TEXT);
            CREATE TABLE topics(name TEXT PRIMARY KEY, description TEXT, level TEXT);
            CREATE TABLE words(id INTEGER PRIMARY KEY, topic TEXT, cefr_level TEXT);
        """)
        for n in range(15):
            self.add(str(n), "A1", f"A1 topic {n}")
        self.add("nom", "A1", "Nomen+")
        self.add("verb", "A1", "Verben+")
        for n, description in enumerate(("Meine Geschichte", "Medien", "Wochenende", "Schule"), 1):
            self.add(f"A2_{n:02}", "A2", description)
        self.put(wf.FILTER_REVISION_KEY, "7")
        self.save({"A1": "all", "A2": "all"})
        self.lang = patch.object(ui, "_user_interface_lang", return_value="ru")
        self.lang.start()
        self.addCleanup(self.lang.stop)

    def add(self, name, level, description=""):
        self.conn.execute("INSERT INTO topics VALUES(?,?,?)", (name, description, level))
        self.conn.execute("INSERT INTO words(topic,cefr_level) VALUES(?,?)", (name, level))

    def put(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO user_settings VALUES(1,?,?)", (key, value))

    def save(self, choices):
        self.put(wf.BOOK_FILTER_KEY, json.dumps({"version": 2, "selected_levels": list(choices),
            "topics_by_level": choices, "browsing_level": next(iter(choices), "A1"), "revision": 7}))

    def state(self):
        return wf.read_filter_state(1, self.conn)

    def topic_buttons(self, markup):
        return [b for row in markup.inline_keyboard for b in row
                if b.callback_data.startswith("wf:v2:t:")]

    def test_two_levels_all_and_actual_banner(self):
        state = self.state()
        self.assertEqual(wf.filter_label_from_state(state), "A1: все темы\nA2: все темы")
        for lang, value in (("ru", "все темы"), ("uk", "усі теми"), ("en", "all topics")):
            with self.subTest(lang=lang), patch("bot.common.lang._user_interface_lang", return_value=lang):
                with patch.object(wf, "_connect", side_effect=AssertionError("secondary filter reader")):
                    banner = _topic_filter_banner(1, self.conn)
            self.assertIn(f"🟥 *A1: {value}\nA2: {value}* 🟥", banner)
        self.assertIn("A1: все темы\nA2: все темы", ui._summary(state, "ru"))

    def test_partial_ranges_in_actual_summary_and_banner_label(self):
        self.save({"A1": ["1", "2", "3", "4", "7", "12"],
                   "A2": ["A2_01", "A2_02", "A2_03"]})
        state = self.state()
        expected = "A1: 1–4, 7, 12\nA2: 1–3"
        self.assertEqual(wf.filter_label_from_state(state), expected)
        self.assertIn(expected, ui._summary(state, "ru"))

    def test_explicit_full_remains_detailed_and_does_not_mutate(self):
        state = self.state()
        self.save({lv: state["by_level"][lv] for lv in ("A1", "A2")})
        state = self.state()
        before = deepcopy(state)
        changes = self.conn.total_changes
        self.assertEqual(wf.filter_label_from_state(state), "A1: 0–14, Nomen+, Verben+\nA2: 1–4")
        ui.render_topics(1, "A2", state)
        self.assertEqual(state, before)
        self.assertEqual(self.conn.total_changes, changes)
        self.assertIsInstance(state["config"]["topics_by_level"]["A1"], list)

    def test_actual_a2_menu_numbers_descriptions_and_exact_callbacks(self):
        state = self.state()
        text, markup = ui.render_topics(1, "A2", state)
        buttons = self.topic_buttons(markup)
        self.assertIn("(A2)", text)
        for n, description in enumerate(("Meine Geschichte", "Medien", "Wochenende", "Schule"), 1):
            self.assertIn(f"[{n}] {description}", text)
        self.assertNotIn("A2_0", text)
        self.assertEqual([b.text for b in buttons], ["1", "2", "3", "4"])
        self.assertEqual([b.callback_data for b in buttons], [
            ui.callback_data("t", 7, "A2", wf.topic_token(f"A2_{n:02}")) for n in range(1, 5)])

    def test_empty_stale_and_invalid_never_become_all(self):
        for choice in (["deleted"], ["A2_01"]):
            with self.subTest(choice=choice):
                self.save({"A1": choice})
                self.assertEqual(wf.filter_label_from_state(self.state()), "A1: ∅")
        self.save({"B1": "all"})
        self.assertEqual(wf.filter_label_from_state(self.state()), "B1: ∅")
        self.put(wf.BOOK_FILTER_KEY, "invalid JSON")
        self.assertEqual(wf.filter_label_from_state(self.state()), "∅")
        minimal = self.state()
        minimal.pop("registry")
        self.assertEqual(wf.filter_label_from_state(minimal), "∅")
        self.assertEqual(format_topic_selection("A1", [], [], {}, dynamic_all=True), "∅")
        self.assertNotEqual(format_topic_selection("A1", ["1"], ["1", "2"], {}, dynamic_all=True), "все темы")

    def test_leading_zero_topics_stay_distinct_in_menu_and_summary(self):
        for name in ("03", "011", "012"):
            self.add(name, "A1", f"legacy {name}")
        self.save({"A1": ["03", "3", "011", "11", "012", "12"]})
        state = self.state()
        text, markup = ui.render_topics(1, "A1", state)
        labels = {b.text for b in self.topic_buttons(markup)}
        for name in ("03", "3", "011", "11", "012", "12"):
            self.assertIn(name, labels)
            self.assertIn(f"[{name}]", text)
        value = wf.filter_label_from_state(state)
        self.assertEqual(value, "A1: 03, 011, 012, 3, 11, 12")
        self.assertEqual(set(wf.filter_from_state(state).topics), {"03", "3", "011", "11", "012", "12"})

    def test_short_label_collisions_fall_back_without_hiding_any_key(self):
        names = ["A2_01", "1", "A2_1", "nom", "Nomen+", "verb"]
        labels = topic_labels("A2", names, {"nom": "Nomen+", "verb": "nom"})
        self.assertEqual(len(set(labels.values())), len(names))
        self.assertEqual(labels["A2_01"], "A2_01")
        self.assertEqual(labels["A2_1"], "A2_1")
        self.assertEqual(labels["nom"], "nom")
        self.assertEqual(labels["verb"], "verb")

    def test_actual_menu_collision_keeps_full_keys_and_callbacks(self):
        self.add("A2_1", "A2", "Other first topic")
        state = self.state()
        text, markup = ui.render_topics(1, "A2", state)
        buttons = self.topic_buttons(markup)
        self.assertEqual(len({b.text for b in buttons}), len(buttons))
        by_text = {b.text: b.callback_data for b in buttons}
        for name in ("A2_01", "A2_1"):
            self.assertIn(f"[{name}]", text)
            self.assertEqual(by_text[name], ui.callback_data("t", 7, "A2", wf.topic_token(name)))

    def test_nonstandard_and_unicode_names_are_preserved(self):
        names = ["2", "4", "²", "３", "A2_ä", "🍎", "nom", "verb"]
        descriptions = {"nom": "Nomen+", "verb": "Verben+"}
        value = format_topic_selection("A2", names, names, descriptions)
        self.assertEqual(value, "2, 4, ², ３, A2_ä, 🍎, Nomen+, Verben+")
        self.assertEqual(topic_labels("A2", ["03", "3"], {}), {"03": "03", "3": "3"})

    def test_explicit_and_dynamic_all_keep_their_growth_semantics(self):
        state = self.state()
        a1 = state["by_level"]["A1"]
        self.save({"A1": a1, "A2": "all"})
        self.add("15", "A1")
        self.add("A2_05", "A2")
        state = self.state()
        self.assertNotIn("15", wf.filter_from_state(state).topics)
        self.assertIn("A2_05", wf.filter_from_state(state).topics)
        self.assertEqual(wf.filter_label_from_state(state), "A1: 0–14, Nomen+, Verben+\nA2: все темы")

    def test_legacy_all_and_cefr_compatibility(self):
        self.conn.execute("DELETE FROM user_settings WHERE key=?", (wf.BOOK_FILTER_KEY,))
        self.put("topic_filter", "all")
        state = self.state()
        self.assertTrue(state["legacy"])
        self.assertEqual(wf.filter_label_from_state(state), "A1: все темы")
        self.put("topic_filter", "deleted")
        self.assertEqual(wf.filter_label_from_state(self.state()), "A1: ∅")
        self.put(wf.FILTER_MODE_KEY, "cefr")
        self.assertEqual(wf.filter_label_from_state(self.state()), "CEFR: all")
        self.conn.execute("INSERT INTO user_levels VALUES(1,'A2')")
        self.assertEqual(wf.filter_label_from_state(self.state()), "CEFR: A2")

    def test_localized_all_and_unchanged_topic_reducer(self):
        state = self.state()
        self.assertEqual(wf.filter_label_from_state(state, "en"), "A1: all topics\nA2: all topics")
        self.assertEqual(wf.filter_label_from_state(state, "uk"), "A1: усі теми\nA2: усі теми")
        before = deepcopy(state)
        mode, config = wf.reduce_filter(state, "topic", "A2", wf.topic_token("A2_01"))
        self.assertEqual(mode, wf.MODE_TOPICS)
        self.assertEqual(config["revision"], 8)
        self.assertEqual(config["topics_by_level"]["A2"], ["A2_02", "A2_03", "A2_04"])
        self.assertEqual(config["topics_by_level"]["A1"], "all")
        self.assertEqual(state, before)

    def test_actual_settings_handler_passes_interface_language(self):
        from bot.handlers import settings as handler
        from bot.common.settings import _topic_filter_label
        for lang, value in (("ru", "все темы"), ("uk", "усі теми"), ("en", "all topics")):
            message = SimpleNamespace(reply_text=AsyncMock())
            update = SimpleNamespace(message=message)
            with self.subTest(lang=lang), \
                    patch.object(handler, "_user_interface_lang", return_value=lang), \
                    patch.object(handler, "list_topics", return_value=[]), \
                    patch.object(handler, "_algo_settings_menu_keys", return_value=[]), \
                    patch.object(handler, "_topic_filter_label", side_effect=lambda uid, lang:
                                 _topic_filter_label(uid, conn=self.conn, lang=lang)) as label:
                asyncio.run(handler._send_algo_settings_menu(update, 1))
                label.assert_called_once_with(1, lang=lang)
            self.assertIn(f"A1: {value}\nA2: {value}", message.reply_text.call_args.args[0])

    def test_actual_unicode_menu_preserves_keys_and_tokens(self):
        names = ["Äpfel", "🍎", "A2_ä"]
        for name in names:
            self.add(name, "A2", "Unicode topic")
        state = self.state()
        text, markup = ui.render_topics(1, "A2", state)
        callbacks = {b.text: b.callback_data for b in self.topic_buttons(markup)}
        for name in names:
            self.assertIn(f"[{name}] Unicode topic", text)
            self.assertEqual(callbacks[name], ui.callback_data("t", 7, "A2", wf.topic_token(name)))
