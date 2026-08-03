"""Word entry → card, question and feedback JSON (miniapp.md §7.4, §7.5, §8.4a).

The card is the app's most reused piece: the dictionary shows it, and from M4 the
quiz feedback shows the same component. So this module produces data only — no
labels, no layout, no ordering decisions that belong to CSS. What it does decide is
what the client is even allowed to render:

  * **empty fields are not sent at all** (§8.4a). "Пустых блоков не бывает" is a
    server-side rule here, not a frontend `if` — a missing key cannot be rendered
    as an empty heading by accident;
  * **German grammar stays German** (§8.4a). Form names travel as `plural`,
    `praeteritum`, `komparativ`; `preposition_type` and `case_type` are passed
    through exactly as the database stores them (`local`, `dativ`), because that
    is how the bot shows them too and because we are teaching German, not
    translating it. Only the word's translation and the interface labels are
    localized, and those come from /me.

The quiz half at the bottom follows the same rule and adds one of its own: what the
question shows and what the feedback calls the expected answer are built from the
very functions the answer is checked against (`_entry_translation`,
`has_plural_form`), so the app cannot ask for one thing and grade another.
"""

from ..common.conjugation import PRONOUNS
from ..text_utils import (
    cap_plural,
    cap_word,
    has_plural_form,
    low_word,
    normalize_russian_singular,
)
from ..ui.formatting import (  # noqa: PLC2701 - the chat's own display rules, reused verbatim
    _entry_translation,
    _expected_partizip2,
)
from .quiz_flow import DE_L1, L1_DE, PL_SG

# Colour of the noun is carried by the article, and the article is always shown as
# text next to it — never colour alone (§8.4, colour-blindness).
GENDER_BY_ARTICLE = {"der": "m", "die": "f", "das": "n"}

# The word's translation column for each dictionary language.
TRANSLATION_COLUMN = {"ru": "russian", "uk": "ukrainian", "en": "english"}

# Extra header forms per word type, in the order the mockup shows them.
FORMS_BY_TYPE = {
    "nomen": ("plural",),
    "verb": ("prateritum", "partizip2"),
    "adjektiv": ("komparativ", "superlativ"),
}


def _text(value):
    return str(value or "").strip()


def gender_of(entry):
    """m / f / n from the article, empty for anything that is not a noun."""
    return GENDER_BY_ARTICLE.get(_text(entry.get("article")).lower(), "")


def headword(entry):
    """The word as the card's first line shows it.

    Verbs are stored with the infinitive in its own column and the same text in
    `german`; prefer the infinitive and fall back, so a row filled only one way
    still renders.
    """
    if _text(entry.get("type")).lower() == "verb":
        return _text(entry.get("infinitive")) or _text(entry.get("german"))
    return _text(entry.get("german"))


def forms_of(entry):
    """Extra forms for this word type, empties dropped."""
    fields = FORMS_BY_TYPE.get(_text(entry.get("type")).lower(), ())
    return {field: _text(entry.get(field)) for field in fields if _text(entry.get(field))}


def translation_of(entry, dict_lang):
    """Translation in the user's dictionary language, personal override first.

    spec #7 keeps a guest's own translation in `my_translation` so the base column
    stays intact; display prefers the personal one.
    """
    personal = _text(entry.get("my_translation"))
    if personal:
        return personal
    column = TRANSLATION_COLUMN.get(dict_lang, "russian")
    return _text(entry.get(column))


def conjugation_of(entry):
    """Präsens / Präteritum as pronoun-form pairs, ready to lay out as a table.

    The stored block is already canonical (bot/common/conjugation.py validates every
    write path), so this splits rather than parses. A block that is somehow
    malformed is dropped rather than half-rendered - the card is not the place to
    surface a data defect.
    """
    out = {}
    for key, column in (("praesens", "conjugation_praesens"), ("praeteritum", "conjugation_praeteritum")):
        rows = _conjugation_rows(entry.get(column))
        if rows:
            out[key] = rows
    return out


def _conjugation_rows(block):
    lines = [line.strip() for line in _text(block).splitlines() if line.strip()]
    if len(lines) != len(PRONOUNS):
        return []
    rows = []
    for pronoun, line in zip(PRONOUNS, lines):
        head, _, form = line.partition(" ")
        if head != pronoun or not form.strip():
            return []
        rows.append([pronoun, form.strip()])
    return rows


def synonyms_of(group, word_id):
    """The other members of this word's synonym group, if it has one."""
    out = []
    for item in group or ():
        if item.get("id") == word_id:
            continue
        entry = {"id": item.get("id"), "german": headword(item)}
        article = _text(item.get("article"))
        if article:
            entry["article"] = article
            entry["gender"] = gender_of(item)
        out.append(entry)
    return out


def word_card(entry, dict_lang, synonyms=(), study=None):
    """Full card payload for one word. Absent data means an absent key."""
    word_id = entry.get("id")
    word_type = _text(entry.get("type")).lower()
    card = {
        "id": word_id,
        "type": word_type,
        "german": headword(entry),
        "translation": translation_of(entry, dict_lang),
    }

    article = _text(entry.get("article"))
    if article:
        card["article"] = article
        card["gender"] = gender_of(entry)

    forms = forms_of(entry)
    if forms:
        card["forms"] = forms

    topic = _text(entry.get("topic"))
    if topic:
        card["topic"] = topic

    # Prepositions carry two curated vocabularies; both stay in their stored,
    # German-facing form.
    for field in ("preposition_type", "case_type"):
        value = _text(entry.get(field))
        if value:
            card[field] = value

    media = {
        field: _text(entry.get(field))
        for field in ("image", "example")
        if _text(entry.get(field))
    }
    if media:
        card["media"] = media

    group = synonyms_of(synonyms, word_id)
    if group:
        card["synonyms"] = group

    conjugation = conjugation_of(entry)
    if conjugation:
        card["conjugation"] = conjugation

    if study:
        card["study"] = study

    return card


# --- quiz question and feedback (§7.4) ---------------------------------------

# The only answer kinds v1 knows. `article_choice` is what turns the answer row
# into three der/die/das buttons; it belongs to weak_article and arrives with M5.
ANSWER_TEXT = "text"
ANSWER_ARTICLE_CHOICE = "article_choice"

# What the learner is expected to type, spelled out under the question (§8.5a-bis):
# the forms and their order, no separators, and the case each form is written in.
# German terms on purpose — they are not translated for any interface language, so
# they are data, not a string table. De→L1 is absent because its hint ("translation")
# is an interface word and therefore comes from /me like every other label.
_ANSWER_FORMS = {
    (L1_DE, "nomen"): ("artikel Singular Plural", ""),
    (L1_DE, "verb"): ("infinitiv partizip II", "infinitiv präteritum partizip II"),
    (L1_DE, "adjektiv"): ("adjektiv", "adjektiv komparativ superlativ"),
    (PL_SG, "nomen"): ("artikel Singular", ""),
}

# Extra forms printed under a De→L1 question, per word type — the chat shows them
# with the question as a memorization aid (_format_de_ru_question).
_QUESTION_FORMS_BY_TYPE = {
    "nomen": ("plural",),
    "verb": ("prateritum", "partizip2"),
    "adjektiv": ("komparativ", "superlativ"),
}


def _word_type(entry):
    return _text(entry.get("type")).lower()


def de_headword(entry):
    """The German side as a question or an answer shows it: article included.

    Nouns keep their article and their capital letter, everything else is written
    the way the dictionary stores it — the same normalization the chat applies
    before printing (`_cap_word` / `_low_word`).
    """
    if _word_type(entry) == "nomen":
        article = _text(entry.get("article"))
        german = cap_word(entry.get("german", ""))
        return f"{article} {german}".strip()
    return low_word(headword(entry))


def question_prompt(entry, mode, dict_lang):
    """(prompt, prompt_lang) — the word being asked, and the language it is in."""
    if mode == L1_DE:
        # Same normalization the chat applies: a translation stored in the plural
        # would otherwise ask for a form the checker does not accept.
        return normalize_russian_singular(_entry_translation(entry, dict_lang)), dict_lang
    if mode == PL_SG:
        return f"die {cap_plural(entry.get('plural', ''))}".strip(), "de"
    return de_headword(entry), "de"


def question_forms(entry):
    """Secondary forms shown under a De→L1 question, empties dropped.

    Partizip II carries its auxiliary (`ist gegangen`), because that is the form
    the learner has to know and the form the chat prints.
    """
    fields = _QUESTION_FORMS_BY_TYPE.get(_word_type(entry), ())
    out = {}
    for field in fields:
        if field == "plural":
            value = cap_plural(entry.get("plural", ""))
        elif field == "partizip2":
            value = _expected_partizip2(entry)
        else:
            value = low_word(entry.get(field, ""))
        if value:
            out[field] = value
    return out


def question_hint(entry, mode):
    """What the client needs to render the question correctly, beyond the word.

    `gender_known` is the permission to colour the word by gender (§8.4): in De→L1
    the article is on screen, so colour merely repeats it; in the other two
    directions the article is the thing being asked, and colouring it in would hand
    the answer over.
    """
    word_type = _word_type(entry)
    hint = {"type": word_type}
    if word_type == "nomen":
        hint["gender_known"] = mode == DE_L1
    # A preposition is ambiguous without them — `in` is a different question with
    # +Dativ than with +D/A, and the chat shows the pair for exactly that reason.
    for field in ("preposition_type", "case_type"):
        value = _text(entry.get(field))
        if value:
            hint[field] = value
    return hint


def answer_forms(entry, mode):
    """The "what to type" hint for this question, or {} when the answer is one word."""
    word_type = _word_type(entry)
    main, alt = _ANSWER_FORMS.get((mode, word_type), ("", ""))
    if not main:
        return {}
    # Nothing plural to ask for: a noun with no stored plural is answered with two
    # words, and the checker accepts exactly that (`_check_ru_de_strict`).
    if mode == L1_DE and word_type == "nomen" and not has_plural_form(entry.get("plural", "")):
        return {"main": "artikel Singular"}
    return {"main": main, "alt": alt} if alt else {"main": main}


def expected_answer(entry, mode, dict_lang):
    """The answer that would have been counted correct, as one line.

    Built from the checker's own sources rather than from the card, so it can never
    name a form the verdict would have rejected. Extra forms are comma-separated
    here, unlike the input hint (§8.5a-bis) — this is a thing to read, not to type.
    """
    if mode == DE_L1:
        return low_word(_entry_translation(entry, dict_lang))
    if mode == PL_SG:
        return de_headword(entry)

    word_type = _word_type(entry)
    parts = [de_headword(entry)]
    if word_type == "nomen":
        if has_plural_form(entry.get("plural", "")):
            parts.append(f"die {cap_plural(entry.get('plural', ''))}")
    elif word_type == "verb":
        parts.extend([low_word(entry.get("prateritum", "")), _expected_partizip2(entry)])
    elif word_type == "adjektiv":
        parts.extend([low_word(entry.get("komparativ", "")), low_word(entry.get("superlativ", ""))])
    return ", ".join(part for part in parts if part)


def quiz_question(
    entry,
    mode,
    dict_lang,
    token,
    progress,
    phase="quiz",
    is_retry=False,
    is_drill=False,
    answer_kind=ANSWER_TEXT,
):
    """One question, ready to send (§7.4)."""
    prompt, prompt_lang = question_prompt(entry, mode, dict_lang)
    payload = {
        "token": token,
        "mode": mode,
        "phase": phase,
        "word_id": entry.get("id"),
        "prompt": prompt,
        "prompt_lang": prompt_lang,
        "answer_kind": answer_kind,
        "hint": question_hint(entry, mode),
        "is_retry": bool(is_retry),
        "is_drill": bool(is_drill),
        "progress": progress,
    }

    forms_hint = answer_forms(entry, mode)
    if forms_hint:
        payload["answer_forms"] = forms_hint

    # Only De→L1 shows the word itself, so only there can it show the word's other
    # forms and its picture without giving the answer away.
    if mode == DE_L1:
        shown = question_forms(entry)
        if shown:
            payload["forms"] = shown
        media = {
            field: _text(entry.get(field))
            for field in ("image", "example")
            if _text(entry.get(field))
        }
        if media:
            payload["media"] = media

    return payload


def quiz_feedback(entry, mode, dict_lang, streak, card=None):
    """The verdict screen's payload: expected answer, streak, and the word card."""
    payload = {"expected": expected_answer(entry, mode, dict_lang), "streak": int(streak or 0)}
    if card:
        payload["card"] = card
    return payload
