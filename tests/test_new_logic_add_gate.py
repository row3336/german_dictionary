"""Every bot Add entry is disabled before its first external or write effect."""

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from _telegram_stub import install
install()

from bot.app_config import ADD_EDIT, MENU
from bot.common import add_words_gate as G
from bot.handlers import add_words as A, clarify_fields as C, menu as M, settings as S
from bot.i18n import t
from bot.storage import add_word, init_db, register_or_update_user, set_user_status
from bot.ui.keyboards import _main_menu_keys, _settings_menu_keys
from bot.verb_forms_parsing import add_flow as V

OWNER, GUEST = 999, 101


def update(uid=OWNER, text="yes", callback=False):
    doc = SimpleNamespace(file_name="fixture.json", get_file=AsyncMock(side_effect=AssertionError("download")))
    msg = SimpleNamespace(text=text, document=doc, reply_text=AsyncMock())
    query = SimpleNamespace(data="vfadd:resume:1", message=msg, answer=AsyncMock()) if callback else None
    return SimpleNamespace(message=msg, effective_message=msg, callback_query=query,
                           effective_user=SimpleNamespace(id=uid), effective_chat=SimpleNamespace(id=uid))


class AddGateTest(unittest.TestCase):
    def setUp(self):
        self.db = str(Path(os.environ["TMPDIR"]) / f"add-gate-{uuid4().hex}.sqlite")
        env = patch.dict(os.environ, {"DB_PATH": self.db, "OWNER_ID": str(OWNER)})
        env.start(); self.addCleanup(env.stop)
        gate = patch.object(G, "ADD_WORDS_ENABLED", False)
        gate.start(); self.addCleanup(gate.stop)
        init_db(self.db)
        register_or_update_user(self.db, GUEST, "fixture", "Fixture")
        set_user_status(self.db, GUEST, "approved")

    def context(self):
        return SimpleNamespace(user_data={"add_queue": ["Haus"], "pending_entry": {"id": "old"},
            "vf_apply_entries": [{"id": "old"}], "topic_return": "add_queue",
            "quiz_entry": {"id": "unrelated"}})

    def test_all_ui_entries_and_effectful_async_services_reject_before_effects(self):
        cases = [(getattr(A, name), ()) for name in (
            "add_menu_choice", "add_input", "add_document", "add_precheck", "add_precheck_edit",
            "add_confirm", "add_clarify", "prompt_add_topic", "_process_next_ai_word", "_ban_pending_word")]
        cases += [(A.handle_add_topic_input, ("new topic",)), (A._finalize_add_topic, ("new topic",)),
                  (A._process_ai_word, ("Haus", None)), (A._precheck_words, (["Haus"],))]
        cases += [(getattr(V, name), ()) for name in (
            "vf_add_prompt", "vf_add_input", "vf_add_document", "vf_recheck_prompt",
            "vf_review_document", "vf_apply_confirm", "vf_add_callback")]
        cases += [(V.start_vf_add_job, (["Haus"],)), (V._launch_job, (1, 1)),
                  (V._start_recheck, ("all",)), (V._prompt_vf_topic, (["Haus"],)),
                  (V._show_apply_preview, ([], []))]
        for uid in (OWNER, GUEST):
            for handler, args in cases:
                with self.subTest(uid=uid, handler=handler.__name__):
                    upd, ctx = update(uid, callback=handler == V.vf_add_callback), self.context()
                    state = asyncio.run(handler(upd, ctx, *args))
                    self.assertEqual(state, MENU)
                    upd.message.document.get_file.assert_not_called()
                    self.assertNotIn("pending_entry", ctx.user_data)
                    self.assertEqual(ctx.user_data["quiz_entry"], {"id": "unrelated"})
                    self.assertEqual(upd.message.reply_text.call_args.args[0], t("en", "add_words_disabled"))

    def test_direct_save_and_apply_services_cannot_write(self):
        with patch.object(A, "add_word", side_effect=AssertionError("write")):
            self.assertEqual(asyncio.run(A._save_entry(update(), {"russian": "x"})), MENU)
        upd = update()
        self.assertEqual(asyncio.run(V._read_document_entries(upd)), MENU)
        upd.message.document.get_file.assert_not_called()
        with patch.object(V, "add_word", side_effect=AssertionError("write")), \
             patch.object(V, "update_word_fields", side_effect=AssertionError("write")):
            with self.assertRaises(G.AddWordsDisabled):
                V.apply_batch(self.db, [{"german": "Haus"}])

    def test_startup_does_not_even_read_or_resume_persisted_jobs(self):
        app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(side_effect=AssertionError("send"))))
        with patch.object(V, "active_jobs", side_effect=AssertionError("job read")), \
             patch.object(V, "run_job", side_effect=AssertionError("launch")):
            asyncio.run(V.resume_jobs(app))

    def test_main_menu_stale_buttons_in_all_languages_reject_owner_and_guest(self):
        for uid in (OWNER, GUEST):
            self.assertNotIn(t("en", "add_word"), [b for row in _main_menu_keys(uid) for b in row])
            for lang in ("en", "ru", "uk"):
                for key in ("add_word", "add_ai", "add_vf", "vf_recheck"):
                    upd, ctx = update(uid, t(lang, key)), self.context()
                    self.assertEqual(asyncio.run(M.menu_choice(upd, ctx)), MENU)
                    self.assertEqual(upd.message.reply_text.call_args.args[0], t("en", "add_words_disabled"))

    def test_cached_add_edit_rejects_but_ordinary_clarify_stays_open(self):
        ctx = self.context(); ctx.user_data.update(edit_source="add", edit_stage="enter_value", edit_field="ru")
        self.assertEqual(asyncio.run(C.clarify_fields_input(update(text="new"), ctx)), MENU)
        entry = add_word(self.db, {"id": "a", "type": "verb", "german": "gehen", "infinitive": "gehen", "russian": "идти"})
        ctx.user_data.update(edit_source="quiz", quiz_edit_entry=entry, edit_stage="pick_field")
        self.assertEqual(asyncio.run(C.clarify_fields_input(update(text="image"), ctx)), ADD_EDIT)

    def test_topic_back_guard_blocks_only_return_to_add_and_proxy_remains(self):
        ctx = self.context()
        self.assertEqual(asyncio.run(S.topic_filter_edit(update(text="back"), ctx)), MENU)
        ctx.user_data["topic_return"] = "tests_menu"
        from bot.app_config import TESTS_MENU
        self.assertEqual(asyncio.run(S.topic_filter_edit(update(text="back"), ctx)), TESTS_MENU)
        from bot.labels import PARSE_VF_TEXT
        self.assertIn(PARSE_VF_TEXT, [b for row in _settings_menu_keys(OWNER) for b in row])
        self.assertNotIn(PARSE_VF_TEXT, [b for row in _settings_menu_keys(GUEST) for b in row])


if __name__ == "__main__":
    unittest.main()
