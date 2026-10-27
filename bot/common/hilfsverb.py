"""Scalar Perfekt auxiliary: words.hilfsverb is NULL, 'hat', 'ist' or 'wechsel'.

Pure contract shared by storage writers, views and written checkers. Präteritum
and Partizip II stay in their own columns; alternatives pair positionally, so a
card never mixes forms of different paradigms (hängte/gehängt vs hing/gehangen).
NULL is an explicitly unknown legacy auxiliary and is never read as 'hat'.
"""

import re
from dataclasses import dataclass

from ..text_utils import normalize_answer
from .verb_construction import InvalidForms, REFLEXIVE_CASES, REFLEXIVE_PRONOUNS

FIELD = "hilfsverb"
# Retired schema21 column; only the v22 migration and old-package rejection read it.
LEGACY_FIELD = "perfekt_variants_json"
VALUES = ("hat", "ist", "wechsel")
_FINITE = {"hat": ("hat",), "ist": ("ist",), "wechsel": ("hat", "ist")}
_LABEL = {"hat": "hat", "ist": "ist", "wechsel": "hat/ist"}
_PERSONAL = {"sich", "mich", "dich", "mir", "dir", "uns", "euch"}
# Dictionary-only fields the guarded writer may change; construction needs repair.
PATCH_FIELDS = frozenset({FIELD, "prateritum", "partizip2", "example", "topic"})
CONSTRUCTION_FIELDS = ("type", "german", "infinitive", "reflexive_case")


class HilfsverbValidationError(InvalidForms):
    """Invalid auxiliary metadata is a data diagnostic, never a wrong answer."""


@dataclass(frozen=True)
class MatchRules:
    """Written-answer requirements fixed for one quiz/session launch.

    Both OFF keeps the previous optional forms: the infinitive with Partizip II
    is enough, and supplied Präteritum/auxiliary must still be correct.
    """
    prateritum: bool = False
    hilfsverb: bool = False


DEFAULT_RULES = MatchRules()


def validate_value(value):
    if value is None:
        return None
    if type(value) is not str or value not in VALUES:
        raise HilfsverbValidationError("invalid hilfsverb: " + repr(value))
    return value


def value(entry):
    return validate_value(entry.get(FIELD))


def alternatives(text):
    """Stored comma/semicolon/slash alternatives, multiword forms kept whole."""
    return list(dict.fromkeys(part.strip() for part in re.split(r"[,/;]", str(text or ""))
                              if part.strip()))


def form_pairs(entry, *, strict=None):
    """Coherent (Präteritum, Partizip II) alternatives of one card.

    Equal counts pair by position, a single form pairs with every alternative of
    the other column. Other shapes are ambiguous: rejected for a card with a
    known auxiliary, kept as the old cross product only for unknown legacy data.
    """
    prs, p2s = alternatives(entry.get("prateritum")), alternatives(entry.get("partizip2"))
    if not p2s:
        return ()
    if not prs:
        return tuple(("", p2) for p2 in p2s)
    if len(prs) == len(p2s):
        pairs = zip(prs, p2s)
    elif len(prs) == 1 or len(p2s) == 1:
        pairs = ((pr, p2) for pr in prs for p2 in p2s)
    else:
        if strict if strict is not None else entry.get(FIELD) is not None:
            raise HilfsverbValidationError("ambiguous Präteritum/Partizip II alternatives")
        pairs = ((pr, p2) for pr in prs for p2 in p2s)
    return tuple(dict.fromkeys(pairs))


def is_reflexive(entry):
    return entry.get("type") == "verb" and entry.get("reflexive_case") in REFLEXIVE_PRONOUNS


def fragment(entry, finite, partizip2):
    """Perfekt fragment keeps the reflexive pronoun: hat sich gewaschen."""
    return finite + (" sich " if is_reflexive(entry) else " ") + partizip2


def validate_entry(entry, *, allow_legacy_key=False):
    """Strict scalar contract for one complete candidate; returns the entry.

    Writers reject a leftover JSON value so a stale caller cannot lose it
    silently; readers of old frozen frames may ignore it (allow_legacy_key).
    """
    if not allow_legacy_key and entry.get(LEGACY_FIELD) is not None:
        raise HilfsverbValidationError("retired perfekt_variants_json")
    aux = value(entry)
    if aux is None:
        return entry
    if entry.get("type") != "verb":
        raise HilfsverbValidationError("hilfsverb requires type verb")
    if entry.get("reflexive_case") not in REFLEXIVE_CASES:
        # No auxiliary on an unconfirmed legacy construction (no NULL inference).
        raise HilfsverbValidationError("hilfsverb requires verified reflexive_case")
    pairs = form_pairs(entry, strict=True)
    if not pairs:
        raise HilfsverbValidationError("hilfsverb requires Partizip II")
    if aux == "wechsel" and len({p2 for _, p2 in pairs}) != 1:
        # Different Partizip II with different auxiliaries are separate cards.
        raise HilfsverbValidationError("wechsel requires one Partizip II")
    reflexive = is_reflexive(entry)
    for pr, p2 in pairs:
        if _PERSONAL.intersection(p2.lower().split()):
            raise HilfsverbValidationError("Partizip II contains a pronoun")
        found = [t for t in pr.lower().split() if t in _PERSONAL]
        if pr and found != (["sich"] if reflexive else []):
            raise HilfsverbValidationError("Präteritum reflexive pronoun mismatch")
    return entry


def normalize_text(text):
    """Commas, periods and whitespace are interchangeable answer separators."""
    return normalize_answer(re.sub(r"[,.\s]+", " ", str(text or "")))


def finite_forms(aux):
    return _FINITE[aux]


def _tails(entry, partizip2, rules):
    finite = _FINITE.get(value(entry), ())
    if rules.hilfsverb and not finite:
        # Required but unknown: a data diagnostic, never an accepted bare form.
        raise HilfsverbValidationError("required hilfsverb is unknown")
    tails = [fragment(entry, f, partizip2) for f in finite]
    if not rules.hilfsverb:
        tails.append(partizip2)
    return tails


def answer_candidates(entry, rules=DEFAULT_RULES):
    """(short, full) normalized complete answers, each from one coherent pair."""
    short, full = set(), set()
    infinitives = alternatives(entry.get("infinitive"))
    for pr, p2 in form_pairs(entry):
        if rules.prateritum and not pr:
            raise HilfsverbValidationError("required Präteritum is missing")
        for tail in _tails(entry, p2, rules):
            for infinitive in infinitives:
                if not rules.prateritum:
                    short.add(normalize_text(infinitive + " " + tail))
                if pr:
                    full.add(normalize_text(infinitive + " " + pr + " " + tail))
    return short, full


def answer_parts(entry, rules=DEFAULT_RULES):
    """Normalized Präteritum and Perfekt/Partizip tails for partial feedback."""
    pairs = form_pairs(entry)
    return ({normalize_text(pr) for pr, _ in pairs if pr},
            {normalize_text(tail) for _, p2 in pairs for tail in _tails(entry, p2, rules)})


def alias_allowed(target, alias, rules=DEFAULT_RULES):
    """A synonym cannot satisfy a required form the target itself demands."""
    if alias.get("type") != "verb":
        return False
    if rules.hilfsverb and value(target) is not None and value(alias) is None:
        return False
    if (rules.prateritum and alternatives(target.get("prateritum"))
            and not alternatives(alias.get("prateritum"))):
        return False
    return True


def label(entry):
    return _LABEL.get(value(entry), "")


def display_partizip2(entry, partizip2):
    aux = label(entry)
    return fragment(entry, aux, partizip2) if aux else partizip2


def display_pairs(entry):
    """One 'Präteritum, Perfekt' text per coherent pair, auxiliary shown once."""
    return tuple(", ".join(x for x in (pr, display_partizip2(entry, p2)) if x)
                 for pr, p2 in form_pairs(entry))


def apply_patch(conn, word_id, patch, *, expected_old, allowed=PATCH_FIELDS):
    """Guarded dictionary-only update in the caller's transaction; no commit.

    Every patched field and the construction must equal expected_old. The full
    old+patch candidate is validated before UPDATE. Returns True when written.
    """
    from .verb_construction import normalize_entry
    if not conn.in_transaction:
        raise HilfsverbValidationError("guarded patch requires caller transaction")
    cursor = conn.execute("SELECT * FROM words WHERE id=?", (word_id,))
    row = cursor.fetchone()
    if row is None:
        raise HilfsverbValidationError("patch target missing")
    old = dict(row) if hasattr(row, "keys") else dict(zip((d[0] for d in cursor.description), row))
    if not isinstance(patch, dict) or not patch or set(patch) - (set(old) & set(allowed)):
        raise HilfsverbValidationError("invalid patch fields")
    required = set(patch) | set(CONSTRUCTION_FIELDS)
    if not isinstance(expected_old, dict) or required - set(expected_old):
        raise HilfsverbValidationError("incomplete expected-old guard")
    if any(k not in old or old[k] != v for k, v in expected_old.items()):
        raise HilfsverbValidationError("expected-old mismatch: " + str(word_id))
    if old.get(FIELD) is not None and patch.get(FIELD, old[FIELD]) is None:
        raise HilfsverbValidationError("cannot discard known hilfsverb")
    candidate = {**old, **patch}
    if any(candidate.get(k) != old.get(k) for k in CONSTRUCTION_FIELDS):
        raise HilfsverbValidationError("construction change requires approved repair")
    try:
        # Validation only: stored legacy spelling is not rewritten by a patch.
        normalize_entry(candidate, allow_legacy=True)
    except ValueError as exc:
        raise HilfsverbValidationError(str(exc)) from exc
    validate_entry(candidate)
    keys = sorted(k for k in patch if candidate[k] != old[k])
    if not keys:
        return False
    conn.execute("UPDATE words SET " + ",".join(k + "=?" for k in keys)
                 + ",updated_at=CURRENT_TIMESTAMP WHERE id=?", [candidate[k] for k in keys] + [word_id])
    return True
