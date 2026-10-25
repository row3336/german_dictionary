"""Connection-only oral reads: preserve due/filter and Telegram media contracts."""

import random

from ..app_config import is_owner
from ..study.generations import selected_entry, preserve_selected_entry
from ..storage import _cefr_filter_sql, _topic_filter_sql, _row_to_entry
from ..common.word_filter import resolve_quiz_filter, effective_filter_fingerprint


def word(conn, user_id, word_id, dict_lang, *, selected=None):
    row = conn.execute('SELECT * FROM words WHERE id=?', (word_id,)).fetchone()
    if row is None:
        return None
    entry = _row_to_entry(row)
    media = conn.execute('SELECT * FROM user_word_media WHERE user_id=? AND word_id=?',
                         (user_id, word_id)).fetchone()
    if media:
        for field in ('image', 'example'):
            if str(media[field] or '').strip():
                entry[field] = media[field]
        if not is_owner(user_id):
            column = {'ru': 'russian', 'uk': 'ukrainian', 'en': 'english'}[dict_lang]
            entry['my_translation'] = media[column] or ''
    return (preserve_selected_entry(entry, selected) if selected is not None
            else selected_entry(conn, user_id, entry))


def due(conn, user_id, mode, options, today, excluded=()):
    """Mirror select_due's eligibility, without opening a nested connection."""
    phrase = mode.startswith('phrase_')
    where = ["LOWER(TRIM(w.type)) {} 'phrase'".format('=' if phrase else '!='),
             'COALESCE(p.due, ?) <= ?',
             """NOT EXISTS (SELECT 1 FROM learning_items li WHERE li.user_id=?
                 AND li.word_id=w.id AND li.status='active')""",
             """(EXISTS (SELECT 1 FROM learning_items l WHERE l.user_id=?
                 AND l.word_id=w.id AND l.status IN ('completed','skipped'))
                 OR EXISTS (SELECT 1 FROM quiz_events e WHERE e.user_id=?
                 AND e.word_id=w.id AND e.study_generation=COALESCE((SELECT generation FROM study_word_generations sg WHERE sg.user_id=e.user_id AND sg.word_id=e.word_id),0) AND e.mode!='learning')
                 OR (?=1 AND p.word_id IS NOT NULL))""",
             """NOT EXISTS (SELECT 1 FROM quiz_events e WHERE e.user_id=?
                 AND e.word_id=w.id AND e.study_generation=COALESCE((SELECT generation FROM study_word_generations sg WHERE sg.user_id=e.user_id AND sg.word_id=e.word_id),0) AND e.mode=? AND e.is_correct=1
                 AND date(e.created_at)=date(?))"""]
    params = [today.isoformat(), today.isoformat(), user_id, user_id, user_id,
              int(phrase), user_id, mode, today.isoformat()]
    topic_clause, topics = _topic_filter_sql(options.get('topics'))
    if topic_clause:
        where.append(topic_clause)
        params.extend(topics)
    clause, levels = _cefr_filter_sql(options.get('cefr_levels'))
    if clause:
        where.append(clause)
        params.extend(levels)
    if excluded:
        where.append('w.id NOT IN ({})'.format(','.join('?' for _ in excluded)))
        params.extend(excluded)
    rows = conn.execute("""SELECT w.id,p.repetitions,
        (SELECT MAX(l.updated_at) FROM learning_items l WHERE l.user_id=?
         AND l.word_id=w.id AND l.status IN ('completed','skipped')) AS done_at,
        EXISTS(SELECT 1 FROM quiz_events e WHERE e.user_id=? AND e.word_id=w.id
               AND e.study_generation=COALESCE((SELECT generation FROM study_word_generations sg WHERE sg.user_id=e.user_id AND sg.word_id=e.word_id),0) AND e.mode!='learning') AS history
        FROM words w LEFT JOIN progress p ON p.word_id=w.id AND p.user_id=? AND p.mode=?
        WHERE """ + ' AND '.join(where) + ' ORDER BY p.due,w.created_at',
        [user_id, user_id, user_id, mode, *params]).fetchall()
    return [dict(row) for row in rows]


def pick_original(conn, user_id, mode, options, today, excluded, frame):
    # Options belong to the original frame. Refresh the effective filter on this
    # transaction, without touching any published/reserved slot or retry budget.
    qf = resolve_quiz_filter(user_id, conn=conn)
    fingerprint = effective_filter_fingerprint(qf)
    options = {**options, 'topics': qf.topics, 'cefr_levels': qf.cefr_levels}
    pool = frame['original_pool']
    if frame.get('original_filter_fingerprint') != fingerprint:
        pool.clear()
        frame['original_filter_fingerprint'] = fingerprint
    reserved = set(frame.get('original_reserved_ids', []))
    excluded = set(excluded) | reserved
    if qf.topics == [] or qf.cefr_levels == []:
        pool.clear()
        return None
    eligible_ids = None
    if not pool:
        candidates = due(conn, user_id, mode, options, today, sorted(excluded))
        eligible_ids = {row['id'] for row in candidates}
        fresh, scheduled = [], []
        for row in candidates:
            target = fresh if (row['done_at'] and not row['history']
                               and int(row['repetitions'] or 0) <= 1) else scheduled
            target.append(row['id'])
        random.shuffle(fresh)
        random.shuffle(scheduled)
        # The ordinary FSRS queue alternates one Learning graduate and 2..3
        # scheduled cards. Keep its queue in the proposed atomic frame.
        while fresh or scheduled:
            if fresh:
                pool.append(fresh.pop(0))
            if scheduled:
                for _ in range(min(len(scheduled), random.randint(2, 3))):
                    pool.append(scheduled.pop(0))
    if eligible_ids is None:
        eligible_ids = {row['id'] for row in due(conn, user_id, mode, options, today, sorted(excluded))}
    while pool:
        wid = pool.pop(0)
        if wid not in excluded and wid in eligible_ids:
            entry = word(conn, user_id, wid, options['dict_lang'])
            if entry:
                reserved.add(wid)
                frame['original_reserved_ids'] = sorted(reserved)
                return entry
    return None
