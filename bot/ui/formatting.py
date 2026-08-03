"""Display formatters for quiz questions/feedback, word cards, and shared helpers.

Leaf module: depends only on text_utils/storage/app_config (no handler back-refs).
"""

from ..app_config import get_db_path
from ..common.conjugation import format_conjugation_block
from ..i18n import t
from ..storage import apply_user_word_media, get_word_study_status
from ..text_utils import cap_plural as _cap_plural
from ..text_utils import cap_word as _cap_word
from ..text_utils import has_plural_form as _has_plural_form
from ..text_utils import low_word as _low_word
from ..text_utils import md_escape as _md_escape
from ..text_utils import normalize_answer as _normalize_answer
from ..text_utils import normalize_russian_singular as _normalize_russian_singular


def _expected_partizip2(entry):
    """Handle expected partizip2."""
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


def _entry_translation(entry, dict_lang):
    """Translation shown to the user, in their dict_lang.

    spec #7: a guest's personal override (entry['my_translation'], set by
    apply_user_word_media for their dict_lang) wins over the shared dictionary.
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
    parts = [word_type]
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


def _weak_status(ok, lang="en"):
    """Handle weak status."""
    return _status_text(ok, lang)


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


def _format_word_card(entry, title="Word card:", user_id=None):
    """Handle format word card."""
    status_line = ""
    if user_id is not None and entry.get("id"):
        status_line = get_word_study_status(get_db_path(), user_id, entry.get("id"))
        # Show this user's personal image/example override if any (P4); owner sees shared.
        entry = apply_user_word_media(get_db_path(), user_id, entry)
    lines = [
        title,
        f"id: {entry.get('id', '')}",
        f"status: {status_line or '-'}",
        f"type: {entry.get('type', '')}",
        f"article: {entry.get('article', '') or '-'}",
        f"german: {entry.get('german', '') or '-'}",
        f"infinitive: {entry.get('infinitive', '') or '-'}",
        f"plural: {entry.get('plural', '') or '-'}",
        f"prateritum: {entry.get('prateritum', '') or '-'}",
        f"partizip2: {entry.get('partizip2', '') or '-'}",
        f"komparativ: {entry.get('komparativ', '') or '-'}",
        f"superlativ: {entry.get('superlativ', '') or '-'}",
        f"russian: {entry.get('russian', '') or '-'}",
        f"ukrainian: {entry.get('ukrainian', '') or '-'}",
        f"english: {entry.get('english', '') or '-'}",
    ]
    # spec #7 / bug #25: a guest's personal translation (their dict_lang override,
    # set by apply_user_word_media above) — shown only when set, so the card CONFIRMS
    # it took (the base russian/ukrainian/english stay the shared dictionary). User-
    # writable → escape like image/example. Owner cards never carry my_translation.
    personal = str(entry.get("my_translation", "") or "").strip()
    if personal:
        lines.append(f"my_translation: {_md_escape(personal)}")
    lines += [
        # Personal image/example are user-writable (P4) → escape so they can't break
        # out of the ```code``` block they're rendered in (P8 hardening).
        f"image: {_md_escape(entry.get('image', '')) or '-'}",
        f"example: {_md_escape(entry.get('example', '')) or '-'}",
    ]
    # spec #1: prepositions carry two extra owner-curated fields — show them so the
    # owner can see/verify them on the card (they drive the quiz hints + dedup).
    if _low_word(entry.get("type", "")) == "praeposition":
        lines.append(f"preposition_type: {entry.get('preposition_type', '') or '-'}")
        lines.append(f"case_type: {entry.get('case_type', '') or '-'}")
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
    word = str(entry.get("german") or entry.get("infinitive") or "").strip()
    translation = _entry_translation(entry, dict_lang)
    header = f"*{_md_escape(word)}*"
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
        article = str(entry.get("article", "")).strip()
        german = _cap_word(entry.get("german", ""))
        answer = f"{article} {german}".strip() if article else german
        lines.append(_md_escape(answer))
        plural = _cap_plural(entry.get("plural", ""))
        if plural:
            lines.append(f"_  pl. die {_md_escape(plural)}_")
    elif word_type == "verb":
        infinitive = _low_word(entry.get("infinitive", ""))
        lines.append(_md_escape(infinitive))
        pr = _low_word(entry.get("prateritum", ""))
        if pr:
            lines.append(f"_  {_md_escape(pr)}_")
        p2 = _expected_partizip2(entry)
        if p2:
            lines.append(f"_  {_md_escape(p2)}_")
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
    else:
        german = _low_word(entry.get("german", ""))
        lines.append(_md_escape(german))
    lines.append(">>    ❓")
    # Always show image/example with the De>Ru question (memorization aid).
    image = str(entry.get("image", "") or "").strip()
    example = str(entry.get("example", "") or "").strip()
    if image:
        lines.append("")
        lines.append(_md_escape(image))
    if example:
        if not image:
            lines.append("")
        lines.append(_md_escape(example))
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
    else:
        base = _low_word(entry.get("german", ""))
        answer = f"{article} {base}".strip() if article else base
    headline = f"{status} >>>     *{_md_escape(answer)}*"
    lines = [headline]

    if word_type == "nomen":
        plural = _cap_plural(entry.get("plural", ""))
        if plural:
            lines.append("")
            lines.append(f"_pl. die {_md_escape(plural)}_")
    elif word_type == "verb":
        pr = _low_word(entry.get("prateritum", ""))
        p2 = str(entry.get("partizip2", "") or "").strip().lower()
        aux = str(
            entry.get("perfect_aux")
            or entry.get("hilfsverb")
            or entry.get("aux")
            or ""
        ).strip().lower()
        if p2 and aux in {"hat", "ist"} and not p2.startswith("hat ") and not p2.startswith("ist "):
            p2 = f"{aux} {p2}"
        forms = ", ".join([x for x in [pr, p2] if x])
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

    _append_hint_lines(lines, entry, ok)
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
        infinitive = _low_word(entry.get("infinitive", ""))
        lines.append(f"*{_md_escape(infinitive)}*")
        pr = _low_word(entry.get("prateritum", ""))
        p2 = _expected_partizip2(entry)
        forms = ", ".join(x for x in [pr, p2] if x)
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
    else:
        german = _low_word(entry.get("german", ""))
        lines.append(f"*{_md_escape(german)}*")
    lines.append(_format_ru_question_no_q(entry, dict_lang))
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


def _format_feedback_plural_weak(entry, ok, lang="en"):
    """Handle format feedback plural weak."""
    wrapped = _nomen_with_edge_emoji(entry.get("article", ""), entry.get("german", ""))
    plural = _cap_plural(entry.get("plural", ""))
    headline = f"{_weak_status(ok, lang)} >>>     {_md_escape(wrapped)}"
    lines = [headline]
    if plural:
        lines.extend(["", f"_pl. die {_md_escape(plural)}_"])
    _append_hint_lines(lines, entry, ok)
    return "\n".join(lines)


def _format_feedback_prateritum_weak(entry, ok, lang="en"):
    """Handle format feedback prateritum weak."""
    infinitive = _low_word(entry.get("infinitive", ""))
    pr = _low_word(entry.get("prateritum", ""))
    headline = f"{_weak_status(ok, lang)} >>>     {_md_escape(infinitive)}"
    lines = [headline]
    if pr:
        lines.extend(["", f"_{_md_escape(pr)}_"])
    _append_hint_lines(lines, entry, ok)
    return "\n".join(lines)


def _format_feedback_partizip2_weak(entry, ok, lang="en"):
    """Handle format feedback partizip2 weak."""
    infinitive = _low_word(entry.get("infinitive", ""))
    p2 = _expected_partizip2(entry)
    headline = f"{_weak_status(ok, lang)} >>>     {_md_escape(infinitive)}"
    lines = [headline]
    if p2:
        lines.extend(["", f"_{_md_escape(p2)}_"])
    _append_hint_lines(lines, entry, ok)
    return "\n".join(lines)


def _format_topic_learning_line(entry):
    """Handle format learning line grouped by topic."""
    entry_type = str(entry.get("type", "")).strip().lower()
    topic = str(entry.get("topic", "")).strip() or "-"
    if entry_type == "verb":
        base = str(entry.get("infinitive", "")).strip()
    else:
        base = str(entry.get("german", "")).strip()
    if entry_type == "nomen":
        article = str(entry.get("article", "")).strip()
        base = f"{article} {base}".strip() if article else base
    return f"[{topic}] - [{base}]"
