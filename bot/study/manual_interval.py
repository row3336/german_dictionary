"""Generation-pinned manual interval bridge; no Learning enqueue or quiz grade."""

from . import answers, deliveries, repository
from .generations import assert_generation


def preflight(db_path, user_id, transport):
    """Resolve a delivery before the clarify UI can point at another card."""
    with repository.transaction(db_path) as conn:
        old = deliveries.lookup_delivery(conn, user_id, transport)
        if old is None:
            return None
        result = answers.lookup_action(conn, user_id, old['action_key']) if old.get('action_key') else None
        return {**(result or old['result']), 'replayed': True, 'delivery_kind': old['action_kind']}


def preview(db_path, user_id, word_id, mode, count, *, expected_generation=0, interval_divisor=1.0):
    from ..storage import simulate_fsrs_advance
    if type(count) is not int or not 1 <= count <= 15:
        raise ValueError('manual interval count must be 1..15')
    with repository.transaction(db_path) as conn:
        answers.authorize_user(conn, user_id)
        assert_generation(conn, user_id, word_id, expected_generation)
        if not conn.execute('SELECT 1 FROM words WHERE id=?', (word_id,)).fetchone():
            raise answers.StaleStudyAction('manual interval parent no longer exists')
        baseline = repository.get_progress(conn, user_id, word_id, mode)
        return simulate_fsrs_advance(baseline, count, interval_divisor=interval_divisor)


confirm = answers.apply_manual_progress
