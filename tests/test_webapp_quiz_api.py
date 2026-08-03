"""Mini App M4.3 — the quiz routes (miniapp.md §7.2, §7.4, §11).

The loop itself is tested in test_webapp_quiz_flow.py; what is checked here is the
contract over HTTP: that a session hands out a question in the documented shape,
that answering returns a verdict together with whatever comes next, that "I don't
know" and "typo" behave the way the chat's own buttons do, and that a drained queue
ends in a summary rather than in a question the user cannot answer.

The serializer classes at the bottom run without HTTP: what a question shows and
what the feedback calls the expected answer are per-word-type rules, and a table of
them reads better than the same assertions spread over a dozen requests.
"""

import unittest

from _webapp_helpers import TOKEN, TempDbMixin, auth_header
from aiohttp.test_utils import AioHTTPTestCase

from bot.app_config import RETRY_GAP_QUESTIONS
from bot.common.session import _set_retry_since_last
from bot.storage import add_word, get_progress, log_quiz_event, set_user_word_media
from bot.webapp import quiz_flow, serializers
from bot.webapp.access import API_PREFIX
from bot.webapp.app_keys import REGISTRY_KEY

USER = 707


class QuizApiTestCase(TempDbMixin, AioHTTPTestCase):
    """One approved user, an empty dictionary, and helpers to drive the routes."""

    MODE = quiz_flow.L1_DE

    async def get_application(self):
        self.setup_temp_db()
        self.make_user(USER, "approved")
        from bot.webapp.server import create_app

        return create_app(TOKEN)

    # -- fixtures --------------------------------------------------------

    def make_due(self, german="Tisch", russian="стол", **fields):
        """A word due right now: seen before, not answered correctly today."""
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
        log_quiz_event(self.db, USER, word["id"], quiz_flow.mode_for(USER, self.MODE), False)
        self.words[word["id"]] = word
        return word

    @property
    def words(self):
        """Everything seeded so far, by id — the picker chooses, the test follows."""
        if not hasattr(self, "_words"):
            self._words = {}
        return self._words

    def correct_for(self, question):
        """The answer that scores this question right, whichever word came up.

        With two due words the pick order is the picker's business, so a test that
        hard-codes "der Tisch" for the second question is a test that passes by
        luck. This looks the word up instead.
        """
        word = self.words[question["word_id"]]
        return f"{word['article']} {word['german']}".strip()

    # -- requests --------------------------------------------------------

    async def start(self, mode=None, **body):
        resp = await self.client.post(
            f"{API_PREFIX}session/start",
            headers=auth_header(USER),
            json={"mode": mode or self.MODE, **body},
        )
        return await resp.json()

    async def post(self, path, **body):
        return await self.client.post(
            f"{API_PREFIX}session/{path}", headers=auth_header(USER), json=body
        )

    async def answer(self, token, text):
        return await (await self.post("answer", question_token=token, text=text)).json()

    @property
    def session(self):
        return self.app[REGISTRY_KEY].require(USER)

    @property
    def mode(self):
        return quiz_flow.mode_for(USER, self.MODE)


class StartTest(QuizApiTestCase):
    async def test_the_session_opens_on_its_first_question(self):
        word = self.make_due()
        body = await self.start()
        question = body["question"]
        self.assertEqual(question["word_id"], word["id"])
        self.assertEqual(question["mode"], "l1_de")
        self.assertEqual(question["prompt"], "стол")
        self.assertEqual(question["prompt_lang"], "ru")
        self.assertEqual(question["answer_kind"], "text")
        self.assertEqual(question["phase"], "quiz")
        self.assertFalse(question["is_retry"])
        self.assertFalse(question["is_drill"])
        self.assertEqual(question["progress"], {"answered": 0, "total_due": 1})
        self.assertTrue(question["token"])

    async def test_an_empty_queue_is_an_outcome_not_a_question(self):
        body = await self.start()
        self.assertEqual(body["empty"], "no_due")
        self.assertIsNone(body["question"])

    async def test_a_mode_the_app_cannot_run_yet_says_so(self):
        # learning (M6) can be opened but not asked here; the client has a screen
        # for that, and it is not an error screen. Weak modes run since M5.
        self.make_due()
        body = await self.start(mode="learning")
        self.assertEqual(body["empty"], "chat_only")

    async def test_reopening_the_app_gets_the_same_question_back(self):
        self.make_due()
        started = await self.start()
        resp = await self.client.get(f"{API_PREFIX}session/question", headers=auth_header(USER))
        self.assertEqual((await resp.json())["question"], started["question"])


class AnswerTest(QuizApiTestCase):
    async def test_a_correct_answer_comes_back_with_its_feedback(self):
        word = self.make_due()
        token = (await self.start())["question"]["token"]
        body = await self.answer(token, "der Tisch")
        self.assertTrue(body["correct"])
        self.assertEqual(body["feedback"]["expected"], "der Tisch")
        self.assertEqual(body["feedback"]["streak"], 1)
        self.assertEqual(body["feedback"]["card"]["id"], word["id"])
        self.assertEqual(get_progress(self.db, USER, word["id"], self.mode)["repetitions"], 1)

    async def test_a_wrong_answer_is_scored_wrong(self):
        self.make_due()
        token = (await self.start())["question"]["token"]
        body = await self.answer(token, "das Haus")
        self.assertFalse(body["correct"])
        self.assertEqual(body["feedback"]["streak"], 0)

    async def test_the_next_question_arrives_with_the_verdict(self):
        self.make_due("Tisch", "стол")
        self.make_due("Haus", "дом")
        token = (await self.start())["question"]["token"]
        body = await self.answer(token, "der Tisch")
        following = body["next"]["question"]
        self.assertEqual(following["progress"], {"answered": 1, "total_due": 2})
        self.assertNotEqual(following["token"], token)

    async def test_a_drained_queue_ends_in_a_summary(self):
        self.make_due()
        token = (await self.start())["question"]["token"]
        body = await self.answer(token, "der Tisch")
        self.assertNotIn("question", body["next"])
        self.assertEqual(
            body["next"]["summary"],
            {
                "answered": 1,
                "correct": 1,
                "wrong": 0,
                "retries": 0,
                "mode": "l1_de",
                "effect": "confetti",
            },
        )

    async def test_a_spent_token_is_refused(self):
        self.make_due()
        token = (await self.start())["question"]["token"]
        await self.answer(token, "der Tisch")
        resp = await self.post("answer", question_token=token, text="der Tisch")
        self.assertEqual(resp.status, 409)
        self.assertEqual((await resp.json())["error"], "stale_question")

    async def test_an_answer_without_a_session_is_refused(self):
        resp = await self.post("answer", question_token="whatever", text="der Tisch")
        self.assertEqual(resp.status, 409)
        self.assertEqual((await resp.json())["error"], "session_taken_over")

    async def test_a_malformed_body_is_a_400(self):
        self.make_due()
        await self.start()
        resp = await self.client.post(
            f"{API_PREFIX}session/answer",
            headers={**auth_header(USER), "Content-Type": "application/json"},
            data="{not json",
        )
        self.assertEqual(resp.status, 400)


class DontKnowTest(QuizApiTestCase):
    async def test_idk_is_wrong_whatever_was_typed(self):
        word = self.make_due()
        token = (await self.start())["question"]["token"]
        resp = await self.post("idk", question_token=token, text="der Tisch")
        body = await resp.json()
        self.assertFalse(body["correct"])
        self.assertEqual(body["feedback"]["expected"], "der Tisch")
        self.assertEqual(get_progress(self.db, USER, word["id"], self.mode)["repetitions"], 0)

    async def test_idk_also_needs_the_token(self):
        self.make_due()
        await self.start()
        resp = await self.post("idk", question_token="not-the-token")
        self.assertEqual(resp.status, 409)


class InputErrTest(QuizApiTestCase):
    async def test_a_typo_turns_the_last_answer_into_a_correct_one(self):
        self.make_due()
        token = (await self.start())["question"]["token"]
        await self.answer(token, "der Tissh")
        body = await (await self.post("input_err")).json()
        self.assertTrue(body["applied"])
        summary = await (await self.post("stop")).json()
        self.assertEqual((summary["summary"]["correct"], summary["summary"]["wrong"]), (1, 0))

    async def test_there_is_nothing_to_fix_after_a_correct_answer(self):
        self.make_due()
        token = (await self.start())["question"]["token"]
        await self.answer(token, "der Tisch")
        self.assertFalse((await (await self.post("input_err")).json())["applied"])

    async def test_it_applies_once(self):
        self.make_due()
        token = (await self.start())["question"]["token"]
        await self.answer(token, "der Tissh")
        await self.post("input_err")
        self.assertFalse((await (await self.post("input_err")).json())["applied"])

    async def test_it_needs_a_live_session(self):
        resp = await self.post("input_err")
        self.assertEqual(resp.status, 409)


class RetryTest(QuizApiTestCase):
    async def test_a_repeated_word_is_marked_as_a_repeat(self):
        self.make_due("Tisch", "стол")
        self.make_due("Haus", "дом")
        started = await self.start()
        # Answer the first word wrong, then let the gap pass so the retry queue is
        # allowed to hand it back rather than waiting for more questions.
        wrong = await self.answer(started["question"]["token"], "falsch")
        _set_retry_since_last(self.session.ctx, self.mode, RETRY_GAP_QUESTIONS)
        second = wrong["next"]["question"]
        following = await self.answer(second["token"], self.correct_for(second))
        repeat = following["next"]["question"]
        self.assertTrue(repeat["is_retry"])
        self.assertEqual(repeat["word_id"], started["question"]["word_id"])

    async def test_a_repeat_is_not_scored_a_second_time(self):
        word = self.make_due()
        started = await self.start()
        wrong = await self.answer(started["question"]["token"], "falsch")
        before = get_progress(self.db, USER, word["id"], self.mode)
        # The queue is empty, so the pending retry is drained straight away.
        repeat = wrong["next"]["question"]
        self.assertTrue(repeat["is_retry"])
        await self.answer(repeat["token"], "der Tisch")
        self.assertEqual(get_progress(self.db, USER, word["id"], self.mode), before)


class StopTest(QuizApiTestCase):
    async def test_stopping_reports_the_tally(self):
        self.make_due("Tisch", "стол")
        self.make_due("Haus", "дом")
        started = await self.start()
        body = await self.answer(started["question"]["token"], "falsch")
        following = body["next"]["question"]
        await self.answer(following["token"], self.correct_for(following))
        summary = (await (await self.post("stop")).json())["summary"]
        self.assertEqual(summary["answered"], 2)
        self.assertEqual(summary["wrong"], 1)
        self.assertEqual(summary["correct"], 1)

    async def test_a_mode_the_app_cannot_run_has_no_tally(self):
        # A learning session closes cleanly, it just has nothing to count yet (M6).
        await self.start(mode="learning")
        self.assertEqual(await (await self.post("stop")).json(), {"stopped": True})


class DeL1ApiTest(QuizApiTestCase):
    MODE = quiz_flow.DE_L1

    async def test_the_question_shows_the_word_with_its_forms(self):
        self.make_due("Tisch", "стол", plural="Tische")
        question = (await self.start())["question"]
        self.assertEqual(question["prompt"], "der Tisch")
        self.assertEqual(question["prompt_lang"], "de")
        self.assertEqual(question["forms"], {"plural": "Tische"})
        # The article is on screen here, so colouring by gender repeats it rather
        # than giving the answer away.
        self.assertTrue(question["hint"]["gender_known"])

    async def test_personal_media_travels_with_the_question(self):
        # The chat shows image/example with a De→L1 question, and so does the app.
        # A guest sees only their own (apply_user_word_media) — restoring the
        # COALESCE fallback to the shared example is an open task for both clients.
        word = self.make_due("Tisch", "стол")
        set_user_word_media(self.db, USER, word["id"], example="Der Tisch ist alt.")
        question = (await self.start())["question"]
        self.assertEqual(question["media"], {"example": "Der Tisch ist alt."})

    async def test_a_word_with_no_media_sends_no_empty_block(self):
        self.make_due("Tisch", "стол")
        self.assertNotIn("media", (await self.start())["question"])

    async def test_the_translation_is_the_expected_answer(self):
        self.make_due("Tisch", "стол, столик")
        token = (await self.start())["question"]["token"]
        body = await self.answer(token, "столик")
        self.assertTrue(body["correct"])
        self.assertEqual(body["feedback"]["expected"], "стол, столик")


class PlSgApiTest(QuizApiTestCase):
    MODE = quiz_flow.PL_SG

    async def test_the_plural_is_asked_and_the_singular_expected(self):
        self.make_due("Tisch", "стол", plural="Tische")
        question = (await self.start())["question"]
        self.assertEqual(question["prompt"], "die Tische")
        self.assertEqual(question["prompt_lang"], "de")
        self.assertFalse(question["hint"]["gender_known"])
        self.assertEqual(question["answer_forms"], {"main": "artikel Singular"})
        body = await self.answer(question["token"], "der Tisch")
        self.assertTrue(body["correct"])
        self.assertEqual(body["feedback"]["expected"], "der Tisch")


class QuestionSerializerTest(unittest.TestCase):
    """What a question shows, per direction and per word type (§7.4, §8.5a-bis)."""

    NOUN = {"id": "w1", "type": "nomen", "article": "der", "german": "tisch", "plural": "Tische", "russian": "стол"}
    VERB = {
        "id": "w2",
        "type": "verb",
        "german": "gehen",
        "infinitive": "gehen",
        "prateritum": "ging",
        "partizip2": "gegangen",
        "perfect_aux": "ist",
        "russian": "идти",
    }
    ADJ = {"id": "w3", "type": "adjektiv", "german": "schnell", "komparativ": "schneller", "superlativ": "am schnellsten", "russian": "быстрый"}
    PREP = {"id": "w4", "type": "praeposition", "german": "in", "preposition_type": "local", "case_type": "wechsel", "russian": "в"}

    def test_the_noun_is_capitalized_with_its_article(self):
        self.assertEqual(serializers.de_headword(self.NOUN), "der Tisch")

    def test_the_hint_carries_the_word_type(self):
        self.assertEqual(serializers.question_hint(self.VERB, quiz_flow.L1_DE)["type"], "verb")

    def test_a_noun_asked_into_german_may_not_be_coloured(self):
        # The article is what is being asked; colouring it in would answer it.
        self.assertFalse(serializers.question_hint(self.NOUN, quiz_flow.L1_DE)["gender_known"])
        self.assertFalse(serializers.question_hint(self.NOUN, quiz_flow.PL_SG)["gender_known"])

    def test_a_preposition_carries_its_type_and_case(self):
        hint = serializers.question_hint(self.PREP, quiz_flow.L1_DE)
        self.assertEqual(hint["preposition_type"], "local")
        self.assertEqual(hint["case_type"], "wechsel")

    def test_the_input_hint_names_the_forms_in_order(self):
        self.assertEqual(
            serializers.answer_forms(self.NOUN, quiz_flow.L1_DE), {"main": "artikel Singular Plural"}
        )
        self.assertEqual(
            serializers.answer_forms(self.VERB, quiz_flow.L1_DE),
            {"main": "infinitiv partizip II", "alt": "infinitiv präteritum partizip II"},
        )
        self.assertEqual(
            serializers.answer_forms(self.ADJ, quiz_flow.L1_DE),
            {"main": "adjektiv", "alt": "adjektiv komparativ superlativ"},
        )

    def test_a_noun_without_a_plural_does_not_ask_for_one(self):
        # The checker accepts two words for such a noun, so the hint must not name
        # a third one the user cannot supply.
        self.assertEqual(
            serializers.answer_forms({**self.NOUN, "plural": ""}, quiz_flow.L1_DE),
            {"main": "artikel Singular"},
        )

    def test_one_word_answers_get_no_input_hint(self):
        self.assertEqual(serializers.answer_forms(self.PREP, quiz_flow.L1_DE), {})
        self.assertEqual(serializers.answer_forms(self.NOUN, quiz_flow.DE_L1), {})

    def test_partizip2_is_shown_with_its_auxiliary(self):
        self.assertEqual(
            serializers.question_forms(self.VERB), {"prateritum": "ging", "partizip2": "ist gegangen"}
        )


class ExpectedAnswerTest(unittest.TestCase):
    """The expected answer is built from what the checker accepts, never from less."""

    def test_a_noun_names_its_plural_too(self):
        self.assertEqual(
            serializers.expected_answer(QuestionSerializerTest.NOUN, quiz_flow.L1_DE, "ru"),
            "der Tisch, die Tische",
        )

    def test_a_noun_without_a_plural_stops_at_the_singular(self):
        self.assertEqual(
            serializers.expected_answer(
                {**QuestionSerializerTest.NOUN, "plural": ""}, quiz_flow.L1_DE, "ru"
            ),
            "der Tisch",
        )

    def test_a_verb_names_all_three_forms(self):
        self.assertEqual(
            serializers.expected_answer(QuestionSerializerTest.VERB, quiz_flow.L1_DE, "ru"),
            "gehen, ging, ist gegangen",
        )

    def test_an_adjective_names_its_degrees(self):
        self.assertEqual(
            serializers.expected_answer(QuestionSerializerTest.ADJ, quiz_flow.L1_DE, "ru"),
            "schnell, schneller, am schnellsten",
        )

    def test_the_translation_follows_the_dictionary_language(self):
        entry = {**QuestionSerializerTest.NOUN, "ukrainian": "стіл"}
        self.assertEqual(serializers.expected_answer(entry, quiz_flow.DE_L1, "uk"), "стіл")

    def test_a_personal_translation_wins(self):
        # spec #7: a guest's own translation is what they were asked to produce.
        entry = {**QuestionSerializerTest.NOUN, "my_translation": "столик"}
        self.assertEqual(serializers.expected_answer(entry, quiz_flow.DE_L1, "ru"), "столик")


if __name__ == "__main__":
    unittest.main()
