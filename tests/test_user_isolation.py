"""Cross-user data isolation contract (docs/multiuser.md P1, §5.1).

The #1 silent multiuser risk: a per-user query that forgets `WHERE user_id = ?`
would mix one user's progress/stats/learning with another's. These tests seed
TWO users sharing the SAME word_ids (one shared dictionary) and assert each
user's reads return only their own rows. Grows per sub-step (P1.1..P1.5).
"""

import os
import sqlite3
import tempfile
import unittest
import warnings
from datetime import date, timedelta

from bot.storage import (
    add_transaction_log,
    add_word,
    delete_transaction_logs,
    fsrs_due_forecast,
    get_daily_first_touch_retention,
    get_statistics_snapshot,
    init_db,
    learning_active_count,
    learning_due_entries,
    learning_get_or_create_batch,
    get_user_languages,
    list_transaction_logs,
    list_weak_words,
    reset_periodicity,
    select_due,
    set_user_dict_lang,
    set_user_interface_lang,
)

warnings.filterwarnings("ignore", category=ResourceWarning)

U1, U2 = 101, 202


def _mk_db():
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.close()
    return tmp.name


def _add_noun(db_path, german, russian, topic="1"):
    return add_word(
        db_path,
        {
            "id": "unique",
            "type": "nomen",
            "article": "der",
            "german": german,
            "russian": russian,
            "ukrainian": "",
            "english": "",
            "topic": topic,
        },
    )


def _event(conn, user_id, word_id, mode, is_correct, created_at):
    conn.execute(
        "INSERT INTO quiz_events (user_id, word_id, mode, is_correct, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, word_id, mode, is_correct, created_at),
    )


def _progress(conn, user_id, word_id, mode, due, interval=1, reps=1, ease=2.5):
    conn.execute(
        "INSERT INTO progress (user_id, word_id, mode, interval, repetitions, ease, due) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, word_id, mode, interval, reps, ease, due),
    )


def _weak_article(conn, user_id, word_id, errors=3):
    # enough article errors to make the word "weak" for this user (other cols default 0)
    conn.execute(
        "INSERT INTO form_stats (user_id, word_id, article_errors) VALUES (?, ?, ?)",
        (user_id, word_id, errors),
    )


def _learning_item(conn, user_id, word_id, batch_id, due_iso="2000-01-01 00:00:00"):
    # an active, preview-done item due in the past → appears in learning_due_entries
    conn.execute(
        "INSERT INTO learning_items (user_id, word_id, batch_id, step_index, preview_done, next_due, status) "
        "VALUES (?, ?, ?, 0, 1, ?, 'active')",
        (user_id, word_id, batch_id, due_iso),
    )


class SrCoreIsolationTest(unittest.TestCase):
    """P1.1 — spaced-repetition core: select_due + fsrs_due_forecast."""

    def setUp(self):
        self.db = _mk_db()
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self.today = date.today().isoformat()

    def test_select_due_is_per_user(self):
        # shared word, introduced only for U1 → U2 must see nothing
        e = _add_noun(self.db, "Schal", "шарф")
        with sqlite3.connect(self.db) as conn:
            _event(conn, U1, e["id"], "ru_de", 0, f"{self.today} 08:00:00")
        self.assertEqual(len(select_due(self.db, user_id=U1, mode="ru_de")), 1)
        self.assertEqual(len(select_due(self.db, user_id=U2, mode="ru_de")), 0)

    def test_other_users_progress_does_not_leak(self):
        # both introduced; U2 scheduled far out → only U1 is due, U2 is not
        e = _add_noun(self.db, "Schal", "шарф")
        future = (date.today() + timedelta(days=999)).isoformat()
        with sqlite3.connect(self.db) as conn:
            _event(conn, U1, e["id"], "ru_de", 0, f"{self.today} 08:00:00")
            _event(conn, U2, e["id"], "ru_de", 0, f"{self.today} 08:00:00")
            _progress(conn, U2, e["id"], "ru_de", future)
        self.assertEqual(len(select_due(self.db, user_id=U1, mode="ru_de")), 1)
        self.assertEqual(len(select_due(self.db, user_id=U2, mode="ru_de")), 0)

    def test_forecast_counts_only_own(self):
        for i in range(2):
            e = _add_noun(self.db, f"Wort{i}", f"слово{i}")
            with sqlite3.connect(self.db) as conn:
                _event(conn, U1, e["id"], "ru_de", 0, f"{self.today} 08:0{i}:00")
        self.assertEqual(fsrs_due_forecast(self.db, U1, "ru_de")["total"], 2)
        self.assertEqual(fsrs_due_forecast(self.db, U2, "ru_de")["total"], 0)


class WeakWordsIsolationTest(unittest.TestCase):
    """P1.2 — weak words (form_stats), incl. the db_search weak list: per-user."""

    def setUp(self):
        self.db = _mk_db()
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)

    def test_weak_list_is_per_user(self):
        e = _add_noun(self.db, "Tisch", "стол")
        with sqlite3.connect(self.db) as conn:
            _weak_article(conn, U1, e["id"], errors=3)  # weak for U1 only
        self.assertEqual(len(list_weak_words(self.db, U1, "article")), 1)
        self.assertEqual(len(list_weak_words(self.db, U2, "article")), 0)

    def test_each_user_sees_only_own_weak(self):
        e1 = _add_noun(self.db, "Tisch", "стол")
        e2 = _add_noun(self.db, "Lampe", "лампа")
        with sqlite3.connect(self.db) as conn:
            _weak_article(conn, U1, e1["id"], errors=3)
            _weak_article(conn, U2, e2["id"], errors=3)
        self.assertEqual([x["german"] for x in list_weak_words(self.db, U1, "article")], ["Tisch"])
        self.assertEqual([x["german"] for x in list_weak_words(self.db, U2, "article")], ["Lampe"])


class LearningIsolationTest(unittest.TestCase):
    """P1.3 — learning sessions (learning_items): each user sees only their own."""

    def setUp(self):
        self.db = _mk_db()
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)

    def test_learning_due_entries_per_user(self):
        e = _add_noun(self.db, "Buch", "книга")
        b1 = learning_get_or_create_batch(self.db, U1, 10)
        with sqlite3.connect(self.db) as conn:
            _learning_item(conn, U1, e["id"], b1)  # active+due for U1 only
        self.assertEqual(len(learning_due_entries(self.db, U1)), 1)
        self.assertEqual(len(learning_due_entries(self.db, U2)), 0)

    def test_active_count_per_user(self):
        e1 = _add_noun(self.db, "Buch", "книга")
        e2 = _add_noun(self.db, "Stuhl", "стул")
        b1 = learning_get_or_create_batch(self.db, U1, 10)
        b2 = learning_get_or_create_batch(self.db, U2, 10)
        with sqlite3.connect(self.db) as conn:
            _learning_item(conn, U1, e1["id"], b1)
            _learning_item(conn, U2, e2["id"], b2)
        self.assertEqual(learning_active_count(self.db, U1), 1)
        self.assertEqual(learning_active_count(self.db, U2), 1)
        # and the due lists don't bleed across users
        self.assertEqual([x["german"] for x in learning_due_entries(self.db, U1)], ["Buch"])
        self.assertEqual([x["german"] for x in learning_due_entries(self.db, U2)], ["Stuhl"])


class StatsIsolationTest(unittest.TestCase):
    """P1.4 — quiz_events / statistics / retention: per-user (dictionary size shared)."""

    def setUp(self):
        self.db = _mk_db()
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self.today = date.today().isoformat()

    def test_retention_is_per_user(self):
        e = _add_noun(self.db, "Hund", "собака")
        with sqlite3.connect(self.db) as conn:
            _event(conn, U1, e["id"], "ru_de", 1, f"{self.today} 08:00:00")  # U1 answered correctly
        r1 = get_daily_first_touch_retention(self.db, U1, modes=["ru_de"])
        r2 = get_daily_first_touch_retention(self.db, U2, modes=["ru_de"])
        self.assertEqual((r1["total"], r1["correct"]), (1, 1))
        self.assertEqual(r2["total"], 0)

    def test_snapshot_activity_per_user_dict_shared(self):
        e = _add_noun(self.db, "Hund", "собака")
        with sqlite3.connect(self.db) as conn:
            _event(conn, U1, e["id"], "ru_de", 1, f"{self.today} 09:00:00")
        s1 = get_statistics_snapshot(self.db, U1, "ru_de")
        s2 = get_statistics_snapshot(self.db, U2, "ru_de")
        # dictionary size is global (shared) — same for everyone
        self.assertEqual(s1["total_words"], s2["total_words"])
        # but per-user activity is isolated
        self.assertGreaterEqual(s1["today_complete"], 1)
        self.assertEqual(s2["today_complete"], 0)


class TransactionLogIsolationTest(unittest.TestCase):
    """P1.5 — transaction_log / undo: list + delete are per-user."""

    def setUp(self):
        self.db = _mk_db()
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)

    def test_list_is_per_user(self):
        add_transaction_log(self.db, U1, "quiz:ru_de", {"snapshot": {"x": 1}})
        self.assertEqual(len(list_transaction_logs(self.db, U1)), 1)
        self.assertEqual(len(list_transaction_logs(self.db, U2)), 0)

    def test_delete_cannot_touch_other_users_log(self):
        add_transaction_log(self.db, U1, "quiz:ru_de", {"a": 1})
        log_id = list_transaction_logs(self.db, U1)[0]["id"]
        # U2's /undo tries to delete U1's log by id → must NOT delete it
        delete_transaction_logs(self.db, U2, [log_id])
        self.assertEqual(len(list_transaction_logs(self.db, U1)), 1)
        # owner of the log can delete it
        delete_transaction_logs(self.db, U1, [log_id])
        self.assertEqual(len(list_transaction_logs(self.db, U1)), 0)


class ResetPeriodicityIsolationTest(unittest.TestCase):
    """P1.6 — a user's "reset periodicity" must wipe ONLY their own progress.

    Regression guard for the one audit finding: reset_periodicity used to
    `DELETE FROM progress` globally (would erase every user's schedule).
    """

    def setUp(self):
        self.db = _mk_db()
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self.today = date.today().isoformat()

    def _progress_count(self, user_id):
        with sqlite3.connect(self.db) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM progress WHERE user_id = ?", (user_id,)
            ).fetchone()[0]

    def test_reset_only_affects_caller(self):
        e = _add_noun(self.db, "Tor", "ворота")
        with sqlite3.connect(self.db) as conn:
            _progress(conn, U1, e["id"], "ru_de", self.today)
            _progress(conn, U2, e["id"], "ru_de", self.today)
        reset_periodicity(self.db, U1)
        self.assertEqual(self._progress_count(U1), 0)  # caller's schedule cleared
        self.assertEqual(self._progress_count(U2), 1)  # everyone else untouched


class LanguageIsolationTest(unittest.TestCase):
    """P2.3 — interface/dict language is per-user (stored in the users table)."""

    def setUp(self):
        self.db = _mk_db()
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)

    def test_interface_lang_per_user(self):
        set_user_interface_lang(self.db, U1, "ru")
        set_user_interface_lang(self.db, U2, "uk")
        self.assertEqual(get_user_languages(self.db, U1)[0], "ru")
        self.assertEqual(get_user_languages(self.db, U2)[0], "uk")

    def test_dict_lang_per_user(self):
        set_user_dict_lang(self.db, U1, "ru")
        set_user_dict_lang(self.db, U2, "uk")
        self.assertEqual(get_user_languages(self.db, U1)[1], "ru")
        self.assertEqual(get_user_languages(self.db, U2)[1], "uk")

    def test_unset_user_has_no_language(self):
        self.assertEqual(get_user_languages(self.db, U1), ("", ""))


class LanguageReaderTest(unittest.TestCase):
    """P2.3 — common.lang reader returns per-user value, falls back to global default."""

    def setUp(self):
        self.db = _mk_db()
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

    def test_reader_per_user_and_fallback(self):
        from bot.common.lang import _user_interface_lang

        set_user_interface_lang(self.db, U1, "uk")
        self.assertEqual(_user_interface_lang(U1), "uk")  # this user's own choice
        self.assertEqual(_user_interface_lang(U2), "en")  # unset → global default ("en")


if __name__ == "__main__":
    unittest.main()
