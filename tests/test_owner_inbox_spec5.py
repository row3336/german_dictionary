"""spec #5 (step 1) — owner inbox engine + digest window logic.

Covers pagination, digest text/skip-empty, the window/dedup gate, and the inline
callback dispatch (owner-gated close/delete/list). Instant-send removal, access
rework and review-UI migration are later steps.
"""

import asyncio
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from bot.handlers.owner_inbox import (
    CB_PREFIX,
    PAGE_SIZE,
    _card_view,
    _count_open,
    _list_view,
    _paginate,
    digest_text,
    owner_inbox_callback,
)
from bot.jobs.owner_digest import _due_window
from bot.storage import add_error_report, add_feedback, add_word, init_db, list_error_reports, list_feedback

BERLIN = ZoneInfo("Europe/Berlin")
OWNER, GUEST = 999, 111


def _mk():
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.close()
    return tmp.name


class DueWindowTest(unittest.TestCase):
    def test_fires_in_window_once(self):
        now = datetime(2026, 7, 4, 14, 30, tzinfo=BERLIN)
        self.assertEqual(_due_window(now, {}), 14)
        # already sent today for that window -> None
        self.assertIsNone(_due_window(now, {14: "2026-07-04"}))
        # sent a different day -> fires again
        self.assertEqual(_due_window(now, {14: "2026-07-03"}), 14)

    def test_outside_any_window(self):
        self.assertIsNone(_due_window(datetime(2026, 7, 4, 9, 0, tzinfo=BERLIN), {}))
        self.assertIsNone(_due_window(datetime(2026, 7, 4, 23, 0, tzinfo=BERLIN), {}))

    def test_each_of_three_windows(self):
        for h in (4, 14, 19):
            self.assertEqual(_due_window(datetime(2026, 7, 4, h, 15, tzinfo=BERLIN), {}), h)


class PaginateTest(unittest.TestCase):
    def test_slice_and_clamp(self):
        items = list(range(25))
        page, pages, chunk = _paginate(items, 0)
        self.assertEqual((page, pages, len(chunk)), (0, 3, PAGE_SIZE))
        page, pages, chunk = _paginate(items, 2)
        self.assertEqual((page, len(chunk)), (2, 5))  # last page has 5
        # over-clamp
        page, _, _ = _paginate(items, 99)
        self.assertEqual(page, 2)
        # empty
        self.assertEqual(_paginate([], 0), (0, 1, []))


class DigestTextTest(unittest.TestCase):
    def test_counts_in_text(self):
        txt = digest_text("en", 3, 5)
        self.assertIn("3", txt)
        self.assertIn("5", txt)
        self.assertIn("reports", txt)
        self.assertIn("feedback", txt)


class _Query:
    def __init__(self, data, uid):
        self.data = data
        self.from_user = type("U", (), {"id": uid})()
        self.answers = []
        self.edits = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))


class _Update:
    def __init__(self, data, uid):
        self.callback_query = _Query(data, uid)


def _flat_cb(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class InboxCallbackTest(unittest.TestCase):
    def setUp(self):
        self.db = _mk()
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = {k: os.environ.get(k) for k in ("DB_PATH", "OWNER_ID")}
        os.environ["DB_PATH"] = self.db
        os.environ["OWNER_ID"] = str(OWNER)
        self.addCleanup(self._restore)
        self.wid = add_word(self.db, {"type": "nomen", "german": "Haus", "russian": "дом"})["id"]

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _run(self, data, uid=OWNER):
        upd = _Update(data, uid)
        asyncio.run(owner_inbox_callback(upd, None))
        return upd.callback_query

    def test_non_owner_denied(self):
        add_error_report(self.db, GUEST, self.wid, "typo")
        q = self._run(f"{CB_PREFIX}r:pg:0", uid=GUEST)
        self.assertTrue(q.answers and q.answers[0][1])  # show_alert=True
        self.assertEqual(q.edits, [])  # nothing edited

    def test_close_report_moves_out_of_open(self):
        add_error_report(self.db, GUEST, self.wid, "typo")
        rid = list_error_reports(self.db)[0]["id"]
        self._run(f"{CB_PREFIX}r:cl:{rid}")
        open_ids = [r["id"] for r in list_error_reports(self.db, status="pending")]
        self.assertNotIn(rid, open_ids)

    def test_delete_feedback_removes_row(self):
        add_feedback(self.db, GUEST, "nice bot")
        fid = list_feedback(self.db)[0]["id"]
        self._run(f"{CB_PREFIX}f:del:{fid}")
        self.assertEqual(list_feedback(self.db), [])

    def test_open_card_then_back(self):
        add_error_report(self.db, GUEST, self.wid, "typo here")
        rid = list_error_reports(self.db)[0]["id"]
        q = self._run(f"{CB_PREFIX}r:it:{rid}")
        text, markup = q.edits[-1]
        self.assertIn(f"#{rid}", text)
        cbs = _flat_cb(markup)
        self.assertIn(f"{CB_PREFIX}r:cl:{rid}", cbs)
        self.assertIn(f"{CB_PREFIX}r:del:{rid}", cbs)

    def test_callback_data_within_64_bytes(self):
        add_error_report(self.db, GUEST, self.wid, "x")
        q = self._run(f"{CB_PREFIX}r:pg:0")
        _, markup = q.edits[-1]
        for cb in _flat_cb(markup):
            self.assertLessEqual(len(cb.encode()), 64)

    def test_count_open(self):
        add_error_report(self.db, GUEST, self.wid, "a")
        add_error_report(self.db, GUEST, self.wid, "b")
        add_feedback(self.db, GUEST, "c")
        self.assertEqual(_count_open(), (2, 1))


if __name__ == "__main__":
    unittest.main()
