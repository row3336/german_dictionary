"""Spec #11 — owner-only stat toggle: [progress] (the existing per-user code
block, unchanged) vs [users] (approved-user activity, most active first).
Both views live on the SAME message, switched by editing it via an inline
keyboard (`^stat:` global callback) — no new messages, and the persistent
reply-keyboard from before this screen is left untouched. Guests never get the
inline buttons and keep today's plain progress-only screen.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..app_config import APP_VERSION, get_db_path, is_owner
from ..common.lang import _mode_de_l1, _mode_l1_de, _user_interface_lang
from ..i18n import t
from ..storage import get_statistics_snapshot, list_user_activity, select_due
from .review import _clickable

STAT_CALLBACK_PREFIX = "stat"


def _format_progress_text(user_id):
    """Per-user progress code-block content (spec #11: 'progress' view — the
    screen as it exists today, untouched)."""
    stats = get_statistics_snapshot(get_db_path(), user_id=user_id, mode=_mode_l1_de(user_id))
    word_types = stats.get("word_types") or {}
    db_path = get_db_path()
    fsrs_ru_de_due = len(
        select_due(db_path, user_id=user_id, mode=_mode_l1_de(user_id), exclude_correct_today=True)
    )
    fsrs_de_ru_due = len(
        select_due(db_path, user_id=user_id, mode=_mode_de_l1(user_id), exclude_correct_today=True)
    )
    lines = [
        f"bot version = v{APP_VERSION}",
        "",
        f"fsrs_ru_de_due = {fsrs_ru_de_due}",
        f"fsrs_de_ru_due = {fsrs_de_ru_due}",
        "",
        f"today complete fsrs Ru > De = {stats['today_complete_fsrs_ru_de']}",
        f"today complete fsrs De > Ru = {stats['today_complete_fsrs_de_ru']}",
        f"today complete pl > sing = {stats.get('today_complete_pl_sg', 0)}",
        "",
        f"today complete weak article = {stats['today_complete_weak_article']}",
        f"today complete weak plural = {stats['today_complete_weak_plural']}",
        f"today complete weak partizip2 = {stats['today_complete_weak_partizip2']}",
        f"today complete weak preteritum = {stats['today_complete_weak_prateritum']}",
        "",
        f"5+ correct streak = {stats.get('streak_5p', 0)}",
        f"4 correct streak = {stats.get('streak_4', 0)}",
        f"3 correct streak = {stats.get('streak_3', stats.get('streak_3p', 0))}",
        f"2 correct streak = {stats.get('streak_2', 0)}",
        f"1 correct = {stats.get('streak_1', 0)}",
        f"wrong fsrs = {stats.get('wrong_fsrs', 0)}",
        "",
        (
            f"weak article = {stats['weak_article']} "
            f"({stats['weak_article_stage1']}) "
            f"({stats['weak_article_stage2']}) "
            f"({stats['weak_article_stage3']})"
        ),
        (
            f"weak plural = {stats['weak_plural']} "
            f"({stats['weak_plural_stage1']}) "
            f"({stats['weak_plural_stage2']}) "
            f"({stats['weak_plural_stage3']})"
        ),
        (
            f"weak partizip2 = {stats['weak_partizip2']} "
            f"({stats['weak_partizip2_stage1']}) "
            f"({stats['weak_partizip2_stage2']}) "
            f"({stats['weak_partizip2_stage3']})"
        ),
        (
            f"weak preterium = {stats['weak_prateritum']} "
            f"({stats['weak_prateritum_stage1']}) "
            f"({stats['weak_prateritum_stage2']}) "
            f"({stats['weak_prateritum_stage3']})"
        ),
        "",
        f"words in dict = {stats['total_words']}",
        f"never touched = {stats['never_answered']}",
        f"words weekly = +{stats['weekly_words']}",
        f"learning weekly = {stats.get('learning_weekly', 0)}",
        "",
        "word types:",
        *[f"{k} = {word_types[k]}" for k in sorted(word_types.keys())],
    ]
    return "\n".join(lines)


def _format_users_text(lang):
    """[users] view: approved users by activity (most active first). Plain
    Markdown, not a code block — clickable name links don't render inside one."""
    items = list_user_activity(get_db_path())
    if not items:
        return t(lang, "stat_users_empty")
    return "\n".join(
        f"{_clickable(item)} — {item['quiz_events_count']} ({item['learning_done_count']})"
        for item in items
    )


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
    except Exception:
        # Identical content (rare double-tap) or an edit race - safe to ignore.
        pass
