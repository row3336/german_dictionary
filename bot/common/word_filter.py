"""Versioned book-topic filters and atomic, revision-checked filter commands."""

from collections import namedtuple
from copy import deepcopy
import hashlib
import json

from ..app_config import CEFR_LEVELS, TOPIC_FILTER_KEY, get_db_path
from ..storage import _connect, set_user_setting
from .settings import _parse_topic_filter
from .topics import sorted_topics

FILTER_MODE_KEY = "filter_mode"
ACTIVE_BOOK_LEVEL_KEY = "active_book_level"
BOOK_FILTER_KEY = "book_topic_filter_v2"
FILTER_REVISION_KEY = "filter_revision"
MODE_TOPICS = "topics"
MODE_CEFR = "cefr"
DEFAULT_BOOK_LEVEL = "A1"
QuizFilter = namedtuple("QuizFilter", ["mode", "topics", "cefr_levels"])


class FilterCommandError(ValueError):
    """A rejected command never changes mode or either stored branch."""


def _empty_config():
    return {"version": 2, "selected_levels": [], "topics_by_level": {},
            "browsing_level": DEFAULT_BOOK_LEVEL, "revision": 0}


def validate_book_config(value):
    """Validate the whole document before canonicalizing; never infer 'all'."""
    keys = {"version", "selected_levels", "topics_by_level", "browsing_level", "revision"}
    if not isinstance(value, dict) or set(value) != keys or type(value["version"]) is not int or value["version"] != 2:
        raise FilterCommandError("invalid")
    selected, choices = value["selected_levels"], value["topics_by_level"]
    if (not isinstance(selected, list) or any(lv not in CEFR_LEVELS for lv in selected)
            or not isinstance(choices, dict) or any(lv not in CEFR_LEVELS for lv in choices)
            or value["browsing_level"] not in CEFR_LEVELS
            or type(value["revision"]) is not int or value["revision"] < 0):
        raise FilterCommandError("invalid")
    result = deepcopy(value)
    result["selected_levels"] = [lv for lv in CEFR_LEVELS if lv in selected]
    normalized = {}
    for lv, topics in choices.items():
        if topics == "all":
            normalized[lv] = "all"
        elif (isinstance(topics, list) and topics
              and all(isinstance(n, str) and n.strip() == n and n for n in topics)):
            normalized[lv] = sorted_topics(set(topics))
        else:
            raise FilterCommandError("invalid")
    if any(lv not in normalized for lv in selected):
        raise FilterCommandError("invalid")
    result["topics_by_level"] = normalized
    return result


def _read_state(conn, user_id):
    settings = dict(conn.execute("SELECT key,value FROM user_settings WHERE user_id=?", (int(user_id),)).fetchall())
    raw_mode = str(settings.get(FILTER_MODE_KEY) or "").strip().lower()
    mode = MODE_CEFR if raw_mode == MODE_CEFR else MODE_TOPICS
    try:
        revision = int(settings.get(FILTER_REVISION_KEY, 0))
        if revision < 0:
            raise ValueError()
    except (TypeError, ValueError):
        revision = -1
    registry = [dict(row) for row in conn.execute(
        """SELECT t.name,t.description,t.level,COUNT(w.id) AS word_count
           FROM topics t LEFT JOIN words w ON w.topic=t.name GROUP BY t.name""").fetchall()]
    by_level = {lv: sorted_topics(r["name"] for r in registry if r["level"] == lv) for lv in CEFR_LEVELS}
    available = [lv for lv in CEFR_LEVELS if any(r["level"] == lv and r["word_count"] for r in registry)]
    raw = settings.get(BOOK_FILTER_KEY)
    valid, legacy = True, raw is None
    if legacy:
        # Legacy scalar readers historically accepted whitespace/case variants.
        # Keep that compatibility here; the versioned JSON validator stays strict.
        level = str(settings.get(ACTIVE_BOOK_LEVEL_KEY, DEFAULT_BOOK_LEVEL) or "").strip().upper()
        level = level if level in CEFR_LEVELS else DEFAULT_BOOK_LEVEL
        raw_topics = settings.get(TOPIC_FILTER_KEY, "")
        # Only an absent/legacy all value means all. Stale explicit names stay empty.
        selected_topics = _parse_topic_filter(raw_topics)
        config = {"version": 2, "selected_levels": [level],
                  "topics_by_level": {level: selected_topics or "all"},
                  "browsing_level": level, "revision": 0}
    else:
        try:
            config = validate_book_config(json.loads(raw))
        except (ValueError, TypeError, KeyError):
            config, valid = _empty_config(), False
    rows = conn.execute("SELECT level FROM user_levels WHERE user_id=?", (int(user_id),)).fetchall()
    levels = [lv for lv in CEFR_LEVELS if lv in {r[0] for r in rows}] if rows else list(CEFR_LEVELS)
    cefr_counts = dict(conn.execute("SELECT cefr_level,COUNT(*) FROM words GROUP BY cefr_level").fetchall())
    total = sum(cefr_counts.values())
    return {"mode": mode, "revision": revision, "config": config, "valid": valid,
            "legacy": legacy, "registry": registry, "by_level": by_level,
            "available_levels": available, "cefr_levels": levels,
            "cefr_counts": cefr_counts, "total": total}


def read_filter_state(user_id, conn=None):
    """Read mode, configuration and registry from one SQLite read snapshot.

    A savepoint also gives a supplied autocommit connection a consistent snapshot;
    releasing it leaves an existing caller transaction untouched.
    """
    if conn is None:
        with _connect(get_db_path()) as owned:
            return read_filter_state(user_id, owned)
    conn.execute("SAVEPOINT word_filter_snapshot")
    try:
        return _read_state(conn, user_id)
    finally:
        conn.execute("RELEASE word_filter_snapshot")


def selected_topics(state, level):
    config = state["config"]
    if not state["valid"] or level not in config["selected_levels"]:
        return []
    names = state["by_level"].get(level, [])
    choice = config["topics_by_level"].get(level, [])
    return names if choice == "all" else [n for n in names if n in choice]


def filter_from_state(state):
    if state["mode"] == MODE_CEFR:
        levels = state["cefr_levels"]
        return QuizFilter(MODE_CEFR, None, None if len(levels) == len(CEFR_LEVELS) else levels)
    topics = {n for lv in state["config"]["selected_levels"] for n in selected_topics(state, lv)}
    return QuizFilter(MODE_TOPICS, sorted_topics(topics), None)


def resolve_quiz_filter(user_id, conn=None):
    """None is unrestricted; [] is explicitly empty on the active axis."""
    return filter_from_state(read_filter_state(user_id, conn))


def effective_filter_fingerprint(qf):
    def axis(values):
        return None if values is None else tuple(sorted(set(values)))
    return json.dumps([qf.mode, axis(qf.topics), axis(qf.cefr_levels)], ensure_ascii=False, separators=(",", ":"))


def filter_label_from_state(state):
    """Effective selection for banners; deleted topics never render as 'all'."""
    qf = filter_from_state(state)
    if qf.mode == MODE_CEFR:
        return "CEFR: " + ("all" if qf.cefr_levels is None else "+".join(qf.cefr_levels) or "∅")
    return "; ".join(lv + ": " + ("+".join(selected_topics(state, lv)) or "∅")
                     for lv in state["config"]["selected_levels"]) or "∅"


def count_words_for_filter(user_id, conn=None):
    state = read_filter_state(user_id, conn)
    qf = filter_from_state(state)
    if qf.mode == MODE_CEFR:
        return state["total"] if qf.cefr_levels is None else sum(state["cefr_counts"].get(lv, 0) for lv in qf.cefr_levels)
    names = set(qf.topics)
    return sum(r["word_count"] for r in state["registry"] if r["name"] in names)


def get_filter_mode(user_id):
    return read_filter_state(user_id)["mode"]


def get_active_book_level(user_id):
    """Compatibility cursor only; it never defines the v2 selected set."""
    return read_filter_state(user_id)["config"]["browsing_level"]


def set_active_book_level(user_id, level):
    if level not in CEFR_LEVELS:
        raise ValueError("Invalid CEFR level.")
    set_user_setting(get_db_path(), user_id, ACTIVE_BOOK_LEVEL_KEY, level)


def _put(conn, user_id, key, value):
    conn.execute("""INSERT INTO user_settings(user_id,key,value) VALUES(?,?,?)
        ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value""", (int(user_id), key, str(value)))


def set_filter_mode(user_id, mode):
    """Compatibility API; new UI uses the complete atomic branch command."""
    if mode not in (MODE_TOPICS, MODE_CEFR):
        raise ValueError("Invalid filter mode.")
    with _connect(get_db_path()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        state = read_filter_state(user_id, conn)
        _put(conn, user_id, FILTER_MODE_KEY, mode)
        _put(conn, user_id, FILTER_REVISION_KEY, state["revision"] + 1)


def topic_token(name):
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:20]


def topic_tokens(names):
    """Collision checking is mandatory even though the digest space is large."""
    result = {}
    for name in names:
        token = topic_token(name)
        if token in result and result[token] != name:
            raise FilterCommandError("collision")
        result[token] = name
    return result


def reduce_filter(state, operation, level=None, token=None):
    """Pure command validation/reduction against the current registry."""
    config = deepcopy(state["config"])
    fresh = state["mode"] != MODE_TOPICS
    available = state["available_levels"]
    if operation == "cefr":
        if level != "all" and level not in state["cefr_counts"]:
            raise FilterCommandError("invalid")
        if level != "all" and level not in CEFR_LEVELS:
            raise FilterCommandError("invalid")
        levels = list(CEFR_LEVELS) if level == "all" else (
            [level] if state["mode"] != MODE_CEFR else
            [lv for lv in CEFR_LEVELS if (lv in state["cefr_levels"]) != (lv == level)])
        if not levels:
            raise FilterCommandError("need_one")
        return MODE_CEFR, levels
    if operation not in ("level", "topic", "all"):
        raise FilterCommandError("invalid")
    if operation in ("level", "topic") and level not in available:
        # A disappeared selected level may only be explicitly switched OFF.
        # This recovery command can never activate an unavailable level.
        removable = (operation == "level" and not fresh and state["valid"]
                     and level in config["selected_levels"]
                     and any(lv in available for lv in config["selected_levels"] if lv != level))
        if not removable:
            raise FilterCommandError("invalid")
    if operation == "level":
        selected = config["selected_levels"]
        if fresh or not state["valid"] or not any(lv in available for lv in selected):
            selected = [level]
        elif level in selected:
            selected = [lv for lv in selected if lv != level]
            if not any(lv in available for lv in selected):
                raise FilterCommandError("need_one")
        else:
            selected = [lv for lv in CEFR_LEVELS if lv in selected or lv == level]
        config["selected_levels"] = selected
        config["topics_by_level"].setdefault(level, "all")
        config["browsing_level"] = level if level in selected else next((lv for lv in selected if lv in available), DEFAULT_BOOK_LEVEL)
    elif operation == "topic":
        name = topic_tokens(state["by_level"][level]).get(token)
        if name is None or (not fresh and level not in config["selected_levels"]):
            raise FilterCommandError("invalid")
        if fresh or not state["valid"]:
            config["selected_levels"] = [level]
            config["topics_by_level"][level] = [name]
        else:
            active = set(selected_topics(state, level))
            if name in active:
                if len(active) == 1:
                    raise FilterCommandError("need_one")
                active.remove(name)
            else:
                active.add(name)
            config["topics_by_level"][level] = sorted_topics(active)
        config["browsing_level"] = level
    else:
        if fresh or not state["valid"] or not any(lv in available for lv in config["selected_levels"]):
            raise FilterCommandError("invalid")
        for lv in config["selected_levels"]:
            config["topics_by_level"][lv] = "all"
    config["revision"] = state["revision"] + 1
    return MODE_TOPICS, validate_book_config(config)


def apply_filter_command(user_id, expected_revision, operation, level=None, token=None, conn=None):
    """Authorize actor, compare revision, then write mode/config/revision atomically."""
    if conn is None:
        with _connect(get_db_path()) as owned:
            owned.execute("BEGIN IMMEDIATE")
            return apply_filter_command(user_id, expected_revision, operation, level, token, owned)
    # Supplied connections belong to a command transaction, never nested commits.
    if not conn.in_transaction:
        raise ValueError("filter command requires a transaction")
    from ..study.answers import authorize_user
    authorize_user(conn, int(user_id))
    state = read_filter_state(user_id, conn)
    if type(expected_revision) is not int or expected_revision != state["revision"] or expected_revision < 0:
        raise FilterCommandError("stale")
    mode, branch = reduce_filter(state, operation, level, token)
    conn.execute("SAVEPOINT word_filter_command")
    try:
        _put(conn, user_id, FILTER_MODE_KEY, mode)
        if mode == MODE_TOPICS:
            _put(conn, user_id, BOOK_FILTER_KEY, json.dumps(branch, ensure_ascii=False, separators=(",", ":")))
        else:
            conn.execute("DELETE FROM user_levels WHERE user_id=?", (int(user_id),))
            conn.executemany("INSERT INTO user_levels(user_id,level) VALUES(?,?)",
                             [(int(user_id), lv) for lv in branch])
        _put(conn, user_id, FILTER_REVISION_KEY, expected_revision + 1)
    except Exception:
        conn.execute("ROLLBACK TO word_filter_command")
        raise
    finally:
        conn.execute("RELEASE word_filter_command")
    return read_filter_state(user_id, conn)
