"""P7 — DB under concurrency: WAL + busy_timeout survive parallel writers/readers.

The storage layer opens a fresh sqlite connection per call (`_connect` sets
journal_mode=WAL + busy_timeout=5000), so many threads writing at once genuinely
contend on the single DB file. busy_timeout makes a blocked writer WAIT instead
of raising "database is locked"; this test pins that — without the PRAGMAs a burst
of concurrent writers would surface lock errors. (No telegram needed — pure DB.)
"""

import os
import tempfile
import threading
import unittest

from bot.storage import get_user_setting, init_db, register_or_update_user, set_user_setting

N_THREADS = 16
WRITES_PER_THREAD = 25


class DbConcurrencyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        self.db = tmp.name
        self.addCleanup(lambda: os.unlink(self.db))
        init_db(self.db)

    def test_concurrent_writers_no_database_is_locked(self):
        errors = []

        def worker(uid):
            try:
                register_or_update_user(self.db, uid, f"u{uid}", "")
                for i in range(WRITES_PER_THREAD):
                    set_user_setting(self.db, uid, "ctr", str(i))
                    get_user_setting(self.db, uid, "ctr", "")  # interleave reads
            except Exception as exc:  # noqa: BLE001 — capture e.g. "database is locked"
                errors.append(f"{uid}: {exc!r}")

        threads = [threading.Thread(target=worker, args=(2000 + i,)) for i in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # No lock/timeout errors raised under contention.
        self.assertEqual(errors, [])
        # Every thread's last write is the one that persisted (per-user isolation holds).
        for i in range(N_THREADS):
            self.assertEqual(
                get_user_setting(self.db, 2000 + i, "ctr", ""), str(WRITES_PER_THREAD - 1)
            )


if __name__ == "__main__":
    unittest.main()
