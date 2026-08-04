"""The topic filter — the one setting the app writes (miniapp.md §7.6).

Reads and writes `topic_filter` in `user_settings` through the bot's own keys and
string format ('0+1+2' or 'all'), so a filter set in the app applies in the chat
and the other way round. No storage of its own, and no second copy of the filter
logic: ordering, 'all' handling and stale-token dropping all come from
`common/topics.py`, the same pure helpers the bot's inline screens use.

⚠️ §7.6 was written before `spec_topics_levels.md` split the quiz filter in two
mutually exclusive modes ('topics' — the topics of the active book-level; 'cefr' —
the word's own level). Confirmed with the owner 2026-08-04, so this module follows
the resolver rather than the older text:

  * the list is the ACTIVE BOOK-LEVEL's topics (`list_topics_for_level`), not every
    name in `words.topic`. On today's data the two are identical (all 13 prod topics
    are A1), but once topics get assigned to A2+ a global list would be offering
    choices `resolve_quiz_filter` then silently ignores;
  * `mode` is reported so the client can tell the user whether the filter is live —
    in 'cefr' mode the selection is stored but nothing is filtered by it.
"""

from aiohttp import web

from ..app_config import TOPIC_FILTER_KEY, get_db_path
from ..common.topics import filter_is_all, filter_tokens, format_filter, resolve_active, sorted_topics
from ..common.word_filter import (
    MODE_TOPICS,
    get_active_book_level,
    get_filter_mode,
    set_filter_mode,
)
from ..storage import (
    count_words_by_topic,
    count_words_for_topics,
    get_user_setting,
    list_topics_for_level,
    set_user_setting,
)
from .access import API_PREFIX, REQUEST_USER_ID
from .http_json import json_body

routes = web.RouteTableDef()

INVALID_VALUE = "invalid_value"
EMPTY_TOPICS = "empty_topics"
UNKNOWN_TOPICS = "unknown_topics"


def _stored_filter(db, user_id):
    """The raw per-user filter string; '' (unset) means 'all', as in the resolver."""
    return str(get_user_setting(db, user_id, TOPIC_FILTER_KEY, "") or "").strip()


def topics_view(user_id):
    """The GET payload: the level's topics, their counts and what is selected.

    `selected` is resolved here rather than left to the client: 'all', an unset
    value and tokens for topics that no longer exist all have to collapse to the
    same answer the resolver would give, and that is exactly the logic the app must
    not own a second copy of.
    """
    db = get_db_path()
    level = get_active_book_level(user_id)
    names = sorted_topics(list_topics_for_level(db, level))
    counts = count_words_by_topic(db)
    # Unset must normalize to 'all' BEFORE resolving: resolve_active() reads '' as
    # "nothing selected", while the resolver reads it as "the whole level" — and on
    # prod most users have no row at all, so getting this backwards would show them
    # an empty screen over a filter that is in fact wide open.
    current = _stored_filter(db, user_id) or "all"
    active = resolve_active(current, names)
    return {
        "mode": get_filter_mode(user_id),
        "level": level,
        "current": current,
        # Words the level covers at all — the denominator the "all" chip shows.
        "total_words": count_words_for_topics(db, names),
        "topics": [
            {"name": name, "count": counts.get(name, 0), "selected": name in active}
            for name in names
        ],
    }


@routes.get(f"{API_PREFIX}topics")
async def get_topics(request):
    return web.json_response(topics_view(request[REQUEST_USER_ID]))


@routes.post(f"{API_PREFIX}topics")
async def set_topics(request):
    """Apply a selection: `{"value": "0+1+2"}` or `{"value": "all"}`.

    Refuses rather than silently narrowing. An empty selection would leave every
    quiz without words (§7.6), and a name outside the active level is a screen the
    user has since outgrown — the chat may have moved the book-level while the app
    was open. Both come back as an error the client fixes with one GET, because
    quietly dropping half of an explicit choice is how the app and the chat start
    disagreeing about what is being studied.
    """
    body = await json_body(request)
    user_id = request[REQUEST_USER_ID]
    db = get_db_path()

    value = body.get("value")
    if not isinstance(value, str) or not value.strip():
        return web.json_response({"error": INVALID_VALUE}, status=400)

    names = sorted_topics(list_topics_for_level(db, get_active_book_level(user_id)))
    if filter_is_all(value):
        selection = list(names)
    else:
        selection = filter_tokens(value)
        unknown = [name for name in selection if name not in set(names)]
        if unknown:
            return web.json_response(
                {"error": UNKNOWN_TOPICS, "topics": unknown}, status=400
            )
    if not selection:
        return web.json_response({"error": EMPTY_TOPICS}, status=400)

    # format_filter collapses a full selection back to 'all', which is what makes a
    # topic added later join the filter by itself instead of being invisible.
    set_user_setting(db, user_id, TOPIC_FILTER_KEY, format_filter(selection, names))
    # Writing the selection is not enough to make it apply: the resolver only looks
    # at topics while the 'topics' branch is live, so a user who switched to CEFR
    # levels in the chat would otherwise tap through this screen with no effect
    # (owner decision, 2026-08-04).
    set_filter_mode(user_id, MODE_TOPICS)

    view = topics_view(user_id)
    # The count for what was just selected — the number the summary line shows.
    view["words"] = count_words_for_topics(db, selection)
    return web.json_response(view)
