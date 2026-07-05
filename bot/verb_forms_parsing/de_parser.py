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

from ..common.conjugation import parse_conjugation

# h1 phrasing -> our word type. Only verbs/nouns/adjectives get a dedicated .de
# card; adverbs and function words (prepositions/conjunctions/particles) usually
# land on a "Deutsche Wörter mit …" search page with no structured entry — those
# are flagged (has_entry=False) for manual handling by the owner.
_H1_TYPE_MARKERS = (
    ("des Verbs", "verb"),
    ("des Substantivs", "nomen"),
    ("des Adjektivs", "adjektiv"),
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
        out[lang] = [p.strip().lower() for p in text.split(",") if p.strip()]
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


def headword_matches(headword, query):
    """True iff the page's headword equals the requested word (spec #2 guard).

    A mismatch (query 'weil' -> page 'weilen') means .de resolved a different
    lemma; the caller must not trust the page and should flag the row.
    """
    return bool(_norm(headword)) and _norm(headword) == _norm(query)
