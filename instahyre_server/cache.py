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

-- The inbound watch: what has already been shown to a human, per stream.
--
-- DELIBERATELY NOT THE ``kv`` TABLE, which is a TTL cache. A watermark that
-- silently expires makes the next read report the whole queue as new, so the
-- one time the operator most needs a small honest answer he gets 227 items and
-- learns to ignore the tool. This is bookkeeping, not a cache; it never expires
-- and it is only ever cleared on request.
CREATE TABLE IF NOT EXISTS watch_seen (
    stream      TEXT NOT NULL,
    identity    TEXT NOT NULL,
    first_seen  REAL NOT NULL,
    PRIMARY KEY (stream, identity)
);
CREATE INDEX IF NOT EXISTS watch_seen_stream_idx ON watch_seen (stream, first_seen);

CREATE TABLE IF NOT EXISTS watch_meta (
    stream        TEXT PRIMARY KEY,
    baselined_at  REAL,
    last_checked  REAL,
    last_advanced REAL,
    last_new      INTEGER
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

    # -- inbound watch -----------------------------------------------------
    #
    # A watermark, not a cache. See the ``watch_seen`` comment in the schema
    # for why these are separate tables from ``kv``.

    def watch_baselined(self, stream: str) -> Optional[float]:
        """When this stream was first baselined, or None if it never was.

        THE DISTINCTION THIS EXISTS FOR: a stream with no rows in
        ``watch_seen`` is ambiguous between "never looked" and "looked, and the
        stream was genuinely empty". Those want opposite answers -- the first
        must not report its whole backlog as news, the second must report a
        real zero -- and a row count cannot tell them apart. The timestamp can.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT baselined_at FROM watch_meta WHERE stream=?", (stream,)
            ).fetchone()
        return row["baselined_at"] if row else None

    def watch_unseen(self, stream: str, identities: Iterable[str]) -> list[str]:
        """Which of ``identities`` this stream has never recorded, IN ORDER.

        Order is the caller's, not the database's: the queue arrives ranked and
        a watcher that reshuffled it would be answering a different question.
        Duplicates within one call are collapsed, since the same item twice is
        one piece of news.
        """
        wanted = list(dict.fromkeys(str(i) for i in identities))
        if not wanted:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT identity FROM watch_seen WHERE stream=? AND identity IN (%s)"
                % ",".join("?" * len(wanted)),
                [stream] + wanted,
            ).fetchall()
        known = {r["identity"] for r in rows}
        return [i for i in wanted if i not in known]

    def watch_record(self, stream: str, identities: Iterable[str]) -> int:
        """Mark ``identities`` seen. Returns how many were NEW to the table.

        ``INSERT OR IGNORE`` keeps the ORIGINAL ``first_seen`` for anything
        already there. Refreshing it would erase the only date this server has
        for when an opportunity actually arrived -- the API publishes none --
        and that date is the whole reason the table exists.

        AN EMPTY ``identities`` STILL MARKS THE STREAM BASELINED, and that is
        the load-bearing half. "I looked and there was nothing" is a baseline;
        treating it as a no-op leaves ``baselined_at`` null, so every later call
        re-baselines -- and the FIRST opportunity ever to arrive gets swallowed
        as "the baseline" instead of reported as the news it is. An account
        whose queue starts empty is exactly the account that most needs to hear
        about its first match, so the early return this replaces was worst
        precisely where it mattered most.
        """
        rows = list(dict.fromkeys(str(i) for i in identities))
        now = time.time()
        with self._lock:
            written = 0
            if rows:
                cur = self._conn.executemany(
                    "INSERT OR IGNORE INTO watch_seen (stream, identity, first_seen) "
                    "VALUES (?,?,?)",
                    [(stream, identity, now) for identity in rows],
                )
                written = cur.rowcount or 0
            self._conn.execute(
                "INSERT INTO watch_meta (stream, baselined_at, last_advanced) "
                "VALUES (?,?,?) "
                "ON CONFLICT(stream) DO UPDATE SET "
                "  baselined_at = COALESCE(watch_meta.baselined_at, excluded.baselined_at), "
                "  last_advanced = excluded.last_advanced",
                (stream, now, now),
            )
            self._conn.commit()
        return written if written > 0 else 0

    def watch_touch(self, stream: str, new_count: int) -> None:
        """Record that the stream was CHECKED, whether or not anything moved.

        Separate from :meth:`watch_record` on purpose. "Last checked" and "last
        advanced" answer different questions -- a watcher read ten times with
        nothing new has one recent check and one old advance -- and collapsing
        them would make a quiet week look like a broken tool.
        """
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO watch_meta (stream, last_checked, last_new) VALUES (?,?,?) "
                "ON CONFLICT(stream) DO UPDATE SET "
                "  last_checked = excluded.last_checked, last_new = excluded.last_new",
                (stream, now, int(new_count)),
            )
            self._conn.commit()

    def watch_stats(self, stream: str) -> dict:
        """Everything recorded about one stream's watch state."""
        with self._lock:
            meta = self._conn.execute(
                "SELECT baselined_at, last_checked, last_advanced, last_new "
                "FROM watch_meta WHERE stream=?",
                (stream,),
            ).fetchone()
            counted = self._conn.execute(
                "SELECT COUNT(*) AS n, MIN(first_seen) AS oldest, MAX(first_seen) AS newest "
                "FROM watch_seen WHERE stream=?",
                (stream,),
            ).fetchone()
        out = {
            "stream": stream,
            "baselined_at": None,
            "last_checked": None,
            "last_advanced": None,
            "last_new": None,
            "known": counted["n"] if counted else 0,
            "oldest_first_seen": counted["oldest"] if counted else None,
            "newest_first_seen": counted["newest"] if counted else None,
        }
        if meta:
            out.update(
                {
                    "baselined_at": meta["baselined_at"],
                    "last_checked": meta["last_checked"],
                    "last_advanced": meta["last_advanced"],
                    "last_new": meta["last_new"],
                }
            )
        return out

    def watch_forget(self, stream: str) -> int:
        """Drop everything remembered for one stream. Returns rows removed.

        The next read after this reports a fresh baseline, NOT a flood of news
        -- which is the behaviour that makes forgetting safe to offer at all.
        """
        with self._lock:
            cur = self._conn.execute("DELETE FROM watch_seen WHERE stream=?", (stream,))
            removed = cur.rowcount or 0
            self._conn.execute("DELETE FROM watch_meta WHERE stream=?", (stream,))
            self._conn.commit()
        return removed


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
