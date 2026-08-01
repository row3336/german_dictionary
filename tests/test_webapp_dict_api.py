"""Mini App M7 step 2 — dictionary search and word card over HTTP
(miniapp.md §7.5, §11).

Covers the three search outcomes, the input limits that keep a lookup from
becoming a query the database has to think about, and the two things that must not
leak: another user's study status, and a query string reaching SQL as anything but
a parameter.
"""

import unittest

from _webapp_helpers import TOKEN, TempDbMixin, auth_header
from aiohttp.test_utils import AioHTTPTestCase

from bot.storage import add_word, set_user_word_media, update_progress
from bot.webapp.access import API_PREFIX
from bot.webapp.server import create_app
from bot.webapp.word_repo import MAX_QUERY_LENGTH, search_words

USER = 111
OTHER = 222


class SearchWordsTest(TempDbMixin, unittest.TestCase):
    def setUp(self):
        self.setup_temp_db()
        self.tisch = self.add_noun("Tisch", "стол", plural="Tische")

    def test_finds_a_word_by_its_german_form(self):
        found = search_words(self.db, "Tisch")
        self.assertEqual([e["german"] for e in found], ["Tisch"])

    def test_finds_a_word_by_its_translation(self):
        self.assertEqual(len(search_words(self.db, "стол")), 1)

    def test_finds_a_word_by_its_plural(self):
        self.assertEqual(len(search_words(self.db, "Tische")), 1)

    def test_ignores_a_leading_article(self):
        # The same normalization the bot applies, so "der Tisch" is not a miss.
        self.assertEqual(len(search_words(self.db, "der Tisch")), 1)

    def test_is_case_insensitive(self):
        self.assertEqual(len(search_words(self.db, "tisch")), 1)

    def test_returns_every_word_sharing_a_translation(self):
        # The bot's search_word takes LIMIT 1 and silently picks one of these; the
        # app is specified to offer the choice, so it has to see both.
        self.add_noun("Pult", "стол", article="das")
        found = search_words(self.db, "стол")
        self.assertEqual(sorted(e["german"] for e in found), ["Pult", "Tisch"])

    def test_falls_back_to_a_substring_match(self):
        self.assertEqual(len(search_words(self.db, "isc")), 1)

    def test_an_exact_match_wins_over_substrings(self):
        self.add_noun("Tischler", "столяр")
        found = search_words(self.db, "Tisch")
        self.assertEqual([e["german"] for e in found], ["Tisch"])

    def test_an_empty_query_never_reaches_the_database(self):
        for query in ("", "   ", None):
            with self.subTest(query=query):
                self.assertEqual(search_words(self.db, query), [])

    def test_nothing_matches_nothing(self):
        self.assertEqual(search_words(self.db, "Kraftfahrzeug"), [])

    def test_the_candidate_list_is_capped(self):
        for index in range(9):
            self.add_noun(f"Tisch{index}", f"стол {index}")
        self.assertLessEqual(len(search_words(self.db, "Tisch")), 5)

    def test_like_wildcards_are_treated_as_text(self):
        # Unescaped, a bare % matches the whole dictionary and _ matches almost it.
        # The user typed a character, not a pattern.
        self.assertEqual(search_words(self.db, "%"), [])
        self.assertEqual(search_words(self.db, "_"), [])
        self.assertEqual(search_words(self.db, "Ti%ch"), [])

    def test_a_quote_in_the_query_is_harmless(self):
        self.assertEqual(search_words(self.db, "'; DROP TABLE words; --"), [])
        self.assertEqual(len(search_words(self.db, "Tisch")), 1)


class SearchApiTest(TempDbMixin, AioHTTPTestCase):
    async def get_application(self):
        self.setup_temp_db()
        self.make_user(USER, "approved")
        self.make_user(OTHER, "approved")
        self.tisch = self.add_noun("Tisch", "стол", plural="Tische")
        return create_app(TOKEN)

    async def search(self, query, user_id=USER):
        return await self.client.get(
            f"{API_PREFIX}search", params={"q": query}, headers=auth_header(user_id)
        )

    async def test_one_match_returns_a_card(self):
        body = await (await self.search("Tisch")).json()
        self.assertEqual(body["result"], "card")
        self.assertEqual(body["card"]["german"], "Tisch")
        self.assertEqual(body["card"]["article"], "der")

    async def test_several_matches_return_a_choice(self):
        self.add_noun("Pult", "стол", article="das")
        body = await (await self.search("стол")).json()
        self.assertEqual(body["result"], "choice")
        self.assertEqual(len(body["candidates"]), 2)
        # A candidate line is a marker, not a card: no example, no study block.
        self.assertNotIn("media", body["candidates"][0])
        self.assertNotIn("study", body["candidates"][0])

    async def test_no_match_returns_empty(self):
        body = await (await self.search("Kraftfahrzeug")).json()
        self.assertEqual(body["result"], "empty")

    async def test_an_empty_query_returns_empty(self):
        body = await (await self.search("   ")).json()
        self.assertEqual(body, {"result": "empty", "query": ""})

    async def test_a_missing_parameter_is_not_an_error(self):
        resp = await self.client.get(f"{API_PREFIX}search", headers=auth_header(USER))
        self.assertEqual(resp.status, 200)
        self.assertEqual((await resp.json())["result"], "empty")

    async def test_an_overlong_query_is_trimmed_not_rejected(self):
        body = await (await self.search("x" * (MAX_QUERY_LENGTH + 50))).json()
        self.assertEqual(len(body["query"]), MAX_QUERY_LENGTH)
        self.assertEqual(body["result"], "empty")

    async def test_search_requires_authentication(self):
        resp = await self.client.get(f"{API_PREFIX}search", params={"q": "Tisch"})
        self.assertEqual(resp.status, 401)

    async def test_the_translation_follows_the_dictionary_language(self):
        from bot.storage import set_user_dict_lang

        word = add_word(
            self.db,
            {
                "id": "unique",
                "type": "nomen",
                "article": "die",
                "german": "Katze",
                "russian": "кошка",
                "ukrainian": "кішка",
                "english": "cat",
                "topic": "1",
            },
        )
        self.assertTrue(word["id"])
        set_user_dict_lang(self.db, USER, "uk")
        body = await (await self.search("Katze")).json()
        self.assertEqual(body["card"]["translation"], "кішка")


class WordApiTest(TempDbMixin, AioHTTPTestCase):
    async def get_application(self):
        self.setup_temp_db()
        self.make_user(USER, "approved")
        self.make_user(OTHER, "approved")
        self.word = add_word(
            self.db,
            {
                "id": "unique",
                "type": "nomen",
                "article": "der",
                "german": "Tisch",
                "plural": "Tische",
                "russian": "стол",
                "topic": "1",
                "example": "Der Tisch steht dort.",
            },
        )
        return create_app(TOKEN)

    async def get_word(self, word_id, user_id=USER):
        return await self.client.get(f"{API_PREFIX}word/{word_id}", headers=auth_header(user_id))

    async def test_returns_the_full_card(self):
        body = await (await self.get_word(self.word["id"])).json()
        self.assertEqual(body["card"]["german"], "Tisch")
        self.assertEqual(body["card"]["media"]["example"], "Der Tisch steht dort.")
        self.assertEqual(body["card"]["study"]["state"], "queue_learning")

    async def test_an_unknown_id_is_a_404(self):
        resp = await self.get_word("no-such-word")
        self.assertEqual(resp.status, 404)
        self.assertEqual((await resp.json())["error"], "word_not_found")

    async def test_requires_authentication(self):
        resp = await self.client.get(f"{API_PREFIX}word/{self.word['id']}")
        self.assertEqual(resp.status, 401)

    async def test_study_status_is_per_user(self):
        update_progress(self.db, user_id=OTHER, word_id=self.word["id"], mode="ru_de", quality=5)
        mine = await (await self.get_word(self.word["id"])).json()
        theirs = await (await self.get_word(self.word["id"], OTHER)).json()
        self.assertEqual(mine["card"]["study"]["state"], "queue_learning")
        self.assertEqual(theirs["card"]["study"]["state"], "fsrs")

    async def test_personal_media_is_per_user(self):
        set_user_word_media(self.db, OTHER, self.word["id"], example="Mein Tisch.")
        mine = await (await self.get_word(self.word["id"])).json()
        theirs = await (await self.get_word(self.word["id"], OTHER)).json()
        self.assertEqual(mine["card"]["media"]["example"], "Der Tisch steht dort.")
        self.assertEqual(theirs["card"]["media"]["example"], "Mein Tisch.")

    async def test_a_pending_user_gets_the_gate_not_the_card(self):
        self.make_user(333, "pending")
        resp = await self.get_word(self.word["id"], 333)
        self.assertEqual(resp.status, 403)


if __name__ == "__main__":
    unittest.main()
