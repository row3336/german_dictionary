"""Mini App M4 — the FSRS quiz loop (miniapp.md §7.2, §7.4).

These tests drive `bot/webapp/quiz_flow.py` directly, with no HTTP in the way: the
loop is the part that has to be provably right, and the routes above it in M4.3 are
plumbing. What is checked here is the behaviour the chat already has and the app
must not re-invent — the pick order, the retry accounting, the SRS write, the
counters — plus the two places the app genuinely differs (`dont_know` instead of a
keyboard, and no message to react to).

The verdict-for-verdict comparison against the bot lives in test_webapp_parity.py
(M4.4); this file checks the loop around it.
"""

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

from _webapp_helpers import TempDbMixin

from bot.app_config import RETRY_GAP_QUESTIONS
from bot.common.session import _set_retry_since_last
from bot.storage import (
    add_word,
    get_progress,
    log_quiz_event,
    set_user_dict_lang,
)
from bot.webapp import quiz_flow
from bot.webapp.sessions import SessionRegistry

USER = 501

REPO_ROOT = Path(__file__).resolve().parent.parent

# The loop must stay callable with neither PTB nor aiohttp importable — that is what
# lets the same code run under the bot and under a test. Checked in a subprocess:
# inside one pytest session the telegram stub is already installed by a neighbour,
# so an in-process import would prove nothing.
IMPORT_PROBE = textwrap.dedent(
    """
    import sys

    from bot.webapp import quiz_flow

    assert "telegram" not in sys.modules, "quiz_flow pulled in PTB"
    assert "aiohttp" not in sys.modules, "quiz_flow pulled in aiohttp"
    assert quiz_flow.FSRS_DIRECTIONS == ("l1_de", "de_l1", "pl_sg")
    print("ok")
    """
)


class QuizFlowTestCase(TempDbMixin, unittest.TestCase):
    """A user with one approved account, a session registry and words on demand."""

    DIRECTION = quiz_flow.L1_DE

    def setUp(self):
        self.setup_temp_db()
        self.make_user(USER, "approved")
        self.registry = SessionRegistry()

    def open_session(self, direction=None, word_type=None):
        return self.registry.start(USER, direction or self.DIRECTION, word_type)

    def make_due(self, german="Tisch", russian="стол", **fields):
        """A word that is due right now: seen before, never answered correctly today."""
        word = add_word(
            self.db,
            {
                "id": "unique",
                "type": "nomen",
                "article": "der",
                "german": german,
                "plural": "",
                "infinitive": "",
                "prateritum": "",
                "partizip2": "",
                "russian": russian,
                "ukrainian": "",
                "english": "",
                "topic": "1",
                **fields,
            },
        )
        # select_due only offers words with a non-learning history, and hides what
        # was answered correctly today — so the seed event has to be a wrong one.
        log_quiz_event(self.db, USER, word["id"], quiz_flow.mode_for(USER, self.DIRECTION), False)
        return word

    @property
    def mode(self):
        return quiz_flow.mode_for(USER, self.DIRECTION)


class LoopStandsAloneTest(unittest.TestCase):
    def test_the_loop_imports_without_telegram_or_aiohttp(self):
        result = subprocess.run(
            [sys.executable, "-c", IMPORT_PROBE],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")


class ModeResolutionTest(QuizFlowTestCase):
    def test_directions_map_to_the_bots_own_mode_strings(self):
        self.assertEqual(quiz_flow.mode_for(USER, quiz_flow.L1_DE), "ru_de")
        self.assertEqual(quiz_flow.mode_for(USER, quiz_flow.DE_L1), "de_ru")
        self.assertEqual(quiz_flow.mode_for(USER, quiz_flow.PL_SG), "pl_sg_ru")

    def test_the_mode_follows_the_users_dictionary_language(self):
        # A Ukrainian dictionary answers into different mode rows entirely; picking
        # the wrong one would score against a stranger's history.
        set_user_dict_lang(self.db, USER, "uk")
        self.assertEqual(quiz_flow.mode_for(USER, quiz_flow.L1_DE), "uk_de")
        self.assertEqual(quiz_flow.mode_for(USER, quiz_flow.DE_L1), "de_uk")
        self.assertEqual(quiz_flow.mode_for(USER, quiz_flow.PL_SG), "pl_sg_uk")

    def test_a_non_fsrs_direction_is_refused(self):
        with self.assertRaises(ValueError):
            quiz_flow.mode_for(USER, "weak_article")


class FirstQuestionTest(QuizFlowTestCase):
    def test_begin_hands_out_a_due_word(self):
        self.make_due()
        entry = quiz_flow.begin(self.open_session(), USER)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["german"], "Tisch")

    def test_begin_starts_the_counters_from_zero(self):
        self.make_due()
        session = self.open_session()
        session.ctx.user_data[f"session_correct_{self.mode}"] = 7
        quiz_flow.begin(session, USER)
        self.assertEqual(quiz_flow.summary(session, USER)["correct"], 0)

    def test_nothing_due_yields_no_question(self):
        session = self.open_session()
        self.assertIsNone(quiz_flow.begin(session, USER))
        self.assertIsNone(quiz_flow.current_entry(session))

    def test_the_word_on_screen_is_where_the_bots_helpers_look_for_it(self):
        self.make_due()
        session = self.open_session()
        entry = quiz_flow.begin(session, USER)
        self.assertEqual(session.ctx.user_data["quiz_entry"], entry)


class AnswerTest(QuizFlowTestCase):
    def setUp(self):
        super().setUp()
        self.word = self.make_due()
        self.session = self.open_session()
        quiz_flow.begin(self.session, USER)

    def test_a_correct_answer_is_scored_and_counted(self):
        result = quiz_flow.submit_answer(self.session, USER, "der Tisch")
        self.assertTrue(result.ok)
        self.assertFalse(result.is_retry)
        self.assertEqual(get_progress(self.db, USER, self.word["id"], self.mode)["repetitions"], 1)
        self.assertEqual(quiz_flow.summary(self.session, USER)["correct"], 1)

    def test_a_wrong_answer_counts_wrong_and_queues_a_retry(self):
        result = quiz_flow.submit_answer(self.session, USER, "das Haus")
        self.assertFalse(result.ok)
        summary = quiz_flow.summary(self.session, USER)
        self.assertEqual((summary["correct"], summary["wrong"]), (0, 1))
        self.assertIn(self.word["id"], self.session.ctx.user_data["retry_queue"][self.mode])

    def test_dont_know_is_a_wrong_answer_whatever_the_text_says(self):
        result = quiz_flow.submit_answer(self.session, USER, "der Tisch", dont_know=True)
        self.assertFalse(result.ok)
        self.assertEqual(quiz_flow.summary(self.session, USER)["wrong"], 1)

    def test_the_streak_grows_with_consecutive_correct_answers(self):
        self.assertEqual(quiz_flow.submit_answer(self.session, USER, "der Tisch").streak, 1)
        quiz_flow.next_question(self.session, USER)
        self.session.ctx.user_data["quiz_entry"] = self.word  # only one word in the DB
        self.assertEqual(quiz_flow.submit_answer(self.session, USER, "der Tisch").streak, 2)

    def test_a_wrong_answer_breaks_the_streak(self):
        quiz_flow.submit_answer(self.session, USER, "der Tisch")
        self.session.ctx.user_data["quiz_entry"] = self.word
        self.assertEqual(quiz_flow.submit_answer(self.session, USER, "falsch").streak, 0)

    def test_answering_with_no_question_on_screen_does_nothing(self):
        self.session.ctx.user_data.pop("quiz_entry")
        self.assertIsNone(quiz_flow.submit_answer(self.session, USER, "der Tisch"))


class RetryShowTest(QuizFlowTestCase):
    """A retry show repeats a word that was already scored — and must stay unscored."""

    def setUp(self):
        super().setUp()
        self.word = self.make_due()
        self.session = self.open_session()
        quiz_flow.begin(self.session, USER)
        quiz_flow.submit_answer(self.session, USER, "falsch")

    def _bring_the_retry_back(self):
        _set_retry_since_last(self.session.ctx, self.mode, RETRY_GAP_QUESTIONS)
        return quiz_flow.next_question(self.session, USER)

    def test_the_word_comes_back_once_the_gap_has_passed(self):
        entry = self._bring_the_retry_back()
        self.assertEqual(entry["id"], self.word["id"])
        self.assertTrue(self.session.ctx.user_data["quiz_is_retry"])

    def test_a_retry_answer_is_not_scored_again(self):
        self._bring_the_retry_back()
        before = get_progress(self.db, USER, self.word["id"], self.mode)
        result = quiz_flow.submit_answer(self.session, USER, "der Tisch")
        self.assertTrue(result.is_retry)
        self.assertEqual(get_progress(self.db, USER, self.word["id"], self.mode), before)
        summary = quiz_flow.summary(self.session, USER)
        # Still the one wrong first answer: retries never re-tally (spec #9).
        self.assertEqual((summary["correct"], summary["wrong"], summary["answered"]), (0, 1, 1))

    def test_retry_shows_are_counted_for_the_summary(self):
        self._bring_the_retry_back()
        self.assertEqual(quiz_flow.summary(self.session, USER)["retries"], 1)

    def test_pending_retries_are_drained_when_nothing_else_is_due(self):
        # The gap is not up, but the FSRS queue is empty: the repeat happens now
        # rather than being lost with the session.
        entry = quiz_flow.next_question(self.session, USER)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["id"], self.word["id"])
        self.assertTrue(self.session.ctx.user_data["quiz_is_retry"])


class InputErrTest(QuizFlowTestCase):
    def setUp(self):
        super().setUp()
        self.word = self.make_due()
        self.session = self.open_session()
        quiz_flow.begin(self.session, USER)

    def test_a_wrong_answer_can_be_taken_back(self):
        quiz_flow.submit_answer(self.session, USER, "der Tissh")
        self.assertTrue(quiz_flow.apply_input_err(self.session, USER))
        summary = quiz_flow.summary(self.session, USER)
        self.assertEqual((summary["correct"], summary["wrong"]), (1, 0))
        self.assertNotIn(self.word["id"], self.session.ctx.user_data["retry_queue"][self.mode])

    def test_a_correct_answer_has_nothing_to_fix(self):
        quiz_flow.submit_answer(self.session, USER, "der Tisch")
        self.assertFalse(quiz_flow.apply_input_err(self.session, USER))

    def test_it_only_applies_once(self):
        quiz_flow.submit_answer(self.session, USER, "der Tissh")
        quiz_flow.apply_input_err(self.session, USER)
        self.assertFalse(quiz_flow.apply_input_err(self.session, USER))

    def test_nothing_answered_yet_is_not_an_error(self):
        self.assertFalse(quiz_flow.apply_input_err(self.session, USER))


class DeL1Test(QuizFlowTestCase):
    DIRECTION = quiz_flow.DE_L1

    def test_any_synonym_of_the_translation_is_accepted(self):
        self.make_due(german="Tisch", russian="стол, столик")
        session = self.open_session()
        quiz_flow.begin(session, USER)
        self.assertTrue(quiz_flow.submit_answer(session, USER, "столик").ok)

    def test_a_different_word_is_wrong(self):
        self.make_due(german="Tisch", russian="стол")
        session = self.open_session()
        quiz_flow.begin(session, USER)
        self.assertFalse(quiz_flow.submit_answer(session, USER, "стул").ok)


class PlSgTest(QuizFlowTestCase):
    DIRECTION = quiz_flow.PL_SG

    def test_the_singular_with_its_article_is_correct(self):
        self.make_due(german="Tisch", plural="Tische")
        session = self.open_session()
        entry = quiz_flow.begin(session, USER)
        self.assertEqual(entry["german"], "Tisch")
        self.assertTrue(quiz_flow.submit_answer(session, USER, "der Tisch").ok)

    def test_the_wrong_article_is_wrong(self):
        self.make_due(german="Tisch", plural="Tische")
        session = self.open_session()
        quiz_flow.begin(session, USER)
        self.assertFalse(quiz_flow.submit_answer(session, USER, "die Tisch").ok)

    def test_a_noun_that_cannot_be_asked_is_skipped(self):
        # "die Lehrerin/Lehrerin" would be a question whose answer is the question.
        self.make_due(german="Lehrerin", plural="Lehrerin", article="die")
        self.assertIsNone(quiz_flow.begin(self.open_session(), USER))

    def test_the_askable_noun_is_found_past_an_unaskable_one(self):
        self.make_due(german="Lehrerin", plural="Lehrerin", article="die")
        self.make_due(german="Tisch", plural="Tische")
        entry = quiz_flow.begin(self.open_session(), USER)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["german"], "Tisch")


class ProgressAndSummaryTest(QuizFlowTestCase):
    def test_the_counter_starts_at_zero_of_one(self):
        self.make_due()
        session = self.open_session()
        quiz_flow.begin(session, USER)
        self.assertEqual(quiz_flow.progress(session, USER), {"answered": 0, "total_due": 1})

    def test_the_answered_half_grows_with_every_word(self):
        self.make_due("Tisch", "стол")
        self.make_due("Haus", "дом")
        session = self.open_session()
        quiz_flow.begin(session, USER)
        self.assertEqual(quiz_flow.progress(session, USER)["total_due"], 2)
        quiz_flow.submit_answer(session, USER, "der Tisch")
        quiz_flow.next_question(session, USER)
        self.assertEqual(quiz_flow.progress(session, USER), {"answered": 1, "total_due": 2})

    def test_a_repeated_word_does_not_inflate_the_total(self):
        self.make_due()
        session = self.open_session()
        quiz_flow.begin(session, USER)
        quiz_flow.submit_answer(session, USER, "falsch")
        quiz_flow.next_question(session, USER)  # the same word, as a retry
        self.assertEqual(quiz_flow.progress(session, USER), {"answered": 1, "total_due": 1})

    def test_a_flawless_session_ends_in_confetti(self):
        self.make_due()
        session = self.open_session()
        quiz_flow.begin(session, USER)
        quiz_flow.submit_answer(session, USER, "der Tisch")
        self.assertEqual(quiz_flow.summary(session, USER)["effect"], "confetti")

    def test_a_session_gone_wrong_ends_in_the_other_effect(self):
        self.make_due()
        session = self.open_session()
        quiz_flow.begin(session, USER)
        quiz_flow.submit_answer(session, USER, "falsch")
        self.assertEqual(quiz_flow.summary(session, USER)["effect"], "poop")

    def test_an_untouched_session_gets_no_effect(self):
        self.make_due()
        session = self.open_session()
        quiz_flow.begin(session, USER)
        summary = quiz_flow.summary(session, USER)
        self.assertIsNone(summary["effect"])
        self.assertEqual(summary["mode"], quiz_flow.L1_DE)


class ExhaustionTest(QuizFlowTestCase):
    def test_the_spent_word_is_cleared_so_it_cannot_be_answered_twice(self):
        self.make_due()
        session = self.open_session()
        quiz_flow.begin(session, USER)
        quiz_flow.submit_answer(session, USER, "der Tisch")
        # Answered correctly today -> no longer due, and no retry is pending.
        self.assertIsNone(quiz_flow.next_question(session, USER))
        self.assertIsNone(quiz_flow.current_entry(session))
        self.assertIsNone(quiz_flow.submit_answer(session, USER, "der Tisch"))


if __name__ == "__main__":
    unittest.main()
