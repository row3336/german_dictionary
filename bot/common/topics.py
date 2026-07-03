"""Topic-filter subsystem (spec #8): ordering, filter-string logic, and screen render.

The on-disk filter format is unchanged — a per-user string like '01+03+7' or 'all'
(see common.settings / _parse_topic_filter). The core logic functions are pure and
DB-free so they unit-test without a database; only the render helpers touch telegram.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# callback_data prefixes for the inline toggle screen
CB_PREFIX = "topicf:"
CB_ALL = "topicf:all"
CB_TOPIC = "topicf:t:"  # + <name>

_ALL_TOKENS = {"all", "*"}
_BUTTONS_PER_ROW = 5


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


def select_all(raw, all_topics):
    """Result of tapping the 'all' button: 'all', or None when already all (no-op)."""
    if filter_is_all(raw):
        return None
    return "all"


def render_text(lang, raw, topics_with_desc):
    """Screen body: localized title + a ```text``` block of '[✅]/[ ] [name] desc'
    lines (sorted), + a localized hint. Content lives inside the code block so curated
    German descriptions never trip Markdown parsing.
    """
    from ..i18n import t

    by_name = {name: desc for name, desc in topics_with_desc}
    active = resolve_active(raw, by_name.keys())
    lines = []
    for name in sorted_topics(by_name.keys()):
        mark = "✅" if name in active else "  "
        desc = (by_name.get(name) or "").strip() or "—"
        lines.append(f"[{mark}] [{name}] {desc}")
    body = "\n".join(lines) if lines else "—"
    return f"{t(lang, 'topic_filter_title')}\n\n```text\n{body}\n```\n{t(lang, 'topic_filter_hint')}"


def keyboard_layout(raw, all_topics):
    """Pure layout (no telegram): list of button rows, each button a
    (text, callback_data, style) tuple. One style='success' (active) / None (neutral)
    button per topic in canonical order, rows of 5, trailing 'all' row (green when
    filter=all). Kept telegram-free so it unit-tests without PTB or its test stub.
    """
    active = resolve_active(raw, all_topics)
    ordered = sorted_topics(all_topics)
    rows = []
    for i in range(0, len(ordered), _BUTTONS_PER_ROW):
        rows.append(
            [
                (name, f"{CB_TOPIC}{name}", "success" if name in active else None)
                for name in ordered[i : i + _BUTTONS_PER_ROW]
            ]
        )
    rows.append([("all", CB_ALL, "success" if filter_is_all(raw) else None)])
    return rows


def build_keyboard(raw, all_topics):
    """Wrap keyboard_layout() into a telegram InlineKeyboardMarkup with style buttons
    (PTB >= 22.7 / Bot API 9.4). Old clients ignore style — the checkmarks in the code
    block carry the state regardless (spec #8 deliberate degradation)."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(text, callback_data=data, style=style)
                for text, data, style in row
            ]
            for row in keyboard_layout(raw, all_topics)
        ]
    )
