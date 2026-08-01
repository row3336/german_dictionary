"""Mini App M1 — initData signature checking (miniapp.md §6.1, §11).

A hole here means "log in as any user", so the negative cases matter more than the
positive one: a forged hash, a hash signed with someone else's bot token, a stale
auth_date, a tampered field with the original hash kept, duplicate keys.

No telegram stub is needed: bot.webapp.auth is stdlib-only by design.
"""

import json
import time
import unittest
from urllib.parse import urlencode

from _webapp_helpers import OTHER_TOKEN, TOKEN, make_fields, sign

from bot.webapp.auth import (
    INVALID_INIT_DATA,
    InitDataError,
    parse_auth_header,
    validate_init_data,
)

USER_ID = 4242


class ValidInitDataTest(unittest.TestCase):
    def test_accepts_a_correctly_signed_payload(self):
        result = validate_init_data(sign(make_fields()), TOKEN)
        self.assertEqual(result["user_id"], USER_ID)
        self.assertEqual(result["user"]["first_name"], "Test")

    def test_accepts_values_needing_url_escaping(self):
        # Real payloads carry JSON with quotes and braces; the check string is built
        # from decoded values, so a naive implementation breaks exactly here.
        fields = make_fields()
        fields["user"] = json.dumps({"id": USER_ID, "first_name": "Ann & Bob"})
        result = validate_init_data(sign(fields), TOKEN)
        self.assertEqual(result["user"]["first_name"], "Ann & Bob")

    def test_accepts_data_just_inside_the_ttl(self):
        now = int(time.time())
        raw = sign(make_fields(auth_date=now - 23 * 60 * 60))
        self.assertEqual(validate_init_data(raw, TOKEN, now=now)["user_id"], USER_ID)


class ForgedInitDataTest(unittest.TestCase):
    def assert_rejected(self, raw, token=TOKEN, **kwargs):
        with self.assertRaises(InitDataError) as ctx:
            validate_init_data(raw, token, **kwargs)
        # One wire code for every failure - the client must not learn which check fired.
        self.assertEqual(ctx.exception.code, INVALID_INIT_DATA)
        return ctx.exception

    def test_rejects_a_tampered_hash(self):
        raw = sign(make_fields())
        self.assert_rejected(raw[:-1] + ("0" if raw[-1] != "0" else "1"))

    def test_rejects_a_missing_hash(self):
        self.assert_rejected(urlencode(make_fields()))

    def test_rejects_an_empty_payload(self):
        self.assert_rejected("")

    def test_rejects_a_payload_signed_with_another_bot_token(self):
        # The alpha and production bots share a host (§9.1): initData minted for one
        # must never authenticate against the other.
        self.assert_rejected(sign(make_fields(), token=OTHER_TOKEN))

    def test_rejects_a_swapped_user_with_the_original_hash(self):
        fields = make_fields()
        raw = sign(fields)
        forged = raw.replace(
            urlencode({"user": fields["user"]}),
            urlencode({"user": json.dumps({"id": 1, "first_name": "Mallory"})}),
        )
        self.assertNotEqual(forged, raw)
        self.assert_rejected(forged)

    def test_rejects_expired_auth_date(self):
        now = int(time.time())
        self.assert_rejected(sign(make_fields(auth_date=now - 25 * 60 * 60)), now=now)

    def test_rejects_auth_date_far_in_the_future(self):
        now = int(time.time())
        self.assert_rejected(sign(make_fields(auth_date=now + 3600)), now=now)

    def test_rejects_non_numeric_auth_date(self):
        self.assert_rejected(sign(make_fields(auth_date="yesterday")))

    def test_rejects_a_duplicated_hash_key(self):
        # Two hashes let a forger sign one copy and hope the reader takes the other.
        raw = sign(make_fields())
        self.assert_rejected(raw + "&hash=" + "0" * 64)

    def test_rejects_a_payload_without_a_user(self):
        fields = make_fields()
        fields.pop("user")
        self.assert_rejected(sign(fields))

    def test_rejects_a_user_without_an_id(self):
        fields = make_fields()
        fields["user"] = json.dumps({"first_name": "Nobody"})
        self.assert_rejected(sign(fields))

    def test_rejects_when_no_bot_token_is_configured(self):
        self.assert_rejected(sign(make_fields()), token="")


class AuthHeaderTest(unittest.TestCase):
    def test_extracts_init_data_from_the_tma_scheme(self):
        self.assertEqual(parse_auth_header("tma auth_date=1&hash=ab"), "auth_date=1&hash=ab")

    def test_scheme_is_case_insensitive(self):
        self.assertEqual(parse_auth_header("TMA payload"), "payload")

    def test_rejects_another_scheme(self):
        with self.assertRaises(InitDataError):
            parse_auth_header("Bearer payload")

    def test_rejects_a_missing_or_empty_header(self):
        for value in (None, "", "tma ", "tma"):
            with self.subTest(value=value), self.assertRaises(InitDataError):
                parse_auth_header(value)


if __name__ == "__main__":
    unittest.main()
