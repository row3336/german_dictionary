"""spec #2 step 3e — resume_jobs relaunches in-flight VF-add jobs after a restart."""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from _telegram_stub import install

install()

import bot.verb_forms_parsing.add_flow as AF  # noqa: E402
from bot.storage import init_db  # noqa: E402
from bot.verb_forms_parsing import job_store as JS  # noqa: E402


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))

        class _M:
            message_id = 42

        return _M()


class _FakeApp:
    def __init__(self):
        self.bot = _FakeBot()


async def _fake_run(db_path, job_id, on_progress=None):
    return "done"


class ResumeTest(unittest.TestCase):
    def setUp(self):
        # The feature is disabled by default; verify its retained internals explicitly.
        enabled = patch("bot.common.add_words_gate.ADD_WORDS_ENABLED", True)
        enabled.start()
        self.addCleanup(enabled.stop)
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = self.db
        AF.VF_ADD_TASKS.clear()
        AF.VF_ADD_UI.clear()
        self._real_run = AF.run_job
        AF.run_job = _fake_run
        self.addCleanup(self._restore)

    def _restore(self):
        AF.run_job = self._real_run
        AF.VF_ADD_TASKS.clear()
        AF.VF_ADD_UI.clear()
        if self._old is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = self._old

    def test_resumes_active_skips_done(self):
        running = JS.create_job(self.db, 7, 100, "t", ["gehen", "Haus"])
        done = JS.create_job(self.db, 7, 100, "t", ["x"])
        JS.set_job_status(self.db, done, "done")
        app = _FakeApp()
        asyncio.run(AF.resume_jobs(app))
        self.assertIn(running, AF.VF_ADD_TASKS)
        self.assertNotIn(done, AF.VF_ADD_TASKS)
        self.assertIn(running, AF.VF_ADD_UI)
        self.assertEqual(len(app.bot.sent), 1)  # one resumed-progress message

    def test_paused_job_is_resumed(self):
        jid = JS.create_job(self.db, 7, 100, "t", ["gehen"])
        JS.set_job_status(self.db, jid, "paused")
        app = _FakeApp()
        asyncio.run(AF.resume_jobs(app))
        self.assertIn(jid, AF.VF_ADD_TASKS)

    def test_already_running_not_double_launched(self):
        jid = JS.create_job(self.db, 7, 100, "t", ["gehen"])
        AF.VF_ADD_TASKS[jid] = object()  # pretend a task is already live
        app = _FakeApp()
        asyncio.run(AF.resume_jobs(app))
        self.assertEqual(app.bot.sent, [])  # skipped, no new message


if __name__ == "__main__":
    unittest.main()
