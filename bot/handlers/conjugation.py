"""Preparation carousel and per-time transitions into persistent conjugation tests."""

import logging
import uuid

from ..common.verb_construction import display_infinitive, construction_label, reflexive_hint
from ..study.generations import frame_generations

from ..common import match_policy

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ..app_config import CONJUGATION_MENU, get_db_path
from ..common.conjugation import pick_dice_pronoun
from ..common.lang import _mode_l1_de, _user_dict_lang, _user_interface_lang
from ..common.navigation import _action_key_from_text, _back_to_tests, _reply_text_with_retry
from ..common.study_lock import study_lock, invalidate_study_frames
from ..conjugation.service import ConjugationService
from ..i18n import t
from ..ui.formatting import _format_conjugation_card

logger = logging.getLogger(__name__)
FRAME = "conjugation_frame"
_CURRENT_ID = "conjugation_current_id"
_SESSION_COUNT = "conjugation_session_count"
_CARD_ENTRIES = "conjugation_card_entries"
_DICE_MSG_ID = "conjugation_dice_msg_id"
_DICE_RECENT = "conjugation_dice_recent"
_CALLBACK_RECEIPTS = "conjugation_callback_receipts"


def _service():
    return ConjugationService(get_db_path())


def _transport_id(update):
    # Real Telegram Updates always carry update_id. The fallback supports the
    # isolated handler stubs; object addresses can be reused after collection.
    value = getattr(update, "update_id", None)
    if value is None:
        value = getattr(update, "_conjugation_transport_id", None)
        if value is None:
            value = uuid.uuid4().int % (2**63-1)
            update._conjugation_transport_id = value
    return int(value)


def _new_frame(pair, kind):
    return {"flow_id": uuid.uuid4().hex, "pair": pair, "kind": kind,
            "view_revision": 0, "callbacks": {}, "touched": [], "last_answer": None,
            "question": None, "entry": None, "blocks": [], "scheduled": []}


def _recovered_undo(context, result):
    if result.get("recovered_undo") and not result.get("replayed"):
        invalidate_study_frames(context)
        return True
    return False


def _remember_callback_commit(context, transport_id):
    """Cache only live delivery acknowledgement, never a restorable old view.

    The service ledger remains authoritative for effects. This bounded cache
    bridges commit/send/ConversationHandler-state assignment in this process.
    """
    receipts = context.user_data.setdefault(_CALLBACK_RECEIPTS, {})
    receipts[transport_id] = True
    while len(receipts) > 64:
        receipts.pop(next(iter(receipts)))


def _carousel_markup(user_id):
    lang = _user_interface_lang(user_id)
    return ReplyKeyboardMarkup([
        [t(lang, "conjugation_next"), t(lang, "conjugation_dice")],
        [t(lang, "conjugation_test"), t(lang, "conjugation_stop")],
    ], resize_keyboard=True)


def _reset_dice(context):
    context.user_data.pop(_DICE_MSG_ID, None)
    context.user_data.pop(_DICE_RECENT, None)



def _selected_generations(frame):
    expected = frame_generations(frame.get('entry'))
    question = frame.get('question')
    if question:
        expected[question['word_id']] = question.get('study_generation', 0)
    for block in frame.get('scheduled', []):
        expected.setdefault(block['word_id'], block.get('study_generation', 0))
    return expected


def _block_button(frame, block, operation, lang, include_word=False):
    token = uuid.uuid4().hex[:16]
    frame["callbacks"][token] = {
        "word_id": block["word_id"], "tense": block["tense"],
        "revision": block["revision"], "cycle": block["cycle"], "operation": operation,
        "study_generation": block.get("study_generation", 0),
    }
    label = t(lang, "conjugation_ready" if operation == "ready" else "conjugation_return").format(
        tense="Präsens" if block["tense"] == "praesens" else "Präteritum")
    if include_word:
        entry = block.get("entry") or {}
        label = (display_infinitive(entry) or block["word_id"]) + reflexive_hint(entry) + " · " + label
    return InlineKeyboardButton(label, callback_data="conj:" + operation + ":" + token)


def _hide_markup(user_id, masked, context=None):
    lang = _user_interface_lang(user_id)
    rows = [[InlineKeyboardButton(t(lang, "conjugation_show" if masked else "conjugation_hide"),
                                  callback_data="conj:show" if masked else "conj:hide")]]
    if context:
        frame = context.user_data.get(FRAME) or {}
        for block in frame.get("blocks", []):
            rows.append([_block_button(frame, block, "ready" if block["phase"] == "preparation" else "reset", lang)])
    return InlineKeyboardMarkup(rows)


async def _strip_card_buttons(update, context):
    entries = context.user_data.get(_CARD_ENTRIES) or {}
    for mid in entries:
        if mid is None:
            continue
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=update.effective_chat.id, message_id=mid, reply_markup=None)
        except Exception:
            logger.debug("stripping old conjugation buttons failed", exc_info=True)


async def _show_card(update, context, entry):
    uid = update.effective_user.id
    await _strip_card_buttons(update, context)
    context.user_data[_CURRENT_ID] = entry.get("id")
    _reset_dice(context)
    sent = await _reply_text_with_retry(
        update.message, _format_conjugation_card(entry, _user_dict_lang(uid)),
        parse_mode="Markdown", reply_markup=_hide_markup(uid, False, context))
    mid = getattr(sent, "message_id", None)
    context.user_data[_CARD_ENTRIES] = {mid: {"entry": entry, "masked": False}} if mid is not None else {}


async def _close_stale(update, context):
    context.user_data.pop(FRAME, None)
    context.user_data.pop(_CURRENT_ID, None)
    context.user_data.pop(_CARD_ENTRIES, None)
    _reset_dice(context)
    await _reply_text_with_retry(update.message, t(_user_interface_lang(update.effective_user.id), 'conjugation_stale'))
    return await _back_to_tests(update, context)


async def _render_preparation(update, context, result, primer=False):
    uid = update.effective_user.id
    if _recovered_undo(context, result):
        return await _back_to_tests(update, context)
    frame = context.user_data[FRAME]
    if result.get("stale"):
        if result.get('replayed'):
            return CONJUGATION_MENU
        return await _close_stale(update, context)
    if result.get("replayed"):
        if (not frame.get("pending_render")
                or frame.get("pending_render_transport") != _transport_id(update)
                or frame.get("pending_render_revision") != frame["view_revision"]):
            return CONJUGATION_MENU
        primer = True
    else:
        prepared = result.get("preparation")
        frame["view_revision"] += 1
        frame["scheduled"] = result.get("scheduled", [])
        frame["entry"] = prepared["entry"] if prepared else None
        frame["blocks"] = prepared["blocks"] if prepared else []
    frame["callbacks"] = {}
    frame["pending_render"] = True
    frame["pending_render_transport"] = _transport_id(update)
    frame["pending_render_revision"] = frame["view_revision"]
    if primer:
        await _reply_text_with_retry(update.message, t(_user_interface_lang(uid), "conjugation"),
                                     reply_markup=_carousel_markup(uid))
    if frame["entry"]:
        await _show_card(update, context, frame["entry"])
    else:
        await _strip_card_buttons(update, context)
        context.user_data.pop(_CURRENT_ID, None)
        context.user_data.pop(_CARD_ENTRIES, None)
        _reset_dice(context)
        await _reply_text_with_retry(update.message, t(_user_interface_lang(uid), "conjugation_preparation_empty"),
                                     reply_markup=_carousel_markup(uid))
    frame["pending_render"] = False
    return CONJUGATION_MENU


async def start_conjugation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with study_lock(update.effective_user.id):
        uid = update.effective_user.id
        old = context.user_data.get(FRAME)
        flow = old["flow_id"] if old and old.get("kind") == "preparation" else uuid.uuid4().hex
        result = _service().preparation(uid, _mode_l1_de(uid), _transport_id(update), flow, new_session=True)
        if _recovered_undo(context, result):
            return await _back_to_tests(update, context)
        if result.get("replayed"):
            if old and old.get("kind") == "preparation":
                return await _render_preparation(update, context, result)
            return CONJUGATION_MENU
        context.user_data[FRAME] = _new_frame(_mode_l1_de(uid), "preparation")
        context.user_data[FRAME]["flow_id"] = flow
        context.user_data[FRAME][match_policy.FRAME_KEY] = result.get(match_policy.FRAME_KEY, {})
        context.user_data[_SESSION_COUNT] = 0
        context.user_data.pop(_CURRENT_ID, None)
        context.user_data.pop(_CARD_ENTRIES, None)
        _reset_dice(context)
        return await _render_preparation(update, context, result, primer=True)


async def _roll_dice(update, context):
    uid = update.effective_user.id
    frame = context.user_data[FRAME]
    if not frame.get("entry"):
        return CONJUGATION_MENU
    recent = context.user_data.get(_DICE_RECENT) or []
    receipt = _service().control(uid, frame["pair"], _transport_id(update), frame["flow_id"],
                                 "conjugation_dice", lambda: {"pronoun": pick_dice_pronoun(recent[-2:])},
                                 expected_generations=_selected_generations(frame))
    if _recovered_undo(context, receipt):
        return await _back_to_tests(update, context)
    if receipt.get("stale"):
        if receipt.get('replayed'):
            return CONJUGATION_MENU
        return await _close_stale(update, context)
    if receipt.get("replayed"):
        return CONJUGATION_MENU
    pronoun = receipt["pronoun"]
    context.user_data[_DICE_RECENT] = (recent + [pronoun])[-2:]
    try:
        await update.message.delete()
    except Exception:
        logger.debug("could not delete dice tap", exc_info=True)
    text = t(_user_interface_lang(uid), "conjugation_dice_prompt").format(pronoun=pronoun)
    mid = context.user_data.get(_DICE_MSG_ID)
    if mid is not None:
        try:
            await context.bot.edit_message_text(chat_id=update.effective_chat.id, message_id=mid, text=text)
            return CONJUGATION_MENU
        except Exception:
            logger.debug("dice edit failed", exc_info=True)
    sent = await context.bot.send_message(chat_id=update.effective_chat.id, text=text)
    context.user_data[_DICE_MSG_ID] = getattr(sent, "message_id", None)
    return CONJUGATION_MENU


async def conjugation_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with study_lock(update.effective_user.id):
        uid = update.effective_user.id
        frame = context.user_data.get(FRAME)
        lang = _user_interface_lang(uid)
        if not frame or frame.get("kind") != "preparation" or frame["pair"] != _mode_l1_de(uid):
            return await _back_to_tests(update, context)
        text = (update.message.text or "").strip()
        action = _action_key_from_text(text, uid)
        if text == t(lang, "conjugation_stop") or action in ("stop", "back"):
            receipt = _service().stop(uid, frame["pair"], frame["flow_id"], _transport_id(update),
                                      expected_generations=_selected_generations(frame))
            if _recovered_undo(context, receipt):
                return await _back_to_tests(update, context)
            if receipt.get("replayed"):
                return CONJUGATION_MENU
            await _strip_card_buttons(update, context)
            count = context.user_data.pop(_SESSION_COUNT, 0)
            context.user_data.pop(FRAME, None)
            context.user_data.pop(_CURRENT_ID, None)
            context.user_data.pop(_CARD_ENTRIES, None)
            _reset_dice(context)
            await _reply_text_with_retry(update.message, t(lang, "conjugation_session").format(count=count))
            return await _back_to_tests(update, context)
        if text == t(lang, "conjugation_test"):
            from .conjugation_test import _start_test_locked
            return await _start_test_locked(update, context)
        if action == "conjugation_dice":
            return await _roll_dice(update, context)
        if text == t(lang, "conjugation_next"):
            viewed = context.user_data.get(_CURRENT_ID)
            result = _service().preparation(uid, frame["pair"], _transport_id(update), frame["flow_id"], viewed_id=viewed,
                expected_generation=(frame.get("entry") or {}).get("study_generation", 0), rules=match_policy.rules(frame))
            if not result.get("replayed") and not result.get("stale") and viewed:
                context.user_data[_SESSION_COUNT] = context.user_data.get(_SESSION_COUNT, 0) + 1
            return await _render_preparation(update, context, result)
        await _reply_text_with_retry(update.message, t(lang, "conjugation_next") + " / " + t(lang, "conjugation_test"),
                                     reply_markup=_carousel_markup(uid))
        return CONJUGATION_MENU


async def conjugation_card_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with study_lock(update.effective_user.id):
        query = update.callback_query
        uid = update.effective_user.id
        transport = _transport_id(update)
        # Consult the accepted delivery before resolving today's frame/token.
        # Reset has already replaced its old token map before the first send.
        committed = transport in context.user_data.get(_CALLBACK_RECEIPTS, {})
        try:
            await query.answer()
        except TelegramError:
            # Callback acknowledgement can expire before a committed delivery
            # is retried. It does not determine whether its view can recover.
            logger.debug("conjugation callback acknowledgement failed", exc_info=True)
        # Reuse the actual callback message as the delivery destination.
        proxy_update = type("ConjugationCallbackUpdate", (), {})()
        proxy_update.message = query.message
        proxy_update.effective_user = update.effective_user
        proxy_update.effective_chat = update.effective_chat
        proxy_update.update_id = transport
        frame = context.user_data.get(FRAME) or {}
        if not frame or frame.get("pair") != _mode_l1_de(uid):
            return
        if committed:
            if frame.get("kind") == "preparation":
                return await _render_preparation(proxy_update, context, {"replayed": True})
            from .conjugation_test import _live_state
            return _live_state(context)
        data = (query.data or "").split(":")
        if len(data) == 3 and data[1] == "page" and frame.get("kind") == "test":
            target = frame.get("callbacks", {}).get(data[2])
            if not target or target["operation"] != "page":
                return
            result = _service().control(uid, frame["pair"], _transport_id(update), frame["flow_id"],
                                         "conjugation_page", lambda: {"page": target["page"]},
                                         expected_generations=_selected_generations(frame))
            if _recovered_undo(context, result):
                return await _back_to_tests(proxy_update, context)
            if result.get('stale'):
                if result.get('replayed'):
                    return
                return await _close_stale(proxy_update, context)
            if not result.get("replayed"):
                _remember_callback_commit(context, transport)
                frame["page"] = result["page"]
                from .conjugation_test import _render_test_locked
                return await _render_test_locked(proxy_update, context)
            return
        if len(data) == 3 and data[1] in ("ready", "reset"):
            target = frame.get("callbacks", {}).get(data[2])
            if not target or target["operation"] != data[1]:
                return
            result = _service().transition(
                uid, frame["pair"], target["word_id"], target["tense"], target["revision"],
                target["cycle"], target["operation"], _transport_id(update), frame["flow_id"],
                expected_generation=target.get("study_generation", 0), rules=match_policy.rules(frame))
            if _recovered_undo(context, result):
                return await _back_to_tests(proxy_update, context)
            if result.get("stale"):
                if result.get('replayed'):
                    return
                return await _close_stale(proxy_update, context)
            if result.get("replayed"):
                return
            _remember_callback_commit(context, transport)
            if frame["kind"] == "test":
                context.user_data[FRAME] = _new_frame(frame["pair"], "preparation")
                context.user_data[FRAME][match_policy.FRAME_KEY] = frame.get(match_policy.FRAME_KEY, {})
                return await _render_preparation(proxy_update, context, result, primer=True)
            return await _render_preparation(proxy_update, context, result)
        if len(data) != 2 or data[1] not in ("hide", "show") or frame.get("kind") != "preparation":
            return
        entries = context.user_data.get(_CARD_ENTRIES) or {}
        state = entries.get(query.message.message_id)
        if not state:
            return
        receipt = _service().control(uid, frame["pair"], _transport_id(update), frame["flow_id"],
                                     "conjugation_visibility", lambda: {"masked": data[1] == "hide"},
                                     expected_generations=_selected_generations(frame))
        if _recovered_undo(context, receipt):
            return await _back_to_tests(proxy_update, context)
        if receipt.get("stale"):
            if receipt.get('replayed'):
                return
            return await _close_stale(proxy_update, context)
        if receipt.get("replayed"):
            return
        _remember_callback_commit(context, transport)
        state["masked"] = receipt["masked"]
        try:
            await query.edit_message_text(
                _format_conjugation_card(state["entry"], _user_dict_lang(uid), mask=state["masked"]),
                parse_mode="Markdown", reply_markup=_hide_markup(uid, state["masked"], context))
        except Exception:
            logger.debug("hide toggle edit skipped", exc_info=True)
