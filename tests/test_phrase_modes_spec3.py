import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.common.lang import direction_mode_family, phrase_mode, selected_direction_mode
from bot.handlers.clarify_fields import _add_int_mode_for_return_state
from bot.handlers.menu import quiz_ru_de_menu_choice
from bot.handlers.quiz import _check_ru_de_strict
from bot.app_config import QUIZ_RU_DE
from bot.storage import (
    get_daily_first_touch_retention,
    get_statistics_snapshot,
    init_db,
    log_quiz_event,
    select_due,
)
from bot.ui.formatting import _format_de_ru_question


class PhraseModeHelpersTest(unittest.TestCase):
    def test_exact_phrase_modes_and_direction_family(self):
        self.assertEqual(phrase_mode("ru_de"), "phrase_ru_de")
        self.assertEqual(phrase_mode("de_uk"), "phrase_de_uk")
        self.assertEqual(direction_mode_family("uk_de"), ("uk_de", "phrase_uk_de"))
        self.assertEqual(direction_mode_family("phrase_de_en"), ("de_en", "phrase_de_en"))

    def test_non_direction_modes_are_rejected(self):
        for value in ("learning", "article", "pl_sg_ru", "", "phrase_learning"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                direction_mode_family(value)

    def test_selected_mode_and_clarify_add_int_keep_exact_phrase_mode(self):
        context = SimpleNamespace(user_data={"quiz_mode": "phrase_ru_de"})
        self.assertEqual(selected_direction_mode(context, "ru_de"), "phrase_ru_de")
        with patch("bot.handlers.clarify_fields._mode_l1_de", return_value="ru_de"):
            self.assertEqual(
                _add_int_mode_for_return_state(1, QUIZ_RU_DE, "phrase_ru_de"),
                "phrase_ru_de",
            )


class PhraseDueAndStatsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "words.db")
        init_db(self.db)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executemany(
                "INSERT INTO words (id, type, german, russian) VALUES (?, ?, ?, ?)",
                [
                    ("word", "nomen", "Haus", "дом"),
                    ("phrase", "phrase", "die Tafel Schokolade", "плитка шоколада"),
                ],
            )
            # Both cards have non-learning history and are therefore eligible.
            conn.executemany(
                "INSERT INTO quiz_events (user_id, word_id, mode, is_correct) VALUES (1, ?, ?, 1)",
                [("word", "ru_de"), ("phrase", "phrase_ru_de")],
            )
            conn.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def test_due_selection_isolates_words_and_phrases_by_exact_mode(self):
        word_due = select_due(self.db, user_id=1, mode="ru_de", today=date.today())
        phrase_due = select_due(self.db, user_id=1, mode="phrase_ru_de", today=date.today())
        self.assertEqual([row["id"] for row in word_due], ["word"])
        self.assertEqual([row["id"] for row in phrase_due], ["phrase"])

    def test_direction_statistics_combine_word_and_phrase_events(self):
        log_quiz_event(self.db, 1, "word", "ru_de", False)
        log_quiz_event(self.db, 1, "phrase", "phrase_ru_de", True)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executemany(
                """
                INSERT INTO progress (user_id, word_id, mode, interval, repetitions, ease, due)
                VALUES (1, ?, ?, 1, ?, 2.5, ?)
                """,
                [
                    ("word", "ru_de", 2, date.today().isoformat()),
                    ("phrase", "phrase_ru_de", 5, date.today().isoformat()),
                ],
            )
            conn.commit()
        stats = get_statistics_snapshot(self.db, user_id=1, mode="ru_de")
        self.assertEqual(stats["today_complete_fsrs_ru_de"], 2)
        self.assertEqual(stats["wrong_fsrs"], 1)
        self.assertEqual(stats["streak_2"], 1)
        self.assertEqual(stats["streak_5p"], 1)
        daily = get_daily_first_touch_retention(
            self.db,
            user_id=1,
            target_date=date.today().isoformat(),
            modes=direction_mode_family("ru_de"),
        )
        self.assertEqual(daily["total"], 2)


class PhraseCasingTest(unittest.TestCase):
    def test_de_to_l1_phrase_preserves_database_case(self):
        prompt = _format_de_ru_question(
            {"type": "phrase", "german": "die Tafel Schokolade", "image": "", "example": ""}
        )
        self.assertIn("die Tafel Schokolade", prompt)
        self.assertNotIn("die tafel schokolade", prompt)

    def test_l1_to_de_phrase_answer_is_case_insensitive(self):
        entry = {
            "id": "phrase",
            "type": "phrase",
            "german": "die Tafel Schokolade",
            "russian": "плитка шоколада",
        }
        ok, _ = _check_ru_de_strict(entry, "DIE TAFEL SCHOKOLADE")
        self.assertTrue(ok)


class PhraseSelectionStateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # These tests isolate routing only. Real atomic entry/reset and failed
        # send recovery use in-memory SQLite plus actual PTB in the oral suite.
        self.enterContext(patch('bot.written.service.preflight', return_value=None))

    async def test_l1_to_de_phrase_delegates_before_resetting_existing_buckets(self):
        from copy import deepcopy
        from bot.app_config import ORAL_QUIZ
        context = SimpleNamespace(user_data={
            "quiz_direction_base": "ru_de",
            "quiz_entry": {"id": "stale-word"},
            "quiz_last_answered_entry": {"id": "stale-word"},
            "retry_queue": {"ru_de": ["word"], "phrase_ru_de": ["old-phrase"]},
            "retry_counts": {"ru_de": {"word": 1}, "phrase_ru_de": {"old-phrase": 2}},
            "retry_since_last": {"ru_de": 3, "phrase_ru_de": 4},
            "fsrs_cycle_queue": {"ru_de": [{"id": "word"}], "phrase_ru_de": [{"id": "old-phrase"}]},
            "session_seen_phrase_ru_de": {"old-phrase"},
            "session_correct_phrase_ru_de": 4,
            "session_wrong_phrase_ru_de": 2,
        })
        before = deepcopy(context.user_data)
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1),
                                 message=SimpleNamespace(text="Phrases"))
        with (
            patch("bot.handlers.menu._action_key_from_text", return_value="quiz_phrases"),
            patch("bot.handlers.menu.start_oral_quiz_locked", new=AsyncMock(return_value=ORAL_QUIZ)) as sender,
            patch("bot.handlers.menu._send_ru_de_prompt", side_effect=AssertionError('written phrase route')),
        ):
            state = await quiz_ru_de_menu_choice(update, context)
        self.assertEqual(state, ORAL_QUIZ)
        self.assertEqual(context.user_data, before)
        sender.assert_awaited_once_with(update, context, "phrase_ru_de")

    async def test_de_to_l1_phrase_selection_uses_exact_oral_mode(self):
        from bot.app_config import ORAL_QUIZ
        context = SimpleNamespace(user_data={"quiz_direction_base": "de_uk"})
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1),
                                 message=SimpleNamespace(text="Фрази"))
        with (
            patch("bot.handlers.menu._action_key_from_text", return_value="quiz_phrases"),
            patch("bot.handlers.menu.start_oral_quiz_locked", new=AsyncMock(return_value=ORAL_QUIZ)) as sender,
            patch("bot.handlers.menu._send_de_ru_prompt", side_effect=AssertionError('written phrase route')),
        ):
            state = await quiz_ru_de_menu_choice(update, context)
        self.assertEqual(state, ORAL_QUIZ)
        self.assertEqual(context.user_data, {"quiz_direction_base": "de_uk"})
        sender.assert_awaited_once_with(update, context, "phrase_de_uk")

    async def test_words_l1_to_de_keep_written_entry_and_exact_mode(self):
        context = SimpleNamespace(user_data={"quiz_direction_base": "ru_de"})
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1),
                                 message=SimpleNamespace(text="Words"))
        with (
            patch("bot.handlers.menu._action_key_from_text", return_value="quiz_words"),
            patch("bot.handlers.menu._send_ru_de_prompt", new=AsyncMock(return_value=QUIZ_RU_DE)) as sender,
            patch("bot.handlers.menu.start_oral_quiz_locked", side_effect=AssertionError('oral written-word route')),
        ):
            state = await quiz_ru_de_menu_choice(update, context)
        self.assertEqual(state, QUIZ_RU_DE)
        self.assertEqual(context.user_data["quiz_mode"], "ru_de")
        sender.assert_awaited_once_with(update, context, None)


if __name__ == "__main__":
    unittest.main()
