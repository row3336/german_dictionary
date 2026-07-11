"""verbformen.de full-page parser (spec #2): HTML -> structured word fields.

Separate from parser.py (which verifies verbformen.com forms) so the existing
verify flow stays untouched. Anchors on STRUCTURAL markers (h1 phrasing,
<dd lang>, div.vTbl, id="stammformen"/"Beispiele") rather than visible text, so
it is resilient to the site's display language. Built and tested against real
.de fixtures in tests/fixtures/verbformen_de/.

Incremental (spec #2, Step 1): this sub-step covers word-type + headword
detection and the headword-mismatch guard. Field extractors (translations,
forms, conjugation, examples, article/plural, komparativ/superlativ) are added
in the following sub-steps.
"""

import html as _html
import re
from urllib.parse import unquote

from ..app_config import CEFR_LEVELS
from ..common.conjugation import parse_conjugation

# h1 phrasing -> our word type. Only verbs/nouns/adjectives get a dedicated .de
# card; adverbs and function words (prepositions/conjunctions/particles) usually
# land on a "Deutsche Wörter mit …" search page with no structured entry — those
# are flagged (has_entry=False) for manual handling by the owner.
_H1_TYPE_MARKERS = (
    ("des Verbs", "verb"),
    ("des Substantivs", "nomen"),
    ("des Adjektivs", "adjektiv"),
    # verben.de type pages (function words) — same "Definition <gen. type> <word>" h1
    ("der Präposition", "praeposition"),
    ("des Adverbs", "adverb"),
    ("der Konjunktion", "konjunktion"),
    ("des Partikel", "partikel"),
)

_QUOTE_CHARS = " „“”\"'»«"


def _h1_text(html):
    """Plain text of the page's <h1>, tags stripped and whitespace collapsed."""
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html or "", re.S)
    if not m:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()


def _norm(word):
    """Loose comparison key: lowercased, whitespace-collapsed."""
    return re.sub(r"\s+", " ", str(word or "").strip().lower())


# Combining stress accents dictionaries put on vowels (grave/acute). Stored words
# must be plain text, so these are dropped from translations. Only these two marks
# are removed — precomposed umlauts (ü/ö/ä) and ё are single codepoints, untouched.
_STRESS_MARKS = str.maketrans("", "", "\u0300\u0301")  # combining grave + acute


def _strip_stress(text):
    """Drop stress accents (мгнове́ние -> мгновение); leaves umlauts/ё intact."""
    return str(text or "").translate(_STRESS_MARKS)


def detect_type_and_headword(summary_html):
    """From a .de summary page return {type, headword, has_entry}.

    - dedicated card (verb/nomen/adjektiv): type set, headword = the lemma the
      page is actually about, has_entry=True.
    - "Deutsche Wörter mit „X“" search page: type="", headword=X, has_entry=False
      (no card — caller flags for manual handling).
    """
    h1 = _h1_text(summary_html)
    for marker, wtype in _H1_TYPE_MARKERS:
        m = re.search(re.escape(marker) + r"\s+(\S+)", h1)
        if m:
            return {"type": wtype, "headword": m.group(1).strip(_QUOTE_CHARS), "has_entry": True}
    m = re.search(r"Deutsche Wörter mit\s+(.+)$", h1)
    quoted = m.group(1).strip(_QUOTE_CHARS) if m else ""
    return {"type": "", "headword": quoted, "has_entry": False}


_TRANSLATION_LANGS = ("ru", "uk", "en")


def parse_translations(summary_html):
    """Ordered translation lists per language from the .de summary page.

    Each language lives in a single ``<dd lang="xx">`` whose text is a
    comma-separated list of meanings (a leading flag ``&nbsp;`` is dropped).
    Returns ``{"ru": [...], "uk": [...], "en": [...]}`` — every value lowercased
    and stripped, order preserved (primary meaning first). Missing language -> [].

    The full list is returned on purpose; how many meanings to keep is a
    presentation/assembly decision (owner trims the review file).
    """
    html = summary_html or ""
    out = {}
    for lang in _TRANSLATION_LANGS:
        m = re.search(r'<dd lang="%s">(.*?)</dd>' % lang, html, re.S)
        if not m:
            out[lang] = []
            continue
        text = _html.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))
        text = re.sub(r"\s+", " ", text).strip()
        seen, meanings = set(), []
        for p in text.split(","):
            p = _strip_stress(p.strip().lower())
            # stripping stress can collapse distinct spellings (здание / зда́ние) into
            # the same word -> keep first occurrence only, order preserved
            if p and p not in seen:
                seen.add(p)
                meanings.append(p)
        out[lang] = meanings
    return out


def _clean_form(part_html):
    """One stammformen part -> clean lowercase word.

    Drops the audio <a> link and a leading auxiliary in its own <i>…</i> before
    the participle <b> (``<i>ist</i><b>…gegangen</b>`` -> ``gegangen``). Tags are
    stripped WITHOUT inserting spaces so a stem split across <b>/<i>/<u> tags
    (``<i>ge</i>g<u>ang</u><i>en</i>``) merges back into one word.
    """
    s = re.sub(r"<a\b[^>]*>.*?</a>", "", part_html or "", flags=re.S).strip()
    s = re.sub(r"^<i>[^<]*</i>\s*(?=<b)", "", s)  # leading auxiliary (ist/hat/…), space-separated
    s = _html.unescape(re.sub(r"<[^>]+>", "", s))
    return re.sub(r"[^A-Za-zÄÖÜäöüß -]+", "", s).strip().lower()


def parse_verb_forms(summary_html):
    """Verb Präteritum + Partizip II from the ``id="stammformen"`` line.

    The line is three ``·``-separated parts: present (ignored), Präteritum,
    auxiliary+Partizip II. Returns ``{"prateritum": ..., "partizip2": ...}``
    (lowercased); missing/non-verb page -> both empty. Separate from the .com
    ``_vf_parse_verb`` because the .de markup splits stems and the auxiliary
    differently.
    """
    m = re.search(r'<p[^>]*id="stammformen"[^>]*>(.*?)</p>', summary_html or "", re.S)
    if not m:
        return {"prateritum": "", "partizip2": ""}
    parts = m.group(1).split("·")
    return {
        "prateritum": _clean_form(parts[1]) if len(parts) > 1 else "",
        "partizip2": _clean_form(parts[2]) if len(parts) > 2 else "",
    }


# The indikativ Präsens table is the only <h2 class="wG">Präsens</h2> on the conj
# page (Konjunktiv blocks are labelled "Konjunktiv I/II"). Its six rows map to the
# canonical ТЗ#3 pronoun order below (site shows er/sie collapsed to "er"/"sie").
_PRAESENS_H2 = '<h2 class="wG">Präsens</h2>'
_CANON_PRONOUNS = ("ich", "du", "er/sie/es", "wir", "ihr", "sie/Sie")


def parse_praesens_conjugation(conj_html):
    """Canonical 6-line Präsens block from the conjugation page, or '' if absent.

    Reads the indikativ Präsens table (6 ``<tr><td>pronoun</td><td>form</td></tr>``
    rows), substitutes the canonical pronouns and runs the result through the
    spec #3 validator (parse_conjugation) so the output matches every other
    conjugation write path. Only Präsens — Präteritum is never parsed (spec #2 §4).
    Raises ValueError (from parse_conjugation) if the extracted forms are not a
    valid block, so the caller can flag that word.
    """
    html = conj_html or ""
    idx = html.find(_PRAESENS_H2)
    if idx == -1:
        return ""
    tm = re.search(r"<table>(.*?)</table>", html[idx:], re.S)
    if not tm:
        return ""
    rows = re.findall(r"<tr><td>(.*?)</td><td>(.*?)</td></tr>", tm.group(1), re.S)
    if len(rows) < 6:
        return ""
    forms = [_html.unescape(re.sub(r"<[^>]+>", "", cell)).strip() for _pron, cell in rows[:6]]
    lines = [f"{pron} {form}" for pron, form in zip(_CANON_PRONOUNS, forms)]
    return parse_conjugation("\n".join(lines))


def _plain_text(html):
    """Whole page as tag-stripped, entity-decoded, whitespace-collapsed text."""
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", html or "")))


def _multi_join(values):
    """De-duplicate (case-insensitively, order-preserving) and comma-join.

    The whole codebase stores a noun's several articles/plurals comma-separated
    (``der, das`` · ``Pizzas, Pizzen`` — see text_utils.split_plural_forms and
    ui.formatting._article_variants), so every multi-value we emit is normalised
    to that one canonical separator regardless of how the site wrote it.
    """
    out, seen = [], set()
    for v in values:
        v = str(v or "").strip()
        key = v.lower()
        if v and key not in seen:
            seen.add(key)
            out.append(v)
    return ", ".join(out)


def _extra_article_variants(summary_html, noun):
    """Alternate articles of a multi-gender noun, from the JSON-LD nav links.

    A multi-gender noun (``der/das/die Joghurt``) cross-links its gender variants
    as ``"name":"das Joghurt"`` SiteNavigationElement entries; a single-gender
    noun has none. Only variants whose noun equals the headword are kept (so a
    compound like ``der Joghurtbecher`` in the same list is ignored).
    """
    if not noun:
        return []
    out = []
    for art, name in re.findall(r'"name":"(der|die|das)\s+([^"]+)"', summary_html or ""):
        if _norm(name) == _norm(noun):
            out.append(art.lower())
    return out


def parse_noun_article_plural(summary_html):
    """Noun article(s) + plural(s) from the .de summary.

    - article: ``Der Artikel lautet „das Haus“`` -> ``das``. A multi-gender noun
      (``der Joghurt`` + JSON-LD ``das/die Joghurt`` nav links) yields every
      article, primary first, comma-joined: ``der, das, die``.
    - plural:  ``Nominativ Plural „Häuser“`` -> ``Häuser`` (capitalisation kept);
      a multi-plural noun (``Pizzas/Pizzen``) is normalised to ``Pizzas, Pizzen``.
    Missing values -> ''.
    """
    text = _plain_text(summary_html)
    m_art = re.search(r"Der Artikel lautet\s*[„\"]\s*(der|die|das)\s+([^„“”\"]+?)\s*[“”\"]", text, re.I)
    primary = m_art.group(1).lower() if m_art else ""
    noun = m_art.group(2).strip() if m_art else ""
    article = _multi_join([primary] + _extra_article_variants(summary_html, noun)) if primary else ""
    m_pl = re.search(r"Nominativ Plural\s*[„\"]\s*([^„“”\"]+?)\s*[“”\"]", text)
    plural = _multi_join(re.split(r"[,/]", m_pl.group(1))) if m_pl else ""
    return {"article": article, "plural": plural}


def parse_adjektiv_degrees(summary_html):
    """Adjective Komparativ + Superlativ from the .de summary.

    The degrees are rendered as a link run ``… <a>schnell</a>, <a>schneller</a>,
    <a>am schnellsten</a> …`` (positive, comparative, superlative). The superlative
    is the first ``am …`` link; the comparative is the link right before it.
    Missing/non-adjective -> ''. Values lowercased.
    """
    links = re.findall(r'<a href="/deklination/adjektive/[^"]*">(.*?)</a>', summary_html or "", re.S)
    links = [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", x))).strip(" „“”\"") for x in links]
    for i, text in enumerate(links):
        if re.match(r"^am\s+\S", text):
            return {
                "komparativ": links[i - 1].lower() if i > 0 else "",
                "superlativ": text.lower(),
            }
    return {"komparativ": "", "superlativ": ""}


def parse_examples(summary_html, limit=3):
    """First `limit` German example sentences from the id="Beispiele" section,
    joined with newlines (owner stores several examples as \\n-separated lines).

    Each ``<li>`` holds a German sentence (with a satzapp.de/?t=<sentence> link
    carrying it already assembled) then, after <br>, the English translation with
    a flag. We take the German sentence from the satzapp link (cleanest), falling
    back to the text before <br>. Missing section -> ''.
    """
    html = summary_html or ""
    start = html.find('id="Beispiele"')
    if start == -1:
        return ""
    ul = re.search(r'<ul[^>]*class="[^"]*rLst[^"]*"[^>]*>(.*?)</ul>', html[start:], re.S)
    if not ul:
        return ""
    out = []
    for li in re.findall(r"<li[^>]*>(.*?)</li>", ul.group(1), re.S):
        m = re.search(r'satzapp\.de/\?t=([^"]+)"', li)
        if m:
            sentence = _html.unescape(unquote(m.group(1))).strip()
        else:  # fallback: German part is everything before the <br> that precedes the translation
            before_br = re.split(r"<br\b", li, maxsplit=1)[0]
            sentence = re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", before_br))).strip()
        if sentence:
            out.append(sentence)
        if len(out) >= limit:
            break
    return "\n".join(out)


# The word's CEFR level rides on a badge next to the headword:
#   <span class="bZrt" title="Wortschatz Zertifikat A1"> A1</span>
# This badge is present on EVERY card type (verbs/nouns/adjectives AND adverbs),
# unlike the prose "gehört zum Wortschatz auf A1-Niveau" which only nouns/verbs
# carry — so the badge is the primary source. The FIRST badge on the page belongs
# to the headword; on a "Deutsche Wörter mit …" search page (no dedicated card)
# later badges are related words, so first-match is still the right pick.
_LEVEL_BADGE_RE = re.compile(
    r'title="Wortschatz Zertifikat\s+(%s)"' % "|".join(CEFR_LEVELS)
)
_LEVEL_PROSE_RE = re.compile(
    r"Wortschatz auf\s+(%s)-Niveau" % "|".join(CEFR_LEVELS)
)


def parse_word_level(summary_html):
    """The headword's CEFR level (``A1``…``C2``) from the .de summary, or '' if absent.

    Primary source is the first ``Wortschatz Zertifikat`` badge (present on all
    card types). Falls back to the ``… auf X-Niveau`` prose (nouns/verbs only) so
    a page that ever drops the badge still yields a level. Returns uppercase.
    """
    m = _LEVEL_BADGE_RE.search(summary_html or "")
    if m:
        return m.group(1).upper()
    m = _LEVEL_PROSE_RE.search(_plain_text(summary_html))
    return m.group(1).upper() if m else ""


def headword_matches(headword, query):
    """True iff the page's headword equals the requested word (spec #2 guard).

    A mismatch (query 'weil' -> page 'weilen') means .de resolved a different
    lemma; the caller must not trust the page and should flag the row.
    """
    return bool(_norm(headword)) and _norm(headword) == _norm(query)


def parse_de(summary_html, conj_html=None, query=None, topic="", type_hint=None):
    """Assemble a full word card from .de HTML (spec #2 Step 1h).

    Calls the per-field extractors by detected type and returns a dict in our
    entry shape (as used by add_word / db_search) plus meta keys:
      - ``_headword``: the lemma the page is actually about
      - ``_query``: the requested word (if given)
      - ``_flags``: list of issues for the owner review file — ``no_card`` (page
        was a "Deutsche Wörter mit …" search result), ``headword_mismatch``
        (query != page lemma, e.g. weil->weilen), ``conjugation_parse_error``,
        ``type_mismatch`` (owner/DB `type_hint` disagrees with the fetched card).

    `type_hint` (a DB word type) is the caller's known type — the owner's `[type]`
    on add or the existing word's type on recheck. It disambiguates function words
    whose verbformen `?w=` search hits a homonym card: the hint wins over a
    conflicting detected type and no wrong-sense structural data is adopted.

    Translations keep ALL meanings (owner trims in review), comma-joined.
    Präteritum table is never parsed (spec #2 §4). Conjugation needs `conj_html`
    (the linked /konjugation/ page) for verbs.
    """
    info = detect_type_and_headword(summary_html)
    detected = info["type"]
    headword = info["headword"]
    flags = []
    if not info["has_entry"]:
        flags.append("no_card")
    if query is not None and headword and not headword_matches(headword, query):
        flags.append("headword_mismatch")

    # The hint (owner/DB) is authoritative: on a real conflict with the fetched
    # card (a homonym slipped through) trust the hint and skip that card's
    # structural fields, rather than overwriting a preposition with "das Aus".
    hint = str(type_hint or "").strip().lower() or None
    wtype = detected
    if hint:
        if detected and detected != hint:
            flags.append("type_mismatch")
        wtype = hint

    # On a type mismatch the page is a different word-sense (a homonym), so every
    # field on it — translations, level, example — is wrong for our word. Emit
    # nothing but the type/word; the owner fills it by hand and apply keeps the
    # existing card intact.
    mismatch = "type_mismatch" in flags
    tr = {"ru": [], "uk": [], "en": []} if mismatch else parse_translations(summary_html)
    entry = {
        "type": wtype,
        "russian": ", ".join(tr["ru"]),
        "ukrainian": ", ".join(tr["uk"]),
        "english": ", ".join(tr["en"]),
        "example": "" if mismatch else parse_examples(summary_html),
        "cefr_level": "" if mismatch else parse_word_level(summary_html),  # A1..C2, type-independent
        "topic": topic,
    }

    if mismatch:
        entry["german"] = query or headword
    elif wtype == "verb":
        forms = parse_verb_forms(summary_html)
        entry["infinitive"] = headword
        entry["prateritum"] = forms["prateritum"]
        entry["partizip2"] = forms["partizip2"]
        try:
            entry["conjugation_praesens"] = parse_praesens_conjugation(conj_html or "")
        except ValueError:
            entry["conjugation_praesens"] = ""
            flags.append("conjugation_parse_error")
    elif wtype == "nomen":
        ap = parse_noun_article_plural(summary_html)
        entry["article"] = ap["article"]
        entry["german"] = headword
        entry["plural"] = ap["plural"]
    elif wtype == "adjektiv":
        deg = parse_adjektiv_degrees(summary_html)
        entry["german"] = headword
        entry["komparativ"] = deg["komparativ"]
        entry["superlativ"] = deg["superlativ"]
    else:
        # no dedicated card (search page) or an unrecognised type: keep what we
        # have (word + any translations) for the owner to finish by hand.
        entry["german"] = headword or (query or "")

    entry["_headword"] = headword
    entry["_query"] = query
    entry["_flags"] = flags
    return entry
