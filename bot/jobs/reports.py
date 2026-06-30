"""Background jobs: daily FSRS due-report, peak-window notify, retention reports."""

import logging
from datetime import date, datetime, timedelta, timezone

from telegram.ext import Application, ContextTypes

from ..app_config import (
    FSRS_PEAK_JOB_NAME,
    FSRS_PEAK_NOTIFY_DATE_KEY,
    FSRS_REPORT_DATE_KEY,
    FSRS_REPORT_JOB_NAME,
    FSRS_WEEKLY_REPORT_WEEK_KEY,
    get_db_path,
    get_owner_id,
)
from ..common.lang import _mode_de_l1, _mode_l1_de, _user_interface_lang
from ..common.outbound import SendStats, send_to_user
from ..common.settings import _fsrs_report_hour_utc
from ..i18n import t
from ..storage import (
    count_learning_done_in_week,
    fsrs_due_forecast,
    get_daily_first_touch_retention,
    get_setting,
    get_user_setting,
    get_weekly_first_touch_retention,
    list_active_users,
    set_setting,
    set_user_setting,
)

# Spread the nightly retention broadcast over the night: each tick sends to at
# most this many not-yet-notified users, so sends drain over several ticks
# instead of bursting (AIORateLimiter paces the within-tick rate on top).
RETENTION_BROADCAST_JOB_NAME = "retention_broadcast_job"
_RETENTION_BATCH_PER_TICK = 5


def _retention_zone(lang, rate):
    """Localized retention-zone label for a first-touch rate.

    Five directional zones (owner-approved): <70% overload (too hard),
    70-80% below optimal, 80-90% optimal, 90-95% slightly easy, >95% too easy.
    Splitting the old catch-all "outside" preserves the direction (overload vs
    too easy), so the advice points the right way.
    """
    if rate < 70.0:
        key = "report_zone_overload"
    elif rate < 80.0:
        key = "report_zone_below_optimal"
    elif rate <= 90.0:
        key = "report_zone_optimal"
    elif rate <= 95.0:
        key = "report_zone_slightly_easy"
    else:
        key = "report_zone_too_easy"
    return t(lang, key)


def _format_retention_daily_text(retention, lang):
    """Localized daily first-touch retention text."""
    day = retention.get("date", "")
    total = int(retention.get("total") or 0)
    correct = int(retention.get("correct") or 0)
    rate = float(retention.get("rate") or 0.0)
    if total <= 0:
        return t(lang, "report_retention_daily_nodata").format(day=day)
    return t(lang, "report_retention_daily").format(
        day=day, rate=f"{rate:.1f}", correct=correct, total=total,
        zone=_retention_zone(lang, rate),
    )


def _format_retention_weekly_text(retention, learning_done, lang):
    """Localized weekly first-touch retention text."""
    ws = retention.get("week_start", "")
    we = retention.get("week_end", "")
    total = int(retention.get("total") or 0)
    correct = int(retention.get("correct") or 0)
    rate = float(retention.get("rate") or 0.0)
    if total <= 0:
        return t(lang, "report_retention_weekly_nodata").format(
            ws=ws, we=we, learning=learning_done,
        )
    return t(lang, "report_retention_weekly").format(
        ws=ws, we=we, rate=f"{rate:.1f}", correct=correct, total=total,
        zone=_retention_zone(lang, rate), learning=learning_done,
    )


def _format_fsrs_report_text(total, peak_hour, peak_count):
    """Handle format fsrs report text."""
    if total <= 0:
        return "FSRS today: 0 due."
    if peak_hour is None:
        return f"FSRS today: {total} due."
    return (
        f"FSRS today: {total} due.\n"
        f"Peak hour ~{peak_hour:02d}:00 UTC (approx), count {peak_count}."
    )


def _should_send_fsrs_daily_report(db_path, now_utc):
    """Handle should send fsrs daily report."""
    today = now_utc.date().isoformat()
    last_date = get_setting(db_path, FSRS_REPORT_DATE_KEY, "")
    if last_date == today:
        return False
    report_hour = _fsrs_report_hour_utc(db_path)
    return now_utc.hour >= report_hour


async def _fsrs_daily_report_job(context: ContextTypes.DEFAULT_TYPE):
    """Owner-only FSRS forecast + peak-window state (retention moved to P5.3 broadcast)."""
    db_path = get_db_path()
    owner_id = get_owner_id()
    if not owner_id:
        return
    now = datetime.now(timezone.utc)
    if not _should_send_fsrs_daily_report(db_path, now):
        return
    today = now.date().isoformat()
    mode = _mode_l1_de(owner_id)
    forecast = fsrs_due_forecast(db_path, owner_id, mode)
    total = int(forecast.get("total", 0))
    peak_hour = forecast.get("peak_hour")
    peak_count = int(forecast.get("peak_count", 0))
    text = _format_fsrs_report_text(total, peak_hour, peak_count)
    try:
        await context.bot.send_message(
            chat_id=owner_id,
            text=f"```text\n{text}\n```",
            parse_mode="Markdown",
        )
        set_setting(db_path, FSRS_REPORT_DATE_KEY, today)
        set_setting(db_path, "fsrs_peak_hour_today", "")
        set_setting(db_path, FSRS_PEAK_NOTIFY_DATE_KEY, "")
        if peak_hour is not None:
            set_setting(db_path, "fsrs_peak_hour_today", str(int(peak_hour)))
    except Exception:
        logging.exception("FSRS daily report failed")
        return


async def _retention_broadcast_job(context: ContextTypes.DEFAULT_TYPE):
    """P5.3 — broadcast per-user retention (daily + weekly-on-Monday) to all users.

    Each user gets their OWN numbers, in their own language, once per day. Sends
    are spread over the night: starting at the report hour, each tick handles at
    most `_RETENTION_BATCH_PER_TICK` not-yet-notified users (per-user dedup keys),
    so the broadcast drains over several ticks instead of bursting.
    """
    db_path = get_db_path()
    now = datetime.now(timezone.utc)
    report_hour = _fsrs_report_hour_utc(db_path)
    if now.hour < report_hour:
        return
    today = now.date().isoformat()
    report_day = (now.date() - timedelta(days=1)).isoformat()
    is_monday = now.weekday() == 0
    prev_ws_iso = ""
    if is_monday:
        prev_ref = now.date() - timedelta(days=7)
        prev_ws_iso = (prev_ref - timedelta(days=prev_ref.weekday())).isoformat()

    send_stats = SendStats()
    handled = 0
    for user_id in list_active_users(db_path):
        if handled >= _RETENTION_BATCH_PER_TICK:
            break
        if get_user_setting(db_path, user_id, FSRS_REPORT_DATE_KEY, "") == today:
            continue
        lang = _user_interface_lang(user_id)
        modes = (_mode_l1_de(user_id), _mode_de_l1(user_id))
        retention = get_daily_first_touch_retention(
            db_path, user_id=user_id, target_date=report_day, modes=modes,
        )
        daily_text = _format_retention_daily_text(retention, lang)
        result = await send_to_user(
            context.bot, user_id, f"```text\n{daily_text}\n```",
            "retention_daily", stats=send_stats, parse_mode="Markdown",
        )
        # Weekly is handled in the same pass (before marking the daily dedup) so a
        # user never gets the daily mark without their pending weekly.
        if is_monday and get_user_setting(db_path, user_id, FSRS_WEEKLY_REPORT_WEEK_KEY, "") != prev_ws_iso:
            weekly = get_weekly_first_touch_retention(
                db_path, user_id=user_id, week_start=prev_ws_iso, modes=modes,
            )
            learning_done = count_learning_done_in_week(db_path, user_id, week_start=prev_ws_iso)
            weekly_text = _format_retention_weekly_text(weekly, learning_done, lang)
            await send_to_user(
                context.bot, user_id, f"```text\n{weekly_text}\n```",
                "retention_weekly", stats=send_stats, parse_mode="Markdown",
            )
            set_user_setting(db_path, user_id, FSRS_WEEKLY_REPORT_WEEK_KEY, prev_ws_iso)
        if result != "error":
            set_user_setting(db_path, user_id, FSRS_REPORT_DATE_KEY, today)
        handled += 1

    send_stats.log("retention")


async def _fsrs_peak_notify_job(context: ContextTypes.DEFAULT_TYPE):
    """Handle fsrs peak notify job."""
    db_path = get_db_path()
    owner_id = get_owner_id()
    if not owner_id:
        return
    today = date.today().isoformat()
    peak_notified = get_setting(db_path, FSRS_PEAK_NOTIFY_DATE_KEY, "")
    if peak_notified == today:
        return
    report_date = get_setting(db_path, FSRS_REPORT_DATE_KEY, "")
    if report_date != today:
        return
    peak_hour_raw = get_setting(db_path, "fsrs_peak_hour_today", "")
    try:
        peak_hour = int(peak_hour_raw)
    except (TypeError, ValueError):
        return
    now = datetime.now(timezone.utc)
    if now.hour < peak_hour:
        return
    mode = _mode_l1_de(owner_id)
    forecast = fsrs_due_forecast(db_path, owner_id, mode)
    total = int(forecast.get("total", 0))
    if total <= 0:
        return
    try:
        await context.bot.send_message(
            chat_id=owner_id,
            text=f"FSRS peak window now. Due today: {total}.",
        )
        set_setting(db_path, FSRS_PEAK_NOTIFY_DATE_KEY, today)
    except Exception:
        logging.exception("FSRS peak notify failed")
        return


def _configure_fsrs_reports(app: Application):
    """Handle configure fsrs reports."""
    job_queue = app.job_queue
    if job_queue is None:
        return
    for name in (FSRS_REPORT_JOB_NAME, FSRS_PEAK_JOB_NAME, RETENTION_BROADCAST_JOB_NAME):
        for job in job_queue.get_jobs_by_name(name):
            job.schedule_removal()
    # Run frequently and guard by date/hour so reports still work after restarts.
    job_queue.run_repeating(
        _fsrs_daily_report_job,
        interval=600,
        first=30,
        name=FSRS_REPORT_JOB_NAME,
    )
    job_queue.run_repeating(
        _fsrs_peak_notify_job,
        interval=900,
        first=900,
        name=FSRS_PEAK_JOB_NAME,
    )
    # Per-user retention broadcast: ticks every 10 min, drains the night window
    # in small batches (the job itself gates by report hour + per-user dedup).
    job_queue.run_repeating(
        _retention_broadcast_job,
        interval=600,
        first=120,
        name=RETENTION_BROADCAST_JOB_NAME,
    )
