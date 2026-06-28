"""P6.3 (8a) — owner review of error reports: list -> open -> item -> close."""

import asyncio
import os
import sqlite3
import tempfile
import unittest

from _telegram_stub import install

install()

from bot.app_config import (  # noqa: E402
    OWNER_REVIEW_MENU,
    REVIEW_FEEDBACK,
    REVIEW_REPORTS,
    SETTINGS_MENU,
)
from bot.handlers.review import (  # noqa: E402
    owner_review_menu_choice,
    review_feedback_choice,
    review_reports_choice,
)
from bot.storage import (  # noqa: E402
    add_error_report,
    add_feedback,
    init_db,
    list_error_reports,
    list_feedback,
)
from bot.ui import keyboards as K  # noqa: E402

OWNER, GUEST = 999, 111


class _Msg:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class _Update:
    def __init__(self, text, uid):
        self.message = _Msg(text)
        self.effective_user = type("U", (), {"id": uid})()


class _Ctx:
    def __init__(self):
        self.user_data = {}


def _flat(rows):
    return [c for row in rows for c in row]


def _run(coro):
    return asyncio.run(coro)


class ReviewReportsTest(unittest.TestCase):
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
        self.rid = add_error_report(self.db, GUEST, "w1", "bad plural", mode="ru_de")
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

    def test_settings_entry_owner_vs_guest(self):
        self.assertIn("review/feedback", _flat(K._settings_menu_keys(OWNER)))
        self.assertNotIn("review/feedback", _flat(K._settings_menu_keys(GUEST)))
        self.assertIn("feedback", _flat(K._settings_menu_keys(GUEST)))

    def test_submenu_back_returns_to_settings(self):
        ctx = _Ctx()
        st = _run(owner_review_menu_choice(_Update("back", OWNER), ctx))
        self.assertEqual(st, SETTINGS_MENU)

    def test_open_pick_close_flow(self):
        ctx = _Ctx()
        st = _run(owner_review_menu_choice(_Update("reports", OWNER), ctx))
        self.assertEqual(st, REVIEW_REPORTS)
        self.assertEqual(ctx.user_data["review_numbers"], {1: self.rid})

        _run(review_reports_choice(_Update("open", OWNER), ctx))
        self.assertEqual(ctx.user_data["review_sub"], "pick")

        upd = _Update("1", OWNER)
        _run(review_reports_choice(upd, ctx))
        self.assertEqual(ctx.user_data["review_sub"], "item")
        self.assertEqual(ctx.user_data["review_current"], self.rid)
        # item card carries a clickable profile link (works without @username)
        self.assertTrue(any("tg://user?id=111" in m for m in upd.message.replies))

        _run(review_reports_choice(_Update("close", OWNER), ctx))
        self.assertEqual(list_error_reports(self.db, status="pending"), [])
        self.assertEqual(len(list_error_reports(self.db, status="reviewed")), 1)

    def test_guest_blocked(self):
        ctx = _Ctx()
        st = _run(review_reports_choice(_Update("open", GUEST), ctx))
        self.assertEqual(st, SETTINGS_MENU)

    def test_delete_single_with_yes_no(self):
        ctx = _Ctx()
        _run(owner_review_menu_choice(_Update("reports", OWNER), ctx))
        _run(review_reports_choice(_Update("open", OWNER), ctx))
        _run(review_reports_choice(_Update("1", OWNER), ctx))
        _run(review_reports_choice(_Update("delete", OWNER), ctx))
        self.assertEqual(ctx.user_data["review_sub"], "confirm_delete")
        _run(review_reports_choice(_Update("yes", OWNER), ctx))
        self.assertEqual(list_error_reports(self.db, status="pending"), [])
        self.assertEqual(list_error_reports(self.db, status="reviewed"), [])  # deleted, not closed

    def test_delete_no_keeps_report(self):
        ctx = _Ctx()
        _run(owner_review_menu_choice(_Update("reports", OWNER), ctx))
        _run(review_reports_choice(_Update("open", OWNER), ctx))
        _run(review_reports_choice(_Update("1", OWNER), ctx))
        _run(review_reports_choice(_Update("delete", OWNER), ctx))
        _run(review_reports_choice(_Update("no", OWNER), ctx))
        self.assertEqual(len(list_error_reports(self.db, status="pending")), 1)

    def test_delete_all_requires_yes(self):
        add_error_report(self.db, GUEST, "w1", "second")
        ctx = _Ctx()
        _run(owner_review_menu_choice(_Update("reports", OWNER), ctx))
        _run(review_reports_choice(_Update("delete all", OWNER), ctx))
        self.assertEqual(ctx.user_data["review_sub"], "delete_all_confirm")
        _run(review_reports_choice(_Update("yes", OWNER), ctx))
        self.assertEqual(list_error_reports(self.db, status="pending"), [])

    def test_closed_tab_shows_reviewed(self):
        ctx = _Ctx()
        _run(owner_review_menu_choice(_Update("reports", OWNER), ctx))
        _run(review_reports_choice(_Update("open", OWNER), ctx))
        _run(review_reports_choice(_Update("1", OWNER), ctx))
        _run(review_reports_choice(_Update("close", OWNER), ctx))
        _run(review_reports_choice(_Update("closed", OWNER), ctx))
        self.assertEqual(ctx.user_data["review_status"], "reviewed")
        self.assertEqual(len(ctx.user_data["review_numbers"]), 1)


class FeedbackReviewTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self.fid = add_feedback(self.db, GUEST, "nice bot")
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

    def test_open_and_delete_feedback(self):
        ctx = _Ctx()
        st = _run(owner_review_menu_choice(_Update("feedback", OWNER), ctx))
        self.assertEqual(st, REVIEW_FEEDBACK)
        self.assertEqual(ctx.user_data["review_numbers"], {1: self.fid})
        _run(review_feedback_choice(_Update("open", OWNER), ctx))
        upd = _Update("1", OWNER)
        _run(review_feedback_choice(upd, ctx))
        self.assertEqual(ctx.user_data["review_sub"], "item")
        self.assertTrue(any("tg://user?id=111" in m for m in upd.message.replies))
        _run(review_feedback_choice(_Update("delete", OWNER), ctx))
        _run(review_feedback_choice(_Update("yes", OWNER), ctx))
        self.assertEqual(list_feedback(self.db, status="pending"), [])

    def test_delete_all_feedback(self):
        add_feedback(self.db, GUEST, "second")
        ctx = _Ctx()
        _run(owner_review_menu_choice(_Update("feedback", OWNER), ctx))
        _run(review_feedback_choice(_Update("delete all", OWNER), ctx))
        _run(review_feedback_choice(_Update("yes", OWNER), ctx))
        self.assertEqual(list_feedback(self.db, status="pending"), [])

    def test_guest_blocked(self):
        ctx = _Ctx()
        self.assertEqual(_run(review_feedback_choice(_Update("open", GUEST), ctx)), SETTINGS_MENU)


if __name__ == "__main__":
    unittest.main()
