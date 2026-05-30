#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Discover Finn.no job listing URLs by partitioning search results."""
from __future__ import annotations

import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Callable, Optional
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import httpx
from selectolax.parser import HTMLParser

try:
    from .parse_job_ad import fetch_html, make_http_client
except ImportError:
    from parse_job_ad import fetch_html, make_http_client


SEARCH_BASE_URL = "https://www.finn.no/job/search"
MAX_RESULTS_WINDOW = 2500
MAX_SEARCH_PAGES = 50
RESULTS_TEXT_RE = re.compile(r"(\d[\d\s\xa0]*)\s+resultat(?:er)?", re.IGNORECASE)
COUNT_SUFFIX_RE = re.compile(r"^(.*?)\(\s*([\d\s\xa0]+)\s*\)$")


@dataclass(frozen=True)
class FilterOption:
    facet: str
    label: str
    url: str
    count: int


@dataclass(frozen=True)
class SearchPartition:
    url: str
    total_results: int
    depth: int
    description: str
    split_strategy: Optional[str] = None
    parent_url: Optional[str] = None
    filter_facet: Optional[str] = None
    filter_value: Optional[str] = None
    filter_label: Optional[str] = None
    facet_chain: tuple[tuple[str, Optional[str], str], ...] = ()


PlanningCallback = Callable[[dict], None]

_discover_worker_local = threading.local()


def _init_discover_worker() -> None:
    _discover_worker_local.client = make_http_client()


def _get_discover_worker_client() -> httpx.Client:
    client = getattr(_discover_worker_local, "client", None)
    if client is None:
        client = make_http_client()
        _discover_worker_local.client = client
    return client


def _resolve_discover_client(shared: Optional[httpx.Client], workers: int) -> httpx.Client:
    if workers > 1:
        return _get_discover_worker_client()
    if shared is not None:
        return shared
    return _get_discover_worker_client()


def collapse_ws(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    collapsed = re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()
    return collapsed or None


def clean_count(raw: str) -> int:
    digits = re.sub(r"[^\d]", "", raw or "")
    return int(digits) if digits else 0


def canonicalize_search_url(url: str, page: Optional[int] = None, include_page: bool = False) -> str:
    absolute = urljoin(SEARCH_BASE_URL, url)
    parsed = urlparse(absolute)
    query = parse_qs(parsed.query, keep_blank_values=True)

    if page is not None:
        if page > 1:
            query["page"] = [str(page)]
        else:
            query.pop("page", None)
    elif not include_page:
        query.pop("page", None)

    ordered_items: list[tuple[str, str]] = []
    for key in sorted(query):
        for value in sorted(query[key]):
            ordered_items.append((key, value))
    new_query = urlencode(ordered_items, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def get_page_url(url: str, page: int) -> str:
    return canonicalize_search_url(url, page=page, include_page=True)


def get_query_param(url: str, key: str) -> Optional[str]:
    query = parse_qs(urlparse(url).query)
    values = query.get(key)
    return values[0] if values else None


def parse_total_results(html: str) -> Optional[int]:
    doc = HTMLParser(html)
    for node in doc.css("output, h1, h2, p, span, div"):
        text = collapse_ws(node.text(separator=" ", strip=True))
        if not text:
            continue
        match = RESULTS_TEXT_RE.search(text)
        if match:
            return clean_count(match.group(1))
    match = RESULTS_TEXT_RE.search(html)
    if match:
        return clean_count(match.group(1))
    return None


def extract_job_urls_from_search_page(html: str) -> list[str]:
    doc = HTMLParser(html)
    job_urls: list[str] = []
    seen: set[str] = set()
    for link in doc.css("a[href*='/job/ad/']"):
        href = link.attributes.get("href") if hasattr(link, "attributes") else None
        if not href:
            continue
        match = re.search(r"/job/ad/(\d+)", href)
        if not match:
            continue
        url = f"https://www.finn.no/job/ad/{match.group(1)}"
        if url in seen:
            continue
        seen.add(url)
        job_urls.append(url)
    return job_urls


def has_next_page(html: str) -> bool:
    doc = HTMLParser(html)
    for link in doc.css("nav a, a"):
        text = collapse_ws(link.text(separator=" ", strip=True)) or ""
        aria = collapse_ws(link.attributes.get("aria-label")) if hasattr(link, "attributes") else None
        combined = " ".join(part for part in (text, aria) if part)
        if "Neste side" in combined:
            return True
    return False


def extract_filter_options(html: str, facet: str, current_url: str) -> list[FilterOption]:
    if facet not in {"location", "occupation"}:
        raise ValueError(f"Unsupported facet: {facet}")

    doc = HTMLParser(html)
    current = canonicalize_search_url(current_url)
    current_query = parse_qs(urlparse(current).query, keep_blank_values=True)
    current_query.pop("page", None)
    options: list[FilterOption] = []
    seen: set[str] = set()

    for anchor in doc.css(f"a[href*='{facet}=']"):
        href = anchor.attributes.get("href") if hasattr(anchor, "attributes") else None
        if not href:
            continue
        absolute = canonicalize_search_url(urljoin(current_url, href))
        if absolute == current or absolute in seen:
            continue
        candidate_query = parse_qs(urlparse(absolute).query, keep_blank_values=True)
        candidate_query.pop("page", None)
        if facet not in candidate_query:
            continue
        comparable_keys = set(current_query) | set(candidate_query)
        comparable_keys.discard(facet)
        if any(candidate_query.get(key) != current_query.get(key) for key in comparable_keys):
            continue

        text = collapse_ws(anchor.text(separator=" ", strip=True))
        if not text:
            continue
        match = COUNT_SUFFIX_RE.match(text)
        if not match:
            continue
        label = collapse_ws(match.group(1))
        count = clean_count(match.group(2))
        if not label or count <= 0:
            continue
        if "ledige stillinger for" in label.casefold():
            continue

        seen.add(absolute)
        options.append(FilterOption(facet=facet, label=label, url=absolute, count=count))

    options.sort(key=lambda option: (-option.count, option.label.casefold()))
    return options


def describe_partition(url: str) -> str:
    query = parse_qs(urlparse(url).query)
    parts: list[str] = []
    if query.get("location"):
        parts.append(f"location={query['location'][0]}")
    if query.get("occupation"):
        parts.append(f"occupation={query['occupation'][0]}")
    if query.get("q"):
        parts.append(f"q={query['q'][0]}")
    return ", ".join(parts) if parts else "all jobs"


def describe_partition_label_chain(facet_chain: tuple[tuple[str, Optional[str], str], ...], fallback_url: str) -> str:
    labels = [label for _, _, label in facet_chain if label]
    return " > ".join(labels) if labels else describe_partition(fallback_url)


def choose_split_options(total_results: int, options: list[FilterOption]) -> list[FilterOption]:
    if len(options) < 2:
        return []
    smaller = [option for option in options if option.count < total_results]
    if len(smaller) >= 2:
        return smaller
    if all(option.count >= total_results for option in options):
        return []
    return options


def count_facet_steps(
    facet_chain: tuple[tuple[str, Optional[str], str], ...],
    facet: str,
) -> int:
    return sum(1 for current_facet, _, _ in facet_chain if current_facet == facet)


@dataclass
class _VisitArgs:
    url: str
    depth: int
    parent_url: Optional[str]
    split_strategy: Optional[str]
    filter_facet: Optional[str]
    filter_label: Optional[str]
    parent_facet_chain: tuple[tuple[str, Optional[str], str], ...]


class _PartitionPlanner:
    def __init__(
        self,
        *,
        max_results_window: int,
        max_depth: int,
        progress_callback: Optional[PlanningCallback],
        shared_client: Optional[httpx.Client],
        workers: int,
    ) -> None:
        self.max_results_window = max_results_window
        self.max_depth = max_depth
        self.progress_callback = progress_callback
        self.shared_client = shared_client
        self.workers = workers
        self.planned: list[SearchPartition] = []
        self._visiting: set[str] = set()
        self._visiting_lock = threading.Lock()
        self._planned_lock = threading.Lock()

    def emit(self, event_type: str, **payload: object) -> None:
        if self.progress_callback:
            self.progress_callback({"type": event_type, **payload})

    def _fetch(self, url: str) -> str:
        client = _resolve_discover_client(self.shared_client, self.workers)
        return fetch_html(url, client=client)

    def _visit_many(self, children: list[_VisitArgs]) -> None:
        if not children:
            return
        if self.workers <= 1 or len(children) == 1:
            for child in children:
                self.visit(child)
            return

        max_workers = min(self.workers, len(children))
        with ThreadPoolExecutor(max_workers=max_workers, initializer=_init_discover_worker) as executor:
            futures = [executor.submit(self.visit, child) for child in children]
            for future in futures:
                future.result()

    def visit(self, args: _VisitArgs) -> None:
        canonical_url = canonicalize_search_url(args.url)
        with self._visiting_lock:
            if canonical_url in self._visiting:
                raise RuntimeError(f"Recursive split loop detected for {canonical_url}")
            self._visiting.add(canonical_url)
        try:
            self.emit(
                "planning_visit_started",
                url=canonical_url,
                depth=args.depth,
                splitStrategy=args.split_strategy,
                parentUrl=args.parent_url,
            )
            html = self._fetch(canonical_url)
            total_results = parse_total_results(html) or 0
            self.emit(
                "planning_visit_loaded",
                url=canonical_url,
                depth=args.depth,
                totalResults=total_results,
            )

            current_step: tuple[tuple[str, Optional[str], str], ...] = ()
            if args.filter_facet and args.filter_label:
                current_step = (
                    (
                        args.filter_facet,
                        get_query_param(canonical_url, args.filter_facet),
                        args.filter_label,
                    ),
                )
            facet_chain = args.parent_facet_chain + current_step
            location_depth = count_facet_steps(facet_chain, "location")
            location_options = extract_filter_options(html, "location", canonical_url)
            can_descend_location = args.depth < self.max_depth and bool(location_options)

            if can_descend_location:
                self.emit(
                    "planning_split_chosen",
                    url=canonical_url,
                    depth=args.depth,
                    facet="location",
                    optionCount=len(location_options),
                    totalResults=total_results,
                )
                children = [
                    _VisitArgs(
                        url=option.url,
                        depth=args.depth + 1,
                        parent_url=canonical_url,
                        split_strategy="location",
                        filter_facet=option.facet,
                        filter_label=option.label,
                        parent_facet_chain=facet_chain,
                    )
                    for option in location_options
                ]
                self._visit_many(children)
                return

            if total_results <= self.max_results_window or args.depth >= self.max_depth:
                partition = SearchPartition(
                    url=canonical_url,
                    total_results=total_results,
                    depth=args.depth,
                    description=describe_partition_label_chain(facet_chain, canonical_url),
                    split_strategy=args.split_strategy,
                    parent_url=args.parent_url,
                    filter_facet=args.filter_facet,
                    filter_value=get_query_param(canonical_url, args.filter_facet) if args.filter_facet else None,
                    filter_label=args.filter_label,
                    facet_chain=facet_chain,
                )
                with self._planned_lock:
                    self.planned.append(partition)
                self.emit(
                    "planning_leaf_ready",
                    url=canonical_url,
                    depth=args.depth,
                    totalResults=total_results,
                    splitStrategy=args.split_strategy,
                )
                return

            occupation_options = choose_split_options(
                total_results, extract_filter_options(html, "occupation", canonical_url)
            )
            if occupation_options:
                self.emit(
                    "planning_split_chosen",
                    url=canonical_url,
                    depth=args.depth,
                    facet="occupation",
                    optionCount=len(occupation_options),
                    totalResults=total_results,
                )
                children = [
                    _VisitArgs(
                        url=option.url,
                        depth=args.depth + 1,
                        parent_url=canonical_url,
                        split_strategy="occupation",
                        filter_facet=option.facet,
                        filter_label=option.label,
                        parent_facet_chain=facet_chain,
                    )
                    for option in occupation_options
                ]
                self._visit_many(children)
                return

            raise RuntimeError(
                f"Search partition '{canonical_url}' returned {total_results} results, "
                f"but no reliable split options were found after exhausting location depth {location_depth}."
            )
        finally:
            with self._visiting_lock:
                self._visiting.remove(canonical_url)


def plan_search_partitions(
    start_url: str = f"{SEARCH_BASE_URL}?location=0.20001",
    max_results_window: int = MAX_RESULTS_WINDOW,
    max_depth: int = 8,
    progress_callback: Optional[PlanningCallback] = None,
    client: Optional[httpx.Client] = None,
    workers: int = 1,
) -> list[SearchPartition]:
    planner = _PartitionPlanner(
        max_results_window=max_results_window,
        max_depth=max_depth,
        progress_callback=progress_callback,
        shared_client=client,
        workers=workers,
    )
    planner.visit(
        _VisitArgs(
            url=start_url,
            depth=0,
            parent_url=None,
            split_strategy=None,
            filter_facet=None,
            filter_label=None,
            parent_facet_chain=(),
        )
    )

    deduped: dict[str, SearchPartition] = {}
    for partition in planner.planned:
        deduped[partition.url] = partition
    return sorted(
        deduped.values(),
        key=lambda partition: (partition.depth, partition.description.casefold(), partition.url),
    )


def _fetch_partition_page(
    partition: SearchPartition,
    page: int,
    expected_pages: int,
    *,
    shared_client: Optional[httpx.Client],
    workers: int,
    progress_callback: Optional[PlanningCallback],
) -> tuple[int, str, list[str]]:
    page_url = get_page_url(partition.url, page)
    if progress_callback:
        progress_callback(
            {
                "type": "partition_page_started",
                "partitionUrl": partition.url,
                "page": page,
                "expectedPages": expected_pages,
                "pageUrl": page_url,
            }
        )
    client = _resolve_discover_client(shared_client, workers)
    html = fetch_html(page_url, client=client)
    page_urls = extract_job_urls_from_search_page(html)
    if progress_callback:
        progress_callback(
            {
                "type": "partition_page_loaded",
                "partitionUrl": partition.url,
                "page": page,
                "expectedPages": expected_pages,
                "pageUrl": page_url,
                "discoveredOnPage": len(page_urls),
            }
        )
    return page, html, page_urls


def discover_job_urls_for_partition(
    partition: SearchPartition,
    delay: float = 0.0,
    max_pages: int = MAX_SEARCH_PAGES,
    progress_callback: Optional[PlanningCallback] = None,
    client: Optional[httpx.Client] = None,
    workers: int = 1,
) -> list[str]:
    all_urls: list[str] = []
    seen: set[str] = set()

    expected_pages = 1
    if partition.total_results > 0:
        expected_pages = min(max_pages, max(1, math.ceil(partition.total_results / 50)))

    if workers <= 1:
        for page in range(1, expected_pages + 1):
            _, html, page_urls = _fetch_partition_page(
                partition,
                page,
                expected_pages,
                shared_client=client,
                workers=workers,
                progress_callback=progress_callback,
            )
            if not page_urls:
                break
            for url in page_urls:
                if url in seen:
                    continue
                seen.add(url)
                all_urls.append(url)
            if page >= max_pages or not has_next_page(html):
                break
            if delay > 0:
                time.sleep(delay)
        return all_urls

    page_workers = min(workers, expected_pages)
    with ThreadPoolExecutor(max_workers=page_workers, initializer=_init_discover_worker) as executor:
        futures = {
            executor.submit(
                _fetch_partition_page,
                partition,
                page,
                expected_pages,
                shared_client=client,
                workers=workers,
                progress_callback=progress_callback,
            ): page
            for page in range(1, expected_pages + 1)
        }
        page_results: dict[int, tuple[str, list[str]]] = {}
        for future in as_completed(futures):
            page, html, page_urls = future.result()
            page_results[page] = (html, page_urls)

    for page in range(1, expected_pages + 1):
        if page not in page_results:
            continue
        html, page_urls = page_results[page]
        if not page_urls:
            break
        for url in page_urls:
            if url in seen:
                continue
            seen.add(url)
            all_urls.append(url)
        if page >= max_pages or not has_next_page(html):
            break

    if delay > 0:
        time.sleep(delay)

    return all_urls


def _discover_partition_task(
    partition: SearchPartition,
    delay: float,
    shared_client: Optional[httpx.Client],
    workers: int,
    progress_callback: Optional[PlanningCallback],
) -> list[str]:
    return discover_job_urls_for_partition(
        partition,
        delay=delay,
        client=shared_client,
        workers=workers,
        progress_callback=progress_callback,
    )


def discover_all_job_urls(
    start_url: str = f"{SEARCH_BASE_URL}?location=0.20001",
    delay: float = 0.0,
    client: Optional[httpx.Client] = None,
    workers: int = 8,
) -> tuple[list[SearchPartition], list[str]]:
    owned_client: Optional[httpx.Client] = None
    if client is None and workers <= 1:
        owned_client = make_http_client()
        client = owned_client

    try:
        print(f"  Planning search partitions (workers={workers}) ...")
        partitions = plan_search_partitions(
            start_url=start_url, client=client, workers=workers
        )
        print(f"  Fetching listing pages for {len(partitions)} partitions (workers={workers}) ...")
        all_urls: list[str] = []
        seen: set[str] = set()

        if workers <= 1:
            for partition in partitions:
                partition_urls = discover_job_urls_for_partition(
                    partition, delay=delay, client=client, workers=workers
                )
                for url in partition_urls:
                    if url in seen:
                        continue
                    seen.add(url)
                    all_urls.append(url)
        else:
            partition_workers = min(workers, len(partitions)) if partitions else 1
            with ThreadPoolExecutor(
                max_workers=partition_workers, initializer=_init_discover_worker
            ) as executor:
                futures = {
                    executor.submit(
                        _discover_partition_task,
                        partition,
                        delay,
                        client,
                        workers,
                        None,
                    ): partition
                    for partition in partitions
                }
                for future in as_completed(futures):
                    partition_urls = future.result()
                    for url in partition_urls:
                        if url in seen:
                            continue
                        seen.add(url)
                        all_urls.append(url)

        return partitions, all_urls
    finally:
        if owned_client is not None:
            owned_client.close()


def partitions_to_dicts(partitions: list[SearchPartition]) -> list[dict]:
    return [asdict(partition) for partition in partitions]
