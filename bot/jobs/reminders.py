"""Background jobs: two independent reminders.

1. Learning-queue reminder — while a learning batch is active, once a quorum of
   its words are due ("ready"), ping the owner (interval before first ping,
   cooldown between repeats).
2. FSRS-queue reminder — once per day at/after 18:00 Europe/Berlin (DST-aware),
   if the FSRS review queue still has due words, ping the owner.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram.ext import Application, ContextTypes

from ..app_config import (
    FSRS_AFTERNOON_REMINDER_JOB_NAME,
    LEARNING_REMINDER_JOB_NAME,
    get_db_path,
)
from ..common.lang import _mode_l1_de, _user_interface_lang
from ..common.outbound import SendStats, send_to_user
from ..common.settings import (
    _learning_ready_quorum,
    _learning_reminder_cooldown,
    _learning_reminder_interval,
)
from ..i18n import t
from ..storage import (
    fsrs_due_forecast,
    get_user_setting,
    learning_active_batches,
    learning_batch_mark_notified,
    learning_batch_update_ready_state,
    learning_status_snapshot,
    list_active_users,
    set_user_setting,
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
    # Broadcast to every user with an active batch (P5.2): timings/quorum and the
    # message language are read per-user (owner override -> global -> default).
    send_stats = SendStats()

    for batch in learning_active_batches(db_path):
        user_id = int(batch.get("user_id") or 0)
        batch_id = str(batch.get("id") or "")
        if not user_id or not batch_id:
            continue
        quorum_pct = _learning_ready_quorum(db_path, user_id)
        reminder_interval = _learning_reminder_interval(db_path, user_id)
        reminder_cooldown = _learning_reminder_cooldown(db_path, user_id)
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
        lang = _user_interface_lang(user_id)
        text = t(lang, "reminder_learning_ready").format(
            due=stats.get("due_count", 0),
            need=stats.get("ready_needed", 0),
        )
        result = await send_to_user(
            context.bot, user_id, text, "learning_reminder", stats=send_stats,
        )
        if result != "error":
            learning_batch_mark_notified(db_path, batch_id=batch_id, now_iso=now_iso)

    send_stats.log("learning_reminder")


async def _fsrs_afternoon_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """FSRS-queue reminder to ALL users: 18:00 Berlin, once/day each, only if due."""
    db_path = get_db_path()
    now_berlin = datetime.now(_BERLIN_TZ)
    if now_berlin.hour < _FSRS_REMINDER_HOUR_BERLIN:
        return

    today = now_berlin.date().isoformat()
    send_stats = SendStats()
    for user_id in list_active_users(db_path):
        # Per-user once-a-day dedup (was a single global key before P5).
        if get_user_setting(db_path, user_id, _FSRS_REMINDER_DATE_KEY, "") == today:
            continue
        mode = _mode_l1_de(user_id)
        total = int(fsrs_due_forecast(db_path, user_id, mode).get("total", 0))
        if total <= 0:
            # Queue is done for now — no reminder, and don't mark the day done: if
            # words become due later this evening we still want to remind once.
            continue
        lang = _user_interface_lang(user_id)
        text = t(lang, "reminder_fsrs_due").format(total=total)
        result = await send_to_user(
            context.bot, user_id, text, "fsrs_reminder", stats=send_stats,
        )
        if result != "error":
            set_user_setting(db_path, user_id, _FSRS_REMINDER_DATE_KEY, today)

    send_stats.log("fsrs_reminder")


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
