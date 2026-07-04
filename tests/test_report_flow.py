"""P6.2 — clarify handler flow: report_comment saves + notifies; delete needs yes.

Exercises clarify_fields_input directly (not just keyboards/storage). openai is
imported lazily by bot.ai, so this runs without the openai package; telegram is
stubbed.
"""

import asyncio
import os
import sqlite3
import tempfile
import unittest

from _telegram_stub import install

install()

from bot.app_config import QUIZ_RU_DE  # noqa: E402
from bot.handlers.clarify_fields import clarify_fields_input  # noqa: E402
from bot.storage import init_db, list_error_reports  # noqa: E402

OWNER, GUEST = 999, 111


class _Msg:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class _User:
    def __init__(self, uid, username=None, first_name=""):
        self.id = uid
        self.username = username
        self.first_name = first_name


class _Update:
    def __init__(self, text, user):
        self.message = _Msg(text)
        self.effective_user = user


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class _Ctx:
    def __init__(self, user_data):
        self.user_data = user_data
        self.bot = _Bot()


def _run(coro):
    return asyncio.run(coro)


def _word_exists(db, wid):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT 1 FROM words WHERE id=?", (wid,)).fetchone() is not None
    finally:
        conn.close()


class ReportFlowTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO words (id, type, german, russian) VALUES ('w1','nomen','Haus','дом')")
        conn.commit()
        conn.close()
        self._old = {k: os.environ.get(k) for k in ("DB_PATH", "OWNER_ID")}
        os.environ["DB_PATH"] = self.db
        os.environ["OWNER_ID"] = str(OWNER)
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_report_comment_saves_no_instant_owner_ping(self):
        # spec #5: the report is persisted, but NOT pushed to the owner instantly
        # (no message, no #error_report tag) — the owner sees it in the digest/inbox.
        ctx = _Ctx({
            "edit_source": "quiz",
            "edit_stage": "report_comment",
            "quiz_edit_entry": {"id": "w1", "german": "Haus"},
            "edit_return_state": QUIZ_RU_DE,  # no quiz_entry -> resume falls back to tests menu
        })
        upd = _Update("plural looks wrong", _User(GUEST, username="bob"))
        _run(clarify_fields_input(upd, ctx))
        reports = list_error_reports(self.db, status="pending")
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["comment"], "plural looks wrong")
        self.assertEqual(reports[0]["user_id"], GUEST)
        # no instant owner notification, no tag
        self.assertFalse(any("#error_report" in m for _cid, m in ctx.bot.sent))

    def test_delete_confirm_yes_deletes_word(self):
        ctx = _Ctx({
            "edit_source": "quiz",
            "edit_stage": "delete_confirm",
            "quiz_edit_entry": {"id": "w1", "german": "Haus"},
            "edit_return_state": QUIZ_RU_DE,
        })
        _run(clarify_fields_input(_Update("yes", _User(OWNER)), ctx))
        self.assertFalse(_word_exists(self.db, "w1"))

    def test_delete_confirm_non_yes_keeps_word(self):
        ctx = _Ctx({
            "edit_source": "quiz",
            "edit_stage": "delete_confirm",
            "quiz_edit_entry": {"id": "w1", "german": "Haus"},
            "edit_return_state": QUIZ_RU_DE,
        })
        _run(clarify_fields_input(_Update("nope", _User(OWNER)), ctx))
        self.assertTrue(_word_exists(self.db, "w1"))


if __name__ == "__main__":
    unittest.main()
