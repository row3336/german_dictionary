"""Per-user algo settings (P2.4a): session retry gap/iterations.

Resolution order: this user's override → legacy global app_settings → constant
default. Strictly user-scoped — one user's algo never affects another or the owner.
"""

import os
import tempfile
import unittest

from bot.app_config import (
    RETRY_GAP_QUESTIONS,
    RETRY_MAX_REPEATS,
    SESSION_RETRY_GAP_KEY,
    SESSION_RETRY_IT_KEY,
)
from bot.common.settings import _session_retry_gap, _session_retry_iterations
from bot.storage import init_db, set_setting, set_user_setting

U1, U2 = 101, 202


def _mk():
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.close()
    return tmp.name


class AlgoSettingsPerUserTest(unittest.TestCase):
    def setUp(self):
        self.db = _mk()
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)

    def test_default_when_unset(self):
        self.assertEqual(_session_retry_gap(self.db, U1), RETRY_GAP_QUESTIONS)
        self.assertEqual(_session_retry_iterations(self.db, U1), RETRY_MAX_REPEATS)

    def test_per_user_override_isolated(self):
        set_user_setting(self.db, U1, SESSION_RETRY_GAP_KEY, "7")
        self.assertEqual(_session_retry_gap(self.db, U1), 7)
        self.assertEqual(_session_retry_gap(self.db, U2), RETRY_GAP_QUESTIONS)  # U2 untouched

    def test_iterations_per_user(self):
        set_user_setting(self.db, U1, SESSION_RETRY_IT_KEY, "4")
        self.assertEqual(_session_retry_iterations(self.db, U1), 4)
        self.assertEqual(_session_retry_iterations(self.db, U2), RETRY_MAX_REPEATS)

    def test_global_fallback_then_own_override(self):
        set_setting(self.db, SESSION_RETRY_GAP_KEY, "5")  # legacy/global default
        self.assertEqual(_session_retry_gap(self.db, U1), 5)  # both fall back to global
        self.assertEqual(_session_retry_gap(self.db, U2), 5)
        set_user_setting(self.db, U1, SESSION_RETRY_GAP_KEY, "9")
        self.assertEqual(_session_retry_gap(self.db, U1), 9)  # own override wins
        self.assertEqual(_session_retry_gap(self.db, U2), 5)  # U2 still on global


if __name__ == "__main__":
    unittest.main()
