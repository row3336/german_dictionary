"""Topic-filter subsystem (spec #8): ordering and filter-string logic.

The on-disk filter format is unchanged — a per-user string like '01+03+7' or 'all'
(see common.settings / _parse_topic_filter). All functions here are pure and DB-free
so they unit-test without a database; the inline screens that consume them live in
handlers/word_filter_ui.py.
"""

import re

_ALL_TOKENS = {"all", "*"}

# add-flow topic input: a name (no separators) then a description; separators
# between them are any run of space / comma / dot / dash (spec #8).
_ADD_SEP = r"[\s,.\-]"
_ADD_NAME_RE = re.compile(rf"[^\s,.\-]+")
_ADD_LEAD_SEP_RE = re.compile(rf"^{_ADD_SEP}+")


def parse_topic_input(raw):
    """Split owner add-flow topic input into (name, description).

    The name is the leading token up to the first separator; the description is the
    rest with the separating run stripped (internal punctuation/spaces preserved).
    '05 Essen und Trinken' / '05, Essen' / '05 - Essen' / '05..Essen' -> ('05','Essen…');
    bare '05' -> ('05', '').
    """
    s = str(raw or "").strip()
    if not s:
        return "", ""
    m = _ADD_NAME_RE.match(s)
    name = m.group(0) if m else ""
    rest = s[len(name):]
    desc = _ADD_LEAD_SEP_RE.sub("", rest).strip()
    return name, desc


def _topic_sort_key(name):
    """Order: leading-zero numerics first (0,01,02,03,04,011,012), then plain
    numerics (1..7,8), then non-numeric names alphabetically (food,nom,verb).

    A 3-tuple keeps element types consistent within each group so sorting never
    compares int against str.
    """
    s = str(name).strip()
    if s.isdigit():
        group = 0 if s.startswith("0") else 1
        return (group, int(s), s)
    return (2, 0, s.lower())


def sorted_topics(names):
    """Apply the spec #8 ordering to an iterable of topic names."""
    return sorted(names, key=_topic_sort_key)


def filter_is_all(raw):
    """True when the stored filter means 'every topic'."""
    return str(raw or "").strip().lower() in _ALL_TOKENS


def filter_tokens(raw):
    """Explicit topic tokens in a filter string ([] when it means 'all')."""
    if filter_is_all(raw):
        return []
    tokens = []
    for chunk in str(raw or "").replace(",", "+").split("+"):
        token = chunk.strip()
        if token:
            tokens.append(token)
    return tokens


def resolve_active(raw, all_topics):
    """Set of currently-active topic names, resolving 'all' to every known topic
    and dropping stale tokens that are no longer in the registry."""
    known = set(all_topics)
    if filter_is_all(raw):
        return set(known)
    return {tok for tok in filter_tokens(raw) if tok in known}


def format_filter(active, all_topics):
    """Serialize an active set back to the stored format: 'all' when it covers every
    known topic (so new topics auto-join), else '+'-joined in canonical order."""
    active = set(active)
    if active and active == set(all_topics):
        return "all"
    return "+".join(sorted_topics(active))


def toggle(raw, name, all_topics):
    """Toggle one topic in the filter.

    Returns (new_filter, rejected): rejected is True when the tap would remove the
    last active topic (empty selection is not allowed) — new_filter is then None and
    the caller shows an alert without changing anything.
    """
    active = resolve_active(raw, all_topics)
    if name in active:
        if len(active) <= 1:
            return None, True
        active.discard(name)
    else:
        active.add(name)
    return format_filter(active, all_topics), False
