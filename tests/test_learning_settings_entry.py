"""Learning menu shortcut to the learning-settings submenu.

The submenu now has two entry points (algo settings and the learning menu), so
"back" must return to whichever one was used. Covers layout, routing, the
origin flag and the nested value-edit round trip.
"""

import asyncio
import os
import tempfile
import unittest

from _telegram_stub import install

install()

from bot.app_config import (  # noqa: E402
    ALGO_SETTINGS_EDIT,
    ALGO_SETTINGS_MENU,
    LEARNING_MENU,
    LEARNING_SETTINGS_MENU,
)
from bot.common.navigation import _action_key_from_text  # noqa: E402
from bot.handlers.learning import learning_menu_choice  # noqa: E402
from bot.handlers.settings import (  # noqa: E402
    algo_settings_edit,
    algo_settings_menu_choice,
    learning_settings_menu_choice,
)
from bot.i18n import t  # noqa: E402
from bot.storage import init_db, register_or_update_user, set_user_interface_lang  # noqa: E402
from bot.ui import keyboards as K  # noqa: E402

OWNER, GUEST = 999, 111


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


def _run(coro):
    return asyncio.run(coro)


class LearningSettingsEntryTest(unittest.TestCase):
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
    def test_second_row_is_settings_then_back(self):
        rows = K._learning_menu_keys(GUEST)
        self.assertEqual(rows[1], [t("en", "learning_sett_btn"), t("en", "back")])

    def test_settings_button_localized_in_all_langs(self):
        for lang in ("en", "ru", "uk"):
            set_user_interface_lang(self.db, GUEST, lang)
            rows = K._learning_menu_keys(GUEST)
            self.assertEqual(rows[1][0], t(lang, "learning_sett_btn"), lang)
            self.assertEqual(_action_key_from_text(rows[1][0], GUEST), "learning_sett", lang)

    def test_button_label_never_collides_with_main_menu_settings(self):
        # _action_key_from_text builds one global text->action map (and always
        # registers the English label too), so an identical string would silently
        # re-route the main-menu Settings button into this submenu.
        for lang in ("en", "ru", "uk"):
            set_user_interface_lang(self.db, GUEST, lang)
            for label in {t(lang, "learning_sett_btn"), t("en", "learning_sett_btn")}:
                self.assertNotEqual(label, t(lang, "settings"), lang)
                self.assertNotEqual(label, t("en", "settings"), lang)
            self.assertEqual(_action_key_from_text(t(lang, "settings"), GUEST), "settings", lang)

    def test_first_row_unchanged(self):
        rows = K._learning_menu_keys(GUEST)
        self.assertEqual(
            rows[0],
            [
                t("en", "learning_start"),
                t("en", "learning_mix_mode"),
                t("en", "add_for_learning"),
                t("en", "learning_reset"),
            ],
        )

    # --- routing ----------------------------------------------------------
    def test_param_labels_drop_the_learning_prefix(self):
        # The submenu header already says "learning"; repeating it in every
        # button made the rows overflow (owner, 01.08).
        for key in (
            "learning_daily_qty",
            "learning_intervals",
            "learning_ready_quorum",
            "learning_reminder_interval",
            "learning_reminder_cooldown",
        ):
            for lang in ("en", "ru", "uk"):
                self.assertFalse(t(lang, key).startswith("learning"), f"{lang}/{key}")

    def test_algo_entry_label_matches_sibling_submenus(self):
        # The algorithm settings row exposes FSRS and learning settings.
        self.assertEqual(t("en", "learning_sett"), "learning_sett")

    def test_press_opens_submenu_and_marks_origin(self):
        set_user_interface_lang(self.db, GUEST, "uk")
        ctx = _Ctx()
        upd = _Update(t("uk", "learning_sett_btn"), GUEST)
        state = _run(learning_menu_choice(upd, ctx))
        self.assertEqual(state, LEARNING_SETTINGS_MENU)
        self.assertEqual(ctx.user_data["learning_sett_return"], "learning_menu")
        self.assertIn(t("uk", "learning_daily_qty_desc"), upd.message.replies[-1])

    def test_back_returns_to_learning_menu(self):
        ctx = _Ctx()
        _run(learning_menu_choice(_Update(t("en", "learning_sett_btn"), GUEST), ctx))
        state = _run(learning_settings_menu_choice(_Update(t("en", "back"), GUEST), ctx))
        self.assertEqual(state, LEARNING_MENU)
        # flag consumed: a later entry from algo settings must not be hijacked
        self.assertNotIn("learning_sett_return", ctx.user_data)

    def test_back_still_returns_to_algo_when_entered_there(self):
        ctx = _Ctx()
        state = _run(algo_settings_menu_choice(_Update(t("en", "learning_sett"), GUEST), ctx))
        self.assertEqual(state, LEARNING_SETTINGS_MENU)
        self.assertEqual(ctx.user_data["learning_sett_return"], "algo")
        state = _run(learning_settings_menu_choice(_Update(t("en", "back"), GUEST), ctx))
        self.assertEqual(state, ALGO_SETTINGS_MENU)

    def test_back_defaults_to_algo_without_flag(self):
        # stale session / restart: no origin recorded -> old behavior
        state = _run(learning_settings_menu_choice(_Update(t("en", "back"), GUEST), _Ctx()))
        self.assertEqual(state, ALGO_SETTINGS_MENU)

    def test_algo_entry_overwrites_stale_learning_origin(self):
        ctx = _Ctx()
        ctx.user_data["learning_sett_return"] = "learning_menu"
        _run(algo_settings_menu_choice(_Update(t("en", "learning_sett"), GUEST), ctx))
        self.assertEqual(ctx.user_data["learning_sett_return"], "algo")

    # --- nested value edit keeps the origin -------------------------------
    def test_edit_value_then_back_lands_in_learning_menu(self):
        ctx = _Ctx()
        _run(learning_menu_choice(_Update(t("en", "learning_sett_btn"), GUEST), ctx))
        state = _run(learning_settings_menu_choice(_Update(t("en", "learning_daily_qty"), GUEST), ctx))
        self.assertEqual(state, ALGO_SETTINGS_EDIT)
        state = _run(algo_settings_edit(_Update("7", GUEST), ctx))
        self.assertEqual(state, LEARNING_SETTINGS_MENU)
        self.assertEqual(ctx.user_data["learning_sett_return"], "learning_menu")
        state = _run(learning_settings_menu_choice(_Update(t("en", "back"), GUEST), ctx))
        self.assertEqual(state, LEARNING_MENU)

    def test_edit_cancel_then_back_lands_in_learning_menu(self):
        ctx = _Ctx()
        _run(learning_menu_choice(_Update(t("en", "learning_sett_btn"), GUEST), ctx))
        _run(learning_settings_menu_choice(_Update(t("en", "learning_intervals"), GUEST), ctx))
        state = _run(algo_settings_edit(_Update(t("en", "back"), GUEST), ctx))
        self.assertEqual(state, LEARNING_SETTINGS_MENU)
        state = _run(learning_settings_menu_choice(_Update(t("en", "back"), GUEST), ctx))
        self.assertEqual(state, LEARNING_MENU)


if __name__ == "__main__":
    unittest.main()
