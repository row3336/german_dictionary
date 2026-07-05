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

# job_id -> asyncio.Task, so the UI can tell whether a job is already running.
VF_ADD_TASKS = {}


def _blocking_drip():
    """Blocking slow-drip delay (runs inside the fetch worker thread)."""
    time.sleep(random.randint(*DELAY_RANGE))


def _default_fetcher(word):
    """Fetch a word's pages with a slow-drip pause between summary/conjugation."""
    return fetch_word_pages(word, pause=_blocking_drip)


async def run_job(
    db_path,
    job_id,
    *,
    fetcher=_default_fetcher,
    delay=None,
    on_progress=None,
    ban_limit=BAN_STREAK_LIMIT,
):
    """Run job `job_id` to a terminal status; returns that status.

    - fetcher(word) -> (summary_html, conj_html), run in a thread; raises on a
      failed fetch (item recorded as 'error').
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
        try:
            summary, conj = await asyncio.to_thread(fetcher, word)
            entry = parse_de(summary, conj, query=word, topic=job["topic"])
            save_item_result(db_path, item["id"], "done", result=entry, flags=",".join(entry.get("_flags") or []))
            ban_streak = 0
        except Exception as exc:  # noqa: BLE001 - one bad word must not kill the batch
            save_item_result(db_path, item["id"], "error", error=str(exc)[:300])
            ban_streak += 1

        if on_progress:
            await on_progress(get_job(db_path, job_id), job_counts(db_path, job_id), done=False)

        if ban_streak >= ban_limit:
            set_job_status(db_path, job_id, "error")
            if on_progress:
                await on_progress(get_job(db_path, job_id), job_counts(db_path, job_id), done=True)
            return "error"

        await delay()
