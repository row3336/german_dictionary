"""Mini App M6 — the learning batch over HTTP (miniapp.md §7.3, §7.4, §11).

The mode with two phases, so most of what is checked here is the seam between
them: a preview card is read and not answered, a question is answered and not
read, and every route that moves the session between the two spends a question
token on the way.

The rest is the batch itself — topping it up, dropping a word from it, sending a
word into it from elsewhere, and the one ending that matters: the last correct
answer completes the word and hands it over to FSRS.

Time is moved rather than waited for: the ladder is measured in seconds, and a
test that slept through it would take longer than the whole suite.
"""

import sqlite3
import unittest

from _webapp_helpers import TOKEN, TempDbMixin, auth_header
from aiohttp.test_utils import AioHTTPTestCase

from bot.app_config import LEARNING_DAILY_QTY_KEY, LEARNING_INTERVALS_KEY
from bot.storage import add_word, set_user_setting
from bot.webapp.access import API_PREFIX
from bot.webapp.app_keys import REGISTRY_KEY

USER = 707

# Warm-up steps (0, 0, 0) plus this one interval: four steps to complete a word,
# short enough to walk through and long enough that nothing is due by accident.
INTERVALS = "60"
LADDER_STEPS = 4

NOUN = {"type": "nomen", "article": "der", "german": "Tisch", "plural": "Tische", "russian": "стол"}
VERB = {
    "type": "verb",
    "german": "gehen",
    "infinitive": "gehen",
    "prateritum": "ging",
    "partizip2": "gegangen",
    "russian": "идти",
}


class LearningApiTestCase(TempDbMixin, AioHTTPTestCase):
    """One approved user, a seeded dictionary, and helpers to drive the routes."""

    async def get_application(self):
        self.setup_temp_db()
        self.make_user(USER, "approved")
        from bot.webapp.server import create_app

        return create_app(TOKEN)

    async def asyncSetUp(self):
        # The temp database only exists once get_application has run, so every
        # setting written here has to wait for the async phase.
        await super().asyncSetUp()
        set_user_setting(self.db, USER, LEARNING_INTERVALS_KEY, INTERVALS)

    # -- fixtures --------------------------------------------------------

    def add_word_row(self, fields=None, suffix=""):
        infinitive = (fields or NOUN).get("infinitive", "")
        fields = fields or NOUN
        return add_word(
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

    async def seed_batch(self, count=1, fields=None):
        """`count` words in the dictionary, all of them pulled into the batch."""
        words = [self.add_word_row(fields, suffix=str(index) if index else "") for index in range(count)]
        await self.fill()
        return words

    def learning_rows(self):
        """This user's learning_items, in insertion order."""
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM learning_items WHERE user_id = ? ORDER BY created_at, rowid",
                    (USER,),
                )
            ]

    def learning_row(self, word_id):
        return next((row for row in self.learning_rows() if row["word_id"] == word_id), None)

    def quiz_events(self, word_id=None):
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            sql = "SELECT * FROM quiz_events WHERE user_id = ? AND mode = 'learning'"
            params = [USER]
            if word_id:
                sql += " AND word_id = ?"
                params.append(word_id)
            return [dict(row) for row in conn.execute(sql + " ORDER BY id", params)]

    def make_due_now(self):
        """Bring every waiting word forward instead of sleeping through the ladder."""
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "UPDATE learning_items SET next_due = '2000-01-01T00:00:00Z' "
                "WHERE user_id = ? AND status = 'active'",
                (USER,),
            )

    # -- requests --------------------------------------------------------

    async def post(self, path, **body):
        return await self.client.post(f"{API_PREFIX}{path}", headers=auth_header(USER), json=body)

    async def start(self):
        return await (await self.post("session/start", mode="learning")).json()

    async def fill(self):
        return await (await self.post("learning/fill")).json()

    async def next_card(self, token):
        return await (await self.post("session/next", question_token=token)).json()

    async def skip(self, token):
        return await (await self.post("session/skip", question_token=token)).json()

    async def answer(self, token, text):
        return await (await self.post("session/answer", question_token=token, text=text)).json()

    async def preview_all(self):
        """Walk the preview phase to its end and return the first quiz question."""
        body = await self.start()
        question = body.get("question")
        while question and question["phase"] == "preview":
            body = await self.next_card(question["token"])
            question = body["next"].get("question")
        return question

    @property
    def session(self):
        return self.app[REGISTRY_KEY].require(USER)


class QuestionShapeTest(LearningApiTestCase):
    async def test_a_preview_card_is_read_rather_than_answered(self):
        [word] = await self.seed_batch()
        question = (await self.start())["question"]

        self.assertEqual(question["mode"], "learning")
        self.assertEqual(question["phase"], "preview")
        self.assertEqual(question["word_id"], word["id"])
        # The word being introduced, article and all — not a word being tested.
        self.assertEqual(question["prompt"], "der Tisch")
        self.assertEqual(question["prompt_lang"], "de")
        self.assertEqual(question["answer_kind"], "none")
        self.assertTrue(question["hint"]["gender_known"])
        self.assertNotIn("answer_forms", question)
        # The whole card, so the client shows the same component as the dictionary.
        self.assertEqual(question["card"]["translation"], "стол")
        self.assertEqual(question["card"]["forms"], {"plural": "Tische"})

    async def test_a_learning_question_is_asked_like_l1_de(self):
        await self.seed_batch()
        question = await self.preview_all()

        self.assertEqual(question["mode"], "learning")
        self.assertEqual(question["phase"], "quiz")
        self.assertEqual(question["prompt"], "стол")
        self.assertEqual(question["prompt_lang"], "ru")
        self.assertEqual(question["answer_kind"], "text")
        self.assertEqual(question["answer_forms"], {"main": "artikel Singular Plural"})
        # The article is the answer here, so the word must not be coloured by gender.
        self.assertFalse(question["hint"]["gender_known"])
        self.assertNotIn("card", question)

    async def test_learning_marks_neither_retries_nor_drills(self):
        await self.seed_batch()
        question = await self.preview_all()
        self.assertFalse(question["is_retry"])
        self.assertFalse(question["is_drill"])

    async def test_the_counter_shrinks_as_words_are_worked_through(self):
        await self.seed_batch(count=2)
        question = (await self.start())["question"]
        self.assertEqual(question["progress"], {"answered": 0, "total_due": 2})

        question = await self.preview_all()
        self.assertEqual(question["progress"]["answered"], 0)
        body = await self.answer(question["token"], "der Tisch Tische")
        self.assertEqual(body["next"]["question"]["progress"]["answered"], 1)


class PreviewPhaseTest(LearningApiTestCase):
    async def test_next_reviews_the_card_and_moves_to_the_following_one(self):
        words = await self.seed_batch(count=2)
        first = (await self.start())["question"]
        body = await self.next_card(first["token"])
        second = body["next"]["question"]

        self.assertEqual(second["phase"], "preview")
        self.assertNotEqual(second["word_id"], first["word_id"])
        self.assertEqual({first["word_id"], second["word_id"]}, {w["id"] for w in words})
        self.assertEqual(self.learning_row(first["word_id"])["preview_done"], 1)
        self.assertEqual(self.learning_row(second["word_id"])["preview_done"], 0)

    async def test_the_quiz_phase_starts_once_every_card_is_reviewed(self):
        await self.seed_batch(count=2)
        question = await self.preview_all()
        self.assertEqual(question["phase"], "quiz")
        self.assertTrue(all(row["preview_done"] == 1 for row in self.learning_rows()))

    async def test_answering_a_preview_card_is_refused(self):
        # There is no verdict to give, and scoring one would mark a word the user
        # has not been asked about yet.
        await self.seed_batch()
        question = (await self.start())["question"]
        resp = await self.post("session/answer", question_token=question["token"], text="der Tisch")

        self.assertEqual(resp.status, 409)
        self.assertEqual((await resp.json())["error"], "stale_question")
        self.assertEqual(self.quiz_events(), [])

    async def test_a_spent_token_cannot_review_a_second_card(self):
        await self.seed_batch(count=2)
        question = (await self.start())["question"]
        await self.next_card(question["token"])
        resp = await self.post("session/next", question_token=question["token"])

        self.assertEqual(resp.status, 409)
        self.assertEqual((await resp.json())["error"], "stale_question")
        self.assertEqual(sum(row["preview_done"] for row in self.learning_rows()), 1)

    async def test_the_preview_routes_refuse_a_session_in_another_mode(self):
        self.add_word_row()
        await self.post("session/start", mode="l1_de")
        for route in ("session/next", "session/skip"):
            with self.subTest(route=route):
                resp = await self.post(route, question_token="whatever")
                self.assertEqual(resp.status, 400)
                self.assertEqual((await resp.json())["error"], "wrong_mode")


class SkipTest(LearningApiTestCase):
    async def test_skipping_a_preview_card_drops_the_word_from_the_batch(self):
        words = await self.seed_batch(count=2)
        question = (await self.start())["question"]
        body = await self.skip(question["token"])

        self.assertEqual(self.learning_row(question["word_id"])["status"], "skipped")
        following = body["next"]["question"]
        self.assertNotEqual(following["word_id"], question["word_id"])
        self.assertIn(following["word_id"], {w["id"] for w in words})

    async def test_skipping_a_question_drops_the_word_too(self):
        await self.seed_batch(count=2)
        question = await self.preview_all()
        await self.skip(question["token"])

        self.assertEqual(self.learning_row(question["word_id"])["status"], "skipped")
        # Skipping is not an answer: nothing is logged and nothing is graded.
        self.assertEqual(self.quiz_events(question["word_id"]), [])

    async def test_skipping_the_last_word_ends_the_session(self):
        await self.seed_batch()
        question = (await self.start())["question"]
        body = await self.skip(question["token"])
        self.assertEqual(body["next"]["empty"], "no_due")


class AnswerTest(LearningApiTestCase):
    async def test_a_right_answer_is_scored_and_the_ladder_moves_up(self):
        await self.seed_batch()
        question = await self.preview_all()
        body = await self.answer(question["token"], "der Tisch Tische")

        self.assertTrue(body["correct"])
        self.assertEqual(body["feedback"]["expected"], "der Tisch, die Tische")
        self.assertEqual(body["feedback"]["card"]["id"], question["word_id"])
        # Learning reactions are flat in the chat, so there is no streak to report.
        self.assertEqual(body["feedback"]["streak"], 0)
        self.assertNotIn("completed", body["feedback"])
        self.assertEqual(self.learning_row(question["word_id"])["step_index"], 1)
        self.assertEqual([bool(e["is_correct"]) for e in self.quiz_events()], [True])

    async def test_a_wrong_answer_sends_the_word_back_to_the_first_step(self):
        await self.seed_batch()
        question = await self.preview_all()
        await self.answer(question["token"], "der Tisch Tische")
        self.make_due_now()

        question = (await self.start())["question"]
        body = await self.answer(question["token"], "die Tisch Tische")
        self.assertFalse(body["correct"])
        self.assertEqual(self.learning_row(question["word_id"])["step_index"], 0)

    async def test_i_dont_know_is_a_wrong_answer_that_shows_the_word(self):
        await self.seed_batch()
        question = await self.preview_all()
        body = await (await self.post("session/idk", question_token=question["token"])).json()

        self.assertFalse(body["correct"])
        self.assertEqual(body["feedback"]["expected"], "der Tisch, die Tische")
        self.assertEqual([bool(e["is_correct"]) for e in self.quiz_events()], [False])

    async def test_the_last_step_completes_the_word_into_fsrs(self):
        await self.seed_batch()
        question = await self.preview_all()
        body = None
        for _ in range(LADDER_STEPS):
            body = await self.answer(question["token"], "der Tisch Tische")
            self.make_due_now()
            question = body["next"].get("question") or (await self.start()).get("question")

        self.assertTrue(body["feedback"]["completed"])
        self.assertEqual(self.learning_row(body["feedback"]["card"]["id"])["status"], "completed")

    async def test_a_typo_can_be_taken_back(self):
        await self.seed_batch()
        question = await self.preview_all()
        await self.answer(question["token"], "der Tish Tische")
        applied = await (await self.post("session/input_err")).json()

        self.assertTrue(applied["applied"])
        # The wrong answer dropped the word to step 0; the rollback re-advances it
        # from the step it was on when the question was asked.
        self.assertEqual(self.learning_row(question["word_id"])["step_index"], 1)
        self.assertEqual([bool(e["is_correct"]) for e in self.quiz_events()], [False, True])

    async def test_input_err_does_nothing_after_a_correct_answer(self):
        await self.seed_batch()
        question = await self.preview_all()
        await self.answer(question["token"], "der Tisch Tische")
        applied = await (await self.post("session/input_err")).json()
        self.assertFalse(applied["applied"])


class EndOfQueueTest(LearningApiTestCase):
    async def test_a_batch_with_nothing_due_says_when_to_come_back(self):
        await self.seed_batch()
        question = await self.preview_all()
        # The three warm-up steps are due immediately, so the word keeps coming back
        # until the ladder reaches its first real interval.
        for _ in range(LADDER_STEPS - 1):
            body = await self.answer(question["token"], "der Tisch Tische")
            question = body["next"].get("question")

        self.assertEqual(body["next"]["empty"], "no_due")
        self.assertTrue(body["next"]["next_due"])

    async def test_an_empty_dictionary_is_a_different_ending(self):
        # Nothing to learn at all is not "come back later": the fix is to pick other
        # topics, and the chat says so too.
        body = await self.start()
        self.assertEqual(body["empty"], "no_words")
        self.assertIsNone(body["question"])

    async def test_a_learning_session_ends_without_a_summary(self):
        # The chat has none here, and the app inherits the chat (as in weak).
        await self.seed_batch()
        await self.preview_all()
        body = await (await self.post("session/stop")).json()
        self.assertTrue(body["stopped"])
        self.assertNotIn("summary", body)


class BatchTest(LearningApiTestCase):
    async def test_filling_pulls_new_words_up_to_the_daily_quota(self):
        set_user_setting(self.db, USER, LEARNING_DAILY_QTY_KEY, "2")
        for index in range(3):
            self.add_word_row(suffix=str(index))
        body = await self.fill()

        self.assertEqual(body["added"], 2)
        self.assertEqual(body["active"], 2)
        self.assertEqual(body["daily"], 2)
        self.assertNotIn("reason", body)
        self.assertEqual(len(self.learning_rows()), 2)

    async def test_a_full_batch_says_so_rather_than_growing(self):
        set_user_setting(self.db, USER, LEARNING_DAILY_QTY_KEY, "1")
        for index in range(2):
            self.add_word_row(suffix=str(index))
        await self.fill()
        body = await self.fill()

        self.assertEqual(body["added"], 0)
        self.assertEqual(body["reason"], "batch_full")
        self.assertEqual(len(self.learning_rows()), 1)

    async def test_an_exhausted_dictionary_says_so_too(self):
        self.add_word_row()
        await self.fill()
        # The only word is already in the batch, and the quota is nowhere near full.
        body = await self.fill()
        self.assertEqual(body["added"], 0)
        self.assertEqual(body["reason"], "no_new")


class SendLearnTest(LearningApiTestCase):
    async def test_a_word_can_be_sent_into_the_batch_by_id(self):
        word = self.add_word_row(VERB)
        body = await (await self.post("session/send_learn", word_id=word["id"])).json()

        self.assertTrue(body["added"])
        row = self.learning_row(word["id"])
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["preview_done"], 0)

    async def test_an_unknown_word_is_refused(self):
        # learning_add_word_by_id upserts blindly; a row that joins to no word would
        # break the preview query for good.
        resp = await self.post("session/send_learn", word_id="no-such-word")
        self.assertEqual(resp.status, 400)
        self.assertEqual((await resp.json())["error"], "unknown_word")
        self.assertEqual(self.learning_rows(), [])

    async def test_send_learn_needs_a_word_id(self):
        resp = await self.post("session/send_learn")
        self.assertEqual(resp.status, 400)
        self.assertEqual((await resp.json())["error"], "unknown_word")


if __name__ == "__main__":
    unittest.main()
