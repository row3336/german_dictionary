"""Versioned import preflight and lossless JSON round-trip; no apply surface."""

import json
import re
import uuid

from .verb_construction import identity, normalize_entry, assert_identity, canonical_infinitive
from .db_safety import _connection_fingerprint
from ..study.repair import digest

SCHEMA = "gd-dictionary-import/v1"
NAMESPACE = uuid.UUID("d7f8ca8b-f821-4f02-baa8-4778ca5f9067")


def _established_identity(entry):
    """Match storage identity; gender and other POS metadata do not split nouns."""
    kind = canonical_infinitive(entry.get("type"))
    if kind == "verb":
        return identity(entry)
    key = (kind, canonical_infinitive(entry.get("german")))
    if kind == "praeposition":
        key += (canonical_infinitive(entry.get("preposition_type")),
                canonical_infinitive(entry.get("case_type")))
    return key


def _assert_nonverb_identity(conn, entry, word_id):
    key = _established_identity(entry)
    for old in conn.execute("SELECT * FROM words"):
        candidate = dict(old)
        if candidate["id"] != word_id and _established_identity(candidate) == key:
            raise ValueError("existing import identity requires its original ID/mapping: " + candidate["id"])


def stable_id(entry):
    key = identity(entry) if entry.get("type") == "verb" else (entry.get("type"), entry.get("german"), entry.get("preposition_type"), entry.get("case_type"))
    return str(uuid.uuid5(NAMESPACE, json.dumps(key, ensure_ascii=False)))


def package_digest(package):
    return digest({k: v for k, v in package.items() if k != "digest"})


def round_trip(package):
    return json.loads(json.dumps(package, ensure_ascii=False, allow_nan=False))


def preflight(conn, package):
    if package.get("schema") != SCHEMA or package.get("digest") != package_digest(package):
        raise ValueError("invalid import schema/digest")
    if package.get("baseline_fingerprint") != _connection_fingerprint(conn):
        raise ValueError("import baseline changed")
    entries = package.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("empty import package")
    seen_ids, seen_identity, result = set(), {}, []
    for item in entries:
        fields = item["fields"]
        normalized = normalize_entry(fields)
        if normalized.get("type") == "verb" and normalized.get("infinitive", "").removeprefix("sich ") in {"erinnern", "unterhalten"}:
            raise ValueError("verb excluded from this import contract")
        wid = item.get("id")
        key = identity(normalized) if normalized.get("type") == "verb" else (normalized.get("type"), normalized.get("german"), normalized.get("preposition_type"), normalized.get("case_type"))
        established = _established_identity(normalized)
        if not wid or wid in seen_ids or item.get("identity") != list(key):
            raise ValueError("duplicate/invalid import identity or id")
        seen_ids.add(wid)
        source = item.get("source", {})
        if not source.get("provenance") or not re.fullmatch(r"[0-9a-f]{64}", source.get("sha256", "")):
            raise ValueError("import requires source provenance and SHA256")
        examples = item.get("examples")
        senses = item.get("senses")
        if not isinstance(senses, list) or not senses or not isinstance(examples, list) or len(examples) < 2:
            raise ValueError("import requires retained senses and at least two examples")
        sense_ids = {x["id"] for x in senses}
        covered = set()
        for example in examples:
            if not example.get("text") or example.get("sense_id") not in sense_ids or example.get("case") != normalized.get("reflexive_case") and normalized.get("type") == "verb":
                raise ValueError("example sense/case mismatch")
            covered.add(example["sense_id"])
        if normalized.get("type") == "praeposition":
            required_cases = ("akkusativ", "dativ") if normalized.get("case_type") == "wechsel" else (normalized.get("case_type"),)
            if any(sum(x.get("case") == case for x in examples) < 2 for case in required_cases):
                raise ValueError("preposition requires two examples for every governed case")
        if covered != sense_ids or fields.get("example") != "\n".join(x["text"] for x in examples):
            raise ValueError("missing/truncated retained examples")
        old = conn.execute("SELECT * FROM words WHERE id=?", (wid,)).fetchone()
        if old:
            if item.get("existing_id") != wid or item.get("expected_before_sha256") != digest(dict(old)):
                raise ValueError("existing import mapping/baseline mismatch")
        elif item.get("existing_id") is not None or wid != stable_id(normalized):
            raise ValueError("new import entry requires stable construction id")
        # Approved historical IDs can share a comparison key (for example
        # der/die See). Preserve their verified mappings without admitting a new
        # ID or a construction change into that occupied identity.
        preserved_mapping = old is not None and established == _established_identity(dict(old))
        if established in seen_identity and not (preserved_mapping and seen_identity[established]):
            raise ValueError("duplicate/invalid import identity or id")
        seen_identity[established] = preserved_mapping
        if normalized.get("type") == "verb":
            assert_identity(conn, normalized, exclude_id=wid)
        elif not preserved_mapping:
            _assert_nonverb_identity(conn, normalized, wid)
        result.append({**item, "fields": {**normalized, "id": wid}})
    return {"entries": result, "linguistic_gate": "separate approval required", "apply_allowed": False}
