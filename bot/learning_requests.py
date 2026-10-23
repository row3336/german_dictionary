"""Explicit Learning requests, candidates and cycle barriers on one connection.

The caller owns authorization, delivery deduplication and the transaction.
"""

from enum import Enum
from uuid import uuid4

from .learning_logic import stamp
from .study.generations import generation_for, StaleGeneration


class EnqueueResult(str, Enum):
    QUEUED = 'queued'
    ALREADY_PENDING = 'already_pending'
    ALREADY_ACTIVE = 'already_active'
    MISSING = 'missing'
    STALE = 'stale'

    def __str__(self):
        return self.value


def lifecycle_for(conn, user_id, word_id):
    row = conn.execute("SELECT learning_lifecycle FROM learning_requests WHERE user_id=? AND word_id=?",
                       (user_id, word_id)).fetchone()
    return row[0] if row else 0


def capture_lifecycles(conn, user_id, word_ids):
    return {wid: lifecycle_for(conn, user_id, wid) for wid in dict.fromkeys(word_ids) if wid}


def assert_lifecycles(conn, user_id, expected):
    for wid, value in expected.items():
        if type(value) is not int or value < 0 or lifecycle_for(conn, user_id, wid) != value:
            raise StaleGeneration('Learning lifecycle changed: ' + str(wid))


def entry_lifecycles(entry):
    if not entry or not entry.get('id'):
        return {}
    return {**entry.get('learning_lifecycles', {}), entry['id']: entry.get('learning_lifecycle', 0)}


def frame_lifecycles(*entries):
    result = {}
    for entry in entries:
        for wid, value in entry_lifecycles(entry).items():
            if wid in result and result[wid] != value:
                raise StaleGeneration('conflicting selected Learning lifecycles: ' + str(wid))
            result[wid] = value
    return result


def enqueue(conn, user_id, word_id, expected_word_generation, now=None):
    if not word_id or not conn.execute('SELECT 1 FROM words WHERE id=?', (word_id,)).fetchone():
        return EnqueueResult.MISSING
    generation = generation_for(conn, user_id, word_id)
    if type(expected_word_generation) is not int or generation != expected_word_generation:
        return EnqueueResult.STALE
    if conn.execute("SELECT 1 FROM learning_items WHERE user_id=? AND word_id=? AND status='active'",
                    (user_id, word_id)).fetchone():
        return EnqueueResult.ALREADY_ACTIVE
    row = conn.execute('SELECT status,word_generation FROM learning_requests WHERE user_id=? AND word_id=?',
                       (user_id, word_id)).fetchone()
    if row and row[0] == 'pending' and row[1] == generation:
        return EnqueueResult.ALREADY_PENDING
    conn.execute("""INSERT INTO learning_requests
        (user_id,word_id,status,request_id,queued_at,consumed_at,word_generation,learning_lifecycle)
        VALUES (?,?,'pending',?,?,NULL,?,0) ON CONFLICT(user_id,word_id) DO UPDATE SET
        status='pending',request_id=excluded.request_id,queued_at=excluded.queued_at,
        consumed_at=NULL,word_generation=excluded.word_generation""",
        (user_id, word_id, uuid4().hex, stamp(now), generation))
    return EnqueueResult.QUEUED


def eligible_pool(conn, user_id, topics=None, cefr_levels=None):
    """One deduplicated pool for list/count/pick, with explicit FIFO first."""
    from .storage import _topic_filter_sql, _cefr_filter_sql, _row_to_entry
    clauses, params = [], [user_id, user_id, user_id, user_id]
    for clause, values in (_topic_filter_sql(topics), _cefr_filter_sql(cefr_levels)):
        if clause:
            clauses.append(clause)
            params.extend(values)
    filters = ''.join(' AND ' + c for c in clauses)
    rows = conn.execute("""SELECT w.*,lr.request_id AS learning_request_id,
        CASE WHEN lr.word_id IS NOT NULL THEN 1 ELSE 0 END AS learning_explicit_pending
        FROM words w LEFT JOIN learning_requests lr ON lr.word_id=w.id AND lr.user_id=?
        AND lr.status='pending' AND lr.word_generation=COALESCE((SELECT generation
            FROM study_word_generations sg WHERE sg.user_id=lr.user_id AND sg.word_id=w.id),0)
        WHERE NOT EXISTS (SELECT 1 FROM learning_items li WHERE li.user_id=?
            AND li.word_id=w.id AND li.status='active')
        AND (lr.word_id IS NOT NULL OR (
            NOT EXISTS (SELECT 1 FROM quiz_events e WHERE e.user_id=? AND e.word_id=w.id
                AND e.study_generation=COALESCE((SELECT generation FROM study_word_generations sg
                    WHERE sg.user_id=e.user_id AND sg.word_id=w.id),0))
            AND NOT EXISTS (SELECT 1 FROM learning_items li WHERE li.user_id=? AND li.word_id=w.id)))
        """ + filters + """ ORDER BY CASE WHEN lr.word_id IS NOT NULL THEN 0 ELSE 1 END,
        lr.queued_at,CASE WHEN lr.word_id IS NOT NULL THEN w.id END,w.created_at,w.id""",
        params).fetchall()
    from .study.generations import selected_entry
    result = []
    for row in rows:
        entry = selected_entry(conn, user_id, _row_to_entry(row))
        entry['learning_explicit_pending'] = bool(row['learning_explicit_pending'])
        entry['learning_request_id'] = row['learning_request_id']
        result.append(entry)
    return result


def count_candidates(conn, user_id, topics=None, cefr_levels=None):
    return len(eligible_pool(conn, user_id, topics, cefr_levels))


def pick(conn, user_id, limit, topics=None, cefr_levels=None, mix=False):
    from .storage import _mix_by_topic_proportional
    pool = eligible_pool(conn, user_id, topics, cefr_levels)
    limit = max(0, int(limit))
    pending = [e for e in pool if e['learning_explicit_pending']][:limit]
    new = [e for e in pool if not e['learning_explicit_pending']]
    need = limit - len(pending)
    return pending + (_mix_by_topic_proportional(new, need) if mix and need else new[:need])


def add_candidates(conn, user_id, daily_qty, topics=None, cefr_levels=None, mix=False, now=None):
    """Reserve, insert/restart and consume only selected requests atomically."""
    from . import learning_store
    from .study.generations import assert_generations, frame_generations
    active = conn.execute("SELECT COUNT(*) FROM learning_items WHERE user_id=? AND status='active'", (user_id,)).fetchone()[0]
    need = max(0, int(daily_qty) - active)
    selected = pick(conn, user_id, need, topics, cefr_levels, mix) if need else []
    assert_generations(conn, user_id, frame_generations(*selected))
    assert_lifecycles(conn, user_id, frame_lifecycles(*selected))
    if selected:
        batch = learning_store.get_or_create_batch(conn, user_id, daily_qty, now)
        for entry in selected:
            wid = entry['id']
            # Pool guarantees that a conflicting row is inactive and explicit.
            if learning_store.add_items(conn, user_id, [wid], batch, now,
                                        restart=entry['learning_explicit_pending']) != 1:
                raise StaleGeneration('Learning candidate changed: ' + wid)
            conn.execute("""INSERT INTO learning_requests
                (user_id,word_id,status,request_id,queued_at,consumed_at,word_generation,learning_lifecycle)
                VALUES (?,?,'consumed',?,?,?,?,1) ON CONFLICT(user_id,word_id) DO UPDATE SET
                request_id=CASE WHEN learning_requests.status='pending'
                    AND learning_requests.word_generation=excluded.word_generation
                    THEN learning_requests.request_id ELSE excluded.request_id END,
                queued_at=CASE WHEN learning_requests.status='pending'
                    AND learning_requests.word_generation=excluded.word_generation
                    THEN learning_requests.queued_at ELSE excluded.queued_at END,
                status='consumed',consumed_at=excluded.consumed_at,
                word_generation=excluded.word_generation,learning_lifecycle=learning_requests.learning_lifecycle+1""",
                (user_id, wid, uuid4().hex, stamp(now), stamp(now), entry['study_generation']))
    return {'added': len(selected), 'active_count': active, 'need': need,
            'selected_ids': [e['id'] for e in selected],
            'study_generations': frame_generations(*selected),
            'learning_lifecycles': capture_lifecycles(conn, user_id, [e['id'] for e in selected]),
            'flow_id': 'learning-menu'}
