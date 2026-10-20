"""Pure Learning step/recovery transitions; settings and clocks are explicit."""

from datetime import datetime, timedelta, timezone


def clock(now=None):
    if isinstance(now, str):
        now = datetime.fromisoformat(now.replace("Z", "+00:00"))
    now = now or datetime.now(timezone.utc)
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)


def stamp(now=None):
    return clock(now).strftime("%Y-%m-%dT%H:%M:%SZ")


def reduce_answer(row, answer_kind, intervals, now):
    """The third recovery success also advances; wrong never loses a step."""
    if answer_kind not in {"correct", "wrong", "idk"}:
        raise ValueError("unsupported Learning answer")
    intervals = [int(value) for value in intervals if int(value) >= 0]
    if not intervals:
        raise ValueError("Learning requires a nonempty schedule")
    result = dict(row)
    step = max(0, int(row.get("step_index") or 0))
    recovery = max(0, min(3, int(row.get("recovery_remaining") or 0)))
    if answer_kind != "correct":
        result.update(recovery_remaining=3, next_due=stamp(now))
    elif recovery > 1:
        result.update(recovery_remaining=recovery - 1, next_due=stamp(now))
    else:
        step += 1
        result.update(step_index=step, recovery_remaining=0)
        if step >= len(intervals):
            result["status"] = "completed"
        else:
            result["next_due"] = stamp(clock(now) + timedelta(seconds=intervals[step]))
    result["updated_at"] = stamp(now)
    return result
