"""Display book topics without changing stored names or selection semantics."""

from collections import Counter
import re

_ASCII_NUMBER = re.compile(r"[0-9]+\Z")
_ALL_LABELS = {"ru": "все темы", "uk": "усі теми", "en": "all topics"}


def topic_labels(level, names, descriptions):
    """Shorten level-prefixed numbers only when every label stays unique."""
    prefix = re.compile(re.escape(level) + r"_([0-9]+)\Z")
    labels = {}
    for name in names:
        match = prefix.fullmatch(name)
        description = (descriptions.get(name) or "").strip()
        labels[name] = (str(int(match[1])) if match else
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
    # Leading-zero variants still represent distinct topics before data merging.
    values = Counter(int(label) for label in labels.values() if _ASCII_NUMBER.fullmatch(label))
    numeric, other = [], []
    for name in selected:
        label = labels.get(name, name)
        if (_ASCII_NUMBER.fullmatch(label) and label == str(int(label))
                and values[int(label)] == 1):
            numeric.append(int(label))
        else:
            other.append(label)
    numeric.sort()
    parts = []
    start = end = None
    for number in numeric:
        if end is not None and number == end + 1:
            end = number
            continue
        if start is not None:
            parts.append(str(start) if start == end else f"{start}–{end}")
        start = end = number
    if start is not None:
        parts.append(str(start) if start == end else f"{start}–{end}")
    return ", ".join(parts + other) or "∅"
