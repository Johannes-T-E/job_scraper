"""SQLite storage for scraped Finn.no job listings."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "jobs.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    finn_code TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT,
    employer TEXT,
    deadline TEXT,
    employment_type TEXT,
    sector TEXT,
    location_text TEXT,
    lat REAL,
    lon REAL,
    last_changed_iso TEXT,
    scraped_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_employer ON jobs(employer);
CREATE INDEX IF NOT EXISTS idx_jobs_scraped_at ON jobs(scraped_at);

CREATE TABLE IF NOT EXISTS scrapes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    urls_discovered INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scrape_jobs (
    scrape_id INTEGER NOT NULL,
    finn_code TEXT NOT NULL,
    url TEXT NOT NULL,
    PRIMARY KEY (scrape_id, finn_code),
    FOREIGN KEY (scrape_id) REFERENCES scrapes(id)
);

CREATE INDEX IF NOT EXISTS idx_scrape_jobs_finn_code ON scrape_jobs(finn_code);
"""

_LIFECYCLE_COLUMNS = (
    ("first_seen_at", "TEXT"),
    ("last_seen_at", "TEXT"),
    ("is_active", "INTEGER NOT NULL DEFAULT 1"),
    ("deactivated_at", "TEXT"),
)


def default_db_path() -> Path:
    return DEFAULT_DB_PATH


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = db_path or default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


def _migrate_jobs_lifecycle(conn: sqlite3.Connection) -> None:
    for column, definition in _LIFECYCLE_COLUMNS:
        _add_column_if_missing(conn, "jobs", column, definition)
    conn.execute(
        """
        UPDATE jobs
        SET first_seen_at = COALESCE(first_seen_at, scraped_at),
            last_seen_at = COALESCE(last_seen_at, scraped_at),
            is_active = COALESCE(is_active, 1)
        WHERE first_seen_at IS NULL OR last_seen_at IS NULL OR is_active IS NULL
        """
    )


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate_jobs_lifecycle(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_is_active ON jobs(is_active)")
    conn.commit()


def finn_code_from_url(url: str) -> str:
    match = re.search(r"/ad/(\d+)", url)
    if match:
        return match.group(1)
    raise ValueError(f"Could not derive finn_code from {url!r}")


def finn_code_from_payload(payload: dict[str, Any], url: str) -> str:
    code = payload.get("finnCode")
    if code:
        return str(code)
    return finn_code_from_url(url)


def begin_scrape(conn: sqlite3.Connection, started_at: str) -> int:
    cur = conn.execute(
        "INSERT INTO scrapes (started_at) VALUES (?)",
        (started_at,),
    )
    conn.commit()
    return int(cur.lastrowid)


def record_discovered_jobs(
    conn: sqlite3.Connection,
    scrape_id: int,
    urls: list[str],
    seen_at: str,
) -> set[str]:
    discovered_codes: set[str] = set()
    for url in urls:
        finn_code = finn_code_from_url(url)
        discovered_codes.add(finn_code)
        conn.execute(
            "INSERT OR IGNORE INTO scrape_jobs (scrape_id, finn_code, url) VALUES (?, ?, ?)",
            (scrape_id, finn_code, url),
        )
        conn.execute(
            """
            INSERT INTO jobs (
                finn_code, url, scraped_at, raw_json,
                first_seen_at, last_seen_at, is_active, deactivated_at
            ) VALUES (?, ?, ?, '{}', ?, ?, 1, NULL)
            ON CONFLICT(finn_code) DO UPDATE SET
                url = excluded.url,
                last_seen_at = excluded.last_seen_at,
                is_active = 1,
                deactivated_at = NULL
            """,
            (finn_code, url, seen_at, seen_at, seen_at),
        )
    conn.commit()
    return discovered_codes


def finalize_scrape(
    conn: sqlite3.Connection,
    scrape_id: int,
    *,
    finished_at: str,
    urls_discovered: int,
    ok: int,
    failed: int,
    discovered_codes: set[str],
) -> tuple[int, int]:
    if discovered_codes:
        placeholders = ",".join("?" * len(discovered_codes))
        deactivated_row = conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM jobs
            WHERE is_active = 1 AND finn_code NOT IN ({placeholders})
            """,
            tuple(discovered_codes),
        ).fetchone()
        conn.execute(
            f"""
            UPDATE jobs
            SET is_active = 0, deactivated_at = ?
            WHERE is_active = 1 AND finn_code NOT IN ({placeholders})
            """,
            (finished_at, *discovered_codes),
        )
    else:
        deactivated_row = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE is_active = 1"
        ).fetchone()
        conn.execute(
            "UPDATE jobs SET is_active = 0, deactivated_at = ? WHERE is_active = 1",
            (finished_at,),
        )

    deactivated_count = int(deactivated_row["n"]) if deactivated_row else 0
    active_count = len(discovered_codes)

    conn.execute(
        """
        UPDATE scrapes
        SET finished_at = ?, urls_discovered = ?, ok = ?, failed = ?
        WHERE id = ?
        """,
        (finished_at, urls_discovered, ok, failed, scrape_id),
    )
    conn.commit()
    return active_count, deactivated_count


def upsert_job(conn: sqlite3.Connection, payload: dict[str, Any], scraped_at: str) -> None:
    url = payload.get("url") or ""
    finn_code = finn_code_from_payload(payload, url)
    coords = payload.get("location_coordinates") or {}
    lat = coords.get("lat")
    lon = coords.get("lon")

    conn.execute(
        """
        INSERT INTO jobs (
            finn_code, url, title, employer, deadline, employment_type, sector,
            location_text, lat, lon, last_changed_iso, scraped_at, raw_json,
            first_seen_at, last_seen_at, is_active, deactivated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL)
        ON CONFLICT(finn_code) DO UPDATE SET
            url = excluded.url,
            title = excluded.title,
            employer = excluded.employer,
            deadline = excluded.deadline,
            employment_type = excluded.employment_type,
            sector = excluded.sector,
            location_text = excluded.location_text,
            lat = excluded.lat,
            lon = excluded.lon,
            last_changed_iso = excluded.last_changed_iso,
            scraped_at = excluded.scraped_at,
            raw_json = excluded.raw_json
        """,
        (
            finn_code,
            url,
            payload.get("title"),
            payload.get("employer"),
            payload.get("deadline"),
            payload.get("employmentType"),
            payload.get("sector"),
            payload.get("location_text"),
            lat,
            lon,
            payload.get("lastChangedISO"),
            scraped_at,
            json.dumps(payload, ensure_ascii=False),
            scraped_at,
            scraped_at,
        ),
    )


def count_jobs(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()
    return int(row["n"]) if row else 0


def list_jobs_for_scrape(conn: sqlite3.Connection, scrape_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT j.*
        FROM scrape_jobs sj
        JOIN jobs j ON j.finn_code = sj.finn_code
        WHERE sj.scrape_id = ?
        ORDER BY j.finn_code
        """,
        (scrape_id,),
    ).fetchall()


def get_job_lifecycle(conn: sqlite3.Connection, finn_code: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT finn_code, url, first_seen_at, last_seen_at, is_active, deactivated_at
        FROM jobs
        WHERE finn_code = ?
        """,
        (finn_code,),
    ).fetchone()
