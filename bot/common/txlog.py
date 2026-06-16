"""Transaction-log helpers (snapshot capture + log write for /undo)."""

from ..app_config import get_db_path
from ..storage import add_transaction_log, snapshot_user_word_state


def _capture_transaction_snapshot(user_id, word_id, modes):
    """Handle capture transaction snapshot."""
    if not word_id:
        return None
    return snapshot_user_word_state(get_db_path(), user_id=user_id, word_id=word_id, modes=modes)


def _log_transaction(user_id, action, snapshot):
    """Handle log transaction."""
    if not snapshot:
        return
    add_transaction_log(
        get_db_path(),
        user_id=user_id,
        action=action,
        payload={"snapshot": snapshot},
    )
