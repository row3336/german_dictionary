"""Comparison of phrase templates with optional ellipsis placeholders."""

import re

from ..text_utils import normalize_answer


_ELLIPSIS = re.compile(r"\.{3,}|…+")


def phrase_answer_matches(expected, answer):
    """Keep legacy punctuation rules, relaxing only stored ellipsis templates."""
    expected = str(expected or "")
    answer = str(answer or "")
    template = bool(_ELLIPSIS.search(expected))

    def normalize(value):
        if template:
            # A placeholder separates words even when it has no surrounding spaces.
            value = _ELLIPSIS.sub(" ", value)
        value = re.sub(r"[,.]+", " ", value)
        if template:
            # Removing a placeholder before '?' must not leave a required space.
            value = re.sub(r"\s+\?", "?", value)
        return normalize_answer(value)

    normalized = normalize(answer)
    return any(char.isalnum() for char in normalized) and normalized == normalize(expected)
