"""Progress and dictionary views for approved users, plus owner-only activity.

Inline navigation edits one message; users activity is protected independently of
which keyboard opened it. New counter queries remain separate from storage APIs.
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
from ..storage import get_statistics_snapshot, list_user_activity
from ..common.stats_queries import dictionary_type_counts, learned_word_counts, progress_breakdown
from .access import access_allowed
from ..text_utils import md_escape
from .review import _clickable

STAT_CALLBACK_PREFIX = "stat"
logger = logging.getLogger(__name__)


def _format_progress_text(user_id):
    """Per-user progress code-block content (spec #11: 'progress' view). Labels are
    localized to the user's interface language; the FSRS direction tokens follow the
    user's DICTIONARY language (Ru>De / Ua>De / En>De, via _quiz_dir_labels), and the
    dictionary type names have a separate view."""
    lang = _user_interface_lang(user_id)
    l1_de_dir, de_l1_dir = _quiz_dir_labels(user_id)
    stats = get_statistics_snapshot(get_db_path(), user_id=user_id, mode=_mode_l1_de(user_id))
    modes = (*direction_mode_family(_mode_l1_de(user_id)),
             *direction_mode_family(_mode_de_l1(user_id)))
    breakdown = progress_breakdown(get_db_path(), user_id, modes)
    labels = (t(lang, "stat_words"), t(lang, "stat_phrases")) * 2
    directions = (l1_de_dir, l1_de_dir, de_l1_dir, de_l1_dir)
    width = max(len(label) for label in labels)
    due_lines = [
        f"{label:<{width}} {t(lang, 'stat_fsrs_due').format(dir=direction)} = {breakdown['due'][mode]}"
        for label, direction, mode in zip(labels, directions, modes)
    ]
    today_lines = [
        f"{label:<{width}} fsrs {direction} = {breakdown['today'][mode]}"
        for label, direction, mode in zip(labels, directions, modes)
    ]
    lines = [
        f"{t(lang, 'stat_bot_version')} = v{APP_VERSION}",
        "",
        *due_lines,
        "",
        t(lang, "stat_today_heading"),
        *today_lines,
        f"           pl > sing = {stats.get('today_complete_pl_sg', 0)}",
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
    ]
    return "\n".join(lines)


def _format_users_text(lang):
    """[users] view: approved users by activity (most active first). Plain
    Markdown, not a code block — clickable name links don't render inside one."""
    items = list_user_activity(get_db_path())
    fsrs_counts = learned_word_counts(get_db_path())
    if not items:
        return t(lang, "stat_users_empty")
    lines = []
    for item in items:
        username = (item.get("username") or "").strip().lstrip("@")
        # This view uses legacy Markdown. Telegram usernames may contain "_",
        # which must be escaped or Telegram rejects the entire edited message.
        user_ref = md_escape(f"@{username}") if username else _clickable(item)
        lines.append(
            f"{user_ref} — {item['quiz_events_count']} ({fsrs_counts.get(item['user_id'], 0)})"
        )
    return "\n".join(lines)


def _owner_stat_keyboard():
    """Owner inline views (EN raw labels preserve the existing convention)."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("progress", callback_data=f"{STAT_CALLBACK_PREFIX}:progress"),
        InlineKeyboardButton("users", callback_data=f"{STAT_CALLBACK_PREFIX}:users"),
        InlineKeyboardButton("word types", callback_data=f"{STAT_CALLBACK_PREFIX}:word_types"),
    ]])


def _stat_keyboard(user_id):
    if is_owner(user_id):
        return _owner_stat_keyboard()
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("progress", callback_data=f"{STAT_CALLBACK_PREFIX}:progress"),
        InlineKeyboardButton("word types in db", callback_data=f"{STAT_CALLBACK_PREFIX}:word_types"),
    ]])


def _format_word_types_text(lang):
    counts = dictionary_type_counts(get_db_path())
    return "\n".join([
        f"{t(lang, 'stat_total')} = {sum(counts.values())}", "",
        *[f"{word_type} = {count}" for word_type, count in counts.items()],
    ])


async def stat_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit the selected view; a users callback always requires owner access."""
    query = update.callback_query
    user_id = update.effective_user.id
    view = (query.data or "").split(":", 1)[-1]
    if (not access_allowed(user_id) or view not in {"progress", "users", "word_types"}
            or (view == "users" and not is_owner(user_id))):
        await query.answer()
        return
    if view == "users":
        text = _format_users_text(_user_interface_lang(user_id))
    elif view == "word_types":
        text = f"```text\n{_format_word_types_text(_user_interface_lang(user_id))}\n```"
    else:
        text = f"```text\n{_format_progress_text(user_id)}\n```"
    await query.answer()
    try:
        await query.edit_message_text(text, reply_markup=_stat_keyboard(user_id), parse_mode="Markdown")
    except BadRequest as exc:
        # A rare double-tap can legitimately edit a message to identical content.
        if "message is not modified" not in str(exc).lower():
            logger.warning("Failed to edit owner statistics message", exc_info=True)
    except TelegramError:
        logger.warning("Failed to edit owner statistics message", exc_info=True)
