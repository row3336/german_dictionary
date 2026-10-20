"""Learning show-order + mix-selection contract.

Two independent layers, verified separately:

* Show layer (handlers.learning._pick_learning_due_entry) — STATIC rule, never
  affected by mix mode: first round (step 0) strictly in preview order; later
  rounds (step > 0) random but no word within the last two shows.
* Selection layer (storage.learning_pick_new_words) — the ONLY place mix acts:
  mix off = in added order; mix on = proportional interleave across topics, and
  a single topic degrades to plain in-order selection without errors.
"""
import os
import random
import sqlite3
import tempfile
import types
import unittest
import warnings

from bot.handlers.learning import _note_learning_shown, _pick_learning_due_entry
from bot.storage import (
    _mix_by_topic_proportional,
    add_word,
    init_db,
    learning_add_words,
    learning_get_or_create_batch,
    learning_pick_new_words,
    learning_update_result,
)

warnings.filterwarnings("ignore", category=ResourceWarning)


def _mk_db():
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.close()
    return tmp.name


def _add(db_path, german, topic):
    return add_word(
        db_path,
        {
            "id": "unique",
            "type": "nomen",
            "article": "der",
            "german": german,
            "plural": "",
            "infinitive": "",
            "prateritum": "",
            "partizip2": "",
            "russian": german.lower(),
            "ukrainian": "",
            "english": "",
            "topic": topic,
        },
    )


def _ctx():
    return types.SimpleNamespace(user_data={})


class LearningShowOrderTests(unittest.TestCase):
    def setUp(self):
        self.db = _mk_db()
        self.addCleanup(lambda: os.unlink(self.db))
        os.environ["DB_PATH"] = self.db  # get_db_path() resolves DB via DB_PATH
        self.addCleanup(lambda: os.environ.pop("DB_PATH", None))
        init_db(self.db)

    def _enqueue_due(self, words, step_index):
        """Put words into learning as due now, at the given step, with distinct
        created_at matching list order (so preview order is deterministic)."""
        batch_id = learning_get_or_create_batch(self.db, user_id=1, daily_qty=99)
        learning_add_words(self.db, user_id=1, words=words, batch_id=batch_id, first_delay_seconds=0)
        with sqlite3.connect(self.db) as conn:
            for i, w in enumerate(words):
                conn.execute(
                    "UPDATE learning_items SET preview_done = 1, first_due_pending = 0, step_index = ?, "
                    "next_due = '2000-01-01T00:00:00Z', created_at = ? "
                    "WHERE user_id = 1 AND word_id = ?",
                    (step_index, f"2000-01-01 00:00:{i:02d}", w["id"]),
                )

    def test_first_round_follows_preview_order(self):
        words = [_add(self.db, g, "1") for g in ("Apfel", "Katze", "Haus", "Buch")]
        self._enqueue_due(words, step_index=0)
        ctx = _ctx()
        seen = []
        for _ in range(len(words)):
            entry = _pick_learning_due_entry(ctx, user_id=1)
            seen.append(entry["german"])
            _note_learning_shown(ctx, entry["id"])
            # answering advances the word out of the first round (step 0 -> 1)
            learning_update_result(self.db, user_id=1, word_id=entry["id"], ok=True, intervals_seconds=[0, 0, 0, 0])
        self.assertEqual(seen, ["Apfel", "Katze", "Haus", "Buch"])

    def test_first_round_order_ignores_mix_mode(self):
        # Show layer must not read mix mode — order is identical with it toggled on.
        from bot.common.settings import _set_learning_mix_mode

        words = [_add(self.db, g, str(i % 2)) for i, g in enumerate(("Apfel", "Katze", "Haus", "Buch"))]
        self._enqueue_due(words, step_index=0)
        _set_learning_mix_mode(1, True)
        ctx = _ctx()
        seen = []
        for _ in range(len(words)):
            entry = _pick_learning_due_entry(ctx, user_id=1)
            seen.append(entry["german"])
            _note_learning_shown(ctx, entry["id"])
            learning_update_result(self.db, user_id=1, word_id=entry["id"], ok=True, intervals_seconds=[0, 0, 0, 0])
        self.assertEqual(seen, ["Apfel", "Katze", "Haus", "Buch"])

    def test_step_zero_reset_does_not_repeat_before_later_step_alternative(self):
        reset_word = _add(self.db, "Apfel", "1")
        alternative = _add(self.db, "Birne", "1")
        self._enqueue_due([reset_word], step_index=0)
        self._enqueue_due([alternative], step_index=3)
        ctx = _ctx()
        ctx.user_data["learning_recent_ids"] = [reset_word["id"]]

        entry = _pick_learning_due_entry(ctx, user_id=1)

        self.assertEqual(entry["id"], alternative["id"])

    def test_later_rounds_spacing_no_repeat_within_three(self):
        words = [_add(self.db, g, "1") for g in ("Apfel", "Katze", "Haus", "Buch")]
        self._enqueue_due(words, step_index=1)  # all past the first round, stay due
        random.seed(0)
        ctx = _ctx()
        shown = []
        for _ in range(40):
            entry = _pick_learning_due_entry(ctx, user_id=1)
            wid = entry["id"]
            # invariant: not equal to either of the last two shows (>= once per three)
            self.assertNotIn(wid, shown[-2:])
            shown.append(wid)
            _note_learning_shown(ctx, wid)
        # sanity: randomization actually happened (not a fixed cycle)
        self.assertGreater(len(set(shown)), 1)

    def test_later_rounds_two_words_alternate_without_immediate_repeat(self):
        # Pool too small for the full 1-in-3 window — must relax yet never repeat back-to-back.
        words = [_add(self.db, g, "1") for g in ("Apfel", "Katze")]
        self._enqueue_due(words, step_index=1)
        random.seed(1)
        ctx = _ctx()
        prev = None
        for _ in range(20):
            entry = _pick_learning_due_entry(ctx, user_id=1)
            self.assertNotEqual(entry["id"], prev)
            prev = entry["id"]
            _note_learning_shown(ctx, entry["id"])

    def test_empty_queue_returns_none(self):
        self.assertIsNone(_pick_learning_due_entry(_ctx(), user_id=1))


class LearningMixSelectionTests(unittest.TestCase):
    def setUp(self):
        self.db = _mk_db()
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)

    def _add_ordered(self, specs):
        """specs = [(german, topic), ...]; set distinct created_at in list order."""
        words = [_add(self.db, g, tp) for g, tp in specs]
        with sqlite3.connect(self.db) as conn:
            for i, w in enumerate(words):
                conn.execute("UPDATE words SET created_at = ? WHERE id = ?", (f"2000-01-01 00:00:{i:02d}", w["id"]))
        return words

    def test_mix_off_takes_words_in_order(self):
        self._add_ordered([("Apfel", "1"), ("Katze", "1"), ("Haus", "1"), ("Buch", "1")])
        picked = learning_pick_new_words(self.db, user_id=1, limit=3, mix=False)
        self.assertEqual([p["german"] for p in picked], ["Apfel", "Katze", "Haus"])

    def test_mix_on_single_topic_takes_from_that_topic_no_error(self):
        self._add_ordered([("Apfel", "7"), ("Katze", "7"), ("Haus", "7")])
        random.seed(0)
        picked = learning_pick_new_words(self.db, user_id=1, limit=3, topics=["7"], mix=True)
        self.assertEqual(len(picked), 3)
        self.assertEqual({p["topic"] for p in picked}, {"7"})

    def test_mix_on_multi_topic_represents_all_topics(self):
        self._add_ordered(
            [("A1", "A"), ("A2", "A"), ("A3", "A"), ("B1", "B"), ("B2", "B"), ("B3", "B")]
        )
        random.seed(0)
        picked = learning_pick_new_words(self.db, user_id=1, limit=6, topics=["A", "B"], mix=True)
        self.assertEqual(len(picked), 6)
        self.assertEqual({p["topic"] for p in picked}, {"A", "B"})

    def test_mix_by_topic_proportional_single_bucket_keeps_order(self):
        entries = [{"id": str(i), "topic": "1", "german": g} for i, g in enumerate(("a", "b", "c"))]
        mixed = _mix_by_topic_proportional(entries, limit=3)
        self.assertEqual([e["german"] for e in mixed], ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
