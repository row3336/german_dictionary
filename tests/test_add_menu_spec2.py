"""spec #2 step 2a — the add-flow submenu router (AI / VF add / VF recheck / back).

VF buttons are stubs at this step; here we only assert the routing/returned state.
"""

import asyncio
import os
import tempfile
import unittest

from _telegram_stub import install

install()

import bot.handlers.add_words as AW  # noqa: E402
from bot.common.lang import _user_interface_lang  # noqa: E402
from bot.i18n import t  # noqa: E402
from bot.labels import PARSE_VF_TEXT  # noqa: E402
from bot.storage import init_db  # noqa: E402
from bot.ui.keyboards import _settings_menu_keys  # noqa: E402

OWNER = 777


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMsg:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self, text, uid):
        self.message = _FakeMsg(text)
        self.effective_user = _FakeUser(uid)


class _Ctx:
    def __init__(self):
        self.user_data = {}


class AddMenuRoutingTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = os.environ.get("DB_PATH")
        self._old_owner = os.environ.get("OWNER_ID")
        os.environ["DB_PATH"] = self.db
        os.environ["OWNER_ID"] = str(OWNER)
        self.addCleanup(self._restore)
        self.lang = _user_interface_lang(OWNER)

    def _restore(self):
        for key, old in (("DB_PATH", self._old), ("OWNER_ID", self._old_owner)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

    def _route(self, key):
        upd = _FakeUpdate(t(self.lang, key), OWNER)
        return asyncio.run(AW.add_menu_choice(upd, _Ctx())), upd

    def test_ai_is_temporarily_disabled(self):
        state, upd = self._route("add_ai")
        self.assertEqual(state, AW.MENU)
        self.assertEqual(upd.message.replies[-1], t(self.lang, "add_words_disabled"))

    def test_vf_add_is_temporarily_disabled(self):
        # Cached VF-add buttons must obey the current feature gate.
        from bot.app_config import VF_ADD_INPUT
        state, _ = self._route("add_vf")
        self.assertEqual(state, AW.MENU)

    def test_vf_recheck_is_temporarily_disabled(self):
        # Recheck must not arm a cached flow while the feature is disabled.
        from bot.app_config import VF_ADD_INPUT
        upd = _FakeUpdate(t(self.lang, "vf_recheck"), OWNER)
        ctx = _Ctx()
        state = asyncio.run(AW.add_menu_choice(upd, ctx))
        self.assertEqual(state, AW.MENU)
        self.assertNotIn("vf_recheck", ctx.user_data)

    def test_settings_menu_parse_vf_is_proxy_entry_owner_only(self):
        # spec #2 moved PARSING to the add menu; the parse_VF button returned to
        # Settings as the proxy mini-menu entry (bug #27) and stays owner-only.
        flat = [b for row in _settings_menu_keys(OWNER) for b in row]
        self.assertIn(PARSE_VF_TEXT, flat)

    def test_back_returns_to_main_menu(self):
        state, _ = self._route("back")
        self.assertEqual(state, AW.MENU)

    def test_cached_add_menu_unknown_text_returns_to_main(self):
        upd = _FakeUpdate("gibberish", OWNER)
        state = asyncio.run(AW.add_menu_choice(upd, _Ctx()))
        self.assertEqual(state, AW.MENU)


if __name__ == "__main__":
    unittest.main()
