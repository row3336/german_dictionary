"""P3.1 — поток одобрения доступа (storage-уровень, без telegram).

Проверяем регистрацию гостя как 'pending', переходы статуса approved/rejected,
изоляцию по юзерам и список pending-заявок для напоминаний владельцу.
"""

import os
import tempfile
import unittest

from bot.storage import (
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
        self.assertIsNone(get_user_status(self.db, U1))  # незнакомый юзер
        status = register_or_update_user(self.db, U1, "alice", "Alice")
        self.assertEqual(status, "pending")
        self.assertEqual(get_user_status(self.db, U1), "pending")

    def test_repeat_start_keeps_pending(self):
        register_or_update_user(self.db, U1, "alice", "Alice")
        # повторный /start не сбрасывает статус и возвращает текущий
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
        set_user_status(self.db, U2, "approved")  # больше не pending
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
        # решение по U1 не трогает U2
        self.assertEqual(get_user_status(self.db, U2), "pending")


if __name__ == "__main__":
    unittest.main()
