"""P3.1 — guest access-approval flow (storage level, no telegram).

Covers registering a guest as 'pending', approved/rejected status transitions,
per-user isolation, and the pending-request list used for owner reminders.
"""

import os
import tempfile
import unittest

from bot.app_config import CEFR_LEVELS
from bot.storage import (
    get_user_levels,
    get_user_status,
    init_db,
    list_pending_users,
    register_or_update_user,
    set_user_status,
)

OWNER, U1, U2 = 999, 111, 222


def _mk():
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.close()
    return tmp.name


class AccessFlowTest(unittest.TestCase):
    def setUp(self):
        self.db = _mk()
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)

    def test_new_user_registers_pending(self):
        self.assertIsNone(get_user_status(self.db, U1))  # unknown user
        status = register_or_update_user(self.db, U1, "alice", "Alice")
        self.assertEqual(status, "pending")
        self.assertEqual(get_user_status(self.db, U1), "pending")

    def test_registration_creates_default_levels(self):
        # P3.3: a freshly registered guest gets all CEFR levels by default,
        # so quizzes/learning aren't empty for them out of the box.
        register_or_update_user(self.db, U1, "alice", "Alice")
        self.assertEqual(get_user_levels(self.db, U1), list(CEFR_LEVELS))

    def test_repeat_start_keeps_pending(self):
        register_or_update_user(self.db, U1, "alice", "Alice")
        # a repeat /start doesn't reset the status and returns the current one
        again = register_or_update_user(self.db, U1, "alice2", "Alice")
        self.assertEqual(again, "pending")

    def test_approve_and_reject_transitions(self):
        register_or_update_user(self.db, U1, "alice", "Alice")
        set_user_status(self.db, U1, "approved")
        self.assertEqual(get_user_status(self.db, U1), "approved")
        set_user_status(self.db, U1, "rejected")
        self.assertEqual(get_user_status(self.db, U1), "rejected")

    def test_invalid_status_rejected(self):
        register_or_update_user(self.db, U1, "alice", "Alice")
        with self.assertRaises(ValueError):
            set_user_status(self.db, U1, "banned")

    def test_list_pending_only_pending(self):
        register_or_update_user(self.db, U1, "alice", "Alice")
        register_or_update_user(self.db, U2, "bob", "Bob")
        set_user_status(self.db, U2, "approved")  # no longer pending
        pending = list_pending_users(self.db)
        ids = [u["user_id"] for u in pending]
        self.assertIn(U1, ids)
        self.assertNotIn(U2, ids)
        row = next(u for u in pending if u["user_id"] == U1)
        self.assertEqual(row["username"], "alice")

    def test_decision_isolated_per_user(self):
        register_or_update_user(self.db, U1, "alice", "Alice")
        register_or_update_user(self.db, U2, "bob", "Bob")
        set_user_status(self.db, U1, "approved")
        # a decision on U1 doesn't affect U2
        self.assertEqual(get_user_status(self.db, U2), "pending")


if __name__ == "__main__":
    unittest.main()
