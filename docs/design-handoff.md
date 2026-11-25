# Job Radar redesign: handoff for implementation

Read this first, then the files in `mockups/`. Goal: implement the redesign in the existing code:
`dashboard.py` (`CSS`, `render_card`, `render_tailor`) and `radar.py` (`build_email`).

## Product (unchanged brief)
A private job-search assistant for one CS student hunting 20 h/week Werkstudent jobs near Darmstadt. Every weekday
morning it collects postings (BA, Stellenwerk, Merck, Arbeitnow, SmartRecruiters), an LLM scores each 0-100 against
the CV, and it sends an HTML email digest plus a private FastAPI dashboard (over Tailscale) for triage, fit analysis,
and generating a tailored resume (LaTeX/PDF). One user, mostly on a phone. Tone: calm, utilitarian, no gamification.

## Hard constraints (must keep)
- Server-rendered HTML via Python f-strings, no build step, no framework, no npm, no external fonts/images/scripts.
- CSP: `default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-...'; connect-src 'self'`.
  So: one nonce'd `<script>`, `addEventListener` only (no `onclick=`), icons as inline SVG or Unicode.
- Mobile first: 360 px wide, no horizontal scroll, tap targets >= 40 px, 16 px gutters.
- Light/dark with `prefers-color-scheme` CSS variables; respect `prefers-reduced-motion`.
- Email: tables + inline styles only, max 640 px, no images, must survive dark-mode inversion. Plain-text alt exists.
- Long German titles and `&` in company names: `overflow-wrap:anywhere` (web), `word-break:break-word` (email); escape HTML.
- AA contrast, `aria-pressed` on status buttons, decorative spinner `aria-hidden`, visible focus.

## The mockups (`mockups/*.dc.html`)
These are design-canvas files. Ignore the `<x-dc>`, `<helmet>`, `support.js` and `data-dc-script` wrappers: the
markup inside `<x-dc>` is plain HTML with inline styles showing exact sizes and colours. Translate it into classes
using the CSS in `Spec.dc.html` (also below) rather than copying inline styles (except for the email, which stays inline).

| File | Shows |
|---|---|
| `Main.dc.html` | Dashboard, phone, light: header counts, skill-gap strip, filter chips, cards in several states, collapsed "skipped" row |
| `MobileDark.dc.html` | Same in dark tokens, incl. failed tailor state |
| `Desktop.dc.html` | 1280 px: 760 px card column + sidebar (skill gaps, keyboard help, sources); keyboard-focused card ring; unscored card |
| `CardStates.dc.html` | Tailor slot in all states: idle, pending, failed, done+warnings, done without PDF, fit analysis open |
| `Extras.dc.html` | Empty states (first run, no filter match) + new `/gaps` weekly skill-gap report |
| `Email.dc.html` | Digest variants: normal, nothing new (heartbeat), source failed (yellow banner) |
| `Spec.dc.html` | Tokens (light/dark), type/spacing, component list, JS behaviour, email rules, CSS additions |

## Design decisions
1. **Card hierarchy:** score badge (48 px, tinted, number in band colour) → title (17/600) → company (14 muted) →
   meta line (location · **distance bold** · source · date) → reason (15) → "CV <track> · missing" + orange gap chips.
2. **One button row per card:** idle "Tailor resume" is a link on the "Posting details" row; only the 4-way status
   segmented control is a button row. Desktop puts tailor link and status in a single footer row.
3. **Status labels:** phone shows "Skip" with `aria-label="Not relevant"`; desktop shows "Not relevant".
   Active "New" = fg fill; other active states = accent fill, white text.
4. **Tailor slot** (one container, swapped in place):
   idle link → pending box (spinner + "about a minute" + indeterminate bar, `role=status`) →
   done box (primary "Resume (PDF)", secondary "LaTeX source", amber `.warn` "Check N claims before sending" list,
   `<details>` "Fit analysis & what to learn", "Regenerate") / done-no-PDF (.tex becomes primary + amber note) /
   failed (red box, mono error text, "Retry"). Red is used ONLY for failed.
5. **Fit analysis markdown:** section headings as small uppercase muted labels; Verdict line gets the band tint;
   Gaps rendered as chips; Application hook as a blockquote.
6. **Triage speed:** filters run client-side (cards carry `data-score/data-source/data-status`, toggle `hidden`,
   sync URL via `history.replaceState`; server filters stay as no-JS fallback). Skip collapses a card to a dashed
   one-line row with Undo. Keyboard: j/k move, 1-4 status, o open, t tailor, u undo (ignored inside inputs).
   Status change is optimistic with revert on error.
7. **Tailor polling:** every 4 s fetch the card's tailor fragment and replace it in place (no full reload), stop
   on done/failed or after 5 min. Needs a small endpoint returning `render_tailor(...)` HTML.
8. **Skill gaps:** strip above the filters ("Top gap this week: Kubernetes (in 4 jobs)…") links to a new `/gaps`
   page: ranked gaps (count × avg score, jobs scoring 50+ only, last 7 days), "start with" tip, also-mentioned
   chips, closed-since-last-week.
9. **Empty states:** first run shows setup facts (CV loaded, sources, radius, next run); no-match explains why and
   offers one-tap fixes with resulting counts; "all triaged" says "Inbox zero. N jobs triaged today."
10. **Email:** score circle = solid band colour + white number (inversion-proof); max 10 cards, only 50+, then
    "+N more on the dashboard"; bulletproof button (padded `<a>` in coloured `td`); source-failed banner is the
    first full-width row naming source, error and "retry tomorrow"; subjects: "Job Radar: 6 new, top 82",
    "Job Radar: nothing new today", "Job Radar: 2 new, top 71 (1 source failed)".

## CSS additions (from Spec)
```css
:root{--hi-fg:#116329;--hi-tint:#dafbe1;--mid-fg:#7d4e00;--mid-tint:#fff8c5;--lo-fg:#59636e;--lo-tint:#eef1f4;
  --gap-fg:#953800;--gap-tint:#fff1e5;--warn-tint:#fff8c5;--warn-line:#d4a72c;--danger:#d1242f;--danger-tint:#ffebe9;
  --accent-solid:#0969da;--accent-tint:#ddf4ff;--focus:#0969da}
@media (prefers-color-scheme:dark){:root{--hi-fg:#3fb950;--hi-tint:#12261e;--mid-fg:#d29922;--mid-tint:#2b2111;
  --lo-fg:#9198a1;--lo-tint:#21262d;--gap-fg:#ffa657;--gap-tint:#2a1708;--warn-tint:#272115;--warn-line:#9e6a03;
  --danger:#f85149;--danger-tint:#3a1d1d;--accent-solid:#1f6feb;--accent-tint:#0f2a4a;--focus:#4493f8}}
.card{display:flex;flex-direction:column;gap:10px;padding:16px;border:1px solid var(--line);border-radius:10px;background:var(--card)}
.card h2,.card .co{overflow-wrap:anywhere}
.score{flex:none;width:48px;height:48px;border-radius:10px;display:grid;place-items:center;font:700 20px/1 inherit}
.score.hi{background:var(--hi-tint);color:var(--hi-fg)} .score.mid{background:var(--mid-tint);color:var(--mid-fg)}
.score.lo{background:var(--lo-tint);color:var(--lo-fg)} .score.none{border:1px dashed var(--line);color:var(--muted)}
.chip.gap{background:var(--gap-tint);color:var(--gap-fg);border-radius:999px;padding:2px 8px;font-size:13px}
.seg{display:grid;grid-template-columns:repeat(4,1fr);border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{min-height:40px;border:0;background:var(--card);color:var(--fg);font:inherit;font-size:13px}
.seg button+button{border-left:1px solid var(--line)}
.seg button[aria-pressed=true]{background:var(--accent-solid);color:#fff;font-weight:600}
.seg button[data-s=new][aria-pressed=true]{background:var(--fg);color:var(--card)}
.warn{background:var(--warn-tint);border:1px solid var(--warn-line);border-radius:8px;padding:10px 12px}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.card.kb{box-shadow:0 0 0 3px color-mix(in srgb,var(--focus) 35%,transparent)}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
```
Existing tokens (`--bg --card --fg --muted --line --accent --high --mid --low`) stay as they are.

## Suggested order of work
1. CSS tokens + `render_card` hierarchy + segmented status. 2. `render_tailor` states + fragment endpoint + polling.
3. Client-side filters, Skip collapse/Undo, keyboard. 4. `build_email` variants. 5. Empty states. 6. `/gaps` page.
Check at 360 px and in dark mode after each step.

Design canvas: kept privately by the owner.
