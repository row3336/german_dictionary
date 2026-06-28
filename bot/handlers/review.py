"""P6.3 — owner review of error reports (and feedback, later).

Lists are printed as a numbered code block (no raw user_id), navigated by a
reply keyboard: open -> pick a number -> the item card with a CLICKABLE author
name (a tg://user profile link that works even without a @username) -> close.
Numbers map to ids from the last render (kept in user_data) and the list is
re-rendered after each action, so the numbering never goes stale.
"""

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..app_config import (
    OWNER_REVIEW_MENU,
    REVIEW_REPORTS,
    SETTINGS_MENU,
    get_db_path,
    is_owner,
)
from ..common.lang import _user_interface_lang
from ..common.navigation import _action_key_from_text
from ..i18n import t
from ..storage import list_error_reports, set_error_report_status
from ..ui.keyboards import _settings_menu_keys


def _who(item):
    """Display label for the report author: @username or first name (never id)."""
    if item.get("username"):
        return "@" + item["username"]
    return item.get("first_name") or "user"


def _clickable(item):
    """Markdown link to the author's profile (works without a @username)."""
    return f"[{_who(item)}](tg://user?id={int(item['user_id'])})"


def _owner_review_keys(user_id):
    lang = _user_interface_lang(user_id)
    return [[t(lang, "review_reports"), t(lang, "review_feedback")], [t(lang, "back")]]


async def _send_review_menu(update: Update, user_id):
    """Owner review submenu: reports / feedback / back."""
    await update.message.reply_text(
        t(_user_interface_lang(user_id), "review_menu"),
        reply_markup=ReplyKeyboardMarkup(_owner_review_keys(user_id), resize_keyboard=True),
    )
    return OWNER_REVIEW_MENU


async def owner_review_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route the owner review submenu."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    if not is_owner(user_id):
        return SETTINGS_MENU
    lang = _user_interface_lang(user_id)
    if _action_key_from_text(text, user_id) == "back":
        await update.message.reply_text(
            t(lang, "settings_title"),
            reply_markup=ReplyKeyboardMarkup(_settings_menu_keys(user_id), resize_keyboard=True),
        )
        return SETTINGS_MENU
    if text == t(lang, "review_reports"):
        return await _show_reports_list(update, context, user_id)
    # feedback list lands in 8c; for now re-show the submenu.
    return await _send_review_menu(update, user_id)


def _fmt_reports_list(reports, lang):
    if not reports:
        return t(lang, "review_empty")
    lines = []
    for i, r in enumerate(reports, 1):
        comment = (r.get("comment") or "").replace("\n", " ")[:60]
        lines.append(f"{i}) {_who(r)} · {r.get('german', '')} · {r.get('mode', '')} · {comment}")
    return "\n".join(lines)


async def _show_reports_list(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id):
    """Render the pending reports list and reset the review sub-state to 'list'."""
    lang = _user_interface_lang(user_id)
    reports = list_error_reports(get_db_path(), status="pending")
    context.user_data["review_numbers"] = {i: r["id"] for i, r in enumerate(reports, 1)}
    context.user_data["review_items"] = {r["id"]: r for r in reports}
    context.user_data["review_sub"] = "list"
    keys = [[t(lang, "review_open")], [t(lang, "back")]] if reports else [[t(lang, "back")]]
    await update.message.reply_text(
        f"```text\n{_fmt_reports_list(reports, lang)}\n```",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(keys, resize_keyboard=True),
    )
    return REVIEW_REPORTS


def _fmt_report_item(item):
    head = (
        f"🛠 report #{item['id']}\n{_clickable(item)}\n"
        f"word: {item.get('german', '')} | {item.get('mode', '')}"
    )
    return f"{head}\n```text\n{item.get('comment', '')}\n```"


async def review_reports_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reports list navigation: open -> pick number -> item card -> close."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    if not is_owner(user_id):
        return SETTINGS_MENU
    lang = _user_interface_lang(user_id)
    action = _action_key_from_text(text, user_id)
    sub = context.user_data.get("review_sub", "list")

    if action == "back":
        if sub in ("pick", "item"):
            return await _show_reports_list(update, context, user_id)
        return await _send_review_menu(update, user_id)

    if sub == "list":
        if text == t(lang, "review_open") and context.user_data.get("review_numbers"):
            context.user_data["review_sub"] = "pick"
            nums = list(context.user_data["review_numbers"])
            rows = [[str(n) for n in nums[i:i + 5]] for i in range(0, len(nums), 5)]
            rows.append([t(lang, "back")])
            await update.message.reply_text(
                t(lang, "review_pick"),
                reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True),
            )
            return REVIEW_REPORTS
        return await _show_reports_list(update, context, user_id)

    if sub == "pick":
        try:
            rid = context.user_data.get("review_numbers", {}).get(int(text.strip()))
        except ValueError:
            rid = None
        item = context.user_data.get("review_items", {}).get(rid)
        if not item:
            return await _show_reports_list(update, context, user_id)
        context.user_data["review_sub"] = "item"
        context.user_data["review_current"] = rid
        await update.message.reply_text(
            _fmt_report_item(item),
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([[t(lang, "review_close")], [t(lang, "back")]], resize_keyboard=True),
        )
        return REVIEW_REPORTS

    if sub == "item" and text == t(lang, "review_close"):
        rid = context.user_data.get("review_current")
        if rid:
            set_error_report_status(get_db_path(), rid, "reviewed")
        return await _show_reports_list(update, context, user_id)

    return await _show_reports_list(update, context, user_id)
