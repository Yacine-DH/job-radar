import sys
import unittest

from tests.helpers import ROOT  # noqa: F401  (puts the project on sys.path)

import cv

# A tiny fake master CV: same shape as the real one (preamble, macros, body), no personal data.
MASTER = r"""\documentclass{article}
\newcommand{\gh}[1]{\href{https://github.com/#1}{GitHub}}
\begin{document}
Projects: \gh{owner/repo} TypeScript, Next.js, Python, C\#, 0{,}958 accuracy, 47 test cases, Docker Compose.
\href{https://example.org/me}{example.org/me}
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

    def test_rejects_unbalanced_braces_and_tiny_output(self):
        self.assertEqual(self.check("\\textbf{open"), "unbalanced braces")
        self.assertEqual(cv.validate_body("tiny", MASTER), "body too short")


class FactFlaggingTests(unittest.TestCase):
    def test_flags_invented_numbers_and_tools_but_not_faithful_text(self):
        faithful = "TypeScript and Python with 0{,}958 accuracy on 47 test cases, Docker Compose."
        self.assertEqual(cv.unverified(faithful, MASTER), [])
        fabricated = faithful + " Kubernetes, Terraform, latency down 73\\%, led 12 engineers."
        flagged = cv.unverified(fabricated, MASTER)
        for expected in ("73", "12", "Kubernetes", "Terraform"):
            self.assertIn(expected, flagged)


if __name__ == "__main__":
    unittest.main()
