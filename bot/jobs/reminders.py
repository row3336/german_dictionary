"""Background jobs: two independent reminders.

1. Learning-queue reminder — while a learning batch is active, once a quorum of
   its words are due ("ready"), ping the owner (interval before first ping,
   cooldown between repeats).
2. FSRS-queue reminder — once per day at/after 18:00 Europe/Berlin (DST-aware),
   if the FSRS review queue still has due words, ping the owner.
"""

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram.ext import Application, ContextTypes

from ..app_config import (
    FSRS_AFTERNOON_REMINDER_JOB_NAME,
    LEARNING_REMINDER_JOB_NAME,
    get_db_path,
    get_owner_id,
)
from ..common.lang import _mode_l1_de
from ..common.settings import (
    _learning_ready_quorum,
    _learning_reminder_cooldown,
    _learning_reminder_interval,
)
from ..storage import (
    fsrs_due_forecast,
    get_setting,
    learning_active_batches,
    learning_batch_mark_notified,
    learning_batch_update_ready_state,
    learning_status_snapshot,
    set_setting,
)

_BERLIN_TZ = ZoneInfo("Europe/Berlin")
_FSRS_REMINDER_HOUR_BERLIN = 18
_FSRS_REMINDER_DATE_KEY = "fsrs_afternoon_reminder_date"


def _parse_utc_iso(token):
    """Handle parse utc iso."""
    raw = str(token or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


async def _learning_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """Handle learning reminder job."""
    db_path = get_db_path()
    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    # Тайминги напоминаний теперь per-user (P2.4d), но фоновый джоб пока читает
    # owner/глобал (owner override → глобал → дефолт). Per-user применим в P5.
    owner_id = get_owner_id()
    reminder_interval = _learning_reminder_interval(db_path, owner_id)
    reminder_cooldown = _learning_reminder_cooldown(db_path, owner_id)
    quorum_pct = _learning_ready_quorum(db_path, owner_id)

    for batch in learning_active_batches(db_path):
        user_id = int(batch.get("user_id") or 0)
        batch_id = str(batch.get("id") or "")
        if not user_id or not batch_id:
            continue
        stats = learning_status_snapshot(db_path, user_id=user_id, quorum_pct=quorum_pct)
        if stats.get("batch_id") != batch_id:
            continue
        active_count = int(stats.get("active_count") or 0)
        if active_count <= 0:
            continue
        ready = bool(stats.get("ready"))
        learning_batch_update_ready_state(db_path, batch_id=batch_id, ready=ready, now_iso=now_iso)
        if not ready:
            continue
        ready_since = _parse_utc_iso(stats.get("ready_since")) or now
        last_notified = _parse_utc_iso(stats.get("last_notified_at"))
        ready_elapsed = (now - ready_since).total_seconds()
        notify_elapsed = (now - last_notified).total_seconds() if last_notified else 10**9
        if ready_elapsed < reminder_interval:
            continue
        if notify_elapsed < reminder_cooldown:
            continue
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"Learning reminder:\n"
                    f"due_now = {stats.get('due_count', 0)} / need {stats.get('ready_needed', 0)}\n"
                    f"Use Learning -> learning session."
                ),
            )
            learning_batch_mark_notified(db_path, batch_id=batch_id, now_iso=now_iso)
        except Exception:
            continue


async def _fsrs_afternoon_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """FSRS-queue reminder: 18:00 Berlin, once/day, only if words are due."""
    db_path = get_db_path()
    owner_id = get_owner_id()
    if not owner_id:
        return

    now_berlin = datetime.now(_BERLIN_TZ)
    if now_berlin.hour < _FSRS_REMINDER_HOUR_BERLIN:
        return

    today = now_berlin.date().isoformat()
    if get_setting(db_path, _FSRS_REMINDER_DATE_KEY, "") == today:
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
        set_setting(db_path, _FSRS_REMINDER_DATE_KEY, today)
    except Exception:
        logging.exception("FSRS afternoon reminder failed")
        return


def _configure_learning_reminders(app: Application):
    """Schedule both the learning-queue and FSRS-afternoon reminders."""
    job_queue = app.job_queue
    if job_queue is None:
        return

    for job in job_queue.get_jobs_by_name(LEARNING_REMINDER_JOB_NAME):
        job.schedule_removal()
    job_queue.run_repeating(
        _learning_reminder_job,
        interval=60,
        first=60,
        name=LEARNING_REMINDER_JOB_NAME,
    )

    for job in job_queue.get_jobs_by_name(FSRS_AFTERNOON_REMINDER_JOB_NAME):
        job.schedule_removal()
    # Run frequently and guard by Berlin hour/date so it still fires after restarts.
    job_queue.run_repeating(
        _fsrs_afternoon_reminder_job,
        interval=600,
        first=120,
        name=FSRS_AFTERNOON_REMINDER_JOB_NAME,
    )
