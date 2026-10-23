"""Delivery preflight and trusted atomic answers for written handlers."""

from ..study import answers, deliveries, repository
from .state import FRAME, ensure_frame, transport_id, selected_generations


def preflight(db_path, user_id, transport):
    """Resolve delivery/recovery before inspecting current question or receipt."""
    with repository.transaction(db_path) as conn:
        answers.authorize_user(conn, user_id)
        old = deliveries.lookup_delivery(conn, user_id, transport)
        if old:
            result = old['result']
            if old.get('action_key'):
                result = answers.lookup_action(conn, user_id, old['action_key']) or result
            return {**result, 'replayed': True}
        recovered = answers.recover_pending_undo(conn, user_id)
        if recovered:
            result = {'state': 'recovered_undo', 'invalidate_frame': True}
            deliveries.begin_delivery(conn, user_id, transport, 'recovery', 'written_recovery')
            deliveries.commit_delivery(conn, user_id, transport, result)
            return result
    return None


def original(db_path, update, context, mode, state, entry, answer_kind, divisor):
    frame = ensure_frame(context, mode, state)
    if frame['question_id'] != entry['id'] or not frame['action_key']:
        # Compatibility for server-side callers establishing a question directly.
        # Production prompt senders always establish this identity before send.
        from .state import establish_view
        frame = establish_view(context, mode, state, entry)
    revision = frame['revision']
    return answers.apply_original_answer(
        db_path, update.effective_user.id, entry['id'], mode, answer_kind,
        frame['action_key'], transport_id(update), revision,
        flow_id=frame['flow_id'], interval_divisor=divisor,
        expected_generation=frame.get('study_generation', 0),
        live_revision=lambda: (context.user_data.get(FRAME) or {}).get('revision'))


def correction(db_path, update, context, mode, divisor):
    frame = context.user_data.get(FRAME)
    parent = frame and frame.get('last_receipt')
    if not frame or frame['mode'] != mode or not parent or parent.get('answer_kind') == 'correct':
        return None
    return answers.correct_last_answer(
        db_path, update.effective_user.id, parent['action_key'], 'written-fix:'+parent['action_key'],
        transport_id(update), frame['revision'], flow_id=frame['flow_id'],
        interval_divisor=divisor, expected_generations=selected_generations(frame),
        live_revision=lambda: (context.user_data.get(FRAME) or {}).get('revision'))


def control(db_path, update, context, mode, state, kind, destination=None):
    frame = ensure_frame(context, mode, state)
    return answers.record_control_transition(
        db_path, update.effective_user.id, transport_id(update), frame['flow_id'], kind,
        lambda conn: {'flow_id': frame['flow_id'], 'revision': frame['revision'],
                      'return_state': state if destination is None else destination, 'mode': mode},
        expected_generations=selected_generations(frame))
