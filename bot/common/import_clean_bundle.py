"""Atomic consumer of the approved clean preparation bundle (scalar hilfsverb).

The preparation file stays untouched and is only a source: bind() derives an
explicit runtime envelope for one schema22 database, preflight() re-derives it
read-only, apply() writes it inside the caller's transaction (SAVEPOINT, no
commit). The 67 deferred cards are never imported. Existing rows are guarded by
their schema17 row hash plus exact expected-old fields; the two same-ID repairs
need a live gd-study-repair/v1 manifest with real generations and users.
"""

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from . import adjective_forms, book_topic_backfill as topics, hilfsverb
from .data18_builder import identity, stable_id
from .db_safety import SCHEMA_VERSION, _connection_fingerprint
from .verb_construction import assert_identity, identity as verb_identity, normalize_entry, synonym_compatible
from ..study import repair
from ..study.generations import generation_for

SOURCE_SCHEMA = "gd888.clean_preparation_bundle.v1"
BOUND_SCHEMA = "gd888.clean-import-bound/v1"
RECEIPT_SCHEMA = "gd888.clean-import-receipt/v1"
# Exact bytes of the approved 27.10 preparation (manifest.json pin).
APPROVED_SOURCE_SHA256 = "e5df02d62083cadf959e764ee75f73cce2fc738c3c96326221b692abb165b1f5"
ORDINARY_EVIDENCE_SOURCE_SHA256 = "9c7a865066e1ef554e2c515a80972cd6ec296f616369420d09d143fb1a0c360a"
SOURCE_KEYS = {"schema", "date", "preparation_complete", "apply_authorized",
               "runtime_apply_compatible", "target_snapshot", "words_insert", "words_repair",
               "existing_word_updates", "adjective_degree_variants", "topics", "synonym_links",
               "deferred_manual", "preserved", "counts", "stage", "execution_gates"}
UPDATE_FIELDS = frozenset({"example", "hilfsverb", "topic", "partizip2", "prateritum"})
# Columns added after the schema17 snapshot the row hashes were taken from.
POST_V17_COLUMNS = ("reflexive_case", "hilfsverb")
EMPTY_TEXT_COLUMNS = ("plural", "komparativ", "superlativ")
TYPES = {"verb", "nomen", "adjektiv", "adverb", "pronomen", "praeposition", "phrase",
         "konjunktion", "partikel"}
CEFR = ("", "A1", "A2", "B1", "B2", "C1", "C2")
LANGUAGES = ("russian", "ukrainian", "english")
digest = repair.digest


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def _reject_constant(_):
    raise ValueError("nonfinite JSON")


def load_source(path):
    """Read exact bytes once: (bundle, sha256). The file is never written."""
    raw = Path(path).read_bytes()
    bundle = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique,
                        parse_constant=_reject_constant)
    return bundle, hashlib.sha256(raw).hexdigest()


def _project(fields):
    result = deepcopy(fields)
    for column in EMPTY_TEXT_COLUMNS:
        if column in result and result[column] is None:
            result[column] = ""
    return result


def _validate_card(fields, label):
    if not isinstance(fields, dict) or hilfsverb.LEGACY_FIELD in fields:
        raise ValueError(label + ": invalid fields or retired Perfekt JSON")
    kind = fields.get("type")
    if kind not in TYPES or fields.get("cefr_level", "") not in CEFR:
        raise ValueError(label + ": unsupported type/CEFR")
    if any(not isinstance(fields.get(k), str) or not fields[k].strip() for k in LANGUAGES):
        raise ValueError(label + ": incomplete translation")
    if kind == "verb":
        if (fields.get(hilfsverb.FIELD) not in hilfsverb.VALUES
                or not fields.get("infinitive") or not fields.get("partizip2")
                or type(fields.get("is_separable")) is not int or fields["is_separable"] not in (0, 1)):
            raise ValueError(label + ": incomplete verb/hilfsverb")
    elif fields.get(hilfsverb.FIELD) is not None:
        raise ValueError(label + ": hilfsverb requires verb")
    try:
        normalized = hilfsverb.validate_entry(normalize_entry(fields))
    except ValueError as exc:
        raise ValueError(label + ": " + str(exc)) from exc
    if any(normalized.get(k) != v for k, v in fields.items()):
        raise ValueError(label + ": fields are not normalized")
    return normalized


def _ids(items, label):
    ids = [item.get("id") for item in items]
    if any(not isinstance(x, str) or not x for x in ids) or len(set(ids)) != len(ids):
        raise ValueError(label + ": missing/duplicate IDs")
    return set(ids)


def _counts(bundle):
    inserts, repairs = bundle["words_insert"], bundle["words_repair"]
    updates = bundle["existing_word_updates"]
    by_field, by_type, aux = {}, {}, {}
    for update in updates:
        for key in update["set"]:
            by_field[key] = by_field.get(key, 0) + 1
    for card in inserts:
        kind = card["fields"]["type"]
        by_type[kind] = by_type.get(kind, 0) + 1
    values = ([c["fields"].get(hilfsverb.FIELD) for c in inserts + repairs]
              + [u["set"].get(hilfsverb.FIELD) for u in updates])
    for value in values:
        if value is not None:
            aux[value] = aux.get(value, 0) + 1
    preserved = bundle["preserved"]
    return dict(inserts=len(inserts), repairs=len(repairs), existing_word_updates=len(updates),
                updates_by_field=by_field, inserted_by_type=by_type, auxiliary_counts=aux,
                verbs_with_auxiliary=sum(aux.values()),
                pons_active=sum(c["fields"].get("topic") == "PONS" for c in inserts),
                pons_deferred=len(bundle["deferred_manual"]["pons_cards"]),
                protected436=len(preserved["protected_example_ids"]),
                prepositions_preserved=len(preserved["existing_preposition_example_ids"]),
                target_words_after_approved_apply=bundle["target_snapshot"]["words"] + len(inserts))


def validate_source(bundle, source_sha256):
    """Pure: refuse anything but the exact approved, internally consistent bundle."""
    if source_sha256 != APPROVED_SOURCE_SHA256:
        raise ValueError("unapproved or modified preparation bundle")
    if not isinstance(bundle, dict) or set(bundle) != SOURCE_KEYS or bundle["schema"] != SOURCE_SCHEMA:
        raise ValueError("unsupported preparation envelope")
    # The file prepares data only; apply authority is a separate approval.
    if (bundle["preparation_complete"] is not True or bundle["apply_authorized"] is not False
            or bundle["runtime_apply_compatible"] is not False):
        raise ValueError("preparation flags changed")
    snapshot = bundle["target_snapshot"]
    if snapshot.get("schema_version") != 17 or type(snapshot.get("words")) is not int:
        raise ValueError("unexpected source snapshot contract")
    inserts, repairs = bundle["words_insert"], bundle["words_repair"]
    updates, deferred = bundle["existing_word_updates"], bundle["deferred_manual"]
    groups = [_ids(inserts, "insert"), _ids(repairs, "repair"), _ids(updates, "update"),
              _ids(deferred["pons_cards"], "deferred")]
    if sum(map(len, groups)) != len(set().union(*groups)):
        raise ValueError("insert/repair/update/deferred scopes overlap")
    if deferred["apply"] is not False or deferred["prepositions"] or any(
            c.get("apply") is not False or c.get("status") != "manual_review_required"
            for c in deferred["pons_cards"]):
        raise ValueError("deferred cards must stay excluded")
    preserved = bundle["preserved"]
    protected = set(preserved["protected_example_ids"]) | set(preserved["existing_preposition_example_ids"])
    mapping = bundle["topics"]["exact_legacy_merge"]
    if not mapping or any(k == v or v in mapping for k, v in mapping.items()):
        raise ValueError("invalid exact topic merge")
    keys = set()
    for card in inserts:
        if set(card) != {"id", "source_key", "fields"}:
            raise ValueError("unsupported insert record")
        fields = _project(card["fields"])
        _validate_card(fields, "insert " + card["id"])
        if card["id"] != stable_id(fields) or fields.get("topic") in mapping:
            raise ValueError("insert identity/topic mismatch: " + card["id"])
        keys.add(identity(fields))
    if len(keys) != len(inserts):
        raise ValueError("duplicate insert identity")
    for card in repairs:
        if (set(card) != {"id", "source_key", "fields", "expected_before", "expected_before_row_sha256",
                          "reset_profile", "preserve_id", "preserve_completed_history",
                          "requires_fresh_live_repair_generation_manifest"}
                or card["reset_profile"] != "relearn_word" or card["preserve_id"] is not True
                or card["requires_fresh_live_repair_generation_manifest"] is not True
                or not isinstance(card["expected_before"], dict)):
            raise ValueError("unsupported repair record")
        fields = _project(card["fields"])
        _validate_card(fields, "repair " + card["id"])
        if identity(fields) in keys:
            raise ValueError("repair identity collides with insert")
        keys.add(identity(fields))
    for update in updates:
        changes, before = update.get("set"), update.get("expected_before")
        if (set(update) != {"id", "expected_before_row_sha256", "expected_before", "set", "reasons"}
                or not isinstance(changes, dict) or not changes or set(changes) - UPDATE_FIELDS
                or not isinstance(before, dict) or set(before) != set(changes)
                or not update["reasons"]):
            raise ValueError("unsupported update record: " + str(update.get("id")))
        if "example" in changes and update["id"] in protected:
            raise ValueError("protected example would change: " + update["id"])
        if hilfsverb.FIELD in changes:
            if before[hilfsverb.FIELD] is not None or hilfsverb.validate_value(changes[hilfsverb.FIELD]) is None:
                raise ValueError("hilfsverb backfill must replace unknown: " + update["id"])
        if "topic" in changes and mapping.get(before["topic"]) != changes["topic"]:
            raise ValueError("topic change outside exact merge: " + update["id"])
        if any(not isinstance(v, str) for k, v in changes.items() if k != hilfsverb.FIELD):
            raise ValueError("invalid update value: " + update["id"])
    insert_types = {c["id"]: c["fields"]["type"] for c in inserts}
    pairs = set()
    for pair in bundle["adjective_degree_variants"]:
        key = (pair.get("word_id"), pair.get("comparative"), pair.get("superlative"))
        if (set(pair) != {"word_id", "comparative", "superlative"} or insert_types.get(key[0]) != "adjektiv"
                or not all(isinstance(x, str) for x in key[1:]) or not (key[1] or key[2]) or key in pairs):
            raise ValueError("invalid adjective degree pair")
        pairs.add(key)
    for link in bundle["synonym_links"]:
        ids = link.get("word_ids")
        if not isinstance(ids, list) or len(ids) < 2 or len(set(ids)) != len(ids):
            raise ValueError("invalid synonym link")
    for op in bundle["topics"]["registry_operations"]:
        if op.get("operation") == "retire_exact_registry_row":
            if not op.get("expected_old") or op["expected_old"].get("name") not in mapping:
                raise ValueError("retire operation outside exact merge")
        elif op.get("operation") != "insert_if_absent_or_exact_match" or set(op.get("new") or {}) != {"name", "description", "level"}:
            raise ValueError("unsupported registry operation")
    if bundle["topics"]["book_display"].get("product_changes_applied") is not False:
        raise ValueError("book display labels are a separate UI task")
    computed = _counts(bundle)
    declared = bundle["counts"]
    if any(declared.get(k) != v for k, v in computed.items()):
        raise ValueError("declared counts disagree with bundle content")
    return computed


def _schema(conn):
    if conn.row_factory is not sqlite3.Row:
        raise ValueError("import requires sqlite3.Row connection")
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        raise ValueError("exact schema%d required; migrate separately first" % SCHEMA_VERSION)
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ValueError("import requires foreign_keys=ON")
    required = {
        "words": {"id", "type", "reflexive_case", hilfsverb.FIELD, "example", "image", "topic",
                  "updated_at", "is_separable"},
        "study_reset_operations": {"id", "manifest_sha256", "result_json", "applied_at"},
        "study_word_generations": {"user_id", "word_id", "generation"},
        "vf_add_jobs": {"status"}, "vf_add_items": {"status"}, "topics": {"name", "description", "level"},
        "user_settings": {"user_id", "key", "value"}, "app_settings": {"key", "value"},
        "word_synonyms": {"word_id", "group_id"}, "synonym_groups": {"id"},
        "study_actions": {"user_id", "receipt_json"}, "study_deliveries": {"user_id", "result_json"},
        "transaction_log": {"user_id", "payload"}}
    for table, columns in required.items():
        if columns - {r[1] for r in conn.execute("PRAGMA table_info(" + table + ")")}:
            raise ValueError("incomplete schema%d: %s" % (SCHEMA_VERSION, table))
    if hilfsverb.LEGACY_FIELD in {r[1] for r in conn.execute("PRAGMA table_info(words)")}:
        raise ValueError("retired perfekt_variants_json column still present")
    # Read-only verification of the canonical DDL, not a destructive probe.
    from ..study.schema_v22 import COLUMN_SQL
    compact = lambda sql: re.sub(r"\s+", "", sql or "")
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='words'").fetchone()[0]
    if compact(hilfsverb.FIELD + " " + COLUMN_SQL) not in compact(sql):
        raise ValueError("schema22 hilfsverb CHECK missing or changed")
    for action in ("INSERT", "UPDATE"):
        name = "words_hilfsverb_type_" + action.lower()
        trigger = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=? AND tbl_name='words'", (name,)).fetchone()
        expected = (f"CREATE TRIGGER {name} BEFORE {action} ON words "
                    "WHEN NEW.hilfsverb IS NOT NULL AND NEW.type!='verb' "
                    "BEGIN SELECT RAISE(ABORT,'hilfsverb requires verb'); END")
        if not trigger or compact(trigger[0]) != compact(expected):
            raise ValueError("schema22 hilfsverb trigger missing or changed: " + name)


def _word(conn, wid):
    row = conn.execute("SELECT * FROM words WHERE id=?", (wid,)).fetchone()
    return dict(row) if row else None


def snapshot_row_sha256(row):
    """Hash of the row as it looked at schema17 (later columns excluded)."""
    return digest({k: v for k, v in row.items() if k not in POST_V17_COLUMNS})


def _guarded_row(conn, item, label, confirmation=None):
    row = _word(conn, item["id"])
    if row is None:
        raise ValueError(label + " target missing: " + item["id"])
    if (snapshot_row_sha256(row) != item["expected_before_row_sha256"]
            or row.get(hilfsverb.FIELD) is not None
            or (row.get("reflexive_case") is not None and not (
                confirmation and row["reflexive_case"] == "none"
                and confirmation["expected_before_sha256"] == digest(row)))
            or any(k not in row or row[k] != v for k, v in item["expected_before"].items())):
        raise ValueError(label + " baseline changed: " + item["id"])
    return row


def _confirmation_specs(conn, bundle, approval):
    """Require explicit pinned evidence and the complete current row per aux target."""
    declared = approval.get("ordinary_confirmations", [])
    if not isinstance(declared, list):
        raise ValueError("explicit ordinary confirmations required")
    specs = {}
    for spec in declared:
        if not isinstance(spec, dict):
            raise ValueError("invalid ordinary confirmation")
        wid = spec.get("word_id")
        row = _word(conn, wid)
        if (not isinstance(wid, str) or wid in specs or not row
                or spec.get("reflexive_case") != "none"
                or row.get("type") != "verb" or row.get("reflexive_case") not in (None, "none")
                or spec.get("expected_before_sha256") != digest(row)
                or spec.get("source_sha256") != ORDINARY_EVIDENCE_SOURCE_SHA256
                or not re.fullmatch(r"metadata/\d+", str(spec.get("source_pointer", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(spec.get("source_record_sha256", "")))
                or not isinstance(spec.get("evidence"), str) or not spec["evidence"].strip()):
            raise ValueError("ordinary confirmation evidence/current-row guard mismatch")
        candidate = normalize_entry({**row, "reflexive_case": "none"})
        if any(candidate.get(k) != row.get(k) for k in
               ("german", "infinitive", "conjugation_praesens", "conjugation_praeteritum")):
            raise ValueError("ordinary confirmation would repair construction/forms")
        assert_identity(conn, candidate, exclude_id=wid)
        specs[wid] = deepcopy(spec)
    required = {u["id"] for u in bundle["existing_word_updates"] if hilfsverb.FIELD in u["set"]}
    if set(specs) != required:
        raise ValueError("explicit per-ID ordinary confirmation required for every auxiliary update")
    return specs


def _approved(approval, source_sha256):
    return (isinstance(approval, dict) and approval.get("owner") is True
            and approval.get("linguistic") is True and isinstance(approval.get("evidence"), str)
            and bool(approval["evidence"].strip()) and approval.get("source_sha256") == source_sha256)


def _check_target(conn, approval):
    if "target_path" not in approval:
        return
    opened = [Path(r[2]).resolve() for r in conn.execute("PRAGMA database_list") if r[1] == "main" and r[2]]
    if opened != [Path(approval["target_path"]).resolve()]:
        raise PermissionError("approval is bound to another database file")


def _repair_targets(conn, bundle):
    """Replacement intentions per repair; current rows must still match."""
    result = {}
    for card in bundle["words_repair"]:
        row = _guarded_row(conn, card, "repair")
        fields = _project(card["fields"])
        replacement = {k: v for k, v in fields.items() if row.get(k) != v}
        candidate = hilfsverb.validate_entry(normalize_entry({**row, **replacement}))
        result[card["id"]] = dict(row=row, replacement=replacement, candidate=candidate)
    return result


def _users(conn, wid):
    users = {r[0] for table in repair.STATE_TABLES
             for r in conn.execute("SELECT DISTINCT user_id FROM " + table + " WHERE word_id=?", (wid,))}
    users.update(repair._durable_users(conn, wid))
    return users


def draft_repair_manifest(conn, bundle, source_sha256):
    """Read-only skeleton from live generations/rows. approvals stay None:
    an owner must set them explicitly and reseal the digest before bind."""
    validate_source(bundle, source_sha256)
    targets = []
    for wid, item in sorted(_repair_targets(conn, bundle).items()):
        users = [dict(user_id=uid, expected_generation=generation_for(conn, uid, wid),
                      expected_rows_sha256=digest(repair.state_rows(conn, uid, wid)))
                 for uid in sorted(_users(conn, wid))]
        targets.append(dict(word_id=wid, old_identity=list(verb_identity(item["row"])),
                            new_identity=list(verb_identity(item["candidate"])),
                            replacement=item["replacement"], expected_before_sha256=digest(item["row"]),
                            users=users))
    manifest = dict(schema="gd-study-repair/v1", operation_id="gd888-clean-repair:" + source_sha256[:16],
                    reset_profile="relearn_word", approvals=None,
                    baseline_fingerprint=_connection_fingerprint(conn), targets=targets)
    manifest["digest"] = repair.manifest_digest(manifest)
    return manifest


def _repair_plan(conn, bundle, approval):
    manifest = approval.get("repair_manifest")
    if not bundle["words_repair"]:
        if manifest is not None:
            raise ValueError("unexpected repair manifest")
        return None
    if (not isinstance(manifest, dict) or manifest.get("approvals") != {"owner": True, "linguistic": True}
            or not approval.get("repair_evidence")):
        raise ValueError("same-ID repairs need an explicitly approved live repair manifest")
    intentions = _repair_targets(conn, bundle)
    if sorted(t.get("word_id") for t in manifest.get("targets", [])) != sorted(intentions):
        raise ValueError("repair manifest must cover exactly the bundle repairs")
    for target in manifest["targets"]:
        wid = target["word_id"]
        if target.get("replacement") != intentions[wid]["replacement"]:
            raise ValueError("repair replacement differs from the bundle: " + wid)
        if {u.get("user_id") for u in target.get("users", [])} != _users(conn, wid):
            raise ValueError("repair users must enumerate actual state/durable users exactly")
    if repair.preflight(conn, manifest)["replayed"]:
        raise ValueError("repair receipt exists without enclosing import receipt")
    return deepcopy(manifest)


def _topic_manifest(bundle):
    data = bundle["topics"]
    moves = [dict(word_id=u["id"], expected_old={"topic": u["expected_before"]["topic"]},
                  new={"topic": u["set"]["topic"]})
             for u in bundle["existing_word_updates"] if "topic" in u["set"]]
    registry = []
    for op in data["registry_operations"]:
        op = deepcopy(op)
        if op["operation"] == "insert_if_absent_or_exact_match":
            op.setdefault("key", op["new"]["name"])
            if op["key"] != op["new"]["name"]:
                raise ValueError("registry key/name mismatch")
        registry.append(op)
    return dict(merge_mapping_exact=dict(data["exact_legacy_merge"]),
                word_topic_operations=moves, topic_registry_operations=registry)


def _patch_plans(conn, bundle, confirmations):
    plans = []
    for update in bundle["existing_word_updates"]:
        row = _guarded_row(conn, update, "update", confirmations.get(update["id"]))
        final = {**row, **update["set"]}
        confirmed = update["id"] in confirmations
        if confirmed:
            final["reflexive_case"] = "none"
        try:
            hilfsverb.validate_entry(normalize_entry(final, allow_legacy=True))
        except ValueError as exc:
            raise ValueError("invalid final card " + update["id"] + ": " + str(exc)) from exc
        patch = {k: v for k, v in update["set"].items() if k != "topic"}
        guard = set(patch) | set(hilfsverb.CONSTRUCTION_FIELDS)
        changes = {**update["set"], **({"reflexive_case": "none"} if confirmed else {})}
        expected = {k: row[k] for k in sorted(guard)}
        if confirmed:
            expected["reflexive_case"] = "none"
        plans.append(dict(word_id=update["id"], patch=patch, final=changes,
                          expected_old=expected, baseline=row))
    return plans


def _insert_plans(conn, bundle, repairs):
    existing = [dict(r) for r in conn.execute("SELECT * FROM words")]
    ids = {r["id"] for r in existing}
    keys = {identity(r) for r in existing if r["id"] not in (repairs or {})}
    keys.update(identity(item["candidate"]) for item in (repairs or {}).values())
    columns = {r[1] for r in conn.execute("PRAGMA table_info(words)")}
    known_topics = {r[0] for r in conn.execute("SELECT name FROM topics")}
    known_topics.update(op["new"]["name"] for op in bundle["topics"]["registry_operations"]
                        if op["operation"] == "insert_if_absent_or_exact_match")
    result = []
    for card in bundle["words_insert"]:
        fields = _project(card["fields"])
        key = identity(fields)
        if card["id"] in ids or key in keys or set(fields) - columns:
            raise ValueError("insert ID/identity/columns conflict: " + card["id"])
        if fields.get("topic") not in known_topics:
            raise ValueError("insert topic missing from registry: " + card["id"])
        if fields["type"] == "verb":
            assert_identity(conn, fields)
        ids.add(card["id"])
        keys.add(key)
        result.append(dict(id=card["id"], fields=fields))
    return result


def _synonym_plans(conn, bundle):
    pending = {c["id"]: _project(c["fields"]) for c in bundle["words_insert"]}
    result = []
    for link in bundle["synonym_links"]:
        ids = link["word_ids"]
        groups = sorted({r[0] for wid in ids for r in
                         conn.execute("SELECT group_id FROM word_synonyms WHERE word_id=?", (wid,))})
        members = set(ids)
        for gid in groups:
            members.update(r[0] for r in conn.execute("SELECT word_id FROM word_synonyms WHERE group_id=?", (gid,)))
        entries = [pending.get(wid) or _word(conn, wid) for wid in sorted(members)]
        if any(e is None for e in entries) or any(
                not synonym_compatible(a, b) for i, a in enumerate(entries) for b in entries[i + 1:]):
            raise ValueError("incompatible full synonym group union")
        result.append(dict(members=sorted(members), groups=groups))
    return result


def _plans(conn, bundle, approval):
    confirmations = _confirmation_specs(conn, bundle, approval)
    reset = _repair_plan(conn, bundle, approval)
    repairs = _repair_targets(conn, bundle)
    patches = _patch_plans(conn, bundle, confirmations)
    topic_plan = topics.preflight(conn, _topic_manifest(bundle))
    inserts = _insert_plans(conn, bundle, repairs)
    table_exists = adjective_forms.table_state(conn)
    if table_exists and any(conn.execute("SELECT 1 FROM adjective_degree_variants WHERE word_id=?",
                                         (item["id"],)).fetchone() for item in inserts):
        raise ValueError("adjective degree pairs already present for new cards")
    # Simulate final translations now, so bind/preflight reveal incomplete baselines.
    projected = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM words")}
    for wid, item in repairs.items():
        projected[wid] = item["candidate"]
    for item in patches:
        projected[item["word_id"]].update(item["final"])
    projected.update({item["id"]: item["fields"] for item in inserts})
    _translation_completeness(projected.values())
    return dict(fingerprint=_connection_fingerprint(conn), repair_manifest=reset,
                confirmations=[confirmations[wid] for wid in sorted(confirmations)
                               if _word(conn, wid)["reflexive_case"] is None],
                repairs={wid: dict(replacement=item["replacement"], baseline=item["row"])
                         for wid, item in sorted(repairs.items())},
                patches=patches, topics=topic_plan, inserts=inserts,
                degree_pairs=deepcopy(bundle["adjective_degree_variants"]),
                degree_table_exists=table_exists, synonyms=_synonym_plans(conn, bundle))


def bind(conn, bundle, source_sha256, approval):
    """Read-only. Approval is copied from the caller, never inferred."""
    _schema(conn)
    counts = validate_source(bundle, source_sha256)
    if not _approved(approval, source_sha256):
        raise PermissionError("explicit owner/linguistic approval for this exact source required")
    _check_target(conn, approval)
    bound = dict(schema=BOUND_SCHEMA, phase="bound", source_sha256=source_sha256,
                 source_digest=digest(bundle), source=deepcopy(bundle),
                 operation_id="gd888-clean-import:" + source_sha256, approvals=deepcopy(approval),
                 expected_counts=counts, binding=_plans(conn, bundle, approval))
    bound["digest"] = digest({k: v for k, v in bound.items() if k != "digest"})
    return bound


def _prior(conn, bound):
    rows = conn.execute("SELECT id,manifest_sha256,result_json FROM study_reset_operations WHERE id=? OR manifest_sha256=?",
                        (bound["operation_id"], bound["digest"])).fetchall()
    if not rows:
        return None
    if len(rows) != 1 or rows[0][0] != bound["operation_id"] or rows[0][1] != bound["digest"]:
        raise ValueError("clean import receipt conflict")
    try:
        receipt = json.loads(rows[0][2])
        if (receipt.get("schema") != RECEIPT_SCHEMA or receipt.get("package_digest") != bound["digest"]
                or receipt.get("operation_id") != bound["operation_id"]
                or receipt.get("receipt_sha256") != digest({k: v for k, v in receipt.items() if k != "receipt_sha256"})):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid clean import receipt") from None
    return receipt


def preflight(conn, bound):
    """Read-only. A matching receipt is checked before the superseded baseline."""
    _schema(conn)
    if (not isinstance(bound, dict) or bound.get("schema") != BOUND_SCHEMA or bound.get("phase") != "bound"
            or bound.get("digest") != digest({k: v for k, v in bound.items() if k != "digest"})):
        raise ValueError("invalid bound clean import")
    source = bound["source"]
    if digest(source) != bound["source_digest"] or validate_source(source, bound["source_sha256"]) != bound["expected_counts"]:
        raise ValueError("bound source changed")
    if not _approved(bound["approvals"], bound["source_sha256"]):
        raise PermissionError("bound approval invalid")
    _check_target(conn, bound["approvals"])
    prior = _prior(conn, bound)
    if prior is not None:
        return dict(replayed=True, operations=0, receipt=prior)
    if bound["binding"]["fingerprint"] != _connection_fingerprint(conn):
        raise ValueError("import baseline fingerprint changed")
    if _plans(conn, source, bound["approvals"]) != bound["binding"]:
        raise ValueError("bound operations/expected-old guards changed")
    return dict(replayed=False, counts=bound["expected_counts"])


@contextmanager
def _savepoint(conn):
    if not conn.in_transaction:
        raise ValueError("import requires caller-owned transaction")
    conn.execute("SAVEPOINT clean_bundle_import")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK TO clean_bundle_import")
        conn.execute("RELEASE clean_bundle_import")
        raise
    else:
        conn.execute("RELEASE clean_bundle_import")


def _postflight(conn, bound, before):
    plans, source = bound["binding"], bound["source"]
    differences = []
    expected = {}
    for item in plans["inserts"]:
        expected[item["id"]] = (None, item["fields"])
    for wid, item in plans["repairs"].items():
        expected[wid] = (item["baseline"], item["replacement"])
    for item in plans["patches"]:
        expected[item["word_id"]] = (item["baseline"], item["final"])
    after = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM words")}
    for wid, (baseline, changes) in expected.items():
        row = after.get(wid)
        if row is None:
            differences.append([wid, "missing"])
            continue
        want = {**(baseline or {}), **changes}
        differences.extend([wid, k] for k, v in want.items()
                           if k != "updated_at" and (baseline is not None or k in changes) and row.get(k) != v)
    untouched = [wid for wid in before if wid not in expected]
    differences.extend([wid, "untouched"] for wid in untouched if after.get(wid) != before[wid])
    unexpected = set(after) - set(before) - set(expected)
    differences.extend([wid, "unexpected"] for wid in sorted(unexpected))
    deferred = {c["id"] for c in source["deferred_manual"]["pons_cards"]} & set(after)
    differences.extend([wid, "deferred"] for wid in sorted(deferred))
    preserved = source["preserved"]
    for wid in set(preserved["protected_example_ids"]) | set(preserved["existing_preposition_example_ids"]):
        if wid in before and after.get(wid, {}).get("example") != before[wid]["example"]:
            differences.append([wid, "protected example"])
    if differences:
        raise ValueError("postflight exact differences: " + json.dumps(differences[:20]))
    pairs = sorted((r[0], r[1], r[2]) for r in conn.execute(
        "SELECT word_id,comparative,superlative FROM adjective_degree_variants")
        if r[0] in {item["id"] for item in plans["inserts"]}) if adjective_forms.table_state(conn) else []
    if pairs != sorted((p["word_id"], p["comparative"], p["superlative"]) for p in plans["degree_pairs"]):
        raise ValueError("adjective degree pairs mismatch")
    missing = _translation_completeness(after.values())
    aux = {}
    for row in after.values():
        if row[hilfsverb.FIELD] is not None:
            aux[row[hilfsverb.FIELD]] = aux.get(row[hilfsverb.FIELD], 0) + 1
    return dict(exact_field_differences=0, untouched_rows=len(untouched), global_translation_missing=missing,
                deferred_imported=0, hilfsverb_counts=aux, words_after=len(after))


def _translation_completeness(rows):
    missing = dict.fromkeys(LANGUAGES, 0)
    for row in rows:
        for language in LANGUAGES:
            if not isinstance(row.get(language), str) or not row[language].strip():
                missing[language] += 1
    if any(missing.values()):
        raise ValueError("global RU/UK/EN translation completeness failed: " + str(missing))
    return missing


def apply(conn, bound, *, approved_digest, integration_verified=False):
    """No commit; the caller owns BEGIN/COMMIT. Any failure rolls back everything."""
    if approved_digest != bound.get("digest") or not _approved(bound.get("approvals"), bound.get("source_sha256")):
        raise PermissionError("exact approved bound import required")
    if bound.get("binding", {}).get("repair_manifest") and integration_verified is not True:
        raise PermissionError("repair requires separately verified integration QA")
    with _savepoint(conn):
        state = preflight(conn, bound)
        if state["replayed"]:
            return dict(replayed=True, operations=0, receipt=state["receipt"])
        plans, operations = bound["binding"], 0
        before = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM words")}
        if plans["repair_manifest"]:
            repair.apply_reset_conn(conn, plans["repair_manifest"], integration_verified=True)
            operations += len(plans["repair_manifest"]["targets"])
        confirmation_digest = None
        if plans["confirmations"]:
            manifest = dict(schema="gd-verb-confirmation/v1",
                            operation_id=bound["operation_id"] + ":ordinary",
                            approvals={"owner": True, "linguistic": True},
                            baseline_fingerprint=_connection_fingerprint(conn),
                            parent_package_digest=bound["digest"], entries=deepcopy(plans["confirmations"]))
            manifest["digest"] = repair.manifest_digest(manifest)
            repair.confirm_legacy_conn(conn, manifest)
            confirmation_digest = manifest["digest"]
            operations += len(plans["confirmations"])
        for item in plans["patches"]:
            if item["patch"]:
                operations += int(hilfsverb.apply_patch(conn, item["word_id"], item["patch"],
                                                        expected_old=item["expected_old"]))
        # Moves, filter revisions and registry follow the exact topic merge.
        operations += topics.apply_plan(conn, plans["topics"])
        columns = {r[1] for r in conn.execute("PRAGMA table_info(words)")}
        for item in plans["inserts"]:
            fields = {**item["fields"], "id": item["id"]}
            if set(fields) - columns:
                raise ValueError("unsupported insert field")
            keys = sorted(fields)
            conn.execute("INSERT INTO words(" + ",".join(keys) + ") VALUES(" + ",".join("?" for _ in keys) + ")",
                         [fields[k] for k in keys])
            operations += 1
        if plans["degree_pairs"]:
            if adjective_forms.table_state(conn) != plans["degree_table_exists"]:
                raise ValueError("adjective degree table changed")
            conn.execute(adjective_forms.CREATE_TABLE_SQL)
            for pair in plans["degree_pairs"]:
                conn.execute("INSERT INTO adjective_degree_variants(word_id,comparative,superlative) VALUES(?,?,?)",
                             (pair["word_id"], pair["comparative"], pair["superlative"]))
                operations += 1
        for link in plans["synonyms"]:
            gid = min(link["groups"]) if link["groups"] else conn.execute(
                "INSERT INTO synonym_groups DEFAULT VALUES").lastrowid
            for wid in link["members"]:
                conn.execute("INSERT INTO word_synonyms(word_id,group_id) VALUES(?,?) "
                             "ON CONFLICT(word_id) DO UPDATE SET group_id=excluded.group_id", (wid, gid))
            operations += 1
        report = _postflight(conn, bound, before)
        receipt = dict(schema=RECEIPT_SCHEMA, operation_id=bound["operation_id"], package_digest=bound["digest"],
                       source_sha256=bound["source_sha256"], applied_operations=operations,
                       counts=bound["expected_counts"], report=report,
                       confirmation_digest=confirmation_digest,
                       repair_operation_id=(plans["repair_manifest"] or {}).get("operation_id"))
        receipt["receipt_sha256"] = digest(receipt)
        conn.execute("INSERT INTO study_reset_operations(id,manifest_sha256,applied_at,result_json) "
                     "VALUES(?,?,CURRENT_TIMESTAMP,?)",
                     (bound["operation_id"], bound["digest"], json.dumps(receipt, ensure_ascii=False, sort_keys=True)))
        return dict(replayed=False, operations=operations, receipt=receipt)
