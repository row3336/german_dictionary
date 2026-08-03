"""Mini App §4.4 — one question, one token, one write (miniapp.md §11).

The risk this covers is not a crash: a double tap on "answer" or a network retry on
a flaky mobile connection would score the same word twice, and the user would never
see it happen — the interval would just be wrong (§12, risk 2). So the assertions
here are about the database, not about the status code: after two identical
requests there has to be exactly one row in `quiz_events` and one FSRS step in
`progress`.
"""

import sqlite3
import unittest

from _webapp_helpers import TOKEN, TempDbMixin, auth_header
from aiohttp.test_utils import AioHTTPTestCase

from bot.storage import add_word, get_progress, log_quiz_event
from bot.webapp import quiz_flow
from bot.webapp.access import API_PREFIX

USER = 808


class QuestionTokenTest(TempDbMixin, AioHTTPTestCase):
    async def get_application(self):
        self.setup_temp_db()
        self.make_user(USER, "approved")
        from bot.webapp.server import create_app

        return create_app(TOKEN)

    async def asyncSetUp(self):
        # Seeded here, not in setUp: the temp database is created by
        # get_application, which aiohttp runs as part of the async setup.
        await super().asyncSetUp()
        self.mode = quiz_flow.mode_for(USER, quiz_flow.L1_DE)
        self.word = self.make_due()

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
        log_quiz_event(self.db, USER, word["id"], self.mode, False)
        return word

    # -- helpers ---------------------------------------------------------

    async def start(self):
        resp = await self.client.post(
            f"{API_PREFIX}session/start", headers=auth_header(USER), json={"mode": "l1_de"}
        )
        return (await resp.json())["question"]["token"]

    async def answer(self, token, text="der Tisch"):
        return await self.client.post(
            f"{API_PREFIX}session/answer",
            headers=auth_header(USER),
            json={"question_token": token, "text": text},
        )

    def event_count(self):
        with sqlite3.connect(self.db) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM quiz_events WHERE user_id = ? AND word_id = ? AND mode = ?",
                (USER, self.word["id"], self.mode),
            ).fetchone()[0]

    # -- tests -----------------------------------------------------------

    async def test_the_same_token_cannot_be_spent_twice(self):
        token = await self.start()
        first = await self.answer(token)
        second = await self.answer(token)
        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 409)
        self.assertEqual((await second.json())["error"], "stale_question")

    async def test_a_repeated_answer_writes_nothing_the_second_time(self):
        token = await self.start()
        await self.answer(token)
        after_one = get_progress(self.db, USER, self.word["id"], self.mode)
        events_after_one = self.event_count()
        await self.answer(token)
        self.assertEqual(get_progress(self.db, USER, self.word["id"], self.mode), after_one)
        self.assertEqual(self.event_count(), events_after_one)

    async def test_the_seed_event_aside_one_answer_is_one_event(self):
        token = await self.start()
        await self.answer(token)
        # The seeding event from setUp plus this one answer, and nothing else.
        self.assertEqual(self.event_count(), 2)

    async def test_an_unknown_token_is_refused_before_anything_is_scored(self):
        await self.start()
        resp = await self.answer("not-the-token")
        self.assertEqual(resp.status, 409)
        self.assertEqual(self.event_count(), 1)  # only the seed event
        self.assertEqual(get_progress(self.db, USER, self.word["id"], self.mode)["repetitions"], 0)

    async def test_every_question_gets_its_own_token(self):
        self.make_due("Haus", "дом", article="das")
        first_token = await self.start()
        body = await (await self.answer(first_token)).json()
        self.assertNotEqual(body["next"]["question"]["token"], first_token)

    async def test_reopening_the_app_keeps_the_outstanding_token(self):
        # A WebView that was minimized and reopened must be able to answer the
        # question it still has on screen (§7.2).
        token = await self.start()
        await self.client.get(f"{API_PREFIX}session/question", headers=auth_header(USER))
        self.assertEqual((await self.answer(token)).status, 200)


if __name__ == "__main__":
    unittest.main()
