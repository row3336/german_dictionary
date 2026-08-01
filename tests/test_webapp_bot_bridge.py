"""Mini App M3 step 2 — the bridge into the bot's memory (miniapp.md §4.2, §11).

Two things carry risk here. Clearing too little leaves the bot holding a stale
question, so the same word gets asked again in the chat; clearing too much throws
away an unrelated half-finished flow the user left open. Both are tested directly
against a fake Application whose user_data is an ordinary dict — which is exactly
what PTB stores.

The chat notice is tested for the condition the owner set: it goes out only when a
question was really interrupted, never as a greeting.
"""

import asyncio
import unittest

from _webapp_helpers import TempDbMixin

from bot.webapp.bot_bridge import BotBridge
from bot.webapp.i18n_strings import APP_TEXTS

USER = 111
OTHER = 222


class FakeBot:
    def __init__(self, fail=False):
        self.token = "123456:AAH-token"
        self.sent = []
        self.fail = fail

    async def send_message(self, chat_id, text):
        if self.fail:
            raise RuntimeError("bot was blocked by the user")
        self.sent.append((chat_id, text))


class FakeApplication:
    """Stand-in for telegram.ext.Application: user_data plus a bot."""

    def __init__(self, user_data=None, fail_send=False):
        self.user_data = {} if user_data is None else user_data
        self.bot = FakeBot(fail=fail_send)


def active_quiz_state():
    """The shape the bot leaves behind mid-session (§4.1)."""
    return {
        "session_mode": "ru_de",
        "quiz_entry": {"id": "w1", "german": "Tisch"},
        "session_seen_ru_de": {"w1"},
        "session_correct_ru_de": 3,
        "session_wrong_ru_de": 1,
        "retry_queue": {"ru_de": ["w1"]},
        "retry_counts": {"ru_de": {"w1": 1}},
        "retry_since_last": {"ru_de": 2},
        "weak_drill_queue": {"weak_plural": ["w2"]},
        "fsrs_cycle_queue": {"ru_de": ["w3"]},
        "quiz_correct_streak": 5,
        "last_answered_entry": {"id": "w1"},
        "quiz_is_retry": True,
    }


class DisabledBridgeTest(unittest.TestCase):
    """Standalone development: no Application, no crashes."""

    def setUp(self):
        self.bridge = BotBridge()

    def test_reports_itself_unavailable(self):
        self.assertFalse(self.bridge.available)

    def test_every_call_is_a_quiet_no_op(self):
        self.assertFalse(self.bridge.has_active_quiz(USER))
        self.assertFalse(self.bridge.clear_quiz_state(USER))
        self.assertFalse(asyncio.run(self.bridge.notify_session_moved(USER)))
        self.assertFalse(asyncio.run(self.bridge.take_over_for_app(USER)))


class ActiveQuizDetectionTest(unittest.TestCase):
    def test_sees_a_session_in_progress(self):
        app = FakeApplication({USER: active_quiz_state()})
        self.assertTrue(BotBridge(app).has_active_quiz(USER))

    def test_a_user_with_no_record_has_no_session(self):
        self.assertFalse(BotBridge(FakeApplication()).has_active_quiz(USER))

    def test_unrelated_state_is_not_a_quiz(self):
        app = FakeApplication({USER: {"pending_entry": {"german": "Tisch"}}})
        self.assertFalse(BotBridge(app).has_active_quiz(USER))

    def test_reading_does_not_create_a_record(self):
        # Indexing PTB's user_data would materialise an empty dict for every user
        # who merely opened the app.
        app = FakeApplication()
        BotBridge(app).has_active_quiz(USER)
        self.assertEqual(app.user_data, {})


class ClearQuizStateTest(unittest.TestCase):
    def setUp(self):
        self.state = active_quiz_state()
        self.app = FakeApplication({USER: self.state})
        self.bridge = BotBridge(self.app)

    def test_removes_every_quiz_key(self):
        self.assertTrue(self.bridge.clear_quiz_state(USER))
        self.assertEqual(self.state, {})

    def test_removes_mode_suffixed_counters(self):
        self.state["session_seen_de_ru"] = {"w9"}
        self.state["session_correct_pl_sg_ru"] = 2
        self.bridge.clear_quiz_state(USER)
        self.assertEqual(self.state, {})

    def test_leaves_unrelated_flows_alone(self):
        # A half-typed word being added is not this feature's business.
        self.state["pending_entry"] = {"german": "Tisch"}
        self.state["edit_field"] = "plural"
        self.bridge.clear_quiz_state(USER)
        self.assertEqual(self.state, {"pending_entry": {"german": "Tisch"}, "edit_field": "plural"})

    def test_reports_when_there_was_nothing_to_clear(self):
        self.app.user_data[OTHER] = {}
        self.assertFalse(self.bridge.clear_quiz_state(OTHER))
        self.assertFalse(self.bridge.clear_quiz_state(999))

    def test_only_touches_the_named_user(self):
        other_state = active_quiz_state()
        self.app.user_data[OTHER] = other_state
        self.bridge.clear_quiz_state(USER)
        self.assertEqual(other_state, active_quiz_state())


class NotifyTest(TempDbMixin, unittest.TestCase):
    def setUp(self):
        self.setup_temp_db()
        self.make_user(USER, "approved")

    def test_sends_the_notice_in_the_users_language(self):
        app = FakeApplication()
        self.assertTrue(asyncio.run(BotBridge(app).notify_session_moved(USER)))
        chat_id, text = app.bot.sent[0]
        self.assertEqual(chat_id, USER)
        self.assertEqual(text, APP_TEXTS["en"]["chat_session_moved"])

    def test_follows_the_interface_language(self):
        from bot.storage import set_user_interface_lang

        set_user_interface_lang(self.db, USER, "uk")
        app = FakeApplication()
        asyncio.run(BotBridge(app).notify_session_moved(USER))
        self.assertEqual(app.bot.sent[0][1], APP_TEXTS["uk"]["chat_session_moved"])

    def test_a_blocked_user_does_not_fail_the_request(self):
        # The app session is already live by the time we try to send; a failure to
        # reach the chat cannot be allowed to undo that.
        app = FakeApplication(fail_send=True)
        with self.assertLogs("bot.webapp.bot_bridge", level="WARNING"):
            self.assertFalse(asyncio.run(BotBridge(app).notify_session_moved(USER)))


class TakeOverTest(TempDbMixin, unittest.TestCase):
    def setUp(self):
        self.setup_temp_db()
        self.make_user(USER, "approved")

    def test_an_interrupted_question_gets_a_notice(self):
        state = active_quiz_state()
        app = FakeApplication({USER: state})
        self.assertTrue(asyncio.run(BotBridge(app).take_over_for_app(USER)))
        self.assertEqual(len(app.bot.sent), 1)
        self.assertEqual(state, {})

    def test_no_session_in_the_chat_means_no_message(self):
        # Owner's decision: opening the app on a clean slate must not put a line
        # into the conversation.
        app = FakeApplication()
        self.assertFalse(asyncio.run(BotBridge(app).take_over_for_app(USER)))
        self.assertEqual(app.bot.sent, [])

    def test_a_second_takeover_stays_silent(self):
        app = FakeApplication({USER: active_quiz_state()})
        bridge = BotBridge(app)
        asyncio.run(bridge.take_over_for_app(USER))
        asyncio.run(bridge.take_over_for_app(USER))
        self.assertEqual(len(app.bot.sent), 1)


if __name__ == "__main__":
    unittest.main()
