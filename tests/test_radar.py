import os
import re
import unittest
from datetime import datetime, timedelta, timezone

from tests.helpers import Row, fresh_db, insert_job, radar


class UserAgentTests(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.pop("JOB_RADAR_CONTACT", None)

    def tearDown(self):
        os.environ.pop("JOB_RADAR_CONTACT", None)
        if self._old is not None:
            os.environ["JOB_RADAR_CONTACT"] = self._old

    def test_default_is_generic_and_carries_no_personal_data(self):
        ua = radar.user_agent()
        self.assertIn("job-radar", ua)
        self.assertNotIn("@", ua)
        self.assertNotIn("contact", ua)

    def test_contact_is_opt_in_through_the_environment(self):
        os.environ["JOB_RADAR_CONTACT"] = "https://example.org/contact"
        self.assertTrue(radar.user_agent().endswith("; contact: https://example.org/contact)"))


class FilterTests(unittest.TestCase):
    def test_keeps_student_cs_jobs(self):
        for title in ("Werkstudent Software Engineering (m/w/d)", "Working Student DevOps", "Werkstudent*in Data Science"):
            self.assertTrue(radar.is_student_cs(title), title)

    def test_drops_non_cs_and_non_student_jobs(self):
        for title in ("Werkstudent Marketing", "Werkstudent Personalentwicklung", "Senior Software Engineer",
                      "Praktikum Softwareentwicklung", "Werkstudent HR-Management"):
            self.assertFalse(radar.is_student_cs(title), title)

    def test_strong_tech_terms_in_the_description_can_rescue_a_generic_title(self):
        self.assertTrue(radar.is_student_cs("Werkstudent (m/w/d)", "", "Du arbeitest mit Python, Docker und Git."))
        self.assertFalse(radar.is_student_cs("Werkstudent (m/w/d)", "", "Du betreust unsere Filiale."))


class DistanceAndDedupTests(unittest.TestCase):
    def test_distance_from_darmstadt(self):
        self.assertEqual(radar.km_from_darmstadt(*radar.DARMSTADT), 0.0)
        frankfurt = radar.km_from_darmstadt(50.1109, 8.6821)
        self.assertTrue(24 < frankfurt < 32, frankfurt)
        self.assertIsNone(radar.km_from_darmstadt(None, "x"))

    def test_dedup_key_ignores_case_gender_suffix_and_punctuation(self):
        a = radar.dedup_key("Merck KGaA", "Werkstudent (m/w/d) IT-Support")
        b = radar.dedup_key("merck kgaa", "Werkstudent IT Support (w/m/d)")
        self.assertEqual(a, b)
        self.assertNotEqual(a, radar.dedup_key("Merck KGaA", "Werkstudent Controlling"))

    def test_store_new_drops_the_same_posting_from_another_source(self):
        keep = fresh_db()  # noqa: F841
        db = radar.open_db()
        job = {"source": "BA", "title": "Werkstudent Python", "company": "Beispiel GmbH", "location": "Darmstadt",
               "distance_km": 1.0, "url": "u", "description": "d"}
        radar.store_new(db, [{**job, "id": "ba:1"}])
        radar.store_new(db, [{**job, "id": "an:9", "source": "Arbeitnow"}, {**job, "id": "ba:1"}])
        self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
        db.close()


class WorkerQueueTests(unittest.TestCase):
    T0 = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)

    def setUp(self):
        self._keep = fresh_db()
        self.db = radar.open_db()

    def tearDown(self):
        self.db.close()

    def at(self, seconds):
        return self.T0 + timedelta(seconds=seconds)

    def test_heartbeat_age_with_a_fake_clock(self):
        self.assertIsNone(radar.worker_age(self.db, self.T0))               # never reported
        radar.beat(self.db, self.T0)
        self.assertEqual(radar.worker_age(self.db, self.at(30)), 30.0)
        radar.beat(self.db, self.at(40))                                    # a new beat replaces the old one
        self.assertEqual(radar.worker_age(self.db, self.at(50)), 10.0)
        self.assertEqual(radar.worker_age(self.db, self.at(-5)), 0.0)       # clock skew never gives a negative age

    def test_online_boundary(self):
        self.assertFalse(radar.worker_online(None))
        self.assertTrue(radar.worker_online(radar.WORKER_STALE_SECONDS))
        self.assertFalse(radar.worker_online(radar.WORKER_STALE_SECONDS + 0.1))

    def add(self, job_id, state, started=None):
        self.db.execute("INSERT INTO tailor_jobs (job_id, state, started) VALUES (?, ?, ?)",
                        (job_id, state, None if started is None else radar.ts_str(started)))
        self.db.commit()

    def state(self, job_id):
        return self.db.execute("SELECT state, started FROM tailor_jobs WHERE job_id = ?", (job_id,)).fetchone()[:]

    def test_only_overdue_running_requests_are_requeued(self):
        self.add("a", "running", self.at(-radar.RUNNING_STALE_SECONDS - 1))   # overdue
        self.add("b", "running", self.at(-radar.RUNNING_STALE_SECONDS + 5))   # still within its time
        self.add("c", "running", None)                                         # no start time: left alone
        self.add("d", "queued")
        self.add("e", "done", self.at(-5000))
        self.assertEqual(radar.requeue_stale(self.db, self.T0), ["a"])
        self.assertEqual(self.state("a"), ("queued", None))
        self.assertEqual(self.state("b")[0], "running")
        self.assertEqual(self.state("c")[0], "running")
        self.assertEqual(self.state("e")[0], "done")
        self.assertEqual(radar.requeue_stale(self.db, self.T0), [])           # idempotent

    def test_an_old_database_gains_meta_and_started(self):
        import tempfile
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old = radar.DB_PATH
        self.addCleanup(setattr, radar, "DB_PATH", old)
        radar.DB_PATH = Path(tmp.name) / "old.db"
        import sqlite3
        con = sqlite3.connect(radar.DB_PATH)
        con.execute("CREATE TABLE tailor_jobs (job_id TEXT PRIMARY KEY, state TEXT NOT NULL, requested TEXT, finished TEXT, error TEXT, pdf INTEGER NOT NULL DEFAULT 0, warnings TEXT)")
        con.execute("INSERT INTO tailor_jobs (job_id, state) VALUES ('t:1', 'queued')")
        con.commit()
        con.close()
        db = radar.open_db()
        try:
            self.assertIn("started", {r["name"] for r in db.execute("PRAGMA table_info(tailor_jobs)")})
            radar.beat(db, self.T0)
            self.assertEqual(db.execute("SELECT state FROM tailor_jobs").fetchone()[0], "queued")
            radar.open_db().close()
        finally:
            db.close()


class EmailTests(unittest.TestCase):
    def setUp(self):
        self._keep = fresh_db()
        self.db = radar.open_db()
        os.environ["DASHBOARD_URL"] = "http://dash.example:8090/"
        self.stats = {"tracked": 30, "interested": 3, "applied": 1, "sources": 5, "n_low": 3, "untailored": 2}

    def tearDown(self):
        os.environ.pop("DASHBOARD_URL", None)
        self.db.close()

    def test_normal_digest(self):
        rows = [insert_job(self.db, id=f"t:{i}", score=s) for i, s in enumerate((88, 71, 55))]
        subject, text, html = radar.build_email(rows, 6, [], {}, self.stats)
        self.assertEqual(subject, "Job Radar: 6 new, top 88")
        self.assertIn("+ 3 more below 50 on the dashboard.", text)
        self.assertIn("Open dashboard to triage", html)

    def test_nothing_new_is_a_heartbeat_with_a_nudge(self):
        subject, text, html = radar.build_email([], 0, [], {}, self.stats)
        self.assertEqual(subject, "Job Radar: nothing new today")
        self.assertIn("The radar is running fine", text)
        self.assertIn("2 jobs marked interested have no tailored resume yet", text)

    def test_failed_source_is_named_in_subject_banner_and_footer(self):
        rows = [insert_job(self.db, score=71)]
        subject, text, html = radar.build_email(rows, 2, ["Merck"], {"Merck": "HTTP 503"}, self.stats)
        self.assertEqual(subject, "Job Radar: 2 new, top 71 (1 source failed)")
        self.assertIn("Merck Careers couldn't be fetched</strong> (HTTP 503)", html)
        self.assertLess(html.index("couldn't be fetched"), html.index("2 new jobs"))  # banner is the first row
        self.assertIn("Merck Careers (failed today)", html)

    def test_html_is_escaped_and_mail_client_safe(self):
        row = Row(insert_job(self.db, score=80))
        row["title"], row["company"] = 'DevOps & <b>Cloud</b> "x"', "Muster & Söhne <GmbH>"
        _, _, html = radar.build_email([row], 1, [], {}, self.stats)
        self.assertIn("DevOps &amp; &lt;b&gt;Cloud&lt;/b&gt;", html)
        self.assertNotIn("<b>Cloud", html)
        self.assertIsNone(re.search(r"<style|<img|<link|<script|@import|url\(", html, re.I))
        self.assertIn('role="presentation"', html)


if __name__ == "__main__":
    unittest.main()
