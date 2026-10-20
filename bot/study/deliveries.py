"""Connection-scoped transport receipts, independent of current frames."""

import json


def _decoded(row):
    if row is None:
        return None
    result = dict(row)
    result["result"] = json.loads(result.get("result_json") or "{}")
    return result


def lookup_delivery(conn, user_id, transport_update_id):
    """Look up BEFORE resolving the current question or last answer."""
    return _decoded(conn.execute(
        "SELECT * FROM study_deliveries WHERE user_id=? AND transport_update_id=?",
        (int(user_id), int(transport_update_id)),
    ).fetchone())


def begin_delivery(conn, user_id, transport_update_id, flow_id, action_kind,
                   action_key=None, result=None):
    conn.execute(
        """INSERT INTO study_deliveries
           (user_id,transport_update_id,flow_id,action_kind,action_key,state,result_json)
           VALUES (?,?,?,?,?,'processing',?)""",
        (int(user_id), int(transport_update_id), str(flow_id), str(action_kind),
         action_key, json.dumps(result or {}, ensure_ascii=False)),
    )
    return lookup_delivery(conn, user_id, transport_update_id)


def commit_delivery(conn, user_id, transport_update_id, result, action_key=None):
    cursor = conn.execute(
        """UPDATE study_deliveries SET state='committed',result_json=?,
           action_key=COALESCE(?,action_key),updated_at=CURRENT_TIMESTAMP
           WHERE user_id=? AND transport_update_id=?""",
        (json.dumps(result, ensure_ascii=False), action_key,
         int(user_id), int(transport_update_id)),
    )
    if cursor.rowcount != 1:
        raise ValueError("delivery does not belong to user")
    return lookup_delivery(conn, user_id, transport_update_id)


def save_processing_result(conn, user_id, transport_update_id, result):
    conn.execute(
        """UPDATE study_deliveries SET result_json=?,updated_at=CURRENT_TIMESTAMP
           WHERE user_id=? AND transport_update_id=? AND state='processing'""",
        (json.dumps(result, ensure_ascii=False), int(user_id), int(transport_update_id)),
    )
