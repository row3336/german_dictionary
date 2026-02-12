
import json
import asyncio
import logging
import os
import random
import re
import shutil
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, time, timezone
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.error import RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .ai import generate_fields
from .storage import (
    CEFR_LEVELS,
    QUIZ_ARTICLE as MODE_ARTICLE,
    QUIZ_DE_RU as MODE_DE_RU,
    QUIZ_RU_DE as MODE_RU_DE,
    add_word,
    count_words,
    find_existing_word,
    get_setting,
    get_user_levels,
    get_user_status,
    init_db,
    migrate_json_to_sqlite,
    register_or_update_user,
    select_due,
    set_setting,
    set_user_status,
    toggle_user_level,
    update_progress,
    update_word_fields,
)

(
    MENU,
    TESTS_MENU,
    SETTINGS_MENU,
    AUTO_DUMP_MENU,
    LEVELS_MENU,
    ADD_INPUT,
    ADD_CONFIRM,
    ADD_CLARIFY,
    ADD_EDIT,
    QUIZ_RU_DE,
    QUIZ_DE_RU,
    QUIZ_ARTICLE,
) = range(12)

BACK_TEXT = "Back"
STOP_TEXT = "Stop"
START_TEXT = "Start"
AI_CONFIRM = "Confirm"
AI_CLARIFY_AI = "Clarify AI"
AI_CLARIFY_FIELD = "Clarify Field"
AI_REJECT = "Reject"

OWNER_MAIN_KEYS = [["Add Word", "Tests"], ["Settings", "Stat"]]
USER_MAIN_KEYS = [["Add Word", "Tests"], ["Info", "Stat"]]
WAIT_KEYS = [[START_TEXT]]
ADD_KEYS = [[BACK_TEXT]]
AI_KEYS = [[AI_CONFIRM, AI_CLARIFY_AI, AI_CLARIFY_FIELD], [AI_REJECT, STOP_TEXT]]
TESTS_KEYS = [["Quiz RU -> DE", "Quiz DE -> RU"], ["Quiz article", BACK_TEXT]]
SETTINGS_KEYS = [["Dump DB", "Dump Logs"], ["Auto Dump", "Levels"], [BACK_TEXT]]
AUTO_DUMP_KEYS = [["Daily", "Weekly"], ["Off", BACK_TEXT]]
LEVELS_BACK_TEXT = "Back"
EDIT_FIELDS_KEYS = [
    ["russian", "level"],
    [BACK_TEXT],
]
EDIT_LEVEL_KEYS = [["A1", "A2", "B1"], ["B2", "C1", "C2"], [BACK_TEXT]]
QUIZ_KEYS = [[STOP_TEXT]]
ARTICLE_KEYS = [["der", "die", "das"], [STOP_TEXT]]

APPROVE_PREFIX = "approve_user:"
REJECT_PREFIX = "reject_user:"
AUTO_DUMP_SETTING_KEY = "auto_dump_mode"
AUTO_DUMP_JOB_NAME = "auto_db_dump"

ARTICLE_SET = {
    "der",
    "die",
    "das",
    "den",
    "dem",
    "des",
    "ein",
    "eine",
    "einen",
    "einem",
    "eines",
    "kein",
    "keine",
    "keinen",
    "keinem",
    "keines",
}

NUMERAL_SET = {
    "null",
    "eins",
    "zwei",
    "drei",
    "vier",
    "fuenf",
    "fuenfzehn",
    "fuenfzig",
    "funf",
    "funfzehn",
    "funfzig",
    "sechs",
    "sieben",
    "acht",
    "neun",
    "zehn",
    "elf",
    "zwoelf",
    "dreizehn",
    "vierzehn",
    "sechzehn",
    "siebzehn",
    "achtzehn",
    "neunzehn",
    "zwanzig",
    "dreissig",
    "vierzig",
    "sechzig",
    "siebzig",
    "achtzig",
    "neunzig",
    "hundert",
    "tausend",
    "million",
    "milliarde",
}

NUMERIC_RE = re.compile(r"^\s*[+-]?(?:(?:\d{1,3}(?:[.,]\d{3})+)|\d+)(?:[.,]\d+)?\s*%?\s*$")

ALLOWED_AI_TYPES = {
    "nomen",
    "verb",
    "adjektiv",
    "pronomen",
    "adverb",
    "praeposition",
    "konjunktion",
    "partikel",
}


def get_db_path():
    return Path(os.getenv("DB_PATH", "./data/words.db"))


def get_legacy_json_path():
    return Path(os.getenv("DATA_PATH", "./data/words.json"))


def get_dump_dir():
    return Path(os.getenv("DUMP_DIR", "./data/backups"))


def get_log_path():
    return Path(os.getenv("LOG_PATH", "./data/bot.log"))


def get_owner_id():
    try:
        return int(os.getenv("OWNER_ID", "0"))
    except ValueError:
        return 0


def is_owner(user_id):
    owner_id = get_owner_id()
    return owner_id != 0 and user_id == owner_id


def _main_menu_keys(user_id):
    return OWNER_MAIN_KEYS if is_owner(user_id) else USER_MAIN_KEYS


def _main_menu_markup(user_id):
    return ReplyKeyboardMarkup(_main_menu_keys(user_id), resize_keyboard=True)


def _wait_markup():
    return ReplyKeyboardMarkup(WAIT_KEYS, resize_keyboard=True)


def _quiz_markup(user_id, article=False):
    rows = []
    if article:
        rows.append(["der", "die", "das"])
    if is_owner(user_id):
        rows.append([AI_CLARIFY_FIELD, STOP_TEXT])
    else:
        rows.append([STOP_TEXT])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _levels_markup(user_id):
    enabled = set(get_user_levels(get_db_path(), user_id))
    rows = []
    row = []
    for idx, level in enumerate(CEFR_LEVELS, start=1):
        mark = "ON" if level in enabled else "OFF"
        row.append(f"{level} {mark}")
        if idx % 2 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([LEVELS_BACK_TEXT])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _parse_level_toggle(text):
    token = (text or "").strip().split(" ", 1)[0].upper()
    return token if token in CEFR_LEVELS else ""


def _normalize_answer(text):
    return " ".join(text.strip().lower().split())


def _normalize_lookup_text(text):
    return " ".join(str(text).strip().lower().split())


def _normalize_russian_singular(text):
    text = str(text).strip()
    if "/" in text:
        text = text.split("/", 1)[0].strip()
    return text


def _russian_variants(text):
    source = str(text or "")
    parts = re.split(r"[,/;]", source)
    out = []
    for part in parts:
        value = _normalize_answer(part)
        if value:
            out.append(value)
    return out


def _set_entry_field(entry, raw_update):
    if "=" not in raw_update:
        return None, "Use format: field=value"
    field, value = raw_update.split("=", 1)
    field = field.strip().lower()
    value = value.strip()

    allowed = {
        "russian",
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


async def _start_quiz_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, return_state):
    context.user_data["edit_source"] = "quiz"
    context.user_data["edit_stage"] = "pick_field"
    context.user_data["edit_return_state"] = return_state
    await update.message.reply_text(
        "Choose field to edit:",
        reply_markup=ReplyKeyboardMarkup(EDIT_FIELDS_KEYS, resize_keyboard=True),
    )
    return ADD_EDIT


async def _show_quiz_prompt_after_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    state = context.user_data.get("edit_return_state")
    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _back_to_tests(update)

    if state == QUIZ_RU_DE:
        prompt = _normalize_russian_singular(entry.get("russian", ""))
        text = _format_question(prompt)
        if prefix_text:
            text = f"{prefix_text}\n\n{text}"
        await update.message.reply_text(
            text,
            reply_markup=_quiz_markup(update.effective_user.id, article=False),
            parse_mode="Markdown",
        )
        return QUIZ_RU_DE

    if state == QUIZ_DE_RU:
        prompt = entry.get("infinitive", "") if entry.get("type") == "verb" else entry.get("german", "")
        text = _format_question(prompt)
        if prefix_text:
            text = f"{prefix_text}\n\n{text}"
        await update.message.reply_text(
            text,
            reply_markup=_quiz_markup(update.effective_user.id, article=False),
            parse_mode="Markdown",
        )
        return QUIZ_DE_RU

    if state == QUIZ_ARTICLE:
        prompt = entry.get("german", "")
        text = _format_question(prompt)
        if prefix_text:
            text = f"{prefix_text}\n\n{text}"
        await update.message.reply_text(
            text,
            reply_markup=_quiz_markup(update.effective_user.id, article=True),
            parse_mode="Markdown",
        )
        return QUIZ_ARTICLE

    return await _back_to_tests(update)


def _md_escape(text):
    text = str(text or "")
    for ch in ("*", "_", "`"):
        text = text.replace(ch, f"\\{ch}")
    return text


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
    if NUMERIC_RE.match(normalized):
        return "Filtered: looks like a number."
    if normalized in ARTICLE_SET:
        return "Filtered: article words are not stored."
    if normalized in NUMERAL_SET:
        return "Filtered: numeral words are not stored."
    return None


def _type_label(raw_type):
    value = _normalize_lookup_text(raw_type)
    if value == "praeposition":
        return "praeposition"
    return value


def _is_user_approved(user_id):
    if is_owner(user_id):
        return True
    return get_user_status(get_db_path(), user_id) == "approved"


async def _request_owner_approval(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id):
    owner_id = get_owner_id()
    if owner_id == 0:
        return
    user = update.effective_user
    text = (
        "New access request:\n"
        f"user_id: {user_id}\n"
        f"username: @{user.username if user and user.username else '-'}\n"
        f"name: {user.first_name if user and user.first_name else '-'}"
    )
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Approve", callback_data=f"{APPROVE_PREFIX}{user_id}"),
            InlineKeyboardButton("Reject", callback_data=f"{REJECT_PREFIX}{user_id}"),
        ]]
    )
    try:
        await context.bot.send_message(chat_id=owner_id, text=text, reply_markup=keyboard)
    except Exception:
        pass


async def _show_waiting(update: Update):
    await update.message.reply_text(
        "Access pending owner approval. Tap Start later.",
        reply_markup=_wait_markup(),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    register_or_update_user(
        get_db_path(),
        user_id,
        username=user.username or "",
        first_name=user.first_name or "",
    )

    if is_owner(user_id):
        set_user_status(get_db_path(), user_id, "approved")
        await update.message.reply_text("Main menu:", reply_markup=_main_menu_markup(user_id))
        return MENU

    status = get_user_status(get_db_path(), user_id)
    if status == "approved":
        await update.message.reply_text("Main menu:", reply_markup=_main_menu_markup(user_id))
        return MENU

    if status == "rejected":
        await update.message.reply_text("Access was rejected by owner.", reply_markup=_wait_markup())
        return MENU

    await _request_owner_approval(update, context, user_id)
    await _show_waiting(update)
    return MENU


async def menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id

    if text == START_TEXT:
        return await start(update, context)

    if not _is_user_approved(user_id):
        await _show_waiting(update)
        return MENU

    if text == "Add Word":
        await update.message.reply_text(
            "Send a word or a list (one per line), or upload a .txt file.",
            reply_markup=ReplyKeyboardMarkup(ADD_KEYS, resize_keyboard=True),
        )
        return ADD_INPUT

    if text == "Tests":
        await update.message.reply_text(
            "Choose test mode:",
            reply_markup=ReplyKeyboardMarkup(TESTS_KEYS, resize_keyboard=True),
        )
        return TESTS_MENU

    if text == "Settings" and is_owner(user_id):
        await update.message.reply_text(
            "Settings:",
            reply_markup=ReplyKeyboardMarkup(SETTINGS_KEYS, resize_keyboard=True),
        )
        return SETTINGS_MENU

    if text == "Info" and not is_owner(user_id):
        await update.message.reply_text("Info will be added soon.", reply_markup=_main_menu_markup(user_id))
        return MENU

    if text == "Stat":
        total = count_words(get_db_path())
        await update.message.reply_text(f"Total words: {total}", reply_markup=_main_menu_markup(user_id))
        return MENU

    await update.message.reply_text("Unknown command. Use the menu buttons.")
    return MENU


async def tests_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == BACK_TEXT:
        return await _back_to_main_menu(update)
    if text == "Quiz RU -> DE":
        return await _send_ru_de_prompt(update, context, None)
    if text == "Quiz DE -> RU":
        return await _send_de_ru_prompt(update, context, None)
    if text == "Quiz article":
        return await _send_article_prompt(update, context, None)

    await update.message.reply_text(
        "Use tests menu buttons.",
        reply_markup=ReplyKeyboardMarkup(TESTS_KEYS, resize_keyboard=True),
    )
    return TESTS_MENU


async def settings_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    if not is_owner(user_id):
        return await _back_to_main_menu(update)

    if text == BACK_TEXT:
        return await _back_to_main_menu(update)

    if text == "Dump DB":
        dump_path = _create_dump_file()
        await update.message.reply_document(
            document=dump_path.open("rb"),
            filename=dump_path.name,
            caption="✅ SQLite dump created.",
        )
        return SETTINGS_MENU

    if text == "Dump Logs":
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

    if text == "Auto Dump":
        mode = get_setting(get_db_path(), AUTO_DUMP_SETTING_KEY, "off")
        await update.message.reply_text(
            f"Current auto dump mode: {mode}",
            reply_markup=ReplyKeyboardMarkup(AUTO_DUMP_KEYS, resize_keyboard=True),
        )
        return AUTO_DUMP_MENU

    if text == "Levels":
        enabled = ", ".join(get_user_levels(get_db_path(), user_id))
        await update.message.reply_text(
            f"Enabled levels: {enabled}",
            reply_markup=_levels_markup(user_id),
        )
        return LEVELS_MENU

    await update.message.reply_text(
        "Use settings buttons.",
        reply_markup=ReplyKeyboardMarkup(SETTINGS_KEYS, resize_keyboard=True),
    )
    return SETTINGS_MENU


async def levels_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    if not is_owner(user_id):
        return await _back_to_main_menu(update)

    if text == LEVELS_BACK_TEXT:
        await update.message.reply_text(
            "Settings:",
            reply_markup=ReplyKeyboardMarkup(SETTINGS_KEYS, resize_keyboard=True),
        )
        return SETTINGS_MENU

    level = _parse_level_toggle(text)
    if not level:
        await update.message.reply_text(
            "Use level buttons.",
            reply_markup=_levels_markup(user_id),
        )
        return LEVELS_MENU

    changed, levels = toggle_user_level(get_db_path(), user_id, level)
    if not changed:
        await update.message.reply_text(
            "At least one level must stay enabled.",
            reply_markup=_levels_markup(user_id),
        )
        return LEVELS_MENU

    await update.message.reply_text(
        f"Enabled levels: {', '.join(levels)}",
        reply_markup=_levels_markup(user_id),
    )
    return LEVELS_MENU


async def auto_dump_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    if not is_owner(user_id):
        return await _back_to_main_menu(update)

    if text == BACK_TEXT:
        await update.message.reply_text(
            "Settings:",
            reply_markup=ReplyKeyboardMarkup(SETTINGS_KEYS, resize_keyboard=True),
        )
        return SETTINGS_MENU

    mode_map = {"Daily": "daily", "Weekly": "weekly", "Off": "off"}
    if text not in mode_map:
        await update.message.reply_text(
            "Use auto dump buttons.",
            reply_markup=ReplyKeyboardMarkup(AUTO_DUMP_KEYS, resize_keyboard=True),
        )
        return AUTO_DUMP_MENU

    mode = mode_map[text]
    set_setting(get_db_path(), AUTO_DUMP_SETTING_KEY, mode)
    _configure_auto_dump(context.application, mode)
    await update.message.reply_text(
        f"✅ Auto dump mode set: {mode}",
        reply_markup=ReplyKeyboardMarkup(SETTINGS_KEYS, resize_keyboard=True),
    )
    return SETTINGS_MENU


async def add_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    user_id = update.effective_user.id
    if raw == BACK_TEXT:
        return await _back_to_main_menu(update)

    if not is_owner(user_id):
        await _forward_text_to_owner(update, context, raw)
        await update.message.reply_text(
            "✅ Successfully sent to the bot Owner",
            reply_markup=_main_menu_markup(user_id),
        )
        return MENU

    if raw.startswith("{"):
        entry = _try_parse_json(raw)
        if entry is None:
            await update.message.reply_text(
                "Invalid JSON. Make sure it starts with '{' and ends with '}'. Try again.",
                reply_markup=ReplyKeyboardMarkup(ADD_KEYS, resize_keyboard=True),
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
            reply_markup=ReplyKeyboardMarkup([[STOP_TEXT]], resize_keyboard=True),
        )
        return await _process_next_ai_word(update, context)

    context.user_data.pop("add_queue", None)
    return await _process_ai_word(update, context, lines[0], None)


async def add_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc or not doc.file_name.lower().endswith(".txt"):
        await update.message.reply_text("Please upload a .txt file.")
        return ADD_INPUT

    user_id = update.effective_user.id
    if not is_owner(user_id):
        await _forward_document_to_owner(update, context)
        await update.message.reply_text(
            "✅ Successfully sent to the bot Owner",
            reply_markup=_main_menu_markup(user_id),
        )
        return MENU

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
        reply_markup=ReplyKeyboardMarkup([[STOP_TEXT]], resize_keyboard=True),
    )
    return await _process_next_ai_word(update, context)


async def add_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == STOP_TEXT:
        return await _stop_queue(update, context)

    if text == AI_CLARIFY_AI:
        await update.message.reply_text(
            "Send clarification text for AI.",
            reply_markup=ReplyKeyboardMarkup([[STOP_TEXT]], resize_keyboard=True),
        )
        return ADD_CLARIFY

    if text == AI_CLARIFY_FIELD:
        context.user_data["edit_source"] = "add"
        context.user_data["edit_stage"] = "pick_field"
        await update.message.reply_text(
            "Choose field to edit:",
            reply_markup=ReplyKeyboardMarkup(EDIT_FIELDS_KEYS, resize_keyboard=True),
        )
        return ADD_EDIT

    if text == AI_REJECT:
        return await _process_next_ai_word(update, context)

    if text != AI_CONFIRM:
        await update.message.reply_text(
            "Use: Confirm, Clarify AI, Clarify Field, or Reject.",
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
    if text == STOP_TEXT:
        return await _stop_queue(update, context)

    word = context.user_data.get("pending_word")
    if not word:
        await update.message.reply_text("No word to clarify.", reply_markup=_main_menu_markup(update.effective_user.id))
        return MENU

    return await _process_ai_word(update, context, word, text)


async def add_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == STOP_TEXT:
        return await _stop_queue(update, context)

    source = context.user_data.get("edit_source", "add")
    entry_key = "pending_entry" if source == "add" else "quiz_entry"
    entry = context.user_data.get(entry_key)
    if not entry:
        await update.message.reply_text("No entry to edit.", reply_markup=_main_menu_markup(update.effective_user.id))
        return MENU

    stage = context.user_data.get("edit_stage", "pick_field")
    if text == BACK_TEXT:
        if stage == "pick_field":
            if source == "quiz":
                return await _show_quiz_prompt_after_edit(update, context, "✅ Edit canceled.")
            return await _show_ai_preview(update, entry)
        if stage == "pick_level":
            context.user_data["edit_stage"] = "pick_field"
            await update.message.reply_text(
                "Choose field to edit:",
                reply_markup=ReplyKeyboardMarkup(EDIT_FIELDS_KEYS, resize_keyboard=True),
            )
            return ADD_EDIT
        context.user_data["edit_stage"] = "pick_field"
        await update.message.reply_text(
            "Choose field to edit:",
            reply_markup=ReplyKeyboardMarkup(EDIT_FIELDS_KEYS, resize_keyboard=True),
        )
        return ADD_EDIT

    if stage == "pick_field":
        field = text.strip().lower()
        allowed_fields = {"russian", "level"}
        if field not in allowed_fields:
            await update.message.reply_text(
                "Use field buttons.",
                reply_markup=ReplyKeyboardMarkup(EDIT_FIELDS_KEYS, resize_keyboard=True),
            )
            return ADD_EDIT
        context.user_data["edit_field"] = field
        if field == "level":
            context.user_data["edit_stage"] = "pick_level"
            await update.message.reply_text(
                "Choose CEFR level:",
                reply_markup=ReplyKeyboardMarkup(EDIT_LEVEL_KEYS, resize_keyboard=True),
            )
            return ADD_EDIT

        context.user_data["edit_stage"] = "enter_value"
        await update.message.reply_text(
            f"Enter new value for {field}:",
            reply_markup=ReplyKeyboardMarkup([[BACK_TEXT, STOP_TEXT]], resize_keyboard=True),
        )
        return ADD_EDIT

    if stage == "pick_level":
        level = _parse_level_toggle(text)
        if not level:
            await update.message.reply_text(
                "Use level buttons.",
                reply_markup=ReplyKeyboardMarkup(EDIT_LEVEL_KEYS, resize_keyboard=True),
            )
            return ADD_EDIT
        updated, error = _set_entry_field_value(entry, "cefr_level", level)
        if error:
            await update.message.reply_text(
                error,
                reply_markup=ReplyKeyboardMarkup(EDIT_LEVEL_KEYS, resize_keyboard=True),
            )
            return ADD_EDIT
        context.user_data[entry_key] = updated
        context.user_data["edit_stage"] = "pick_field"
        context.user_data.pop("edit_field", None)
        if source == "quiz":
            update_word_fields(get_db_path(), updated.get("id"), updated)
            return await _show_quiz_prompt_after_edit(update, context, "✅ Updated.")
        return await _show_ai_preview(update, updated)

    field = context.user_data.get("edit_field", "")
    if not field:
        context.user_data["edit_stage"] = "pick_field"
        await update.message.reply_text(
            "Choose field to edit:",
            reply_markup=ReplyKeyboardMarkup(EDIT_FIELDS_KEYS, resize_keyboard=True),
        )
        return ADD_EDIT

    updated, error = _set_entry_field_value(entry, field, text)
    if error:
        await update.message.reply_text(
            error,
            reply_markup=ReplyKeyboardMarkup([[BACK_TEXT, STOP_TEXT]], resize_keyboard=True),
        )
        return ADD_EDIT

    context.user_data[entry_key] = updated
    context.user_data["edit_stage"] = "pick_field"
    context.user_data.pop("edit_field", None)
    if source == "quiz":
        update_word_fields(get_db_path(), updated.get("id"), updated)
        return await _show_quiz_prompt_after_edit(update, context, "✅ Updated.")
    return await _show_ai_preview(update, updated)


async def quiz_ru_de(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == STOP_TEXT:
        return await _back_to_tests(update)
    if text == AI_CLARIFY_FIELD and is_owner(update.effective_user.id):
        return await _start_quiz_edit(update, context, QUIZ_RU_DE)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_ru_de_prompt(update, context, None)

    expected = _get_ru_de_answer(entry)
    ok = _normalize_answer(text) == _normalize_answer(expected)
    _update_srs_for_entry(update.effective_user.id, entry, MODE_RU_DE, ok)

    feedback = _format_feedback(entry, ok)
    return await _send_ru_de_prompt(update, context, feedback)


async def quiz_de_ru(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == STOP_TEXT:
        return await _back_to_tests(update)
    if text == AI_CLARIFY_FIELD and is_owner(update.effective_user.id):
        return await _start_quiz_edit(update, context, QUIZ_DE_RU)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_de_ru_prompt(update, context, None)

    answer = _normalize_answer(text)
    variants = _russian_variants(entry.get("russian", ""))
    ok = answer in variants
    _update_srs_for_entry(update.effective_user.id, entry, MODE_DE_RU, ok)

    feedback = _format_feedback(entry, ok)
    return await _send_de_ru_prompt(update, context, feedback)


async def quiz_article(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == STOP_TEXT:
        return await _back_to_tests(update)
    if text == AI_CLARIFY_FIELD and is_owner(update.effective_user.id):
        return await _start_quiz_edit(update, context, QUIZ_ARTICLE)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_article_prompt(update, context, None)

    expected = entry.get("article", "")
    ok = _normalize_answer(text) == _normalize_answer(expected)
    _update_srs_for_entry(update.effective_user.id, entry, MODE_ARTICLE, ok)

    feedback = _format_feedback(entry, ok)
    return await _send_article_prompt(update, context, feedback)


async def approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_owner(query.from_user.id):
        await query.edit_message_text("Owner only action.")
        return

    data = query.data or ""
    if data.startswith(APPROVE_PREFIX):
        target_id = int(data.replace(APPROVE_PREFIX, "", 1))
        set_user_status(get_db_path(), target_id, "approved")
        await query.edit_message_text(f"Approved user {target_id}")
        try:
            await context.bot.send_message(target_id, "✅ Access approved. Tap Start.")
        except Exception:
            pass
        return

    if data.startswith(REJECT_PREFIX):
        target_id = int(data.replace(REJECT_PREFIX, "", 1))
        set_user_status(get_db_path(), target_id, "rejected")
        await query.edit_message_text(f"Rejected user {target_id}")
        try:
            await context.bot.send_message(target_id, "❌ Access rejected.")
        except Exception:
            pass


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


def _format_feedback(entry, ok):
    status = "Correct ✅" if ok else "Wrong ❌"
    if entry.get("type") == "verb":
        inf = entry.get("infinitive", "")
        pr = entry.get("prateritum", "")
        p2 = entry.get("partizip2", "")
        extras = " / ".join([x for x in [pr, p2] if x])
        if extras:
            extras = f" ({extras})"
        return f"{status} >>> *{_md_escape(inf)}*{_md_escape(extras)}"

    art = entry.get("article", "")
    ger = entry.get("german", "")
    extras = f" ({entry.get('plural')})" if entry.get("type") == "nomen" and entry.get("plural") else ""
    if art:
        return f"{status} >>> {art} *{_md_escape(ger)}*{_md_escape(extras)}"
    return f"{status} >>> *{_md_escape(ger)}*{_md_escape(extras)}"


def _format_question(word):
    if word:
        word = str(word).strip()
        word = word[:1].upper() + word[1:].lower()
    return f">>    *{_md_escape(word)}* ?"


def _pick_due_entry(context, user_id, mode, word_type=None):
    levels = get_user_levels(get_db_path(), user_id)
    due = select_due(get_db_path(), user_id=user_id, mode=mode, word_type=word_type, levels=levels)
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
    entry = _pick_due_entry(context, update.effective_user.id, MODE_RU_DE)
    if not entry:
        await update.message.reply_text(
            "No due words. Add more or wait until due date.",
            reply_markup=ReplyKeyboardMarkup(TESTS_KEYS, resize_keyboard=True),
        )
        return TESTS_MENU

    context.user_data["quiz_entry"] = entry
    prompt_text = _format_question(_normalize_russian_singular(entry.get("russian", "")))
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"

    await update.message.reply_text(prompt_text, reply_markup=_quiz_markup(update.effective_user.id, article=False), parse_mode="Markdown")
    return QUIZ_RU_DE


async def _send_de_ru_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    entry = _pick_due_entry(context, update.effective_user.id, MODE_DE_RU)
    if not entry:
        await update.message.reply_text(
            "No due words. Add more or wait until due date.",
            reply_markup=ReplyKeyboardMarkup(TESTS_KEYS, resize_keyboard=True),
        )
        return TESTS_MENU

    context.user_data["quiz_entry"] = entry
    prompt = entry.get("infinitive", "") if entry.get("type") == "verb" else entry.get("german", "")
    prompt_text = _format_question(prompt)
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"

    await update.message.reply_text(prompt_text, reply_markup=_quiz_markup(update.effective_user.id, article=False), parse_mode="Markdown")
    return QUIZ_DE_RU


async def _send_article_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    entry = _pick_due_entry(context, update.effective_user.id, MODE_ARTICLE, word_type="nomen")
    if not entry:
        await update.message.reply_text(
            "No due nouns. Add more or wait until due date.",
            reply_markup=ReplyKeyboardMarkup(TESTS_KEYS, resize_keyboard=True),
        )
        return TESTS_MENU

    context.user_data["quiz_entry"] = entry
    prompt_text = _format_question(entry.get("german", ""))
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"

    await update.message.reply_text(prompt_text, reply_markup=_quiz_markup(update.effective_user.id, article=True), parse_mode="Markdown")
    return QUIZ_ARTICLE


def _ai_prompt(word, clarification):
    extra = f"\nClarification: {clarification}" if clarification else ""
    return (
        "Return only a JSON object and nothing else. "
        "Detect German part of speech. Allowed types: "
        "nomen, verb, adjektiv, pronomen, adverb, praeposition, konjunktion, partikel. "
        "Set cefr_level from: A1, A2, B1, B2, C1, C2. "
        "For nomen return: type, article, german, plural, russian, cefr_level. "
        "For verb return: type, infinitive, prateritum, partizip2, russian, cefr_level. "
        "For all other allowed types return: type, german, russian, cefr_level. "
        "Use Russian translation in singular only. No extra text."
        f"\nWord: {word}{extra}"
    )


def _validate_ai_entry(entry):
    if not isinstance(entry, dict):
        return False

    entry_type = _type_label(entry.get("type", ""))
    if entry_type not in ALLOWED_AI_TYPES:
        return False

    if entry_type == "nomen":
        return bool(entry.get("german"))
    if entry_type == "verb":
        return bool(entry.get("infinitive"))
    return bool(entry.get("german"))


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
            "cefr_level": cefr_level,
        }

    if entry_type == "nomen":
        return {
            "type": "nomen",
            "article": str(entry.get("article", "")).strip(),
            "german": str(entry.get("german", "")).strip(),
            "plural": str(entry.get("plural", "")).strip(),
            "russian": _normalize_russian_singular(entry.get("russian", "")),
            "cefr_level": cefr_level,
        }

    return {
        "type": entry_type,
        "german": str(entry.get("german", "")).strip(),
        "russian": _normalize_russian_singular(entry.get("russian", "")),
        "cefr_level": cefr_level,
    }


def _format_ai_preview(entry):
    entry_type = entry.get("type")
    level = entry.get("cefr_level", "") or "NO_LEVEL"
    if entry_type == "nomen":
        art = entry.get("article", "")
        ger = entry.get("german", "")
        plural = entry.get("plural", "")
        rus = entry.get("russian", "")
        extras = f" ({plural})" if plural else ""
        return (
            f"Recognized [Nomen {level}]\n"
            f"{art} *{_md_escape(ger)}* - {_md_escape(rus)}{_md_escape(extras)}"
        )

    if entry_type == "verb":
        inf = entry.get("infinitive", "")
        pr = entry.get("prateritum", "")
        p2 = entry.get("partizip2", "")
        rus = entry.get("russian", "")
        extras = " / ".join([x for x in [pr, p2] if x])
        extras = f" ({extras})" if extras else ""
        return (
            f"Recognized [Verb {level}]\n"
            f"*{_md_escape(inf)}* - {_md_escape(rus)}{_md_escape(extras)}"
        )

    ger = entry.get("german", "")
    rus = entry.get("russian", "")
    return f"Recognized [{entry_type} {level}]\n*{_md_escape(ger)}* - {_md_escape(rus)}"


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
            reply_markup=ReplyKeyboardMarkup(ADD_KEYS, resize_keyboard=True),
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
            reply_markup=ReplyKeyboardMarkup(ADD_KEYS, resize_keyboard=True),
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
            f"❌ AI request failed: {exc}",
            reply_markup=ReplyKeyboardMarkup([[STOP_TEXT]], resize_keyboard=True),
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
                f"❌ AI failed to parse for word: {word}\n"
                f"Clarification: {clarification or '-'}\n"
                "Use Clarify AI or Clarify Field."
            ),
            reply_markup=ReplyKeyboardMarkup([[STOP_TEXT]], resize_keyboard=True),
        )
        return ADD_CLARIFY

    entry = _normalize_ai_entry(entry)
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


async def _stop_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("add_queue", None)
    context.user_data.pop("pending_word", None)
    context.user_data.pop("pending_entry", None)
    context.user_data.pop("edit_stage", None)
    context.user_data.pop("edit_field", None)
    context.user_data.pop("edit_source", None)
    context.user_data.pop("edit_return_state", None)
    await update.message.reply_text(
        "❌ Stopped.",
        reply_markup=_main_menu_markup(update.effective_user.id),
    )
    return MENU


async def _back_to_main_menu(update: Update):
    await update.message.reply_text(
        "Main menu:",
        reply_markup=_main_menu_markup(update.effective_user.id),
    )
    return MENU


async def _back_to_tests(update: Update):
    await update.message.reply_text(
        "Tests:",
        reply_markup=ReplyKeyboardMarkup(TESTS_KEYS, resize_keyboard=True),
    )
    return TESTS_MENU


def _try_parse_json(text):
    candidate = _extract_json(text)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        opens = candidate.count("{")
        closes = candidate.count("}")
        if opens > closes:
            fixed = candidate + ("}" * (opens - closes))
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                return None
        return None


def _extract_json(text):
    start = text.find("{")
    end = text.rfind("}")
    if start == -1:
        return text
    if end == -1:
        return text[start:]
    return text[start : end + 1]


def _update_srs_for_entry(user_id, entry, mode, ok):
    quality = 5 if ok else 2
    update_progress(get_db_path(), user_id=user_id, word_id=entry.get("id"), mode=mode, quality=quality)


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
            await context.bot.send_message(owner_id, f"Auto dump created: {dump_path.name}")
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


async def _forward_text_to_owner(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str):
    owner_id = get_owner_id()
    if owner_id == 0:
        return

    user = update.effective_user
    info = (
        "User submitted words:\n"
        f"user_id: {user.id}\n"
        f"username: @{user.username if user.username else '-'}\n"
        f"name: {user.first_name if user.first_name else '-'}"
    )
    try:
        await context.bot.send_message(owner_id, info)
        await context.bot.send_message(owner_id, raw_text)
    except Exception:
        pass


async def _forward_document_to_owner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    owner_id = get_owner_id()
    if owner_id == 0:
        return

    user = update.effective_user
    info = (
        "User submitted file:\n"
        f"user_id: {user.id}\n"
        f"username: @{user.username if user.username else '-'}\n"
        f"name: {user.first_name if user.first_name else '-'}"
    )
    try:
        await context.bot.send_message(owner_id, info)
        await context.bot.forward_message(owner_id, update.effective_chat.id, update.message.message_id)
    except Exception:
        pass


def _prepare_storage():
    db_path = get_db_path()
    init_db(db_path)

    legacy_json_path = get_legacy_json_path()
    if legacy_json_path.suffix.lower() == ".json":
        migrate_json_to_sqlite(legacy_json_path, db_path)

    owner_id = get_owner_id()
    if owner_id:
        set_user_status(db_path, owner_id, "approved")


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

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex(f"^{START_TEXT}$") & ~filters.COMMAND, start),
        ],
        states={
            MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_choice)],
            TESTS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, tests_menu_choice)],
            SETTINGS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_menu_choice)],
            AUTO_DUMP_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, auto_dump_choice)],
            LEVELS_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, levels_choice)],
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
    app.add_handler(CallbackQueryHandler(approve_callback, pattern=f"^({APPROVE_PREFIX}|{REJECT_PREFIX}).+"))
    app.run_polling()


if __name__ == "__main__":
    main()
