"""Per-user algo settings (P2.4a): session retry gap/iterations.

Resolution order: this user's override → legacy global app_settings → constant
default. Strictly user-scoped — one user's algo never affects another or the owner.
"""

import os
import sqlite3
import tempfile
import unittest

from bot.app_config import (
    LEARNING_DAILY_QTY_DEFAULT,
    LEARNING_DAILY_QTY_KEY,
    LEARNING_INTERVALS_KEY,
    LEARNING_READY_QUORUM_DEFAULT,
    LEARNING_READY_QUORUM_KEY,
    LEARNING_REMINDER_COOLDOWN_DEFAULT,
    LEARNING_REMINDER_COOLDOWN_KEY,
    LEARNING_REMINDER_INTERVAL_DEFAULT,
    LEARNING_REMINDER_INTERVAL_KEY,
    LEARNING_WARMUP_INTERVALS,
    RETRY_GAP_QUESTIONS,
    RETRY_MAX_REPEATS,
    SESSION_RETRY_GAP_KEY,
    SESSION_RETRY_IT_KEY,
    TOPIC_FILTER_KEY,
)
from bot.common.settings import (
    _learning_daily_qty,
    _learning_intervals_seconds,
    _learning_ready_quorum,
    _learning_reminder_cooldown,
    _learning_reminder_interval,
    _learning_schedule_seconds,
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
        with sqlite3.connect(self.db) as conn:
            conn.executemany("INSERT INTO topics(name, level) VALUES (?, 'A1')",
                             [("1",), ("2",), ("food",)])
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
        self.assertEqual(_topic_filter_for_query(U2), ["1", "2", "food"])  # Absent legacy filter selects all A1 topics.

    def test_global_fallback_then_own_override(self):
        set_setting(self.db, TOPIC_FILTER_KEY, "3")  # legacy/global default
        self.assertEqual(_topic_filter_value(U1), "3")
        self.assertEqual(_topic_filter_value(U2), "3")
        set_user_setting(self.db, U1, TOPIC_FILTER_KEY, "7")
        self.assertEqual(_topic_filter_value(U1), "7")  # own override wins
        self.assertEqual(_topic_filter_value(U2), "3")  # U2 still global


class LearningPerUserTest(unittest.TestCase):
    """P2.4d — learning params are per-user (override → global → default)."""

    def setUp(self):
        self.db = _mk()
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)

    def test_defaults_when_unset(self):
        self.assertEqual(_learning_daily_qty(self.db, U1), LEARNING_DAILY_QTY_DEFAULT)
        self.assertEqual(_learning_ready_quorum(self.db, U1), LEARNING_READY_QUORUM_DEFAULT)
        self.assertEqual(_learning_reminder_interval(self.db, U1), LEARNING_REMINDER_INTERVAL_DEFAULT)
        self.assertEqual(_learning_reminder_cooldown(self.db, U1), LEARNING_REMINDER_COOLDOWN_DEFAULT)

    def test_learning_override_isolated(self):
        set_user_setting(self.db, U1, LEARNING_DAILY_QTY_KEY, "25")
        set_user_setting(self.db, U1, LEARNING_READY_QUORUM_KEY, "50")
        self.assertEqual(_learning_daily_qty(self.db, U1), 25)
        self.assertEqual(_learning_ready_quorum(self.db, U1), 50)
        self.assertEqual(_learning_daily_qty(self.db, U2), LEARNING_DAILY_QTY_DEFAULT)
        self.assertEqual(_learning_ready_quorum(self.db, U2), LEARNING_READY_QUORUM_DEFAULT)

    def test_learning_intervals_per_user(self):
        set_user_setting(self.db, U1, LEARNING_INTERVALS_KEY, "30,120,600")
        self.assertEqual(_learning_intervals_seconds(self.db, U1), [30, 120, 600])
        # schedule = warmup prefix + this user's intervals
        self.assertEqual(
            _learning_schedule_seconds(self.db, U1),
            list(LEARNING_WARMUP_INTERVALS) + [30, 120, 600],
        )
        # U2 stays on its own (default) intervals, not U1's
        self.assertNotEqual(_learning_intervals_seconds(self.db, U2), [30, 120, 600])

    def test_reset_clears_study_keys_only_for_caller(self):
        keys = [LEARNING_DAILY_QTY_KEY, LEARNING_INTERVALS_KEY]
        for k, v in [(LEARNING_DAILY_QTY_KEY, "30"), (LEARNING_INTERVALS_KEY, "30,60")]:
            set_user_setting(self.db, U1, k, v)
        set_user_setting(self.db, U2, LEARNING_DAILY_QTY_KEY, "4")
        reset_user_settings(self.db, U1, keys=keys)
        self.assertEqual(_learning_daily_qty(self.db, U1), LEARNING_DAILY_QTY_DEFAULT)
        self.assertEqual(_learning_daily_qty(self.db, U2), 4)  # U2 untouched


if __name__ == "__main__":
    unittest.main()
