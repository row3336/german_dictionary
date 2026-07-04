"""spec #5: owner inbox — inline navigation for reports & feedback in ONE message.

The nightly/thrice-daily digest (bot/jobs/owner_digest.py) posts a summary with
[reports][feedback] buttons; every step after that edits the SAME message
(editMessageText) — list (paginated 10) → item card → close/delete/back. All
callbacks are owner-only. Callback data is compact (Telegram's 64-byte limit):
`inb:home`, `inb:<r|f>:pg:<n>`, `inb:<r|f>:it:<id>`, `inb:<r|f>:cl:<id>`,
`inb:<r|f>:del:<id>`, `inb:open:<report_id>`.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..app_config import get_db_path, is_owner
from ..common.lang import _user_interface_lang
from ..i18n import t
from ..storage import (
    delete_error_report,
    delete_feedback,
    list_error_reports,
    list_feedback,
    set_error_report_status,
    set_feedback_status,
)

logger = logging.getLogger(__name__)

CB_PREFIX = "inb:"
PAGE_SIZE = 10


# --- data access (kind = 'r' reports | 'f' feedback) --------------------------

def _open_items(kind):
    """Open (pending) items for a kind, newest last."""
    if kind == "r":
        return list_error_reports(get_db_path(), status="pending")
    return list_feedback(get_db_path(), status="pending")


def _count_open():
    """(reports_open, feedback_open) — used by the digest text."""
    return len(list_error_reports(get_db_path(), status="pending")), len(
        list_feedback(get_db_path(), status="pending")
    )


# --- text + markup ------------------------------------------------------------

def digest_text(lang, reports_n, feedback_n):
    """The digest summary line (spec #5): 🛠 reports: N · 💬 feedback: M."""
    return f"🛠 {t(lang, 'inbox_reports')}: {reports_n} · 💬 {t(lang, 'inbox_feedback')}: {feedback_n}"


def digest_home_markup(lang):
    """[🛠 reports][💬 feedback] — entry buttons under the digest."""
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(f"🛠 {t(lang, 'inbox_reports')}", callback_data=f"{CB_PREFIX}r:pg:0"),
            InlineKeyboardButton(f"💬 {t(lang, 'inbox_feedback')}", callback_data=f"{CB_PREFIX}f:pg:0"),
        ]]
    )


def _who(item):
    return ("@" + item["username"]) if item.get("username") else (item.get("first_name") or "user")


def _row_label(kind, item):
    """One-line label for a list row / number button."""
    if kind == "r":
        tail = (item.get("comment") or item.get("german") or "").replace("\n", " ")
    else:
        tail = (item.get("text") or "").replace("\n", " ")
    tail = tail[:40]
    return f"{_who(item)} · {tail}".strip(" ·")


def _paginate(items, page):
    """Clamp page and return (page, total_pages, slice)."""
    total_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    return page, total_pages, items[start:start + PAGE_SIZE]


def _list_view(kind, lang, page):
    """(text, markup) for a paginated list of open items."""
    items = _open_items(kind)
    page, total_pages, chunk = _paginate(items, page)
    title = t(lang, "inbox_reports" if kind == "r" else "inbox_feedback")
    header = t(lang, "inbox_list_header").format(n=len(items), page=page + 1, pages=total_pages)
    if not items:
        text = f"{title}\n{t(lang, 'review_empty')}"
    else:
        lines = [f"{i}) {_row_label(kind, it)}" for i, it in enumerate(chunk, 1 + page * PAGE_SIZE)]
        text = f"{title} — {header}\n\n" + "\n".join(lines)

    rows = []
    num_row = [
        InlineKeyboardButton(str(1 + page * PAGE_SIZE + j), callback_data=f"{CB_PREFIX}{kind}:it:{it['id']}")
        for j, it in enumerate(chunk)
    ]
    # number buttons in rows of 5
    for i in range(0, len(num_row), 5):
        rows.append(num_row[i:i + 5])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"{CB_PREFIX}{kind}:pg:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶", callback_data=f"{CB_PREFIX}{kind}:pg:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("↩", callback_data=f"{CB_PREFIX}home")])
    return text, InlineKeyboardMarkup(rows)


def _find(kind, item_id):
    for it in _open_items(kind):
        if int(it["id"]) == int(item_id):
            return it
    return None


def _card_view(kind, lang, item):
    """(text, markup) for a single report/feedback card."""
    if kind == "r":
        text = (
            f"🛠 report #{item['id']}\n{_who(item)}\n"
            f"word: {item.get('german', '')} | {item.get('mode', '')}\n\n"
            f"{item.get('comment', '')}"
        )
        rows = [[
            InlineKeyboardButton(f"✅ {t(lang, 'inbox_close')}", callback_data=f"{CB_PREFIX}r:cl:{item['id']}"),
            InlineKeyboardButton(f"🗑 {t(lang, 'inbox_delete')}", callback_data=f"{CB_PREFIX}r:del:{item['id']}"),
        ], [InlineKeyboardButton("↩", callback_data=f"{CB_PREFIX}r:pg:0")]]
        # TODO (spec #5 step 1b): [🔧 open word] -> clarify card for item['word_id']
        # (inline -> reply-keyboard clarify transition; deferred, wired next step).
    else:
        text = f"💬 feedback #{item['id']}\n{_who(item)}\n\n{item.get('text', '')}"
        rows = [[
            InlineKeyboardButton(f"✅ {t(lang, 'inbox_close')}", callback_data=f"{CB_PREFIX}f:cl:{item['id']}"),
            InlineKeyboardButton(f"🗑 {t(lang, 'inbox_delete')}", callback_data=f"{CB_PREFIX}f:del:{item['id']}"),
        ], [InlineKeyboardButton("↩", callback_data=f"{CB_PREFIX}f:pg:0")]]
    return text, InlineKeyboardMarkup(rows)


def _home_view(lang):
    r_n, f_n = _count_open()
    return digest_text(lang, r_n, f_n), digest_home_markup(lang)


# --- callback dispatcher ------------------------------------------------------

async def owner_inbox_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all `inb:` inline callbacks (owner-only), editing the message in place."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_owner(user_id):
        await query.answer(t(_user_interface_lang(user_id), "owner_only"), show_alert=True)
        return
    lang = _user_interface_lang(user_id)
    data = (query.data or "")[len(CB_PREFIX):]

    text, markup = None, None
    if data == "home":
        text, markup = _home_view(lang)
    else:
        parts = data.split(":")  # <kind>:<action>:<arg>
        kind, action, arg = parts[0], parts[1], (parts[2] if len(parts) > 2 else "")
        if action == "pg":
            text, markup = _list_view(kind, lang, int(arg or 0))
        elif action == "it":
            item = _find(kind, arg)
            text, markup = _card_view(kind, lang, item) if item else _list_view(kind, lang, 0)
        elif action == "cl":
            if kind == "r":
                set_error_report_status(get_db_path(), int(arg), "reviewed")
            else:
                set_feedback_status(get_db_path(), int(arg), "reviewed")
            text, markup = _list_view(kind, lang, 0)
        elif action == "del":
            if kind == "r":
                delete_error_report(get_db_path(), int(arg))
            else:
                delete_feedback(get_db_path(), int(arg))
            text, markup = _list_view(kind, lang, 0)

    await query.answer()
    if text is not None:
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except Exception:  # identical content / edit race — safe to ignore
            logger.debug("owner_inbox edit skipped", exc_info=True)
