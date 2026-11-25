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
        self.assertIn('<ol class="steps">', out)

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


class RegenerateOptionTests(unittest.TestCase):
    def setUp(self):
        self._keep = fresh_db()
        self.db = radar.open_db()
        self.db.execute("INSERT INTO resume_versions (job_id, n) VALUES ('t:1', 1)")
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def check(self, notes="", base=None, label=""):
        return d.check_tailor_options(self.db, "t:1", notes, base, label)

    def test_plain_regenerate_and_valid_edit_are_accepted(self):
        self.assertIsNone(self.check())
        self.assertIsNone(self.check("swap project A for B", 1, "final"))

    def test_editing_a_version_needs_instructions_and_an_existing_version(self):
        self.assertIn("describe", self.check("  ", 1))
        self.assertIn("does not exist", self.check("x", 7))

    def test_bad_labels_and_overlong_notes_are_rejected(self):
        self.assertIn("label", self.check(label="a;b"))
        self.assertIn("too long", self.check("x" * 2001))


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

    def test_history_appears_for_a_single_version_so_it_can_be_deleted_but_never_on_an_idle_card(self):
        h = self.done([self.v(1)])
        self.assertIn("History (1)", h)
        self.assertIn('data-act="resume-delete" data-n="1"', h)
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



class HistoryControlsTests(unittest.TestCase):
    def v(self, n, label=None, keep=0):
        return Row(job_id="t:1", n=n, created="2026-11-16 09:03:00", role="devops", lang="de", pdf=1, label=label, keep=keep)

    def html(self, versions):
        return d.render_tailor(Row(id="t:1", score=78, tj_state="done", tj_pdf=1, tj_warnings="[]",
                                   tj_finished="2026-11-24 16:14:00"), versions)

    def test_every_version_can_be_deleted_and_the_current_one_is_marked_so(self):
        h = self.html([self.v(3), self.v(2), self.v(1)])
        for n in (1, 2, 3):
            self.assertIn(f'data-act="resume-delete" data-n="{n}"', h)
        self.assertIn('data-n="3" data-pinned="0" data-cur="1" data-prev="1"', h)
        self.assertIn('data-n="2" data-pinned="0" data-cur="0"', h)
        self.assertIn('data-n="1" data-pinned="0" data-cur="1" data-prev="0"', self.html([self.v(1)]))
        self.assertIn("resume-delete-older", h)

    def test_labels_are_shown_escaped_and_the_bulk_button_hides_when_only_pinned_remain(self):
        h = self.html([self.v(2), self.v(1, "<b>x</b>", 1)])
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", h)
        self.assertNotIn("<b>x</b>", h)
        self.assertNotIn("resume-delete-older", h)

    def test_regenerate_panel_defaults_to_the_latest_version(self):
        h = self.html([self.v(2), self.v(1)])
        self.assertIn('<option value="2" selected>', h)
        self.assertIn("Master CV (start fresh)", h)


class DemoTests(unittest.TestCase):
    """demo.py seeds made-up data into a throwaway database: it must stay consistent with the schema and the renderers."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        import demo
        self._keep = fresh_db()
        self._tmp = tempfile.TemporaryDirectory()
        self._old = radar.TAILORED_DIR
        radar.TAILORED_DIR = Path(self._tmp.name)
        self.demo = demo
        self.db = radar.open_db()
        demo.seed(radar, self.db)

    def tearDown(self):
        self.db.close()
        radar.TAILORED_DIR = self._old
        self._tmp.cleanup()

    def test_seed_creates_jobs_a_fit_analysis_and_three_resume_versions_with_files(self):
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], len(self.demo.JOBS))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM resume_versions WHERE job_id = 'demo:1'").fetchone()[0], 3)
        token = radar.file_token("demo:1")
        for n in (1, 2, 3):
            self.assertTrue(radar.version_path(token, n, "tex").exists() and radar.version_path(token, n, "pdf").exists())

    def test_the_card_of_the_demo_job_renders_gaps_history_and_the_pinned_label(self):
        row = self.db.execute(d.ROW_SQL + " WHERE jobs.id = 'demo:1'").fetchone()
        with d.db() as con:
            versions = d.versions_for(con, ["demo:1"])["demo:1"]
        html = d.render_tailor(row, versions) + d.fit_details(row)
        for needle in ("gapcard crit", "2 gaps", "History (3)", 'class="tag keep">final', 'data-act="resume-delete" data-n="1"'):
            self.assertIn(needle, html)

    def test_the_demo_contains_no_real_personal_data(self):
        blob = " ".join(str(v) for r in self.db.execute("SELECT * FROM jobs") for v in tuple(r)) + self.demo.FIT
        for forbidden in ("@", "gmail", "Darmstadt, Germany"):
            self.assertNotIn(forbidden, blob)


class FitPanelTests(unittest.TestCase):
    MD = """## Verdict
Good fit.

## What matches
- **Python** -> five years of it
- plain bullet without arrow

## Gaps
- Kubernetes is not in the CV. The posting says containers (nice-to-have).
- No AWS experience (critical to clarify, not a skill gap).

## Learn first
1. **Kubernetes basics**: run k3s on the server. About 8\u201312 h.
2. Just read a book.

## CV tailoring
Variant: devops.

## Application hook
Hello <b>there</b>.
"""

    def html(self):
        return d.render_fit(self.MD, "hi")

    def test_gaps_come_right_after_the_verdict_before_the_matches(self):
        h = self.html()
        self.assertLess(h.index("Skill gaps"), h.index("What matches"))
        self.assertLess(h.index("verdict"), h.index("Skill gaps"))
        self.assertLess(h.index("What matches"), h.index("Learn first"))

    def test_gaps_are_cards_with_severity_headline_and_detail(self):
        h = self.html()
        self.assertIn('class="gapcard nice"', h)
        self.assertIn('class="gapcard crit"', h)
        self.assertIn("<strong>Kubernetes is not in the CV</strong>", h)
        self.assertIn("The posting says containers", h)
        self.assertIn("(to clarify, not a skill gap)", h)  # the explanation survives, only the keyword became a tag
        self.assertIn("The posting says containers.", h)  # no stray space where the tag was
        self.assertNotIn("(nice-to-have)", h)

    def test_matches_and_learning_steps_are_parsed_with_a_graceful_fallback(self):
        h = self.html()
        self.assertIn("<strong>Python</strong><span>five years of it</span>", h)
        self.assertIn("plain bullet without arrow", h)
        self.assertIn('<span class="hours">8\u201312 h</span>', h)
        self.assertIn("<strong>Kubernetes basics</strong>", h)
        self.assertIn("Just read a book.", h)

    def test_everything_is_escaped_and_cv_tailoring_is_collapsed(self):
        h = self.html()
        self.assertNotIn("<b>there</b>", h)
        self.assertIn("&lt;b&gt;there&lt;/b&gt;", h)
        self.assertIn('<details class="sub">', h)

    def test_unstructured_text_still_renders(self):
        self.assertIn("plain", d.render_fit("plain text only", "mid"))

    def test_the_summary_counts_the_job_gaps(self):
        r = Row(id="t:1", score=70, tailoring_md=self.MD, gaps='["Kubernetes", "Terraform"]')
        self.assertIn("2 gaps", d.fit_details(r))
        self.assertNotIn("gapcount", d.fit_details(Row(id="t:1", score=70, tailoring_md=self.MD, gaps="[]")))


if __name__ == "__main__":
    unittest.main()
