"""#15 step 6 — the whole `db_search.py` module rendered its prompts and
status messages as raw English for ru/uk users (entry prompt, Choose topic/
type, No words/topics found, Not found, Added to learning, etc.). They are now
routed through `t(lang, ...)`. These tests drive the handler as a ru guest and
assert the localized text shows up (and the raw English does not).

The terse data-dump content inside ```text``` monospace blocks (`topic X: N
words`, `learning queue: N words`, `plural_match: N`, `Phrase matches:`, the
`Word card:` title) is intentionally kept English — it is a technical readout,
like the word card — so it is not asserted here.
"""

import asyncio
import os
import tempfile
import unittest

from _telegram_stub import install

install()

from bot.handlers import db_search as D  # noqa: E402
from bot.i18n import TEXTS  # noqa: E402
from bot.storage import (  # noqa: E402
    add_word,
    init_db,
    register_or_update_user,
    set_user_interface_lang,
)

OWNER, GUEST = 999, 111

_NEW_KEYS = (
    "search_prompt",
    "words_lists",
    "choose_topic",
    "choose_type",
    "no_words_in_topic",
    "no_type_words",
    "no_queued_learning",
    "added_to_learning_n",
    "no_matches_found",
    "no_plural_singular",
    "no_topics_in_db",
    "no_words_in_db",
    "not_found",
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


class DbSearchI18nTest(unittest.TestCase):
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

    def _drive(self, label, user_data=None):
        update = _Update(GUEST, label)
        _run(D.db_search_input(update, _Ctx(user_data or {})))
        return update.message.calls[-1][0]

    # ---- i18n table completeness ----

    def test_new_keys_present_and_localized(self):
        for key in _NEW_KEYS:
            for lang in ("en", "ru", "uk"):
                self.assertIn(key, TEXTS[lang], f"{key} missing in {lang}")
                self.assertTrue(TEXTS[lang][key].strip(), f"{key} empty in {lang}")
            self.assertNotEqual(TEXTS["ru"][key], TEXTS["en"][key], key)
            self.assertNotEqual(TEXTS["uk"][key], TEXTS["en"][key], key)

    def test_placeholders_intact(self):
        self.assertIn("{topic}", TEXTS["ru"]["no_words_in_topic"])
        self.assertIn("{type}", TEXTS["ru"]["no_type_words"])
        self.assertIn("{added}", TEXTS["ru"]["added_to_learning_n"])

    # ---- handler-driven (ru guest) ----

    def test_search_prompt_localized(self):
        update = _Update(GUEST)
        _run(D._send_db_search_prompt(update, GUEST, None))
        last = update.message.calls[-1][0]
        self.assertEqual(last, TEXTS["ru"]["search_prompt"])
        self.assertNotIn("Send a word", last)

    def test_search_prompt_prefix_localized(self):
        update = _Update(GUEST)
        _run(D._send_db_search_prompt(update, GUEST, TEXTS["ru"]["not_found"]))
        last = update.message.calls[-1][0]
        self.assertIn(TEXTS["ru"]["not_found"], last)
        self.assertIn(TEXTS["ru"]["search_prompt"], last)

    def test_not_found_localized(self):
        last = self._drive("zzz-nonexistent-word")
        self.assertIn(TEXTS["ru"]["not_found"], last)
        self.assertNotIn("Not found.", last)

    def test_words_lists_header_localized(self):
        last = self._drive(TEXTS["ru"]["db_search_topic_words"])
        self.assertEqual(last, TEXTS["ru"]["words_lists"])

    def test_no_topics_in_db_localized(self):
        last = self._drive(TEXTS["ru"]["db_search_topics_lists"])
        self.assertEqual(last, TEXTS["ru"]["no_topics_in_db"])
        self.assertNotIn("No topics", last)

    def test_no_words_in_db_localized(self):
        last = self._drive(TEXTS["ru"]["db_search_type_lists"])
        self.assertEqual(last, TEXTS["ru"]["no_words_in_db"])

    def test_no_matches_found_localized(self):
        last = self._drive(TEXTS["ru"]["db_search_matches"])
        self.assertIn(TEXTS["ru"]["no_matches_found"], last)

    def test_no_plural_singular_localized(self):
        last = self._drive(TEXTS["ru"]["db_search_plural_match"])
        self.assertIn(TEXTS["ru"]["no_plural_singular"], last)

    def test_no_queued_learning_localized(self):
        last = self._drive(TEXTS["ru"]["db_search_topics_lern"])
        self.assertIn(TEXTS["ru"]["no_queued_learning"], last)

    def test_choose_topic_reprompt_localized(self):
        last = self._drive("not-a-topic", {"db_search_topic_pick": True})
        self.assertEqual(last, TEXTS["ru"]["choose_topic"])
        self.assertNotIn("Choose topic", last)

    def test_choose_type_reprompt_localized(self):
        last = self._drive("not-a-type", {"db_search_type_pick": True})
        self.assertEqual(last, TEXTS["ru"]["choose_type"])

    def test_added_to_learning_n_localized(self):
        word = add_word(self.db, {
            "type": "nomen", "article": "der", "german": "Apfel",
            "plural": "Äpfel", "russian": "яблоко", "topic": "1",
        })
        last = self._drive(TEXTS["ru"]["send_learn"], {"db_search_weak_ids": [word["id"]]})
        self.assertIn("Добавлено в изучение:", last)
        self.assertNotIn("Added to learning", last)


if __name__ == "__main__":
    unittest.main()
