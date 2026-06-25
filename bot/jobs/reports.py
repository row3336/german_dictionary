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
from ..common.lang import _mode_de_l1, _mode_l1_de
from ..common.settings import _fsrs_report_hour_utc
from ..storage import (
    count_learning_done_in_week,
    fsrs_due_forecast,
    get_daily_first_touch_retention,
    get_setting,
    get_weekly_first_touch_retention,
    set_setting,
)


def _format_retention_daily_text(retention):
    """Handle format daily first-touch retention text."""
    day = retention.get("date", "")
    total = int(retention.get("total") or 0)
    correct = int(retention.get("correct") or 0)
    rate = float(retention.get("rate") or 0.0)
    if total <= 0:
        return f"#daily\nRetention rate ({day}, first touch): no data."
    if rate < 70.0:
        zone = "<70%: overload"
    elif 80.0 <= rate <= 90.0:
        zone = "80-90%: optimal"
    elif rate > 95.0:
        zone = ">95%: too easy (you can add more new words)"
    else:
        zone = "outside optimal range"
    return (
        f"#daily\n"
        f"Retention rate ({day}, first touch): {rate:.1f}%\n"
        f"Correct: {correct}/{total}\n"
        f"{zone}"
    )


def _format_retention_weekly_text(retention, learning_done=0):
    """Handle weekly first-touch retention text."""
    ws = retention.get("week_start", "")
    we = retention.get("week_end", "")
    total = int(retention.get("total") or 0)
    correct = int(retention.get("correct") or 0)
    rate = float(retention.get("rate") or 0.0)
    learning_line = f"Learning completed: {learning_done}"
    if total <= 0:
        return f"#weekly\nRetention rate ({ws}..{we}, first touch): no data.\n{learning_line}"
    if rate < 70.0:
        zone = "<70%: overload"
    elif 80.0 <= rate <= 90.0:
        zone = "80-90%: optimal"
    elif rate > 95.0:
        zone = ">95%: too easy (you can add more new words)"
    else:
        zone = "outside optimal range"
    return (
        f"#weekly\n"
        f"Retention rate ({ws}..{we}, first touch): {rate:.1f}%\n"
        f"Correct: {correct}/{total}\n"
        f"{zone}\n"
        f"{learning_line}"
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
    """Handle fsrs daily report job."""
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
        report_day = (now.date() - timedelta(days=1)).isoformat()
        retention = get_daily_first_touch_retention(
            db_path,
            user_id=owner_id,
            target_date=report_day,
            modes=(_mode_l1_de(owner_id), _mode_de_l1(owner_id)),
        )
        await context.bot.send_message(
            chat_id=owner_id,
            text=f"```text\n{_format_retention_daily_text(retention)}\n```",
            parse_mode="Markdown",
        )
        # Weekly report: send once on Monday after report hour, for previous week.
        report_hour = _fsrs_report_hour_utc(db_path)
        if now.weekday() == 0 and now.hour >= report_hour:
            prev_ref = now.date() - timedelta(days=7)
            prev_ws = prev_ref - timedelta(days=prev_ref.weekday())
            prev_ws_iso = prev_ws.isoformat()
            last_weekly = get_setting(db_path, FSRS_WEEKLY_REPORT_WEEK_KEY, "")
            if last_weekly != prev_ws_iso:
                weekly = get_weekly_first_touch_retention(
                    db_path,
                    user_id=owner_id,
                    week_start=prev_ws_iso,
                    modes=(_mode_l1_de(owner_id), _mode_de_l1(owner_id)),
                )
                learning_done = count_learning_done_in_week(db_path, owner_id, week_start=prev_ws_iso)
                await context.bot.send_message(
                    chat_id=owner_id,
                    text=f"```text\n{_format_retention_weekly_text(weekly, learning_done)}\n```",
                    parse_mode="Markdown",
                )
                set_setting(db_path, FSRS_WEEKLY_REPORT_WEEK_KEY, prev_ws_iso)
        set_setting(db_path, FSRS_REPORT_DATE_KEY, today)
        set_setting(db_path, "fsrs_peak_hour_today", "")
        set_setting(db_path, FSRS_PEAK_NOTIFY_DATE_KEY, "")
        if peak_hour is not None:
            set_setting(db_path, "fsrs_peak_hour_today", str(int(peak_hour)))
    except Exception:
        logging.exception("FSRS daily report failed")
        return


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
    for job in job_queue.get_jobs_by_name(FSRS_REPORT_JOB_NAME):
        job.schedule_removal()
    for job in job_queue.get_jobs_by_name(FSRS_PEAK_JOB_NAME):
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
