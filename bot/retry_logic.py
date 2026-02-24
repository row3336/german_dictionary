def maybe_take_retry_entry(queue, counts, since_last, gap, get_entry):
    """Select retry entry from queue if gap satisfied."""
    if not queue:
        return None, since_last, False
    if since_last < gap:
        return None, since_last, False
    word_id = queue.pop(0)
    entry = get_entry(word_id)
    if not entry:
        counts.pop(word_id, None)
        return None, since_last, False
    counts[word_id] = counts.get(word_id, 0) + 1
    return entry, 0, True


def register_retry_result(queue, counts, word_id, ok, was_retry, max_repeats):
    """Update retry queue after answer."""
    if was_retry:
        if counts.get(word_id, 0) >= max_repeats:
            counts.pop(word_id, None)
            return
        if word_id not in queue:
            queue.append(word_id)
        return
    if ok:
        return
    counts.setdefault(word_id, 0)
    if word_id not in queue:
        queue.append(word_id)
