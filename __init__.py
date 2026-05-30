"""Portable Finn.no job scraper with SQLite storage."""

from . import job_storage
from .discover_job_urls import discover_all_job_urls
from .parse_job_ad import extract_finn_job
from .run_scraper import ScrapeResult, main, run_scrape

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "discover_all_job_urls",
    "extract_finn_job",
    "job_storage",
    "main",
    "run_scrape",
    "ScrapeResult",
]
