"""#0 — a guest can open the clarify card (image/example, P4) from the learning
session, matching quiz/db_search. `learning.py` used to gate the
`clarify_fields` button behind `is_owner()`, so a guest's tap fell through to
the answer-handling logic instead of opening the card (visible as the keyboard
collapsing to next/skip/back with no clarify screen, and in normal mode the
button text being scored as a wrong FSRS answer).
"""

import asyncio
import os
import tempfile
import unittest

from _telegram_stub import install

install()

from bot.app_config import ADD_EDIT, AI_CLARIFY_FIELD, LEARNING_QUIZ  # noqa: E402
from bot.handlers import learning as L  # noqa: E402
from bot.i18n import t  # noqa: E402
from bot.storage import add_word, init_db, register_or_update_user, set_user_status  # noqa: E402

OWNER, GUEST = 999, 111


class _Msg:
    def __init__(self, text=AI_CLARIFY_FIELD):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        return type("Sent", (), {"message_id": 1})()


class _Update:
    def __init__(self, uid, text=AI_CLARIFY_FIELD):
        self.message = _Msg(text)
        self.effective_user = type("U", (), {"id": uid, "username": "u", "first_name": "U"})()
        self.effective_chat = type("C", (), {"id": uid})()


class _Ctx:
    def __init__(self, user_data=None):
        self.user_data = user_data or {}


def _run(coro):
    return asyncio.run(coro)


class LearningClarifyGateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = {k: os.environ.get(k) for k in ("DB_PATH", "OWNER_ID")}
        os.environ["DB_PATH"] = self.db
        os.environ["OWNER_ID"] = str(OWNER)
        self.addCleanup(self._restore)
        register_or_update_user(self.db, GUEST, "g", "G")
        set_user_status(self.db, GUEST, "approved")
        self.entry = add_word(self.db, {
            "type": "nomen", "article": "der", "german": "Apfel",
            "plural": "Äpfel", "russian": "яблоко", "topic": "1",
        })
        self.hidden = add_word(self.db, {
            "type": "verb", "german": "gehen", "infinitive": "gehen",
            "russian": "идти", "topic": "1",
        })

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_guest_opens_clarify_from_learning_normal_mode(self):
        ctx = _Ctx({"quiz_entry": self.hidden, "quiz_last_answered_entry": self.entry,
                    "learning_preview_mode": False})
        state = _run(L.learning_quiz(_Update(GUEST), ctx))
        self.assertEqual(state, ADD_EDIT)
        self.assertEqual(ctx.user_data.get("edit_source"), "quiz")
        self.assertEqual(ctx.user_data.get("edit_stage"), "pick_field")
        self.assertEqual(ctx.user_data["quiz_edit_entry"]["id"], self.entry["id"])
        self.assertEqual(ctx.user_data["quiz_entry"]["id"], self.hidden["id"])

    def test_guest_opens_clarify_from_learning_preview_mode(self):
        # Preview mode used to fall into the "Use next/skip/back." dead end.
        ctx = _Ctx({"quiz_entry": self.entry, "learning_preview_mode": True})
        state = _run(L.learning_quiz(_Update(GUEST), ctx))
        self.assertEqual(state, ADD_EDIT)
        self.assertEqual(ctx.user_data.get("edit_source"), "quiz")

    def test_owner_still_opens_clarify_from_learning(self):
        ctx = _Ctx({"quiz_entry": self.hidden, "quiz_last_answered_entry": self.entry,
                    "learning_preview_mode": False})
        state = _run(L.learning_quiz(_Update(OWNER), ctx))
        self.assertEqual(state, ADD_EDIT)
        self.assertEqual(ctx.user_data["quiz_edit_entry"]["id"], self.entry["id"])
        self.assertEqual(ctx.user_data["quiz_entry"]["id"], self.hidden["id"])

    def test_first_hidden_question_requires_answer_for_both_roles(self):
        # REQ-Q01 / D22: the first hidden question is never a clarify target.
        for uid in (OWNER, GUEST):
            with self.subTest(uid=uid):
                ctx = _Ctx({"quiz_entry": self.hidden, "learning_preview_mode": False})
                upd = _Update(uid)
                state = _run(L.learning_quiz(upd, ctx))
                self.assertEqual(state, LEARNING_QUIZ)
                self.assertEqual(upd.message.replies, [t("en", "clarify_answer_first")])
                self.assertNotIn("quiz_edit_entry", ctx.user_data)
                self.assertNotIn("edit_source", ctx.user_data)
                self.assertEqual(ctx.user_data["quiz_entry"]["id"], self.hidden["id"])


if __name__ == "__main__":
    unittest.main()
