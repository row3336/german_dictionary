"""TOPIC_FILTER_EDIT text handler — surviving behavior after the inline word-filter
screen replaced the old topic-filter screen (spec_topics_levels.md).

The quiz filter is now chosen via inline `wf:` buttons, so `topic_filter_edit` only
handles the reply-`back` return to the calling menu and the localized quiz banner.
telegram is stubbed (PTB may be absent).
"""

import asyncio
import os
import tempfile
import unittest

from _telegram_stub import install

install()

from bot.app_config import (  # noqa: E402
    ALGO_SETTINGS_MENU,
    QUIZ_RU_DE_MENU,
    SETTINGS_MENU,
    TESTS_MENU,
)
from bot.common.settings import _topic_filter_banner, _topic_filter_value  # noqa: E402
from bot.handlers.settings import topic_filter_edit  # noqa: E402
from bot.storage import (  # noqa: E402
    init_db,
    insert_topic_if_absent,
    register_or_update_user,
    set_user_interface_lang,
    set_user_setting,
)

U1, U2 = 101, 202


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeReplyMsg:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append(text)


class _FakeMsgUpdate:
    def __init__(self, text, uid):
        self.message = _FakeReplyMsg(text)
        self.effective_user = _FakeUser(uid)


class _FakeCtx:
    def __init__(self, user_data=None):
        self.user_data = user_data or {}


def _edit(text, uid, return_to):
    upd = _FakeMsgUpdate(text, uid)
    ctx = _FakeCtx({"topic_return": return_to})
    state = asyncio.run(topic_filter_edit(upd, ctx))
    return upd, state


class TopicFilterBackTest(unittest.TestCase):
    """reply-`back` returns to the calling menu and never changes the stored filter."""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        for name in ("01", "03", "7"):
            insert_topic_if_absent(self.db, name, "")
        register_or_update_user(self.db, U1)
        set_user_interface_lang(self.db, U1, "ru")
        self._old = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = self.db
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = self._old

    def test_back_returns_to_each_menu(self):
        set_user_setting(self.db, U1, "topic_filter", "01")
        for return_to, expected in (
            ("tests_menu", TESTS_MENU),
            ("ru_de_menu", QUIZ_RU_DE_MENU),
            ("algo", ALGO_SETTINGS_MENU),
            ("settings", SETTINGS_MENU),
        ):
            _upd, state = _edit("back", U1, return_to)
            self.assertEqual(state, expected, f"back from {return_to}")
            # back must not change the stored filter
            self.assertEqual(_topic_filter_value(U1), "01")

    def test_topic_filter_banner_localized(self):
        # #15: the quiz banner heading "Topic filter:" must be localized for ru/uk
        from bot.i18n import t

        for uid, lang in ((U1, "ru"), (U2, "uk"), (303, "en")):
            register_or_update_user(self.db, uid, f"u{uid}", "U")
            set_user_interface_lang(self.db, uid, lang)
            banner = _topic_filter_banner(uid)
            self.assertTrue(banner.startswith(t(lang, "topic_filter_banner")), f"{lang}: {banner!r}")
        self.assertNotIn("Topic filter:", _topic_filter_banner(U1))  # no raw EN for ru
        self.assertNotIn("Topic filter:", _topic_filter_banner(U2))  # no raw EN for uk


if __name__ == "__main__":
    unittest.main()
