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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # run with -I, so add our own dir explicitly

import cv
import radar

POLL_SECONDS = 3
HEARTBEAT_SECONDS = 10
_stop = False


def _on_signal(signum, _frame):
    global _stop
    _stop = True
    radar.log(f"signal {signum}: finishing the current job, then exiting")


def claim(db: sqlite3.Connection) -> str | None:
    row = db.execute("SELECT job_id FROM tailor_jobs WHERE state = 'queued' ORDER BY requested LIMIT 1").fetchone()
    if row is None:
        return None
    cur = db.execute("UPDATE tailor_jobs SET state = 'running', error = NULL, started = CURRENT_TIMESTAMP "
                     "WHERE job_id = ? AND state = 'queued'", (row["job_id"],))
    db.commit()
    return row["job_id"] if cur.rowcount else None


def finish(db: sqlite3.Connection, job_id: str, state: str, pdf: int = 0, warnings: list | None = None,
           error: str | None = None) -> None:
    db.execute("UPDATE tailor_jobs SET state = ?, finished = CURRENT_TIMESTAMP, pdf = ?, warnings = ?, error = ? "
               "WHERE job_id = ?", (state, pdf, json.dumps(warnings or [], ensure_ascii=False), error, job_id))
    db.commit()


def heartbeat_loop(stop: threading.Event) -> None:
    """Report 'alive' every few seconds from a separate connection, so a long tailoring job (up to ~10 minutes) never
    makes the dashboard think the worker is offline."""
    con = radar.open_db()
    try:
        while not stop.is_set():
            try:
                radar.beat(con)
            except sqlite3.OperationalError:  # database busy: the next beat will do
                pass
            stop.wait(HEARTBEAT_SECONDS)
    finally:
        con.close()


def main() -> int:
    radar.load_env()
    db = radar.open_db()
    stop_beat = threading.Event()
    threading.Thread(target=heartbeat_loop, args=(stop_beat,), daemon=True, name="heartbeat").start()
    # A crash or restart mid-job leaves 'running' rows behind: put them back in the queue.
    db.execute("UPDATE tailor_jobs SET state = 'queued' WHERE state = 'running'")
    db.commit()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    radar.log("tailor worker started")
    while not _stop:
        for stale in radar.requeue_stale(db):
            radar.log(f"requeued {stale}: it was 'running' for more than {radar.RUNNING_STALE_SECONDS} s")
        job_id = claim(db)
        if job_id is None:
            time.sleep(POLL_SECONDS)
            continue  # the heartbeat thread keeps reporting meanwhile
        radar.log(f"tailoring {job_id}")
        try:
            req = db.execute("SELECT notes, base_n, label FROM tailor_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if req is None:
                raise RuntimeError("the request disappeared")
            res = cv.run_request(db, job_id, req["notes"] or "", req["base_n"], req["label"])
            finish(db, job_id, "done", int(res["pdf"]), res["warnings"])
            radar.log(f"done {job_id}: pdf={res['pdf']} warnings={len(res['warnings'])}")
        except Exception as e:  # one bad job must not stop the worker
            finish(db, job_id, "failed", error=str(e)[:300])
            radar.log(f"failed {job_id}: {e!r}")
    stop_beat.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
