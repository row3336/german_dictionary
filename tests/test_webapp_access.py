"""Mini App M2 — the access gate on /api/v1/ (miniapp.md §6.2, §11).

Two promises are tested here. First, the matrix: approved and owner get in, pending
and rejected and unknown get their own 403 code so the app can show the right
screen. Second, and more important, that an unauthenticated request never reaches
a handler at all — a gate that runs after the handler has already read the database
is not a gate.
"""

import sqlite3
import unittest

from _webapp_helpers import OTHER_TOKEN, TOKEN, TempDbMixin, auth_header, make_fields, sign
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from bot.webapp.access import (
    ACCESS_OK,
    ACCESS_PENDING,
    ACCESS_REJECTED,
    ACCESS_UNKNOWN,
    API_PREFIX,
    REQUEST_USER_ID,
    resolve_access,
)
from bot.webapp.server import create_app

APPROVED = 111
PENDING = 222
REJECTED = 333
STRANGER = 444


class ResolveAccessTest(TempDbMixin, unittest.TestCase):
    def setUp(self):
        self.setup_temp_db()

    def test_approved_user_passes(self):
        self.make_user(APPROVED, "approved")
        self.assertEqual(resolve_access(APPROVED), ACCESS_OK)

    def test_pending_user_is_pending(self):
        self.make_user(PENDING, "pending")
        self.assertEqual(resolve_access(PENDING), ACCESS_PENDING)

    def test_rejected_user_is_rejected(self):
        self.make_user(REJECTED, "rejected")
        self.assertEqual(resolve_access(REJECTED), ACCESS_REJECTED)

    def test_user_without_a_row_is_unknown(self):
        self.assertEqual(resolve_access(STRANGER), ACCESS_UNKNOWN)

    def test_owner_passes_even_without_an_approved_row(self):
        # The owner must not be able to lock themselves out by a stale users row.
        self.make_user(self.OWNER_ID, "pending")
        self.assertEqual(resolve_access(self.OWNER_ID), ACCESS_OK)

    def test_an_unrecognised_status_is_treated_as_pending(self):
        # set_user_status validates, so this is a hand-written row - the shape a
        # future migration or a manual DB edit could leave behind.
        self.make_user(PENDING, "approved")
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE users SET status = 'banned' WHERE user_id = ?", (PENDING,))
        self.assertEqual(resolve_access(PENDING), ACCESS_PENDING)


class GateOverHttpTest(TempDbMixin, AioHTTPTestCase):
    async def get_application(self):
        self.setup_temp_db()
        self.handler_calls = []
        app = create_app(TOKEN)

        async def probe(request):
            # Records that the handler ran, and with which identity.
            self.handler_calls.append(request[REQUEST_USER_ID])
            return web.json_response({"seen": request[REQUEST_USER_ID]})

        app.router.add_get(f"{API_PREFIX}probe", probe)
        return app

    async def test_approved_user_reaches_the_handler(self):
        self.make_user(APPROVED, "approved")
        resp = await self.client.get(f"{API_PREFIX}probe", headers=auth_header(APPROVED))
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), {"seen": APPROVED})
        self.assertEqual(self.handler_calls, [APPROVED])

    async def test_pending_and_rejected_and_unknown_get_their_own_code(self):
        self.make_user(PENDING, "pending")
        self.make_user(REJECTED, "rejected")
        cases = {
            PENDING: ACCESS_PENDING,
            REJECTED: ACCESS_REJECTED,
            STRANGER: ACCESS_UNKNOWN,
        }
        for user_id, code in cases.items():
            with self.subTest(user=user_id):
                resp = await self.client.get(
                    f"{API_PREFIX}probe", headers=auth_header(user_id)
                )
                self.assertEqual(resp.status, 403)
                self.assertEqual((await resp.json())["error"], code)
        self.assertEqual(self.handler_calls, [])

    async def test_a_request_without_a_header_never_reaches_the_handler(self):
        self.make_user(APPROVED, "approved")
        resp = await self.client.get(f"{API_PREFIX}probe")
        self.assertEqual(resp.status, 401)
        self.assertEqual((await resp.json())["error"], "invalid_init_data")
        self.assertEqual(self.handler_calls, [])

    async def test_a_forged_signature_never_reaches_the_handler(self):
        self.make_user(APPROVED, "approved")
        forged = {"Authorization": "tma " + sign(make_fields(APPROVED), token=OTHER_TOKEN)}
        resp = await self.client.get(f"{API_PREFIX}probe", headers=forged)
        self.assertEqual(resp.status, 401)
        self.assertEqual(self.handler_calls, [])

    async def test_the_identity_comes_from_the_signature_not_the_request(self):
        # A query parameter claiming to be somebody else changes nothing: the gate
        # only ever trusts the signed payload (risk #6).
        self.make_user(APPROVED, "approved")
        resp = await self.client.get(
            f"{API_PREFIX}probe?user_id={REJECTED}", headers=auth_header(APPROVED)
        )
        self.assertEqual((await resp.json())["seen"], APPROVED)

    async def test_healthz_stays_open(self):
        # Outside the prefix: the shell has to load to render a "no access" screen.
        self.assertEqual((await self.client.get("/healthz")).status, 200)


if __name__ == "__main__":
    unittest.main()
