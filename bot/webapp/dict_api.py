"""Dictionary routes: search and word card (miniapp.md §7.5).

Three outcomes for a search, and the client renders one screen per outcome:
`card` when the query names exactly one word, `choice` when it is ambiguous, and
`empty` when nothing matches. The ambiguity screen is a small gain over the chat,
where the same lookup silently returns whichever homonym the query hit first.

Both routes sit behind the access gate, and both derive the user from the verified
signature — the study status and the personal example on a card are per-user data,
and there is no parameter here that could ask for somebody else's (risk #6).
"""

import logging

from aiohttp import web

from ..app_config import get_db_path
from ..common.lang import _user_dict_lang
from .access import API_PREFIX, REQUEST_USER_ID
from .serializers import gender_of, headword, translation_of, word_card
from .word_repo import MAX_QUERY_LENGTH, load_card_sources, search_words

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()

WORD_NOT_FOUND = "word_not_found"


def _card_for(user_id, word_id, dict_lang):
    """Assemble one word's card, or None when the word is gone."""
    sources = load_card_sources(get_db_path(), user_id, word_id)
    if not sources:
        return None
    return word_card(sources["entry"], dict_lang, sources["synonyms"], sources["study"])


def _candidate(entry, dict_lang):
    """A compact line for the disambiguation screen — a marker, not a card (§8.4)."""
    item = {
        "id": entry.get("id"),
        "german": headword(entry),
        "type": (entry.get("type") or "").strip().lower(),
        "translation": translation_of(entry, dict_lang),
    }
    article = (entry.get("article") or "").strip()
    if article:
        item["article"] = article
        item["gender"] = gender_of(entry)
    return item


@routes.get(f"{API_PREFIX}search")
async def search(request):
    """Look a word up. Returns one of card / choice / empty."""
    user_id = request[REQUEST_USER_ID]
    # Trimmed before it reaches the database: a query longer than this is not a
    # word, and an empty one is not worth a query at all.
    query = (request.query.get("q") or "").strip()[:MAX_QUERY_LENGTH]
    if not query:
        return web.json_response({"result": "empty", "query": ""})

    dict_lang = _user_dict_lang(user_id)
    matches = search_words(get_db_path(), query)
    if not matches:
        return web.json_response({"result": "empty", "query": query})
    if len(matches) == 1:
        card = _card_for(user_id, matches[0].get("id"), dict_lang)
        if card:
            return web.json_response({"result": "card", "query": query, "card": card})
        return web.json_response({"result": "empty", "query": query})
    return web.json_response(
        {
            "result": "choice",
            "query": query,
            "candidates": [_candidate(entry, dict_lang) for entry in matches],
        }
    )


@routes.get(f"{API_PREFIX}word/{{word_id}}")
async def word(request):
    """The full card for one word, by id."""
    user_id = request[REQUEST_USER_ID]
    card = _card_for(user_id, request.match_info["word_id"], _user_dict_lang(user_id))
    if not card:
        return web.json_response({"error": WORD_NOT_FOUND}, status=404)
    return web.json_response({"card": card})
