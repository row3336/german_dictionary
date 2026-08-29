"""#1/#2 — `clarify_fields` (in the learning session) and `input_err`
(everywhere) used to render as raw English literals for ru/uk users, because
the keyboards were built from the raw `app_config` constants instead of
`t(lang, ...)`. `input_err` additionally had NO i18n entry at all. Both are now
localized and still route correctly — `_action_key_from_text` maps the
translated label back to the action, so the button keeps working after
translation (the same wiring already used for `clarify_fields` in quiz.py).
"""

import asyncio
import os
import tempfile
import unittest

from _telegram_stub import install

install()

from bot.app_config import LEARNING_QUIZ  # noqa: E402
from bot.common.navigation import _action_key_from_text  # noqa: E402
from bot.handlers import learning as L  # noqa: E402
from bot.i18n import TEXTS  # noqa: E402
from bot.storage import (  # noqa: E402
    add_word,
    init_db,
    learning_add_words,
    learning_get_or_create_batch,
    learning_mark_preview_next,
    register_or_update_user,
    set_user_interface_lang,
)
from bot.ui import keyboards as K  # noqa: E402

OWNER, GUEST = 999, 111


def _flat(rows):
    return [c for row in rows for c in row]


class _Msg:
    def __init__(self, text=""):
        self.text = text
        self.calls = []

    async def reply_text(self, text, **kw):
        self.calls.append((text, kw))
        return type("Sent", (), {"message_id": 1})()


class _Update:
    def __init__(self, uid, text=""):
        self.message = _Msg(text)
        self.effective_user = type("U", (), {"id": uid, "username": "u", "first_name": "U"})()
        self.effective_chat = type("C", (), {"id": uid})()


class _Ctx:
    def __init__(self, user_data=None):
        self.user_data = user_data or {}


def _run(coro):
    return asyncio.run(coro)


class RawButtonI18nTest(unittest.TestCase):
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
        set_user_interface_lang(self.db, GUEST, "ru")

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _add_learning_word_past_preview(self):
        word = add_word(self.db, {
            "type": "nomen", "article": "der", "german": "Apfel",
            "plural": "Äpfel", "russian": "яблоко", "topic": "1",
        })
        batch_id = learning_get_or_create_batch(self.db, user_id=GUEST, daily_qty=20)
        learning_add_words(self.db, user_id=GUEST, words=[word], batch_id=batch_id, first_delay_seconds=0)
        learning_mark_preview_next(self.db, user_id=GUEST, word_id=word["id"], first_interval_seconds=0)
        return word

    def test_input_err_i18n_table(self):
        self.assertEqual(TEXTS["en"]["input_err"], "input_err")
        self.assertEqual(TEXTS["ru"]["input_err"], "я ошибся")
        self.assertEqual(TEXTS["uk"]["input_err"], "помилка")

    def test_clarify_fields_i18n_table_unchanged(self):
        self.assertEqual(TEXTS["en"]["clarify_fields"], "clarify_fields")
        self.assertEqual(TEXTS["ru"]["clarify_fields"], "уточнить поля")
        self.assertEqual(TEXTS["uk"]["clarify_fields"], "уточнити поля")

    def test_action_key_routes_translated_labels(self):
        self.assertEqual(_action_key_from_text("я ошибся", GUEST), "input_err")
        self.assertEqual(_action_key_from_text("уточнить поля", GUEST), "clarify_fields")

    def test_quiz_keyboards_localize_input_err_for_ru(self):
        for flat in (
            _flat(K._quiz_markup(GUEST).rows),
            _flat(K._quiz_idle_markup(GUEST).rows),
        ):
            self.assertIn("я ошибся", flat)
            self.assertNotIn("input_err", flat)

    def test_learning_normal_mode_keyboard_localizes_both_for_ru(self):
        self._add_learning_word_past_preview()
        update = _Update(GUEST)
        _run(L._send_learning_prompt(update, _Ctx(), None))
        _, kw = update.message.calls[-1]
        flat = _flat(kw["reply_markup"].rows)
        self.assertIn("уточнить поля", flat)
        self.assertNotIn("clarify_fields", flat)
        self.assertIn("я ошибся", flat)
        self.assertNotIn("input_err", flat)

    def test_learning_preview_keyboard_localizes_clarify_for_ru(self):
        self._add_word_in_preview = add_word(self.db, {
            "type": "nomen", "article": "der", "german": "Birne",
            "plural": "Birnen", "russian": "груша", "topic": "1",
        })
        batch_id = learning_get_or_create_batch(self.db, user_id=GUEST, daily_qty=20)
        learning_add_words(self.db, user_id=GUEST, words=[self._add_word_in_preview], batch_id=batch_id, first_delay_seconds=0)
        update = _Update(GUEST)
        _run(L._send_learning_prompt(update, _Ctx(), None))
        _, kw = update.message.calls[-1]
        flat = _flat(kw["reply_markup"].rows)
        self.assertIn("уточнить поля", flat)
        self.assertNotIn("clarify_fields", flat)

    def test_guest_input_err_button_routes_in_learning_ru(self):
        # The translated ru label must still trigger the input-err handler, not
        # fall through to answer-scoring like the raw-constant comparison did.
        word = self._add_learning_word_past_preview()
        ctx = _Ctx({"quiz_entry": word, "learning_preview_mode": False})
        update = _Update(GUEST, "я ошибся")
        state = _run(L.learning_quiz(update, ctx))
        self.assertEqual(state, LEARNING_QUIZ)
        last_text = update.message.calls[-1][0]
        # ru guest -> localized "Nothing to fix." (input_err now routed through t(lang, ...) in learning.py)
        self.assertIn(TEXTS["ru"]["input_err_nothing"], last_text)
        self.assertNotIn("Nothing to fix.", last_text)


if __name__ == "__main__":
    unittest.main()
