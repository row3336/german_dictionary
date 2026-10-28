"""#34 — FSRS_sett is one inline message: toggles edit it in place, never re-send."""
import asyncio
import os
import tempfile
import unittest
from itertools import product

from _telegram_stub import install

install()

from fsrs_menu_fake import Ctx, Msg, buttons, entry_update, press  # noqa: E402
from telegram.error import TimedOut  # noqa: E402

from bot.app_config import ALGO_SETTINGS_EDIT, ALGO_SETTINGS_MENU, FSRS_SETTINGS_MENU  # noqa: E402
from bot.common import match_policy as P  # noqa: E402
from bot.handlers import settings as S  # noqa: E402
from bot.i18n import t  # noqa: E402
from bot.storage import init_db, register_or_update_user, set_user_interface_lang, set_user_setting, set_user_status  # noqa: E402

OWNER, A, B = 999, 111, 222
ON, OFF = "🟢 [ON]", "⚪️ [OFF]"


class FsrsInlineTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)
        self._old = {k: os.environ.get(k) for k in ("DB_PATH", "OWNER_ID")}
        os.environ.update(DB_PATH=self.db, OWNER_ID=str(OWNER))
        self.addCleanup(self._restore)
        for uid in (A, B):
            register_or_update_user(self.db, uid, "u", "U")
            set_user_status(self.db, uid, "approved")

    def _restore(self):
        for k, v in self._old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def open(self, uid=A, ctx=None):
        ctx = ctx or Ctx()
        upd = entry_update(t("en", "fsrs_sett"), uid)
        state = asyncio.run(S._send_fsrs_settings_menu(upd, uid, ctx))
        self.assertEqual(state, FSRS_SETTINGS_MENU)
        self.assertEqual(len(upd.message.sent), 1)
        return ctx, upd.message.sent[0]

    def status_lines(self, msg):
        return [ln for ln in msg.text.splitlines() if ln.startswith((ON, OFF))]

    def test_four_flag_combinations_three_languages_toggle_by_edit(self):
        for lang, (pr, hv) in product(("en", "ru", "uk"), product((False, True), repeat=2)):
            with self.subTest(lang=lang, pr=pr, hv=hv):
                set_user_interface_lang(self.db, A, lang)
                set_user_setting(self.db, A, P.PR_KEY, "1" if pr else "0")
                set_user_setting(self.db, A, P.KEY, "1" if hv else "0")
                ctx, msg = self.open()
                original = msg.text
                self.assertEqual(self.status_lines(msg), [
                    f"{ON if pr else OFF} - {t(lang, 'prateritum_match_status')}",
                    f"{ON if hv else OFF} - {t(lang, 'hilfsverb_match_status')}"])
                self.assertNotIn("•", msg.text.split("\n\n")[1].split("\n")[0])
                for data, key, flag in (("fsrs:pr", P.PR_KEY, pr), ("fsrs:hv", P.KEY, hv)):
                    state, q = press(S.fsrs_settings_callback, ctx, A, data, msg)
                    self.assertEqual(state, FSRS_SETTINGS_MENU)
                    self.assertEqual(P.enabled(self.db, A, key), not flag)
                    self.assertEqual(len(q.edits), 1)
                self.assertEqual(msg.sent, [])  # no confirmation / second FSRS message
                self.assertEqual(self.status_lines(msg), [
                    f"{OFF if pr else ON} - {t(lang, 'prateritum_match_status')}",
                    f"{OFF if hv else ON} - {t(lang, 'hilfsverb_match_status')}"])
                # description and both params survive; only status lines differ
                rest = lambda s: [ln for ln in s.splitlines() if not ln.startswith((ON, OFF))]
                self.assertEqual(rest(msg.text), rest(original))
                self.assertIn(t(lang, "ses_rtr_gap_desc"), msg.text)
                self.assertIn(t(lang, "ses_rtr_it_desc"), msg.text)
                self.assertEqual(buttons(msg), ["fsrs:gap", "fsrs:it", "fsrs:pr", "fsrs:hv", "fsrs:back"])

    def test_other_user_untouched_and_foreign_or_stale_message_ignored(self):
        ctx, msg = self.open()
        _, other = self.open(B, Ctx())
        state, q = press(S.fsrs_settings_callback, Ctx(), B, "fsrs:pr", msg)  # B pressing A's message
        self.assertIsNone(state)
        self.assertFalse(P.enabled(self.db, B, P.PR_KEY))
        self.assertEqual(q.edits, [])
        ctx2, newer = self.open(A, ctx)  # re-entry makes the old message stale
        state, q = press(S.fsrs_settings_callback, ctx, A, "fsrs:pr", msg)
        self.assertIsNone(state)
        self.assertFalse(P.enabled(self.db, A, P.PR_KEY))
        self.assertEqual((q.edits, q.acks, msg.sent), ([], 1, []))
        state, _ = press(S.fsrs_settings_callback, ctx, A, "fsrs:pr", newer)
        self.assertEqual(state, FSRS_SETTINGS_MENU)
        self.assertTrue(P.enabled(self.db, A, P.PR_KEY))

    def test_revoked_access_changes_nothing(self):
        ctx, msg = self.open()
        set_user_status(self.db, A, "rejected")
        state, q = press(S.fsrs_settings_callback, ctx, A, "fsrs:hv", msg)
        self.assertIsNone(state)
        self.assertFalse(P.enabled(self.db, A, P.KEY))
        self.assertEqual(q.edits, [])

    def test_failed_edit_rolls_back_and_announces_nothing(self):
        ctx, msg = self.open()
        before = msg.text
        press(S.fsrs_settings_callback, ctx, A, "fsrs:hv", msg, fail_edit=TimedOut("down"))
        self.assertFalse(P.enabled(self.db, A, P.KEY))
        self.assertEqual((msg.text, msg.sent), (before, []))

    def test_params_prompt_then_edit_returns_with_fresh_menu_and_stale_old_one(self):
        ctx, msg = self.open()
        state, _ = press(S.fsrs_settings_callback, ctx, A, "fsrs:gap", msg)
        self.assertEqual(state, ALGO_SETTINGS_EDIT)
        self.assertEqual(ctx.user_data["algo_edit_return"], "fsrs")
        self.assertIn("ses.rtr_gap", msg.sent[0].text)
        upd = entry_update("7", A)
        self.assertEqual(asyncio.run(S.algo_settings_edit(upd, ctx)), FSRS_SETTINGS_MENU)
        self.assertIn("7", upd.message.sent[0].text)
        self.assertEqual(ctx.user_data["fsrs_menu"]["msg"], upd.message.sent[0].message_id)

    def test_back_goes_to_algo_menu_and_old_buttons_die(self):
        ctx, msg = self.open()
        state, _ = press(S.fsrs_settings_callback, ctx, A, "fsrs:back", msg)
        self.assertEqual(state, ALGO_SETTINGS_MENU)
        self.assertIn(t("en", "algo_settings"), msg.sent[0].text)
        self.assertNotIn("fsrs_menu", ctx.user_data)
        state, _ = press(S.fsrs_settings_callback, ctx, A, "fsrs:pr", msg)
        self.assertIsNone(state)
        self.assertFalse(P.enabled(self.db, A, P.PR_KEY))

    def test_text_back_from_algo_keyboard_works_and_other_text_is_silent(self):
        ctx, msg = self.open()
        upd = entry_update("Hilfsverb_match", A)
        self.assertEqual(asyncio.run(S.fsrs_settings_menu_choice(upd, ctx)), FSRS_SETTINGS_MENU)
        self.assertEqual(upd.message.sent, [])
        self.assertFalse(P.enabled(self.db, A, P.KEY))
        upd = entry_update(t("en", "back"), A)
        self.assertEqual(asyncio.run(S.fsrs_settings_menu_choice(upd, ctx)), ALGO_SETTINGS_MENU)
        self.assertNotIn("fsrs_menu", ctx.user_data)

    def test_owner_also_works_via_algo_menu_entry(self):
        upd = entry_update(t("en", "fsrs_sett"), OWNER)
        ctx = Ctx()
        self.assertEqual(asyncio.run(S.algo_settings_menu_choice(upd, ctx)), FSRS_SETTINGS_MENU)
        self.assertEqual(len(upd.message.sent), 1)
        state, _ = press(S.fsrs_settings_callback, ctx, OWNER, "fsrs:pr", upd.message.sent[0])
        self.assertTrue(P.enabled(self.db, OWNER, P.PR_KEY))


if __name__ == "__main__":
    unittest.main()
