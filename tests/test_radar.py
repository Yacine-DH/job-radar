import os
import re
import unittest
from unittest import mock

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


class DedupeByCityTests(unittest.TestCase):
    JOB = {"source": "BA", "title": "Werkstudent (m/w/d)", "company": "Beispiel GmbH", "location": "Darmstadt",
           "distance_km": 1.0, "url": "u", "description": "d"}

    def setUp(self):
        self._keep = fresh_db()
        self.db = radar.open_db()

    def tearDown(self):
        self.db.close()

    def ids(self):
        return [r[0] for r in self.db.execute("SELECT id FROM jobs ORDER BY id")]

    def test_the_same_generic_title_in_two_cities_is_kept_twice(self):
        radar.store_new(self.db, [{**self.JOB, "id": "a:1"}, {**self.JOB, "id": "a:2", "location": "Frankfurt am Main"}])
        self.assertEqual(self.ids(), ["a:1", "a:2"])

    def test_the_same_posting_on_two_sources_is_collapsed_and_remembers_the_other_source(self):
        radar.store_new(self.db, [{**self.JOB, "id": "ba:1"}])
        radar.store_new(self.db, [{**self.JOB, "id": "sw:7", "source": "Stellenwerk", "location": "Darmstadt, Hessen"}])
        self.assertEqual(self.ids(), ["ba:1"])
        self.assertEqual(self.db.execute("SELECT also_on FROM jobs").fetchone()[0], '["Stellenwerk"]')

    def test_a_repost_from_the_same_source_is_dropped_without_an_also_on_entry(self):
        radar.store_new(self.db, [{**self.JOB, "id": "ba:1"}, {**self.JOB, "id": "ba:2"}])
        self.assertEqual(self.ids(), ["ba:1"])
        self.assertIsNone(self.db.execute("SELECT also_on FROM jobs").fetchone()[0])

    def test_a_missing_city_falls_back_to_company_and_title(self):
        radar.store_new(self.db, [{**self.JOB, "id": "ba:1"}, {**self.JOB, "id": "an:1", "source": "Arbeitnow", "location": ""}])
        self.assertEqual(self.ids(), ["ba:1"])

    def test_two_different_cities_in_one_batch_and_a_later_duplicate_of_the_second(self):
        radar.store_new(self.db, [{**self.JOB, "id": "a:1"}, {**self.JOB, "id": "a:2", "location": "Mainz"}])
        radar.store_new(self.db, [{**self.JOB, "id": "b:3", "source": "Stellenwerk", "location": "Mainz (Rheinland-Pfalz)"}])
        self.assertEqual(self.ids(), ["a:1", "a:2"])
        self.assertEqual(self.db.execute("SELECT also_on FROM jobs WHERE id = 'a:2'").fetchone()[0], '["Stellenwerk"]')

    def test_the_log_line_names_the_dropped_posting_and_the_one_it_matched(self):
        radar.store_new(self.db, [{**self.JOB, "id": "ba:1"}])
        with mock.patch.object(radar, "log") as log:
            radar.store_new(self.db, [{**self.JOB, "id": "an:9", "source": "Arbeitnow"}])
        lines = " ".join(str(c.args[0]) for c in log.call_args_list)
        self.assertIn("an:9", lines)
        self.assertIn("ba:1", lines)
        self.assertIn("Beispiel GmbH", lines)

    def test_a_job_without_a_company_is_never_deduplicated(self):
        radar.store_new(self.db, [{**self.JOB, "id": "a:1", "company": ""}, {**self.JOB, "id": "a:2", "company": ""}])
        self.assertEqual(self.ids(), ["a:1", "a:2"])

    def test_city_key(self):
        self.assertEqual(radar.city_key("Darmstadt, Hessen"), "darmstadt")
        self.assertEqual(radar.city_key("Mainz (Rheinland-Pfalz)"), "mainz")
        self.assertEqual(radar.city_key("  Frankfurt am Main "), "frankfurt am main")
        self.assertEqual(radar.city_key(None), "")


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
