import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional

import httpx

from ai_monitor.storage import db
from ai_monitor.storage.models import Item, Source

log = logging.getLogger(__name__)

API_URL = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"

DEFAULT_CATEGORIES = ["cs.AI", "cs.LG", "cs.CL"]

# Trailing version marker on an arXiv id, e.g. "2608.27454v2".
_VERSION_RE = re.compile(r"v\d+$")


def _strip_version(arxiv_id: str) -> str:
    """Drop the vN suffix so a revised paper updates its row instead of adding one."""
    return _VERSION_RE.sub("", arxiv_id)


def _parse_datetime(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


def _text(entry: ET.Element, tag: str) -> str:
    node = entry.find(f"{ATOM}{tag}")
    if node is None or node.text is None:
        return ""
    return " ".join(node.text.split())


def parse_entry(entry: ET.Element) -> Item:
    raw_id = _text(entry, "id").rsplit("/abs/", 1)[-1]
    categories = [
        c.get("term")
        for c in entry.findall(f"{ATOM}category")
        if c.get("term")
    ]
    primary = entry.find(f"{ARXIV_NS}primary_category")

    return Item(
        source=Source.ARXIV,
        source_id=_strip_version(raw_id),
        title=_text(entry, "title"),
        url=f"https://arxiv.org/abs/{_strip_version(raw_id)}",
        content=_text(entry, "summary"),
        authors=[
            name.text.strip()
            for author in entry.findall(f"{ATOM}author")
            if (name := author.find(f"{ATOM}name")) is not None and name.text
        ],
        published_at=_parse_datetime(_text(entry, "published") or None),
        raw={
            "arxiv_id": raw_id,
            "categories": categories,
            "primary_category": primary.get("term") if primary is not None else None,
            "updated": _text(entry, "updated"),
            "pdf_url": f"https://arxiv.org/pdf/{raw_id}",
        },
    )


def fetch(
    categories: Optional[list[str]] = None,
    max_results: int = 50,
    timeout: float = 30.0,
) -> list[Item]:
    """Fetch the most recently submitted papers in the given arXiv categories."""
    categories = categories or DEFAULT_CATEGORIES
    query = " OR ".join(f"cat:{c}" for c in categories)
    params = {
        "search_query": query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": max_results,
    }

    # arXiv redirects plain http:// to https://, so follow redirects.
    response = httpx.get(
        API_URL, params=params, timeout=timeout, follow_redirects=True
    )
    response.raise_for_status()

    feed = ET.fromstring(response.text)
    items = []
    for entry in feed.findall(f"{ATOM}entry"):
        try:
            items.append(parse_entry(entry))
        except Exception:
            log.exception("failed to parse arXiv entry; skipping")
    return items


def fetch_and_store(
    conn: sqlite3.Connection,
    categories: Optional[list[str]] = None,
    max_results: int = 50,
) -> list[int]:
    items = fetch(categories=categories, max_results=max_results)
    ids = [db.upsert_item(conn, item) for item in items]
    log.info("arxiv: fetched %d items", len(ids))
    return ids
