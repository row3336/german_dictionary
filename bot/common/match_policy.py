"""Personal matching preference and immutable JSON session snapshots."""
from .hilfsverb import MatchRules, HilfsverbValidationError, value

KEY = "hilfsverb_match"
PR_KEY = "prateritum_match"
FRAME_KEY = "match_policy"


def enabled(db_path, user_id, key=KEY):
    from ..storage import get_user_setting
    return get_user_setting(db_path, user_id, key, "0") == "1"


def capture(db_path, user_id):
    return {"hilfsverb": bool(enabled(db_path, user_id)), "prateritum": bool(enabled(db_path, user_id, PR_KEY))}


def capture_conn(conn, user_id):
    values = dict(conn.execute("SELECT key,value FROM user_settings WHERE user_id=? AND key IN (?,?)",
                               (user_id, KEY, PR_KEY)).fetchall())
    return {"hilfsverb": values.get(KEY) == "1", "prateritum": values.get(PR_KEY) == "1"}


def snapshot(policy):
    """JSON values only; MatchRules objects never enter receipts or live frames."""
    return {"hilfsverb": bool(policy.hilfsverb), "prateritum": bool(policy.prateritum)}


def rules(frame):
    policy = (frame or {}).get(FRAME_KEY) or {}
    return MatchRules(hilfsverb=policy.get("hilfsverb") is True,
                      prateritum=policy.get("prateritum") is True)


def validate_required(entry, policy):
    """Unknown required forms are data errors, including an 'I don't know' answer."""
    if entry.get("type") == "verb" and policy.hilfsverb:
        if value(entry) is None or not entry.get("partizip2") or not entry.get("infinitive"):
            raise HilfsverbValidationError("required Perfekt data is unknown")
    if entry.get("type") == "verb" and policy.prateritum:
        from .hilfsverb import alternatives, normalize_text
        # PrON requires the complete Inf + Pr + P2 answer, even when the
        # auxiliary is optional/unknown. Check before matching or grading IDK.
        for field in ("infinitive", "prateritum", "partizip2"):
            if not any(normalize_text(form) for form in alternatives(entry.get(field))):
                raise HilfsverbValidationError("required verb form is unknown: " + field)
