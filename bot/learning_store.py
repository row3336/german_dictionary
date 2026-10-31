"""Durable Learning questions and atomic Learning-only actions."""

import json
import random
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from .learning_logic import reduce_answer, stamp
from .storage import _connect, _row_to_entry


class StaleLearningAction(ValueError):
    """The displayed Learning question no longer accepts this action."""


@contextmanager
def transaction(db_path):
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        yield conn


def _row(conn, sql, args):
    found = conn.execute(sql, args).fetchone()
    return dict(found) if found else None


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _display(conn, user_id):
    return _row(conn, "SELECT * FROM learning_display_state WHERE user_id=?", (int(user_id),))


def get_display(db_path, user_id):
    with _connect(db_path) as conn:
        return _display(conn, user_id)


def get_display_entry(db_path, user_id, display):
    """Read the pinned word, including personal question fields, without choosing anew."""
    if not display or not display.get("word_id"):
        return None
    with _connect(db_path) as conn:
        row = conn.execute("""SELECT w.*, li.step_index AS learning_step,
            li.next_due AS learning_due, li.batch_id AS learning_batch_id,
            li.recovery_remaining AS learning_recovery_remaining,
            li.cycle_token AS learning_cycle_token
            FROM learning_items li JOIN words w ON w.id=li.word_id
            WHERE li.user_id=? AND li.word_id=? AND li.status='active'
              AND li.cycle_token=?""",
            (int(user_id), display["word_id"], display["cycle_token"])).fetchone()
    return _row_to_entry(row) if row else None


def refresh_invalid_display(db_path, user_id, recent_ids=(), now=None):
    """Replace a pinned word only if it was deleted or entered a new cycle."""
    now = now or datetime.now(timezone.utc)
    with transaction(db_path) as conn:
        display = _display(conn, user_id)
        if not display or not display["word_id"]:
            return display
        item = _item(conn, user_id, display["word_id"])
        if item and item["status"] == "active" and item["cycle_token"] == display["cycle_token"]:
            return display
        return _save_display(conn, user_id, display["flow_id"], recent_ids, now,
                             display["feedback_text"], display["source_action_key"])


def _choose(conn, user_id, recent_ids, now):
    preview = _row(conn, """SELECT word_id,cycle_token FROM learning_items
        WHERE user_id=? AND status='active' AND preview_done=0
        ORDER BY created_at,word_id LIMIT 1""", (int(user_id),))
    if preview:
        return "preview", preview
    due = [dict(row) for row in conn.execute("""SELECT word_id,cycle_token,step_index
        FROM learning_items WHERE user_id=? AND status='active' AND preview_done=1
          AND next_due<=? ORDER BY created_at,word_id""", (int(user_id), stamp(now)))]
    if due:
        recent = list(recent_ids or [])[-2:]
        alternatives = [item for item in due if item["word_id"] != recent[-1]] if recent else due
        pool = alternatives or due
        first = [item for item in pool if item["step_index"] == 0]
        if first:
            return "question", first[0]
        spaced = [item for item in pool if item["word_id"] not in recent]
        return "question", random.choice(spaced or pool)
    active = conn.execute("SELECT 1 FROM learning_items WHERE user_id=? AND status='active' LIMIT 1",
                          (int(user_id),)).fetchone()
    return ("waiting" if active else "completed"), None


def _save_display(conn, user_id, flow_id, recent_ids, now, feedback="", source_action_key=None,
                  keep=None):
    old = _display(conn, user_id)
    revision = int(old["revision"]) + 1 if old else 1
    if keep:
        phase, word_id, token, action_key = (keep[k] for k in
                                             ("phase", "word_id", "cycle_token", "action_key"))
    else:
        phase, chosen = _choose(conn, user_id, recent_ids, now)
        word_id = chosen["word_id"] if chosen else None
        token = chosen["cycle_token"] if chosen else None
        action_key = uuid.uuid4().hex if chosen else None
    conn.execute("""INSERT INTO learning_display_state
        (user_id,flow_id,revision,phase,word_id,action_key,source_action_key,
         feedback_text,send_state,cycle_token,suspended_for_clarify,updated_at)
        VALUES (?,?,?,?,?,?,?,?,'planned',?,0,?)
        ON CONFLICT(user_id) DO UPDATE SET
        flow_id=excluded.flow_id,revision=excluded.revision,phase=excluded.phase,
        word_id=excluded.word_id,action_key=excluded.action_key,
        source_action_key=excluded.source_action_key,feedback_text=excluded.feedback_text,
        send_state='planned',telegram_message_id=NULL,cycle_token=excluded.cycle_token,
        suspended_for_clarify=0,updated_at=excluded.updated_at""",
        (int(user_id), flow_id, revision, phase, word_id, action_key,
         source_action_key, feedback, token, stamp(now)))
    return _display(conn, user_id)


def start_display(db_path, user_id, recent_ids=(), now=None):
    """Open a new Learning flow. Legacy in-memory questions are never guessed."""
    now = now or datetime.now(timezone.utc)
    with transaction(db_path) as conn:
        return _save_display(conn, user_id, uuid.uuid4().hex, recent_ids, now)


def close_display(db_path, user_id):
    with transaction(db_path) as conn:
        conn.execute("DELETE FROM learning_display_state WHERE user_id=?", (int(user_id),))


def suspend_for_clarify(db_path, user_id):
    with transaction(db_path) as conn:
        conn.execute("""UPDATE learning_display_state
            SET suspended_for_clarify=1,send_state='planned',telegram_message_id=NULL
            WHERE user_id=?""",
                     (int(user_id),))


def mark_sent(db_path, user_id, display, state, telegram_message_id=None):
    if state not in {"confirmed", "uncertain", "failed"}:
        raise ValueError("invalid Learning delivery state")
    with transaction(db_path) as conn:
        changed = conn.execute("""UPDATE learning_display_state
            SET send_state=?,telegram_message_id=?,
                suspended_for_clarify=CASE WHEN ?='confirmed' THEN 0 ELSE suspended_for_clarify END,
                updated_at=CURRENT_TIMESTAMP
            WHERE user_id=? AND flow_id=? AND revision=?
              AND action_key IS ? AND send_state IN ('planned','uncertain')""",
            (state, telegram_message_id, state, int(user_id), display["flow_id"],
             display["revision"], display["action_key"])).rowcount
    return changed == 1


def _item(conn, user_id, word_id):
    return _row(conn, "SELECT * FROM learning_items WHERE user_id=? AND word_id=?",
                (int(user_id), word_id))


def _write_item(conn, row):
    conn.execute("""UPDATE learning_items SET step_index=?,recovery_remaining=?,
        next_due=?,status=?,preview_done=?,first_due_pending=?,updated_at=?
        WHERE user_id=? AND word_id=? AND cycle_token=?""",
        (row["step_index"], row["recovery_remaining"], row["next_due"], row["status"],
         row["preview_done"], row["first_due_pending"], row["updated_at"],
         row["user_id"], row["word_id"], row["cycle_token"]))


def _authorize(conn, user_id):
    from .app_config import is_owner
    if is_owner(user_id):
        return
    user = conn.execute("SELECT status FROM users WHERE user_id=?", (int(user_id),)).fetchone()
    if not user or user[0] != "approved":
        raise PermissionError("Learning action requires an approved user")


def _delivery(conn, user_id, transport_update_id):
    return _row(conn, """SELECT * FROM study_deliveries
        WHERE user_id=? AND transport_update_id=?""", (int(user_id), int(transport_update_id)))


def _save_delivery(conn, user_id, transport_update_id, action_key, flow_id, kind, result):
    conn.execute("""INSERT INTO study_deliveries
        (user_id,transport_update_id,action_key,kind,flow_id,state,result)
        VALUES (?,?,?,?,?,'committed',?)""",
        (int(user_id), int(transport_update_id), action_key, kind, flow_id, _json(result)))


def _action(conn, user_id, action_key):
    found = _row(conn, "SELECT * FROM study_actions WHERE user_id=? AND action_key=?",
                 (int(user_id), action_key))
    if found:
        found["receipt"] = json.loads(found["receipt"])
    return found


def _save_action(conn, user_id, action_key, kind, display, receipt):
    conn.execute("""INSERT INTO study_actions
        (user_id,action_key,kind,flow_id,revision,word_id,state,receipt)
        VALUES (?,?,?,?,?,?,'applied',?)""",
        (int(user_id), action_key, kind, display["flow_id"], display["revision"],
         display["word_id"], _json(receipt)))


def _log(conn, user_id, action, action_key):
    return conn.execute("""INSERT INTO transaction_log(user_id,action,payload)
        VALUES (?,?,?)""", (int(user_id), action,
                            _json({"learning_action_key": action_key}))).lastrowid


def _apply_preview(conn, user_id, item, kind, now):
    """Preview and all newly assigned first deadlines form one action."""
    batch_id = item["batch_id"]
    pending_other = conn.execute("""SELECT 1 FROM learning_items
        WHERE user_id=? AND batch_id=? AND status='active' AND preview_done=0
          AND word_id!=? LIMIT 1""", (int(user_id), batch_id, item["word_id"])).fetchone()
    finalize = pending_other is None
    affected = {item["word_id"]: item}
    if finalize:
        for row in conn.execute("""SELECT * FROM learning_items
            WHERE user_id=? AND batch_id=? AND status='active' AND preview_done=1
              AND first_due_pending=1""", (int(user_id), batch_id)):
            affected[row["word_id"]] = dict(row)
    if kind == "preview_next":
        conn.execute("""UPDATE learning_items SET preview_done=1,updated_at=?
            WHERE user_id=? AND word_id=? AND cycle_token=? AND status='active'""",
            (stamp(now), int(user_id), item["word_id"], item["cycle_token"]))
    else:
        conn.execute("""UPDATE learning_items SET status='skipped',updated_at=?
            WHERE user_id=? AND word_id=? AND cycle_token=? AND status='active'""",
            (stamp(now), int(user_id), item["word_id"], item["cycle_token"]))
    if finalize:
        conn.execute("""UPDATE learning_items SET next_due=?,first_due_pending=0,
            updated_at=? WHERE user_id=? AND batch_id=? AND status='active'
              AND preview_done=1 AND first_due_pending=1""",
            (stamp(now), stamp(now), int(user_id), batch_id))
    conn.execute("UPDATE learning_batches SET updated_at=? WHERE id=? AND user_id=?",
                 (stamp(now), batch_id, int(user_id)))
    return affected


def apply_action(db_path, user_id, transport_update_id, action_key, kind,
                 schedule, feedback, recent_ids=(), now=None):
    """Commit a Learning answer/next/skip, event, undo, and next display together."""
    if kind not in {"correct", "wrong", "idk", "preview_next", "skip"}:
        raise ValueError("unsupported Learning action")
    now = now or datetime.now(timezone.utc)
    with transaction(db_path) as conn:
        _authorize(conn, user_id)
        old_delivery = _delivery(conn, user_id, transport_update_id)
        if old_delivery:
            return {"status": "replayed", "result": json.loads(old_delivery["result"])}
        old_action = _action(conn, user_id, action_key)
        if old_action:
            result = {"status": "replayed", "receipt": old_action["receipt"]}
            _save_delivery(conn, user_id, transport_update_id, action_key,
                           old_action["flow_id"], kind, result)
            return result
        display = _display(conn, user_id)
        if not display or display["action_key"] != action_key:
            result = {"status": "stale"}
            _save_delivery(conn, user_id, transport_update_id, None,
                           display["flow_id"] if display else "", kind, result)
            return result
        if display["send_state"] != "confirmed" or display["suspended_for_clarify"]:
            result = {"status": "unconfirmed"}
            _save_delivery(conn, user_id, transport_update_id, None,
                           display["flow_id"], kind, result)
            return result
        if kind == "preview_next" and display["phase"] != "preview":
            raise StaleLearningAction("next requires a preview")
        if kind in {"correct", "wrong", "idk"} and display["phase"] != "question":
            raise StaleLearningAction("answer requires a question")
        item = _item(conn, user_id, display["word_id"])
        if not item or item["status"] != "active" or item["cycle_token"] != display["cycle_token"]:
            result = {"status": "stale"}
            _save_delivery(conn, user_id, transport_update_id, None, display["flow_id"], kind, result)
            return result
        if kind == "skip" and display["phase"] not in {"preview", "question"}:
            raise StaleLearningAction("skip requires a word")
        if kind in {"correct", "wrong", "idk"}:
            after = reduce_answer(item, kind, schedule, now)
            _write_item(conn, after)
            before_rows = {item["word_id"]: item}
            previous = conn.execute("""SELECT is_correct FROM quiz_events
                WHERE user_id=? AND word_id=? AND mode='learning'
                ORDER BY id DESC LIMIT 1""", (int(user_id), item["word_id"])).fetchone()
            event_id = conn.execute("""INSERT INTO quiz_events
                (user_id,word_id,mode,is_correct,prev_is_correct)
                VALUES (?,?,'learning',?,?)""",
                (int(user_id), item["word_id"], int(kind == "correct"),
                 None if previous is None else int(previous[0]))).lastrowid
        elif display["phase"] == "preview":
            before_rows = _apply_preview(conn, user_id, item, kind, now)
            event_id = None
        else:
            before_rows = {item["word_id"]: item}
            conn.execute("""UPDATE learning_items SET status='skipped',updated_at=?
                WHERE user_id=? AND word_id=? AND cycle_token=?""",
                (stamp(now), int(user_id), item["word_id"], item["cycle_token"]))
            event_id = None
        after_rows = {word_id: _item(conn, user_id, word_id) for word_id in before_rows}
        feedback_text = feedback(after_rows) if callable(feedback) else feedback
        plan = _save_display(conn, user_id, display["flow_id"], recent_ids, now,
                             feedback_text, action_key if kind in {"correct", "wrong", "idk"} else None)
        receipt = {"action_key": action_key, "kind": kind, "word_id": item["word_id"],
                   "flow_id": display["flow_id"], "revision": display["revision"],
                   "before_rows": before_rows, "after_rows": after_rows,
                   "event_id": event_id, "cycle_token": item["cycle_token"]}
        tx_id = _log(conn, user_id, "learning:" + kind, action_key)
        receipt["tx_id"] = tx_id
        _save_action(conn, user_id, action_key, kind, display, receipt)
        result = {"status": "applied", "receipt": receipt, "display": plan}
        _save_delivery(conn, user_id, transport_update_id, action_key,
                       display["flow_id"], kind, result)
        return result


def recover_display(db_path, user_id, transport_update_id, recent_ids=(), now=None):
    """Consume one fresh input to re-show a planned or uncertain screen."""
    now = now or datetime.now(timezone.utc)
    with transaction(db_path) as conn:
        _authorize(conn, user_id)
        prior = _delivery(conn, user_id, transport_update_id)
        if prior:
            return {"status": "replayed"}
        display = _display(conn, user_id)
        if not display:
            return {"status": "closed"}
        if display["word_id"]:
            item = _item(conn, user_id, display["word_id"])
            if not item or item["status"] != "active" or item["cycle_token"] != display["cycle_token"]:
                display = _save_display(conn, user_id, display["flow_id"], recent_ids, now,
                                        display["feedback_text"], display["source_action_key"])
            else:
                conn.execute("""UPDATE learning_display_state SET send_state='planned',
                    telegram_message_id=NULL WHERE user_id=?""", (int(user_id),))
                display = _display(conn, user_id)
        else:
            phase, chosen = _choose(conn, user_id, recent_ids, now)
            if chosen or phase != display["phase"]:
                display = _save_display(conn, user_id, display["flow_id"], recent_ids, now,
                                        display["feedback_text"], display["source_action_key"])
            else:
                conn.execute("""UPDATE learning_display_state SET send_state='planned',
                    telegram_message_id=NULL WHERE user_id=?""", (int(user_id),))
                display = _display(conn, user_id)
        _save_delivery(conn, user_id, transport_update_id, None,
                       display["flow_id"], "recover", {"status": "recovered"})
        return {"status": "recovered", "display": display}


def correct_last(db_path, user_id, transport_update_id, parent_action_key,
                 schedule, feedback, recent_ids=(), now=None):
    """Replace one wrong answer from its before-state and retain its event ID."""
    now = now or datetime.now(timezone.utc)
    with transaction(db_path) as conn:
        _authorize(conn, user_id)
        prior = _delivery(conn, user_id, transport_update_id)
        if prior:
            return {"status": "replayed"}
        parent = _action(conn, user_id, parent_action_key) if parent_action_key else None
        display = _display(conn, user_id)
        if (not parent or parent["state"] != "applied"
                or parent["kind"] not in {"wrong", "idk"}
                or not display or display["source_action_key"] != parent_action_key
                or display["flow_id"] != parent["flow_id"]):
            result = {"status": "unavailable"}
            _save_delivery(conn, user_id, transport_update_id, None,
                           display["flow_id"] if display else "", "correction", result)
            return result
        original = parent["receipt"]
        word_id = original["word_id"]
        before = original["before_rows"][word_id]
        current = _item(conn, user_id, word_id)
        event_id = original["event_id"]
        event = _row(conn, """SELECT * FROM quiz_events WHERE id=? AND user_id=?
            AND word_id=? AND mode='learning'""", (event_id, int(user_id), word_id))
        if (not current or current["cycle_token"] != original["cycle_token"]
                or not event or event["is_correct"] != 0):
            raise StaleLearningAction("original Learning answer is no longer correctable")
        after = reduce_answer(before, "correct", schedule, now)
        _write_item(conn, after)
        conn.execute("UPDATE quiz_events SET is_correct=1 WHERE id=? AND user_id=?",
                     (event_id, int(user_id)))
        conn.execute("UPDATE learning_batches SET updated_at=? WHERE id=? AND user_id=?",
                     (stamp(now), current["batch_id"], int(user_id)))
        keep = display if display["word_id"] != word_id else None
        if display["word_id"] == word_id and after["status"] == "active" and after["next_due"] <= stamp(now):
            keep = display
        correction_key = uuid.uuid4().hex
        plan = _save_display(conn, user_id, display["flow_id"], recent_ids, now,
                             feedback, correction_key, keep=keep)
        receipt = {"action_key": correction_key, "kind": "correction",
                   "parent_action_key": parent_action_key, "word_id": word_id,
                   "flow_id": display["flow_id"], "revision": display["revision"],
                   "before_rows": {word_id: current},
                   "after_rows": {word_id: _item(conn, user_id, word_id)},
                   "event_id": event_id, "event_before": event,
                   "cycle_token": current["cycle_token"]}
        tx_id = _log(conn, user_id, "input_err:learning", correction_key)
        receipt["tx_id"] = tx_id
        _save_action(conn, user_id, correction_key, "correction", display, receipt)
        result = {"status": "applied", "receipt": receipt, "display": plan}
        _save_delivery(conn, user_id, transport_update_id, correction_key,
                       display["flow_id"], "correction", result)
        return result


def undo_learning_log(db_path, user_id, log_id):
    """Restore one Learning step, extinguish its receipt, and remove its log atomically."""
    with transaction(db_path) as conn:
        log = _row(conn, "SELECT * FROM transaction_log WHERE id=? AND user_id=?",
                   (int(log_id), int(user_id)))
        if not log:
            return {"status": "missing"}
        try:
            payload = json.loads(log["payload"])
        except (ValueError, TypeError):
            payload = {}
        action_key = payload.get("learning_action_key") if isinstance(payload, dict) else None
        action = _action(conn, user_id, action_key) if action_key else None
        if not action or action["state"] != "applied":
            conn.execute("DELETE FROM transaction_log WHERE id=? AND user_id=?",
                         (int(log_id), int(user_id)))
            conn.execute("DELETE FROM learning_display_state WHERE user_id=?", (int(user_id),))
            return {"status": "skipped", "reason": "legacy_or_unavailable"}
        receipt = action["receipt"]
        before_rows = receipt.get("before_rows")
        if not isinstance(before_rows, dict) or not before_rows:
            raise ValueError("invalid Learning undo receipt")
        restored = []
        skipped = []
        for word_id, before in before_rows.items():
            if not isinstance(before, dict) or not before.get("cycle_token"):
                skipped.append(word_id)
                continue
            current = _item(conn, user_id, word_id)
            batch = conn.execute("SELECT 1 FROM learning_batches WHERE id=? AND user_id=?",
                                 (before.get("batch_id"), int(user_id))).fetchone()
            if (not current or not batch or current["cycle_token"] != before["cycle_token"]
                    or before.get("user_id") != int(user_id) or before.get("word_id") != word_id):
                skipped.append(word_id)
                continue
            _write_item(conn, before)
            restored.append(word_id)
            conn.execute("UPDATE learning_batches SET updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
                         (before["batch_id"], int(user_id)))
        event_id = receipt.get("event_id")
        if event_id and receipt["word_id"] in restored:
            if action["kind"] == "correction":
                event_before = receipt.get("event_before") or {}
                conn.execute("UPDATE quiz_events SET is_correct=? WHERE id=? AND user_id=?",
                             (event_before.get("is_correct", 0), int(event_id), int(user_id)))
            else:
                conn.execute("DELETE FROM quiz_events WHERE id=? AND user_id=?",
                             (int(event_id), int(user_id)))
        conn.execute("UPDATE study_actions SET state='undone' WHERE user_id=? AND action_key=?",
                     (int(user_id), action_key))
        conn.execute("DELETE FROM transaction_log WHERE id=? AND user_id=?",
                     (int(log_id), int(user_id)))
        conn.execute("DELETE FROM learning_display_state WHERE user_id=?", (int(user_id),))
        return {"status": "restored" if restored else "skipped",
                "restored": restored, "skipped": skipped}
