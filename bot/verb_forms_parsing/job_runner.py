"""VF-add batch job runner (spec #2, Step 3c).

Drives one persisted job to completion: pull the next pending item, fetch its
verbformen.de pages through the proxy, parse with parse_de, store the result,
then wait a random 20-50s (slow-drip) before the next. Pause/stop are read from
the job's DB status each iteration, so they survive a restart and an inline
button just flips the status. I/O (fetch, sleeping) is injectable so the loop is
unit-testable without network or real waits.
"""

import asyncio
import random
import time

from ..storage import mark_word_vf_checked
from .de_fetch import fetch_word_pages
from .de_parser import parse_de
from .job_store import (
    get_job,
    job_counts,
    next_pending_item,
    save_item_result,
    set_job_status,
)

DELAY_RANGE = (20, 50)  # seconds between site requests (owner's slow-drip rule)
BAN_STREAK_LIMIT = 10   # consecutive BLOCK responses (403/429/throttle) -> emergency stop
PAUSE_POLL_SEC = 3      # how often a paused job re-checks its status
FETCH_RETRIES = 2       # extra attempts for a transient (proxy/5xx) fetch failure

# A transient wave (verbformen.de/proxy temporarily down) must NOT look like a ban:
# after this many consecutive site-overload failures the job cools down and keeps
# going instead of dying, so an overnight rough patch doesn't kill a long batch.
TRANSIENT_STREAK_LIMIT = 5    # consecutive 5xx/proxy failures -> cool down (each already retried)
TRANSIENT_COOLDOWN_SEC = 600  # wait out the site's rough patch before resuming
MAX_TRANSIENT_COOLDOWNS = 6   # give up (hand back partial results) if it stays down across this many

# A failed fetch is classified by the http code embedded in its message
# (fetch_word_pages raises "summary fetch failed (http=CODE) for ..."):
#   ban       -> 403/429 (explicit block) or 200 (a 200 that still failed is
#                verbformen's "Zugriffe"/captcha stub, not a real page) -> likely blocked.
#   transient -> 0 (proxy/transport) or 5xx (site overloaded) -> retry, then cool down; NOT a ban.
#   absent    -> 404 or anything else (parse error, ...) -> the word isn't there; the site is fine.
# Only `ban` accrues toward the emergency stop, so a run of genuine 404s never trips it.
_BAN_HTTP = (200, 403, 429)
_TRANSIENT_HTTP = (0, 500, 502, 503, 504)


def _fetch_error_kind(exc):
    """Classify a failed-fetch exception -> 'ban' | 'transient' | 'absent' (see tables)."""
    msg = str(exc)
    if any(f"http={code}" in msg for code in _BAN_HTTP):
        return "ban"
    if any(f"http={code}" in msg for code in _TRANSIENT_HTTP):
        return "transient"
    return "absent"

# job_id -> asyncio.Task, so the UI can tell whether a job is already running.
VF_ADD_TASKS = {}


def _blocking_drip():
    """Blocking slow-drip delay (runs inside the fetch worker thread)."""
    time.sleep(random.randint(*DELAY_RANGE))


def _default_fetcher(word, type_hint=""):
    """Fetch a word's pages with a slow-drip pause between summary/conjugation."""
    return fetch_word_pages(word, pause=_blocking_drip, type_hint=type_hint)


async def run_job(
    db_path,
    job_id,
    *,
    fetcher=_default_fetcher,
    delay=None,
    cooldown=None,
    on_progress=None,
    ban_limit=BAN_STREAK_LIMIT,
    transient_limit=TRANSIENT_STREAK_LIMIT,
    max_cooldowns=MAX_TRANSIENT_COOLDOWNS,
    fetch_retries=FETCH_RETRIES,
):
    """Run job `job_id` to a terminal status; returns that status.

    - fetcher(word, type_hint) -> (summary_html, conj_html), run in a thread; raises on a
      failed fetch. A transient failure (proxy/5xx, see `_fetch_error_kind`) is retried
      up to `fetch_retries` times (slow-drip between attempts) before the item is
      recorded as 'error'; a 404/block fails on the first try.
    - delay: async callable awaited between items (defaults to random 20-50s).
    - cooldown: async callable awaited after a transient wave (defaults to 10 min).
    - on_progress(job, counts, done): optional async callback for the UI.

    Terminal-stop policy: only a run of `ban_limit` real BLOCK responses (403/429/throttle)
    stops the job ('error'). A run of `transient_limit` site-overload failures (5xx/proxy)
    triggers a `cooldown` and the job keeps going — up to `max_cooldowns` waves before it
    gives up. Genuine 404s never accrue toward either, so absent words don't halt the batch.
    """
    if delay is None:
        async def delay():
            await asyncio.sleep(random.randint(*DELAY_RANGE))
    if cooldown is None:
        async def cooldown():
            await asyncio.sleep(TRANSIENT_COOLDOWN_SEC)

    async def _finish(status):
        set_job_status(db_path, job_id, status)
        if on_progress:
            await on_progress(get_job(db_path, job_id), job_counts(db_path, job_id), done=True)
        return status

    ban_streak = 0
    transient_streak = 0
    cooldowns_used = 0
    while True:
        job = get_job(db_path, job_id)
        if not job or job["status"] == "stopped":
            # a manual stop is still terminal: hand the partial results to the UI
            # (which sends the review file) just like a normal completion.
            if on_progress and job:
                await on_progress(job, job_counts(db_path, job_id), done=True)
            return job["status"] if job else "stopped"
        if job["status"] == "paused":
            await asyncio.sleep(PAUSE_POLL_SEC)
            continue

        item = next_pending_item(db_path, job_id)
        if item is None:
            return await _finish("done")

        word = item["word"]
        type_hint = item.get("type_hint") or ""
        attempt = 0
        error_kind = None
        while True:
            try:
                summary, conj = await asyncio.to_thread(fetcher, word, type_hint)
                entry = parse_de(summary, conj, query=word, topic=job["topic"], type_hint=type_hint)
                save_item_result(db_path, item["id"], "done", result=entry, flags=",".join(entry.get("_flags") or []))
                if item.get("word_id"):  # recheck item -> stamp the word as verified (v13)
                    mark_word_vf_checked(db_path, item["word_id"])
                error_kind = None
                break
            except Exception as exc:  # noqa: BLE001 - one bad word must not kill the batch
                kind = _fetch_error_kind(exc)
                if attempt < fetch_retries and kind == "transient":
                    attempt += 1
                    await delay()  # slow-drip before retrying a transient fetch failure
                    continue
                suffix = f" (after {attempt} retr{'y' if attempt == 1 else 'ies'})" if attempt else ""
                save_item_result(db_path, item["id"], "error", error=str(exc)[:280] + suffix)
                error_kind = kind
                break

        # A block accrues toward the emergency stop; a site-overload toward a cool-down.
        # A parsed word or a genuine 404 means the site answered and isn't blocking us,
        # so both streaks reset.
        if error_kind == "ban":
            ban_streak += 1
            transient_streak = 0
        elif error_kind == "transient":
            transient_streak += 1
            ban_streak = 0
        else:
            ban_streak = 0
            transient_streak = 0

        if on_progress:
            await on_progress(get_job(db_path, job_id), job_counts(db_path, job_id), done=False)

        if ban_streak >= ban_limit:
            return await _finish("error")  # sustained block -> stop before we look banned

        if transient_streak >= transient_limit:
            cooldowns_used += 1
            if cooldowns_used > max_cooldowns:
                return await _finish("error")  # site stayed down through the whole budget
            transient_streak = 0
            await cooldown()  # wait out the rough patch instead of hammering it
            continue          # re-check status, then keep going (skip the short delay)

        await delay()
