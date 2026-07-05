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


def headword_matches(headword, query):
    """True iff the page's headword equals the requested word (spec #2 guard).

    A mismatch (query 'weil' -> page 'weilen') means .de resolved a different
    lemma; the caller must not trust the page and should flag the row.
    """
    return bool(_norm(headword)) and _norm(headword) == _norm(query)
