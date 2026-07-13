"""Word-filter screens S0–S3 (spec_topics_levels.md §4).

One inline message, edited in place as the user drills the fork:

    S0 fork ─▶ S1 CEFR levels          (tap = toggle, mode -> cefr)
           └▶ S2 book-levels ─▶ S3 topics of that level  (mode -> topics)

Inline `wf:` buttons drive the drill (a global CallbackQueryHandler, so they survive
a restart — all state lives in the DB, nothing in memory). The reply keyboard carries
only `back`, which returns to the calling menu (via `topic_return`) and prints a summary
of the now-active filter. `style="success"` (Bot API 9.4) greens the active choices;
the checkmarks in the code block carry the same state for older clients.

All screen layouts are pure (DB in, (text, rows) out) so they unit-test without PTB.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..app_config import CEFR_LEVELS, TOPIC_FILTER_KEY, TOPIC_FILTER_EDIT, get_db_path
from ..common.lang import _user_interface_lang
from ..common.topics import filter_is_all, resolve_active, sorted_topics, toggle as topic_toggle
from ..common.word_filter import (
    MODE_CEFR,
    MODE_TOPICS,
    get_active_book_level,
    get_filter_mode,
    resolve_quiz_filter,
    set_active_book_level,
    set_filter_mode,
)
from ..i18n import t
from ..storage import (
    count_words_for_book_level,
    count_words_for_cefr,
    get_user_levels,
    get_user_setting,
    list_book_levels,
    list_cefr_levels_with_words,
    list_topics_with_level,
    set_user_setting,
    toggle_user_level,
)

logger = logging.getLogger(__name__)

# callback_data vocabulary
CB_FORK_CEFR = "wf:fork:cefr"
CB_FORK_TOPICS = "wf:fork:topics"
CB_CEFR = "wf:cefr:"      # + <LEVEL> | 'all'
CB_BLVL = "wf:blvl:"      # + <LEVEL>
CB_TOPIC = "wf:topic:"    # + <name> | 'all'
CB_BACK = "wf:back:"      # + 'fork' | 'blvl'

_LEVELS_PER_ROW = 3
_TOPICS_PER_ROW = 5


def _btn(text, data, active):
    return InlineKeyboardButton(text, callback_data=data, style="success" if active else None)


def _level_topics_with_desc(db, level):
    """(name, description) for topics assigned to `level`, in canonical order."""
    rows = [(n, d) for n, d, lv in list_topics_with_level(db) if lv == level]
    order = {n: i for i, n in enumerate(sorted_topics([n for n, _ in rows]))}
    return sorted(rows, key=lambda nd: order[nd[0]])


# --- S0 fork ---

def render_fork(user_id):
    lang = _user_interface_lang(user_id)
    mode = get_filter_mode(user_id)
    text = t(lang, "wf_fork_title")
    rows = [[
        _btn(t(lang, "wf_btn_cefr"), CB_FORK_CEFR, mode == MODE_CEFR),
        _btn(t(lang, "wf_btn_topics"), CB_FORK_TOPICS, mode == MODE_TOPICS),
    ]]
    return text, InlineKeyboardMarkup(rows)


# --- S1 CEFR levels ---

def render_cefr(user_id):
    lang = _user_interface_lang(user_id)
    db = get_db_path()
    levels = list_cefr_levels_with_words(db)  # dynamic: only levels that have words
    selected = set(get_user_levels(db, user_id))
    all_selected = len(selected) >= len(CEFR_LEVELS)
    body = "\n".join(
        f"[{'✅' if lv in selected else '  '}] {lv} (words: {count_words_for_cefr(db, lv)})"
        for lv in levels
    ) or "—"
    text = f"{t(lang, 'wf_cefr_title')}\n\n```text\n{body}\n```"
    rows = []
    for i in range(0, len(levels), _LEVELS_PER_ROW):
        rows.append([_btn(lv, CB_CEFR + lv, lv in selected) for lv in levels[i : i + _LEVELS_PER_ROW]])
    rows.append([_btn(t(lang, "wf_btn_all"), CB_CEFR + "all", all_selected)])
    rows.append([InlineKeyboardButton(t(lang, "wf_btn_back"), callback_data=CB_BACK + "fork")])
    return text, InlineKeyboardMarkup(rows)


# --- S2 book-levels ---

def render_booklevels(user_id):
    lang = _user_interface_lang(user_id)
    db = get_db_path()
    levels = list_book_levels(db)  # dynamic: only levels whose topics hold words
    active = get_active_book_level(user_id)
    body = "\n".join(
        f"{lv} (words: {count_words_for_book_level(db, lv)})" for lv in levels
    ) or "—"
    text = f"{t(lang, 'wf_booklevel_title')}\n\n```text\n{body}\n```"
    rows = []
    for i in range(0, len(levels), _LEVELS_PER_ROW):
        rows.append([_btn(lv, CB_BLVL + lv, lv == active) for lv in levels[i : i + _LEVELS_PER_ROW]])
    rows.append([InlineKeyboardButton(t(lang, "wf_btn_back"), callback_data=CB_BACK + "fork")])
    return text, InlineKeyboardMarkup(rows)


# --- S3 topics of the active level ---

def render_topics(user_id):
    lang = _user_interface_lang(user_id)
    db = get_db_path()
    level = get_active_book_level(user_id)
    pairs = _level_topics_with_desc(db, level)
    names = [n for n, _ in pairs]
    raw = get_user_setting(db, user_id, TOPIC_FILTER_KEY, "")
    active = resolve_active(raw, names)
    body = "\n".join(
        f"[{'✅' if n in active else '  '}] [{n}] {(d or '').strip() or '—'}" for n, d in pairs
    ) or "—"
    text = f"{t(lang, 'wf_topics_title')} ({level})\n\n```text\n{body}\n```"
    rows = []
    ordered = sorted_topics(names)
    for i in range(0, len(ordered), _TOPICS_PER_ROW):
        rows.append([_btn(n, CB_TOPIC + n, n in active) for n in ordered[i : i + _TOPICS_PER_ROW]])
    rows.append([_btn(t(lang, "wf_btn_all"), CB_TOPIC + "all", filter_is_all(raw))])
    rows.append([InlineKeyboardButton(t(lang, "wf_btn_back"), callback_data=CB_BACK + "blvl")])
    return text, InlineKeyboardMarkup(rows)


# --- entry (replaces the old topic-filter screen at every quiz entry point) ---

async def send_word_filter_screen(update, context, user_id, return_to):
    """Open the filter at S0. Reply keyboard carries `back` -> caller; inline drills."""
    lang = _user_interface_lang(user_id)
    context.user_data["topic_return"] = return_to
    context.user_data["wf_active"] = True
    await update.message.reply_text(
        t(lang, "wf_fork_title"),
        reply_markup=ReplyKeyboardMarkup([[t(lang, "back")]], resize_keyboard=True),
    )
    text, kb = render_fork(user_id)
    await update.message.reply_text(text, reply_markup=kb)
    return TOPIC_FILTER_EDIT


# --- summary printed when the user leaves via reply-back ---

def filter_summary(user_id):
    """One-line summary of the active filter for the leave-message."""
    lang = _user_interface_lang(user_id)
    qf = resolve_quiz_filter(user_id)
    if qf.mode == MODE_CEFR:
        value = "all" if qf.cefr_levels is None else "+".join(qf.cefr_levels)
        return t(lang, "wf_summary_cefr").format(value=value)
    level = get_active_book_level(user_id)
    raw = get_user_setting(get_db_path(), user_id, TOPIC_FILTER_KEY, "")
    value = "all" if not raw or filter_is_all(raw) else "+".join(sorted_topics(qf.topics or []))
    return t(lang, "wf_summary_topics").format(level=level, value=value)


# --- inline callback: drives the whole drill ---

async def word_filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle a tap on any `wf:` inline button (global handler)."""
    query = update.callback_query
    user_id = query.from_user.id
    lang = _user_interface_lang(user_id)
    db = get_db_path()
    data = query.data or ""

    render = None
    if data == CB_FORK_CEFR:
        render = render_cefr
    elif data == CB_FORK_TOPICS:
        render = render_topics
    elif data == CB_BACK + "fork":
        render = render_fork
    elif data == CB_BACK + "blvl":
        render = render_booklevels
    elif data.startswith(CB_CEFR):
        set_filter_mode(user_id, MODE_CEFR)
        key = data[len(CB_CEFR):]
        if key == "all":
            for lv in CEFR_LEVELS:
                if lv not in set(get_user_levels(db, user_id)):
                    toggle_user_level(db, user_id, lv)
        else:
            ok, _ = toggle_user_level(db, user_id, key)
            if not ok:  # would drop the last level — keep at least one
                await query.answer(t(lang, "topic_filter_need_one"), show_alert=True)
                return
        render = render_cefr
    elif data.startswith(CB_BLVL):
        level = data[len(CB_BLVL):]
        set_filter_mode(user_id, MODE_TOPICS)
        set_active_book_level(user_id, level)
        set_user_setting(db, user_id, TOPIC_FILTER_KEY, "all")  # fresh level -> all
        render = render_topics
    elif data.startswith(CB_TOPIC):
        set_filter_mode(user_id, MODE_TOPICS)
        level = get_active_book_level(user_id)
        names = [n for n, _ in _level_topics_with_desc(db, level)]
        key = data[len(CB_TOPIC):]
        if key == "all":
            set_user_setting(db, user_id, TOPIC_FILTER_KEY, "all")
        elif key in names:
            raw = get_user_setting(db, user_id, TOPIC_FILTER_KEY, "")
            new, rejected = topic_toggle(raw, key, names)
            if rejected:
                await query.answer(t(lang, "topic_filter_need_one"), show_alert=True)
                return
            set_user_setting(db, user_id, TOPIC_FILTER_KEY, new)
        render = render_topics
    else:
        await query.answer()
        return

    await query.answer()
    text, kb = render(user_id)
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:  # identical content (double-tap) or edit race — safe to ignore
        logger.debug("word_filter edit skipped", exc_info=True)
