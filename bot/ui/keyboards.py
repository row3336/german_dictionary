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
from ..labels import (
    PARSE_VF_TEXT,
    QUIZ_DE_RU_TEXT,
    QUIZ_PL_SG_TEXT,
    QUIZ_RU_DE_TEXT,
    SYNONYMS_TEXT,
)


def _main_menu_keys(user_id):
    """Main menu. The "Add words" button is owner-only (P3.2)."""
    lang = _user_interface_lang(user_id)
    first_row = [t(lang, "tests"), t(lang, "db_search")]
    if is_owner(user_id):
        first_row.insert(0, t(lang, "add_word"))
    return [
        first_row,
        [t(lang, "settings"), t(lang, "stat")],
    ]


def _main_menu_markup(user_id):
    """Handle main menu markup."""
    return ReplyKeyboardMarkup(_main_menu_keys(user_id), resize_keyboard=True)


def _tests_menu_keys(user_id):
    """Tests menu keyboard ("Learning" button is localized, quiz modes are neutral)."""
    lang = _user_interface_lang(user_id)
    return [
        [t(lang, "learning"), QUIZ_RU_DE_TEXT, QUIZ_DE_RU_TEXT, QUIZ_PL_SG_TEXT],
        [t(lang, "topic_filter"), t(lang, "back")],
    ]


def _ru_de_menu_keys(user_id):
    """Lang>De keyboard: standard FSRS + weak modes (labels localized)."""
    lang = _user_interface_lang(user_id)
    return [
        [t(lang, "standard_fsrs"), t(lang, "weak_article_mode")],
        [t(lang, "weak_plural_mode"), t(lang, "weak_verb_mode")],
        [t(lang, "back")],
    ]


def _learning_menu_keys(user_id):
    """Learning menu keyboard (all labels localized)."""
    lang = _user_interface_lang(user_id)
    return [
        [t(lang, "learning_start"), t(lang, "learning_mix_mode"), t(lang, "add_for_learning"), t(lang, "learning_reset")],
        [t(lang, "back")],
    ]


def _settings_menu_keys(user_id):
    """Settings menu. parse_VF and dump are owner-only (P3.2)."""
    lang = _user_interface_lang(user_id)
    owner = is_owner(user_id)
    rows = [[PARSE_VF_TEXT, t(lang, "algo_settings")] if owner else [t(lang, "algo_settings")]]
    rows.append([t(lang, "interface_lang_btn"), t(lang, "dict_lang_btn")])
    rows.append([t(lang, "dump"), t(lang, "back")] if owner else [t(lang, "back")])
    return rows


def _algo_settings_menu_keys(user_id):
    """Algo settings menu. "unit_tests" is owner-only (P3.2)."""
    lang = _user_interface_lang(user_id)
    # "unit_tests" is owner-only (P3.2): in-bot test run.
    study_row = [t(lang, "drill_sett"), t(lang, "learning_sett")]
    if is_owner(user_id):
        study_row.append(t(lang, "unit_tests"))
    return [
        [t(lang, "topic_filter"), t(lang, "ses_rtr_gap"), t(lang, "ses_rtr_it")],
        study_row,
        [t(lang, "reset_periodicity"), t(lang, "reset_defaults")],
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
    """Keyboard under a quiz question (answer options + service buttons)."""
    lang = _user_interface_lang(user_id)
    rows = []
    if article:
        rows.append(["der", "die", "das"])
    elif allow_idk:
        rows.append([t(lang, "send_learn"), I_DONT_KNOW_TEXT, INPUT_ERR_TEXT])
    if is_owner(user_id):
        rows.append([AI_CLARIFY_FIELD, t(lang, "stop")])
    else:
        rows.append([t(lang, "stop")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _quiz_idle_markup(user_id):
    """Keyboard while the quiz is idle (between questions)."""
    lang = _user_interface_lang(user_id)
    rows = [[t(lang, "send_learn"), INPUT_ERR_TEXT]]
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


def _clarify_fields_markup(source, user_id=None):
    """Field-picker keyboard. A guest (non-owner) edits only image/example —
    their personal version (P4); the owner gets the full field grid."""
    if source == "quiz" and user_id is not None and not is_owner(user_id):
        return ReplyKeyboardMarkup([["image", "example", BACK_TEXT]], resize_keyboard=True)
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


def _clarify_root_markup(user_id):
    """Root card keyboard. Editing/deleting a shared word is owner-only (P3.2);
    a guest sees only "send to learning" (their personal learning)."""
    lang = _user_interface_lang(user_id)
    if is_owner(user_id):
        return ReplyKeyboardMarkup(
            [[t(lang, "clarify_fields"), t(lang, "send_learn")], ["delete", t(lang, "back")]],
            resize_keyboard=True,
        )
    # Guest: edit example/image (personal version, P4) + send to learning; no delete.
    return ReplyKeyboardMarkup(
        [[t(lang, "clarify_fields"), t(lang, "send_learn")], [t(lang, "back")]],
        resize_keyboard=True,
    )


def _add_keys(user_id):
    """Handle add keys."""
    return [[t(_user_interface_lang(user_id), "back")]]


def _add_input_markup(user_id):
    """Handle add input markup."""
    return ReplyKeyboardMarkup([[t(_user_interface_lang(user_id), "back")]], resize_keyboard=True)
