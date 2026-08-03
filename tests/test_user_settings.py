"""Per-user settings layer (P2.2): get/set with global-default fallback + reset.

Strictly user-scoped — one user's override never leaks to another or the owner.
"""

import os
import tempfile
import unittest

from bot.storage import (
    get_user_setting,
    init_db,
    reset_user_settings,
    set_user_setting,
)

U1, U2 = 101, 202


class UserSettingsTest(unittest.TestCase):
    def setUp(self):
        self.db = _mk()
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)

    def test_set_get_roundtrip(self):
        set_user_setting(self.db, U1, "topic_filter", "5")
        self.assertEqual(get_user_setting(self.db, U1, "topic_filter"), "5")

    def test_unset_returns_default(self):
        self.assertEqual(get_user_setting(self.db, U1, "topic_filter", "1"), "1")
        self.assertIsNone(get_user_setting(self.db, U1, "missing"))

    def test_per_user_isolation(self):
        set_user_setting(self.db, U1, "topic_filter", "5")
        # U2 has nothing set → falls back to default, does NOT see U1's value
        self.assertEqual(get_user_setting(self.db, U2, "topic_filter", "1"), "1")

    def test_upsert_overwrites_same_user(self):
        set_user_setting(self.db, U1, "k", "a")
        set_user_setting(self.db, U1, "k", "b")
        self.assertEqual(get_user_setting(self.db, U1, "k"), "b")

    def test_reset_specific_keys_only_caller(self):
        set_user_setting(self.db, U1, "a", "1")
        set_user_setting(self.db, U1, "b", "2")
        set_user_setting(self.db, U2, "a", "9")
        reset_user_settings(self.db, U1, keys=["a"])
        self.assertEqual(get_user_setting(self.db, U1, "a", "def"), "def")  # reset → default
        self.assertEqual(get_user_setting(self.db, U1, "b"), "2")           # other key kept
        self.assertEqual(get_user_setting(self.db, U2, "a"), "9")           # other user untouched

    def test_reset_all_only_caller(self):
        set_user_setting(self.db, U1, "a", "1")
        set_user_setting(self.db, U2, "a", "9")
        reset_user_settings(self.db, U1)
        self.assertIsNone(get_user_setting(self.db, U1, "a"))
        self.assertEqual(get_user_setting(self.db, U2, "a"), "9")


def _mk():
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.close()
    return tmp.name


if __name__ == "__main__":
    unittest.main()
