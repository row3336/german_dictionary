"""spec #8 — inline topic-filter toggle callback (^topicf:) behavior.

telegram is stubbed (PTB may be absent); the callback only builds keyboards via the
stubbed classes and edits through a capturing fake query, so no real PTB is needed.
"""

import asyncio
import os
import tempfile
import unittest

from _telegram_stub import install

install()

import bot.handlers.topic_filter as TF  # noqa: E402
from bot.app_config import (  # noqa: E402
    ALGO_SETTINGS_MENU,
    QUIZ_RU_DE_MENU,
    SETTINGS_MENU,
    TESTS_MENU,
    TOPIC_FILTER_EDIT,
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


class _FakeMessage:
    def __init__(self):
        self.text = ""


class _FakeQuery:
    def __init__(self, data, uid):
        self.data = data
        self.from_user = _FakeUser(uid)
        self.message = _FakeMessage()
        self.answers = []          # list of (text, show_alert)
        self.edits = []            # list of edited texts

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, reply_markup=None, **kwargs):
        self.message.text = text
        self.edits.append(text)


class _FakeUpdate:
    def __init__(self, query):
        self.callback_query = query


def _tap(data, uid):
    q = _FakeQuery(data, uid)
    asyncio.run(TF.topic_filter_toggle_callback(_FakeUpdate(q), None))
    return q


class TopicFilterCallbackTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        for name in ("01", "03", "7"):
            insert_topic_if_absent(self.db, name, "")
        self._old = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = self.db
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = self._old

    def test_toggle_add_writes_filter_and_edits(self):
        set_user_setting(self.db, U1, "topic_filter", "01")
        q = _tap("topicf:t:03", U1)
        self.assertEqual(_topic_filter_value(U1), "01+03")
        self.assertEqual(len(q.edits), 1)  # same message edited in place

    def test_toggle_remove(self):
        set_user_setting(self.db, U1, "topic_filter", "01+03")
        _tap("topicf:t:03", U1)
        self.assertEqual(_topic_filter_value(U1), "01")

    def test_last_active_shows_alert_and_keeps_value(self):
        set_user_setting(self.db, U1, "topic_filter", "01")
        q = _tap("topicf:t:01", U1)
        self.assertEqual(_topic_filter_value(U1), "01")  # unchanged
        self.assertTrue(any(show for _text, show in q.answers))  # alert raised
        self.assertEqual(q.edits, [])  # nothing edited

    def test_all_button_sets_all(self):
        set_user_setting(self.db, U1, "topic_filter", "01")
        _tap("topicf:all", U1)
        self.assertEqual(_topic_filter_value(U1), "all")

    def test_all_button_noop_when_already_all(self):
        set_user_setting(self.db, U1, "topic_filter", "all")
        q = _tap("topicf:all", U1)
        self.assertEqual(_topic_filter_value(U1), "all")
        self.assertEqual(q.edits, [])  # no-op, no edit

    def test_manual_full_selection_collapses_to_all(self):
        set_user_setting(self.db, U1, "topic_filter", "01+03")
        _tap("topicf:t:7", U1)  # last missing topic -> 'all'
        self.assertEqual(_topic_filter_value(U1), "all")

    def test_per_user_isolation(self):
        set_user_setting(self.db, U1, "topic_filter", "01")
        set_user_setting(self.db, U2, "topic_filter", "03")
        _tap("topicf:t:7", U1)
        self.assertEqual(_topic_filter_value(U1), "01+7")
        self.assertEqual(_topic_filter_value(U2), "03")  # untouched


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


class TopicFilterTextFallbackTest(unittest.TestCase):
    """spec #8 block 5 — text fallback + back returns still work (and are localized)."""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        for name in ("01", "03", "7"):
            insert_topic_if_absent(self.db, name, "")
        register_or_update_user(self.db, U1)
        set_user_interface_lang(self.db, U1, "ru")  # verify ru localization
        self._old = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = self.db
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = self._old

    def test_text_writes_filter_and_localized_confirm(self):
        upd, state = _edit("01+03", U1, "tests_menu")
        self.assertEqual(_topic_filter_value(U1), "01+03")
        self.assertEqual(state, TESTS_MENU)
        # ru confirm, not raw English
        self.assertTrue(any("Фильтр тем: 01+03" in r for r in upd.message.replies))
        self.assertFalse(any("Topic filter set to" in r for r in upd.message.replies))

    def test_text_all(self):
        _edit("all", U1, "algo")
        self.assertEqual(_topic_filter_value(U1), "all")

    def test_empty_input_localized_and_stays(self):
        upd, state = _edit("   ", U1, "tests_menu")
        self.assertEqual(state, TOPIC_FILTER_EDIT)
        self.assertTrue(any("непустую тему" in r for r in upd.message.replies))

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
