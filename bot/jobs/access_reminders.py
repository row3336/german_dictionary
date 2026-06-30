"""Background reminder to the owner about unhandled access requests (P3.1).

Every 2 hours (ACCESS_REMINDER_INTERVAL_SECONDS), if any users are 'pending',
sends the owner a digest list. The digest carries no live
#user_confirmation_request hashtag by default (so the open-hashtag search isn't
polluted by every reminder); instead it offers a toggle button that reveals the
searchable hashtag on demand and hides it again — see access.pending_tag_keyboard.
"""

import logging

from telegram.ext import Application, ContextTypes

from ..app_config import (
    ACCESS_REMINDER_INTERVAL_SECONDS,
    ACCESS_REMINDER_JOB_NAME,
    get_db_path,
    get_owner_id,
)
from ..common.lang import _user_interface_lang
from ..handlers.access import pending_tag_keyboard
from ..i18n import t
from ..storage import list_pending_users

logger = logging.getLogger(__name__)


def _user_tag(user):
    """Digest label for a user: @username, else first name, else id."""
    if user.get("username"):
        return "@" + user["username"]
    return user.get("first_name") or str(user.get("user_id") or "?")


async def _access_pending_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """Remind the owner about pending access requests (digest)."""
    owner_id = get_owner_id()
    if not owner_id:
        return
    pending = list_pending_users(get_db_path())
    if not pending:
        return
    lang = _user_interface_lang(owner_id)
    lines = [f"⏰ {len(pending)} {t(lang, 'access_reminder_owner')}:"]
    for user in pending:
        lines.append(f"• {_user_tag(user)} (id {user['user_id']})")
    # No hashtag in the body: the "show tag" button reveals it on demand instead,
    # so the open-requests hashtag search isn't cluttered by every reminder.
    try:
        await context.bot.send_message(
            chat_id=owner_id,
            text="\n".join(lines),
            reply_markup=pending_tag_keyboard(lang, shown=False),
        )
    except Exception:
        logger.warning("Failed to send the access-requests reminder", exc_info=True)


def _configure_access_reminders(app: Application):
    """Schedule the repeating access-requests reminder job."""
    job_queue = app.job_queue
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(ACCESS_REMINDER_JOB_NAME):
        job.schedule_removal()
    job_queue.run_repeating(
        _access_pending_reminder_job,
        interval=ACCESS_REMINDER_INTERVAL_SECONDS,
        first=ACCESS_REMINDER_INTERVAL_SECONDS,
        name=ACCESS_REMINDER_JOB_NAME,
    )
