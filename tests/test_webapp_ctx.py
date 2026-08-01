"""Mini App M2 — WebContext drives the bot's own session helpers (miniapp.md §5).

This is the test that keeps §5.1 honest. The Mini App is not allowed to reimplement
the FSRS picker, the retry queue or the weak drill; it passes a WebContext into the
very functions the bot calls. If someone later adds `context.bot` or
`context.application` to bot/common/session.py, these tests fail immediately —
WebContext has __slots__, so any other attribute raises AttributeError instead of
silently springing into existence.
"""

import unittest

from _webapp_helpers import TempDbMixin

from bot.common.session import (
    _bump_session_result,
    _enqueue_weak_drill,
    _maybe_take_retry_entry,
    _maybe_take_weak_drill_entry,
    _pick_due_entry,
    _register_retry_result,
    _remove_from_retry,
    _reset_session,
    _session_correct_count,
    _session_count,
    _session_wrong_count,
    _set_retry_since_last,
)
from bot.app_config import RETRY_GAP_QUESTIONS
from bot.storage import log_quiz_event
from bot.webapp.ctx import WebContext

USER = 111
MODE = "ru_de"


class WebContextShapeTest(unittest.TestCase):
    def test_starts_with_an_empty_user_data_dict(self):
        self.assertEqual(WebContext().user_data, {})

    def test_each_instance_gets_its_own_dict(self):
        first, second = WebContext(), WebContext()
        first.user_data["x"] = 1
        self.assertEqual(second.user_data, {})

    def test_accepts_an_existing_dict(self):
        data = {"session_mode": MODE}
        self.assertIs(WebContext(data).user_data, data)

    def test_exposes_nothing_but_user_data(self):
        # The whole premise of §5.2: if a helper ever reaches for another context
        # attribute, it fails loudly here rather than in production.
        ctx = WebContext()
        for attribute in ("bot", "application", "job_queue", "chat_data", "bot_data"):
            with self.subTest(attribute=attribute), self.assertRaises(AttributeError):
                getattr(ctx, attribute)


class SessionCountersTest(TempDbMixin, unittest.TestCase):
    def setUp(self):
        self.setup_temp_db()
        self.make_user(USER, "approved")

    def test_counters_run_through_the_bots_helpers(self):
        ctx = WebContext()
        _reset_session(ctx, MODE)
        _bump_session_result(ctx, MODE, ok=True)
        _bump_session_result(ctx, MODE, ok=True)
        _bump_session_result(ctx, MODE, ok=False)
        self.assertEqual(_session_correct_count(ctx, MODE), 2)
        self.assertEqual(_session_wrong_count(ctx, MODE), 1)

    def test_reset_clears_a_previous_session(self):
        ctx = WebContext()
        _reset_session(ctx, MODE)
        _bump_session_result(ctx, MODE, ok=True)
        _reset_session(ctx, MODE)
        self.assertEqual(_session_correct_count(ctx, MODE), 0)
        self.assertEqual(_session_count(ctx, MODE), 0)

    def test_state_lands_in_user_data_where_the_bot_keeps_it(self):
        # Same keys, same shapes: a session started in the app is readable by the
        # bot's own code without a translation layer.
        ctx = WebContext()
        _reset_session(ctx, MODE)
        _bump_session_result(ctx, MODE, ok=True)
        self.assertEqual(ctx.user_data["session_mode"], MODE)
        self.assertEqual(ctx.user_data[f"session_correct_{MODE}"], 1)


class DuePickerTest(TempDbMixin, unittest.TestCase):
    def setUp(self):
        self.setup_temp_db()
        self.make_user(USER, "approved")

    def test_picks_a_due_word_for_a_web_context(self):
        word = self.add_noun("Tisch", "стол")
        # A wrong answer, not a right one: FSRS modes hide what was already
        # answered correctly today, so a correct event would make the word not due.
        log_quiz_event(self.db, USER, word["id"], MODE, False)
        entry = _pick_due_entry(WebContext(), USER, MODE)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["german"], "Tisch")

    def test_returns_nothing_when_no_word_is_due(self):
        self.add_noun("Tisch", "стол")  # never studied - not due for anybody
        self.assertIsNone(_pick_due_entry(WebContext(), USER, MODE))


class RetryQueueTest(TempDbMixin, unittest.TestCase):
    def setUp(self):
        self.setup_temp_db()
        self.make_user(USER, "approved")
        self.word = self.add_noun("Tisch", "стол")

    def test_a_wrong_answer_waits_for_the_gap_then_comes_back(self):
        ctx = WebContext()
        _register_retry_result(ctx, MODE, self.word["id"], ok=False, was_retry=False, user_id=USER)
        self.assertIn(self.word["id"], ctx.user_data["retry_queue"][MODE])

        # Straight away the word is still too fresh to re-ask.
        self.assertIsNone(_maybe_take_retry_entry(ctx, MODE, USER))

        # The gap counter is bumped by whoever shows a normal question
        # (bot/handlers/quiz.py:184) - the app will have to do the same in M4.
        _set_retry_since_last(ctx, MODE, RETRY_GAP_QUESTIONS)
        entry = _maybe_take_retry_entry(ctx, MODE, USER)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["id"], self.word["id"])
        self.assertTrue(ctx.user_data["quiz_is_retry"])

    def test_an_empty_queue_yields_nothing(self):
        self.assertIsNone(_maybe_take_retry_entry(WebContext(), MODE, USER))

    def test_a_word_can_be_pulled_out_of_the_queue(self):
        ctx = WebContext()
        _register_retry_result(ctx, MODE, self.word["id"], ok=False, was_retry=False, user_id=USER)
        _remove_from_retry(ctx, MODE, self.word["id"])
        self.assertEqual(ctx.user_data["retry_queue"][MODE], [])


class WeakDrillTest(TempDbMixin, unittest.TestCase):
    def setUp(self):
        self.setup_temp_db()
        self.make_user(USER, "approved")
        self.word = self.add_noun("Tisch", "стол", plural="Tische")

    def test_an_enqueued_word_is_drilled(self):
        ctx = WebContext()
        mode = "weak_plural"
        _enqueue_weak_drill(ctx, mode, self.word["id"], USER)
        self.assertIn(self.word["id"], ctx.user_data["weak_drill_queue"][mode])
        entry = _maybe_take_weak_drill_entry(ctx, mode, USER, force=True)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["id"], self.word["id"])
        self.assertTrue(ctx.user_data["quiz_is_weak_drill"])

    def test_an_empty_drill_queue_yields_nothing(self):
        self.assertIsNone(
            _maybe_take_weak_drill_entry(WebContext(), "weak_plural", USER, force=True)
        )


if __name__ == "__main__":
    unittest.main()
