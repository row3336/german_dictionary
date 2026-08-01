"""P3 filter screens S0–S3 + inline drill callback (spec_topics_levels.md §4).

Renders are pure (DB in, text+markup out); the callback mutates only the DB, so we
assert persisted state (mode / levels / active book-level / topic selection) plus the
screen each render produces. Green `style` is not asserted — the stub drops it; state
is carried by the ✅ marks in the code block (deliberate old-client degradation).
"""

import asyncio
import os
import tempfile
import unittest

from _telegram_stub import install

install()

import bot.handlers.word_filter_ui as UI  # noqa: E402
from bot.common import word_filter as wf  # noqa: E402
from bot.storage import (  # noqa: E402
    get_user_levels,
    get_user_setting,
    init_db,
    register_or_update_user,
    set_topic_level,
    set_user_interface_lang,
)
from bot.app_config import TOPIC_FILTER_KEY  # noqa: E402

U = 777


def _add(db, german, wtype, russian, topic, cefr=""):
    from bot.storage import add_word
    e = {"german": german, "type": wtype, "russian": russian, "topic": topic, "cefr_level": cefr}
    if wtype == "nomen":
        e["article"] = "das"
    add_word(db, e)


class _Query:
    def __init__(self, data, uid):
        self.data = data
        self.from_user = type("U", (), {"id": uid})()
        self.edits = []
        self.alerts = []

    async def answer(self, text=None, show_alert=False, **k):
        if text:
            self.alerts.append(text)

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.edits.append((text, reply_markup))


class _Ctx:
    def __init__(self):
        self.user_data = {}


def _cbs(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _run(query):
    upd = type("Upd", (), {"callback_query": query})()
    asyncio.run(UI.word_filter_callback(upd, _Ctx()))


class WordFilterUITest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = self.db
        self.addCleanup(self._restore)
        register_or_update_user(self.db, U, "u", "U")
        set_user_interface_lang(self.db, U, "ru")
        # A1: topic 1 (Haus A1, Buch A2); B1: topic 2 (reisen B1). No C-level words.
        _add(self.db, "Haus", "nomen", "дом", "1", "A1")
        _add(self.db, "Buch", "nomen", "книга", "1", "A2")
        _add(self.db, "reisen", "verb", "ехать", "2", "B1")
        set_topic_level(self.db, "1", "A1")
        set_topic_level(self.db, "2", "B1")

    def _restore(self):
        if self._old is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = self._old

    # --- renders ---

    def test_fork_has_two_branches(self):
        text, kb = UI.render_fork(U)
        self.assertEqual(set(_cbs(kb)), {UI.CB_FORK_CEFR, UI.CB_FORK_TOPICS})

    def test_cefr_screen_dynamic_and_counts(self):
        text, kb = UI.render_cefr(U)
        cbs = _cbs(kb)
        # only levels that have words: A1, A2, B1 (not C1/C2); + all + back
        self.assertIn(UI.CB_CEFR + "A1", cbs)
        self.assertIn(UI.CB_CEFR + "B1", cbs)
        self.assertNotIn(UI.CB_CEFR + "C1", cbs)
        self.assertIn(UI.CB_CEFR + "all", cbs)
        self.assertIn(UI.CB_BACK + "fork", cbs)
        self.assertIn("A1 (words: 1)", text)

    def test_booklevels_dynamic(self):
        text, kb = UI.render_booklevels(U)
        cbs = _cbs(kb)
        self.assertIn(UI.CB_BLVL + "A1", cbs)
        self.assertIn(UI.CB_BLVL + "B1", cbs)
        self.assertNotIn(UI.CB_BLVL + "C1", cbs)
        self.assertIn(UI.CB_BACK + "fork", cbs)

    def test_topics_screen_scoped_to_active_level(self):
        wf.set_active_book_level(U, "A1")
        text, kb = UI.render_topics(U)
        cbs = _cbs(kb)
        self.assertIn(UI.CB_TOPIC + "1", cbs)      # A1 topic
        self.assertNotIn(UI.CB_TOPIC + "2", cbs)   # B1 topic hidden here
        self.assertIn(UI.CB_TOPIC + "all", cbs)
        self.assertIn(UI.CB_BACK + "blvl", cbs)

    # --- callback drill ---

    def test_cefr_toggle_sets_mode_and_levels(self):
        _run(_Query(UI.CB_CEFR + "A1", U))  # default = all six; toggling A1 removes it
        self.assertEqual(wf.get_filter_mode(U), "cefr")
        self.assertNotIn("A1", get_user_levels(self.db, U))

    def test_cefr_all_selects_everything(self):
        # first narrow to a subset, then tap 'all'
        for lv in ("A2", "B1", "B2", "C1", "C2"):
            _run(_Query(UI.CB_CEFR + lv, U))
        _run(_Query(UI.CB_CEFR + "all", U))
        self.assertEqual(len(get_user_levels(self.db, U)), 6)

    def test_booklevel_pick_sets_level_mode_and_resets_topics(self):
        from bot.storage import set_user_setting
        set_user_setting(self.db, U, TOPIC_FILTER_KEY, "1")  # stale narrow selection
        _run(_Query(UI.CB_BLVL + "B1", U))
        self.assertEqual(wf.get_filter_mode(U), "topics")
        self.assertEqual(wf.get_active_book_level(U), "B1")
        self.assertEqual(get_user_setting(self.db, U, TOPIC_FILTER_KEY, ""), "all")

    def test_book_topics_with_multiple_levels_opens_level_picker(self):
        q = _Query(UI.CB_FORK_TOPICS, U)
        _run(q)
        text, kb = q.edits[-1]
        self.assertEqual(text, UI.render_booklevels(U)[0])
        self.assertIn(UI.CB_BLVL + "A1", _cbs(kb))
        self.assertIn(UI.CB_BLVL + "B1", _cbs(kb))

    def test_migrated_unassigned_topics_show_explicit_empty_state(self):
        from bot.storage import _connect
        with _connect(self.db) as conn:
            # v11 -> v14 deliberately leaves existing topics unassigned.
            conn.execute("UPDATE topics SET level = ''")
        q = _Query(UI.CB_FORK_TOPICS, U)
        _run(q)
        text, kb = q.edits[-1]
        self.assertIn("Уровни тем по книгам пока не настроены.", text)
        self.assertNotIn("\n—\n", text)
        self.assertEqual(_cbs(kb), [UI.CB_BACK + "fork"])

    def test_book_topics_with_single_a1_skips_level_picker(self):
        from bot.storage import _connect
        with _connect(self.db) as conn:
            conn.execute("DELETE FROM words WHERE topic = '2'")
        q = _Query(UI.CB_FORK_TOPICS, U)
        _run(q)
        text, kb = q.edits[-1]
        self.assertEqual(wf.get_active_book_level(U), "A1")
        self.assertEqual(text, UI.render_topics(U)[0])
        self.assertIn(UI.CB_TOPIC + "1", _cbs(kb))
        self.assertNotIn(UI.CB_BLVL + "A1", _cbs(kb))

    def test_book_topics_with_single_nondefault_level_selects_it(self):
        from bot.storage import _connect, set_user_setting
        with _connect(self.db) as conn:
            conn.execute("DELETE FROM words WHERE topic = '1'")
        set_user_setting(self.db, U, TOPIC_FILTER_KEY, "1")
        q = _Query(UI.CB_FORK_TOPICS, U)
        _run(q)
        text, kb = q.edits[-1]
        self.assertEqual(wf.get_active_book_level(U), "B1")
        self.assertEqual(get_user_setting(self.db, U, TOPIC_FILTER_KEY, ""), "all")
        self.assertEqual(text, UI.render_topics(U)[0])
        self.assertIn(UI.CB_TOPIC + "2", _cbs(kb))
        self.assertNotIn(UI.CB_BLVL + "B1", _cbs(kb))

    def test_topic_toggle(self):
        wf.set_active_book_level(U, "A1")
        _run(_Query(UI.CB_TOPIC + "1", U))  # only topic in A1 -> can't drop last -> alert
        # A1 has a single topic; toggling the only active one is rejected (>=1 rule)
        self.assertEqual(wf.get_filter_mode(U), "topics")

    def test_back_to_fork_renders_fork(self):
        q = _Query(UI.CB_BACK + "fork", U)
        _run(q)
        self.assertEqual(q.edits[-1][0], UI.render_fork(U)[0])

    # --- summary ---

    def test_summary_topics_and_cefr(self):
        wf.set_active_book_level(U, "A1")
        s = UI.filter_summary(U)
        self.assertIn("A1", s)
        wf.set_filter_mode(U, "cefr")
        s2 = UI.filter_summary(U)
        self.assertIn("CEFR", s2)


if __name__ == "__main__":
    unittest.main()
