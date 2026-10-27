"""Pure construction validation and connection-scoped identity checks."""

import re

from .conjugation import parse_conjugation

REFLEXIVE_CASES = ("none", "akkusativ", "dativ")
REFLEXIVE_PRONOUNS = {
    "akkusativ": ("mich", "dich", "sich", "uns", "euch", "sich"),
    "dativ": ("mir", "dir", "sich", "uns", "euch", "sich"),
}


class InvalidClassification(ValueError):
    pass


class InvalidForms(ValueError):
    pass


class DuplicateIdentity(ValueError):
    pass


class LegacyIdentityCollision(DuplicateIdentity):
    pass


def canonical_infinitive(value):
    return " ".join(str(value or "").split()).lower()


def identity(entry):
    return ("verb", canonical_infinitive(entry.get("infinitive")), entry.get("reflexive_case"))


def normalize_entry(entry, *, allow_legacy=False):
    """Validate complete candidate; empty reference blocks remain permissible."""
    result = dict(entry)
    case = result.get("reflexive_case")
    if case is not None and case not in REFLEXIVE_CASES:
        raise InvalidClassification("invalid reflexive_case")
    if result.get("type") != "verb":
        if case is not None:
            raise InvalidClassification("reflexive_case requires type verb")
        result["reflexive_case"] = None
        return result
    if case is None and not allow_legacy:
        raise InvalidClassification("new verb requires verified reflexive_case")
    infinitive = canonical_infinitive(result.get("infinitive"))
    if case is not None:
        if not infinitive:
            raise InvalidClassification("verb requires infinitive")
        tokens = infinitive.split()
        if case in REFLEXIVE_PRONOUNS:
            if len(tokens) < 2 or tokens[0] != "sich" or tokens.count("sich") != 1 or any(token in {"mich","dich","mir","dir","uns","euch"} for token in tokens[1:]):
                raise InvalidClassification("reflexive infinitive requires one initial sich")
        elif any(token in {"sich", "mich", "dich", "mir", "dir", "uns", "euch"} for token in tokens):
            raise InvalidClassification("ordinary infinitive has reflexive pronoun")
        german = canonical_infinitive(result.get("german"))
        if german and german != infinitive:
            raise InvalidClassification("german and infinitive disagree")
    result["infinitive"], result["reflexive_case"] = infinitive, case
    if result.get("german"):
        result["german"] = canonical_infinitive(result["german"])
    for field in ("conjugation_praesens", "conjugation_praeteritum"):
        raw = result.get(field, "")
        try:
            block = parse_conjugation(raw) if str(raw or "").strip() else ""
        except ValueError as exc:
            raise InvalidForms(str(exc)) from exc
        if block:
            validate_conjugation(result, block)
        result[field] = block
    return result


def identity_candidates(conn, infinitive):
    """Return all canonical candidates, including unresolved legacy rows."""
    canonical = canonical_infinitive(infinitive)
    cursor = conn.execute("SELECT * FROM words WHERE type='verb'")
    columns = [d[0] for d in cursor.description]
    result = []
    for row in cursor:
        entry = dict(zip(columns, row))
        if canonical_infinitive(entry["infinitive"]) == canonical:
            result.append(entry)
    return result


def assert_identity(conn, entry, exclude_id=None):
    for old in identity_candidates(conn, entry.get("infinitive")):
        if old["id"] == exclude_id:
            continue
        if old.get("reflexive_case") is None or entry.get("reflexive_case") is None:
            raise LegacyIdentityCollision("unresolved legacy identity: " + old["id"])
        if identity(old) == identity(entry):
            raise DuplicateIdentity("duplicate verb identity: " + old["id"])


def display_infinitive(entry):
    """Prefer the authoritative infinitive over a stale legacy German label."""
    return str(entry.get("infinitive") or entry.get("german") or "").strip()


def construction_label(entry):
    case = entry.get("reflexive_case")
    if entry.get("type") == "verb" and case in REFLEXIVE_PRONOUNS:
        return "verb-reflexive, " + {"akkusativ": "AKK", "dativ": "DATIV"}[case]
    return str(entry.get("type") or "").strip()


def form_alternatives(value):
    """Split form alternatives only after the subject has been removed."""
    return [part.strip() for part in re.split(r"\s*(?:[,;/]|\s+oder\s+)\s*", str(value or ""))]


def validate_conjugation(entry, text):
    """Validate each complete alternative without removing its particles."""
    block = parse_conjugation(text)
    case = entry.get("reflexive_case")
    if case is not None and case not in REFLEXIVE_CASES:
        raise InvalidClassification("invalid reflexive_case")
    reflexives = {"mich", "dich", "sich", "mir", "dir", "uns", "euch"}
    subjects = {"ich", "du", "er", "sie", "es", "wir", "ihr"}
    if case in REFLEXIVE_PRONOUNS:
        inf = canonical_infinitive(entry.get("infinitive"))
        if not inf.startswith("sich ") or inf.split().count("sich") != 1 or any(token in reflexives - {"sich"} for token in inf.split()[1:]):
            raise InvalidClassification("reflexive infinitive requires one initial sich")
    elif case == "none" and any(token in reflexives for token in canonical_infinitive(entry.get("infinitive")).split()):
        raise InvalidClassification("ordinary infinitive has reflexive pronoun")
    for index, line in enumerate(block.splitlines()):
        for alternative in form_alternatives(line.split(None, 1)[1]):
            tokens = re.findall(r"\w+", alternative.lower())
            if not tokens:
                raise InvalidForms("empty form alternative")
            if case is None:
                # Legacy ordinary reference tables retain their prior contract.
                continue
            found = [token for token in tokens if token in reflexives]
            expected = [REFLEXIVE_PRONOUNS[case][index]] if case in REFLEXIVE_PRONOUNS else []
            if found != expected or tokens[0] in reflexives or any(t in subjects for t in tokens):
                raise InvalidForms("incorrect personal reflexive pronoun or subject")
    return block


def synonym_compatible(target, candidate):
    """Unknown classification cannot prove compatibility with a reflexive verb."""
    cases = (target.get("reflexive_case"), candidate.get("reflexive_case"))
    if not any(case in REFLEXIVE_PRONOUNS for case in cases):
        # Explicit personal/reflexive tokens in an unresolved legacy infinitive
        # are not evidence that it is an ordinary construction.
        personal = {"sich", "mich", "dich", "mir", "dir", "uns", "euch"}
        for entry in (target, candidate):
            if entry.get('type') == 'verb' and entry.get('reflexive_case') is None:
                if any(token in personal for token in canonical_infinitive(entry.get('infinitive')).split()):
                    return False
        return True
    if target.get("type") != "verb" or candidate.get("type") != "verb" or cases[0] != cases[1]:
        return False
    try:
        for entry in (target, candidate):
            # Ignore obsolete display metadata; validate the actual construction.
            normalize_entry({**entry, "german": display_infinitive(entry)})
    except ValueError:
        return False
    return True


def validate_quiz_entry(entry):
    """Invalid metadata is a data diagnostic, never a learner's wrong answer."""
    if entry.get('type') != 'verb':
        return
    if unverified_reflexive(entry):
        raise InvalidClassification('unverified legacy reflexive construction')
    normalize_entry({**entry, 'german': display_infinitive(entry)}, allow_legacy=True)
    # Import lazily: the scalar auxiliary contract uses construction validation.
    # Old frozen frames may still carry retired JSON; it is ignored, not read.
    from .hilfsverb import validate_entry
    validate_entry(entry, allow_legacy_key=True)


def unverified_reflexive(entry):
    return (entry.get('type') == 'verb' and entry.get('reflexive_case') is None
            and any(token in {'sich','mich','dich','mir','dir','uns','euch'}
                    for token in canonical_infinitive(entry.get('infinitive')).split()))


def reflexive_hint(entry):
    return " (" + construction_label(entry) + ")" if entry.get('type') == 'verb' and entry.get('reflexive_case') in REFLEXIVE_PRONOUNS else ""
