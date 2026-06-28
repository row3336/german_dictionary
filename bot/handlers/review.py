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
    REVIEW_FEEDBACK,
    REVIEW_REPORTS,
    SETTINGS_MENU,
    get_db_path,
    is_owner,
)
from ..common.lang import _user_interface_lang
from ..common.navigation import _action_key_from_text
from ..i18n import t
from ..storage import (
    delete_all_error_reports,
    delete_all_feedback,
    delete_error_report,
    delete_feedback,
    list_error_reports,
    list_feedback,
    set_error_report_status,
)
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
    if text == t(lang, "review_feedback"):
        return await _show_feedback_list(update, context, user_id)
    return await _send_review_menu(update, user_id)


def _fmt_reports_list(reports, lang):
    if not reports:
        return t(lang, "review_empty")
    lines = []
    for i, r in enumerate(reports, 1):
        comment = (r.get("comment") or "").replace("\n", " ")[:60]
        lines.append(f"{i}) {_who(r)} · {r.get('german', '')} · {r.get('mode', '')} · {comment}")
    return "\n".join(lines)


def _list_keys(lang, reports, status):
    """List keyboard: open/delete-all when non-empty; pending list also offers 'closed'."""
    rows = []
    if reports:
        rows.append([t(lang, "review_open"), t(lang, "review_delete_all")])
    if status == "pending":
        rows.append([t(lang, "review_closed"), t(lang, "back")])
    else:
        rows.append([t(lang, "back")])
    return rows


def _item_keys(lang, status):
    """Item-card keyboard: close only for open reports; delete for both."""
    if status == "pending":
        return [[t(lang, "review_close"), t(lang, "review_delete")], [t(lang, "back")]]
    return [[t(lang, "review_delete")], [t(lang, "back")]]


async def _show_reports_list(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id, status="pending"):
    """Render the reports list for `status` (pending/reviewed); reset sub-state to 'list'."""
    lang = _user_interface_lang(user_id)
    reports = list_error_reports(get_db_path(), status=status)
    context.user_data["review_numbers"] = {i: r["id"] for i, r in enumerate(reports, 1)}
    context.user_data["review_items"] = {r["id"]: r for r in reports}
    context.user_data["review_sub"] = "list"
    context.user_data["review_status"] = status
    await update.message.reply_text(
        f"```text\n{_fmt_reports_list(reports, lang)}\n```",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(_list_keys(lang, reports, status), resize_keyboard=True),
    )
    return REVIEW_REPORTS


def _fmt_report_item(item):
    head = (
        f"🛠 report #{item['id']}\n{_clickable(item)}\n"
        f"word: {item.get('german', '')} | {item.get('mode', '')}"
    )
    return f"{head}\n```text\n{item.get('comment', '')}\n```"


async def review_reports_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reports navigation: open→number→card→close/delete; delete-all; closed tab."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    if not is_owner(user_id):
        return SETTINGS_MENU
    lang = _user_interface_lang(user_id)
    action = _action_key_from_text(text, user_id)
    sub = context.user_data.get("review_sub", "list")
    status = context.user_data.get("review_status", "pending")

    if action == "back":
        if sub in ("pick", "item", "confirm_delete", "delete_all_confirm"):
            return await _show_reports_list(update, context, user_id, status)
        # back from a list: closed tab returns to open; open list returns to submenu.
        if status == "reviewed":
            return await _show_reports_list(update, context, user_id, "pending")
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
        if text == t(lang, "review_delete_all") and context.user_data.get("review_numbers"):
            context.user_data["review_sub"] = "delete_all_confirm"
            await update.message.reply_text(
                t(lang, "review_delete_all_prompt"),
                reply_markup=ReplyKeyboardMarkup([[t(lang, "back")]], resize_keyboard=True),
            )
            return REVIEW_REPORTS
        if status == "pending" and text == t(lang, "review_closed"):
            return await _show_reports_list(update, context, user_id, "reviewed")
        return await _show_reports_list(update, context, user_id, status)

    if sub == "pick":
        try:
            rid = context.user_data.get("review_numbers", {}).get(int(text.strip()))
        except ValueError:
            rid = None
        item = context.user_data.get("review_items", {}).get(rid)
        if not item:
            return await _show_reports_list(update, context, user_id, status)
        context.user_data["review_sub"] = "item"
        context.user_data["review_current"] = rid
        await update.message.reply_text(
            _fmt_report_item(item),
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup(_item_keys(lang, status), resize_keyboard=True),
        )
        return REVIEW_REPORTS

    if sub == "item":
        if status == "pending" and text == t(lang, "review_close"):
            rid = context.user_data.get("review_current")
            if rid:
                set_error_report_status(get_db_path(), rid, "reviewed")
            return await _show_reports_list(update, context, user_id, status)
        if text == t(lang, "review_delete"):
            context.user_data["review_sub"] = "confirm_delete"
            await update.message.reply_text(
                t(lang, "review_confirm_delete"),
                reply_markup=ReplyKeyboardMarkup([[t(lang, "yes_btn"), t(lang, "no_btn")]], resize_keyboard=True),
            )
            return REVIEW_REPORTS
        return await _show_reports_list(update, context, user_id, status)

    if sub == "confirm_delete":
        if text == t(lang, "yes_btn"):
            rid = context.user_data.get("review_current")
            if rid:
                delete_error_report(get_db_path(), rid)
        return await _show_reports_list(update, context, user_id, status)

    if sub == "delete_all_confirm":
        if text.strip().lower() == "yes":
            delete_all_error_reports(get_db_path(), status=status)
        return await _show_reports_list(update, context, user_id, status)

    return await _show_reports_list(update, context, user_id, status)


# --- Feedback list (P6.3 8c): no status/close — just view + delete. ---

def _fmt_feedback_list(items, lang):
    if not items:
        return t(lang, "review_fb_empty")
    lines = []
    for i, f in enumerate(items, 1):
        txt = (f.get("text") or "").replace("\n", " ")[:70]
        lines.append(f"{i}) {_who(f)} · {txt}")
    return "\n".join(lines)


def _fb_list_keys(lang, items):
    rows = [[t(lang, "review_open"), t(lang, "review_delete_all")]] if items else []
    rows.append([t(lang, "back")])
    return rows


async def _show_feedback_list(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id):
    """Render the feedback list and reset the review sub-state to 'list'."""
    lang = _user_interface_lang(user_id)
    items = list_feedback(get_db_path(), status="pending")
    context.user_data["review_numbers"] = {i: f["id"] for i, f in enumerate(items, 1)}
    context.user_data["review_items"] = {f["id"]: f for f in items}
    context.user_data["review_sub"] = "list"
    await update.message.reply_text(
        f"```text\n{_fmt_feedback_list(items, lang)}\n```",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(_fb_list_keys(lang, items), resize_keyboard=True),
    )
    return REVIEW_FEEDBACK


def _fmt_feedback_item(item):
    return f"💬 feedback #{item['id']}\n{_clickable(item)}\n```text\n{item.get('text', '')}\n```"


async def review_feedback_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Feedback navigation: open→number→card→delete; delete-all. No close/status."""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    if not is_owner(user_id):
        return SETTINGS_MENU
    lang = _user_interface_lang(user_id)
    action = _action_key_from_text(text, user_id)
    sub = context.user_data.get("review_sub", "list")

    if action == "back":
        if sub in ("pick", "item", "confirm_delete", "delete_all_confirm"):
            return await _show_feedback_list(update, context, user_id)
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
            return REVIEW_FEEDBACK
        if text == t(lang, "review_delete_all") and context.user_data.get("review_numbers"):
            context.user_data["review_sub"] = "delete_all_confirm"
            await update.message.reply_text(
                t(lang, "review_delete_all_prompt"),
                reply_markup=ReplyKeyboardMarkup([[t(lang, "back")]], resize_keyboard=True),
            )
            return REVIEW_FEEDBACK
        return await _show_feedback_list(update, context, user_id)

    if sub == "pick":
        try:
            fid = context.user_data.get("review_numbers", {}).get(int(text.strip()))
        except ValueError:
            fid = None
        item = context.user_data.get("review_items", {}).get(fid)
        if not item:
            return await _show_feedback_list(update, context, user_id)
        context.user_data["review_sub"] = "item"
        context.user_data["review_current"] = fid
        await update.message.reply_text(
            _fmt_feedback_item(item),
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([[t(lang, "review_delete")], [t(lang, "back")]], resize_keyboard=True),
        )
        return REVIEW_FEEDBACK

    if sub == "item" and text == t(lang, "review_delete"):
        context.user_data["review_sub"] = "confirm_delete"
        await update.message.reply_text(
            t(lang, "review_confirm_delete"),
            reply_markup=ReplyKeyboardMarkup([[t(lang, "yes_btn"), t(lang, "no_btn")]], resize_keyboard=True),
        )
        return REVIEW_FEEDBACK

    if sub == "confirm_delete":
        if text == t(lang, "yes_btn"):
            fid = context.user_data.get("review_current")
            if fid:
                delete_feedback(get_db_path(), fid)
        return await _show_feedback_list(update, context, user_id)

    if sub == "delete_all_confirm":
        if text.strip().lower() == "yes":
            delete_all_feedback(get_db_path())
        return await _show_feedback_list(update, context, user_id)

    return await _show_feedback_list(update, context, user_id)
