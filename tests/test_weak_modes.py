import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
import warnings

from bot.storage import (
    add_word,
    apply_weak_input_err,
    get_form_review_stage,
    get_form_state,
    get_statistics_snapshot,
    init_db,
    list_weak_words,
    update_form_stats,
    update_weak_review_state,
)

warnings.filterwarnings("ignore", category=ResourceWarning)


def _mk_db():
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.close()
    return tmp.name


def _add_noun(db_path, german="Jacke", russian="куртка"):
    return add_word(
        db_path,
        {
            "id": "unique",
            "type": "nomen",
            "article": "die",
            "german": german,
            "plural": "Jacken",
            "infinitive": "",
            "prateritum": "",
            "partizip2": "",
            "russian": russian,
            "ukrainian": "",
            "english": "",
            "topic": "1",
        },
    )


def _add_verb(db_path, infinitive="gehen", russian="идти"):
    return add_word(
        db_path,
        {
            "id": "unique",
            "type": "verb",
            "article": "",
            "german": infinitive,
            "plural": "",
            "infinitive": infinitive,
            "prateritum": "ging",
            "partizip2": "gegangen",
            "russian": russian,
            "ukrainian": "",
            "english": "",
            "topic": "1",
        },
    )


class WeakModesTests(unittest.TestCase):
    def test_word_enters_active_weak_after_two_errors(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        entry = _add_noun(db_path)
        update_form_stats(db_path, 1, entry["id"], {"article": False})
        update_form_stats(db_path, 1, entry["id"], {"article": False})
        state = get_form_state(db_path, 1, entry["id"], "article")
        self.assertTrue(state["active"])
        due = list_weak_words(db_path, 1, "article", word_type="nomen")
        self.assertEqual(len(due), 1)

    def test_active_weak_moves_to_review_stage_1_after_three_correct(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        entry = _add_noun(db_path)
        update_form_stats(db_path, 1, entry["id"], {"article": False})
        update_form_stats(db_path, 1, entry["id"], {"article": False})
        update_form_stats(db_path, 1, entry["id"], {"article": True})
        update_form_stats(db_path, 1, entry["id"], {"article": True})
        update_form_stats(db_path, 1, entry["id"], {"article": True})
        stage = get_form_review_stage(db_path, 1, entry["id"], "article")
        self.assertEqual(stage, 1)
        due_today = list_weak_words(db_path, 1, "article", word_type="nomen")
        self.assertEqual(due_today, [])

    def test_review_stage_progression(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        entry = _add_noun(db_path)
        update_form_stats(db_path, 1, entry["id"], {"article": False})
        update_form_stats(db_path, 1, entry["id"], {"article": False})
        update_form_stats(db_path, 1, entry["id"], {"article": True})
        update_form_stats(db_path, 1, entry["id"], {"article": True})
        update_form_stats(db_path, 1, entry["id"], {"article": True})

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE form_stats SET article_next_review = ? WHERE user_id = ? AND word_id = ?",
                (date.today().isoformat(), 1, entry["id"]),
            )

        self.assertEqual(update_weak_review_state(db_path, 1, entry["id"], "article", True), 2)
        self.assertEqual(update_weak_review_state(db_path, 1, entry["id"], "article", False), 1)
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT article_next_review FROM form_stats WHERE user_id = ? AND word_id = ?",
                (1, entry["id"]),
            ).fetchone()
        self.assertEqual(row[0], (date.today() + timedelta(days=1)).isoformat())
        self.assertEqual(update_weak_review_state(db_path, 1, entry["id"], "article", True), 2)
        self.assertEqual(update_weak_review_state(db_path, 1, entry["id"], "article", True), 3)
        self.assertEqual(update_weak_review_state(db_path, 1, entry["id"], "article", True), 0)

    def test_two_non_consecutive_errors_do_not_make_weak(self):
        # #18: wrong-correct-wrong (2 unrelated errors) must NOT enter weak —
        # entry now needs errors>=3 total OR 2 wrong in a row.
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        entry = _add_noun(db_path)
        update_form_stats(db_path, 1, entry["id"], {"article": False})
        update_form_stats(db_path, 1, entry["id"], {"article": True})
        update_form_stats(db_path, 1, entry["id"], {"article": False})
        self.assertFalse(get_form_state(db_path, 1, entry["id"], "article")["active"])
        self.assertEqual(list_weak_words(db_path, 1, "article", word_type="nomen"), [])

    def test_three_total_errors_enter_weak(self):
        # #18: three errors in total (even spread out) cross the entry threshold.
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        entry = _add_noun(db_path)
        update_form_stats(db_path, 1, entry["id"], {"article": False})
        update_form_stats(db_path, 1, entry["id"], {"article": True})
        update_form_stats(db_path, 1, entry["id"], {"article": False})
        update_form_stats(db_path, 1, entry["id"], {"article": True})
        update_form_stats(db_path, 1, entry["id"], {"article": False})
        self.assertTrue(get_form_state(db_path, 1, entry["id"], "article")["active"])
        self.assertEqual(len(list_weak_words(db_path, 1, "article", word_type="nomen")), 1)

    def test_entered_word_is_held_until_three_correct_in_a_row(self):
        # #18 core: a word entered via 2-wrong-in-a-row must stay weak after a
        # single correct (the bug the episode flag fixes), and only leave the
        # quiz once it earns 3 correct in a row → review ladder.
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        entry = _add_noun(db_path)
        update_form_stats(db_path, 1, entry["id"], {"article": False})
        update_form_stats(db_path, 1, entry["id"], {"article": False})
        self.assertTrue(get_form_state(db_path, 1, entry["id"], "article")["active"])
        # one correct: still held (would have dropped under the old errors-only rule)
        update_form_stats(db_path, 1, entry["id"], {"article": True})
        self.assertTrue(get_form_state(db_path, 1, entry["id"], "article")["active"])
        self.assertEqual(len(list_weak_words(db_path, 1, "article", word_type="nomen")), 1)
        # two correct: still held
        update_form_stats(db_path, 1, entry["id"], {"article": True})
        self.assertTrue(get_form_state(db_path, 1, entry["id"], "article")["active"])
        # third correct in a row: clears the episode and promotes to ladder
        update_form_stats(db_path, 1, entry["id"], {"article": True})
        self.assertFalse(get_form_state(db_path, 1, entry["id"], "article")["active"])
        self.assertEqual(get_form_review_stage(db_path, 1, entry["id"], "article"), 1)

    def test_input_err_backs_out_a_wrongly_entered_episode(self):
        # #18: input_err reverses the last (mis-matched) wrong → if the reverted
        # counters no longer meet the entry threshold, the word leaves weak.
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)
        entry = _add_noun(db_path)
        update_form_stats(db_path, 1, entry["id"], {"article": False})
        update_form_stats(db_path, 1, entry["id"], {"article": False})
        self.assertTrue(get_form_state(db_path, 1, entry["id"], "article")["active"])
        self.assertTrue(apply_weak_input_err(db_path, 1, entry["id"], "article"))
        self.assertFalse(get_form_state(db_path, 1, entry["id"], "article")["active"])
        self.assertEqual(list_weak_words(db_path, 1, "article", word_type="nomen"), [])

    def test_statistics_weak_breakdown_active_has_priority_over_review_stages(self):
        db_path = _mk_db()
        self.addCleanup(lambda: os.unlink(db_path))
        init_db(db_path)

        active = _add_noun(db_path, german="Apfel", russian="яблоко")
        stage1 = _add_noun(db_path, german="Katze", russian="кошка")
        active_with_stage = _add_noun(db_path, german="Jacke", russian="куртка")
        verb = _add_verb(db_path, infinitive="sehen", russian="видеть")

        update_form_stats(db_path, 1, active["id"], {"article": False})
        update_form_stats(db_path, 1, active["id"], {"article": False})

        update_form_stats(db_path, 1, stage1["id"], {"article": False})
        update_form_stats(db_path, 1, stage1["id"], {"article": False})
        update_form_stats(db_path, 1, stage1["id"], {"article": True})
        update_form_stats(db_path, 1, stage1["id"], {"article": True})
        update_form_stats(db_path, 1, stage1["id"], {"article": True})

        update_form_stats(db_path, 1, active_with_stage["id"], {"article": False})
        update_form_stats(db_path, 1, active_with_stage["id"], {"article": False})

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE form_stats SET article_review_stage = 2 WHERE user_id = ? AND word_id = ?",
                (1, active_with_stage["id"]),
            )
            conn.execute(
                "INSERT INTO form_stats (user_id, word_id, article_review_stage) VALUES (?, ?, ?)",
                (1, verb["id"], 1),
            )

        stats = get_statistics_snapshot(db_path, user_id=1, mode="ru_de")
        self.assertEqual(stats["weak_article"], 2)
        self.assertEqual(stats["weak_article_stage1"], 1)
        self.assertEqual(stats["weak_article_stage2"], 0)
        self.assertEqual(stats["weak_article_stage3"], 0)


if __name__ == "__main__":
    unittest.main()
