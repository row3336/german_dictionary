"""K16 book-level contracts on disposable SQLite; no Telegram/network/real data."""

import asyncio
import copy
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests._telegram_stub import install
install()

from bot import storage
from bot.app_config import CEFR_LEVELS, TOPIC_FILTER_KEY
from bot.common import word_filter as wf, settings, session
from bot.handlers import word_filter_ui as UI
from bot.i18n import t
from bot.oral import repository as oral_repo
from tests import test_new_logic_oral as oral_fixture


class Query:
    def __init__(self, data, uid=7):
        self.data = data
        self.from_user = SimpleNamespace(id=uid)
        self.edits, self.alerts = [], []

    async def answer(self, text=None, **kwargs):
        if text:
            self.alerts.append(text)

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("reply_markup")))


def buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


class BookLevelsTests(unittest.TestCase):
    def setUp(self):
        # The runner directs TMPDIR to its approved stand. Retain this test's
        # runtime there: directory removal is unavailable in the managed client.
        self.tmp = tempfile.mkdtemp(prefix="gd-k16-")
        self.db = os.path.join(self.tmp, "synthetic.sqlite")
        self.env = patch.dict(os.environ, {"DB_PATH": self.db})
        self.env.start()
        self.addCleanup(self.env.stop)
        storage.init_db(self.db)
        for uid in (7, 8):
            storage.set_user_status(self.db, uid, "approved")
            storage.set_user_interface_lang(self.db, uid, "en")
        for name, level, cefr in (("01", "A1", "B1"), ("03", "A1", "A1"),
                                  ("11", "A2", "A2"), ("13", "A2", "A1")):
            storage.add_word(self.db, {"german": "Haus" + name, "type": "nomen",
                "article": "das", "russian": "дом", "topic": name, "cefr_level": cefr})
            storage.set_topic_level(self.db, name, level)

    def state(self, uid=7):
        return wf.read_filter_state(uid)

    def command(self, operation, level=None, token=None, uid=7):
        return wf.apply_filter_command(uid, self.state(uid)["revision"], operation, level, token)

    def tap(self, operation, level="", name=None, uid=7):
        data = UI.callback_data(operation, self.state(uid)["revision"], level,
                                wf.topic_token(name) if name else "")
        return self.raw_tap(data, uid)

    def raw_tap(self, data, uid=7):
        q = Query(data, uid)
        asyncio.run(UI.word_filter_callback(SimpleNamespace(callback_query=q), SimpleNamespace(user_data={})))
        return q

    def stored(self, uid=7):
        with storage._connect(self.db) as conn:
            return [tuple(row) for row in conn.execute("SELECT key,value FROM user_settings WHERE user_id=? ORDER BY key", (uid,))]

    def config(self, **changes):
        value = {"version": 2, "selected_levels": ["A1"], "topics_by_level": {"A1": "all"},
                 "browsing_level": "A1", "revision": 0}
        value.update(changes)
        return value

    def save_config(self, value):
        storage.set_user_setting(self.db, 7, wf.BOOK_FILTER_KEY, json.dumps(value))

    def test_B01_both_level_orders_and_per_level_retention(self):
        for uid, first, second in ((7, "A1", "A2"), (8, "A2", "A1")):
            wf.set_active_book_level(uid, first)
            self.command("level", second, uid=uid)
            self.assertEqual(self.state(uid)["config"]["selected_levels"], ["A1", "A2"])
            self.command("topic", "A1", wf.topic_token("03"), uid)
            self.command("topic", "A2", wf.topic_token("13"), uid)
            self.assertEqual(wf.resolve_quiz_filter(uid).topics, ["01", "11"])
            self.command("level", first, uid=uid)
            self.assertEqual(self.state(uid)["config"]["selected_levels"], [second])
            self.command("level", first, uid=uid)
            self.assertEqual(wf.resolve_quiz_filter(uid).topics, ["01", "11"])
            self.assertEqual(self.state(uid)["config"]["topics_by_level"], {"A1": ["01"], "A2": ["11"]})

    def test_B01_all_changes_only_selected_levels(self):
        self.command("level", "A2")
        self.command("topic", "A1", wf.topic_token("03"))
        self.command("topic", "A2", wf.topic_token("13"))
        self.command("level", "A2")
        self.command("all")
        self.assertEqual(self.state()["config"]["topics_by_level"], {"A1": "all", "A2": ["11"]})
        self.command("level", "A2")
        self.assertEqual(wf.resolve_quiz_filter(7).topics, ["01", "03", "11"])

    def test_B01_reject_last_level_and_topic_without_any_write(self):
        before = self.stored()
        with self.assertRaisesRegex(wf.FilterCommandError, "need_one"):
            self.command("level", "A1")
        self.assertEqual(self.stored(), before)
        self.command("topic", "A1", wf.topic_token("03"))
        before = self.stored()
        with self.assertRaisesRegex(wf.FilterCommandError, "need_one"):
            self.command("topic", "A1", wf.topic_token("01"))
        self.assertEqual(self.stored(), before)

    def test_B02_pure_legacy_reader_and_equivalent_first_write(self):
        storage.set_user_setting(self.db, 7, TOPIC_FILTER_KEY, "03")
        before = self.stored()
        UI.render_booklevels(7)
        UI.render_topics(7)
        self.assertEqual(wf.resolve_quiz_filter(7).topics, ["03"])
        self.assertEqual(wf.count_words_for_filter(7), 1)
        self.assertEqual(self.stored(), before)
        self.command("level", "A2")
        self.assertFalse(self.state()["legacy"])
        self.assertEqual(self.state()["config"]["topics_by_level"]["A1"], ["03"])
        self.assertEqual(wf.resolve_quiz_filter(7).topics, ["03", "11", "13"])

    def test_B02_malformed_future_empty_and_unknown_topics_never_expand(self):
        for value in ("{", '{"version":99}', json.dumps(self.config(selected_levels=[])),
                      json.dumps(self.config(topics_by_level={"A1": ["missing"]})),
                      json.dumps(self.config(topics_by_level={"A1": []}))):
            with self.subTest(value=value):
                storage.set_user_setting(self.db, 7, wf.BOOK_FILTER_KEY, value)
                before = self.stored()
                self.assertEqual(wf.resolve_quiz_filter(7).topics, [])
                self.assertEqual(wf.count_words_for_filter(7), 0)
                self.assertIn("∅", settings._topic_filter_label(7))
                self.assertEqual(self.stored(), before)
                UI.render_booklevels(7)
                self.assertEqual(self.stored(), before)

    def test_B02_explicit_choice_repairs_missing_legacy_level_without_all_fallback(self):
        with storage._connect(self.db) as conn:
            conn.execute("UPDATE topics SET level='' WHERE level='A1'")
        self.assertEqual(wf.resolve_quiz_filter(7).topics, [])
        self.command("level", "A2")
        self.assertEqual(self.state()["config"]["selected_levels"], ["A2"])
        self.assertEqual(wf.resolve_quiz_filter(7).topics, ["11", "13"])

    def test_B02_validator_is_strict_and_canonical(self):
        value = self.config(selected_levels=["A2", "A1", "A2"],
                            topics_by_level={"A2": ["13", "11", "13"], "A1": "all"})
        canonical = wf.validate_book_config(value)
        self.assertEqual(canonical["selected_levels"], ["A1", "A2"])
        self.assertEqual(canonical["topics_by_level"]["A2"], ["11", "13"])
        self.assertEqual(value["selected_levels"], ["A2", "A1", "A2"])
        for changes in ({"version": True}, {"revision": True}, {"browsing_level": "Z"},
                        {"selected_levels": ["Z"]}, {"topics_by_level": {"A1": [""]}}):
            with self.assertRaises(wf.FilterCommandError):
                wf.validate_book_config(self.config(**changes))

    def test_B03_stale_callbacks_and_raw_legacy_are_read_only(self):
        stale = UI.callback_data("l", self.state()["revision"], "A2")
        self.command("topic", "A1", wf.topic_token("03"))
        before = self.stored()
        for data in (stale, UI.CB_BLVL + "A2", UI.CB_TOPIC + "all", UI.CB_CEFR + "A1",
                     "wf:v2:l:NaN:A2", "wf:v2:t:0:A1:forged"):
            q = self.raw_tap(data)
            self.assertTrue(q.alerts)
            self.assertEqual(self.stored(), before)

    def test_B03_two_users_reentry_restart_and_nav_do_not_mutate(self):
        self.command("level", "A2")
        before = self.stored()
        state = self.state()
        for data in (UI.callback_data("n", state["revision"], "fork"),
                     UI.callback_data("n", state["revision"], "topics", "A1"),
                     UI.callback_data("n", state["revision"], "topics", "A2")):
            self.raw_tap(data)
        self.assertEqual(self.stored(), before)
        with storage._connect(self.db) as conn:
            self.assertEqual(wf.resolve_quiz_filter(7, conn=conn).topics, ["01", "03", "11", "13"])
        self.assertEqual(wf.resolve_quiz_filter(8).topics, ["01", "03"])
        self.assertIn("A1", UI.filter_summary(7))
        self.assertIn("A2", UI.filter_summary(7))

    def test_B03_only_populated_level_navigation_never_migrates_or_selects(self):
        with storage._connect(self.db) as conn:
            conn.execute("UPDATE topics SET level='' WHERE level='A1'")
        before = self.stored()
        q = self.raw_tap(UI.CB_FORK_TOPICS)
        self.assertIn(t("en", "wf_booklevel_title"), q.edits[-1][0])
        self.assertIn(UI.callback_data("l", self.state()["revision"], "A2"),
                      [b.callback_data for b in buttons(q.edits[-1][1])])
        self.assertEqual(self.stored(), before)
        self.assertEqual(self.state()["config"]["selected_levels"], ["A1"])
        self.assertEqual(wf.resolve_quiz_filter(7).topics, [])

    def test_QA02_legacy_case_and_whitespace_variants_are_read_only_and_equivalent(self):
        for raw in (" a2 ", "a2", "\tA2\n"):
            with self.subTest(level=raw):
                with storage._connect(self.db) as conn:
                    conn.execute("INSERT OR REPLACE INTO user_settings VALUES(7,?,?)",
                                 (wf.ACTIVE_BOOK_LEVEL_KEY, raw))
                    conn.execute("INSERT OR REPLACE INTO user_settings VALUES(7,?,?)",
                                 (wf.FILTER_MODE_KEY, " ToPiCs "))
                before = self.stored()
                self.assertEqual(self.state()["config"]["selected_levels"], ["A2"])
                self.assertEqual(wf.resolve_quiz_filter(7).topics, ["11", "13"])
                self.assertEqual(wf.count_words_for_filter(7), 2)
                self.assertEqual(settings._topic_filter_label(7), "A2: 11+13")
                self.assertIn("A2", UI.filter_summary(7))
                self.assertEqual(self.stored(), before)
        storage.set_user_levels(self.db, 7, ["C1"])
        for raw in (" CEFR ", "CeFr", "\tcefr\n"):
            with self.subTest(mode=raw):
                with storage._connect(self.db) as conn:
                    conn.execute("INSERT OR REPLACE INTO user_settings VALUES(7,?,?)",
                                 (wf.FILTER_MODE_KEY, raw))
                before = self.stored()
                qf = wf.resolve_quiz_filter(7)
                self.assertEqual(qf.mode, wf.MODE_CEFR)
                self.assertIsNone(qf.topics)
                self.assertEqual(qf.cefr_levels, ["C1"])
                self.assertEqual(wf.count_words_for_filter(7), 0)
                self.assertEqual(settings._topic_filter_label(7), "CEFR: C1")
                self.assertEqual(self.stored(), before)

    def test_QA02_legacy_normalization_does_not_relax_versioned_JSON(self):
        for value in (
                self.config(selected_levels=[" a2 "], topics_by_level={" a2 ": "all"}),
                self.config(selected_levels=["A2"], topics_by_level={"A2": "all"},
                            browsing_level=" a2 "),
                self.config(version=3)):
            self.save_config(value)
            before = self.stored()
            self.assertFalse(self.state()["valid"])
            self.assertEqual(wf.resolve_quiz_filter(7).topics, [])
            self.assertEqual(wf.count_words_for_filter(7), 0)
            self.assertEqual(self.stored(), before)

    def test_QA01_sole_ghost_and_invalid_recover_via_real_S2_buttons_in_three_languages(self):
        with storage._connect(self.db) as conn:
            conn.execute("UPDATE topics SET level='' WHERE level='A1'")
        for lang in ("ru", "uk", "en"):
            for variant in ("legacy", "future", "damaged", "empty"):
                with self.subTest(lang=lang, variant=variant):
                    with storage._connect(self.db) as conn:
                        conn.execute("DELETE FROM user_settings WHERE user_id=7 AND key IN (?,?,?,?,?)",
                                     (wf.BOOK_FILTER_KEY, wf.FILTER_MODE_KEY, wf.FILTER_REVISION_KEY,
                                      wf.ACTIVE_BOOK_LEVEL_KEY, TOPIC_FILTER_KEY))
                        if variant != "legacy":
                            value = {"future": '{"version":99}', "damaged": "{",
                                     "empty": json.dumps(self.config(selected_levels=[]))}[variant]
                            conn.execute("INSERT INTO user_settings VALUES(7,?,?)", (wf.BOOK_FILTER_KEY, value))
                    storage.set_user_interface_lang(self.db, 7, lang)
                    before = self.stored()
                    fork = UI.render_fork(7)[1]
                    enter = next(b for b in buttons(fork) if b.text == t(lang, "wf_btn_topics"))
                    opened = self.raw_tap(enter.callback_data)
                    text, markup = opened.edits[-1]
                    self.assertIn(t(lang, "wf_booklevel_title"), text)
                    self.assertEqual(self.stored(), before)
                    self.assertEqual(wf.resolve_quiz_filter(7).topics, [])
                    # Actual Back reaches the fork; repeated entry still reaches S2.
                    back = next(b for b in buttons(markup) if b.text == t(lang, "wf_btn_back"))
                    returned = self.raw_tap(back.callback_data)
                    self.assertEqual(returned.edits[-1][0], UI.render_fork(7)[0])
                    enter = next(b for b in buttons(returned.edits[-1][1])
                                 if b.text == t(lang, "wf_btn_topics"))
                    reopened = self.raw_tap(enter.callback_data)
                    self.assertEqual(self.stored(), before)
                    pick = next(b for b in buttons(reopened.edits[-1][1]) if b.text == "A2")
                    chosen = self.raw_tap(pick.callback_data)
                    self.assertFalse(chosen.alerts)
                    self.assertIn(t(lang, "wf_booklevel_title"), chosen.edits[-1][0])
                    self.assertEqual(self.state()["config"]["selected_levels"], ["A2"])
                    self.assertEqual(wf.resolve_quiz_filter(7).topics, ["11", "13"])
                    self.assertEqual(wf.count_words_for_filter(7), 2)
                    self.assertEqual(settings._topic_filter_label(7), "A2: 11+13")
                    selected = self.stored()
                    themes = next(b for b in buttons(chosen.edits[-1][1])
                                  if b.text == t(lang, "wf_btn_edit_topics"))
                    viewed = self.raw_tap(themes.callback_data)
                    self.assertIn("(A2)", viewed.edits[-1][0])
                    self.assertEqual(self.stored(), selected)
                    # The actual S3 Back must reach S2 even after repair made
                    # the sole available level selected (the former loop).
                    back = next(b for b in buttons(viewed.edits[-1][1]) if b.text == t(lang, "wf_btn_back"))
                    self.assertEqual(back.callback_data, UI.callback_data("n", self.state()["revision"], "books"))
                    levels = self.raw_tap(back.callback_data)
                    self.assertIn(t(lang, "wf_booklevel_title"), levels.edits[-1][0])
                    self.assertIn("A2", levels.edits[-1][0])
                    self.assertEqual(self.stored(), selected)
                    back = next(b for b in buttons(levels.edits[-1][1]) if b.text == t(lang, "wf_btn_back"))
                    forked = self.raw_tap(back.callback_data)
                    self.assertEqual(forked.edits[-1][0], UI.render_fork(7)[0])
                    self.assertEqual(self.stored(), selected)
                    # Reopen the explicit Topics control and retain the
                    # original topic-toggle/count/banner assertions.
                    themes = next(b for b in buttons(levels.edits[-1][1])
                                  if b.text == t(lang, "wf_btn_edit_topics"))
                    viewed = self.raw_tap(themes.callback_data)
                    topic = next(b for b in buttons(viewed.edits[-1][1]) if b.text == "13")
                    changed = self.raw_tap(topic.callback_data)
                    self.assertFalse(changed.alerts)
                    self.assertEqual(wf.resolve_quiz_filter(7).topics, ["11"])
                    self.assertEqual(wf.count_words_for_filter(7), 1)
                    self.assertEqual(settings._topic_filter_label(7), "A2: 11")

    def test_QA01_sole_selected_level_shortcut_is_pure(self):
        with storage._connect(self.db) as conn:
            conn.execute("UPDATE topics SET level='' WHERE level='A1'")
            conn.execute("INSERT OR REPLACE INTO user_settings VALUES(7,?,?)",
                         (wf.ACTIVE_BOOK_LEVEL_KEY, " a2 "))
        for lang in ("ru", "uk", "en"):
            storage.set_user_interface_lang(self.db, 7, lang)
            before = self.stored()
            fork = UI.render_fork(7)[1]
            enter = next(b for b in buttons(fork) if b.text == t(lang, "wf_btn_topics"))
            self.assertEqual(enter.callback_data, UI.callback_data("n", self.state()["revision"], "book_entry"))
            viewed = self.raw_tap(enter.callback_data)
            self.assertIn("(A2)", viewed.edits[-1][0])
            self.assertEqual(self.stored(), before)
            self.assertEqual(wf.resolve_quiz_filter(7).topics, ["11", "13"])
            back = next(b for b in buttons(viewed.edits[-1][1]) if b.text == t(lang, "wf_btn_back"))
            levels = self.raw_tap(back.callback_data)
            self.assertIn(t(lang, "wf_booklevel_title"), levels.edits[-1][0])
            self.assertEqual(self.stored(), before)
            back = next(b for b in buttons(levels.edits[-1][1]) if b.text == t(lang, "wf_btn_back"))
            returned = self.raw_tap(back.callback_data)
            self.assertEqual(returned.edits[-1][0], UI.render_fork(7)[0])
            self.assertEqual(self.stored(), before)

    def test_QA01_inactive_valid_sole_level_shortcut_keeps_fresh_first_topic_tap(self):
        with storage._connect(self.db) as conn:
            conn.execute("UPDATE topics SET level='' WHERE level='A1'")
        storage.set_user_levels(self.db, 7, ["A1"])
        for lang in ("ru", "uk", "en"):
            storage.set_user_interface_lang(self.db, 7, lang)
            with storage._connect(self.db) as conn:
                conn.execute("INSERT OR REPLACE INTO user_settings VALUES(7,?,?)",
                             (wf.FILTER_MODE_KEY, " CEFR "))
            before = self.stored()
            fork = UI.render_fork(7)[1]
            enter = next(b for b in buttons(fork) if b.text == t(lang, "wf_btn_topics"))
            viewed = self.raw_tap(enter.callback_data)
            self.assertIn("(A2)", viewed.edits[-1][0])
            self.assertEqual(self.stored(), before)
            back = next(b for b in buttons(viewed.edits[-1][1]) if b.text == t(lang, "wf_btn_back"))
            levels = self.raw_tap(back.callback_data)
            self.assertIn(t(lang, "wf_booklevel_title"), levels.edits[-1][0])
            self.assertEqual(self.stored(), before)
            back = next(b for b in buttons(levels.edits[-1][1]) if b.text == t(lang, "wf_btn_back"))
            returned = self.raw_tap(back.callback_data)
            self.assertEqual(returned.edits[-1][0], UI.render_fork(7)[0])
            self.assertEqual(self.stored(), before)
            enter = next(b for b in buttons(returned.edits[-1][1])
                         if b.text == t(lang, "wf_btn_topics"))
            viewed = self.raw_tap(enter.callback_data)
            self.assertIn("(A2)", viewed.edits[-1][0])
            topic = next(b for b in buttons(viewed.edits[-1][1]) if b.text == "13")
            changed = self.raw_tap(topic.callback_data)
            self.assertFalse(changed.alerts)
            self.assertEqual(wf.resolve_quiz_filter(7).topics, ["13"])
            self.assertEqual(wf.count_words_for_filter(7), 1)
            self.assertEqual(storage.get_user_levels(self.db, 7), ["A1"])

    def test_B04_union_uses_topics_level_not_word_cefr_and_matches_count_banner(self):
        self.command("level", "A2")
        self.command("topic", "A1", wf.topic_token("03"))
        self.command("topic", "A2", wf.topic_token("13"))
        qf = wf.resolve_quiz_filter(7)
        picked = storage.learning_pick_new_words(self.db, 7, 99, topics=qf.topics, cefr_levels=qf.cefr_levels)
        self.assertEqual({row["topic"] for row in picked}, {"01", "11"})
        self.assertEqual(wf.count_words_for_filter(7), len(picked))
        label = settings._topic_filter_label(7)
        self.assertEqual(label, "A1: 01; A2: 11")
        self.assertIn("01", settings._topic_filter_banner(7))
        self.assertIn("A1", UI.filter_summary(7))
        self.assertIsNone(qf.cefr_levels)

    def test_B04_first_inactive_branch_tap_selects_and_preserves_other_branch(self):
        self.command("level", "A2")
        self.command("topic", "A1", wf.topic_token("03"))
        topics_before = copy.deepcopy(self.state()["config"])
        self.command("cefr", "A1")
        self.assertEqual(wf.resolve_quiz_filter(7).cefr_levels, ["A1"])
        self.assertEqual(self.state()["config"], topics_before)
        self.command("level", "A2")
        self.assertEqual(self.state()["config"]["selected_levels"], ["A2"])
        self.assertEqual(self.state()["config"]["topics_by_level"]["A1"], ["01"])
        self.assertEqual(storage.get_user_levels(self.db, 7), ["A1"])
        self.command("cefr", "A2")
        self.assertEqual(wf.resolve_quiz_filter(7).cefr_levels, ["A2"])
        self.assertEqual(settings._topic_filter_label(7), "CEFR: A2")

    def test_B04_first_topic_tap_inactive_selects_only_tapped_topic(self):
        self.command("level", "A2")
        self.command("cefr", "A1")
        self.command("topic", "A2", wf.topic_token("11"))
        self.assertEqual(wf.resolve_quiz_filter(7).topics, ["11"])
        self.assertEqual(self.state()["config"]["selected_levels"], ["A2"])
        self.assertEqual(storage.get_user_levels(self.db, 7), ["A1"])

    def test_B05_explicit_empty_all_storage_boundaries(self):
        self.assertEqual(storage.count_words_for_topics(self.db, []), 0)
        self.assertEqual(storage.count_words_for_topics(self.db, None), 4)
        for kwargs in ({"topics": []}, {"cefr_levels": []}):
            self.assertEqual(storage.learning_pick_new_words(self.db, 7, 99, **kwargs), [])
            self.assertEqual(storage.list_learning_queue_words(self.db, 7, **kwargs), [])
            self.assertEqual(storage.select_due(self.db, 7, "ru_de", **kwargs), [])
        self.assertEqual(storage._cefr_filter_sql(["invalid"]), ("0", []))
        self.assertEqual(storage._topic_filter_sql([""]), ("0", []))

    def test_B05_deleted_topic_registry_and_zero_levels_are_empty_everywhere(self):
        self.save_config(self.config(topics_by_level={"A1": ["01"]}))
        with storage._connect(self.db) as conn:
            conn.execute("DELETE FROM topics WHERE name='01'")
        self.assertEqual(wf.resolve_quiz_filter(7).topics, [])
        self.assertEqual(wf.count_words_for_filter(7), 0)
        self.assertEqual(settings._topic_filter_label(7), "A1: ∅")
        self.assertIn("∅", UI.filter_summary(7))
        self.assertIn(t("en", "wf_refresh"), UI.render_booklevels(7)[0])
        with storage._connect(self.db) as conn:
            conn.execute("UPDATE topics SET level=''")
        self.assertEqual(self.state()["available_levels"], [])
        self.assertEqual(wf.resolve_quiz_filter(7).topics, [])
        self.assertIn(t("en", "wf_booklevel_empty"), UI.render_booklevels(7)[0])

    def test_B05_explicit_ghost_removal_preserves_topics_and_never_reenables_missing_level(self):
        self.command("level", "A2")
        self.command("topic", "A1", wf.topic_token("03"))
        with storage._connect(self.db) as conn:
            conn.execute("UPDATE topics SET level='' WHERE level='A1'")
        before = self.stored()
        _, markup = UI.render_booklevels(7)
        self.assertIn("A1 ∅", [b.text for b in buttons(markup)])
        self.assertEqual(self.stored(), before)
        q = self.tap("l", "A1")
        self.assertFalse(q.alerts)
        self.assertEqual(self.state()["config"]["selected_levels"], ["A2"])
        self.assertEqual(self.state()["config"]["topics_by_level"]["A1"], ["01"])
        self.assertNotIn(t("en", "wf_refresh"), q.edits[-1][0])
        before = self.stored()
        with self.assertRaisesRegex(wf.FilterCommandError, "invalid"):
            self.command("level", "A1")
        self.assertEqual(self.stored(), before)
        with self.assertRaisesRegex(wf.FilterCommandError, "need_one"):
            self.command("level", "A2")
        self.assertEqual(self.stored(), before)

    def test_B06_long_unicode_topic_tokens_are_restart_stable_and_collision_checked(self):
        name = "Тема🌍" * 50 + ":all"
        storage.add_word(self.db, {"german": "Lang", "type": "nomen", "article": "das", "topic": name})
        storage.set_topic_level(self.db, name, "A1")
        state = self.state()
        data = UI.callback_data("t", state["revision"], "A1", wf.topic_token(name))
        for lang in ("ru", "uk", "en"):
            storage.set_user_interface_lang(self.db, 7, lang)
            text, markup = UI.render_topics(7)
            self.assertIn(name, text)
            self.assertIn(t(lang, "wf_btn_all_selected"), [b.text for b in buttons(markup)])
            self.assertTrue(all(b.callback_data.startswith("wf:v2:") and len(b.callback_data.encode()) <= 64 for b in buttons(markup)))
        with storage._connect(self.db) as conn:
            self.assertEqual(wf.topic_tokens(wf.read_filter_state(7, conn)["by_level"]["A1"])[wf.topic_token(name)], name)
        self.assertFalse(self.raw_tap(data).alerts)
        with patch.object(wf, "topic_token", return_value="collision"):
            with self.assertRaisesRegex(wf.FilterCommandError, "collision"):
                wf.topic_tokens(["one", "two"])

    def test_B06_S2_stays_open_and_S3_navigates_without_toggling(self):
        q = self.tap("l", "A2")
        self.assertIn(t("en", "wf_booklevel_title"), q.edits[-1][0])
        self.assertEqual(self.state()["config"]["selected_levels"], ["A1", "A2"])
        before = self.stored()
        q = self.raw_tap(UI.callback_data("n", self.state()["revision"], "topics", "A1"))
        self.assertIn("(A1)", q.edits[-1][0])
        self.assertIn("A2", q.edits[-1][0])
        self.assertEqual(self.stored(), before)
        q = self.raw_tap(UI.callback_data("n", self.state()["revision"], "topics", "A2"))
        self.assertIn("(A2)", q.edits[-1][0])
        self.assertEqual(self.stored(), before)

    def test_B06_unauthorized_actor_cannot_mutate_or_read_screen(self):
        storage.set_user_status(self.db, 8, "pending")
        before = self.stored(8)
        with self.assertRaises(PermissionError):
            self.command("level", "A2", uid=8)
        q = self.raw_tap(UI.callback_data("l", self.state(8)["revision"], "A2"), 8)
        self.assertTrue(q.alerts)
        self.assertEqual(q.edits, [])
        self.assertEqual(self.stored(8), before)

    def test_B06_filter_changes_preserve_active_learning_group(self):
        batch = storage.learning_get_or_create_batch(self.db, 7, 3)
        entries = storage.learning_pick_new_words(self.db, 7, 2, topics=["01", "03"])
        storage.learning_add_words(self.db, 7, entries, batch, 0)
        with storage._connect(self.db) as conn:
            before = {table: [tuple(row) for row in conn.execute("SELECT * FROM " + table)]
                      for table in ("learning_items", "learning_batches")}
        self.command("level", "A2")
        self.command("level", "A1")
        with storage._connect(self.db) as conn:
            after = {table: [tuple(row) for row in conn.execute("SELECT * FROM " + table)]
                     for table in ("learning_items", "learning_batches")}
        self.assertEqual(after, before)
        self.assertEqual(storage.learning_active_count(self.db, 7), 2)

    def test_B08_read_snapshot_remains_consistent_when_another_writer_commits(self):
        with storage._connect(self.db) as conn:
            conn.execute("BEGIN")
            initial = wf.read_filter_state(7, conn)
            self.command("level", "A2")
            self.assertEqual(wf.read_filter_state(7, conn), initial)
            self.assertEqual(wf.resolve_quiz_filter(7, conn).topics, ["01", "03"])
            self.assertEqual(wf.count_words_for_filter(7, conn), 2)
            self.assertEqual(wf.filter_label_from_state(wf.read_filter_state(7, conn)), "A1: 01+03")
        self.assertEqual(wf.resolve_quiz_filter(7).topics, ["01", "03", "11", "13"])

    def test_B08_failure_between_mode_and_branch_rolls_back_even_if_caller_catches(self):
        self.command("cefr", "A1")
        revision = self.state()["revision"]
        with storage._connect(self.db) as conn:
            conn.execute("CREATE TRIGGER fail_book BEFORE INSERT ON user_settings WHEN NEW.key='book_topic_filter_v2' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        before = self.stored()
        with storage._connect(self.db) as conn:
            conn.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic"):
                wf.apply_filter_command(7, revision, "level", "A2", conn=conn)
            self.assertEqual(wf.read_filter_state(7, conn)["mode"], "cefr")
        self.assertEqual(self.stored(), before)

    def test_B08_cefr_failure_does_not_activate_branch_or_drop_old_levels(self):
        with storage._connect(self.db) as conn:
            conn.execute("CREATE TRIGGER fail_cefr BEFORE INSERT ON user_levels WHEN NEW.user_id=7 BEGIN SELECT RAISE(ABORT,'synthetic CEFR failure'); END")
        before, old = self.stored(), storage.get_user_levels(self.db, 7)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic"):
            self.command("cefr", "A2")
        self.assertEqual(self.stored(), before)
        self.assertEqual(storage.get_user_levels(self.db, 7), old)

    def test_B08_concurrent_cross_branch_commands_have_one_winner(self):
        revision = self.state()["revision"]
        barrier = threading.Barrier(2)
        outcomes = []
        def apply(operation, level):
            barrier.wait()
            try:
                wf.apply_filter_command(7, revision, operation, level)
                outcomes.append("applied")
            except wf.FilterCommandError as exc:
                outcomes.append(str(exc))
        threads = [threading.Thread(target=apply, args=("level", "A2")),
                   threading.Thread(target=apply, args=("cefr", "A2"))]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertCountEqual(outcomes, ["applied", "stale"])
        state = self.state()
        self.assertEqual(state["revision"], revision + 1)
        if state["mode"] == "cefr":
            self.assertTrue(state["legacy"])
            self.assertEqual(state["cefr_levels"], ["A2"])
        else:
            self.assertEqual(state["config"]["selected_levels"], ["A1", "A2"])
            self.assertEqual(state["cefr_levels"], list(CEFR_LEVELS))

    def test_B08_same_connection_snapshot_does_not_open_secondary_readers(self):
        with storage._connect(self.db) as conn:
            conn.execute("BEGIN")
            state = wf.read_filter_state(7, conn)
            with patch.object(wf, "_connect", side_effect=AssertionError("nested connection")):
                qf = wf.resolve_quiz_filter(7, conn=conn)
                count = wf.count_words_for_filter(7, conn=conn)
            self.assertEqual(qf, wf.filter_from_state(state))
            self.assertEqual(count, 2)
            self.assertEqual(settings._topic_filter_label(7, conn=conn), "A1: 01+03")
            self.assertEqual(wf.filter_label_from_state(state), "A1: 01+03")
            self.assertTrue(conn.in_transaction)


class OriginalPoolTests(unittest.TestCase):
    def harness(self):
        h = oral_fixture.OralTests()
        h.setUp()
        self.addCleanup(h.doCleanups)
        h.add("a", "b", "c", "d", "e", "f")
        h.conn.execute("INSERT INTO topics(name,level) VALUES('2','A2')")
        h.conn.execute("UPDATE words SET topic='2' WHERE id IN ('e','f')")
        h.conn.commit()
        return h

    def command(self, h, operation, level=None):
        with h.transaction("synthetic") as conn:
            state = wf.read_filter_state(7, conn)
            return wf.apply_filter_command(7, state["revision"], operation, level, conn=conn)

    def test_B07_oral_rebuilds_unshown_pool_preserving_published_AB_actions_and_budget(self):
        h = self.harness()
        h.start()
        h.call("oral_next")
        a, b = copy.deepcopy(h.frame()["pending"]), copy.deepcopy(h.frame()["hidden"])
        self.assertEqual((a["word_id"], b["word_id"]), ("a", "b"))
        self.assertTrue(h.frame()["original_pool"])
        before = copy.deepcopy(h.frame())
        self.command(h, "level", "A2")
        self.command(h, "level", "A1")
        self.assertEqual(h.frame(), before)
        h.grade(False)
        self.assertEqual(h.frame()["pending"], b)
        self.assertEqual(h.frame()["hidden"]["word_id"], "e")
        self.assertEqual(h.frame()["retry_counts"], {"a": 0})
        self.assertEqual(h.frame()["wrong"], 1)
        self.assertEqual(h.frame()["correct"], 0)
        shown_e = copy.deepcopy(h.frame()["hidden"])
        # Registry edits also change the effective fingerprint without a callback.
        h.conn.execute("UPDATE topics SET level='' WHERE name='2'")
        h.conn.commit()
        with patch.object(oral_repo, "due", side_effect=AssertionError("empty must not run SQL")):
            h.grade(True)
        self.assertEqual(h.frame()["pending"], shown_e)
        self.assertEqual(h.frame()["original_pool"], [])
        self.assertEqual(h.frame()["retry_counts"], {"a": 0})
        self.assertNotEqual(h.frame()["original_filter_fingerprint"], before["original_filter_fingerprint"])
        self.assertNotIn("c", h.frame()["original_reserved_ids"])
        self.assertNotIn("d", h.frame()["original_reserved_ids"])

    def test_B07_oral_pool_uses_current_filter_not_stale_options(self):
        h = self.harness()
        h.options["topics"] = ["not-the-current-filter"]
        h.start()
        self.assertEqual(h.frame()["hidden"]["word_id"], "a")
        self.command(h, "level", "A2")
        self.command(h, "level", "A1")
        h.call("oral_next")
        self.assertEqual(h.frame()["pending"]["word_id"], "a")
        self.assertEqual(h.frame()["hidden"]["word_id"], "e")

    def test_B07_fsrs_rebuilds_only_unshown_pool_and_excludes_reserved_accepted(self):
        h = self.harness()
        frame = {"mode": "ru_de", "question_id": "published", "action_key": "frozen",
                 "revision": 12, "next_view": {"entry": {"id": "reserved"}}}
        ctx = SimpleNamespace(user_data={"session_seen_ru_de": {"accepted"},
            "quiz_written_frame": frame, "retry_counts": {"ru_de": {"a": 1}},
            "retry_queue": {"ru_de": ["a"]}})
        calls = []
        def fetch(*args, **kwargs):
            calls.append(kwargs)
            return [dict(row) for row in h.conn.execute("SELECT * FROM words WHERE topic IN (" +
                ",".join("?" for _ in kwargs["topics"]) + ") ORDER BY id", kwargs["topics"])]
        with patch.object(session, "resolve_quiz_filter", side_effect=lambda uid: wf.resolve_quiz_filter(uid, conn=h.conn)), \
             patch.object(session, "select_due", side_effect=fetch), \
             patch.object(session, "_mode_l1_de", return_value="ru_de"), \
             patch.object(session, "_mode_de_l1", return_value="de_ru"), \
             patch.object(session, "_mode_pl_sg", return_value="pl_sg"), \
             patch.object(session.random, "shuffle", side_effect=lambda values: None):
            self.assertEqual(session._pick_due_entry(ctx, 7, "ru_de")["id"], "a")
            self.assertTrue(ctx.user_data["fsrs_cycle_queue"]["ru_de"])
            old_frame = copy.deepcopy(frame)
            self.command(h, "level", "A2")
            self.command(h, "level", "A1")
            self.assertEqual(session._pick_due_entry(ctx, 7, "ru_de")["id"], "e")
            self.assertEqual(frame, old_frame)
            self.assertEqual(ctx.user_data["retry_counts"], {"ru_de": {"a": 1}})
            self.command(h, "level", "A1")
            self.command(h, "level", "A2")
            self.assertEqual(session._pick_due_entry(ctx, 7, "ru_de")["id"], "b")
            self.assertEqual(ctx.user_data["fsrs_cycle_reserved"]["ru_de"], {"a", "b", "e"})
            h.conn.execute("UPDATE topics SET level='' WHERE name='1'")
            h.conn.commit()
            count = len(calls)
            self.assertIsNone(session._pick_due_entry(ctx, 7, "ru_de"))
            self.assertEqual(len(calls), count)
            self.assertEqual(ctx.user_data["fsrs_cycle_queue"]["ru_de"], [])
            self.assertEqual(frame, old_frame)

    def test_B07_oral_json_roundtrip_and_cursor_only_preserve_unshown_pool(self):
        h = self.harness()
        self.command(h, "level", "A2")
        self.command(h, "level", "A2")
        h.start()
        frame = json.loads(json.dumps(h.frame()))
        fingerprint = frame["original_filter_fingerprint"]
        old_pool = frame["original_pool"][:]
        with h.transaction("synthetic") as conn:
            state = wf.read_filter_state(7, conn)
            config = state["config"]
            config["browsing_level"] = "A2"
            conn.execute("UPDATE user_settings SET value=? WHERE user_id=7 AND key=?",
                         (json.dumps(config), wf.BOOK_FILTER_KEY))
        with patch.object(oral_repo, "due", side_effect=AssertionError("unchanged fingerprint must retain pool")):
            with h.transaction("synthetic") as conn:
                entry = oral_repo.pick_original(conn, 7, "de_ru", h.options,
                    oral_fixture.DAY, {frame["hidden"]["word_id"]}, frame)
        self.assertEqual(entry["id"], old_pool[0])
        self.assertEqual(frame["original_pool"], old_pool[1:])
        self.assertEqual(frame["original_filter_fingerprint"], fingerprint)

    def test_B05_oral_due_respects_empty_topics_and_cefr(self):
        h = self.harness()
        for options in ({"topics": [], "cefr_levels": None}, {"topics": None, "cefr_levels": []}):
            self.assertEqual(oral_repo.due(h.conn, 7, "de_ru", options, oral_fixture.DAY), [])


if __name__ == "__main__":
    unittest.main()
