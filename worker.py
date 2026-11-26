#!/usr/bin/env python3
"""Background worker for the dashboard's "Tailor resume" button.

The dashboard is sandboxed and cannot run `claude`, so it only enqueues rows in tailor_jobs; this process
(no network exposure, runs as the user) claims them one at a time and runs cv.run_request().
"""
import json
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # run with -I, so add our own dir explicitly

import cv
import radar

POLL_SECONDS = 3
HEARTBEAT_SECONDS = 10
STALE_AFTER_SECONDS = 600 + 60
_stop = False


def _on_signal(signum, _frame):
    global _stop
    _stop = True
    radar.log(f"signal {signum}: finishing the current job, then exiting")


def _utc_stamp(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def heartbeat(db: sqlite3.Connection, now: datetime | None = None) -> None:
    db.execute("INSERT INTO worker_heartbeat (id, updated) VALUES (1, ?) "
               "ON CONFLICT(id) DO UPDATE SET updated = excluded.updated", (_utc_stamp(now),))
    db.commit()


def requeue_stale(db: sqlite3.Connection, now: datetime | None = None) -> int:
    cutoff = _utc_stamp((now or datetime.now(timezone.utc)) - timedelta(seconds=STALE_AFTER_SECONDS))
    cur = db.execute("UPDATE tailor_jobs SET state = 'queued', started = NULL "
                     "WHERE state = 'running' AND COALESCE(started, requested) < ?", (cutoff,))
    db.commit()
    return cur.rowcount


def claim(db: sqlite3.Connection, now: datetime | None = None) -> tuple[str, int] | None:
    row = db.execute("SELECT job_id FROM tailor_jobs WHERE state = 'queued' ORDER BY requested LIMIT 1").fetchone()
    if row is None:
        return None
    cur = db.execute("UPDATE tailor_jobs SET state = 'running', started = ?, attempt = attempt + 1, error = NULL "
                     "WHERE job_id = ? AND state = 'queued'", (_utc_stamp(now), row["job_id"]))
    attempt = None
    if cur.rowcount:
        attempt = db.execute("SELECT attempt FROM tailor_jobs WHERE job_id = ?", (row["job_id"],)).fetchone()["attempt"]
    db.commit()
    return (row["job_id"], attempt) if attempt is not None else None


def finish(db: sqlite3.Connection, job_id: str, attempt: int, state: str, pdf: int = 0, warnings: list | None = None,
           error: str | None = None) -> None:
    db.execute("UPDATE tailor_jobs SET state = ?, finished = CURRENT_TIMESTAMP, pdf = ?, warnings = ?, error = ? "
               "WHERE job_id = ? AND state = 'running' AND attempt = ?",
               (state, pdf, json.dumps(warnings or [], ensure_ascii=False), error, job_id, attempt))
    db.commit()


def _heartbeat_loop(stop: threading.Event) -> None:
    db = radar.open_db()
    try:
        while not stop.is_set():
            heartbeat(db)
            requeue_stale(db)
            stop.wait(HEARTBEAT_SECONDS)
    finally:
        db.close()


def main() -> int:
    radar.load_env()
    db = radar.open_db()
    requeue_stale(db)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    radar.log("tailor worker started")
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(target=_heartbeat_loop, args=(heartbeat_stop,), daemon=True)
    heartbeat_thread.start()
    while not _stop:
        heartbeat(db)
        claimed = claim(db)
        if claimed is None:
            time.sleep(POLL_SECONDS)
            continue
        job_id, attempt = claimed
        radar.log(f"tailoring {job_id}")
        try:
            req = db.execute("SELECT notes, base_n, label FROM tailor_jobs "
                             "WHERE job_id = ? AND state = 'running' AND attempt = ?", (job_id, attempt)).fetchone()
            if req is None:
                if db.execute("SELECT 1 FROM tailor_jobs WHERE job_id = ?", (job_id,)).fetchone() is None:
                    raise RuntimeError("the request disappeared")
                continue
            res = cv.run_request(db, job_id, req["notes"] or "", req["base_n"], req["label"])
            finish(db, job_id, attempt, "done", int(res["pdf"]), res["warnings"])
            radar.log(f"done {job_id}: pdf={res['pdf']} warnings={len(res['warnings'])}")
        except Exception as e:  # one bad job must not stop the worker
            finish(db, job_id, attempt, "failed", error=str(e)[:300])
            radar.log(f"failed {job_id}: {e!r}")
    heartbeat_stop.set()
    heartbeat_thread.join(timeout=HEARTBEAT_SECONDS + 1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
