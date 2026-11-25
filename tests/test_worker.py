"""The tailor worker's queue handling: claim stamps the start time, the heartbeat thread reports while a job runs."""
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

from tests.helpers import fresh_db, radar

import worker  # noqa: E402


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self._keep = fresh_db()
        self.db = radar.open_db()

    def tearDown(self):
        self.db.close()

    def test_claim_marks_the_request_running_and_stamps_when_it_started(self):
        self.db.execute("INSERT INTO tailor_jobs (job_id, state, requested) VALUES ('t:1', 'queued', '2026-11-16 08:00:00')")
        self.db.commit()
        before = radar.utc_now() - timedelta(seconds=2)
        self.assertEqual(worker.claim(self.db), "t:1")
        row = self.db.execute("SELECT state, started FROM tailor_jobs").fetchone()
        self.assertEqual(row["state"], "running")
        self.assertGreaterEqual(radar.ts_parse(row["started"]), before.replace(microsecond=0))
        self.assertIsNone(worker.claim(self.db))                      # nothing else queued; a running one is not claimed twice

    def test_the_heartbeat_thread_reports_while_the_main_loop_is_busy(self):
        stop = threading.Event()
        old = worker.HEARTBEAT_SECONDS
        worker.HEARTBEAT_SECONDS = 0.05
        t = threading.Thread(target=worker.heartbeat_loop, args=(stop,), daemon=True)
        try:
            self.assertIsNone(radar.worker_age(self.db))
            t.start()
            deadline = time.time() + 5
            while radar.worker_age(self.db) is None and time.time() < deadline:
                time.sleep(0.02)
            self.assertIsNotNone(radar.worker_age(self.db), "no heartbeat was written")
            self.assertTrue(radar.worker_online(radar.worker_age(self.db)))
        finally:
            stop.set()
            t.join(timeout=5)
            worker.HEARTBEAT_SECONDS = old
        self.assertFalse(t.is_alive())


if __name__ == "__main__":
    unittest.main()
