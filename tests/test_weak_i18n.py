"""#15 (weak.py slice) — the verb-weak submenu had a hardcoded-English fallback
("Use verb weak mode buttons.") that was ALSO dead code: it sat after `return`
inside the weak_partizip2 branch, so invalid input in the submenu silently did
nothing. Fixed: the fallback is de-indented to the function level (so it fires on
invalid input) and localized via t(lang, "weak_use_mode_buttons").
"""

import asyncio
import os
import sqlite3
import sys
import tempfile
import types
import unittest

from _telegram_stub import install

install()
if "dotenv" not in sys.modules:
    _d = types.ModuleType("dotenv")
    _d.load_dotenv = lambda *a, **k: False
    sys.modules["dotenv"] = _d

from bot.app_config import QUIZ_VERB_WEAK_MENU  # noqa: E402
from bot.handlers import weak as W  # noqa: E402
from bot.i18n import TEXTS, t  # noqa: E402
from bot.storage import init_db, register_or_update_user, set_user_interface_lang  # noqa: E402

OWNER, GUEST = 999, 111


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


class WeakI18nTest(unittest.TestCase):
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

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_i18n_table_has_weak_use_mode_buttons(self):
        for lang in ("en", "ru", "uk"):
            self.assertIn("weak_use_mode_buttons", TEXTS[lang], lang)
        self.assertNotEqual(TEXTS["ru"]["weak_use_mode_buttons"], TEXTS["en"]["weak_use_mode_buttons"])
        self.assertNotEqual(TEXTS["uk"]["weak_use_mode_buttons"], TEXTS["en"]["weak_use_mode_buttons"])

    def _invalid_submenu_reply(self, lang):
        set_user_interface_lang(self.db, GUEST, lang)
        update = _Update(GUEST, "zzz-not-a-submenu-button")
        state = _run(W.quiz_verb_weak_menu_choice(update, _Ctx()))
        return state, update.message.calls

    def test_invalid_submenu_input_shows_localized_fallback_ru(self):
        # Regression: the fallback used to be dead code (nothing sent). Now it fires
        # AND is localized.
        state, calls = self._invalid_submenu_reply("ru")
        self.assertEqual(state, QUIZ_VERB_WEAK_MENU)
        self.assertTrue(calls, "invalid input produced no reply (dead-code regression)")
        self.assertEqual(calls[0][0], t("ru", "weak_use_mode_buttons"))
        self.assertNotIn("Use verb weak mode buttons", calls[0][0])

    def test_invalid_submenu_input_shows_localized_fallback_uk(self):
        state, calls = self._invalid_submenu_reply("uk")
        self.assertEqual(state, QUIZ_VERB_WEAK_MENU)
        self.assertEqual(calls[0][0], t("uk", "weak_use_mode_buttons"))


if __name__ == "__main__":
    unittest.main()
