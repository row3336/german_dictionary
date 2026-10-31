"""Pure transitions for the Learning schedule."""

from datetime import datetime, timedelta, timezone


WARMUP_STEPS = 3


def clock(now):
    if isinstance(now, str):
        now = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def stamp(now):
    return clock(now).strftime("%Y-%m-%dT%H:%M:%SZ")


def reduce_answer(before, answer_kind, intervals, now):
    """Return a new state; the caller owns persistence and event recording."""
    if answer_kind not in {"correct", "wrong", "idk"}:
        raise ValueError("unsupported Learning answer")
    if not isinstance(before, dict) or before.get("status") != "active":
        raise ValueError("Learning item is not active")
    if not isinstance(intervals, (list, tuple)) or len(intervals) <= WARMUP_STEPS:
        raise ValueError("Learning schedule requires three warm-up and periodic steps")
    if tuple(intervals[:WARMUP_STEPS]) != (0, 0, 0):
        raise ValueError("Learning warm-up schedule must be 0,0,0")
    if any(not isinstance(value, int) or value <= 0 for value in intervals[WARMUP_STEPS:]):
        raise ValueError("Learning periodic intervals must be positive integers")
    step = before.get("step_index")
    remaining = before.get("recovery_remaining", 0)
    if not isinstance(step, int) or step < 0 or not isinstance(remaining, int) or not 0 <= remaining <= 3:
        raise ValueError("invalid Learning state")
    if step < WARMUP_STEPS and remaining:
        raise ValueError("warm-up item cannot have recovery")
    result = dict(before)
    timestamp = stamp(now)
    if answer_kind != "correct":
        result["step_index"] = 0 if step < WARMUP_STEPS else step
        result["recovery_remaining"] = 0 if step < WARMUP_STEPS else 3
        result["next_due"] = timestamp
    elif step >= WARMUP_STEPS and remaining > 1:
        result["recovery_remaining"] = remaining - 1
        result["next_due"] = timestamp
    else:
        next_step = step + 1
        result["step_index"] = next_step
        result["recovery_remaining"] = 0
        if next_step >= len(intervals):
            result["status"] = "completed"
        else:
            result["next_due"] = stamp(clock(now) + timedelta(seconds=intervals[next_step]))
    result["updated_at"] = timestamp
    return result
