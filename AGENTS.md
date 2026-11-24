# AGENTS.md: shared context for every coding agent on this repo

Read this first, then the issue you were assigned. Humans: `README.md`. Claude Code also reads `CLAUDE.md`.
The full rules are in issue #1; this file is the short, stable version plus the hand-off protocol.

## The project in one paragraph
A private job-search assistant for one person: a daily job collects postings, ranks them with an LLM against the
owner's CV, emails a digest, and serves a phone-first dashboard (Tailscale only) where jobs are triaged and tailored
resumes (LaTeX/PDF) are generated. See `README.md` for layout and commands.

## Non-negotiable rules
- **Privacy:** never commit or print personal data (names, email, phone, CV text, application notes). Never commit
  `.env*`, `jobs.db`, `cv/`, `profile.md`, `.orca/`, `.venv/`. Fixtures and tests use obviously fake data.
- **Dashboard:** server-rendered f-strings, no build step, no framework, no external assets. CSP is
  `default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-...'; connect-src 'self'`: no inline event
  handlers. Escape every dynamic value; only `http(s)` links; mobile first (360 px), light and dark, AA contrast.
- **Backend** (`radar.py`, `cv.py`, `worker.py`): standard library only. New dependencies need prior agreement.
- **Resumes are faithful:** generated resumes may only use facts from the master CV. Evidence from other sources
  (GitHub, LinkedIn) is a *suggestion* until the owner approves it and it is added to the master CV.
- **Safety:** never use `--dangerously-skip-permissions` or equivalents, never push, never touch the live server or
  its services, never read outside your workspace. The owner (or the orchestrating agent) pushes and merges.
- **Tests:** `.venv/bin/python -m unittest discover -s tests -t .` must pass; new logic needs tests; no network in tests.

## Who owns what (avoid editing each other's files)
| Area | Owner |
|---|---|
| `radar.py` (ranking, email), `cv.py`, `worker.py`, collector, store/sync, scheduler | Claude |
| `skills_catalog.py`, the `/skills` page and its API | Antigravity |
| `dashboard.py` outside `/skills` | coordinate through an issue first |
Anything not listed: ask in the issue before editing.

## Hand-off protocol (how agents talk to each other)
1. **Work item:** a GitHub issue labelled `agent:<name>`. Branch `agent/<name>/<topic>`, one PR per issue,
   `Closes #<n>`.
2. **Brief (orchestrator -> agent):** `.agent/BRIEF.md` in your workspace (or the prompt) holds the issue text, the
   shared contracts and the list of files you may touch. If the brief and the code disagree, say so; do not guess.
3. **Report (agent -> orchestrator):** before you finish, write `.agent/REPORT.md` with these headings:
   `Summary`, `Files changed`, `Decisions` (and why), `Tests run` (command + result), `Not verified`,
   `Questions`. If you are blocked by missing information, stop and put the question under `Questions`.
   `.agent/` is git-ignored: it is a mailbox, not part of the code.
4. **Durable messages:** anything the owner should see goes in the issue or PR thread, signed `[Claude]` or
   `[Antigravity]` (both post through the owner's GitHub login, so the signature is the only attribution).
5. **Reviews:** every PR is reviewed by the *other* agent. Format: `Review by <agent>: APPROVE | REQUEST CHANGES`,
   then findings as `file:line, severity (blocker/major/minor), problem, suggested fix`. Check the rules above first.
6. **Disagreements:** the owner decides; record the decision in the issue.

## Commit and PR hygiene
Small commits, imperative messages, a co-author trailer for the tool that wrote them. PR description: what, why,
how tested, screenshots for UI (light, dark, 360 px). UI changes keep the `needs-browser-test` label until the owner
has checked them on a real device.
