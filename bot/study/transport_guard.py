"""Reject accepted study transports before a new FSM state interprets them."""

import sqlite3
from pathlib import Path

from .deliveries import lookup_delivery

LAST_TRANSPORT = "_study_dispatch_transport"


def inspect_delivery(conn, user_id, transport):
    """Connection-scoped read seam; no initialization, recovery, or mutation."""
    from ..app_config import is_owner

    user = conn.execute("SELECT status FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not is_owner(user_id) and (not user or user[0] != "approved"):
        return False, None
    delivery = lookup_delivery(conn, user_id, transport)
    if delivery and delivery.get("action_key"):
        action = conn.execute("SELECT state FROM study_actions WHERE user_id=? AND action_key=?",
                              (user_id, delivery["action_key"])).fetchone()
        delivery["action_state"] = action[0] if action else None
    return True, delivery


def read_delivery(db_path, user_id, transport):
    """Read the configured file without creating a database or writer."""
    conn = sqlite3.connect(Path(db_path).absolute().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return inspect_delivery(conn, user_id, transport)
    finally:
        conn.close()


def _live_navigation(data, transport):
    if any(data.get(key) for key in ("learning_frame", "quiz_written_frame", "conjugation_frame",
                                    "oral_frame", "study_frame", "quiz_entry", "quiz_published_view")):
        return False
    return any((data.get(key) or {}).get("transport") == transport for key in (
        "quiz_start_pending", "quiz_notification_pending", "quiz_learning_pending_navigation"))


def _same_render(frame, transport, transport_key, revision_key, current_revision):
    return (frame.get("pending_render") and frame.get(transport_key) == transport
            and frame.get(revision_key) == frame.get(current_revision))


def _pending_view(data, transport, delivery):
    if _live_navigation(data, transport):
        return True
    frame = data.get("learning_frame") or {}
    if (frame.get("pending_render") and frame.get("accepted_delivery") == transport
            and frame.get("flow_id") == delivery["flow_id"]):
        return True
    frame = data.get("quiz_written_frame") or {}
    if _same_render(frame, transport, "render_transport", "render_revision", "revision"):
        return True
    pending = frame.get("next_view") or {}
    if pending.get("transport") == transport and pending.get("revision") == frame.get("revision"):
        delivered = (frame.get("render_transport") == transport
                     and frame.get("render_revision") == frame.get("revision")
                     and not frame.get("pending_render"))
        if not pending.get("adopted") or not delivered:
            return True
    frame = data.get("oral_frame") or {}
    published = data.get("quiz_published_view") or {}
    if (published.get("caller_kind") == "oral" and published.get("flow_id") == frame.get("flow_id")
            and _same_render(frame, transport, "render_transport", "render_revision", "revision")):
        return True
    frame = data.get("conjugation_frame") or {}
    if _same_render(frame, transport, "pending_render_transport", "pending_render_revision", "view_revision"):
        return True
    # Test renders have no transport cache; the dispatcher watermark and exact
    # persisted flow bind their pending view to the last accepted input.
    return bool(frame.get("kind") == "test" and frame.get("pending_render")
                and frame.get("flow_id") == delivery["flow_id"])


def allow_dispatch(data, transport, delivery):
    """Fresh input passes; an old receipt may finish only its own live view."""
    if delivery is None:
        data[LAST_TRANSPORT] = transport
        return True
    if delivery["action_kind"] == "undo" and delivery["state"] == "processing":
        return True
    if delivery.get("action_state") == "undone":
        return False
    if data.get(LAST_TRANSPORT, transport) != transport:
        return False
    if not _pending_view(data, transport, delivery):
        return False
    data[LAST_TRANSPORT] = transport
    return True
