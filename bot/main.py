import json
import os
import random
from pathlib import Path

from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .ai import generate_fields
from .storage import (
    add_word,
    create_template_from_word,
    load_words,
    save_words,
    select_due,
    update_sm2,
)

MENU, ADD_INPUT, ADD_CONFIRM, ADD_CLARIFY, QUIZ_RU_DE, QUIZ_DE_RU, QUIZ_ARTICLE = range(7)
BACK_TEXT = "Back"
STOP_TEXT = "Stop"
CONFIRM_ADD = "Add"
CONFIRM_CANCEL = "Cancel"
AI_CONFIRM = "Confirm"
AI_CLARIFY = "Clarify"
AI_REJECT = "Reject"

MENU_KEYS = [
    ["Add word", "Export data"],
    ["Quiz RU -> DE", "Quiz DE -> RU"],
    ["Quiz article", "Stats"],
]

ADD_KEYS = [[BACK_TEXT]]
CONFIRM_KEYS = [[CONFIRM_ADD, CONFIRM_CANCEL]]
QUIZ_KEYS = [[STOP_TEXT]]
ARTICLE_KEYS = [["der", "die", "das"], [STOP_TEXT]]
AI_KEYS = [[AI_CONFIRM, AI_CLARIFY, AI_REJECT], [STOP_TEXT]]


def get_data_path():
    return Path(os.getenv("DATA_PATH", "./data/words.json"))


def is_owner(user_id):
    try:
        owner_id = int(os.getenv("OWNER_ID", "0"))
    except ValueError:
        owner_id = 0
    return owner_id != 0 and user_id == owner_id


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply_markup = ReplyKeyboardMarkup(MENU_KEYS, resize_keyboard=True)
    await update.message.reply_text(
        "Hi! Choose a mode:",
        reply_markup=reply_markup,
    )
    return MENU


async def menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id

    if text == "Add word":
        if not is_owner(user_id):
            await update.message.reply_text(
                "Only the owner can add words.",
                reply_markup=ReplyKeyboardMarkup(MENU_KEYS, resize_keyboard=True),
            )
            return MENU
        await update.message.reply_text(
            "Send a word or a list (one per line), or upload a .txt file.",
            reply_markup=ReplyKeyboardMarkup(ADD_KEYS, resize_keyboard=True),
        )
        return ADD_INPUT

    if text == "Quiz RU -> DE":
        return await _start_ru_de_quiz(update, context)

    if text == "Quiz DE -> RU":
        return await _start_de_ru_quiz(update, context)

    if text == "Quiz article":
        return await _start_article_quiz(update, context)

    if text == "Export data":
        data_path = get_data_path()
        if not data_path.exists():
            await update.message.reply_text("No data file found yet.")
            return MENU
        await update.message.reply_document(
            document=data_path.open("rb"),
            filename=data_path.name,
            caption="Here is your words.json dump.",
        )
        return MENU

    if text == "Stats":
        data_path = get_data_path()
        words = load_words(data_path)
        await update.message.reply_text(
            f"Total words: {len(words)}",
            reply_markup=ReplyKeyboardMarkup(MENU_KEYS, resize_keyboard=True),
        )
        return MENU

    await update.message.reply_text("Unknown command. Use the menu buttons.")
    return MENU


async def add_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if raw == BACK_TEXT:
        return await _back_to_menu(update)

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
        await update.message.reply_text(
            "Nothing to save.",
            reply_markup=ReplyKeyboardMarkup(MENU_KEYS, resize_keyboard=True),
        )
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
        await update.message.reply_text(
            "No word to clarify.",
            reply_markup=ReplyKeyboardMarkup(MENU_KEYS, resize_keyboard=True),
        )
        return MENU

    return await _process_ai_word(update, context, word, text)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Ok, canceled.",
        reply_markup=ReplyKeyboardMarkup(MENU_KEYS, resize_keyboard=True),
    )
    return MENU


async def quiz_ru_de(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == STOP_TEXT:
        return await _back_to_menu(update)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_ru_de_prompt(update, context, None)

    expected = _get_ru_de_answer(entry)
    ok = _normalize_answer(text) == _normalize_answer(expected)

    _update_srs_for_entry(entry, ok)

    feedback = _format_feedback(entry, ok)
    return await _send_ru_de_prompt(update, context, feedback)


async def quiz_de_ru(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == STOP_TEXT:
        return await _back_to_menu(update)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_de_ru_prompt(update, context, None)

    expected = _normalize_russian_singular(entry.get("russian", ""))
    ok = _normalize_answer(text) == _normalize_answer(expected)

    _update_srs_for_entry(entry, ok)

    feedback = _format_feedback(entry, ok)
    return await _send_de_ru_prompt(update, context, feedback)


async def quiz_article(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == STOP_TEXT:
        return await _back_to_menu(update)

    entry = context.user_data.get("quiz_entry")
    if not entry:
        return await _send_article_prompt(update, context, None)

    expected = entry.get("article", "")
    ok = _normalize_answer(text) == _normalize_answer(expected)

    _update_srs_for_entry(entry, ok)

    feedback = _format_feedback(entry, ok)
    return await _send_article_prompt(update, context, feedback)


async def _save_entry(update: Update, entry):
    data_path = get_data_path()
    words = load_words(data_path)
    if "russian" in entry:
        entry["russian"] = _normalize_russian_singular(entry["russian"])
    try:
        saved = add_word(words, entry)
    except ValueError as exc:
        await update.message.reply_text(
            str(exc),
            reply_markup=ReplyKeyboardMarkup(MENU_KEYS, resize_keyboard=True),
        )
        return None
    save_words(data_path, words)

    await update.message.reply_text(
        f"Saved: {saved.get('id')}",
        reply_markup=ReplyKeyboardMarkup(MENU_KEYS, resize_keyboard=True),
    )
    return saved


def _get_ru_de_answer(entry):
    if entry.get("type") == "verb":
        return entry.get("infinitive", "")
    return entry.get("german", "")


def _normalize_answer(text):
    return " ".join(text.strip().lower().split())


def _normalize_lookup_text(text):
    return " ".join(str(text).strip().lower().split())


def _normalize_russian_singular(text):
    text = text.strip()
    if "/" in text:
        text = text.split("/", 1)[0].strip()
    return text


def _md_escape(text):
    for ch in ("*", "_", "`"):
        text = text.replace(ch, f"\\{ch}")
    return text


async def _start_ru_de_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _send_ru_de_prompt(update, context, None)


async def _start_de_ru_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _send_de_ru_prompt(update, context, None)


async def _start_article_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _send_article_prompt(update, context, None)


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
    plural = entry.get("plural", "")
    extras = f" ({plural})" if plural else ""
    if art:
        return f"{status} >>> {art} *{_md_escape(ger)}*{_md_escape(extras)}"
    return f"{status} >>> *{_md_escape(ger)}*{_md_escape(extras)}"


def _format_question(word):
    if word:
        word = word.strip()
        word = word[:1].upper() + word[1:].lower()
    return f">>    *{_md_escape(word)}* ?"


def _pick_due_entry(context, words, predicate=None):
    due = select_due(words)
    if predicate:
        due = [w for w in due if predicate(w)]
    if not due:
        return None
    last_id = context.user_data.get("last_quiz_id")
    pool = [w for w in due if w.get("id") != last_id] if len(due) > 1 else due
    if not pool:
        pool = due
    entry = random.choice(pool)
    context.user_data["last_quiz_id"] = entry.get("id")
    return entry


async def _send_ru_de_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    data_path = get_data_path()
    words = load_words(data_path)
    entry = _pick_due_entry(context, words)
    if not entry:
        await update.message.reply_text(
            "No due words. Add more or wait until due date.",
            reply_markup=ReplyKeyboardMarkup(MENU_KEYS, resize_keyboard=True),
        )
        return MENU

    context.user_data["quiz_entry"] = entry
    prompt = _normalize_russian_singular(entry.get("russian", ""))
    prompt_text = _format_question(prompt)
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"

    await update.message.reply_text(
        prompt_text,
        reply_markup=ReplyKeyboardMarkup(QUIZ_KEYS, resize_keyboard=True),
        parse_mode="Markdown",
    )
    return QUIZ_RU_DE


async def _send_de_ru_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    data_path = get_data_path()
    words = load_words(data_path)
    entry = _pick_due_entry(context, words)
    if not entry:
        await update.message.reply_text(
            "No due words. Add more or wait until due date.",
            reply_markup=ReplyKeyboardMarkup(MENU_KEYS, resize_keyboard=True),
        )
        return MENU

    context.user_data["quiz_entry"] = entry
    if entry.get("type") == "verb":
        prompt = entry.get("infinitive", "")
    else:
        prompt = entry.get("german", "")
    prompt_text = _format_question(prompt)
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"

    await update.message.reply_text(
        prompt_text,
        reply_markup=ReplyKeyboardMarkup(QUIZ_KEYS, resize_keyboard=True),
        parse_mode="Markdown",
    )
    return QUIZ_DE_RU


async def _send_article_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text):
    data_path = get_data_path()
    words = load_words(data_path)
    entry = _pick_due_entry(context, words, predicate=lambda w: w.get("type") == "nomen")
    if not entry:
        await update.message.reply_text(
            "No due nouns. Add more or wait until due date.",
            reply_markup=ReplyKeyboardMarkup(MENU_KEYS, resize_keyboard=True),
        )
        return MENU

    context.user_data["quiz_entry"] = entry
    prompt = entry.get("german", "")
    prompt_text = _format_question(prompt)
    if prefix_text:
        prompt_text = f"{prefix_text}\n\n{prompt_text}"

    await update.message.reply_text(
        prompt_text,
        reply_markup=ReplyKeyboardMarkup(ARTICLE_KEYS, resize_keyboard=True),
        parse_mode="Markdown",
    )
    return QUIZ_ARTICLE


def _ai_prompt(word, clarification):
    extra = f"\nClarification: {clarification}" if clarification else ""
    return (
        "Return only a JSON object and nothing else. "
        "Detect whether the German word is a noun or a verb. "
        "For noun, return keys: type='nomen', article, german, plural, russian. "
        "For verb, return keys: type='verb', infinitive, prateritum, partizip2, russian. "
        "Use Russian translation in singular only. No extra text."
        f"\nWord: {word}{extra}"
    )


def _validate_ai_entry(entry):
    if not isinstance(entry, dict):
        return False
    entry_type = entry.get("type")
    if entry_type == "nomen":
        return bool(entry.get("german"))
    if entry_type == "verb":
        return bool(entry.get("infinitive"))
    return False


def _find_existing_word(words, raw_word):
    candidate = _normalize_lookup_text(raw_word)
    if not candidate:
        return None

    for entry in words:
        if _normalize_lookup_text(entry.get("german", "")) == candidate:
            return entry
        if _normalize_lookup_text(entry.get("infinitive", "")) == candidate:
            return entry
    return None


def _format_ai_preview(entry):
    entry_type = entry.get("type")
    if entry_type == "nomen":
        art = entry.get("article", "")
        ger = entry.get("german", "")
        plural = entry.get("plural", "")
        rus = entry.get("russian", "")
        extras = f" ({plural})" if plural else ""
        return (
            "Recognized [Nomen]\n"
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
            "Recognized [Verb]\n"
            f"*{_md_escape(inf)}* - {_md_escape(rus)}{_md_escape(extras)}"
        )
    return "AI returned unknown type."


async def _process_ai_word(update: Update, context: ContextTypes.DEFAULT_TYPE, word, clarification):
    data_path = get_data_path()
    words = load_words(data_path)
    existing = _find_existing_word(words, word)
    if existing:
        existing_type = existing.get("type", "word")
        existing_value = (
            existing.get("infinitive", "") if existing_type == "verb" else existing.get("german", "")
        )
        has_queue = bool(context.user_data.get("add_queue"))
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
            reply_markup=ReplyKeyboardMarkup(MENU_KEYS, resize_keyboard=True),
        )
        return MENU

    context.user_data["pending_word"] = word
    prompt = _ai_prompt(word, clarification)
    response_text = generate_fields(prompt)
    entry = _try_parse_json(response_text)
    if entry is None or not _validate_ai_entry(entry):
        await update.message.reply_text(
            "AI failed to parse. Send clarification text or Stop.",
            reply_markup=ReplyKeyboardMarkup([[STOP_TEXT]], resize_keyboard=True),
        )
        return ADD_CLARIFY

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
            reply_markup=ReplyKeyboardMarkup(MENU_KEYS, resize_keyboard=True),
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
        reply_markup=ReplyKeyboardMarkup(MENU_KEYS, resize_keyboard=True),
    )
    return MENU


async def _back_to_menu(update: Update):
    await update.message.reply_text(
        "Back to menu.",
        reply_markup=ReplyKeyboardMarkup(MENU_KEYS, resize_keyboard=True),
    )
    return MENU


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


def _update_srs_for_entry(entry, ok):
    data_path = get_data_path()
    words = load_words(data_path)
    for w in words:
        if w.get("id") == entry.get("id"):
            quality = 5 if ok else 2
            w["srs"] = update_sm2(w.get("srs", {}), quality)
            break
    save_words(data_path, words)


def main():
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN is missing in .env")

    app = Application.builder().token(token).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_choice)],
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
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    app.run_polling()


if __name__ == "__main__":
    main()
