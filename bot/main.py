
import asyncio
import logging
import os
import random
import shutil
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, time, timezone
from datetime import timedelta

from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, Update
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
    AI_CLARIFY_AI,
    AI_BAN_WORD,
    AI_CLARIFY_FIELD,
    AI_CONFIRM,
    AI_KEYS,
    AI_REJECT,
    AI_SKIP,
    ALLOWED_AI_TYPES,
    ARTICLE_SET,
    AUTO_DUMP_JOB_NAME,
    AUTO_DUMP_KEYS,
    AUTO_DUMP_MENU,
    AUTO_DUMP_SETTING_KEY,
    BACK_TEXT,
    FLAG_TEXT,
    MENU,
    NUMERAL_SET,
    NUMERIC_RE,
    QUIZ_ARTICLE,
    QUIZ_DE_RU,
    QUIZ_RU_DE,
    SETTINGS_MENU,
    SELECT_DICT_LANG,
    SELECT_UI_LANG,
    START_TEXT,
    STOP_TEXT,
    TESTS_MENU,
    get_db_path,
    get_dump_dir,
    get_info_path,
    get_log_path,
    get_owner_id,
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
    count_words,
    delete_word,
    find_existing_word,
    get_setting,
    init_db,
    select_due,
    set_setting,
    update_progress,
    update_word_fields,
    list_pending_word_flags,
    list_daily_leaderboard,
    list_daily_user_stats,
    get_daily_totals,
    is_banned_word,
    log_quiz_event,
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


DAILY_STATS_JOB_NAME = "daily_stats_report"
OWNER_INTERFACE_LANG_KEY = "owner_interface_lang"
OWNER_DICT_LANG_KEY = "owner_dict_lang"


def _main_menu_keys(user_id):
    lang = _user_interface_lang(user_id)
    return [[t(lang, "add_word"), t(lang, "tests")], [t(lang, "settings"), t(lang, "stat")]]


def _main_menu_markup(user_id):
    return ReplyKeyboardMarkup(_main_menu_keys(user_id), resize_keyboard=True)


def _user_interface_lang(user_id):
    lang = get_setting(get_db_path(), OWNER_INTERFACE_LANG_KEY, "en")
    return normalize_interface_lang(lang, default="en")


def _user_dict_lang(user_id):
    dict_lang = get_setting(get_db_path(), OWNER_DICT_LANG_KEY, "ru")
    return normalize_dict_lang(dict_lang, default="ru")


def _dict_label(user_id):
    dict_lang = _user_dict_lang(user_id)
    if dict_lang == "uk":
        return "UA"
    if dict_lang == "en":
        return "EN"
    return "RU"


def _mode_l1_de(user_id):
    dict_lang = _user_dict_lang(user_id)
    if dict_lang == "ru":
        return MODE_RU_DE
    return f"{dict_lang}_de"


def _mode_de_l1(user_id):
    dict_lang = _user_dict_lang(user_id)
    if dict_lang == "ru":
        return MODE_DE_RU
    return f"de_{dict_lang}"


def _tests_menu_keys(user_id):
    lang = _user_interface_lang(user_id)
    label = _dict_label(user_id)
    return [
        [f"{t(lang, 'quiz_l1_de').replace('L1', label)}", f"{t(lang, 'quiz_de_l1').replace('L1', label)}"],
        [t(lang, "quiz_article"), t(lang, "back")],
    ]


def _settings_menu_keys(user_id):
    lang = _user_interface_lang(user_id)
    return [
        [t(lang, "suggested_words"), t(lang, "review_flags")],
        [t(lang, "dump_logs"), t(lang, "dump_db")],
        [t(lang, "interface_lang_btn"), t(lang, "dict_lang_btn")],
        [t(lang, "auto_dump"), t(lang, "back")],
    ]


def _action_key_from_text(text, user_id):
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
        ("suggested_words", "suggested_words"),
        ("review_flags", "review_flags"),
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
    quiz_l1_de = t(lang, "quiz_l1_de").replace("L1", _dict_label(user_id))
    quiz_de_l1 = t(lang, "quiz_de_l1").replace("L1", _dict_label(user_id))
    mapping[quiz_l1_de] = "quiz_l1_de"
    mapping[quiz_de_l1] = "quiz_de_l1"
    mapping[t("en", "quiz_l1_de").replace("L1", _dict_label(user_id))] = "quiz_l1_de"
    mapping[t("en", "quiz_de_l1").replace("L1", _dict_label(user_id))] = "quiz_de_l1"
    return mapping.get(text.strip(), "")


def _auto_dump_keys(user_id):
    lang = _user_interface_lang(user_id)
    return [[t(lang, "daily"), t(lang, "weekly")], [t(lang, "off"), t(lang, "back")]]


def _add_keys(user_id):
    return [[t(_user_interface_lang(user_id), "back")]]


def _stop_keys(user_id):
    return [[t(_user_interface_lang(user_id), "stop")]]


def _clarify_keys(user_id):
    return [
        [AI_CLARIFY_AI, AI_CLARIFY_FIELD, AI_BAN_WORD],
        [AI_SKIP, t(_user_interface_lang(user_id), "stop")],
    ]


def _add_input_markup(user_id):
    return ReplyKeyboardMarkup([[t(_user_interface_lang(user_id), "back")]], resize_keyboard=True)


def _split_text_chunks(text, max_size=3500):
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


def _quiz_markup(user_id, article=False):
    lang = _user_interface_lang(user_id)
    rows = []
    if article:
        rows.append(["der", "die", "das"])
    if is_owner(user_id):
        rows.append([AI_CLARIFY_FIELD, FLAG_TEXT, t(lang, "stop")])
    else:
        rows.append([FLAG_TEXT, t(lang, "stop")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _parse_level_toggle(text):
    token = (text or "").strip().split(" ", 1)[0].upper()
    return token if token in {"A1", "A2", "B1", "B2", "C1", "C2"} else ""


def _edit_fields_markup(source):
    if source == "add":
        return ReplyKeyboardMarkup([["russian", "ukrainian", "english"], [BACK_TEXT]], resize_keyboard=True)
    if source == "quiz":
        return ReplyKeyboardMarkup([["russian", "ukrainian", "english"], ["article", "plural"], ["prateritum", "partizip2"], ["german", "infinitive"], ["type", "delete"], [BACK_TEXT]], resize_keyboard=True)
    return ReplyKeyboardMarkup([["russian", BACK_TEXT]], resize_keyboard=True)


def _allowed_edit_fields(source):
    if source == "add":
        return {"russian", "ukrainian", "english"}
    if source == "quiz":
        return {"russian", "ukrainian", "english", "article", "plural", "prateritum", "partizip2", "german", "infinitive", "type", "delete"}
    return {"russian", "level"}


def _set_entry_field(entry, raw_update):
    if "=" not in raw_update:
        return None, "Use format: field=value"
    field, value = raw_update.split("=", 1)
    field = field.strip().lower()
    value = value.strip()

    allowed = {
        "russian",
        "ukrainian",
        "english",
        "level",
        "cefr_level",
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
    if field == "level":
        field = "cefr_level"

    if field == "cefr_level":
        level = _parse_level_toggle(value)
        if not level:
            return None, "level must be one of A1,A2,B1,B2,C1,C2"
        updated["cefr_level"] = level
    elif field == "type":
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
    return _set_entry_field(entry, f"{field}={value}")


def _get_quiz_action_entry(context: ContextTypes.DEFAULT_TYPE):
    last_answered = context.user_data.get("quiz_last_answered_entry")
    if last_answered:
        return last_answered, "Last answered word:"
    current = context.user_data.get("quiz_entry")
    if current:
        return current, "Current quiz word:"
    return None, ""


async def _start_quiz_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, return_state):
    entry, title = _get_quiz_action_entry(context)
    if not entry:
        return await _back_to_tests(update)
    context.user_data["edit_source"] = "quiz"
    context.user_data["quiz_edit_entry"] = entry
    context.user_data["edit_stage"] = "pick_field"
    context.user_data["edit_return_state"] = return_state
    await update.message.reply_text(_format_word_card(entry, title))
    await update.message.reply_text(
        "Choose field to edit:",
        reply_markup=_edit_fields_markup("quiz"),
    )
    return ADD_EDIT


async def _show_quiz_prompt_after_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text, state_override=None):
    context.user_data.pop("quiz_edit_entry", None)
    state = state_override or context.user_data.get("edit_return_state")
    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _back_to_tests(update)

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

    return await _back_to_tests(update)


def _format_word_card(entry, title="Word card:"):
    lines = [
        title,
        f"id: {entry.get('id', '')}",
        f"type: {entry.get('type', '')}",
        f"level: {entry.get('cefr_level', '') or '-'}",
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


async def _reply_text_with_retry(message, text, max_retries=2, **kwargs):
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
    return is_owner(user_id)


async def _ask_interface_language(update: Update):
    await update.message.reply_text(
        "Choose interface language:",
        reply_markup=ReplyKeyboardMarkup([interface_choices()], resize_keyboard=True),
    )
    return SELECT_UI_LANG


async def _ask_dictionary_language(update: Update, user_id):
    lang = _user_interface_lang(user_id)
    choices = dict_choices(lang)
    await update.message.reply_text(
        t(lang, "choose_dict_lang"),
        reply_markup=ReplyKeyboardMarkup([[choices[0]], [choices[1]], [choices[2]]], resize_keyboard=True),
    )
    return SELECT_DICT_LANG


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        total = count_words(get_db_path())
        await update.message.reply_text(f"Total words: {total}", reply_markup=_main_menu_markup(user_id))
        return MENU

    await update.message.reply_text(t(_user_interface_lang(user_id), "unknown_menu"))
    return MENU


async def select_ui_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)
    if action == "back":
        return await _back_to_main_menu(update)
    if action == "quiz_l1_de":
        return await _send_ru_de_prompt(update, context, None)
    if action == "quiz_de_l1":
        return await _send_de_ru_prompt(update, context, None)
    if action == "quiz_article":
        return await _send_article_prompt(update, context, None)

    await update.message.reply_text(
        "Use tests menu buttons.",
        reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(user_id), resize_keyboard=True),
    )
    return TESTS_MENU


async def settings_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    action = _action_key_from_text(text, user_id)

    if action == "back":
        return await _back_to_main_menu(update)

    if action == "interface_lang":
        return await _ask_interface_language(update)

    if action == "dict_lang":
        return await _ask_dictionary_language(update, user_id)

    if action == "dump_db":
        dump_path = _create_dump_file()
        await update.message.reply_document(
            document=dump_path.open("rb"),
            filename=dump_path.name,
            caption="✅ SQLite dump created.",
        )
        return SETTINGS_MENU

    if action == "dump_logs":
        logs_path = _create_logs_dump_file()
        if logs_path is None:
            await update.message.reply_text("Log file is empty or not found yet.")
            return SETTINGS_MENU
        await update.message.reply_document(
            document=logs_path.open("rb"),
            filename=logs_path.name,
            caption="✅ Logs dump created.",
        )
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

    if action == "auto_dump":
        mode = get_setting(get_db_path(), AUTO_DUMP_SETTING_KEY, "off")
        await update.message.reply_text(
            f"Current auto dump mode: {mode}",
            reply_markup=ReplyKeyboardMarkup(_auto_dump_keys(user_id), resize_keyboard=True),
        )
        return AUTO_DUMP_MENU

    await update.message.reply_text(
        "Use settings buttons.",
        reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
    )
    return SETTINGS_MENU


async def auto_dump_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    if not is_owner(user_id):
        return await _back_to_main_menu(update)

    action = _action_key_from_text(text, user_id)
    if action == "back":
        await update.message.reply_text(
            t(_user_interface_lang(user_id), "settings_title"),
            reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
        )
        return SETTINGS_MENU

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
        reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
    )
    return SETTINGS_MENU


async def add_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    if len(lines) > 1:
        filtered, existing, recognition = _scan_words_before_ai(lines)
        context.user_data["add_queue"] = recognition
        logging.info(
            "Batch add scan: total=%s filtered=%s existing=%s recognition=%s",
            len(lines),
            len(filtered),
            len(existing),
            len(recognition),
        )
        logging.info("Batch add details: filtered=%s", filtered)
        logging.info("Batch add details: existing=%s", existing)
        logging.info("Batch add details: recognition=%s", recognition)
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

    context.user_data.pop("add_queue", None)
    return await _process_ai_word(update, context, lines[0], None)


async def add_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    context.user_data["add_queue"] = recognition
    logging.info(
        "Batch add scan: total=%s filtered=%s existing=%s recognition=%s",
        len(lines),
        len(filtered),
        len(existing),
        len(recognition),
    )
    logging.info("Batch add details: filtered=%s", filtered)
    logging.info("Batch add details: existing=%s", existing)
    logging.info("Batch add details: recognition=%s", recognition)
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


async def add_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await update.message.reply_text(
            "Choose field to edit:",
            reply_markup=_edit_fields_markup("add"),
        )
        return ADD_EDIT

    if text == AI_REJECT:
        return await _process_next_ai_word(update, context)

    if text != AI_CONFIRM:
        await update.message.reply_text(
            "Use: Confirm, Clarify AI, Clarify Field, Reject, or Ban Word.",
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
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _stop_queue(update, context)

    source = context.user_data.get("edit_source", "add")
    if source == "add":
        entry_key = "pending_entry"
    else:
        entry_key = "quiz_entry"
    entry = context.user_data.get(entry_key)
    if not entry:
        await update.message.reply_text("No entry to edit.", reply_markup=_main_menu_markup(update.effective_user.id))
        return MENU

    stage = context.user_data.get("edit_stage", "pick_field")
    if _action_key_from_text(text, update.effective_user.id) == "back":
        if stage == "pick_field":
            if source == "quiz":
                return await _show_quiz_prompt_after_edit(update, context, "✅ Edit canceled.")
            return await _show_ai_preview(update, entry)
        context.user_data["edit_stage"] = "pick_field"
        await update.message.reply_text(
            "Choose field to edit:",
            reply_markup=_edit_fields_markup(source),
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
        if source == "quiz" and field == "delete":
            word_id = entry.get("id")
            if not word_id:
                await update.message.reply_text(
                    "❌ Cannot delete: missing word id.",
                    reply_markup=_edit_fields_markup(source),
                )
                return ADD_EDIT
            removed = delete_word(get_db_path(), word_id)
            context.user_data.pop("quiz_edit_entry", None)
            context.user_data.pop("quiz_entry", None)
            last_answered = context.user_data.get("quiz_last_answered_entry")
            if last_answered and last_answered.get("id") == word_id:
                context.user_data.pop("quiz_last_answered_entry", None)
            context.user_data["edit_stage"] = "pick_field"
            context.user_data.pop("edit_field", None)
            state = context.user_data.get("edit_return_state")
            prefix = "✅ Deleted." if removed else "❌ Word not found."
            if state == QUIZ_RU_DE:
                return await _send_ru_de_prompt(update, context, prefix)
            if state == QUIZ_DE_RU:
                return await _send_de_ru_prompt(update, context, prefix)
            if state == QUIZ_ARTICLE:
                return await _send_article_prompt(update, context, prefix)
            return await _back_to_tests(update)
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
    context.user_data["edit_stage"] = "pick_field"
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


async def _flag_current_word(update: Update, context: ContextTypes.DEFAULT_TYPE, return_state):
    entry, _ = _get_quiz_action_entry(context)
    if not entry:
        return await _back_to_tests(update)
    word_id = entry.get("id")
    if not word_id:
        return await _back_to_tests(update)
    add_word_flag(get_db_path(), word_id=word_id, user_id=update.effective_user.id, comment="")
    if return_state == QUIZ_RU_DE:
        return await _show_quiz_prompt_after_edit(update, context, "✅ Flag saved.", state_override=QUIZ_RU_DE)
    if return_state == QUIZ_DE_RU:
        return await _show_quiz_prompt_after_edit(update, context, "✅ Flag saved.", state_override=QUIZ_DE_RU)
    if return_state == QUIZ_ARTICLE:
        return await _show_quiz_prompt_after_edit(update, context, "✅ Flag saved.", state_override=QUIZ_ARTICLE)
    return await _back_to_tests(update)


async def quiz_ru_de(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _back_to_tests(update)
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_RU_DE)
    if text == AI_CLARIFY_FIELD and is_owner(update.effective_user.id):
        return await _start_quiz_edit(update, context, QUIZ_RU_DE)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_ru_de_prompt(update, context, None)

    expected = _get_ru_de_answer(entry)
    ok = _normalize_answer(text) == _normalize_answer(expected)
    _update_srs_for_entry(update.effective_user.id, entry, _mode_l1_de(update.effective_user.id), ok)
    context.user_data["quiz_last_answered_entry"] = entry

    feedback = _format_feedback_ru_de(entry, ok)
    return await _send_ru_de_prompt(update, context, feedback)


async def quiz_de_ru(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _back_to_tests(update)
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_DE_RU)
    if text == AI_CLARIFY_FIELD and is_owner(update.effective_user.id):
        return await _start_quiz_edit(update, context, QUIZ_DE_RU)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_de_ru_prompt(update, context, None)

    answer = _normalize_answer(text)
    variants = _russian_variants(_entry_translation(entry, _user_dict_lang(update.effective_user.id)))
    ok = answer in variants
    _update_srs_for_entry(update.effective_user.id, entry, _mode_de_l1(update.effective_user.id), ok)
    context.user_data["quiz_last_answered_entry"] = entry

    feedback = _format_feedback_de_ru(entry, ok, _user_dict_lang(update.effective_user.id))
    return await _send_de_ru_prompt(update, context, feedback)


async def quiz_article(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if _action_key_from_text(text, update.effective_user.id) == "stop":
        return await _back_to_tests(update)
    if text == FLAG_TEXT:
        return await _flag_current_word(update, context, QUIZ_ARTICLE)
    if text == AI_CLARIFY_FIELD and is_owner(update.effective_user.id):
        return await _start_quiz_edit(update, context, QUIZ_ARTICLE)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_article_prompt(update, context, None)

    expected = entry.get("article", "")
    ok = _normalize_answer(text) == _normalize_answer(expected)
    _update_srs_for_entry(update.effective_user.id, entry, MODE_ARTICLE, ok)
    context.user_data["quiz_last_answered_entry"] = entry

    feedback = _format_feedback_article(entry, ok, _user_dict_lang(update.effective_user.id))
    return await _send_article_prompt(update, context, feedback)


async def _save_entry(update: Update, entry):
    if "russian" in entry:
        entry["russian"] = _normalize_russian_singular(entry["russian"])
    try:
        saved = add_word(get_db_path(), entry)
    except ValueError as exc:
        await update.message.reply_text(str(exc), reply_markup=_main_menu_markup(update.effective_user.id))
        return None

    await update.message.reply_text(f"✅ Saved: {saved.get('id')}", reply_markup=_main_menu_markup(update.effective_user.id))
    return saved


def _get_ru_de_answer(entry):
    if entry.get("type") == "verb":
        return entry.get("infinitive", "")
    return entry.get("german", "")


def _entry_translation(entry, dict_lang):
    ru = str(entry.get("russian", "") or "").strip()
    ukr = str(entry.get("ukrainian", "") or "").strip()
    eng = str(entry.get("english", "") or "").strip()
    if dict_lang == "uk":
        return ukr or ru
    if dict_lang == "en":
        return eng or ru
    return ru


def _cap_word(word):
    word = str(word or "").strip()
    if not word:
        return ""
    return word[:1].upper() + word[1:].lower()


def _low_word(word):
    return str(word or "").strip().lower()


def _format_ru_question(entry, dict_lang):
    value = _normalize_russian_singular(_entry_translation(entry, dict_lang))
    word_type = _low_word(entry.get("type", ""))
    type_hint = f" ({word_type})" if word_type else ""
    return f">>    {_md_escape(value)}{_md_escape(type_hint)} ?"


def _format_de_ru_question(entry):
    word_type = entry.get("type", "")
    article = str(entry.get("article", "")).strip()
    base = entry.get("infinitive", "") if word_type == "verb" else entry.get("german", "")
    base = _cap_word(base) if word_type == "nomen" else _low_word(base)
    prefix = f"{article} " if article else ""
    return f">>    {_md_escape(prefix + base)}?"


def _format_article_question(entry):
    return f">>     {_md_escape(_cap_word(entry.get('german', '')))} ?"


def _format_feedback_ru_de(entry, ok):
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
    status = "Correct ✅" if ok else "Wrong ❌"
    return f"{status} >>>     *{_md_escape(_low_word(_entry_translation(entry, dict_lang)))}*"


def _format_feedback_article(entry, ok, dict_lang):
    status = "Correct ✅" if ok else "Wrong ❌"
    article = str(entry.get("article", "")).strip()
    german = _cap_word(entry.get("german", ""))
    plural = _cap_word(entry.get("plural", ""))
    russian = _low_word(_entry_translation(entry, dict_lang))
    headline = f"{status} >>>     *{_md_escape((article + ' ' + german).strip())}*"
    if russian:
        headline += f" ({_md_escape(russian)})"
    lines = [headline.strip()]
    if plural:
        lines.append("")
        lines.append(f"_pl. die {_md_escape(plural)}_")
    return "\n".join(lines)


def _pick_due_entry(context, user_id, mode, word_type=None):
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


async def _send_ru_de_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    mode = _mode_l1_de(update.effective_user.id)
    entry = _pick_due_entry(context, update.effective_user.id, mode)
    if not entry:
        await update.message.reply_text(
            "No due words. Add more or wait until due date.",
            reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(update.effective_user.id), resize_keyboard=True),
        )
        return TESTS_MENU

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
    mode = _mode_de_l1(update.effective_user.id)
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
    entry = _pick_due_entry(context, update.effective_user.id, MODE_ARTICLE, word_type="nomen")
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


def _ai_prompt(word, clarification):
    extra = f"\nClarification: {clarification}" if clarification else ""
    return (
        "Return only a JSON object and nothing else. "
        "Detect German part of speech. Allowed types: "
        "nomen, verb, adjektiv, pronomen, adverb, praeposition, konjunktion, partikel. "
        "Set cefr_level from: A1, A2, B1, B2, C1, C2. "
        "For nomen return: type, article, german, plural, russian, ukrainian, english, cefr_level. "
        "For verb return: type, infinitive, prateritum, partizip2, russian, ukrainian, english, cefr_level. "
        "For all other allowed types return: type, german, russian, ukrainian, english, cefr_level. "
        "Use Russian/Ukrainian translations in singular only. No extra text."
        f"\nWord: {word}{extra}"
    )


def _validate_ai_entry(entry):
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
    entry = dict(entry)
    entry_type = _type_label(entry.get("type", ""))
    entry["type"] = entry_type
    cefr_level = _parse_level_toggle(entry.get("cefr_level", ""))

    if entry_type == "verb":
        return {
            "type": "verb",
            "infinitive": str(entry.get("infinitive", "")).strip(),
            "prateritum": str(entry.get("prateritum", "")).strip(),
            "partizip2": str(entry.get("partizip2", "")).strip(),
            "russian": _normalize_russian_singular(entry.get("russian", "")),
            "ukrainian": str(entry.get("ukrainian", "")).strip(),
            "english": str(entry.get("english", "")).strip(),
            "cefr_level": cefr_level,
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
            "cefr_level": cefr_level,
        }

    return {
        "type": entry_type,
        "german": str(entry.get("german", "")).strip(),
        "russian": _normalize_russian_singular(entry.get("russian", "")),
        "ukrainian": str(entry.get("ukrainian", "")).strip(),
        "english": str(entry.get("english", "")).strip(),
        "cefr_level": cefr_level,
    }


def _is_non_infinitive_verb_input(raw_word, entry):
    if entry.get("type") != "verb":
        return False
    source = _normalize_lookup_text(raw_word)
    infinitive = _normalize_lookup_text(entry.get("infinitive", ""))
    if not source or not infinitive:
        return False
    return source != infinitive


def _format_ai_preview(entry):
    entry_type = entry.get("type")
    level = entry.get("cefr_level", "") or "NO_LEVEL"
    if entry_type == "nomen":
        art = entry.get("article", "")
        ger = entry.get("german", "")
        plural = entry.get("plural", "")
        rus = entry.get("russian", "")
        ukr = entry.get("ukrainian", "")
        eng = entry.get("english", "")
        extras = f" ({plural})" if plural else ""
        return (
            f"Recognized [Nomen {level}]\n"
            f"{art} *{_md_escape(ger)}* - {_md_escape(rus)} / {_md_escape(ukr)} / {_md_escape(eng)}{_md_escape(extras)}"
        )

    if entry_type == "verb":
        inf = entry.get("infinitive", "")
        pr = entry.get("prateritum", "")
        p2 = entry.get("partizip2", "")
        rus = entry.get("russian", "")
        ukr = entry.get("ukrainian", "")
        eng = entry.get("english", "")
        extras = " / ".join([x for x in [pr, p2] if x])
        extras = f" ({extras})" if extras else ""
        return (
            f"Recognized [Verb {level}]\n"
            f"*{_md_escape(inf)}* - {_md_escape(rus)} / {_md_escape(ukr)} / {_md_escape(eng)}{_md_escape(extras)}"
        )

    ger = entry.get("german", "")
    rus = entry.get("russian", "")
    ukr = entry.get("ukrainian", "")
    eng = entry.get("english", "")
    return f"Recognized [{entry_type} {level}]\n*{_md_escape(ger)}* - {_md_escape(rus)} / {_md_escape(ukr)} / {_md_escape(eng)}"


async def _process_ai_word(update: Update, context: ContextTypes.DEFAULT_TYPE, word, clarification):
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
                "Use Clarify AI, Clarify Field, Skip, or Ban Word."
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
    preview = _format_ai_preview(entry)
    await _reply_text_with_retry(
        update.message,
        preview,
        reply_markup=ReplyKeyboardMarkup(AI_KEYS, resize_keyboard=True),
        parse_mode="Markdown",
    )
    return ADD_CONFIRM


async def _process_next_ai_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        "❌ Stopped.",
        reply_markup=_main_menu_markup(update.effective_user.id),
    )
    return MENU


async def _back_to_main_menu(update: Update):
    user_id = update.effective_user.id
    await update.message.reply_text(
        t(_user_interface_lang(user_id), "main_menu"),
        reply_markup=_main_menu_markup(user_id),
    )
    return MENU


async def _back_to_tests(update: Update):
    user_id = update.effective_user.id
    await update.message.reply_text(
        t(_user_interface_lang(user_id), "tests") + ":",
        reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(user_id), resize_keyboard=True),
    )
    return TESTS_MENU


def _user_display(row):
    username = str(row.get("username") or "").strip()
    first_name = str(row.get("first_name") or "").strip()
    if username:
        return f"@{username}"
    if first_name:
        return first_name
    return str(row.get("user_id"))


def _format_daily_stats_text(day_iso, totals, per_user):
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
    if not is_owner(update.effective_user.id):
        return
    day_iso = datetime.now(timezone.utc).date().isoformat()
    totals = get_daily_totals(get_db_path(), day_iso)
    per_user = list_daily_user_stats(get_db_path(), day_iso)
    await update.message.reply_text(_format_daily_stats_text(day_iso, totals, per_user))


async def leaderboard_today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    day_iso = datetime.now(timezone.utc).date().isoformat()
    rows = list_daily_leaderboard(get_db_path(), day_iso, min_attempts=1, limit=20)
    await update.message.reply_text(_format_leaderboard_text(day_iso, rows))


async def _daily_stats_job(context: ContextTypes.DEFAULT_TYPE):
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
    quality = 5 if ok else 2
    update_progress(get_db_path(), user_id=user_id, word_id=entry.get("id"), mode=mode, quality=quality)
    log_quiz_event(get_db_path(), user_id=user_id, word_id=entry.get("id"), mode=mode, is_correct=ok)


def _create_dump_file():
    db_path = get_db_path()
    dump_dir = get_dump_dir()
    dump_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dump_path = dump_dir / f"words-{stamp}.db"
    shutil.copy2(db_path, dump_path)
    return dump_path


def _create_logs_dump_file():
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
    db_path = get_db_path()
    init_db(db_path)
    if not get_setting(db_path, OWNER_INTERFACE_LANG_KEY):
        set_setting(db_path, OWNER_INTERFACE_LANG_KEY, "en")
    if not get_setting(db_path, OWNER_DICT_LANG_KEY):
        set_setting(db_path, OWNER_DICT_LANG_KEY, "ru")


def main():
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
            SETTINGS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_menu_choice)],
            AUTO_DUMP_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, auto_dump_choice)],
            ADD_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_input),
                MessageHandler(filters.Document.ALL, add_document),
            ],
            ADD_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_confirm)],
            ADD_CLARIFY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_clarify)],
            ADD_EDIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_edit)],
            QUIZ_RU_DE: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_ru_de)],
            QUIZ_DE_RU: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_de_ru)],
            QUIZ_ARTICLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, quiz_article)],
        },
        fallbacks=[CommandHandler("start", start)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("stats_today", stats_today_command))
    app.add_handler(CommandHandler("leaderboard_today", leaderboard_today_command))
    app.run_polling()


if __name__ == "__main__":
    main()


