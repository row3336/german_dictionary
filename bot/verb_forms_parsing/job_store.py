"""Persistence for the VF-add batch job (spec #2, Step 3).

A job is the owner's word list; each item is fetched/parsed one at a time and its
result stored, so a bot restart resumes from the first pending item instead of
re-hitting the site. Thin DB layer over storage._connect (schema lives in
storage.init_db: vf_add_jobs / vf_add_items).
"""

import json

from ..storage import _connect, init_db

# job.status: running -> (paused) -> done | stopped | error
ACTIVE_STATUSES = ("running", "paused")


def _row_to_dict(row):
    return dict(row) if row is not None else None


def _split_word_hint(item):
    """Normalize a job word into (word, type_hint). Accepts a bare string or a
    (word, type_hint) pair, so old callers passing plain strings still work."""
    if isinstance(item, (tuple, list)):
        word = str(item[0]).strip()
        hint = str(item[1]).strip().lower() if len(item) > 1 else ""
        return word, hint
    return str(item).strip(), ""


def create_job(db_path, user_id, chat_id, topic, words):
    """Create a job and return its id. `words` is a list of query strings or of
    (word, type_hint) pairs — the hint routes function words to their type page."""
    init_db(db_path)
    pairs = [wh for wh in (_split_word_hint(w) for w in words) if wh[0]]
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO vf_add_jobs (user_id, chat_id, status, topic, total) VALUES (?, ?, 'running', ?, ?)",
            (int(user_id), int(chat_id), str(topic or ""), len(pairs)),
        )
        job_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO vf_add_items (job_id, position, word, type_hint) VALUES (?, ?, ?, ?)",
            [(job_id, i, w, h) for i, (w, h) in enumerate(pairs)],
        )
    return job_id


def create_recheck_job(db_path, user_id, chat_id, words_with_ids):
    """Create a recheck job over existing words. `words_with_ids` = list of
    (lookup_word, word_id) or (lookup_word, word_id, type_hint); word_id links the
    diff apply back to the word, type_hint routes the lookup by the word's type.
    Returns the job id."""
    init_db(db_path)
    rows = []
    for rec in words_with_ids:
        word = str(rec[0]).strip()
        if not word:
            continue
        wid = str(rec[1])
        hint = str(rec[2]).strip().lower() if len(rec) > 2 else ""
        rows.append((word, wid, hint))
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO vf_add_jobs (user_id, chat_id, status, topic, total) VALUES (?, ?, 'running', '', ?)",
            (int(user_id), int(chat_id), len(rows)),
        )
        job_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO vf_add_items (job_id, position, word, word_id, type_hint) VALUES (?, ?, ?, ?, ?)",
            [(job_id, i, w, wid, h) for i, (w, wid, h) in enumerate(rows)],
        )
    return job_id


def get_job(db_path, job_id):
    with _connect(db_path) as conn:
        return _row_to_dict(conn.execute("SELECT * FROM vf_add_jobs WHERE id = ?", (job_id,)).fetchone())


def set_job_status(db_path, job_id, status):
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE vf_add_jobs SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, job_id),
        )


def active_jobs(db_path):
    """Jobs still running/paused — used to resume after a restart."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM vf_add_jobs WHERE status IN ('running', 'paused') ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def next_pending_item(db_path, job_id):
    """Lowest-position pending item of a job, or None when the queue is drained."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM vf_add_items WHERE job_id = ? AND status = 'pending' ORDER BY position LIMIT 1",
            (job_id,),
        ).fetchone()
    return _row_to_dict(row)


def save_item_result(db_path, item_id, status, result=None, flags="", error=""):
    """Record one item's outcome (status: done | error | skipped)."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE vf_add_items SET status = ?, result_json = ?, flags = ?, error = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, json.dumps(result, ensure_ascii=False) if result is not None else "", str(flags or ""), str(error or ""), item_id),
        )


def job_counts(db_path, job_id):
    """{total, pending, done, error, skipped} for progress reporting."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM vf_add_items WHERE job_id = ? GROUP BY status",
            (job_id,),
        ).fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    total = sum(counts.values())
    return {
        "total": total,
        "pending": counts.get("pending", 0),
        "done": counts.get("done", 0),
        "error": counts.get("error", 0),
        "skipped": counts.get("skipped", 0),
    }


def list_items(db_path, job_id, status=None):
    """All items of a job (optionally filtered by status), with result parsed."""
    query = "SELECT * FROM vf_add_items WHERE job_id = ?"
    params = [job_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY position"
    with _connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    items = []
    for r in rows:
        item = dict(r)
        item["result"] = json.loads(item["result_json"]) if item["result_json"] else None
        items.append(item)
    return items
