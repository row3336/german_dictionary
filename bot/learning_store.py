"""Connection-scoped Learning storage. No connection or commit is opened here."""

import uuid
from datetime import timedelta

from .app_config import LEARNING_INTERVALS_DEFAULT, LEARNING_INTERVALS_KEY, LEARNING_WARMUP_INTERVALS
from .learning_logic import clock, reduce_answer, stamp


def schedule(conn, user_id):
    row = conn.execute("SELECT value FROM user_settings WHERE user_id=? AND key=?",
                       (user_id, LEARNING_INTERVALS_KEY)).fetchone()
    if row is None:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (LEARNING_INTERVALS_KEY,)).fetchone()
    raw = row[0] if row else LEARNING_INTERVALS_DEFAULT
    values = []
    for token in str(raw or "").replace(";", ",").split(","):
        try:
            value = int(token)
        except ValueError:
            continue
        if value > 0:
            values.append(value)
    if not values:
        values = [int(value) for value in LEARNING_INTERVALS_DEFAULT.split(",")]
    return [*LEARNING_WARMUP_INTERVALS, *values]


def item(conn, user_id, word_id):
    row = conn.execute("SELECT * FROM learning_items WHERE user_id=? AND word_id=?",
                       (user_id, word_id)).fetchone()
    return dict(row) if row else None


def touch_batch(conn, user_id, batch_id, now=None):
    """Reopen an existing batch if undo restored active items; never create it."""
    conn.execute("""UPDATE learning_batches SET updated_at=?,ready_since=NULL,
        status=CASE WHEN EXISTS (SELECT 1 FROM learning_items
        WHERE user_id=? AND batch_id=? AND status='active') THEN 'active' ELSE status END
        WHERE id=? AND user_id=?""",
        (stamp(now), user_id, batch_id, batch_id, user_id))


def get_or_create_batch(conn, user_id, daily_qty, now=None):
    row = conn.execute("""SELECT id FROM learning_batches WHERE user_id=? AND status='active'
                          ORDER BY created_at DESC LIMIT 1""", (user_id,)).fetchone()
    if row:
        conn.execute("UPDATE learning_batches SET target_qty=? WHERE id=? AND user_id=?",
                     (int(daily_qty), row[0], user_id))
        return row[0]
    batch_id = str(uuid.uuid4())
    conn.execute("INSERT INTO learning_batches(id,user_id,target_qty,status) VALUES (?,?,?,'active')",
                 (batch_id, user_id, int(daily_qty)))
    return batch_id


def add_items(conn, user_id, word_ids, batch_id, now=None, restart=False):
    if not conn.execute("SELECT 1 FROM learning_batches WHERE id=? AND user_id=?",
                        (batch_id, user_id)).fetchone():
        raise ValueError("Learning batch does not belong to user")
    added = 0
    conflict = """DO UPDATE SET batch_id=excluded.batch_id,step_index=0,preview_done=0,
        recovery_remaining=0,first_due_pending=1,next_due=excluded.next_due,status='active',
        updated_at=excluded.updated_at""" if restart else "DO NOTHING"
    for word_id in word_ids:
        cursor = conn.execute(f"""INSERT INTO learning_items
            (user_id,word_id,batch_id,step_index,preview_done,next_due,status,
             recovery_remaining,first_due_pending,updated_at)
            VALUES (?,?,?,0,0,?,'active',0,1,?)
            ON CONFLICT(user_id,word_id) {conflict}""",
            (user_id, word_id, batch_id, stamp(now), stamp(now)))
        added += cursor.rowcount > 0
    touch_batch(conn, user_id, batch_id, now)
    return int(added)


def finalize_preview_if_ready(conn, user_id, batch_id, now, first_interval_seconds=None):
    if conn.execute("""SELECT 1 FROM learning_items WHERE user_id=? AND batch_id=?
        AND status='active' AND preview_done=0 LIMIT 1""", (user_id, batch_id)).fetchone():
        return []
    ids = [row[0] for row in conn.execute("""SELECT word_id FROM learning_items
        WHERE user_id=? AND batch_id=? AND status='active' AND preview_done=1
        AND first_due_pending=1 ORDER BY created_at,word_id""", (user_id, batch_id))]
    if not ids:
        return []
    delay = schedule(conn, user_id)[0] if first_interval_seconds is None else max(0, int(first_interval_seconds))
    due = stamp(clock(now) + timedelta(seconds=delay))
    conn.execute("""UPDATE learning_items SET next_due=?,first_due_pending=0,updated_at=?
        WHERE user_id=? AND batch_id=? AND status='active' AND preview_done=1 AND first_due_pending=1""",
        (due, stamp(now), user_id, batch_id))
    from .notifications.settings import record_learning_new_start
    record_learning_new_start(conn, user_id, clock(now))
    touch_batch(conn, user_id, batch_id, now)
    return ids


def preview_affected_ids(conn, user_id, word_id):
    row = item(conn, user_id, word_id)
    if not row:
        return []
    return [record[0] for record in conn.execute("""SELECT word_id FROM learning_items
        WHERE user_id=? AND batch_id=? AND status='active' AND first_due_pending=1""",
        (user_id, row["batch_id"]))]


def preview_transition(conn, user_id, word_id, action_kind, now, first_interval_seconds=None):
    from .study.answers import StaleStudyAction
    row = item(conn, user_id, word_id)
    if not row or row["status"] != "active":
        raise StaleStudyAction("Learning item is unavailable")
    if action_kind == "preview_next":
        if row["preview_done"]:
            raise StaleStudyAction("Learning preview was already completed")
        conn.execute("UPDATE learning_items SET preview_done=1,updated_at=? WHERE user_id=? AND word_id=?",
                     (stamp(now), user_id, word_id))
    elif action_kind == "skip":
        conn.execute("UPDATE learning_items SET status='skipped',updated_at=? WHERE user_id=? AND word_id=?",
                     (stamp(now), user_id, word_id))
    else:
        raise ValueError("unsupported Learning preview transition")
    ids = finalize_preview_if_ready(conn, user_id, row["batch_id"], now, first_interval_seconds)
    touch_batch(conn, user_id, row["batch_id"], now)
    return {"newly_started_ids": ids, "learning_item": item(conn, user_id, word_id)}


def apply_answer(conn, snapshot, answer_kind, now, intervals=None):
    from .study.answers import StaleStudyAction
    # Compatibility callers can pass a historical before directly. Preserve both
    # barriers here too; live answers additionally validate before capture.
    from .study.repository import assert_snapshot_generations
    from .learning_requests import assert_lifecycles
    assert_snapshot_generations(conn, snapshot)
    assert_lifecycles(conn, snapshot['user_id'], {snapshot['word_id']:
        (snapshot.get('learning_lifecycles') or {}).get(snapshot['word_id'], 0)})
    before = snapshot.get("learning_item")
    user_id, word_id = snapshot["user_id"], snapshot["word_id"]
    current = item(conn, user_id, word_id)
    if (not before or not current or before["status"] != "active" or not before["preview_done"]
            or before.get("first_due_pending", 0)):
        raise StaleStudyAction("Learning answer requires an interval item")
    after = reduce_answer(before, answer_kind, schedule(conn, user_id) if intervals is None else intervals, now)
    conn.execute("""UPDATE learning_items SET step_index=?,recovery_remaining=?,next_due=?,status=?,updated_at=?
        WHERE user_id=? AND word_id=?""", (after["step_index"], after["recovery_remaining"], after["next_due"],
        after["status"], after["updated_at"], user_id, word_id))
    touch_batch(conn, user_id, before["batch_id"], now)
    return {"completed": after["status"] == "completed", "step_index": after["step_index"],
            "next_due": None if after["status"] == "completed" else after["next_due"],
            "recovery_remaining": after["recovery_remaining"], "learning_item": after}


def reset(conn, user_id):
    conn.execute("""DELETE FROM quiz_events WHERE user_id=? AND mode='learning'
        AND word_id IN (SELECT word_id FROM learning_items WHERE user_id=? AND status='active')""",
        (user_id, user_id))
    conn.execute("DELETE FROM learning_items WHERE user_id=? AND status='active'", (user_id,))
    conn.execute("""DELETE FROM learning_batches WHERE user_id=? AND NOT EXISTS
        (SELECT 1 FROM learning_items WHERE batch_id=learning_batches.id AND user_id=?)""",
        (user_id, user_id))
    return True


def preview_entry(conn, user_id):
    from .storage import _row_to_entry
    row = conn.execute("""SELECT w.*,li.step_index AS learning_step,li.next_due AS learning_due,
        li.batch_id AS learning_batch_id,li.recovery_remaining AS learning_recovery_remaining,
        li.first_due_pending AS learning_first_due_pending
        FROM learning_items li JOIN words w ON w.id=li.word_id
        WHERE li.user_id=? AND li.status='active' AND li.preview_done=0
        ORDER BY li.created_at ASC LIMIT 1""", (user_id,)).fetchone()
    from .study.generations import selected_entry
    return selected_entry(conn, user_id, _row_to_entry(row), preview_affected_ids(conn, user_id, row['id'])) if row else None


def due_entries(conn, user_id, now=None):
    from .storage import _row_to_entry
    rows = conn.execute("""SELECT w.*,li.step_index AS learning_step,li.next_due AS learning_due,
        li.batch_id AS learning_batch_id,li.recovery_remaining AS learning_recovery_remaining,
        li.first_due_pending AS learning_first_due_pending
        FROM learning_items li JOIN words w ON w.id=li.word_id
        WHERE li.user_id=? AND li.status='active' AND li.preview_done=1
        AND li.first_due_pending=0 AND li.next_due<=?
        ORDER BY CASE WHEN li.step_index=0 THEN 0 ELSE 1 END,
        CASE WHEN li.step_index=0 THEN li.created_at ELSE NULL END,li.next_due,li.updated_at""",
        (user_id, stamp(now))).fetchall()
    from .study.generations import selected_entry
    return [selected_entry(conn, user_id, _row_to_entry(row), preview_affected_ids(conn, user_id, row['id'])) for row in rows]


def recover_ready_previews(conn, user_id, now):
    """Resolve a restored pending tail whose last unseen parent was deleted."""
    rows = conn.execute("""SELECT DISTINCT batch_id FROM learning_items
        WHERE user_id=? AND status='active' AND first_due_pending=1""", (user_id,)).fetchall()
    for row in rows:
        finalize_preview_if_ready(conn, user_id, row[0], now)
