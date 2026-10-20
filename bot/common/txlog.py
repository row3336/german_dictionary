"""Transaction-log helpers (snapshot capture + log write for /undo)."""

from ..app_config import get_db_path
from ..storage import add_transaction_log, snapshot_user_word_state

# context.user_data keys for the live /undo counter message (see undo_steps):
# consecutive /undo presses edit ONE message in place instead of spamming a new
# one each time; any other action resets it via reset_undo_progress().
UNDO_MSG_ID_KEY = "undo_progress_msg_id"
UNDO_TOTAL_KEY = "undo_progress_total"


def reset_undo_progress(context):
    """Drop the live /undo counter so the next /undo starts a fresh message."""
    context.user_data.pop(UNDO_MSG_ID_KEY, None)
    context.user_data.pop(UNDO_TOTAL_KEY, None)


def _capture_transaction_snapshot(user_id, word_id, modes, *, conn=None):
    """Handle capture transaction snapshot."""
    if not word_id:
        return None
    if conn is not None:
        from ..study.repository import capture_snapshot
        return capture_snapshot(conn, user_id, word_id, modes)
    return snapshot_user_word_state(get_db_path(), user_id=user_id, word_id=word_id, modes=modes)


def _log_transaction(user_id, action, snapshot, *, conn=None, action_key=None):
    """Handle log transaction."""
    if not snapshot:
        return
    if conn is not None:
        from ..study.repository import append_transaction
        return append_transaction(conn, user_id, action,
                                  {"snapshot": snapshot, "action_key": action_key})
    return add_transaction_log(
        get_db_path(),
        user_id=user_id,
        action=action,
        payload={"snapshot": snapshot},
    )
