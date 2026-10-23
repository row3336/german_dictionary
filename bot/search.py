"""Read-only dictionary lookup, shared by multi-card and legacy search callers."""

import sqlite3
from contextlib import closing
from pathlib import Path


FIELDS = ("german", "infinitive", "plural", "prateritum", "partizip2",
          "russian", "ukrainian", "english")
TRANSLATIONS = ("russian", "ukrainian", "english")
SIMILAR_LIMIT = 20


def normalize_lookup(value):
    """Use Python Unicode case/whitespace rules for both queries and fields."""
    return " ".join(str(value or "").strip().lower().split())


def match_entries(entries, token):
    """Return an ordered ID snapshot; translations alone accept legacy variants."""
    from .storage import _ARTICLE_PREFIXES, _form_variants

    candidate = normalize_lookup(token)
    empty = {"kind": "empty", "ordered_ids": [], "has_more_similar": False}
    if not candidate:
        return empty
    candidates = {candidate}
    words = candidate.split()
    if words[0] in _ARTICLE_PREFIXES and len(words) > 1:
        candidates.add(" ".join(words[1:]))
    exact, similar, seen = [], [], set()
    for entry in entries:
        word_id = entry["id"]
        if word_id in seen:
            continue
        seen.add(word_id)
        forms = {normalize_lookup(entry.get(field)) for field in FIELDS}
        variants = set().union(*(_form_variants(entry.get(field)) for field in TRANSLATIONS))
        if word_id == str(token).strip() or candidates.intersection(forms | variants):
            exact.append(word_id)
        elif any(part in form for part in candidates for form in forms if form):
            similar.append(word_id)
    if exact:
        return {"kind": "exact", "ordered_ids": exact, "has_more_similar": False}
    # An article alone must not become an unbounded substring query.
    if candidate in _ARTICLE_PREFIXES:
        return empty
    return {"kind": "similar" if similar else "empty",
            "ordered_ids": similar[:SIMILAR_LIMIT],
            "has_more_similar": len(similar) > SIMILAR_LIMIT}


def _read_rows(db_path, word_id=None):
    """Never initialize/migrate a database as a side effect of search."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        if word_id is None:
            rows = conn.execute("SELECT * FROM words ORDER BY created_at ASC, id ASC").fetchall()
        else:
            rows = conn.execute("SELECT * FROM words WHERE id = ?", (str(word_id),)).fetchall()
    from .storage import _row_to_entry
    return [_row_to_entry(row) for row in rows]


def search_entries(db_path, token):
    """Read the dictionary once, with stable ordering and no exact-result cap."""
    return match_entries(_read_rows(db_path), token)


def read_search_entry(db_path, word_id):
    """Refresh one snapshot ID without a text lookup or schema mutation."""
    rows = _read_rows(db_path, word_id)
    return rows[0] if rows else None
