"""#15 step 5 — transient learning-session strings used to render as raw
English for ru/uk users (batch-full / no-new / added / no-due / reset /
menu-fallback / skip / saved / next-skip-back / input_err). They are now
routed through `t(lang, ...)`. These tests drive the learning handlers as a
ru guest and assert the localized text shows up (and the raw English does not).

The terse `metric = value` diagnostic block of the learning menu
(`active = / due_now = / next_due = ...`) is intentionally kept English (an
on-screen technical readout, like the word card) — not asserted here.
"""

import asyncio
import os
import sqlite3
import tempfile
import unittest

from _telegram_stub import install

install()

from bot.handlers import learning as L  # noqa: E402
from bot.i18n import TEXTS  # noqa: E402
from bot.storage import (  # noqa: E402
    add_word,
    init_db,
    learning_add_words,
    learning_get_or_create_batch,
    register_or_update_user,
    set_user_status,
    set_user_interface_lang,
)

OWNER, GUEST = 999, 111

_NEW_KEYS = (
    "learning_batch_full",
    "learning_no_new",
    "learning_added",
    "learning_no_due",
    "learning_session_reset",
    "learning_use_menu_buttons",
    "learning_skipped",
    "learning_saved_next",
    "learning_use_next_skip_back",
    "learning_done_sent_fsrs",
)


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


class LearningI18nTest(unittest.TestCase):
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
        # The language scenarios require an explicitly configured populated book level.
        from bot.storage import set_topic_level
        set_topic_level(self.db, "1", "A1")

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _add_word(self, german="Apfel", article="der", russian="яблоко", topic="1"):
        return add_word(self.db, {
            "type": "nomen", "article": article, "german": german,
            "plural": german + "n", "russian": russian, "topic": topic,
        })

    def _add_word_in_preview(self):
        word = self._add_word()
        batch_id = learning_get_or_create_batch(self.db, user_id=GUEST, daily_qty=20)
        learning_add_words(self.db, user_id=GUEST, words=[word], batch_id=batch_id, first_delay_seconds=0)
        return word

    # ---- i18n table completeness ----

    def test_new_keys_present_and_localized(self):
        for key in _NEW_KEYS:
            for lang in ("en", "ru", "uk"):
                self.assertIn(key, TEXTS[lang], f"{key} missing in {lang}")
                self.assertTrue(TEXTS[lang][key].strip(), f"{key} empty in {lang}")
            # ru/uk must actually differ from raw English
            self.assertNotEqual(TEXTS["ru"][key], TEXTS["en"][key], key)
            self.assertNotEqual(TEXTS["uk"][key], TEXTS["en"][key], key)

    def test_batch_full_placeholders_intact(self):
        self.assertIn("{active}", TEXTS["ru"]["learning_batch_full"])
        self.assertIn("{daily}", TEXTS["ru"]["learning_batch_full"])
        self.assertIn("{added}", TEXTS["ru"]["learning_added"])

    # ---- handler-driven (ru guest) ----

    def test_add_words_no_new_localized(self):
        # empty DB -> nothing to pick -> localized "no new words"
        update = _Update(GUEST)
        _run(L._learning_add_words(update, _Ctx()))
        last = update.message.calls[-1][0]
        self.assertEqual(last, TEXTS["ru"]["learning_no_new"])
        self.assertNotIn("No new words", last)

    def test_add_words_added_localized(self):
        self._add_word()
        update = _Update(GUEST)
        _run(L._learning_add_words(update, _Ctx()))
        last = update.message.calls[-1][0]
        self.assertIn("Добавлено для изучения:", last)
        self.assertNotIn("Added for learning", last)

    def test_menu_reset_localized(self):
        update = _Update(GUEST, TEXTS["ru"]["learning_reset"])
        _run(L.learning_menu_choice(update, _Ctx()))
        last = update.message.calls[-1][0]
        self.assertEqual(last, TEXTS["ru"]["learning_session_reset"])
        self.assertNotIn("Words returned to new", last)

    def test_menu_unknown_button_localized(self):
        update = _Update(GUEST, "zzz-not-a-button")
        _run(L.learning_menu_choice(update, _Ctx()))
        last = update.message.calls[-1][0]
        self.assertEqual(last, TEXTS["ru"]["learning_use_menu_buttons"])

    def test_no_due_sentence_localized_next_due_kept(self):
        # a word is queued but NOT due yet -> localized "no due" sentence + the
        # English "next_due =" readout (shown only when there's a real ETA).
        word = self._add_word()
        batch_id = learning_get_or_create_batch(self.db, user_id=GUEST, daily_qty=20)
        learning_add_words(self.db, user_id=GUEST, words=[word], batch_id=batch_id, first_delay_seconds=0)
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "UPDATE learning_items SET preview_done=1, first_due_pending=0, next_due='2099-01-01T00:00:00Z' WHERE user_id=? AND word_id=?",
                (GUEST, word["id"]),
            )
        update = _Update(GUEST)
        _run(L._send_learning_prompt(update, _Ctx(), None))
        last = update.message.calls[-1][0]
        self.assertIn(TEXTS["ru"]["learning_no_due"], last)
        self.assertNotIn("No due learning words", last)
        self.assertIn("next_due =", last)  # technical readout stays English
        # the plain 'next_due' tail must never be sent as Markdown (underscore crash)
        self.assertNotEqual(update.message.calls[-1][1].get("parse_mode"), "Markdown")

    def test_preview_skip_localized(self):
        word = self._add_word_in_preview()
        ctx = _Ctx({"quiz_entry": word, "learning_preview_mode": True})
        update = _Update(GUEST, TEXTS["ru"]["learning_skip"])
        _run(L.learning_quiz(update, ctx))
        # skipping the last preview word emits the prefix as its own message, so
        # scan every reply, not just the final "nothing due" one.
        texts = "\n".join(c[0] for c in update.message.calls)
        self.assertIn(TEXTS["ru"]["learning_skipped"], texts)
        self.assertNotIn("Skipped for learning", texts)

    def test_preview_next_localized(self):
        word = self._add_word_in_preview()
        ctx = _Ctx({"quiz_entry": word, "learning_preview_mode": True})
        update = _Update(GUEST, TEXTS["ru"]["learning_next"])
        _run(L.learning_quiz(update, ctx))
        last = update.message.calls[-1][0]
        self.assertIn(TEXTS["ru"]["learning_saved_next"], last)
        self.assertNotIn("Next preview word", last)

    def test_preview_unknown_button_localized(self):
        word = self._add_word_in_preview()
        ctx = _Ctx({"quiz_entry": word, "learning_preview_mode": True})
        update = _Update(GUEST, "zzz-not-a-button")
        _run(L.learning_quiz(update, ctx))
        last = update.message.calls[-1][0]
        self.assertEqual(last, TEXTS["ru"]["learning_use_next_skip_back"])


if __name__ == "__main__":
    unittest.main()
