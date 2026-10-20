"""Learning input_err preserves the already displayed B with one original event."""

import asyncio
import unittest
from unittest.mock import patch

from tests import test_new_logic_learning as fixture
from bot.handlers import learning as handler


class LearningInputErrPreservesNextCardTest(unittest.TestCase):
    setUp = fixture.LearningTests.setUp
    update = fixture.LearningTests.update
    add = fixture.LearningTests.add
    rows = fixture.LearningTests.rows

    def test_wrong_a_then_input_err_keeps_b_without_reselecting_or_updating_it(self):
        self.add(("a", "b"), ready=True)
        asyncio.run(handler.learning_menu_choice(self.update("learning_start"), self.ctx))
        asyncio.run(handler.learning_quiz(self.update("wrong"), self.ctx))
        before = list(self.ctx.user_data["learning_recent_ids"])
        with patch.object(handler, "_pick_learning_due_entry", side_effect=AssertionError("picked")):
            asyncio.run(handler.learning_quiz(self.update("input_err"), self.ctx))
            asyncio.run(handler.learning_quiz(self.update("input_err"), self.ctx))
        self.assertEqual(self.ctx.user_data[handler.FRAME]["current"]["entry"]["id"], "b")
        self.assertEqual(self.ctx.user_data["learning_recent_ids"], before)
        self.assertEqual(len(self.rows("quiz_events")), 1)
        self.assertEqual(self.rows("quiz_events")[0]["is_correct"], 1)
        self.assertEqual(len(self.rows("transaction_log")), 2)


if __name__ == "__main__":
    unittest.main()
