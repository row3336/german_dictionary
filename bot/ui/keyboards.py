"""Core menu keyboard builders (reply keyboards for the main navigation menus).

Feature-specific markups (db_search/vf/synonyms/edit-fields/weak/dump) stay with
their handler groups and are moved alongside them in later refactor stages.
"""

from telegram import ReplyKeyboardMarkup

from ..app_config import (
    AI_CLARIFY_FIELD,
    BACK_TEXT,
    I_DONT_KNOW_TEXT,
    INPUT_ERR_TEXT,
    is_owner,
)
from ..common.lang import _user_interface_lang
from ..i18n import t
from ..labels import QUIZ_FIELDS_TEXT, SEND_LEARN_TEXT, SYNONYMS_TEXT
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
    STANDARD_FSRS_TEXT,
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
        [STANDARD_FSRS_TEXT, TEST_ARTICLE_WEAK_TEXT],
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


def _quiz_markup(user_id, article=False, allow_idk=True):
    """Handle quiz markup."""
    lang = _user_interface_lang(user_id)
    rows = []
    if article:
        rows.append(["der", "die", "das"])
    elif allow_idk:
        rows.append([SEND_LEARN_TEXT, I_DONT_KNOW_TEXT, INPUT_ERR_TEXT])
    if is_owner(user_id):
        rows.append([AI_CLARIFY_FIELD, t(lang, "stop")])
    else:
        rows.append([t(lang, "stop")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _quiz_idle_markup(user_id):
    """Handle quiz idle markup."""
    lang = _user_interface_lang(user_id)
    rows = [[SEND_LEARN_TEXT, INPUT_ERR_TEXT]]
    if is_owner(user_id):
        rows.append([AI_CLARIFY_FIELD, t(lang, "back")])
    else:
        rows.append([t(lang, "back")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _weak_article_markup(user_id):
    """Handle weak article markup."""
    lang = _user_interface_lang(user_id)
    rows = [["der", "die", "das"]]
    if is_owner(user_id):
        rows.append([AI_CLARIFY_FIELD, INPUT_ERR_TEXT, t(lang, "stop")])
    else:
        rows.append([INPUT_ERR_TEXT, t(lang, "stop")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _edit_fields_markup(source):
    """Handle edit fields markup."""
    if source == "add":
        return ReplyKeyboardMarkup(
            [
                ["ru", "ukr", "en", "de"],
                ["article", "plural", "type", "infinitive"],
                ["prateritum", "partizip2", "komparativ", "superlativ"],
                ["image", "example", BACK_TEXT],
            ],
            resize_keyboard=True,
        )
    if source == "quiz":
        return ReplyKeyboardMarkup(
            [
                ["ru", "ukr", "de", SYNONYMS_TEXT],
                ["article", "plural", "type", "infinitive"],
                ["prateritum", "partizip2", "komparativ", "superlativ"],
                ["image", "example", BACK_TEXT],
            ],
            resize_keyboard=True,
    )
    return ReplyKeyboardMarkup([["russian", BACK_TEXT]], resize_keyboard=True)


def _type_choice_markup():
    """Handle type choice markup."""
    types = ["Nomen", "Verb", "Phrase", "Adjektiv", "Pronomen", "Adverb", "Praeposition", "Konjunktion", "Partikel"]
    rows = [types[i:i + 4] for i in range(0, len(types), 4)]
    if rows and len(rows[-1]) < 4:
        rows[-1].append(BACK_TEXT)
    else:
        rows.append([BACK_TEXT])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _quiz_edit_root_markup(user_id):
    """Handle quiz edit root markup."""
    return ReplyKeyboardMarkup(
        [[QUIZ_FIELDS_TEXT, SEND_LEARN_TEXT], ["delete", t(_user_interface_lang(user_id), "back")]],
        resize_keyboard=True,
    )
