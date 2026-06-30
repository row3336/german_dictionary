"""P6.3 — Settings reorg + language submenu routing."""

import asyncio
import os
import tempfile
import unittest

from _telegram_stub import install

install()

from bot.app_config import (  # noqa: E402
    LANGUAGE_MENU,
    SELECT_DICT_LANG,
    SELECT_UI_LANG,
    SETTINGS_MENU,
)
from bot.handlers.settings import language_menu_choice  # noqa: E402
from bot.storage import init_db  # noqa: E402
from bot.ui import keyboards as K  # noqa: E402

OWNER, GUEST = 999, 111


class _Msg:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class _Update:
    def __init__(self, text, uid):
        self.message = _Msg(text)
        self.effective_user = type("U", (), {"id": uid})()


def _flat(rows):
    return [c for row in rows for c in row]


def _run(coro):
    return asyncio.run(coro)


class LanguageMenuTest(unittest.TestCase):
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

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_settings_has_language_entry_not_raw_lang_buttons(self):
        flat = _flat(K._settings_menu_keys(GUEST))
        self.assertIn("language_sett", flat)                 # en label
        self.assertNotIn("interface language", flat)         # moved into the submenu
        self.assertNotIn("dict language", flat)

    def test_language_submenu_contents(self):
        flat = _flat(K._language_menu_keys(GUEST))
        self.assertIn("interface language", flat)
        self.assertIn("dict language", flat)
        self.assertIn("back", flat)

    def test_router_back_returns_to_settings(self):
        state = _run(language_menu_choice(_Update("back", GUEST), None))
        self.assertEqual(state, SETTINGS_MENU)

    def test_router_opens_language_pickers(self):
        self.assertEqual(_run(language_menu_choice(_Update("interface language", GUEST), None)), SELECT_UI_LANG)
        self.assertEqual(_run(language_menu_choice(_Update("dict language", GUEST), None)), SELECT_DICT_LANG)

    def test_router_unknown_redraws_submenu(self):
        self.assertEqual(_run(language_menu_choice(_Update("???", GUEST), None)), LANGUAGE_MENU)

    def test_no_periodicity_typo(self):
        # #8 regression: the RU "сброс переодичности" typo must not come back
        # anywhere in the i18n tables.
        from bot.i18n import TEXTS
        for lang, table in TEXTS.items():
            for key, value in table.items():
                self.assertNotIn("переодичн", str(value), f"{lang}/{key}")

    def test_learning_skip_label_clear_and_routes(self):
        # #12: the learning skip button is localized for ru/uk ("в FSRS"/"у FSRS")
        # and still routes; EN keeps the raw programmer-style key (owner's rule).
        from bot.common.navigation import _action_key_from_text
        from bot.i18n import TEXTS
        from bot.storage import register_or_update_user, set_user_interface_lang
        self.assertEqual(TEXTS["en"]["learning_skip"], "skip_learning")
        self.assertEqual(TEXTS["ru"]["learning_skip"], "в FSRS")
        self.assertEqual(TEXTS["uk"]["learning_skip"], "у FSRS")
        register_or_update_user(self.db, GUEST, "g", "G")
        set_user_interface_lang(self.db, GUEST, "ru")
        self.assertEqual(_action_key_from_text("в FSRS", GUEST), "learning_skip")


if __name__ == "__main__":
    unittest.main()
