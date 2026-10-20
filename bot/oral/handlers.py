"""Oral entry, serialized controls, and render-only clarify/delete adapters."""

from copy import deepcopy
from functools import wraps

from ..app_config import ORAL_QUIZ, get_db_path
from ..common.lang import _mode_de_l1, _user_dict_lang, _user_interface_lang
from ..common.navigation import _back_to_main_menu, _back_to_tests, _reply_text_with_retry
from ..common.quiz_resume import publish_view, register_renderer
from ..common.settings import _session_retry_gap, _session_retry_iterations
from ..common.study_lock import invalidate_study_frames, study_lock
from ..common.word_filter import resolve_quiz_filter
from ..i18n import t
from ..study.answers import StaleStudyAction
from ..written.state import transport_id
from . import domain, view
from .service import OralService, accepted_frame

FRAME = 'oral_frame'


def _service():
    return OralService(get_db_path())


def _options(user_id):
    qf = resolve_quiz_filter(user_id)
    return {'gap': _session_retry_gap(get_db_path(), user_id),
            'limit': _session_retry_iterations(get_db_path(), user_id),
            'topics': qf.topics, 'cefr_levels': qf.cefr_levels,
            'dict_lang': _user_dict_lang(user_id)}


def _live_frame(context):
    frame = context.user_data.get(FRAME)
    published = context.user_data.get('quiz_published_view') or {}
    return frame if (frame and published.get('caller_kind') == 'oral'
                     and published.get('flow_id') == frame['flow_id']) else None


def _publish(context):
    frame = context.user_data[FRAME]
    context.user_data['quiz_mode'] = frame['mode']
    hidden = frame.get('hidden') or {}
    visible = frame.get('pending') or frame.get('last_visible') or {}
    publish_view(context, caller_kind='oral', mode=frame['mode'], phase=frame['phase'],
                 flow_id=frame['flow_id'], view_revision=frame['revision'],
                 current_question_id=hidden.get('word_id'),
                 visible_answer_id=visible.get('word_id'))
    if visible:
        context.user_data['quiz_last_answered_entry'] = visible['entry']
    else:
        context.user_data.pop('quiz_last_answered_entry', None)
    # The clarify target is always visible; the hidden B never becomes quiz_entry.
    context.user_data.pop('quiz_entry', None)


def _adopt(context, receipt, transport, *, entry=False):
    frame = deepcopy(accepted_frame(receipt))
    if entry:
        invalidate_study_frames(context)
    frame.update(pending_render=True, render_transport=transport,
                 render_revision=frame['revision'])
    context.user_data[FRAME] = frame
    _publish(context)
    return frame


async def _render(update, context, *, refresh=False, prefix=None):
    frame = context.user_data[FRAME]
    # Keep the requested presentation, not just the old cached text. A failed
    # clarify read/format must retry its refresh and prefix on any drain delivery.
    pending = frame.get('pending_render')
    if (not refresh and prefix is None and isinstance(pending, dict)
            and frame.get('render_revision') == frame['revision']):
        refresh, prefix = pending['refresh'], pending['prefix']
    frame.update(pending_render={'refresh': refresh, 'prefix': prefix},
                 render_transport=transport_id(update), render_revision=frame['revision'])
    if refresh or prefix or not frame.get('view'):
        shown = (_service().refresh(update.effective_user.id, frame,
                 domain.dictionary_language(frame['mode'])) if refresh else frame)
        frame['view'] = view.message(shown, _user_interface_lang(update.effective_user.id), prefix)
    text, kwargs = frame['view']
    await _reply_text_with_retry(update.message, text, **kwargs)
    frame['pending_render'] = False
    return ORAL_QUIZ


async def _preflight(update, context):
    receipt = _service().preflight(update.effective_user.id, transport_id(update))
    if receipt is None:
        return False, None
    if not receipt.get('replayed') and receipt.get('invalidate_frame'):
        invalidate_study_frames(context)
        return True, await _back_to_main_menu(update, context)
    frame = _live_frame(context)
    if (receipt.get('state') != 'undone' and frame and frame.get('pending_render')
            and frame.get('render_transport') == transport_id(update)
            and frame.get('render_revision') == frame['revision']):
        return True, await _render(update, context)
    # Late replay, an extinguished action or restart without a live frame is a
    # no-op, including its ConversationHandler state.
    return True, None


async def _drain(update, context):
    frame = _live_frame(context)
    if not frame or not frame.get('pending_render'):
        return False, None
    receipt = _service().bind_render(update.effective_user.id, transport_id(update), frame)
    if not receipt.get('replayed') and receipt.get('invalidate_frame'):
        invalidate_study_frames(context)
        return True, await _back_to_main_menu(update, context)
    if receipt.get('replayed'):
        return True, None
    return True, await _render(update, context)


def serialized_oral_entry(handler):
    """Menu entry adapter: delivery lookup before reset/mode mutations."""
    @wraps(handler)
    async def wrapped(update, context, *args, **kwargs):
        async with study_lock(update.effective_user.id):
            handled, result = await _preflight(update, context)
            if handled:
                return result
            handled, result = await _drain(update, context)
            if handled:
                return result
            return await handler(update, context, *args, **kwargs)
    return wrapped


async def start_oral_quiz(update, context, mode=None):
    """Explicit entry; callers holding the lock use start_oral_quiz_locked."""
    async with study_lock(update.effective_user.id):
        return await start_oral_quiz_locked(update, context, mode)


async def start_oral_quiz_locked(update, context, mode=None):
    """Internal entry for serialized menus; never reacquire study_lock."""
    handled, result = await _preflight(update, context)
    if handled:
        return result
    handled, result = await _drain(update, context)
    if handled:
        return result
    mode = mode or context.user_data.get('quiz_mode') or _mode_de_l1(update.effective_user.id)
    options = _options(update.effective_user.id)
    expected = domain.dictionary_language(mode)
    if mode not in domain.MODES or expected != options['dict_lang']:
        raise ValueError('oral mode must use the selected dictionary language')
    receipt = _service().start(update.effective_user.id, mode, transport_id(update), options)
    if receipt.get('replayed'):
        return None
    if receipt.get('invalidate_frame'):
        invalidate_study_frames(context)
        return await _back_to_main_menu(update, context)
    _adopt(context, receipt, transport_id(update), entry=True)
    return await _render(update, context)


async def oral_quiz(update, context):
    async with study_lock(update.effective_user.id):
        return await oral_quiz_locked(update, context)


async def oral_quiz_locked(update, context):
    """Directional legacy/mixed-menu adapters already hold study_lock."""
    handled, result = await _preflight(update, context)
    if handled:
        return result
    handled, result = await _drain(update, context)
    if handled:
        return result
    frame = _live_frame(context)
    if not frame:
        return None
    uid = update.effective_user.id
    text, lang = (update.message.text or '').strip(), _user_interface_lang(uid)
    if text == t(lang, 'clarify_fields'):
        from ..handlers.clarify_fields import _start_clarify_fields
        _service().bind_control(uid, transport_id(update), frame, 'clarify')
        _publish(context)
        return await _start_clarify_fields(update, context, ORAL_QUIZ)
    if text == t(lang, 'back') and frame['phase'] == 'completed':
        _service().bind_control(uid, transport_id(update), frame, 'back')
        invalidate_study_frames(context)
        return await _back_to_tests(update, context)
    action = {t(lang, 'oral_next'): 'next', t(lang, 'oral_correct'): 'correct',
              t(lang, 'oral_wrong'): 'wrong', t(lang, 'stop'): 'stop'}.get(text)
    if (not action or (action == 'next' and frame['phase'] != 'initial_hidden')
            or (action in ('correct', 'wrong') and frame['phase'] != 'grading')
            or frame['phase'] == 'completed'):
        _service().bind_control(uid, transport_id(update), frame, 'ignored')
        return ORAL_QUIZ
    try:
        receipt = _service().transition(uid, frame, action, transport_id(update), _options(uid))
    except StaleStudyAction:
        # Deletion is handled through the explicit owner-delete adapter;
        # a stale unsolicited answer never selects another target.
        return ORAL_QUIZ
    if receipt.get('replayed'):
        return None
    if receipt.get('invalidate_frame'):
        invalidate_study_frames(context)
        return await _back_to_main_menu(update, context)
    _adopt(context, receipt, transport_id(update))
    return await _render(update, context)


def _matching(context, point):
    frame = _live_frame(context)
    return frame if (frame and frame['flow_id'] == point.flow_id
                     and frame['revision'] == point.view_revision) else None


async def render_existing_oral(update, context, point, prefix_text=None):
    """The clarify/synonyms outer handler already owns study_lock."""
    if not _matching(context, point):
        return None
    return await _render(update, context, refresh=True, prefix=prefix_text)


async def delete_from_oral(update, context, point, word_id, prefix_text=None):
    """Only an explicit deletion may advance the remaining oral session."""
    frame = _matching(context, point)
    if not frame:
        return None
    receipt = _service().transition(update.effective_user.id, frame, 'delete',
        transport_id(update), _options(update.effective_user.id), deleted_word_id=word_id)
    if not receipt.get('replayed') and receipt.get('invalidate_frame'):
        invalidate_study_frames(context)
        return await _back_to_main_menu(update, context)
    if receipt.get('replayed'):
        return None
    _adopt(context, receipt, transport_id(update))
    return await _render(update, context, prefix=prefix_text)


register_renderer('oral', render_existing_oral, delete_transition=delete_from_oral)
