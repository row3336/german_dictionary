"""spec #3 — conjugation carousel handler flow + menu button/routing.

Storage/validator/render are covered in test_conjugation_spec3.py; here we drive
the conversation loop (next/stop/empty) and the tests-menu entry point.
"""

import asyncio
import os
import sqlite3
import tempfile
import unittest

from _telegram_stub import install

install()

from bot.app_config import CONJUGATION_MENU, TESTS_MENU  # noqa: E402
from bot.common.navigation import _action_key_from_text  # noqa: E402
from bot.handlers.conjugation import conjugation_input, start_conjugation  # noqa: E402
from bot.i18n import t  # noqa: E402
from bot.storage import (  # noqa: E402
    init_db,
    register_or_update_user,
    set_user_status,
    set_user_dict_lang,
    set_user_interface_lang,
    set_word_conjugation,
)
from bot.ui.keyboards import _tests_menu_keys  # noqa: E402

OWNER, GUEST = 999, 111
GOOD = "ich gehe\ndu gehst\ner/sie/es geht\nwir gehen\nihr geht\nsie/Sie gehen"


class _Msg:
    def __init__(self, text):
        self.text = text
        self.replies = []
        self.markups = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        self.markups.append(kw.get("reply_markup"))


class _Update:
    def __init__(self, text, uid):
        self.message = _Msg(text)
        self.effective_user = type("U", (), {"id": uid})()


class _Ctx:
    def __init__(self):
        self.user_data = {}


def _run(coro):
    return asyncio.run(coro)


def _kb_texts(markup):
    rows = getattr(markup, "keyboard", None) or getattr(markup, "rows", None) or []
    out = []
    for row in rows:
        for btn in row:
            out.append(getattr(btn, "text", btn))
    return out


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = {k: os.environ.get(k) for k in ("DB_PATH", "OWNER_ID")}
        os.environ["DB_PATH"] = self.db
        os.environ["OWNER_ID"] = str(OWNER)
        self.addCleanup(self._restore)
        register_or_update_user(self.db, GUEST, "g", "G")
        set_user_status(self.db, GUEST, "approved")
        set_user_interface_lang(self.db, GUEST, "ru")
        set_user_dict_lang(self.db, GUEST, "ru")

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _add_eligible_verb(self, wid, infinitive, russian="идти", reps=2, mode="ru_de"):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO words (id, type, german, infinitive, russian, topic) VALUES (?, 'verb', ?, ?, ?, '1')",
            (wid, infinitive, infinitive, russian),
        )
        conn.execute(
            "INSERT INTO progress (user_id, word_id, mode, interval, repetitions, ease, due) "
            "VALUES (?, ?, ?, 3, ?, 2.5, '2020-01-01')",
            (GUEST, wid, mode, reps),
        )
        conn.execute("UPDATE words SET is_separable=0 WHERE id=?", (wid,))
        from bot.conjugation.schema import admit_if_qualified
        admit_if_qualified(conn, GUEST, wid, mode, reps)
        conn.commit()
        conn.close()
        set_word_conjugation(self.db, wid, GOOD)

    def _seen_count(self, wid):
        conn = sqlite3.connect(self.db)
        row = conn.execute(
            "SELECT seen_count FROM conjugation_progress WHERE user_id=? AND word_id=?", (GUEST, wid)
        ).fetchone()
        conn.close()
        return row[0] if row else None


class MenuEntryTest(_Base):
    def test_button_in_second_row_left_of_topic_filter(self):
        rows = _tests_menu_keys(GUEST)
        second = rows[1]
        self.assertEqual(second[0], t("ru", "conjugation"))
        self.assertEqual(second[1], t("ru", "topic_filter"))
        self.assertEqual(second[0], "конъюгация")

    def test_entry_routes_for_all_langs_no_raw_english(self):
        for lang, label in (("ru", "конъюгация"), ("uk", "кон'югація"), ("en", "next")):
            set_user_interface_lang(self.db, GUEST, lang)
            btn = t(lang, "conjugation")
            self.assertEqual(_action_key_from_text(btn, GUEST), "conjugation")
            if lang != "en":
                self.assertNotEqual(btn, "conjugation")  # ru/uk not raw english key


class CarouselFlowTest(_Base):
    def test_empty_preparation_keeps_access_to_tests(self):
        upd, ctx = _Update("", GUEST), _Ctx()
        state = _run(start_conjugation(upd, ctx))
        self.assertEqual(state, CONJUGATION_MENU)
        self.assertIn(t("ru", "conjugation_preparation_empty"), upd.message.replies)

    def test_start_shows_card_and_keyboard(self):
        self._add_eligible_verb("w1", "gehen")
        upd, ctx = _Update("", GUEST), _Ctx()
        state = _run(start_conjugation(upd, ctx))
        self.assertEqual(state, CONJUGATION_MENU)
        # A primer message carries the persistent carousel keyboard; the card follows.
        self.assertEqual(_kb_texts(upd.message.markups[0]), ["далее", "🎲 кубик", t("ru", "conjugation_test"), "стоп"])
        self.assertIn("*gehen* — идти", upd.message.replies[1])

    def test_next_counts_current_and_advances(self):
        self._add_eligible_verb("w1", "gehen")
        self._add_eligible_verb("w2", "sein")
        ctx = _Ctx()
        _run(start_conjugation(_Update("", GUEST), ctx))
        shown_first = ctx.user_data["conjugation_current_id"]
        upd = _Update("далее", GUEST)
        state = _run(conjugation_input(upd, ctx))
        self.assertEqual(state, CONJUGATION_MENU)
        # the preparation view counter advances; no graded SRS answer is inferred
        self.assertEqual(self._seen_count(shown_first), 1)
        self.assertEqual(ctx.user_data["conjugation_session_count"], 1)
        # a (different) card is now shown
        self.assertNotEqual(ctx.user_data["conjugation_current_id"], None)

    def test_stop_reports_session_and_returns_to_tests(self):
        self._add_eligible_verb("w1", "gehen")
        ctx = _Ctx()
        _run(start_conjugation(_Update("", GUEST), ctx))
        _run(conjugation_input(_Update("далее", GUEST), ctx))  # count 1
        upd = _Update("стоп", GUEST)
        state = _run(conjugation_input(upd, ctx))
        self.assertEqual(state, TESTS_MENU)
        self.assertIn("За сессию: 1", upd.message.replies)
        self.assertNotIn("conjugation_current_id", ctx.user_data)

    def test_stop_without_next_reports_zero(self):
        self._add_eligible_verb("w1", "gehen")
        ctx = _Ctx()
        _run(start_conjugation(_Update("", GUEST), ctx))
        upd = _Update("стоп", GUEST)
        state = _run(conjugation_input(upd, ctx))
        self.assertEqual(state, TESTS_MENU)
        self.assertIn("За сессию: 0", upd.message.replies)

    def test_unrecognized_input_stays_without_counting(self):
        self._add_eligible_verb("w1", "gehen")
        ctx = _Ctx()
        _run(start_conjugation(_Update("", GUEST), ctx))
        upd = _Update("бла бла", GUEST)
        state = _run(conjugation_input(upd, ctx))
        self.assertEqual(state, CONJUGATION_MENU)
        self.assertEqual(self._seen_count("w1"), None)  # nothing counted
        self.assertEqual(ctx.user_data["conjugation_session_count"], 0)


if __name__ == "__main__":
    unittest.main()
