"""Search snapshots, message-bound actions, and resumable card delivery."""

import logging
import secrets

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity

from ..app_config import DB_SEARCH, DB_SEARCH_VIEW, get_db_path
from ..i18n import t
from ..search import normalize_lookup, read_search_entry
from .lang import _user_interface_lang


logger = logging.getLogger(__name__)
SEARCH_KEY = "db_search_snapshot"
ORIGIN_KEY = "search_edit_origin"
CALLBACK_PATTERN = r"^ds:"
PAGE_SIZE = 5
MESSAGE_BUDGET = 3500


def utf16_length(value):
    return len(str(value).encode("utf-16-le")) // 2


def split_card(text, word_id):
    """Split rendered text, including single long lines, without cutting Unicode."""
    # Reserve room for the identity/part heading and use entities instead of
    # Markdown wrappers: arbitrary backticks cannot terminate the card's block.
    label = str(word_id)
    if utf16_length(label) > 100:
        label = label[:40] + "…"
    prefix = f"id: {label}"
    limit = MESSAGE_BUDGET - utf16_length(prefix) - 80
    bodies, current, size = [], "", 0
    for line in str(text).splitlines(keepends=True):
        if size + utf16_length(line) <= limit:
            current += line
            size += utf16_length(line)
            continue
        if current:
            bodies.append(current)
            current, size = "", 0
        for char in line:
            units = utf16_length(char)
            if size + units > limit:
                bodies.append(current)
                current, size = "", 0
            current += char
            size += units
    if current or not bodies:
        bodies.append(current)
    return [f"{prefix} [{i}/{len(bodies)}]\n{body}"
            for i, body in enumerate(bodies, 1)]


def new_snapshot(context, user_id, chat_id, token, result):
    if context.user_data.pop(ORIGIN_KEY, None):
        context.user_data.pop("quiz_edit_entry", None)
        context.user_data.pop("edit_field", None)
        context.user_data.pop("prep_cascade", None)
    previous = context.user_data.get(SEARCH_KEY) or {}
    state = {**result, "query_id": secrets.token_urlsafe(8),
             "revision": previous.get("revision", 0) + 1,
             "user_id": user_id, "chat_id": chat_id,
             "normalized_query": normalize_lookup(token), "page_cursor": 0,
             "bindings": {}, "messages": {}, "pending": None, "page": None}
    context.user_data[SEARCH_KEY] = state
    return state


def _new_binding(state, action, **extra):
    token = secrets.token_urlsafe(12)
    state["bindings"][token] = {
        "action": action, "user_id": state["user_id"], "chat_id": state["chat_id"],
        "query_id": state["query_id"], "revision": state["revision"],
        "message_id": None, **extra,
    }
    return token


def _markup(label, token):
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"ds:{token}")]])


async def _navigation(message, state, lang, retry=False):
    if not retry and state["page_cursor"] >= len(state["ordered_ids"]):
        return
    token = _new_binding(state, "page", cursor=state["page_cursor"])
    try:
        sent = await message.reply_text(
            t(lang, "search_delivery_retry" if retry else "search_continues"),
            reply_markup=_markup(t(lang, "search_retry" if retry else "search_more"), token),
        )
    except Exception:
        state["bindings"].pop(token, None)
        raise
    state["bindings"][token]["message_id"] = sent.message_id


async def send_page(message, context):
    """Advance only confirmed chunks/IDs; a failed page can resume its tail."""
    from ..ui.formatting import _format_word_card

    state = context.user_data[SEARCH_KEY]
    uid, lang = state["user_id"], _user_interface_lang(state["user_id"])
    if state["page"] is None:
        state["page"] = {"start": state["page_cursor"],
                         "stop": min(state["page_cursor"] + PAGE_SIZE, len(state["ordered_ids"])),
                         "header_sent": False}
    page = state["page"]
    try:
        if not page["header_sent"]:
            kind = t(lang, "search_exact" if state["kind"] == "exact" else "search_similar")
            heading = t(lang, "search_page").format(
                kind=kind, start=page["start"] + 1, end=page["stop"], total=len(state["ordered_ids"]))
            if state["has_more_similar"]:
                heading += "\n" + t(lang, "search_similar_limit")
            await message.reply_text(heading)
            page["header_sent"] = True
        while state["page_cursor"] < page["stop"]:
            wid = state["ordered_ids"][state["page_cursor"]]
            entry = read_search_entry(get_db_path(), wid)
            if entry is None:
                await message.reply_text(t(lang, "search_card_unavailable").format(id=wid))
                state["page_cursor"] += 1
                state["pending"] = None
                continue
            pending = state["pending"]
            if pending is None:
                pending = {"word_id": wid, "next_chunk": 0,
                           "chunks": split_card(_format_word_card(entry, user_id=uid), wid)}
                state["pending"] = pending
            while pending["next_chunk"] < len(pending["chunks"]):
                index = pending["next_chunk"]
                chunk = pending["chunks"][index]
                token = None
                markup = None
                if index == len(pending["chunks"]) - 1:
                    token = _new_binding(state, "card", word_id=wid)
                    markup = _markup(t(lang, "clarify_fields"), token)
                try:
                    sent = await message.reply_text(
                        chunk, entities=[MessageEntity(type="pre", offset=0, length=utf16_length(chunk))],
                        reply_markup=markup,
                    )
                except Exception:
                    if token:
                        state["bindings"].pop(token, None)
                    raise
                state["messages"][sent.message_id] = wid
                if token:
                    state["bindings"][token]["message_id"] = sent.message_id
                pending["next_chunk"] += 1
            state["page_cursor"] += 1
            state["pending"] = None
        await _navigation(message, state, lang)
        state["page"] = None
    except Exception:
        # A timeout can mean Telegram accepted the message. Retrying may repeat
        # that chunk, but it never advances to another ID or performs a grade.
        logger.warning("Search delivery interrupted; confirmed cursor retained", exc_info=True)
        try:
            await _navigation(message, state, lang, retry=True)
        except Exception:
            logger.warning("Search retry control could not be delivered", exc_info=True)
    return DB_SEARCH_VIEW


def resolve_binding(update, context):
    query = update.callback_query
    state = context.user_data.get(SEARCH_KEY)
    if not state or not query or not query.message:
        return None
    token = str(query.data or "").removeprefix("ds:")
    bound = state["bindings"].get(token)
    if not bound:
        return None
    if (query.from_user.id != state["user_id"] or update.effective_chat.id != state["chat_id"]
            or bound["user_id"] != query.from_user.id or bound["chat_id"] != update.effective_chat.id
            or bound["message_id"] != query.message.message_id
            or bound["query_id"] != state["query_id"] or bound["revision"] != state["revision"]):
        return None
    return state, token, bound


async def stale_callback(update, context):
    """ACK a search control outside search, without changing conversation state."""
    await _answer(update.callback_query, t(_user_interface_lang(update.effective_user.id), "search_stale"))
    return None


async def _answer(query, text=None):
    """Expired ACKs do not turn a valid message action into a failed delivery."""
    try:
        await query.answer(text)
    except Exception:
        logger.warning("Search callback acknowledgement failed", exc_info=True)


async def search_callback(update, context):
    from ..handlers.access import access_allowed
    from ..handlers.clarify_fields import open_search_entry
    from .study_lock import study_lock

    async with study_lock(update.effective_user.id):
        resolved = resolve_binding(update, context)
        if not resolved or not access_allowed(update.effective_user.id):
            return await stale_callback(update, context)
        state, token, bound = resolved
        if bound["action"] == "page":
            if bound["cursor"] != state["page_cursor"]:
                return await stale_callback(update, context)
            await _answer(update.callback_query)
            state["bindings"].pop(token, None)
            return await send_page(update.callback_query.message, context)
        entry = read_search_entry(get_db_path(), bound["word_id"])
        if entry is None:
            await _answer(update.callback_query, t(_user_interface_lang(state["user_id"]), "search_stale"))
            await update.callback_query.message.reply_text(
                t(_user_interface_lang(state["user_id"]), "search_card_unavailable").format(id=bound["word_id"]))
            return None
        await _answer(update.callback_query)
        origin = {key: bound[key] for key in ("user_id", "chat_id", "query_id", "revision", "word_id")}
        origin["page"] = state["page_cursor"]
        return await open_search_entry(update, context, entry["id"], origin)


def with_search_callbacks(states):
    """Install state-aware handlers inside the owning ConversationHandler."""
    from telegram.ext import CallbackQueryHandler
    for state, handlers in states.items():
        callback = search_callback if state in (DB_SEARCH, DB_SEARCH_VIEW) else stale_callback
        handlers.append(CallbackQueryHandler(callback, pattern=CALLBACK_PATTERN))
    return states


async def return_to_search(update, context, prefix_text=None):
    """Return to the saved query without repeating its cards or touching study."""
    from ..handlers.db_search import _db_search_markup

    uid = update.effective_user.id
    lang = _user_interface_lang(uid)
    context.user_data.pop(ORIGIN_KEY, None)
    context.user_data.pop("quiz_edit_entry", None)
    context.user_data.pop("edit_field", None)
    context.user_data.pop("prep_cascade", None)
    message = update.effective_message
    await message.reply_text(prefix_text or t(lang, "search_return"), reply_markup=_db_search_markup(uid))
    state = context.user_data.get(SEARCH_KEY)
    if state and state["user_id"] == uid and state["chat_id"] == update.effective_chat.id:
        await _navigation(message, state, lang, retry=state["page"] is not None)
    return DB_SEARCH_VIEW
