"""Shared test helpers: project on sys.path, a throwaway database, and sqlite3.Row stand-ins.

Tests never touch the real jobs.db, the CV or the network."""
import atexit
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import radar  # noqa: E402


class Row(dict):
    """Stand-in for sqlite3.Row: a missing column reads as None."""

    def __getitem__(self, key):
        return self.get(key)


def fresh_db():
    """Point radar at an empty temporary database; keep the returned object alive for the test."""
    tmp = tempfile.TemporaryDirectory()
    atexit.register(tmp.cleanup)  # stays alive (and tidy) until the test run ends
    radar.DB_PATH = Path(tmp.name) / "jobs.db"
    radar.open_db().close()
    return tmp


def insert_job(db, **kw):
    """Insert a fake job and return it as a real sqlite3.Row."""
    row = {"id": "t:1", "source": "BA", "title": "Werkstudent Python", "company": "Beispiel GmbH", "location": "Darmstadt",
           "distance_km": 3.0, "url": "https://example.org/job/1", "description": "Python and SQL", "score": 80,
           "reason": "Fits well.", "cv_track": "swe", "gaps": '["Docker"]', "status": "new"}
    row.update(kw)
    cols = ", ".join(row)
    db.execute(f"INSERT INTO jobs ({cols}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
    db.commit()
    return db.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
