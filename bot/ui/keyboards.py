"""Core menu keyboard builders (reply keyboards for the main navigation menus).

Feature-specific markups (db_search/vf/synonyms/edit-fields/weak/dump) stay with
their handler groups and are moved alongside them in later refactor stages.
"""

from telegram import ReplyKeyboardMarkup

from ..common.lang import _user_interface_lang
from ..i18n import t
from ..labels import (
    LEARNING_ADD_TEXT,
    LEARNING_MIX_MODE_TEXT,
    LEARNING_RESET_TEXT,
    LEARNING_START_TEXT,
    LEARNING_TEXT,
    PARSE_VF_TEXT,
    QUIZ_DE_RU_TEXT,
    QUIZ_PL_SG_TEXT,
    QUIZ_RU_DE_TEXT,
    STANDARD_SM2_TEXT,
    TEST_ARTICLE_WEAK_TEXT,
    TEST_PLURAL_WEAK_TEXT,
    TEST_VERB_WEAK_TEXT,
)


def _main_menu_keys(user_id):
    """Handle main menu keys."""
    lang = _user_interface_lang(user_id)
    return [
        [t(lang, "add_word"), t(lang, "tests"), t(lang, "db_search")],
        [t(lang, "settings"), t(lang, "stat")],
    ]


def _main_menu_markup(user_id):
    """Handle main menu markup."""
    return ReplyKeyboardMarkup(_main_menu_keys(user_id), resize_keyboard=True)


def _tests_menu_keys(user_id):
    """Handle tests menu keys."""
    return [
        [LEARNING_TEXT, QUIZ_RU_DE_TEXT, QUIZ_DE_RU_TEXT, QUIZ_PL_SG_TEXT],
        [t(_user_interface_lang(user_id), "topic_filter"), t(_user_interface_lang(user_id), "back")],
    ]


def _ru_de_menu_keys(user_id):
    """Handle ru de menu keys."""
    return [
        [STANDARD_SM2_TEXT, TEST_ARTICLE_WEAK_TEXT],
        [TEST_PLURAL_WEAK_TEXT, TEST_VERB_WEAK_TEXT],
        [t(_user_interface_lang(user_id), "back")],
    ]


def _learning_menu_keys(user_id):
    """Handle learning menu keys."""
    return [
        [LEARNING_START_TEXT, LEARNING_MIX_MODE_TEXT, LEARNING_ADD_TEXT, LEARNING_RESET_TEXT],
        [t(_user_interface_lang(user_id), "back")],
    ]


def _settings_menu_keys(user_id):
    """Handle settings menu keys."""
    lang = _user_interface_lang(user_id)
    return [
        [PARSE_VF_TEXT, t(lang, "algo_settings")],
        [t(lang, "interface_lang_btn"), t(lang, "dict_lang_btn")],
        [t(lang, "dump"), t(lang, "back")],
    ]


def _algo_settings_menu_keys(user_id):
    """Handle algo settings menu keys."""
    lang = _user_interface_lang(user_id)
    return [
        [t(lang, "topic_filter"), t(lang, "ses_rtr_gap"), t(lang, "ses_rtr_it")],
        [t(lang, "drill_sett"), t(lang, "learning_sett"), t(lang, "unit_tests")],
        [t(lang, "reset_periodicity")],
        [t(lang, "back")],
    ]


def _drill_settings_menu_keys(user_id):
    """Handle drill settings menu keys."""
    lang = _user_interface_lang(user_id)
    return [
        [t(lang, "weak_drill_gap"), t(lang, "weak_drill_repeats")],
        [t(lang, "back")],
    ]


def _learning_settings_menu_keys(user_id):
    """Handle learning settings menu keys."""
    lang = _user_interface_lang(user_id)
    return [
        [t(lang, "learning_daily_qty"), t(lang, "learning_intervals"), t(lang, "learning_ready_quorum")],
        [t(lang, "learning_reminder_interval"), t(lang, "learning_reminder_cooldown"), t(lang, "back")],
    ]
