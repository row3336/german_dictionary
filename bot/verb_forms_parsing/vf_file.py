"""VF-add review file (spec #2, Step 4): build the JSON the owner edits.

One document per job: a list of word cards (our entry fields, as in db_search)
plus a per-card ``_review`` block (original query, headword, status, flags,
error) so the owner can spot mismatches/failures and fix them by hand. Step 5
reads the edited file back.
"""

import json
from datetime import datetime, timezone

from .job_store import get_job, list_items

INSTRUCTIONS = (
    "VF parse review. Edit the fields, delete words you don't want, keep the JSON "
    "valid, then send this file back to apply. Translations keep ALL meanings — "
    "trim as you like. _review.flags: no_card = the site had no dedicated entry "
    "(word/type to fill by hand); headword_mismatch = the site resolved a different "
    "lemma than requested; conjugation_parse_error = the Präsens table couldn't be "
    "read. _review is informational; edit the real fields above it."
)


def _card_from_item(item):
    """Turn a stored job item into an owner-facing card + _review meta."""
    result = item.get("result")
    review = {
        "query": item.get("word", ""),
        "status": item.get("status", ""),
        "flags": [f for f in (item.get("flags") or "").split(",") if f],
        "error": item.get("error", ""),
    }
    if isinstance(result, dict):
        card = {k: v for k, v in result.items() if not k.startswith("_")}
        review["headword"] = result.get("_headword", "")
    else:
        # error / never-parsed item: leave a stub for the owner to complete
        card = {"type": "", "german": item.get("word", "")}
    card["_review"] = review
    return card


def build_review_document(db_path, job_id):
    """Assemble the review document (dict) for a finished/partial job."""
    job = get_job(db_path, job_id)
    items = list_items(db_path, job_id)
    return {
        "job_id": job_id,
        "topic": (job or {}).get("topic", ""),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "instructions": INSTRUCTIONS,
        "words": [_card_from_item(it) for it in items],
    }


def render_review_json(doc):
    """Pretty JSON bytes ready to send as a document."""
    return json.dumps(doc, ensure_ascii=False, indent=2)


def review_filename(job_id):
    return f"vf_review_job{job_id}.json"
