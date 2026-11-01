"""Connection-scoped operations; never open another connection during an action."""

import json
from contextlib import contextmanager

from ..storage import _connect


@contextmanager
def transaction(db_path):
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        yield conn


def row(conn, sql, args=()):
    found = conn.execute(sql, args).fetchone()
    return dict(found) if found else None


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def authorize(conn, user_id):
    from ..app_config import is_owner
    if is_owner(user_id):
        return
    user = row(conn, "SELECT status FROM users WHERE user_id=?", (int(user_id),))
    if not user or user["status"] != "approved":
        raise PermissionError("Written action requires an approved user")


def progress(conn, user_id, word_id, mode):
    return row(conn, "SELECT * FROM progress WHERE user_id=? AND word_id=? AND mode=?",
               (int(user_id), word_id, mode))


def write_progress(conn, user_id, word_id, mode, state):
    conn.execute("""INSERT INTO progress
        (user_id,word_id,mode,interval,repetitions,ease,due,updated_at)
        VALUES(?,?,?,?,?,?,?,COALESCE(?,CURRENT_TIMESTAMP))
        ON CONFLICT(user_id,word_id,mode) DO UPDATE SET interval=excluded.interval,
        repetitions=excluded.repetitions,ease=excluded.ease,due=excluded.due,
        updated_at=excluded.updated_at""",
        (int(user_id), word_id, mode, state["interval"], state["repetitions"],
         state["ease"], state["due"], state.get("updated_at")))


def action(conn, user_id, key):
    found = row(conn, "SELECT * FROM study_actions WHERE user_id=? AND action_key=?",
                (int(user_id), key))
    if found:
        found["receipt"] = json.loads(found["receipt"])
    return found


def delivery(conn, user_id, update_id):
    found = row(conn, "SELECT * FROM study_deliveries WHERE user_id=? AND transport_update_id=?",
                (int(user_id), int(update_id)))
    if found:
        found["result"] = json.loads(found["result"])
    return found


def save_delivery(conn, user_id, update_id, key, flow_id, kind, result, state="committed"):
    conn.execute("""INSERT INTO study_deliveries
        (user_id,transport_update_id,action_key,kind,flow_id,state,result)
        VALUES(?,?,?,?,?,?,?)""",
        (int(user_id), int(update_id), key, kind, flow_id, state, dumps(result)))


def save_action(conn, user_id, receipt):
    conn.execute("""INSERT INTO study_actions
        (user_id,action_key,kind,flow_id,revision,word_id,state,receipt)
        VALUES(?,?,?,?,?,?,'applied',?)""",
        (int(user_id), receipt["action_key"], "written:" + receipt["kind"],
         receipt["flow_id"], receipt["revision"], receipt["word_id"], dumps(receipt)))


def log(conn, user_id, key, kind):
    return conn.execute("INSERT INTO transaction_log(user_id,action,payload) VALUES(?,?,?)",
                        (int(user_id), "written:" + kind,
                         dumps({"written_action_key": key}))).lastrowid
