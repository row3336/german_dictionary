"""Mini App M8 — Learning settings in plain language (miniapp.md §7.6a, §11).

Owner decision 2026-08-03: the app gets to read and write the five learning-batch
parameters the bot's own settings menu labels with the raw storage key
(`daily_qty`, `intervals_sec`...). What is tested here: the numbers round-trip
through the app's minutes <-> the bot's seconds without drifting, a write through
the app is visible to the bot's own accessors (one setting, two clients - the same
contract `topic_filter` already has), bad input is rejected before it reaches
storage, and one user's settings never leak into another's.
"""

import unittest

from _webapp_helpers import TOKEN, TempDbMixin, auth_header
from aiohttp.test_utils import AioHTTPTestCase

from bot.app_config import get_db_path
from bot.common.settings import (
    _learning_daily_qty,
    _learning_intervals_seconds,
    _learning_ready_quorum,
    _learning_reminder_cooldown,
    _learning_reminder_interval,
)
from bot.webapp.access import API_PREFIX
from bot.webapp.learning_settings_api import INVALID_VALUE
from bot.webapp.server import create_app

USER = 111
OTHER = 222


class LearningSettingsApiTest(TempDbMixin, AioHTTPTestCase):
    async def get_application(self):
        self.setup_temp_db()
        self.make_user(USER, "approved")
        self.make_user(OTHER, "approved")
        return create_app(TOKEN)

    async def get_settings(self, user_id=USER):
        return await self.client.get(f"{API_PREFIX}learning/settings", headers=auth_header(user_id))

    async def post_settings(self, body, user_id=USER):
        return await self.client.post(
            f"{API_PREFIX}learning/settings", headers=auth_header(user_id), json=body
        )

    # -- GET: defaults, in human units ------------------------------------

    async def test_defaults_match_the_bots_own_constants_in_minutes(self):
        resp = await self.get_settings()
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["daily_qty"], 20)
        self.assertEqual(body["quorum_pct"], 70)
        self.assertEqual(body["reminder_delay_minutes"], 1.0)
        self.assertEqual(body["reminder_cooldown_minutes"], 15.0)
        self.assertEqual(body["intervals_minutes"], [0.5, 2.0, 10.0, 60.0, 180.0, 300.0])

    async def test_an_unauthenticated_request_is_refused(self):
        resp = await self.client.get(f"{API_PREFIX}learning/settings")
        self.assertEqual(resp.status, 401)

    # -- POST: round trip, one setting for both clients --------------------

    async def test_daily_qty_write_is_visible_through_the_bots_own_accessor(self):
        resp = await self.post_settings({"daily_qty": 15})
        self.assertEqual(resp.status, 200)
        self.assertEqual((await resp.json())["daily_qty"], 15)
        self.assertEqual(_learning_daily_qty(get_db_path(), USER), 15)

    async def test_quorum_pct_write_is_visible_through_the_bots_own_accessor(self):
        await self.post_settings({"quorum_pct": 90})
        self.assertEqual(_learning_ready_quorum(get_db_path(), USER), 90)

    async def test_reminder_minutes_convert_to_seconds_without_drift(self):
        resp = await self.post_settings({"reminder_delay_minutes": 2.5, "reminder_cooldown_minutes": 20})
        body = await resp.json()
        self.assertEqual(body["reminder_delay_minutes"], 2.5)
        self.assertEqual(body["reminder_cooldown_minutes"], 20.0)
        self.assertEqual(_learning_reminder_interval(get_db_path(), USER), 150)
        self.assertEqual(_learning_reminder_cooldown(get_db_path(), USER), 1200)

    async def test_intervals_round_trip_through_the_bots_own_parser(self):
        resp = await self.post_settings({"intervals_minutes": [1, 5, 30]})
        body = await resp.json()
        self.assertEqual(body["intervals_minutes"], [1.0, 5.0, 30.0])
        self.assertEqual(_learning_intervals_seconds(get_db_path(), USER), [60, 300, 1800])

    async def test_a_partial_update_leaves_the_other_fields_untouched(self):
        await self.post_settings({"daily_qty": 5})
        resp = await self.post_settings({"quorum_pct": 50})
        body = await resp.json()
        self.assertEqual(body["daily_qty"], 5)
        self.assertEqual(body["quorum_pct"], 50)

    async def test_an_empty_body_is_a_no_op(self):
        before = await (await self.get_settings()).json()
        resp = await self.post_settings({})
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), before)

    # -- validation, same bounds as the bot's edit flow --------------------

    async def test_quorum_pct_above_100_is_refused(self):
        resp = await self.post_settings({"quorum_pct": 101})
        self.assertEqual(resp.status, 400)
        self.assertEqual(await resp.json(), {"error": INVALID_VALUE})
        self.assertEqual(_learning_ready_quorum(get_db_path(), USER), 70)  # unchanged

    async def test_quorum_pct_below_1_is_refused(self):
        resp = await self.post_settings({"quorum_pct": 0})
        self.assertEqual(resp.status, 400)

    async def test_daily_qty_below_1_is_refused(self):
        resp = await self.post_settings({"daily_qty": 0})
        self.assertEqual(resp.status, 400)

    async def test_non_numeric_value_is_refused(self):
        resp = await self.post_settings({"daily_qty": "not-a-number"})
        self.assertEqual(resp.status, 400)

    async def test_an_empty_intervals_list_is_refused(self):
        resp = await self.post_settings({"intervals_minutes": []})
        self.assertEqual(resp.status, 400)

    async def test_a_non_list_intervals_value_is_refused(self):
        resp = await self.post_settings({"intervals_minutes": "5,10"})
        self.assertEqual(resp.status, 400)

    async def test_a_non_positive_interval_entry_is_refused(self):
        resp = await self.post_settings({"intervals_minutes": [5, 0]})
        self.assertEqual(resp.status, 400)
        # Nothing partial got written - the whole list is rejected together.
        self.assertEqual(
            _learning_intervals_seconds(get_db_path(), USER),
            [30, 120, 600, 3600, 10800, 18000],
        )

    # -- isolation: risk #6, §12 --------------------------------------------

    async def test_one_users_settings_do_not_leak_into_anothers(self):
        await self.post_settings({"daily_qty": 5}, user_id=USER)
        resp = await self.get_settings(user_id=OTHER)
        self.assertEqual((await resp.json())["daily_qty"], 20)  # OTHER still on the default


if __name__ == "__main__":
    unittest.main()
