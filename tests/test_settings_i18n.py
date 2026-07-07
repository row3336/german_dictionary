"""#15 step 7 — settings/menu transient prompts, validators, reset messages and
menu fallbacks rendered as raw English for ru/uk users. The settings *screens*
and per-param *descriptions* were already localized; this step localizes the
leftover transient bits.

Design (owner, 2026-07-07):
- generic UI chrome (Canceled / Use X buttons / reset messages / validators /
  "Choose interface language:") -> new localized keys.
- per-setting "Enter <param>:" input prompt -> reuse existing `clarify_enter_value`
  with the already-localized param name: EN keeps the technical id (ses.rtr_gap),
  ru/uk show the human name (интервал повтора).
- owner-only parse_VF/proxy prompts stay English (not asserted here).
"""

import asyncio
import os
import tempfile
import unittest

from _telegram_stub import install

install()

from bot.app_config import (  # noqa: E402
    LEARNING_INTERVALS_KEY,
    LEARNING_READY_QUORUM_KEY,
    SESSION_RETRY_GAP_KEY,
)
from bot.handlers import menu as M  # noqa: E402
from bot.handlers import settings as S  # noqa: E402
from bot.i18n import TEXTS  # noqa: E402
from bot.storage import (  # noqa: E402
    init_db,
    register_or_update_user,
    set_user_interface_lang,
)

OWNER, GUEST = 999, 111

_NEW_KEYS = (
    "choose_interface_lang",
    "settings_reset_periodicity_confirm",
    "settings_algo_reset_done",
    "settings_periodicity_reset_done",
    "settings_canceled",
    "settings_topic_selection_canceled",
    "settings_use_algo_buttons",
    "settings_use_drill_buttons",
    "settings_use_learning_buttons",
    "settings_use_buttons",
    "settings_use_tests_buttons",
    "settings_enter_csv_seconds",
    "settings_enter_number",
    "settings_range_1_100",
    "settings_min_1",
)


class _Msg:
    def __init__(self, text=""):
        self.text = text
        self.calls = []

    async def reply_text(self, text, **kw):
        self.calls.append((text, kw))
        return type("Sent", (), {"message_id": 1})()


class _Update:
    def __init__(self, uid, text=""):
        self.message = _Msg(text)
        self.effective_user = type("U", (), {"id": uid, "username": "u", "first_name": "U"})()
        self.effective_chat = type("C", (), {"id": uid})()


class _Ctx:
    def __init__(self, user_data=None):
        self.user_data = user_data or {}


def _run(coro):
    return asyncio.run(coro)


class SettingsI18nTest(unittest.TestCase):
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
        set_user_interface_lang(self.db, GUEST, "ru")

    def _restore(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _texts(self, update):
        return "\n".join(c[0] for c in update.message.calls)

    # ---- i18n table completeness ----

    def test_new_keys_present_and_localized(self):
        for key in _NEW_KEYS:
            for lang in ("en", "ru", "uk"):
                self.assertIn(key, TEXTS[lang], f"{key} missing in {lang}")
                self.assertTrue(TEXTS[lang][key].strip(), f"{key} empty in {lang}")
            self.assertNotEqual(TEXTS["ru"][key], TEXTS["en"][key], key)
            self.assertNotEqual(TEXTS["uk"][key], TEXTS["en"][key], key)

    # ---- 7c: interface-language prompt ----

    def test_ask_interface_language_localized(self):
        update = _Update(GUEST)
        _run(S._ask_interface_language(update))
        self.assertEqual(update.message.calls[-1][0], TEXTS["ru"]["choose_interface_lang"])

    # ---- 7a: reset confirm / done / cancel / fallback ----

    def test_periodicity_confirm_yes_no_localized(self):
        up_yes = _Update(GUEST, "yes")
        _run(S.algo_settings_confirm(up_yes, _Ctx()))
        self.assertIn(TEXTS["ru"]["settings_periodicity_reset_done"], self._texts(up_yes))

        up_no = _Update(GUEST, "nope")
        _run(S.algo_settings_confirm(up_no, _Ctx()))
        self.assertIn(TEXTS["ru"]["settings_canceled"], self._texts(up_no))
        self.assertNotIn("Canceled.", self._texts(up_no))

    def test_algo_menu_reset_and_fallback_localized(self):
        up_conf = _Update(GUEST, TEXTS["ru"]["reset_periodicity"])
        _run(S.algo_settings_menu_choice(up_conf, _Ctx()))
        self.assertIn(TEXTS["ru"]["settings_reset_periodicity_confirm"], self._texts(up_conf))

        up_reset = _Update(GUEST, TEXTS["ru"]["reset_defaults"])
        _run(S.algo_settings_menu_choice(up_reset, _Ctx()))
        self.assertIn(TEXTS["ru"]["settings_algo_reset_done"], self._texts(up_reset))

        up_bad = _Update(GUEST, "zzz-not-a-button")
        _run(S.algo_settings_menu_choice(up_bad, _Ctx()))
        self.assertEqual(up_bad.message.calls[-1][0], TEXTS["ru"]["settings_use_algo_buttons"])

    # ---- 7b: value validators ----

    def _edit(self, key, text):
        up = _Update(GUEST, text)
        _run(S.algo_settings_edit(up, _Ctx({"algo_setting_edit": key, "algo_edit_return": "learning"})))
        return self._texts(up)

    def test_validator_not_a_number(self):
        self.assertIn(TEXTS["ru"]["settings_enter_number"], self._edit(SESSION_RETRY_GAP_KEY, "abc"))

    def test_validator_range_1_100(self):
        self.assertIn(TEXTS["ru"]["settings_range_1_100"], self._edit(LEARNING_READY_QUORUM_KEY, "200"))

    def test_validator_min_1(self):
        self.assertIn(TEXTS["ru"]["settings_min_1"], self._edit(SESSION_RETRY_GAP_KEY, "0"))

    def test_validator_csv_seconds(self):
        self.assertIn(TEXTS["ru"]["settings_enter_csv_seconds"], self._edit(LEARNING_INTERVALS_KEY, "not-numbers"))

    # ---- 7d: per-setting "Enter <param>:" prompt (reuse clarify_enter_value) ----

    def test_param_prompt_localized_name_for_ru(self):
        up = _Update(GUEST, TEXTS["ru"]["ses_rtr_gap"])
        _run(S.fsrs_settings_menu_choice(up, _Ctx()))
        last = up.message.calls[-1][0]
        # ru: localized wrapper + localized param name, no raw English
        self.assertEqual(last, TEXTS["ru"]["clarify_enter_value"].format(field=TEXTS["ru"]["ses_rtr_gap"]))
        self.assertIn("интервал повтора", last)
        self.assertNotIn("ses.rtr_gap", last)

    def test_param_prompt_keeps_technical_id_for_en(self):
        set_user_interface_lang(self.db, GUEST, "en")
        up = _Update(GUEST, TEXTS["en"]["ses_rtr_gap"])
        _run(S.fsrs_settings_menu_choice(up, _Ctx()))
        last = up.message.calls[-1][0]
        # en: technical identifier stays
        self.assertIn("ses.rtr_gap", last)

    # ---- 7e: tests-menu fallback ----

    def test_tests_menu_fallback_localized(self):
        up = _Update(GUEST, "zzz-not-a-button")
        _run(M.tests_menu_choice(up, _Ctx()))
        self.assertEqual(up.message.calls[-1][0], TEXTS["ru"]["settings_use_tests_buttons"])


if __name__ == "__main__":
    unittest.main()
