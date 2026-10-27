"""Pure six-form parsing, eligibility and pass reduction."""

import re
from datetime import date

from ..common.conjugation import PRONOUNS, parse_conjugation, takes_praeteritum
from ..common.verb_construction import validate_conjugation, form_alternatives, unverified_reflexive
from ..common.hilfsverb import DEFAULT_RULES
from ..study.srs import update_fsrs
from ..text_utils import normalize_answer

PAIRS = ('ru_de', 'uk_de', 'en_de')
TENSES = ('praesens', 'praeteritum')
PERSONAL_PRONOUNS = frozenset(('ich', 'du', 'er', 'sie', 'es', 'wir', 'ihr', 'er/sie/es', 'sie/sie'))


class StaleAction(ValueError):
    """An action no longer belongs to an eligible live block or pass."""


def forms_for(entry, tense, rules=DEFAULT_RULES):
    """Return six complete canonical forms; invalid/missing text is ineligible."""
    if tense not in TENSES or unverified_reflexive(entry):
        return None
    if tense == 'praeteritum' and not rules.prateritum and not takes_praeteritum(entry.get('infinitive')):
        return None
    try:
        canonical = validate_conjugation(entry, entry.get('conjugation_' + tense, ''))
    except ValueError:
        return None
    return {p: line.split(None, 1)[1] for p, line in zip(PRONOUNS, canonical.splitlines())}


def eligible_word(entry):
    """Unknown separability is not a confirmed non-separable verb."""
    if not entry or entry.get('type') != 'verb' or entry.get('is_separable') != 0:
        return False
    if unverified_reflexive(entry):
        return False
    return True


def check_form(text, pronoun, expected):
    """Accept the expected form, optionally prefixed by this group's pronoun."""
    value = normalize_answer(str(text or ''))
    allowed_pronouns = set(pronoun.lower().split('/')) | {pronoun.lower()}
    pieces = value.split(' ', 1)
    if pieces[0] in PERSONAL_PRONOUNS:
        if len(pieces) != 2 or pieces[0] not in allowed_pronouns:
            return False
        value = pieces[1]
    # The existing blocks may explicitly list alternative forms. Do not invent
    # root matching or strip an arbitrary first token from a multiword form.
    alternatives = form_alternatives(expected)
    return bool(value) and value in {normalize_answer(x) for x in alternatives if x.strip()}


def reduce_pass(baseline, answers, previous_result, previous_day, today):
    """Recompute one effective result from baseline, never compound penalties."""
    wrong = any(not bool(a['is_correct']) for a in answers)
    result = 'wrong' if wrong else ('correct' if len(answers) == 6 else None)
    if result is None:
        return dict(baseline), None, None
    result_day = previous_day if result == previous_result and previous_day else today.isoformat()
    updated = update_fsrs(
        baseline, 2 if result == 'wrong' else 5,
        today=date.fromisoformat(result_day), interval_divisor=1.0,
    )
    return updated, result, result_day
