#!/usr/bin/env python3
"""Background worker for the dashboard's "Tailor resume" button.

The dashboard is sandboxed and cannot run `claude`, so it only enqueues rows in tailor_jobs; this process
(no network exposure, runs as the user) claims them one at a time and runs cv.run_request().
"""
import json
import signal
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # run with -I, so add our own dir explicitly

import cv
import radar

POLL_SECONDS = 3
_stop = False


def _on_signal(signum, _frame):
    global _stop
    _stop = True
    radar.log(f"signal {signum}: finishing the current job, then exiting")


def claim(db: sqlite3.Connection) -> str | None:
    row = db.execute("SELECT job_id FROM tailor_jobs WHERE state = 'queued' ORDER BY requested LIMIT 1").fetchone()
    if row is None:
        return None
    cur = db.execute("UPDATE tailor_jobs SET state = 'running', error = NULL WHERE job_id = ? AND state = 'queued'",
                     (row["job_id"],))
    db.commit()
    return row["job_id"] if cur.rowcount else None


def finish(db: sqlite3.Connection, job_id: str, state: str, pdf: int = 0, warnings: list | None = None,
           error: str | None = None) -> None:
    db.execute("UPDATE tailor_jobs SET state = ?, finished = CURRENT_TIMESTAMP, pdf = ?, warnings = ?, error = ? "
               "WHERE job_id = ?", (state, pdf, json.dumps(warnings or [], ensure_ascii=False), error, job_id))
    db.commit()


def main() -> int:
    radar.load_env()
    db = radar.open_db()
    # A crash or restart mid-job leaves 'running' rows behind: put them back in the queue.
    db.execute("UPDATE tailor_jobs SET state = 'queued' WHERE state = 'running'")
    db.commit()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    radar.log("tailor worker started")
    while not _stop:
        job_id = claim(db)
        if job_id is None:
            time.sleep(POLL_SECONDS)
            continue
        radar.log(f"tailoring {job_id}")
        try:
            res = cv.run_request(db, job_id)
            finish(db, job_id, "done", int(res["pdf"]), res["warnings"])
            radar.log(f"done {job_id}: pdf={res['pdf']} warnings={len(res['warnings'])}")
        except Exception as e:  # one bad job must not stop the worker
            finish(db, job_id, "failed", error=str(e)[:300])
            radar.log(f"failed {job_id}: {e!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
