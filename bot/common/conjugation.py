"""Conjugation domain: canonical format + validation (spec #3).

A verb's conjugation is stored as a plain 6-line block (one line per pronoun,
in a fixed order). This module is the single source of truth for that format so
that every write path — AI ingest, verbformen.de parsing, the future backfill
script, a possible clarify-edit button — validates identically. Keeping the
stored text strictly canonical means a later active quiz over individual forms
can split the lines for free.

Rendering (aligned monospace card) lives alongside in this module; the DB only
ever holds clean lines, never alignment or markup.
"""

# Canonical pronoun order. Each is a single whitespace-free token (the slashes
# in "er/sie/es" / "sie/Sie" are intentional — they are NOT word separators), so
# a stored line splits cleanly into (pronoun, form) on the first space.
PRONOUNS = ("ich", "du", "er/sie/es", "wir", "ihr", "sie/Sie")

# Präteritum is only stored for a closed set of verbs (spec #3): the six modals
# plus werden/sein/haben. Matched by infinitive; no schema flag needed.
PRAETERITUM_INFINITIVES = frozenset(
    {"können", "müssen", "wollen", "sollen", "dürfen", "mögen", "werden", "sein", "haben"}
)


def takes_praeteritum(infinitive):
    """True if this verb belongs to the closed Präteritum list (by infinitive)."""
    return str(infinitive or "").strip().lower() in PRAETERITUM_INFINITIVES


def _expected_format_hint():
    """Human-readable reminder of the canonical shape, shown on rejection."""
    return "Expected exactly 6 lines, each starting with its pronoun in order:\n" + "\n".join(
        f"{p} <form>" for p in PRONOUNS
    )


def parse_conjugation(text):
    """Validate a conjugation block and return its canonical stored form.

    Accepts the 6 canonical lines (order ich / du / er/sie/es / wir / ihr /
    sie/Sie), each beginning with its pronoun followed by a non-empty form.
    Surrounding blank lines and per-line trailing whitespace are tolerated and
    stripped. Returns the cleaned "\\n"-joined 6-line string.

    Raises ValueError (with the expected format) on anything else: wrong line
    count, blank form, or a pronoun out of order / missing. One validator for
    every write path — see module docstring.
    """
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    if len(lines) != len(PRONOUNS):
        raise ValueError(
            f"Conjugation must have {len(PRONOUNS)} non-empty lines, got {len(lines)}.\n"
            + _expected_format_hint()
        )
    cleaned = []
    for pronoun, line in zip(PRONOUNS, lines):
        parts = line.split(None, 1)
        if parts[0] != pronoun:
            raise ValueError(
                f"Expected pronoun '{pronoun}', got '{parts[0]}'.\n" + _expected_format_hint()
            )
        if len(parts) < 2 or not parts[1].strip():
            raise ValueError(
                f"Missing form after '{pronoun}'.\n" + _expected_format_hint()
            )
        # Re-join with a single space so stored text is canonical regardless of
        # how much whitespace the source put between pronoun and form.
        cleaned.append(f"{pronoun} {parts[1].strip()}")
    return "\n".join(cleaned)


def is_valid_conjugation(text):
    """True if ``text`` is a valid canonical block (never raises)."""
    try:
        parse_conjugation(text)
        return True
    except ValueError:
        return False
