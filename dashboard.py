#!/usr/bin/env python3
"""Job radar dashboard: browse jobs.db, filter, and track application status.

Binds only to a Tailscale (or loopback) address; refuses anything else.
"""
import html
import json
import re
import ipaddress
import os
import secrets
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))  # run with -I, so add our own dir explicitly

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

import radar

PORT = int(os.environ.get("DASHBOARD_PORT", "8090"))
HOST = os.environ.get("DASHBOARD_HOST", "")
EXTRA_HOSTS = [h.strip().lower() for h in os.environ.get("DASHBOARD_ALLOWED_HOSTS", "").split(",") if h.strip()]

STATUSES = {"new": "New", "interested": "Interested", "applied": "Applied", "not_relevant": "Not relevant"}
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


MAX_QUEUED = 5


@app.post("/api/tailor")
async def request_tailor(body: TailorRequest, request: Request):
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc.lower() not in allowed_hosts():
        return JSONResponse({"error": "bad origin"}, status_code=403)
    if not radar.CV_TEX.exists():
        return JSONResponse({"error": "cv/cv-content.tex is not on the server"}, status_code=409)
    with db() as con:
        if con.execute("SELECT 1 FROM jobs WHERE id = ?", (body.id,)).fetchone() is None:
            return JSONResponse({"error": "unknown job"}, status_code=404)
        row = con.execute("SELECT state FROM tailor_jobs WHERE job_id = ?", (body.id,)).fetchone()
        if row and row["state"] in ("queued", "running"):
            return {"state": row["state"]}
        if con.execute("SELECT COUNT(*) FROM tailor_jobs WHERE state IN ('queued','running')").fetchone()[0] >= MAX_QUEUED:
            return JSONResponse({"error": "queue is full, try again in a few minutes"}, status_code=429)
        con.execute(
            "INSERT INTO tailor_jobs (job_id, state, requested) VALUES (?, 'queued', CURRENT_TIMESTAMP) "
            "ON CONFLICT(job_id) DO UPDATE SET state = 'queued', requested = CURRENT_TIMESTAMP, "
            "finished = NULL, error = NULL, pdf = 0, warnings = NULL", (body.id,))
    return {"state": "queued"}


@app.get("/api/tailor/status")
def tailor_status(id: str):
    with db() as con:
        row = con.execute("SELECT state FROM tailor_jobs WHERE job_id = ?", (id,)).fetchone()
    return {"state": row["state"] if row else None}


FILE_NAME_RE = re.compile(r"^[0-9a-f]{16}\.(pdf|tex)$")


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


def safe_url(u: str) -> str:
    return u if urlsplit(u or "").scheme in ("http", "https") else "#"


def render_md(md: str) -> str:
    """Tiny, safe markdown subset for the tailoring text: escape first, then headings and **bold**."""
    out = []
    for line in md.splitlines():
        line = html.escape(line)
        if line.startswith("## "):
            out.append(f"<h4>{line[3:]}</h4>")
        else:
            out.append(re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line))
    return '<div class="md">' + "\n".join(out) + "</div>"


def render_tailor(r: sqlite3.Row) -> str:
    """The 'Tailor resume' area of a card: button, progress, downloads, warnings, analysis."""
    e = html.escape
    state, token = r["tj_state"], radar.file_token(r["id"])
    if state in ("queued", "running"):
        return ('<div class="tailorbox" data-pending="1"><span class="spin" aria-hidden="true"></span> '
                f'Preparing your resume ({"waiting in queue" if state == "queued" else "working"}), about a minute…</div>')
    parts = []
    if state == "done":
        links = []
        if r["tj_pdf"]:
            links.append(f'<a class="btn" href="/files/{token}.pdf" download>Resume (PDF)</a>')
        links.append(f'<a class="btn" href="/files/{token}.tex" download>LaTeX source</a>')
        parts.append(f'<div class="actions">{"".join(links)}</div>')
        try:
            warnings = json.loads(r["tj_warnings"] or "[]")
        except ValueError:
            warnings = []
        parts += [f'<div class="warn">⚠ {e(w)}</div>' for w in warnings]
    elif state == "failed":
        parts.append(f'<div class="warn">Could not prepare the resume: {e(r["tj_error"] or "unknown error")}</div>')
    if r["tailoring_md"]:
        parts.append(f'<details class="tailor"><summary>Fit analysis &amp; what to learn</summary>{render_md(r["tailoring_md"])}</details>')
    label = "Tailor resume" if state is None else "Regenerate" if state == "done" else "Retry"
    parts.append(f'<button type="button" class="tailor-btn">{label}</button>')
    return f'<div class="tailorbox">{"".join(parts)}</div>'


def render_card(r: sqlite3.Row) -> str:
    e = html.escape
    score = r["score"]
    cls = "na" if score is None else "hi" if score >= 70 else "mid" if score >= 50 else "lo"
    dist = f"{r['distance_km']:g} km" if r["distance_km"] is not None else "–"
    buttons = "".join(
        f'<button type="button" data-status="{k}" aria-pressed="{str(r["status"] == k).lower()}">{v}</button>'
        for k, v in STATUSES.items())
    desc = (r["description"] or "").strip()
    more = f"<details><summary>Details</summary><p>{e(desc[:2000])}</p></details>" if desc else ""
    hint = radar.cv_hint(r)
    cv_line = f'<div class="cv">{e(hint)}</div>' if hint else ""
    tailor = render_tailor(r)
    return f"""<article class="card" data-id="{e(r['id'], quote=True)}" data-status="{e(r['status'], quote=True)}">
  <div class="score {cls}">{'–' if score is None else score}</div>
  <div class="main">
    <a class="title" href="{e(safe_url(r['url']), quote=True)}" target="_blank" rel="noopener noreferrer">{e(r['title'] or '')}</a>
    <div class="meta">{e(r['company'] or '')}</div>
    <div class="meta sub">{e(r['location'] or '')} · {dist} · {e(r['source'] or '')} · {e((r['first_seen'] or '')[:10])}</div>
    <div class="reason">{e(r['reason'] or '')}</div>
    {cv_line}
    {more}
    {tailor}
    <div class="status" role="group" aria-label="Status">{buttons}</div>
  </div>
</article>"""


CSS = """
:root{--bg:#f6f8fa;--card:#fff;--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--accent:#0969da;--hi:#1a7f37;--mid:#9a6700;--lo:#6e7781}
@media(prefers-color-scheme:dark){:root{--bg:#0d1117;--card:#151b23;--fg:#e6edf3;--muted:#9198a1;--line:#2f3742;--accent:#4493f8;--hi:#3fb950;--mid:#d29922;--lo:#8b949e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.4 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:12px 16px 40px}
h1{font-size:20px;margin:8px 0 2px}.count{color:var(--muted);font-size:14px;margin-bottom:10px}
form.filters{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:14px}
form.filters label{font-size:12px;color:var(--muted);display:flex;flex-direction:column;gap:3px}
form.filters .full{grid-column:1/-1}
select,input[type=range],input[type=search]{width:100%;min-height:40px;font:inherit;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:0 8px}
input[type=range]{padding:0;accent-color:var(--accent)}
.card{display:flex;gap:12px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:10px}
.card[data-status=not_relevant]{opacity:.5}
.score{flex:0 0 44px;height:44px;border-radius:22px;color:#fff;font-weight:700;display:grid;place-items:center}
.score.hi{background:var(--hi)}.score.mid{background:var(--mid)}.score.lo,.score.na{background:var(--lo)}
.main{min-width:0;flex:1}.title{color:var(--accent);font-weight:600;text-decoration:none;overflow-wrap:anywhere}
.meta{font-size:14px;margin-top:2px;overflow-wrap:anywhere}.sub{color:var(--muted);font-size:13px}
.reason{font-size:13px;color:var(--muted);font-style:italic;margin-top:6px}
.cv{font-size:12px;color:var(--accent);margin-top:6px;overflow-wrap:anywhere}
.tailorbox{margin-top:10px;display:flex;flex-direction:column;gap:8px;font-size:13px;color:var(--muted)}
.actions{display:flex;flex-wrap:wrap;gap:8px}
.btn,.tailor-btn{display:inline-flex;align-items:center;justify-content:center;min-height:40px;padding:0 14px;font:inherit;font-size:13px;font-weight:600;border-radius:8px;border:1px solid var(--accent);color:var(--accent);background:transparent;text-decoration:none;cursor:pointer}
.actions .btn:first-child{background:var(--accent);color:#fff}
.tailor-btn{align-self:flex-start}.tailor-btn:disabled{opacity:.5;cursor:default}
.warn{font-size:12px;color:var(--mid);overflow-wrap:anywhere}
.spin{width:14px;height:14px;border:2px solid var(--line);border-top-color:var(--accent);border-radius:50%;display:inline-block;vertical-align:-2px;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
@media(prefers-reduced-motion:reduce){.spin{animation:none}}
.md{white-space:pre-wrap;overflow-wrap:anywhere;color:var(--fg);line-height:1.5}.md h4{margin:10px 0 2px;font-size:14px}
details{margin-top:6px;font-size:13px;color:var(--muted)}summary{cursor:pointer;min-height:24px}details p{white-space:pre-wrap;overflow-wrap:anywhere}
.status{display:grid;grid-template-columns:repeat(2,1fr);gap:6px;margin-top:10px}
@media(min-width:560px){.status{grid-template-columns:repeat(4,1fr)}}
.status button{min-height:40px;font:inherit;font-size:13px;border:1px solid var(--line);background:transparent;color:var(--fg);border-radius:8px;cursor:pointer}
.status button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#fff}
.empty{text-align:center;color:var(--muted);padding:40px 0}
"""

JS = """
const f=document.querySelector('form.filters');
f.addEventListener('change',()=>f.submit());
document.addEventListener('click',async e=>{
  const b=e.target.closest('button[data-status]');if(!b)return;
  const card=b.closest('.card');b.disabled=true;
  try{
    const r=await fetch('/api/status',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id:card.dataset.id,status:b.dataset.status})});
    if(!r.ok)throw new Error(r.status);
    card.dataset.status=b.dataset.status;
    card.querySelectorAll('button[data-status]').forEach(x=>x.setAttribute('aria-pressed',x===b));
  }catch(err){alert('Could not save status ('+err.message+')');}
  finally{b.disabled=false;}
});
document.addEventListener('click',async e=>{
  const t=e.target.closest('.tailor-btn');if(!t)return;
  const card=t.closest('.card');t.disabled=true;
  try{
    const r=await fetch('/api/tailor',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id:card.dataset.id})});
    if(!r.ok){const j=await r.json().catch(()=>({}));throw new Error(j.error||r.status);}
    location.reload();
  }catch(err){alert('Could not start: '+err.message);t.disabled=false;}
});
const pending=[...document.querySelectorAll('.tailorbox[data-pending]')];
if(pending.length){
  setInterval(async()=>{
    for(const box of pending){
      const id=box.closest('.card').dataset.id;
      try{
        const j=await(await fetch('/api/tailor/status?id='+encodeURIComponent(id))).json();
        if(j.state!=='queued'&&j.state!=='running'){location.reload();return;}
      }catch(e){}
    }
  },4000);
}
"""


@app.get("/", response_class=HTMLResponse)
def index(request: Request, min_score: int = Query(0, ge=0, le=100),
          source: str = "all", status: str = "active",
          sort: str = "score", q: str = Query("", max_length=100)):
    e = html.escape
    with db() as con:
        sources = [r[0] for r in con.execute("SELECT DISTINCT source FROM jobs ORDER BY source")]
    source = source if source in sources else "all"
    status = status if status in STATUSES or status == "all" else "active"
    where, args = ["COALESCE(score, 0) >= ?"], [min_score]
    if source != "all":
        where.append("source = ?"); args.append(source)
    q = q.strip()
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(title LIKE ? ESCAPE '\\' OR company LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')")
        args += [like, like, like]
    order = ("COALESCE(score, -1) DESC, first_seen DESC" if sort == "score"
             else "first_seen DESC, COALESCE(score, -1) DESC")
    if status == "active":
        where.append("status != 'not_relevant'")
    elif status != "all":
        where.append("status = ?"); args.append(status)
    with db() as con:
        rows = con.execute(
            "SELECT jobs.*, t.markdown AS tailoring_md, tj.state AS tj_state, tj.pdf AS tj_pdf, "
            "tj.warnings AS tj_warnings, tj.error AS tj_error FROM jobs "
            "LEFT JOIN tailoring t ON t.job_id = jobs.id LEFT JOIN tailor_jobs tj ON tj.job_id = jobs.id "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY {order}, id", args).fetchall()
        total = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        counts = dict(con.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())

    def opts(pairs, cur):
        return "".join(f'<option value="{e(k)}"{" selected" if k == cur else ""}>{e(v)}</option>' for k, v in pairs)

    cards = "".join(render_card(r) for r in rows) or '<div class="empty">No jobs match these filters.</div>'
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark">
<title>Job radar</title><style>{CSS}</style></head><body><div class="wrap">
<h1>Job radar</h1><div class="count">{len(rows)} shown · {total} total · {counts.get('interested', 0)} interested · {counts.get('applied', 0)} applied · {counts.get('not_relevant', 0)} dismissed <span title="Your buttons train the ranking: run radar.py --rescore to re-rank with them">· feedback improves ranking</span></div>
<form class="filters" method="get">
  <label class="full">Min score: {min_score}<input type="range" name="min_score" min="0" max="100" step="5" value="{min_score}"></label>
  <label class="full">Search<input type="search" name="q" value="{e(q, quote=True)}" placeholder="title, company, keyword"></label>
  <label>Sort<select name="sort">{opts([("score", "Best fit first"), ("new", "Newest first")], sort if sort in ("score", "new") else "score")}</select></label>
  <label>Source<select name="source">{opts([("all", "All")] + [(s, s) for s in sources], source)}</select></label>
  <label>Status<select name="status">{opts([("active", "Active (hide not relevant)"), ("all", "All")] + list(STATUSES.items()), status)}</select></label>
</form>
{cards}
</div><script nonce="{request.state.nonce}">{JS}</script></body></html>"""
    return HTMLResponse(page)


if __name__ == "__main__":
    radar.load_env()
    HOST = os.environ.get("DASHBOARD_HOST", HOST)
    EXTRA_HOSTS[:] = [h.strip().lower() for h in os.environ.get("DASHBOARD_ALLOWED_HOSTS", "").split(",") if h.strip()]
    check_bind_host(HOST)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", server_header=False)
