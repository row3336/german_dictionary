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
BAN_STREAK_LIMIT = 10   # consecutive fetch errors -> emergency stop (possible ban)
PAUSE_POLL_SEC = 3      # how often a paused job re-checks its status
FETCH_RETRIES = 2       # extra attempts for a transient (proxy/5xx) fetch failure

# HTTP codes worth another attempt: 0 = proxy/transport error (the flaky-proxy
# case), plus throttle/5xx. A real 404 (word genuinely absent) is NOT retried.
_RETRYABLE_HTTP = (0, 429, 500, 502, 503, 504)


def _is_retryable_fetch_error(exc):
    """True for a transient fetch failure (proxy/network/throttle/5xx) that a
    retry may fix; False for a definitive 404 or a non-fetch error."""
    msg = str(exc)
    return any(f"http={code}" in msg for code in _RETRYABLE_HTTP)

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
    on_progress=None,
    ban_limit=BAN_STREAK_LIMIT,
    fetch_retries=FETCH_RETRIES,
):
    """Run job `job_id` to a terminal status; returns that status.

    - fetcher(word, type_hint) -> (summary_html, conj_html), run in a thread; raises on a
      failed fetch. A transient failure (proxy/5xx, see `_is_retryable_fetch_error`)
      is retried up to `fetch_retries` times (slow-drip between attempts) before
      the item is recorded as 'error'; a real 404 fails on the first try.
    - delay: async callable awaited between items (defaults to random 20-50s).
    - on_progress(job, counts, done): optional async callback for the UI.
    """
    if delay is None:
        async def delay():
            await asyncio.sleep(random.randint(*DELAY_RANGE))

    ban_streak = 0
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
            set_job_status(db_path, job_id, "done")
            if on_progress:
                await on_progress(get_job(db_path, job_id), job_counts(db_path, job_id), done=True)
            return "done"

        word = item["word"]
        type_hint = item.get("type_hint") or ""
        attempt = 0
        while True:
            try:
                summary, conj = await asyncio.to_thread(fetcher, word, type_hint)
                entry = parse_de(summary, conj, query=word, topic=job["topic"], type_hint=type_hint)
                save_item_result(db_path, item["id"], "done", result=entry, flags=",".join(entry.get("_flags") or []))
                ban_streak = 0
                break
            except Exception as exc:  # noqa: BLE001 - one bad word must not kill the batch
                if attempt < fetch_retries and _is_retryable_fetch_error(exc):
                    attempt += 1
                    await delay()  # slow-drip before retrying a transient fetch failure
                    continue
                suffix = f" (after {attempt} retr{'y' if attempt == 1 else 'ies'})" if attempt else ""
                save_item_result(db_path, item["id"], "error", error=str(exc)[:280] + suffix)
                ban_streak += 1
                break

        if on_progress:
            await on_progress(get_job(db_path, job_id), job_counts(db_path, job_id), done=False)

        if ban_streak >= ban_limit:
            set_job_status(db_path, job_id, "error")
            if on_progress:
                await on_progress(get_job(db_path, job_id), job_counts(db_path, job_id), done=True)
            return "error"

        await delay()
