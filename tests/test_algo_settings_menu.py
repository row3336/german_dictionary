"""Bug #0 — algo_sett reorganization: FSRS/weak submenus + per-setting
explanations, button layout, and action routing."""

import asyncio
import os
import tempfile
import unittest

from _telegram_stub import install

install()

from bot.app_config import (  # noqa: E402
    ALGO_SETTINGS_EDIT,
    ALGO_SETTINGS_MENU,
    DRILL_SETTINGS_MENU,
    FSRS_SETTINGS_MENU,
    LEARNING_SETTINGS_MENU,
)
from bot.common.navigation import _action_key_from_text  # noqa: E402
from bot.handlers.settings import (  # noqa: E402
    algo_settings_edit,
    algo_settings_menu_choice,
    fsrs_settings_menu_choice,
)
from bot.i18n import TEXTS, t  # noqa: E402
from bot.storage import init_db, register_or_update_user, set_user_interface_lang  # noqa: E402
from bot.ui import keyboards as K  # noqa: E402

OWNER, GUEST = 999, 111

_DESC_KEYS = [
    "fsrs_sett_desc", "ses_rtr_gap_desc", "ses_rtr_it_desc",
    "weak_sett_desc", "weak_drill_gap_desc", "weak_drill_repeats_desc",
    "learning_sett_desc", "learning_daily_qty_desc", "learning_intervals_desc",
    "learning_ready_quorum_desc", "learning_reminder_interval_desc",
    "learning_reminder_cooldown_desc",
]


class _Msg:
    def __init__(self, text):
        self.text = text
        self.replies = []
        self.markups = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        self.markups.append(kw.get("reply_markup"))


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


class AlgoSettingsMenuTest(unittest.TestCase):
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

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # --- layout -----------------------------------------------------------
    def test_root_has_submenus_not_inline_retry_params(self):
        flat = _flat(K._algo_settings_menu_keys(GUEST))
        self.assertIn(t("en", "fsrs_sett"), flat)
        self.assertIn(t("en", "weak_sett"), flat)
        self.assertIn(t("en", "learning_sett"), flat)
        # the two FSRS retry params moved into the FSRS submenu, off the root
        self.assertNotIn(t("en", "ses_rtr_gap"), flat)
        self.assertNotIn(t("en", "ses_rtr_it"), flat)

    def test_fsrs_submenu_has_retry_params(self):
        flat = _flat(K._fsrs_settings_menu_keys(GUEST))
        self.assertIn(t("en", "ses_rtr_gap"), flat)
        self.assertIn(t("en", "ses_rtr_it"), flat)
        self.assertIn(t("en", "back"), flat)

    def test_weak_submenu_has_drill_params(self):
        flat = _flat(K._drill_settings_menu_keys(GUEST))
        self.assertIn(t("en", "weak_drill_gap"), flat)
        self.assertIn(t("en", "weak_drill_repeats"), flat)
        self.assertIn(t("en", "back"), flat)

    def test_en_submenu_label_is_raw_key(self):
        self.assertEqual(TEXTS["en"]["algo_settings"], "algo_sett")

    # --- action routing ---------------------------------------------------
    def test_action_mapping_all_langs(self):
        for lang in ("en", "ru", "uk"):
            set_user_interface_lang(self.db, GUEST, lang)
            self.assertEqual(_action_key_from_text(t(lang, "fsrs_sett"), GUEST), "fsrs_sett")
            self.assertEqual(_action_key_from_text(t(lang, "weak_sett"), GUEST), "weak_sett")

    def test_press_fsrs_opens_submenu_with_explanations(self):
        set_user_interface_lang(self.db, GUEST, "ru")
        upd = _Update(t("ru", "fsrs_sett"), GUEST)
        state = _run(algo_settings_menu_choice(upd, _Ctx()))
        self.assertEqual(state, FSRS_SETTINGS_MENU)
        body = upd.message.replies[-1]
        self.assertIn(t("ru", "fsrs_sett_desc"), body)
        self.assertIn(t("ru", "ses_rtr_gap_desc"), body)

    def test_press_weak_opens_submenu_with_explanations(self):
        set_user_interface_lang(self.db, GUEST, "ru")
        upd = _Update(t("ru", "weak_sett"), GUEST)
        state = _run(algo_settings_menu_choice(upd, _Ctx()))
        self.assertEqual(state, DRILL_SETTINGS_MENU)
        self.assertIn(t("ru", "weak_drill_gap_desc"), upd.message.replies[-1])

    def test_press_learning_opens_submenu_with_explanations(self):
        set_user_interface_lang(self.db, GUEST, "ru")
        upd = _Update(t("ru", "learning_sett"), GUEST)
        state = _run(algo_settings_menu_choice(upd, _Ctx()))
        self.assertEqual(state, LEARNING_SETTINGS_MENU)
        self.assertIn(t("ru", "learning_daily_qty_desc"), upd.message.replies[-1])

    def test_fsrs_back_returns_to_root(self):
        state = _run(fsrs_settings_menu_choice(_Update("back", GUEST), _Ctx()))
        self.assertEqual(state, ALGO_SETTINGS_MENU)

    def test_edit_from_fsrs_returns_to_fsrs(self):
        # press ses_rtr_gap inside the FSRS submenu, then submit a value
        ctx = _Ctx()
        state = _run(fsrs_settings_menu_choice(_Update(t("en", "ses_rtr_gap"), GUEST), ctx))
        self.assertEqual(state, ALGO_SETTINGS_EDIT)
        self.assertEqual(ctx.user_data["algo_edit_return"], "fsrs")
        state = _run(algo_settings_edit(_Update("4", GUEST), ctx))
        self.assertEqual(state, FSRS_SETTINGS_MENU)

    # --- #19: value-input prompts offer a Back button (exit without a value) ---
    def test_value_prompt_offers_back_button(self):
        from telegram import ReplyKeyboardMarkup
        upd = _Update(t("en", "ses_rtr_gap"), GUEST)
        _run(fsrs_settings_menu_choice(upd, _Ctx()))
        markup = upd.message.markups[-1]
        self.assertIsInstance(markup, ReplyKeyboardMarkup)
        self.assertIn(t("en", "back"), [c for row in markup.rows for c in row])

    def test_value_prompt_back_cancels_to_submenu(self):
        # enter the edit, then press Back -> returns to the originating submenu,
        # and no value got persisted (state cleared)
        ctx = _Ctx()
        _run(fsrs_settings_menu_choice(_Update(t("en", "ses_rtr_gap"), GUEST), ctx))
        state = _run(algo_settings_edit(_Update(t("en", "back"), GUEST), ctx))
        self.assertEqual(state, FSRS_SETTINGS_MENU)
        self.assertNotIn("algo_setting_edit", ctx.user_data)

    def test_reset_periodicity_prompt_offers_back_button(self):
        from telegram import ReplyKeyboardMarkup
        upd = _Update(t("en", "reset_periodicity"), GUEST)
        _run(algo_settings_menu_choice(upd, _Ctx()))
        markup = upd.message.markups[-1]
        self.assertIsInstance(markup, ReplyKeyboardMarkup)
        self.assertIn(t("en", "back"), [c for row in markup.rows for c in row])

    # --- i18n completeness ------------------------------------------------
    def test_explanation_keys_present_and_localized(self):
        for key in _DESC_KEYS:
            for lang in ("en", "ru", "uk"):
                self.assertIn(key, TEXTS[lang], f"{lang}/{key} missing")
                self.assertTrue(str(TEXTS[lang][key]).strip(), f"{lang}/{key} empty")
            # ru/uk must be translated, not the raw english fallback
            self.assertNotEqual(TEXTS["ru"][key], TEXTS["en"][key], f"ru/{key} not translated")
            self.assertNotEqual(TEXTS["uk"][key], TEXTS["en"][key], f"uk/{key} not translated")


if __name__ == "__main__":
    unittest.main()
