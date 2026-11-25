#!/usr/bin/env python3
"""Run the real dashboard on made-up data, so you can look around without a CV, an API key or a server.

    .venv/bin/python demo.py                       # http://127.0.0.1:8091/
    .venv/bin/python demo.py --snapshot demo.html  # write the front page as one static HTML file and exit

Everything lives in a temporary directory that is deleted on exit: it never reads or writes your real jobs.db or cv/.
Needs the dashboard dependencies (see requirements.txt). The people, companies and postings below are invented.
"""
import argparse
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

FIT = """## Verdict
Strong fit, so apply. Your Python and Docker experience matches the core of this posting and the office is close to Darmstadt.

## What matches
- **Python and SQL** -> two course projects and the Job Radar pipeline (SQLite, FastAPI).
- **Docker and Linux** -> a self-hosted home server run with Docker Compose and systemd timers.
- **CI/CD** -> GitHub Actions deployment for a small web app.
- **Fluent German and English** -> German C1, English C1.

## Gaps
- Kubernetes is not in the CV. The posting mentions it once as a plus (nice-to-have).
- Terraform is not in the CV, but the posting asks for infrastructure as code (critical to address in the cover letter).
- No professional DevOps role yet. The experience is from personal projects (nice-to-have; present it honestly).

## Learn first
1. **Kubernetes basics**: run k3s on the home server and deploy one Compose app with a Deployment and a Service. About 8-12 h.
2. **Terraform**: describe the server's base setup as code and apply it to a fresh VM. About 6-8 h.
3. **Pipeline depth**: add test and image-push stages to the existing GitHub Actions workflow. About 3-4 h.

## CV tailoring
**Variant:** devops. **Language:** English.

- Lead with the home-server project, then the pipeline project.

## Application hook
I run my own 24/7 Linux server with Docker Compose and automated backups next to my computer science studies, and I would like to bring that practice to your platform team.
"""

# (id, title, company, location, km, score, reason, track, gaps, status, source, days_ago)
JOBS = [
    ("demo:1", "Werkstudent DevOps Engineering (w/m/d)", "Beispiel Systems GmbH", "Darmstadt", 3, 86,
     "Docker, Linux and CI/CD match the CV; Kubernetes is a small gap.", "devops", ["Kubernetes", "Terraform"], "interested", "BA", 0),
    ("demo:2", "Werkstudent Softwareentwicklung Python", "Muster Analytics AG", "Frankfurt am Main", 27, 78,
     "Python and SQL fit well; the stack is mostly backend work.", "swe", ["Airflow"], "new", "Stellenwerk", 0),
    ("demo:3", "Working Student Machine Learning", "Example Labs GmbH", "Darmstadt", 5, 71,
     "Good ML coursework and a fine-tuning project; limited production experience.", "ml", ["PyTorch Lightning", "MLflow"], "new", "Arbeitnow", 1),
    ("demo:4", "Werkstudent Frontend (React)", "Probe Digital GmbH", "Mainz", 29, 64,
     "Frontend experience is there, but the posting wants strong TypeScript.", "swe", ["TypeScript"], "new", "BA", 1),
    ("demo:5", "Werkstudent IT-Support", "Test Logistik SE", "Weiterstadt", 8, 52,
     "Support work matches the current job, but it is not a developer role.", "swe", [], "applied", "BA", 3),
    ("demo:6", "Werkstudent Data Engineering", "Fiktiv Data GmbH", "Wiesbaden", 38, 49,
     "Right topics, but outside the 30 km radius for regular office days.", "ml", ["Spark", "Kafka"], "not_relevant", "Merck", 4),
    ("demo:8", "Werkstudent PHP Webentwicklung", "Alt & Neu Medien GmbH", "Offenbach", 24, 41,
     "Mostly PHP and WordPress; little overlap with the CV.", "swe", [], "not_relevant", "BA", 10),
    ("demo:7", "Praktikum Cloud Operations", "Platzhalter Cloud AG", "Darmstadt", 2, 58,
     "Cloud and Linux fit; it is an internship, not a Werkstudent role.", "devops", ["AWS"], "new", "SmartRecruiters", 2),
]

MINIMAL_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
               b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")


def ts(days: float = 0, hours: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days, hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def seed(radar, db) -> None:
    for jid, title, company, loc, km, score, reason, track, gaps, status, source, days in JOBS:
        db.execute(
            "INSERT INTO jobs (id, source, title, company, location, distance_km, url, description, first_seen, score, reason,"
            " cv_track, gaps, status, status_at, notified) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (jid, source, title, company, loc, km, "https://example.org/jobs/" + jid.split(":")[1],
             f"{title} at {company}. You work on real projects next to your studies, about 20 hours per week.",
             ts(days), score, reason, track, json.dumps(gaps), status, None if status == "new" else ts(days)))
    db.execute("INSERT INTO tailoring (job_id, track, markdown) VALUES ('demo:1', 'devops', ?)", (FIT,))
    token = radar.file_token("demo:1")
    radar.TAILORED_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
    versions = [(1, "first draft", 0, 1), (2, None, 1, 0), (3, "final", 2, 1)]  # a label always pins
    for n, label, hours_ago, keep in versions:
        db.execute("INSERT INTO resume_versions (job_id, n, created, role, lang, pdf, warnings, label, keep) "
                   "VALUES ('demo:1', ?, ?, 'devops', 'en', 1, '[]', ?, ?)", (n, ts(hours=3 - hours_ago), label, keep))
        for ext, data in (("tex", b"% demo placeholder: the real tool writes your tailored LaTeX here\n"), ("pdf", MINIMAL_PDF)):
            radar.version_path(token, n, ext).write_bytes(data)
    for ext, data in (("tex", b"% demo placeholder\n"), ("pdf", MINIMAL_PDF)):
        (radar.TAILORED_DIR / f"{token}.{ext}").write_bytes(data)
    db.execute("INSERT INTO tailor_jobs (job_id, state, pdf, warnings, finished) VALUES ('demo:1', 'done', 1, '[]', ?)", (ts(),))
    db.execute("INSERT INTO runs (ts, counts, failed) VALUES (?, ?, '[]')",
               (ts(hours=1), json.dumps({"BA": 41, "Stellenwerk": 9, "Merck": 3, "Arbeitnow": 12, "SmartRecruiters": 5})))
    db.commit()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--snapshot", metavar="FILE", help="save the front page as static HTML and exit")
    args = ap.parse_args()

    tmp = tempfile.TemporaryDirectory(prefix="job-radar-demo-")
    os.environ["DASHBOARD_HOST"], os.environ["DASHBOARD_PORT"] = "127.0.0.1", str(args.port)
    import radar
    radar.DB_PATH, radar.CV_DIR = Path(tmp.name) / "demo.db", Path(tmp.name) / "cv"
    radar.TAILORED_DIR = radar.CV_DIR / "tailored"
    radar.CV_TEX = radar.CV_DIR / "cv-content.tex"
    db = radar.open_db()
    seed(radar, db)
    db.close()
    try:
        import dashboard  # noqa: E402  (imported late: it reads the paths above)
        import uvicorn
    except ImportError as exc:
        sys.exit(f"{exc}. Install the dashboard dependencies first: pip install -r requirements.txt")

    server = uvicorn.Server(uvicorn.Config(dashboard.app, host="127.0.0.1", port=args.port, log_level="warning", server_header=False))
    if not args.snapshot:
        print(f"Demo with made-up data: http://127.0.0.1:{args.port}/   (Ctrl+C to stop; nothing is kept)")
        server.run()
        return 0
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        sys.exit("the demo server did not start")
    html = urllib.request.urlopen(f"http://127.0.0.1:{args.port}/").read().decode()
    Path(args.snapshot).write_text(html)
    server.should_exit = True
    thread.join(timeout=5)
    print(f"wrote {args.snapshot} ({len(html):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
