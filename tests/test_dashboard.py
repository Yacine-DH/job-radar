import asyncio
import json
import sqlite3
import tempfile
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
        con = d.db()
        try:  # a `with` on a sqlite connection commits but does not close it
            versions = d.versions_for(con, ["demo:1"])["demo:1"]
        finally:
            con.close()
        html = d.render_tailor(row, versions) + d.fit_details(row)
        for needle in ("gapcard crit", "2 gaps", "History (3)", 'class="tag keep">final', 'data-act="resume-delete" data-n="1"'):
            self.assertIn(needle, html)

    def test_the_demo_contains_no_real_personal_data(self):
        blob = " ".join(str(v) for r in self.db.execute("SELECT * FROM jobs") for v in tuple(r)) + self.demo.FIT
        for forbidden in ("@", "gmail", "Darmstadt, Germany"):
            self.assertNotIn(forbidden, blob)


class ArchiveAndViewsTests(unittest.TestCase):
    """Skipped jobs age out of every view after SKIP_DAYS; Applied/Skipped are reachable by a click; nothing is deleted."""

    NOW = datetime(2026, 11, 25, 12, 0, tzinfo=d.BERLIN)

    def r(self, status, days_ago=None, **kw):
        at = None if days_ago is None else (self.NOW.astimezone(d.timezone.utc) - d.timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
        return Row(id="t:1", status=status, status_at=at, **kw)

    def test_only_old_or_undated_skipped_jobs_are_archived(self):
        self.assertFalse(d.is_archived(self.r("not_relevant", 6.9), self.NOW))
        self.assertTrue(d.is_archived(self.r("not_relevant", 7.1), self.NOW))
        self.assertTrue(d.is_archived(self.r("not_relevant", None), self.NOW))   # skipped before the timestamp existed
        for st in ("new", "interested", "applied"):
            self.assertFalse(d.is_archived(self.r(st, 400), self.NOW), st)        # an old application is never archived

    def test_view_matrix(self):
        ok = d.status_ok
        self.assertTrue(ok("applied", "applied") and ok("applied", "all") and not ok("applied", "open"))
        self.assertTrue(ok("not_relevant", "not_relevant") and ok("not_relevant", "all") and not ok("not_relevant", "active"))
        for mode in ("all", "active", "open", "not_relevant"):
            self.assertFalse(ok("not_relevant", mode, old=True), mode)   # archived jobs vanish from every normal view
        self.assertTrue(ok("not_relevant", "archived", old=True))
        self.assertFalse(ok("not_relevant", "archived", old=False))
        self.assertFalse(ok("applied", "archived"))

    def test_the_card_carries_the_archive_flag_for_the_client_side_filter(self):
        from datetime import datetime as dt
        old = Row(id="t:1", title="x", status="not_relevant", status_at="2020-01-01 00:00:00", score=40, first_seen="2026-01-01 00:00:00")
        recent = Row(id="t:2", title="x", status="not_relevant", status_at=dt.now(d.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                     score=40, first_seen="2026-01-01 00:00:00")
        self.assertIn('data-old="1"', d.render_card(old))
        self.assertNotIn("data-old", d.render_card(recent))


class FrontPageTests(unittest.TestCase):
    """The whole front page rendered from the demo data (tiles, chips, counts)."""

    class Req:
        class state:
            nonce = "n0nce"

    def setUp(self):
        import demo
        from pathlib import Path
        from unittest import mock
        self._keep = fresh_db()
        self._tmp = tempfile.TemporaryDirectory()
        self._old = radar.TAILORED_DIR
        radar.TAILORED_DIR = Path(self._tmp.name)
        self._conns = []

        def connect():
            con = sqlite3.connect(radar.DB_PATH, timeout=10)
            con.row_factory = sqlite3.Row
            self._conns.append(con)
            return con

        self._patch = mock.patch.object(d, "db", connect)
        self._patch.start()
        setup = radar.open_db()
        demo.seed(radar, setup)
        setup.close()

    def tearDown(self):
        self._patch.stop()
        for con in self._conns:
            con.close()
        radar.TAILORED_DIR = self._old
        self._tmp.cleanup()

    def page(self, **kw):
        args = dict(min_score=0, source="all", status="open", sort="score", q="")
        args.update(kw)
        return d.index(self.Req(), **args).body.decode()

    def test_counters_are_buttons_and_the_chips_show_the_right_counts(self):
        h = self.page()
        for view in ("new", "all", "interested", "applied"):
            self.assertRegex(h, rf'<button type="button" class="stat[^"]*" data-view="{view}">')
            self.assertIn(f'data-view="{view}"', h)
        self.assertIn("Applied · 1", h)      # demo:5
        self.assertIn("Skipped · 1", h)      # demo:6 (4 days ago); demo:8 was skipped 10 days ago and is archived
        self.assertIn("Archived · 1", h)
        self.assertIn('data-chip="archived"', h)

    def test_the_archived_job_is_flagged_and_not_counted_as_tracked(self):
        import demo
        h = self.page()
        self.assertEqual(h.count('data-old="1"'), 1)
        self.assertIn(f'data-total="{len(demo.JOBS) - 1}"', h)     # 'tracked' excludes the archived job

    def test_the_server_side_views_show_applied_and_skipped_jobs_and_hide_the_archived_one_everywhere_else(self):
        self.assertIn("Showing 1 of", self.page(status="applied"))
        self.assertIn("Showing 1 of", self.page(status="not_relevant"))   # the recent skip only
        self.assertIn("Showing 1 of", self.page(status="archived"))       # the 10-day-old skip only
        import demo
        self.assertIn(f"Showing {len(demo.JOBS) - 1} of", self.page(status="all"))  # everything but the archived job

    def test_the_script_knows_the_views_the_undo_toast_and_the_archive_flag(self):
        h = self.page()
        for needle in ("c.dataset.old==='1'", "showView(", "['Undo','undo']", "delete c.dataset.old",
                       "okStatus=(s,m,old)"):
            self.assertIn(needle, h)


class RouteTests(unittest.TestCase):
    """The new JSON routes called directly (no HTTP server in the test environment): validation, origin check, persistence."""

    class Req:
        def __init__(self, origin=None):
            self.headers = {"origin": origin} if origin else {}

    def setUp(self):
        from pathlib import Path
        from unittest import mock
        self._keep = fresh_db()
        self._tmp = tempfile.TemporaryDirectory()
        self._conns = []
        self._old = (radar.CV_TEX, radar.TAILORED_DIR)
        radar.CV_TEX = Path(self._tmp.name) / "master.tex"
        radar.CV_TEX.write_text("x")
        radar.TAILORED_DIR = Path(self._tmp.name)

        def connect():
            con = sqlite3.connect(radar.DB_PATH, timeout=10)
            con.row_factory = sqlite3.Row
            self._conns.append(con)
            return con

        self._patch = mock.patch.object(d, "db", connect)
        self._patch.start()
        setup = radar.open_db()
        self.token = radar.file_token("t:1")
        setup.execute("INSERT INTO jobs (id, title, status) VALUES ('t:1', 'x', 'new')")
        for n in (1, 2):
            setup.execute("INSERT INTO resume_versions (job_id, n) VALUES ('t:1', ?)", (n,))
            for ext in ("tex", "pdf"):
                radar.version_path(self.token, n, ext).write_bytes(b"v%d" % n)
        setup.commit()
        setup.close()

    def tearDown(self):
        self._patch.stop()
        for con in self._conns:
            con.close()
        radar.CV_TEX, radar.TAILORED_DIR = self._old
        self._tmp.cleanup()

    def call(self, fn, body, origin=None):
        res = asyncio.run(fn(body, self.Req(origin)))
        return getattr(res, "status_code", 200), (json.loads(res.body) if hasattr(res, "body") else res)

    def row(self, sql):
        con = sqlite3.connect(radar.DB_PATH)
        try:
            return con.execute(sql).fetchone()
        finally:
            con.close()

    def test_a_valid_edit_request_persists_notes_base_and_label(self):
        status, body = self.call(d.request_tailor, d.TailorRequest(id="t:1", notes=" swap A for B ", base=1, label="final"))
        self.assertEqual((status, body), (200, {"state": "queued"}))
        self.assertEqual(self.row("SELECT notes, base_n, label, state FROM tailor_jobs"), ("swap A for B", 1, "final", "queued"))

    def test_bad_origin_unknown_version_missing_notes_and_bad_label_are_rejected(self):
        self.assertEqual(self.call(d.request_tailor, d.TailorRequest(id="t:1"), origin="http://evil.example")[0], 403)
        self.assertEqual(self.call(d.request_tailor, d.TailorRequest(id="t:1", notes="x", base=7))[0], 422)
        self.assertEqual(self.call(d.request_tailor, d.TailorRequest(id="t:1", base=1))[0], 422)
        self.assertEqual(self.call(d.request_tailor, d.TailorRequest(id="t:1", label="a;b"))[0], 422)
        self.assertEqual(self.call(d.request_tailor, d.TailorRequest(id="nope"))[0], 404)
        self.assertIsNone(self.row("SELECT 1 FROM tailor_jobs"))

    def test_numbers_outside_the_sqlite_range_are_a_validation_error_not_a_crash(self):
        from pydantic import ValidationError
        for bad in (2**63, 0, -1):
            with self.assertRaises(ValidationError, msg=bad):
                d.TailorRequest(id="t:1", notes="x", base=bad)
            with self.assertRaises(ValidationError, msg=bad):
                d.DeleteVersions(id="t:1", n=bad)

    def test_delete_route_checks_origin_and_active_jobs_and_deletes_the_newest_too(self):
        self.assertEqual(self.call(d.delete_resume_versions, d.DeleteVersions(id="t:1", n=1), origin="http://evil.example")[0], 403)
        self.assertEqual(self.call(d.delete_resume_versions, d.DeleteVersions(id="t:1"))[0], 422)
        con = sqlite3.connect(radar.DB_PATH)
        con.execute("INSERT INTO tailor_jobs (job_id, state) VALUES ('t:1', 'running')")
        con.commit()
        con.close()
        self.assertEqual(self.call(d.delete_resume_versions, d.DeleteVersions(id="t:1", n=1))[0], 409)
        con = sqlite3.connect(radar.DB_PATH)
        con.execute("UPDATE tailor_jobs SET state = 'done'")
        con.commit()
        con.close()
        self.assertEqual(self.call(d.delete_resume_versions, d.DeleteVersions(id="t:1", n=2)), (200, {"deleted": [2]}))
        self.assertEqual((radar.TAILORED_DIR / f"{self.token}.tex").read_bytes(), b"v1")  # v1 is current again


    def test_a_locked_database_is_a_503_not_a_crash(self):
        from unittest import mock
        err = sqlite3.OperationalError("database is locked")
        with mock.patch.object(d, "_request_tailor", side_effect=err):
            self.assertEqual(self.call(d.request_tailor, d.TailorRequest(id="t:1"))[0], 503)
        with mock.patch.object(d.cv, "delete_versions", side_effect=err):
            self.assertEqual(self.call(d.delete_resume_versions, d.DeleteVersions(id="t:1", n=1))[0], 503)
        with mock.patch.object(d, "_request_tailor", side_effect=sqlite3.OperationalError("no such table: x")):
            with self.assertRaises(sqlite3.OperationalError):
                self.call(d.request_tailor, d.TailorRequest(id="t:1"))


class MigrationTests(unittest.TestCase):
    def test_an_old_database_gains_the_new_columns_and_keeps_its_rows(self):
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old = radar.DB_PATH
        self.addCleanup(setattr, radar, "DB_PATH", old)
        radar.DB_PATH = Path(tmp.name) / "old.db"
        con = sqlite3.connect(radar.DB_PATH)
        con.execute("CREATE TABLE tailor_jobs (job_id TEXT PRIMARY KEY, state TEXT NOT NULL, requested TEXT, finished TEXT, error TEXT, pdf INTEGER NOT NULL DEFAULT 0, warnings TEXT)")
        con.execute("CREATE TABLE resume_versions (job_id TEXT NOT NULL, n INTEGER NOT NULL, created TEXT, role TEXT, lang TEXT, pdf INTEGER NOT NULL DEFAULT 0, warnings TEXT, PRIMARY KEY (job_id, n))")
        con.execute("INSERT INTO tailor_jobs (job_id, state) VALUES ('t:1', 'done')")
        con.execute("INSERT INTO resume_versions (job_id, n) VALUES ('t:1', 1)")
        con.commit()
        con.close()
        db = radar.open_db()
        try:
            tj = {r["name"] for r in db.execute("PRAGMA table_info(tailor_jobs)")}
            rv = {r["name"] for r in db.execute("PRAGMA table_info(resume_versions)")}
            self.assertLessEqual({"notes", "base_n", "label"}, tj)
            self.assertLessEqual({"label", "keep"}, rv)
            self.assertEqual(db.execute("SELECT keep, label FROM resume_versions").fetchone()[:], (0, None))
            self.assertEqual(db.execute("SELECT state FROM tailor_jobs").fetchone()[0], "done")
            radar.open_db().close()  # opening twice must not fail on the existing columns
        finally:
            db.close()


class DownloadNameTests(unittest.TestCase):
    TOKEN = "0123456789abcdef"

    def test_slug_handles_umlauts_ampersands_and_punctuation(self):
        self.assertEqual(d.slug("Müller & Söhne GmbH", 40), "Mueller-Soehne-GmbH")
        self.assertEqual(d.slug("Straße (m/w/d) – IT", 40), "Strasse-m-w-d-IT")
        self.assertEqual(d.slug("Café Ünal", 40), "Cafe-Uenal")

    def test_decomposed_umlauts_and_the_capital_sharp_s(self):
        self.assertEqual(d.slug("Mu\u0308ller", 40), "Mueller")        # u + combining diaeresis (macOS, PDF, scraped feeds)
        self.assertEqual(d.slug("Sch\u00f6n\u0308", 40), "Schoen")      # already-composed o-umlaut plus a stray mark
        self.assertEqual(d.slug("STRA\u1e9eE", 40), "STRASSE")
        self.assertEqual(d.slug("Stra\u00dfe", 40), "Strasse")

    def test_slug_is_limited_at_a_word_boundary_and_never_empty_handed(self):
        words = "Werkstudent Softwareentwicklung Python Backend"
        self.assertEqual(d.slug(words, 31), "Werkstudent-Softwareentwicklung")   # exactly 31 characters fits
        self.assertEqual(d.slug(words, 30), "Werkstudent")                      # one more word would not: cut at the boundary
        self.assertLessEqual(len(d.slug("x" * 200, 24)), 24)
        self.assertEqual(d.slug("x" * 200, 24), "x" * 24)
        self.assertEqual((d.slug(None, 10), d.slug("", 10), d.slug("&&& ---", 10)), ("", "", ""))

    def test_download_name_for_pdf_tex_and_versions(self):
        self.assertEqual(d.download_name("Beispiel GmbH", "Werkstudent DevOps (w/m/d)", f"{self.TOKEN}.pdf"),
                         "Resume_Beispiel-GmbH_Werkstudent-DevOps-w-m-d.pdf")
        self.assertEqual(d.download_name("Beispiel GmbH", "Werkstudent", f"{self.TOKEN}.tex"), "Resume_Beispiel-GmbH_Werkstudent.tex")
        self.assertEqual(d.download_name("Beispiel GmbH", "Werkstudent", f"{self.TOKEN}.v12.pdf"), "Resume_Beispiel-GmbH_Werkstudent_v12.pdf")

    def test_empty_company_or_title_and_unknown_shapes(self):
        self.assertEqual(d.download_name("", "Werkstudent", f"{self.TOKEN}.pdf"), "Resume_Werkstudent.pdf")
        self.assertEqual(d.download_name(None, None, f"{self.TOKEN}.pdf"), "Resume.pdf")
        self.assertEqual(d.download_name("A", "B", "../evil.pdf"), "resume-../evil.pdf")  # never reached: the route 404s first

    def test_non_ascii_digits_are_not_version_numbers(self):
        for digits in ("\u0662", "\uff12", "1\u0662"):                       # Arabic-Indic, fullwidth, mixed
            name = f"{self.TOKEN}.v{digits}.pdf"
            self.assertEqual(d.download_name("A", "B", name), f"resume-{name}", digits)   # not parsed as a version
            self.assertIsNone(d.FILE_NAME_RE.match(name), digits)                         # and refused by the whitelist

    def test_the_result_is_always_a_safe_ascii_filename(self):
        for company, title in (('Evil"; filename="x', "a\r\nSet-Cookie: x=1"), ("../../etc", "passwd\x00"), ("日本語", "テスト")):
            name = d.download_name(company, title, f"{self.TOKEN}.pdf")
            self.assertRegex(name, r"\AResume(_[A-Za-z0-9-]+)*\.pdf\Z", name)
            self.assertLessEqual(len(name), 80)


class DownloadRouteTests(unittest.TestCase):
    def setUp(self):
        from pathlib import Path
        self._keep = fresh_db()
        self._tmp = tempfile.TemporaryDirectory()
        self._old = radar.TAILORED_DIR
        radar.TAILORED_DIR = Path(self._tmp.name)
        db = radar.open_db()
        db.execute("INSERT INTO jobs (id, title, company, status) VALUES ('t:1', 'Werkstudent Python', 'Müller AG', 'new')")
        db.commit()
        db.close()
        self.token = radar.file_token("t:1")
        for suffix in (".pdf", ".tex", ".v2.pdf"):
            (radar.TAILORED_DIR / f"{self.token}{suffix}").write_bytes(b"x")
        (radar.TAILORED_DIR / ("a" * 16 + ".pdf")).write_bytes(b"x")  # a file whose job is unknown

    def tearDown(self):
        radar.TAILORED_DIR = self._old
        self._tmp.cleanup()

    def get(self, name):
        from unittest import mock
        conns = []

        def connect():
            con = sqlite3.connect(radar.DB_PATH, timeout=10)
            con.row_factory = sqlite3.Row
            conns.append(con)
            return con

        try:
            with mock.patch.object(d, "db", connect):
                return d.download(name)
        finally:
            for con in conns:
                con.close()

    def test_pdf_tex_and_version_downloads_get_friendly_names(self):
        for suffix, expected in ((".pdf", "Resume_Mueller-AG_Werkstudent-Python.pdf"), (".tex", "Resume_Mueller-AG_Werkstudent-Python.tex"),
                                 (".v2.pdf", "Resume_Mueller-AG_Werkstudent-Python_v2.pdf")):
            res = self.get(self.token + suffix)
            self.assertEqual(res.status_code, 200)
            self.assertIn(f'filename="{expected}"', res.headers["content-disposition"], suffix)

    def test_an_unknown_job_keeps_the_old_name(self):
        res = self.get("a" * 16 + ".pdf")
        self.assertIn('filename="resume-' + "a" * 16 + '.pdf"', res.headers["content-disposition"])

    def test_a_locked_database_falls_back_to_the_plain_name_instead_of_a_500(self):
        from unittest import mock
        with mock.patch.object(d, "db", side_effect=sqlite3.OperationalError("database is locked")):
            res = d.download(self.token + ".pdf")
        self.assertEqual(res.status_code, 200)
        self.assertIn(f'filename="resume-{self.token}.pdf"', res.headers["content-disposition"])

    def test_unicode_digit_versions_are_404_in_the_route_even_when_such_a_file_exists(self):
        for digits in ("\u0662", "1\u0662"):                          # first position, and after an ASCII digit (the \d{0,5} part)
            (radar.TAILORED_DIR / f"{self.token}.v{digits}.pdf").write_bytes(b"x")   # so only the whitelist can say no
            self.assertEqual(self.get(f"{self.token}.v{digits}.pdf").status_code, 404, digits)

    def test_path_traversal_and_odd_names_still_404(self):
        for bad in ("../" + self.token + ".pdf", self.token + ".pdf/../x", "%2e%2e%2f" + self.token + ".pdf", self.token + ".exe",
                    "A" * 16 + ".pdf", self.token + ".v0.pdf", self.token + ".v1234567.pdf", "..", ""):
            self.assertEqual(self.get(bad).status_code, 404, bad)


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

    def test_wrapped_bullets_and_prose_lines_are_kept_not_dropped(self):
        md = "## Gaps\nSome intro sentence.\n- Kubernetes is missing\n  and this wrapped line has the detail (nice-to-have)\n- Second gap\n"
        h = d.render_fit(md, "mid")
        self.assertIn("Some intro sentence.", h)
        self.assertIn("this wrapped line has the detail", h)
        self.assertEqual(h.count('<li class="gapcard'), 2)
        self.assertIn('class="gapcard nice"', h)

    def test_abbreviations_do_not_split_the_headline(self):
        sev, head, detail = d.split_gap("Missing cloud tools (e.g. AWS, GCP). Required for the role.")
        self.assertEqual(head, "Missing cloud tools (e.g. AWS, GCP)")
        self.assertEqual(detail, "Required for the role.")
        self.assertEqual(d.split_gap("Uses i.e. tools vs. others. Detail here.")[1], "Uses i.e. tools vs. others")

    def test_a_second_hours_estimate_stays_in_the_text(self):
        h = d.render_learn_block(["1. **K8s**: install k3s (About 2 h) and deploy an app. About 4 h."])
        self.assertIn('<span class="hours">2 h</span>', h)
        self.assertIn("About 4 h", h)
        self.assertNotIn("()", h)


    def test_abbreviation_matching_has_word_boundaries_and_handles_german_forms(self):
        self.assertEqual(d.split_gap("Fehlt z.B. Kubernetes. Mehr Details.")[1:], ("Fehlt z.B. Kubernetes", "Mehr Details."))
        self.assertEqual(d.split_gap("Fehlt z. B. Docker. Rest.")[1], "Fehlt z. B. Docker")
        self.assertEqual(d.split_gap("No experience managing a read replica. Add a DB project.")[1:],
                         ("No experience managing a read replica", "Add a DB project."))
        self.assertEqual(d.split_gap("Worked with CSVs. Need Parquet.")[1], "Worked with CSVs")
        self.assertEqual(d.split_gap("No production experience with C. Add a systems project.")[1], "No production experience with C")

    def test_a_real_sentence_end_before_a_capitalized_abbreviation_still_splits(self):
        self.assertEqual(d.split_gap("Missing backend experience. E.g. FastAPI or Django.")[1:],
                         ("Missing backend experience", "E.g. FastAPI or Django."))
        self.assertEqual(d.split_gap("Requires work permit. U.S. citizens only.")[1], "Requires work permit")

    def test_spaced_dotted_abbreviations_do_not_split(self):
        self.assertEqual(d.split_gap("Cloud tools e. g. Terraform. Nice to have.")[1], "Cloud tools e. g. Terraform")
        self.assertEqual(d.split_gap("Backend d. h. FastAPI. Nice to have.")[1], "Backend d. h. FastAPI")

    def test_empty_parentheses_in_code_survive_and_only_the_wrapping_ones_go(self):
        h = d.render_learn_block(["1. **Async**: practice asyncio.run() and main(). About 3 h."])
        self.assertIn("asyncio.run() and main()", h)
        h = d.render_learn_block(["1. **K8s**: install k3s (About 2 h) and deploy."])
        self.assertIn("install k3s and deploy", h)
        self.assertNotIn("()", h)

    def test_a_closing_note_stays_after_the_list(self):
        h = d.render_gaps_block(["- Gap one (nice-to-have)", "", "Overall these are minor."])
        self.assertLess(h.index("gapcard"), h.index("Overall these are minor."))

    def test_unstructured_text_still_renders(self):
        self.assertIn("plain", d.render_fit("plain text only", "mid"))

    def test_the_summary_counts_the_job_gaps(self):
        r = Row(id="t:1", score=70, tailoring_md=self.MD, gaps='["Kubernetes", "Terraform"]')
        self.assertIn("2 gaps", d.fit_details(r))
        self.assertNotIn("gapcount", d.fit_details(Row(id="t:1", score=70, tailoring_md=self.MD, gaps="[]")))


if __name__ == "__main__":
    unittest.main()
