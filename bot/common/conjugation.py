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

import random

# Canonical pronoun order. Each is a single whitespace-free token (the slashes
# in "er/sie/es" / "sie/Sie" are intentional — they are NOT word separators), so
# a stored line splits cleanly into (pronoun, form) on the first space.
PRONOUNS = ("ich", "du", "er/sie/es", "wir", "ihr", "sie/Sie")

# Personal pronouns offered by the 🎲 dice drill (spec: active recall — a pronoun
# pops up and the user recites the verb form from memory). Same six persons as
# PRONOUNS but with the "Sie/sie" spelling the owner asked the dice to show.
DICE_PRONOUNS = ("ich", "du", "er/sie/es", "wir", "ihr", "Sie/sie")


def pick_dice_pronoun(recent=None):
    """Return a random dice pronoun, excluding the recently shown ones.

    Pass the last two shown pronouns (`recent`): excluding both guarantees a
    pronoun never repeats more than once per 3 shows, so all six get covered
    without a near-immediate repeat. Accepts a single pronoun or an iterable.
    Falls back to the full set if exclusion would empty the pool.
    """
    if isinstance(recent, str):
        recent = (recent,)
    recent = set(recent or ())
    choices = [p for p in DICE_PRONOUNS if p not in recent] or list(DICE_PRONOUNS)
    return random.choice(choices)

# Placeholder shown in place of a hidden conjugated form (Feature B "hide forms").
_MASK = "***"

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


# Pronoun column width for the aligned monospace card: widest pronoun ("er/sie/es",
# 9 chars) + 1 padding space, so every form starts in the same column.
_PRONOUN_PAD = max(len(p) for p in PRONOUNS) + 1


def format_conjugation_block(stored_text, mask=False):
    """Render a stored block as vertically aligned plain text (no markup).

    Pronouns are left-padded to a fixed column so the forms line up; the DB holds
    only clean lines, so this alignment is applied purely at render time. Returns
    '' for an empty block. Tolerant of already-canonical DB text (does not
    re-validate — writes are validated by parse_conjugation).

    With ``mask=True`` every conjugated FORM is replaced by ``***`` while the
    pronoun column and alignment stay intact (Feature B "hide forms" — lets the
    user recite from memory). Empty forms stay empty so line count is preserved.
    """
    out = []
    for line in str(stored_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        pronoun = parts[0]
        form = parts[1] if len(parts) > 1 else ""
        if mask and form:
            form = _MASK
        out.append(f"{pronoun.ljust(_PRONOUN_PAD)}{form}")
    return "\n".join(out)
