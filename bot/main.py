
import json
import os
import random
import re
import shutil
from datetime import datetime, time, timezone
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
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
    QUIZ_ARTICLE as MODE_ARTICLE,
    QUIZ_DE_RU as MODE_DE_RU,
    QUIZ_RU_DE as MODE_RU_DE,
    add_word,
    count_words,
    find_existing_word,
    get_setting,
    get_user_status,
    init_db,
    migrate_json_to_sqlite,
    register_or_update_user,
    select_due,
    set_setting,
    set_user_status,
    update_progress,
)

(
    MENU,
    TESTS_MENU,
    SETTINGS_MENU,
    AUTO_DUMP_MENU,
    ADD_INPUT,
    ADD_CONFIRM,
    ADD_CLARIFY,
    QUIZ_RU_DE,
    QUIZ_DE_RU,
    QUIZ_ARTICLE,
) = range(10)

BACK_TEXT = "Back"
STOP_TEXT = "Stop"
START_TEXT = "Start"
AI_CONFIRM = "Confirm"
AI_CLARIFY = "Clarify"
AI_REJECT = "Reject"

OWNER_MAIN_KEYS = [["Add Word", "Tests"], ["Settings", "Stat"]]
USER_MAIN_KEYS = [["Add Word", "Tests"], ["Info", "Stat"]]
WAIT_KEYS = [[START_TEXT]]
ADD_KEYS = [[BACK_TEXT]]
AI_KEYS = [[AI_CONFIRM, AI_CLARIFY, AI_REJECT], [STOP_TEXT]]
TESTS_KEYS = [["Quiz RU -> DE", "Quiz DE -> RU"], ["Quiz article", BACK_TEXT]]
SETTINGS_KEYS = [["Dump DB", "Auto Dump"], [BACK_TEXT]]
AUTO_DUMP_KEYS = [["Daily", "Weekly"], ["Off", BACK_TEXT]]
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
    "funf",
    "fuenf",
    "funfzehn",
    "funfzig",
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
    "zwolf",
    "zwoelf",
    "zwolf",
    "dreizehn",
    "vierzehn",
    "sechzehn",
    "siebzehn",
    "achtzehn",
    "neunzehn",
    "zwanzig",
    "dreissig",
    "drei?ig",
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
    "praposition",
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


def _normalize_answer(text):
    return " ".join(text.strip().lower().split())


def _normalize_lookup_text(text):
    return " ".join(str(text).strip().lower().split())


def _normalize_russian_singular(text):
    text = str(text).strip()
    if "/" in text:
        text = text.split("/", 1)[0].strip()
    return text


def _md_escape(text):
    text = str(text or "")
    for ch in ("*", "_", "`"):
        text = text.replace(ch, f"\\{ch}")
    return text


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
        return "praposition"
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
            caption="SQLite dump created.",
        )
        return SETTINGS_MENU

    if text == "Auto Dump":
        mode = get_setting(get_db_path(), AUTO_DUMP_SETTING_KEY, "off")
        await update.message.reply_text(
            f"Current auto dump mode: {mode}",
            reply_markup=ReplyKeyboardMarkup(AUTO_DUMP_KEYS, resize_keyboard=True),
        )
        return AUTO_DUMP_MENU

    await update.message.reply_text(
        "Use settings buttons.",
        reply_markup=ReplyKeyboardMarkup(SETTINGS_KEYS, resize_keyboard=True),
    )
    return SETTINGS_MENU


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
        f"Auto dump mode set: {mode}",
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
            "Successfully sent to the bot Owner",
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
        context.user_data["add_queue"] = lines
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
            "Successfully sent to the bot Owner",
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

    context.user_data["add_queue"] = lines
    return await _process_next_ai_word(update, context)


async def add_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == STOP_TEXT:
        return await _stop_queue(update, context)

    if text == AI_CLARIFY:
        await update.message.reply_text(
            "Send clarification text.",
            reply_markup=ReplyKeyboardMarkup([[STOP_TEXT]], resize_keyboard=True),
        )
        return ADD_CLARIFY

    if text == AI_REJECT:
        return await _process_next_ai_word(update, context)

    if text != AI_CONFIRM:
        await update.message.reply_text(
            "Use the buttons: Confirm, Clarify, or Reject.",
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


async def quiz_ru_de(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == STOP_TEXT:
        return await _back_to_tests(update)

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

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_de_ru_prompt(update, context, None)

    expected = _normalize_russian_singular(entry.get("russian", ""))
    ok = _normalize_answer(text) == _normalize_answer(expected)
    _update_srs_for_entry(update.effective_user.id, entry, MODE_DE_RU, ok)

    feedback = _format_feedback(entry, ok)
    return await _send_de_ru_prompt(update, context, feedback)


async def quiz_article(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == STOP_TEXT:
        return await _back_to_tests(update)

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
            await context.bot.send_message(target_id, "Access approved. Tap Start.")
        except Exception:
            pass
        return

    if data.startswith(REJECT_PREFIX):
        target_id = int(data.replace(REJECT_PREFIX, "", 1))
        set_user_status(get_db_path(), target_id, "rejected")
        await query.edit_message_text(f"Rejected user {target_id}")
        try:
            await context.bot.send_message(target_id, "Access rejected.")
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

    await update.message.reply_text(f"Saved: {saved.get('id')}", reply_markup=_main_menu_markup(update.effective_user.id))
    return saved


def _get_ru_de_answer(entry):
    if entry.get("type") == "verb":
        return entry.get("infinitive", "")
    return entry.get("german", "")


def _format_feedback(entry, ok):
    status = "Correct ?" if ok else "Wrong ?"
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

    await update.message.reply_text(prompt_text, reply_markup=ReplyKeyboardMarkup(QUIZ_KEYS, resize_keyboard=True), parse_mode="Markdown")
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

    await update.message.reply_text(prompt_text, reply_markup=ReplyKeyboardMarkup(QUIZ_KEYS, resize_keyboard=True), parse_mode="Markdown")
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

    await update.message.reply_text(prompt_text, reply_markup=ReplyKeyboardMarkup(ARTICLE_KEYS, resize_keyboard=True), parse_mode="Markdown")
    return QUIZ_ARTICLE


def _ai_prompt(word, clarification):
    extra = f"\nClarification: {clarification}" if clarification else ""
    return (
        "Return only a JSON object and nothing else. "
        "Detect German part of speech. Allowed types: "
        "nomen, verb, adjektiv, pronomen, adverb, praposition, konjunktion, partikel. "
        "For nomen return: type, article, german, plural, russian. "
        "For verb return: type, infinitive, prateritum, partizip2, russian. "
        "For all other allowed types return: type, german, russian. "
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

    if entry_type == "verb":
        return {
            "type": "verb",
            "infinitive": str(entry.get("infinitive", "")).strip(),
            "prateritum": str(entry.get("prateritum", "")).strip(),
            "partizip2": str(entry.get("partizip2", "")).strip(),
            "russian": _normalize_russian_singular(entry.get("russian", "")),
        }

    if entry_type == "nomen":
        return {
            "type": "nomen",
            "article": str(entry.get("article", "")).strip(),
            "german": str(entry.get("german", "")).strip(),
            "plural": str(entry.get("plural", "")).strip(),
            "russian": _normalize_russian_singular(entry.get("russian", "")),
        }

    return {
        "type": entry_type,
        "german": str(entry.get("german", "")).strip(),
        "russian": _normalize_russian_singular(entry.get("russian", "")),
    }


def _format_ai_preview(entry):
    entry_type = entry.get("type")
    if entry_type == "nomen":
        art = entry.get("article", "")
        ger = entry.get("german", "")
        plural = entry.get("plural", "")
        rus = entry.get("russian", "")
        extras = f" ({plural})" if plural else ""
        return "Recognized [Nomen]\n" f"{art} *{_md_escape(ger)}* - {_md_escape(rus)}{_md_escape(extras)}"

    if entry_type == "verb":
        inf = entry.get("infinitive", "")
        pr = entry.get("prateritum", "")
        p2 = entry.get("partizip2", "")
        rus = entry.get("russian", "")
        extras = " / ".join([x for x in [pr, p2] if x])
        extras = f" ({extras})" if extras else ""
        return "Recognized [Verb]\n" f"*{_md_escape(inf)}* - {_md_escape(rus)}{_md_escape(extras)}"

    ger = entry.get("german", "")
    rus = entry.get("russian", "")
    return f"Recognized [{entry_type}]\n*{_md_escape(ger)}* - {_md_escape(rus)}"


async def _process_ai_word(update: Update, context: ContextTypes.DEFAULT_TYPE, word, clarification):
    has_queue = bool(context.user_data.get("add_queue"))

    reject_reason = _prefilter_reject_reason(word)
    if reject_reason:
        await update.message.reply_text(
            f"{reject_reason} Skipped: {word}",
            reply_markup=(
                ReplyKeyboardMarkup([[STOP_TEXT]], resize_keyboard=True)
                if has_queue
                else ReplyKeyboardMarkup(ADD_KEYS, resize_keyboard=True)
            ),
        )
        if has_queue:
            return await _process_next_ai_word(update, context)
        return ADD_INPUT

    existing = find_existing_word(get_db_path(), word)
    if existing:
        existing_type = existing.get("type", "word")
        existing_value = existing.get("infinitive", "") if existing_type == "verb" else existing.get("german", "")
        await update.message.reply_text(
            f"Already in database: [{existing_type}] {existing_value}",
            reply_markup=(
                ReplyKeyboardMarkup([[STOP_TEXT]], resize_keyboard=True)
                if has_queue
                else ReplyKeyboardMarkup(ADD_KEYS, resize_keyboard=True)
            ),
        )
        if has_queue:
            return await _process_next_ai_word(update, context)
        return ADD_INPUT

    if not os.getenv("OPENAI_API_KEY"):
        await update.message.reply_text(
            "AI is not configured. Set OPENAI_API_KEY in .env.",
            reply_markup=_main_menu_markup(update.effective_user.id),
        )
        return MENU

    context.user_data["pending_word"] = word
    response_text = generate_fields(_ai_prompt(word, clarification))
    entry = _try_parse_json(response_text)
    if entry is None or not _validate_ai_entry(entry):
        await update.message.reply_text(
            "AI failed to parse. Send clarification text or Stop.",
            reply_markup=ReplyKeyboardMarkup([[STOP_TEXT]], resize_keyboard=True),
        )
        return ADD_CLARIFY

    entry = _normalize_ai_entry(entry)
    context.user_data["pending_entry"] = entry
    return await _show_ai_preview(update, entry)


async def _show_ai_preview(update: Update, entry):
    preview = _format_ai_preview(entry)
    await update.message.reply_text(
        preview,
        reply_markup=ReplyKeyboardMarkup(AI_KEYS, resize_keyboard=True),
        parse_mode="Markdown",
    )
    return ADD_CONFIRM


async def _process_next_ai_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    queue = context.user_data.get("add_queue") or []
    if not queue:
        context.user_data.pop("pending_word", None)
        context.user_data.pop("pending_entry", None)
        await update.message.reply_text(
            "Done.",
            reply_markup=_main_menu_markup(update.effective_user.id),
        )
        return MENU

    word = queue.pop(0)
    context.user_data["add_queue"] = queue
    return await _process_ai_word(update, context, word, None)


async def _stop_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("add_queue", None)
    context.user_data.pop("pending_word", None)
    context.user_data.pop("pending_entry", None)
    await update.message.reply_text(
        "Stopped.",
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
            ADD_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_input),
                MessageHandler(filters.Document.ALL, add_document),
            ],
            ADD_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_confirm)],
            ADD_CLARIFY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_clarify)],
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
