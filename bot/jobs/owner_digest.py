"""spec #5: owner reports/feedback digest — 3×/day, dedup per window.

Ticks every 10 min and self-gates by Berlin hour + a per-window dedup date (same
pattern as the retention broadcast), so it still fires correctly after restarts.
An empty digest (0 reports AND 0 feedback) is never sent — and, since we only
mark the window once a digest actually goes out, items that appear later within
the same window are still delivered on a following tick.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from telegram.ext import Application, ContextTypes

from ..app_config import get_db_path, get_owner_id
from ..common.lang import _user_interface_lang
from ..common.outbound import SendStats, send_to_user
from ..handlers.owner_inbox import _count_open, digest_home_markup, digest_text
from ..storage import get_setting, set_setting

_BERLIN_TZ = ZoneInfo("Europe/Berlin")
# Berlin hours the digest goes out at (~04:30 / 14:00 / 19:00 — hour-gated).
OWNER_DIGEST_WINDOWS = (4, 14, 19)
_WINDOW_HOURS = 2
OWNER_DIGEST_JOB_NAME = "owner_digest_job"


def _digest_setting_key(hour):
    return f"owner_digest_{int(hour)}_date"


def _due_window(now, sent_dates):
    """The window-hour whose window contains `now` and wasn't sent today, else None.

    `sent_dates`: {window_hour: iso_date_already_sent}. Pure/testable.
    """
    today = now.date().isoformat()
    for hour in OWNER_DIGEST_WINDOWS:
        if hour <= now.hour < hour + _WINDOW_HOURS and sent_dates.get(hour) != today:
            return hour
    return None


async def _owner_digest_job(context: ContextTypes.DEFAULT_TYPE):
    db_path = get_db_path()
    owner_id = get_owner_id()
    if not owner_id:
        return
    now = datetime.now(_BERLIN_TZ)
    sent = {h: get_setting(db_path, _digest_setting_key(h), "") for h in OWNER_DIGEST_WINDOWS}
    window = _due_window(now, sent)
    if window is None:
        return
    reports_n, feedback_n = _count_open()
    if reports_n == 0 and feedback_n == 0:
        return  # empty digest is never sent (and the window stays unmarked)
    lang = _user_interface_lang(owner_id)
    send_stats = SendStats()
    result = await send_to_user(
        context.bot, owner_id, digest_text(lang, reports_n, feedback_n),
        "owner_digest", stats=send_stats, reply_markup=digest_home_markup(lang),
    )
    if result != "error":
        set_setting(db_path, _digest_setting_key(window), now.date().isoformat())
    send_stats.log("owner_digest")


def _configure_owner_digest(app: Application):
    """Schedule the owner digest (self-gates by window; ticks every 10 min)."""
    job_queue = app.job_queue
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(OWNER_DIGEST_JOB_NAME):
        job.schedule_removal()
    job_queue.run_repeating(
        _owner_digest_job, interval=600, first=90, name=OWNER_DIGEST_JOB_NAME,
    )
