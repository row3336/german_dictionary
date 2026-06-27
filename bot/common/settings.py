"""Getters over app_settings (algorithm params) and topic-filter helpers."""

from ..app_config import (
    LEARNING_DAILY_QTY_DEFAULT,
    LEARNING_DAILY_QTY_KEY,
    LEARNING_INTERVALS_DEFAULT,
    LEARNING_INTERVALS_KEY,
    LEARNING_MIX_MODE_DEFAULT,
    LEARNING_READY_QUORUM_DEFAULT,
    LEARNING_READY_QUORUM_KEY,
    LEARNING_REMINDER_COOLDOWN_DEFAULT,
    LEARNING_REMINDER_COOLDOWN_KEY,
    LEARNING_REMINDER_INTERVAL_DEFAULT,
    LEARNING_REMINDER_INTERVAL_KEY,
    LEARNING_WARMUP_INTERVALS,
    RETRY_GAP_QUESTIONS,
    RETRY_MAX_REPEATS,
    SESSION_RETRY_GAP_KEY,
    SESSION_RETRY_IT_KEY,
    FSRS_REPORT_HOUR_KEY,
    FSRS_REPORT_UTC_HOUR_DEFAULT,
    TOPIC_FILTER_KEY,
    WEAK_DRILL_GAP_DEFAULT,
    WEAK_DRILL_GAP_KEY,
    WEAK_DRILL_REPEATS_DEFAULT,
    WEAK_DRILL_REPEATS_KEY,
    get_db_path,
)
from ..storage import _parse_learning_intervals, get_setting, get_user_setting, set_setting
from ..text_utils import md_escape as _md_escape


def _get_int_setting(db_path, key, default):
    """Handle get int setting."""
    raw = get_setting(db_path, key, None)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = int(default)
    return value


def _get_int_user_setting(db_path, user_id, key, default):
    """Per-user int setting: this user's value → global app_settings → `default`.

    Mirrors the per-user language fallback: a user's own override wins; otherwise
    the legacy global value (owner's setting) acts as the default; otherwise the
    constant default. Strictly user-scoped — never another user's value.
    """
    raw = get_user_setting(db_path, user_id, key, None)
    if raw is None:
        raw = get_setting(db_path, key, None)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


def _session_retry_gap(db_path, user_id):
    """Per-user retry gap (questions between repeats)."""
    return max(1, _get_int_user_setting(db_path, user_id, SESSION_RETRY_GAP_KEY, RETRY_GAP_QUESTIONS))


def _session_retry_iterations(db_path, user_id):
    """Per-user retry iterations (max repeats of a missed word)."""
    return max(1, _get_int_user_setting(db_path, user_id, SESSION_RETRY_IT_KEY, RETRY_MAX_REPEATS))


def _weak_drill_gap(db_path, user_id):
    """Per-user: интервал weak-дрилла (обычных weak-показов между повторами дрилла)."""
    value = _get_int_user_setting(db_path, user_id, WEAK_DRILL_GAP_KEY, WEAK_DRILL_GAP_DEFAULT)
    return max(1, value)


def _weak_drill_repeats(db_path, user_id):
    """Per-user: число повторов weak-дрилла (сколько раз слово показать за сессию)."""
    value = _get_int_user_setting(db_path, user_id, WEAK_DRILL_REPEATS_KEY, WEAK_DRILL_REPEATS_DEFAULT)
    return max(1, value)


def _learning_daily_qty(db_path, user_id):
    """Per-user: дневная норма learning."""
    value = _get_int_user_setting(db_path, user_id, LEARNING_DAILY_QTY_KEY, LEARNING_DAILY_QTY_DEFAULT)
    return max(1, value)


def _learning_intervals_seconds(db_path, user_id):
    """Per-user интервалы learning: значение юзера → глобал app_settings → дефолт."""
    raw = get_user_setting(db_path, user_id, LEARNING_INTERVALS_KEY, None)
    if raw is None:
        raw = get_setting(db_path, LEARNING_INTERVALS_KEY, LEARNING_INTERVALS_DEFAULT)
    values = _parse_learning_intervals(raw)
    if not values:
        values = _parse_learning_intervals(LEARNING_INTERVALS_DEFAULT)
    return values


def _learning_intervals_text(db_path, user_id):
    """Per-user интервалы learning текстом."""
    return ",".join(str(v) for v in _learning_intervals_seconds(db_path, user_id))


def _learning_schedule_seconds(db_path, user_id):
    """Per-user расписание learning с прогревочными повторами."""
    return list(LEARNING_WARMUP_INTERVALS) + _learning_intervals_seconds(db_path, user_id)


def _learning_reminder_interval(db_path, user_id):
    """Per-user интервал напоминаний learning."""
    value = _get_int_user_setting(db_path, user_id, LEARNING_REMINDER_INTERVAL_KEY, LEARNING_REMINDER_INTERVAL_DEFAULT)
    return max(10, value)


def _learning_reminder_cooldown(db_path, user_id):
    """Per-user кулдаун напоминаний learning."""
    value = _get_int_user_setting(db_path, user_id, LEARNING_REMINDER_COOLDOWN_KEY, LEARNING_REMINDER_COOLDOWN_DEFAULT)
    return max(60, value)


def _learning_ready_quorum(db_path, user_id):
    """Per-user кворум готовности learning."""
    value = _get_int_user_setting(db_path, user_id, LEARNING_READY_QUORUM_KEY, LEARNING_READY_QUORUM_DEFAULT)
    return min(100, max(1, value))


def _learning_mix_mode_key(user_id):
    """Handle learning mix mode key."""
    return f"learning_mix_mode:{int(user_id)}"


def _learning_mix_mode_enabled(user_id):
    """Handle learning mix mode enabled."""
    value = get_setting(get_db_path(), _learning_mix_mode_key(user_id), LEARNING_MIX_MODE_DEFAULT)
    return str(value).strip().lower() in {"1", "true", "on", "yes"}


def _set_learning_mix_mode(user_id, enabled):
    """Persist learning mix mode."""
    set_setting(get_db_path(), _learning_mix_mode_key(user_id), "1" if enabled else "0")


def _fsrs_report_hour_utc(db_path):
    """Handle fsrs report hour utc."""
    value = _get_int_setting(db_path, FSRS_REPORT_HOUR_KEY, FSRS_REPORT_UTC_HOUR_DEFAULT)
    return min(23, max(0, value))


def _topic_filter_value(user_id):
    """This user's topic filter: per-user override → global app_settings → '1'."""
    raw = get_user_setting(get_db_path(), user_id, TOPIC_FILTER_KEY, None)
    if raw is None:
        raw = get_setting(get_db_path(), TOPIC_FILTER_KEY, "1")
    return str(raw or "").strip() or "1"


def _parse_topic_filter(raw):
    """Handle parse topic filter."""
    if not raw:
        return []
    normalized = raw.strip().lower()
    if normalized in {"all", "*"}:
        return []
    tokens = []
    for chunk in raw.replace(",", "+").split("+"):
        token = chunk.strip()
        if token:
            tokens.append(token)
    return tokens


def _topic_filter_for_query(user_id):
    """Handle topic filter for query."""
    raw = _topic_filter_value(user_id)
    topics = _parse_topic_filter(raw)
    return topics or None


def _topic_filter_label(user_id):
    """Handle topic filter label."""
    raw = _topic_filter_value(user_id)
    topics = _parse_topic_filter(raw)
    if not topics:
        return "all"
    return "+".join(topics)


def _topic_filter_banner(user_id):
    """Handle topic filter banner."""
    label = _topic_filter_label(user_id)
    return f"Topic filter:\n\n🟥 *{_md_escape(label)}* 🟥"


def _current_topic_value(user_id):
    """Handle current topic value."""
    raw = _topic_filter_value(user_id)
    topics = _parse_topic_filter(raw)
    if not topics:
        return "1"
    return topics[0]
