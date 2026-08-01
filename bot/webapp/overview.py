"""The /me payload: who the user is and what is waiting for them (miniapp.md §7.1).

One request feeds the whole main screen — languages, the topic filter, the counter
on every mode tile and the full string table — because the app opens on that screen
and a tile without its number is a tile the user cannot choose between.

Every number here is produced by the same code the bot uses, with the same active
filter (`resolve_quiz_filter`), so a mode showing "24" in the app and "24" in the
chat is not a coincidence that can drift apart.
"""

from ..app_config import TOPIC_FILTER_KEY, get_db_path, is_owner
from ..common.lang import (
    _mode_de_l1,
    _mode_l1_de,
    _mode_pl_sg,
    _user_dict_lang,
    _user_interface_lang,
)
from ..common.settings import _learning_ready_quorum
from ..common.word_filter import resolve_quiz_filter
from ..storage import (
    get_user_setting,
    learning_status_snapshot,
    list_weak_words,
    select_due,
)
from .i18n_strings import app_strings

# Mode ids as the frontend knows them. Stable wire names: the bot's own mode
# strings depend on the dictionary language (`ru_de`, `uk_de`, `pl_sg_en`…), which
# is an implementation detail the client has no reason to learn.
FSRS_MODES = ("l1_de", "de_l1", "pl_sg")
WEAK_FIELDS = ("article", "plural", "prateritum", "partizip2")
MODES = FSRS_MODES + tuple(f"weak_{field}" for field in WEAK_FIELDS) + ("learning",)

# list_weak_words is LIMIT-capped by design, so this is a display cap, not a true
# count: past it the tile means "at least this many". Kept well above a plausible
# session so the number is honest in practice.
WEAK_COUNT_CAP = 300


def _fsrs_modes_for(user_id):
    """Wire name -> the bot's dict-language-dependent mode string."""
    return {
        "l1_de": _mode_l1_de(user_id),
        "de_l1": _mode_de_l1(user_id),
        "pl_sg": _mode_pl_sg(user_id),
    }


def due_counts(user_id):
    """How many words are due right now, per mode, under the user's active filter."""
    db = get_db_path()
    quiz_filter = resolve_quiz_filter(user_id)
    counts = {}
    for wire_name, mode in _fsrs_modes_for(user_id).items():
        counts[wire_name] = len(
            select_due(
                db,
                user_id=user_id,
                mode=mode,
                # pl->sg only makes sense for nouns; the bot picks with the same
                # restriction (bot/handlers/quiz.py:543).
                word_type="nomen" if wire_name == "pl_sg" else None,
                topics=quiz_filter.topics,
                cefr_levels=quiz_filter.cefr_levels,
                # FSRS modes hide what was already answered correctly today, exactly
                # as _pick_due_entry does (bot/common/session.py:179).
                exclude_correct_today=True,
            )
        )
    for field in WEAK_FIELDS:
        counts[f"weak_{field}"] = len(
            list_weak_words(
                db,
                user_id,
                field,
                topics=quiz_filter.topics,
                cefr_levels=quiz_filter.cefr_levels,
                limit=WEAK_COUNT_CAP,
            )
        )
    return counts


def learning_overview(user_id):
    """Active learning items and whether the batch has reached its quorum."""
    db = get_db_path()
    snapshot = learning_status_snapshot(db, user_id, _learning_ready_quorum(db, user_id))
    return {
        "active": int(snapshot.get("active_count") or 0),
        "batch_ready": bool(snapshot.get("ready")),
    }


def topic_filter_value(user_id):
    """The stored filter string, in the bot's own format ('0+1+2' or 'all').

    An unset value means "the whole active level", which the app shows as `all` —
    the same meaning POST /topics accepts back (§7.6).
    """
    raw = str(get_user_setting(get_db_path(), user_id, TOPIC_FILTER_KEY, "") or "").strip()
    return raw or "all"


def build_me(user_id):
    """Assemble the /me response for an authenticated, approved user."""
    interface_lang = _user_interface_lang(user_id)
    return {
        "user_id": user_id,
        # v1 has no owner-only feature; this is here so the frontend can grow one
        # without a second round-trip (§6.3).
        "is_owner": is_owner(user_id),
        "interface_lang": interface_lang,
        "dict_lang": _user_dict_lang(user_id),
        "topic_filter": topic_filter_value(user_id),
        "modes": list(MODES),
        "due": due_counts(user_id),
        "learning": learning_overview(user_id),
        "strings": app_strings(interface_lang),
    }
