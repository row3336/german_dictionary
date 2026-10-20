"""Pure oral A/B transitions; all mutations target a detached proposed frame."""

from copy import deepcopy
from uuid import uuid4

from ..retry_logic import maybe_take_retry_entry, register_retry_result

MODES = frozenset(
    [f'de_{lang}' for lang in ('ru', 'uk', 'en')]
    + [f'phrase_{direction}' for lang in ('ru', 'uk', 'en')
       for direction in (f'de_{lang}', f'{lang}_de')])


def copy_frame(current):
    # Telegram markup and pending-send metadata stay outside durable plans.
    return deepcopy({key: value for key, value in current.items()
                     if key not in ('view', 'pending_render', 'render_transport', 'render_revision')})


def new_frame(mode):
    if mode not in MODES:
        raise ValueError('unsupported oral mode')
    return {'flow_id': uuid4().hex, 'mode': mode, 'revision': 0,
            'phase': 'initial_hidden', 'pending': None, 'hidden': None,
            'last_visible': None, 'seen': [], 'correct': 0, 'wrong': 0,
            'retry_queue': [], 'retry_counts': {}, 'since_retry': 0,
            'original_pool': []}


def dictionary_language(mode):
    base = mode.removeprefix('phrase_')
    return base[3:] if base.startswith('de_') else base[:-3]


def slot(entry, retry=False, presentation=0):
    return {'word_id': entry['id'], 'entry': entry, 'action_key': uuid4().hex,
            'retry': retry, 'presentation': presentation}


def choose(frame, pick_original, get_word, gap):
    """Reserve a question once; a hidden retry already spends its presentation."""
    excluded = {s['word_id'] for s in (frame['pending'], frame['hidden']) if s}
    excluded.update(frame['seen'])
    reserved = {s['word_id'] for s in (frame['pending'], frame['hidden']) if s}
    queue, counts = frame['retry_queue'], frame['retry_counts']

    def retry(force=False):
        # Never rotate an unconsumed reserved retry into another slot.
        available = [wid for wid in queue if wid not in reserved]
        while available:
            size = len(available)
            entry, since, took = maybe_take_retry_entry(
                available, counts, 0 if force else frame['since_retry'],
                0 if force else gap, get_word)
            if not entry and not took:
                # A gap miss preserves the queue; missing words are discarded.
                missing = [wid for wid in queue if wid not in reserved and wid not in available]
                for wid in missing:
                    queue.remove(wid)
                if len(available) == size:
                    return None
                continue
            wid = entry['id']
            queue.remove(wid)
            frame['since_retry'] = since
            return slot(entry, True, counts[wid])
        return None

    candidate = retry()
    if candidate:
        return candidate
    original = pick_original(excluded, frame)
    if original:
        return slot(original)
    # An ungraded original may still open/change a drill tail. Do not drain
    # prematurely merely because it is the final visible original.
    if any(s and not s['retry'] for s in (frame['pending'], frame['hidden'])):
        return None
    return retry(force=True)


def start(mode, pick_original, get_word, gap):
    frame = new_frame(mode)
    frame['hidden'] = choose(frame, pick_original, get_word, gap)
    if not frame['hidden']:
        frame['phase'] = 'completed'
    frame['revision'] = 1
    return frame


def reveal(current, pick_original, get_word, gap):
    frame = copy_frame(current)
    if frame['phase'] != 'initial_hidden' or not frame['hidden']:
        return frame
    frame['pending'], frame['hidden'] = frame['hidden'], None
    frame['last_visible'] = frame['pending']
    frame['phase'] = 'grading'
    frame['hidden'] = choose(frame, pick_original, get_word, gap)
    frame['revision'] += 1
    return frame


def grade(current, correct, pick_original, get_word, gap, limit):
    frame = copy_frame(current)
    pending = frame['pending']
    if frame['phase'] != 'grading' or pending is None:
        raise ValueError('oral answer is not revealed')
    wid, retry = pending['word_id'], pending['retry']
    register_retry_result(frame['retry_queue'], frame['retry_counts'], wid,
                          correct, retry, limit)
    if not retry:
        frame['seen'].append(wid)
        frame['correct' if correct else 'wrong'] += 1
        frame['since_retry'] = frame['since_retry'] + 1 if correct else 0
    # B has already been presented. Eligibility created by A applies only to
    # the next unreserved slot, never to replacing B.
    frame['pending'], frame['hidden'] = frame['hidden'], None
    if frame['pending'] is None:
        frame['pending'] = choose(frame, pick_original, get_word, gap)
    if frame['pending']:
        frame['last_visible'] = frame['pending']
        frame['hidden'] = choose(frame, pick_original, get_word, gap)
    else:
        frame['phase'] = 'completed'
    frame['revision'] += 1
    return frame


def stop(current):
    frame = copy_frame(current)
    frame.update(phase='completed', pending=None, hidden=None)
    frame['revision'] += 1
    return frame


def delete(current, word_id, pick_original, get_word, gap):
    frame = copy_frame(current)
    frame['retry_queue'] = [wid for wid in frame['retry_queue'] if wid != word_id]
    frame['retry_counts'].pop(word_id, None)
    if (frame['last_visible'] or {}).get('word_id') == word_id:
        frame['last_visible'] = None
    hidden_deleted = (frame['hidden'] or {}).get('word_id') == word_id
    pending_deleted = (frame['pending'] or {}).get('word_id') == word_id
    if hidden_deleted:
        frame['hidden'] = None
    if pending_deleted:
        frame['pending'], frame['hidden'] = frame['hidden'], None
        if not frame['pending']:
            frame['pending'] = choose(frame, pick_original, get_word, gap)
        if frame['pending']:
            frame['last_visible'] = frame['pending']
        else:
            frame['phase'] = 'completed'
    if frame['phase'] != 'completed' and (hidden_deleted or pending_deleted):
        frame['hidden'] = choose(frame, pick_original, get_word, gap)
        if frame['phase'] == 'initial_hidden' and not frame['hidden']:
            frame['phase'] = 'completed'
    frame['revision'] += 1
    return frame


def summary(frame):
    count = frame['correct'] + frame['wrong']
    return {'count': count, 'correct': frame['correct'], 'wrong': frame['wrong'],
            'accuracy': f"{round(100 * frame['correct'] / count)}%" if count else '—'}
