"""Disk state: a TTL cache, a job index, and the corpus tracker.

Three tables, one sqlite file, all of them serving one idea -- the API gives us
no posting date, so *we* have to be the clock. A job's ``first_seen`` in this
index is the closest thing to a "posted on" that will ever exist for Instahyre,
and it only becomes true if we start writing it down now.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

DEFAULT_DB_NAME = "instahyre.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    namespace  TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    stored_at  REAL NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (namespace, key)
);

CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY,
    title         TEXT,
    company       TEXT,
    company_id    INTEGER,
    company_size  INTEGER,
    founded       INTEGER,
    locations     TEXT,
    skills        TEXT,
    first_seen    REAL NOT NULL,
    last_seen     REAL NOT NULL,
    is_agency     INTEGER,
    agency_name   TEXT,
    workex_min    INTEGER,
    workex_max    INTEGER,
    detail_at     REAL
);
CREATE INDEX IF NOT EXISTS jobs_company_idx ON jobs (company);
CREATE INDEX IF NOT EXISTS jobs_first_seen_idx ON jobs (first_seen);

CREATE TABLE IF NOT EXISTS corpus (
    ts           REAL NOT NULL,
    label        TEXT NOT NULL,
    total_count  INTEGER,
    max_id       INTEGER,
    PRIMARY KEY (ts, label)
);
"""


def default_db_path() -> Path:
    """Where state lives. ``INSTAHYRE_HOME`` overrides; otherwise next to the package."""
    env = os.environ.get("INSTAHYRE_HOME")
    base = Path(env) if env else Path(__file__).resolve().parent.parent / "_state"
    base.mkdir(parents=True, exist_ok=True)
    return base / DEFAULT_DB_NAME


class Store:
    """Thread-safe sqlite wrapper. One per process is plenty."""

    def __init__(self, path: Optional[Path | str] = None) -> None:
        self.path = Path(path) if path else default_db_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- TTL cache ---------------------------------------------------------

    def get(self, namespace: str, key: str) -> Optional[Any]:
        """Return the cached value, or None if absent or expired."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value, expires_at FROM kv WHERE namespace=? AND key=?",
                (namespace, key),
            ).fetchone()
        if row is None or row["expires_at"] < time.time():
            return None
        try:
            return json.loads(row["value"])
        except ValueError:
            return None

    def put(self, namespace: str, key: str, value: Any, ttl: float) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO kv (namespace, key, value, stored_at, expires_at) "
                "VALUES (?,?,?,?,?)",
                (namespace, key, json.dumps(value), now, now + ttl),
            )
            self._conn.commit()

    def purge_expired(self) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM kv WHERE expires_at < ?", (time.time(),))
            self._conn.commit()
            return cur.rowcount

    # -- job index ---------------------------------------------------------

    def upsert_jobs(self, records: Iterable[dict]) -> tuple[int, int]:
        """Record what we just saw. Returns ``(new_count, seen_count)``.

        ``first_seen`` is written once and never touched again -- that column is
        the whole point of this table.
        """
        now = time.time()
        new = 0
        seen = 0
        with self._lock:
            for rec in records:
                seen += 1
                job_id = rec.get("id")
                if job_id is None:
                    continue
                exists = self._conn.execute(
                    "SELECT 1 FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
                if exists:
                    self._conn.execute(
                        "UPDATE jobs SET last_seen=?, title=?, company=?, company_id=?, "
                        "company_size=?, founded=?, locations=?, skills=? WHERE id=?",
                        (
                            now,
                            rec.get("title"),
                            rec.get("company"),
                            rec.get("company_id"),
                            rec.get("company_size"),
                            rec.get("founded"),
                            json.dumps(rec.get("locations") or []),
                            json.dumps(rec.get("skills") or []),
                            job_id,
                        ),
                    )
                else:
                    new += 1
                    self._conn.execute(
                        "INSERT INTO jobs (id, title, company, company_id, company_size, "
                        "founded, locations, skills, first_seen, last_seen) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            job_id,
                            rec.get("title"),
                            rec.get("company"),
                            rec.get("company_id"),
                            rec.get("company_size"),
                            rec.get("founded"),
                            json.dumps(rec.get("locations") or []),
                            json.dumps(rec.get("skills") or []),
                            now,
                            now,
                        ),
                    )
            self._conn.commit()
        return new, seen

    def record_agency_flag(
        self,
        job_id: int,
        is_agency: bool,
        agency_name: Optional[str] = None,
        workex_min: Optional[int] = None,
        workex_max: Optional[int] = None,
    ) -> None:
        """Remember a detail-only fact so the next search does not re-fetch it."""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, first_seen, last_seen, is_agency, agency_name, "
                "workex_min, workex_max, detail_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET is_agency=excluded.is_agency, "
                "agency_name=excluded.agency_name, workex_min=excluded.workex_min, "
                "workex_max=excluded.workex_max, detail_at=excluded.detail_at",
                (job_id, now, now, int(is_agency), agency_name, workex_min, workex_max, now),
            )
            self._conn.commit()

    def agency_flags(self, job_ids: Iterable[int], max_age: float) -> dict[int, dict]:
        """Look up cached agency verdicts that are still fresh enough to trust."""
        ids = list(job_ids)
        if not ids:
            return {}
        cutoff = time.time() - max_age
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, is_agency, agency_name, workex_min, workex_max FROM jobs "
                f"WHERE id IN ({placeholders}) AND is_agency IS NOT NULL AND detail_at >= ?",
                (*ids, cutoff),
            ).fetchall()
        return {
            row["id"]: {
                "is_agency": bool(row["is_agency"]),
                "agency_name": row["agency_name"],
                "workex_min": row["workex_min"],
                "workex_max": row["workex_max"],
            }
            for row in rows
        }

    def jobs_first_seen_after(self, since: float, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE first_seen >= ? ORDER BY first_seen DESC LIMIT ?",
                (since, limit),
            ).fetchall()
        return [_job_row_to_dict(r) for r in rows]

    def index_stats(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n, MIN(first_seen) AS oldest, MAX(last_seen) AS newest, "
                "COUNT(is_agency) AS with_agency FROM jobs"
            ).fetchone()
            companies = self._conn.execute(
                "SELECT COUNT(DISTINCT company) AS n FROM jobs WHERE company IS NOT NULL"
            ).fetchone()
        return {
            "jobs_indexed": row["n"],
            "distinct_companies": companies["n"],
            "agency_flags_cached": row["with_agency"],
            "oldest_first_seen": row["oldest"],
            "newest_last_seen": row["newest"],
        }

    # -- corpus tracker ----------------------------------------------------

    def record_corpus(self, label: str, total_count: Optional[int], max_id: Optional[int]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO corpus (ts, label, total_count, max_id) VALUES (?,?,?,?)",
                (time.time(), label, total_count, max_id),
            )
            self._conn.commit()

    def corpus_history(self, label: str = "all", limit: int = 30) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, total_count, max_id FROM corpus WHERE label=? ORDER BY ts DESC LIMIT ?",
                (label, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def _job_row_to_dict(row: sqlite3.Row) -> dict:
    out = dict(row)
    for key in ("locations", "skills"):
        raw = out.get(key)
        if isinstance(raw, str):
            try:
                out[key] = json.loads(raw)
            except ValueError:
                out[key] = []
    if out.get("is_agency") is not None:
        out["is_agency"] = bool(out["is_agency"])
    return out
