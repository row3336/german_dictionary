"""Topic-filter screen + inline toggle callback (spec #8).

The screen is two messages: an existing reply-keyboard prompt (carries `back` and
the text-input fallback, unchanged) plus a new inline message — a Markdown code-block
of topics with checkmarks and colored (style) toggle buttons. Tapping a button edits
the inline message in place. The stored filter format (per-user '01+03+7' / 'all') is
unchanged, so word selection everywhere keeps working.
"""

import logging

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..app_config import TOPIC_FILTER_EDIT, TOPIC_FILTER_KEY, get_db_path
from ..common.lang import _user_interface_lang
from ..common.settings import _topic_filter_value
from ..common.topics import (
    CB_ALL,
    CB_TOPIC,
    build_keyboard,
    render_text,
    select_all,
    toggle,
)
from ..i18n import t
from ..storage import list_topics_with_desc, set_user_setting

logger = logging.getLogger(__name__)


def _topics_and_names(db_path):
    topics = list_topics_with_desc(db_path)
    return topics, [name for name, _ in topics]


async def send_topic_filter_screen(update, context, user_id, return_to):
    """Enter the topic-filter screen from any entry point.

    Sends the reply-keyboard prompt (back + text fallback) and the inline toggle
    screen, records where `back`/text should return to, and enters TOPIC_FILTER_EDIT.
    """
    lang = _user_interface_lang(user_id)
    raw = _topic_filter_value(user_id)
    topics = list_topics_with_desc(get_db_path())
    all_topics = [name for name, _ in topics]

    # 1) prompt carrying `back` + the text-input fallback (unchanged behavior)
    await update.message.reply_text(
        f"{t(lang, 'topic_filter')}:",
        reply_markup=ReplyKeyboardMarkup([[t(lang, "back")]], resize_keyboard=True),
    )
    # 2) inline screen (edited in place on toggles)
    await update.message.reply_text(
        render_text(lang, raw, topics),
        reply_markup=build_keyboard(raw, all_topics),
        parse_mode="Markdown",
    )
    context.user_data["topic_return"] = return_to
    return TOPIC_FILTER_EDIT


async def topic_filter_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle a tap on the inline topic-filter screen (^topicf:)."""
    query = update.callback_query
    user_id = query.from_user.id
    lang = _user_interface_lang(user_id)
    data = query.data or ""

    topics = list_topics_with_desc(get_db_path())
    all_topics = [name for name, _ in topics]
    current = _topic_filter_value(user_id)

    if data == CB_ALL:
        new = select_all(current, all_topics)
        if new is None:  # already all -> no-op
            await query.answer()
            return
    elif data.startswith(CB_TOPIC):
        name = data[len(CB_TOPIC):]
        if name not in all_topics:  # stale button (topic vanished) -> just refresh
            await query.answer()
            new = current
        else:
            new, rejected = toggle(current, name, all_topics)
            if rejected:
                await query.answer(t(lang, "topic_filter_need_one"), show_alert=True)
                return
    else:
        await query.answer()
        return

    if new != current:
        set_user_setting(get_db_path(), user_id, TOPIC_FILTER_KEY, new)
    await query.answer()
    try:
        await query.edit_message_text(
            render_text(lang, new, topics),
            reply_markup=build_keyboard(new, all_topics),
            parse_mode="Markdown",
        )
    except Exception:
        # identical content (rare double-tap) or an edit race — safe to ignore
        logger.debug("topic_filter edit skipped", exc_info=True)
