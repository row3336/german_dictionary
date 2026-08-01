"""Mini App M3 step 1 — the session registry and mutual exclusion (miniapp.md §4, §11).

The rules under test are the ones that go wrong silently: a word asked twice, an
answer written twice, a session that outlives the tab it belonged to. The registry
is pure in-memory logic on purpose, so all of it can be driven directly here with a
fake clock instead of through HTTP.
"""

import unittest

from bot.webapp.sessions import (
    CLIENT_APP,
    CLIENT_BOT,
    SESSION_TTL_SECONDS,
    SessionRegistry,
    SessionTakenOver,
    StaleQuestion,
)

USER = 111
OTHER = 222
MODE = "l1_de"


class FakeClock:
    """Monotonic-ish clock the tests advance by hand."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_registry(clock=None):
    return SessionRegistry(time_source=clock or FakeClock())


class StartAndOwnershipTest(unittest.TestCase):
    def setUp(self):
        self.registry = make_registry()

    def test_an_unknown_user_owns_nothing(self):
        self.assertIsNone(self.registry.active_client(USER))

    def test_starting_in_the_app_makes_the_app_the_owner(self):
        session = self.registry.start(USER, MODE)
        self.assertEqual(self.registry.active_client(USER), CLIENT_APP)
        self.assertEqual(session.mode, MODE)
        self.assertTrue(session.session_id)

    def test_each_session_gets_its_own_id_and_context(self):
        first = self.registry.start(USER, MODE)
        second = self.registry.start(OTHER, MODE)
        self.assertNotEqual(first.session_id, second.session_id)
        first.ctx.user_data["x"] = 1
        self.assertEqual(second.ctx.user_data, {})

    def test_sessions_of_different_users_do_not_collide(self):
        self.registry.start(USER, MODE)
        self.registry.start(OTHER, "de_l1")
        self.assertEqual(self.registry.require(USER).mode, MODE)
        self.assertEqual(self.registry.require(OTHER).mode, "de_l1")

    def test_stop_closes_the_session(self):
        self.registry.start(USER, MODE)
        self.registry.stop(USER)
        self.assertIsNone(self.registry.active_client(USER))
        with self.assertRaises(SessionTakenOver):
            self.registry.require(USER)


class TakeoverTest(unittest.TestCase):
    def setUp(self):
        self.registry = make_registry()

    def test_answering_in_the_chat_kills_the_app_session(self):
        session = self.registry.start(USER, MODE)
        self.assertTrue(self.registry.claim_for_bot(USER))
        self.assertEqual(self.registry.active_client(USER), CLIENT_BOT)
        with self.assertRaises(SessionTakenOver):
            self.registry.require(USER, session.session_id)

    def test_claiming_without_an_app_session_reports_nothing_was_taken(self):
        # The user was in the chat all along - the caller uses this to stay quiet
        # instead of announcing a takeover that did not happen.
        self.assertFalse(self.registry.claim_for_bot(USER))
        self.assertEqual(self.registry.active_client(USER), CLIENT_BOT)

    def test_a_second_claim_in_a_row_reports_nothing_new(self):
        self.registry.start(USER, MODE)
        self.assertTrue(self.registry.claim_for_bot(USER))
        self.assertFalse(self.registry.claim_for_bot(USER))

    def test_starting_in_the_app_takes_the_session_back(self):
        self.registry.claim_for_bot(USER)
        self.registry.start(USER, MODE)
        self.assertEqual(self.registry.active_client(USER), CLIENT_APP)
        self.assertIsNotNone(self.registry.require(USER))

    def test_a_second_tab_supersedes_the_first(self):
        # §4.3: phone and desktop are the same event as the bot answering.
        first = self.registry.start(USER, MODE)
        second = self.registry.start(USER, MODE)
        self.assertNotEqual(first.session_id, second.session_id)
        with self.assertRaises(SessionTakenOver):
            self.registry.require(USER, first.session_id)
        self.assertIs(self.registry.require(USER, second.session_id), second)

    def test_a_takeover_only_touches_the_user_it_names(self):
        self.registry.start(USER, MODE)
        other = self.registry.start(OTHER, MODE)
        self.registry.claim_for_bot(USER)
        self.assertIs(self.registry.require(OTHER), other)

    def test_the_error_carries_the_wire_code(self):
        self.registry.start(USER, MODE)
        self.registry.claim_for_bot(USER)
        with self.assertRaises(SessionTakenOver) as ctx:
            self.registry.require(USER)
        self.assertEqual(ctx.exception.code, "session_taken_over")


class QuestionTokenTest(unittest.TestCase):
    def setUp(self):
        self.registry = make_registry()
        self.session = self.registry.start(USER, MODE)

    def test_a_fresh_token_is_accepted_once(self):
        token = self.registry.issue_question(self.session)
        self.registry.consume_question(self.session, token)  # first answer lands

    def test_the_same_token_cannot_be_spent_twice(self):
        # The double tap / network retry case: the second attempt must not reach
        # the database at all (§12 risk 2).
        token = self.registry.issue_question(self.session)
        self.registry.consume_question(self.session, token)
        with self.assertRaises(StaleQuestion) as ctx:
            self.registry.consume_question(self.session, token)
        self.assertEqual(ctx.exception.code, "stale_question")

    def test_an_unknown_token_is_refused(self):
        self.registry.issue_question(self.session)
        with self.assertRaises(StaleQuestion):
            self.registry.consume_question(self.session, "not-the-token")

    def test_an_answer_before_any_question_is_refused(self):
        with self.assertRaises(StaleQuestion):
            self.registry.consume_question(self.session, "anything")

    def test_a_new_question_invalidates_the_previous_token(self):
        stale = self.registry.issue_question(self.session)
        fresh = self.registry.issue_question(self.session)
        self.assertNotEqual(stale, fresh)
        with self.assertRaises(StaleQuestion):
            self.registry.consume_question(self.session, stale)
        self.registry.consume_question(self.session, fresh)

    def test_a_token_is_not_valid_in_another_session(self):
        token = self.registry.issue_question(self.session)
        other = self.registry.start(OTHER, MODE)
        with self.assertRaises(StaleQuestion):
            self.registry.consume_question(other, token)


class IdleSweepTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.registry = make_registry(self.clock)

    def test_a_fresh_session_survives(self):
        self.registry.start(USER, MODE)
        self.clock.advance(SESSION_TTL_SECONDS - 1)
        self.assertEqual(self.registry.sweep(), 0)
        self.assertIsNotNone(self.registry.require(USER))

    def test_an_idle_session_is_dropped(self):
        self.registry.start(USER, MODE)
        self.clock.advance(SESSION_TTL_SECONDS + 1)
        self.assertEqual(self.registry.sweep(), 1)
        self.assertEqual(len(self.registry), 0)
        with self.assertRaises(SessionTakenOver):
            self.registry.require(USER)

    def test_a_swept_session_leaves_no_ownership_behind(self):
        # Otherwise the user stays marked "in the app" forever and the bot keeps
        # announcing a takeover of a session that is long gone.
        self.registry.start(USER, MODE)
        self.clock.advance(SESSION_TTL_SECONDS + 1)
        self.registry.sweep()
        self.assertIsNone(self.registry.active_client(USER))

    def test_activity_keeps_a_session_alive(self):
        self.registry.start(USER, MODE)
        for _ in range(3):
            self.clock.advance(SESSION_TTL_SECONDS - 10)
            self.registry.require(USER)  # a request touches the session
        self.clock.advance(5)
        self.assertEqual(self.registry.sweep(), 0)

    def test_issuing_a_question_counts_as_activity(self):
        session = self.registry.start(USER, MODE)
        self.clock.advance(SESSION_TTL_SECONDS - 10)
        self.registry.issue_question(session)
        self.clock.advance(20)
        self.assertEqual(self.registry.sweep(), 0)

    def test_the_sweep_leaves_a_bot_owner_alone(self):
        self.registry.claim_for_bot(USER)
        self.clock.advance(SESSION_TTL_SECONDS * 2)
        self.registry.sweep()
        self.assertEqual(self.registry.active_client(USER), CLIENT_BOT)


if __name__ == "__main__":
    unittest.main()
