"""Connection-only oral reads: preserve due/filter and Telegram media contracts."""

import random

from ..app_config import is_owner
from ..storage import _cefr_filter_sql, _row_to_entry


def word(conn, user_id, word_id, dict_lang):
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
    return entry


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
                 AND e.word_id=w.id AND e.mode!='learning')
                 OR (?=1 AND p.word_id IS NOT NULL))""",
             """NOT EXISTS (SELECT 1 FROM quiz_events e WHERE e.user_id=?
                 AND e.word_id=w.id AND e.mode=? AND e.is_correct=1
                 AND date(e.created_at)=date(?))"""]
    params = [today.isoformat(), today.isoformat(), user_id, user_id, user_id,
              int(phrase), user_id, mode, today.isoformat()]
    topics = [str(value).strip() for value in options.get('topics') or [] if str(value).strip()]
    if topics:
        where.append('w.topic IN ({})'.format(','.join('?' for _ in topics)))
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
               AND e.mode!='learning') AS history
        FROM words w LEFT JOIN progress p ON p.word_id=w.id AND p.user_id=? AND p.mode=?
        WHERE """ + ' AND '.join(where) + ' ORDER BY p.due,w.created_at',
        [user_id, user_id, user_id, mode, *params]).fetchall()
    return [dict(row) for row in rows]


def pick_original(conn, user_id, mode, options, today, excluded, frame):
    pool = frame['original_pool']
    if not pool:
        candidates = due(conn, user_id, mode, options, today, sorted(excluded))
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
    while pool:
        wid = pool.pop(0)
        if wid not in excluded:
            entry = word(conn, user_id, wid, options['dict_lang'])
            if entry:
                return entry
    return None
