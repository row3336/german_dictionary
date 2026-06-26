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
    TOPIC_FILTER_KEY,
)
from bot.common.settings import (
    _session_retry_gap,
    _session_retry_iterations,
    _topic_filter_for_query,
    _topic_filter_value,
)
from bot.storage import init_db, reset_user_settings, set_setting, set_user_setting

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

    def test_reset_defaults_clears_only_callers_algo(self):
        # P2.4c — "reset to default" deletes this user's algo overrides only
        set_user_setting(self.db, U1, SESSION_RETRY_GAP_KEY, "9")
        set_user_setting(self.db, U1, SESSION_RETRY_IT_KEY, "8")
        set_user_setting(self.db, U2, SESSION_RETRY_GAP_KEY, "4")
        reset_user_settings(self.db, U1, keys=[SESSION_RETRY_GAP_KEY, SESSION_RETRY_IT_KEY])
        self.assertEqual(_session_retry_gap(self.db, U1), RETRY_GAP_QUESTIONS)       # back to default
        self.assertEqual(_session_retry_iterations(self.db, U1), RETRY_MAX_REPEATS)
        self.assertEqual(_session_retry_gap(self.db, U2), 4)                         # U2 untouched


class TopicFilterPerUserTest(unittest.TestCase):
    """P2.4b — topic_filter is per-user (override → global → '1').

    The topic getters resolve the DB via get_db_path(), so point it at the temp DB.
    """

    def setUp(self):
        self.db = _mk()
        init_db(self.db)
        self._old_db_path = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = self.db

        def _restore():
            if self._old_db_path is None:
                os.environ.pop("DB_PATH", None)
            else:
                os.environ["DB_PATH"] = self._old_db_path
            os.unlink(self.db)

        self.addCleanup(_restore)

    def test_default_is_one(self):
        self.assertEqual(_topic_filter_value(U1), "1")

    def test_per_user_override_isolated(self):
        set_user_setting(self.db, U1, TOPIC_FILTER_KEY, "5")
        self.assertEqual(_topic_filter_value(U1), "5")
        self.assertEqual(_topic_filter_value(U2), "1")  # U2 stays default

    def test_for_query_parses_per_user(self):
        set_user_setting(self.db, U1, TOPIC_FILTER_KEY, "2+food")
        self.assertEqual(_topic_filter_for_query(U1), ["2", "food"])
        self.assertEqual(_topic_filter_for_query(U2), ["1"])  # default topic

    def test_global_fallback_then_own_override(self):
        set_setting(self.db, TOPIC_FILTER_KEY, "3")  # legacy/global default
        self.assertEqual(_topic_filter_value(U1), "3")
        self.assertEqual(_topic_filter_value(U2), "3")
        set_user_setting(self.db, U1, TOPIC_FILTER_KEY, "7")
        self.assertEqual(_topic_filter_value(U1), "7")  # own override wins
        self.assertEqual(_topic_filter_value(U2), "3")  # U2 still global


if __name__ == "__main__":
    unittest.main()
