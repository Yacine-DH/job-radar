# Job Radar: design brief

Context file for a design pass (Claude Design or a human). Everything here is non-personal; sample data is fake.

## Product in one paragraph
A private job-search assistant for one person: a computer-science student looking for 20 h/week Werkstudent jobs
near Darmstadt. Every weekday morning it collects postings from several sources, an LLM scores each 0-100 against
the student's CV, and the student gets (1) an HTML email digest and (2) a private web dashboard to triage jobs,
see why a job fits, what skills are missing, and generate a tailored resume for a posting.

## Users and contexts
- One user, technical, German/English bilingual, fast-scanning: checks the email on the phone at breakfast, opens
  the dashboard on the phone (mostly) or laptop (sometimes) to triage 2-10 new jobs in a few minutes.
- Reached over a private VPN (Tailscale). No marketing, no onboarding, no accounts. Tone: calm, utilitarian,
  trustworthy; no gamification.

## Screens and surfaces
1. **Dashboard list** (single page, `/`): header with counts, filter bar (min score slider 0-100, source select,
   status select), then job cards, newest first.
2. **Job card**: score badge (colour by band: >=70 high, 50-69 mid, <50 low), title (link out), company, location
   + distance in km + source + date, one-line LLM reason, "CV track" + "missing skills" line, collapsible raw
   details, tailor area, four status buttons (New / Interested / Applied / Not relevant, one active).
3. **Tailor area** (per card) has five states: *idle* (button "Tailor resume"), *pending* (spinner, ~1 min, polls
   every 4 s, then reloads), *done* (primary "Resume (PDF)" + "LaTeX source" buttons, warnings list, collapsible
   "Fit analysis & what to learn" containing markdown: Verdict / What matches / Gaps / Learn first / CV tailoring /
   Application hook, "Regenerate"), *failed* (error text + "Retry"), *done without PDF* (only .tex + a warning).
4. **Email digest** (HTML email, max 640 px): header, up to 10 job cards (score circle, title link, company,
   location/distance/source, reason, CV/missing-skills line), footer with dashboard link and source credits.
   Variants: "N new, top score X"; "nothing new today" heartbeat with a one-line tracker summary
   ("24 jobs tracked / 2 interested / 1 applied"); a yellow banner when a source failed.
5. Not built yet, candidates for design: weekly skill-gap report, a job detail view, an empty/first-run state.

## Data each card can show
`score` 0-100 (may be empty), `title`, `company`, `location`, `distance_km` (may be empty), `source`
(BA, Stellenwerk, Merck, Arbeitnow, SmartRecruiters), `first_seen` date, `reason` (<=15 words), `cv_track`
(swe | devops | ml), `gaps` (0-3 short skill names), `status`, `description` (long, may be empty), tailor state,
warnings (e.g. "Not found in your master CV, please verify: Kubernetes, Terraform").

Fake sample:
```json
{"score": 78, "title": "Werkstudent (m/w/d) IT-Support / IT-Systemadministration", "company": "Example Systems GmbH",
 "location": "Darmstadt", "distance_km": 2.1, "source": "BA", "first_seen": "2026-11-12",
 "reason": "IT support matches current role; hours unclear.", "cv_track": "devops",
 "gaps": ["Active Directory", "ITIL"], "status": "new"}
```

## Hard technical constraints
- Server-rendered HTML built with Python f-strings (FastAPI), **no build step, no framework, no npm**. Deliver
  plain HTML + CSS (+ small vanilla JS if needed).
- Strict CSP: `default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-...'; connect-src 'self'`.
  So: no external fonts, images, icon CDNs or scripts; no inline event handlers (`onclick=`); inline `<style>`
  is fine; icons must be inline SVG or Unicode. System font stack only.
- Mobile first: must work at 360 px wide, no horizontal scroll, tap targets >= 40 px, 16 px gutters.
- Light and dark via `prefers-color-scheme` (already done with CSS variables); respect `prefers-reduced-motion`.
- Email: table layout, **inline styles only** (Gmail strips `<style>`), max 640 px, must degrade without images,
  should survive dark-mode inversion in mail clients. Plain-text alternative exists.
- Long text is common: titles 80+ chars, German compound words, company names with `&`; use `overflow-wrap`.
- Accessibility: contrast AA, status buttons use `aria-pressed`, spinner is decorative, focus states visible.

## Current visual tokens (CSS variables)
Light: bg `#f6f8fa`, card `#fff`, fg `#1f2328`, muted `#59636e`, line `#d1d9e0`, accent `#0969da`,
score high `#1a7f37`, mid `#9a6700`, low `#6e7781`. Dark: bg `#0d1117`, card `#151b23`, fg `#e6edf3`,
muted `#9198a1`, line `#2f3742`, accent `#4493f8`, high `#3fb950`, mid `#d29922`, low `#8b949e`.
Radius 8-10 px; single column max 760 px; system sans-serif 16 px. Look: GitHub-like, neutral.

## Known UX pain points to improve
- Cards are dense; the eye has no clear hierarchy beyond the score badge. What matters most when scanning:
  score, title, company, distance, reason, then status.
- The tailor area competes with the status buttons; two rows of buttons feel heavy on a phone.
- Filtering reloads the page; status changes are instant. Consider whether triage needs a faster flow
  (e.g. swipe-like or keyboard shortcuts) without adding dependencies.
- "Missing skills" is useful but easy to miss; the skill-gap story (what to learn next) has no home yet.
- Done state with warnings (unverified facts in a generated resume) must feel important but not alarming.
- Empty states (no jobs match filters, no jobs yet) are bare.

## What to deliver
Mockups or HTML/CSS for: dashboard (mobile + desktop), a card in all tailor states, the email digest (normal,
nothing-new, source-failed), and a short component/token spec. Keep within the constraints above so it can be
dropped into `dashboard.py` (`CSS`, `render_card`, `render_tailor`) and `radar.py` (`build_email`).
