"""Word-level study barriers. All operations use the caller's transaction."""


from copy import deepcopy


class StaleGeneration(ValueError):
    pass


def generation_for(conn, user_id, word_id):
    row = conn.execute("SELECT generation FROM study_word_generations WHERE user_id=? AND word_id=?",
                       (user_id, word_id)).fetchone()
    return row[0] if row else 0


def capture_generations(conn, user_id, word_ids):
    return {wid: generation_for(conn, user_id, wid) for wid in dict.fromkeys(word_ids) if wid}


def assert_generation(conn, user_id, word_id, expected=0):
    if type(expected) is not int or expected < 0 or generation_for(conn, user_id, word_id) != expected:
        raise StaleGeneration("study generation changed: " + str(word_id))


def assert_generations(conn, user_id, expected):
    for wid, generation in expected.items():
        assert_generation(conn, user_id, wid, generation)


def advance_generation(conn, user_id, word_id, expected):
    assert_generation(conn, user_id, word_id, expected)
    conn.execute("""INSERT INTO study_word_generations(user_id,word_id,generation) VALUES (?,?,?)
        ON CONFLICT(user_id,word_id) DO UPDATE SET generation=excluded.generation""",
                 (user_id, word_id, expected + 1))
    return expected + 1


def selected_entry(conn, user_id, entry, participant_ids=()):
    """Capture only at reservation; callers must not re-capture an existing frame."""
    if entry is None:
        return None
    from ..storage import _row_to_entry
    from ..common.adjective_forms import attach_degree_pairs
    from ..common.verb_construction import synonym_compatible
    result = deepcopy(entry)
    attach_degree_pairs(conn, result)
    result['answer_aliases'] = []
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='word_synonyms'").fetchone():
        rows = conn.execute("""SELECT w.* FROM words w JOIN word_synonyms s ON s.word_id=w.id
            WHERE s.group_id=(SELECT group_id FROM word_synonyms WHERE word_id=?)
            AND w.id!=? ORDER BY w.id""", (entry['id'], entry['id'])).fetchall()
        for row in rows:
            candidate = _row_to_entry(row)
            if synonym_compatible(entry, candidate):
                attach_degree_pairs(conn, candidate)
                result['answer_aliases'].append(candidate)
    expected = capture_generations(conn, user_id,
        [entry['id'], *participant_ids, *(alias['id'] for alias in result['answer_aliases'])])
    result['study_generation'] = expected[entry['id']]
    result['study_generations'] = expected
    from ..learning_requests import capture_lifecycles
    lifecycles = capture_lifecycles(conn, user_id, expected)
    result['learning_lifecycle'] = lifecycles[entry['id']]
    result['learning_lifecycles'] = lifecycles
    return result


def entry_generations(entry):
    """Missing metadata belongs exclusively to generation-zero legacy frames."""
    if not entry or not entry.get('id'):
        return {}
    return {**entry.get('study_generations', {}), entry['id']: entry.get('study_generation', 0)}


def frame_generations(*entries):
    result = {}
    for entry in entries:
        for wid, expected in entry_generations(entry).items():
            if wid in result and result[wid] != expected:
                raise StaleGeneration('conflicting selected generations: ' + str(wid))
            result[wid] = expected
    return result


FROZEN_ANSWER_FIELDS = (
    'type', 'german', 'article', 'plural', 'infinitive', 'reflexive_case',
    'prateritum', 'partizip2', 'komparativ', 'superlativ', 'perfect_aux', 'hilfsverb', 'aux',
    'conjugation_praesens', 'conjugation_praeteritum', 'perfekt_variants_json',
)
FROZEN_ADJECTIVE_FIELDS = (
    'german', 'komparativ', 'superlativ', 'adjective_degree_pairs',
)


def preserve_selected_entry(fresh, selected):
    """Refresh display data while retaining the exact selected answer contract."""
    if fresh is None:
        return None
    if not selected:
        return deepcopy(fresh)
    # The words DTO refreshes dictionary fields, not the selected study state.
    result = deepcopy({**selected, **fresh})
    frozen_fields = (FROZEN_ANSWER_FIELDS if selected.get('type') == 'verb' else
                     FROZEN_ADJECTIVE_FIELDS if selected.get('type') == 'adjektiv' else ())
    for key in frozen_fields:
        # An old snapshot missing annotation is legacy, even after backfill.
        result[key] = deepcopy(selected.get(key))
    result['answer_aliases'] = deepcopy(selected.get('answer_aliases', []))
    for key in ('study_generation', 'study_generations', 'learning_lifecycle', 'learning_lifecycles'):
        if key in selected:
            result[key] = deepcopy(selected[key])
    return result
