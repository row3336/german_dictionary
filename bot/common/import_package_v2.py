"""Versioned conn-owned dictionary import. No implicit migration or DB path."""
from contextlib import contextmanager
from copy import deepcopy
import json
import sqlite3
from . import perfekt, book_topic_backfill as topics
from .data18_builder import (SCHEMA, PINNED, RESTRICTED, identity, package_digest, seal,
                             validate_content, required_gaps, validate_card)
from .db_safety import _connection_fingerprint
from .verb_construction import normalize_entry, assert_identity, synonym_compatible
from ..study import repair
from ..study.generations import generation_for

RECEIPT_SCHEMA = "gd-dictionary-import-receipt/v2"
REPAIR_IDS = {"fb358512-a98a-5f0e-a667-644aa6c5bf5e", "f749184e-2226-5175-b761-04a4d99a19ef"}
SCALAR_CORRECTIONS = {"backen", "geboren werden", "hängen", "senden"}

def _schema(conn):
    if conn.row_factory is not sqlite3.Row:
        raise ValueError("import requires sqlite3.Row connection")
    if conn.execute("PRAGMA user_version").fetchone()[0] != 21:
        raise ValueError("exact schema21 required; migrate separately on an approved copy")
    required = {"words": {"id", "type", "reflexive_case", perfekt.FIELD, "conjugation_praesens", "conjugation_praeteritum", "updated_at", "is_separable"}, "study_reset_operations": {"id", "manifest_sha256", "result_json", "applied_at"}, "study_word_generations": {"user_id", "word_id", "generation"}, "vf_add_jobs": {"status"}, "vf_add_items": {"status"}, "topics": {"name", "description", "level"}}
    required.update({
        "study_actions": {"user_id", "receipt_json"}, "study_deliveries": {"user_id", "result_json"},
        "transaction_log": {"user_id", "payload"}, "progress": {"user_id", "word_id"},
        "learning_items": {"user_id", "word_id", "recovery_remaining", "first_due_pending"},
        "conjugation_admissions": {"user_id", "word_id", "study_generation"},
        "conjugation_passes": {"user_id", "word_id", "prompt_snapshot_json"},
        "conjugation_blocks": {"user_id", "word_id", "phase", "cycle", "revision"},
        "conjugation_answers": {"user_id", "pass_id"}, "quiz_events": {"user_id", "word_id", "study_generation"},
        "learning_requests": {"user_id", "word_id", "status", "request_id", "queued_at", "consumed_at", "word_generation", "learning_lifecycle"},
        "user_settings": {"user_id", "key", "value"}, "app_settings": {"key", "value"},
        "word_synonyms": {"word_id", "group_id"}, "synonym_groups": {"id"}})
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ValueError("import requires foreign_keys=ON")
    for table, columns in required.items():
        if columns - {r[1] for r in conn.execute("PRAGMA table_info(" + table + ")")}:
            raise ValueError("incomplete schema21: " + table)

def _word(conn, wid):
    row = conn.execute("SELECT * FROM words WHERE id=?", (wid,)).fetchone()
    return dict(row) if row else None

def _approved(approval):
    return (isinstance(approval, dict) and approval.get("owner") is True and
            approval.get("linguistic") is True and isinstance(approval.get("evidence"), str) and bool(approval["evidence"].strip()))

def _prior(conn, package):
    rows = conn.execute("SELECT id,manifest_sha256,result_json FROM study_reset_operations WHERE id=? OR manifest_sha256=?", (package["operation_id"], package["digest"])).fetchall()
    if not rows:
        return None
    if len(rows) != 1 or rows[0][0] != package["operation_id"] or rows[0][1] != package["digest"]:
        raise ValueError("import receipt id/digest conflict")
    try:
        receipt = json.loads(rows[0][2])
        fields = {"schema", "operation_id", "package_digest", "applied_operations", "counts", "report", "confirmation_digest", "receipt_sha256"}
        if (not isinstance(receipt, dict) or set(receipt) != fields or receipt["schema"] != RECEIPT_SCHEMA or receipt["operation_id"] != package["operation_id"] or receipt["package_digest"] != package["digest"] or type(receipt["applied_operations"]) is not int or receipt["applied_operations"] < 1 or receipt["counts"] != package["expected_counts"] or receipt["receipt_sha256"] != repair.digest({k:v for k,v in receipt.items() if k != "receipt_sha256"}) or not isinstance(receipt["report"], dict)):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise ValueError("invalid import receipt") from None
    return receipt

def _confirmation_specs(conn, package, approval):
    declared = (approval or {}).get("ordinary_confirmations", [])
    if not isinstance(declared, list):
        raise ValueError("explicit ordinary confirmations required")
    by_id = {}
    for item in declared:
        wid = item.get("word_id")
        if wid in by_id or not item.get("evidence") or item.get("expected_before_sha256") != repair.digest(_word(conn, wid)):
            raise ValueError("ordinary confirmation approval/baseline mismatch")
        by_id[wid] = item
    specs = []
    repair_ids = {c["id"] for c in package["cards"] if c.get("existing_id")}
    for item in package["metadata"]:
        wid = item["id"]
        if not item.get("existing_id") or wid in repair_ids:
            continue
        old = _word(conn, wid)
        if not old:
            raise ValueError("metadata target missing")
        if old["reflexive_case"] is None:
            if wid not in by_id or item["variants"][0]["reflexive_case"] != "none":
                raise ValueError("legacy NULL requires explicit per-ID ordinary confirmation")
            candidate = normalize_entry({**old, "reflexive_case": "none"})
            if any(candidate.get(k) != old.get(k) for k in ("german", "infinitive", "conjugation_praesens", "conjugation_praeteritum")):
                raise ValueError("ordinary confirmation would repair construction/forms")
            assert_identity(conn, candidate, exclude_id=wid)
            specs.append(dict(word_id=wid, expected_before_sha256=repair.digest(old), evidence=by_id[wid]["evidence"]))
    if set(by_id) != {x["word_id"] for x in specs}:
        raise ValueError("ordinary confirmation scope differs from actual NULL targets")
    return specs

def _repair_manifest(conn, package, approval):
    cards = {c["id"]: c for c in package["cards"] if c.get("existing_id")}
    manifest = (approval or {}).get("repair_manifest")
    if not cards:
        if manifest is not None:
            raise ValueError("unexpected repair manifest")
        return None
    if not _approved(approval) or not manifest or manifest.get("approvals") != {"owner": True, "linguistic": True} or not (approval or {}).get("repair_evidence"):
        raise ValueError("repair needs explicit owner/linguistic manifest and evidence")
    if set(cards) != REPAIR_IDS or {t["word_id"] for t in manifest["targets"]} != set(cards):
        raise ValueError("only exact two accepted same-ID repairs are allowed")
    checked = repair.preflight(conn, manifest)
    if checked["replayed"]:
        raise ValueError("repair receipt exists without enclosing import receipt")
    for target in manifest["targets"]:
        wid, old = target["word_id"], _word(conn, target["word_id"])
        expected = {k:v for k,v in cards[wid]["fields"].items() if old.get(k) != v}
        if target["replacement"] != expected:
            raise ValueError("repair replacement differs from immutable source intentions")
        actual = {r[0] for table in repair.STATE_TABLES for r in conn.execute("SELECT DISTINCT user_id FROM " + table + " WHERE word_id=?", (wid,))}
        actual.update(repair._durable_users(conn, wid))
        if actual != {u["user_id"] for u in target["users"]}:
            raise ValueError("repair users must enumerate actual state/durable users exactly")
    return deepcopy(manifest)

def _metadata_plans(conn, package, specs):
    confirms = {s["word_id"] for s in specs}
    repair_ids = {c["id"] for c in package["cards"] if c.get("existing_id")}
    plans = []
    for item in package["metadata"]:
        if not item.get("existing_id") or item["id"] in repair_ids:
            continue
        wid, old = item["id"], _word(conn, item["id"])
        if not old:
            raise ValueError("missing existing metadata target")
        if wid in confirms:
            old["reflexive_case"] = "none"
        vs = item["variants"]
        projection = perfekt.scalar_projection(vs)
        changed_scalars = any(old.get(k) != v for k,v in projection.items())
        if changed_scalars and item["lemma"] not in SCALAR_CORRECTIONS:
            raise ValueError("unapproved existing scalar correction: " + item["lemma"])
        patch = {**projection, perfekt.FIELD: dict(version=1,status="verified",variants=vs)}
        proposal = item.get("dictionary_patch_proposal")
        if proposal:
            if any(old.get(k) != v for k,v in proposal["expected_old"].items()):
                raise ValueError("translation proposal expected-old changed")
            patch.update(proposal["proposed"])
        candidate = perfekt.validate_entry(normalize_entry({**old, **patch}, allow_legacy=True))
        if item.get("owner_moechten_preserve_all_existing_columns"):
            if any(old.get(k) != candidate.get(k) for k in old if k != perfekt.FIELD):
                raise ValueError("möchten old columns must all remain unchanged")
        patch = {k:candidate[k] for k in patch if candidate[k] != old.get(k)}
        required = set(patch) | set(perfekt.CONSTRUCTION_FIELDS) | set(perfekt.SCALAR_FIELDS) | {perfekt.FIELD}
        plans.append(dict(word_id=wid, patch=patch, expected_old={k:old[k] for k in sorted(required)},
                          historical_gaps=required_gaps(old)))
    return plans

def _synonym_plans(conn, package):
    pending = {c["id"]: c["fields"] for c in package["cards"]}
    result = []
    for link in package["synonym_links"]:
        ids = link["word_ids"]
        if len(ids) < 2 or len(set(ids)) != len(ids):
            raise ValueError("invalid synonym link")
        groups = sorted({r[0] for wid in ids for r in conn.execute("SELECT group_id FROM word_synonyms WHERE word_id=?", (wid,))})
        members = set(ids)
        for gid in groups:
            members.update(r[0] for r in conn.execute("SELECT word_id FROM word_synonyms WHERE group_id=?", (gid,)))
        entries = [pending.get(wid) or _word(conn,wid) for wid in sorted(members)]
        if any(e is None for e in entries) or any(not synonym_compatible(a,b) for i,a in enumerate(entries) for b in entries[i+1:]):
            raise ValueError("incompatible full synonym group union")
        result.append(dict(members=sorted(members), groups=groups))
    return result

def _plans(conn, package, approval):
    specs = _confirmation_specs(conn, package, approval)
    reset = _repair_manifest(conn, package, approval)
    metadata = _metadata_plans(conn, package, specs)
    topic_plan = topics.preflight(conn, package["topics"])
    moves = {m["word_id"] for m in topic_plan["moves"]}
    if moves & {c["id"] for c in package["cards"]}:
        raise ValueError("repair/new-card and legacy-topic move scopes overlap")
    existing = {r["id"]:dict(r) for r in conn.execute("SELECT * FROM words")}
    keys = {identity(r) for r in existing.values()}
    inserts = []
    for card in package["cards"]:
        if card.get("existing_id"):
            continue
        candidate = perfekt.validate_entry(normalize_entry(card["fields"]))
        if card["id"] in existing or identity(candidate) in keys:
            raise ValueError("new word ID/identity already exists")
        if candidate["type"] == "verb":
            assert_identity(conn, candidate)
        if candidate.get("topic") in package["topics"].get("merge_mapping_exact", {}):
            raise ValueError("future insert uses retired topic key")
        keys.add(identity(candidate))
        inserts.append(dict(id=card["id"], fields=candidate))
    touched = {p["word_id"] for p in metadata} | {m["word_id"] for m in topic_plan["moves"]} | {c["id"] for c in package["cards"] if c.get("existing_id")}
    return dict(fingerprint=_connection_fingerprint(conn), existing_rows={wid:existing[wid] for wid in sorted(touched)}, confirmations=specs, repair_manifest=reset, metadata=metadata, topics=topic_plan, inserts=inserts, synonyms=_synonym_plans(conn, package))

def bind_package(conn, draft, approval=None):
    """Read-only schema21 binding; approval is copied, never inferred or granted."""
    _schema(conn)
    validate_content(draft)
    if draft.get("phase") != "draft" or draft.get("binding") is not None:
        raise ValueError("only unbound draft may be bound")
    package = deepcopy(draft)
    package["phase"], package["approvals"] = "bound", deepcopy(approval)
    package["draft_digest"] = draft["digest"]
    package["binding"] = _plans(conn, package, approval)
    return seal(package)

def preflight(conn, package):
    """Read-only. A receipt always precedes superseded baseline checks."""
    _schema(conn)
    counts = validate_content(package)
    if counts != package["expected_counts"]:
        raise ValueError("package expected counts mismatch")
    if package.get("phase") != "bound" or not isinstance(package.get("binding"), dict):
        raise ValueError("package has no exact schema21 binding")
    prior = _prior(conn, package)
    if prior is not None:
        return dict(replayed=True, operations=0, receipt=prior)
    if package["binding"]["fingerprint"] != _connection_fingerprint(conn):
        raise ValueError("import baseline fingerprint changed")
    plans = _plans(conn, package, package.get("approvals"))
    if plans != package["binding"]:
        raise ValueError("bound operations/expected-old guards changed")
    return dict(replayed=False, plans=plans, counts=counts)

@contextmanager
def _savepoint(conn):
    if not conn.in_transaction:
        raise ValueError("import requires caller-owned transaction")
    conn.execute("SAVEPOINT dictionary_import_v2")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK TO dictionary_import_v2")
        conn.execute("RELEASE dictionary_import_v2")
        raise
    else:
        conn.execute("RELEASE dictionary_import_v2")

def _completeness(conn, package, plans):
    differences = []
    for insert in plans["inserts"]:
        old = _word(conn, insert["id"])
        differences.extend([insert["id"],k] for k,v in insert["fields"].items() if old.get(k) != v)
    for card in package["cards"]:
        current = _word(conn,card["id"])
        expected = perfekt.validate_entry(normalize_entry(card["fields"]))
        differences.extend([card["id"],k] for k,v in expected.items() if current.get(k) != v)
        validate_card({**card, "fields": {**card["fields"], **{k:current[k] for k in card["fields"]}}})
    for metadata in plans["metadata"]:
        current = _word(conn, metadata["word_id"])
        differences.extend([metadata["word_id"],k] for k,v in metadata["patch"].items() if current.get(k) != v)
        # Historical sep/table gaps are baseline facts, never auxiliary backfill work.
        baseline = set(metadata["historical_gaps"])
        if set(required_gaps(current)) - baseline:
            raise ValueError("new historical metadata completeness gap")
    desired = deepcopy(plans["existing_rows"])
    for spec in plans["confirmations"]:
        desired[spec["word_id"]]["reflexive_case"] = "none"
    if plans["repair_manifest"]:
        for target in plans["repair_manifest"]["targets"]:
            desired[target["word_id"]].update(target["replacement"])
    for item in plans["metadata"]:
        desired[item["word_id"]].update(item["patch"])
    for move in plans["topics"]["moves"]:
        desired[move["word_id"]].update(move["new"])
    for wid, expected in desired.items():
        current = _word(conn,wid)
        differences.extend([wid,k] for k,v in expected.items() if k != "updated_at" and current.get(k) != v)
    languages = ("russian", "ukrainian", "english")
    global_missing = dict.fromkeys(languages, 0)
    for row in conn.execute("SELECT russian,ukrainian,english FROM words"):
        for language, value in zip(languages, row):
            if not isinstance(value, str) or not value.strip():
                global_missing[language] += 1
    if any(global_missing.values()):
        raise ValueError("global RU/UK/EN translation completeness failed: " + str(global_missing))
    if differences:
        raise ValueError("postflight exact field differences")
    return dict(imported_updated_exact_field_differences=0, new_cards_required_missing=0,
                metadata_new_missing=0, global_translation_missing=global_missing,
                historical_metadata_gaps={m["word_id"]:m["historical_gaps"] for m in plans["metadata"] if m["historical_gaps"]},
                separability_point20_applied=False)

def apply(conn, package, *, approved_digest, integration_verified=False):
    """No commit. The caller owns BEGIN/COMMIT; failures roll back this savepoint."""
    if approved_digest != package.get("digest") or not _approved(package.get("approvals")):
        raise PermissionError("explicit owner/linguistic evidence and approved package digest required")
    if package.get("binding", {}).get("repair_manifest") and integration_verified is not True:
        raise PermissionError("repair requires separately verified integration QA")
    with _savepoint(conn):
        checked = preflight(conn, package)
        if checked["replayed"]:
            return dict(replayed=True, operations=0, receipt=checked["receipt"])
        plans, operations, confirmation_digest = checked["plans"], 0, None
        if plans["repair_manifest"]:
            repair.apply_reset_conn(conn, plans["repair_manifest"], integration_verified=integration_verified)
            operations += len(plans["repair_manifest"]["targets"])
        if plans["confirmations"]:
            confirmation = dict(schema="gd-verb-confirmation/v1", operation_id=package["operation_id"] + ":ordinary", approvals={k:package["approvals"][k] for k in ("owner", "linguistic")}, baseline_fingerprint=_connection_fingerprint(conn), parent_package_digest=package["digest"], entries=deepcopy(plans["confirmations"]))
            confirmation["digest"] = repair.manifest_digest(confirmation)
            confirmation_digest = confirmation["digest"]
            repair.confirm_legacy_conn(conn, confirmation)
            operations += len(confirmation["entries"])
        for item in plans["metadata"]:
            if item["patch"]:
                operations += int(perfekt.apply_metadata_patch(conn,item["word_id"],item["patch"],expected_old=item["expected_old"]))
        # Registry creation/retirement follows exact old-word/filter transforms.
        operations += topics.apply_plan(conn, plans["topics"])
        for item in plans["inserts"]:
            fields = {**item["fields"], "id":item["id"]}
            allowed = {r[1] for r in conn.execute("PRAGMA table_info(words)")}
            if set(fields) - allowed:
                raise ValueError("unsupported insert field")
            keys = sorted(fields)
            conn.execute("INSERT INTO words(" + ",".join(keys) + ") VALUES(" + ",".join("?" for _ in keys) + ")", [fields[k] for k in keys])
            operations += 1
        for link in plans["synonyms"]:
            gid = min(link["groups"]) if link["groups"] else conn.execute("INSERT INTO synonym_groups DEFAULT VALUES").lastrowid
            for wid in link["members"]:
                conn.execute("INSERT INTO word_synonyms(word_id,group_id) VALUES(?,?) ON CONFLICT(word_id) DO UPDATE SET group_id=excluded.group_id", (wid,gid))
            operations += 1
        report = _completeness(conn,package,plans)
        receipt = dict(schema=RECEIPT_SCHEMA, operation_id=package["operation_id"], package_digest=package["digest"], applied_operations=operations, counts=checked["counts"], report=report, confirmation_digest=confirmation_digest)
        receipt["receipt_sha256"] = repair.digest(receipt)
        conn.execute("INSERT INTO study_reset_operations(id,manifest_sha256,applied_at,result_json) VALUES(?,?,CURRENT_TIMESTAMP,?)", (package["operation_id"],package["digest"],json.dumps(receipt,ensure_ascii=False,sort_keys=True)))
        return dict(replayed=False,operations=operations,receipt=receipt)
