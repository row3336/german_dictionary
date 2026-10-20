"""Live written frame identity; cached views never choose another question."""

from uuid import uuid4

from ..common.quiz_resume import publish_view
from ..common.session import _ensure_session_mode

FRAME = 'quiz_written_frame'


def transport_id(update):
    value = getattr(update, 'update_id', None)
    if value is None:
        value = getattr(update, '_written_transport_id', None)
        if value is None:
            value = uuid4().int % (2**63-1)
            update._written_transport_id = value
    return int(value)


def ensure_frame(context, mode, return_state):
    _ensure_session_mode(context, mode)
    frame = context.user_data.get(FRAME)
    if not frame or frame['mode'] != mode:
        for key in ('quiz_published_view', 'quiz_return_point', 'quiz_last_answered_entry'):
            context.user_data.pop(key, None)
        frame = {'flow_id': uuid4().hex, 'mode': mode, 'revision': 0,
                 'return_state': return_state, 'phase': 'completed',
                 'question_id': None, 'action_key': None, 'last_receipt': None,
                 'pending_render': False, 'view': []}
        context.user_data[FRAME] = frame
    return frame


def establish_view(context, mode, return_state, entry):
    frame = ensure_frame(context, mode, return_state)
    frame['revision'] += 1
    frame['phase'] = 'question' if entry else 'completed'
    frame['question_id'] = entry.get('id') if entry else None
    frame['action_key'] = uuid4().hex if entry else None
    if entry:
        context.user_data['quiz_entry'] = entry
    else:
        context.user_data.pop('quiz_entry', None)
        context.user_data.pop('quiz_is_retry', None)
    publish(context)
    return frame


def publish(context):
    frame = context.user_data[FRAME]
    visible = context.user_data.get('quiz_last_answered_entry') or {}
    publish_view(context, caller_kind='written', mode=frame['mode'], phase=frame['phase'],
                 current_question_id=frame['question_id'], visible_answer_id=visible.get('id'),
                 flow_id=frame['flow_id'], view_revision=frame['revision'])
