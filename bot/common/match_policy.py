"""Personal matching preference and immutable JSON session snapshots."""
from .hilfsverb import MatchRules, HilfsverbValidationError, value

KEY = "hilfsverb_match"
FRAME_KEY = "match_policy"


def enabled(db_path, user_id):
    from ..storage import get_user_setting
    return get_user_setting(db_path, user_id, KEY, "0") == "1"


def capture(db_path, user_id):
    return {"hilfsverb": bool(enabled(db_path, user_id)), "prateritum": False}


def capture_conn(conn, user_id):
    row = conn.execute("SELECT value FROM user_settings WHERE user_id=? AND key=?",
                       (user_id, KEY)).fetchone()
    return {"hilfsverb": bool(row and row[0] == "1"), "prateritum": False}


def rules(frame):
    policy = (frame or {}).get(FRAME_KEY) or {}
    return MatchRules(hilfsverb=policy.get("hilfsverb") is True)


def validate_required(entry, policy):
    """Unknown required forms are data errors, including an 'I don't know' answer."""
    if entry.get("type") == "verb" and policy.hilfsverb:
        if value(entry) is None or not entry.get("partizip2") or not entry.get("infinitive"):
            raise HilfsverbValidationError("required Perfekt data is unknown")
