"""Pure quiz core: FSRS scoring and answer checking, without Telegram.

Split out of `handlers/quiz.py` verbatim so the mini app can reuse the very same
verdict and the very same progress update as the chat (miniapp.md §5.1). A second
implementation of either one would drift, and the user would be the one to find
out: the bot counts an answer, the app does not, and nobody can say which is right.

Nothing here touches PTB's `context` or a Telegram object — only plain values
(an entry dict, the answer text, a user id) and the data layer — which is what
makes these functions callable straight from an aiohttp request handler.
"""

import re

from ..app_config import get_db_path
from ..storage import QUIZ_RU_DE as MODE_RU_DE
from ..storage import get_synonym_group, log_quiz_event, update_progress
from ..text_utils import has_plural_form
from ..text_utils import normalize_answer as _normalize_answer
from ..text_utils import split_plural_forms
from ..ui.formatting import _article_matches, _expected_partizip2
from .lang import _mode_de_l1, _mode_pl_sg
from .txlog import _capture_transaction_snapshot, _log_transaction


# --- FSRS scoring ---

def _interval_divisor_for_mode(user_id, mode):
    """Handle interval divisor for mode."""
    if mode == MODE_RU_DE:
        return 1.65
    if mode == _mode_pl_sg(user_id):
        return 2.0
    if mode == _mode_de_l1(user_id):
        return 2.0
    return 1.0


def _update_srs_for_entry(user_id, entry, mode, ok):
    """Handle update srs for entry."""
    snapshot = _capture_transaction_snapshot(user_id, entry.get("id"), [mode])
    quality = 5 if ok else 2
    update_progress(
        get_db_path(),
        user_id=user_id,
        word_id=entry.get("id"),
        mode=mode,
        quality=quality,
        interval_divisor=_interval_divisor_for_mode(user_id, mode),
    )
    log_quiz_event(get_db_path(), user_id=user_id, word_id=entry.get("id"), mode=mode, is_correct=ok)
    _log_transaction(user_id, f"quiz:{mode}", snapshot)


# --- answer checking (Ru>De strict) ---

def _split_answer_parts(text):
    """Split a multi-field answer (nomen: article/noun/plural, verb: infinitive/
    prateritum/partizip2) into its fields.

    Commas, periods and whitespace are all treated as interchangeable separators:
    users routinely add stray punctuation — a comma or period after a space or
    right before the plural (e.g. "die Bankverbindung, Bankverbindungen") — and
    the answer must still parse into the same fields and validate correctly.
    """
    raw = str(text or "").strip()
    if not raw:
        return []
    return [p for p in re.split(r"[,.\s]+", raw) if p]


def _normalize_multi_field_answer(text):
    """Collapse punctuation and whitespace without guessing field boundaries."""
    return _normalize_answer(re.sub(r"[,.\s]+", " ", str(text or "")))


def _stored_form_variants(value):
    """Return normalized alternatives from a comma/slash/semicolon field."""
    return {
        normalized
        for part in re.split(r"[,/;]", str(value or ""))
        if (normalized := _normalize_answer(part))
    }


def _partizip2_variants(entry):
    """Expand stored Partizip II alternatives while preserving an auxiliary."""
    variants = set()
    for part in _stored_form_variants(entry.get("partizip2", "")):
        candidate = dict(entry)
        candidate["partizip2"] = part
        expected = _expected_partizip2(candidate)
        if expected:
            variants.add(expected)
    return variants


def _verb_answer_candidates(entry):
    """Build complete short/full answers without splitting multi-word forms."""
    infinitives = _stored_form_variants(entry.get("infinitive", ""))
    prateritums = _stored_form_variants(entry.get("prateritum", ""))
    partizip2s = _partizip2_variants(entry)
    short = {
        _normalize_multi_field_answer(f"{infinitive} {partizip2}")
        for infinitive in infinitives
        for partizip2 in partizip2s
    }
    full = {
        _normalize_multi_field_answer(f"{infinitive} {prateritum} {partizip2}")
        for infinitive in infinitives
        for prateritum in prateritums
        for partizip2 in partizip2s
    }
    return short, full


def _normalize_plural_value(value):
    """Handle normalize plural value."""
    token = _normalize_answer(value)
    if token.startswith("die "):
        token = token[4:].strip()
    return token


def _plural_form_set(stored):
    """Normalized set of accepted plural forms (a noun may have several)."""
    forms = {_normalize_plural_value(p) for p in split_plural_forms(stored)}
    forms = {f for f in forms if f}
    return forms or {_normalize_plural_value(stored)}


def _plural_matches(answer, stored):
    """True if the normalized answer equals any of the stored plural forms."""
    return _normalize_plural_value(answer) in _plural_form_set(stored)


def _synonym_entries(entry):
    """Handle synonym entries."""
    wid = entry.get("id")
    if not wid:
        return [entry]
    group = get_synonym_group(get_db_path(), wid)
    if not group:
        return [entry]
    return group


def _check_ru_de_strict(entry, answer_text):
    """Handle check ru de strict."""
    entry_type = (entry.get("type") or "").strip().lower()
    parts = _split_answer_parts(answer_text)
    synonyms = _synonym_entries(entry)

    if entry_type == "nomen":
        if len(parts) < 2:
            checks = {"article": False}
            if has_plural_form(entry.get("plural", "")):
                checks["plural"] = False
            return False, checks
        article_in = _normalize_answer(parts[0])
        german_in = _normalize_answer(parts[1])
        plural_in = _normalize_plural_value(" ".join(parts[2:])) if len(parts) >= 3 else german_in
        for cand in synonyms:
            if (cand.get("type") or "").strip().lower() != "nomen":
                continue
            article_ok = _article_matches(article_in, cand.get("article", ""))
            german_ok = german_in == _normalize_answer(cand.get("german", ""))
            has_plural = has_plural_form(cand.get("plural", ""))
            plural_ok = (
                plural_in in _plural_form_set(cand.get("plural", ""))
                if has_plural
                else len(parts) == 2
            )
            if article_ok and german_ok and plural_ok:
                checks = {"article": True}
                if has_plural:
                    checks["plural"] = True
                return True, checks
        checks = {"article": _article_matches(article_in, entry.get("article", ""))}
        if has_plural_form(entry.get("plural", "")):
            checks["plural"] = plural_in in _plural_form_set(entry.get("plural", ""))
        return False, checks

    if entry_type == "verb":
        answer = _normalize_multi_field_answer(answer_text)
        if not answer:
            return False, {"partizip2": False, "prateritum": False}
        for cand in synonyms:
            if (cand.get("type") or "").strip().lower() != "verb":
                continue
            short, full = _verb_answer_candidates(cand)
            if answer in full:
                return True, {"partizip2": True, "prateritum": True}
            if answer in short:
                return True, {"partizip2": True, "prateritum": False}

        partizip2s = _partizip2_variants(entry)
        prateritums = _stored_form_variants(entry.get("prateritum", ""))
        p2_ok = any(answer == value or answer.endswith(f" {value}") for value in partizip2s)
        pr_ok = any(
            answer == value
            or answer.startswith(f"{value} ")
            or answer.endswith(f" {value}")
            or f" {value} " in answer
            for value in prateritums
        )
        return False, {
            "partizip2": p2_ok,
            "prateritum": pr_ok,
        }

    # single-field answer (adjective/adverb/…). An adjective may be answered with
    # the base form alone, or base+komparativ, or base+komparativ+superlativ — in
    # that order, the extra degrees optional (bug #26). Commas, periods, newlines
    # and stray whitespace are all interchangeable separators, and the superlativ
    # itself carries a space ("am leisesten"), so instead of re-splitting the
    # answer into fields we compare the fully-collapsed answer against the equally
    # collapsed expected prefixes (base / base+komp / base+komp+super).
    def _collapse(value):
        return _normalize_answer(re.sub(r"[,.]+", " ", str(value or "")))

    answer = _collapse(answer_text)
    expected_values = set()
    for cand in synonyms:
        ctype = (cand.get("type") or "").strip().lower()
        if ctype == "verb":
            expected_values.add(_normalize_answer(cand.get("infinitive", "")))
            continue
        forms = [cand.get("german", "")]
        if ctype == "adjektiv":
            forms += [cand.get("komparativ", ""), cand.get("superlativ", "")]
        forms = [f for f in forms if str(f).strip()]
        for n in range(1, len(forms) + 1):
            expected_values.add(_collapse(" ".join(forms[:n])))
    return answer in expected_values, {}
