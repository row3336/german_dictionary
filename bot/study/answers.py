"""Atomic answers, corrections, composite preview actions, and frozen undo."""

import json
from datetime import date, datetime, timezone

from . import deliveries, repository as repo
from .srs import apply_answer, default_srs
from .generations import assert_generation, generation_for, assert_generations, StaleGeneration
from ..learning_requests import assert_lifecycles


class StaleStudyAction(ValueError):
    """The authoritative live frame no longer accepts this new action."""


def authorize_user(conn, user_id):
    from ..app_config import is_owner
    row = conn.execute("SELECT status FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not is_owner(user_id) and (not row or row[0] != "approved"):
        raise PermissionError("study action requires an approved user")


def lookup_action(conn, user_id, action_key):
    row = conn.execute("SELECT state,receipt_json FROM study_actions WHERE user_id=? AND action_key=?",
                       (user_id, action_key)).fetchone()
    if row is None:
        return None
    result = json.loads(row["receipt_json"])
    result["state"] = row["state"]
    return result


def _delivery_result(conn, user_id, delivery):
    result = delivery["result"]
    if delivery.get("action_key"):
        result = lookup_action(conn, user_id, delivery["action_key"]) or result
    return {**result, "replayed": True}


def _accepted(conn, user_id, action_key, transport_update_id, flow_id, kind):
    delivery = deliveries.lookup_delivery(conn, user_id, transport_update_id)
    if delivery:
        return _delivery_result(conn, user_id, delivery)
    receipt = lookup_action(conn, user_id, action_key)
    if receipt:
        deliveries.begin_delivery(conn, user_id, transport_update_id, flow_id, kind, action_key)
        deliveries.commit_delivery(conn, user_id, transport_update_id, receipt, action_key)
        return {**receipt, "replayed": True}
    return None


def _validate_target(conn, user_id, word_id, expected_revision, live_revision):
    current = live_revision() if callable(live_revision) else live_revision
    if current is None or current != expected_revision:
        raise StaleStudyAction("study frame revision changed")
    if not conn.execute("SELECT 1 FROM words WHERE id=?", (word_id,)).fetchone():
        raise StaleStudyAction("word no longer exists")


def _save_action(conn, user_id, action_key, flow_id, kind, word_id, mode, receipt):
    conn.execute("""INSERT INTO study_actions
        (user_id,action_key,flow_id,kind,word_id,mode,state,receipt_json)
        VALUES (?,?,?,?,?,?,'applied',?)""",
        (user_id, action_key, flow_id, kind, word_id, mode,
         json.dumps(receipt, ensure_ascii=False)))


def _finish(conn, user_id, action_key, transport_update_id, flow_id, kind,
            word_id, mode, snapshot, after, revision, event_id=None,
            parent_action_key=None, answer_kind=None, transaction_kind=None):
    payload = {"snapshot": snapshot, "action_key": action_key}
    tx_id = repo.append_transaction(conn, user_id, transaction_kind or kind, payload)
    receipt = {"version": 2, "state": "applied", "action_key": action_key,
               "parent_action_key": parent_action_key, "flow_id": flow_id,
               "word_id": word_id, "mode": mode, "kind": kind,
               "answer_kind": answer_kind, "revision": revision,
               "before": snapshot, "after": after, "event_id": event_id,
               "tx_id": tx_id, "replayed": False}
    _save_action(conn, user_id, action_key, flow_id, kind, word_id, mode, receipt)
    deliveries.begin_delivery(conn, user_id, transport_update_id, flow_id, kind, action_key)
    deliveries.commit_delivery(conn, user_id, transport_update_id, receipt, action_key)
    return receipt


def _record_recovery(conn, user_id, transport_update_id, flow_id, action_kind, recovered):
    result = {"state": "recovered_undo", "invalidate_frame": True, "undo": recovered}
    deliveries.begin_delivery(conn, user_id, transport_update_id, flow_id, action_kind)
    deliveries.commit_delivery(conn, user_id, transport_update_id, result)
    return result


def apply_original_answer(db_path, user_id, word_id, mode, answer_kind, action_key,
                          transport_update_id, expected_revision, now=None, *,
                          flow_id, live_revision, interval_divisor=1.0,
                          learning_transition=None, target_resolver=None, srs_day=None, expected_generation=0, expected_generations=None, expected_lifecycles=None):
    """Persist one original answer. Replay never invokes target_resolver."""
    # No explicit clock preserves the legacy host-local date.today() contract.
    day = srs_day or (now.date() if now is not None else date.today())
    now = now or datetime.now(timezone.utc)
    with repo.transaction(db_path) as conn:
        authorize_user(conn, user_id)
        accepted = _accepted(conn, user_id, action_key, transport_update_id, flow_id, "answer")
        if accepted is not None:
            return accepted
        recovered = recover_pending_undo(conn, user_id)
        if recovered:
            return _record_recovery(conn, user_id, transport_update_id, flow_id,
                                    "answer", recovered)
        if target_resolver:
            target = target_resolver(conn)
            word_id = target["word_id"]
            mode = target.get("mode", mode)
        _validate_target(conn, user_id, word_id, expected_revision, live_revision)
        assert_generations(conn, user_id, expected_generations or {})
        assert_generation(conn, user_id, word_id, expected_generation)
        if answer_kind not in {"correct", "wrong", "idk"}:
            raise ValueError("unsupported answer kind")
        if mode == "learning":
            if expected_lifecycles is not None and word_id not in expected_lifecycles:
                raise StaleGeneration("Learning target was not selected")
            assert_lifecycles(conn, user_id, expected_lifecycles or {word_id: 0})
        snapshot = repo.capture_snapshot(conn, user_id, word_id, [mode])
        if learning_transition:
            after = learning_transition(conn, snapshot, answer_kind, now)
        else:
            before_srs = repo.get_progress(conn, user_id, word_id, mode, day)
            after = apply_answer(before_srs, answer_kind, day, interval_divisor)
            repo.set_progress(conn, user_id, word_id, mode, after, now)
        event_id = repo.append_event(conn, user_id, word_id, mode, answer_kind == "correct", now)
        repo.record_created_events(conn, snapshot, [event_id])
        return _finish(conn, user_id, action_key, transport_update_id, flow_id, "answer",
                       word_id, mode, snapshot, after, expected_revision, event_id,
                       answer_kind=answer_kind)


def correct_last_answer(db_path, user_id, parent_action_key, action_key,
                        transport_update_id, expected_revision, now=None, *,
                        flow_id, live_revision, interval_divisor=1.0,
                        learning_transition=None, srs_day=None, expected_generations=None, expected_lifecycles=None):
    """Replace a wrong result from its original baseline, retaining its event."""
    day = srs_day or (now.date() if now is not None else date.today())
    now = now or datetime.now(timezone.utc)
    if isinstance(parent_action_key, dict):
        parent_action_key = parent_action_key["action_key"]
    with repo.transaction(db_path) as conn:
        authorize_user(conn, user_id)
        accepted = _accepted(conn, user_id, action_key, transport_update_id, flow_id, "correction")
        if accepted is not None:
            return accepted
        recovered = recover_pending_undo(conn, user_id)
        if recovered:
            return _record_recovery(conn, user_id, transport_update_id, flow_id,
                                    "correction", recovered)
        parent = lookup_action(conn, user_id, parent_action_key)
        if not parent or parent["state"] != "applied" or parent["kind"] != "answer":
            raise StaleStudyAction("original answer is unavailable")
        if parent["flow_id"] != flow_id or parent["answer_kind"] == "correct":
            raise StaleStudyAction("answer cannot be corrected")
        assert_generations(conn, user_id, expected_generations or {})
        repo.assert_snapshot_generations(conn, parent["before"])
        word_id, mode, event_id = parent["word_id"], parent["mode"], parent["event_id"]
        _validate_target(conn, user_id, word_id, expected_revision, live_revision)
        event = conn.execute("SELECT is_correct FROM quiz_events WHERE user_id=? AND id=?",
                             (user_id, event_id)).fetchone()
        if not event or event[0]:
            raise StaleStudyAction("answer was already corrected or deleted")
        if mode == "learning":
            pins = parent['before'].get('learning_lifecycles') or {}
            assert_lifecycles(conn, user_id, {wid: pins.get(wid, 0) for wid in
                {word_id, *(i['word_id'] for i in parent['before'].get('affected_learning_items', []))}})
            assert_lifecycles(conn, user_id, expected_lifecycles or {})
        snapshot = repo.capture_snapshot(conn, user_id, word_id, [mode], event_ids=[event_id])
        if learning_transition:
            after = learning_transition(conn, parent["before"], "correct", now)
        else:
            baseline = (parent["before"].get("progress") or {}).get(mode) or default_srs(day)
            after = apply_answer(baseline, "correct", day, interval_divisor)
            repo.set_progress(conn, user_id, word_id, mode, after, now)
        conn.execute("UPDATE quiz_events SET is_correct=1 WHERE user_id=? AND id=?", (user_id, event_id))
        return _finish(conn, user_id, action_key, transport_update_id, flow_id, "correction",
                       word_id, mode, snapshot, after, expected_revision, event_id,
                       parent_action_key, "correct")


def apply_preview_transition(db_path, user_id, word_id, action_kind, action_key,
                             transport_update_id, expected_revision, now=None, *,
                             flow_id, live_revision, transition,
                             affected_learning_ids=None, expected_generations=None, expected_lifecycles=None):
    """Capture every affected row before one preview next/skip finalization."""
    now = now or datetime.now(timezone.utc)
    with repo.transaction(db_path) as conn:
        authorize_user(conn, user_id)
        accepted = _accepted(conn, user_id, action_key, transport_update_id, flow_id, action_kind)
        if accepted is not None:
            return accepted
        recovered = recover_pending_undo(conn, user_id)
        if recovered:
            return _record_recovery(conn, user_id, transport_update_id, flow_id,
                                    action_kind, recovered)
        _validate_target(conn, user_id, word_id, expected_revision, live_revision)
        ids = affected_learning_ids(conn) if callable(affected_learning_ids) else affected_learning_ids
        for wid in dict.fromkeys([word_id, *(ids or [])]):
            if expected_generations is not None and wid not in expected_generations:
                raise StaleGeneration("preview participant was not selected: " + str(wid))
            assert_generation(conn, user_id, wid, (expected_generations or {}).get(wid, 0))
            if expected_lifecycles is not None and wid not in expected_lifecycles:
                raise StaleGeneration('preview Learning participant was not selected: ' + str(wid))
            assert_lifecycles(conn, user_id, {wid: (expected_lifecycles or {}).get(wid, 0)})
        snapshot = repo.capture_snapshot(conn, user_id, word_id, [], affected_learning_ids=ids)
        after = transition(conn, snapshot, now)
        return _finish(conn, user_id, action_key, transport_update_id, flow_id, action_kind,
                       word_id, "learning", snapshot, after, expected_revision)


def record_control_transition(db_path, user_id, transport_update_id, flow_id,
                              action_kind, resolve, *, expected_generations=None, expected_lifecycles=None):
    """Deduplicate next/reveal/stop before resolving the live frame."""
    with repo.transaction(db_path) as conn:
        authorize_user(conn, user_id)
        old = deliveries.lookup_delivery(conn, user_id, transport_update_id)
        if old:
            return _delivery_result(conn, user_id, old)
        recovered = recover_pending_undo(conn, user_id)
        if recovered:
            return _record_recovery(conn, user_id, transport_update_id, flow_id,
                                    action_kind, recovered)
        assert_generations(conn, user_id, expected_generations or {})
        assert_lifecycles(conn, user_id, expected_lifecycles or {})
        result = resolve(conn)
        if expected_lifecycles is not None:
            result = {**result, 'learning_lifecycles': dict(expected_lifecycles)}
        deliveries.begin_delivery(conn, user_id, transport_update_id, flow_id, action_kind)
        deliveries.commit_delivery(conn, user_id, transport_update_id, result)
        return result


def close_study_flow(db_path, user_id, transport_update_id, flow_id="navigation",
                     action_kind="start"):
    """Close live correction rights once, before callers invalidate UI frames.

    The caller holds study_lock. Persisted pass/results/admissions survive this
    navigation boundary; replay never closes a subsequently opened flow.
    """
    def close(conn):
        conn.execute("UPDATE conjugation_answers SET correction_session_id=NULL WHERE user_id=?",
                     (user_id,))
        return {"flow_id": flow_id, "invalidate_frame": True, "replayed": False}

    return record_control_transition(db_path, user_id, transport_update_id, flow_id,
                                     action_kind, close)


def undo_transaction(conn, user_id, tx_id):
    """Restore, extinguish receipt, and delete precisely one user's log."""
    row = conn.execute("SELECT payload FROM transaction_log WHERE user_id=? AND id=?",
                       (user_id, tx_id)).fetchone()
    if not row:
        return {"tx_id": tx_id, "status": "skipped", "reason": "missing"}
    conn.execute("SAVEPOINT undo_restore")
    try:
        payload = json.loads(row[0])
        if not isinstance(payload, dict):
            raise ValueError("invalid transaction payload")
        if payload.get("action_key") is not None and not isinstance(payload["action_key"], str):
            raise ValueError("invalid transaction action key")
        snapshot = payload.get("snapshot")
        repo.validate_snapshot(snapshot)
        if int(snapshot.get("user_id", 0)) != int(user_id):
            raise ValueError("invalid snapshot owner")
        restored = repo.restore_snapshot(conn, snapshot)
    except (ValueError, TypeError, KeyError):
        conn.execute("ROLLBACK TO undo_restore")
        restored = False
    finally:
        conn.execute("RELEASE undo_restore")
    action_key = payload.get("action_key") if isinstance(locals().get("payload"), dict) else None
    if isinstance(action_key, str) and action_key:
        conn.execute("UPDATE study_actions SET state='undone' WHERE user_id=? AND action_key=?",
                     (user_id, action_key))
    conn.execute("DELETE FROM transaction_log WHERE user_id=? AND id=?", (user_id, tx_id))
    return {"tx_id": tx_id, "status": "restored" if restored else "skipped",
            "participants": snapshot.get("restore_outcomes", {}) if isinstance(locals().get("snapshot"), dict) else {}}


def _advance_undo(conn, user_id, delivery, one_step=False):
    result = delivery["result"]
    ids = result["tx_ids"]
    while result["cursor"] < len(ids):
        outcome = undo_transaction(conn, user_id, ids[result["cursor"]])
        result["outcomes"].append(outcome)
        result["cursor"] += 1
        deliveries.save_processing_result(conn, user_id, delivery["transport_update_id"], result)
        if one_step:
            break
    if result["cursor"] == len(ids):
        # Generic undo closes a live conjugation correction right, never a form
        # result or pass. A replay of a completed undo does not execute this.
        conn.execute("UPDATE conjugation_answers SET correction_session_id=NULL WHERE user_id=?",
                     (user_id,))
        result["restored"] = sum(x["status"] == "restored" for x in result["outcomes"])
        result["invalidate_frame"] = True
        deliveries.commit_delivery(conn, user_id, delivery["transport_update_id"], result)
    return result


def recover_pending_undo(conn, user_id):
    """Finish frozen requests in the caller's transaction before new mutations."""
    rows = conn.execute("""SELECT transport_update_id FROM study_deliveries
        WHERE user_id=? AND action_kind='undo' AND state='processing'
        ORDER BY created_at,transport_update_id""", (user_id,)).fetchall()
    return [_advance_undo(conn, user_id, deliveries.lookup_delivery(conn, user_id, row[0]))
            for row in rows]


def undo_steps(db_path, user_id, count, transport_update_id, flow_id="undo"):
    """Freeze the complete selection, then commit each restore with its cursor."""
    with repo.transaction(db_path) as conn:
        authorize_user(conn, user_id)
        old = deliveries.lookup_delivery(conn, user_id, transport_update_id)
        if old and old["state"] == "committed":
            return {**old["result"], "replayed": True}
        if not old:
            recovered = recover_pending_undo(conn, user_id)
            if recovered:
                result = {"restored": sum(r.get("restored", 0) for r in recovered),
                          "recovered_undo": recovered, "invalidate_frame": True}
                deliveries.begin_delivery(conn, user_id, transport_update_id, flow_id, "undo")
                deliveries.commit_delivery(conn, user_id, transport_update_id, result)
                return result
            ids = [r[0] for r in conn.execute("""SELECT id FROM transaction_log
                   WHERE user_id=? ORDER BY id DESC LIMIT ?""",
                   (user_id, max(1, min(20, int(count))))) ]
            deliveries.begin_delivery(conn, user_id, transport_update_id, flow_id, "undo",
                                      result={"tx_ids": ids, "cursor": 0, "outcomes": []})
    while True:
        with repo.transaction(db_path) as conn:
            delivery = deliveries.lookup_delivery(conn, user_id, transport_update_id)
            if delivery["state"] == "committed":
                return delivery["result"]
            _advance_undo(conn, user_id, delivery, one_step=True)


def resume_pending_undo(db_path, user_id):
    with repo.transaction(db_path) as conn:
        return recover_pending_undo(conn, user_id)


def reject_stale(db_path, user_id, transport_update_id, flow_id):
    """Remember a rejected input without evaluating or resolving its old target."""
    return record_control_transition(db_path, user_id, transport_update_id, flow_id,
        "stale_frame", lambda conn: {"stale": True, "invalidate_frame": True, "flow_id": flow_id})


def apply_manual_progress(db_path, user_id, word_id, mode, preview, action_key,
                          transport_update_id, *, expected_generation=0, flow_id="manual_interval"):
    """One frozen manual interval, one undo unit; never a quiz answer/event."""
    with repo.transaction(db_path) as conn:
        authorize_user(conn, user_id)
        accepted = _accepted(conn, user_id, action_key, transport_update_id, flow_id, "manual_interval")
        if accepted is not None:
            return accepted
        recovered = recover_pending_undo(conn, user_id)
        if recovered:
            return _record_recovery(conn, user_id, transport_update_id, flow_id, "manual_interval", recovered)
        assert_generation(conn, user_id, word_id, expected_generation)
        if not conn.execute("SELECT 1 FROM words WHERE id=?", (word_id,)).fetchone():
            raise StaleStudyAction("manual interval parent no longer exists")
        snapshot = repo.capture_snapshot(conn, user_id, word_id, [mode])
        repo.set_progress(conn, user_id, word_id, mode, preview)
        return _finish(conn, user_id, action_key, transport_update_id, flow_id,
            "manual_interval", word_id, mode, snapshot, dict(preview), 0,
            transaction_kind="add_int:" + mode)
