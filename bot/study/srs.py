"""Pure, backwards-compatible scheduling calculations."""

from datetime import date, timedelta

DEFAULT_EASE = 2.5
MIN_EASE = 1.3
MAX_EASE = 3.5

def default_srs(today=None):
    """Handle default srs."""
    if today is None:
        today = date.today()
    return {
        "interval": 1,
        "repetitions": 0,
        "ease": DEFAULT_EASE,
        "due": today.isoformat(),
    }

def update_fsrs(srs, quality, today=None, interval_divisor=1.0):
    """Handle FSRS-like update in existing FSRS storage format."""
    if today is None:
        today = date.today()

    quality = max(0, min(5, int(quality)))
    try:
        interval_divisor = float(interval_divisor)
    except (TypeError, ValueError):
        interval_divisor = 1.0
    if interval_divisor <= 0:
        interval_divisor = 1.0
    interval = max(1, int(srs.get("interval", 1)))
    repetitions = max(0, int(srs.get("repetitions", 0)))
    ease = float(srs.get("ease", DEFAULT_EASE))
    ease = max(MIN_EASE, min(MAX_EASE, ease))

    if quality < 3:
        # Softer than classic FSRS reset: step back one stage and shorten interval.
        repetitions = max(0, repetitions - 1)
        ease = max(MIN_EASE, ease - 0.25)
        interval = max(1, int(round(interval * 0.4)))
    else:
        repetitions += 1
        ease = min(MAX_EASE, ease + 0.05 * (quality - 3))
        if repetitions == 1:
            interval = 1
        elif repetitions == 2:
            interval = 3
        else:
            growth = 1.7 + (ease - DEFAULT_EASE) * 0.35 + (quality - 3) * 0.08
            growth = max(1.3, min(2.3, growth))
            effective_growth = 1.0 + ((growth - 1.0) / interval_divisor)
            interval = max(1, int(round(interval * effective_growth)))
    due = today + timedelta(days=interval)
    return {
        "interval": interval,
        "repetitions": repetitions,
        "ease": round(ease, 2),
        "due": due.isoformat(),
    }

def apply_answer(srs, answer_kind, today=None, interval_divisor=1.0):
    """Calculate an original answer from one baseline and one calendar day."""
    if answer_kind not in {"correct", "wrong", "idk"}:
        raise ValueError("unsupported answer kind")
    today = today or date.today()
    result = dict(srs)
    for _ in range(3 if answer_kind == "idk" else 1):
        result = update_fsrs(result, 5 if answer_kind == "correct" else 2,
                             today=today, interval_divisor=interval_divisor)
    return result
