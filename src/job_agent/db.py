import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import DB_PATH, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    title TEXT, company TEXT, location TEXT, url TEXT,
    apply_type TEXT, description TEXT,
    keywords TEXT, match_score REAL,
    original_coverage REAL, tailored_coverage REAL, missing TEXT,
    changes TEXT, tailored_pdf TEXT,
    status TEXT DEFAULT 'matched', notes TEXT,
    created_at TEXT, updated_at TEXT
)
"""

JSON_FIELDS = ("keywords", "missing")


@contextmanager
def connect():
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    for f in JSON_FIELDS:
        d[f] = json.loads(d[f]) if d.get(f) else []
    return d


def upsert_job(job_id: str, **fields) -> None:
    for f in JSON_FIELDS:
        if f in fields and not isinstance(fields[f], str):
            fields[f] = json.dumps(fields[f])
    now = _now()
    with connect() as c:
        exists = c.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if exists:
            cols = ", ".join(f"{k}=?" for k in fields)
            c.execute(
                f"UPDATE jobs SET {cols}, updated_at=? WHERE job_id=?",
                (*fields.values(), now, job_id),
            )
        else:
            keys = ["job_id", *fields, "created_at", "updated_at"]
            vals = [job_id, *fields.values(), now, now]
            c.execute(
                f"INSERT INTO jobs ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                vals,
            )


def get_job(job_id: str) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    return _row(r) if r else None


def list_jobs(status: str | None = None) -> list[dict]:
    with connect() as c:
        if status:
            rows = c.execute(
                "SELECT * FROM jobs WHERE status=? ORDER BY match_score DESC", (status,)
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM jobs ORDER BY match_score DESC").fetchall()
    return [_row(r) for r in rows]
