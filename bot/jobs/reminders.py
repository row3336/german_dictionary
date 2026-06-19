"""Background job: afternoon FSRS reminder.

Once per day, at/after 18:00 Europe/Berlin (DST-aware), if the FSRS review queue
is not done yet (there are due words), ping the owner with how many are ready.
Replaces the earlier (non-working) learning-batch "N ready" reminder.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram.ext import Application, ContextTypes

from ..app_config import LEARNING_REMINDER_JOB_NAME, get_db_path, get_owner_id
from ..common.lang import _mode_l1_de
from ..storage import fsrs_due_forecast, get_setting, set_setting

_BERLIN_TZ = ZoneInfo("Europe/Berlin")
_REMINDER_HOUR_BERLIN = 18
_REMINDER_DATE_KEY = "fsrs_afternoon_reminder_date"


async def _learning_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """Afternoon FSRS reminder: 18:00 Berlin, once/day, only if words are due."""
    db_path = get_db_path()
    owner_id = get_owner_id()
    if not owner_id:
        return

    now_berlin = datetime.now(_BERLIN_TZ)
    if now_berlin.hour < _REMINDER_HOUR_BERLIN:
        return

    today = now_berlin.date().isoformat()
    if get_setting(db_path, _REMINDER_DATE_KEY, "") == today:
        return

    mode = _mode_l1_de(owner_id)
    total = int(fsrs_due_forecast(db_path, owner_id, mode).get("total", 0))
    if total <= 0:
        # Queue is done for now — no reminder, and don't mark the day done: if words
        # become due later this evening we still want to remind once.
        return

    try:
        await context.bot.send_message(
            chat_id=owner_id,
            text=(
                f"⏰ FSRS queue not done: {total} word(s) ready to review.\n"
                f"Open tests → Ru>De to finish today's queue."
            ),
        )
        set_setting(db_path, _REMINDER_DATE_KEY, today)
    except Exception:
        logging.exception("FSRS afternoon reminder failed")
        return


def _configure_learning_reminders(app: Application):
    """Handle configure learning reminders."""
    job_queue = app.job_queue
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(LEARNING_REMINDER_JOB_NAME):
        job.schedule_removal()
    # Run frequently and guard by Berlin hour/date so it still fires after restarts.
    job_queue.run_repeating(
        _learning_reminder_job,
        interval=600,
        first=60,
        name=LEARNING_REMINDER_JOB_NAME,
    )
