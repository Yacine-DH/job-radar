#!/usr/bin/env python3
"""Job radar dashboard: browse jobs.db, filter, triage, tailor resumes, see skill gaps.

Binds only to a Tailscale (or loopback) address; refuses anything else.
Server-rendered f-string HTML, one nonce'd inline script, no build step, no external assets.
"""
import html
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))  # run with -I, so add our own dir explicitly

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

import cv
import radar

PORT = int(os.environ.get("DASHBOARD_PORT", "8090"))
HOST = os.environ.get("DASHBOARD_HOST", "")
EXTRA_HOSTS = [h.strip().lower() for h in os.environ.get("DASHBOARD_ALLOWED_HOSTS", "").split(",") if h.strip()]

BERLIN = ZoneInfo("Europe/Berlin")
STATUSES = {"new": "New", "interested": "Interested", "applied": "Applied", "not_relevant": "Not relevant"}
STATUS_ORDER = tuple(STATUSES)
OPEN_STATUSES = ("new", "interested")  # "To triage": still needs a decision or an application
VIEWS = {"open": "To triage", "active": "Active (hide not relevant)", "all": "All", **STATUSES}
SORTS = {"score": "Best fit first", "new": "Newest first"}
TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def check_bind_host(host: str) -> None:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        sys.exit(f"DASHBOARD_HOST must be an IP address, got {host!r}")
    if not (ip.is_loopback or ip in TAILSCALE_V4 or ip in TAILSCALE_V6):
        sys.exit(f"refusing to bind to {host}: only Tailscale or loopback addresses are allowed")


def allowed_hosts() -> set[str]:
    hosts = {h for h in EXTRA_HOSTS}
    if HOST:
        hosts.add(HOST if ":" not in HOST else f"[{HOST}]")
    return {f"{h}:{PORT}" for h in hosts}


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
radar.open_db().close()  # run migrations once at startup


def db() -> sqlite3.Connection:
    con = sqlite3.connect(radar.DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


@app.middleware("http")
async def guard(request: Request, call_next):
    # DNS-rebinding defence: only answer for the names/IPs we are meant to be reached by.
    if request.headers.get("host", "").lower() not in allowed_hosts():
        return JSONResponse({"error": "bad host"}, status_code=421)
    nonce = secrets.token_urlsafe(16)
    request.state.nonce = nonce
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = (
        f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store"
    return resp


# --- API -------------------------------------------------------------------------------------------

class StatusUpdate(BaseModel):
    id: str
    status: str


@app.post("/api/status")
async def set_status(body: StatusUpdate, request: Request):
    # JSON body => cross-site pages can't send this without a CORS preflight, which we never approve.
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc.lower() not in allowed_hosts():
        return JSONResponse({"error": "bad origin"}, status_code=403)
    if body.status not in STATUSES:
        return JSONResponse({"error": "bad status"}, status_code=422)
    with db() as con:
        cur = con.execute("UPDATE jobs SET status = ?, status_at = CURRENT_TIMESTAMP WHERE id = ?", (body.status, body.id))
    if cur.rowcount == 0:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return {"ok": True}


class TailorRequest(BaseModel):
    id: str
    notes: str = ""      # what to change (instructions for this one resume)
    base: int | None = Field(default=None, ge=1, le=2**31 - 1)  # archived version to edit; None = start from the master CV
    label: str = ""      # names and pins the resulting version, e.g. "final"


MAX_QUEUED = 5

ROW_SQL = (
    "SELECT jobs.*, t.markdown AS tailoring_md, tj.state AS tj_state, tj.pdf AS tj_pdf, "
    "tj.warnings AS tj_warnings, tj.error AS tj_error, tj.requested AS tj_requested, tj.finished AS tj_finished "
    "FROM jobs LEFT JOIN tailoring t ON t.job_id = jobs.id LEFT JOIN tailor_jobs tj ON tj.job_id = jobs.id")


def check_tailor_options(con: sqlite3.Connection, job_id: str, notes: str, base: int | None, label: str) -> str | None:
    """An error message for unusable regenerate options, or None."""
    if len(notes) > 2000:
        return "instructions are too long (max 2000 characters)"
    if label and not radar.LABEL_RE.match(label):
        return "label: 1-24 letters, digits, space, dot, dash or underscore"
    if base is not None:
        if not notes.strip():
            return "describe what to change when you edit a version"
        if con.execute("SELECT 1 FROM resume_versions WHERE job_id = ? AND n = ?", (job_id, base)).fetchone() is None:
            return f"version {base} does not exist"
    return None


@app.post("/api/tailor")
async def request_tailor(body: TailorRequest, request: Request):
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc.lower() not in allowed_hosts():
        return JSONResponse({"error": "bad origin"}, status_code=403)
    if not radar.CV_TEX.exists():
        return JSONResponse({"error": "cv/cv-content.tex is not on the server"}, status_code=409)
    with db() as con:
        con.execute("BEGIN IMMEDIATE")  # check and insert as one step, so a concurrent delete or request cannot slip in between
        if con.execute("SELECT 1 FROM jobs WHERE id = ?", (body.id,)).fetchone() is None:
            return JSONResponse({"error": "unknown job"}, status_code=404)
        row = con.execute("SELECT state FROM tailor_jobs WHERE job_id = ?", (body.id,)).fetchone()
        if row and row["state"] in ("queued", "running"):
            return {"state": row["state"]}
        if con.execute("SELECT COUNT(*) FROM tailor_jobs WHERE state IN ('queued','running')").fetchone()[0] >= MAX_QUEUED:
            return JSONResponse({"error": "queue is full, try again in a few minutes"}, status_code=429)
        problem = check_tailor_options(con, body.id, body.notes, body.base, body.label.strip())
        if problem:
            return JSONResponse({"error": problem}, status_code=422)
        con.execute(
            "INSERT INTO tailor_jobs (job_id, state, requested, notes, base_n, label) VALUES (?, 'queued', CURRENT_TIMESTAMP, ?, ?, ?) "
            "ON CONFLICT(job_id) DO UPDATE SET state = 'queued', requested = CURRENT_TIMESTAMP, "
            "finished = NULL, error = NULL, pdf = 0, warnings = NULL, "
            "notes = excluded.notes, base_n = excluded.base_n, label = excluded.label",
            (body.id, body.notes.strip() or None, body.base, body.label.strip() or None))
    return {"state": "queued"}


class DeleteVersions(BaseModel):
    id: str
    n: int | None = Field(default=None, ge=1, le=2**31 - 1)
    all_older: bool = False


@app.post("/api/resume/delete")
async def delete_resume_versions(body: DeleteVersions, request: Request):
    """Delete older archived resume versions (never the newest; pinned ones only one by one)."""
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc.lower() not in allowed_hosts():
        return JSONResponse({"error": "bad origin"}, status_code=403)
    if body.n is None and not body.all_older:
        return JSONResponse({"error": "give a version number or all_older"}, status_code=422)
    with db() as con:
        try:
            deleted = cv.delete_versions(con, body.id, body.n, body.all_older)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
    return {"deleted": deleted}


@app.get("/api/tailor/status")
def tailor_status(id: str):
    with db() as con:
        row = con.execute("SELECT state FROM tailor_jobs WHERE job_id = ?", (id,)).fetchone()
    return {"state": row["state"] if row else None}


@app.get("/api/tailor/fragment", response_class=HTMLResponse)
def tailor_fragment(id: str):
    """HTML of one card's tailor slot, so the page can swap it in place while polling."""
    with db() as con:
        row = con.execute(f"{ROW_SQL} WHERE jobs.id = ?", (id,)).fetchone()
        vers = versions_for(con, [id]) if row else {}
    if row is None:
        return HTMLResponse("", status_code=404)
    return HTMLResponse(render_tailor(row, vers.get(id)))


FILE_NAME_RE = re.compile(r"\A[0-9a-f]{16}(\.v[1-9]\d{0,5})?\.(pdf|tex)\Z")  # current files and archived versions


@app.get("/files/{name}")
def download(name: str):
    # Strict whitelist: nothing user-controlled reaches the filesystem path.
    if not FILE_NAME_RE.match(name):
        return JSONResponse({"error": "not found"}, status_code=404)
    path = radar.TAILORED_DIR / name
    if not path.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    media = "application/pdf" if name.endswith(".pdf") else "text/plain; charset=utf-8"
    return FileResponse(path, media_type=media, filename=f"resume-{name}")


# --- helpers ---------------------------------------------------------------------------------------

e = html.escape


def safe_url(u: str) -> str:
    return u if urlsplit(u or "").scheme in ("http", "https") else "#"


def parse_ts(s: str | None) -> datetime | None:
    """SQLite CURRENT_TIMESTAMP (UTC) -> aware Berlin datetime."""
    try:
        return datetime.strptime((s or "")[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(BERLIN)
    except ValueError:
        return None


def fmt_day(s: str | None) -> str:
    d = parse_ts(s)
    return f"{d.day} {d.strftime('%b')}" if d else ""


def fmt_hm(s: str | None) -> str:
    d = parse_ts(s)
    return d.strftime("%H:%M") if d else ""


def next_run(now: datetime | None = None) -> datetime:
    """Next Mon-Fri 08:00 Berlin, matching job-radar.timer."""
    now = now or datetime.now(BERLIN)
    for add in range(8):
        d = (now + timedelta(days=add)).replace(hour=8, minute=0, second=0, microsecond=0)
        if d > now and d.weekday() < 5:
            return d
    return now


def next_run_text(now: datetime | None = None) -> str:
    now = now or datetime.now(BERLIN)
    d = next_run(now)
    days = (d.date() - now.date()).days
    return (f"today, {d:%H:%M}" if days == 0 else f"tomorrow, {d:%H:%M}" if days == 1
            else f"{d.strftime('%a')} {d.day} {d.strftime('%b')}, {d:%H:%M}")


def band(score) -> str:
    return "none" if score is None else "hi" if score >= 70 else "mid" if score >= 50 else "lo"


BAND_WORD = {"hi": "high", "mid": "medium", "lo": "low"}


def status_ok(st: str, mode: str) -> bool:
    return (mode == "all" or (mode == "open" and st in OPEN_STATUSES)
            or (mode == "active" and st != "not_relevant") or st == mode)


def job_gaps(r) -> list[str]:
    try:
        return [str(g) for g in json.loads(r["gaps"] or "[]")]
    except ValueError:
        return []


# Inline icons (stroke = currentColor, so CSS colours them).
ICONS = {
    "radar": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><path d="M12 12l6-6"/>',
    "pin": '<path d="M12 21s-7-6.2-7-11a7 7 0 0 1 14 0c0 4.8-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/>',
    "file": '<path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8z"/><path d="M14 3v5h5"/>',
    "check": '<path d="M5 12l5 5 9-10"/>',
    "alert": '<circle cx="12" cy="12" r="9"/><path d="M12 8v5"/><path d="M12 16h.01"/>',
    "xcircle": '<circle cx="12" cy="12" r="9"/><path d="M15 9l-6 6"/><path d="M9 9l6 6"/>',
    "chevron": '<path d="M9 6l6 6-6 6"/>',
    "back": '<path d="M15 6l-6 6 6 6"/>',
    "download": '<path d="M12 4v11"/><path d="M7 10l5 5 5-5"/><path d="M5 20h14"/>',
    "chart": '<path d="M4 19V5"/><path d="M4 19h16"/><path d="M8 15l3-4 3 2 5-6"/>',
    "sliders": '<path d="M4 6h16"/><path d="M7 12h10"/><path d="M10 18h4"/>',
}


def icon(name: str, size: int = 16, sw: float = 2) -> str:
    return (f'<svg class="ic" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="{sw}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{ICONS[name]}</svg>')


# --- skill gaps ------------------------------------------------------------------------------------

def gap_stats(con: sqlite3.Connection, days: int = 7) -> dict:
    """Missing skills over the last `days` days among jobs scoring 50+ (ranked by count x average score)."""
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%d %H:%M:%S"
    cut, cut2 = (now - timedelta(days=days)).strftime(fmt), (now - timedelta(days=2 * days)).strftime(fmt)
    q = "SELECT id, title, score, gaps, cv_track, first_seen FROM jobs WHERE score >= 50 AND status != 'not_relevant' AND gaps IS NOT NULL"
    cur_rows = con.execute(f"{q} AND first_seen >= ?", (cut,)).fetchall()
    prev_rows = con.execute(f"{q} AND first_seen >= ? AND first_seen < ?", (cut2, cut)).fetchall()
    prev: Counter = Counter()
    for r in prev_rows:
        prev.update({g.strip().lower() for g in job_gaps(r)})
    agg: dict[str, dict] = defaultdict(lambda: {"names": Counter(), "n": 0, "total": 0, "tracks": Counter(), "jobs": []})
    for r in cur_rows:
        for g in {x.strip() for x in job_gaps(r) if x.strip()}:
            a = agg[g.lower()]
            a["names"][g] += 1
            a["n"] += 1
            a["total"] += r["score"]
            if r["cv_track"]:
                a["tracks"][r["cv_track"]] += 1
            a["jobs"].append(r["title"])
    ranked = []
    for key, a in agg.items():
        ranked.append({"key": key, "name": a["names"].most_common(1)[0][0], "n": a["n"], "avg": round(a["total"] / a["n"]),
                       "weight": a["total"], "track": a["tracks"].most_common(1)[0][0] if a["tracks"] else None,
                       "prev": prev.get(key, 0), "jobs": a["jobs"]})
    ranked.sort(key=lambda g: (-g["weight"], g["name"]))
    return {"ranked": ranked, "jobs": len(cur_rows), "since": parse_ts(cut), "days": days}


# --- rendering -------------------------------------------------------------------------------------

def inline(s: str) -> str:
    """Already-escaped text -> **bold** and the (critical)/(nice-to-have) tags as small chips."""
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = s.replace("(critical)", '<span class="tag crit">critical</span>')
    return s.replace("(nice-to-have)", '<span class="tag nice">nice to have</span>')


def md_blocks(lines: list[str]) -> str:
    out, kind, buf = [], None, []

    def flush():
        nonlocal kind, buf
        if buf:
            out.append(f"<{kind}>" + "".join(f"<li>{x}</li>" for x in buf) + f"</{kind}>")
        kind, buf = None, []

    for raw in lines:
        line = raw.strip()
        if not line:
            flush()
            continue
        m_b, m_n = re.match(r"^[-*]\s+(.*)", line), re.match(r"^\d+[.)]\s+(.*)", line)
        if m_b or m_n:
            want = "ul" if m_b else "ol"
            if kind != want:
                flush()
                kind = want
            buf.append(inline(e((m_b or m_n).group(1))))
        else:
            flush()
            out.append(f"<p>{inline(e(line))}</p>")
    flush()
    return "".join(out)


SEVERITY_RE = re.compile(r"\(\s*(critical|important|nice[- ]to[- ]have|minor)\b([^)]*)\)", re.I)
HOURS_RE = re.compile(r"\bAbout\s+(\d+(?:\s*[\u2013-]\s*\d+)?)\s*h\b\.?", re.I)
ABBREV = ("e.g", "i.e", "vs", "etc", "z.B", "z. B", "d.h", "ca", "approx", "incl", "bzw", "inkl", "usw")


def first_sentence_end(text: str) -> int:
    """Index of the '. ' that ends the first sentence, ignoring abbreviations, parentheses and **bold**; -1 if none."""
    depth = 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "." and depth == 0 and text[i + 1:i + 2] == " " and text[:i].count("**") % 2 == 0:
            if not text[:i].lower().endswith(ABBREV) and not re.search(r"(?:^|\s)\w$", text[:i]):
                return i
    return -1


def split_gap(text: str) -> tuple[str, str, str]:
    """One gap bullet -> (severity, headline, detail). Severity is 'crit', 'nice' or ''."""
    sev = ""
    m = SEVERITY_RE.search(text)
    if m:
        sev = "crit" if m.group(1).lower() in ("critical", "important") else "nice"
        rest = m.group(2).strip(" ;,:")
        text = re.sub(r"\s+([.,;:])", r"\1", text[:m.start()] + (f"({rest})" if rest else "") + text[m.end():]).strip()
    end = first_sentence_end(text)
    head, detail = (text, "") if end < 0 else (text[:end], text[end + 1:])
    return sev, head.rstrip(".").strip(), detail.strip()


def md_items(lines: list[str]) -> tuple[list[str], list[str]]:
    """(list items, other prose lines). A line right after an item that is not itself an item continues that item
    (wrapped bullets); prose before the first item, or after a blank line, is returned separately so nothing is lost."""
    items: list[str] = []
    prose: list[str] = []
    after_item = False
    for ln in lines:
        m = re.match(r"^\s*(?:[-*]|\d+[.)])\s+(.*)", ln)
        if m:
            items.append(m.group(1).strip())
            after_item = True
        elif not ln.strip():
            after_item = False
        elif after_item:
            items[-1] += " " + ln.strip()
        else:
            prose.append(ln.strip())
    return items, prose


def prose_html(prose: list[str]) -> str:
    return f'<p class="lead">{inline(e(" ".join(prose)))}</p>' if prose else ""


def render_gaps_block(lines: list[str]) -> str:
    cards, (items, prose) = [], md_items(lines)
    for raw in items:
        sev, head, detail = split_gap(raw)
        tag = {"crit": '<span class="tag crit">critical</span>', "nice": '<span class="tag nice">nice to have</span>'}.get(sev, "")
        cards.append(f'<li class="gapcard {sev}">{icon("alert", 16)}<div><strong>{inline(e(head))}</strong>{tag}'
                     + (f'<p>{inline(e(detail))}</p>' if detail else "") + "</div></li>")
    return prose_html(prose) + f'<ul class="gapcards">{"".join(cards)}</ul>' if cards else ""


def render_matches_block(lines: list[str]) -> str:
    rows, (items, prose) = [], md_items(lines)
    for raw in items:
        req, arrow, ev = raw.partition(" -> ")
        if not arrow:
            rows.append(f'<li class="match">{icon("check", 15, 2.6)}<div>{inline(e(raw))}</div></li>')
            continue
        rows.append(f'<li class="match">{icon("check", 15, 2.6)}<div><strong>{inline(e(req.replace("**", "")))}</strong>'
                    f'<span>{inline(e(ev))}</span></div></li>')
    return prose_html(prose) + f'<ul class="matches">{"".join(rows)}</ul>' if rows else ""


def render_learn_block(lines: list[str]) -> str:
    steps, (items, prose) = [], md_items(lines)
    for i, raw in enumerate(items, 1):
        hm = HOURS_RE.search(raw)
        hours = f'<span class="hours">{e(hm.group(1).replace(" ", ""))} h</span>' if hm else ""
        body = re.sub(r"\(\s*\)", "", (raw[:hm.start()] + raw[hm.end():]) if hm else raw).strip()  # only the shown estimate leaves the text
        title, sep, detail = body.partition(":")
        if sep and title.startswith("**") and title.endswith("**"):
            title, detail = title.strip("*"), detail.strip()
        else:
            title, detail = "", body
        steps.append(f'<li class="step"><span class="num">{i}</span><div>' + (f"<strong>{e(title)}</strong>" if title else "")
                     + f'<p>{inline(e(detail))}</p></div>{hours}</li>')
    return prose_html(prose) + f'<ol class="steps">{"".join(steps)}</ol>' if steps else ""


def render_fit(md: str, bnd: str) -> str:
    """Fit analysis markdown as components: verdict callout, skill gaps first (amber cards with severity), matches as
    requirement/evidence rows, numbered learning steps with hours, CV tailoring collapsed, hook as a quote."""
    sections: list[tuple[str, list[str]]] = []
    for line in md.splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif sections:
            sections[-1][1].append(line)
    if not sections:
        return f'<div class="md">{md_blocks(md.splitlines())}</div>'
    order = {"verdict": 0, "gaps": 1, "what matches": 2, "learn first": 3, "cv tailoring": 4, "application hook": 5}
    out = []
    for name, lines in sorted(sections, key=lambda sec: order.get(sec[0].lower(), 3.5)):
        key, body = name.lower(), " ".join(x.strip() for x in lines if x.strip())
        if key.startswith("verdict"):
            out.append(f'<div class="verdict {bnd if bnd != "none" else "lo"}"><strong>Verdict</strong> · {inline(e(body))}</div>')
        elif key.startswith("application hook"):
            out.append(f'<div><h4>{e(name)}</h4><blockquote>{inline(e(body))}</blockquote></div>')
        elif key == "gaps" and (blk := render_gaps_block(lines)):
            out.append(f'<div class="fitsec gaps"><h4>{icon("alert", 14)}Skill gaps</h4>{blk}</div>')
        elif key == "what matches" and (blk := render_matches_block(lines)):
            out.append(f'<div class="fitsec"><h4>{icon("check", 14, 2.6)}What matches</h4>{blk}</div>')
        elif key == "learn first" and (blk := render_learn_block(lines)):
            out.append(f'<div class="fitsec"><h4>{icon("chart", 14)}Learn first</h4>{blk}</div>')
        elif key.startswith("cv tailoring"):
            out.append(f'<details class="sub"><summary>{icon("chevron", 12, 2.4)}{e(name)}</summary>{md_blocks(lines)}</details>')
        else:
            out.append(f"<div><h4>{e(name)}</h4>{md_blocks(lines)}</div>")
    return '<div class="md">' + "".join(out) + "</div>"


def fit_details(r, alone: bool = False) -> str:
    """The collapsed 'Fit analysis' control; its label counts the skill gaps so they are visible before opening."""
    gaps = job_gaps(r)
    badge = f'<span class="gapcount">{len(gaps)} gap{"s" if len(gaps) != 1 else ""}</span>' if gaps else ""
    inner = (f'<details class="fit"><summary>{icon("chevron", 14, 2.4)}Fit analysis &amp; what to learn{badge}</summary>'
             f'{render_fit(r["tailoring_md"], band(r["score"]))}</details>')
    return f'<div class="fitwrap{" alone" if alone else ""}">{inner}</div>'


def split_warnings(raw: str | None) -> tuple[list[str], list[str], bool]:
    """(claims not found in the master CV, other notes, True if the server had no LaTeX engine)."""
    try:
        items = [str(w) for w in json.loads(raw or "[]")]
    except ValueError:
        items = []
    claims, notes, no_engine = [], [], False
    for w in items:
        m = re.match(r"Not found in your master CV, please verify:\s*(.*)", w)
        if m:
            claims += [t.strip() for t in m.group(1).split(",") if t.strip()]
        elif w.startswith("No LaTeX engine"):
            no_engine = True
        else:
            notes.append(w)
    return claims, notes, no_engine


def versions_for(con: sqlite3.Connection, ids: list[str] | None = None) -> dict[str, list]:
    """Archived resume versions per job, newest first."""
    sql = "SELECT job_id, n, created, role, lang, pdf, label, keep FROM resume_versions"
    args: list = []
    if ids is not None:
        if not ids:
            return {}
        sql += " WHERE job_id IN (" + ",".join("?" * len(ids)) + ")"
        args = list(ids)
    out: dict[str, list] = defaultdict(list)
    for v in con.execute(sql + " ORDER BY job_id, n DESC", args):
        out[v["job_id"]].append(v)
    return out


def render_history(r, versions: list, state: str | None) -> str:
    """Collapsed list of earlier resume versions: shown only when there is something older to look at.
    Each version can be edited (opens the Regenerate panel on it) or deleted, the current one included."""
    if not versions or state not in ("done", "failed"):
        return ""
    token, latest, items = radar.file_token(r["id"]), int(versions[0]["n"]), []
    for v in versions:
        n = int(v["n"])
        d = parse_ts(v["created"])
        when = f"{d.day} {d.strftime('%b')}, {d:%H:%M}" if d else ""
        tags = " · ".join(x for x in (str(v["lang"] or "").upper(), v["role"] or "") if x)
        meta = " · ".join(x for x in (when, tags) if x)
        label = str(v["label"] or "").strip()
        pin = f' <span class="tag keep">{e(label)}</span>' if label else ""
        cur = ' <span class="tag nice">current</span>' if n == latest and state == "done" else ""
        links = (f'<a href="/files/{token}.v{n}.pdf" download aria-label="Download version {n} as PDF">PDF</a>' if v["pdf"] else "") + \
                f'<a href="/files/{token}.v{n}.tex" download aria-label="Download version {n} LaTeX source">.tex</a>'
        edit = f'<button type="button" class="lnk muted" data-act="edit-version" data-n="{n}" aria-label="Edit version {n}">Edit…</button>'
        dele = (f'<button type="button" class="lnk del" data-act="resume-delete" data-n="{n}" data-pinned="{int(bool(v["keep"]))}" '
                f'data-cur="{int(n == latest)}" data-prev="{int(len(versions) > 1)}" aria-label="Delete version {n}">Delete</button>')
        items.append(f'<li><span class="vn">v{n}</span><span class="muted vm">{e(meta)}</span>{pin}{cur}'
                     f'<span class="vl">{edit}{links}{dele}</span></li>')
    older = ""
    if any(int(v["n"]) != latest and not v["keep"] for v in versions):
        older = ('<button type="button" class="lnk del" data-act="resume-delete-older">Delete all older versions '
                 '<span class="muted">(keeps the newest and pinned ones)</span></button>')
    return (f'<details class="hist"><summary>{icon("chevron", 14, 2.4)}History ({len(versions)})</summary>'
            f'<ul>{"".join(items)}</ul>{older}</details>')


def render_regen(r, versions: list | None) -> str:
    """Collapsed Regenerate panel: what to change, which version to edit, and an optional label that pins the result."""
    versions = versions or []
    latest = int(versions[0]["n"]) if versions else None
    opts = '<option value="">Master CV (start fresh)</option>' + "".join(
        f'<option value="{int(v["n"])}"{" selected" if int(v["n"]) == latest else ""}>v{int(v["n"])}'
        f'{" · " + e(v["label"]) if v["label"] else ""}{" (latest)" if int(v["n"]) == latest else ""}</option>' for v in versions)
    return (f'<details class="regen"><summary>{icon("chevron", 14, 2.4)}Regenerate…</summary><div class="regenform">'
            '<label>What should change?<textarea data-f="notes" rows="3" maxlength="2000" '
            'placeholder="e.g. replace project X with Y, or shorten Z"></textarea></label>'
            f'<label>Based on<select data-f="base">{opts}</select></label>'
            '<label>Label (pins this version)<input data-f="label" maxlength="24" placeholder="e.g. final"></label>'
            '<p class="small muted">Without instructions this regenerates from your master CV. With instructions, the chosen '
            'version is edited and everything else stays as it is.</p>'
            '<button type="button" class="btn primary" data-act="regen-go">Generate</button></div></details>')


def render_tailor(r, versions: list | None = None) -> str:
    """The tailor slot of a card (swapped in place by the page while a request is pending)."""
    state, token = r["tj_state"], radar.file_token(r["id"])
    box, idle = "", state is None
    if state in ("queued", "running"):
        started = fmt_hm(r["tj_requested"])
        what = "Waiting in the queue…" if state == "queued" else "Tailoring resume…"
        box = (f'<div class="box pend" role="status"><div class="prow"><span class="spin" aria-hidden="true"></span>'
               f'<span>{what} <span class="muted">usually about a minute</span></span></div>'
               f'<div class="bar"><i></i></div><span class="small muted">{"Started " + started + " · " if started else ""}'
               "you can keep triaging; this card updates on its own.</span></div>")
    elif state == "failed":
        box = (f'<div class="box fail" role="alert"><div class="frow">{icon("xcircle", 18)}<div>'
               f'<strong>Couldn\'t tailor this resume.</strong><div class="mono">{e(r["tj_error"] or "unknown error")}</div>'
               '</div></div><button type="button" class="btn" data-act="tailor">Retry</button></div>')
        box += f'<div class="boxfoot">{render_regen(r, versions)}{render_history(r, versions or [], state)}</div>'
    elif state == "done":
        claims, notes, no_engine = split_warnings(r["tj_warnings"])
        has_pdf = bool(r["tj_pdf"])
        stamp = (f'<div class="stamp">{icon("check", 16, 2.4)}Resume tailored · {fmt_hm(r["tj_finished"])}</div>' if has_pdf
                 else f'<div class="stamp amber">{icon("check", 16, 2.4)}LaTeX ready · no PDF built</div>')
        tex = f'<a class="btn{"" if has_pdf else " primary"}" href="/files/{token}.tex" download>{"LaTeX source" if has_pdf else "LaTeX source (.tex)"}</a>'
        pdf = f'<a class="btn primary grow" href="/files/{token}.pdf" download>{icon("download")}Resume (PDF)</a>' if has_pdf else ""
        warn = ""
        if not has_pdf:
            why = "The server has no LaTeX engine (tectonic)." if no_engine else "The PDF build did not finish."
            warn += f'<div class="warn"><strong>No PDF this time.</strong> {why} Compile the .tex locally or in Overleaf.</div>'
        items = []
        if claims:
            items.append("Not in your master CV: " + ", ".join(f"<strong>{e(c)}</strong>" for c in claims))
        items += [e(n) for n in notes]
        if items:
            n = len(claims) + len(notes)
            head = f"Check {n} claim{'s' if n != 1 else ''} before sending" if claims else "Check before sending"
            warn += (f'<div class="warn" role="note"><div class="whead">{icon("alert", 18)}<strong>{head}</strong></div>'
                     "<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul></div>")
        fit = ""
        if r["tailoring_md"]:
            fit = fit_details(r)
        box = (f'<section class="box done" aria-label="Tailored resume">{stamp}<div class="dl">{pdf}{tex}</div>{warn}{fit}'
               f'<div class="boxfoot">{render_regen(r, versions)}{render_history(r, versions or [], state)}</div></section>')
    elif r["tailoring_md"]:  # analysis made on the command line, no resume requested from the dashboard
        box = fit_details(r, alone=True)
    desc = (r["description"] or "").strip()
    details = ('<button type="button" class="lnk muted" data-act="details" aria-expanded="false">Posting details</button>'
               if desc else "<span></span>")
    tailor = (f'<button type="button" class="lnk strong" data-act="tailor">{icon("file")}Tailor resume'
              '<kbd aria-hidden="true">t</kbd></button>') if idle else ""
    pending = ' data-pending="1"' if state in ("queued", "running") else ""
    return (f'<div class="tailor" data-state="{e(state or "idle")}"{pending}>{box}'
            f'<div class="foot">{details}{tailor}</div></div>')


def seg(status: str) -> str:
    btns = []
    for k in STATUS_ORDER:
        short = "Skip" if k == "not_relevant" else STATUSES[k]
        aria = ' aria-label="Not relevant"' if k == "not_relevant" else ""
        btns.append(f'<button type="button" data-s="{k}" aria-pressed="{str(status == k).lower()}"{aria}>'
                    f'<span class="s-short">{short}</span><span class="s-long">{STATUSES[k]}</span></button>')
    return f'<div class="seg" role="group" aria-label="Status">{"".join(btns)}</div>'


def render_card(r, hidden: bool = False, versions: list | None = None) -> str:
    score, bnd = r["score"], band(r["score"])
    fs = parse_ts(r["first_seen"])
    fresh = r["status"] == "new" and fs is not None and datetime.now(BERLIN) - fs < timedelta(hours=48)
    if score is None:
        badge = '<div class="score none" role="img" aria-label="No score yet">–</div>'
        reason = "Not scored: the posting had no usable description. Open it to judge yourself."
    else:
        badge = f'<div class="score {bnd}" role="img" aria-label="Score {score} of 100, {BAND_WORD[bnd]}">{score}</div>'
        reason = r["reason"] or ""
    loc, dist = (r["location"] or "").strip(), r["distance_km"]
    if dist is not None:
        where = f'<span class="loc">{icon("pin", 14)}{e(loc)}{" · " if loc else ""}<strong>{dist:g} km</strong></span>'
    else:
        where = f'<span>{e(loc) + " · " if loc else ""}distance unknown</span>'
    gaps = job_gaps(r)
    cv_bits = []
    if r["cv_track"]:
        cv_bits.append(f'<span class="muted">CV <strong>{e(r["cv_track"])}</strong>{" · missing" if gaps else ""}</span>')
    elif gaps:
        cv_bits.append('<span class="muted">Missing</span>')
    cv_bits += [f'<span class="chip gap">{e(g)}</span>' for g in gaps]
    cv_line = f'<div class="cvline">{"".join(cv_bits)}</div>' if cv_bits else ""
    desc = (r["description"] or "").strip()
    title = r["title"] or ""
    return f"""<article class="card" data-id="{e(r['id'], quote=True)}" data-score="{'' if score is None else score}" \
data-source="{e(r['source'] or '', quote=True)}" data-status="{e(r['status'], quote=True)}" \
data-first="{e(r['first_seen'] or '', quote=True)}"{' hidden' if hidden else ''}>
  <div class="head">{badge}<div class="ttl"><a class="title" href="{e(safe_url(r['url']), quote=True)}" target="_blank" rel="noopener noreferrer">{e(title)}</a>{'<span class="pill">NEW</span>' if fresh else ''}<span class="co">{e(r['company'] or '')}</span></div></div>
  <div class="rest">
    <div class="meta">{where}<span>{e(r['source'] or '')}</span><span>{e(fmt_day(r['first_seen']))}</span></div>
    <p class="reason{' muted' if score is None else ''}">{e(reason)}</p>
    {cv_line}
    {render_tailor(r, versions)}
    {seg(r['status'])}
    {f'<div class="desc" hidden>{e(desc[:2000])}</div>' if desc else ''}
  </div>
</article>
<div class="collapsed" data-for="{e(r['id'], quote=True)}" hidden><span class="ctext">Skipped · {e(title)}</span><button type="button" class="lnk strong" data-act="undo">Undo<kbd aria-hidden="true">u</kbd></button></div>"""


CSS = """
:root{--bg:#f6f8fa;--card:#fff;--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--accent:#0969da;--high:#1a7f37;--mid:#9a6700;--low:#6e7781;
--hi-fg:#116329;--hi-tint:#dafbe1;--mid-fg:#7d4e00;--mid-tint:#fff8c5;--lo-fg:#59636e;--lo-tint:#eef1f4;
--gap-fg:#953800;--gap-tint:#fff1e5;--gap-line:#ffd8b5;--gap-bar:#bc4c00;--warn-tint:#fff8c5;--warn-line:#d4a72c;
--danger:#d1242f;--danger-tint:#ffebe9;--danger-line:#ff8182;--accent-solid:#0969da;--accent-tint:#ddf4ff;--accent-fg:#0550ae;--focus:#0969da}
@media(prefers-color-scheme:dark){:root{--bg:#0d1117;--card:#151b23;--fg:#e6edf3;--muted:#9198a1;--line:#2f3742;--accent:#4493f8;--high:#3fb950;--mid:#d29922;--low:#8b949e;
--hi-fg:#3fb950;--hi-tint:#12261e;--mid-fg:#d29922;--mid-tint:#2b2111;--lo-fg:#9198a1;--lo-tint:#21262d;
--gap-fg:#ffa657;--gap-tint:#2a1708;--gap-line:#5a3412;--gap-bar:#db6d28;--warn-tint:#272115;--warn-line:#9e6a03;
--danger:#f85149;--danger-tint:#3a1d1d;--danger-line:#8e1519;--accent-solid:#1f6feb;--accent-tint:#0f2a4a;--accent-fg:#79c0ff;--focus:#4493f8}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans",Helvetica,Arial,sans-serif}
a{color:var(--accent)}button{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.muted{color:var(--muted)}.small{font-size:12px}.ic{flex:none}
kbd{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);border:1px solid var(--line);border-bottom-width:2px;border-radius:4px;padding:0 4px;margin-left:6px;display:none}
/* header */
.top{background:var(--card);border-bottom:1px solid var(--line)}
.top .in{max-width:1120px;margin:0 auto;padding:14px 16px 12px;display:flex;flex-direction:column;gap:10px}
.bar1{display:flex;align-items:center;justify-content:space-between;gap:12px}
.brand{display:flex;align-items:center;gap:8px;color:var(--accent)}.brand h1{margin:0;font-size:18px;font-weight:650;letter-spacing:-.01em;color:var(--fg)}
.lastrun{font-size:13px;color:var(--muted)}.lr-long{display:none}
.stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
.stat{background:var(--bg);border-radius:8px;padding:8px 10px}.stat b{display:block;font-size:20px;font-weight:650;line-height:1.1}
.stat span{font-size:12px;color:var(--muted)}.stat.hot{background:var(--accent-tint)}.stat.hot b,.stat.hot span{color:var(--accent-fg)}.stat.tracked{display:none}
.back{width:40px;height:40px;margin-left:-10px;display:flex;align-items:center;justify-content:center;color:var(--fg)}
/* gap strip + filters */
.gapstrip{margin:12px 16px 0;background:var(--gap-tint);border:1px solid var(--gap-line);border-radius:10px;padding:10px 12px;display:flex;align-items:center;gap:10px;text-decoration:none;color:var(--fg);min-height:44px;font-size:14px;line-height:1.35}
.gapstrip .ic{color:var(--gap-fg)}.gapstrip span{flex:1}
.layout{max-width:1120px;margin:0 auto;display:block}
.filters{padding:12px 16px 4px;display:flex;flex-direction:column;gap:10px}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.cb{height:40px;padding:0 14px;border-radius:999px;border:1px solid var(--line);background:var(--card);font-size:14px;cursor:pointer;display:inline-flex;align-items:center;gap:6px}
.cb[aria-pressed=true]{background:var(--accent-solid);border-color:var(--accent-solid);color:#fff;font-weight:600}
.showing{margin:0;font-size:13px;color:var(--muted)}
.more{display:none;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;grid-template-columns:1fr 1fr;gap:10px}
.more.open{display:grid}.more label{display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--muted)}.more .full{grid-column:1/-1}
.more select,.more input[type=search]{width:100%;min-height:40px;font:inherit;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:0 8px}
.more input[type=range]{width:100%;min-height:40px;accent-color:var(--accent)}.more output{font-weight:600;color:var(--fg)}
/* list + cards */
.col{min-width:0}
main{padding:8px 16px 24px;display:flex;flex-direction:column;gap:12px}
#list{display:flex;flex-direction:column;gap:12px}
.card{display:flex;flex-direction:column;gap:10px;padding:16px;border:1px solid var(--line);border-radius:10px;background:var(--card)}
.card[hidden],.collapsed[hidden],.desc[hidden]{display:none}
.card.kb{box-shadow:0 0 0 3px color-mix(in srgb,var(--focus) 35%,transparent);border-color:var(--focus)}
.head{display:flex;gap:12px;align-items:flex-start}
.ttl{flex:1;min-width:0;display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 8px}
.title{font-size:17px;font-weight:600;line-height:1.3;color:var(--fg);text-decoration:none;overflow-wrap:anywhere}.title:hover{text-decoration:underline}
.co{flex-basis:100%;font-size:14px;color:var(--muted);overflow-wrap:anywhere}
.pill{font-size:11px;font-weight:700;letter-spacing:.04em;color:var(--accent-fg);background:var(--accent-tint);padding:2px 6px;border-radius:999px}
.score{flex:none;width:48px;height:48px;border-radius:10px;display:grid;place-items:center;font-size:20px;font-weight:700;line-height:1}
.score.hi{background:var(--hi-tint);color:var(--hi-fg)}.score.mid{background:var(--mid-tint);color:var(--mid-fg)}
.score.lo{background:var(--lo-tint);color:var(--lo-fg)}.score.none{border:1px dashed var(--line);color:var(--muted)}
.rest{display:flex;flex-direction:column;gap:10px;min-width:0}
.meta{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:13px;color:var(--muted)}.meta strong{color:var(--fg);font-weight:600}
.loc{display:inline-flex;align-items:center;gap:4px}
.reason{margin:0;font-size:15px;line-height:1.45;overflow-wrap:anywhere}
.cvline{display:flex;flex-wrap:wrap;align-items:center;gap:6px;font-size:13px}.cvline strong{color:var(--fg);font-weight:600}
.chip{border-radius:999px;padding:2px 8px;font-size:13px;font-weight:500}.chip.gap{background:var(--gap-tint);color:var(--gap-fg)}
.lnk{background:none;border:0;padding:0;min-height:40px;display:inline-flex;align-items:center;gap:6px;font-size:14px;cursor:pointer;text-decoration:none;color:var(--fg)}
.lnk.muted{color:var(--muted)}.lnk.strong{font-weight:600}.lnk:hover{text-decoration:underline}
.foot{display:flex;align-items:center;justify-content:space-between;border-top:1px solid var(--line);margin:2px -16px 0;padding:4px 16px 0}
.box{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:12px;display:flex;flex-direction:column;gap:10px;font-size:14px}
.box.fail{background:var(--danger-tint);border-color:var(--danger-line)}.box.fail .frow{display:flex;gap:10px;align-items:flex-start;color:var(--fg)}.box.fail .ic{color:var(--danger);margin-top:1px}
.mono{color:var(--muted);font:13px ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere;margin-top:2px}
.prow{display:flex;align-items:center;gap:10px}
.spin{width:18px;height:18px;border:2.6px solid var(--line);border-top-color:var(--accent);border-radius:50%;display:inline-block;animation:spin 1s linear infinite}
.bar{height:4px;border-radius:2px;background:var(--line);overflow:hidden}.bar i{display:block;width:40%;height:4px;background:var(--accent);animation:slide 1.6s ease-in-out infinite alternate}
@keyframes spin{to{transform:rotate(360deg)}}@keyframes slide{from{transform:translateX(0)}to{transform:translateX(150%)}}
.stamp{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--muted)}.stamp .ic{color:var(--high)}.stamp.amber .ic{color:var(--mid)}
.dl{display:flex;gap:8px;flex-wrap:wrap}
.btn{height:40px;padding:0 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);text-decoration:none;display:inline-flex;align-items:center;justify-content:center;gap:6px;font-size:14px;cursor:pointer}
.btn.primary{background:var(--accent-solid);border-color:var(--accent-solid);color:#fff;font-weight:600}.btn.grow{flex:1}.btn:hover{filter:brightness(.97)}
.box.fail .btn{align-self:flex-start;font-weight:600}
.warn{background:var(--warn-tint);border:1px solid var(--warn-line);border-radius:8px;padding:10px 12px;font-size:14px}
.warn .whead{display:flex;gap:8px;align-items:flex-start}.warn .ic{color:var(--mid-fg);margin-top:1px}
.warn ul{margin:6px 0 0;padding-left:26px;display:flex;flex-direction:column;gap:2px}
.fitwrap{position:relative}.fitwrap.alone{border:1px solid var(--line);border-radius:8px}
.fit summary{min-height:40px;display:flex;align-items:center;gap:6px;font-size:14px;font-weight:600;color:var(--accent);cursor:pointer;list-style:none;padding-right:96px}
.fit summary::-webkit-details-marker{display:none}.fit summary .ic{transition:transform .15s}.fit[open] summary .ic{transform:rotate(90deg)}
.fitwrap>.lnk{position:absolute;right:0;top:0;font-size:13px}
.fitwrap.alone>.lnk{display:none}.fitwrap.alone summary{padding-left:12px}
.hist{font-size:13px}.hist summary{min-height:40px;display:inline-flex;align-items:center;gap:6px;color:var(--muted);cursor:pointer;list-style:none}
.hist summary::-webkit-details-marker{display:none}.hist summary .ic{transition:transform .15s}.hist[open] summary{color:var(--fg)}.hist[open] summary .ic{transform:rotate(90deg)}
.hist ul{margin:0;padding:0;list-style:none;display:flex;flex-direction:column}
.hist li{display:flex;flex-wrap:wrap;align-items:center;gap:0 8px;min-height:40px;border-top:1px solid var(--line)}
.hist .vn{font-weight:600;min-width:28px}.hist .vl{margin-left:auto;display:flex;gap:4px;flex-shrink:0}
.hist .vl a{display:inline-flex;align-items:center;min-height:40px;padding:0 8px}
.hist .vl .lnk{padding:0 8px;font-size:13px}.hist .del,.lnk.del{color:var(--danger)}.hist>.lnk.del{margin-top:4px}
.tag.keep{background:var(--accent-tint);color:var(--accent-fg)}
.fit summary .gapcount{margin-left:8px;font-size:12px;font-weight:600;border-radius:999px;padding:1px 8px;background:var(--gap-tint);color:var(--gap-fg);border:1px solid var(--gap-line)}
.md .lead{margin:0 0 6px;font-size:13px;color:var(--muted)}
.fitsec h4{display:flex;align-items:center;gap:6px}.fitsec.gaps h4{color:var(--gap-fg)}
.gapcards,.matches,.steps{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px}
.gapcard{display:flex;gap:10px;align-items:flex-start;background:var(--gap-tint);border:1px solid var(--gap-line);border-left:4px solid var(--gap-bar);border-radius:8px;padding:8px 10px}
.gapcard.nice{border-left-color:var(--gap-line)}.gapcard .ic{color:var(--gap-fg);margin-top:2px;flex-shrink:0}
.gapcard strong{font-weight:600}.gapcard p{margin:2px 0 0;color:var(--muted);font-size:13px}
.match{display:flex;gap:10px;align-items:flex-start;padding:6px 0;border-top:1px solid var(--line)}.match:first-child{border-top:0}
.match .ic{color:var(--hi-fg);margin-top:3px;flex-shrink:0}.match strong{display:block;font-weight:600}.match span{color:var(--muted);font-size:13px}
.step{display:flex;gap:10px;align-items:flex-start;border:1px solid var(--line);border-radius:8px;padding:8px 10px}
.step .num{flex-shrink:0;width:22px;height:22px;border-radius:50%;background:var(--accent-tint);color:var(--accent-fg);font-size:12px;font-weight:600;display:flex;align-items:center;justify-content:center;margin-top:1px}
.step>div{flex:1;min-width:0}.step strong{font-weight:600}.step p{margin:2px 0 0;font-size:13px;color:var(--muted)}
.step .hours{flex-shrink:0;font-size:12px;font-weight:600;border-radius:999px;padding:1px 8px;background:var(--card);border:1px solid var(--line);white-space:nowrap}
.md details.sub summary{display:inline-flex;align-items:center;gap:6px;min-height:36px;cursor:pointer;color:var(--muted);font-size:12px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;list-style:none}
.md details.sub summary::-webkit-details-marker{display:none}.md details.sub[open] summary .ic{transform:rotate(90deg)}
.boxfoot{display:flex;flex-wrap:wrap;align-items:flex-start;gap:0 16px}.boxfoot>details{flex:1 1 100%}
.regen{font-size:13px}.regen summary{min-height:40px;display:inline-flex;align-items:center;gap:6px;color:var(--muted);cursor:pointer;list-style:none}
.regen summary::-webkit-details-marker{display:none}.regen summary .ic{transition:transform .15s}.regen[open] summary{color:var(--fg)}.regen[open] summary .ic{transform:rotate(90deg)}
.regenform{display:flex;flex-direction:column;gap:10px;padding:4px 0 8px}.regenform label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--muted)}
.regenform textarea,.regenform select,.regenform input{font:inherit;font-size:16px;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px;width:100%}
.regenform textarea{min-height:76px;resize:vertical}.regenform .btn{align-self:flex-start}
.md{display:flex;flex-direction:column;gap:12px;font-size:14px;line-height:1.5;padding:4px 0 8px}.fitwrap.alone .md{padding:4px 14px 12px}
.md h4{margin:0 0 4px;font-size:12px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}
.md p{margin:0}.md ul,.md ol{margin:0;padding-left:20px;display:flex;flex-direction:column;gap:3px}
.md blockquote{margin:0;padding:6px 10px;border-left:3px solid var(--line);font-style:italic}
.verdict{border-radius:6px;padding:8px 10px}.verdict.hi{background:var(--hi-tint)}.verdict.mid{background:var(--mid-tint)}.verdict.lo{background:var(--lo-tint)}
.verdict.hi strong{color:var(--hi-fg)}.verdict.mid strong{color:var(--mid-fg)}
.tag{font-size:12px;font-weight:600;border-radius:999px;padding:1px 7px;margin-left:4px}.tag.crit{background:var(--gap-tint);color:var(--gap-fg)}.tag.nice{background:var(--lo-tint);color:var(--lo-fg)}
.seg{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{min-height:40px;border:0;background:var(--card);font-size:13px;cursor:pointer}.seg button+button{border-left:1px solid var(--line)}
.seg button[aria-pressed=true]{background:var(--accent-solid);color:#fff;font-weight:600}
.seg button[data-s=new][aria-pressed=true]{background:var(--fg);color:var(--card)}
.s-long{display:none}
.desc{font-size:13px;color:var(--muted);white-space:pre-wrap;overflow-wrap:anywhere;border-top:1px dashed var(--line);padding-top:8px}
.collapsed{background:var(--card);border:1px dashed var(--line);border-radius:10px;padding:4px 8px 4px 16px;display:flex;align-items:center;gap:10px;font-size:14px;color:var(--muted)}
.ctext{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.collapsed .lnk{padding:0 12px;color:var(--accent)}
.endmsg{margin:8px 0 0;text-align:center;font-size:13px;color:var(--muted)}
/* empty states */
.empty,.firstrun{background:var(--card);border:1px dashed var(--line);border-radius:10px;padding:28px 20px;display:flex;flex-direction:column;align-items:center;gap:10px;text-align:center}
.empty[hidden]{display:none}.empty h2,.firstrun h2{margin:0;font-size:17px;font-weight:600}.empty p,.firstrun p{margin:0;font-size:15px;color:var(--muted)}
.empty .acts{display:flex;flex-direction:column;gap:8px;width:100%}.empty .btn{height:44px;font-weight:600}.empty .btn.ghost{border:0;background:transparent;color:var(--accent)}
.firstrun{border-style:solid;padding:40px 24px;gap:14px}.firstrun .ic{color:var(--muted)}
.facts{width:100%;background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:12px 14px;text-align:left;display:flex;flex-direction:column;gap:8px;font-size:14px}
.facts div{display:flex;justify-content:space-between;gap:12px}.facts .ok{color:var(--high);font-weight:600}.facts .bad{color:var(--danger);font-weight:600}
/* sidebar + report */
.side{display:none}.box2{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;display:flex;flex-direction:column;gap:10px;font-size:14px}
.box2 h2{margin:0;font-size:15px;font-weight:600}.box2 .hd{display:flex;align-items:baseline;justify-content:space-between}.box2 .hd a{font-size:13px}
.gbar{height:6px;background:var(--lo-tint);border-radius:3px;margin-top:4px}.gbar i{display:block;height:6px;background:var(--gap-bar);border-radius:3px}
.row2{display:flex;justify-content:space-between;gap:12px}.row2 .muted{white-space:nowrap}.bad{color:var(--danger)}
dl.keys{margin:0;display:grid;grid-template-columns:auto minmax(0,1fr);gap:6px 12px;align-items:center}dl.keys dt{white-space:nowrap}dl.keys dd{margin:0;color:var(--muted)}dl.keys kbd{display:inline-block;margin:0 2px 0 0}
.report{max-width:760px;margin:0 auto;padding:16px;display:flex;flex-direction:column;gap:14px}
.gcard{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;display:flex;flex-direction:column;gap:8px}
.gcard .hd{display:flex;align-items:center;gap:8px}.gcard h3{margin:0;font-size:16px;font-weight:600}.gcard .hd .muted{margin-left:auto;font-size:13px}
.rank{font-size:12px;font-weight:700;color:var(--gap-fg);background:var(--gap-tint);border-radius:999px;padding:2px 8px}
.gbar.big{height:8px;border-radius:4px}.gbar.big i{height:8px;border-radius:4px}
.gcard p{margin:0;font-size:14px}.gcard .jobs{font-size:13px}
.also{display:flex;flex-wrap:wrap;gap:6px;font-size:13px}.also span{background:var(--lo-tint);border-radius:999px;padding:2px 8px}
.rhead{display:flex;align-items:center;gap:8px}.rhead .t{font-size:17px;font-weight:650}.rhead .s{font-size:13px;color:var(--muted)}
#toast{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:var(--fg);color:var(--card);padding:10px 16px;border-radius:8px;font-size:14px;max-width:calc(100vw - 32px);z-index:10}
#toast[hidden]{display:none}
@media (min-width:1000px){
  .top .in{flex-direction:row;align-items:center;gap:24px;padding:14px 16px}.bar1{display:contents}
  .lr-short{display:none}.lr-long{display:inline}.lastrun{margin-left:auto;order:3}
  .stats{display:flex;gap:20px;order:2}.stat{background:none;padding:0;font-size:14px}.stat b{display:inline;font-size:16px}.stat span{font-size:14px}.stat.hot{background:none}.stat.tracked{display:block}
  .layout{display:flex;gap:32px;align-items:flex-start;padding:20px 16px 40px}.col{flex:0 0 760px}.side{display:flex;flex-direction:column;gap:16px;flex:1 1 280px;min-width:0}
  .gapstrip{display:none}.filters,main{padding-left:0;padding-right:0}.filters{padding-top:0}.cb.more-btn{display:none}.more{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));align-items:end}.more .full{grid-column:span 2}
  .card{display:grid;grid-template-columns:56px minmax(0,1fr);column-gap:16px;row-gap:8px;padding:16px 20px}
  .head{display:contents}.score{grid-column:1;grid-row:1/span 2;width:56px;height:56px;border-radius:12px;font-size:22px}
  .ttl{grid-column:2;grid-row:1}.title{font-size:18px}.co{flex-basis:auto;font-size:14px}.co::before{content:"· ";margin-left:-4px}
  .rest{grid-column:2;grid-row:2;display:grid;grid-template-columns:minmax(0,1fr) 400px;gap:8px 16px;align-items:center}
  .rest>.meta,.rest>.reason,.rest>.cvline,.rest>.desc{grid-column:1/-1}.tailor{display:contents}
  .tailor>.box,.tailor>.fitwrap{grid-column:1/-1}.tailor>.foot{grid-column:1;border:0;margin:0;padding:0;justify-content:flex-start;gap:16px}.rest>.seg{grid-column:2}
  .seg button{min-height:36px}.s-short{display:none}.s-long{display:inline}kbd{display:inline-block}
  .collapsed{padding:6px 12px 6px 20px}
}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

JS = r"""
(()=>{
const $=(s,r=document)=>r.querySelector(s),$$=(s,r=document)=>[...r.querySelectorAll(s)];
const list=$('#list');if(!list)return;
const more=$('#more'),showing=$('#showing'),empty=$('#empty'),toast=$('#toast'),total=+list.dataset.total;
const ctl={min:$('#f-min'),source:$('#f-source'),status:$('#f-status'),q:$('#f-q'),sort:$('#f-sort')};
const OPEN=['new','interested'],undo=[];let cur=null,toastT=0;
const say=m=>{toast.textContent=m;toast.hidden=false;clearTimeout(toastT);toastT=setTimeout(()=>toast.hidden=true,4500);};
const okStatus=(s,m)=>m==='all'||(m==='open'&&OPEN.includes(s))||(m==='active'&&s!=='not_relevant')||m===s;
const cards=()=>$$('.card',list);
const row=c=>{const n=c.nextElementSibling;return n&&n.classList.contains('collapsed')?n:null;};
function matches(c,f){
  const sc=c.dataset.score===''?0:+c.dataset.score;
  return sc>=f.min&&(f.source==='all'||c.dataset.source===f.source)&&okStatus(c.dataset.status,f.status)&&
    (!f.q||c.textContent.toLowerCase().includes(f.q));
}
const filt=()=>({min:+ctl.min.value,source:ctl.source.value,status:ctl.status.value,q:ctl.q.value.trim().toLowerCase(),sort:ctl.sort.value});
function sortCards(mode){
  const key=c=>mode==='new'?[c.dataset.first,c.dataset.score===''?-1:+c.dataset.score]:[c.dataset.score===''?-1:+c.dataset.score,c.dataset.first];
  const cs=cards().sort((a,b)=>{const x=key(a),y=key(b);return x[0]<y[0]?1:x[0]>y[0]?-1:x[1]<y[1]?1:x[1]>y[1]?-1:0;});
  for(const c of cs){const r=row(c);list.appendChild(c);if(r)list.appendChild(r);}
}
function showEmpty(shown,f){
  if(shown>0||!total){empty.hidden=true;return;}
  const live=cards().filter(c=>!c.dataset.skipped),n=(g)=>live.filter(c=>matches(c,g)).length;
  const h=$('h2',empty),p=$('p',empty),acts=$('.acts',empty);acts.textContent='';
  const only=f.min===0&&f.source==='all'&&!f.q;
  if(only&&f.status==='open'){h.textContent='Inbox zero.';p.textContent=list.dataset.triaged+' jobs triaged today.';}
  else{
    const what=[f.min?'at '+f.min+'+':'',f.source!=='all'?'from '+f.source:''].filter(Boolean).join(' ');
    h.textContent=what?'Nothing '+what:'No jobs match these filters';
    const pool=live.filter(c=>f.source==='all'||c.dataset.source===f.source);
    const best=Math.max(-1,...pool.map(c=>c.dataset.score===''?-1:+c.dataset.score));
    p.textContent=f.source!=='all'&&pool.length?pool.length+' '+f.source+' jobs are tracked'+(best>=0?'; the best scores '+best+'.':'.'):'Try a lower minimum score or clear the search.';
    if(f.min>0){const t=f.min>50?50:0,k=n({...f,min:t});if(k){acts.append(mk('btn','Lower to ≥ '+t+' ('+k+' jobs)','lower:'+t));}}
  }
  acts.append(mk('btn ghost','Clear all filters ('+live.length+' jobs)','clear'));
  empty.hidden=false;
}
function mk(cls,text,fix){const b=document.createElement('button');b.type='button';b.className=cls;b.textContent=text;b.dataset.fix=fix;return b;}
function apply(){
  const f=filt();let shown=0;
  sortCards(f.sort);
  for(const c of cards()){if(c.dataset.skipped){c.hidden=true;continue;}const ok=matches(c,f);c.hidden=!ok;if(ok)shown++;}
  $('#f-min-out').textContent=f.min;
  showing.textContent='Showing '+shown+' of '+total;
  $$('[data-chip]').forEach(b=>b.setAttribute('aria-pressed',String(
    b.dataset.chip==='triage'?f.status==='open':b.dataset.chip==='interested'?f.status==='interested':f.min>=70)));
  showEmpty(shown,f);
  const u=new URLSearchParams();
  if(f.status!=='open')u.set('status',f.status);if(f.min)u.set('min_score',f.min);if(f.source!=='all')u.set('source',f.source);
  if(f.q)u.set('q',ctl.q.value.trim());if(f.sort!=='score')u.set('sort',f.sort);
  history.replaceState(null,'',location.pathname+(u.toString()?'?'+u:''));
}
function setPressed(c,s){$$('.seg button',c).forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.s===s)));}
function collapse(c){c.dataset.skipped='1';c.hidden=true;c.classList.remove('kb');const r=row(c);if(r)r.hidden=false;}
function expand(c){delete c.dataset.skipped;const r=row(c);if(r)r.hidden=true;c.hidden=false;}
async function setStatus(c,s,undoing){
  const prev=c.dataset.status;if(prev===s)return;
  setPressed(c,s);c.dataset.status=s;
  if(undoing)expand(c);else if(s==='not_relevant')collapse(c);
  try{
    const r=await fetch('/api/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:c.dataset.id,status:s})});
    if(!r.ok)throw new Error(r.status);
    if(!undoing)undo.push({c,prev});
  }catch(err){setPressed(c,prev);c.dataset.status=prev;if(s==='not_relevant'&&!undoing)expand(c);say('Could not save status ('+err.message+')');}
}
function doUndo(){const u=undo.pop();if(u)setStatus(u.c,u.prev,true);else say('Nothing to undo');}
async function refreshTailor(c){
  try{
    const r=await fetch('/api/tailor/fragment?id='+encodeURIComponent(c.dataset.id));if(!r.ok)return;
    const t=document.createElement('div');t.innerHTML=await r.text();const n=t.firstElementChild,old=$('.tailor',c);
    if(n&&old)old.replaceWith(n);
  }catch(e){}
}
async function startTailor(c,opts){
  try{
    const r=await fetch('/api/tailor',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:c.dataset.id,...(opts||{})})});
    if(!r.ok){const j=await r.json().catch(()=>({}));throw new Error(j.error||r.status);}
    c.dataset.t0=Date.now();await refreshTailor(c);
  }catch(err){say('Could not start: '+err.message);}
}
async function deleteVersions(c,payload,msg){
  if(!confirm(msg))return;
  try{
    const r=await fetch('/api/resume/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:c.dataset.id,...payload})});
    const j=await r.json().catch(()=>({}));
    if(!r.ok)throw new Error(j.error||r.status);
    say(j.deleted.length?('Deleted '+j.deleted.map(n=>'v'+n).join(', ')):'Nothing to delete');await refreshTailor(c);
  }catch(err){say('Could not delete: '+err.message);}
}
function setKb(c){if(cur)cur.classList.remove('kb');cur=c;if(c){c.classList.add('kb');c.scrollIntoView({block:'nearest'});}}
function move(d){
  const vis=cards().filter(c=>!c.hidden);if(!vis.length)return;
  let i=vis.indexOf(cur);i=i<0?(d>0?0:vis.length-1):Math.max(0,Math.min(vis.length-1,i+d));setKb(vis[i]);
}
document.addEventListener('click',e=>{
  const t=e.target;
  const sb=t.closest('.seg button');if(sb){setStatus(sb.closest('.card'),sb.dataset.s,false);return;}
  const a=t.closest('[data-act]');
  if(a){
    const c=a.closest('.card')||(a.closest('.collapsed')&&list.querySelector('.card[data-id="'+CSS.escape(a.closest('.collapsed').dataset.for)+'"]'));
    if(a.dataset.act==='tailor'&&c)startTailor(c);
    else if(a.dataset.act==='regen-go'&&c){
      const box=a.closest('.regen'),notes=$('[data-f=notes]',box).value.trim(),base=$('[data-f=base]',box).value,label=$('[data-f=label]',box).value.trim();
      const o={notes,label};if(notes&&base!=='')o.base=+base;startTailor(c,o);}
    else if(a.dataset.act==='edit-version'&&c){const box=$('.regen',c);if(box){box.open=true;$('[data-f=base]',box).value=a.dataset.n;$('[data-f=notes]',box).focus();}}
    else if(a.dataset.act==='resume-delete'&&c){const d=a.dataset;
      deleteVersions(c,{n:+d.n},'Delete version v'+d.n+(d.pinned==='1'?' (pinned: you labelled it to keep it)':'')+(d.cur==='1'?(d.prev==='1'?', your CURRENT resume? The previous version becomes current':', your only resume? The resume for this job will be removed'):'? ')+'. This cannot be undone.');}
    else if(a.dataset.act==='resume-delete-older'&&c)deleteVersions(c,{all_older:true},'Delete all older versions except the newest and the pinned ones? This cannot be undone.');
    else if(a.dataset.act==='undo')doUndo();
    else if(a.dataset.act==='details'&&c){const d=$('.desc',c),open=d.hidden;d.hidden=!open;a.setAttribute('aria-expanded',String(open));}
    else if(a.dataset.act==='more'){const o=more.classList.toggle('open');a.setAttribute('aria-expanded',String(o));}
    return;
  }
  const ch=t.closest('[data-chip]');
  if(ch){const n=ch.dataset.chip;
    if(n==='triage')ctl.status.value='open';
    else if(n==='interested')ctl.status.value=ctl.status.value==='interested'?'open':'interested';
    else ctl.min.value=+ctl.min.value>=70?0:70;
    apply();return;}
  const fx=t.closest('[data-fix]');
  if(fx){const v=fx.dataset.fix;
    if(v==='clear'){ctl.status.value='all';ctl.min.value=0;ctl.source.value='all';ctl.q.value='';}else ctl.min.value=+v.split(':')[1];
    apply();return;}
  const c=t.closest('.card');if(c&&!t.closest('a'))setKb(c);
});
for(const k of Object.keys(ctl))ctl[k].addEventListener(k==='min'||k==='q'?'input':'change',apply);
more.addEventListener('submit',e=>{e.preventDefault();apply();});
document.addEventListener('keydown',e=>{
  if(e.metaKey||e.ctrlKey||e.altKey||e.target.closest('input,select,textarea,[contenteditable]'))return;
  const k=e.key;
  if(k==='j')move(1);else if(k==='k')move(-1);
  else if(k==='u')doUndo();
  else if(cur&&/^[1-4]$/.test(k))setStatus(cur,['new','interested','applied','not_relevant'][+k-1],false);
  else if(cur&&k==='o'){const a=$('.title',cur);if(a)a.click();}
  else if(cur&&k==='t'){const b=$('[data-act=tailor]',cur);if(b)b.click();else{const g=$('.regen',cur);if(g){g.open=true;const f=$('[data-f=notes]',g);if(f)f.focus();}}}
  else return;
  if('jku1234ot'.includes(k))e.preventDefault();
});
setInterval(()=>{
  for(const c of $$('.card')){
    if(!$('.tailor[data-pending]',c))continue;
    const t0=+(c.dataset.t0||(c.dataset.t0=Date.now()));
    if(Date.now()-t0<300000)refreshTailor(c);
  }
},4000);
apply();
})();
"""


# --- pages -----------------------------------------------------------------------------------------

def last_run(con: sqlite3.Connection):
    r = con.execute("SELECT ts, counts, failed FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if r is None:
        return None
    try:
        counts, failed = json.loads(r["counts"] or "{}"), json.loads(r["failed"] or "[]")
    except ValueError:
        counts, failed = {}, []
    return {"ts": r["ts"], "counts": counts, "failed": failed}


def shell(title: str, header: str, body: str, nonce: str | None) -> str:
    script = f'<script nonce="{nonce}">{JS}</script>' if nonce else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark">'
            f"<title>{e(title)}</title><style>{CSS}</style></head><body>{header}{body}"
            f'<div id="toast" role="status" aria-live="polite" hidden></div>{script}</body></html>')


def gap_strip_text(ranked: list[dict]) -> str:
    top = ranked[:2]
    if not top:
        return ""
    parts = [f"{e(top[0]['name'])} (in {top[0]['n']} job{'s' if top[0]['n'] != 1 else ''})"]
    parts += [f"{e(g['name'])} ({g['n']})" for g in top[1:]]
    return "<strong>Top gap this week:</strong> " + ", ".join(parts)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, min_score: int = Query(0, ge=0, le=100), source: str = "all", status: str = "open",
          sort: str = "score", q: str = Query("", max_length=100)):
    with db() as con:
        sources = [r[0] for r in con.execute("SELECT DISTINCT source FROM jobs ORDER BY source")]
        order = ("COALESCE(score, -1) DESC, first_seen DESC" if sort != "new" else "first_seen DESC, COALESCE(score, -1) DESC")
        rows = con.execute(f"{ROW_SQL} ORDER BY {order}, jobs.id").fetchall()
        counts = dict(con.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
        run = last_run(con)
        gaps = gap_stats(con)
        vers = versions_for(con)
        today = datetime.now(BERLIN).date()
        triaged = sum(1 for r in con.execute("SELECT status_at FROM jobs WHERE status != 'new' AND status_at IS NOT NULL")
                      if (parse_ts(r[0]) or datetime.min.replace(tzinfo=BERLIN)).date() == today)
    source = source if source in sources else "all"
    status = status if status in VIEWS else "open"
    sort = sort if sort in SORTS else "score"
    q = q.strip()
    ql = q.lower()

    def visible(r) -> bool:
        sc = r["score"] or 0
        hay = " ".join(str(r[k] or "") for k in ("title", "company", "location", "description", "reason", "cv_track", "gaps")).lower()
        return sc >= min_score and (source == "all" or r["source"] == source) and status_ok(r["status"], status) and (not ql or ql in hay)

    total = len(rows)
    shown = sum(1 for r in rows if visible(r))
    triage_n = counts.get("new", 0) + counts.get("interested", 0)
    nxt = next_run_text()

    # header
    lr = fmt_last_run(run)
    header = f"""<header class="top"><div class="in">
  <div class="bar1"><div class="brand">{icon("radar", 22)}<h1>Job Radar</h1></div>
    <span class="lastrun"><span class="lr-short">{e(lr[0])}</span><span class="lr-long">{e(lr[1])}</span></span></div>
  <div class="stats"><div class="stat hot"><b>{counts.get('new', 0)}</b><span>new</span></div>
    <div class="stat tracked"><b>{total}</b><span>tracked</span></div>
    <div class="stat"><b>{counts.get('interested', 0)}</b><span>interested</span></div>
    <div class="stat"><b>{counts.get('applied', 0)}</b><span>applied</span></div></div>
</div></header>"""

    strip = (f'<a class="gapstrip" href="/gaps">{icon("chart", 18)}<span>{gap_strip_text(gaps["ranked"])}</span>{icon("chevron")}</a>'
             if gaps["ranked"] else "")

    def opts(pairs, cur):
        return "".join(f'<option value="{e(k)}"{" selected" if k == cur else ""}>{e(v)}</option>' for k, v in pairs)

    filters = f"""<nav class="filters" aria-label="Filters">
  <div class="chips">
    <button type="button" class="cb" data-chip="triage" aria-pressed="{str(status == 'open').lower()}">To triage · {triage_n}</button>
    <button type="button" class="cb" data-chip="s70" aria-pressed="{str(min_score >= 70).lower()}">Score ≥ 70</button>
    <button type="button" class="cb" data-chip="interested" aria-pressed="{str(status == 'interested').lower()}">Interested</button>
    <button type="button" class="cb more-btn" data-act="more" aria-expanded="false" aria-controls="more">{icon("sliders")}More</button>
  </div>
  <form id="more" class="more" method="get" action="/">
    <label class="full">Minimum score: <output id="f-min-out">{min_score}</output><input id="f-min" name="min_score" type="range" min="0" max="100" step="5" value="{min_score}"></label>
    <label>Source<select id="f-source" name="source">{opts([("all", "All sources")] + [(s, s) for s in sources], source)}</select></label>
    <label>Status<select id="f-status" name="status">{opts(list(VIEWS.items()), status)}</select></label>
    <label>Sort<select id="f-sort" name="sort">{opts(list(SORTS.items()), sort)}</select></label>
    <label class="full">Search<input id="f-q" name="q" type="search" value="{e(q, quote=True)}" placeholder="title, company, keyword"></label>
    <noscript><button type="submit" class="btn">Apply</button></noscript>
  </form>
  <p class="showing" id="showing">Showing {shown} of {total}</p>
</nav>"""

    if total == 0:
        cv_ok = radar.CV_TEX.exists()
        main = f"""<main><div class="firstrun">{icon("radar", 56, 1.4)}<h2>The radar hasn't run yet</h2>
<p>The first run is scheduled for <strong>{e(nxt)}</strong>. Jobs will appear here and in your inbox.</p>
<div class="facts"><div><span class="muted">Master CV</span>{'<span class="ok">✓ loaded</span>' if cv_ok else '<span class="bad">not found</span>'}</div>
<div><span class="muted">Sources</span><span>{len(radar.SOURCE_NAMES)} configured</span></div>
<div><span class="muted">Search radius</span><span>{radar.BA_RADIUS_KM} km of Darmstadt</span></div></div></div></main>"""
    else:
        cards = "".join(render_card(r, hidden=not visible(r), versions=vers.get(r["id"])) for r in rows)
        main = f"""<main><div id="list" data-total="{total}" data-triaged="{triaged}">{cards}</div>
<div class="empty" id="empty" hidden><h2></h2><p></p><div class="acts"></div></div>
<p class="endmsg">That's all for today. Next run {e(nxt)}.</p></main>"""

    # sidebar (desktop)
    gbars = "".join(
        f'<div><div class="row2"><span>{e(g["name"])}</span><span class="muted">{g["n"]} job{"s" if g["n"] != 1 else ""}</span></div>'
        f'<div class="gbar"><i style="width:{round(100 * g["n"] / max(1, gaps["ranked"][0]["n"]))}%"></i></div></div>'
        for g in gaps["ranked"][:4]) or '<p class="muted">No skill gaps yet: they appear once jobs are ranked against your CV.</p>'
    if run:
        srcs = "".join(
            f'<div class="row2"><span>{e(s)}</span><span class="{"bad" if s in run["failed"] else "muted"}">'
            f'{"failed" if s in run["failed"] else str(run["counts"].get(s, 0)) + " fetched"}</span></div>'
            for s in radar.SOURCE_NAMES)
    else:
        srcs = '<p class="muted">No run recorded yet; this fills in after the next scheduled run.</p>'
    side = f"""<aside class="side">
<section class="box2" id="gaps"><div class="hd"><h2>Skill gaps, last 7 days</h2><a href="/gaps">Report</a></div>{gbars}
<p class="small muted">Only counts jobs scoring 50+, so gaps reflect jobs you'd actually want.</p></section>
<section class="box2"><h2>Keyboard</h2><dl class="keys">
<dt><kbd>j</kbd><kbd>k</kbd></dt><dd>Next / previous card</dd><dt><kbd>1</kbd>–<kbd>4</kbd></dt><dd>New · Interested · Applied · Not relevant</dd>
<dt><kbd>o</kbd></dt><dd>Open posting</dd><dt><kbd>t</kbd></dt><dd>Tailor resume</dd><dt><kbd>u</kbd></dt><dd>Undo last status</dd></dl>
<p class="small muted">Your status buttons train the ranking. Run <code>radar.py --rescore</code> to re-rank with them.</p></section>
<section class="box2"><h2>Sources, last run</h2>{srcs}</section></aside>"""

    body = f'{strip}<div class="layout"><div class="col">{filters}{main}</div>{side}</div>'
    return HTMLResponse(shell("Job Radar", header, body, request.state.nonce))


def fmt_last_run(run) -> tuple[str, str]:
    if not run:
        return "No run recorded yet", "No run recorded yet"
    d = parse_ts(run["ts"])
    if not d:
        return "", ""
    short = f"{d.strftime('%a')} {d.day} {d.strftime('%b')} · {d:%H:%M}"
    ok = len(radar.SOURCE_NAMES) - len(run["failed"])
    state = (f"{ok} sources OK" if not run["failed"]
             else f"{len(run['failed'])} source{'s' if len(run['failed']) != 1 else ''} failed: {', '.join(run['failed'])}")
    return short, f"Last run {d.strftime('%a')} {d.day} {d.strftime('%b')}, {d:%H:%M} · {state}"


@app.get("/gaps", response_class=HTMLResponse)
def gaps_page(request: Request):
    with db() as con:
        g = gap_stats(con)
    ranked, since = g["ranked"], g["since"]
    today = datetime.now(BERLIN)
    span = f"{since.day}–{today.day} {today.strftime('%b')}" if since and since.month == today.month else (
        f"{since.day} {since.strftime('%b')}–{today.day} {today.strftime('%b')}" if since else "")
    header = f"""<header class="top"><div class="in"><div class="rhead"><a class="back" href="/" aria-label="Back to jobs">{icon("back", 20)}</a>
<div><div class="t">What to learn next</div><div class="s">Last {g['days']} days · {e(span)} · {g['jobs']} job{'s' if g['jobs'] != 1 else ''} scored 50+</div></div></div></div></header>"""
    parts = []
    if not ranked:
        parts.append('<div class="empty"><h2>No skill gaps this week</h2><p>Gaps appear when jobs scoring 50+ ask for skills your CV lacks.</p></div>')
    top = ranked[0]["n"] if ranked else 1
    for i, gp in enumerate(ranked[:3], 1):
        trend = ("New this week." if not gp["prev"] else f"Up from {gp['prev']} last week." if gp["n"] > gp["prev"]
                 else f"Down from {gp['prev']} last week." if gp["n"] < gp["prev"] else "Same as last week.")
        mostly = f"Mostly in {e(gp['track'])} jobs. " if gp["track"] else ""
        sample = "; ".join(e(t[:60]) for t in gp["jobs"][:2])
        parts.append(f"""<section class="gcard"><div class="hd"><span class="rank">#{i}</span><h3>{e(gp['name'])}</h3>
<span class="muted">{gp['n']} job{'s' if gp['n'] != 1 else ''} · avg {gp['avg']}</span></div>
<div class="gbar big"><i style="width:{round(100 * gp['n'] / top)}%"></i></div>
<p class="muted">{mostly}{trend}</p><p class="jobs muted">e.g. {sample}</p>
<p><a href="/?status=all&amp;q={e(quote(gp['name']), quote=True)}">See these jobs</a></p></section>""")
    rest = ranked[3:]
    if rest:
        parts.append('<section class="gcard"><h3 class="muted" style="font-size:13px;letter-spacing:.04em;text-transform:uppercase">Also mentioned</h3><div class="also">'
                     + "".join(f"<span>{e(x['name'])} · {x['n']}</span>" for x in rest[:20]) + "</div></section>")
    parts.append('<p class="small muted">Ranked by number of jobs × their average score, counting jobs scoring 50+ you haven\'t marked not relevant.</p>')
    return HTMLResponse(shell("What to learn next", header, f'<div class="report">{"".join(parts)}</div>', None))


if __name__ == "__main__":
    radar.load_env()
    HOST = os.environ.get("DASHBOARD_HOST", HOST)
    EXTRA_HOSTS[:] = [h.strip().lower() for h in os.environ.get("DASHBOARD_ALLOWED_HOSTS", "").split(",") if h.strip()]
    check_bind_host(HOST)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", server_header=False)
