#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FINN.no job ad parser.

Usage:
  python parse_job_ad.py "https://www.finn.no/job/ad/460062297" [--debug] [--out PATH]
"""

from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime
from typing import Any, Dict, Iterable, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:142.0) Gecko/20100101 Firefox/142.0"
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,no;q=0.7,en;q=0.3",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Pragma": "no-cache",
}
RETRY_STATUS = {429, 500, 502, 503, 504}
SECTION_SPLIT_RE = re.compile(
    r"(?:(?:^|(?:<br\s*/?>\s*){2,})\s*)<strong>([^<]{1,180}?)</strong>\s*(?:<br\s*/?>\s*)+",
    re.IGNORECASE | re.DOTALL,
)
BULLET_LINE_RE = re.compile(r"^\s*(?P<marker>(?:[•●▪◦‣⁃*-])|(?:\d+[\.)]))\s*(?P<text>.+?)\s*$")
TARGETING_ENTRY_RE = re.compile(r'\{"key":"(?P<key>[^"]+)","value":\[(?P<values>[^\]]*)\]\}')
SECTOR_HINT_RE = re.compile(r'"(?P<code>181[2-6])","value","(?P<label>[^"]{1,120})"')
REMOTE_HINT_RE = re.compile(r'"(?P<code>[123])","(?P<label>Delvis hjemmekontor|Kun hjemmekontor|P(?:å|a) kontoret)"')
JOBTYPE_HINT_RE = re.compile(r'"jobType","(?P<value>FULLTIME|PARTTIME)"')


def make_http_client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=timeout,
        http2=True,
    )


def fetch_html(
    url: str,
    max_attempts: int = 5,
    timeout: float = 30.0,
    trace: Optional[Dict[str, Any]] = None,
    client: Optional[httpx.Client] = None,
) -> str:
    if client is not None:
        return _fetch_html_with_client(url, client, max_attempts=max_attempts, trace=trace)

    with make_http_client(timeout=timeout) as owned_client:
        return _fetch_html_with_client(url, owned_client, max_attempts=max_attempts, trace=trace)


def _fetch_html_with_client(
    url: str,
    client: httpx.Client,
    max_attempts: int = 5,
    trace: Optional[Dict[str, Any]] = None,
) -> str:
    attempt = 0
    backoff = 1.0
    while True:
        attempt += 1
        if trace is not None:
            trace["http_attempts"] = attempt
        try:
            response = client.get(url)
            if response.status_code in RETRY_STATUS:
                if trace is not None:
                    trace.setdefault("http_retry_statuses", []).append(response.status_code)
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        sleep_s = int(retry_after)
                    except ValueError:
                        sleep_s = backoff
                else:
                    sleep_s = backoff + random.uniform(0.0, 0.5)
                if attempt >= max_attempts:
                    response.raise_for_status()
                time.sleep(sleep_s)
                backoff = min(backoff * 2, 8.0)
                continue
            response.raise_for_status()
            return response.text
        except httpx.HTTPError:
            if attempt >= max_attempts:
                raise
            time.sleep(backoff + random.uniform(0.0, 0.5))
            backoff = min(backoff * 2, 8.0)


def collapse_ws(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    collapsed = re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()
    return collapsed or None


def node_text(node) -> Optional[str]:
    if not node:
        return None
    return collapse_ws(node.text(separator=" ", strip=True))


def clean_text_list(values: Iterable[Optional[str]]) -> Optional[list[str]]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = collapse_ws(value)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result or None


def deepest_text(node) -> Optional[str]:
    if not node:
        return None
    texts = clean_text_list(child.text(strip=True) for child in node.css("*"))
    if texts:
        return texts[-1]
    return collapse_ws(node.text(strip=True))


def normalize_heading(text: Optional[str]) -> str:
    value = collapse_ws(text) or ""
    value = value.replace("AI-generert", "").strip()
    return value.casefold()


def label_key(text: Optional[str]) -> str:
    value = collapse_ws(text) or ""
    return value.rstrip(":").strip().casefold()


def parse_json_string_array(fragment: str) -> list[str]:
    payload = f"[{fragment}]"
    try:
        values = json.loads(payload)
    except Exception:
        return []
    result: list[str] = []
    for value in values:
        cleaned = collapse_ws(value if isinstance(value, str) else None)
        if cleaned:
            result.append(cleaned)
    return result


def extract_targeting_pairs(html: str) -> dict[str, list[str]]:
    targeting: dict[str, list[str]] = {}
    for match in TARGETING_ENTRY_RE.finditer(html):
        key = collapse_ws(match.group("key"))
        if not key:
            continue
        values = parse_json_string_array(match.group("values"))
        if values:
            targeting[key] = values
    return targeting


def extract_embedded_facet_hints(html: str) -> dict[str, Any]:
    hints: dict[str, Any] = {}
    targeting = extract_targeting_pairs(html)
    if targeting:
        hints["targeting"] = targeting

    sector_match = SECTOR_HINT_RE.search(html)
    if sector_match:
        hints["job_sector"] = {
            "value": sector_match.group("code"),
            "label": collapse_ws(sector_match.group("label")),
        }

    remote_match = REMOTE_HINT_RE.search(html)
    if remote_match:
        hints["home_office"] = {
            "value": remote_match.group("code"),
            "label": collapse_ws(remote_match.group("label")),
        }

    jobtype_match = JOBTYPE_HINT_RE.search(html)
    if jobtype_match:
        raw_value = jobtype_match.group("value")
        if raw_value == "FULLTIME":
            hints["extent"] = {"value": "3947", "label": "Heltid"}
        elif raw_value == "PARTTIME":
            hints["extent"] = {"value": "3948", "label": "Deltid"}

    working_language = targeting.get("working_language")
    if working_language:
        hints["working_language"] = {
            "values": working_language,
            "labels": working_language,
        }

    if targeting.get("occupation"):
        hints["occupation"] = {"values": targeting["occupation"]}
    if targeting.get("industry"):
        hints["industry"] = {"values": targeting["industry"]}
    if targeting.get("county") or targeting.get("municipality"):
        hints["location_codes"] = {
            "county": (targeting.get("county") or [None])[0],
            "municipality": (targeting.get("municipality") or [None])[0],
        }

    return hints


def wrap_fragment(fragment: str) -> str:
    return f"<p>{fragment}</p>"


def html_fragment_to_text(fragment: str) -> Optional[str]:
    doc = HTMLParser(f"<div>{fragment}</div>")
    root = doc.css_first("div")
    if not root:
        return None
    return collapse_ws(root.text(separator="\n", strip=True))


def html_fragment_to_lines(fragment: str) -> list[str]:
    doc = HTMLParser(f"<div>{fragment}</div>")
    root = doc.css_first("div")
    if not root:
        return []
    return [line for line in (collapse_ws(part) for part in root.text(separator="\n", strip=True).splitlines()) if line]


def description_blocks_from_fragment(fragment: str, *, infer_unmarked_list: bool = False) -> list[dict[str, Any]]:
    lines = html_fragment_to_lines(fragment)
    if not lines:
        return []
    block_html = fragment if fragment.lstrip().startswith("<") else wrap_fragment(fragment)

    bullet_matches = [BULLET_LINE_RE.match(line) for line in lines]
    if all(bullet_matches):
        return [
            {
                "type": "list",
                "ordered": all(match.group("marker")[0].isdigit() for match in bullet_matches if match),
                "items": [match.group("text") for match in bullet_matches if match],
                "html": block_html,
            }
        ]

    if infer_unmarked_list and len(lines) >= 3:
        return [
            {
                "type": "list",
                "ordered": False,
                "items": lines,
                "html": block_html,
            }
        ]

    text = html_fragment_to_text(fragment)
    return [{"type": "paragraph", "text": text, "html": block_html}] if text else []


def strip_outer_tag(html_fragment: str, tag: str) -> str:
    pattern = re.compile(
        rf"^\s*<{tag}\b[^>]*>(.*)</{tag}>\s*$",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.match(html_fragment or "")
    return match.group(1) if match else html_fragment


def split_paragraph_into_sections(node) -> Optional[list[dict[str, Optional[str]]]]:
    raw_html = getattr(node, "html", "") or ""
    inner_html = strip_outer_tag(raw_html, "p")
    matches = list(SECTION_SPLIT_RE.finditer(inner_html))
    if not matches:
        return None
    if inner_html[: matches[0].start()].strip():
        return None

    sections: list[dict[str, Optional[str]]] = []
    for index, match in enumerate(matches):
        title = html_fragment_to_text(match.group(1))
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(inner_html)
        body_html = inner_html[match.end() : next_start].strip()
        body_text = html_fragment_to_text(body_html) if body_html else None
        sections.append(
            {
                "title": title,
                "html": wrap_fragment(body_html) if body_html else None,
                "text": body_text,
                "blocks": description_blocks_from_fragment(body_html, infer_unmarked_list=True) if body_html else [],
            }
        )
    return sections or None


def is_strong_heading_paragraph(node) -> Optional[str]:
    if not node or (node.tag or "").lower() != "p":
        return None
    bold_el = node.css_first("strong, b")
    if not bold_el:
        return None
    strong_text = node_text(bold_el)
    para_text = collapse_ws(node.text(strip=True))
    if not strong_text or not para_text:
        return None
    candidates = {strong_text, strong_text + ":"}
    if para_text in candidates and len(para_text) <= 140:
        return strong_text
    return None


def extract_description_sections(container, job_title: Optional[str]) -> Optional[list[dict[str, Any]]]:
    if not container:
        return None

    sections: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None

    def start_section(title: Optional[str], level: Optional[int]) -> None:
        nonlocal current
        current = {"title": title, "level": level, "blocks": []}
        sections.append(current)

    start_section(job_title or None, None)
    allowed = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "blockquote"}

    for node in container.css("*"):
        tag = (node.tag or "").lower()
        if tag not in allowed:
            continue

        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            text = node_text(node)
            if text:
                try:
                    level = int(tag[1])
                except Exception:
                    level = None
                start_section(text, level)
            continue

        if tag == "p":
            ancestor = node.parent
            inside_list = False
            while ancestor is not None:
                parent_tag = (ancestor.tag or "").lower()
                if parent_tag in {"ul", "ol"}:
                    inside_list = True
                    break
                ancestor = ancestor.parent
            if inside_list:
                continue

            split_sections = split_paragraph_into_sections(node)
            if split_sections:
                for item in split_sections:
                    start_section(item["title"], 3)
                    current["blocks"].extend(item["blocks"])
                continue

            strong_heading = is_strong_heading_paragraph(node)
            if strong_heading:
                start_section(strong_heading, 3)
                continue

            blocks = description_blocks_from_fragment(getattr(node, "html", None) or node.text(separator="\n", strip=True))
            current["blocks"].extend(blocks)
            continue

        if tag in {"ul", "ol"}:
            items = clean_text_list(li.text(strip=True) for li in node.css("li"))
            if items:
                current["blocks"].append(
                    {
                        "type": "list",
                        "ordered": tag == "ol",
                        "items": items,
                        "html": getattr(node, "html", None),
                    }
                )
            continue

        if tag == "blockquote":
            text = collapse_ws(node.text(separator="\n", strip=True))
            if text:
                current["blocks"].append(
                    {
                        "type": "blockquote",
                        "text": text,
                        "html": getattr(node, "html", None),
                    }
                )

    if sections and not sections[0]["blocks"] and (sections[0]["title"] is None or len(sections) > 1):
        sections.pop(0)

    cleaned_sections: list[dict[str, Any]] = []
    for section in sections:
        if section["title"] is None and not section["blocks"]:
            continue
        if section["title"] and not section["blocks"] and cleaned_sections:
            continue
        cleaned_sections.append(section)
    return cleaned_sections or None


def description_sections_from_html(description_html: Optional[str], job_title: Optional[str]) -> Optional[list[dict[str, Any]]]:
    if not description_html:
        return None
    doc = HTMLParser(f"<div>{description_html}</div>")
    root = doc.css_first("div")
    return extract_description_sections(root, job_title)


def extract_meta(html: str, names_or_props: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name_or_prop in names_or_props:
        meta_name = re.search(
            rf'<meta[^>]+name=["\']{re.escape(name_or_prop)}["\'][^>]+content=["\']([^"\']+)["\']',
            html,
            re.IGNORECASE,
        )
        if meta_name:
            out[name_or_prop] = meta_name.group(1)
        meta_prop = re.search(
            rf'<meta[^>]+property=["\']{re.escape(name_or_prop)}["\'][^>]+content=["\']([^"\']+)["\']',
            html,
            re.IGNORECASE,
        )
        if meta_prop:
            out[name_or_prop] = meta_prop.group(1)
    return out


def extract_jsonld_objects(html: str) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        raw = (match.group(1) or "").strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "script:ld+json" in parsed:
            parsed = parsed["script:ld+json"]
        if isinstance(parsed, dict):
            objects.append(parsed)
        elif isinstance(parsed, list):
            objects.extend(item for item in parsed if isinstance(item, dict))
    return objects


def extract_jobposting_jsonld(html: str) -> Optional[dict[str, Any]]:
    for obj in extract_jsonld_objects(html):
        if collapse_ws(obj.get("@type")) == "JobPosting":
            return obj
    return None


def normalize_phone(raw: str) -> Optional[str]:
    cleaned = collapse_ws(raw)
    if not cleaned:
        return None
    plus = cleaned.startswith("+")
    digits = re.sub(r"\D+", "", cleaned)
    if not digits:
        return None
    return f"+{digits}" if plus else digits


def extract_phones_from_node(node) -> list[str]:
    if not node:
        return []
    phones: list[str] = []
    for anchor in node.css('a[href^="tel:"]'):
        href = anchor.attributes.get("href") if hasattr(anchor, "attributes") else None
        if href and href.lower().startswith("tel:"):
            phone = normalize_phone(href[4:])
            if phone:
                phones.append(phone)
    if not phones:
        text = collapse_ws(node.text(separator=" ", strip=True)) or ""
        for match in re.finditer(r"\+?\d[\d\s\-()]{6,}\d", text):
            phone = normalize_phone(match.group(0))
            if phone:
                phones.append(phone)
    return clean_text_list(phones) or []


def extract_emails_from_node(node) -> list[str]:
    if not node:
        return []
    emails: list[str] = []
    for anchor in node.css('a[href^="mailto:"]'):
        href = anchor.attributes.get("href") if hasattr(anchor, "attributes") else None
        if href and href.lower().startswith("mailto:"):
            value = collapse_ws(href[7:])
            if value:
                emails.append(value)
    if not emails:
        text = collapse_ws(node.text(separator=" ", strip=True)) or ""
        for match in re.finditer(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text):
            emails.append(match.group(0))
    return clean_text_list(emails) or []


def parse_location_line(addr_text: Optional[str]) -> Optional[Dict[str, Any]]:
    text = collapse_ws(addr_text)
    if not text:
        return None

    match = re.match(r"^(?:(.*?),\s*)?(\d{4})\s+(.+)$", text)
    if match:
        street = collapse_ws(match.group(1))
        return {
            "streetAddress": street,
            "postalCode": match.group(2),
            "addressLocality": collapse_ws(match.group(3)),
        }

    match = re.match(r"^(\d{4})\s+(.+)$", text)
    if match:
        return {
            "streetAddress": None,
            "postalCode": match.group(1),
            "addressLocality": collapse_ws(match.group(2)),
        }

    if "," in text:
        street, locality = [collapse_ws(part) for part in text.split(",", 1)]
        return {
            "streetAddress": street,
            "postalCode": None,
            "addressLocality": locality,
        }

    return {
        "streetAddress": text,
        "postalCode": None,
        "addressLocality": None,
    }


def extract_footer_fields_from_html(html: str) -> dict[str, str]:
    out: dict[str, str] = {}
    finn_code = re.search(r"FINN-kode[\s\S]{0,160}?(\d{6,})", html, re.IGNORECASE)
    if finn_code:
        out["finnCode"] = finn_code.group(1)

    last_changed_iso = re.search(
        r"Sist\s*endret[\s\S]{0,200}?time[^>]*dateTime=[\"']([^\"']+)[\"']",
        html,
        re.IGNORECASE,
    )
    if last_changed_iso:
        out["lastChangedISO"] = last_changed_iso.group(1)

    last_changed = re.search(
        r"Sist\s*endret[\s\S]{0,200}?<time[^>]*>([^<]+)</time>",
        html,
        re.IGNORECASE,
    )
    if last_changed:
        out["lastChanged"] = collapse_ws(last_changed.group(1)) or last_changed.group(1).strip()
    return out


def text_after_bold_label(node) -> Optional[str]:
    if not node:
        return None
    label_node = node.css_first("span.font-bold, strong, b")
    label = node_text(label_node)
    full = collapse_ws(node.text(separator=" ", strip=True)) or ""
    if label:
        label_clean = label.rstrip(":").strip()
        for candidate in (label, f"{label_clean}:", label_clean):
            if full.casefold().startswith(candidate.casefold()):
                remainder = full[len(candidate) :].strip(" :")
                return collapse_ws(remainder)
    return full or None


def li_values_from_links(node) -> Optional[list[str]]:
    if not node:
        return None
    return clean_text_list(anchor.text(strip=True) for anchor in node.css("a"))


def find_section_by_heading(doc: HTMLParser, heading: str):
    needle = normalize_heading(heading)
    for heading_node in doc.css("h1, h2, h3"):
        if not needle or needle not in normalize_heading(node_text(heading_node)):
            continue
        ancestor = heading_node
        while ancestor is not None and (ancestor.tag or "").lower() != "section":
            ancestor = ancestor.parent
        if ancestor is not None:
            return ancestor
    return None


def find_header_section(doc: HTMLParser):
    for section in doc.css("section"):
        heading = section.css_first("h2")
        if not heading:
            continue
        text = collapse_ws(section.text(separator=" ", strip=True)) or ""
        if "Frist" in text and "Ansettelsesform" in text:
            return section
    return None


def extract_structured_core(jobposting: Optional[dict[str, Any]], url: str) -> dict[str, Any]:
    if not jobposting:
        return {}

    location = None
    job_location = jobposting.get("jobLocation")
    if isinstance(job_location, dict):
        address = job_location.get("address")
        if isinstance(address, dict):
            location = {
                "streetAddress": collapse_ws(address.get("streetAddress")),
                "postalCode": collapse_ws(address.get("postalCode")),
                "addressLocality": collapse_ws(address.get("addressLocality")),
            }

    employment_type = jobposting.get("employmentType")
    if isinstance(employment_type, list):
        employment_type = ", ".join(collapse_ws(value) for value in employment_type if collapse_ws(value))

    employer_homepage = None
    hiring_org = jobposting.get("hiringOrganization")
    if isinstance(hiring_org, dict):
        employer_homepage = collapse_ws(hiring_org.get("url"))

    deadline = None
    valid_through = collapse_ws(jobposting.get("validThrough"))
    if valid_through:
        try:
            dt = datetime.fromisoformat(valid_through)
            deadline = f"{dt.day}.{dt.month}.{dt.year}"
        except ValueError:
            deadline = valid_through

    identifier = None
    raw_identifier = jobposting.get("identifier")
    if isinstance(raw_identifier, dict):
        identifier = collapse_ws(raw_identifier.get("value"))
    elif isinstance(raw_identifier, str):
        identifier = collapse_ws(raw_identifier)

    return {
        "title": collapse_ws(jobposting.get("title")),
        "employer": collapse_ws(hiring_org.get("name")) if isinstance(hiring_org, dict) else None,
        "employerHomepage": employer_homepage,
        "employmentType": collapse_ws(employment_type),
        "deadline": deadline,
        "description_sections": description_sections_from_html(jobposting.get("description"), collapse_ws(jobposting.get("title"))),
        "location": location,
        "finnCode": identifier or derive_finn_code_from_url(url),
    }


def extract_dom_fields(html: str, url: str, trace: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    doc = HTMLParser(html)

    header_sec = find_header_section(doc)
    title = None
    employer = None
    deadline = None
    employment = None

    if trace is not None:
        trace.setdefault("dom", {})["header_section_found"] = bool(header_sec)

    if header_sec:
        title = node_text(header_sec.css_first("h2"))
        for paragraph in header_sec.css("p"):
            employer = node_text(paragraph)
            if employer:
                break
        for item in header_sec.css("li"):
            item_text = collapse_ws(item.text(separator=" ", strip=True)) or ""
            value = node_text(item.css_first("span.font-bold, strong, b")) or text_after_bold_label(item)
            if item_text.casefold().startswith("frist") and not deadline:
                deadline = value
            if item_text.casefold().startswith("ansettelsesform") and not employment:
                employment = value

    description_host = None
    body_sec = None
    for section in doc.css("section"):
        if section.css_first("h1") and section.css_first("div.import-decoration"):
            body_sec = section
            description_host = section.css_first("div.import-decoration")
            break
    if not description_host and body_sec:
        description_host = body_sec

    if trace is not None:
        trace.setdefault("dom", {})["body_section_found"] = bool(description_host)

    description_sections = extract_description_sections(description_host, title)

    details_sec = find_section_by_heading(doc, "Om arbeidsgiveren")
    sector = None
    number_of_positions = None
    location_text = None
    home_office = None
    industry = None
    function = None
    language = None
    education = None
    experience = None
    extent = None
    manager_role = None

    if trace is not None:
        trace.setdefault("dom", {})["details_section_found"] = bool(details_sec)

    if details_sec:
        for item in details_sec.css("li"):
            label = node_text(item.css_first("span.font-bold, strong, b"))
            key = label_key(label)
            if not key:
                continue
            if key == "sektor":
                sector = text_after_bold_label(item)
            elif key == "antall stillinger":
                raw = text_after_bold_label(item)
                if raw:
                    match = re.search(r"\d+", raw)
                    number_of_positions = int(match.group(0)) if match else None
            elif key == "sted":
                location_text = text_after_bold_label(item)
            elif key == "hjemmekontor":
                home_office = text_after_bold_label(item)
            elif key == "bransje":
                industry = li_values_from_links(item) or clean_text_list([text_after_bold_label(item)])
            elif key == "stillingsfunksjon":
                function = li_values_from_links(item) or clean_text_list([text_after_bold_label(item)])
            elif key.startswith("arbeidsspr"):
                language = text_after_bold_label(item)
            elif key == "arbeidsspråk":
                language = text_after_bold_label(item)
            elif key == "utdanning":
                education = text_after_bold_label(item)
            elif key == "arbeidserfaring":
                experience = text_after_bold_label(item)
            elif key == "heltid/deltid":
                extent = text_after_bold_label(item)
            elif key == "lederkategori":
                manager_role = text_after_bold_label(item)

    keywords = None
    keywords_sec = find_section_by_heading(doc, "Nøkkelord")
    if trace is not None:
        trace.setdefault("dom", {})["keywords_section_found"] = bool(keywords_sec)
    if keywords_sec:
        paragraph = keywords_sec.css_first("p")
        if paragraph:
            raw = collapse_ws(paragraph.text(strip=True))
            if raw:
                keywords = clean_text_list(part.strip() for part in raw.split(","))

    skills = None
    skills_sec = find_section_by_heading(doc, "Ferdigheter")
    if trace is not None:
        trace.setdefault("dom", {})["skills_section_found"] = bool(skills_sec)
    if skills_sec:
        skills = clean_text_list(deepest_text(item) for item in skills_sec.css("ul > li"))

    apply = None
    apply_anchor = doc.css_first("a#job-apply-button, a[data-job-application-type], a[href*='/job-apply/']")
    if apply_anchor and hasattr(apply_anchor, "attributes"):
        href = apply_anchor.attributes.get("href")
        apply = {
            "href": urljoin(url, href) if href else None,
            "label": node_text(apply_anchor),
            "type": apply_anchor.attributes.get("data-job-application-type"),
            "target": apply_anchor.attributes.get("target"),
            "attrs": {
                key: value
                for key in (
                    "data-job-application-type",
                    "data-braze-api-key",
                    "data-braze-endpoint",
                    "data-user-id",
                )
                if (value := apply_anchor.attributes.get(key)) not in (None, "")
            }
            or None,
        }

    employer_homepage = None
    for anchor in doc.css("a"):
        text = node_text(anchor)
        if text and text.casefold() == "hjemmeside" and hasattr(anchor, "attributes"):
            href = anchor.attributes.get("href")
            if href:
                employer_homepage = urljoin(url, href)
                break

    location_coords = None
    location_structured = None
    location_map_url = None
    location_map_image = None
    location_sec = find_section_by_heading(doc, "Firmaets beliggenhet")
    if trace is not None:
        trace.setdefault("dom", {})["location_section_found"] = bool(location_sec)
    if location_sec:
        paragraph = location_sec.css_first("p")
        addr_text = node_text(paragraph)
        if addr_text and not location_text:
            location_text = addr_text
        if addr_text:
            location_structured = parse_location_line(addr_text)

        map_anchor = location_sec.css_first("a[href*='map?']")
        if map_anchor and hasattr(map_anchor, "attributes"):
            href = map_anchor.attributes.get("href")
            if href:
                location_map_url = urljoin(url, href)
                lat_match = re.search(r"[?&]lat=([0-9.\-]+)", href)
                lon_match = re.search(r"[?&](?:lon|lng)=([0-9.\-]+)", href)
                if lat_match and lon_match:
                    try:
                        location_coords = {
                            "lat": float(lat_match.group(1)),
                            "lon": float(lon_match.group(1)),
                        }
                    except ValueError:
                        location_coords = None

        if not location_coords:
            image = location_sec.css_first("img[src*='staticmap'], img[src*='maptiles'], img")
            if image and hasattr(image, "attributes"):
                src = image.attributes.get("src")
                if src:
                    location_map_image = urljoin(url, src)
                    lat_match = re.search(r"[?&]lat=([0-9.\-]+)", src)
                    lon_match = re.search(r"[?&](?:lon|lng)=([0-9.\-]+)", src)
                    if lat_match and lon_match:
                        try:
                            location_coords = {
                                "lat": float(lat_match.group(1)),
                                "lon": float(lon_match.group(1)),
                            }
                        except ValueError:
                            location_coords = None

    contacts = None
    contacts_sec = find_section_by_heading(doc, "Spørsmål om stillingen")
    if trace is not None:
        trace.setdefault("dom", {})["contacts_section_found"] = bool(contacts_sec)
    if contacts_sec:
        found: list[Dict[str, Any]] = []
        for group in contacts_sec.css("ul"):
            current: Dict[str, Any] = {}
            for item in group.css("li"):
                label = label_key(node_text(item.css_first("span.font-bold, strong, b")))
                value_text = text_after_bold_label(item)
                if label.startswith("kontaktperson"):
                    if current:
                        found.append(current)
                        current = {}
                    current["name"] = value_text
                elif label.startswith("stillingstittel") or label == "tittel":
                    current["title"] = value_text
                elif label.startswith("mobil") or label.startswith("telefon"):
                    phones = extract_phones_from_node(item)
                    if phones:
                        current["phones"] = phones
                elif label in {"epost", "e-post", "email"}:
                    emails = extract_emails_from_node(item)
                    if emails:
                        current["emails"] = emails
            if current:
                found.append(current)
        contacts = [contact for contact in found if any(contact.get(key) for key in ("name", "title", "phones", "emails"))] or None

    finn_code = None
    last_changed = None
    last_changed_iso = None
    info_sec = find_section_by_heading(doc, "Annonseinformasjon")
    if trace is not None:
        trace.setdefault("dom", {})["info_section_found"] = bool(info_sec)
    if info_sec:
        for item in info_sec.css("li"):
            label = label_key(node_text(item.css_first("span.font-bold, strong, b")))
            if not label:
                continue
            if "finn-kode" in label or ("finn" in label and "kode" in label):
                match = re.search(r"\d+", text_after_bold_label(item) or "")
                if match:
                    finn_code = match.group(0)
            elif "sist endret" in label:
                time_node = item.css_first("time")
                if time_node:
                    last_changed = node_text(time_node)
                    last_changed_iso = collapse_ws(time_node.attributes.get("datetime")) if hasattr(time_node, "attributes") else None
                if not last_changed:
                    last_changed = text_after_bold_label(item)

    if not finn_code:
        report_anchor = doc.css_first('a[href*="/report/ad?adId="]')
        if report_anchor and hasattr(report_anchor, "attributes"):
            href = report_anchor.attributes.get("href")
            if href:
                match = re.search(r"adId=(\d+)", href)
                if match:
                    finn_code = match.group(1)

    return {
        "title": title,
        "employer": employer,
        "deadline": deadline,
        "employmentType": employment,
        "location_text": location_text,
        "sector": sector,
        "industry": industry,
        "function": function,
        "language": language,
        "education": education,
        "experience": experience,
        "extent": extent,
        "managerRole": manager_role,
        "numberOfPositions": number_of_positions,
        "homeOffice": home_office,
        "keywords": keywords,
        "skills": skills,
        "apply": apply,
        "employerHomepage": employer_homepage,
        "location": location_structured,
        "location_coordinates": location_coords,
        "location_map_url": location_map_url,
        "location_map_image": location_map_image,
        "contacts": contacts,
        "finnCode": finn_code,
        "lastChanged": last_changed,
        "lastChangedISO": last_changed_iso,
        "description_sections": description_sections,
    }


def prefer_visible(visible: Any, structured: Any) -> Any:
    if visible in (None, "", [], {}):
        return structured
    return visible


def prefer_nonempty_dict(visible: Optional[dict[str, Any]], structured: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if visible and any(value not in (None, "", [], {}) for value in visible.values()):
        return visible
    return structured


def derive_finn_code_from_url(url: str) -> Optional[str]:
    direct_match = re.search(r"/ad/(\d+)", url)
    if direct_match:
        return direct_match.group(1)

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("finnkode", "adid", "adId"):
        values = query.get(key)
        if values:
            value = collapse_ws(values[0])
            if value:
                return value
    return None


def reorder_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    order = [
        "url",
        "extractedAt",
        "finnCode",
        "lastChanged",
        "lastChangedISO",
        "title",
        "employer",
        "employerHomepage",
        "deadline",
        "employmentType",
        "sector",
        "language",
        "education",
        "experience",
        "extent",
        "managerRole",
        "numberOfPositions",
        "homeOffice",
        "embeddedFacetHints",
        "location",
        "location_text",
        "location_coordinates",
        "location_map_url",
        "location_map_image",
        "industry",
        "function",
        "keywords",
        "skills",
        "description_sections",
        "contacts",
        "apply",
    ]
    ordered: Dict[str, Any] = {}
    for key in order:
        if key in data:
            ordered[key] = data[key]
    for key, value in data.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def parse_finn_job_html(
    url: str,
    html: str,
    debug: bool = False,
    trace: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if debug and trace is None:
        trace = {}

    jobposting = extract_jobposting_jsonld(html)
    structured = extract_structured_core(jobposting, url)
    dom = extract_dom_fields(html, url, trace=trace)
    metas = extract_meta(html, ["adid", "og:title", "twitter:title"])
    footer = extract_footer_fields_from_html(html)

    finn_code = (
        prefer_visible(dom.get("finnCode"), structured.get("finnCode"))
        or metas.get("adid")
        or footer.get("finnCode")
        or derive_finn_code_from_url(url)
    )
    last_changed = dom.get("lastChanged") or footer.get("lastChanged")
    last_changed_iso = dom.get("lastChangedISO") or footer.get("lastChangedISO")
    embedded_hints = extract_embedded_facet_hints(html)

    if not last_changed and last_changed_iso:
        try:
            iso_value = last_changed_iso.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso_value)
            last_changed = f"{dt.day}.{dt.month}.{dt.year}, {dt.hour:02d}:{dt.minute:02d}"
        except ValueError:
            pass

    result = {
        "url": url,
        "extractedAt": datetime.utcnow().isoformat() + "Z",
        "finnCode": finn_code,
        "lastChanged": last_changed,
        "lastChangedISO": last_changed_iso,
        "title": prefer_visible(dom.get("title"), structured.get("title")),
        "employer": prefer_visible(dom.get("employer"), structured.get("employer")),
        "employerHomepage": prefer_visible(dom.get("employerHomepage"), structured.get("employerHomepage")),
        "deadline": prefer_visible(dom.get("deadline"), structured.get("deadline")),
        "employmentType": prefer_visible(dom.get("employmentType"), structured.get("employmentType")),
        "sector": dom.get("sector") or ((embedded_hints.get("job_sector") or {}).get("label")),
        "language": dom.get("language") or ", ".join((embedded_hints.get("working_language") or {}).get("labels") or []),
        "education": dom.get("education"),
        "experience": dom.get("experience"),
        "extent": dom.get("extent") or ((embedded_hints.get("extent") or {}).get("label")),
        "managerRole": dom.get("managerRole"),
        "numberOfPositions": dom.get("numberOfPositions"),
        "homeOffice": dom.get("homeOffice") or ((embedded_hints.get("home_office") or {}).get("label")),
        "embeddedFacetHints": embedded_hints or None,
        "location": prefer_nonempty_dict(dom.get("location"), structured.get("location")),
        "location_text": dom.get("location_text"),
        "location_coordinates": dom.get("location_coordinates"),
        "location_map_url": dom.get("location_map_url"),
        "location_map_image": dom.get("location_map_image"),
        "industry": dom.get("industry"),
        "function": dom.get("function"),
        "keywords": dom.get("keywords"),
        "skills": dom.get("skills"),
        "description_sections": prefer_visible(dom.get("description_sections"), structured.get("description_sections")),
        "contacts": dom.get("contacts"),
        "apply": dom.get("apply"),
    }
    result = reorder_fields(result)

    if debug and trace is not None:
        trace["jobposting_found"] = bool(jobposting)
        trace["field_sources"] = {
            "title": "dom" if dom.get("title") else "jsonld",
            "employer": "dom" if dom.get("employer") else "jsonld",
            "employerHomepage": "dom" if dom.get("employerHomepage") else "jsonld",
            "deadline": "dom" if dom.get("deadline") else "jsonld",
            "employmentType": "dom" if dom.get("employmentType") else "jsonld",
            "location": "dom" if dom.get("location") else "jsonld",
            "description_sections": "dom" if dom.get("description_sections") else "jsonld",
            "finnCode": "dom/meta/footer",
            "lastChanged": "dom/footer",
        }
        result["_debug"] = trace
    return result


def extract_finn_job(
    url: str,
    debug: bool = False,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    trace: Optional[Dict[str, Any]] = {} if debug else None
    html = fetch_html(url, trace=trace, client=client)
    return parse_finn_job_html(url, html, debug=debug, trace=trace)


def derive_default_outfile(url: str, result: Dict[str, Any]) -> str:
    ad_id = derive_finn_code_from_url(url)
    if not ad_id:
        finn_code = collapse_ws(result.get("finnCode"))
        ad_id = finn_code
    if not ad_id:
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        return f"finn_job_{timestamp}.json"
    return f"finn_job_{ad_id}.json"


if __name__ == "__main__":
    import sys

    args = [arg for arg in sys.argv[1:] if arg.strip()]
    if not args:
        print("Usage: python parse_job_ad.py <finn_job_url> [--debug] [--out PATH]")
        sys.exit(1)

    debug_flag = False
    if "--debug" in args:
        debug_flag = True
        args.remove("--debug")

    out_path: Optional[str] = None
    if "--out" in args:
        try:
            index = args.index("--out")
            out_path = args[index + 1]
            del args[index : index + 2]
        except Exception:
            print("Error: --out requires a file path")
            sys.exit(2)

    if not args:
        print("Usage: python parse_job_ad.py <finn_job_url> [--debug] [--out PATH]")
        sys.exit(1)

    target_url = args[0].strip()
    try:
        result = extract_finn_job(target_url, debug=debug_flag)
        if not out_path:
            out_path = derive_default_outfile(target_url, result)
        with open(out_path, "w", encoding="utf-8") as file_handle:
            json.dump(result, file_handle, ensure_ascii=False, indent=2)
        if "--out" not in sys.argv[1:]:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print(f"Wrote {out_path}")
    except Exception as exc:
        import traceback

        print(f"Extractor failed: {exc!r}")
        traceback.print_exc()
        sys.exit(1)
