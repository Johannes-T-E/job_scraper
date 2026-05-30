#!/usr/bin/env python3
"""Orchestrate Finn.no job discovery, parsing, and SQLite storage."""
from __future__ import annotations

import argparse
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx

try:
    from . import job_storage
    from .discover_job_urls import SEARCH_BASE_URL, discover_all_job_urls
    from .parse_job_ad import extract_finn_job, fetch_html, make_http_client, parse_finn_job_html
except ImportError:
    import job_storage
    from discover_job_urls import SEARCH_BASE_URL, discover_all_job_urls
    from parse_job_ad import extract_finn_job, fetch_html, make_http_client, parse_finn_job_html

DEFAULT_START_URL = f"{SEARCH_BASE_URL}?location=0.20001"

_worker_local = threading.local()


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _init_worker() -> None:
    _worker_local.client = make_http_client()


def _get_worker_client() -> httpx.Client:
    client = getattr(_worker_local, "client", None)
    if client is None:
        client = make_http_client()
        _worker_local.client = client
    return client


def _scrape_one(url: str) -> tuple[str, Optional[dict[str, Any]], Optional[str]]:
    try:
        client = _get_worker_client()
        html = fetch_html(url, client=client)
        payload = parse_finn_job_html(url, html)
        return url, payload, None
    except Exception as exc:
        return url, None, str(exc)


def _maybe_commit(conn: sqlite3.Connection, pending: int, commit_every: int) -> int:
    if pending >= commit_every:
        conn.commit()
        return 0
    return pending


def scrape_jobs_sequential(
    conn: sqlite3.Connection,
    job_urls: list[str],
    scraped_at: str,
    *,
    delay: float,
    commit_every: int,
) -> tuple[int, int]:
    ok = 0
    failed = 0
    pending = 0

    with make_http_client() as client:
        for index, url in enumerate(job_urls, 1):
            try:
                payload = extract_finn_job(url, client=client)
                job_storage.upsert_job(conn, payload, scraped_at)
                ok += 1
                pending += 1
                pending = _maybe_commit(conn, pending, commit_every)
            except Exception as exc:
                failed += 1
                print(f"  [{index}/{len(job_urls)}] FAIL {url}: {exc}")
            if index % 50 == 0 or index == len(job_urls):
                print(f"  [{index}/{len(job_urls)}] stored (ok={ok}, failed={failed})")
            if index < len(job_urls) and delay > 0:
                time.sleep(delay)

    if pending:
        conn.commit()
    return ok, failed


def scrape_jobs_parallel(
    conn: sqlite3.Connection,
    job_urls: list[str],
    scraped_at: str,
    *,
    workers: int,
    delay: float,
    commit_every: int,
) -> tuple[int, int]:
    ok = 0
    failed = 0
    pending = 0
    total = len(job_urls)
    completed = 0

    with ThreadPoolExecutor(max_workers=workers, initializer=_init_worker) as executor:
        futures = {executor.submit(_scrape_one, url): url for url in job_urls}
        for future in as_completed(futures):
            url, payload, error = future.result()
            completed += 1
            if error is not None:
                failed += 1
                print(f"  [{completed}/{total}] FAIL {url}: {error}")
            else:
                job_storage.upsert_job(conn, payload, scraped_at)
                ok += 1
                pending += 1
                pending = _maybe_commit(conn, pending, commit_every)
            if completed % 50 == 0 or completed == total:
                print(f"  [{completed}/{total}] stored (ok={ok}, failed={failed})")
            if delay > 0:
                time.sleep(delay)

    if pending:
        conn.commit()
    return ok, failed


@dataclass
class ScrapeResult:
    db_path: Path
    scrape_id: int
    partitions: int
    urls_found: int
    ok: int
    failed: int
    active_count: int
    deactivated_count: int
    total_in_db: int


def run_scrape(
    *,
    db_path: Path | None = None,
    start_url: str = DEFAULT_START_URL,
    workers: int = 8,
    discover_workers: int = 8,
    delay: float = 0.0,
    discover_delay: float = 0.0,
    commit_every: int = 50,
) -> ScrapeResult:
    """Discover and scrape Finn.no jobs into SQLite.

    Returns a summary of the run. Raises ValueError for invalid parameters.
    """
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if discover_workers < 1:
        raise ValueError("discover_workers must be at least 1")
    if commit_every < 1:
        raise ValueError("commit_every must be at least 1")

    resolved_db_path = db_path or job_storage.default_db_path()
    conn = job_storage.connect(resolved_db_path)
    job_storage.init_db(conn)

    started_at = utc_now()
    scrape_id = job_storage.begin_scrape(conn, started_at)

    print(f"Database: {resolved_db_path}")
    print(f"Scrape run: {scrape_id}")
    print(f"Discovering job URLs from {start_url} ...")
    partitions, job_urls = discover_all_job_urls(
        start_url=start_url,
        delay=discover_delay,
        workers=discover_workers,
    )
    print(f"Partitions: {len(partitions)}")
    print(f"Unique job URLs: {len(job_urls)}")

    discovered_at = utc_now()
    discovered_codes = job_storage.record_discovered_jobs(
        conn, scrape_id, job_urls, discovered_at
    )

    print(
        f"Scrape: workers={workers}, delay={delay}, commit_every={commit_every} "
        f"(discovery used discover_workers={discover_workers}, discover_delay={discover_delay})"
    )

    scraped_at = utc_now()
    if not job_urls:
        finished_at = utc_now()
        active_count, deactivated_count = job_storage.finalize_scrape(
            conn,
            scrape_id,
            finished_at=finished_at,
            urls_discovered=0,
            ok=0,
            failed=0,
            discovered_codes=discovered_codes,
        )
        total_in_db = job_storage.count_jobs(conn)
        conn.close()
        print(
            f"Done. No job URLs to scrape. Deactivated {deactivated_count} previously active jobs."
        )
        return ScrapeResult(
            db_path=resolved_db_path,
            scrape_id=scrape_id,
            partitions=len(partitions),
            urls_found=0,
            ok=0,
            failed=0,
            active_count=active_count,
            deactivated_count=deactivated_count,
            total_in_db=total_in_db,
        )

    if workers == 1:
        ok, failed = scrape_jobs_sequential(
            conn,
            job_urls,
            scraped_at,
            delay=delay,
            commit_every=commit_every,
        )
    else:
        ok, failed = scrape_jobs_parallel(
            conn,
            job_urls,
            scraped_at,
            workers=workers,
            delay=delay,
            commit_every=commit_every,
        )

    finished_at = utc_now()
    active_count, deactivated_count = job_storage.finalize_scrape(
        conn,
        scrape_id,
        finished_at=finished_at,
        urls_discovered=len(job_urls),
        ok=ok,
        failed=failed,
        discovered_codes=discovered_codes,
    )

    total_in_db = job_storage.count_jobs(conn)
    conn.close()
    print(
        f"Done. This run: ok={ok}, failed={failed}, active={active_count}, "
        f"deactivated={deactivated_count}. Rows in database: {total_in_db}"
    )
    return ScrapeResult(
        db_path=resolved_db_path,
        scrape_id=scrape_id,
        partitions=len(partitions),
        urls_found=len(job_urls),
        ok=ok,
        failed=failed,
        active_count=active_count,
        deactivated_count=deactivated_count,
        total_in_db=total_in_db,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scrape all Finn.no jobs into SQLite.")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite database path (default: job_scraper/data/jobs.sqlite3)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Seconds to sleep after each detail page fetch (per worker when parallel)",
    )
    parser.add_argument(
        "--discover-delay",
        type=float,
        default=0.0,
        help="Seconds to sleep during discovery (per partition when parallel)",
    )
    parser.add_argument(
        "--discover-workers",
        type=int,
        default=8,
        help="Parallel workers for discovery planning and search pages (1 = sequential)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Parallel workers for detail page fetches (1 = sequential)",
    )
    parser.add_argument(
        "--commit-every",
        type=int,
        default=50,
        help="Commit SQLite transaction every N successful upserts",
    )
    parser.add_argument(
        "--start-url",
        default=DEFAULT_START_URL,
        help="Finn job search URL to partition from",
    )
    args = parser.parse_args(argv)

    try:
        result = run_scrape(
            db_path=args.db,
            start_url=args.start_url,
            workers=args.workers,
            discover_workers=args.discover_workers,
            delay=args.delay,
            discover_delay=args.discover_delay,
            commit_every=args.commit_every,
        )
    except ValueError as exc:
        parser.error(str(exc))

    return 1 if result.failed and not result.ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
