import json
import unittest
from datetime import datetime

from tests.helpers import Row, fresh_db, radar

_keep = fresh_db()  # dashboard.py migrates the database at import time: make sure that is the temporary one
import dashboard as d  # noqa: E402  (needs the venv: fastapi, uvicorn)


class BindGuardTests(unittest.TestCase):
    def test_only_tailscale_and_loopback_addresses_are_allowed(self):
        for ok in ("100.64.0.1", "127.0.0.1", "fd7a:115c:a1e0::1"):
            d.check_bind_host(ok)  # must not raise
        for bad in ("0.0.0.0", "192.168.1.5", "8.8.8.8", "::", "example.com", ""):
            with self.assertRaises(SystemExit, msg=bad):
                d.check_bind_host(bad)


class HelperTests(unittest.TestCase):
    def test_safe_url_only_allows_http_and_https(self):
        self.assertEqual(d.safe_url("https://ok.example/x"), "https://ok.example/x")
        for bad in ("javascript:alert(1)", "data:text/html,x", "", None):
            self.assertEqual(d.safe_url(bad), "#")

    def test_status_views(self):
        self.assertTrue(d.status_ok("new", "open") and d.status_ok("interested", "open"))
        self.assertFalse(d.status_ok("applied", "open") or d.status_ok("not_relevant", "open"))
        self.assertFalse(d.status_ok("not_relevant", "active"))
        self.assertTrue(d.status_ok("not_relevant", "all") and d.status_ok("applied", "applied"))

    def test_bands(self):
        self.assertEqual([d.band(s) for s in (None, 90, 70, 69, 50, 49, 0)], ["none", "hi", "hi", "mid", "mid", "lo", "lo"])

    def test_next_run_skips_weekends_and_matches_the_timer(self):
        tz = d.BERLIN
        self.assertEqual(d.next_run(datetime(2026, 11, 14, 12, 0, tzinfo=tz)), datetime(2026, 11, 16, 8, 0, tzinfo=tz))  # Saturday
        self.assertEqual(d.next_run(datetime(2026, 11, 20, 9, 0, tzinfo=tz)), datetime(2026, 11, 23, 8, 0, tzinfo=tz))   # Friday after 08:00
        self.assertEqual(d.next_run(datetime(2026, 11, 16, 7, 0, tzinfo=tz)), datetime(2026, 11, 16, 8, 0, tzinfo=tz))   # Monday early

    def test_download_names_are_a_strict_whitelist(self):
        self.assertTrue(d.FILE_NAME_RE.match("0123456789abcdef.pdf"))
        for bad in ("../radar.py", "0123456789abcdef.sh", "0123456789ABCDEF.pdf", "0123456789abcde.pdf", "a/0123456789abcdef.pdf"):
            self.assertIsNone(d.FILE_NAME_RE.match(bad), bad)


class RenderingTests(unittest.TestCase):
    def test_fit_analysis_is_escaped_and_structured(self):
        md = "## Verdict\nGood fit <script>x</script>\n## Gaps\n- Docker (critical)\n- ITIL (nice-to-have)\n## Learn first\n1. **AD** lab"
        out = d.render_fit(md, "hi")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn('class="verdict hi"', out)
        self.assertIn('tag crit', out)
        self.assertIn("<ol>", out)

    def test_tailor_slot_states(self):
        base = Row(id="t:1", score=78, description="x", tailoring_md=None)
        idle = d.render_tailor(Row(base))
        self.assertIn('data-state="idle"', idle)
        self.assertIn("Tailor resume", idle)
        pending = d.render_tailor(Row(base, tj_state="running", tj_requested="2026-11-16 08:13:00"))
        self.assertIn("data-pending", pending)
        self.assertIn('role="status"', pending)
        failed = d.render_tailor(Row(base, tj_state="failed", tj_error="<boom>"))
        self.assertIn('role="alert"', failed)
        self.assertIn("&lt;boom&gt;", failed)

    def test_done_state_with_pdf_and_warnings(self):
        warnings = json.dumps(["Not found in your master CV, please verify: Kubernetes, Terraform"])
        out = d.render_tailor(Row(id="t:1", score=78, tj_state="done", tj_pdf=1, tj_warnings=warnings,
                                  tj_finished="2026-11-16 07:14:00"))
        self.assertIn("Resume (PDF)", out)
        self.assertIn("Check 2 claims before sending", out)
        self.assertIn("<strong>Kubernetes</strong>", out)
        self.assertIn("Resume tailored · 08:14", out)  # stored UTC, shown in Berlin time

    def test_done_state_without_a_pdf_promotes_the_tex(self):
        warnings = json.dumps(["No LaTeX engine (tectonic) installed on the server: only the .tex was produced."])
        out = d.render_tailor(Row(id="t:1", score=78, tj_state="done", tj_pdf=0, tj_warnings=warnings,
                                  tj_finished="2026-11-16 07:14:00"))
        self.assertNotIn("Resume (PDF)", out)
        self.assertIn("No PDF this time", out)
        self.assertIn("LaTeX source (.tex)", out)

    def test_card_never_leaks_markup_from_job_data(self):
        self._keep = fresh_db()
        db = radar.open_db()
        self.addCleanup(db.close)
        from tests.helpers import insert_job
        row = Row(insert_job(db, title='<img src=x onerror=1>', company="A & B", url="javascript:alert(1)"))
        out = d.render_card(row)
        self.assertNotIn("<img", out)
        self.assertIn('href="#"', out)
        self.assertIn("A &amp; B", out)


class HistoryRenderingTests(unittest.TestCase):
    def v(self, n, pdf=1, role="devops", lang="de", created="2026-11-16 09:03:00"):
        return Row(job_id="t:1", n=n, created=created, role=role, lang=lang, pdf=pdf)

    def done(self, versions):
        return d.render_tailor(Row(id="t:1", score=78, tj_state="done", tj_pdf=1, tj_warnings="[]",
                                   tj_finished="2026-11-24 16:14:00"), versions)

    def test_filename_whitelist_accepts_versions_only_in_the_strict_form(self):
        for ok in ("0123456789abcdef.v1.pdf", "0123456789abcdef.v12.tex", "0123456789abcdef.v1234.pdf", "0123456789abcdef.pdf"):
            self.assertTrue(d.FILE_NAME_RE.match(ok), ok)
        for bad in ("0123456789abcdef.v.pdf", "0123456789abcdef.v0.pdf", "0123456789abcdef.v01.pdf", "0123456789abcdef.v1234567.pdf",
                    "0123456789abcdef.v1.sh", "0123456789abcdef..v1.pdf", "../0123456789abcdef.v1.pdf", "0123456789abcdef.v-1.pdf",
                    "0123456789abcdef.pdf\n", "0123456789abcdef.v1.pdf\n"):
            self.assertIsNone(d.FILE_NAME_RE.match(bad), repr(bad))

    def test_history_is_collapsed_by_default_and_lists_every_version(self):
        out = self.done([self.v(2, role="swe", lang="en"), self.v(1)])
        self.assertIn('<details class="hist">', out)  # no "open" attribute: the owner pops it up
        self.assertIn("History (2)", out)
        self.assertIn(".v1.pdf", out)
        self.assertIn(".v2.tex", out)
        self.assertEqual(out.count(">current</span>"), 1)  # only the newest is tagged

    def test_no_history_control_when_there_is_only_one_version(self):
        self.assertNotIn("hist", self.done([self.v(1)]))
        self.assertNotIn("hist", self.done([]))
        self.assertNotIn("hist", d.render_tailor(Row(id="t:1", score=5), [self.v(1), self.v(2)]))  # idle card

    def test_failed_regeneration_still_offers_the_previous_version(self):
        out = d.render_tailor(Row(id="t:1", score=78, tj_state="failed", tj_error="boom"), [self.v(1)])
        self.assertIn("History (1)", out)
        self.assertNotIn(">current</span>", out)  # nothing is current after a failed run

    def test_versions_without_a_pdf_only_link_the_tex(self):
        out = self.done([self.v(2, pdf=0), self.v(1)])
        self.assertNotIn(".v2.pdf", out)
        self.assertIn(".v2.tex", out)

    def test_versions_for_filters_and_never_widens_an_empty_list(self):
        self._keep = fresh_db()  # own database: never share state with other tests
        db = radar.open_db()
        self.addCleanup(db.close)
        for job, n in (("t:1", 1), ("t:1", 2), ("t:2", 1)):
            db.execute("INSERT INTO resume_versions (job_id, n, role, lang, pdf) VALUES (?, ?, 'swe', 'en', 1)", (job, n))
        db.commit()
        everything = d.versions_for(db)
        self.assertEqual({k: [v["n"] for v in vs] for k, vs in everything.items()}, {"t:1": [2, 1], "t:2": [1]})  # newest first
        self.assertEqual(list(d.versions_for(db, ["t:2"])), ["t:2"])
        self.assertEqual(d.versions_for(db, []), {})  # an empty id list must not return every job

    def test_history_escapes_metadata_and_has_no_stray_separator(self):
        bad = Row(job_id="t:1", n=2, created=None, role="<b>x</b>", lang="<i>", pdf=1)
        out = d.render_history(Row(id="t:1"), [bad, self.v(1)], "done")
        self.assertNotIn("<b>x", out)
        self.assertIn("&lt;B&gt;", out.replace("&lt;I&gt;", "&lt;B&gt;"))  # language is upper-cased, then escaped
        self.assertNotIn('vm"> ·', out)  # empty date: no leading separator
        self.assertIn('aria-label="Download version 2 as PDF"', out)
        self.assertIn('aria-label="Download version 1 LaTeX source"', out)


if __name__ == "__main__":
    unittest.main()
