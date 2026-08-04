"""Mini App M8 — the topic filter (miniapp.md §7.6, §11).

The one setting the app is allowed to WRITE, which makes the contract here narrower
than it looks: the filter string belongs to the bot (`topic_filter` in
`user_settings`), and both clients have to keep reading it the same way. So most of
this file is about agreement rather than about the endpoint — what the app shows is
what `resolve_quiz_filter` will apply, and what the chat sets is what the app draws.

§7.6 predates `spec_topics_levels.md`, and the owner confirmed the newer model on
2026-08-04: the list is the active book-level's topics, and applying a selection
also switches the filter branch to 'topics'. Both are pinned below, because both are
invisible on today's data (every prod topic is A1) and would rot silently.
"""

import unittest

from _webapp_helpers import TOKEN, TempDbMixin, auth_header
from aiohttp.test_utils import AioHTTPTestCase

from bot.app_config import TOPIC_FILTER_KEY, get_db_path
from bot.common.word_filter import (
    MODE_CEFR,
    MODE_TOPICS,
    get_filter_mode,
    resolve_quiz_filter,
    set_active_book_level,
    set_filter_mode,
)
from bot.storage import get_user_setting, set_topic_level, set_user_setting
from bot.webapp.access import API_PREFIX
from bot.webapp.server import create_app
from bot.webapp.topics_api import EMPTY_TOPICS, INVALID_VALUE, UNKNOWN_TOPICS

USER = 111
OTHER = 222
PENDING = 333

# Names chosen so canonical order (spec #8) differs from plain string sort: the
# leading-zero group comes first, then plain numerics, then words.
A1_TOPICS = {"0": 2, "03": 1, "1": 3, "2": 1, "verb": 1}
A2_TOPICS = {"9": 1}


class TopicsApiTest(TempDbMixin, AioHTTPTestCase):
    async def get_application(self):
        # Seeded here, not in setUp: get_application is what creates the temp DB,
        # and aiohttp only calls it inside the async phase.
        self.setup_temp_db()
        for user in (USER, OTHER):
            self.make_user(user, "approved")
        self.make_user(PENDING, "pending")
        for topic, count in {**A1_TOPICS, **A2_TOPICS}.items():
            for i in range(count):
                self.add_noun(f"Wort{topic}x{i}", f"слово{topic}{i}", topic=topic)
        # Topics are seeded by init_db with an empty level; assigning one is a
        # deliberate backfill (storage.py), which is why the tests do it explicitly.
        for topic in A1_TOPICS:
            set_topic_level(self.db, topic, "A1")
        for topic in A2_TOPICS:
            set_topic_level(self.db, topic, "A2")
        return create_app(TOKEN)

    async def get_topics(self, user_id=USER):
        return await self.client.get(f"{API_PREFIX}topics", headers=auth_header(user_id))

    async def post_topics(self, value, user_id=USER):
        return await self.client.post(
            f"{API_PREFIX}topics", headers=auth_header(user_id), json={"value": value}
        )

    def stored_filter(self, user_id=USER):
        return get_user_setting(get_db_path(), user_id, TOPIC_FILTER_KEY, "")

    # -- GET ---------------------------------------------------------------

    async def test_lists_the_active_levels_topics_with_counts(self):
        body = await (await self.get_topics()).json()
        self.assertEqual(body["mode"], MODE_TOPICS)
        self.assertEqual(body["level"], "A1")
        self.assertEqual(body["total_words"], sum(A1_TOPICS.values()))
        self.assertEqual(
            {t["name"]: t["count"] for t in body["topics"]},
            A1_TOPICS,
        )

    async def test_chips_are_in_canonical_order_not_string_order(self):
        body = await (await self.get_topics()).json()
        # leading-zero numerics, then plain numerics, then words (common/topics.py)
        self.assertEqual([t["name"] for t in body["topics"]], ["0", "03", "1", "2", "verb"])

    async def test_all_means_every_chip_is_selected(self):
        set_user_setting(self.db, USER, TOPIC_FILTER_KEY, "all")
        body = await (await self.get_topics()).json()
        self.assertTrue(all(t["selected"] for t in body["topics"]))
        self.assertEqual(body["current"], "all")

    async def test_an_unset_filter_reads_as_the_whole_level(self):
        """Regression: resolve_active('') means "nothing", the resolver means "all".

        Most prod users have no `topic_filter` row at all, so reading the raw value
        without normalising it first showed them an empty screen over a filter that
        was in fact wide open.
        """
        self.assertEqual(self.stored_filter(), "")
        body = await (await self.get_topics()).json()
        self.assertEqual(body["current"], "all")
        self.assertTrue(all(t["selected"] for t in body["topics"]))

    async def test_a_stale_topic_in_the_stored_value_is_dropped(self):
        set_user_setting(self.db, USER, TOPIC_FILTER_KEY, "1+gone")
        body = await (await self.get_topics()).json()
        self.assertEqual([t["name"] for t in body["topics"] if t["selected"]], ["1"])

    # -- POST --------------------------------------------------------------

    async def test_applying_a_selection_stores_it_and_reports_the_count(self):
        resp = await self.post_topics("0+1")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["current"], "0+1")
        self.assertEqual(body["words"], A1_TOPICS["0"] + A1_TOPICS["1"])
        self.assertEqual(self.stored_filter(), "0+1")
        self.assertEqual(
            [t["name"] for t in body["topics"] if t["selected"]], ["0", "1"]
        )

    async def test_selecting_every_topic_collapses_to_all(self):
        """So a topic added later joins the filter instead of being invisible."""
        resp = await self.post_topics("+".join(A1_TOPICS))
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.stored_filter(), "all")

    async def test_an_empty_selection_is_refused(self):
        set_user_setting(self.db, USER, TOPIC_FILTER_KEY, "1")
        resp = await self.post_topics("+ , +")
        self.assertEqual(resp.status, 400)
        self.assertEqual((await resp.json())["error"], EMPTY_TOPICS)
        self.assertEqual(self.stored_filter(), "1")  # unchanged

    async def test_a_topic_outside_the_active_level_is_refused(self):
        resp = await self.post_topics("1+9")
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        self.assertEqual(body["error"], UNKNOWN_TOPICS)
        self.assertEqual(body["topics"], ["9"])
        self.assertEqual(self.stored_filter(), "")  # nothing written

    async def test_an_unknown_topic_is_refused(self):
        resp = await self.post_topics("1+nosuch")
        self.assertEqual(resp.status, 400)
        self.assertEqual((await resp.json())["error"], UNKNOWN_TOPICS)

    async def test_a_non_string_value_is_refused(self):
        resp = await self.client.post(
            f"{API_PREFIX}topics", headers=auth_header(USER), json={"value": ["0", "1"]}
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual((await resp.json())["error"], INVALID_VALUE)

    async def test_applying_switches_the_filter_branch_back_to_topics(self):
        """Owner decision 2026-08-04: otherwise a user who moved to CEFR levels in
        the chat taps through this screen and nothing filters."""
        set_filter_mode(USER, MODE_CEFR)
        resp = await self.post_topics("1")
        self.assertEqual(resp.status, 200)
        self.assertEqual(get_filter_mode(USER), MODE_TOPICS)
        self.assertEqual((await resp.json())["mode"], MODE_TOPICS)

    async def test_one_users_filter_does_not_touch_another(self):
        await self.post_topics("1", user_id=USER)
        await self.post_topics("2", user_id=OTHER)
        self.assertEqual(self.stored_filter(USER), "1")
        self.assertEqual(self.stored_filter(OTHER), "2")

    # -- agreement with the bot --------------------------------------------

    async def test_what_the_app_writes_is_what_the_bot_applies(self):
        await self.post_topics("0+2")
        quiz_filter = resolve_quiz_filter(USER)
        self.assertEqual(quiz_filter.mode, MODE_TOPICS)
        self.assertEqual(sorted(quiz_filter.topics), ["0", "2"])

    async def test_what_the_bot_sets_is_what_the_app_draws(self):
        set_user_setting(self.db, USER, TOPIC_FILTER_KEY, "03+verb")
        body = await (await self.get_topics()).json()
        self.assertEqual(
            sorted(t["name"] for t in body["topics"] if t["selected"]),
            sorted(resolve_quiz_filter(USER).topics),
        )

    async def test_the_screen_follows_the_active_book_level(self):
        set_active_book_level(USER, "A2")
        body = await (await self.get_topics()).json()
        self.assertEqual(body["level"], "A2")
        self.assertEqual([t["name"] for t in body["topics"]], list(A2_TOPICS))
        self.assertEqual(body["total_words"], sum(A2_TOPICS.values()))

    # -- the gate ----------------------------------------------------------

    async def test_both_routes_need_init_data(self):
        self.assertEqual((await self.client.get(f"{API_PREFIX}topics")).status, 401)
        self.assertEqual(
            (await self.client.post(f"{API_PREFIX}topics", json={"value": "1"})).status, 401
        )

    async def test_a_user_without_access_is_refused(self):
        self.assertEqual((await self.get_topics(PENDING)).status, 403)
        self.assertEqual((await self.post_topics("1", user_id=PENDING)).status, 403)


if __name__ == "__main__":
    unittest.main()
