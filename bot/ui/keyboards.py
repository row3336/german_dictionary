"""Core menu keyboard builders (reply keyboards for the main navigation menus).

Feature-specific markups (db_search/vf/synonyms/edit-fields/dump) stay with
their handler groups and are moved alongside them in later refactor stages.
"""

from telegram import ReplyKeyboardMarkup

from ..app_config import (
    BACK_TEXT,
    CASE_TYPES,
    I_DONT_KNOW_TEXT,
    PREPOSITION_TYPES,
    is_owner,
)
from ..common.lang import _quiz_dir_labels, _user_interface_lang
from ..common.add_words_gate import add_words_enabled
from ..i18n import t
from ..labels import (
    PARSE_VF_TEXT,
    QUIZ_PL_SG_TEXT,
    SYNONYMS_TEXT,
)


def _main_menu_keys(user_id):
    """Main menu; bot Add words stays hidden while the feature is disabled."""
    lang = _user_interface_lang(user_id)
    first_row = [t(lang, "tests"), t(lang, "db_search")]
    if is_owner(user_id) and add_words_enabled():
        first_row.insert(0, t(lang, "add_word"))
    return [
        first_row,
        [t(lang, "settings"), t(lang, "stat")],
    ]


def _main_menu_markup(user_id):
    """Handle main menu markup."""
    return ReplyKeyboardMarkup(_main_menu_keys(user_id), resize_keyboard=True)


def _tests_menu_keys(user_id):
    """Tests menu keyboard. Quiz-direction buttons carry the dict language (#17)."""
    lang = _user_interface_lang(user_id)
    l1de, del1 = _quiz_dir_labels(user_id)
    return [
        [t(lang, "learning"), l1de, del1, QUIZ_PL_SG_TEXT],
        [t(lang, "conjugation"), t(lang, "topic_filter"), t(lang, "back")],
    ]


def _ru_de_menu_keys(user_id):
    """Lang>De keyboard for separate word and phrase FSRS modes."""
    lang = _user_interface_lang(user_id)
    return [
        [t(lang, "quiz_words"), t(lang, "quiz_phrases")],
        [t(lang, "back")],
    ]


def _de_l1_menu_keys(user_id):
    """De>Lang keyboard: choose isolated word or phrase SRS."""
    lang = _user_interface_lang(user_id)
    return [
        [t(lang, "quiz_words"), t(lang, "quiz_phrases")],
        [t(lang, "back")],
    ]


def _learning_menu_keys(user_id):
    """Learning menu keyboard (all labels localized).

    The second row duplicates the "learning_sett" entry from algo settings so the
    params can be tuned without leaving the learning flow; back from there returns
    here (see learning_sett_return in handlers/settings.py)."""
    lang = _user_interface_lang(user_id)
    return [
        [t(lang, "learning_start"), t(lang, "learning_mix_mode"), t(lang, "add_for_learning"), t(lang, "learning_reset")],
        [t(lang, "learning_sett_btn"), t(lang, "back")],
    ]


def _settings_menu_keys(user_id):
    """Settings menu. parse_VF and dump are owner-only (P3.2)."""
    lang = _user_interface_lang(user_id)
    owner = is_owner(user_id)
    # Languages now live in their own submenu (P6.3): "language_sett". parse_VF moved
    # to the add-flow submenu (spec #2): AI / VF add / VF recheck.
    rows = [[t(lang, "algo_settings"), t(lang, "language_sett")],
            [t(lang, "notifications_title")]]
    # Owner reviews reports/feedback (review_menu); a guest submits feedback +
    # can support the project with Telegram Stars (P6.3/P6.4).
    # parse_VF (bug #27): the proxy mini-menu needs a visible owner entry — the
    # old button was dropped when VF parsing moved to the add menu, leaving the
    # proxy settings reachable only by typing "parse_VF".
    if owner:
        rows.append([t(lang, "review_menu"), t(lang, "dump"), PARSE_VF_TEXT])
        rows.append([t(lang, "back")])
    else:
        rows.append([t(lang, "feedback_btn"), t(lang, "support_project"), t(lang, "back")])
    return rows


def _language_menu_keys(user_id):
    """Language submenu (P6.3): interface + dictionary language, then back."""
    lang = _user_interface_lang(user_id)
    return [
        [t(lang, "interface_lang_btn"), t(lang, "dict_lang_btn")],
        [t(lang, "back")],
    ]


def _algo_settings_menu_keys(user_id):
    """Algo settings menu with owner-only unit-test access."""
    lang = _user_interface_lang(user_id)
    study_row = [t(lang, "learning_sett")]
    if is_owner(user_id):
        study_row.append(t(lang, "unit_tests"))
    return [
        [t(lang, "topic_filter"), t(lang, "fsrs_sett")],
        study_row,
        [t(lang, "reset_periodicity"), t(lang, "reset_defaults"), t(lang, "back")],
    ]


def _fsrs_settings_menu_keys(user_id):
    """FSRS settings submenu: the two in-session retry params, then back (bug #0)."""
    lang = _user_interface_lang(user_id)
    return [
        [t(lang, "ses_rtr_gap"), t(lang, "ses_rtr_it")],
        [t(lang, "prateritum_match"), t(lang, "hilfsverb_match"), t(lang, "back")],
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
        rows.append([t(lang, "send_learn"), I_DONT_KNOW_TEXT, t(lang, "input_err")])
    # "clarify_fields" (card with personal example/image + report) is open to all (P6.2).
    rows.append([t(lang, "clarify_fields"), t(lang, "stop")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _quiz_idle_markup(user_id):
    """Keyboard while the quiz is idle (between questions)."""
    lang = _user_interface_lang(user_id)
    rows = [[t(lang, "send_learn"), t(lang, "input_err")]]
    rows.append([t(lang, "clarify_fields"), t(lang, "back")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _db_search_variants_markup(labels, user_id):
    """One semantic-card variant per row, plus a localized search back button."""
    lang = _user_interface_lang(user_id)
    rows = [[label] for label in labels]
    rows.append([t(lang, "back")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _clarify_fields_markup(
    source,
    user_id=None,
    in_search=False,
    is_prep=False,
    is_verb=False,
    show_add_int=True,
    show_variants=False,
):
    """Unified clarify/card keyboard (P6: no separate root menu — one screen).

    A guest edits personal image/example/translation + report. The owner gets
    the full field grid, with personal image/example, and delete. For quizzes/db_search the screen also
    carries the report button, owner delete, and (db_search only) send-to-learning.
    `source == "add"` keeps the plain field grid (AI add flow, no card actions).
    """
    lang = _user_interface_lang(user_id) if user_id is not None else "en"
    if source == "add":
        rows = [
            ["ru", "ukr", "en", "de"],
            ["article", "plural", "type", "infinitive"],
            ["prateritum", "partizip2", "komparativ", "superlativ"],
        ]
        if is_prep:
            # spec #1: preposition-only fields (owner add flow).
            rows.append(["preposition_type", "case_type"])
        rows.append(["image", "example", BACK_TEXT])
        return ReplyKeyboardMarkup(rows, resize_keyboard=True)
    if source == "quiz" and user_id is not None and not is_owner(user_id):
        # spec #7: guest also gets a personal-translation edit button (their dict_lang).
        # add_int (spec #15/T5) pushes the guest's OWN progress — safe to offer.
        card_actions = ["image", "example"]
        if show_add_int:
            card_actions.append("add_int")
        rows = [card_actions, [t(lang, "my_translation"), t(lang, "report_card")]]
        last = [t(lang, "send_learn")] if in_search else []
        if show_variants:
            last.append(t(lang, "db_search_back_to_variants"))
        last.append(BACK_TEXT)
        rows.append(last)
        return ReplyKeyboardMarkup(rows, resize_keyboard=True)
    if source == "quiz":
        # Owner edits fields directly, so no "report / fix" button (#20) — that's
        # for guests to report TO the owner; the owner has nobody to report to.
        card_actions = ["image", "example"]
        if is_verb:
            card_actions.append(t(lang, "clarify_conjugation"))
        if show_add_int:
            card_actions.append("add_int")
        rows = [
            ["ru", "ukr", "de", SYNONYMS_TEXT],
            ["article", "plural", "type", "infinitive"],
            ["prateritum", "partizip2", "komparativ", "superlativ"],
            card_actions,
        ]
        if is_prep:
            # spec #1: preposition-only fields, so the owner can (re)set the category
            # and case on an existing preposition straight from its card.
            rows.append(["preposition_type", "case_type"])
        last = [t(lang, "send_learn")] if in_search else []
        if show_variants:
            last.append(t(lang, "db_search_back_to_variants"))
        last.extend(["delete", BACK_TEXT])
        rows.append(last)
        return ReplyKeyboardMarkup(rows, resize_keyboard=True)
    return ReplyKeyboardMarkup([["russian", BACK_TEXT]], resize_keyboard=True)


def _clarify_conjugation_markup(user_id, allow_praeteritum):
    """Owner-only actions for the conjugation block editor."""
    lang = _user_interface_lang(user_id)
    if allow_praeteritum:
        rows = [
            [
                t(lang, "clarify_conjugation_view"),
                t(lang, "clarify_conjugation_edit_praesens"),
                t(lang, "clarify_conjugation_edit_praeteritum"),
            ],
            [
                t(lang, "clarify_conjugation_clear_praesens"),
                t(lang, "clarify_conjugation_clear_praeteritum"),
                t(lang, "back"),
            ],
        ]
    else:
        rows = [
            [t(lang, "clarify_conjugation_view"), t(lang, "clarify_conjugation_edit_praesens")],
            [t(lang, "clarify_conjugation_clear_praesens"), t(lang, "back")],
        ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _type_choice_markup():
    """Handle type choice markup."""
    types = ["Nomen", "Verb", "Phrase", "Adjektiv", "Pronomen", "Adverb", "Praeposition", "Konjunktion", "Partikel"]
    rows = [types[i:i + 4] for i in range(0, len(types), 4)]
    if rows and len(rows[-1]) < 4:
        rows[-1].append(BACK_TEXT)
    else:
        rows.append([BACK_TEXT])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _preset_value_markup(values):
    """One row of capitalized preset buttons + BACK (spec #1 preposition cascade)."""
    row = [v.capitalize() for v in values]
    return ReplyKeyboardMarkup([row, [BACK_TEXT]], resize_keyboard=True)


def _preposition_type_markup():
    """Preset buttons for a preposition's semantic category (local/temporal/...)."""
    return _preset_value_markup(PREPOSITION_TYPES)


def _case_type_markup():
    """Preset buttons for a preposition's governed case (dativ/akkusativ/...)."""
    return _preset_value_markup(CASE_TYPES)


def _value_entry_markup(field, user_id, allow_stop=True):
    """Keyboard shown when entering a value for `field` during clarify.

    Controlled-vocabulary fields get preset buttons; everything else gets the
    plain back/stop keyboard for free-text entry.
    """
    if field == "type":
        return _type_choice_markup()
    if field == "preposition_type":
        return _preposition_type_markup()
    if field == "case_type":
        return _case_type_markup()
    lang = _user_interface_lang(user_id)
    row = [t(lang, "back")]
    if allow_stop:
        row.append(t(lang, "stop"))
    return ReplyKeyboardMarkup([row], resize_keyboard=True)


def _learning_preview_markup(user_id):
    lang = _user_interface_lang(user_id)
    return ReplyKeyboardMarkup(
        [[t(lang, "learning_next"), t(lang, "learning_skip")],
         [t(lang, "clarify_fields"), t(lang, "back")]], resize_keyboard=True,
    )


def _learning_question_markup(user_id):
    lang = _user_interface_lang(user_id)
    return ReplyKeyboardMarkup(
        [[I_DONT_KNOW_TEXT, t(lang, "input_err")],
         [t(lang, "learning_skip"), t(lang, "clarify_fields"), t(lang, "back")]],
        resize_keyboard=True,
    )


def _add_keys(user_id):
    """Handle add keys."""
    return [[t(_user_interface_lang(user_id), "back")]]


def _add_input_markup(user_id):
    """Handle add input markup."""
    return ReplyKeyboardMarkup([[t(_user_interface_lang(user_id), "back")]], resize_keyboard=True)


def _add_menu_markup(user_id):
    """Add submenu (spec #2): pick the recognition method — AI (current flow),
    VF add, or VF recheck — plus back. Owner-only screen."""
    lang = _user_interface_lang(user_id)
    return ReplyKeyboardMarkup(
        [[t(lang, "add_ai"), t(lang, "add_vf"), t(lang, "vf_recheck")], [t(lang, "back")]],
        resize_keyboard=True,
    )
