"""Quiz word-filter resolver (spec_topics_levels.md).

Two MUTUALLY EXCLUSIVE filter modes decide which words a quiz/learning query may
draw from:

- 'topics'  — book topics of the currently selected book-level (`active_book_level`),
              narrowed by the per-user topic selection (`TOPIC_FILTER_KEY`; 'all' =
              every topic of that level).
- 'cefr'    — the word's own CEFR level (`words.cefr_level`), by the per-user level
              selection (`user_levels`). All six levels selected = no restriction
              (every word, including an empty cefr_level).

`resolve_quiz_filter` turns the stored per-user state into the concrete arguments the
storage layer expects (`select_due(topics=..., cefr_levels=...)`); exactly one axis is
ever set, the other is None. This is the single source of truth so every quiz entry
point (FSRS, weak, learning add, empty-state checks) filters identically.
"""

from collections import namedtuple

from ..app_config import CEFR_LEVELS, TOPIC_FILTER_KEY, get_db_path
from ..storage import (
    count_words_for_cefr,
    count_words_for_topics,
    get_user_levels,
    get_user_setting,
    list_topics_for_level,
    set_user_setting,
)
from .settings import _parse_topic_filter

FILTER_MODE_KEY = "filter_mode"
ACTIVE_BOOK_LEVEL_KEY = "active_book_level"
MODE_TOPICS = "topics"
MODE_CEFR = "cefr"
DEFAULT_BOOK_LEVEL = "A1"

# topics / cefr_levels: at most one is non-None (the active axis). cefr_levels=None in
# CEFR mode means "no restriction" (all six levels selected).
QuizFilter = namedtuple("QuizFilter", ["mode", "topics", "cefr_levels"])


def get_filter_mode(user_id):
    """Active filter mode for this user ('topics' | 'cefr'). Default 'topics'."""
    raw = get_user_setting(get_db_path(), user_id, FILTER_MODE_KEY, MODE_TOPICS)
    return MODE_CEFR if str(raw or "").strip().lower() == MODE_CEFR else MODE_TOPICS


def set_filter_mode(user_id, mode):
    """Switch the active filter mode (anything but 'cefr' normalizes to 'topics')."""
    mode = MODE_CEFR if str(mode or "").strip().lower() == MODE_CEFR else MODE_TOPICS
    set_user_setting(get_db_path(), user_id, FILTER_MODE_KEY, mode)


def get_active_book_level(user_id):
    """Current book-level for topics mode (A1..C2). Default A1."""
    raw = get_user_setting(get_db_path(), user_id, ACTIVE_BOOK_LEVEL_KEY, DEFAULT_BOOK_LEVEL)
    level = str(raw or "").strip().upper()
    return level if level in CEFR_LEVELS else DEFAULT_BOOK_LEVEL


def set_active_book_level(user_id, level):
    """Set the current book-level; rejects anything outside CEFR_LEVELS."""
    level = str(level or "").strip().upper()
    if level not in CEFR_LEVELS:
        raise ValueError("Invalid CEFR level.")
    set_user_setting(get_db_path(), user_id, ACTIVE_BOOK_LEVEL_KEY, level)


def resolve_quiz_filter(user_id):
    """Stored per-user filter state -> concrete query arguments (QuizFilter)."""
    db = get_db_path()
    if get_filter_mode(user_id) == MODE_CEFR:
        levels = get_user_levels(db, user_id)  # always >=1, in CEFR order
        # all six selected == no CEFR restriction (show every word, incl. empty level)
        cefr = None if len(levels) >= len(CEFR_LEVELS) else levels
        return QuizFilter(MODE_CEFR, None, cefr)

    level = get_active_book_level(user_id)
    level_topics = list_topics_for_level(db, level)
    # Read the raw per-user selection: unset/empty == 'all of this level' (the new
    # default is topics/A1/all — we deliberately do NOT use the legacy _topic_filter_value
    # whose default is the single topic '1').
    raw = get_user_setting(db, user_id, TOPIC_FILTER_KEY, "")
    selection = _parse_topic_filter(raw)  # [] == 'all'
    if not selection:
        topics = level_topics
    else:
        allowed = set(level_topics)
        # keep only selected topics that belong to the active level; a stale/empty
        # intersection falls back to the whole level (never silently 'all words')
        topics = [t for t in selection if t in allowed] or level_topics
    return QuizFilter(MODE_TOPICS, topics, None)


def count_words_for_filter(user_id):
    """Words matching the active filter — for the 'no words under this filter' check.

    Mirrors resolve_quiz_filter's mode split so the empty-state banner and the actual
    query never disagree.
    """
    db = get_db_path()
    qf = resolve_quiz_filter(user_id)
    if qf.mode == MODE_CEFR:
        if qf.cefr_levels is None:
            return count_words_for_topics(db, None)  # no restriction -> every word
        return sum(count_words_for_cefr(db, lv) for lv in qf.cefr_levels)
    return count_words_for_topics(db, qf.topics)
