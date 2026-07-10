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

from telegram import ReplyKeyboardMarkup

from ..app_config import MENU, VF_ADD_INPUT, VF_APPLY_CONFIRM, get_db_path, get_dump_dir
from ..common.db_safety import backup_db_with_event
from ..common.navigation import _action_key_from_text, _back_to_main_menu, _reply_text_with_retry
from ..common.settings import _current_topic_value
from ..i18n import t
from ..common.lang import _user_interface_lang
from ..storage import (
    add_word,
    get_word_by_id,
    list_words_for_vf_recheck,
    reset_vf_checks,
    set_word_conjugation,
    update_word_fields,
)
from ..ui.keyboards import _add_menu_markup, _main_menu_markup
from .job_runner import DELAY_RANGE, VF_ADD_TASKS, run_job
from .job_store import (
    active_jobs,
    create_job,
    create_recheck_job,
    get_job,
    job_counts,
    list_items,
    set_job_status,
)
from .vf_file import (
    build_review_document,
    parse_review_document,
    render_apply_preview_json,
    render_review_json,
    review_filename,
)

# Rough per-word estimates, shown with a "≈" so no false precision. Average of a
# single summary fetch and a verb's summary+conjugation, plus the slow-drip delay.
_AVG_SEC_PER_WORD = (sum(DELAY_RANGE) // 2) + 10
_AVG_KB_PER_WORD = 60

# job_id -> live UI state {chat_id, message_id, details} so callbacks can re-render.
VF_ADD_UI = {}

CALLBACK_STATUS = {"pause": "paused", "resume": "running", "stop": "stopped"}


# DB word types the owner may pin with a trailing [type]; anything else is treated
# as part of the word. A pinned type routes the lookup (function words -> their
# verben.de type page) and gates the parse against a homonym card.
VALID_TYPE_HINTS = frozenset(
    {"nomen", "verb", "adjektiv", "adverb", "praeposition", "konjunktion", "partikel", "pronomen", "phrase"}
)


def parse_word_list(text):
    """Split an owner word list into (word, type_hint) pairs.

    A trailing ``[type]`` pins the word's type (e.g. ``aus [praeposition]``); an
    unknown/typo type in brackets is left as '' (no hint). Returned pairs feed
    create_job, which stores the hint per item."""
    out = []
    for line in str(text or "").splitlines():
        m = re.search(r"\[([a-zA-Z_]+)\]\s*$", line)
        hint = m.group(1).strip().lower() if m else ""
        if hint not in VALID_TYPE_HINTS:
            hint = ""
        word = re.sub(r"\s*\[[a-zA-Z_]+\]\s*$", "", line).strip()
        if word:
            out.append((word, hint))
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


async def _send_review_file(bot, chat_id, job_id, counts):
    """Build the review JSON for a finished/partial/stopped job and send it."""
    data = render_review_json(build_review_document(get_db_path(), job_id)).encode("utf-8")
    caption = f"🌐 VF review — {counts['done']} parsed, {counts['error']} errors. Edit and send back to apply."
    try:
        await bot.send_document(chat_id=chat_id, document=data, filename=review_filename(job_id), caption=caption)
    except Exception:
        await bot.send_message(chat_id=chat_id, text="VF parsing finished, but sending the review file failed.")


async def _make_progress_cb(bot, chat_id, job_id):
    async def on_progress(job, counts, done):
        text, kb = _render(get_db_path(), job_id)
        if text is not None:
            ui = VF_ADD_UI.get(job_id, {})
            try:
                await bot.edit_message_text(text, chat_id=chat_id, message_id=ui.get("message_id"), reply_markup=kb)
            except Exception:
                pass  # a "message not modified" / transient edit error must not kill the job
        if done:
            await _send_review_file(bot, chat_id, job_id, counts)
            VF_ADD_TASKS.pop(job_id, None)

    return on_progress


async def _launch_job(update: Update, context: ContextTypes.DEFAULT_TYPE, job_id, count):
    """Send the pre-start + live progress messages and start the background task."""
    chat_id = update.effective_chat.id
    db_path = get_db_path()
    await _reply_text_with_retry(update.message, prestart_text(count))
    text, kb = _render(db_path, job_id)
    msg = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
    VF_ADD_UI[job_id] = {"chat_id": chat_id, "message_id": msg.message_id, "details": False}
    on_progress = await _make_progress_cb(context.bot, chat_id, job_id)
    VF_ADD_TASKS[job_id] = asyncio.create_task(run_job(db_path, job_id, on_progress=on_progress))
    return MENU


async def start_vf_add_job(update: Update, context: ContextTypes.DEFAULT_TYPE, words):
    """Create + launch a VF-add job from a word list."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    job_id = create_job(get_db_path(), user_id, chat_id, _current_topic_value(user_id), words)
    return await _launch_job(update, context, job_id, len(words))


async def _start_recheck(update: Update, context: ContextTypes.DEFAULT_TYPE, text):
    """Create + launch a VF-recheck job over existing words ('all' or a list)."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    db_path = get_db_path()
    context.user_data.pop("vf_recheck", None)
    candidates = list_words_for_vf_recheck(db_path)  # (word_id, lookup, type)
    if text.strip().lower() == "all":
        pairs = [(lookup, wid, _t) for wid, lookup, _t in candidates]
    else:
        wanted = {w.lower() for w, _h in parse_word_list(text)}
        pairs = [(lookup, wid, _t) for wid, lookup, _t in candidates if lookup.lower() in wanted]
    if not pairs:
        await _reply_text_with_retry(
            update.message,
            "No matching existing words to recheck.",
            reply_markup=_add_menu_markup(user_id),
        )
        return VF_ADD_INPUT
    job_id = create_recheck_job(db_path, user_id, chat_id, pairs)
    return await _launch_job(update, context, job_id, len(pairs))


async def vf_recheck_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry from the Add submenu 'VF recheck' button: wipe stale checks, ask what to recheck."""
    reset_vf_checks(get_db_path())  # old .com results are stale under the .de parser
    context.user_data["vf_recheck"] = True
    await _reply_text_with_retry(
        update.message,
        "🔁 Recheck existing words against verbformen. Send 'all' for every word (except phrases), "
        "or a list (one per line). Old check results were cleared.",
        reply_markup=_add_menu_markup(update.effective_user.id),
    )
    return VF_ADD_INPUT


async def resume_jobs(app):
    """After a restart, relaunch every running/paused job from its pending items.

    Wired as the Application's post_init. The in-memory progress message id is lost
    on restart, so we post a fresh progress message and continue from there; the
    persisted item results mean no already-parsed word is re-fetched.
    """
    db_path = get_db_path()
    for job in active_jobs(db_path):
        jid = job["id"]
        if jid in VF_ADD_TASKS:
            continue
        chat_id = job["chat_id"]
        text, kb = _render(db_path, jid)
        message_id = None
        try:
            msg = await app.bot.send_message(chat_id, f"↻ Resumed after restart.\n{text}", reply_markup=kb)
            message_id = msg.message_id
        except Exception:
            pass
        VF_ADD_UI[jid] = {"chat_id": chat_id, "message_id": message_id, "details": False}
        on_progress = await _make_progress_cb(app.bot, chat_id, jid)
        VF_ADD_TASKS[jid] = asyncio.create_task(run_job(db_path, jid, on_progress=on_progress))


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
        context.user_data.pop("vf_recheck", None)
        return await _back_to_main_menu(update)
    if context.user_data.get("vf_recheck"):
        return await _start_recheck(update, context, text)
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


# --- Step 5: apply the owner's edited review file ---

def _apply_markup(user_id):
    lang = _user_interface_lang(user_id)
    return ReplyKeyboardMarkup([["yes"], [t(lang, "back")]], resize_keyboard=True)


def _apply_preview_text(entries, issues):
    lines = [f"🌐 VF apply preview: {len(entries)} to add, {len(issues)} skipped."]
    if issues:
        lines.append("Skipped:")
        for it in issues[:20]:
            lines.append(f"- {it['word']} → {it['reason']}")
    lines.append("")
    lines.append("Send 'yes' to apply (DB is backed up first), edit & resend the file to change, or back.")
    return "\n".join(lines)


def apply_batch(db_path, entries):
    """Apply a reviewed batch. A card with an existing ``id`` (recheck diff) updates
    that word; otherwise it is inserted (add). Verbs also get their Präsens
    conjugation. Returns (added, updated, duplicates, errors); a duplicate
    (ValueError from add_word) is skipped."""
    added = updated = duplicates = errors = 0
    for entry in entries:
        conj = str(entry.get("conjugation_praesens", "")).strip()
        wid = str(entry.get("id", "") or "").strip()
        if wid and get_word_by_id(db_path, wid):
            try:
                update_word_fields(db_path, wid, entry)
                if conj:
                    set_word_conjugation(db_path, wid, conj)
                updated += 1
            except Exception:  # noqa: BLE001 - one bad row must not abort the batch
                errors += 1
            continue
        try:
            row = add_word(db_path, entry)
        except ValueError:
            duplicates += 1
            continue
        except Exception:  # noqa: BLE001
            errors += 1
            continue
        if row and conj:
            try:
                set_word_conjugation(db_path, row["id"], conj)
            except Exception:
                pass
        added += 1
    return added, updated, duplicates, errors


async def _show_apply_preview(update, context, entries, issues):
    user_id = update.effective_user.id
    context.user_data["vf_apply_entries"] = entries
    await _reply_text_with_retry(update.message, _apply_preview_text(entries, issues), reply_markup=_apply_markup(user_id))
    data = render_apply_preview_json(entries, issues).encode("utf-8")
    try:
        await update.message.reply_document(document=data, filename="vf_apply_preview.json")
    except Exception:
        pass
    return VF_APPLY_CONFIRM


async def _read_document_entries(update):
    doc = update.message.document
    file = await doc.get_file()
    data = await file.download_as_bytearray()
    return parse_review_document(data.decode("utf-8", errors="ignore"))


async def vf_review_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """MENU: an uploaded VF review file -> validate, preview, ask to confirm."""
    if not update.message.document:
        return MENU
    entries, issues = await _read_document_entries(update)
    if entries is None:
        await _reply_text_with_retry(
            update.message,
            "Not a VF review file (expected the JSON the bot sent).",
            reply_markup=_main_menu_markup(update.effective_user.id),
        )
        return MENU
    return await _show_apply_preview(update, context, entries, issues)


async def vf_apply_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """VF_APPLY_CONFIRM: re-edited file re-previews (loop); 'yes' applies; back cancels."""
    user_id = update.effective_user.id
    if update.message.document:
        entries, issues = await _read_document_entries(update)
        if entries is None:
            await _reply_text_with_retry(update.message, "Not a valid review file. Try again or back.", reply_markup=_apply_markup(user_id))
            return VF_APPLY_CONFIRM
        return await _show_apply_preview(update, context, entries, issues)

    text = (update.message.text or "").strip()
    if _action_key_from_text(text, user_id) == "back":
        context.user_data.pop("vf_apply_entries", None)
        return await _back_to_main_menu(update)
    if text.lower() != "yes":
        await _reply_text_with_retry(update.message, "Send 'yes' to apply, edit & resend the file, or back.", reply_markup=_apply_markup(user_id))
        return VF_APPLY_CONFIRM

    entries = context.user_data.pop("vf_apply_entries", [])
    if not entries:
        await _reply_text_with_retry(update.message, "Nothing to apply.", reply_markup=_main_menu_markup(user_id))
        return MENU
    db_path = get_db_path()
    backup_db_with_event(db_path, get_dump_dir(), "vfadd_apply")  # tagged snapshot before writing
    added, updated, dups, errors = apply_batch(db_path, entries)
    await _reply_text_with_retry(
        update.message,
        f"✅ VF apply done: {added} added, {updated} updated, {dups} duplicates, {errors} errors. (DB backed up first.)",
        reply_markup=_main_menu_markup(user_id),
    )
    return MENU


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
