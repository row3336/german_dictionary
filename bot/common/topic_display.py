"""Display book topics without changing stored names or selection semantics."""

from collections import Counter
import re

_ASCII_NUMBER = re.compile(r"[0-9]+\Z")
_ALL_LABELS = {"ru": "все темы", "uk": "усі теми", "en": "all topics"}
# Book chapters shown with their printed width (PONS 00..14); others drop zeros.
_LABEL_WIDTH = {"PONS": 2}
# Stable keys kept for stored filters, shown under the owner's button text.
_LABEL_OVERRIDES = {"GSV": "reflexive"}


def topic_labels(level, names, descriptions):
    """Shorten level-prefixed numbers only when every label stays unique."""
    prefix = re.compile(re.escape(level) + r"_([0-9]+)\Z")
    width = _LABEL_WIDTH.get(level, 0)
    labels = {}
    for name in names:
        match = prefix.fullmatch(name)
        description = (descriptions.get(name) or "").strip()
        labels[name] = (str(int(match[1])).zfill(width) if match else
                        _LABEL_OVERRIDES[name] if name in _LABEL_OVERRIDES else
                        description if name in ("nom", "verb") and description else name)
    # A raw fallback can itself collide with another proposed short label.
    # Repeat until all keys have their own, unambiguous visible identity.
    while True:
        counts = Counter(labels.values())
        collisions = [name for name in names if counts[labels[name]] > 1 and labels[name] != name]
        if not collisions:
            return labels
        for name in collisions:
            labels[name] = name


def format_topic_selection(level, selected, available, descriptions, *, dynamic_all=False, lang="ru"):
    """Compress canonical numeric labels; preserve explicit and stale selections."""
    selected = list(selected)
    if not selected:
        return "∅"
    if dynamic_all and set(selected) == set(available):
        return _ALL_LABELS.get(lang, _ALL_LABELS["en"])
    labels = topic_labels(level, available, descriptions)
    width = _LABEL_WIDTH.get(level, 0)
    # Leading-zero variants still represent distinct topics before data merging.
    values = Counter(int(label) for label in labels.values() if _ASCII_NUMBER.fullmatch(label))
    numeric, other = [], []
    for name in selected:
        label = labels.get(name, name)
        if (_ASCII_NUMBER.fullmatch(label) and label == str(int(label)).zfill(width)
                and values[int(label)] == 1):
            numeric.append(int(label))
        else:
            other.append(label)
    numeric.sort()
    parts = []
    start = end = None

    def span(a, b):
        a, b = str(a).zfill(width), str(b).zfill(width)
        return a if a == b else f"{a}–{b}"

    for number in numeric:
        if end is not None and number == end + 1:
            end = number
            continue
        if start is not None:
            parts.append(span(start, end))
        start = end = number
    if start is not None:
        parts.append(span(start, end))
    return ", ".join(parts + other) or "∅"
