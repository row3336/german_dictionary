"""Learning mode: the learning menu, the preview/quiz flow, the static show
order (first round in preview order, later rounds random with spacing) plus the
separate topic-mix word selection, 👍/👎 answer reactions, the plain
end-of-queue service message, batch top-up, input-err correction, and the helper
that pushes a quiz entry into the learning batch (used by quiz / clarify_fields /
db_search).
"""

import random
import uuid
from copy import deepcopy
from datetime import datetime, timezone

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..app_config import (
    I_DONT_KNOW_TEXT,
    LEARNING_DAILY_QTY_KEY,
    LEARNING_DAILY_QTY_DEFAULT,
    LEARNING_MENU,
    LEARNING_QUIZ,
    TESTS_MENU,
    get_db_path,
)
from ..common.bot_media import resolve_bot_word_media
from ..common.lang import _user_dict_lang, _user_interface_lang
from ..common.navigation import _action_key_from_text, _reply_text_with_retry
from ..common.settings import (
    _learning_daily_qty,
    _learning_intervals_text,
    _learning_mix_mode_enabled,
    _learning_ready_quorum,
    _topic_filter_banner,
)
from ..common.word_filter import count_words_for_filter, resolve_quiz_filter
from ..common.study_lock import study_lock, invalidate_study_frames
from ..common.quiz_resume import register_renderer, publish_view, RETURN_POINT_KEY, PUBLISHED_VIEW_KEY
from .. import learning_store, learning_requests
from ..learning_requests import EnqueueResult, frame_lifecycles
from ..study import answers, deliveries, repository
from ..study.generations import frame_generations, assert_generations, StaleGeneration
from ..common.verb_construction import display_infinitive, construction_label, reflexive_hint, InvalidClassification, InvalidForms, validate_quiz_entry
from ..handlers.quiz import _check_ru_de_strict, _set_reaction
from ..i18n import t
from ..storage import (
    learning_due_entries,
    learning_preview_entry,
    learning_status_snapshot,
    list_learning_active_words,
)
from ..text_utils import cap_word as _cap_word
from ..text_utils import low_word as _low_word
from ..ui.formatting import (
    _entry_translation,
    _format_feedback_ru_de,
    _format_learning_preview_prompt,
    _format_ru_question,
    _format_topic_learning_line,
)
from ..ui.keyboards import _learning_menu_keys, _tests_menu_keys


def _format_eta_from_iso(next_due_iso):
    """Handle format eta from iso."""
    token = str(next_due_iso or "").strip()
    if not token:
        return "-"
    try:
        when = datetime.strptime(token, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return token
    now = datetime.now(timezone.utc)
    delta = int((when - now).total_seconds())
    if delta <= 0:
        return "now"
    minutes, seconds = divmod(delta, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _learning_empty_text(user_id):
    """Service message when the running queue has nothing to show now.

    Distinguishes 'the selected topics hold no words at all' (spec #8) from
    'words exist but nothing is due yet' — in the latter case the next-due ETA is
    appended, but only when it is a real future time (skip the '-'/'now' filler).

    NOTE: this text is plain (it contains 'next_due', whose underscore would open
    an unterminated italic entity under parse_mode='Markdown'). It must never be
    sent with parse_mode='Markdown'.
    """
    if count_words_for_filter(user_id) == 0:
        return t(_user_interface_lang(user_id), "topic_no_words_learning")
    snapshot = learning_status_snapshot(get_db_path(), user_id, _learning_ready_quorum(get_db_path(), user_id))
    base = t(_user_interface_lang(user_id), "learning_no_due")
    eta = _format_eta_from_iso(snapshot.get("next_due"))
    if eta in ("-", "now"):
        return base
    # "next_due = <eta>" kept as EN technical readout (matches the menu diagnostic block)
    return f"{base}\nnext_due = {eta}"


# A word must not reappear within this many consecutive shows (>= once per three).
_LEARNING_RECENT_SHOWS = 2


def _pick_learning_due_entry(context, user_id, conn=None):
    """Pick the next word to show in the running learning queue.

    STATIC show rule — identical whether or not mix mode is on (mix only shapes
    which words enter the queue, never their show order; see storage.py:
    learning_pick_new_words):
      - first round (freshly previewed, step_index == 0): strict preview order
        (words as they were reviewed, i.e. created_at ASC from the query);
      - later rounds (step_index > 0): random, but a word is not shown again
        within the last two shows — at most once per three consecutive shows.
    Recent shows are tracked in context ("learning_recent_ids"); when the pool is
    too small to honour the spacing, it relaxes as far as needed. An available
    alternative to the last shown word is selected before step priority is
    applied, so a recovering word cannot jump ahead of that alternative and
    immediately repeat.
    """
    due_entries = (learning_store.due_entries(conn, user_id) if conn is not None
                   else learning_due_entries(get_db_path(), user_id=user_id))
    if not due_entries:
        return None
    recent = context.user_data.get("learning_recent_ids") or []
    last_shown = recent[-1] if recent else None
    alternatives = [entry for entry in due_entries if entry.get("id") != last_shown]
    selection_entries = alternatives or due_entries

    first_round = [entry for entry in selection_entries if int(entry.get("learning_step") or 0) == 0]
    if first_round:
        # already ordered by created_at ASC (preview order) in the query
        return first_round[0]

    pool = [entry for entry in selection_entries if entry.get("id") not in recent]
    if not pool:
        pool = selection_entries
    return random.choice(pool)


def _note_learning_shown(context, word_id):
    """Record a shown word for the later-round spacing window (keep last N ids)."""
    if not word_id:
        return
    recent = context.user_data.get("learning_recent_ids") or []
    recent.append(word_id)
    context.user_data["learning_recent_ids"] = recent[-_LEARNING_RECENT_SHOWS:]


async def _apply_learning_reaction(update, context, ok):
    """React to the learning answer message: 👍 correct, 👎 wrong. Flat — no streak
    escalation — mirroring the FSRS quiz reaction contract. Best-effort. The
    message id is remembered so an input_err correction can lift the 👎 later."""
    context.user_data["last_learning_answer_message_id"] = update.message.message_id
    emoji = "\U0001F44D" if ok else "\U0001F44E"  # 👍 / 👎
    await _set_reaction(context, update.effective_user.id, update.message.message_id, [emoji])


FRAME = "learning_frame"
_PENDING_NAVIGATION = "quiz_learning_pending_navigation"


def _transport_id(update):
    value = getattr(update, "update_id", None)
    if value is None:
        value = getattr(update, "_learning_transport_id", None)
        if value is None:
            value = uuid.uuid4().int & ((1 << 63) - 1)
            update._learning_transport_id = value
    return int(value)


def _new_frame():
    return {"flow_id": uuid.uuid4().hex, "revision": 0, "phase": "completed",
            "current": None, "last_answer": None, "last_visible_id": None,
            "pending_render": False, "accepted_delivery": None}


def _legacy_frame(context):
    frame = context.user_data.get(FRAME)
    if frame:
        return frame
    frame = _new_frame()
    entry = context.user_data.get("quiz_entry")
    if entry:
        frame.update(current={"entry": entry, "action_key": uuid.uuid4().hex}, revision=1,
                     phase="preview" if context.user_data.get("learning_preview_mode") else "question")
    last = context.user_data.get("quiz_last_answered_entry")
    frame["last_visible_id"] = last.get("id") if last else None
    context.user_data[FRAME] = frame
    return frame


def _delivery_receipt(update, context):
    uid, transport = update.effective_user.id, _transport_id(update)
    with repository.transaction(get_db_path()) as conn:
        answers.authorize_user(conn, uid)
        old = deliveries.lookup_delivery(conn, uid, transport)
        if not old:
            return None
        receipt = (answers.lookup_action(conn, uid, old["action_key"]) if old.get("action_key") else None)
        return {**(receipt or old["result"]), "replayed": True}


async def _present_learning_navigation(update, context, pending):
    uid = update.effective_user.id
    if pending["destination"] == LEARNING_MENU:
        state = await _send_learning_menu(update, uid)
    else:
        await _reply_text_with_retry(update.message, t(_user_interface_lang(uid), "tests") + ":",
            reply_markup=ReplyKeyboardMarkup(_tests_menu_keys(uid), resize_keyboard=True))
        state = TESTS_MENU
    if context.user_data.get(_PENDING_NAVIGATION) is pending:
        context.user_data.pop(_PENDING_NAVIGATION, None)
    return state


async def _leave_learning(update, context, receipt, destination):
    invalidate_study_frames(context)
    pending = {"transport": _transport_id(update), "flow_id": receipt["flow_id"],
               "destination": destination}
    context.user_data[_PENDING_NAVIGATION] = pending
    return await _present_learning_navigation(update, context, pending)


async def _handle_replay(update, context, receipt):
    pending = context.user_data.get(_PENDING_NAVIGATION)
    if (pending and pending["transport"] == _transport_id(update) and
            pending["flow_id"] == receipt.get("flow_id")):
        live_study = any(context.user_data.get(key) for key in (
            FRAME, "quiz_written_frame", "conjugation_frame", "study_frame",
            "quiz_entry", "quiz_published_view"))
        if not live_study:
            return await _present_learning_navigation(update, context, pending)
    frame = context.user_data.get(FRAME)
    if (frame and frame.get("pending_render") and
            frame.get("accepted_delivery") == _transport_id(update) and
            receipt.get("flow_id") == frame["flow_id"]):
        return await _render_learning_frame(update, context)
    return None  # Keep a newer conversation state/screen intact.


async def _drain_pending_learning_view(update, context):
    """Consume new input as delivery of an already committed unseen view."""
    frame = context.user_data.get(FRAME)
    if not frame or not frame.get("pending_render"):
        return False, None
    uid, transport = update.effective_user.id, _transport_id(update)
    try:
        result = answers.record_control_transition(get_db_path(), uid, transport,
            frame["flow_id"], "learning_drain_view",
            lambda conn: {"flow_id": frame["flow_id"], "revision": frame["revision"]},
            expected_generations=frame_generations((frame.get("current") or {}).get("entry")),
            expected_lifecycles=frame_lifecycles((frame.get("current") or {}).get("entry")))
    except StaleGeneration:
        answers.reject_stale(get_db_path(), uid, transport, frame["flow_id"])
        invalidate_study_frames(context)
        return True, await _send_learning_menu(update, uid)
    if result.get("replayed"):
        return True, await _handle_replay(update, context, result)
    if _learning_recovery(context, result):
        return True, await _send_learning_menu(update, uid)
    frame["accepted_delivery"] = transport
    return True, await _render_learning_frame(update, context)


def _send_entry_to_learning(update: Update, context: ContextTypes.DEFAULT_TYPE, entry_key="quiz_entry"):
    """Enqueue only; authorization/delivery/recovery precede mutable target reads."""
    uid, transport = update.effective_user.id, _transport_id(update)
    with repository.transaction(get_db_path()) as conn:
        answers.authorize_user(conn, uid)
        old = deliveries.lookup_delivery(conn, uid, transport)
        if old:
            result = {**old['result'], 'replayed': True}
        elif answers.recover_pending_undo(conn, uid):
            result = {'recovered_undo': True, 'enqueue_result': EnqueueResult.STALE.value}
            deliveries.begin_delivery(conn, uid, transport, 'send_learn', 'send_learn')
            deliveries.commit_delivery(conn, uid, transport, result)
        else:
            entry = context.user_data.get(entry_key) or {}
            status = learning_requests.enqueue(conn, uid, entry.get('id'), entry.get('study_generation', 0))
            result = {'enqueue_result': status.value, 'word_id': entry.get('id'),
                      'flow_id': (context.user_data.get(PUBLISHED_VIEW_KEY) or {}).get('flow_id', 'send_learn')}
            deliveries.begin_delivery(conn, uid, transport, result['flow_id'], 'send_learn')
            deliveries.commit_delivery(conn, uid, transport, result)
    result['transport_update_id'] = transport
    if result.get('recovered_undo') and not result.get('replayed'):
        invalidate_study_frames(context)
    context.user_data['learning_send_receipt'] = result
    # Historical receipts retain their old fields and are never reinterpreted.
    return EnqueueResult(result.get('enqueue_result', EnqueueResult.STALE.value))


def _learning_send_text(user_id, result):
    return t(_user_interface_lang(user_id), 'learning_request_' + str(result))


async def _send_learning_menu(update: Update, user_id):
    """Show the learning menu with a summary of the current session."""
    stats = learning_status_snapshot(
        get_db_path(),
        user_id=user_id,
        quorum_pct=_learning_ready_quorum(get_db_path(), user_id),
    )
    active_entries = list_learning_active_words(get_db_path(), user_id=user_id)
    active_lines = [_format_topic_learning_line(item) for item in active_entries]
    active_preview = active_lines[:30]
    if len(active_lines) > 30:
        active_preview.append(f"... +{len(active_lines) - 30} more")
    msg = "\n".join(
        [
            f"{t(_user_interface_lang(user_id), 'learning')}:",
            "",
            _topic_filter_banner(user_id),
            "",
            f"{t(_user_interface_lang(user_id), 'learning_daily_qty')} = {_learning_daily_qty(get_db_path(), user_id)}",
            f"{t(_user_interface_lang(user_id), 'learning_intervals')} = {_learning_intervals_text(get_db_path(), user_id)}",
            f"{t(_user_interface_lang(user_id), 'learning_mix_mode')} = {'ON' if _learning_mix_mode_enabled(user_id) else 'OFF'}",
            f"active = {stats.get('active_count', 0)}",
            f"preview_pending = {stats.get('preview_pending', 0)}",
            f"due_now = {stats.get('due_count', 0)} / need {stats.get('ready_needed', 0)}",
            f"completed_in_batch = {stats.get('completed_count', 0)}",
            f"next_due = {_format_eta_from_iso(stats.get('next_due'))}",
            "",
            "active words:",
            *(active_preview if active_preview else ["-"]),
        ]
    )
    await _reply_text_with_retry(
        update.message,
        msg,
        reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(user_id), resize_keyboard=True),
    )
    return LEARNING_MENU


def _format_learning_preview_line(entry, dict_lang):
    """Handle format learning preview line."""
    word_type = (entry.get("type") or "").strip().lower()
    if word_type == "verb":
        left = _low_word(display_infinitive(entry)) + reflexive_hint(entry)
    else:
        article = (entry.get("article") or "").strip()
        left = f"{article} {_cap_word(entry.get('german', ''))}".strip()
    right = _entry_translation(entry, dict_lang)
    return f"- {left} = {right}"


async def _learning_add_words(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    db = get_db_path()
    def add(conn):
        # Settings, capacity and the pool are read only after delivery preflight,
        # on the same writer connection as insert/consume/lifecycle/receipt.
        row = conn.execute('SELECT value FROM user_settings WHERE user_id=? AND key=?',
                           (uid, LEARNING_DAILY_QTY_KEY)).fetchone()
        if row is None:
            row = conn.execute('SELECT value FROM app_settings WHERE key=?', (LEARNING_DAILY_QTY_KEY,)).fetchone()
        try:
            qty = max(1, int(row[0] if row else LEARNING_DAILY_QTY_DEFAULT))
        except (ValueError, TypeError):
            qty = LEARNING_DAILY_QTY_DEFAULT
        qf = resolve_quiz_filter(uid, conn=conn)
        mix_row = conn.execute('SELECT value FROM app_settings WHERE key=?', (f'learning_mix_mode:{uid}',)).fetchone()
        mix = bool(mix_row and str(mix_row[0]).strip().lower() in {'1', 'true', 'yes', 'on'})
        return {**learning_requests.add_candidates(conn, uid, qty, qf.topics, qf.cefr_levels, mix), 'daily_qty': qty}

    result = answers.record_control_transition(db, uid, _transport_id(update), 'learning-menu', 'learning_add', add)
    if _learning_recovery(context, result):
        return await _send_learning_menu(update, uid)
    lang = _user_interface_lang(uid)
    if 'need' not in result:
        # Pre20 accepted Add receipts are historical; never select again.
        text = t(lang, 'learning_added').format(added=result.get('added', 0))
        await _reply_text_with_retry(update.message, text,
            reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(uid), resize_keyboard=True))
        return LEARNING_MENU
    text = (t(lang, "learning_batch_full").format(active=result["active_count"], daily=result["daily_qty"]) if not result["need"]
            else t(lang, "learning_added").format(added=result["added"]) if result["added"] else t(lang, "learning_no_new"))
    await _reply_text_with_retry(update.message, text,
        reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(uid), resize_keyboard=True))
    return LEARNING_MENU


def _next_learning_view(conn, context, user_id):
    entry = learning_store.preview_entry(conn, user_id)
    if entry:
        return {"entry": entry, "phase": "preview"}
    entry = _pick_learning_due_entry(context, user_id, conn=conn)
    active = conn.execute("SELECT 1 FROM learning_items WHERE user_id=? AND status='active' LIMIT 1", (user_id,)).fetchone()
    return {"entry": entry, "phase": "question" if entry else "waiting" if active else "completed"}


def _establish_learning_frame(context, user_id, prefix_text=None, force_entry=None, transport=None, plan=None):
    frame = deepcopy(context.user_data.get(FRAME) or _new_frame())
    entry = plan["entry"] if plan is not None else force_entry
    phase = plan["phase"] if plan is not None else "question"
    if plan is None and entry is None:
        entry = learning_preview_entry(get_db_path(), user_id=user_id)
        if entry:
            phase = "preview"
        else:
            entry = _pick_learning_due_entry(context, user_id)
    if entry:
        entry = deepcopy(entry)
        entry.setdefault('study_generation', 0)
        entry.setdefault('answer_aliases', [])
        frame["current"] = {"entry": entry, "action_key": uuid.uuid4().hex}
        frame["phase"] = phase
        context.user_data["quiz_entry"] = entry
        context.user_data["learning_preview_mode"] = phase == "preview"
        if phase == "question" and force_entry is None:
            _note_learning_shown(context, entry.get("id"))
    else:
        if plan is None:
            stats = learning_status_snapshot(get_db_path(), user_id)
            phase = "waiting" if stats["active_count"] else "completed"
        frame.update(current=None, phase=phase)
        context.user_data.pop("quiz_entry", None)
        context.user_data["learning_preview_mode"] = False
    frame.update(revision=frame["revision"] + 1, prefix_text=prefix_text,
                 accepted_delivery=transport, pending_render=True)
    context.user_data[FRAME] = frame
    return frame


async def _render_learning_frame(update, context, prefix_text=None):
    """Render established state only: never select or count another question."""
    from ..storage import get_word_by_id
    uid = update.effective_user.id
    frame = context.user_data.get(FRAME)
    if not frame:
        return await _send_learning_menu(update, uid)
    frame["pending_render"] = True
    prefix = frame.get("prefix_text") if prefix_text is None else prefix_text
    slot = frame.get("current")
    entry = slot["entry"] if slot else None
    if entry:
        stale = False
        with repository.transaction(get_db_path()) as conn:
            try:
                assert_generations(conn, uid, frame_generations(entry))
                learning_requests.assert_lifecycles(conn, uid, frame_lifecycles(entry))
            except StaleGeneration:
                stale = True
        if stale:
            invalidate_study_frames(context)
            return await _send_learning_menu(update, uid)
        fresh = get_word_by_id(get_db_path(), entry["id"])
        if not fresh:
            return await _learning_deleted(update, context, None, entry["id"], prefix)
        from ..study.generations import preserve_selected_entry
        slot["entry"] = preserve_selected_entry(fresh, entry)
        entry = resolve_bot_word_media(get_db_path(), uid, slot["entry"])
        context.user_data["quiz_entry"] = entry
    phase = frame["phase"]
    publish_view(context, caller_kind="learning", mode="learning", phase=phase,
                 current_question_id=entry["id"] if entry else None,
                 visible_answer_id=entry["id"] if phase == "preview" and entry else frame.get("last_visible_id"),
                 flow_id=frame["flow_id"], view_revision=frame["revision"])
    lang = _user_interface_lang(uid)
    if entry:
        text = (_format_learning_preview_prompt(entry, _user_dict_lang(uid)) if phase == "preview"
                else _format_ru_question(entry, _user_dict_lang(uid)))
        if prefix:
            text = prefix + "\n\n" + text
        rows = ([[t(lang, "learning_next"), t(lang, "learning_skip")],
                 [t(lang, "clarify_fields"), t(lang, "back")]] if phase == "preview" else
                [[I_DONT_KNOW_TEXT, t(lang, "input_err")],
                 [t(lang, "learning_skip"), t(lang, "clarify_fields"), t(lang, "back")]])
        await _reply_text_with_retry(update.message, text,
            reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True), parse_mode="Markdown")
    else:
        if prefix:
            await _reply_text_with_retry(update.message, prefix, parse_mode="Markdown")
        await _reply_text_with_retry(update.message, _learning_empty_text(uid),
            reply_markup=ReplyKeyboardMarkup([[t(lang, "back")]], resize_keyboard=True))
    frame["pending_render"] = False
    return LEARNING_QUIZ


async def _send_learning_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix_text, force_entry=None):
    _establish_learning_frame(context, update.effective_user.id, prefix_text, force_entry)
    return await _render_learning_frame(update, context)


async def _learning_existing(update, context, point, prefix_text):
    frame = context.user_data.get(FRAME)
    if not frame:
        _legacy_frame(context)
        frame = context.user_data[FRAME]
        frame.update(flow_id=point.flow_id, revision=point.view_revision, phase=point.phase)
    if point.flow_id != frame["flow_id"] or point.view_revision != frame["revision"]:
        return await _send_learning_menu(update, update.effective_user.id)
    return await _render_learning_frame(update, context, prefix_text)


async def _learning_deleted(update, context, point, word_id, prefix_text):
    frame = context.user_data.get(FRAME)
    if not frame:
        return await _send_learning_menu(update, update.effective_user.id)
    if frame.get("last_visible_id") == word_id:
        frame["last_visible_id"] = None
        frame["last_answer"] = None
    slot = frame.get("current")
    if slot and slot["entry"]["id"] == word_id:
        _establish_learning_frame(context, update.effective_user.id, prefix_text)
    return await _render_learning_frame(update, context, prefix_text)


async def learning_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with study_lock(update.effective_user.id):
        receipt = _delivery_receipt(update, context)
        if receipt is not None:
            return await _handle_replay(update, context, receipt)
        context.user_data.pop(_PENDING_NAVIGATION, None)
        drained, state = await _drain_pending_learning_view(update, context)
        if drained:
            return state
        return await _learning_menu_locked(update, context)


async def _learning_menu_locked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    action = _action_key_from_text(update.message.text.strip(), uid)
    if action == "learning_add":
        return await _learning_add_words(update, context)
    accepted = {"back", "learning_sett", "learning_mix_mode", "learning_reset", "learning_start"}
    if action not in accepted:
        await _reply_text_with_retry(update.message, t(_user_interface_lang(uid), "learning_use_menu_buttons"),
            reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(uid), resize_keyboard=True))
        return LEARNING_MENU
    flow = uuid.uuid4().hex

    def control(conn):
        ids = [row[0] for row in conn.execute(
            "SELECT word_id FROM learning_items WHERE user_id=? AND status='active'", (uid,))]
        result = {"flow_id": flow,
                  "learning_lifecycles": learning_requests.capture_lifecycles(conn, uid, ids)}
        if action == "learning_reset":
            learning_store.reset(conn, uid)
        elif action == "learning_mix_mode":
            key = f"learning_mix_mode:{uid}"
            row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
            enabled = bool(row and str(row[0]).lower() in {"1", "true", "yes", "on"})
            conn.execute("""INSERT INTO app_settings(key,value) VALUES (?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, "0" if enabled else "1"))
            result["enabled"] = not enabled
        if action == "learning_start":
            learning_store.recover_ready_previews(conn, uid, datetime.now(timezone.utc))
            empty_history = type("LearningStartContext", (), {"user_data": {}})()
            result["next_view"] = _next_learning_view(conn, empty_history, uid)
        return result

    result = answers.record_control_transition(get_db_path(), uid, _transport_id(update), flow, action, control)
    if _learning_recovery(context, result):
        return await _send_learning_menu(update, uid)
    if action == "back":
        return await _leave_learning(update, context, result, TESTS_MENU)
    if action == "learning_sett":
        from .settings import _send_learning_settings_menu
        context.user_data["learning_sett_return"] = "learning_menu"
        return await _send_learning_settings_menu(update, uid)
    if action == "learning_mix_mode":
        await _reply_text_with_retry(update.message,
            f"{t(_user_interface_lang(uid), 'learning_mix_mode')} = {'ON' if result['enabled'] else 'OFF'}")
        return await _send_learning_menu(update, uid)
    if action == "learning_reset":
        invalidate_study_frames(context)
        await _reply_text_with_retry(update.message, t(_user_interface_lang(uid), "learning_session_reset"),
            reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(uid), resize_keyboard=True))
        return LEARNING_MENU
    context.user_data.pop("learning_recent_ids", None)
    context.user_data.pop("quiz_last_answered_entry", None)
    frame = _new_frame()
    frame["flow_id"] = flow
    context.user_data[FRAME] = frame
    if not result["next_view"]["entry"]:
        context.user_data.pop(FRAME, None)
        await _reply_text_with_retry(update.message, _learning_empty_text(uid),
            reply_markup=ReplyKeyboardMarkup(_learning_menu_keys(uid), resize_keyboard=True))
        return LEARNING_MENU
    _establish_learning_frame(context, uid, transport=_transport_id(update), plan=result["next_view"])
    return await _render_learning_frame(update, context)


async def learning_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with study_lock(update.effective_user.id):
        receipt = _delivery_receipt(update, context)
        if receipt is not None:
            return await _handle_replay(update, context, receipt)
        context.user_data.pop(_PENDING_NAVIGATION, None)
        try:
            drained, state = await _drain_pending_learning_view(update, context)
            if drained:
                return state
            return await _learning_quiz_locked(update, context)
        except (InvalidClassification, InvalidForms):
            frame = context.user_data.get(FRAME) or {}
            answers.reject_stale(get_db_path(), update.effective_user.id, _transport_id(update), frame.get('flow_id', 'learning'))
            invalidate_study_frames(context)
            await _reply_text_with_retry(update.message, t(_user_interface_lang(update.effective_user.id), 'quiz_invalid_construction'))
            return await _send_learning_menu(update, update.effective_user.id)
        except (StaleGeneration, answers.StaleStudyAction):
            frame = context.user_data.get(FRAME) or {}
            answers.reject_stale(get_db_path(), update.effective_user.id, _transport_id(update), frame.get('flow_id', 'learning'))
            invalidate_study_frames(context)
            return await _send_learning_menu(update, update.effective_user.id)


def _learning_recovery(context, receipt):
    if receipt.get("invalidate_frame") or receipt.get("state") == "recovered_undo":
        invalidate_study_frames(context)
        return True
    return False


async def _learning_quiz_locked(update, context):
    from .clarify_fields import _start_clarify_fields
    uid, transport = update.effective_user.id, _transport_id(update)
    text = update.message.text.strip()
    action = _action_key_from_text(text, uid)
    frame = _legacy_frame(context)
    if action in ("back", "clarify_fields"):
        result = answers.record_control_transition(get_db_path(), uid, transport, frame["flow_id"], action,
            lambda conn: {"flow_id": frame["flow_id"], "revision": frame["revision"]},
            expected_generations=frame_generations((frame.get("current") or {}).get("entry")),
            expected_lifecycles=frame_lifecycles((frame.get("current") or {}).get("entry")))
        if _learning_recovery(context, result):
            return await _send_learning_menu(update, uid)
        if action == "back":
            return await _leave_learning(update, context, result, LEARNING_MENU)
        return await _start_clarify_fields(update, context, LEARNING_QUIZ, prefer_current=True)
    slot = frame.get("current")
    if action == "input_err":
        last = frame.get("last_answer")
        if not last or last.get("ok") or frame["phase"] == "preview":
            result = answers.record_control_transition(get_db_path(), uid, transport, frame["flow_id"], "input_err_empty",
                lambda conn: {"flow_id": frame["flow_id"], "revision": frame["revision"]},
                expected_generations=frame_generations((frame.get("current") or {}).get("entry")),
                expected_lifecycles=frame_lifecycles((frame.get("current") or {}).get("entry")))
            if _learning_recovery(context, result):
                return await _send_learning_menu(update, uid)
            return await _render_learning_frame(update, context, t(_user_interface_lang(uid), "input_err_nothing"))
        def correct(conn, snapshot, kind, now):
            result = learning_store.apply_answer(conn, snapshot, kind, now)
            if not slot or slot["entry"]["id"] == last["word_id"]:
                result["next_view"] = _next_learning_view(conn, context, uid)
            return result

        receipt = answers.correct_last_answer(get_db_path(), uid, last["action_key"], last["correction_key"],
            transport, frame["revision"], flow_id=frame["flow_id"],
            live_revision=lambda: context.user_data[FRAME]["revision"], learning_transition=correct,
            expected_generations=frame_generations((frame.get("current") or {}).get("entry")),
            expected_lifecycles=frame_lifecycles((frame.get("current") or {}).get("entry")))
        if _learning_recovery(context, receipt):
            return await _send_learning_menu(update, uid)
        if receipt.get("replayed"):
            return await _handle_replay(update, context, receipt)
        frame["last_answer"]["ok"] = True
        context.user_data["last_learning_ok"] = True
        prefix = t(_user_interface_lang(uid), "input_err_fixed")
        if slot and slot["entry"]["id"] != last["word_id"]:
            frame.update(revision=frame["revision"] + 1, prefix_text=prefix,
                         accepted_delivery=transport, pending_render=True)
        else:
            _establish_learning_frame(context, uid, prefix, transport=transport, plan=receipt["after"]["next_view"])
        await _set_reaction(context, uid, context.user_data.get("last_learning_answer_message_id"), [])
        return await _render_learning_frame(update, context)
    if not slot:
        return await _render_learning_frame(update, context)
    entry = slot["entry"]
    if action == "learning_skip" or (frame["phase"] == "preview" and action == "learning_next"):
        kind = "skip" if action == "learning_skip" else "preview_next"
        def preview(conn, snapshot, now):
            result = learning_store.preview_transition(conn, uid, entry["id"], kind, now)
            result["next_view"] = _next_learning_view(conn, context, uid)
            return result

        receipt = answers.apply_preview_transition(get_db_path(), uid, entry["id"], kind, slot["action_key"],
            transport, frame["revision"], flow_id=frame["flow_id"],
            live_revision=lambda: context.user_data[FRAME]["revision"],
            affected_learning_ids=lambda conn: learning_store.preview_affected_ids(conn, uid, entry["id"]),
            transition=preview, expected_generations=frame_generations(entry),
            expected_lifecycles=frame_lifecycles(entry))
        if _learning_recovery(context, receipt):
            return await _send_learning_menu(update, uid)
        if receipt.get("replayed"):
            return await _handle_replay(update, context, receipt)
        if kind == "skip" and (frame.get("last_answer") or {}).get("word_id") == entry["id"]:
            frame["last_answer"] = None
        prefix = t(_user_interface_lang(uid), "learning_skipped" if kind == "skip" else "learning_saved_next")
        _establish_learning_frame(context, uid, prefix, transport=transport, plan=receipt["after"]["next_view"])
        return await _render_learning_frame(update, context)
    if frame["phase"] == "preview":
        lang = _user_interface_lang(uid)
        await _reply_text_with_retry(update.message, t(lang, "learning_use_next_skip_back"),
            reply_markup=ReplyKeyboardMarkup([[t(lang, "learning_next"), t(lang, "learning_skip")],
                                              [t(lang, "back")]], resize_keyboard=True))
        return LEARNING_QUIZ
    validate_quiz_entry(entry)
    ok = False if text == I_DONT_KNOW_TEXT else _check_ru_de_strict(entry, text)[0]
    kind = "idk" if text == I_DONT_KNOW_TEXT else "correct" if ok else "wrong"
    def grade(conn, snapshot, answer, now):
        result = learning_store.apply_answer(conn, snapshot, answer, now)
        result["next_view"] = _next_learning_view(conn, context, uid)
        return result

    receipt = answers.apply_original_answer(get_db_path(), uid, entry["id"], "learning", kind, slot["action_key"],
        transport, frame["revision"], flow_id=frame["flow_id"],
        live_revision=lambda: context.user_data[FRAME]["revision"], learning_transition=grade,
        expected_generation=entry.get("study_generation", 0), expected_generations=frame_generations(entry),
        expected_lifecycles=frame_lifecycles(entry))
    if _learning_recovery(context, receipt):
        return await _send_learning_menu(update, uid)
    if receipt.get("replayed"):
        return await _handle_replay(update, context, receipt)
    frame["last_answer"] = {"word_id": entry["id"], "action_key": slot["action_key"],
                             "correction_key": uuid.uuid4().hex, "ok": bool(ok)}
    frame["last_visible_id"] = entry["id"]
    context.user_data.update(last_learning_entry_id=entry["id"], last_learning_ok=bool(ok),
                             last_learning_prev_step=receipt["before"]["learning_item"]["step_index"],
                             quiz_last_answered_entry=entry)
    feedback = _format_feedback_ru_de(entry, ok, lang=_user_interface_lang(uid))
    if receipt["after"].get("completed"):
        feedback += "\n\n" + t(_user_interface_lang(uid), "learning_done_sent_fsrs")
    _establish_learning_frame(context, uid, feedback, transport=transport, plan=receipt["after"]["next_view"])
    await _apply_learning_reaction(update, context, ok)
    return await _render_learning_frame(update, context)


register_renderer("learning", _learning_existing, _learning_deleted)
