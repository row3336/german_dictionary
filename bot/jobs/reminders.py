"""Learning continuation plus personal quiz/new-word reminder dispatch."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram.ext import Application, ContextTypes

from ..app_config import (
    FSRS_AFTERNOON_REMINDER_JOB_NAME,
    LEARNING_REMINDER_JOB_NAME,
    get_db_path,
)
from ..common.lang import _mode_l1_de, _quiz_dir_labels, _user_interface_lang
from ..common.outbound import SendStats, send_to_user
from ..common.settings import (
    _learning_ready_quorum,
    _learning_reminder_cooldown,
    _learning_reminder_interval,
)
from ..i18n import t
from ..storage import (
    select_due,
    learning_active_batches,
    learning_batch_mark_notified,
    learning_batch_update_ready_state,
    learning_status_snapshot,
    list_active_users,
)

from ..notifications.settings import due_to_notify, mark_sent
from ..notifications.handlers import notification_inline_markup

_BERLIN_TZ = ZoneInfo("Europe/Berlin")
_FSRS_REMINDER_DATE_KEY = "fsrs_afternoon_reminder_date"
_dispatcher_running = False


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


def _now_berlin():
    return datetime.now(_BERLIN_TZ)


def _count_quiz_due(db_path, user_id, today):
    # Exact written L1->De words, independent of the user's topic/CEFR filter.
    return len(select_due(db_path, user_id=user_id, mode=_mode_l1_de(user_id),
                          today=today, exclude_correct_today=True))


async def _fsrs_afternoon_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """Minute dispatcher; legacy job name/day-key preserve restart dedup.

    A successful Telegram send and the SQLite day marker are not atomic. A crash
    between them can cause a duplicate; no exactly-once delivery is claimed.
    """
    global _dispatcher_running
    if _dispatcher_running:
        return
    _dispatcher_running = True
    try:
        db_path = get_db_path()
        stats = {"quiz": SendStats(), "new_words": SendStats()}
        for user_id in list_active_users(db_path):
            for kind in ("quiz", "new_words"):
                now = _now_berlin()
                today = now.date().isoformat()
                if not due_to_notify(db_path, user_id, kind, now):
                    continue
                lang = _user_interface_lang(user_id)
                if kind == "quiz":
                    total = _count_quiz_due(db_path, user_id, now.date())
                    if total <= 0:
                        # Late readiness must remain eligible for a later tick.
                        continue
                    text = t(lang, "reminder_fsrs_due").format(
                        total=total, dir=_quiz_dir_labels(user_id)[0],
                    )
                    send_kind = "fsrs_reminder"
                else:
                    text = t(lang, "reminder_new_words")
                    send_kind = "new_words_reminder"
                # Re-read after any prior await; never send a previous day's catch-up.
                current = _now_berlin()
                if current.date().isoformat() != today or not due_to_notify(db_path, user_id, kind, current):
                    continue
                result = await send_to_user(
                    context.bot, user_id, text, send_kind, stats=stats[kind],
                    reply_markup=notification_inline_markup(user_id, kind),
                )
                if result == "ok":
                    mark_sent(db_path, user_id, kind, today)
        stats["quiz"].log("fsrs_reminder")
        stats["new_words"].log("new_words_reminder")
    finally:
        _dispatcher_running = False


def _configure_learning_reminders(app: Application):
    """Keep Learning continuation and schedule one personal dispatcher each minute."""
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
    # Replace the previous evening job rather than creating a parallel sender.
    job_queue.run_repeating(
        _fsrs_afternoon_reminder_job,
        interval=60,
        first=60,
        name=FSRS_AFTERNOON_REMINDER_JOB_NAME,
    )
