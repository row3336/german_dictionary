"""#15 (clarify_fields.py slice) — the field-edit screen used to hardcode English
("Choose field to edit:", "Use field buttons.", "Nothing to send."/"Added to
learning.", "❌ Edit canceled.") outside of `t(lang, ...)`, so a ru/uk guest
clarifying a quiz card saw English regardless of interface language. These guest-
reachable prompts now route through i18n. (Type/preposition prompts stay English:
those fields are owner-only per `_allowed_clarify_fields`.)
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

from bot.app_config import ADD_EDIT  # noqa: E402
from bot.handlers import clarify_fields as C  # noqa: E402
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


class ClarifyI18nTest(unittest.TestCase):
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

    def _guest_edit_ctx(self, **extra):
        # A guest editing a quiz card: source=quiz, an entry present, on the field
        # picker. Guests may only touch image/example/my_translation.
        user_data = {
            "edit_source": "quiz",
            "quiz_edit_entry": {"id": "w1", "type": "nomen", "de": "Haus"},
            "edit_stage": "pick_field",
        }
        user_data.update(extra)
        return _Ctx(user_data)

    def test_i18n_table_has_clarify_keys(self):
        for lang in ("en", "ru", "uk"):
            for key in ("clarify_choose_field", "clarify_use_field_buttons", "clarify_edit_canceled"):
                self.assertIn(key, TEXTS[lang], f"{lang}/{key}")
        # ru/uk must differ from the English source (real localization, not a copy).
        for key in ("clarify_choose_field", "clarify_use_field_buttons", "clarify_edit_canceled"):
            self.assertNotEqual(TEXTS["ru"][key], TEXTS["en"][key], key)
            self.assertNotEqual(TEXTS["uk"][key], TEXTS["en"][key], key)

    def test_invalid_field_uses_localized_prompt_for_ru_guest(self):
        # Guest types junk that is not an allowed field button -> "Use field buttons."
        update = _Update(GUEST, "zzz-not-a-field")
        state = _run(C.clarify_fields_input(update, self._guest_edit_ctx()))
        self.assertEqual(state, ADD_EDIT)
        first_text = update.message.calls[0][0]
        self.assertEqual(first_text, t("ru", "clarify_use_field_buttons"))
        self.assertNotIn("Use field buttons", first_text)

    def test_back_from_value_reprompts_localized_for_ru_guest(self):
        # Back from a value-entry returns to the field picker, localized.
        update = _Update(GUEST, t("ru", "back"))
        ctx = self._guest_edit_ctx(edit_stage="enter_value", edit_field="example")
        state = _run(C.clarify_fields_input(update, ctx))
        self.assertEqual(state, ADD_EDIT)
        first_text = update.message.calls[0][0]
        self.assertEqual(first_text, t("ru", "clarify_choose_field"))
        self.assertNotIn("Choose field to edit", first_text)


if __name__ == "__main__":
    unittest.main()
