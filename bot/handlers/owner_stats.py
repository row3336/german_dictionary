"""Spec #11 — owner-only stat toggle: [progress] (the existing per-user code
block, unchanged) vs [users] (approved-user activity, most active first).
Both views live on the SAME message, switched by editing it via an inline
keyboard (`^stat:` global callback) — no new messages, and the persistent
reply-keyboard from before this screen is left untouched. Guests never get the
inline buttons and keep today's plain progress-only screen.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from ..app_config import APP_VERSION, get_db_path, is_owner
from ..common.lang import (
    _mode_de_l1,
    _mode_l1_de,
    _quiz_dir_labels,
    _user_interface_lang,
    direction_mode_family,
)
from ..i18n import t
from ..storage import get_statistics_snapshot, list_user_activity, select_due
from ..text_utils import md_escape
from .review import _clickable

STAT_CALLBACK_PREFIX = "stat"
logger = logging.getLogger(__name__)


def _format_progress_text(user_id):
    """Per-user progress code-block content (spec #11: 'progress' view). Labels are
    localized to the user's interface language; the FSRS direction tokens follow the
    user's DICTIONARY language (Ru>De / Ua>De / En>De, via _quiz_dir_labels), and the
    German word-type names at the end stay as-is."""
    lang = _user_interface_lang(user_id)
    l1_de_dir, de_l1_dir = _quiz_dir_labels(user_id)
    stats = get_statistics_snapshot(get_db_path(), user_id=user_id, mode=_mode_l1_de(user_id))
    word_types = stats.get("word_types") or {}
    db_path = get_db_path()
    fsrs_ru_de_due = sum(
        len(select_due(db_path, user_id=user_id, mode=mode, exclude_correct_today=True))
        for mode in direction_mode_family(_mode_l1_de(user_id))
    )
    fsrs_de_ru_due = sum(
        len(select_due(db_path, user_id=user_id, mode=mode, exclude_correct_today=True))
        for mode in direction_mode_family(_mode_de_l1(user_id))
    )
    lines = [
        f"{t(lang, 'stat_bot_version')} = v{APP_VERSION}",
        "",
        f"{t(lang, 'stat_fsrs_due').format(dir=l1_de_dir)} = {fsrs_ru_de_due}",
        f"{t(lang, 'stat_fsrs_due').format(dir=de_l1_dir)} = {fsrs_de_ru_due}",
        "",
        f"{t(lang, 'stat_today_fsrs').format(dir=l1_de_dir)} = {stats['today_complete_fsrs_ru_de']}",
        f"{t(lang, 'stat_today_fsrs').format(dir=de_l1_dir)} = {stats['today_complete_fsrs_de_ru']}",
        f"{t(lang, 'stat_today_pl_sg')} = {stats.get('today_complete_pl_sg', 0)}",
        "",
        f"{t(lang, 'stat_streak_5p')} = {stats.get('streak_5p', 0)}",
        f"{t(lang, 'stat_streak_4')} = {stats.get('streak_4', 0)}",
        f"{t(lang, 'stat_streak_3')} = {stats.get('streak_3', stats.get('streak_3p', 0))}",
        f"{t(lang, 'stat_streak_2')} = {stats.get('streak_2', 0)}",
        f"{t(lang, 'stat_streak_1')} = {stats.get('streak_1', 0)}",
        f"{t(lang, 'stat_wrong_fsrs')} = {stats.get('wrong_fsrs', 0)}",
        "",
        f"{t(lang, 'stat_words_in_dict')} = {stats['total_words']}",
        f"{t(lang, 'stat_never_touched')} = {stats['never_answered']}",
        f"{t(lang, 'stat_words_weekly')} = +{stats['weekly_words']}",
        f"{t(lang, 'stat_learning_weekly')} = {stats.get('learning_weekly', 0)}",
        "",
        t(lang, "stat_word_types"),
        *[f"{k} = {word_types[k]}" for k in sorted(word_types.keys())],
    ]
    return "\n".join(lines)


def _format_users_text(lang):
    """[users] view: approved users by activity (most active first). Plain
    Markdown, not a code block — clickable name links don't render inside one."""
    items = list_user_activity(get_db_path())
    if not items:
        return t(lang, "stat_users_empty")
    lines = []
    for item in items:
        username = (item.get("username") or "").strip().lstrip("@")
        # This view uses legacy Markdown. Telegram usernames may contain "_",
        # which must be escaped or Telegram rejects the entire edited message.
        user_ref = md_escape(f"@{username}") if username else _clickable(item)
        lines.append(
            f"{user_ref} — {item['quiz_events_count']} ({item['learning_done_count']})"
        )
    return "\n".join(lines)


def _owner_stat_keyboard():
    """The [progress]/[users] inline toggle row (EN raw labels: owner-only norm)."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("progress", callback_data=f"{STAT_CALLBACK_PREFIX}:progress"),
        InlineKeyboardButton("users", callback_data=f"{STAT_CALLBACK_PREFIX}:users"),
    ]])


async def stat_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle a tap on the [progress]/[users] inline toggle (^stat:); owner-only."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_owner(user_id):
        await query.answer()
        return
    view = (query.data or "").split(":", 1)[-1]
    if view == "users":
        text = _format_users_text(_user_interface_lang(user_id))
    else:
        text = f"```text\n{_format_progress_text(user_id)}\n```"
    await query.answer()
    try:
        await query.edit_message_text(text, reply_markup=_owner_stat_keyboard(), parse_mode="Markdown")
    except BadRequest as exc:
        # A rare double-tap can legitimately edit a message to identical content.
        if "message is not modified" not in str(exc).lower():
            logger.warning("Failed to edit owner statistics message", exc_info=True)
    except TelegramError:
        logger.warning("Failed to edit owner statistics message", exc_info=True)
