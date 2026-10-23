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
            if len(tokens) < 2 or tokens[0] != "sich" or tokens.count("sich") != 1:
                raise InvalidClassification("reflexive infinitive requires one initial sich")
        elif tokens[0] in {"sich", "mich", "dich", "mir", "dir"}:
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
        if block and case in REFLEXIVE_PRONOUNS:
            for line, expected in zip(block.splitlines(), REFLEXIVE_PRONOUNS[case]):
                tokens = re.findall(r"\w+", line.split(None, 1)[1].lower())
                found = [t for t in tokens if t in {"mich", "dich", "sich", "mir", "dir", "uns", "euch"}]
                if found != [expected] or not tokens or tokens[0] == expected:
                    raise InvalidForms("incorrect personal reflexive pronoun")
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
