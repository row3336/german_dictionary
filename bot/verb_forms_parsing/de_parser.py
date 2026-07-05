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


def headword_matches(headword, query):
    """True iff the page's headword equals the requested word (spec #2 guard).

    A mismatch (query 'weil' -> page 'weilen') means .de resolved a different
    lemma; the caller must not trust the page and should flag the row.
    """
    return bool(_norm(headword)) and _norm(headword) == _norm(query)
