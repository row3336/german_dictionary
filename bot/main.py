
import asyncio
import logging
import os
import random
import shutil
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, time, timezone
from datetime import timedelta

from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.error import RetryAfter
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .ai import generate_fields
from .app_config import (
    ADD_CLARIFY,
    ADD_CONFIRM,
    ADD_EDIT,
    ADD_INPUT,
    ADD_PRECHECK,
    ADD_PRECHECK_EDIT,
    AI_CLARIFY_AI,
    AI_BAN_WORD,
    AI_CLARIFY_FIELD,
    AI_CONFIRM,
    AI_KEYS,
    AI_REJECT,
    AI_SKIP,
    ALLOWED_AI_TYPES,
    ALGO_SETTINGS_CONFIRM,
    ALGO_SETTINGS_EDIT,
    ALGO_SETTINGS_MENU,
    ARTICLE_SET,
    AUTO_DUMP_JOB_NAME,
    AUTO_DUMP_KEYS,
    DUMP_MENU,
    AUTO_DUMP_MENU,
    AUTO_DUMP_SETTING_KEY,
    BACK_TEXT,
    FLAG_TEXT,
    I_DONT_KNOW_TEXT,
    MENU,
    NUMERAL_SET,
    NUMERIC_RE,
    QUIZ_ARTICLE,
    QUIZ_ARTICLE_WEAK,
    QUIZ_DE_RU,
    QUIZ_PARTIZIP2_WEAK,
    QUIZ_PLURAL_WEAK,
    QUIZ_PRATERITUM_WEAK,
    QUIZ_RU_DE_MENU,
    QUIZ_RU_DE,
    QUIZ_VERB_WEAK_MENU,
    SETTINGS_MENU,
    SELECT_DICT_LANG,
    SELECT_UI_LANG,
    SYN_ADD_INPUT,
    SYN_AUTO_CONFIRM,
    SYN_CLEAR_CONFIRM,
    SYN_MENU,
    SYN_REMOVE_INPUT,
    PRECHECK_CONFIRM,
    PRECHECK_EDIT,
    PRECHECK_KEYS,
    PRECHECK_REJECT,
    START_TEXT,
    STOP_TEXT,
    TESTS_MENU,
    get_db_path,
    get_dump_dir,
    get_info_path,
    get_log_path,
    get_owner_id,
    SESSION_RETRY_GAP_KEY,
    SESSION_RETRY_IT_KEY,
    get_suggestions_path,
    is_owner,
)
from .storage import (
    QUIZ_ARTICLE as MODE_ARTICLE,
    QUIZ_DE_RU as MODE_DE_RU,
    QUIZ_RU_DE as MODE_RU_DE,
    add_word_flag,
    add_word,
    add_banned_word,
    add_synonym_link,
    get_statistics_snapshot,
    get_form_state,
    get_form_review_stage,
    list_weak_words,
    delete_word,
    find_word_by_value,
    find_existing_word,
    get_word_by_id,
    get_synonym_group,
    get_setting,
    init_db,
    list_synonym_candidates_by_ru,
    select_due,
    remove_synonym_link,
    clear_synonym_group,
    set_setting,
    update_form_stats,
    update_weak_review_state,
    update_progress,
    update_word_fields,
    list_pending_word_flags,
    list_daily_leaderboard,
    list_daily_user_stats,
    get_daily_totals,
    is_banned_word,
    log_quiz_event,
    reset_periodicity,
)
from .i18n import (
    dict_choices,
    interface_choices,
    normalize_dict_lang,
    normalize_interface_lang,
    parse_dict_choice,
    parse_interface_choice,
    t,
)
from .text_utils import (
    md_escape as _md_escape,
    normalize_answer as _normalize_answer,
    normalize_lookup_text as _normalize_lookup_text,
    normalize_russian_singular as _normalize_russian_singular,
    russian_variants as _russian_variants,
    try_parse_json as _try_parse_json,
    type_label as _type_label,
)
from .retry_logic import maybe_take_retry_entry, register_retry_result


DAILY_STATS_JOB_NAME = "daily_stats_report"
OWNER_INTERFACE_LANG_KEY = "owner_interface_lang"
OWNER_DICT_LANG_KEY = "owner_dict_lang"
TEST_ARTICLE_WEAK_TEXT = "Article Weak Mode"
TEST_PLURAL_WEAK_TEXT = "Plural Weak Mode"
TEST_VERB_WEAK_TEXT = "Verb Forms Weak Mode"
TEST_PRATERITUM_WEAK_TEXT = "Prateritum Weak Mode"
TEST_PARTIZIP2_WEAK_TEXT = "Partizip2 Weak Mode"
STANDARD_SM2_TEXT = "Standart mode SM2"
SYNONYMS_TEXT = "synonyms"
SYN_ADD_TEXT = "add"
SYN_REMOVE_TEXT = "remove"
SYN_AUTO_TEXT = "auto by"
SYN_CLEAR_TEXT = "clear group"
SYN_FLAG_TEXT = "flag!"
SYN_CONFIRM_TEXT = "Confirm"
SYN_CANCEL_TEXT = "Cancel"
QUIZ_FIELDS_TEXT = "fields"
QUIZ_RU_DE_TEXT = "Ru > De"
QUIZ_DE_RU_TEXT = "De > Ru"

RETRY_GAP_QUESTIONS = 3
RETRY_MAX_REPEATS = 4


def _main_menu_keys(user_id):
    """Handle main menu keys."""
    lang = _user_interface_lang(user_id)
    return [[t(lang, "add_word"), t(lang, "tests")], [t(lang, "settings"), t(lang, "stat")]]


def _main_menu_markup(user_id):
    """Handle main menu markup."""
    return ReplyKeyboardMarkup(_main_menu_keys(user_id), resize_keyboard=True)


def _user_interface_lang(user_id):
    """Handle user interface lang."""
    lang = get_setting(get_db_path(), OWNER_INTERFACE_LANG_KEY, "en")
    return normalize_interface_lang(lang, default="en")


def _user_dict_lang(user_id):
    """Handle user dict lang."""
    dict_lang = get_setting(get_db_path(), OWNER_DICT_LANG_KEY, "ru")
    return normalize_dict_lang(dict_lang, default="ru")


def _dict_label(user_id):
    """Handle dict label."""
    dict_lang = _user_dict_lang(user_id)
    if dict_lang == "uk":
        return "Ua"
    if dict_lang == "en":
        return "En"
    return "Ru"


def _mode_l1_de(user_id):
    """Handle mode l1 de."""
    dict_lang = _user_dict_lang(user_id)
    if dict_lang == "ru":
        return MODE_RU_DE
    return f"{dict_lang}_de"


def _mode_de_l1(user_id):
    """Handle mode de l1."""
    dict_lang = _user_dict_lang(user_id)
    if dict_lang == "ru":
        return MODE_DE_RU
    return f"de_{dict_lang}"


def _tests_menu_keys(user_id):
    """Handle tests menu keys."""
    return [
        [QUIZ_RU_DE_TEXT, QUIZ_DE_RU_TEXT],
        [t(_user_interface_lang(user_id), "back")],
    ]


def _ru_de_menu_keys(user_id):
    """Handle ru de menu keys."""
    return [
        [STANDARD_SM2_TEXT, TEST_ARTICLE_WEAK_TEXT],
        [TEST_PLURAL_WEAK_TEXT, TEST_VERB_WEAK_TEXT],
        [t(_user_interface_lang(user_id), "back")],
    ]


def _settings_menu_keys(user_id):
    """Handle settings menu keys."""
    lang = _user_interface_lang(user_id)
    return [
        ["flags!", t(lang, "algo_settings")],
        [t(lang, "interface_lang_btn"), t(lang, "dict_lang_btn")],
        [t(lang, "dump"), t(lang, "back")],
    ]


def _algo_settings_menu_keys(user_id):
    """Handle algo settings menu keys."""
    lang = _user_interface_lang(user_id)
    return [
        [t(lang, "ses_rtr_gap"), t(lang, "ses_rtr_it")],
        [t(lang, "reset_periodicity")],
        [t(lang, "back")],
    ]


def _get_int_setting(db_path, key, default):
    """Handle get int setting."""
    raw = get_setting(db_path, key, None)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = int(default)
    return value


def _session_retry_gap(db_path):
    """Handle session retry gap."""
    value = _get_int_setting(db_path, SESSION_RETRY_GAP_KEY, RETRY_GAP_QUESTIONS)
    return max(1, value)


def _session_retry_iterations(db_path):
    """Handle session retry iterations."""
    value = _get_int_setting(db_path, SESSION_RETRY_IT_KEY, RETRY_MAX_REPEATS)
    return max(1, value)


async def _send_algo_settings_menu(update: Update, user_id):
    """Handle send algo settings menu."""
    lang = _user_interface_lang(user_id)
    gap = _session_retry_gap(get_db_path())
    iterations = _session_retry_iterations(get_db_path())
    msg = "\n".join(
        [
            f"{t(lang, 'algo_settings')}:",
            f"{t(lang, 'ses_rtr_gap')} = {gap}",
            f"{t(lang, 'ses_rtr_it')} = {iterations}",
        ]
    )
    await update.message.reply_text(
        msg,
        reply_markup=ReplyKeyboardMarkup(_algo_settings_menu_keys(user_id), resize_keyboard=True),
    )
    return ALGO_SETTINGS_MENU


def _dump_menu_keys(user_id):
    """Handle dump menu keys."""
    lang = _user_interface_lang(user_id)
    return [
        [t(lang, "dump_logs"), t(lang, "dump_db")],
        [t(lang, "auto_dump"), t(lang, "back")],
    ]


def _action_key_from_text(text, user_id):
    """Handle action key from text."""
    lang = _user_interface_lang(user_id)
    keys = [
        ("start", "start"),
        ("back", "back"),
        ("stop", "stop"),
        ("add_word", "add_word"),
        ("tests", "tests"),
        ("settings", "settings"),
        ("info", "info"),
        ("stat", "stat"),
        ("quiz_article", "quiz_article"),
        ("dump_db", "dump_db"),
        ("dump_logs", "dump_logs"),
        ("dump", "dump"),
        ("suggested_words", "suggested_words"),
        ("review_flags", "review_flags"),
        ("algo_settings", "algo_settings"),
        ("auto_dump", "auto_dump"),
        ("daily", "daily"),
        ("weekly", "weekly"),
        ("off", "off"),
        ("interface_lang_btn", "interface_lang"),
        ("dict_lang_btn", "dict_lang"),
    ]
    mapping = {}
    for key, action in keys:
        mapping[t(lang, key)] = action
        mapping[t("en", key)] = action
    mapping[QUIZ_RU_DE_TEXT] = "quiz_l1_de"
    mapping[QUIZ_DE_RU_TEXT] = "quiz_de_l1"
    mapping["flags!"] = "review_flags"
    mapping["Back"] = "back"
    mapping["Stop"] = "stop"
    return mapping.get(text.strip(), "")


def _auto_dump_keys(user_id):
    """Handle auto dump keys."""
    lang = _user_interface_lang(user_id)
    return [[t(lang, "daily"), t(lang, "weekly")], [t(lang, "off"), t(lang, "back")]]


def _add_keys(user_id):
    """Handle add keys."""
    return [[t(_user_interface_lang(user_id), "back")]]


def _stop_keys(user_id):
    """Handle stop keys."""
    return [[t(_user_interface_lang(user_id), "stop")]]


def _clarify_keys(user_id):
    """Handle clarify keys."""
    return AI_KEYS


def _add_input_markup(user_id):
    """Handle add input markup."""
    return ReplyKeyboardMarkup([[t(_user_interface_lang(user_id), "back")]], resize_keyboard=True)


def _split_text_chunks(text, max_size=3500):
    """Handle split text chunks."""
    text = str(text or "").strip()
    if not text:
        return []
    chunks = []
    current = []
    current_len = 0
    for line in text.splitlines():
        add = len(line) + 1
        if current and current_len + add > max_size:
            chunks.append("\n".join(current).strip())
            current = [line]
            current_len = add
        else:
            current.append(line)
            current_len += add
    if current:
        chunks.append("\n".join(current).strip())
    return [c for c in chunks if c]


def _load_info_text(lang="en"):
    """Handle load info text."""
    lang = normalize_interface_lang(lang, default="en")
    path = get_info_path().parent / f"info_{lang}.md"
    if not path.exists():
        path = get_info_path()
    if path.exists():
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return "Info file not found."


def _quiz_markup(user_id, article=False, allow_idk=True):
    """Handle quiz markup."""
    lang = _user_interface_lang(user_id)
    rows = []
    if article:
        rows.append(["der", "die", "das"])
    elif allow_idk:
        rows.append([I_DONT_KNOW_TEXT])
    if is_owner(user_id):
        rows.append([AI_CLARIFY_FIELD, t(lang, "stop")])
    else:
        rows.append([t(lang, "stop")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _parse_level_toggle(text):
    """Handle parse level toggle."""
    token = (text or "").strip().split(" ", 1)[0].upper()
    return token if token in {"A1", "A2", "B1", "B2", "C1", "C2"} else ""


def _edit_fields_markup(source):
    """Handle edit fields markup."""
    if source == "add":
        return ReplyKeyboardMarkup(
            [
                ["ru", "ukr", "de"],
                ["article", "plural", "type"],
                ["prateritum", "partizip2", "infinitive"],
                [BACK_TEXT],
            ],
            resize_keyboard=True,
        )
    if source == "quiz":
        return ReplyKeyboardMarkup(
            [["ru", "ukr", "de"], ["article", "plural", "type"], ["prateritum", "partizip2", "infinitive"], [SYNONYMS_TEXT, BACK_TEXT]],
            resize_keyboard=True,
        )
    return ReplyKeyboardMarkup([["russian", BACK_TEXT]], resize_keyboard=True)


def _allowed_edit_fields(source):
    """Handle allowed edit fields."""
    if source == "add":
        return {"ru", "ukr", "de", "article", "plural", "type", "prateritum", "partizip2", "infinitive"}
    if source == "quiz":
        return {"ru", "ukr", "de", "article", "plural", "prateritum", "partizip2", "infinitive", "type", SYNONYMS_TEXT}
    return {"russian", "level"}


def _quiz_edit_root_markup(user_id):
    """Handle quiz edit root markup."""
    return ReplyKeyboardMarkup([[QUIZ_FIELDS_TEXT], ["delete", t(_user_interface_lang(user_id), "back")]], resize_keyboard=True)


def _dict_lang_tag(user_id):
    """Handle dict lang tag."""
    dict_lang = _user_dict_lang(user_id)
    if dict_lang == "uk":
        return "UA"
    if dict_lang == "en":
        return "EN"
    return "RU"


def _syn_auto_text(user_id):
    """Handle syn auto text."""
    return f"{SYN_AUTO_TEXT} {_dict_lang_tag(user_id)}"


def _set_entry_field(entry, raw_update):
    """Handle set entry field."""
    if "=" not in raw_update:
        return None, "Use format: field=value"
    field, value = raw_update.split("=", 1)
    field = field.strip().lower()
    value = value.strip()

    alias = {
        "ru": "russian",
        "ukr": "ukrainian",
        "en": "english",
        "de": "german",
    }
    field = alias.get(field, field)

    allowed = {
        "russian",
        "ukrainian",
        "english",
        "article",
        "plural",
        "prateritum",
        "partizip2",
        "german",
        "infinitive",
        "type",
    }
    if field not in allowed:
        return None, f"Unknown field: {field}"

    updated = dict(entry)
    if field == "type":
        normalized_type = _type_label(value)
        if normalized_type not in ALLOWED_AI_TYPES:
            return None, "Invalid type."
        updated["type"] = normalized_type
    elif field == "russian":
        updated["russian"] = value
    else:
        updated[field] = value
    return updated, None


def _set_entry_field_value(entry, field, value):
    """Handle set entry field value."""
    return _set_entry_field(entry, f"{field}={value}")


def _get_quiz_action_entry(context: ContextTypes.DEFAULT_TYPE):
    """Handle get quiz action entry."""
    last_answered = context.user_data.get("quiz_last_answered_entry")
    if last_answered:
        return last_answered, "Last answered word:"
    current = context.user_data.get("quiz_entry")
    if current:
        return current, "Current quiz word:"
    return None, ""


async def _start_quiz_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, return_state):
    """Handle start quiz edit."""
    entry, title = _get_quiz_action_entry(context)
    if not entry:
        return await _back_to_tests(update, context)
    context.user_data["edit_source"] = "quiz"
    context.user_data["quiz_edit_entry"] = entry
    context.user_data["edit_stage"] = "pick_scope"
    context.user_data["edit_return_state"] = return_state
    await update.message.reply_text(_format_word_card(entry, title))
    await update.message.reply_text(
        "Choose edit action:",
        reply_markup=_quiz_edit_root_markup(update.effective_user.id),
    )
    return ADD_EDIT


async def _show_quiz_prompt_after_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text, state_override=None):
    """Handle show quiz prompt after edit."""
    context.user_data.pop("quiz_edit_entry", None)
    state = state_override or context.user_data.get("edit_return_state")
    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _back_to_tests(update, context)

    if state == QUIZ_RU_DE:
        text = _format_ru_question(entry, _user_dict_lang(update.effective_user.id))
        if prefix_text:
            text = f"{prefix_text}\n\n{text}"
        await update.message.reply_text(
            text,
            reply_markup=_quiz_markup(update.effective_user.id, article=False),
            parse_mode="Markdown",
        )
        return QUIZ_RU_DE

    if state == QUIZ_DE_RU:
        text = _format_de_ru_question(entry)
        if prefix_text:
            text = f"{prefix_text}\n\n{text}"
        await update.message.reply_text(
            text,
            reply_markup=_quiz_markup(update.effective_user.id, article=False),
            parse_mode="Markdown",
        )
        return QUIZ_DE_RU

    if state == QUIZ_ARTICLE:
        text = _format_article_question(entry)
        if prefix_text:
            text = f"{prefix_text}\n\n{text}"
        await update.message.reply_text(
            text,
            reply_markup=_quiz_markup(update.effective_user.id, article=True),
            parse_mode="Markdown",
        )
        return QUIZ_ARTICLE

    if state == QUIZ_ARTICLE_WEAK:
        return await _send_article_weak_prompt(update, context, prefix_text)
    if state == QUIZ_PLURAL_WEAK:
        return await _send_plural_weak_prompt(update, context, prefix_text)
    if state == QUIZ_PRATERITUM_WEAK:
        return await _send_prateritum_weak_prompt(update, context, prefix_text)
    if state == QUIZ_PARTIZIP2_WEAK:
        return await _send_partizip2_weak_prompt(update, context, prefix_text)

    return await _back_to_tests(update, context)


def _format_word_card(entry, title="Word card:"):
    """Handle format word card."""
    lines = [
        title,
        f"id: {entry.get('id', '')}",
        f"type: {entry.get('type', '')}",
        f"article: {entry.get('article', '') or '-'}",
        f"german: {entry.get('german', '') or '-'}",
        f"infinitive: {entry.get('infinitive', '') or '-'}",
        f"plural: {entry.get('plural', '') or '-'}",
        f"prateritum: {entry.get('prateritum', '') or '-'}",
        f"partizip2: {entry.get('partizip2', '') or '-'}",
        f"russian: {entry.get('russian', '') or '-'}",
        f"ukrainian: {entry.get('ukrainian', '') or '-'}",
        f"english: {entry.get('english', '') or '-'}",
    ]
    return "\n".join(lines)


def _syn_menu_markup(user_id):
    """Handle syn menu markup."""
    return ReplyKeyboardMarkup(
        [
            [SYN_ADD_TEXT, SYN_REMOVE_TEXT, _syn_auto_text(user_id)],
            [SYN_CLEAR_TEXT, SYN_FLAG_TEXT, BACK_TEXT],
        ],
        resize_keyboard=True,
    )


def _syn_confirm_markup():
    """Handle syn confirm markup."""
    return ReplyKeyboardMarkup([[SYN_CONFIRM_TEXT, SYN_CANCEL_TEXT]], resize_keyboard=True)


def _format_syn_group(anchor_entry, group_entries, dict_lang):
    """Handle format syn group."""
    anchor_type = anchor_entry.get("type", "")
    anchor_base = anchor_entry.get("infinitive", "") if anchor_type == "verb" else anchor_entry.get("german", "")
    lines = [f"Synonyms for: {anchor_base} ({anchor_type})"]
    if not group_entries:
        lines.append("- no linked synonyms")
        return "\n".join(lines)
    for idx, item in enumerate(group_entries, start=1):
        item_type = item.get("type", "")
        base = item.get("infinitive", "") if item_type == "verb" else item.get("german", "")
        article = (item.get("article", "") or "").strip()
        left = f"{article} {base}".strip()
        right = _entry_translation(item, dict_lang)
        lines.append(f"{idx}. {left} >> {right}")
    return "\n".join(lines)


async def _open_synonyms_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, anchor_entry, return_state):
    """Handle open synonyms menu."""
    if not anchor_entry or not anchor_entry.get("id"):
        return await _back_to_tests(update, context)
    context.user_data["syn_anchor_id"] = anchor_entry.get("id")
    context.user_data["syn_return_state"] = return_state
    group_entries = get_synonym_group(get_db_path(), anchor_entry.get("id"))
    await update.message.reply_text(
        _format_syn_group(anchor_entry, group_entries, _user_dict_lang(update.effective_user.id)),
        reply_markup=_syn_menu_markup(update.effective_user.id),
    )
    return SYN_MENU

async def _reply_text_with_retry(message, text, max_retries=2, **kwargs):
    """Handle reply text with retry."""
    attempts = 0
    while True:
        try:
            return await message.reply_text(text, **kwargs)
        except RetryAfter as exc:
            attempts += 1
            if attempts > max_retries:
                raise
            await asyncio.sleep(int(getattr(exc, "retry_after", 1)) + 1)


def _scan_words_before_ai(lines):
    """Handle scan words before ai."""
    filtered = []
    existing = []
    recognition = []
    for raw in lines:
        word = raw.strip()
        if not word:
            continue
        reason = _prefilter_reject_reason(word)
        if reason:
            filtered.append(word)
            continue
        if find_existing_word(get_db_path(), word):
            existing.append(word)
            continue
        recognition.append(word)
    return filtered, existing, recognition


def _prefilter_reject_reason(word):
    """Handle prefilter reject reason."""
    normalized = _normalize_lookup_text(word)
    if not normalized:
        return "Empty value."
    if len(normalized) < 2:
        return "Filtered: too short word."
    if NUMERIC_RE.match(normalized):
        return "Filtered: looks like a number."
    if normalized in ARTICLE_SET:
        return "Filtered: article words are not stored."
    if normalized in NUMERAL_SET:
        return "Filtered: numeral words are not stored."
    if is_banned_word(get_db_path(), normalized):
        return "Filtered: banned word."
    return None


def _is_owner_only(user_id):
    """Handle is owner only."""
    return is_owner(user_id)


async def _ask_interface_language(update: Update):
    """Handle ask interface language."""
    await update.message.reply_text(
        "Choose interface language:",
        reply_markup=ReplyKeyboardMarkup([interface_choices()], resize_keyboard=True),
    )
    return SELECT_UI_LANG


async def _ask_dictionary_language(update: Update, user_id):
    """Handle ask dictionary language."""
    lang = _user_interface_lang(user_id)
    choices = dict_choices(lang)
    await update.message.reply_text(
        t(lang, "choose_dict_lang"),
        reply_markup=ReplyKeyboardMarkup([[choices[0]], [choices[1]], [choices[2]]], resize_keyboard=True),
    )
    return SELECT_DICT_LANG


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle start."""
    user = update.effective_user
    user_id = user.id
    if not _is_owner_only(user_id):
        await update.message.reply_text("❌ Owner-only bot.")
        return ConversationHandler.END
    ui_lang = get_setting(get_db_path(), OWNER_INTERFACE_LANG_KEY, "")
    dict_lang = get_setting(get_db_path(), OWNER_DICT_LANG_KEY, "")
    if not ui_lang or not dict_lang:
        return await _ask_interface_language(update)
    await update.message.reply_text(t(_user_interface_lang(user_id), "main_menu"), reply_markup=_main_menu_markup(user_id))
    return MENU


async def menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle menu choice."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)

    if action == "start":
        return await start(update, context)

    if not _is_owner_only(user_id):
        await update.message.reply_text("❌ Owner-only bot.")
        return ConversationHandler.END

    if action == "add_word":
        await update.message.reply_text(
            "Send a word or a list (one per line), or upload a .txt file.",
            reply_markup=_add_input_markup(user_id),
        )
        return ADD_INPUT

    if action == "tests":
        await update.message.reply_text(
            t(_user_interface_lang(user_id), "choose_test"),
            reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(user_id), resize_keyboard=True),
        )
        return TESTS_MENU

    if action == "settings":
        await update.message.reply_text(
            t(_user_interface_lang(user_id), "settings_title"),
            reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
        )
        return SETTINGS_MENU

    if action == "info":
        chunks = _split_text_chunks(_load_info_text(_user_interface_lang(user_id)))
        if not chunks:
            await update.message.reply_text("Info is empty.", reply_markup=_main_menu_markup(user_id))
            return MENU
        for idx, chunk in enumerate(chunks):
            markup = _main_menu_markup(user_id) if idx == len(chunks) - 1 else None
            await update.message.reply_text(chunk, reply_markup=markup)
        return MENU

    if action == "stat":
        stats = get_statistics_snapshot(get_db_path(), user_id=user_id, mode=_mode_l1_de(user_id))
        word_types = stats.get("word_types") or {}
        lines = [
            f"today complete = {stats['today_complete']}",
            f"3+ correct streak = {stats['streak_3p']}",
            f"2 correct streak = {stats['streak_2']}",
            f"1 correct = {stats['streak_1']}",
            "",
            f"weak article = {stats['weak_article']}",
            f"weak plural = {stats['weak_plural']}",
            f"weak partizip2 = {stats['weak_partizip2']}",
            f"weak preterium = {stats['weak_prateritum']}",
            "",
            f"words in dict = {stats['total_words']}",
            f"never touched = {stats['never_answered']}",
            f"words weekly = +{stats['weekly_words']}",
            "",
            "_word types:_",
            *[f"_{k} = {word_types[k]}_" for k in sorted(word_types.keys())],
        ]
        await update.message.reply_text("\n".join(lines), reply_markup=_main_menu_markup(user_id), parse_mode="Markdown")
        return MENU

    await update.message.reply_text(t(_user_interface_lang(user_id), "unknown_menu"))
    return MENU


async def select_ui_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle select ui lang."""
    user_id = update.effective_user.id
    if not _is_owner_only(user_id):
        await update.message.reply_text("❌ Owner-only bot.")
        return ConversationHandler.END

    interface_lang = parse_interface_choice(update.message.text.strip())
    if not interface_lang:
        await update.message.reply_text(
            "Choose interface language:",
            reply_markup=ReplyKeyboardMarkup([interface_choices()], resize_keyboard=True),
        )
        return SELECT_UI_LANG

    set_setting(get_db_path(), OWNER_INTERFACE_LANG_KEY, interface_lang)
    return await _ask_dictionary_language(update, user_id)


async def select_dict_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle select dict lang."""
    user_id = update.effective_user.id
    if not _is_owner_only(user_id):
        await update.message.reply_text("❌ Owner-only bot.")
        return ConversationHandler.END

    lang = _user_interface_lang(user_id)
    dict_lang = parse_dict_choice(update.message.text.strip(), lang)
    if not dict_lang:
        return await _ask_dictionary_language(update, user_id)

    set_setting(get_db_path(), OWNER_DICT_LANG_KEY, dict_lang)
    await update.message.reply_text(
        t(lang, "main_menu"),
        reply_markup=_main_menu_markup(user_id),
    )
    return MENU


async def tests_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle tests menu choice."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)
    if action == "stop":
        return await _back_to_main_menu(update)
    if action == "back":
        return await _back_to_main_menu(update)
    if action == "quiz_l1_de":
        await update.message.reply_text(
            f"{QUIZ_RU_DE_TEXT}:",
            reply_markup=ReplyKeyboardMarkup(_ru_de_menu_keys(user_id), resize_keyboard=True),
        )
        return QUIZ_RU_DE_MENU
    if action == "quiz_de_l1":
        return await _send_de_ru_prompt(update, context, None)

    await update.message.reply_text(
        "Use tests menu buttons.",
        reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(user_id), resize_keyboard=True),
    )
    return TESTS_MENU


async def quiz_ru_de_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz ru de menu choice."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)
    if action == "back":
        await update.message.reply_text(
            t(_user_interface_lang(user_id), "tests") + ":",
            reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(user_id), resize_keyboard=True),
        )
        return TESTS_MENU
    if text == STANDARD_SM2_TEXT:
        _reset_retry_state(context)
        return await _send_ru_de_prompt(update, context, None)
    if text == TEST_ARTICLE_WEAK_TEXT:
        return await _send_article_weak_prompt(update, context, None)
    if text == TEST_PLURAL_WEAK_TEXT:
        return await _send_plural_weak_prompt(update, context, None)
    if text == TEST_VERB_WEAK_TEXT:
        await update.message.reply_text(
            "Choose verb weak mode:",
            reply_markup=ReplyKeyboardMarkup([[TEST_PRATERITUM_WEAK_TEXT, TEST_PARTIZIP2_WEAK_TEXT], [t(_user_interface_lang(user_id), "back")]], resize_keyboard=True),
        )
        return QUIZ_VERB_WEAK_MENU
    await update.message.reply_text(
        "Use Ru > De menu buttons.",
        reply_markup=ReplyKeyboardMarkup(_ru_de_menu_keys(user_id), resize_keyboard=True),
    )
    return QUIZ_RU_DE_MENU


async def quiz_verb_weak_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz verb weak menu choice."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "back":
        await update.message.reply_text(
            f"{QUIZ_RU_DE_TEXT}:",
            reply_markup=ReplyKeyboardMarkup(_ru_de_menu_keys(update.effective_user.id), resize_keyboard=True),
        )
        return QUIZ_RU_DE_MENU
    if text == TEST_PRATERITUM_WEAK_TEXT:
        return await _send_prateritum_weak_prompt(update, context, None)
    if text == TEST_PARTIZIP2_WEAK_TEXT:
        return await _send_partizip2_weak_prompt(update, context, None)
    await update.message.reply_text(
        "Use verb weak mode buttons.",
        reply_markup=ReplyKeyboardMarkup([[TEST_PRATERITUM_WEAK_TEXT, TEST_PARTIZIP2_WEAK_TEXT], [t(_user_interface_lang(update.effective_user.id), "back")]], resize_keyboard=True),
    )
    return QUIZ_VERB_WEAK_MENU


async def settings_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle settings menu choice."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)

    if action == "back":
        return await _back_to_main_menu(update)

    if action == "interface_lang":
        return await _ask_interface_language(update)

    if action == "dict_lang":
        return await _ask_dictionary_language(update, user_id)

    if action == "algo_settings":
        return await _send_algo_settings_menu(update, user_id)

    if action == "dump":
        await update.message.reply_text(
            "dump:",
            reply_markup=ReplyKeyboardMarkup(_dump_menu_keys(user_id), resize_keyboard=True),
        )
        return DUMP_MENU

    if action == "review_flags":
        items = list_pending_word_flags(get_db_path(), limit=500)
        if not items:
            await update.message.reply_text(
                "✅ No flagged words.",
                reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
            )
            return SETTINGS_MENU
        lines = []
        for row in items:
            word_type = row.get("type", "")
            base = row.get("infinitive", "") if word_type == "verb" else row.get("german", "")
            article = (row.get("article", "") or "").strip()
            left = f"{article} {base}".strip()
            right = _entry_translation(row, _user_dict_lang(user_id))
            lines.append(f"{left} >> {right}")
        chunks = _split_text_chunks("\n".join(lines))
        for idx, chunk in enumerate(chunks):
            markup = ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True) if idx == len(chunks) - 1 else None
            await update.message.reply_text(chunk, reply_markup=markup)
        return SETTINGS_MENU

    if action == "suggested_words":
        suggestions_path = _create_suggestions_dump_file()
        if suggestions_path is None:
            await update.message.reply_text("No suggested words file yet.")
            return SETTINGS_MENU
        await update.message.reply_document(
            document=suggestions_path.open("rb"),
            filename=suggestions_path.name,
            caption="✅ Suggested words dump created.",
        )
        return SETTINGS_MENU

    await update.message.reply_text(
        "Use settings buttons.",
        reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
    )
    return SETTINGS_MENU


async def algo_settings_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle algo settings menu choice."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    lang = _user_interface_lang(user_id)
    action = _action_key_from_text(text, user_id)

    if action == "back":
        await update.message.reply_text(
            t(lang, "settings_title"),
            reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
        )
        return SETTINGS_MENU

    if text == t(lang, "ses_rtr_gap"):
        context.user_data["algo_setting_edit"] = SESSION_RETRY_GAP_KEY
        await update.message.reply_text(
            "Enter ses.rtr_gap (number of SM2 questions between retries):",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ALGO_SETTINGS_EDIT

    if text == t(lang, "ses_rtr_it"):
        context.user_data["algo_setting_edit"] = SESSION_RETRY_IT_KEY
        await update.message.reply_text(
            "Enter ses.rtr_it (retry repetitions per word):",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ALGO_SETTINGS_EDIT

    if text == t(lang, "reset_periodicity"):
        context.user_data["algo_setting_edit"] = "reset_periodicity"
        await update.message.reply_text(
            'Type "yes" to reset periodicity:',
            reply_markup=ReplyKeyboardRemove(),
        )
        return ALGO_SETTINGS_CONFIRM

    await update.message.reply_text(
        "Use algo settings buttons.",
        reply_markup=ReplyKeyboardMarkup(_algo_settings_menu_keys(user_id), resize_keyboard=True),
    )
    return ALGO_SETTINGS_MENU


async def algo_settings_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle algo settings edit."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    if _action_key_from_text(text, user_id) == "back":
        return await _send_algo_settings_menu(update, user_id)

    key = context.user_data.get("algo_setting_edit")
    if key not in {SESSION_RETRY_GAP_KEY, SESSION_RETRY_IT_KEY}:
        return await _send_algo_settings_menu(update, user_id)

    try:
        value = int(text)
    except ValueError:
        await update.message.reply_text("Enter a number.")
        return ALGO_SETTINGS_EDIT

    if value < 1:
        await update.message.reply_text("Value must be >= 1.")
        return ALGO_SETTINGS_EDIT

    set_setting(get_db_path(), key, str(value))
    return await _send_algo_settings_menu(update, user_id)


async def algo_settings_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle algo settings confirm."""
    text = update.message.text.strip().lower()
    user_id = update.effective_user.id
    if text == "yes":
        reset_periodicity(get_db_path())
        await update.message.reply_text("✅ Periodicity reset.")
        return await _send_algo_settings_menu(update, user_id)

    await update.message.reply_text("Canceled.")
    return await _send_algo_settings_menu(update, user_id)


async def dump_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle dump menu choice."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)

    if action == "back":
        await update.message.reply_text(
            t(_user_interface_lang(user_id), "settings_title"),
            reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
        )
        return SETTINGS_MENU

    if action == "dump_db":
        dump_path = _create_dump_file()
        await update.message.reply_document(
            document=dump_path.open("rb"),
            filename=dump_path.name,
            caption="✅ SQLite dump created.",
        )
        return DUMP_MENU

    if action == "dump_logs":
        logs_path = _create_logs_dump_file()
        if logs_path is None:
            await update.message.reply_text(
                "Log file is empty or not found yet.",
                reply_markup=ReplyKeyboardMarkup(_dump_menu_keys(user_id), resize_keyboard=True),
            )
            return DUMP_MENU
        await update.message.reply_document(
            document=logs_path.open("rb"),
            filename=logs_path.name,
            caption="✅ Logs dump created.",
        )
        return DUMP_MENU

    if action == "auto_dump":
        mode = get_setting(get_db_path(), AUTO_DUMP_SETTING_KEY, "off")
        await update.message.reply_text(
            f"Current auto dump mode: {mode}",
            reply_markup=ReplyKeyboardMarkup(_auto_dump_keys(user_id), resize_keyboard=True),
        )
        return AUTO_DUMP_MENU

    await update.message.reply_text(
        "Use dump menu buttons.",
        reply_markup=ReplyKeyboardMarkup(_dump_menu_keys(user_id), resize_keyboard=True),
    )
    return DUMP_MENU


async def auto_dump_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle auto dump choice."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    if not is_owner(user_id):
        return await _back_to_main_menu(update)

    action = _action_key_from_text(text, user_id)
    if action == "back":
        await update.message.reply_text(
            "dump:",
            reply_markup=ReplyKeyboardMarkup(_dump_menu_keys(user_id), resize_keyboard=True),
        )
        return DUMP_MENU

    mode_map = {"daily": "daily", "weekly": "weekly", "off": "off"}
    if action not in mode_map:
        await update.message.reply_text(
            "Use auto dump buttons.",
            reply_markup=ReplyKeyboardMarkup(_auto_dump_keys(user_id), resize_keyboard=True),
        )
        return AUTO_DUMP_MENU

    mode = mode_map[action]
    set_setting(get_db_path(), AUTO_DUMP_SETTING_KEY, mode)
    _configure_auto_dump(context.application, mode)
    await update.message.reply_text(
        f"✅ Auto dump mode set: {mode}",
        reply_markup=ReplyKeyboardMarkup(_dump_menu_keys(user_id), resize_keyboard=True),
    )
    return DUMP_MENU

async def add_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle add input."""
    raw = update.message.text.strip()
    user_id = update.effective_user.id
    if _action_key_from_text(raw, user_id) == "back":
        return await _back_to_main_menu(update)

    if raw.startswith("{"):
        entry = _try_parse_json(raw)
        if entry is None:
            await update.message.reply_text(
                "Invalid JSON. Make sure it starts with '{' and ends with '}'. Try again.",
                reply_markup=ReplyKeyboardMarkup(_add_keys(user_id), resize_keyboard=True),
            )
            return ADD_INPUT
        context.user_data["pending_entry"] = entry
        return await _show_ai_preview(update, entry)

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        await update.message.reply_text("Empty input. Send a word or list.")
        return ADD_INPUT

    filtered, existing, recognition = _scan_words_before_ai(lines)
    context.user_data["precheck_filtered"] = filtered
    context.user_data["precheck_existing"] = existing
    if not recognition:
        await _reply_text_with_retry(
            update.message,
            (
                f"Already in database {len(existing)}\n"
                f"Filtred {len(filtered)}\n"
                "Nothing to recognize."
            ),
            reply_markup=ReplyKeyboardMarkup(_add_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_INPUT

    items = await _precheck_words(update, context, recognition)
    if items is None:
        return MENU
    context.user_data["precheck_items"] = items
    await _reply_text_with_retry(
        update.message,
        _format_precheck_list(items),
        reply_markup=ReplyKeyboardMarkup(PRECHECK_KEYS, resize_keyboard=True),
    )
    return ADD_PRECHECK


async def add_precheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle add precheck."""
    text = update.message.text.strip()
    if text == STOP_TEXT or _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _stop_queue(update, context)
    if text == PRECHECK_REJECT:
        context.user_data.pop("precheck_items", None)
        context.user_data.pop("precheck_filtered", None)
        context.user_data.pop("precheck_existing", None)
        await _reply_text_with_retry(
            update.message,
            "Precheck rejected. Send a word or list.",
            reply_markup=ReplyKeyboardMarkup(_add_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_INPUT
    if text == PRECHECK_EDIT:
        await _reply_text_with_retry(
            update.message,
            "Send corrected list (one per line).",
            reply_markup=ReplyKeyboardMarkup(_stop_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_PRECHECK_EDIT
    if text != PRECHECK_CONFIRM:
        await _reply_text_with_retry(
            update.message,
            "Use Approve or Reject list.",
            reply_markup=ReplyKeyboardMarkup(PRECHECK_KEYS, resize_keyboard=True),
        )
        return ADD_PRECHECK

    items = context.user_data.get("precheck_items") or []
    recognition = [x.get("word") for x in items if x.get("word")]
    context.user_data.pop("precheck_items", None)
    filtered = context.user_data.pop("precheck_filtered", [])
    existing = context.user_data.pop("precheck_existing", [])
    context.user_data["add_queue"] = recognition
    await _reply_text_with_retry(
        update.message,
        (
            f"Already in database {len(existing)}\n"
            f"Filtred {len(filtered)}\n"
            f"For recognition {len(recognition)}\n"
            "Logs: Settings -> Dump Logs"
        ),
        reply_markup=ReplyKeyboardMarkup(_stop_keys(update.effective_user.id), resize_keyboard=True),
    )
    return await _process_next_ai_word(update, context)


def _parse_precheck_edit_lines(lines):
    """Handle parse precheck edit lines."""
    result = []
    for line in lines:
        raw = str(line or "").strip()
        if not raw:
            continue
        if "[" in raw and "]" in raw:
            raw = raw.split("[", 1)[0].strip()
        if " - " in raw:
            raw = raw.split(" - ", 1)[0].strip()
        result.append(raw)
    return result


async def add_precheck_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle add precheck edit."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _stop_queue(update, context)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        await _reply_text_with_retry(
            update.message,
            "Empty list. Send corrected list (one per line).",
            reply_markup=ReplyKeyboardMarkup(_stop_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_PRECHECK_EDIT
    recognition = _parse_precheck_edit_lines(lines)
    if not recognition:
        await _reply_text_with_retry(
            update.message,
            "No valid words in list.",
            reply_markup=ReplyKeyboardMarkup(_stop_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_PRECHECK_EDIT
    context.user_data.pop("precheck_items", None)
    filtered = context.user_data.pop("precheck_filtered", [])
    existing = context.user_data.pop("precheck_existing", [])
    context.user_data["add_queue"] = recognition
    await _reply_text_with_retry(
        update.message,
        (
            f"Already in database {len(existing)}\n"
            f"Filtred {len(filtered)}\n"
            f"For recognition {len(recognition)}\n"
            "Logs: Settings -> Dump Logs"
        ),
        reply_markup=ReplyKeyboardMarkup(_stop_keys(update.effective_user.id), resize_keyboard=True),
    )
    return await _process_next_ai_word(update, context)


async def add_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle add document."""
    doc = update.message.document
    if not doc or not doc.file_name.lower().endswith(".txt"):
        await update.message.reply_text("Please upload a .txt file.")
        return ADD_INPUT

    file = await doc.get_file()
    data = await file.download_as_bytearray()
    text = data.decode("utf-8", errors="ignore")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        await update.message.reply_text("File is empty.")
        return ADD_INPUT

    filtered, existing, recognition = _scan_words_before_ai(lines)
    context.user_data["precheck_filtered"] = filtered
    context.user_data["precheck_existing"] = existing
    if not recognition:
        await _reply_text_with_retry(
            update.message,
            (
                f"Already in database {len(existing)}\n"
                f"Filtred {len(filtered)}\n"
                "Nothing to recognize."
            ),
            reply_markup=ReplyKeyboardMarkup(_add_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_INPUT

    items = await _precheck_words(update, context, recognition)
    if items is None:
        return MENU
    context.user_data["precheck_items"] = items
    await _reply_text_with_retry(
        update.message,
        _format_precheck_list(items),
        reply_markup=ReplyKeyboardMarkup(PRECHECK_KEYS, resize_keyboard=True),
    )
    return ADD_PRECHECK


async def add_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle add confirm."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _stop_queue(update, context)

    if text == AI_BAN_WORD:
        return await _ban_pending_word(update, context)

    if text == AI_CLARIFY_AI:
        await update.message.reply_text(
            "Send clarification text for AI.",
            reply_markup=ReplyKeyboardMarkup(_clarify_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_CLARIFY

    if text == AI_CLARIFY_FIELD:
        context.user_data["edit_source"] = "add"
        context.user_data["edit_stage"] = "pick_field"
        entry = context.user_data.get("pending_entry")
        if entry:
            await update.message.reply_text(_format_word_card(entry, "AI word card:"))
        await update.message.reply_text(
            "Choose field to edit:",
            reply_markup=_edit_fields_markup("add"),
        )
        return ADD_EDIT

    if text == AI_REJECT:
        return await _process_next_ai_word(update, context)

    if text != AI_CONFIRM:
        await update.message.reply_text(
            "Use: Confirm, Clarify AI, Clarify field, Reject, Ban, or Stop.",
            reply_markup=ReplyKeyboardMarkup(AI_KEYS, resize_keyboard=True),
        )
        return ADD_CONFIRM

    entry = context.user_data.pop("pending_entry", None)
    if not entry:
        await update.message.reply_text("Nothing to save.", reply_markup=_main_menu_markup(update.effective_user.id))
        return MENU

    saved = await _save_entry(update, entry)
    if saved is None:
        return await _process_next_ai_word(update, context)

    return await _process_next_ai_word(update, context)


async def add_clarify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle add clarify."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _stop_queue(update, context)
    if text == AI_SKIP:
        return await _process_next_ai_word(update, context)
    if text == AI_BAN_WORD:
        return await _ban_pending_word(update, context)
    if text == AI_CLARIFY_FIELD:
        context.user_data["edit_source"] = "add"
        context.user_data["edit_stage"] = "pick_field"
        entry = context.user_data.get("pending_entry")
        if entry:
            await update.message.reply_text(_format_word_card(entry, "AI word card:"))
        await update.message.reply_text(
            "Choose field to edit:",
            reply_markup=_edit_fields_markup("add"),
        )
        return ADD_EDIT
    if text == AI_CLARIFY_AI:
        await update.message.reply_text(
            "Send clarification text for AI.",
            reply_markup=ReplyKeyboardMarkup(_clarify_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_CLARIFY

    word = context.user_data.get("pending_word")
    if not word:
        await update.message.reply_text("No word to clarify.", reply_markup=_main_menu_markup(update.effective_user.id))
        return MENU

    return await _process_ai_word(update, context, word, text)


async def add_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle add edit."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _stop_queue(update, context)

    source = context.user_data.get("edit_source", "add")
    if source == "add":
        entry_key = "pending_entry"
    else:
        entry_key = "quiz_edit_entry"
    entry = context.user_data.get(entry_key)
    if not entry:
        await update.message.reply_text("No entry to edit.", reply_markup=_main_menu_markup(update.effective_user.id))
        return MENU

    stage = context.user_data.get("edit_stage", "pick_scope" if source == "quiz" else "pick_field")
    if _action_key_from_text(text, update.effective_user.id) == "back":
        if source == "quiz":
            if stage == "pick_scope":
                return await _show_quiz_prompt_after_edit(update, context, "❌ Edit canceled.")
            if stage == "pick_field":
                context.user_data["edit_stage"] = "pick_scope"
                await update.message.reply_text(
                    "Choose edit action:",
                    reply_markup=_quiz_edit_root_markup(update.effective_user.id),
                )
                return ADD_EDIT
            context.user_data["edit_stage"] = "pick_field"
            await update.message.reply_text(
                "Choose field to edit:",
                reply_markup=_edit_fields_markup(source),
            )
            return ADD_EDIT

        if stage == "pick_field":
            return await _show_ai_preview(update, entry)
        context.user_data["edit_stage"] = "pick_field"
        await update.message.reply_text(
            "Choose field to edit:",
            reply_markup=_edit_fields_markup(source),
        )
        return ADD_EDIT

    if source == "quiz" and stage == "pick_scope":
        token = text.strip().lower()
        if token == QUIZ_FIELDS_TEXT:
            context.user_data["edit_stage"] = "pick_field"
            await update.message.reply_text(
                "Choose field to edit:",
                reply_markup=_edit_fields_markup("quiz"),
            )
            return ADD_EDIT
        if token == "delete":
            word_id = entry.get("id")
            if not word_id:
                await update.message.reply_text(
                    "❌ Cannot delete: missing word id.",
                    reply_markup=_quiz_edit_root_markup(update.effective_user.id),
                )
                return ADD_EDIT
            removed = delete_word(get_db_path(), word_id)
            context.user_data.pop("quiz_edit_entry", None)
            current_quiz_entry = context.user_data.get("quiz_entry")
            if current_quiz_entry and current_quiz_entry.get("id") == word_id:
                context.user_data.pop("quiz_entry", None)
            last_answered = context.user_data.get("quiz_last_answered_entry")
            if last_answered and last_answered.get("id") == word_id:
                context.user_data.pop("quiz_last_answered_entry", None)
            context.user_data["edit_stage"] = "pick_scope"
            context.user_data.pop("edit_field", None)
            state = context.user_data.get("edit_return_state")
            prefix = "✅ Deleted." if removed else "❌ Word not found."
            if state == QUIZ_RU_DE:
                return await _send_ru_de_prompt(update, context, prefix)
            if state == QUIZ_DE_RU:
                return await _send_de_ru_prompt(update, context, prefix)
            if state == QUIZ_ARTICLE:
                return await _send_article_prompt(update, context, prefix)
            if state == QUIZ_ARTICLE_WEAK:
                return await _send_article_weak_prompt(update, context, prefix)
            if state == QUIZ_PLURAL_WEAK:
                return await _send_plural_weak_prompt(update, context, prefix)
            if state == QUIZ_PRATERITUM_WEAK:
                return await _send_prateritum_weak_prompt(update, context, prefix)
            if state == QUIZ_PARTIZIP2_WEAK:
                return await _send_partizip2_weak_prompt(update, context, prefix)
            return await _back_to_tests(update, context)
        await update.message.reply_text(
            "Use buttons: fields, delete, back.",
            reply_markup=_quiz_edit_root_markup(update.effective_user.id),
        )
        return ADD_EDIT

    if stage == "pick_field":
        field = text.strip().lower()
        allowed_fields = _allowed_edit_fields(source)
        if field not in allowed_fields:
            await update.message.reply_text(
                "Use field buttons.",
                reply_markup=_edit_fields_markup(source),
            )
            return ADD_EDIT
        if source == "quiz" and field == SYNONYMS_TEXT:
            state = context.user_data.get("edit_return_state")
            return await _open_synonyms_menu(update, context, entry, state)
        context.user_data["edit_field"] = field

        context.user_data["edit_stage"] = "enter_value"
        await update.message.reply_text(
            f"Enter new value for {field}:",
            reply_markup=ReplyKeyboardMarkup([[t(_user_interface_lang(update.effective_user.id), "back"), t(_user_interface_lang(update.effective_user.id), "stop")]], resize_keyboard=True),
        )
        return ADD_EDIT

    field = context.user_data.get("edit_field", "")
    if not field:
        context.user_data["edit_stage"] = "pick_field"
        await update.message.reply_text(
            "Choose field to edit:",
            reply_markup=_edit_fields_markup(source),
        )
        return ADD_EDIT

    updated, error = _set_entry_field_value(entry, field, text)
    if error:
        await update.message.reply_text(
            error,
            reply_markup=ReplyKeyboardMarkup([[t(_user_interface_lang(update.effective_user.id), "back"), t(_user_interface_lang(update.effective_user.id), "stop")]], resize_keyboard=True),
        )
        return ADD_EDIT

    context.user_data[entry_key] = updated
    context.user_data["edit_stage"] = "pick_scope" if source == "quiz" else "pick_field"
    context.user_data.pop("edit_field", None)
    if source == "quiz":
        update_word_fields(get_db_path(), updated.get("id"), updated)
        current_quiz_entry = context.user_data.get("quiz_entry")
        if current_quiz_entry and current_quiz_entry.get("id") == updated.get("id"):
            context.user_data["quiz_entry"] = updated
        last_answered = context.user_data.get("quiz_last_answered_entry")
        if last_answered and last_answered.get("id") == updated.get("id"):
            context.user_data["quiz_last_answered_entry"] = updated
        return await _show_quiz_prompt_after_edit(update, context, "✅ Updated.")
    return await _show_ai_preview(update, updated)

async def _return_from_synonyms(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix="✅ Done."):
    """Handle return from synonyms."""
    anchor_id = context.user_data.get("syn_anchor_id")
    return_state = context.user_data.get("syn_return_state")
    context.user_data.pop("syn_auto_candidates", None)
    context.user_data.pop("syn_anchor_id", None)
    context.user_data.pop("syn_return_state", None)
    entry = get_word_by_id(get_db_path(), anchor_id) if anchor_id else None
    if entry:
        context.user_data["quiz_entry"] = entry
    if return_state == QUIZ_RU_DE:
        return await _send_ru_de_prompt(update, context, prefix)
    if return_state == QUIZ_DE_RU:
        return await _send_de_ru_prompt(update, context, prefix)
    if return_state == QUIZ_ARTICLE:
        return await _send_article_prompt(update, context, prefix)
    if return_state == QUIZ_ARTICLE_WEAK:
        return await _send_article_weak_prompt(update, context, prefix)
    if return_state == QUIZ_PLURAL_WEAK:
        return await _send_plural_weak_prompt(update, context, prefix)
    if return_state == QUIZ_PRATERITUM_WEAK:
        return await _send_prateritum_weak_prompt(update, context, prefix)
    if return_state == QUIZ_PARTIZIP2_WEAK:
        return await _send_partizip2_weak_prompt(update, context, prefix)
    return await _back_to_tests(update, context)


async def synonyms_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle synonyms menu."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _back_to_tests(update, context)
    if _action_key_from_text(text, update.effective_user.id) == "back":
        return await _return_from_synonyms(update, context, None)
    anchor_id = context.user_data.get("syn_anchor_id")
    anchor_entry = get_word_by_id(get_db_path(), anchor_id) if anchor_id else None
    if not anchor_entry:
        return await _back_to_tests(update, context)
    if text == SYN_ADD_TEXT:
        await update.message.reply_text("Send German word or id to add:", reply_markup=ReplyKeyboardRemove())
        return SYN_ADD_INPUT
    if text == SYN_REMOVE_TEXT:
        group = get_synonym_group(get_db_path(), anchor_id)
        await update.message.reply_text(
            _format_syn_group(anchor_entry, group, _user_dict_lang(update.effective_user.id)) + "\n\nSend number, word, or id to remove.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return SYN_REMOVE_INPUT
    if text == _syn_auto_text(update.effective_user.id):
        candidates = list_synonym_candidates_by_ru(get_db_path(), anchor_id)
        if not candidates:
            await update.message.reply_text(f"No {_syn_auto_text(update.effective_user.id)} candidates.", reply_markup=_syn_menu_markup(update.effective_user.id))
            return SYN_MENU
        context.user_data["syn_auto_candidates"] = [x.get("id") for x in candidates]
        preview = _format_syn_group(anchor_entry, candidates, _user_dict_lang(update.effective_user.id))
        await update.message.reply_text(
            f"Will add {len(candidates)} candidate(s):\n\n{preview}",
            reply_markup=_syn_confirm_markup(),
        )
        return SYN_AUTO_CONFIRM
    if text == SYN_CLEAR_TEXT:
        await update.message.reply_text("Clear whole synonym group?", reply_markup=_syn_confirm_markup())
        return SYN_CLEAR_CONFIRM
    if text == SYN_FLAG_TEXT:
        add_word_flag(get_db_path(), word_id=anchor_id, user_id=update.effective_user.id, comment="synonym_review")
        await update.message.reply_text("✅ Flag saved.", reply_markup=_syn_menu_markup(update.effective_user.id))
        return SYN_MENU
    await update.message.reply_text("Use synonyms buttons.", reply_markup=_syn_menu_markup(update.effective_user.id))
    return SYN_MENU


async def synonyms_add_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle synonyms add input."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "back":
        anchor_entry = get_word_by_id(get_db_path(), context.user_data.get("syn_anchor_id"))
        group = get_synonym_group(get_db_path(), context.user_data.get("syn_anchor_id"))
        await update.message.reply_text(_format_syn_group(anchor_entry or {}, group, _user_dict_lang(update.effective_user.id)), reply_markup=_syn_menu_markup(update.effective_user.id))
        return SYN_MENU
    anchor_id = context.user_data.get("syn_anchor_id")
    anchor_entry = get_word_by_id(get_db_path(), anchor_id)
    target = find_word_by_value(get_db_path(), text)
    if not anchor_entry or not target:
        await update.message.reply_text("Target not found. Send German word or id.")
        return SYN_ADD_INPUT
    if (anchor_entry.get("type", "") or "").lower() != (target.get("type", "") or "").lower():
        await update.message.reply_text("Type mismatch. Only same type is allowed.")
        return SYN_ADD_INPUT
    changed, _ = add_synonym_link(get_db_path(), anchor_id, target.get("id"))
    msg = "✅ Linked." if changed else "✅ Already linked."
    group = get_synonym_group(get_db_path(), anchor_id)
    await update.message.reply_text(f"{msg}\n\n{_format_syn_group(anchor_entry, group, _user_dict_lang(update.effective_user.id))}", reply_markup=_syn_menu_markup(update.effective_user.id))
    return SYN_MENU


async def synonyms_remove_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle synonyms remove input."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "back":
        return SYN_MENU
    anchor_id = context.user_data.get("syn_anchor_id")
    anchor = get_word_by_id(get_db_path(), anchor_id)
    group = get_synonym_group(get_db_path(), anchor_id)
    if not anchor or not group:
        return await _return_from_synonyms(update, context, None)
    target = None
    if text.isdigit():
        idx = int(text)
        if 1 <= idx <= len(group):
            target = group[idx - 1]
    if target is None:
        target = find_word_by_value(get_db_path(), text)
    if not target:
        await update.message.reply_text("Target not found in group.")
        return SYN_REMOVE_INPUT
    if target.get("id") == anchor_id:
        await update.message.reply_text("Cannot remove anchor word from itself. Use Clear group.")
        return SYN_REMOVE_INPUT
    removed = remove_synonym_link(get_db_path(), target.get("id"))
    msg = "✅ Removed." if removed else "❌ Not linked."
    group_new = get_synonym_group(get_db_path(), anchor_id)
    await update.message.reply_text(f"{msg}\n\n{_format_syn_group(anchor, group_new, _user_dict_lang(update.effective_user.id))}", reply_markup=_syn_menu_markup(update.effective_user.id))
    return SYN_MENU


async def synonyms_auto_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle synonyms auto confirm."""
    text = update.message.text.strip()
    if text == SYN_CANCEL_TEXT or _action_key_from_text(text, update.effective_user.id) == "back":
        anchor_id = context.user_data.get("syn_anchor_id")
        anchor = get_word_by_id(get_db_path(), anchor_id)
        group = get_synonym_group(get_db_path(), anchor_id)
        await update.message.reply_text(_format_syn_group(anchor or {}, group, _user_dict_lang(update.effective_user.id)), reply_markup=_syn_menu_markup(update.effective_user.id))
        return SYN_MENU
    if text != SYN_CONFIRM_TEXT:
        await update.message.reply_text("Use Confirm or Cancel.", reply_markup=_syn_confirm_markup())
        return SYN_AUTO_CONFIRM
    anchor_id = context.user_data.get("syn_anchor_id")
    ids = context.user_data.get("syn_auto_candidates") or []
    added = 0
    for wid in ids:
        changed, _ = add_synonym_link(get_db_path(), anchor_id, wid)
        if changed:
            added += 1
    anchor = get_word_by_id(get_db_path(), anchor_id)
    group = get_synonym_group(get_db_path(), anchor_id)
    await update.message.reply_text(f"✅ Auto linked: {added}", reply_markup=_syn_menu_markup(update.effective_user.id))
    await update.message.reply_text(_format_syn_group(anchor or {}, group, _user_dict_lang(update.effective_user.id)), reply_markup=_syn_menu_markup(update.effective_user.id))
    return SYN_MENU


async def synonyms_clear_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle synonyms clear confirm."""
    text = update.message.text.strip()
    if text == SYN_CANCEL_TEXT or _action_key_from_text(text, update.effective_user.id) == "back":
        return SYN_MENU
    if text != SYN_CONFIRM_TEXT:
        await update.message.reply_text("Use Confirm or Cancel.", reply_markup=_syn_confirm_markup())
        return SYN_CLEAR_CONFIRM
    anchor_id = context.user_data.get("syn_anchor_id")
    cleared = clear_synonym_group(get_db_path(), anchor_id)
    await update.message.reply_text("✅ Group cleared." if cleared else "✅ Group already empty.", reply_markup=_syn_menu_markup(update.effective_user.id))
    return SYN_MENU


async def _flag_current_word(update: Update, context: ContextTypes.DEFAULT_TYPE, return_state):
    """Handle flag current word."""
    entry, _ = _get_quiz_action_entry(context)
    if not entry:
        return await _back_to_tests(update, context)
    word_id = entry.get("id")
    if not word_id:
        return await _back_to_tests(update, context)
    add_word_flag(get_db_path(), word_id=word_id, user_id=update.effective_user.id, comment="")
    if return_state == QUIZ_RU_DE:
        return await _show_quiz_prompt_after_edit(update, context, "✅ Flag saved.", state_override=QUIZ_RU_DE)
    if return_state == QUIZ_DE_RU:
        return await _show_quiz_prompt_after_edit(update, context, "✅ Flag saved.", state_override=QUIZ_DE_RU)
    if return_state == QUIZ_ARTICLE:
        return await _show_quiz_prompt_after_edit(update, context, "✅ Flag saved.", state_override=QUIZ_ARTICLE)
    if return_state == QUIZ_ARTICLE_WEAK:
        return await _show_quiz_prompt_after_edit(update, context, "✅ Flag saved.", state_override=QUIZ_ARTICLE_WEAK)
    if return_state == QUIZ_PLURAL_WEAK:
        return await _show_quiz_prompt_after_edit(update, context, "✅ Flag saved.", state_override=QUIZ_PLURAL_WEAK)
    if return_state == QUIZ_PRATERITUM_WEAK:
        return await _show_quiz_prompt_after_edit(update, context, "✅ Flag saved.", state_override=QUIZ_PRATERITUM_WEAK)
    if return_state == QUIZ_PARTIZIP2_WEAK:
        return await _show_quiz_prompt_after_edit(update, context, "✅ Flag saved.", state_override=QUIZ_PARTIZIP2_WEAK)
    return await _back_to_tests(update, context)


async def quiz_ru_de(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz ru de."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _back_to_tests(update, context)
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_RU_DE)
    if text == AI_CLARIFY_FIELD and is_owner(update.effective_user.id):
        return await _start_quiz_edit(update, context, QUIZ_RU_DE)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_ru_de_prompt(update, context, None)
    is_retry = bool(context.user_data.get("quiz_is_retry"))
    if text == I_DONT_KNOW_TEXT:
        ok = False
        form_checks = {}
    else:
        ok, form_checks = _check_ru_de_strict(entry, text)
    mode = _mode_l1_de(update.effective_user.id)
    if not is_retry:
        _update_srs_for_entry(update.effective_user.id, entry, mode, ok)
        if form_checks:
            update_form_stats(get_db_path(), update.effective_user.id, entry.get("id"), form_checks)
    context.user_data["quiz_last_answered_entry"] = entry
    _register_retry_result(context, mode, entry.get("id"), ok, is_retry)
    if not is_retry:
        _set_retry_since_last(context, mode, _retry_since_last(context, mode) + 1)

    feedback = _format_feedback_ru_de(entry, ok)
    return await _send_ru_de_prompt(update, context, feedback)


async def quiz_de_ru(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz de ru."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _back_to_tests(update, context)
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_DE_RU)
    if text == AI_CLARIFY_FIELD and is_owner(update.effective_user.id):
        return await _start_quiz_edit(update, context, QUIZ_DE_RU)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_de_ru_prompt(update, context, None)
    is_retry = bool(context.user_data.get("quiz_is_retry"))

    if text == I_DONT_KNOW_TEXT:
        ok = False
    else:
        answer = _normalize_answer(text)
        variants = _russian_variants(_entry_translation(entry, _user_dict_lang(update.effective_user.id)))
        ok = answer in variants
    mode = _mode_de_l1(update.effective_user.id)
    if not is_retry:
        _update_srs_for_entry(update.effective_user.id, entry, mode, ok)
    context.user_data["quiz_last_answered_entry"] = entry
    _register_retry_result(context, mode, entry.get("id"), ok, is_retry)
    if not is_retry:
        _set_retry_since_last(context, mode, _retry_since_last(context, mode) + 1)

    feedback = _format_feedback_de_ru(entry, ok, _user_dict_lang(update.effective_user.id))
    return await _send_de_ru_prompt(update, context, feedback)


async def quiz_article(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz article."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _back_to_tests(update, context)
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_ARTICLE)
    if text == AI_CLARIFY_FIELD and is_owner(update.effective_user.id):
        return await _start_quiz_edit(update, context, QUIZ_ARTICLE)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_article_prompt(update, context, None)
    is_retry = bool(context.user_data.get("quiz_is_retry"))

    if text == I_DONT_KNOW_TEXT:
        ok = False
    else:
        expected = entry.get("article", "")
        ok = _normalize_answer(text) == _normalize_answer(expected)
    mode = MODE_ARTICLE
    if not is_retry:
        _update_srs_for_entry(update.effective_user.id, entry, mode, ok)
    context.user_data["quiz_last_answered_entry"] = entry
    _register_retry_result(context, mode, entry.get("id"), ok, is_retry)
    if not is_retry:
        _set_retry_since_last(context, mode, _retry_since_last(context, mode) + 1)

    feedback = _format_feedback_article(entry, ok, _user_dict_lang(update.effective_user.id))
    return await _send_article_prompt(update, context, feedback)


def _weak_status(ok):
    """Handle weak status."""
    return "Correct ✅" if ok else "Wrong ❌"


def _format_feedback_plural_weak(entry, ok):
    """Handle format feedback plural weak."""
    article = str(entry.get("article", "")).strip()
    german = _cap_word(entry.get("german", ""))
    plural = _cap_word(entry.get("plural", ""))
    headline = f"{_weak_status(ok)} >>>     {_md_escape((article + ' ' + german).strip())}"
    if plural:
        return f"{headline}\n\n_pl. die {_md_escape(plural)}_"
    return headline


def _format_feedback_prateritum_weak(entry, ok):
    """Handle format feedback prateritum weak."""
    infinitive = _low_word(entry.get("infinitive", ""))
    pr = _low_word(entry.get("prateritum", ""))
    headline = f"{_weak_status(ok)} >>>     {_md_escape(infinitive)}"
    if pr:
        return f"{headline}\n\n_{_md_escape(pr)}_"
    return headline


def _format_feedback_partizip2_weak(entry, ok):
    """Handle format feedback partizip2 weak."""
    infinitive = _low_word(entry.get("infinitive", ""))
    p2 = _expected_partizip2(entry)
    headline = f"{_weak_status(ok)} >>>     {_md_escape(infinitive)}"
    if p2:
        return f"{headline}\n\n_{_md_escape(p2)}_"
    return headline


async def quiz_article_weak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz article weak."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _back_to_tests(update, context)
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_ARTICLE_WEAK)
    if text == AI_CLARIFY_FIELD and is_owner(update.effective_user.id):
        return await _start_quiz_edit(update, context, QUIZ_ARTICLE_WEAK)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_article_weak_prompt(update, context, None)

    state = get_form_state(get_db_path(), update.effective_user.id, entry.get("id"), "article")
    ok = _normalize_answer(text) == _normalize_answer(entry.get("article", ""))
    update_form_stats(get_db_path(), update.effective_user.id, entry.get("id"), {"article": ok})
    if not state["active"] and state["review_stage"] > 0:
        stage_snapshot = update_weak_review_state(get_db_path(), update.effective_user.id, entry.get("id"), "article", ok)
    else:
        stage_snapshot = get_form_review_stage(get_db_path(), update.effective_user.id, entry.get("id"), "article")
    log_quiz_event(
        get_db_path(),
        user_id=update.effective_user.id,
        word_id=entry.get("id"),
        mode="weak_article",
        is_correct=ok,
        review_stage_snapshot=stage_snapshot,
    )
    context.user_data["quiz_last_answered_entry"] = entry
    feedback = _format_feedback_article(entry, ok, _user_dict_lang(update.effective_user.id))
    return await _send_article_weak_prompt(update, context, feedback)


async def quiz_plural_weak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz plural weak."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _back_to_tests(update, context)
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_PLURAL_WEAK)
    if text == AI_CLARIFY_FIELD and is_owner(update.effective_user.id):
        return await _start_quiz_edit(update, context, QUIZ_PLURAL_WEAK)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_plural_weak_prompt(update, context, None)

    state = get_form_state(get_db_path(), update.effective_user.id, entry.get("id"), "plural")
    if text == I_DONT_KNOW_TEXT:
        ok = False
    else:
        ok = _normalize_plural_value(text) == _normalize_plural_value(entry.get("plural", ""))
    update_form_stats(get_db_path(), update.effective_user.id, entry.get("id"), {"plural": ok})
    if not state["active"] and state["review_stage"] > 0:
        stage_snapshot = update_weak_review_state(get_db_path(), update.effective_user.id, entry.get("id"), "plural", ok)
    else:
        stage_snapshot = get_form_review_stage(get_db_path(), update.effective_user.id, entry.get("id"), "plural")
    log_quiz_event(
        get_db_path(),
        user_id=update.effective_user.id,
        word_id=entry.get("id"),
        mode="weak_plural",
        is_correct=ok,
        review_stage_snapshot=stage_snapshot,
    )
    context.user_data["quiz_last_answered_entry"] = entry
    feedback = _format_feedback_plural_weak(entry, ok)
    return await _send_plural_weak_prompt(update, context, feedback)


async def quiz_prateritum_weak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz prateritum weak."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _back_to_tests(update, context)
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_PRATERITUM_WEAK)
    if text == AI_CLARIFY_FIELD and is_owner(update.effective_user.id):
        return await _start_quiz_edit(update, context, QUIZ_PRATERITUM_WEAK)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_prateritum_weak_prompt(update, context, None)

    state = get_form_state(get_db_path(), update.effective_user.id, entry.get("id"), "prateritum")
    if text == I_DONT_KNOW_TEXT:
        ok = False
    else:
        ok = _normalize_answer(text) == _normalize_answer(entry.get("prateritum", ""))
    update_form_stats(get_db_path(), update.effective_user.id, entry.get("id"), {"prateritum": ok})
    if not state["active"] and state["review_stage"] > 0:
        stage_snapshot = update_weak_review_state(get_db_path(), update.effective_user.id, entry.get("id"), "prateritum", ok)
    else:
        stage_snapshot = get_form_review_stage(get_db_path(), update.effective_user.id, entry.get("id"), "prateritum")
    log_quiz_event(
        get_db_path(),
        user_id=update.effective_user.id,
        word_id=entry.get("id"),
        mode="weak_prateritum",
        is_correct=ok,
        review_stage_snapshot=stage_snapshot,
    )
    context.user_data["quiz_last_answered_entry"] = entry
    feedback = _format_feedback_prateritum_weak(entry, ok)
    return await _send_prateritum_weak_prompt(update, context, feedback)


async def quiz_partizip2_weak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle quiz partizip2 weak."""
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _back_to_tests(update, context)
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_PARTIZIP2_WEAK)
    if text == AI_CLARIFY_FIELD and is_owner(update.effective_user.id):
        return await _start_quiz_edit(update, context, QUIZ_PARTIZIP2_WEAK)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_partizip2_weak_prompt(update, context, None)

    state = get_form_state(get_db_path(), update.effective_user.id, entry.get("id"), "partizip2")
    if text == I_DONT_KNOW_TEXT:
        ok = False
    else:
        ok = _normalize_answer(text) == _expected_partizip2(entry)
    update_form_stats(get_db_path(), update.effective_user.id, entry.get("id"), {"partizip2": ok})
    if not state["active"] and state["review_stage"] > 0:
        stage_snapshot = update_weak_review_state(get_db_path(), update.effective_user.id, entry.get("id"), "partizip2", ok)
    else:
        stage_snapshot = get_form_review_stage(get_db_path(), update.effective_user.id, entry.get("id"), "partizip2")
    log_quiz_event(
        get_db_path(),
        user_id=update.effective_user.id,
        word_id=entry.get("id"),
        mode="weak_partizip2",
        is_correct=ok,
        review_stage_snapshot=stage_snapshot,
    )
    context.user_data["quiz_last_answered_entry"] = entry
    feedback = _format_feedback_partizip2_weak(entry, ok)
    return await _send_partizip2_weak_prompt(update, context, feedback)


async def _save_entry(update: Update, entry):
    """Handle save entry."""
    if "russian" in entry:
        entry["russian"] = _normalize_russian_singular(entry["russian"])
    try:
        saved = add_word(get_db_path(), entry)
    except ValueError as exc:
        await update.message.reply_text(
            str(exc),
            reply_markup=ReplyKeyboardMarkup(AI_KEYS, resize_keyboard=True),
        )
        return None

    await update.message.reply_text(
        f"✅ Saved: {saved.get('id')}",
        reply_markup=ReplyKeyboardMarkup(AI_KEYS, resize_keyboard=True),
    )
    return saved


def _get_ru_de_answer(entry):
    """Handle get ru de answer."""
    if entry.get("type") == "verb":
        return entry.get("infinitive", "")
    return entry.get("german", "")


def _split_answer_parts(text):
    """Handle split answer parts."""
    raw = str(text or "").strip()
    if not raw:
        return []
    if "," in raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    return [p.strip() for p in raw.split() if p.strip()]


def _normalize_plural_value(value):
    """Handle normalize plural value."""
    token = _normalize_answer(value)
    if token.startswith("die "):
        token = token[4:].strip()
    return token


def _expected_partizip2(entry):
    """Handle expected partizip2."""
    part = _normalize_answer(entry.get("partizip2", ""))
    aux = _normalize_answer(
        entry.get("perfect_aux")
        or entry.get("hilfsverb")
        or entry.get("aux")
        or ""
    )
    if aux in {"hat", "ist"}:
        if part.startswith("hat ") or part.startswith("ist "):
            return part
        return f"{aux} {part}".strip()
    return part


def _synonym_entries(entry):
    """Handle synonym entries."""
    wid = entry.get("id")
    if not wid:
        return [entry]
    group = get_synonym_group(get_db_path(), wid)
    if not group:
        return [entry]
    return group


def _check_ru_de_strict(entry, answer_text):
    """Handle check ru de strict."""
    entry_type = (entry.get("type") or "").strip().lower()
    parts = _split_answer_parts(answer_text)
    synonyms = _synonym_entries(entry)

    if entry_type == "nomen":
        if len(parts) < 3:
            return False, {"article": False, "plural": False}
        article_in = _normalize_answer(parts[0])
        german_in = _normalize_answer(parts[1])
        plural_in = _normalize_plural_value(" ".join(parts[2:]))
        for cand in synonyms:
            if (cand.get("type") or "").strip().lower() != "nomen":
                continue
            article_ok = article_in == _normalize_answer(cand.get("article", ""))
            german_ok = german_in == _normalize_answer(cand.get("german", ""))
            plural_ok = plural_in == _normalize_plural_value(cand.get("plural", ""))
            if article_ok and german_ok and plural_ok:
                return True, {"article": True, "plural": True}
        return False, {
            "article": article_in == _normalize_answer(entry.get("article", "")),
            "plural": plural_in == _normalize_plural_value(entry.get("plural", "")),
        }

    if entry_type == "verb":
        if len(parts) < 2:
            return False, {"partizip2": False}
        infinitive_in = _normalize_answer(parts[0])
        if len(parts) == 2:
            p2_in = _normalize_answer(parts[1])
        else:
            p2_in = _normalize_answer(" ".join(parts[2:]))
        for cand in synonyms:
            if (cand.get("type") or "").strip().lower() != "verb":
                continue
            inf_ok = infinitive_in == _normalize_answer(cand.get("infinitive", ""))
            p2_ok = p2_in == _expected_partizip2(cand)
            if inf_ok and p2_ok:
                return True, {"partizip2": True}
        return False, {"partizip2": p2_in == _expected_partizip2(entry)}

    answer = _normalize_answer(answer_text)
    expected_values = []
    for cand in synonyms:
        if (cand.get("type") or "").strip().lower() == "verb":
            expected_values.append(_normalize_answer(cand.get("infinitive", "")))
        else:
            expected_values.append(_normalize_answer(cand.get("german", "")))
    return answer in expected_values, {}


def _entry_translation(entry, dict_lang):
    """Handle entry translation."""
    ru = str(entry.get("russian", "") or "").strip()
    ukr = str(entry.get("ukrainian", "") or "").strip()
    eng = str(entry.get("english", "") or "").strip()
    if dict_lang == "uk":
        return ukr or ru
    if dict_lang == "en":
        return eng or ru
    return ru


def _cap_word(word):
    """Handle cap word."""
    word = str(word or "").strip()
    if not word:
        return ""
    return word[:1].upper() + word[1:].lower()


def _low_word(word):
    """Handle low word."""
    return str(word or "").strip().lower()


def _format_ru_question(entry, dict_lang):
    """Handle format ru question."""
    value = _normalize_russian_singular(_entry_translation(entry, dict_lang))
    word_type = _low_word(entry.get("type", ""))
    type_hint = f" ({word_type})" if word_type else ""
    return f">>    {_md_escape(value)}{_md_escape(type_hint)} ?"


def _format_de_ru_question(entry):
    """Handle format de ru question."""
    word_type = entry.get("type", "")
    article = str(entry.get("article", "")).strip()
    base = entry.get("infinitive", "") if word_type == "verb" else entry.get("german", "")
    base = _cap_word(base) if word_type == "nomen" else _low_word(base)
    prefix = f"{article} " if article else ""
    return f">>    {_md_escape(prefix + base)}?"


def _format_article_question(entry):
    """Handle format article question."""
    return f">>     {_md_escape(_cap_word(entry.get('german', '')))} ?"


def _format_feedback_ru_de(entry, ok):
    """Handle format feedback ru de."""
    status = "Correct ✅" if ok else "Wrong ❌"
    word_type = entry.get("type", "")
    article = str(entry.get("article", "")).strip()
    if word_type == "nomen":
        base = _cap_word(entry.get("german", ""))
    elif word_type == "verb":
        base = _low_word(entry.get("infinitive", ""))
    else:
        base = _low_word(entry.get("german", ""))

    answer = f"{article} {base}".strip() if article else base
    headline = f"{status} >>>     *{_md_escape(answer)}*"
    lines = [headline]

    if word_type == "nomen":
        plural = _cap_word(entry.get("plural", ""))
        if plural:
            lines.append("")
            lines.append(f"_pl. die {_md_escape(plural)}_")
    elif word_type == "verb":
        pr = _low_word(entry.get("prateritum", ""))
        p2 = str(entry.get("partizip2", "") or "").strip().lower()
        aux = str(
            entry.get("perfect_aux")
            or entry.get("hilfsverb")
            or entry.get("aux")
            or ""
        ).strip().lower()
        if p2 and aux in {"hat", "ist"} and not p2.startswith("hat ") and not p2.startswith("ist "):
            p2 = f"{aux} {p2}"
        forms = ", ".join([x for x in [pr, p2] if x])
        if forms:
            lines.append("")
            lines.append(f"_{_md_escape(forms)}_")

    return "\n".join(lines)


def _format_feedback_de_ru(entry, ok, dict_lang):
    """Handle format feedback de ru."""
    status = "Correct ✅" if ok else "Wrong ❌"
    return f"{status} >>>     *{_md_escape(_low_word(_entry_translation(entry, dict_lang)))}*"


def _format_feedback_article(entry, ok, dict_lang):
    """Handle format feedback article."""
    status = "Correct ✅" if ok else "Wrong ❌"
    article = str(entry.get("article", "")).strip()
    german = _cap_word(entry.get("german", ""))
    plural = _cap_word(entry.get("plural", ""))
    headline = f"{status} >>>     {_md_escape((article + ' ' + german).strip())}"
    lines = [headline.strip()]
    if plural:
        lines.append("")
        lines.append(f"_pl. die {_md_escape(plural)}_")
    return "\n".join(lines)


def _pick_due_entry(context, user_id, mode, word_type=None):
    """Handle pick due entry."""
    due = select_due(get_db_path(), user_id=user_id, mode=mode, word_type=word_type)
    if not due:
        return None

    last_ids = context.user_data.setdefault("last_quiz_ids", {})
    last_id = last_ids.get(mode)
    pool = [w for w in due if w.get("id") != last_id] if len(due) > 1 else due
    if not pool:
        pool = due

    entry = random.choice(pool)
    last_ids[mode] = entry.get("id")
    return entry


def _retry_queue(context, mode):
    """Handle retry queue."""
    queues = context.user_data.setdefault("retry_queue", {})
    return queues.setdefault(mode, [])


def _retry_counts(context, mode):
    """Handle retry counts."""
    counts = context.user_data.setdefault("retry_counts", {})
    return counts.setdefault(mode, {})


def _retry_since_last(context, mode):
    """Handle retry since last."""
    counters = context.user_data.setdefault("retry_since_last", {})
    return counters.get(mode, 0)


def _set_retry_since_last(context, mode, value):
    """Handle set retry since last."""
    counters = context.user_data.setdefault("retry_since_last", {})
    counters[mode] = int(value)


def _maybe_take_retry_entry(context, mode):
    """Handle maybe take retry entry."""
    queue = _retry_queue(context, mode)
    if not queue:
        return None
    counts = _retry_counts(context, mode)
    gap = _session_retry_gap(get_db_path())
    since_last = _retry_since_last(context, mode)
    entry, new_since, took = maybe_take_retry_entry(
        queue,
        counts,
        since_last,
        gap,
        lambda word_id: get_word_by_id(get_db_path(), word_id),
    )
    if not entry:
        return None
    context.user_data["quiz_is_retry"] = True
    if took:
        _set_retry_since_last(context, mode, new_since)
    return entry


def _register_retry_result(context, mode, word_id, ok, was_retry):
    """Handle register retry result."""
    queue = _retry_queue(context, mode)
    counts = _retry_counts(context, mode)
    max_repeats = _session_retry_iterations(get_db_path())
    register_retry_result(queue, counts, word_id, ok, was_retry, max_repeats)
    if not was_retry and not ok:
        _set_retry_since_last(context, mode, 0)


def _reset_retry_state(context):
    """Handle reset retry state."""
    context.user_data.pop("retry_queue", None)
    context.user_data.pop("retry_counts", None)
    context.user_data.pop("retry_since_last", None)
    context.user_data.pop("quiz_is_retry", None)


def _pick_weak_entry(context, user_id, weak_field, word_type=None):
    """Handle pick weak entry."""
    due = list_weak_words(get_db_path(), user_id=user_id, weak_field=weak_field, word_type=word_type, limit=300)
    if not due:
        return None
    active = [w for w in due if w.get("is_active")]
    pool_src = active if active else due
    last_ids = context.user_data.setdefault("last_quiz_ids", {})
    mode = f"weak_{weak_field}"
    last_id = last_ids.get(mode)
    pool = [w for w in pool_src if w.get("id") != last_id] if len(pool_src) > 1 else pool_src
    if not pool:
        pool = pool_src
    entry = random.choice(pool)
    last_ids[mode] = entry.get("id")
    return entry


async def _send_ru_de_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    """Handle send ru de prompt."""
    mode = _mode_l1_de(update.effective_user.id)
    entry = _maybe_take_retry_entry(context, mode)
    if not entry:
        context.user_data["quiz_is_retry"] = False
        entry = _pick_due_entry(context, update.effective_user.id, mode)
    if not entry:
        await update.message.reply_text(
            "No due words. Add more or wait until due date.",
            reply_markup=ReplyKeyboardMarkup(_ru_de_menu_keys(update.effective_user.id), resize_keyboard=True),
        )
        return QUIZ_RU_DE_MENU

    context.user_data["quiz_entry"] = entry
    prompt_text = _format_ru_question(entry, _user_dict_lang(update.effective_user.id))
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"

    await update.message.reply_text(
        prompt_text,
        reply_markup=_quiz_markup(update.effective_user.id, article=False),
        parse_mode="Markdown",
    )
    return QUIZ_RU_DE


async def _send_de_ru_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    """Handle send de ru prompt."""
    mode = _mode_de_l1(update.effective_user.id)
    entry = _maybe_take_retry_entry(context, mode)
    if not entry:
        context.user_data["quiz_is_retry"] = False
        entry = _pick_due_entry(context, update.effective_user.id, mode)
    if not entry:
        await update.message.reply_text(
            "No due words. Add more or wait until due date.",
            reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(update.effective_user.id), resize_keyboard=True),
        )
        return TESTS_MENU

    context.user_data["quiz_entry"] = entry
    prompt_text = _format_de_ru_question(entry)
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"

    await update.message.reply_text(
        prompt_text,
        reply_markup=_quiz_markup(update.effective_user.id, article=False),
        parse_mode="Markdown",
    )
    return QUIZ_DE_RU


async def _send_article_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    """Handle send article prompt."""
    mode = MODE_ARTICLE
    entry = _maybe_take_retry_entry(context, mode)
    if not entry:
        context.user_data["quiz_is_retry"] = False
        entry = _pick_due_entry(context, update.effective_user.id, mode, word_type="nomen")
    if not entry:
        await update.message.reply_text(
            "No due nouns. Add more or wait until due date.",
            reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(update.effective_user.id), resize_keyboard=True),
        )
        return TESTS_MENU

    context.user_data["quiz_entry"] = entry
    prompt_text = _format_article_question(entry)
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"

    await update.message.reply_text(
        prompt_text,
        reply_markup=_quiz_markup(update.effective_user.id, article=True),
        parse_mode="Markdown",
    )
    return QUIZ_ARTICLE


async def _send_article_weak_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    """Handle send article weak prompt."""
    entry = _pick_weak_entry(context, update.effective_user.id, "article", word_type="nomen")
    if not entry:
        await update.message.reply_text(
            "No weak article words.",
            reply_markup=ReplyKeyboardMarkup(_ru_de_menu_keys(update.effective_user.id), resize_keyboard=True),
        )
        return QUIZ_RU_DE_MENU
    context.user_data["quiz_entry"] = entry
    text = f">>    {_normalize_russian_singular(_entry_translation(entry, _user_dict_lang(update.effective_user.id)))} -> ____ {_cap_word(entry.get('german', ''))}?"
    text = _md_escape(text)
    if prefix_text:
        text = f"{prefix_text}\n\n{text}"
    await update.message.reply_text(
        text,
        reply_markup=_quiz_markup(update.effective_user.id, article=True, allow_idk=False),
        parse_mode="Markdown",
    )
    return QUIZ_ARTICLE_WEAK


async def _send_plural_weak_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    """Handle send plural weak prompt."""
    entry = _pick_weak_entry(context, update.effective_user.id, "plural", word_type="nomen")
    if not entry:
        await update.message.reply_text(
            "No weak plural words.",
            reply_markup=ReplyKeyboardMarkup(_ru_de_menu_keys(update.effective_user.id), resize_keyboard=True),
        )
        return QUIZ_RU_DE_MENU
    context.user_data["quiz_entry"] = entry
    article = (entry.get("article", "") or "").strip()
    text = f">>    {article} {_cap_word(entry.get('german', ''))} -> plural?"
    text = _md_escape(text)
    if prefix_text:
        text = f"{prefix_text}\n\n{text}"
    await update.message.reply_text(
        text,
        reply_markup=_quiz_markup(update.effective_user.id, article=False, allow_idk=True),
        parse_mode="Markdown",
    )
    return QUIZ_PLURAL_WEAK


async def _send_prateritum_weak_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    """Handle send prateritum weak prompt."""
    entry = _pick_weak_entry(context, update.effective_user.id, "prateritum", word_type="verb")
    if not entry:
        await update.message.reply_text(
            "No weak prateritum words.",
            reply_markup=ReplyKeyboardMarkup([[TEST_PRATERITUM_WEAK_TEXT, TEST_PARTIZIP2_WEAK_TEXT], [t(_user_interface_lang(update.effective_user.id), "back")]], resize_keyboard=True),
        )
        return QUIZ_VERB_WEAK_MENU
    context.user_data["quiz_entry"] = entry
    text = f">>    {_low_word(entry.get('infinitive', ''))} -> Prateritum?"
    text = _md_escape(text)
    if prefix_text:
        text = f"{prefix_text}\n\n{text}"
    await update.message.reply_text(
        text,
        reply_markup=_quiz_markup(update.effective_user.id, article=False, allow_idk=True),
        parse_mode="Markdown",
    )
    return QUIZ_PRATERITUM_WEAK


async def _send_partizip2_weak_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    """Handle send partizip2 weak prompt."""
    entry = _pick_weak_entry(context, update.effective_user.id, "partizip2", word_type="verb")
    if not entry:
        await update.message.reply_text(
            "No weak partizip2 words.",
            reply_markup=ReplyKeyboardMarkup([[TEST_PRATERITUM_WEAK_TEXT, TEST_PARTIZIP2_WEAK_TEXT], [t(_user_interface_lang(update.effective_user.id), "back")]], resize_keyboard=True),
        )
        return QUIZ_VERB_WEAK_MENU
    context.user_data["quiz_entry"] = entry
    text = f">>    {_low_word(entry.get('infinitive', ''))} -> Partizip2?"
    text = _md_escape(text)
    if prefix_text:
        text = f"{prefix_text}\n\n{text}"
    await update.message.reply_text(
        text,
        reply_markup=_quiz_markup(update.effective_user.id, article=False, allow_idk=True),
        parse_mode="Markdown",
    )
    return QUIZ_PARTIZIP2_WEAK


def _ai_prompt(word, clarification):
    """Handle ai prompt."""
    extra = f"\nClarification: {clarification}" if clarification else ""
    return (
        "Return only a JSON object and nothing else. "
        "Detect German part of speech. Allowed types: "
        "nomen, verb, adjektiv, pronomen, adverb, praeposition, konjunktion, partikel. "
        "For nomen return: type, article, german, plural, russian. "
        "For verb return: type, infinitive, prateritum, partizip2, russian. "
        "For all other allowed types return: type, german, russian. "
        "Use Russian translations in singular only. No extra text."
        f"\nWord: {word}{extra}"
    )


def _ai_precheck_prompt(word):
    """Handle ai precheck prompt."""
    return (
        "Return only a JSON object and nothing else. "
        "Detect German part of speech. Allowed types: "
        "nomen, verb, adjektiv, pronomen, adverb, praeposition, konjunktion, partikel. "
        "Correct spelling if needed. "
        "Return JSON fields: type, value (corrected base form). "
        "For nouns, value is the singular base form (no article). "
        "For verbs, value is the infinitive. "
        "For other types, value is the base German form. "
        f"\nWord: {word}"
    )


def _parse_precheck_entry(entry):
    """Handle parse precheck entry."""
    if not isinstance(entry, dict):
        return None
    entry_type = _type_label(entry.get("type", ""))
    if entry_type not in ALLOWED_AI_TYPES:
        return None
    value = entry.get("value") or entry.get("german") or entry.get("infinitive") or entry.get("word")
    value = str(value or "").strip()
    if not value:
        return None
    return {"type": entry_type, "word": value}


def _format_precheck_list(items):
    """Handle format precheck list."""
    lines = ["Precheck list:"]
    for idx, item in enumerate(items, start=1):
        word = item.get("word", "")
        item_type = item.get("type", "?")
        original = item.get("original", "")
        if original and _normalize_lookup_text(original) != _normalize_lookup_text(word):
            lines.append(f"{idx}. {word} [{item_type}] (from {original})")
        else:
            lines.append(f"{idx}. {word} [{item_type}]")
    return "\n".join(lines)


async def _precheck_words(update: Update, context: ContextTypes.DEFAULT_TYPE, words):
    """Handle precheck words."""
    if not os.getenv("OPENAI_API_KEY"):
        await _reply_text_with_retry(
            update.message,
            "AI is not configured. Set OPENAI_API_KEY in .env.",
            reply_markup=_main_menu_markup(update.effective_user.id),
        )
        return None

    items = []
    for raw in words:
        prompt_text = _ai_precheck_prompt(raw)
        try:
            response_text = generate_fields(prompt_text)
        except Exception:
            items.append({"original": raw, "word": raw, "type": "?"})
            continue
        entry = _try_parse_json(response_text)
        parsed = _parse_precheck_entry(entry)
        if not parsed:
            items.append({"original": raw, "word": raw, "type": "?"})
            continue
        items.append({"original": raw, "word": parsed["word"], "type": parsed["type"]})
    return items


def _validate_ai_entry(entry):
    """Handle validate ai entry."""
    if not isinstance(entry, dict):
        return False

    entry_type = _type_label(entry.get("type", ""))
    if entry_type not in ALLOWED_AI_TYPES:
        return False

    if entry_type == "nomen":
        return bool(entry.get("german")) and bool(entry.get("russian"))
    if entry_type == "verb":
        return bool(entry.get("infinitive")) and bool(entry.get("russian"))
    return bool(entry.get("german")) and bool(entry.get("russian"))


def _normalize_ai_entry(entry):
    """Handle normalize ai entry."""
    entry = dict(entry)
    entry_type = _type_label(entry.get("type", ""))
    entry["type"] = entry_type

    if entry_type == "verb":
        return {
            "type": "verb",
            "infinitive": str(entry.get("infinitive", "")).strip(),
            "prateritum": str(entry.get("prateritum", "")).strip(),
            "partizip2": str(entry.get("partizip2", "")).strip(),
            "russian": _normalize_russian_singular(entry.get("russian", "")),
            "ukrainian": str(entry.get("ukrainian", "")).strip(),
            "english": str(entry.get("english", "")).strip(),
            "cefr_level": "",
        }

    if entry_type == "nomen":
        return {
            "type": "nomen",
            "article": str(entry.get("article", "")).strip(),
            "german": str(entry.get("german", "")).strip(),
            "plural": str(entry.get("plural", "")).strip(),
            "russian": _normalize_russian_singular(entry.get("russian", "")),
            "ukrainian": str(entry.get("ukrainian", "")).strip(),
            "english": str(entry.get("english", "")).strip(),
            "cefr_level": "",
        }

    return {
        "type": entry_type,
        "german": str(entry.get("german", "")).strip(),
        "russian": _normalize_russian_singular(entry.get("russian", "")),
        "ukrainian": str(entry.get("ukrainian", "")).strip(),
        "english": str(entry.get("english", "")).strip(),
        "cefr_level": "",
    }


def _is_non_infinitive_verb_input(raw_word, entry):
    """Handle is non infinitive verb input."""
    if entry.get("type") != "verb":
        return False
    source = _normalize_lookup_text(raw_word)
    infinitive = _normalize_lookup_text(entry.get("infinitive", ""))
    if not source or not infinitive:
        return False
    return source != infinitive


def _format_ai_preview(entry):
    """Handle format ai preview."""
    entry_type = entry.get("type")
    if entry_type == "nomen":
        art = entry.get("article", "")
        ger = entry.get("german", "")
        plural = entry.get("plural", "")
        rus = entry.get("russian", "")
        extras = f" ({plural})" if plural else ""
        return (
            "Recognized [Nomen]\n"
            f"{art} *{_md_escape(ger)}*{_md_escape(extras)} - {_md_escape(rus)}"
        )

    if entry_type == "verb":
        inf = entry.get("infinitive", "")
        pr = entry.get("prateritum", "")
        p2 = entry.get("partizip2", "")
        rus = entry.get("russian", "")
        extras = " / ".join([x for x in [pr, p2] if x])
        extras = f" ({extras})" if extras else ""
        return (
            "Recognized [Verb]\n"
            f"*{_md_escape(inf)}*{_md_escape(extras)} - {_md_escape(rus)}"
        )

    ger = entry.get("german", "")
    rus = entry.get("russian", "")
    return f"Recognized [{entry_type}]\n*{_md_escape(ger)}* - {_md_escape(rus)}"


async def _process_ai_word(update: Update, context: ContextTypes.DEFAULT_TYPE, word, clarification):
    """Handle process ai word."""
    has_queue = bool(context.user_data.get("add_queue"))

    reject_reason = _prefilter_reject_reason(word)
    if reject_reason:
        if has_queue:
            logging.info("Queue skip filtered word: %s", word)
            return ADD_INPUT
        await _reply_text_with_retry(
            update.message,
            f"{reject_reason} Skipped: {word}",
            reply_markup=ReplyKeyboardMarkup(_add_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_INPUT

    existing = find_existing_word(get_db_path(), word)
    if existing:
        existing_type = existing.get("type", "word")
        existing_value = existing.get("infinitive", "") if existing_type == "verb" else existing.get("german", "")
        if has_queue:
            logging.info("Queue skip existing word: %s", word)
            return ADD_INPUT
        await _reply_text_with_retry(
            update.message,
            f"Already in database: [{existing_type}] {existing_value}",
            reply_markup=ReplyKeyboardMarkup(_add_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_INPUT

    if not os.getenv("OPENAI_API_KEY"):
        await _reply_text_with_retry(
            update.message,
            "AI is not configured. Set OPENAI_API_KEY in .env.",
            reply_markup=_main_menu_markup(update.effective_user.id),
        )
        return MENU

    context.user_data["pending_word"] = word
    prompt_text = _ai_prompt(word, clarification)
    context.user_data["last_ai_prompt"] = prompt_text
    try:
        response_text = generate_fields(prompt_text)
    except Exception as exc:
        logging.exception("AI request failed for word: %s", word)
        await _reply_text_with_retry(
            update.message,
            f"Error: AI request failed: {exc}",
            reply_markup=ReplyKeyboardMarkup(_clarify_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_CLARIFY

    entry = _try_parse_json(response_text)
    if entry is None or not _validate_ai_entry(entry):
        logging.warning(
            "AI parse failed word=%s clarification=%s raw=%s",
            word,
            clarification,
            (response_text or "")[:500],
        )
        await _reply_text_with_retry(
            update.message,
            (
                f"Error: AI failed to parse for word: {word}\n"
                f"Clarification: {clarification or '-'}\n"
                "Use Confirm, Clarify AI, Clarify field, Reject, Ban, or Stop."
            ),
            reply_markup=ReplyKeyboardMarkup(_clarify_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_CLARIFY

    entry = _normalize_ai_entry(entry)
    if _is_non_infinitive_verb_input(word, entry):
        inf = entry.get("infinitive", "")
        existing_inf = find_existing_word(get_db_path(), inf) if inf else None
        if existing_inf:
            msg = f"❌ Verb form detected: {word}. Base infinitive: {inf}. Already in database, not saved."
            if has_queue:
                await _reply_text_with_retry(update.message, msg)
                logging.info("Queue skip non-infinitive verb input (existing infinitive): %s -> %s", word, inf)
                return ADD_INPUT
            await _reply_text_with_retry(
                update.message,
                msg,
                reply_markup=ReplyKeyboardMarkup(_add_keys(update.effective_user.id), resize_keyboard=True),
            )
            return ADD_INPUT

    context.user_data["pending_entry"] = entry
    return await _show_ai_preview(update, entry)


async def _show_ai_preview(update: Update, entry):
    """Handle show ai preview."""
    preview = _format_ai_preview(entry)
    await _reply_text_with_retry(
        update.message,
        preview,
        reply_markup=ReplyKeyboardMarkup(AI_KEYS, resize_keyboard=True),
        parse_mode="Markdown",
    )
    return ADD_CONFIRM


async def _process_next_ai_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle process next ai word."""
    queue = context.user_data.get("add_queue") or []
    while queue:
        word = queue.pop(0)
        context.user_data["add_queue"] = queue
        state = await _process_ai_word(update, context, word, None)
        if state in (ADD_CONFIRM, ADD_CLARIFY, MENU):
            return state
        queue = context.user_data.get("add_queue") or []

    context.user_data.pop("pending_word", None)
    context.user_data.pop("pending_entry", None)
    await _reply_text_with_retry(
        update.message,
        "✅ Done.",
        reply_markup=_main_menu_markup(update.effective_user.id),
    )
    return MENU
async def _ban_pending_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle ban pending word."""
    word = (context.user_data.get("pending_word") or "").strip()
    if not word:
        await update.message.reply_text(
            "❌ No word to ban.",
            reply_markup=ReplyKeyboardMarkup(_add_keys(update.effective_user.id), resize_keyboard=True),
        )
        return ADD_INPUT
    created = add_banned_word(get_db_path(), word)
    status = "✅ Added to banned list." if created else "✅ Already in banned list."
    await update.message.reply_text(f"{status} ({word})")
    context.user_data.pop("pending_entry", None)
    return await _process_next_ai_word(update, context)


async def _stop_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle stop queue."""
    context.user_data.pop("add_queue", None)
    context.user_data.pop("pending_word", None)
    context.user_data.pop("pending_entry", None)
    context.user_data.pop("edit_stage", None)
    context.user_data.pop("edit_field", None)
    context.user_data.pop("edit_source", None)
    context.user_data.pop("edit_return_state", None)
    context.user_data.pop("flag_word_id", None)
    context.user_data.pop("flag_return_state", None)
    await update.message.reply_text(
        "⏹ Stopped.",
        reply_markup=_main_menu_markup(update.effective_user.id),
    )
    return MENU


async def _back_to_main_menu(update: Update):
    """Handle back to main menu."""
    user_id = update.effective_user.id
    await update.message.reply_text(
        t(_user_interface_lang(user_id), "main_menu"),
        reply_markup=_main_menu_markup(user_id),
    )
    return MENU


async def _back_to_tests(update: Update, context: ContextTypes.DEFAULT_TYPE = None):
    """Handle back to tests."""
    if context is not None:
        _reset_retry_state(context)
    user_id = update.effective_user.id
    await update.message.reply_text(
        t(_user_interface_lang(user_id), "tests") + ":",
        reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(user_id), resize_keyboard=True),
    )
    return TESTS_MENU


def _user_display(row):
    """Handle user display."""
    username = str(row.get("username") or "").strip()
    first_name = str(row.get("first_name") or "").strip()
    if username:
        return f"@{username}"
    if first_name:
        return first_name
    return str(row.get("user_id"))


def _format_daily_stats_text(day_iso, totals, per_user):
    """Handle format daily stats text."""
    lines = [
        f"Daily stats ({day_iso})",
        f"Users: {totals.get('unique_users', 0)}",
        f"Attempts: {totals.get('total_attempts', 0)}",
        f"Correct: {totals.get('correct_attempts', 0)}",
        f"Win rate: {totals.get('win_rate', 0.0)}%",
        f"Words touched: {totals.get('unique_words', 0)}",
        f"Wrong->Correct: {totals.get('wrong_to_correct', 0)}",
        f"Correct->Correct: {totals.get('correct_to_correct', 0)}",
        f"First-time correct: {totals.get('first_time_correct', 0)}",
        f"Memory score: {totals.get('memory_score', 0.0)}",
    ]
    if per_user:
        lines.append("")
        lines.append("Per user:")
        for row in per_user:
            lines.append(
                f"- {_user_display(row)} | attempts={row['total_attempts']} | win={row['win_rate']}% | "
                f"W->C={row['wrong_to_correct']} | C->C={row['correct_to_correct']} | score={row['memory_score']}"
            )
    return "\n".join(lines)


def _format_leaderboard_text(day_iso, rows):
    """Handle format leaderboard text."""
    lines = [f"Leaderboard ({day_iso})"]
    if not rows:
        lines.append("No data.")
        return "\n".join(lines)
    for idx, row in enumerate(rows, start=1):
        lines.append(
            f"{idx}. {_user_display(row)} | win={row['win_rate']}% | attempts={row['total_attempts']} | score={row['memory_score']}"
        )
    return "\n".join(lines)


async def stats_today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle stats today command."""
    if not is_owner(update.effective_user.id):
        return
    day_iso = datetime.now(timezone.utc).date().isoformat()
    totals = get_daily_totals(get_db_path(), day_iso)
    per_user = list_daily_user_stats(get_db_path(), day_iso)
    await update.message.reply_text(_format_daily_stats_text(day_iso, totals, per_user))


async def leaderboard_today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle leaderboard today command."""
    if not is_owner(update.effective_user.id):
        return
    day_iso = datetime.now(timezone.utc).date().isoformat()
    rows = list_daily_leaderboard(get_db_path(), day_iso, min_attempts=1, limit=20)
    await update.message.reply_text(_format_leaderboard_text(day_iso, rows))


async def _daily_stats_job(context: ContextTypes.DEFAULT_TYPE):
    """Handle daily stats job."""
    owner_id = get_owner_id()
    if not owner_id:
        return
    day_iso = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    totals = get_daily_totals(get_db_path(), day_iso)
    per_user = list_daily_user_stats(get_db_path(), day_iso)
    leaderboard = list_daily_leaderboard(get_db_path(), day_iso, min_attempts=1, limit=10)
    text = _format_daily_stats_text(day_iso, totals, per_user)
    board = _format_leaderboard_text(day_iso, leaderboard)
    try:
        await context.bot.send_message(chat_id=owner_id, text=text)
        await context.bot.send_message(chat_id=owner_id, text=board)
    except Exception:
        pass


def _configure_daily_stats_report(app: Application):
    """Handle configure daily stats report."""
    job_queue = app.job_queue
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(DAILY_STATS_JOB_NAME):
        job.schedule_removal()
    job_queue.run_daily(
        _daily_stats_job,
        time=time(hour=0, minute=0, tzinfo=timezone.utc),
        name=DAILY_STATS_JOB_NAME,
    )


def _update_srs_for_entry(user_id, entry, mode, ok):
    """Handle update srs for entry."""
    quality = 5 if ok else 2
    update_progress(get_db_path(), user_id=user_id, word_id=entry.get("id"), mode=mode, quality=quality)
    log_quiz_event(get_db_path(), user_id=user_id, word_id=entry.get("id"), mode=mode, is_correct=ok)


def _create_dump_file():
    """Handle create dump file."""
    db_path = get_db_path()
    dump_dir = get_dump_dir()
    dump_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dump_path = dump_dir / f"words-{stamp}.db"
    shutil.copy2(db_path, dump_path)
    return dump_path


def _create_logs_dump_file():
    """Handle create logs dump file."""
    log_path = get_log_path()
    if not log_path.exists() or log_path.stat().st_size == 0:
        return None

    dump_dir = get_dump_dir()
    dump_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = dump_dir / f"bot-logs-{stamp}.txt"
    shutil.copy2(log_path, out_path)
    return out_path


def _create_suggestions_dump_file():
    """Handle create suggestions dump file."""
    src = get_suggestions_path()
    if not src.exists() or src.stat().st_size == 0:
        return None

    dump_dir = get_dump_dir()
    dump_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = dump_dir / f"suggested-words-{stamp}.txt"
    shutil.copy2(src, out_path)
    return out_path


def _append_suggested_words(user, words):
    """Handle append suggested words."""
    items = [str(x).strip() for x in (words or []) if str(x).strip()]
    if not items:
        return

    path = get_suggestions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    user_id = getattr(user, "id", 0)
    username = getattr(user, "username", "") or "-"
    first_name = getattr(user, "first_name", "") or "-"

    with path.open("a", encoding="utf-8") as f:
        f.write(f"\n[{ts}] user_id={user_id} username=@{username} name={first_name}\n")
        for word in items:
            f.write(f"{word}\n")


def _configure_logging():
    """Handle configure logging."""
    log_path = get_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = TimedRotatingFileHandler(
        filename=log_path,
        when="D",
        interval=1,
        backupCount=14,
        encoding="utf-8",
        utc=True,
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)

    _cleanup_old_dump_logs()


def _cleanup_old_dump_logs(days=14):
    """Handle cleanup old dump logs."""
    cutoff = datetime.now(timezone.utc).timestamp() - (days * 24 * 60 * 60)
    dump_dir = get_dump_dir()
    if not dump_dir.exists():
        return
    for path in dump_dir.glob("bot-logs-*.txt"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            continue


async def _auto_dump_job(context: ContextTypes.DEFAULT_TYPE):
    """Handle auto dump job."""
    try:
        dump_path = _create_dump_file()
    except Exception:
        return

    owner_id = get_owner_id()
    if owner_id:
        try:
            await context.bot.send_document(
                chat_id=owner_id,
                document=dump_path.open("rb"),
                filename=dump_path.name,
                caption=f"✅ Auto dump created: {dump_path.name}",
            )
        except Exception:
            pass


def _configure_auto_dump(app: Application, mode: str):
    """Handle configure auto dump."""
    job_queue = app.job_queue
    if job_queue is None:
        return

    for job in job_queue.get_jobs_by_name(AUTO_DUMP_JOB_NAME):
        job.schedule_removal()

    mode = (mode or "off").lower()
    if mode == "daily":
        job_queue.run_daily(
            _auto_dump_job,
            time=time(hour=0, minute=0, tzinfo=timezone.utc),
            name=AUTO_DUMP_JOB_NAME,
        )
    elif mode == "weekly":
        job_queue.run_daily(
            _auto_dump_job,
            time=time(hour=0, minute=0, tzinfo=timezone.utc),
            days=(0,),
            name=AUTO_DUMP_JOB_NAME,
        )


def _prepare_storage():
    """Handle prepare storage."""
    db_path = get_db_path()
    init_db(db_path)
    if not get_setting(db_path, OWNER_INTERFACE_LANG_KEY):
        set_setting(db_path, OWNER_INTERFACE_LANG_KEY, "en")
    if not get_setting(db_path, OWNER_DICT_LANG_KEY):
        set_setting(db_path, OWNER_DICT_LANG_KEY, "ru")


def main():
    """Handle main."""
    load_dotenv()
    _configure_logging()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN is missing in .env")

    _prepare_storage()

    app = Application.builder().token(token).build()

    mode = get_setting(get_db_path(), AUTO_DUMP_SETTING_KEY, "off")
    _configure_auto_dump(app, mode)
    _configure_daily_stats_report(app)

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex(f"^{START_TEXT}$") & ~filters.COMMAND, start),
        ],
        states={
            MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_choice)],
            SELECT_UI_LANG: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_ui_lang)],
            SELECT_DICT_LANG: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_dict_lang)],
            TESTS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, tests_menu_choice)],
            QUIZ_RU_DE_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_ru_de_menu_choice)],
            SETTINGS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_menu_choice)],
            ALGO_SETTINGS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, algo_settings_menu_choice)],
            ALGO_SETTINGS_EDIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, algo_settings_edit)],
            ALGO_SETTINGS_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, algo_settings_confirm)],
            DUMP_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, dump_menu_choice)],
            AUTO_DUMP_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, auto_dump_choice)],
            ADD_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_input),
                MessageHandler(filters.Document.ALL, add_document),
            ],
            ADD_PRECHECK: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_precheck)],
            ADD_PRECHECK_EDIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_precheck_edit)],
            ADD_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_confirm)],
            ADD_CLARIFY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_clarify)],
            ADD_EDIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_edit)],
            QUIZ_RU_DE: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_ru_de)],
            QUIZ_DE_RU: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_de_ru)],
            QUIZ_ARTICLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_article)],
            QUIZ_ARTICLE_WEAK: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_article_weak)],
            QUIZ_PLURAL_WEAK: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_plural_weak)],
            QUIZ_VERB_WEAK_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_verb_weak_menu_choice)],
            QUIZ_PRATERITUM_WEAK: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_prateritum_weak)],
            QUIZ_PARTIZIP2_WEAK: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_partizip2_weak)],
            SYN_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, synonyms_menu)],
            SYN_ADD_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, synonyms_add_input)],
            SYN_REMOVE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, synonyms_remove_input)],
            SYN_AUTO_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, synonyms_auto_confirm)],
            SYN_CLEAR_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, synonyms_clear_confirm)],
        },
        fallbacks=[CommandHandler("start", start)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("stats_today", stats_today_command))
    app.add_handler(CommandHandler("leaderboard_today", leaderboard_today_command))
    app.run_polling()


if __name__ == "__main__":
    main()
