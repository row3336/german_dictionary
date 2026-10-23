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
    if wtype == "verb":
        e["reflexive_case"] = "none"
        e["infinitive"] = german
    if wtype == "nomen":
        e["article"] = "das"
    add_word(db, e)


class _Query:
    def __init__(self, data, uid):
        # These pre-v2 screen tests retain their operation vocabulary; translate
        # it into a CURRENT rendered command. Raw legacy rejection is covered by K16.
        state = wf.read_filter_state(uid)
        rev = state["revision"]
        if data.startswith(UI.CB_CEFR):
            data = UI.callback_data("c", rev, data[len(UI.CB_CEFR):])
        elif data.startswith(UI.CB_BLVL):
            data = UI.callback_data("l", rev, data[len(UI.CB_BLVL):])
        elif data.startswith(UI.CB_TOPIC):
            name = data[len(UI.CB_TOPIC):]
            data = (UI.callback_data("a", rev) if name == "all" else
                    UI.callback_data("t", rev, state["config"]["browsing_level"], wf.topic_token(name)))
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
    """Semantic button identities, independent of revision/token transport."""
    state = wf.read_filter_state(U)
    names = {wf.topic_token(r["name"]): r["name"] for r in state["registry"]}
    result = []
    for row in markup.inline_keyboard:
        for button in row:
            value = button.callback_data
            parts = value.split(":")
            if value.startswith(UI.V2):
                op = parts[2]
                if op == "c": value = UI.CB_CEFR + parts[4]
                elif op == "l": value = UI.CB_BLVL + parts[4]
                elif op == "t": value = UI.CB_TOPIC + names[parts[5]]
                elif op == "a": value = UI.CB_TOPIC + "all"
                elif op == "n":
                    value = {"cefr": UI.CB_FORK_CEFR, "book_entry": UI.CB_FORK_TOPICS,
                             "books": UI.CB_BACK + "blvl", "fork": UI.CB_BACK + "fork"}.get(parts[4], value)
            result.append(value)
    return result


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
        from bot.storage import set_user_status
        set_user_status(self.db, U, "approved")
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
        # bug #28: default mode is 'topics', so the first CEFR tap is a fresh pick
        # (A1 alone), not a toggle against the invisible default-all selection.
        _run(_Query(UI.CB_CEFR + "A1", U))
        self.assertEqual(wf.get_filter_mode(U), "cefr")
        self.assertEqual(get_user_levels(self.db, U), ["A1"])
        # now the branch is live, so the next tap toggles as before
        _run(_Query(UI.CB_CEFR + "A2", U))
        self.assertEqual(get_user_levels(self.db, U), ["A1", "A2"])

    # --- bug #28: the inactive branch renders empty and its first tap is a fresh pick ---

    def test_inactive_cefr_branch_renders_nothing_selected(self):
        wf.set_filter_mode(U, "topics")
        text, _ = UI.render_cefr(U)
        self.assertNotIn("✅", text)

    def test_active_cefr_branch_renders_its_selection(self):
        wf.set_filter_mode(U, "cefr")
        text, _ = UI.render_cefr(U)
        self.assertIn("✅", text)  # default selection is every level

    def test_inactive_topics_branch_renders_nothing_selected(self):
        from bot.storage import set_user_setting
        wf.set_active_book_level(U, "A1")
        set_user_setting(self.db, U, TOPIC_FILTER_KEY, "all")
        wf.set_filter_mode(U, "cefr")
        text, _ = UI.render_topics(U)
        self.assertNotIn("✅", text)

    def test_inactive_booklevel_branch_highlights_nothing(self):
        wf.set_active_book_level(U, "A1")
        wf.set_filter_mode(U, "cefr")
        _, kb = UI.render_booklevels(U)
        styles = [getattr(b, "style", None) for row in kb.inline_keyboard for b in row]
        self.assertEqual([s for s in styles if s], [])

    def test_first_topic_tap_after_cefr_selects_only_that_topic(self):
        from bot.storage import set_user_setting
        wf.set_active_book_level(U, "A1")
        set_user_setting(self.db, U, TOPIC_FILTER_KEY, "all")
        wf.set_filter_mode(U, "cefr")
        _run(_Query(UI.CB_TOPIC + "1", U))
        self.assertEqual(wf.get_filter_mode(U), "topics")
        self.assertEqual(wf.read_filter_state(U)["config"]["topics_by_level"]["A1"], ["1"])

    def test_entering_a_branch_without_tapping_changes_nothing(self):
        wf.set_filter_mode(U, "topics")
        before = get_user_levels(self.db, U)
        _run(_Query(UI.CB_FORK_CEFR, U))   # just open the CEFR screen
        _run(_Query(UI.CB_BACK + "fork", U))  # and walk back out
        self.assertEqual(wf.get_filter_mode(U), "topics")
        self.assertEqual(get_user_levels(self.db, U), before)

    def test_cefr_all_selects_everything(self):
        # first narrow to a subset, then tap 'all'
        for lv in ("A2", "B1", "B2", "C1", "C2"):
            _run(_Query(UI.CB_CEFR + lv, U))
        _run(_Query(UI.CB_CEFR + "all", U))
        self.assertEqual(len(get_user_levels(self.db, U)), 6)

    def test_booklevel_pick_adds_level_and_preserves_other_topics(self):
        from bot.storage import set_user_setting
        set_user_setting(self.db, U, TOPIC_FILTER_KEY, "1")  # stale narrow selection
        _run(_Query(UI.CB_BLVL + "B1", U))
        self.assertEqual(wf.get_filter_mode(U), "topics")
        self.assertEqual(wf.get_active_book_level(U), "B1")
        state = wf.read_filter_state(U)
        self.assertEqual(state["config"]["topics_by_level"], {"A1": ["1"], "B1": "all"})
        self.assertEqual(get_user_setting(self.db, U, TOPIC_FILTER_KEY, ""), "1")

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

    def test_book_topics_with_single_nondefault_level_requires_explicit_repair(self):
        from bot.storage import _connect, set_user_setting
        with _connect(self.db) as conn:
            conn.execute("DELETE FROM words WHERE topic = '1'")
        set_user_setting(self.db, U, TOPIC_FILTER_KEY, "1")
        q = _Query(UI.CB_FORK_TOPICS, U)
        _run(q)
        text, kb = q.edits[-1]
        self.assertEqual(wf.get_active_book_level(U), "A1")
        self.assertEqual(get_user_setting(self.db, U, TOPIC_FILTER_KEY, ""), "1")
        self.assertTrue(wf.read_filter_state(U)["legacy"])
        self.assertEqual(text, UI.render_booklevels(U)[0])
        self.assertNotIn(UI.CB_TOPIC + "2", _cbs(kb))
        self.assertIn(UI.CB_BLVL + "B1", _cbs(kb))
        # Recovery requires the explicit level tap; it remains on S2.
        pick = _Query(UI.CB_BLVL + "B1", U)
        _run(pick)
        self.assertEqual(wf.read_filter_state(U)["config"]["selected_levels"], ["B1"])
        self.assertEqual(pick.edits[-1][0], UI.render_booklevels(U)[0])
        self.assertEqual(wf.resolve_quiz_filter(U).topics, ["2"])
        self.assertEqual(get_user_setting(self.db, U, TOPIC_FILTER_KEY, ""), "1")

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
