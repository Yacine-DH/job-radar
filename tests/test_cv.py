import json
import os
import stat
import subprocess
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

from tests.helpers import ROOT, fresh_db, radar  # noqa: F401  (ROOT puts the project on sys.path)

import cv

# A tiny fake master CV: same shape as the real one (preamble, macros, body), no personal data.
MASTER = r"""\documentclass{article}
\newcommand{\gh}[1]{\href{https://github.com/#1}{GitHub}}
\begin{document}
Projects: \gh{owner/repo} TypeScript, Next.js, Python, C\#, 0{,}958 accuracy, 47 test cases, Docker Compose.
\href{https://example.org/me}{example.org/me}
\plink{https://example.org/proj}{Proj}
\end{document}"""
PADDING = "\\item Built things with TypeScript and Python in 47 test cases. " * 12


class SplitAndLanguageTests(unittest.TestCase):
    def test_split_master(self):
        pre, body = cv.split_master(MASTER)
        self.assertIn("\\documentclass", pre)
        self.assertNotIn("\\begin{document}", pre)
        self.assertIn("Projects:", body)
        self.assertNotIn("\\end{document}", body)

    def test_guess_lang(self):
        self.assertEqual(cv.guess_lang("Wir suchen eine Werkstudentin (m/w/d) mit Erfahrung und Kenntnissen"), "de")
        self.assertEqual(cv.guess_lang("We are looking for a working student with experience and skills"), "en")


class ValidatorTests(unittest.TestCase):
    def check(self, extra=""):
        return cv.validate_body(PADDING + extra, MASTER)

    def test_accepts_a_faithful_body(self):
        self.assertIsNone(self.check("\\gh{owner/repo} \\href{https://example.org/me}{me}"))

    def test_rejects_dangerous_or_redefining_latex(self):
        for bad in ("\\input{/etc/passwd}", "\\immediate\\write18{id}", "\\begin{document}", "\\end{document}",
                    "\\newcommand{\\x}{y}", "\\usepackage{x}", "\\def\\a{b}", "\\verb|x|"):
            self.assertIn("forbidden", self.check(bad) or "", bad)

    def test_rejects_links_that_are_not_in_the_master_cv(self):
        self.assertIn("link not in", self.check("\\href{https://evil.example}{x}"))
        self.assertIn("link not in", self.check("\\gh{evil/repo}"))

    def test_plink_urls_must_come_from_the_master_cv(self):
        self.assertIsNone(self.check("\\plink{https://example.org/proj}{Proj}"))
        self.assertIn("link not in", self.check("\\plink{https://evil.example/x}{Proj}"))
        self.assertIn("link not in", self.check("\\plink{https://example.org/proj/../evil}{Proj}"))

    def test_rejects_unbalanced_braces_and_tiny_output(self):
        self.assertEqual(self.check("\\textbf{open"), "unbalanced braces")
        self.assertEqual(cv.validate_body("tiny", MASTER), "body too short")


class OwnerInstructionsTests(unittest.TestCase):
    def prompt_for(self, notes=""):
        captured = {}

        def fake_ask(prompt, system=None, timeout=0):
            captured["p"] = prompt
            return "% role=devops lang=de\n" + PADDING

        job = {"title": "Werkstudent DevOps", "company": "Beispiel GmbH", "location": "Frankfurt", "distance_km": 5,
               "source": "BA", "description": "x" * 400, "cv_track": "devops"}
        with mock.patch.object(radar, "ask_claude", side_effect=fake_ask):
            cv._gen_body(job, "analysis", "\\begin{document}body\\end{document}", "", notes)
        return captured["p"]

    def test_owner_instructions_reach_the_prompt_with_the_no_invention_guard(self):
        p = self.prompt_for("Group the hackathon series as one block and keep all four results.")
        self.assertIn("OWNER INSTRUCTIONS", p)
        self.assertIn("Group the hackathon series as one block", p)
        self.assertIn("never invent facts or links", p)
        self.assertLess(p.index("OWNER INSTRUCTIONS"), p.index("JOB POSTING:"))

    def test_no_instructions_block_when_there_are_none(self):
        self.assertNotIn("OWNER INSTRUCTIONS", self.prompt_for(""))
        self.assertNotIn("OWNER INSTRUCTIONS", self.prompt_for("   "))


class FactFlaggingTests(unittest.TestCase):
    def test_flags_invented_numbers_and_tools_but_not_faithful_text(self):
        faithful = "TypeScript and Python with 0{,}958 accuracy on 47 test cases, Docker Compose."
        self.assertEqual(cv.unverified(faithful, MASTER), [])
        fabricated = faithful + " Kubernetes, Terraform, latency down 73\\%, led 12 engineers."
        flagged = cv.unverified(fabricated, MASTER)
        for expected in ("73", "12", "Kubernetes", "Terraform"):
            self.assertIn(expected, flagged)


class VersionHistoryTests(unittest.TestCase):
    """Every regenerated resume is archived as a numbered version; nothing is lost on Regenerate."""

    def setUp(self):
        self._keep = fresh_db()
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = radar.TAILORED_DIR
        radar.TAILORED_DIR = Path(self._tmp.name)
        self.db = radar.open_db()
        self.job = "t:1"
        self.token = radar.file_token(self.job)

    def tearDown(self):
        self.db.close()
        radar.TAILORED_DIR = self._old_dir
        self._tmp.cleanup()

    def write_current(self, text, pdf=True):
        (radar.TAILORED_DIR / f"{self.token}.tex").write_text(text)
        if pdf:
            (radar.TAILORED_DIR / f"{self.token}.pdf").write_bytes(b"%PDF fake " + text.encode())

    def rows(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM resume_versions WHERE job_id = ? ORDER BY n", (self.job,))]

    def archived(self, n, ext):
        return radar.version_path(self.token, n, ext)

    def test_snapshots_are_numbered_and_keep_their_own_content(self):
        self.write_current("first")
        self.assertEqual(cv.snapshot_version(self.db, self.job, "devops", "de", []), 1)
        self.write_current("second")
        self.assertEqual(cv.snapshot_version(self.db, self.job, "swe", "en", ["w"]), 2)
        self.assertEqual(self.archived(1, "tex").read_text(), "first")
        self.assertEqual(self.archived(2, "tex").read_text(), "second")
        self.assertTrue(self.archived(1, "pdf").exists() and self.archived(2, "pdf").exists())
        self.assertEqual([(r["n"], r["role"], r["lang"], r["pdf"]) for r in self.rows()], [(1, "devops", "de", 1), (2, "swe", "en", 1)])
        self.assertEqual((radar.TAILORED_DIR / f"{self.token}.tex").read_text(), "second")  # current names unchanged

    def test_no_current_files_means_no_version(self):
        self.assertIsNone(cv.snapshot_version(self.db, self.job))
        self.assertEqual(self.rows(), [])

    def test_a_version_without_pdf_is_recorded_as_such(self):
        self.write_current("tex only", pdf=False)
        cv.snapshot_version(self.db, self.job, "swe", "de", [])
        self.assertEqual(self.rows()[0]["pdf"], 0)
        self.assertFalse(self.archived(1, "pdf").exists())

    def test_role_and_language_come_from_the_wrapper_line_when_not_given(self):
        self.write_current("\\def\\cvrole{ml}\\def\\cvlang{en}\\def\\cvstyle{classic}\nbody")
        cv.snapshot_version(self.db, self.job)
        self.assertEqual((self.rows()[0]["role"], self.rows()[0]["lang"]), ("ml", "en"))

    def test_baseline_registers_existing_files_once_with_their_file_date(self):
        self.write_current("liked version")
        ts = datetime(2026, 11, 16, 9, 3, tzinfo=timezone.utc).timestamp()
        os.utime(radar.TAILORED_DIR / f"{self.token}.tex", (ts, ts))
        self.db.execute("INSERT INTO tailor_jobs (job_id, state, warnings) VALUES (?, 'done', ?)", (self.job, json.dumps(["w1"])))
        cv.ensure_baseline(self.db, self.job)
        cv.ensure_baseline(self.db, self.job)  # idempotent
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["created"], "2026-11-16 09:03:00")
        self.assertEqual(json.loads(rows[0]["warnings"]), ["w1"])
        self.assertEqual(self.archived(1, "tex").read_text(), "liked version")

    def test_regenerating_after_a_baseline_keeps_the_old_version(self):
        self.write_current("old")
        cv.ensure_baseline(self.db, self.job)
        self.write_current("new")  # what resume_job does after the baseline
        cv.snapshot_version(self.db, self.job, "devops", "de", [])
        self.assertEqual([self.archived(n, "tex").read_text() for n in (1, 2)], ["old", "new"])

    def test_only_the_newest_ten_versions_are_kept(self):
        for i in range(1, 13):
            self.write_current(f"v{i}")
            cv.snapshot_version(self.db, self.job, "swe", "en", [])
        self.assertEqual([r["n"] for r in self.rows()], list(range(3, 13)))
        self.assertFalse(self.archived(1, "tex").exists() or self.archived(2, "pdf").exists())
        self.assertEqual(self.archived(12, "tex").read_text(), "v12")

    def test_retention_is_isolated_per_job(self):
        other = "t:2"
        other_token = radar.file_token(other)
        (radar.TAILORED_DIR / f"{other_token}.tex").write_text("other job")
        cv.snapshot_version(self.db, other, "swe", "en", [])
        for i in range(1, 13):
            self.write_current(f"v{i}")
            cv.snapshot_version(self.db, self.job, "swe", "en", [])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM resume_versions WHERE job_id = ?", (other,)).fetchone()[0], 1)
        self.assertEqual(radar.version_path(other_token, 1, "tex").read_text(), "other job")
        self.assertEqual((radar.TAILORED_DIR / f"{other_token}.tex").read_text(), "other job")

    def test_archived_files_are_private(self):
        self.write_current("secret")
        cv.snapshot_version(self.db, self.job, "swe", "en", [])
        for ext in ("tex", "pdf"):
            self.assertEqual(stat.S_IMODE(self.archived(1, ext).stat().st_mode), 0o600)
        self.assertEqual([p.name for p in radar.TAILORED_DIR.iterdir() if p.name.startswith(".")], [])  # no temp files left behind

    def test_a_failed_copy_leaves_no_temp_file_and_keeps_the_old_target(self):
        src, dst = radar.TAILORED_DIR / "src.bin", radar.TAILORED_DIR / "dst.bin"
        src.write_bytes(b"new")
        dst.write_bytes(b"old")
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            cv._private_copy(src, dst)
        self.assertEqual(dst.read_bytes(), b"old")
        self.assertEqual([p.name for p in radar.TAILORED_DIR.iterdir() if p.name.startswith(".")], [])

    def test_baseline_ignores_missing_files_and_survives_bad_warnings_json(self):
        cv.ensure_baseline(self.db, self.job)  # nothing on disk: no version
        self.assertEqual(self.rows(), [])
        self.write_current("liked")
        self.db.execute("INSERT INTO tailor_jobs (job_id, state, warnings) VALUES (?, 'done', '{not json')", (self.job,))
        cv.ensure_baseline(self.db, self.job)
        self.assertEqual(json.loads(self.rows()[0]["warnings"]), [])

    def test_a_stale_archived_pdf_is_removed_when_the_current_pdf_is_gone(self):
        self.write_current("one")
        cv.snapshot_version(self.db, self.job, "swe", "en", [])
        self.archived(2, "pdf").write_bytes(b"stale leftover of an aborted run")
        self.write_current("two", pdf=False)
        (radar.TAILORED_DIR / f"{self.token}.pdf").unlink()
        cv.snapshot_version(self.db, self.job, "swe", "en", [])
        self.assertFalse(self.archived(2, "pdf").exists())
        self.assertEqual(self.rows()[1]["pdf"], 0)

    def test_recompile_updates_the_newest_version_and_its_warnings(self):
        self.write_current("tex", pdf=False)
        cv.snapshot_version(self.db, self.job, "devops", "de", ["No LaTeX engine (tectonic) installed on the server: only the .tex was produced."])
        self.db.execute("INSERT INTO tailor_jobs (job_id, state, pdf, warnings) VALUES (?, 'done', 0, ?)",
                        (self.job, json.dumps(["No LaTeX engine (tectonic) installed on the server: only the .tex was produced."])))
        self.db.commit()

        def fake_compile(exe, tex_path):
            tex_path.with_suffix(".pdf").write_bytes(b"%PDF built")
            return subprocess.CompletedProcess([], 0, "", "")

        with mock.patch.object(cv, "find_tectonic", return_value="tectonic"), \
                mock.patch.object(cv, "compile_tex", side_effect=fake_compile), mock.patch.object(cv, "pdf_pages", return_value=1):
            cv.recompile(None)
        row = self.rows()[0]
        self.assertEqual((row["pdf"], json.loads(row["warnings"])), (1, []))
        self.assertEqual(self.archived(1, "pdf").read_bytes(), b"%PDF built")
        self.assertEqual(stat.S_IMODE(self.archived(1, "pdf").stat().st_mode), 0o600)
        self.assertEqual(self.db.execute("SELECT pdf FROM tailor_jobs WHERE job_id = ?", (self.job,)).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
