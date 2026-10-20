"""Connection-scoped conjugation queries; transactions belong to the service."""

import json
import random
import uuid

from ..common.conjugation import PRONOUNS
from .domain import eligible_word, forms_for, StaleAction


def asdict(row):
    return dict(row) if row is not None else None


def block_key(block):
    return (block['user_id'], block['pair'], block['word_id'], block['tense'])


def sync_blocks(conn, user_id, pair):
    """Materialize only admitted, confirmed eligible tables; never reset old state."""
    entries = conn.execute(
        """SELECT w.* FROM words w JOIN conjugation_admissions a ON a.word_id=w.id
           WHERE a.user_id=? AND a.pair=? AND w.type='verb' AND w.is_separable=0""",
        (user_id, pair),
    ).fetchall()
    for row in entries:
        entry = dict(row)
        for tense in ('praesens', 'praeteritum'):
            if forms_for(entry, tense):
                conn.execute(
                    """INSERT INTO conjugation_blocks(user_id,pair,word_id,tense)
                       VALUES (?,?,?,?) ON CONFLICT DO NOTHING""",
                    (user_id, pair, entry['id'], tense),
                )


def word(conn, word_id):
    return asdict(conn.execute('SELECT * FROM words WHERE id=?', (word_id,)).fetchone())


def block(conn, user_id, pair, word_id, tense, *, valid=True):
    row = asdict(conn.execute(
        """SELECT * FROM conjugation_blocks
           WHERE user_id=? AND pair=? AND word_id=? AND tense=?""",
        (user_id, pair, word_id, tense),
    ).fetchone())
    entry = word(conn, word_id)
    if row and (not valid or (eligible_word(entry) and forms_for(entry, tense))):
        return row
    return None


def blocks(conn, user_id, pair):
    result = []
    for row in conn.execute(
        'SELECT * FROM conjugation_blocks WHERE user_id=? AND pair=? ORDER BY due,word_id,tense',
        (user_id, pair),
    ).fetchall():
        item = dict(row)
        entry = word(conn, item['word_id'])
        if eligible_word(entry) and forms_for(entry, item['tense']):
            item['entry'] = entry
            result.append(item)
    return result


def preparation(conn, user_id, pair):
    """Use the existing view weights, restricted to unprepared time blocks."""
    items = blocks(conn, user_id, pair)
    by_word = {}
    for item in items:
        if item['phase'] == 'preparation':
            by_word.setdefault(item['word_id'], []).append(item)
    if not by_word:
        return None
    rows = conn.execute(
        """SELECT w.id FROM words w LEFT JOIN conjugation_progress cp
             ON cp.word_id=w.id AND cp.user_id=?
           ORDER BY (cp.last_seen_at IS NULL) DESC,
             (julianday('now')-julianday(cp.last_seen_at))/(1+COALESCE(cp.seen_count,0)) DESC,
             COALESCE(cp.seen_count,0),w.id""", (user_id,),
    ).fetchall()
    for row in rows:
        if row['id'] in by_word:
            pending = by_word[row['id']]
            entry = dict(pending[0]['entry'])
            for tense in ('praesens', 'praeteritum'):
                if any(x['tense'] == tense and x['phase'] == 'test' for x in items if x['word_id'] == entry['id']):
                    entry['conjugation_' + tense] = ''
            return {'entry': entry, 'blocks': [x for x in items if x['word_id'] == entry['id']]}
    return None


def record_view(conn, user_id, word_id):
    conn.execute(
        """INSERT INTO conjugation_progress(user_id,word_id,seen_count,last_seen_at)
           VALUES (?,?,1,CURRENT_TIMESTAMP) ON CONFLICT(user_id,word_id)
           DO UPDATE SET seen_count=seen_count+1,last_seen_at=CURRENT_TIMESTAMP""",
        (user_id, word_id),
    )


def pass_row(conn, user_id, pass_id):
    return asdict(conn.execute(
        'SELECT * FROM conjugation_passes WHERE user_id=? AND id=?', (user_id, pass_id),
    ).fetchone())


def answers(conn, pass_id):
    return [dict(x) for x in conn.execute(
        'SELECT * FROM conjugation_answers WHERE pass_id=? ORDER BY ordinal', (pass_id,),
    ).fetchall()]


def srs(block):
    return {key: block[key] for key in ('interval', 'repetitions', 'ease', 'due')}


def select_pass(conn, user_id, pair, today, now_iso):
    """Resume first; future next-pass due does not block unfinished training."""
    for row in conn.execute(
        """SELECT * FROM conjugation_passes WHERE user_id=? AND pair=? AND status='active'
           ORDER BY started_at,id""", (user_id, pair),
    ).fetchall():
        active = dict(row)
        current = block(conn, user_id, pair, active['word_id'], active['tense'], valid=False)
        if current and eligible_word(word(conn, active['word_id'])) and current['phase'] == 'test' and current['cycle'] == active['cycle']:
            return active
    candidates = [b for b in blocks(conn, user_id, pair) if b['phase'] == 'test' and b['due'] <= today.isoformat()]
    if not candidates:
        return None
    current = candidates[0]
    order = list(PRONOUNS)
    random.shuffle(order)
    pid = uuid.uuid4().hex
    conn.execute(
        """INSERT INTO conjugation_passes(id,user_id,pair,word_id,tense,cycle,status,
           pronoun_order_json,expected_forms_json,baseline_srs_json,started_at)
           VALUES (?,?,?,?,?,?,'active',?,?,?,?)""",
        (pid, user_id, pair, current['word_id'], current['tense'], current['cycle'],
         json.dumps(order), json.dumps(forms_for(current['entry'], current['tense'])),
         json.dumps(srs(current)), now_iso),
    )
    return pass_row(conn, user_id, pid)


def question(conn, current):
    if not current:
        return None
    answered = answers(conn, current['id'])
    order = json.loads(current['pronoun_order_json'])
    remaining = [p for p in order if p not in {a['pronoun_group'] for a in answered}]
    if not remaining:
        return None
    entry = word(conn, current['word_id'])
    return {
        'pass_id': current['id'], 'word_id': current['word_id'], 'pair': current['pair'],
        'tense': current['tense'], 'cycle': current['cycle'], 'revision': current['revision'],
        'pronoun': remaining[0], 'ordinal': len(answered)+1,
        'word': (entry or {}).get('german') or (entry or {}).get('infinitive', ''),
        'has_error': any(not a['is_correct'] for a in answered),
    }


def assert_pass(conn, user_id, pair, pass_id, revision):
    current = pass_row(conn, user_id, pass_id)
    if not current or current['pair'] != pair or current['status'] != 'active' or current['revision'] != revision:
        raise StaleAction('stale pass')
    b = block(conn, user_id, pair, current['word_id'], current['tense'], valid=False)
    if not b or not eligible_word(word(conn, current['word_id'])) or b['phase'] != 'test' or b['cycle'] != current['cycle']:
        raise StaleAction('ineligible block')
    return current


def summary(conn, user_id, pass_ids):
    completed = incomplete = 0
    for pid in set(pass_ids):
        row = pass_row(conn, user_id, pid)
        if row and row['status'] != 'reset' and answers(conn, pid):
            if row['status'] == 'completed':
                completed += 1
            else:
                incomplete += 1
    return {'completed': completed, 'incomplete': incomplete}
