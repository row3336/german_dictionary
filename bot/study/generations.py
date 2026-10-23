"""Word-level study barriers. All operations use the caller's transaction."""


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
    result = dict(entry)
    expected = capture_generations(conn, user_id, [entry['id'], *participant_ids])
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
