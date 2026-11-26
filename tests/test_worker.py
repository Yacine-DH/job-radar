import unittest
from datetime import datetime, timedelta, timezone

from tests.helpers import fresh_db, radar

import worker


class WorkerQueueTests(unittest.TestCase):
    def setUp(self):
        self._keep = fresh_db()
        self.db = radar.open_db()

    def tearDown(self):
        self.db.close()

    def add_request(self, job_id, state, started=None, attempt=0):
        self.db.execute(
            "INSERT INTO tailor_jobs (job_id, state, requested, started, attempt) VALUES (?, ?, ?, ?, ?)",
            (job_id, state, "2026-11-26 10:00:00", started, attempt))
        self.db.commit()

    def test_heartbeat_uses_the_injected_clock(self):
        now = datetime(2026, 11, 26, 10, 0, tzinfo=timezone.utc)

        worker.heartbeat(self.db, now)

        self.assertEqual(self.db.execute("SELECT updated FROM worker_heartbeat WHERE id = 1").fetchone()[0],
                         "2026-11-26 10:00:00")

    def test_stale_recovery_requeues_only_running_jobs_past_the_timeout_and_margin(self):
        now = datetime(2026, 11, 26, 10, 20, tzinfo=timezone.utc)
        old = now - timedelta(seconds=worker.STALE_AFTER_SECONDS + 1)
        boundary = now - timedelta(seconds=worker.STALE_AFTER_SECONDS)
        recent = now - timedelta(seconds=worker.STALE_AFTER_SECONDS - 1)
        self.add_request("stale", "running", old.strftime("%Y-%m-%d %H:%M:%S"))
        self.add_request("boundary", "running", boundary.strftime("%Y-%m-%d %H:%M:%S"))
        self.add_request("recent", "running", recent.strftime("%Y-%m-%d %H:%M:%S"))
        self.add_request("legacy", "running")
        self.add_request("queued", "queued")
        self.add_request("cancelled", "cancelled", old.strftime("%Y-%m-%d %H:%M:%S"))

        count = worker.requeue_stale(self.db, now)

        states = dict(self.db.execute("SELECT job_id, state FROM tailor_jobs"))
        self.assertEqual(count, 2)
        self.assertEqual(states, {"stale": "queued", "boundary": "running", "recent": "running",
                                  "legacy": "queued", "queued": "queued", "cancelled": "cancelled"})

    def test_claim_skips_cancelled_jobs_and_increments_attempt(self):
        self.add_request("cancelled", "cancelled")
        self.add_request("queued", "queued")

        claimed = worker.claim(self.db, datetime(2026, 11, 26, 10, 1, tzinfo=timezone.utc))

        self.assertEqual(claimed, ("queued", 1))
        self.assertEqual(self.db.execute("SELECT state, attempt, started FROM tailor_jobs WHERE job_id = 'queued'").fetchone()[:],
                         ("running", 1, "2026-11-26 10:01:00"))
        self.assertIsNone(worker.claim(self.db))

    def test_finish_does_not_overwrite_cancelled_or_reclaimed_attempts(self):
        self.add_request("cancelled", "cancelled", attempt=1)
        self.add_request("reclaimed", "running", attempt=2)

        worker.finish(self.db, "cancelled", 1, "done")
        worker.finish(self.db, "reclaimed", 1, "failed", error="old attempt")

        states = dict(self.db.execute("SELECT job_id, state FROM tailor_jobs"))
        self.assertEqual(states, {"cancelled": "cancelled", "reclaimed": "running"})


if __name__ == "__main__":
    unittest.main()
