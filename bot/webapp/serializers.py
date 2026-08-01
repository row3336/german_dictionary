"""Word entry → card JSON (miniapp.md §7.5, §8.4a).

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
"""

from ..common.conjugation import PRONOUNS

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
