"""Mini App M2 — GET /api/v1/me (miniapp.md §7.1, §11).

The main screen is built from this one response, so the tests cover both halves of
it: the identity/settings block, and the counters behind every mode tile. The
counters are the part that can drift — they must come out the same as the numbers
the bot would show, under the same active filter, which is why they are asserted
against words in a real database rather than against a mock.

Also covers isolation (risk #6): one user's counters must not move when another
user studies.
"""

import unittest

from _webapp_helpers import TOKEN, TempDbMixin, auth_header
from aiohttp.test_utils import AioHTTPTestCase

from bot.webapp.access import API_PREFIX
from bot.webapp.i18n_strings import APP_TEXTS
from bot.webapp.overview import MODES, build_me, topic_filter_value
from bot.webapp.server import create_app
from bot.storage import (
    learning_add_words,
    learning_get_or_create_batch,
    log_quiz_event,
    set_topic_level,
    set_user_dict_lang,
    set_user_interface_lang,
    set_user_setting,
)
from bot.app_config import TOPIC_FILTER_KEY

USER = 111
OTHER = 222


class MePayloadTest(TempDbMixin, unittest.TestCase):
    def setUp(self):
        self.setup_temp_db()
        self.make_user(USER, "approved")

    def _release_to_fsrs(self, word, user_id=USER):
        """Make a word eligible for FSRS the way the bot does.

        select_due only offers words with a finished learning row or some
        non-learning history - a freshly added word is not due for anybody. The
        event is a wrong answer on purpose: FSRS counters hide what was already
        answered correctly today, so a correct one would make the word not due.
        """
        log_quiz_event(self.db, user_id, word["id"], "ru_de", False)

    def test_reports_identity_and_settings(self):
        set_user_interface_lang(self.db, USER, "uk")
        set_user_dict_lang(self.db, USER, "en")
        payload = build_me(USER)
        self.assertEqual(payload["user_id"], USER)
        self.assertFalse(payload["is_owner"])
        self.assertEqual(payload["interface_lang"], "uk")
        self.assertEqual(payload["dict_lang"], "en")

    def test_marks_the_owner(self):
        self.make_user(self.OWNER_ID, "approved")
        self.assertTrue(build_me(self.OWNER_ID)["is_owner"])

    def test_lists_every_v1_mode(self):
        self.assertEqual(
            build_me(USER)["modes"],
            [
                "l1_de",
                "de_l1",
                "pl_sg",
                "weak_article",
                "weak_plural",
                "weak_prateritum",
                "weak_partizip2",
                "learning",
            ],
        )

    def test_due_has_a_counter_for_every_quiz_mode(self):
        due = build_me(USER)["due"]
        self.assertEqual(set(due), set(MODES) - {"learning"})
        self.assertTrue(all(isinstance(value, int) for value in due.values()))

    def test_counts_words_that_are_actually_due(self):
        self.assertEqual(build_me(USER)["due"]["l1_de"], 0)
        word = self.add_noun("Tisch", "стол")
        self.assertEqual(build_me(USER)["due"]["l1_de"], 0)  # never studied yet
        self._release_to_fsrs(word)
        self.assertEqual(build_me(USER)["due"]["l1_de"], 1)

    def test_pl_sg_counts_only_nouns(self):
        noun = self.add_noun("Tisch", "стол")
        self._release_to_fsrs(noun)
        self.assertEqual(build_me(USER)["due"]["pl_sg"], 1)

    def test_the_topic_filter_narrows_the_counters(self):
        first = self.add_noun("Tisch", "стол", topic="1")
        second = self.add_noun("Katze", "кошка", topic="2")
        # resolve_quiz_filter intersects the selection with the topics of the active
        # book level, so both topics have to belong to A1 for the filter to bite.
        set_topic_level(self.db, "1", "A1")
        set_topic_level(self.db, "2", "A1")
        self._release_to_fsrs(first)
        self._release_to_fsrs(second)
        self.assertEqual(build_me(USER)["due"]["l1_de"], 2)
        set_user_setting(self.db, USER, TOPIC_FILTER_KEY, "1")
        payload = build_me(USER)
        self.assertEqual(payload["topic_filter"], "1")
        self.assertEqual(payload["due"]["l1_de"], 1)

    def test_an_unset_topic_filter_reads_as_all(self):
        self.assertEqual(topic_filter_value(USER), "all")
        self.assertEqual(build_me(USER)["topic_filter"], "all")

    def test_learning_block_reports_active_items(self):
        payload = build_me(USER)
        self.assertEqual(payload["learning"], {"active": 0, "batch_ready": False})
        word = self.add_noun("Tisch", "стол")
        batch = learning_get_or_create_batch(self.db, user_id=USER, daily_qty=20)
        learning_add_words(
            self.db, user_id=USER, words=[word], batch_id=batch, first_delay_seconds=30
        )
        self.assertEqual(build_me(USER)["learning"]["active"], 1)

    def test_words_in_learning_are_not_counted_as_due(self):
        # The two blocks must not double-count the same word - that is exactly the
        # split select_due enforces for the bot.
        word = self.add_noun("Tisch", "стол")
        self._release_to_fsrs(word)
        batch = learning_get_or_create_batch(self.db, user_id=USER, daily_qty=20)
        learning_add_words(
            self.db, user_id=USER, words=[word], batch_id=batch, first_delay_seconds=30
        )
        payload = build_me(USER)
        self.assertEqual(payload["due"]["l1_de"], 0)
        self.assertEqual(payload["learning"]["active"], 1)

    def test_strings_come_in_the_users_interface_language(self):
        set_user_interface_lang(self.db, USER, "ru")
        strings = build_me(USER)["strings"]
        self.assertEqual(strings["action_next"], APP_TEXTS["ru"]["action_next"])
        set_user_interface_lang(self.db, USER, "uk")
        self.assertEqual(
            build_me(USER)["strings"]["action_next"], APP_TEXTS["uk"]["action_next"]
        )

    def test_no_string_is_left_untranslated(self):
        # A key present in en but missing from ru/uk would reach the user as English
        # inside an otherwise translated screen.
        for lang in ("ru", "uk"):
            with self.subTest(lang=lang):
                self.assertEqual(set(APP_TEXTS[lang]), set(APP_TEXTS["en"]))
                self.assertTrue(all(APP_TEXTS[lang].values()))


class MeIsolationTest(TempDbMixin, unittest.TestCase):
    """Risk #6: one user's numbers must never include another user's progress."""

    def setUp(self):
        self.setup_temp_db()
        self.make_user(USER, "approved")
        self.make_user(OTHER, "approved")

    def test_counters_are_per_user(self):
        word = self.add_noun("Tisch", "стол")
        log_quiz_event(self.db, OTHER, word["id"], "ru_de", False)
        self.assertEqual(build_me(USER)["due"]["l1_de"], 0)
        self.assertEqual(build_me(OTHER)["due"]["l1_de"], 1)

    def test_learning_is_per_user(self):
        word = self.add_noun("Tisch", "стол")
        batch = learning_get_or_create_batch(self.db, user_id=OTHER, daily_qty=20)
        learning_add_words(
            self.db, user_id=OTHER, words=[word], batch_id=batch, first_delay_seconds=30
        )
        self.assertEqual(build_me(USER)["learning"]["active"], 0)
        self.assertEqual(build_me(OTHER)["learning"]["active"], 1)

    def test_the_topic_filter_is_per_user(self):
        set_user_setting(self.db, OTHER, TOPIC_FILTER_KEY, "3")
        self.assertEqual(topic_filter_value(USER), "all")
        self.assertEqual(topic_filter_value(OTHER), "3")


class MeOverHttpTest(TempDbMixin, AioHTTPTestCase):
    async def get_application(self):
        self.setup_temp_db()
        self.make_user(USER, "approved")
        return create_app(TOKEN)

    async def test_serves_the_payload_for_the_signed_user(self):
        resp = await self.client.get(f"{API_PREFIX}me", headers=auth_header(USER))
        self.assertEqual(resp.status, 200)
        payload = await resp.json()
        self.assertEqual(payload["user_id"], USER)
        self.assertIn("strings", payload)
        self.assertIn("due", payload)

    async def test_requires_authentication(self):
        self.assertEqual((await self.client.get(f"{API_PREFIX}me")).status, 401)

    async def test_never_returns_the_bot_token(self):
        body = await (await self.client.get(f"{API_PREFIX}me", headers=auth_header(USER))).text()
        self.assertNotIn(TOKEN, body)
        self.assertNotIn(TOKEN.split(":", 1)[1], body)

    async def test_a_pending_user_gets_the_gate_code_not_the_payload(self):
        self.make_user(OTHER, "pending")
        resp = await self.client.get(f"{API_PREFIX}me", headers=auth_header(OTHER))
        self.assertEqual(resp.status, 403)
        self.assertEqual(await resp.json(), {"error": "access_pending"})


if __name__ == "__main__":
    unittest.main()
