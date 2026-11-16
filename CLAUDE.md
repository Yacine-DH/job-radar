# Job Radar

Personal tool for one user (CS student, TU Darmstadt, max 20 h/week): finds Werkstudent jobs within 30 km of
Darmstadt, ranks them with `claude -p` against the user's CV, emails a digest and serves a private dashboard.
Runs on the home server `my-server` (Ubuntu, 24/7). Private repo: `git@github.com:Yacine-DH/job-radar.git`.

## How the user wants to work
- **Explain each step before running commands.** Ask before outward-facing actions (git push, sending mail,
  publishing, installing software). Never claim something works without having run it; say what was not tested.
- The user moves between machines/chats: keep this file current when architecture or gotchas change.

## Layout
- `radar.py` - fetchers (BA Jobsuche v6, Stellenwerk, Merck/Phenom JSON, Arbeitnow, SmartRecruiters via
  `sources.json`), CS filter, SQLite store + dedupe, ranking, HTML email. Daily entry point.
- `cv.py` - `build` (distill CV -> `cv/profile.md`), `tailor`, `resume` (tailored one-page LaTeX), `skills`.
- `worker.py` - polls the `tailor_jobs` queue and runs `cv.run_request` (the dashboard cannot run `claude`).
- `dashboard.py` - FastAPI, f-string HTML, one nonce'd inline script, no build step. Implements the redesign in
  `docs/design-handoff.md`: client-side filters (chips, min score, source, status, search, sort; URL params are
  the no-JS fallback), optimistic status buttons with Skip/Undo, keyboard (j/k/1-4/o/t/u), tailor slot swapped
  in place via `/api/tailor/fragment` polling, `/gaps` weekly skill-gap report, desktop sidebar, empty states.
  Routes: `/`, `/gaps`, `/api/status`, `/api/tailor`, `/api/tailor/status`, `/api/tailor/fragment`, `/files/<token>.pdf|tex`.
- `job-radar.timer|service`, `job-radar-dashboard.service`, `job-radar-worker.service` - systemd **user** units,
  symlinked with `systemctl --user link`. After editing: `systemctl --user daemon-reload` and restart.
- `DESIGN.md` - brief for UI/design work.

## Run / operate
- Dry run (fetch + rank, print email, send nothing): `python3 -I radar.py --dry-run`
- Re-rank jobs still `new` (after CV/profile change): `python3 -I radar.py --rescore`
- CV tools: `python3 -I cv.py build|tailor JOB_ID|resume JOB_ID|skills`
- Timer: Mon-Fri 08:00 Europe/Berlin (`job-radar.timer`). The daily email is sent even when nothing is new
  (heartbeat) and names failed sources. Logs: `journalctl --user -u job-radar.service`.
- Dashboard: `http://100.64.0.1:8090/` (Tailscale only). Worker: `journalctl --user -u job-radar-worker`.

## Gotchas
- Always run scripts with `python3 -I`; they insert their own directory into `sys.path`. radar/cv/worker are
  **stdlib only** (system python). Only the dashboard needs `.venv` (fastapi, uvicorn). The venv was built
  `--without-pip` and bootstrapped with get-pip (no python3-venv/pip on the server, no sudo).
- BA API: use **v6** (`/pc/v6/jobs`), header `X-API-Key: jobboerse-jobsuche`; v4 is gone. BA gives no full
  description, so BA jobs are ranked and tailored on title/metadata only.
- Stellenwerk: only the front page (~10 jobs) works; `?page=2` is a 500.
- Politeness: 3 s between requests per host, honest User-Agent (do not spoof), retries honour `Retry-After`.
- All `claude -p` calls are tool-less (`--tools ""`); job text is untrusted data (prompt injection).
- Gmail digest needs an App Password in `.env` (`GMAIL_USER`, `GMAIL_APP_PASSWORD`, `MAIL_TO`, `DASHBOARD_URL`).
- `Linger=no`: user services only run while the user is logged in (the server stays logged in).

## Security model of the dashboard
No login; the only access control is Tailscale. `dashboard.py` refuses to bind anything but a Tailscale/loopback
address, checks `Host`/`Origin`, requires JSON for POSTs, sends a nonce CSP, escapes all output and only serves
files matching `^[0-9a-f]{16}\.(pdf|tex)$`. Another Tailscale account has a device visible on the tailnet:
consider a password before exposing anything more sensitive.

## Never commit (all git-ignored)
`.env*`, `jobs.db`, `cv/` (master CV `cv-content.tex`, derived `profile.md`, generated resumes), `profile.md`,
`.orca/`, `.venv/`. The CV contains personal contact data, so the repo must stay private. A stray `.env.`
file may exist: it is ignored, check it for leftover credentials.

## Privacy rule: the owner's email address
Never put the owner's email address (or phone, name) in request headers, URLs or payloads sent to external
sites, and never hard-code it in a script. The User-Agent is generic (`user_agent()` in `radar.py`); a contact
is appended only if the owner sets `JOB_RADAR_CONTACT` in `.env` themselves. If a site needs a real contact,
ask the owner which one to use. (An earlier version hard-coded the address in the User-Agent; that is fixed.)

## Data model (`jobs.db`)
`jobs` (id `source:ref`, score, reason, cv_track swe|devops|ml, gaps JSON, status new|interested|applied|
not_relevant, notified, distance_km...), `tailoring` (markdown analysis per job), `tailor_jobs` (queue:
queued|running|done|failed, pdf flag, warnings JSON), `runs` (one row per real daily run: per-source counts and
failed sources, shown in the dashboard header/sidebar; dry runs do not write it). Ranking prompts include the
user's past status decisions. Email shows at most `TOP_N` cards scoring >= `MIN_SCORE` (50), rest as "+N more".

## Known limitations / open items
- PDFs: `tectonic` 0.17.0 (XeTeX) is installed at `~/.local/bin/tectonic` (official GitHub release, SHA-256 matched
  the published digest). The master CV uses `fontspec`, so `pdflatex` cannot compile it. First compile fetches TeX
  support files (~2 min), later ones are cached. If tectonic is missing, resumes fall back to `.tex` only.
- Fact check of generated resumes is a heuristic (flags numbers/tool names absent from the master CV).
- The server has no Node or browser, so the dashboard's client-side JS has only had static checks (bracket balance,
  DOM hooks present, server HTML verified with curl). Verify interactions on a real phone/laptop after changes.
- Auto-mode blocks unrequested downloads and `pip install` here (the tectonic download only went through after the
  owner explicitly asked for it); ask the owner first instead of retrying.
- Other sessions/tools edit this repo too (`q`/`sort` search, `status_at`, `--rescore`, `feedback_block` were not
  written by the assistant): read the current file before rewriting it and preserve features.
- Next ideas: weekly skill-gap section in the email, dashboard password, more employer career-page sources.

Commits end with: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` (plus the session trailer if given).
