"""Sprint item 16: owner-only conjugation editor inside clarify."""

import asyncio
import os
import sqlite3
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

from bot.app_config import ADD_EDIT, DB_SEARCH_VIEW  # noqa: E402
from bot.handlers import clarify_fields as C  # noqa: E402
from bot.i18n import TEXTS, t  # noqa: E402
from bot.labels import SYN_CONFIRM_TEXT  # noqa: E402
from bot.storage import (  # noqa: E402
    add_word,
    get_word_by_id,
    init_db,
    set_word_conjugation,
    update_word_fields,
)

OWNER, GUEST = 999, 111
PRAESENS = "ich kann\ndu kannst\ner/sie/es kann\nwir können\nihr könnt\nsie/Sie können"
PRAESENS_NEW = "ich mag\ndu magst\ner/sie/es mag\nwir mögen\nihr mögt\nsie/Sie mögen"
PRAETERITUM = "ich konnte\ndu konntest\ner/sie/es konnte\nwir konnten\nihr konntet\nsie/Sie konnten"
PRAETERITUM_NEW = "ich musste\ndu musstest\ner/sie/es musste\nwir mussten\nihr musstet\nsie/Sie mussten"


class _Msg:
    def __init__(self, text=""):
        self.text = text
        self.calls = []

    async def reply_text(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return type("Sent", (), {"message_id": 1})()


class _Update:
    def __init__(self, uid, text=""):
        self.message = _Msg(text)
        self.effective_user = type("U", (), {"id": uid})()
        self.effective_chat = type("C", (), {"id": uid})()


class _Ctx:
    def __init__(self, user_data=None):
        self.user_data = user_data or {}


def _flat(markup):
    return [button for row in markup.rows for button in row]


class ClarifyConjugationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = {key: os.environ.get(key) for key in ("DB_PATH", "OWNER_ID")}
        os.environ["DB_PATH"] = self.db
        os.environ["OWNER_ID"] = str(OWNER)
        self.addCleanup(self._restore_env)

        self.modal = add_word(self.db, {
            "id": "modal", "type": "verb", "german": "können", "infinitive": "können",
            "partizip2": "gekonnt", "russian": "мочь", "topic": "1",
        })
        self.regular = add_word(self.db, {
            "id": "regular", "type": "verb", "german": "gehen", "infinitive": "gehen",
            "partizip2": "gegangen", "russian": "идти", "topic": "1",
        })
        set_word_conjugation(self.db, "modal", PRAESENS, PRAETERITUM)
        set_word_conjugation(self.db, "regular", PRAESENS)
        self.modal.update(
            conjugation_praesens=PRAESENS,
            conjugation_praeteritum=PRAETERITUM,
        )
        self.regular.update(
            conjugation_praesens=PRAESENS,
            conjugation_praeteritum="",
        )

    def _restore_env(self):
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _ctx(self, entry):
        return _Ctx({
            "edit_source": "quiz",
            "quiz_edit_entry": dict(entry),
            "quiz_entry": dict(entry),
            "edit_stage": "pick_field",
            "edit_return_state": DB_SEARCH_VIEW,
        })

    def _conjugation(self, word_id):
        with sqlite3.connect(self.db) as conn:
            return conn.execute(
                "SELECT conjugation_praesens, conjugation_praeteritum FROM words WHERE id = ?",
                (word_id,),
            ).fetchone()

    def _send(self, uid, text, ctx):
        update = _Update(uid, text)
        state = asyncio.run(C.clarify_fields_input(update, ctx))
        self.assertEqual(state, ADD_EDIT)
        return update

    def _open(self, entry, uid=OWNER):
        ctx = self._ctx(entry)
        update = self._send(uid, t("en", "clarify_conjugation"), ctx)
        return update, ctx

    def _action(self, ctx, key, uid=OWNER):
        return self._send(uid, t("en", key), ctx)

    def test_i18n_contract_present_for_all_interfaces(self):
        keys = (
            "clarify_conjugation", "clarify_conjugation_view",
            "clarify_conjugation_edit_praesens", "clarify_conjugation_edit_praeteritum",
            "clarify_conjugation_clear_praesens", "clarify_conjugation_clear_praeteritum",
            "clarify_conjugation_invalid", "clarify_conjugation_forbidden",
            "clarify_conjugation_clear_prompt", "clarify_conjugation_updated",
        )
        for lang in ("en", "ru", "uk"):
            for key in keys:
                self.assertIn(key, TEXTS[lang], f"{lang}/{key}")

    def test_owner_verb_has_submenu_guest_and_nonverb_do_not(self):
        owner_buttons = _flat(C._clarify_screen(self._ctx(self.modal), OWNER))
        guest_buttons = _flat(C._clarify_screen(self._ctx(self.modal), GUEST))
        noun = dict(self.modal, type="nomen")
        noun_buttons = _flat(C._clarify_screen(self._ctx(noun), OWNER))
        label = t("en", "clarify_conjugation")
        self.assertIn(label, owner_buttons)
        self.assertNotIn(label, guest_buttons)
        self.assertNotIn(label, noun_buttons)

    def test_db_search_entry_keyboard_has_owner_submenu(self):
        from bot.handlers.db_search import _send_db_search_card

        update = _Update(OWNER)
        ctx = _Ctx()
        state = asyncio.run(_send_db_search_card(update, ctx, dict(self.modal), "können"))
        self.assertEqual(state, ADD_EDIT)
        buttons = _flat(update.message.calls[-1][1]["reply_markup"])
        self.assertIn(t("en", "clarify_conjugation"), buttons)

    def test_guest_manual_submenu_input_is_rejected(self):
        ctx = self._ctx(self.modal)
        update = self._send(GUEST, t("en", "clarify_conjugation"), ctx)
        self.assertEqual(ctx.user_data["edit_stage"], "pick_field")
        self.assertEqual(update.message.calls[-1][0], t("en", "clarify_conjugation_forbidden"))

    def test_modal_menu_has_both_blocks_regular_hides_praeteritum_actions(self):
        modal_update, modal_ctx = self._open(self.modal)
        regular_update, regular_ctx = self._open(self.regular)
        modal_buttons = _flat(modal_update.message.calls[-1][1]["reply_markup"])
        regular_buttons = _flat(regular_update.message.calls[-1][1]["reply_markup"])
        self.assertEqual(modal_ctx.user_data["edit_stage"], "conjugation_menu")
        self.assertEqual(regular_ctx.user_data["edit_stage"], "conjugation_menu")
        for key in ("clarify_conjugation_edit_praeteritum", "clarify_conjugation_clear_praeteritum"):
            self.assertIn(t("en", key), modal_buttons)
            self.assertNotIn(t("en", key), regular_buttons)

    def test_view_keeps_submenu_and_shows_both_blocks(self):
        _, ctx = self._open(self.modal)
        update = self._action(ctx, "clarify_conjugation_view")
        self.assertEqual(ctx.user_data["edit_stage"], "conjugation_menu")
        rendered = update.message.calls[0][0]
        self.assertIn("_Präsens_", rendered)
        self.assertIn("_Präteritum_", rendered)
        self.assertIn("ich       kann", rendered)
        self.assertIn("ich       konnte", rendered)

    def test_view_and_edit_load_current_blocks_when_session_copy_is_stale(self):
        stale = dict(self.modal)
        stale.pop("conjugation_praesens")
        stale.pop("conjugation_praeteritum")
        _, ctx = self._open(stale)
        update = self._action(ctx, "clarify_conjugation_view")
        self.assertIn("ich       kann", update.message.calls[0][0])
        self.assertIn("ich       konnte", update.message.calls[0][0])

    def test_invalid_praesens_stays_on_stage_without_db_change_then_retry_saves(self):
        _, ctx = self._open(self.modal)
        self._action(ctx, "clarify_conjugation_edit_praesens")
        before = self._conjugation("modal")
        invalid = self._send(OWNER, "ich only-one-line", ctx)
        self.assertEqual(ctx.user_data["edit_stage"], "conjugation_enter")
        self.assertEqual(self._conjugation("modal")[0], before[0])
        self.assertIn(t("en", "clarify_conjugation_invalid"), invalid.message.calls[-1][0])

        self._send(OWNER, PRAESENS_NEW, ctx)
        stored = self._conjugation("modal")
        self.assertEqual(stored[0], PRAESENS_NEW)
        self.assertEqual(stored[1], PRAETERITUM)
        self.assertEqual(ctx.user_data["edit_stage"], "conjugation_menu")

    def test_valid_praeteritum_preserves_praesens(self):
        _, ctx = self._open(self.modal)
        self._action(ctx, "clarify_conjugation_edit_praeteritum")
        self._send(OWNER, PRAETERITUM_NEW, ctx)
        stored = self._conjugation("modal")
        self.assertEqual(stored[0], PRAESENS)
        self.assertEqual(stored[1], PRAETERITUM_NEW)

    def test_clear_praesens_requires_confirm_and_cancel_writes_nothing(self):
        _, ctx = self._open(self.modal)
        self._action(ctx, "clarify_conjugation_clear_praesens")
        self.assertEqual(ctx.user_data["edit_stage"], "conjugation_clear_confirm")
        self._send(OWNER, t("en", "back"), ctx)
        self.assertEqual(self._conjugation("modal")[0], PRAESENS)
        self.assertEqual(ctx.user_data["edit_stage"], "conjugation_menu")

        self._action(ctx, "clarify_conjugation_clear_praesens")
        self._send(OWNER, SYN_CONFIRM_TEXT, ctx)
        stored = self._conjugation("modal")
        self.assertEqual(stored[0], "")
        self.assertEqual(stored[1], PRAETERITUM)

    def test_clear_praeteritum_is_separate_and_confirmed(self):
        _, ctx = self._open(self.modal)
        self._action(ctx, "clarify_conjugation_clear_praeteritum")
        self._send(OWNER, SYN_CONFIRM_TEXT, ctx)
        stored = self._conjugation("modal")
        self.assertEqual(stored[0], PRAESENS)
        self.assertEqual(stored[1], "")

    def test_regular_manual_praeteritum_action_is_explicitly_rejected(self):
        _, ctx = self._open(self.regular)
        update = self._action(ctx, "clarify_conjugation_edit_praeteritum")
        self.assertEqual(ctx.user_data["edit_stage"], "conjugation_menu")
        self.assertEqual(update.message.calls[-1][0], t("en", "clarify_conjugation_forbidden"))
        self.assertEqual(self._conjugation("regular")[0], PRAESENS)

    def test_stale_praeteritum_entry_is_revalidated_without_silent_clear(self):
        _, ctx = self._open(self.modal)
        self._action(ctx, "clarify_conjugation_edit_praeteritum")
        update_word_fields(self.db, "modal", {"infinitive": "gehen"})
        before = self._conjugation("modal")

        update = self._send(OWNER, PRAETERITUM_NEW, ctx)
        after = self._conjugation("modal")
        self.assertEqual(update.message.calls[-1][0], t("en", "clarify_conjugation_forbidden"))
        self.assertEqual(after[1], before[1])
        self.assertEqual(ctx.user_data["edit_stage"], "conjugation_menu")

    def test_back_from_entry_returns_to_submenu_and_back_from_submenu_to_card(self):
        _, ctx = self._open(self.modal)
        self._action(ctx, "clarify_conjugation_edit_praesens")
        self._send(OWNER, t("en", "back"), ctx)
        self.assertEqual(ctx.user_data["edit_stage"], "conjugation_menu")
        self.assertNotIn("conjugation_block", ctx.user_data)
        self._send(OWNER, t("en", "back"), ctx)
        self.assertEqual(ctx.user_data["edit_stage"], "pick_field")

    def test_partizip2_remains_a_normal_clarify_field(self):
        ctx = self._ctx(self.modal)
        ctx.user_data.update(edit_stage="enter_value", edit_field="partizip2")
        self._send(OWNER, "GEMOCHT", ctx)
        stored = get_word_by_id(self.db, "modal")
        self.assertEqual(stored["partizip2"], "gemocht")
        self.assertEqual(self._conjugation("modal"), (PRAESENS, PRAETERITUM))

    def test_generic_update_does_not_gain_conjugation_bypass(self):
        update_word_fields(self.db, "modal", {"conjugation_praesens": PRAESENS_NEW})
        self.assertEqual(self._conjugation("modal")[0], PRAESENS)


if __name__ == "__main__":
    unittest.main()
