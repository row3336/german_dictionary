"""Background job: scheduled auto-dump of the DB (daily/weekly), sent to owner."""

import logging
from datetime import time, timezone

from telegram.ext import Application, ContextTypes

from ..app_config import AUTO_DUMP_JOB_NAME, get_db_path, get_owner_id
from ..common.dumps import _create_dump_file
from ..storage import cleanup_transaction_logs


async def _auto_dump_job(context: ContextTypes.DEFAULT_TYPE):
    """Handle auto dump job."""
    try:
        dump_path = _create_dump_file()
    except Exception:
        return

    owner_id = get_owner_id()
    if owner_id:
        try:
            await context.bot.send_document(
                chat_id=owner_id,
                document=dump_path.open("rb"),
                filename=dump_path.name,
                caption=f"✅ Auto dump created: {dump_path.name}",
            )
        except Exception:
            logging.exception("Auto dump job failed")
    cleanup_transaction_logs(get_db_path(), days=7)


def _configure_auto_dump(app: Application, mode: str):
    """Handle configure auto dump."""
    job_queue = app.job_queue
    if job_queue is None:
        return

    for job in job_queue.get_jobs_by_name(AUTO_DUMP_JOB_NAME):
        job.schedule_removal()

    mode = (mode or "off").lower()
    if mode == "daily":
        job_queue.run_daily(
            _auto_dump_job,
            time=time(hour=0, minute=0, tzinfo=timezone.utc),
            name=AUTO_DUMP_JOB_NAME,
        )
    elif mode == "weekly":
        job_queue.run_daily(
            _auto_dump_job,
            time=time(hour=0, minute=0, tzinfo=timezone.utc),
            days=(0,),
            name=AUTO_DUMP_JOB_NAME,
        )
