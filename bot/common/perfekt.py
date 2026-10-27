"""Retired schema21 linked Perfekt JSON (perfekt_variants_json): legacy only.

Used by the schema22 migration to convert stored values to the scalar
words.hilfsverb, and by retired schema21 package tooling. Runtime DTOs, views,
writers and checkers use common.hilfsverb instead.
"""

import json

from . import verb_construction as construction

FIELD = "perfekt_variants_json"
SCALAR_FIELDS = ("prateritum", "partizip2")
CONSTRUCTION_FIELDS = ("type", "german", "infinitive", "reflexive_case")


class PerfektValidationError(ValueError):
    """Invalid annotation is a diagnostic, never a legacy reference."""


def _text(value, label, *, form=False):
    if not isinstance(value, str) or not value.strip():
        raise PerfektValidationError("missing/invalid " + label)
    if form and value != construction.canonical_infinitive(value):
        raise PerfektValidationError("noncanonical " + label)
    return value


def scalar_projection(variants):
    """Keep distinct complete forms in declared variant order."""
    return {k: ", ".join(dict.fromkeys(v[k] for v in variants)) for k in SCALAR_FIELDS}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PerfektValidationError("duplicate Perfekt JSON key: " + key)
        result[key] = value
    return result


def normalize_payload(payload, entry):
    """Validate linked forms; accept historical example metadata while migrating it away."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload, object_pairs_hook=_unique_object)
        except ValueError as exc:
            raise PerfektValidationError("invalid Perfekt JSON") from exc
    if (not isinstance(payload, dict) or set(payload) != {"version", "status", "variants"}
            or type(payload.get("version")) is not int or payload["version"] != 1
            or payload.get("status") != "verified"):
        raise PerfektValidationError("unsupported Perfekt version/status/envelope")
    variants = payload["variants"]
    if not isinstance(variants, list) or not variants:
        raise PerfektValidationError("verified Perfekt requires variants")
    try:
        candidate = construction.normalize_entry(entry)
    except ValueError as exc:
        raise PerfektValidationError(str(exc)) from exc
    if candidate.get("type") != "verb" or construction.unverified_reflexive(candidate):
        raise PerfektValidationError("Perfekt requires a verified verb construction")
    lemma = construction.canonical_infinitive(candidate.get("infinitive"))
    if not lemma:
        raise PerfektValidationError("Perfekt requires infinitive")
    case, ids = candidate.get("reflexive_case"), set()
    pronouns = {"sich", "mich", "dich", "mir", "dir", "uns", "euch"}
    for variant in variants:
        if not isinstance(variant, dict):
            raise PerfektValidationError("invalid Perfekt variant")
        for field in ("variant_id", "sense_id", "sense_ru"):
            _text(variant.get(field), field)
        if variant["variant_id"] in ids:
            raise PerfektValidationError("duplicate variant_id")
        ids.add(variant["variant_id"])
        vc = variant.get("reflexive_case")
        if vc not in construction.REFLEXIVE_CASES or vc != case:
            raise PerfektValidationError("variant construction disagrees with entry")
        if _text(variant.get("lemma_for_answer"), "lemma_for_answer", form=True) != lemma:
            raise PerfektValidationError("variant lemma disagrees with entry")
        scope = variant.get("person_scope")
        if scope not in ("third_singular_reference", "third_plural_reciprocal_reference"):
            raise PerfektValidationError("unsupported person_scope")
        plural = scope == "third_plural_reciprocal_reference"
        if plural and vc == "none":
            raise PerfektValidationError("reciprocal reference requires reflexive construction")
        aux = variant.get("auxiliary")
        if not isinstance(aux, str):
            raise PerfektValidationError("invalid auxiliary")
        finite = {"haben": "haben" if plural else "hat", "sein": "sind" if plural else "ist"}.get(aux)
        if finite is None or variant.get("finite_auxiliary") != finite:
            raise PerfektValidationError("auxiliary/person mismatch")
        for field in ("prateritum", "partizip2", "perfekt_fragment"):
            _text(variant.get(field), field, form=True)
        reflexive = vc in construction.REFLEXIVE_PRONOUNS
        expected = finite + (" sich " if reflexive else " ") + variant["partizip2"]
        if variant["perfekt_fragment"] != expected:
            raise PerfektValidationError("Perfekt fragment disagrees with linked forms")
        found = [t for t in variant["prateritum"].split() if t in pronouns]
        if found != (["sich"] if reflexive else []):
            raise PerfektValidationError("Prateritum reflexive reference mismatch")
        if pronouns.intersection(variant["partizip2"].split()):
            raise PerfektValidationError("Partizip complement contains personal pronoun")
        kind = variant.get("partizip2_kind", "participle")
        if kind not in ("participle", "passive_perfekt_complement", "ersatzinfinitiv_construction_complement"):
            raise PerfektValidationError("unsupported Partizip complement kind")
        if kind == "passive_perfekt_complement" and (aux != "sein" or not variant["partizip2"].endswith(" worden")):
            raise PerfektValidationError("invalid passive Perfekt complement")
        if kind == "ersatzinfinitiv_construction_complement" and (aux != "haben" or not variant["partizip2"].endswith(" lassen")):
            raise PerfektValidationError("invalid Ersatzinfinitiv complement")
        for field, value in (("short_answer", lemma + " " + expected),
                             ("full_answer", lemma + " " + variant["prateritum"] + " " + expected)):
            if field in variant and variant[field] != value:
                raise PerfektValidationError(field + " disagrees with linked forms")
        if "examples" in variant:
            examples = variant["examples"]
            if not isinstance(examples, list) or not examples:
                raise PerfektValidationError("invalid variant examples")
            for example in examples:
                if not isinstance(example, dict):
                    raise PerfektValidationError("invalid variant example")
                _text(example.get("text_de"), "example.text_de")
        if not any(variant.get(k) for k in ("source_urls", "source_pointers", "evidence_pointer", "evidence")):
            raise PerfektValidationError("variant requires evidence")
        for field in ("source_urls", "source_pointers"):
            if field in variant:
                values = variant[field]
                if not isinstance(values, list) or not values:
                    raise PerfektValidationError("invalid " + field)
                for value in values:
                    _text(value, field)
        if "evidence_pointer" in variant:
            _text(variant["evidence_pointer"], "evidence_pointer")
    for field, value in scalar_projection(variants).items():
        if candidate.get(field) != value:
            raise PerfektValidationError("stale scalar projection: " + field)
    try:
        return json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    except (ValueError, TypeError) as exc:
        raise PerfektValidationError("non-JSON Perfekt metadata") from exc


def serialize_payload(payload, entry):
    return json.dumps(normalize_payload(payload, entry), ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"), allow_nan=False)


def read_variants(entry):
    raw = entry.get(FIELD)
    return None if raw is None else tuple(normalize_payload(raw, entry)["variants"])


def answer_tuples(entry):
    variants = read_variants(entry)
    return None if variants is None else tuple(dict.fromkeys(
        (v["lemma_for_answer"], v["prateritum"], v["perfekt_fragment"]) for v in variants))


def legacy_hilfsverb(entry):
    """Convert one validated schema21 value to hat/ist/wechsel, never guessing.

    Reciprocal plural haben/sind is the same auxiliary (hat/ist). Two
    auxiliaries become wechsel only with one shared Partizip II; otherwise the
    card needs separate usage cards and the value is unsupported. The scalar
    columns must already pair exactly like the linked variants.
    """
    from . import hilfsverb
    variants = read_variants(entry)
    if variants is None:
        raise PerfektValidationError("missing legacy Perfekt value")
    auxiliaries = {v["auxiliary"] for v in variants}
    if auxiliaries == {"haben"}:
        value = "hat"
    elif auxiliaries == {"sein"}:
        value = "ist"
    elif len({v["partizip2"] for v in variants}) == 1:
        value = "wechsel"
    else:
        raise PerfektValidationError("different Partizip II with different auxiliaries need separate cards")
    candidate = {**entry, FIELD: None, hilfsverb.FIELD: value}
    try:
        pairs = set(hilfsverb.form_pairs(candidate, strict=True))
        hilfsverb.validate_entry(candidate)
    except ValueError as exc:
        raise PerfektValidationError(str(exc)) from exc
    if pairs != {(v["prateritum"], v["partizip2"]) for v in variants}:
        raise PerfektValidationError("scalar forms would combine unlinked Präteritum/Partizip II")
    # One scalar auxiliary applies to every pair: wechsel needs each (Präteritum,
    # Partizip II) linked with both hat and ist, not just one auxiliary each.
    finite = {"haben": "hat", "sein": "ist"}
    expected = {(pr, p2, f) for pr, p2 in pairs for f in hilfsverb.finite_forms(value)}
    if expected != {(v["prateritum"], v["partizip2"], finite[v["auxiliary"]]) for v in variants}:
        raise PerfektValidationError("auxiliary is not linked with every Präteritum/Partizip II pair")
    return value


def validate_entry(entry):
    result = dict(entry)
    if result.get(FIELD) is not None:
        result[FIELD] = serialize_payload(result[FIELD], result)
    return result


def apply_metadata_patch(conn, word_id, patch, *, expected_old):
    """Guard dictionary-only writes; no commit, reset or implicit classification."""
    if not conn.in_transaction:
        raise PerfektValidationError("metadata patch requires caller transaction")
    cursor = conn.execute("SELECT * FROM words WHERE id=?", (word_id,))
    row = cursor.fetchone()
    if row is None:
        raise PerfektValidationError("metadata target missing")
    old = dict(row) if hasattr(row, "keys") else dict(zip((d[0] for d in cursor.description), row))
    allowed = {FIELD, *SCALAR_FIELDS, "russian", "ukrainian", "english", "image", "example", "topic",
               "cefr_level", "vf_checked_at", "is_separable", "conjugation_praesens", "conjugation_praeteritum"}
    if not isinstance(patch, dict) or not patch or set(patch) - (set(old) & allowed):
        raise PerfektValidationError("invalid metadata patch fields")
    required = set(patch) | set(CONSTRUCTION_FIELDS) | set(SCALAR_FIELDS) | {FIELD}
    if not isinstance(expected_old, dict) or required - set(expected_old):
        raise PerfektValidationError("incomplete expected-old guard")
    if any(k not in old or old[k] != v for k, v in expected_old.items()):
        raise PerfektValidationError("metadata expected-old mismatch")
    candidate = {**old, **patch}
    if any(candidate.get(k) != old.get(k) for k in CONSTRUCTION_FIELDS):
        raise PerfektValidationError("construction change requires approved repair/confirmation")
    if old.get(FIELD) is not None and candidate.get(FIELD) is None:
        raise PerfektValidationError("cannot discard annotated Perfekt")
    try:
        candidate = construction.normalize_entry(candidate, allow_legacy=True)
    except ValueError as exc:
        raise PerfektValidationError(str(exc)) from exc
    if (candidate.get("is_separable") is not None
            and (type(candidate["is_separable"]) is not int or candidate["is_separable"] not in (0, 1))):
        raise PerfektValidationError("invalid is_separable")
    if candidate.get("cefr_level", "") not in ("", "A1", "A2", "B1", "B2", "C1", "C2"):
        raise PerfektValidationError("invalid cefr_level")
    candidate = validate_entry(candidate)
    keys = sorted(k for k in patch if candidate[k] != old[k])
    if not keys:
        return False
    conn.execute("UPDATE words SET " + ",".join(k + "=?" for k in keys)
                 + ",updated_at=CURRENT_TIMESTAMP WHERE id=?", [candidate[k] for k in keys] + [word_id])
    return True
