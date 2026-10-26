"""Revision-checked word-filter screens; browsing never activates a branch."""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..app_config import BOOK_LEVELS, CEFR_LEVELS, TOPIC_FILTER_EDIT
from ..common.lang import _user_interface_lang
from ..common import word_filter as wf
from ..common.topic_display import topic_labels
from ..i18n import t

logger = logging.getLogger(__name__)

# Old mutation callbacks must be rejected rather than interpreted as v2 toggles.
CB_FORK_CEFR = "wf:fork:cefr"
CB_FORK_TOPICS = "wf:fork:topics"
CB_CEFR = "wf:cefr:"
CB_BLVL = "wf:blvl:"
CB_TOPIC = "wf:topic:"
CB_BACK = "wf:back:"
V2 = "wf:v2:"
_LEVELS_PER_ROW = 3
_TOPICS_PER_ROW = 5


def callback_data(operation, revision, level="", token=""):
    parts = ["wf", "v2", operation, str(revision)]
    if level or token:
        parts.append(level)
    if token:
        parts.append(token)
    value = ":".join(parts)
    if len(value.encode("utf-8")) > 64:
        raise ValueError("callback exceeds Telegram's byte limit")
    return value


def _btn(text, data, active=False):
    return InlineKeyboardButton(text, callback_data=data, style="success" if active else None)


def _nav(lang, state, target, level="", label=None):
    return _btn(label or t(lang, "wf_btn_back"), callback_data("n", state["revision"], target, level))


def _summary(state, lang):
    qf = wf.filter_from_state(state)
    if qf.mode == wf.MODE_CEFR:
        value = "all" if qf.cefr_levels is None else "+".join(qf.cefr_levels) or "∅"
        return t(lang, "wf_summary_cefr").format(value=value)
    selected = state["config"]["selected_levels"]
    return t(lang, "wf_summary_topics").format(
        level="+".join(selected) or "∅", value=wf.filter_label_from_state(state, lang))


def _needs_refresh(state):
    config = state["config"]
    return (not state["valid"] or not config["selected_levels"]
            or any(lv not in state["available_levels"] or not wf.selected_topics(state, lv)
                   or (config["topics_by_level"][lv] != "all"
                       and set(config["topics_by_level"][lv]) - set(state["by_level"][lv]))
                   for lv in config["selected_levels"]))


def render_fork(user_id, state=None):
    state = state or wf.read_filter_state(user_id)
    lang = _user_interface_lang(user_id)
    rows = [[
        _btn(t(lang, "wf_btn_cefr"), callback_data("n", state["revision"], "cefr"), state["mode"] == wf.MODE_CEFR),
        _btn(t(lang, "wf_btn_topics"), callback_data("n", state["revision"], "book_entry"), state["mode"] == wf.MODE_TOPICS),
    ]]
    return t(lang, "wf_fork_title"), InlineKeyboardMarkup(rows)


def render_cefr(user_id, state=None):
    state = state or wf.read_filter_state(user_id)
    lang = _user_interface_lang(user_id)
    levels = [lv for lv in CEFR_LEVELS if state["cefr_counts"].get(lv)]
    selected = set(state["cefr_levels"]) if state["mode"] == wf.MODE_CEFR else set()
    body = "\n".join(f"[{'✅' if lv in selected else '  '}] {lv} (words: {state['cefr_counts'][lv]})" for lv in levels) or "∅"
    rows = [[_btn(lv, callback_data("c", state["revision"], lv), lv in selected)
             for lv in levels[i:i + _LEVELS_PER_ROW]] for i in range(0, len(levels), _LEVELS_PER_ROW)]
    rows.append([_btn(t(lang, "wf_btn_all"), callback_data("c", state["revision"], "all"), len(selected) == len(CEFR_LEVELS))])
    rows.append([_nav(lang, state, "fork")])
    return f"{t(lang, 'wf_cefr_title')}\n\n{body}", InlineKeyboardMarkup(rows)


def render_booklevels(user_id, state=None):
    state = state or wf.read_filter_state(user_id)
    lang = _user_interface_lang(user_id)
    available = state["available_levels"]
    selected = set(state["config"]["selected_levels"]) if state["mode"] == wf.MODE_TOPICS else set()
    # A saved level that disappeared remains removable, while a valid selected
    # level keeps the minimum-one invariant. Opening the screen never repairs it.
    removable = selected - set(available) if selected.intersection(available) else set()
    levels = [lv for lv in BOOK_LEVELS if lv in available or lv in removable]
    counts = {lv: sum(r["word_count"] for r in state["registry"] if r["level"] == lv) for lv in levels}
    body = "\n".join(f"[{'✅' if lv in selected else '  '}] {lv} (words: {counts[lv]})" for lv in levels) or t(lang, "wf_booklevel_empty")
    rows = [[_btn(lv + (" ∅" if lv in removable else ""), callback_data("l", state["revision"], lv), lv in selected)
             for lv in levels[i:i + _LEVELS_PER_ROW]] for i in range(0, len(levels), _LEVELS_PER_ROW)]
    browse = [lv for lv in available if lv in selected]
    if browse:
        cursor = state["config"]["browsing_level"]
        cursor = cursor if cursor in browse else browse[0]
        rows.append([_nav(lang, state, "topics", cursor, t(lang, "wf_btn_edit_topics"))])
    if _needs_refresh(state):
        body += "\n\n" + t(lang, "wf_refresh")
    rows.append([_nav(lang, state, "fork")])
    return f"{t(lang, 'wf_booklevel_title')}\n\n{body}\n\n{_summary(state, lang)}", InlineKeyboardMarkup(rows)


def render_topics(user_id, level=None, state=None):
    state = state or wf.read_filter_state(user_id)
    lang = _user_interface_lang(user_id)
    config = state["config"]
    browse = [lv for lv in config["selected_levels"] if lv in state["available_levels"]]
    if level is None:
        level = config["browsing_level"]
        if level not in browse:
            level = browse[0] if browse else (state["available_levels"][0] if len(state["available_levels"]) == 1 else None)
    if level not in state["available_levels"]:
        return render_booklevels(user_id, state)
    names = state["by_level"][level]
    tokens = wf.topic_tokens(names)
    reverse = {name: token for token, name in tokens.items()}
    active = set(wf.selected_topics(state, level)) if state["mode"] == wf.MODE_TOPICS else set()
    descriptions = {r["name"]: r["description"] for r in state["registry"]}
    labels = topic_labels(level, names, descriptions)
    body = "\n".join(f"[{'✅' if n in active else '  '}] [{labels[n]}] {(descriptions[n] or '').strip() or '—'}" for n in names) or "∅"
    rows = [[_btn(labels[n], callback_data("t", state["revision"], level, reverse[n]), n in active)
             for n in names[i:i + _TOPICS_PER_ROW]] for i in range(0, len(names), _TOPICS_PER_ROW)]
    if state["mode"] == wf.MODE_TOPICS and browse:
        all_selected = all(config["topics_by_level"][lv] == "all" for lv in config["selected_levels"])
        rows.append([_btn(t(lang, "wf_btn_all_selected"), callback_data("a", state["revision"]), all_selected)])
    if len(browse) > 1:
        rows.append([_nav(lang, state, "topics", lv, lv) for lv in browse if lv != level])
    rows.append([_nav(lang, state, "books")])
    if _needs_refresh(state):
        body += "\n\n" + t(lang, "wf_refresh")
    text = f"{t(lang, 'wf_topics_title')} ({level})\n\n{body}\n\n{_summary(state, lang)}"
    return text, InlineKeyboardMarkup(rows)


async def send_word_filter_screen(update, context, user_id, return_to):
    lang = _user_interface_lang(user_id)
    context.user_data["topic_return"] = return_to
    context.user_data["wf_active"] = True
    await update.message.reply_text(t(lang, "wf_fork_title"),
        reply_markup=ReplyKeyboardMarkup([[t(lang, "back")]], resize_keyboard=True))
    text, kb = render_fork(user_id)
    await update.message.reply_text(text, reply_markup=kb)
    return TOPIC_FILTER_EDIT


def filter_summary(user_id):
    return _summary(wf.read_filter_state(user_id), _user_interface_lang(user_id))


async def word_filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    lang = _user_interface_lang(user_id)
    data = query.data or ""
    state = wf.read_filter_state(user_id)
    alert, target, level = None, "books", None
    try:
        # Navigation also checks current access; callback payload never chooses actor.
        from ..storage import _connect
        from ..app_config import get_db_path
        from ..study.answers import authorize_user
        with _connect(get_db_path()) as conn:
            authorize_user(conn, user_id)
        if data in (CB_FORK_CEFR, CB_FORK_TOPICS, CB_BACK + "fork", CB_BACK + "blvl"):
            target = {CB_FORK_CEFR: "cefr", CB_FORK_TOPICS: "book_entry",
                      CB_BACK + "fork": "fork", CB_BACK + "blvl": "books"}[data]
        else:
            parts = data.split(":")
            if not data.startswith(V2) or len(parts) < 4:
                raise wf.FilterCommandError("stale")
            op, revision = parts[2], int(parts[3])
            if str(revision) != parts[3] or revision != state["revision"]:
                raise wf.FilterCommandError("stale")
            if op == "n" and 5 <= len(parts) <= 6:
                target = parts[4]
                level = parts[5] if len(parts) == 6 else None
                if target not in ("fork", "cefr", "book_entry", "books", "topics"):
                    raise wf.FilterCommandError("invalid")
                if target == "topics" and (level not in state["available_levels"]
                        or (state["mode"] == wf.MODE_TOPICS and level not in state["config"]["selected_levels"])):
                    raise wf.FilterCommandError("invalid")
            elif op in ("c", "l") and len(parts) == 5:
                state = wf.apply_filter_command(user_id, revision, "cefr" if op == "c" else "level", parts[4])
                target = "cefr" if op == "c" else "books"
            elif op == "t" and len(parts) == 6:
                level = parts[4]
                state = wf.apply_filter_command(user_id, revision, "topic", level, parts[5])
                target = "topics"
            elif op == "a" and len(parts) == 4:
                state = wf.apply_filter_command(user_id, revision, "all")
                target = "topics"
            else:
                raise wf.FilterCommandError("invalid")
    except PermissionError:
        await query.answer(t(lang, "wf_refresh"), show_alert=True)
        return
    except ValueError as exc:
        alert = t(lang, "wf_need_one" if str(exc) == "need_one" else "wf_refresh")
        state = wf.read_filter_state(user_id)
    if target == "book_entry":
        # Explicit S3 Back keeps the "books" route, including already-issued
        # callbacks. Only entering from the fork may skip the level screen.
        target = "books"
        if len(state["available_levels"]) == 1:
            sole_level = state["available_levels"][0]
            # A shortcut must not hide the explicit level-selection recovery.
            # Inactive valid branches still support the fresh first topic tap.
            can_browse = state["valid"] and (
                state["mode"] != wf.MODE_TOPICS or sole_level in state["config"]["selected_levels"])
            if can_browse:
                target, level = "topics", sole_level
    render = {"fork": render_fork, "cefr": render_cefr, "books": render_booklevels}
    if target == "topics":
        text, kb = render_topics(user_id, level, state)
    else:
        text, kb = render[target](user_id, state)
    await query.answer(alert, show_alert=bool(alert))
    try:
        await query.edit_message_text(text, reply_markup=kb)
    except Exception:
        logger.debug("word_filter edit skipped", exc_info=True)
