"""Session summary + report counting (prod report 2026-07-07).

Two guarantees the owner asked for:
1. The end-of-session summary is per-word: correct+wrong == distinct words seen.
   In-session FSRS retries must NOT re-tally (they used to, so a word answered
   twice pushed correct+wrong above 'session words' — e.g. 50+3 over 49 words).
2. input_err (a typo, not a real miss) counts as correct — both in the session
   summary (flip wrong->correct) and in the daily/weekly first-touch report
   (the last wrong event is flipped in place, not appended, so first-touch sees
   it as correct).
"""

import os
import tempfile
import unittest

from _telegram_stub import install

install()

from bot.common.session import (  # noqa: E402
    _bump_session_result,
    _flip_session_result_to_correct,
    _mark_session_word,
    _reset_session,
    _session_correct_count,
    _session_count,
    _session_wrong_count,
)
from bot.storage import (  # noqa: E402
    QUIZ_RU_DE,
    add_word,
    get_daily_first_touch_retention,
    init_db,
    log_quiz_event,
    mark_last_quiz_event_correct,
    register_or_update_user,
)

MODE = "ru_de"


class _Ctx:
    def __init__(self):
        self.user_data = {}


class SessionSummaryTest(unittest.TestCase):
    def _answer(self, ctx, word_id, ok, is_retry=False):
        if not is_retry:
            _mark_session_word(ctx, MODE, word_id)
        _bump_session_result(ctx, MODE, ok, is_retry=is_retry)

    def test_sum_equals_distinct_words_with_retries(self):
        ctx = _Ctx()
        _reset_session(ctx, MODE)
        # 3 distinct words, first-touch: w1 correct, w2 wrong, w3 correct
        self._answer(ctx, "w1", True)
        self._answer(ctx, "w2", False)
        self._answer(ctx, "w3", True)
        # w2 comes back as an in-session retry and is now answered correctly
        self._answer(ctx, "w2", True, is_retry=True)
        # ...and again
        self._answer(ctx, "w2", True, is_retry=True)
        correct = _session_correct_count(ctx, MODE)
        wrong = _session_wrong_count(ctx, MODE)
        words = _session_count(ctx, MODE)
        self.assertEqual(words, 3)
        self.assertEqual(correct, 2)
        self.assertEqual(wrong, 1)
        self.assertEqual(correct + wrong, words)  # the whole point: sum matches

    def test_input_err_flip_moves_wrong_to_correct(self):
        ctx = _Ctx()
        _reset_session(ctx, MODE)
        self._answer(ctx, "w1", False)  # first-touch wrong
        self.assertEqual((_session_correct_count(ctx, MODE), _session_wrong_count(ctx, MODE)), (0, 1))
        _flip_session_result_to_correct(ctx, MODE)  # input_err correction
        self.assertEqual((_session_correct_count(ctx, MODE), _session_wrong_count(ctx, MODE)), (1, 0))
        # still one distinct word, sum still matches
        self.assertEqual(_session_correct_count(ctx, MODE) + _session_wrong_count(ctx, MODE), _session_count(ctx, MODE))

    def test_flip_noop_when_nothing_wrong(self):
        ctx = _Ctx()
        _reset_session(ctx, MODE)
        _flip_session_result_to_correct(ctx, MODE)  # no wrong to flip
        self.assertEqual((_session_correct_count(ctx, MODE), _session_wrong_count(ctx, MODE)), (0, 0))


class ReportFlipTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)

    def test_input_err_flip_makes_first_touch_correct(self):
        uid = 111
        register_or_update_user(self.db, uid, "g", "G")
        wid = add_word(self.db, {"type": "nomen", "article": "die", "german": "Sache", "plural": "Sachen", "russian": "вещь", "topic": "1"})["id"]
        log_quiz_event(self.db, user_id=uid, word_id=wid, mode=QUIZ_RU_DE, is_correct=False)
        before = get_daily_first_touch_retention(self.db, uid, modes=(QUIZ_RU_DE,))
        self.assertEqual((before["total"], before["correct"]), (1, 0))  # wrong first-touch
        flipped = mark_last_quiz_event_correct(self.db, uid, wid, QUIZ_RU_DE)
        self.assertTrue(flipped)
        after = get_daily_first_touch_retention(self.db, uid, modes=(QUIZ_RU_DE,))
        self.assertEqual((after["total"], after["correct"]), (1, 1))  # now counts correct
        self.assertEqual(after["rate"], 100.0)

    def test_flip_returns_false_when_no_event(self):
        self.assertFalse(mark_last_quiz_event_correct(self.db, 999, 777, QUIZ_RU_DE))


if __name__ == "__main__":
    unittest.main()
