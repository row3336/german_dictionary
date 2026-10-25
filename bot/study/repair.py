"""Manifest-only targeted relearning. No CLI and no implicit apply permission."""

import hashlib
import json
from contextlib import contextmanager

from ..common.db_safety import _connection_fingerprint
from ..common.verb_construction import assert_identity, identity, normalize_entry
from ..common.perfekt import validate_entry
from .generations import advance_generation, assert_generation
from .repository import transaction, utc_iso

STATE_TABLES = ("progress", "learning_items", "quiz_events", "conjugation_admissions",
                "conjugation_blocks", "conjugation_passes", "study_word_generations")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def manifest_digest(manifest):
    return digest({k: v for k, v in manifest.items() if k != "digest"})


def state_rows(conn, user_id, word_id):
    result = {table: [dict(r) for r in conn.execute(
        f"SELECT * FROM {table} WHERE user_id=? AND word_id=? ORDER BY rowid", (user_id, word_id))]
        for table in STATE_TABLES}
    result["conjugation_answers"] = [dict(r) for r in conn.execute("""SELECT * FROM conjugation_answers
        WHERE user_id=? AND pass_id IN (SELECT id FROM conjugation_passes WHERE user_id=? AND word_id=?) ORDER BY id""", (user_id, user_id, word_id))]
    return result


def _references_word(value, word_id):
    if isinstance(value, list):
        return any(_references_word(x, word_id) for x in value)
    if not isinstance(value, dict):
        return False
    generations = value.get("generations")
    return (value.get("word_id") == word_id or
            isinstance(generations, dict) and word_id in generations or
            any(_references_word(x, word_id) for x in value.values()))


def _durable_users(conn, word_id):
    """Include composite/legacy undo and delayed frames without progress."""
    users = set()
    for table, column in (("study_actions", "receipt_json"), ("transaction_log", "payload"),
                          ("study_deliveries", "result_json")):
        for uid, raw in conn.execute(f"SELECT user_id,{column} FROM {table}"):
            try:
                value = json.loads(raw)
            except (ValueError, TypeError):
                raise ValueError("invalid durable study JSON requires review") from None
            if _references_word(value, word_id):
                users.add(uid)
    return users


def _prior(conn, manifest):
    rows = conn.execute("SELECT id,manifest_sha256,result_json FROM study_reset_operations WHERE id=? OR manifest_sha256=?",
                        (manifest["operation_id"], manifest["digest"])).fetchall()
    if not rows:
        return None
    if len(rows) != 1 or rows[0][0] != manifest["operation_id"] or rows[0][1] != manifest["digest"]:
        raise ValueError("reset operation id/digest conflict")
    return json.loads(rows[0][2])


def preflight(conn, manifest):
    """Read-only checks. A replay is checked before the superseded baseline."""
    if not isinstance(manifest, dict) or manifest.get("schema") != "gd-study-repair/v1":
        raise ValueError("unsupported reset manifest schema")
    if not manifest.get("operation_id") or manifest.get("digest") != manifest_digest(manifest):
        raise ValueError("invalid reset manifest digest/id")
    prior = _prior(conn, manifest)
    if prior is not None:
        return {"replayed": True, "result": prior}
    if manifest.get("reset_profile") != "relearn_word" or "modes" in manifest:
        raise ValueError("only full relearn_word profile is supported")
    if manifest.get("baseline_fingerprint") != _connection_fingerprint(conn):
        raise ValueError("reset baseline fingerprint changed")
    targets = manifest.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError("empty reset manifest")
    seen, plans = set(), []
    for target in targets:
        wid = target["word_id"]
        if wid in seen:
            raise ValueError("duplicate reset target")
        seen.add(wid)
        row = conn.execute("SELECT * FROM words WHERE id=?", (wid,)).fetchone()
        if row is None or target.get("expected_before_sha256") != digest(dict(row)):
            raise ValueError("dictionary baseline changed")
        old = dict(row)
        if target.get("old_identity") != list(identity(old)):
            raise ValueError("old identity mismatch")
        fields = target.get("replacement")
        if not isinstance(fields, dict) or not fields or set(fields) - (set(old) - {"id", "created_at", "updated_at"}):
            raise ValueError("invalid replacement fields")
        candidate = validate_entry(normalize_entry({**old, **fields}))
        if target.get("new_identity") != list(identity(candidate)):
            raise ValueError("new identity mismatch")
        assert_identity(conn, candidate, exclude_id=wid)
        users = target.get("users")
        if not isinstance(users, list) or not users:
            raise ValueError("reset must explicitly enumerate users")
        declared = {u["user_id"] for u in users}
        if len(declared) != len(users) or any(type(u) is not int for u in declared):
            raise ValueError("duplicate/invalid reset users")
        actual = {r[0] for table in STATE_TABLES for r in conn.execute(
            f"SELECT DISTINCT user_id FROM {table} WHERE word_id=?", (wid,))}
        actual.update(_durable_users(conn, wid))
        if actual - declared:
            raise ValueError("manifest omits shared-word study users")
        audits = []
        for user in users:
            if "modes" in user or "reset_profile" in user:
                raise ValueError("partial reset is unsupported")
            uid = user["user_id"]
            assert_generation(conn, uid, wid, user.get("expected_generation"))
            rows = state_rows(conn, uid, wid)
            if user.get("expected_rows_sha256") != digest(rows):
                raise ValueError("target study rows changed")
            audits.append({"user_id": uid, "generation": user["expected_generation"], "before": rows})
        plans.append({"word_id": wid, "before": old, "candidate": candidate,
                      "fields": fields, "users": audits})
    keys = [identity(p["candidate"]) for p in plans]
    if len(set(keys)) != len(keys):
        raise ValueError("replacement identities collide")
    return {"replayed": False, "plans": plans}


@contextmanager
def _caller_transaction(conn, name):
    if not conn.in_transaction:
        raise ValueError("repair requires caller transaction")
    conn.execute("SAVEPOINT " + name)
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK TO " + name)
        conn.execute("RELEASE " + name)
        raise
    else:
        conn.execute("RELEASE " + name)


def apply_reset_conn(conn, manifest, *, integration_verified=False):
    """Apply only after explicit integration QA and approved linguistic manifest.

    Callers must supply the independently verified gate; the default remains
    closed until live-frame adapters are integrated and independently tested.
    """
    if integration_verified is not True or manifest.get("approvals") != {"owner": True, "linguistic": True}:
        raise PermissionError("reset requires integration QA and approved manifest")
    with _caller_transaction(conn, "approved_repair_reset"):
        checked = preflight(conn, manifest)
        if checked["replayed"]:
            return {**checked["result"], "replayed": True}
        result = {"operation_id": manifest["operation_id"], "replayed": False, "targets": []}
        for plan in checked["plans"]:
            wid = plan["word_id"]
            for audit in plan["users"]:
                uid = audit["user_id"]
                audit["new_generation"] = advance_generation(conn, uid, wid, audit["generation"])
                batches = [r[0] for r in conn.execute("SELECT batch_id FROM learning_items WHERE user_id=? AND word_id=?", (uid, wid))]
                conn.execute("DELETE FROM progress WHERE user_id=? AND word_id=?", (uid, wid))
                conn.execute("DELETE FROM learning_items WHERE user_id=? AND word_id=?", (uid, wid))
                for bid in batches:
                    conn.execute("""UPDATE learning_batches SET ready_since=NULL,
                        status=CASE WHEN EXISTS(SELECT 1 FROM learning_items WHERE user_id=? AND batch_id=? AND status='active')
                        THEN status ELSE 'completed' END WHERE user_id=? AND id=?""", (uid, bid, uid, bid))
                conn.execute("""UPDATE conjugation_blocks SET phase='preparation',cycle=cycle+1,
                    revision=revision+1,interval=1,repetitions=0,ease=2.5,due=NULL,prepared_at=NULL
                    WHERE user_id=? AND word_id=?""", (uid, wid))
                conn.execute("""UPDATE conjugation_passes SET status='reset',revision=revision+1
                    WHERE user_id=? AND word_id=? AND status='active'""", (uid, wid))
                conn.execute("""UPDATE conjugation_answers SET correction_session_id=NULL WHERE user_id=?
                    AND pass_id IN (SELECT id FROM conjugation_passes WHERE user_id=? AND word_id=?)""", (uid, uid, wid))
            keys = sorted(set(plan["fields"]) | {"reflexive_case", "infinitive", "german", "conjugation_praesens", "conjugation_praeteritum"})
            conn.execute("UPDATE words SET " + ",".join(k + "=?" for k in keys) + ",updated_at=? WHERE id=?",
                         [plan["candidate"][k] for k in keys] + [utc_iso(), wid])
            result["targets"].append({"word_id": wid, "dictionary_before": plan["before"], "users": plan["users"]})
        conn.execute("INSERT INTO study_reset_operations(id,manifest_sha256,applied_at,result_json) VALUES (?,?,?,?)",
                     (manifest["operation_id"], manifest["digest"], utc_iso(), json.dumps(result, ensure_ascii=False)))
        return result


def apply_reset(db_path, manifest, *, integration_verified=False):
    """Backward-compatible path wrapper; the connection seam owns no commit."""
    with transaction(db_path) as conn:
        return apply_reset_conn(conn, manifest, integration_verified=integration_verified)


def confirm_legacy_conn(conn, manifest):
    """Confirm unchanged ordinary construction by verified manifest, without reset."""
    if (manifest.get("schema") != "gd-verb-confirmation/v1" or
            manifest.get("approvals") != {"owner": True, "linguistic": True} or
            not manifest.get("operation_id") or manifest.get("digest") != manifest_digest(manifest)):
        raise ValueError("verified legacy confirmation manifest required")
    with _caller_transaction(conn, "approved_legacy_confirmation"):
        prior = _prior(conn, manifest)
        if prior is not None:
            return {**prior, "replayed": True}
        if manifest.get("baseline_fingerprint") != _connection_fingerprint(conn):
            raise ValueError("confirmation baseline changed")
        entries = manifest.get("entries")
        if not isinstance(entries, list) or not entries:
            raise ValueError("empty confirmation manifest")
        seen = set()
        for item in entries:
            wid = item["word_id"]
            if wid in seen:
                raise ValueError("duplicate confirmation target")
            seen.add(wid)
            row = conn.execute("SELECT * FROM words WHERE id=?", (wid,)).fetchone()
            if row is None or digest(dict(row)) != item.get("expected_before_sha256"):
                raise ValueError("confirmation dictionary baseline changed")
            old = dict(row)
            if old["type"] != "verb" or old["reflexive_case"] is not None:
                raise ValueError("confirmation requires legacy verb")
            candidate = validate_entry(normalize_entry({**old, "reflexive_case": "none"}))
            if any(candidate.get(key) != old.get(key) for key in ("german", "infinitive", "conjugation_praesens", "conjugation_praeteritum")):
                raise ValueError("metadata confirmation cannot repair construction or forms")
            assert_identity(conn, candidate, exclude_id=wid)
        for wid in seen:
            conn.execute("UPDATE words SET reflexive_case='none',updated_at=? WHERE id=?", (utc_iso(), wid))
        result = {"operation_id": manifest["operation_id"], "confirmed": sorted(seen), "replayed": False}
        conn.execute("INSERT INTO study_reset_operations(id,manifest_sha256,applied_at,result_json) VALUES (?,?,?,?)",
                     (manifest["operation_id"], manifest["digest"], utc_iso(), json.dumps(result)))
        return result


def confirm_legacy(db_path, manifest):
    """Backward-compatible path wrapper for explicit per-ID confirmation."""
    with transaction(db_path) as conn:
        return confirm_legacy_conn(conn, manifest)
