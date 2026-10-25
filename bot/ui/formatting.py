"""Display formatters for quiz questions/feedback, word cards, and shared helpers.

Leaf module: depends only on text_utils/storage/app_config (no handler back-refs).
"""

from ..app_config import get_db_path, is_owner
from ..common.bot_media import resolve_bot_word_media
from ..common.lang import _user_dict_lang
from ..common.translation_view import translation_card_lines
from ..common.conjugation import format_conjugation_block
from ..common.verb_construction import display_infinitive, construction_label, reflexive_hint, validate_quiz_entry
from ..i18n import t
from ..storage import get_word_study_status
from ..text_utils import cap_plural as _cap_plural
from ..text_utils import cap_word as _cap_word
from ..text_utils import has_plural_form as _has_plural_form
from ..text_utils import low_word as _low_word
from ..text_utils import md_escape as _md_escape
from ..text_utils import normalize_answer as _normalize_answer
from ..text_utils import normalize_russian_singular as _normalize_russian_singular


def _expected_partizip2(entry):
    """Handle expected partizip2."""
    from ..common.perfekt import display_forms
    if entry.get("perfekt_variants_json") is not None:
        validate_quiz_entry(entry)
    linked = display_forms(entry)
    if linked is not None:
        return ", ".join(dict.fromkeys(perf for _, perf in linked))
    part = _normalize_answer(entry.get("partizip2", ""))
    aux = _normalize_answer(
        entry.get("perfect_aux")
        or entry.get("hilfsverb")
        or entry.get("aux")
        or ""
    )
    if aux in {"hat", "ist"}:
        if part.startswith("hat ") or part.startswith("ist "):
            return part
        return f"{aux} {part}".strip()
    return part


def _linked_verb_lines(entry, *, examples_already_shown=()):
    """Render each reference with its own examples; never join scalar lists."""
    from ..common.perfekt import read_variants
    if entry.get("perfekt_variants_json") is None:
        return None
    validate_quiz_entry(entry)
    variants = read_variants(entry)
    if variants is None:
        return None
    lines = []
    shown = set(examples_already_shown)
    for variant in variants:
        lines.append(variant["prateritum"] + ", " + variant["perfekt_fragment"])
        lines.extend(example["text_de"] for example in variant["examples"]
                     if example["text_de"] not in shown)
    return lines


def _append_verb_forms(lines, entry, *, examples_already_shown=()):
    linked = _linked_verb_lines(entry, examples_already_shown=examples_already_shown)
    if linked is not None:
        lines.extend("_  " + _md_escape(value) + "_" for value in linked)
    else:
        for value in (_low_word(entry.get("prateritum", "")), _expected_partizip2(entry)):
            if value:
                lines.append("_  " + _md_escape(value) + "_")
    return lines


def _entry_translation(entry, dict_lang):
    """Translation shown to the user, in their dict_lang.

    spec #7: a guest's personal override (entry['my_translation'], set by the
    Telegram media resolver for their dict_lang) wins over the shared dictionary.
    Falls back to the base russian/ukrainian/english fields when unset.
    """
    personal = str(entry.get("my_translation", "") or "").strip()
    if personal:
        return personal
    ru = str(entry.get("russian", "") or "").strip()
    ukr = str(entry.get("ukrainian", "") or "").strip()
    eng = str(entry.get("english", "") or "").strip()
    if dict_lang == "uk":
        return ukr or ru
    if dict_lang == "en":
        return eng or ru
    return ru


# spec #1: how a preposition's governed case is shown PASSIVELY in a question
# (the learner types only the word — the case is a hint, never asked). Wechsel gets
# a compact two-way label. Unknown/empty -> no label.
_CASE_LABELS = {
    "dativ": "+Dativ",
    "akkusativ": "+Akkusativ",
    "genitiv": "+Genitiv",
    "wechsel": "+D/A (Wechsel)",
}


def _case_label(case_type):
    """Passive case hint for a preposition question, or '' if none/unknown."""
    return _CASE_LABELS.get(_low_word(case_type), "")


def _preposition_hint_parts(entry):
    """Extra hint tokens for a preposition: [preposition_type, case_label].

    Both are optional (skipped when not yet annotated). They disambiguate a
    preposition with several meanings, e.g. `in` = (temporal, +Dativ) vs
    (local, +D/A (Wechsel)).
    """
    parts = []
    ptype = _low_word(entry.get("preposition_type", ""))
    if ptype:
        parts.append(ptype)
    case_label = _case_label(entry.get("case_type", ""))
    if case_label:
        parts.append(case_label)
    return parts


def _type_hint(entry):
    """Parenthetical hint after an L1>De prompt: '(type[, subtype, +Case])'.

    For a preposition the subtype/case are folded in so the learner sees which
    meaning is being asked; other types get just '(type)'.
    """
    word_type = _low_word(entry.get("type", ""))
    if not word_type:
        return ""
    parts = [construction_label(entry)]
    if word_type == "praeposition":
        parts.extend(_preposition_hint_parts(entry))
    return f" ({', '.join(parts)})"


def _article_variants(text):
    """Split comma-separated article variants and normalize."""
    raw = str(text or "")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return set()
    return {_normalize_answer(p) for p in parts if _normalize_answer(p)}


def _article_matches(answer, expected_text):
    """Check whether answer matches one of expected article variants."""
    ans = _normalize_answer(answer)
    variants = _article_variants(expected_text)
    return bool(ans) and ans in variants


def _article_with_emoji(text):
    """Format article variants with color emoji markers."""
    mapping = {"der": "🔵 der", "die": "🔴 die", "das": "🟢 das"}
    parts = [p.strip() for p in str(text or "").split(",") if p.strip()]
    if not parts:
        return ""
    out = []
    for part in parts:
        token = _normalize_answer(part)
        out.append(mapping.get(token, part))
    return ", ".join(out)


def _status_text(ok, lang="en"):
    """Neutral monochrome status markers, localized (#15). lang defaults to en."""
    return t(lang, "answer_correct") if ok else t(lang, "answer_wrong")


def _nomen_with_edge_emoji(article_text, noun_text):
    """Wrap noun answer with edge emoji before article and after noun."""
    mapping = {"der": "🔵", "die": "🔴", "das": "🟢"}
    variants = [v for v in _article_variants(article_text) if v in mapping]
    edge = mapping.get((variants[0] if variants else ""), "⚪")
    article = str(article_text or "").strip()
    noun = _cap_word(noun_text)
    core = f"{article} {noun}".strip() if article else noun
    return f"{edge} {core} {edge}".strip()


def _append_hint_lines(lines, entry, ok):
    """Append image/example hints after wrong answers."""
    if ok:
        return lines
    image = str(entry.get("image", "") or "").strip()
    example = str(entry.get("example", "") or "").strip()
    if not image and not example:
        return lines
    lines.append("")
    if image:
        lines.append(_md_escape(image))
    if example:
        if image:
            lines.append("")
        lines.append(_md_escape(example))
    return lines


def _example_lines(example):
    """Return trimmed nonempty example lines without splitting sentences."""
    return [line.strip() for line in str(example or "").splitlines() if line.strip()]


def _append_example_lines(lines, examples):
    """Append already scoped example lines as one optional Telegram hint block."""
    if examples:
        lines.append("")
        lines.extend(_md_escape(example) for example in examples)
    return lines


def _format_word_card(entry, title="Word card:", user_id=None):
    """Handle format word card."""
    status_line = ""
    if user_id is not None and entry.get("id"):
        status_line = get_word_study_status(get_db_path(), user_id, entry.get("id"))
        entry = resolve_bot_word_media(get_db_path(), user_id, entry)
    lines = [
        title,
        f"id: {entry.get('id', '')}",
        f"status: {status_line or '-'}",
        f"type: {construction_label(entry)}",
        f"article: {entry.get('article', '') or '-'}",
        f"german: {(display_infinitive(entry) if entry.get('type') == 'verb' else entry.get('german', '')) or '-'}",
        f"infinitive: {entry.get('infinitive', '') or '-'}",
        f"plural: {entry.get('plural', '') or '-'}",
        f"prateritum: {entry.get('prateritum', '') or '-'}",
        f"partizip2: {entry.get('partizip2', '') or '-'}",
        f"komparativ: {entry.get('komparativ', '') or '-'}",
        f"superlativ: {entry.get('superlativ', '') or '-'}",
    ]
    lines.extend(
        f"{column}: {_md_escape(value)}"
        for column, value in translation_card_lines(
            entry, _user_dict_lang(user_id) if user_id is not None else "ru",
            owner=user_id is None or is_owner(user_id),
        )
    )
    # Personal translation uses the same selected language as the shared row.
    # Escape user text like media; owner translations remain shared.
    personal = str(entry.get("my_translation", "") or "").strip()
    if personal:
        lines.append(f"my_translation: {_md_escape(personal)}")
    lines += [
        # Personal image/example are user-writable (P4) → escape so they can't break
        # out of the ```code``` block they're rendered in (P8 hardening).
        f"image: {_md_escape(entry.get('image', '')) or '-'}",
        f"example: {_md_escape(entry.get('example', '')) or '-'}",
    ]
    if entry.get("type") == "verb":
        linked = _linked_verb_lines(entry,
            examples_already_shown=_example_lines(entry.get("example", "")))
        if linked is not None:
            lines.append("Perfekt:")
            lines.extend(_md_escape(value) for value in linked)
    # spec #1: prepositions carry two extra owner-curated fields — show them so the
    # owner can see/verify them on the card (they drive the quiz hints + dedup).
    if _low_word(entry.get("type", "")) == "praeposition":
        lines.append(f"preposition_type: {entry.get('preposition_type', '') or '-'}")
        lines.append(f"case_type: {entry.get('case_type', '') or '-'}")
    conj_praesens = format_conjugation_block(entry.get("conjugation_praesens", ""))
    if conj_praesens:
        lines.append("")
        lines.append("Präsens:")
        lines.append(conj_praesens)
    conj_praeteritum = format_conjugation_block(entry.get("conjugation_praeteritum", ""))
    if conj_praeteritum:
        lines.append("")
        lines.append("Präteritum:")
        lines.append(conj_praeteritum)
    return "\n".join(lines)


def _format_conjugation_card(entry, dict_lang, mask=False):
    """spec #3 (variant A): conjugation drill card.

    Line 1: ``word — translation`` (translation by the user's dict_lang). Then the
    Präsens block, and for the closed Präteritum list a second block. Section
    headers (Präsens/Präteritum) are italic and live OUTSIDE the monospace blocks
    — Telegram forbids formatting inside ```pre```. Pronouns are plain (owner
    decision 2026-07-02: no italic, no unicode-italic hack). Blocks are pre-aligned
    by format_conjugation_block; wrapped here in ```text so forms stay monospaced.

    With ``mask=True`` (Feature B "hide forms") every conjugated form renders as
    ``***`` with the pronoun column intact, so the user can recite from memory.
    """
    word = display_infinitive(entry)
    translation = _entry_translation(entry, dict_lang)
    header = f"*{_md_escape(word)}*{_md_escape(reflexive_hint(entry))}"
    if translation:
        header += f" — {_md_escape(translation)}"
    parts = [header]
    praesens = format_conjugation_block(entry.get("conjugation_praesens", ""), mask=mask)
    if praesens:
        parts.append(f"_Präsens_\n```text\n{praesens}\n```")
    praeteritum = format_conjugation_block(entry.get("conjugation_praeteritum", ""), mask=mask)
    if praeteritum:
        parts.append(f"_Präteritum_\n```text\n{praeteritum}\n```")
    return "\n\n".join(parts)


def _format_ru_question(entry, dict_lang):
    """Handle format ru question."""
    value = _normalize_russian_singular(_entry_translation(entry, dict_lang))
    return f">>    {_md_escape(value)}{_md_escape(_type_hint(entry))} ?"


def _format_ru_question_no_q(entry, dict_lang):
    """Handle format ru question no q."""
    value = _normalize_russian_singular(_entry_translation(entry, dict_lang))
    return f">>    {_md_escape(value)}{_md_escape(_type_hint(entry))}"


def _format_de_ru_question(entry):
    """Handle format de ru question."""
    word_type = _low_word(entry.get("type", ""))
    lines = []
    if word_type == "nomen":
        article = _article_with_emoji(entry.get("article", ""))
        german = _cap_word(entry.get("german", ""))
        answer = f"{article} {german}".strip() if article else german
        lines.append(_md_escape(answer))
        plural = _cap_plural(entry.get("plural", ""))
        if plural:
            lines.append(f"_  pl. die {_md_escape(plural)}_")
    elif word_type == "verb":
        infinitive = _low_word(display_infinitive(entry))
        lines.append(_md_escape(infinitive + reflexive_hint(entry)))
        _append_verb_forms(lines, entry,
            examples_already_shown=_example_lines(entry.get("example", ""))[:1])
    elif word_type == "adjektiv":
        german = _low_word(entry.get("german", ""))
        lines.append(_md_escape(german))
        komp = _low_word(entry.get("komparativ", ""))
        if komp:
            lines.append(f"_  {_md_escape(komp)}_")
        supr = _low_word(entry.get("superlativ", ""))
        if supr:
            lines.append(f"_  {_md_escape(supr)}_")
    elif word_type == "praeposition":
        # spec #1: the case/subtype hint is MANDATORY here — without it `in?` is
        # ambiguous between the preposition's several meanings.
        german = _low_word(entry.get("german", ""))
        parts = _preposition_hint_parts(entry)
        if parts:
            lines.append(f"{_md_escape(german)} {_md_escape('(' + ', '.join(parts) + ')')}")
        else:
            lines.append(_md_escape(german))
    elif word_type == "phrase":
        lines.append(_md_escape(str(entry.get("german", "") or "").strip()))
    else:
        german = _low_word(entry.get("german", ""))
        lines.append(_md_escape(german))
    lines.append(">>    ❓")
    # Always show image and the first example line with the De>Ru question.
    image = str(entry.get("image", "") or "").strip()
    examples = _example_lines(entry.get("example", ""))[:1]
    if image:
        lines.append("")
        lines.append(_md_escape(image))
    if examples:
        if image:
            lines.append("")
            lines.extend(_md_escape(example) for example in examples)
        else:
            _append_example_lines(lines, examples)
    return "\n".join(lines)


def _is_pl_sg_eligible(entry):
    """Exclude nouns where plural and singular forms are identical and article is die."""
    if _low_word(entry.get("type", "")) != "nomen":
        return False
    singular_article = _normalize_answer(entry.get("article", ""))
    singular = _normalize_answer(entry.get("german", ""))
    plural = _normalize_answer(entry.get("plural", ""))
    if not singular or not _has_plural_form(entry.get("plural", "")):
        return False
    if singular_article == "die" and singular == plural:
        return False
    return True


def _format_pl_sg_question(entry):
    """Handle format plural->singular question."""
    plural = _cap_plural(entry.get("plural", ""))
    return f">>    die {_md_escape(plural)} -> singular?"


def _format_feedback_pl_sg(entry, ok, lang="en"):
    """Handle format feedback plural->singular."""
    status = _status_text(ok, lang)
    lines = [f"{status} >>>     {_md_escape(_nomen_with_edge_emoji(entry.get('article', ''), entry.get('german', '')))}"]
    _append_hint_lines(lines, entry, ok)
    return "\n".join(lines)


def _format_article_question(entry):
    """Handle format article question."""
    return f">>     {_md_escape(_cap_word(entry.get('german', '')))} ?"


def _format_feedback_ru_de(entry, ok, lang="en"):
    """Handle format feedback ru de."""
    status = _status_text(ok, lang)
    word_type = entry.get("type", "")
    article = _article_with_emoji(entry.get("article", "")) if word_type == "nomen" else str(entry.get("article", "")).strip()
    if word_type == "nomen":
        answer = _nomen_with_edge_emoji(entry.get("article", ""), entry.get("german", ""))
    elif word_type == "verb":
        base = _low_word(entry.get("infinitive", ""))
        answer = f"{article} {base}".strip() if article else base
    elif word_type == "phrase":
        answer = str(entry.get("german", "") or "").strip()
    else:
        base = _low_word(entry.get("german", ""))
        answer = f"{article} {base}".strip() if article else base
    headline = f"{status} >>>     *{_md_escape(answer)}*"
    if word_type == "verb":
        headline += _md_escape(reflexive_hint(entry))
    lines = [headline]

    if word_type == "nomen":
        plural = _cap_plural(entry.get("plural", ""))
        if plural:
            lines.append("")
            lines.append(f"_pl. die {_md_escape(plural)}_")
    elif word_type == "verb":
        linked = _linked_verb_lines(entry)
        if linked is not None:
            lines.append("")
            _append_verb_forms(lines, entry)
        else:
            forms = ", ".join(value for value in
                (_low_word(entry.get("prateritum", "")), _expected_partizip2(entry)) if value)
            if forms:
                lines.append("")
                lines.append(f"_{_md_escape(forms)}_")
    elif word_type == "adjektiv":
        komp = _low_word(entry.get("komparativ", ""))
        supr = _low_word(entry.get("superlativ", ""))
        forms = ", ".join([x for x in [komp, supr] if x])
        if forms:
            lines.append("")
            lines.append(f"_  {_md_escape(forms)}_")

    # Ru→De keeps image behavior in the generic wrong-answer path. Examples are
    # intentionally narrower: only prepositions show their full multiline value,
    # and they show it after both correct and incorrect answers.
    image = str(entry.get("image", "") or "").strip()
    examples = _example_lines(entry.get("example", "")) if _low_word(word_type) == "praeposition" else []
    if not ok and image:
        lines.append("")
        lines.append(_md_escape(image))
    if examples:
        _append_example_lines(lines, examples)
    return "\n".join(lines)


def _format_learning_preview_prompt(entry, dict_lang):
    """Handle format learning preview prompt."""
    word_type = _low_word(entry.get("type", ""))
    lines = []
    if word_type == "nomen":
        answer = _nomen_with_edge_emoji(entry.get("article", ""), entry.get("german", ""))
        lines.append(f"*{_md_escape(answer)}*")
        plural = _cap_plural(entry.get("plural", ""))
        if plural:
            lines.append(f"_pl. die {_md_escape(plural)}_")
    elif word_type == "verb":
        infinitive = _low_word(display_infinitive(entry))
        lines.append(f"*{_md_escape(infinitive)}*")
        if _linked_verb_lines(entry) is not None:
            _append_verb_forms(lines, entry,
                examples_already_shown=_example_lines(entry.get("example", ""))[:1])
        else:
            forms = ", ".join(value for value in
                (_low_word(entry.get("prateritum", "")), _expected_partizip2(entry)) if value)
            if forms:
                lines.append(f"_{_md_escape(forms)}_")
    elif word_type == "adjektiv":
        german = _low_word(entry.get("german", ""))
        lines.append(f"*{_md_escape(german)}*")
        komp = _low_word(entry.get("komparativ", ""))
        supr = _low_word(entry.get("superlativ", ""))
        forms = ", ".join(x for x in [komp, supr] if x)
        if forms:
            lines.append(f"_  {_md_escape(forms)}_")
    elif word_type == "phrase":
        german = str(entry.get("german", "") or "").strip()
        lines.append(f"*{_md_escape(german)}*")
    else:
        german = _low_word(entry.get("german", ""))
        lines.append(f"*{_md_escape(german)}*")
    lines.append(_format_ru_question_no_q(entry, dict_lang))
    examples = _example_lines(entry.get("example", ""))
    if word_type != "praeposition":
        examples = examples[:1]
    _append_example_lines(lines, examples)
    return "\n".join(lines)


def _format_feedback_de_ru(entry, ok, dict_lang, user_id=None, lang="en"):
    """Handle format feedback de ru."""
    status = _status_text(ok, lang)
    # image/example are shown with the question now (_format_de_ru_question), not here.
    return f"{status} >>>     *{_md_escape(_low_word(_entry_translation(entry, dict_lang)))}*"


def _format_feedback_article(entry, ok, dict_lang, lang="en"):
    """Handle format feedback article."""
    status = _status_text(ok, lang)
    plural = _cap_plural(entry.get("plural", ""))
    headline = f"{status} >>>     {_md_escape(_nomen_with_edge_emoji(entry.get('article', ''), entry.get('german', '')))}"
    lines = [headline.strip()]
    if plural:
        lines.append("")
        lines.append(f"_pl. die {_md_escape(plural)}_")
    _append_hint_lines(lines, entry, ok)
    return "\n".join(lines)


def _format_topic_learning_line(entry):
    """Handle format learning line grouped by topic."""
    entry_type = str(entry.get("type", "")).strip().lower()
    topic = str(entry.get("topic", "")).strip() or "-"
    if entry_type == "verb":
        base = display_infinitive(entry) + reflexive_hint(entry)
    else:
        base = str(entry.get("german", "")).strip()
    if entry_type == "nomen":
        article = str(entry.get("article", "")).strip()
        base = f"{article} {base}".strip() if article else base
    return f"[{topic}] - [{base}]"
