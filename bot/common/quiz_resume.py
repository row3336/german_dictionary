"""Render-only returns shared by clarify and its nested editors.

Flow owners publish their established view and register a renderer. Renderers
must not pick a word, mark it shown, record an answer, or change counters. Delete
adapters are separate transitions and may advance the remaining session. The
outer handler owns the study lock; these helpers never reacquire it.
"""

from dataclasses import dataclass, replace

RETURN_POINT_KEY = "quiz_return_point"
PUBLISHED_VIEW_KEY = "quiz_published_view"
_RENDERERS = {}
_DELETE_TRANSITIONS = {}


@dataclass(frozen=True)
class QuizReturnPoint:
    flow_id: str
    mode: str
    phase: str
    current_question_id: str | None
    visible_answer_id: str | None
    view_revision: int
    caller_kind: str
    return_state: int


def register_renderer(caller_kind, renderer, delete_transition=None):
    """Register async renderer(update, context, point, prefix_text).

    Optional delete adapter: async(update, context, point, word_id, prefix_text).
    Register once when the owning handler module is imported. The adapter must
    validate its live frame/revision before rendering; point stores no queue copy.
    """
    _RENDERERS[caller_kind] = renderer
    if delete_transition is not None:
        _DELETE_TRANSITIONS[caller_kind] = delete_transition


def publish_view(context, *, caller_kind, mode, phase, current_question_id=None,
                 visible_answer_id=None, flow_id=None, view_revision=0):
    """Publish metadata only after the flow has established its current frame."""
    context.user_data[PUBLISHED_VIEW_KEY] = {
        "flow_id": str(flow_id or f"{caller_kind}:{mode}"), "mode": mode,
        "phase": phase, "current_question_id": current_question_id,
        "visible_answer_id": visible_answer_id, "view_revision": view_revision,
        "caller_kind": caller_kind,
    }


def capture_return_point(context, return_state, caller_kind=None):
    """Capture once, preserving the original caller throughout nested menus."""
    existing = context.user_data.get(RETURN_POINT_KEY)
    if isinstance(existing, QuizReturnPoint):
        return existing
    from ..app_config import LEARNING_QUIZ

    data = context.user_data
    published = data.get(PUBLISHED_VIEW_KEY)
    if published:
        point = QuizReturnPoint(**published, return_state=return_state)
    else:
        current = data.get("quiz_entry") or {}
        last = data.get("quiz_last_answered_entry") or {}
        kind = caller_kind or ("learning" if return_state == LEARNING_QUIZ else "written")
        preview = kind == "learning" and data.get("learning_preview_mode")
        point = QuizReturnPoint(
            flow_id=f"{kind}:{data.get('quiz_mode') or return_state}",
            mode=data.get("quiz_mode") or data.get("session_mode") or "",
            phase="preview" if preview else ("question" if current else "completed"),
            current_question_id=current.get("id"),
            visible_answer_id=(current if preview else last).get("id"),
            view_revision=0, caller_kind=kind, return_state=return_state,
        )
    data[RETURN_POINT_KEY] = point
    return point


def close_return_point(context):
    context.user_data.pop(RETURN_POINT_KEY, None)


async def render_existing_frame(update, context, prefix_text=None):
    """Return to the existing view without invoking a normal prompt/picker."""
    point = context.user_data.get(RETURN_POINT_KEY)
    if not isinstance(point, QuizReturnPoint):
        point = capture_return_point(context, context.user_data.get("edit_return_state"))
    renderer = _RENDERERS.get(point.caller_kind, _render_legacy_frame)
    state = await renderer(update, context, point, prefix_text)
    close_return_point(context)
    return state


def _entry_for_id(word_id):
    from ..app_config import get_db_path
    from ..storage import get_word_by_id
    # A deleted word never comes back from a cached copy.
    return get_word_by_id(get_db_path(), word_id) if word_id else None


async def _render_legacy_frame(update, context, point, prefix_text):
    from telegram import ReplyKeyboardMarkup
    from ..app_config import LEARNING_QUIZ, QUIZ_ARTICLE, QUIZ_DE_RU, QUIZ_PL_SG, get_db_path
    from ..common.lang import _user_dict_lang, _user_interface_lang
    from ..common.navigation import _reply_text_with_retry
    from ..common.session import _session_correct_count, _session_count, _session_wrong_count
    from ..i18n import t
    from ..ui.formatting import (
        _format_article_question, _format_de_ru_question, _format_learning_preview_prompt,
        _format_pl_sg_question, _format_ru_question,
    )
    from ..ui.keyboards import _learning_question_markup, _learning_preview_markup, _quiz_idle_markup, _quiz_markup
    from .bot_media import resolve_bot_word_media

    uid = update.effective_user.id
    lang = _user_interface_lang(uid)
    entry = None if point.phase in ("completed", "waiting") else _entry_for_id(point.current_question_id)
    if entry:
        entry = resolve_bot_word_media(get_db_path(), uid, entry)
    if not entry:
        if point.return_state == LEARNING_QUIZ:
            from ..handlers.learning import _learning_empty_text
            text = _learning_empty_text(uid)
            markup = ReplyKeyboardMarkup([[t(lang, "back")]], resize_keyboard=True)
        else:
            text = t(lang, "session_words_count").format(
                count=_session_count(context, point.mode),
                correct=_session_correct_count(context, point.mode),
                wrong=_session_wrong_count(context, point.mode),
            )
            markup = _quiz_idle_markup(uid)
        if prefix_text:
            await _reply_text_with_retry(update.message, prefix_text, parse_mode="Markdown")
        await _reply_text_with_retry(update.message, text, reply_markup=markup)
        return point.return_state
    if point.return_state == LEARNING_QUIZ:
        if point.phase == "preview":
            text = _format_learning_preview_prompt(entry, _user_dict_lang(uid))
            markup = _learning_preview_markup(uid)
        else:
            text = _format_ru_question(entry, _user_dict_lang(uid))
            markup = _learning_question_markup(uid)
    else:
        formatter = {
            QUIZ_DE_RU: _format_de_ru_question, QUIZ_PL_SG: _format_pl_sg_question,
            QUIZ_ARTICLE: _format_article_question,
        }.get(point.return_state)
        text = formatter(entry) if formatter else _format_ru_question(entry, _user_dict_lang(uid))
        markup = _quiz_markup(uid, article=point.return_state == QUIZ_ARTICLE)
    if prefix_text:
        text = f"{prefix_text}\n\n{text}"
    await _reply_text_with_retry(update.message, text, reply_markup=markup, parse_mode="Markdown")
    return point.return_state


async def resume_after_delete(update, context, word_id, prefix_text=None):
    """Invalidate deleted identities, then perform the owner's explicit transition."""
    point = context.user_data.get(RETURN_POINT_KEY)
    if not isinstance(point, QuizReturnPoint):
        point = capture_return_point(context, context.user_data.get("edit_return_state"))
    current_deleted = point.current_question_id == word_id
    point = replace(point,
        current_question_id=None if current_deleted else point.current_question_id,
        visible_answer_id=None if point.visible_answer_id == word_id else point.visible_answer_id,
    )
    context.user_data[RETURN_POINT_KEY] = point
    published = context.user_data.get(PUBLISHED_VIEW_KEY)
    if published:
        for key in ("current_question_id", "visible_answer_id"):
            if published.get(key) == word_id:
                published[key] = None
    for key in ("quiz_entry", "quiz_last_answered_entry", "quiz_edit_entry", "last_answered_entry"):
        if (context.user_data.get(key) or {}).get("id") == word_id:
            context.user_data.pop(key, None)
    for key in ("retry_queue", "fsrs_cycle_queue"):
        for queue in (context.user_data.get(key) or {}).values():
            if isinstance(queue, list):
                queue[:] = [item for item in queue if (item.get("id") if isinstance(item, dict) else item) != word_id]
    for counts in (context.user_data.get("retry_counts") or {}).values():
        counts.pop(word_id, None)
    for mode, last_id in list((context.user_data.get("last_quiz_ids") or {}).items()):
        if last_id == word_id:
            context.user_data["last_quiz_ids"].pop(mode, None)
    transition = _DELETE_TRANSITIONS.get(point.caller_kind)
    if transition:
        state = await transition(update, context, point, word_id, prefix_text)
        close_return_point(context)
        return state
    if current_deleted and point.phase not in ("completed", "waiting"):
        from ..app_config import LEARNING_QUIZ, QUIZ_ARTICLE, QUIZ_DE_RU, QUIZ_PL_SG, QUIZ_RU_DE
        if point.return_state == LEARNING_QUIZ:
            from ..handlers.learning import _send_learning_prompt
            close_return_point(context)
            return await _send_learning_prompt(update, context, prefix_text)
        from ..handlers.quiz import _send_article_prompt, _send_de_ru_prompt, _send_pl_sg_prompt, _send_ru_de_prompt
        sender = {QUIZ_RU_DE: _send_ru_de_prompt, QUIZ_DE_RU: _send_de_ru_prompt,
                  QUIZ_PL_SG: _send_pl_sg_prompt, QUIZ_ARTICLE: _send_article_prompt}.get(point.return_state)
        if sender:
            close_return_point(context)
            return await sender(update, context, prefix_text)
    return await render_existing_frame(update, context, prefix_text)
