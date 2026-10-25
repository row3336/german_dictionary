"""Deterministic lossless preparation of the accepted data18.6 layer."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unicodedata
import uuid
from . import perfekt
from .verb_construction import normalize_entry
from ..study.repair import digest

SCHEMA = "gd-dictionary-import/v2"
NAMESPACE = uuid.UUID("eac8b920-20df-5a8e-a064-6b3e67177509")
PINNED = {
 "word_plan.json": "ebe460a7694a3f9e4197cd8fe53f78d4eab2e071454d0213ef500ebd3530de58",
 "perfekt_plan.json": "8f5b4eb0781aa9caf16c9ea3143c9867bef6cf1185948d28941b7230367a8254",
 "topic_manifest.json": "5cef712bb29d282aa161cc647a1539211238a1665627a65f0b2019ec6bbe5eb1",
 "../data18_6_dedup_2026-10-25/phrases_new.json": "b7b2a38a1a3a611e2080db05b9736f1254d5d412df65c9d675cd8d4e7f0e593c",
 "../data18_5_final_2026-10-24/review.json": "f439780d9c5b9362e6f07be22370eb4dc9322547fac52e175f149d14b6a0338f",
 "acceptance.json": "5e6ce67942789519eb021ec869b4c19f3afdaa142b59716eb6fa7857dd986c37"}
RESTRICTED = frozenset(("reflexive:00009:Akk", "reflexive:00011:Akk", "reflexive:00015:Akk", "reflexive:00038:Dat", "reflexive:00049:Akk", "reflexive:00055:Akk", "reflexive:00057:Akk", "reflexive:00070:Akk", "reflexive:00073:Akk", "reflexive:00078:Akk"))

def canonical(value):
    return " ".join(unicodedata.normalize("NFC", str(value or "")).split()).lower()

def identity(fields):
    kind = canonical(fields.get("type"))
    if kind == "verb":
        return kind, canonical(fields.get("infinitive")), fields.get("reflexive_case")
    key = (kind, canonical(fields.get("german")))
    if kind == "nomen":
        return key + (canonical(fields.get("article")),)
    if kind == "praeposition":
        return key + (canonical(fields.get("preposition_type")), canonical(fields.get("case_type")))
    return key

def stable_id(fields):
    return str(uuid.uuid5(NAMESPACE, json.dumps(identity(fields), ensure_ascii=False, separators=(",", ":"))))

def package_digest(package):
    return digest({k: v for k, v in package.items() if k != "digest"})

def seal(package):
    package["digest"] = package_digest(package)
    return package

def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result

def load_json(path):
    def reject(_):
        raise ValueError("nonfinite JSON")
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique, parse_constant=reject)

def required_gaps(fields, *, restricted=False):
    gaps = [k for k in ("type", "russian", "ukrainian", "english", "topic") if not isinstance(fields.get(k), str) or not fields[k].strip()]
    kind = fields.get("type")
    if kind not in {"verb", "nomen", "adjektiv", "adverb", "pronomen", "praeposition", "phrase", "konjunktion", "partikel"}:
        gaps.append("supported_type")
    if fields.get("cefr_level", "") not in ("", "A1", "A2", "B1", "B2", "C1", "C2"):
        gaps.append("cefr_level")
    if kind == "nomen" and fields.get("article") not in ("der", "die", "das"):
        gaps.append("article_classification")
    if kind == "praeposition" and (fields.get("case_type") not in ("dativ", "akkusativ", "genitiv", "wechsel") or fields.get("preposition_type") not in ("local", "temporal", "modal", "kausal", "final")):
        gaps.append("preposition_classification")
    required = {"nomen": ("german", "article", "plural"), "verb": ("infinitive", "prateritum", "partizip2"), "adjektiv": ("german", "komparativ", "superlativ"), "praeposition": ("german", "preposition_type", "case_type")}
    gaps.extend(k for k in required.get(kind, ("german",)) if not fields.get(k))
    if kind == "verb":
        if type(fields.get("is_separable")) is not int or fields["is_separable"] not in (0, 1):
            gaps.append("is_separable")
        if fields.get("reflexive_case") not in ("none", "akkusativ", "dativ"):
            gaps.append("reflexive_case")
        tables = ("conjugation_praesens", "conjugation_praeteritum")
        if not restricted:
            gaps.extend(k for k in tables if not fields.get(k))
        elif any(fields.get(k) for k in tables):
            gaps.append("restricted_tables_must_be_empty")
        if not fields.get(perfekt.FIELD):
            gaps.append(perfekt.FIELD)
    return gaps

def validate_card(card):
    fields = card["fields"]
    gaps = required_gaps(fields, restricted=card.get("source_key") in RESTRICTED)
    accepted_absent = {"made:00007": ("komparativ", "superlativ"), "made:00009": ("komparativ", "superlativ"), "made:00011": ("komparativ", "superlativ"), "tartorg:00013": ("komparativ", "superlativ"), "made:00030": ("plural",), "tartorg:00016": ("plural",), "tartorg:00019": ("plural",)}
    absent = accepted_absent.get(card.get("source_key"), ())
    original = card.get("provenance", {})
    if card.get("source_key") in RESTRICTED:
        if (original.get("source_layer_sha256") != PINNED["../data18_5_final_2026-10-24/review.json"] or original.get("person_restriction_applies") is not True or original.get("full_person_policy") != "forbid_full_six_person_training" or any(original.get("fields", {}).get(k) != "" for k in ("conjugation_praesens", "conjugation_praeteritum"))):
            raise ValueError("restricted table exception requires exact accepted source policy")
    if absent:
        if original.get("source_layer_sha256") != PINNED["../data18_5_final_2026-10-24/review.json"] or any(original.get("fields", {}).get(k) != "" or fields.get(k) != "" for k in absent):
            raise ValueError("accepted absent form provenance mismatch")
        gaps = [k for k in gaps if k not in absent]
    if gaps:
        raise ValueError("incomplete new/repair card " + card["id"] + ": " + ",".join(gaps))
    normalize_entry(fields)
    perfekt.validate_entry(fields)
    if fields["type"] == "phrase":
        return
    senses, examples = card.get("senses"), card.get("examples")
    if not senses or not isinstance(examples, list) or len(examples) < 2:
        raise ValueError("retained senses and two examples required")
    sense_ids, covered = {s["sense_id"] for s in senses}, set()
    for ex in examples:
        if not ex.get("text_de") or not ex.get("translation_ru") or not ex.get("sense_ids") or set(ex["sense_ids"]) - sense_ids:
            raise ValueError("retained example incomplete or unknown sense")
        covered.update(ex["sense_ids"])
    if covered != sense_ids or fields.get("example") != "\n".join(ex["text_de"] for ex in examples):
        raise ValueError("retained example/sense loss")
    if fields["type"] == "praeposition":
        cases = ("akkusativ", "dativ") if fields["case_type"] == "wechsel" else (fields["case_type"],)
        aliases = {"akkusativ": {"Akk", "akkusativ"}, "dativ": {"Dat", "dativ"}, "genitiv": {"Gen", "genitiv"}}
        for case in cases:
            labels = aliases.get(case, {case})
            if sum(ex.get("case") in labels or any(p.get("case") in labels for p in ex.get("prepositions", [])) for ex in examples) < 2:
                raise ValueError("preposition requires two examples per case")

def validate_content(package):
    if package.get("schema") != SCHEMA or package.get("digest") != package_digest(package):
        raise ValueError("invalid package schema/digest")
    if not package.get("sources") or any(len(s) != 64 or set(s) - set("0123456789abcdef") for s in package["sources"].values()):
        raise ValueError("source SHA manifest required")
    cards, seen, keys = package.get("cards", []), set(), set()
    for card in cards:
        validate_card(card)
        wid, key = card["id"], identity(card["fields"])
        if wid in seen or key in keys or card.get("identity") != list(key):
            raise ValueError("duplicate card identity/id")
        seen.add(wid)
        keys.add(key)
        if not card.get("existing_id") and wid != stable_id(card["fields"]):
            raise ValueError("new card stable ID mismatch")
        if card.get("existing_id") and card["existing_id"] != wid:
            raise ValueError("repair must preserve ID")
    mapping = package.get("source_mapping", {})
    if len(mapping) != len(cards) or set(mapping.values()) != seen:
        raise ValueError("source mapping lost")
    variants = package.get("variant_mapping", {})
    if len(variants) != sum(len(x["variants"]) for x in package.get("metadata", [])):
        raise ValueError("variant mapping lost")
    card_by_id = {c["id"]:c for c in cards}
    metadata_ids = set()
    for item in package.get("metadata", []):
        wid, vs = item["id"], item["variants"]
        if wid in metadata_ids or not vs or any(variants.get(v["variant_id"]) != wid for v in vs):
            raise ValueError("variant mapping mismatch/duplicate metadata target")
        metadata_ids.add(wid)
        virtual = dict(type="verb", infinitive=vs[0]["lemma_for_answer"], german=vs[0]["lemma_for_answer"], reflexive_case=vs[0]["reflexive_case"], **perfekt.scalar_projection(vs))
        payload = perfekt.normalize_payload(dict(version=1,status="verified",variants=vs), virtual)
        if wid in card_by_id and json.loads(card_by_id[wid]["fields"][perfekt.FIELD]) != payload:
            raise ValueError("new/repair metadata differs from stored linked fields")
    if {c["id"] for c in cards if c["fields"]["type"] == "verb"} - metadata_ids:
        raise ValueError("new/repair verb has no mapped metadata")
    return dict(cards=len(cards), inserts=sum(not x.get("existing_id") for x in cards), repairs=sum(bool(x.get("existing_id")) for x in cards), word_examples=sum(len(x.get("examples", [])) for x in cards), verbs=len(package.get("metadata", [])), variants=len(variants), extra_examples=sum(len(v["examples"]) for x in package.get("metadata", []) for v in x["variants"]))

def build_package(source_root):
    """Pinned read-only sources, no DB access, timestamp or generated approval."""
    root, docs = Path(source_root), {}
    for name, expected in PINNED.items():
        path = root / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("pinned source changed: " + name)
        docs[name] = load_json(path)
    word_plan, aux, topics = (docs[x] for x in ("word_plan.json", "perfekt_plan.json", "topic_manifest.json"))
    topic_map = {x["source_key"]: x["target_topic"] for x in topics["new_source_topic_mapping"]}
    cards, mapping, metadata, variant_mapping = [], {}, [], {}
    by_source = {x["source_key"]: x for x in aux["entries"] if x.get("source_key")}
    for row in word_plan["rows"]:
        fields = deepcopy(row["fields"])
        fields["topic"] = topic_map[row["source_key"]]
        if fields["type"] == "verb":
            variants = by_source[row["source_key"]]["variants"]
            fields.update(perfekt.scalar_projection(variants))
            fields[perfekt.FIELD] = perfekt.serialize_payload(dict(version=1, status="verified", variants=variants), fields)
        wid = row["existing_id"] or stable_id(fields)
        mapping[row["source_key"]] = wid
        cards.append(dict(id=wid, existing_id=row["existing_id"], source_key=row["source_key"], identity=list(identity(fields)), fields=fields, senses=deepcopy(row["retained_senses"]), examples=deepcopy(row["retained_examples"]), provenance=deepcopy(row)))
    for row in docs["../data18_6_dedup_2026-10-25/phrases_new.json"]["phrase_variants"]:
        fields = dict(type="phrase", german=row["de"], russian=row["ru"], ukrainian=row["uk"], english=row["en"], topic=topic_map[row["variant_id"]])
        wid = stable_id(fields)
        mapping[row["variant_id"]] = wid
        cards.append(dict(id=wid, existing_id=None, source_key=row["variant_id"], identity=list(identity(fields)), fields=fields, senses=[], examples=[], provenance=deepcopy(row)))
    for row in aux["entries"]:
        wid = row["existing_id"] or mapping[row["source_key"]]
        item = {**deepcopy(row), "id": wid}
        vs = item["variants"]
        entry = dict(type="verb", infinitive=vs[0]["lemma_for_answer"], german=vs[0]["lemma_for_answer"], reflexive_case=vs[0]["reflexive_case"], **perfekt.scalar_projection(vs))
        perfekt.normalize_payload(dict(version=1, status="verified", variants=vs), entry)
        metadata.append(item)
        for v in vs:
            if v["variant_id"] in variant_mapping:
                raise ValueError("duplicate source variant")
            variant_mapping[v["variant_id"]] = wid
    links = []
    for link in docs["../data18_5_final_2026-10-24/review.json"]["synonym_links_proposed"]:
        ids = [next(c["id"] for c in cards if c["provenance"].get("source_id") == sid) for sid in link["source_ids"]]
        links.append(dict(word_ids=ids, provenance=deepcopy(link)))
    package = seal(dict(schema=SCHEMA, phase="draft", operation_id="data18.7:" + digest(PINNED), sources=deepcopy(PINNED), cards=cards, metadata=metadata, source_mapping=mapping, variant_mapping=variant_mapping, topics=deepcopy(topics), synonym_links=links, expected_counts=dict(cards=350, inserts=348, repairs=2, word_examples=396, verbs=459, variants=486, extra_examples=571), approvals=None, binding=None))
    actual = validate_content(package)
    if actual != package["expected_counts"]:
        raise ValueError("accepted source counts changed: " + str(actual))
    return package
