# Job Radar

A self-hosted job-search assistant for a working-student (Werkstudent) search. Every weekday morning it collects
postings near Darmstadt, scores each one against your CV with `claude -p`, emails a digest, and serves a private
dashboard where you triage jobs, see which skills you are missing, and generate a tailored one-page resume
(LaTeX and PDF) for a posting.

```
sources ──> filter + dedupe ──> SQLite ──> rank with claude -p ──> email digest
                                   │
                                   └──> dashboard (Tailscale only) ──> tailor button ──> worker ──> .tex / .pdf
```

## Try it in 30 seconds (made-up data)

You do not need a CV, a Gmail account or a server. The demo seeds a throwaway database with invented jobs and runs the
real dashboard on your own machine:

```bash
git clone https://github.com/Yacine-DH/job-radar && cd job-radar
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python demo.py          # open http://127.0.0.1:8091/
```

Everything lives in a temporary directory that is deleted when you stop it. `--snapshot page.html` saves the front page
as a single static file instead. In the demo, open the first card ("Werkstudent DevOps Engineering") to see:

- **Fit analysis:** the verdict, the **skill gaps first** (amber cards tagged *critical* or *nice to have*), what matches
  as requirement → evidence rows, and numbered "learn first" steps with hour estimates.
- **Resume history:** three archived versions, one pinned with a label. Every version can be downloaded, edited or deleted.
- **Regenerate…:** describe what to change, pick the version to edit (or start from the master CV) and optionally label it.
- Keyboard shortcuts: `j`/`k` move, `1`-`4` set the status, `o` opens the posting, `t` tailors, `u` undoes.

The demo cannot generate resumes (that needs your own CV and `claude`); it shows the dashboard around them.

## What it does

- **Collects** jobs from the Bundesagentur für Arbeit Jobsuche API (v6), Stellenwerk Darmstadt, Merck careers,
  Arbeitnow and any company on SmartRecruiters listed in `sources.json`. Requests are rate-limited (3 s per host),
  retried politely and sent with a generic User-Agent.
- **Ranks** new jobs with `claude -p` against a profile distilled from your CV. Your earlier triage decisions are part of
  the prompt, so the ranking learns what you skip.
- **Emails** a digest every weekday at 08:00 (even when nothing is new, so you know it ran; failed sources are named).
- **Tailors** a one-page resume per posting from your master CV. The model only picks and orders what the CV already
  says: every link must exist in the master, numbers and tool names are checked against it, language levels are
  verified, and anything unsupported is flagged for you before you send it.
- **Keeps history:** every generated resume is archived as a numbered version. Regenerate from scratch or edit a chosen
  version with instructions; label a version to pin it; delete any version you do not want.
- **Shows the gaps:** a weekly report of the skills your CV lacks that postings ask for most.
- **Never loses a decision:** the counters at the top (New, Interested, Applied) and the Skipped/Archived chips open those
  jobs with their resumes; every status change shows an Undo; skipped jobs move to Archived after 7 days (kept in the
  database so they are never fetched again as new).

Private projects carry a small padlock instead of the arrow that marks a clickable project name in the resume.

## Layout

| Path | What it is |
|---|---|
| `radar.py` | Fetchers, CS filter, SQLite store, ranking, email. The daily entry point. |
| `cv.py` | Distil the master CV, analyse and tailor it to one job, build the PDF, versions, skill-gap report. |
| `worker.py` | Background worker for the dashboard's "Tailor resume" queue. |
| `dashboard.py` | FastAPI dashboard (server-rendered HTML, one inline script, no build step). |
| `demo.py` | The dashboard on made-up data (see above). |
| `systemd/` | User units (daily timer, dashboard, worker) and `install.sh`. |
| `tests/` | Offline unit tests (`unittest`, fake data only). |
| `docs/` | Design brief and the design handoff the dashboard was built from. |
| `sources.json` | Extra SmartRecruiters companies to watch. |
| `CLAUDE.md`, `AGENTS.md` | Context and working rules for Claude Code and other coding agents. |

## Setup (your own instance)

```bash
cp .env.example .env && chmod 600 .env        # Gmail app password, recipient, dashboard URL
mkdir -p cv && cp /path/to/cv-content.tex cv/  # your master CV (git-ignored)
python3 -I cv.py build                         # distil it into cv/profile.md for ranking

# dashboard dependencies (radar, cv and worker need none)
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
#   no python3-venv? python3 -m venv --without-pip .venv, then bootstrap pip with get-pip.py

./systemd/install.sh                           # link and enable the timer, dashboard and worker
```

The master CV is a LaTeX file with the macros `\entry`, `\sub`, `\gh`, `\T{english}{deutsch}`, `\plink{url}{text}`
(clickable name with an arrow) and `\privlink{text}` (name with a padlock, for private repos).

PDFs need a XeTeX engine; the master CV uses `fontspec`, so `pdflatex` will not work. Install
[tectonic](https://tectonic-typesetting.github.io/) and put it on `PATH` or at `~/.local/bin/tectonic`. Without it
the tailor button still produces the `.tex`.

The units expect the checkout at `~/job-radar`. Put your Tailscale address in `.env` as `DASHBOARD_HOST` (and the
names you open it by in `DASHBOARD_ALLOWED_HOSTS`); the dashboard refuses to start on any other kind of address.

## Everyday commands

```bash
python3 -I radar.py --dry-run          # fetch, rank, print the email; send and record nothing
python3 -I radar.py --rescore          # re-rank jobs still marked "new" (after a CV or profile change)
python3 -I cv.py tailor JOB_ID         # fit analysis for one job
python3 -I cv.py resume JOB_ID         # analysis plus a tailored resume (older versions stay in History)
python3 -I cv.py resume JOB_ID --notes-file notes.txt              # same, with your instructions for this resume
python3 -I cv.py resume JOB_ID --base 6 --notes "swap project A for B" --label final   # edit version 6, pin the result
python3 -I cv.py versions JOB_ID       # list archived versions
python3 -I cv.py delete-version JOB_ID 3          # delete one version (--all-older keeps the newest and pinned ones)
python3 -I cv.py recompile             # build missing PDFs from .tex files already on disk
python3 -I cv.py skills                # most demanded skills your CV lacks
journalctl --user -u job-radar.service # logs of the daily run

.venv/bin/python -m unittest discover -s tests -t .    # tests
```

## Configuration (`.env`)

`GMAIL_USER`, `GMAIL_APP_PASSWORD` (a Google app password, not your account password), `MAIL_TO`, `DASHBOARD_URL`.
Optional: `JOB_RADAR_CONTACT`, a contact appended to the User-Agent. It is empty by default, so no personal data is
sent to job sites unless you opt in.

## License

[MIT](LICENSE).

## Security and privacy

- The dashboard has no login. It binds only to a Tailscale (or loopback) address and refuses to start otherwise, so
  anyone on your tailnet can open it. It checks `Host` and `Origin`, requires JSON for writes, sends a nonce CSP, and
  serves only files named `<16 hex chars>[.vN].pdf|tex`.
- Job text is untrusted: every `claude -p` call runs with tools disabled.
- Your CV, database, `.env` and generated resumes are git-ignored (`cv/`, `jobs.db`, `.env*`, `profile.md`, `.orca/`).
  The tests and the demo use invented data only.
