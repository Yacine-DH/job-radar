# Job Radar

A private job-search assistant. Every weekday morning it collects Werkstudent postings near Darmstadt, scores each
one against your CV with `claude -p`, emails a digest, and serves a small dashboard where you triage jobs, see what
skills you are missing, and generate a tailored one-page resume (LaTeX and PDF) for a posting.

```
sources ──> filter + dedupe ──> SQLite ──> rank with claude -p ──> email digest
                                   │
                                   └──> dashboard (Tailscale only) ──> tailor button ──> worker ──> .tex / .pdf
```

## Sources

Bundesagentur für Arbeit Jobsuche API (v6), Stellenwerk Darmstadt, Merck careers, Arbeitnow, and any company on
SmartRecruiters listed in `sources.json`. Requests are rate-limited (3 s per host), retried politely, and sent with a
generic User-Agent.

## Layout

| Path | What it is |
|---|---|
| `radar.py` | Fetchers, CS filter, SQLite store, ranking, email. The daily entry point. |
| `cv.py` | Distil the master CV, analyse and tailor it to one job, build the PDF, skill-gap report. |
| `worker.py` | Background worker for the dashboard's "Tailor resume" queue. |
| `dashboard.py` | FastAPI dashboard (server-rendered HTML, one inline script, no build step). |
| `systemd/` | User units (daily timer, dashboard, worker) and `install.sh`. |
| `tests/` | Offline unit tests (`unittest`, fake data only). |
| `docs/` | Design brief and the design handoff the dashboard was built from. |
| `sources.json` | Extra SmartRecruiters companies to watch. |
| `CLAUDE.md` | Context for Claude Code sessions (architecture, gotchas, rules). |

## Setup

```bash
cp .env.example .env && chmod 600 .env        # Gmail app password, recipient, dashboard URL
mkdir -p cv && cp /path/to/cv-content.tex cv/  # your master CV (git-ignored)
python3 -I cv.py build                         # distil it into cv/profile.md for ranking

# dashboard dependencies (radar, cv and worker need none)
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
#   no python3-venv? python3 -m venv --without-pip .venv, then bootstrap pip with get-pip.py

./systemd/install.sh                           # link and enable the timer, dashboard and worker
```

PDFs need a XeTeX engine; the master CV uses `fontspec`, so `pdflatex` will not work. Install
[tectonic](https://tectonic-typesetting.github.io/) and put it on `PATH` or at `~/.local/bin/tectonic`. Without it
the tailor button still produces the `.tex`.

The unit files hard-code `/home/user/job-radar` and the server's Tailscale IP: edit them before installing elsewhere.

## Everyday commands

```bash
python3 -I radar.py --dry-run          # fetch, rank, print the email; send and record nothing
python3 -I radar.py --rescore          # re-rank jobs still marked "new" (after a CV or profile change)
python3 -I cv.py tailor JOB_ID         # fit analysis for one job
python3 -I cv.py resume JOB_ID         # analysis plus a tailored resume
python3 -I cv.py recompile             # build missing PDFs from .tex files already on disk
python3 -I cv.py skills                # most demanded skills your CV lacks
journalctl --user -u job-radar.service # logs of the daily run

.venv/bin/python -m unittest discover -s tests -t .    # tests
```

## Configuration (`.env`)

`GMAIL_USER`, `GMAIL_APP_PASSWORD` (a Google app password, not your account password), `MAIL_TO`, `DASHBOARD_URL`.
Optional: `JOB_RADAR_CONTACT`, a contact appended to the User-Agent. It is empty by default, so no personal data is
sent to job sites unless you opt in.

## Security notes

- The dashboard has no login. It binds only to a Tailscale (or loopback) address and refuses to start otherwise, so
  anyone on your tailnet can open it. It checks `Host` and `Origin`, requires JSON for writes, sends a nonce CSP, and
  serves only files named `<16 hex chars>.pdf|tex`.
- Job text is untrusted: every `claude -p` call runs with tools disabled.
- Never commit `.env*`, `jobs.db`, `cv/`, `profile.md` or `.orca/` (all git-ignored). Keep the repo private.
