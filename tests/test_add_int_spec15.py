"""spec #15 / todo T5 — add_int: push a word's next show further out from its card.

Flow: a directional quiz supplies its progress mode; db_search asks for it.
Then type n (1..15, fool-proofed) -> preview "next show = <due>" -> Confirm ->
progress is written as if the word had been answered correctly n times in a
row; undo-able via the transaction log.
"""

import asyncio
import os
import sys
import tempfile
import types
import unittest

from _telegram_stub import install

install()
if "dotenv" not in sys.modules:
    _d = types.ModuleType("dotenv")
    _d.load_dotenv = lambda *a, **k: False
    sys.modules["dotenv"] = _d

from bot.app_config import (  # noqa: E402
    DB_SEARCH_VIEW,
    QUIZ_ARTICLE,
    QUIZ_DE_RU,
    QUIZ_PL_SG,
    QUIZ_RU_DE,
)
from bot.labels import SYN_CONFIRM_TEXT  # noqa: E402
from bot.storage import (  # noqa: E402
    add_word,
    default_srs,
    get_progress,
    init_db,
    list_transaction_logs,
    restore_user_word_state,
    simulate_fsrs_advance,
    update_fsrs,
)

OWNER = 999
GUEST = 111


class SimulateAdvanceTest(unittest.TestCase):
    def test_equivalent_to_n_perfect_answers(self):
        srs = default_srs()
        expected = dict(srs)
        for _ in range(3):
            expected = update_fsrs(expected, 5, interval_divisor=1.65)
        self.assertEqual(simulate_fsrs_advance(srs, 3, interval_divisor=1.65), expected)

    def test_no_write_input_untouched(self):
        srs = default_srs()
        before = dict(srs)
        simulate_fsrs_advance(srs, 5)
        self.assertEqual(srs, before)

    def test_zero_and_none_srs(self):
        self.assertEqual(simulate_fsrs_advance(default_srs(), 0), default_srs())
        self.assertEqual(simulate_fsrs_advance(None, 0)["repetitions"], 0)


class _Msg:
    def __init__(self, text=""):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        return type("Sent", (), {"message_id": 1})()


class _Update:
    def __init__(self, uid, text):
        self.message = _Msg(text)
        self.effective_user = type("U", (), {"id": uid})()
        self.effective_chat = type("C", (), {"id": uid})()


class _Ctx:
    def __init__(self):
        self.user_data = {}


class AddIntFlowTest(unittest.TestCase):
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
        self.entry = add_word(self.db, {
            "id": "w-int", "type": "nomen", "article": "das", "german": "Haus",
            "russian": "дом", "topic": "1",
        })

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _ctx_for(self, uid, return_state=DB_SEARCH_VIEW):
        ctx = _Ctx()
        ctx.user_data.update({
            "edit_source": "quiz",
            "quiz_edit_entry": dict(self.entry),
            "edit_stage": "pick_field",
            "edit_return_state": return_state,
        })
        return ctx

    def _send(self, uid, text, ctx):
        from bot.handlers.clarify_fields import clarify_fields_input
        upd = _Update(uid, text)
        asyncio.run(clarify_fields_input(upd, ctx))
        return upd

    def _l1_de_label(self, uid):
        from bot.common.lang import _quiz_dir_labels
        return _quiz_dir_labels(uid)[0]

    def _mode(self, uid):
        from bot.common.lang import _mode_l1_de
        return _mode_l1_de(uid)

    def _drive_to_confirm(self, uid, n_text="5"):
        ctx = self._ctx_for(uid)
        self._send(uid, "add_int", ctx)
        self.assertEqual(ctx.user_data["edit_stage"], "add_int_mode")
        self._send(uid, self._l1_de_label(uid), ctx)
        self.assertEqual(ctx.user_data["edit_stage"], "add_int_n")
        upd = self._send(uid, n_text, ctx)
        return upd, ctx

    def test_directional_quiz_skips_mode_picker(self):
        ctx = self._ctx_for(OWNER, QUIZ_RU_DE)
        upd = self._send(OWNER, "add_int", ctx)
        self.assertEqual(ctx.user_data["edit_stage"], "add_int_n")
        self.assertEqual(ctx.user_data["add_int_mode"], self._mode(OWNER))
        self.assertIn("1", upd.message.replies[-1])
        self.assertIn("15", upd.message.replies[-1])

    def test_each_directional_quiz_resolves_its_progress_mode(self):
        from bot.common.lang import _mode_de_l1, _mode_pl_sg
        from bot.handlers.clarify_fields import _add_int_mode_for_return_state
        from bot.storage import QUIZ_ARTICLE as MODE_ARTICLE

        expected = {
            QUIZ_RU_DE: self._mode(OWNER),
            QUIZ_DE_RU: _mode_de_l1(OWNER),
            QUIZ_PL_SG: _mode_pl_sg(OWNER),
            QUIZ_ARTICLE: MODE_ARTICLE,
        }
        for return_state, mode in expected.items():
            with self.subTest(return_state=return_state):
                self.assertEqual(
                    _add_int_mode_for_return_state(OWNER, return_state),
                    mode,
                )

    def test_full_flow_writes_progress_equal_to_preview(self):
        upd, ctx = self._drive_to_confirm(OWNER, "5")
        self.assertEqual(ctx.user_data["edit_stage"], "add_int_confirm")
        preview = dict(ctx.user_data["add_int_preview"])
        self.assertIn(preview["due"], upd.message.replies[-1])
        self._send(OWNER, SYN_CONFIRM_TEXT, ctx)
        stored = get_progress(self.db, OWNER, "w-int", self._mode(OWNER))
        self.assertEqual(stored, preview)
        self.assertEqual(stored["repetitions"], 5)

    def test_invalid_n_is_reasked_and_nothing_written(self):
        for bad in ("abc", "0", "16", "99", ""):
            ctx = self._ctx_for(OWNER)
            self._send(OWNER, "add_int", ctx)
            self._send(OWNER, self._l1_de_label(OWNER), ctx)
            self._send(OWNER, bad, ctx)
            self.assertEqual(ctx.user_data["edit_stage"], "add_int_n", bad)
        self.assertEqual(get_progress(self.db, OWNER, "w-int", self._mode(OWNER)), default_srs())

    def test_range_accepts_one_and_fifteen(self):
        for value in ("1", "15"):
            _, ctx = self._drive_to_confirm(OWNER, value)
            self.assertEqual(ctx.user_data["edit_stage"], "add_int_confirm")

    def test_cancel_on_preview_writes_nothing(self):
        _, ctx = self._drive_to_confirm(OWNER, "4")
        self._send(OWNER, "back", ctx)  # anything but Confirm cancels
        self.assertEqual(ctx.user_data["edit_stage"], "pick_field")
        self.assertEqual(get_progress(self.db, OWNER, "w-int", self._mode(OWNER)), default_srs())

    def test_guest_pushes_own_progress_only(self):
        _, ctx = self._drive_to_confirm(GUEST, "3")
        self._send(GUEST, SYN_CONFIRM_TEXT, ctx)
        self.assertEqual(get_progress(self.db, GUEST, "w-int", self._mode(GUEST))["repetitions"], 3)
        self.assertEqual(get_progress(self.db, OWNER, "w-int", self._mode(OWNER)), default_srs())

    def test_undoable_via_transaction_log(self):
        _, ctx = self._drive_to_confirm(OWNER, "5")
        self._send(OWNER, SYN_CONFIRM_TEXT, ctx)
        logs = list_transaction_logs(self.db, user_id=OWNER, limit=1)
        self.assertEqual(len(logs), 1)
        self.assertTrue(logs[0]["action"].startswith("add_int:"))
        restore_user_word_state(self.db, logs[0]["payload"]["snapshot"])
        self.assertEqual(get_progress(self.db, OWNER, "w-int", self._mode(OWNER)), default_srs())


if __name__ == "__main__":
    unittest.main()
