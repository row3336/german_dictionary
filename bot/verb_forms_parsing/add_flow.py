"""VF-add UI flow (spec #2, Step 3d).

Owner picks "VF add" -> sends a word list -> a persisted job starts in the
background. We send a pre-start message (est. time + traffic + via proxy) and a
live progress message with inline buttons (Pause/Resume, Stop, Details). The
runner (job_runner.run_job) does the slow-drip work; here we only build the
messages/keyboard and translate button taps into job-status changes.

Pure helpers (estimates, message text, keyboard, callback->status) are unit
tested; the async handlers wire them to Telegram.
"""

import asyncio
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..app_config import MENU, VF_ADD_INPUT, get_db_path
from ..common.navigation import _action_key_from_text, _back_to_main_menu, _reply_text_with_retry
from ..common.settings import _current_topic_value
from ..ui.keyboards import _add_menu_markup
from .job_runner import DELAY_RANGE, VF_ADD_TASKS, run_job
from .job_store import create_job, get_job, job_counts, list_items, set_job_status

# Rough per-word estimates, shown with a "≈" so no false precision. Average of a
# single summary fetch and a verb's summary+conjugation, plus the slow-drip delay.
_AVG_SEC_PER_WORD = (sum(DELAY_RANGE) // 2) + 10
_AVG_KB_PER_WORD = 60

# job_id -> live UI state {chat_id, message_id, details} so callbacks can re-render.
VF_ADD_UI = {}

CALLBACK_STATUS = {"pause": "paused", "resume": "running", "stop": "stopped"}


def parse_word_list(text):
    """Split an owner word list into clean query strings (drop a trailing [type])."""
    out = []
    for line in str(text or "").splitlines():
        word = re.sub(r"\s*\[[a-zA-Z_]+\]\s*$", "", line).strip()
        if word:
            out.append(word)
    return out


def estimate_eta_seconds(n):
    return int(n) * _AVG_SEC_PER_WORD


def estimate_traffic_kb(n):
    return int(n) * _AVG_KB_PER_WORD


def _fmt_duration(sec):
    minutes = max(1, round(sec / 60))
    if minutes < 60:
        return f"~{minutes} min"
    return f"~{minutes // 60}h {minutes % 60}m"


def _fmt_traffic(kb):
    return f"~{kb / 1024:.1f} MB" if kb >= 1024 else f"~{kb} KB"


def prestart_text(n):
    return (
        f"🌐 VF parsing queued: {n} word(s).\n"
        f"Est. time {_fmt_duration(estimate_eta_seconds(n))} · "
        f"est. traffic {_fmt_traffic(estimate_traffic_kb(n))} · via proxy.\n"
        "You'll get a file to review when it's done."
    )


def progress_text(job, counts, details_items=None):
    """Progress line(s) for the live message. #parsingVF tag + %/ETA; optional
    per-word detail when Details is toggled on."""
    total = counts["total"]
    processed = counts["done"] + counts["error"] + counts["skipped"]
    pct = int(processed * 100 / total) if total else 100
    eta = _fmt_duration(estimate_eta_seconds(counts["pending"]))
    lines = [
        f"#parsingVF  · {job['status']}",
        f"{processed}/{total} ({pct}%) · errors {counts['error']} · ETA {eta}",
    ]
    if details_items is not None:
        lines.append("")
        for it in details_items[-15:]:
            if it["status"] == "pending":
                continue
            mark = {"done": "✅", "error": "❌", "skipped": "⏭"}.get(it["status"], "•")
            note = ""
            if it["status"] == "error":
                note = f" — {it['error'][:40]}"
            elif it.get("flags"):
                note = f" — ⚠ {it['flags']}"
            lines.append(f"{mark} {it['word']}{note}")
    return "\n".join(lines)


def progress_keyboard(job):
    jid = job["id"]
    paused = job["status"] == "paused"
    toggle = InlineKeyboardButton(
        "▶️ Resume" if paused else "⏸ Pause",
        callback_data=f"vfadd:{'resume' if paused else 'pause'}:{jid}",
    )
    return InlineKeyboardMarkup(
        [
            [toggle, InlineKeyboardButton("⏹ Stop", callback_data=f"vfadd:stop:{jid}")],
            [InlineKeyboardButton("🔎 Details", callback_data=f"vfadd:details:{jid}")],
        ]
    )


def _render(db_path, job_id):
    """(text, keyboard) for a job's current state, honoring the Details toggle."""
    job = get_job(db_path, job_id)
    if not job:
        return None, None
    counts = job_counts(db_path, job_id)
    ui = VF_ADD_UI.get(job_id, {})
    items = list_items(db_path, job_id) if ui.get("details") else None
    return progress_text(job, counts, details_items=items), progress_keyboard(job)


async def _make_progress_cb(bot, chat_id, job_id):
    async def on_progress(job, counts, done):
        text, kb = _render(get_db_path(), job_id)
        if text is None:
            return
        ui = VF_ADD_UI.get(job_id, {})
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=ui.get("message_id"), reply_markup=kb)
        except Exception:
            pass  # a "message not modified" / transient edit error must not kill the job
        if done:
            await bot.send_message(chat_id=chat_id, text=f"✅ VF parsing finished ({counts['done']} ok, {counts['error']} errors). File next (step 4/5).")
            VF_ADD_TASKS.pop(job_id, None)

    return on_progress


async def start_vf_add_job(update: Update, context: ContextTypes.DEFAULT_TYPE, words):
    """Create + launch a VF-add job from a word list; wire the live progress msg."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    db_path = get_db_path()
    topic = _current_topic_value(user_id)
    job_id = create_job(db_path, user_id, chat_id, topic, words)

    await _reply_text_with_retry(update.message, prestart_text(len(words)))
    text, kb = _render(db_path, job_id)
    msg = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
    VF_ADD_UI[job_id] = {"chat_id": chat_id, "message_id": msg.message_id, "details": False}

    on_progress = await _make_progress_cb(context.bot, chat_id, job_id)
    VF_ADD_TASKS[job_id] = asyncio.create_task(run_job(db_path, job_id, on_progress=on_progress))
    return MENU


async def vf_add_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry from the Add submenu 'VF add' button: ask for the word list."""
    await _reply_text_with_retry(
        update.message,
        "🌐 Send words for VF parsing (one per line), or upload a .txt file.",
        reply_markup=_add_menu_markup(update.effective_user.id),
    )
    return VF_ADD_INPUT


async def vf_add_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive the word list (text) and launch the job."""
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    if _action_key_from_text(text, user_id) == "back":
        return await _back_to_main_menu(update)
    words = parse_word_list(text)
    if not words:
        await _reply_text_with_retry(
            update.message,
            "Empty list. Send words (one per line) or press back.",
            reply_markup=_add_menu_markup(user_id),
        )
        return VF_ADD_INPUT
    return await start_vf_add_job(update, context, words)


async def vf_add_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive a .txt word list and launch the job."""
    doc = update.message.document
    if not doc or not doc.file_name.lower().endswith(".txt"):
        await update.message.reply_text("Please upload a .txt file.")
        return VF_ADD_INPUT
    file = await doc.get_file()
    data = await file.download_as_bytearray()
    words = parse_word_list(data.decode("utf-8", errors="ignore"))
    if not words:
        await update.message.reply_text("File is empty.")
        return VF_ADD_INPUT
    return await start_vf_add_job(update, context, words)


async def vf_add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inline button handler: pause/resume/stop/details for a running job."""
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        return
    _, action, job_id_raw = parts
    try:
        job_id = int(job_id_raw)
    except ValueError:
        return
    db_path = get_db_path()
    if action in CALLBACK_STATUS:
        set_job_status(db_path, job_id, CALLBACK_STATUS[action])
    elif action == "details":
        ui = VF_ADD_UI.setdefault(job_id, {})
        ui["details"] = not ui.get("details")
    text, kb = _render(db_path, job_id)
    if text is None:
        return
    try:
        await query.edit_message_text(text, reply_markup=kb)
    except Exception:
        pass
