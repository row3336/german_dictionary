"""spec #2 step 3d — VF-add UI: estimates, progress text/keyboard, callbacks,
input routing. Telegram objects are faked; the background runner is not started."""

import asyncio
import os
import tempfile
import unittest

from _telegram_stub import install

install()

import bot.verb_forms_parsing.add_flow as AF  # noqa: E402
from bot.app_config import MENU, VF_ADD_INPUT  # noqa: E402
from bot.storage import init_db  # noqa: E402
from bot.verb_forms_parsing import job_store as JS  # noqa: E402

OWNER = 777


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeMsg:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, reply_markup=None, **kwargs):
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self, text, uid):
        self.message = _FakeMsg(text)
        self.effective_user = _FakeUser(uid)


class _Ctx:
    def __init__(self):
        self.user_data = {}


class _FakeCbQuery:
    def __init__(self, data):
        self.data = data
        self.edits = []

    async def answer(self):
        pass

    async def edit_message_text(self, text, reply_markup=None):
        self.edits.append(text)


class _FakeCbUpdate:
    def __init__(self, data):
        self.callback_query = _FakeCbQuery(data)


class PureHelpersTest(unittest.TestCase):
    def test_parse_word_list(self):
        # bare words -> no hint; a valid [type] is captured (case-insensitive);
        # an unknown [type] is dropped as no hint (word kept).
        self.assertEqual(
            AF.parse_word_list("gehen\n  Haus  \nvon [Praeposition]\naus [xyz]\n\n"),
            [("gehen", ""), ("Haus", ""), ("von", "praeposition"), ("aus", "")],
        )

    def test_estimates_scale_with_count(self):
        self.assertEqual(AF.estimate_eta_seconds(10), 10 * AF._AVG_SEC_PER_WORD)
        self.assertEqual(AF.estimate_traffic_kb(10), 10 * AF._AVG_KB_PER_WORD)

    def test_prestart_text_mentions_proxy_and_count(self):
        t = AF.prestart_text(12)
        self.assertIn("12 word", t)
        self.assertIn("via proxy", t)
        self.assertIn("Est. time", t)

    def test_progress_text(self):
        job = {"id": 1, "status": "running"}
        counts = {"total": 5, "pending": 2, "done": 2, "error": 1, "skipped": 0}
        t = AF.progress_text(job, counts)
        self.assertIn("#parsingVF", t)
        self.assertIn("3/5 (60%)", t)
        self.assertIn("errors 1", t)

    def test_progress_text_details(self):
        job = {"id": 1, "status": "running"}
        counts = {"total": 2, "pending": 0, "done": 1, "error": 1, "skipped": 0}
        items = [
            {"word": "gehen", "status": "done", "flags": "", "error": ""},
            {"word": "weil", "status": "error", "flags": "", "error": "http 429"},
        ]
        t = AF.progress_text(job, counts, details_items=items)
        self.assertIn("✅ gehen", t)
        self.assertIn("❌ weil", t)
        self.assertIn("http 429", t)

    def test_progress_text_details_shows_processed_not_trailing_pending(self):
        # regression: a mid-run batch's tail positions are all pending; Details must
        # show the processed words (not render an empty block that Telegram rejects).
        job = {"id": 1, "status": "running"}
        counts = {"total": 20, "pending": 18, "done": 2, "error": 0, "skipped": 0}
        items = (
            [{"word": "gehen", "status": "done", "flags": "", "error": ""},
             {"word": "Haus", "status": "done", "flags": "", "error": ""}]
            + [{"word": f"w{i}", "status": "pending", "flags": "", "error": ""} for i in range(18)]
        )
        t = AF.progress_text(job, counts, details_items=items)
        self.assertIn("✅ gehen", t)
        self.assertIn("✅ Haus", t)
        self.assertNotIn("w0", t)  # trailing pending words are not listed

    def test_progress_keyboard_toggles_pause_resume(self):
        run_kb = AF.progress_keyboard({"id": 9, "status": "running"})
        btn = run_kb.inline_keyboard[0][0]
        self.assertIn("Pause", btn.text)
        self.assertEqual(btn.callback_data, "vfadd:pause:9")
        pause_kb = AF.progress_keyboard({"id": 9, "status": "paused"})
        self.assertIn("Resume", pause_kb.inline_keyboard[0][0].text)
        self.assertEqual(pause_kb.inline_keyboard[0][0].callback_data, "vfadd:resume:9")

    def test_progress_keyboard_none_when_terminal(self):
        # a done/stopped/error job has no live task -> no dead control buttons
        for status in ("done", "stopped", "error"):
            self.assertIsNone(AF.progress_keyboard({"id": 9, "status": status}))


class CallbackTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = self.db
        self.addCleanup(self._restore)
        self.jid = JS.create_job(self.db, OWNER, 100, "t", ["gehen", "Haus"])
        AF.VF_ADD_UI[self.jid] = {"chat_id": 100, "message_id": 1, "details": False}
        self.addCleanup(lambda: AF.VF_ADD_UI.pop(self.jid, None))

    def _restore(self):
        if self._old is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = self._old

    def _cb(self, action):
        asyncio.run(AF.vf_add_callback(_FakeCbUpdate(f"vfadd:{action}:{self.jid}"), None))

    def test_pause_resume_stop(self):
        self._cb("pause")
        self.assertEqual(JS.get_job(self.db, self.jid)["status"], "paused")
        self._cb("resume")
        self.assertEqual(JS.get_job(self.db, self.jid)["status"], "running")
        self._cb("stop")
        self.assertEqual(JS.get_job(self.db, self.jid)["status"], "stopped")

    def test_details_toggle(self):
        self.assertFalse(AF.VF_ADD_UI[self.jid]["details"])
        self._cb("details")
        self.assertTrue(AF.VF_ADD_UI[self.jid]["details"])


class InputRoutingTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = self.db
        self.addCleanup(lambda: os.environ.__setitem__("DB_PATH", self._old) if self._old else os.environ.pop("DB_PATH", None))

    def test_empty_list_stays(self):
        state = asyncio.run(AF.vf_add_input(_FakeUpdate("   \n  ", OWNER), _Ctx()))
        self.assertEqual(state, VF_ADD_INPUT)

    def test_back_returns_menu(self):
        from bot.i18n import t
        from bot.common.lang import _user_interface_lang
        state = asyncio.run(AF.vf_add_input(_FakeUpdate(t(_user_interface_lang(OWNER), "back"), OWNER), _Ctx()))
        self.assertEqual(state, MENU)


if __name__ == "__main__":
    unittest.main()
