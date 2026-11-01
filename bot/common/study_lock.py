"""Serialize all study transitions for one user, including mixed-mode undo."""

import asyncio

_locks = {}


def study_lock(user_id):
    return _locks.setdefault(int(user_id), asyncio.Lock())
