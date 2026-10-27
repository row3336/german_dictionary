"""Retired consumer for the frozen data18 + PONS schema21 envelope.

bind/preflight/apply refuse through the retired v2 schema gate; the approved
clean bundle is consumed by common.import_clean_bundle instead.
"""

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from . import adjective_forms, data18_builder as builder, import_package_v2 as v2, perfekt
from .db_safety import _connection_fingerprint
from .verb_construction import assert_identity, normalize_entry
from ..study import repair


SOURCE_SCHEMA = "gd888.pons-merged-preparation/v1"
BOUND_SCHEMA = "gd888.pons-merged-import/v1"
RECEIPT_SCHEMA = "gd888.pons-merged-receipt/v1"
SOURCE_DIGEST = "539375cf74803c12c85677b45563632762652421b6526ee443eba75de0c88a76"
GRAMM_ID = "7aaa97c1-dd9b-4fbf-b442-247a27f1bfdb"
GRAMM_OLD = "f3d12e70eb8dddc194bd01068c6573b14e6947733166308db8071ddd4a90ee0e"
GRAMM_FRESH = "6cea3e42315be922a31a49c1e87de72d9ee379c705edd9fc008d78dedec3a2e5"
EXPECTED = dict(cards=1760, inserts=1758, repairs=2, pons_new_cards=1410,
                verb_metadata=715, variants=746, pons_verbs=256, pons_examples=2914,
                old_example_patches=1202, target_only_witnesses=42,
                protected_examples=436, translation_proposals_not_applied=303,
                raw_book_occurrences=2523, book_groups=2453)
COMPARISON_ALTERNATE_CARDS = 74
CITATION_SUPERLATIVE_CARDS = 73
CITATION_ONLY_EXCEPTIONS = {"verschieden", "innere"}
# Pin the selected answer set as well as the frozen source: an older bound
# package used the same source digest but still included article forms.
DEGREE_PAIRS_SHA256 = "af6bf4fd2d13e144e8aa1afcc2a5914726c6c16b3ba42df59bd7377bfde4e665"
EXCEPTION_COUNTS = dict(documented_absent_plural=112,
                        explicit_comparison_restriction=79,
                        explicit_selected_paradigm_or_P2_override=7,
                        book_supported_multiple_articles=5,
                        resolved_homonym_separability=4,
                        book_normative_alternative_preterite=1)
MIGRATION_COLUMNS = {"reflexive_case", "perfekt_variants_json"}
EMPTY_PROJECTION_COLUMNS = {"plural", "komparativ", "superlativ"}
EXPLICIT_DEGREE_PAIRS = {
    "88176cfb-94ac-51c4-ad7f-c74e1366aaa6": (
        ("blasser", "am blassesten"),
        ("blässer", "am blässesten"),
    ),
    "b0b2b407-56d3-5d03-8fb4-c81db8dc53d5": (
        ("ungesünder", "am ungesündesten"),
        ("ungesunder", "am ungesundesten"),
    ),
}
REHEARSAL_ROOT = Path("/Users/wf/py/_agent_work/888_german_dictionary/data18_8_20261026/rehearsal")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def word(conn, wid):
    result = conn.execute("SELECT * FROM words WHERE id=?", (wid,)).fetchone()
    return dict(result) if result else None


def validate_source(source):
    """Source remains preparation only; this adapter never edits its flags."""
    if (source.get("schema") != SOURCE_SCHEMA or source.get("digest") != SOURCE_DIGEST
            or digest({k:v for k,v in source.items() if k != "digest"}) != SOURCE_DIGEST):
        raise ValueError("unrecognized or modified source envelope")
    if (source.get("phase") != "prepared_data_unbound" or source.get("binding") is not None
            or any(source.get(k) is not False for k in
                   ("import_ready", "runtime_apply_compatible", "approved_to_apply"))):
        raise ValueError("source apply flags changed")
    if source.get("expected_counts") != EXPECTED:
        raise ValueError("source counts changed")
    base = source["original_package"]
    if builder.validate_content(base) != base["expected_counts"]:
        raise ValueError("original data18 package changed")
    if (source["cards"][:350] != base["cards"] or source["metadata"][:459] != base["metadata"]
            or len(source["cards"]) != 1760 or len(source["metadata"]) != 715):
        raise ValueError("original data18 slice changed")
    if (source["book"]["topic"] != "PONS" or source["book"]["book_level"] != "PONS"
            or source["book"]["target_registry_level"] != "PONS"
            or source["book"]["word_level_policy"] != "frozen_per_card_review"):
        raise ValueError("mixed-level PONS book policy changed")
    if source["owner_translation_notes"]["apply"] is not False:
        raise ValueError("owner translation notes are not approved for apply")
    kinds = {}
    for item in source["accepted_exceptions"]:
        kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
    if kinds != EXCEPTION_COUNTS:
        raise ValueError("PONS exception set changed")
    by_exception = {(e["id"],e["kind"]):e for e in source["accepted_exceptions"]}
    if len(by_exception) != len(source["accepted_exceptions"]):
        raise ValueError("duplicate PONS exception")
    ids, identities = set(), set()
    for card in source["cards"][350:]:
        fields, sid = card["fields"], card["source_key"]
        if (card["id"] != builder.stable_id(fields) or card["existing_id"] is not None
                or card["identity"] != list(builder.identity(fields))
                or fields["topic"] != "PONS" or not fields.get("cefr_level")
                or fields["cefr_level"] not in ("A1","A2","B1","B2","C1","C2")):
            raise ValueError("invalid PONS identity/topic/CEFR")
        if card["id"] in ids or tuple(card["identity"]) in identities:
            raise ValueError("duplicate PONS card")
        ids.add(card["id"])
        identities.add(tuple(card["identity"]))
        for key in ("russian","ukrainian","english"):
            if not isinstance(fields.get(key),str) or not fields[key].strip():
                raise ValueError("incomplete PONS translation")
        if fields["type"] == "nomen":
            article = fields.get("article")
            if article not in ("der","die","das"):
                evidence = by_exception.get((sid,"book_supported_multiple_articles"))
                if not evidence or article != evidence["article"]:
                    raise ValueError("unapproved article variant")
            if fields.get("plural") is None and (sid,"documented_absent_plural") not in by_exception:
                raise ValueError("unapproved absent plural")
        if fields["type"] == "adjektiv":
            comparison = card["comparison"]
            for key, forms in (("komparativ","comparative_forms"),("superlativ","superlative_forms")):
                if fields.get(key) != (comparison[forms][0] if comparison[forms] else None):
                    raise ValueError("comparison projection disagrees with alternatives")
            if (not fields.get("komparativ") or not fields.get("superlativ")) and (sid,"explicit_comparison_restriction") not in by_exception:
                raise ValueError("unapproved comparison restriction")
        if fields["type"] == "verb":
            for key in ("infinitive","prateritum","partizip2","conjugation_praesens",
                        "conjugation_praeteritum",perfekt.FIELD):
                if not fields.get(key):
                    raise ValueError("incomplete PONS verb")
            if type(fields.get("is_separable")) is not int or fields["is_separable"] not in (0,1):
                raise ValueError("unverified PONS separability")
        if fields["type"] == "phrase":
            if card["examples"] or "example" in fields:
                raise ValueError("phrase must not have additional examples")
        else:
            if len(card["examples"]) < 2 or fields.get("example") != "\n\n".join(e["text_de"] for e in card["examples"]):
                raise ValueError("PONS example projection mismatch")
        normalized = perfekt.validate_entry(normalize_entry(fields))
        if any(normalized.get(key) != value for key,value in fields.items()):
            raise ValueError("PONS fields not normalized")
    adjectives = [card for card in source["cards"][350:]
                  if card["fields"]["type"] == "adjektiv"]
    alternate_cards = sum(len(card["comparison"]["comparative_forms"]) > 1
                          or len(card["comparison"]["superlative_forms"]) > 1
                          for card in adjectives)
    citation_cards = sum(any(value.startswith(("der ", "die ", "das "))
                             for value in card["comparison"]["superlative_forms"])
                         for card in adjectives)
    if (len(adjectives) != 175 or alternate_cards != COMPARISON_ALTERNATE_CARDS
            or citation_cards != CITATION_SUPERLATIVE_CARDS):
        raise ValueError("PONS comparison alternatives/citations changed")
    variant_map = {v["variant_id"]:m["id"] for m in source["metadata"] for v in m["variants"]}
    if variant_map != source["variant_mapping"] or len(variant_map) != 746:
        raise ValueError("linked variant mapping changed")
    protected = set(source["protected_examples"]["ids"]) | set(source["protected_examples"]["same_id_repairs"])
    patches = source["old_example_patches"]
    if len(patches) != 1202 or len({p["id"] for p in patches}) != 1202:
        raise ValueError("example patch set changed")
    for patch in patches:
        if (patch["id"] in protected or patch["allowed_columns"] != ["example"]
                or patch["operation"] != "guarded_example_only_update"
                or patch["translation_change_allowed"] is not False
                or hashlib.sha256(patch["expected_old_example"].encode()).hexdigest() != patch["expected_old_example_sha256"]
                or hashlib.sha256(patch["new_example"].encode()).hexdigest() != patch["new_example_sha256"]):
            raise ValueError("invalid guarded example patch")
    return EXPECTED.copy()


def _topic_plan(conn):
    expected = dict(name="PONS", description="PONS", level="PONS")
    row = conn.execute("SELECT name,description,level FROM topics WHERE name=?", ("PONS",)).fetchone()
    if row is not None and dict(row) != expected:
        raise ValueError("conflicting PONS registry row")
    return dict(insert=row is None, expected=expected)


def _check_rehearsal_target(conn, approval):
    if approval.get("scope") != "rehearsal_only":
        return
    target = Path(approval.get("target_path", "")).resolve()
    rows = conn.execute("PRAGMA database_list").fetchall()
    opened = [Path(row[2]).resolve() for row in rows if row[1] == "main"]
    if (len(opened) != 1 or target != opened[0]
            or not target.is_relative_to(REHEARSAL_ROOT.resolve())):
        raise PermissionError("rehearsal approval is bound to one disposable DB path")


def _patch_plans(conn, source):
    result = []
    for patch in source["old_example_patches"]:
        old = word(conn,patch["id"])
        expected = GRAMM_FRESH if patch["id"] == GRAMM_ID else patch["expected_word_row_sha256"]
        if patch["id"] == GRAMM_ID and patch["expected_word_row_sha256"] != GRAMM_OLD:
            raise ValueError("original Gramm guard changed")
        if (old is None or any(old.get(key) is not None for key in MIGRATION_COLUMNS)
                or digest({key:value for key,value in old.items() if key not in MIGRATION_COLUMNS}) != expected
                or old["example"] != patch["expected_old_example"]
                or any(old[key] != value for key,value in patch["translations"].items())):
            raise ValueError("example target changed: " + patch["id"])
        if patch["id"] == GRAMM_ID and (old["plural"] != "Gramm" or old["updated_at"] != "2026-10-25 17:46:53"):
            raise ValueError("unreviewed Gramm drift")
        result.append(dict(id=patch["id"],baseline=old,new_example=patch["new_example"],
                           would_change=patch["would_change_example"]))
    return result


def _insert_plans(conn, source, base_bound):
    columns = {r[1] for r in conn.execute("PRAGMA table_info(words)")}
    existing = [dict(row) for row in conn.execute("SELECT * FROM words")]
    ids = {row["id"] for row in existing}
    identities = {builder.identity(row) for row in existing}
    for item in base_bound["binding"]["inserts"]:
        ids.add(item["id"])
        identities.add(builder.identity(item["fields"]))
    result = []
    for card in source["cards"][350:]:
        projected = deepcopy(card["fields"])
        for column in EMPTY_PROJECTION_COLUMNS:
            if projected.get(column) is None and column in projected:
                projected[column] = ""
        fields = perfekt.validate_entry(normalize_entry(projected))
        key = builder.identity(fields)
        if card["id"] in ids or key in identities or set(fields) - columns:
            raise ValueError("PONS insert ID/identity/columns conflict")
        if fields["type"] == "verb":
            assert_identity(conn,fields)
        result.append(dict(id=card["id"],fields=fields))
        ids.add(card["id"])
        identities.add(key)
    return result


def _degree_pair_plans(source):
    """Keep answer forms, omitting article-inflected citation alternatives."""
    plans = []
    seen_overrides = set()
    for card in source["cards"][350:]:
        if card["fields"]["type"] != "adjektiv":
            continue
        word_id = card["id"]
        forms = card["comparison"]
        comparatives = forms["comparative_forms"]
        source_superlatives = forms["superlative_forms"]
        if (not any(form.startswith("am ") for form in source_superlatives)
                and any(form.startswith(("der ", "die ", "das "))
                        for form in source_superlatives)
                and card["fields"]["german"] not in CITATION_ONLY_EXCEPTIONS):
            raise ValueError("unreviewed citation-only superlative")
        # The article-inflected citation belongs to a noun phrase, not to this
        # standalone word-card answer. Keep source data untouched for audit.
        superlatives = ([form for form in source_superlatives
                         if not form.startswith(("der ", "die ", "das "))]
                        if any(form.startswith("am ") for form in source_superlatives)
                        else source_superlatives)
        if word_id in EXPLICIT_DEGREE_PAIRS:
            pairs = EXPLICIT_DEGREE_PAIRS[word_id]
            seen_overrides.add(word_id)
        elif len(comparatives) > 1 and len(superlatives) > 1:
            raise ValueError("unreviewed adjective degree combinations")
        elif comparatives or superlatives:
            pairs = tuple((comparative, superlative)
                          for comparative in (comparatives or [""])
                          for superlative in (superlatives or [""]))
        else:
            pairs = ()
        if (len(pairs) != len(set(pairs))
                or {a for a, _ in pairs if a} != set(comparatives)
                or {b for _, b in pairs if b} != set(superlatives)
                or any(not a and not b for a, b in pairs)):
            raise ValueError("adjective degree pairs do not cover frozen source")
        plans.extend(dict(word_id=word_id, comparative=a, superlative=b)
                     for a, b in pairs)
    if seen_overrides != set(EXPLICIT_DEGREE_PAIRS):
        raise ValueError("degree-pair exception missing from frozen package")
    if (len(plans) != 103 or len({plan["word_id"] for plan in plans}) != 97):
        raise ValueError("unexpected standalone adjective degree selection")
    return plans


def _degree_table_state(conn):
    name = adjective_forms.TABLE
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    if row is None:
        return False
    exact_ddl = adjective_forms.CREATE_TABLE_SQL.replace("IF NOT EXISTS ", "", 1)
    if row[0] != exact_ddl:
        raise ValueError("incompatible adjective degree table DDL")
    columns = conn.execute("PRAGMA table_info(" + name + ")").fetchall()
    if ([(r[1], r[2].upper(), r[3], r[5]) for r in columns]
            != [("word_id", "TEXT", 1, 1), ("comparative", "TEXT", 1, 2),
                ("superlative", "TEXT", 1, 3)]):
        raise ValueError("incompatible adjective degree table")
    foreign = conn.execute("PRAGMA foreign_key_list(" + name + ")").fetchall()
    if len(foreign) != 1 or tuple(foreign[0][2:5]) != ("words", "word_id", "id"):
        raise ValueError("incompatible adjective degree foreign key")
    return True


def bind_package(conn, source, approval):
    v2._schema(conn)
    validate_source(source)
    if not v2._approved(approval):
        raise PermissionError("explicit owner and linguistic approval required")
    _check_rehearsal_target(conn, approval)
    base = v2.bind_package(conn,source["original_package"],approval)
    bound = dict(schema=BOUND_SCHEMA,phase="bound",source_digest=SOURCE_DIGEST,
                 operation_id=source["original_package"]["operation_id"]+":PONS",
                 approvals=deepcopy(approval),original_bound=base,
                 expected_counts=EXPECTED.copy(),
                 binding=dict(fingerprint=_connection_fingerprint(conn),
                              topic=_topic_plan(conn),patches=_patch_plans(conn,source),
                              inserts=_insert_plans(conn,source,base),
                              degree_pairs=_degree_pair_plans(source),
                              degree_table_exists=_degree_table_state(conn)))
    bound["digest"] = digest(bound)
    return bound


def _prior(conn,bound):
    rows = conn.execute("SELECT id,manifest_sha256,result_json FROM study_reset_operations WHERE id=? OR manifest_sha256=?",
                        (bound["operation_id"],bound["digest"])).fetchall()
    if not rows:
        return None
    if len(rows)!=1 or rows[0][0]!=bound["operation_id"] or rows[0][1]!=bound["digest"]:
        raise ValueError("PONS receipt conflict")
    receipt = json.loads(rows[0][2])
    if (receipt.get("schema")!=RECEIPT_SCHEMA or receipt.get("package_digest")!=bound["digest"]
            or receipt.get("operation_id")!=bound["operation_id"]
            or receipt.get("receipt_sha256")!=repair.digest({k:v for k,v in receipt.items() if k!="receipt_sha256"})):
        raise ValueError("invalid PONS receipt")
    return receipt


def preflight(conn,bound):
    v2._schema(conn)
    if (bound.get("schema")!=BOUND_SCHEMA or bound.get("phase")!="bound"
            or bound.get("source_digest")!=SOURCE_DIGEST or bound.get("expected_counts")!=EXPECTED
            or bound.get("digest")!=digest({k:v for k,v in bound.items() if k!="digest"})):
        raise ValueError("invalid bound PONS package")
    if digest(bound.get("binding", {}).get("degree_pairs")) != DEGREE_PAIRS_SHA256:
        raise ValueError("outdated adjective degree selection")
    _check_rehearsal_target(conn,bound["approvals"])
    prior = _prior(conn,bound)
    if prior is not None:
        return dict(replayed=True,operations=0,receipt=prior)
    if bound["binding"]["fingerprint"]!=_connection_fingerprint(conn):
        raise ValueError("PONS baseline fingerprint changed")
    base = v2.preflight(conn,bound["original_bound"])
    if base["replayed"]:
        raise ValueError("data18 receipt without PONS receipt")
    if _topic_plan(conn)!=bound["binding"]["topic"]:
        raise ValueError("PONS topic registry changed")
    for patch in bound["binding"]["patches"]:
        if word(conn,patch["id"])!=patch["baseline"]:
            raise ValueError("example baseline changed")
    if any(word(conn,item["id"]) for item in bound["binding"]["inserts"]):
        raise ValueError("PONS insert appeared")
    if _degree_table_state(conn) != bound["binding"]["degree_table_exists"]:
        raise ValueError("adjective degree table changed")
    if bound["binding"]["degree_table_exists"]:
        if any(conn.execute("SELECT 1 FROM adjective_degree_variants WHERE word_id=?", (item["id"],)).fetchone()
               for item in bound["binding"]["inserts"]):
            raise ValueError("PONS adjective degree pairs appeared")
    return dict(replayed=False,counts=EXPECTED.copy(),
                comparison_answer_policy="standalone_am_or_citation_only_exception",
                adjective_degree_pairs=len(bound["binding"]["degree_pairs"]))


@contextmanager
def _savepoint(conn):
    if not conn.in_transaction:
        raise ValueError("caller-owned transaction required")
    conn.execute("SAVEPOINT dictionary_import_v3")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK TO dictionary_import_v3")
        conn.execute("RELEASE dictionary_import_v3")
        raise
    else:
        conn.execute("RELEASE dictionary_import_v3")


def _expected_after_base(bound, patch):
    """Account only for the v2 changes that may overlap an example patch."""
    plan = bound["original_bound"]["binding"]
    result = patch["baseline"].copy()
    wid = patch["id"]
    changed = False
    for item in plan["confirmations"]:
        if item["word_id"] == wid:
            result["reflexive_case"] = "none"
            changed = True
    for item in plan["metadata"]:
        if item["word_id"] == wid and item["patch"]:
            result.update(item["patch"])
            changed = True
    for item in plan["topics"]["moves"]:
        if item["word_id"] == wid:
            result.update(item["new"])
            changed = True
    return result, changed


def apply(conn,bound,*,approved_digest,integration_verified=False):
    if approved_digest!=bound.get("digest") or not v2._approved(bound.get("approvals")):
        raise PermissionError("exact approved bound package required")
    with _savepoint(conn):
        state = preflight(conn,bound)
        if state["replayed"]:
            return state
        base = v2.apply(conn,bound["original_bound"],
                        approved_digest=bound["original_bound"]["digest"],
                        integration_verified=integration_verified)
        operations = base["operations"]
        topic = bound["binding"]["topic"]
        if topic["insert"]:
            row=topic["expected"]
            conn.execute("INSERT INTO topics(name,description,level) VALUES(?,?,?)",
                         (row["name"],row["description"],row["level"]))
            operations+=1
        allowed={r[1] for r in conn.execute("PRAGMA table_info(words)")}
        for item in bound["binding"]["inserts"]:
            fields={**item["fields"],"id":item["id"]}
            if set(fields)-allowed:
                raise ValueError("unsupported PONS column")
            keys=sorted(fields)
            conn.execute("INSERT INTO words("+",".join(keys)+") VALUES("+",".join("?" for _ in keys)+")",
                         [fields[k] for k in keys])
            operations+=1
        conn.execute(adjective_forms.CREATE_TABLE_SQL)
        for pair in bound["binding"]["degree_pairs"]:
            conn.execute("INSERT INTO adjective_degree_variants(word_id,comparative,superlative) VALUES(?,?,?)",
                         (pair["word_id"],pair["comparative"],pair["superlative"]))
            operations+=1
        after_base = {}
        for patch in bound["binding"]["patches"]:
            before=patch["baseline"]
            current=word(conn,patch["id"])
            expected, timestamp_may_change = _expected_after_base(bound, patch)
            if (current is None or any(current[key] != value for key,value in expected.items()
                                       if key != "updated_at")
                    or (not timestamp_may_change and current["updated_at"] != expected["updated_at"])):
                raise ValueError("unexpected base change before example patch")
            after_base[patch["id"]] = current
            if patch["would_change"]:
                result=conn.execute("UPDATE words SET example=? WHERE id=? AND example=?",
                                    (patch["new_example"],patch["id"],before["example"]))
                if result.rowcount!=1:
                    raise ValueError("example update failed")
                operations+=1
        for item in bound["binding"]["inserts"]:
            current=word(conn,item["id"])
            if current is None or any(current.get(k)!=v for k,v in item["fields"].items()):
                raise ValueError("PONS inserted fields mismatch")
        inserted_ids = {item["id"] for item in bound["binding"]["inserts"]}
        actual_pairs = [dict(word_id=row[0],comparative=row[1],superlative=row[2])
                        for row in conn.execute("SELECT word_id,comparative,superlative "
                                                "FROM adjective_degree_variants ORDER BY word_id,comparative,superlative")
                        if row[0] in inserted_ids]
        expected_pairs = sorted(bound["binding"]["degree_pairs"],
                                key=lambda p:(p["word_id"],p["comparative"],p["superlative"]))
        if actual_pairs != expected_pairs:
            raise ValueError("PONS adjective degree pairs mismatch")
        for patch in bound["binding"]["patches"]:
            current=word(conn,patch["id"])
            expected={**after_base[patch["id"]],"example":patch["new_example"]}
            if current != expected:
                raise ValueError("example-only postflight mismatch")
        missing={name:0 for name in ("russian","ukrainian","english")}
        for row in conn.execute("SELECT russian,ukrainian,english FROM words"):
            for name,value in zip(missing,row):
                if not isinstance(value,str) or not value.strip():
                    missing[name]+=1
        if any(missing.values()):
            raise ValueError("dictionary translations incomplete")
        receipt=dict(schema=RECEIPT_SCHEMA,operation_id=bound["operation_id"],
                     package_digest=bound["digest"],applied_operations=operations,
                     counts=EXPECTED.copy(),base_receipt_sha256=base["receipt"]["receipt_sha256"],
                     report=dict(pons_insert_mismatches=0,example_patch_mismatches=0,
                                 global_translation_missing=missing,gramm_plural_preserved=True,
                                 comparison_answer_policy="standalone_am_or_citation_only_exception",
                                 adjective_degree_pairs=len(expected_pairs),
                                 adjective_alternative_cards_covered=5,
                                 citation_superlative_source_cards=CITATION_SUPERLATIVE_CARDS,
                                 citation_only_exception_cards=2,
                                 inflected_citation_alternatives_omitted=72))
        receipt["receipt_sha256"]=repair.digest(receipt)
        conn.execute("INSERT INTO study_reset_operations(id,manifest_sha256,applied_at,result_json) VALUES(?,?,CURRENT_TIMESTAMP,?)",
                     (bound["operation_id"],bound["digest"],json.dumps(receipt,ensure_ascii=False,sort_keys=True)))
        return dict(replayed=False,operations=operations,receipt=receipt)
