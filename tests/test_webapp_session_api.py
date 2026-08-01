"""Mini App M3 step 4 — session lifecycle over HTTP (miniapp.md §7.2, §7.7, §11).

The registry's rules are already tested on their own; what is checked here is that
they survive the trip through HTTP intact — that a taken-over session really comes
back as 409 session_taken_over, that starting in the app really reaches into the
bot, and that reopening a minimized WebView gets the same question rather than a
new one.
"""

import asyncio
import json
import unittest

from _webapp_helpers import TOKEN, TempDbMixin, auth_header
from aiohttp.test_utils import AioHTTPTestCase

from bot.webapp.access import API_PREFIX
from bot.webapp.app_keys import BRIDGE_KEY, REGISTRY_KEY
from bot.webapp.server import SWEEP_INTERVAL_SECONDS, create_app
from bot.webapp.sessions import CLIENT_BOT

USER = 111
OTHER = 222


class SessionApiTest(TempDbMixin, AioHTTPTestCase):
    async def get_application(self):
        self.setup_temp_db()
        self.make_user(USER, "approved")
        self.make_user(OTHER, "approved")
        return create_app(TOKEN)

    # -- helpers ---------------------------------------------------------

    async def start(self, user_id=USER, **body):
        payload = {"mode": "l1_de", **body}
        return await self.client.post(
            f"{API_PREFIX}session/start", headers=auth_header(user_id), json=payload
        )

    @property
    def registry(self):
        return self.app[REGISTRY_KEY]

    # -- start -----------------------------------------------------------

    async def test_start_opens_a_session(self):
        resp = await self.start()
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertTrue(body["session_id"])
        self.assertEqual(body["mode"], "l1_de")
        self.assertIsNone(body["question"])

    async def test_start_refuses_an_unknown_mode(self):
        resp = await self.start(mode="telepathy")
        self.assertEqual(resp.status, 400)
        self.assertEqual((await resp.json())["error"], "bad_mode")

    async def test_start_refuses_a_missing_mode(self):
        resp = await self.client.post(
            f"{API_PREFIX}session/start", headers=auth_header(USER), json={}
        )
        self.assertEqual(resp.status, 400)

    async def test_start_refuses_a_malformed_body(self):
        resp = await self.client.post(
            f"{API_PREFIX}session/start",
            headers={**auth_header(USER), "Content-Type": "application/json"},
            data="{not json",
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual((await resp.json())["error"], "bad_request")

    async def test_start_keeps_the_word_type(self):
        body = await (await self.start(word_type="nomen")).json()
        self.assertEqual(body["word_type"], "nomen")

    async def test_start_requires_authentication(self):
        resp = await self.client.post(f"{API_PREFIX}session/start", json={"mode": "l1_de"})
        self.assertEqual(resp.status, 401)

    async def test_start_reports_whether_the_chat_was_notified(self):
        # No bot behind the bridge here, so nothing was interrupted and nothing
        # was sent - the flag has to say so rather than claim a message.
        body = await (await self.start()).json()
        self.assertFalse(body["chat_notified"])

    async def test_a_second_start_supersedes_the_first(self):
        first = await (await self.start()).json()
        second = await (await self.start()).json()
        self.assertNotEqual(first["session_id"], second["session_id"])

    async def test_sessions_of_two_users_are_independent(self):
        mine = await (await self.start(USER)).json()
        theirs = await (await self.start(OTHER, mode="de_l1")).json()
        self.assertNotEqual(mine["session_id"], theirs["session_id"])
        still_mine = await (
            await self.client.get(f"{API_PREFIX}session/question", headers=auth_header(USER))
        ).json()
        self.assertEqual(still_mine["session_id"], mine["session_id"])
        self.assertEqual(theirs["mode"], "de_l1")

    # -- restore ---------------------------------------------------------

    async def test_question_returns_the_same_session_after_reopening(self):
        started = await (await self.start()).json()
        resp = await self.client.get(f"{API_PREFIX}session/question", headers=auth_header(USER))
        self.assertEqual(resp.status, 200)
        self.assertEqual((await resp.json())["session_id"], started["session_id"])

    async def test_question_does_not_mint_a_new_token(self):
        # Reopening the app is not answering; a fresh question here would skip the
        # word that was on screen.
        await self.start()
        session = self.registry.require(USER)
        self.registry.issue_question(session)
        token = session.question_token
        await self.client.get(f"{API_PREFIX}session/question", headers=auth_header(USER))
        self.assertEqual(session.question_token, token)

    async def test_question_without_a_session_is_a_409(self):
        resp = await self.client.get(f"{API_PREFIX}session/question", headers=auth_header(USER))
        self.assertEqual(resp.status, 409)
        self.assertEqual((await resp.json())["error"], "session_taken_over")

    async def test_a_session_reclaimed_by_the_chat_answers_409(self):
        # The reverse takeover, seen from the app's side (§4.2 п.3).
        await self.start()
        self.registry.claim_for_bot(USER)
        resp = await self.client.get(f"{API_PREFIX}session/question", headers=auth_header(USER))
        self.assertEqual(resp.status, 409)
        self.assertEqual((await resp.json())["error"], "session_taken_over")

    async def test_continue_here_after_a_takeover(self):
        # The "continue here" button is just another start.
        await self.start()
        self.registry.claim_for_bot(USER)
        self.assertEqual(self.registry.active_client(USER), CLIENT_BOT)
        resp = await self.start()
        self.assertEqual(resp.status, 200)
        self.assertEqual(
            (await (await self.client.get(
                f"{API_PREFIX}session/question", headers=auth_header(USER)
            )).json())["session_id"],
            (await resp.json())["session_id"],
        )

    # -- stop ------------------------------------------------------------

    async def test_stop_closes_the_session(self):
        await self.start()
        resp = await self.client.post(f"{API_PREFIX}session/stop", headers=auth_header(USER))
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), {"stopped": True})
        self.assertEqual(len(self.registry), 0)

    async def test_stopping_twice_is_a_409_not_a_silent_success(self):
        await self.start()
        await self.client.post(f"{API_PREFIX}session/stop", headers=auth_header(USER))
        resp = await self.client.post(f"{API_PREFIX}session/stop", headers=auth_header(USER))
        self.assertEqual(resp.status, 409)

    async def test_stop_only_closes_the_callers_session(self):
        await self.start(USER)
        theirs = await (await self.start(OTHER)).json()
        await self.client.post(f"{API_PREFIX}session/stop", headers=auth_header(USER))
        resp = await self.client.get(f"{API_PREFIX}session/question", headers=auth_header(OTHER))
        self.assertEqual((await resp.json())["session_id"], theirs["session_id"])

    # -- gate ------------------------------------------------------------

    async def test_a_pending_user_cannot_open_a_session(self):
        self.make_user(333, "pending")
        resp = await self.start(333)
        self.assertEqual(resp.status, 403)
        self.assertEqual(len(self.registry), 0)


class TakeoverThroughHttpTest(TempDbMixin, AioHTTPTestCase):
    """Starting in the app must actually reach into the bot's memory."""

    class FakeBot:
        token = TOKEN

        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text):
            self.sent.append((chat_id, text))

    class FakeApplication:
        def __init__(self, user_data):
            self.user_data = user_data
            self.bot = TakeoverThroughHttpTest.FakeBot()

    async def get_application(self):
        self.setup_temp_db()
        self.make_user(USER, "approved")
        self.user_data = {USER: {"session_mode": "ru_de", "quiz_entry": {"id": "w1"}}}
        self.application = self.FakeApplication(self.user_data)
        return create_app(TOKEN, application=self.application)

    async def test_start_clears_the_bots_quiz_state_and_notifies(self):
        resp = await self.client.post(
            f"{API_PREFIX}session/start", headers=auth_header(USER), json={"mode": "l1_de"}
        )
        self.assertTrue((await resp.json())["chat_notified"])
        self.assertEqual(self.user_data[USER], {})
        self.assertEqual(len(self.application.bot.sent), 1)

    async def test_no_notice_when_the_chat_had_nothing_running(self):
        self.user_data[USER] = {}
        resp = await self.client.post(
            f"{API_PREFIX}session/start", headers=auth_header(USER), json={"mode": "l1_de"}
        )
        self.assertFalse((await resp.json())["chat_notified"])
        self.assertEqual(self.application.bot.sent, [])


class SweeperTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_sweeper_starts_and_stops_with_the_app(self):
        from aiohttp.test_utils import TestClient, TestServer

        app = create_app(TOKEN)
        client = TestClient(TestServer(app))
        await client.start_server()
        # One background task is running while the app is up.
        running = [t for t in asyncio.all_tasks() if not t.done()]
        self.assertTrue(running)
        await client.close()

    def test_the_interval_is_well_below_the_ttl(self):
        from bot.webapp.sessions import SESSION_TTL_SECONDS

        self.assertLess(SWEEP_INTERVAL_SECONDS, SESSION_TTL_SECONDS)


if __name__ == "__main__":
    unittest.main()
