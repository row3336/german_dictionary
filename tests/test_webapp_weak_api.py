"""Mini App M5 — the weak-forms quiz over HTTP (miniapp.md §7.2, §7.4, §11).

Four modes on one loop: article, plural, präteritum, partizip2. What is checked
here is the contract the client is written against — the shape of each question
(the article one is answered by button, the other three by typing), the verdict and
what it writes, the drill repeat that scores nothing, and the two endings a weak
session can have: it runs out of words, or the user stops it.

The chat has no summary worth showing in these modes (bug #30), so neither does the
app; a session that runs out ends on `no_due` instead. That is asserted rather than
assumed, because it is the one place the app deliberately differs from the FSRS
loop next door.
"""

import sqlite3
import unittest

from _webapp_helpers import TOKEN, TempDbMixin, auth_header
from aiohttp.test_utils import AioHTTPTestCase

from bot.app_config import WEAK_DRILL_GAP_KEY
from bot.storage import (
    WEAK_ACTIVE_CLEAR_STREAK,
    add_word,
    get_form_state,
    set_user_setting,
    update_form_stats,
)
from bot.webapp.access import API_PREFIX
from bot.webapp.app_keys import REGISTRY_KEY

USER = 808

NOUN = {"type": "nomen", "article": "der", "german": "Tisch", "plural": "Tische", "russian": "стол"}
VERB = {
    "type": "verb",
    "german": "gehen",
    "infinitive": "gehen",
    "prateritum": "ging",
    "partizip2": "gegangen",
    "russian": "идти",
}

# Two wrong answers is what flags a form as weak (WEAK_ERRORS_TO_ACTIVE); the app
# never does the flagging itself, it only drills what the chat's own rules flagged.
ERRORS_TO_WEAK = 2


class WeakApiTestCase(TempDbMixin, AioHTTPTestCase):
    """One approved user and helpers to seed weak words and drive the routes."""

    FIELD = "article"

    async def get_application(self):
        self.setup_temp_db()
        self.make_user(USER, "approved")
        from bot.webapp.server import create_app

        return create_app(TOKEN)

    @property
    def mode(self):
        return f"weak_{self.FIELD}"

    # -- fixtures --------------------------------------------------------

    def make_weak(self, fields=None, field=None, suffix=""):
        """A word whose form is flagged weak for this user, and therefore due."""
        field = field or self.FIELD
        fields = fields or NOUN
        infinitive = fields.get("infinitive", "")
        word = add_word(
            self.db,
            {
                "id": "unique",
                "article": "",
                "plural": "",
                "infinitive": "",
                "prateritum": "",
                "partizip2": "",
                "ukrainian": "",
                "english": "",
                "topic": "1",
                **fields,
                "german": f"{fields['german']}{suffix}",
                "russian": f"{fields['russian']}{suffix}",
                "infinitive": f"{infinitive}{suffix}" if infinitive else "",
            },
        )
        for _ in range(ERRORS_TO_WEAK):
            update_form_stats(self.db, USER, word["id"], {field: False})
        self.words[word["id"]] = word
        return word

    @property
    def words(self):
        """Everything seeded so far, by id — the picker chooses, the test follows."""
        if not hasattr(self, "_words"):
            self._words = {}
        return self._words

    def quiz_events(self, word_id=None):
        """Rows this user's answers have written, newest last."""
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            sql = "SELECT * FROM quiz_events WHERE user_id = ? AND mode = ?"
            params = [USER, self.mode]
            if word_id:
                sql += " AND word_id = ?"
                params.append(word_id)
            return [dict(row) for row in conn.execute(sql + " ORDER BY id", params)]

    # -- requests --------------------------------------------------------

    async def start(self, mode=None):
        resp = await self.client.post(
            f"{API_PREFIX}session/start",
            headers=auth_header(USER),
            json={"mode": mode or self.mode},
        )
        return await resp.json()

    async def post(self, path, **body):
        return await self.client.post(
            f"{API_PREFIX}session/{path}", headers=auth_header(USER), json=body
        )

    async def answer(self, token, text):
        return await (await self.post("answer", question_token=token, text=text)).json()

    async def answer_question(self, question, text):
        """Answer whatever is on screen and hand back the whole response."""
        return await self.answer(question["token"], text)

    @property
    def session(self):
        return self.app[REGISTRY_KEY].require(USER)


class QuestionShapeTest(WeakApiTestCase):
    async def test_the_article_question_is_answered_by_button(self):
        word = self.make_weak()
        question = (await self.start())["question"]
        self.assertEqual(question["word_id"], word["id"])
        self.assertEqual(question["mode"], "weak_article")
        # The bare noun: its article is the answer, so it is not in the question.
        self.assertEqual(question["prompt"], "Tisch")
        self.assertEqual(question["prompt_lang"], "de")
        self.assertEqual(question["answer_kind"], "article_choice")
        # der See / die See: without the translation the question has two answers.
        self.assertEqual(question["translation"], "стол")
        self.assertFalse(question["hint"]["gender_known"])
        self.assertNotIn("answer_forms", question)
        self.assertEqual(question["progress"], {"answered": 0, "total_due": 1})

    async def test_the_plural_question_shows_the_article_and_asks_for_the_plural(self):
        self.FIELD = "plural"
        self.make_weak()
        question = (await self.start())["question"]
        self.assertEqual(question["prompt"], "der Tisch")
        self.assertEqual(question["answer_kind"], "text")
        self.assertEqual(question["answer_forms"], {"main": "Plural"})
        # The article is on screen already, so colouring by gender gives nothing away.
        self.assertTrue(question["hint"]["gender_known"])
        self.assertNotIn("translation", question)

    async def test_the_verb_questions_ask_from_the_infinitive(self):
        for field, hint in (("prateritum", "präteritum"), ("partizip2", "partizip II")):
            with self.subTest(field=field):
                self.FIELD = field
                self.make_weak(VERB, suffix=field)
                question = (await self.start())["question"]
                self.assertEqual(question["prompt"], f"gehen{field}")
                self.assertEqual(question["answer_forms"], {"main": hint})
                self.assertEqual(question["hint"]["type"], "verb")

    async def test_a_weak_question_is_never_marked_as_a_retry(self):
        # Weak has no retry queue: a repeat here is a drill, and the client draws
        # the two differently.
        self.make_weak()
        question = (await self.start())["question"]
        self.assertFalse(question["is_retry"])
        self.assertFalse(question["is_drill"])


class AnswerTest(WeakApiTestCase):
    async def test_a_right_article_is_scored_and_answered_back(self):
        word = self.make_weak()
        question = (await self.start())["question"]
        body = await self.answer_question(question, "der")

        self.assertTrue(body["correct"])
        self.assertEqual(body["feedback"]["expected"], "der Tisch")
        self.assertEqual(body["feedback"]["card"]["id"], word["id"])
        # Weak answers carry no streak — the chat reacts to FSRS answers only.
        self.assertEqual(body["feedback"]["streak"], 0)
        events = self.quiz_events(word["id"])
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["is_correct"])

    async def test_a_wrong_answer_is_recorded_and_the_next_question_follows(self):
        word = self.make_weak()
        question = (await self.start())["question"]
        body = await self.answer_question(question, "die")

        self.assertFalse(body["correct"])
        self.assertEqual(body["feedback"]["expected"], "der Tisch")
        self.assertIn("question", body["next"])
        events = self.quiz_events(word["id"])
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0]["is_correct"])

    async def test_i_dont_know_is_a_wrong_answer_that_shows_the_form(self):
        self.FIELD = "plural"
        word = self.make_weak()
        question = (await self.start())["question"]
        resp = await self.post("idk", question_token=question["token"])
        body = await resp.json()

        self.assertFalse(body["correct"])
        self.assertEqual(body["feedback"]["expected"], "die Tische")
        self.assertFalse(self.quiz_events(word["id"])[0]["is_correct"])

    async def test_the_expected_answer_is_the_form_that_was_asked_for(self):
        for field, expected in (("prateritum", "ging"), ("partizip2", "gegangen")):
            with self.subTest(field=field):
                self.FIELD = field
                self.make_weak(VERB, suffix=field)
                question = (await self.start())["question"]
                body = await self.answer_question(question, "falsch")
                self.assertEqual(body["feedback"]["expected"], expected)

    async def test_a_spent_token_is_refused(self):
        # Same guard as the FSRS routes (§4.4): a double tap must not score twice.
        word = self.make_weak()
        question = (await self.start())["question"]
        await self.answer_question(question, "der")
        resp = await self.post("answer", question_token=question["token"], text="der")
        self.assertEqual(resp.status, 409)
        self.assertEqual(len(self.quiz_events(word["id"])), 1)


class DrillTest(WeakApiTestCase):
    """A wrong answer comes back later in the session — and scores nothing."""

    async def asyncSetUp(self):
        # The temp database is created by get_application, which aiohttp calls in
        # the async phase — so the setting cannot be written any earlier.
        await super().asyncSetUp()
        # The default gap is 4 normal shows between repeats; one keeps the test to
        # the three questions the rule actually needs.
        set_user_setting(self.db, USER, WEAK_DRILL_GAP_KEY, "1")

    async def test_a_wrong_answer_comes_back_as_a_drill(self):
        self.make_weak(suffix="a")
        self.make_weak(suffix="b")

        first = (await self.start())["question"]
        second = (await self.answer_question(first, "die"))["next"]["question"]
        # Never two in a row: the repeat waits for another word to be asked.
        self.assertFalse(second["is_drill"])
        self.assertNotEqual(second["word_id"], first["word_id"])

        third = (await self.answer_question(second, "die"))["next"]["question"]
        self.assertTrue(third["is_drill"])
        self.assertEqual(third["word_id"], first["word_id"])

    async def test_answering_a_drill_scores_nothing(self):
        first_word = self.make_weak(suffix="a")
        self.make_weak(suffix="b")

        first = (await self.start())["question"]
        second = (await self.answer_question(first, "die"))["next"]["question"]
        third = (await self.answer_question(second, "die"))["next"]["question"]
        before = get_form_state(self.db, USER, first_word["id"], "article")
        events_before = len(self.quiz_events(first_word["id"]))

        body = await self.answer_question(third, "der")

        # The verdict is still shown — the point of a drill is to practise it — but
        # nothing is written: no event, no change to the weak state.
        self.assertTrue(body["correct"])
        self.assertEqual(len(self.quiz_events(first_word["id"])), events_before)
        self.assertEqual(get_form_state(self.db, USER, first_word["id"], "article"), before)

    async def test_a_drill_does_not_count_towards_the_progress_counter(self):
        self.make_weak(suffix="a")
        self.make_weak(suffix="b")

        first = (await self.start())["question"]
        second = (await self.answer_question(first, "die"))["next"]["question"]
        third = (await self.answer_question(second, "die"))["next"]["question"]

        self.assertTrue(third["is_drill"])
        # Both words are answered and still weak; the repeat adds neither side.
        self.assertEqual(third["progress"], {"answered": 2, "total_due": 2})


class InputErrTest(WeakApiTestCase):
    async def test_the_last_wrong_answer_can_be_taken_back(self):
        word = self.make_weak()
        question = (await self.start())["question"]
        await self.answer_question(question, "die")

        body = await (await self.post("input_err")).json()
        self.assertTrue(body["applied"])
        # The chat's own rollback: the miss is undone and logged as correct.
        events = self.quiz_events(word["id"])
        self.assertTrue(events[-1]["is_correct"])

    async def test_it_fires_once(self):
        self.make_weak()
        question = (await self.start())["question"]
        await self.answer_question(question, "die")
        await self.post("input_err")
        body = await (await self.post("input_err")).json()
        self.assertFalse(body["applied"])

    async def test_a_correct_answer_has_nothing_to_take_back(self):
        self.make_weak()
        question = (await self.start())["question"]
        await self.answer_question(question, "der")
        body = await (await self.post("input_err")).json()
        self.assertFalse(body["applied"])


class SessionEndTest(WeakApiTestCase):
    async def test_a_session_with_nothing_weak_is_an_outcome_not_a_question(self):
        body = await self.start()
        self.assertEqual(body["empty"], "no_due")
        self.assertIsNone(body["question"])

    async def test_a_drained_session_ends_without_a_summary(self):
        # Three correct answers in a row clear the weak flag and put the word on the
        # review ladder, due tomorrow — so the mode runs out of words mid-session.
        self.make_weak()
        question = (await self.start())["question"]
        for _ in range(WEAK_ACTIVE_CLEAR_STREAK - 1):
            question = (await self.answer_question(question, "der"))["next"]["question"]
        body = await self.answer_question(question, "der")

        self.assertEqual(body["next"], {"empty": "no_due"})
        self.assertNotIn("summary", body["next"])

    async def test_stopping_reports_no_summary(self):
        self.make_weak()
        await self.start()
        body = await (await self.post("stop")).json()
        self.assertTrue(body["stopped"])
        # Weak inherits the chat, which has no summary worth showing here (bug #30).
        self.assertNotIn("summary", body)

    async def test_the_progress_counter_tracks_the_words_left(self):
        self.make_weak(suffix="a")
        self.make_weak(suffix="b")
        question = (await self.start())["question"]
        self.assertEqual(question["progress"], {"answered": 0, "total_due": 2})

        nxt = (await self.answer_question(question, "der"))["next"]["question"]
        self.assertEqual(nxt["progress"], {"answered": 1, "total_due": 2})


if __name__ == "__main__":
    unittest.main()
