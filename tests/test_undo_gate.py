"""#9 — /undo is available to any APPROVED user, scoped to their own steps.

The owner-only gate was wrong: transaction_log / restore / delete are all scoped
by user_id, so an approved guest undoing only rolls back THEIR OWN operations.
A non-approved user is routed to the guest-start flow instead of running undo.

bot.main pulls in dotenv (absent in the dev env) and telegram — both stubbed
before import; _guest_start is monkeypatched to a recorder so the gate is tested
without the registration flow's internals.
"""

import asyncio
import os
import sys
import tempfile
import types
import unittest

from _telegram_stub import install

install()
if "dotenv" not in sys.modules:  # bot.main does `from dotenv import load_dotenv`
    _d = types.ModuleType("dotenv")
    _d.load_dotenv = lambda *a, **k: False
    sys.modules["dotenv"] = _d

import bot.main as M  # noqa: E402
from bot.common.txlog import reset_undo_progress  # noqa: E402
from bot.storage import (  # noqa: E402
    add_transaction_log,
    add_word,
    init_db,
    list_transaction_logs,
    register_or_update_user,
    set_user_status,
    snapshot_user_word_state,
    update_progress,
)

OWNER, GUEST, PENDING = 999, 111, 222


class _Msg:
    def __init__(self):
        self.replies = []
        self._next_id = 1000

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        self._next_id += 1
        return type("Sent", (), {"message_id": self._next_id})()


class _Update:
    def __init__(self, uid):
        self.message = _Msg()
        self.effective_user = type("U", (), {"id": uid, "username": "u", "first_name": "U"})()
        self.effective_chat = type("C", (), {"id": uid})()


class _Bot:
    def __init__(self):
        self.edits = []

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        self.edits.append((message_id, text))


class _Ctx:
    def __init__(self, args=None):
        self.args = args or []
        self.user_data = {}
        self.bot = _Bot()


def _run(coro):
    return asyncio.run(coro)


class UndoGateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = {k: os.environ.get(k) for k in ("DB_PATH", "OWNER_ID")}
        os.environ["DB_PATH"] = self.db
        os.environ["OWNER_ID"] = str(OWNER)
        # Record _guest_start calls instead of running the registration flow.
        self._orig_guest_start = M._guest_start
        self._guest_start_calls = []

        async def _fake_guest_start(update, context):
            self._guest_start_calls.append(update.effective_user.id)
            return M.MENU

        M._guest_start = _fake_guest_start
        self.addCleanup(self._restore)
        # A shared word so snapshots are real and restore actually rolls back.
        self.wid = add_word(self.db, {
            "type": "nomen", "article": "der", "german": "Apfel",
            "plural": "Äpfel", "russian": "яблоко", "topic": "1",
        })["id"]

    def _restore(self):
        M._guest_start = self._orig_guest_start
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _log_a_step(self, uid):
        """Snapshot uid's clean state, dirty it, and record a transaction log."""
        snap = snapshot_user_word_state(self.db, user_id=uid, word_id=self.wid, modes=["ru_de"])
        update_progress(self.db, user_id=uid, word_id=self.wid, mode="ru_de", quality=2)
        add_transaction_log(self.db, user_id=uid, action="quiz:ru_de", payload={"snapshot": snap})

    def test_approved_guest_undo_scoped_to_own_logs(self):
        register_or_update_user(self.db, GUEST, "g", "G")
        set_user_status(self.db, GUEST, "approved")
        self._log_a_step(GUEST)
        self._log_a_step(OWNER)  # owner has their own pending log

        _run(M.undo_steps(_Update(GUEST), _Ctx()))

        # Guest's log was consumed by their undo; owner's is untouched.
        self.assertEqual(list_transaction_logs(self.db, user_id=GUEST, limit=10), [])
        self.assertEqual(len(list_transaction_logs(self.db, user_id=OWNER, limit=10)), 1)
        self.assertEqual(self._guest_start_calls, [])  # gate let the guest through

    def test_failed_restore_still_consumes_log(self):
        """A raising restore must not brick /undo: the entry is still deleted so
        repeated presses can't loop forever on the same poisoned snapshot."""
        register_or_update_user(self.db, GUEST, "g", "G")
        set_user_status(self.db, GUEST, "approved")
        self._log_a_step(GUEST)

        orig = M.restore_user_word_state

        def _boom(*a, **k):
            raise RuntimeError("FOREIGN KEY constraint failed")

        M.restore_user_word_state = _boom
        try:
            _run(M.undo_steps(_Update(GUEST), _Ctx()))  # must not raise
        finally:
            M.restore_user_word_state = orig

        # Log consumed despite the failure -> next /undo won't re-hit it.
        self.assertEqual(list_transaction_logs(self.db, user_id=GUEST, limit=10), [])

    def test_non_approved_routed_to_guest_start_no_undo(self):
        register_or_update_user(self.db, PENDING, "p", "P")  # stays pending
        self._log_a_step(PENDING)

        _run(M.undo_steps(_Update(PENDING), _Ctx()))

        # Gate blocked undo → routed to guest-start, their log is NOT deleted.
        self.assertEqual(self._guest_start_calls, [PENDING])
        self.assertEqual(len(list_transaction_logs(self.db, user_id=PENDING, limit=10)), 1)


class UndoCounterTest(unittest.TestCase):
    """Consecutive /undo edits ONE message with a live step counter; navigating
    away resets it so the next /undo starts a fresh message."""

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
        self.wid = add_word(self.db, {
            "type": "nomen", "article": "der", "german": "Apfel",
            "plural": "Äpfel", "russian": "яблоко", "topic": "1",
        })["id"]

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _log_a_step(self):
        snap = snapshot_user_word_state(self.db, user_id=GUEST, word_id=self.wid, modes=["ru_de"])
        update_progress(self.db, user_id=GUEST, word_id=self.wid, mode="ru_de", quality=2)
        add_transaction_log(self.db, user_id=GUEST, action="quiz:ru_de", payload={"snapshot": snap})

    def test_consecutive_undo_edits_single_message(self):
        for _ in range(3):
            self._log_a_step()
        upd, ctx = _Update(GUEST), _Ctx()
        _run(M.undo_steps(upd, ctx))
        self.assertEqual(len(upd.message.replies), 1)      # one new message
        self.assertIn("1 step", upd.message.replies[0])
        self.assertEqual(ctx.bot.edits, [])

        _run(M.undo_steps(upd, ctx))
        self.assertEqual(len(upd.message.replies), 1)      # no second message
        self.assertEqual(len(ctx.bot.edits), 1)            # edited in place
        self.assertIn("2 step", ctx.bot.edits[-1][1])      # counter accumulated

    def test_navigation_resets_counter(self):
        for _ in range(2):
            self._log_a_step()
        upd, ctx = _Update(GUEST), _Ctx()
        _run(M.undo_steps(upd, ctx))
        self.assertEqual(len(upd.message.replies), 1)
        reset_undo_progress(ctx)                            # simulate leaving the undo flow
        _run(M.undo_steps(upd, ctx))
        self.assertEqual(len(upd.message.replies), 2)       # fresh message, not an edit
        self.assertEqual(ctx.bot.edits, [])
        self.assertIn("1 step", upd.message.replies[-1])    # counter restarted


if __name__ == "__main__":
    unittest.main()
