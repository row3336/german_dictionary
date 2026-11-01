"""Atomic original answers, retry receipts, correction and exact-event undo.

The caller owns the per-user study lock. Receipts never restore a live session
after restart; only SQL history survives.
"""

import json
from datetime import date

from ..storage import _connect
from . import repository as repo
from .logic import MODES, reduce_answer


class StaleWrittenAction(ValueError):
    """The live question no longer authorizes this action."""


def action_key(frame, retry=False):
    return "written:{}:{}:{}".format("retry" if retry else "answer",
                                     frame["flow_id"], frame["question_token"])


def _replay(conn, user_id, delivery):
    result = dict(delivery["result"])
    action = repo.action(conn, user_id, delivery["action_key"]) if delivery["action_key"] else None
    result["status"] = "undone" if action and action["state"] == "undone" else "replayed"
    return result


def find_delivery(db_path, user_id, update_id):
    """Check transport replay before looking at the current question."""
    with _connect(db_path) as conn:
        found = repo.delivery(conn, user_id, update_id)
        return _replay(conn, user_id, found) if found else None


def _validate(conn, user_id, word_id, mode, frame, retry):
    repo.authorize(conn, user_id)
    if mode not in MODES:
        raise StaleWrittenAction("unsupported written mode")
    if (not frame or frame.get("user_id") != int(user_id) or frame.get("mode") != mode
            or frame.get("word_id") != word_id or bool(frame.get("retry")) != retry
            or frame.get("send_state") != "confirmed" or not frame.get("flow_id")
            or not frame.get("question_token") or frame.get("consumed")):
        raise StaleWrittenAction("question is closed or not confirmed")
    if not repo.row(conn, "SELECT id FROM words WHERE id=?", (word_id,)):
        raise StaleWrittenAction("word no longer exists")


def _existing(conn, user_id, update_id, key, frame, kind):
    prior = repo.delivery(conn, user_id, update_id)
    if prior:
        return _replay(conn, user_id, prior)
    existing = repo.action(conn, user_id, key)
    if existing:
        result = {"status": existing["state"], "receipt": existing["receipt"]}
        repo.save_delivery(conn, user_id, update_id, key, frame["flow_id"],
                           "written:" + kind, result)
        result["status"] = "undone" if existing["state"] == "undone" else "replayed"
        return result
    return None


def _base_receipt(user_id, word_id, mode, frame, kind, key, today):
    return {"user_id": int(user_id), "word_id": word_id, "mode": mode,
            "flow_id": frame["flow_id"], "question_token": frame["question_token"],
            "revision": int(frame["revision"]), "kind": kind, "action_key": key,
            "today": today.isoformat()}


def _finish(conn, user_id, update_id, receipt):
    repo.save_action(conn, user_id, receipt)
    result = {"status": "applied", "receipt": receipt}
    repo.save_delivery(conn, user_id, update_id, receipt["action_key"],
                       receipt["flow_id"], "written:" + receipt["kind"], result)
    return result


def apply_original_answer(db_path, user_id, word_id, mode, kind, frame,
                          transport_update_id, today=None):
    today = today or date.today()
    with repo.transaction(db_path) as conn:
        prior = repo.delivery(conn, user_id, transport_update_id)
        if prior:
            return _replay(conn, user_id, prior)
        if not frame:
            raise StaleWrittenAction("no live question")
        key = action_key(frame)
        replay = _existing(conn, user_id, transport_update_id, key, frame, kind)
        if replay:
            return replay
        _validate(conn, user_id, word_id, mode, frame, False)
        before = repo.progress(conn, user_id, word_id, mode)
        after = reduce_answer(before, kind, mode, today)
        previous = repo.row(conn, """SELECT is_correct FROM quiz_events
            WHERE user_id=? AND word_id=? AND mode=? ORDER BY id DESC LIMIT 1""",
            (int(user_id), word_id, mode))
        repo.write_progress(conn, user_id, word_id, mode, after)
        event_id = conn.execute("""INSERT INTO quiz_events
            (user_id,word_id,mode,is_correct,prev_is_correct) VALUES(?,?,?,?,?)""",
            (int(user_id), word_id, mode, int(kind == "correct"),
             previous["is_correct"] if previous else None)).lastrowid
        receipt = _base_receipt(user_id, word_id, mode, frame, kind, key, today)
        receipt.update(before=before, after=repo.progress(conn, user_id, word_id, mode),
                       event_id=event_id, event_before=None,
                       event_after=repo.row(conn, "SELECT * FROM quiz_events WHERE id=?", (event_id,)),
                       tx_id=repo.log(conn, user_id, key, kind))
        return _finish(conn, user_id, transport_update_id, receipt)


def apply_retry(db_path, user_id, word_id, mode, kind, frame, transport_update_id,
                live_before, live_after, today=None):
    """Persist retry consumption without touching progress, events or undo log."""
    if kind not in {"correct", "wrong", "idk"}:
        raise ValueError("unsupported retry answer")
    with repo.transaction(db_path) as conn:
        prior = repo.delivery(conn, user_id, transport_update_id)
        if prior:
            return _replay(conn, user_id, prior)
        if not frame:
            raise StaleWrittenAction("no live question")
        key = action_key(frame, retry=True)
        replay = _existing(conn, user_id, transport_update_id, key, frame, "retry")
        if replay:
            return replay
        _validate(conn, user_id, word_id, mode, frame, True)
        receipt = _base_receipt(user_id, word_id, mode, frame, "retry", key, today or date.today())
        receipt.update(answer_kind=kind, live_before=live_before, live_after=live_after)
        return _finish(conn, user_id, transport_update_id, receipt)


def record_ungraded_delivery(db_path, user_id, update_id, frame):
    """A resend consumes this Update even when sending fails again."""
    with repo.transaction(db_path) as conn:
        prior = repo.delivery(conn, user_id, update_id)
        if prior:
            return _replay(conn, user_id, prior)
        repo.authorize(conn, user_id)
        result = {"status": "ungraded", "question_token": frame["question_token"]}
        repo.save_delivery(conn, user_id, update_id, None, frame["flow_id"],
                           "written:resend", result)
        return result


def correct_last_answer(db_path, user_id, mode, parent_key, frame,
                        transport_update_id, today=None):
    today = today or date.today()
    with repo.transaction(db_path) as conn:
        prior = repo.delivery(conn, user_id, transport_update_id)
        if prior:
            return _replay(conn, user_id, prior)
        repo.authorize(conn, user_id)
        parent = repo.action(conn, user_id, parent_key) if parent_key else None
        key = "written:correction:" + (parent_key or "")
        if parent:
            replay = _existing(conn, user_id, transport_update_id, key, frame, "correction")
            if replay:
                return replay
        original = parent["receipt"] if parent else {}
        valid = (parent and parent["state"] == "applied"
                 and parent["kind"] in {"written:wrong", "written:idk"}
                 and mode in MODES and original.get("mode") == mode
                 and frame and frame.get("user_id") == int(user_id)
                 and frame.get("mode") == mode and frame.get("flow_id") == parent["flow_id"]
                 and frame.get("last_original_key") == parent_key
                 and frame.get("send_state") == "confirmed")
        status = "unavailable"
        event = None
        if valid:
            word_id = original["word_id"]
            event = repo.row(conn, """SELECT * FROM quiz_events
                WHERE id=? AND user_id=? AND word_id=? AND mode=?""",
                (original["event_id"], int(user_id), word_id, mode))
            exists = repo.row(conn, "SELECT id FROM words WHERE id=?", (word_id,))
            if not exists or event != original["event_after"]:
                valid = False
            elif repo.progress(conn, user_id, word_id, mode) != original["after"]:
                valid = False
                status = "conflict"
            latest = repo.row(conn, """SELECT id FROM quiz_events
                WHERE user_id=? AND word_id=? AND mode=? ORDER BY id DESC LIMIT 1""",
                (int(user_id), word_id, mode))
            if not latest or latest["id"] != original["event_id"]:
                valid = False
        if not valid:
            result = {"status": status}
            repo.save_delivery(conn, user_id, transport_update_id, None,
                               frame.get("flow_id", "") if frame else "",
                               "written:correction", result)
            return result
        before = repo.progress(conn, user_id, word_id, mode)
        after = reduce_answer(original["before"], "correct", mode, today)
        repo.write_progress(conn, user_id, word_id, mode, after)
        conn.execute("UPDATE quiz_events SET is_correct=1 WHERE id=? AND user_id=?",
                     (original["event_id"], int(user_id)))
        receipt = _base_receipt(user_id, word_id, mode, frame, "correction", key, today)
        receipt.update(parent_action_key=parent_key, before=before,
                       after=repo.progress(conn, user_id, word_id, mode),
                       event_id=original["event_id"], event_before=event,
                       event_after=repo.row(conn, "SELECT * FROM quiz_events WHERE id=?",
                                            (original["event_id"],)),
                       tx_id=repo.log(conn, user_id, key, "correction"))
        return _finish(conn, user_id, transport_update_id, receipt)


def _advance_plan(conn, user_id, update_id, log_id, restored, reason=""):
    plan = repo.delivery(conn, user_id, update_id)
    result = plan["result"]
    cursor = result["cursor"]
    if cursor >= len(result["log_ids"]) or result["log_ids"][cursor] != log_id:
        raise StaleWrittenAction("undo cursor changed")
    result["cursor"] += 1
    result["restored"] += int(restored)
    if reason:
        result.setdefault("skipped", []).append({"log_id": log_id, "reason": reason})
    state = "done" if result["cursor"] == len(result["log_ids"]) else "planned"
    conn.execute("""UPDATE study_deliveries SET result=?,state=?
        WHERE user_id=? AND transport_update_id=?""",
        (repo.dumps(result), state, int(user_id), int(update_id)))
    return result


def undo_written_log(db_path, user_id, log_id, plan_update_id=None):
    """Restore one exact written receipt; stale/deleted steps are consumed safely."""
    with repo.transaction(db_path) as conn:
        repo.authorize(conn, user_id)
        log = repo.row(conn, "SELECT * FROM transaction_log WHERE id=? AND user_id=?",
                       (int(log_id), int(user_id)))
        restored = False
        reason = "missing"
        if log:
            try:
                payload = json.loads(log["payload"])
            except (ValueError, TypeError):
                payload = {}
            key = payload.get("written_action_key") if isinstance(payload, dict) else None
            action = repo.action(conn, user_id, key) if key and key.startswith("written:") else None
            receipt = action["receipt"] if action else {}
            word_id, mode = receipt.get("word_id"), receipt.get("mode")
            event = repo.row(conn, "SELECT * FROM quiz_events WHERE id=? AND user_id=?",
                             (receipt.get("event_id"), int(user_id)))
            latest = repo.row(conn, """SELECT id FROM quiz_events WHERE user_id=?
                AND word_id=? AND mode=? ORDER BY id DESC LIMIT 1""",
                (int(user_id), word_id, mode))
            valid = (action and action["state"] == "applied"
                     and receipt.get("user_id") == int(user_id)
                     and receipt.get("tx_id") == int(log_id) and mode in MODES
                     and repo.row(conn, "SELECT id FROM words WHERE id=?", (word_id,))
                     and repo.progress(conn, user_id, word_id, mode) == receipt.get("after")
                     and event == receipt.get("event_after") and latest
                     and latest["id"] == receipt.get("event_id"))
            if valid:
                if receipt["before"] is None:
                    conn.execute("DELETE FROM progress WHERE user_id=? AND word_id=? AND mode=?",
                                 (int(user_id), word_id, mode))
                else:
                    repo.write_progress(conn, user_id, word_id, mode, receipt["before"])
                if receipt["event_before"] is None:
                    conn.execute("DELETE FROM quiz_events WHERE id=? AND user_id=?",
                                 (receipt["event_id"], int(user_id)))
                else:
                    conn.execute("UPDATE quiz_events SET is_correct=? WHERE id=? AND user_id=?",
                                 (receipt["event_before"]["is_correct"], receipt["event_id"], int(user_id)))
                restored = True
                reason = ""
            else:
                reason = "stale state or missing word"
            if action and receipt.get("tx_id") == int(log_id):
                conn.execute("UPDATE study_actions SET state='undone' WHERE user_id=? AND action_key=?",
                             (int(user_id), key))
            conn.execute("DELETE FROM transaction_log WHERE id=? AND user_id=?",
                         (int(log_id), int(user_id)))
        if plan_update_id is not None:
            _advance_plan(conn, user_id, plan_update_id, int(log_id), restored, reason)
        return {"status": "restored" if restored else "skipped", "reason": reason}


def start_undo_plan(db_path, user_id, update_id, steps):
    """Pin IDs before any undo, resume incomplete plans before selecting more."""
    with repo.transaction(db_path) as conn:
        repo.authorize(conn, user_id)
        prior = repo.delivery(conn, user_id, update_id)
        if prior:
            if prior["kind"] == "written_undo_alias":
                prior = repo.delivery(conn, user_id, prior["result"]["plan_update_id"])
            return prior
        pending = repo.row(conn, """SELECT transport_update_id FROM study_deliveries
            WHERE user_id=? AND kind='written_undo_plan' AND state='planned'
            ORDER BY created_at,transport_update_id LIMIT 1""", (int(user_id),))
        if pending:
            plan_id = pending["transport_update_id"]
            repo.save_delivery(conn, user_id, update_id, None, "", "written_undo_alias",
                               {"plan_update_id": plan_id})
            return repo.delivery(conn, user_id, plan_id)
        ids = [r[0] for r in conn.execute("""SELECT id FROM transaction_log
            WHERE user_id=? ORDER BY id DESC LIMIT ?""", (int(user_id), max(1, min(20, steps))))]
        result = {"log_ids": ids, "cursor": 0, "restored": 0, "skipped": []}
        repo.save_delivery(conn, user_id, update_id, None, "", "written_undo_plan",
                           result, state="planned" if ids else "done")
        return repo.delivery(conn, user_id, update_id)


def advance_undo_plan(db_path, user_id, plan_update_id, log_id, restored, reason=""):
    with repo.transaction(db_path) as conn:
        return _advance_plan(conn, user_id, plan_update_id, log_id, restored, reason)


def finish_undo_result(db_path, user_id, plan_update_id, total, text):
    """Retain the exact summary so a delayed replay returns that command's result."""
    with repo.transaction(db_path) as conn:
        plan = repo.delivery(conn, user_id, plan_update_id)
        result = plan["result"]
        if "text" not in result:
            result.update(total=total, text=text)
            conn.execute("""UPDATE study_deliveries SET result=?
                WHERE user_id=? AND transport_update_id=?""",
                (repo.dumps(result), int(user_id), int(plan_update_id)))
        return result
