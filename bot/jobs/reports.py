"""Background job: per-user retention reports (daily + weekly-on-Monday).

The owner-only FSRS forecast + peak-window notify were removed: the 18:00 Berlin
evening reminder already nudges about unfinished due words, and the live stats
show the ready count at any moment, so those two extra owner sends were redundant.
"""

import logging
from datetime import datetime, timedelta, timezone

from telegram.ext import Application, ContextTypes

from ..app_config import (
    FSRS_REPORT_DATE_KEY,
    FSRS_WEEKLY_REPORT_WEEK_KEY,
    get_db_path,
)
from ..common.lang import _mode_de_l1, _mode_l1_de, _user_interface_lang
from ..common.outbound import SendStats, send_to_user
from ..common.settings import _fsrs_report_hour_utc
from ..i18n import t
from ..storage import (
    count_learning_done_in_week,
    get_daily_first_touch_retention,
    get_user_setting,
    get_weekly_first_touch_retention,
    list_active_users,
    set_user_setting,
)

# Spread the retention broadcast over the morning window: each tick sends to at
# most this many not-yet-notified users, so sends drain over several ticks
# instead of bursting (AIORateLimiter paces the within-tick rate on top).
RETENTION_BROADCAST_JOB_NAME = "retention_broadcast_job"
_RETENTION_BATCH_PER_TICK = 5
# Bug #21: send ONLY in a narrow morning window [report_hour, report_hour+N), not
# all day — so the report lands once in the morning and never mid-day. The window
# must be wide enough to drain the user base in batches (N hours x 6 ticks/hour x
# batch). Default report_hour is early UTC, so report_hour+N stays within the day.
_RETENTION_WINDOW_HOURS = 3


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


async def _retention_broadcast_job(context: ContextTypes.DEFAULT_TYPE):
    """P5.3 — broadcast per-user retention (daily + weekly-on-Monday) to all users.

    Each user gets their OWN numbers, in their own language, once per day. Bug #21:
    the broadcast runs ONLY in a narrow morning window (so it lands once in the
    morning, never mid-day), and a report with no data (total==0) is skipped
    entirely — a brand-new or idle user gets nothing instead of a "no data"
    message. The window drains in batches of `_RETENTION_BATCH_PER_TICK` per tick,
    gated by per-user dedup keys.
    """
    db_path = get_db_path()
    now = datetime.now(timezone.utc)
    report_hour = _fsrs_report_hour_utc(db_path)
    if not (report_hour <= now.hour < report_hour + _RETENTION_WINDOW_HOURS):
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
        # Skip the daily send when there is nothing to report (bug #21). No send =
        # no error, so we still mark the dedup below and won't retry this user.
        daily_ok = True
        if int(retention.get("total") or 0) > 0:
            daily_text = _format_retention_daily_text(retention, lang)
            result = await send_to_user(
                context.bot, user_id, f"```text\n{daily_text}\n```",
                "retention_daily", stats=send_stats, parse_mode="Markdown",
            )
            daily_ok = result != "error"
        # Weekly is handled in the same pass (before marking the daily dedup) so a
        # user never gets the daily mark without their pending weekly.
        if is_monday and get_user_setting(db_path, user_id, FSRS_WEEKLY_REPORT_WEEK_KEY, "") != prev_ws_iso:
            weekly = get_weekly_first_touch_retention(
                db_path, user_id=user_id, week_start=prev_ws_iso, modes=modes,
            )
            if int(weekly.get("total") or 0) > 0:
                learning_done = count_learning_done_in_week(db_path, user_id, week_start=prev_ws_iso)
                weekly_text = _format_retention_weekly_text(weekly, learning_done, lang)
                await send_to_user(
                    context.bot, user_id, f"```text\n{weekly_text}\n```",
                    "retention_weekly", stats=send_stats, parse_mode="Markdown",
                )
            # Mark the week handled even when empty (skipped), so we don't recompute
            # it on every Monday tick.
            set_user_setting(db_path, user_id, FSRS_WEEKLY_REPORT_WEEK_KEY, prev_ws_iso)
        if daily_ok:
            set_user_setting(db_path, user_id, FSRS_REPORT_DATE_KEY, today)
        handled += 1

    send_stats.log("retention")


def _configure_fsrs_reports(app: Application):
    """Schedule the per-user retention broadcast (forecast/peak jobs were removed)."""
    job_queue = app.job_queue
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(RETENTION_BROADCAST_JOB_NAME):
        job.schedule_removal()
    # Per-user retention broadcast: ticks every 10 min, drains the morning window
    # in small batches (the job itself gates by the window + per-user dedup).
    job_queue.run_repeating(
        _retention_broadcast_job,
        interval=600,
        first=120,
        name=RETENTION_BROADCAST_JOB_NAME,
    )
