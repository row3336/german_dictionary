"""Background job: learning "N ready" reminders per active batch.

NOTE: per the owner this reminder feature is not fully working yet (see
docs/log.md / memory). Extraction is behavior-preserving; the logic is unchanged.
"""

from datetime import datetime, timezone

from telegram.ext import Application, ContextTypes

from ..app_config import LEARNING_REMINDER_JOB_NAME, get_db_path
from ..common.settings import (
    _learning_ready_quorum,
    _learning_reminder_cooldown,
    _learning_reminder_interval,
)
from ..storage import (
    learning_active_batches,
    learning_batch_mark_notified,
    learning_batch_update_ready_state,
    learning_status_snapshot,
)


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
    reminder_interval = _learning_reminder_interval(db_path)
    reminder_cooldown = _learning_reminder_cooldown(db_path)
    quorum_pct = _learning_ready_quorum(db_path)

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


def _configure_learning_reminders(app: Application):
    """Handle configure learning reminders."""
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
