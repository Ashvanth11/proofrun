import logging
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

import httpx

from ai_monitor.storage import db
from ai_monitor.storage.models import Item, Source

log = logging.getLogger(__name__)

BASE_URL = "https://hacker-news.firebaseio.com/v0"

# HN is a general-interest firehose, so items are keyword-filtered before
# storage - unlike arXiv/GitHub, where the query itself already narrows things.
AI_KEYWORDS = [
    "llm", "gpt", "claude", "gemini", "llama", "mistral", "qwen",
    "transformer", "diffusion", "embedding", "rag", "fine-tun",
    "ai agent", "agentic", "agent", "openai", "anthropic", "deepmind",
    "hugging face", "huggingface", "pytorch", "tensorflow", "inference",
    "machine learning", "neural", "artificial intelligence", " ai ",
    "chatbot", "prompt", "context window", "mcp", "reasoning model",
]

DEFAULT_MIN_SCORE = 20


def _compile_pattern(keywords: list[str]) -> re.Pattern:
    # Word-boundary match so "agent" doesn't fire on "urgent" and "ai" doesn't
    # fire on "said". Multi-word phrases are matched literally.
    parts = [
        rf"\b{re.escape(k.strip())}\b" if " " not in k.strip() else re.escape(k.strip())
        for k in keywords
    ]
    return re.compile("|".join(parts), re.IGNORECASE)


PATTERN = _compile_pattern(AI_KEYWORDS)


def is_relevant(title: str, pattern: Optional[re.Pattern] = None) -> bool:
    return bool((pattern or PATTERN).search(title or ""))


def parse_story(story: dict) -> Item:
    """Map an HN story onto the normalized Item.

    HN stories carry no body text - only a title and an outbound link - so
    `content` stays sparse. Downstream analysis of an HN item is effectively
    title-only; fetching the linked page is deliberately out of scope here.
    """
    story_id = str(story["id"])
    discussion = f"https://news.ycombinator.com/item?id={story_id}"

    return Item(
        source=Source.HN,
        source_id=story_id,
        title=story.get("title", ""),
        url=story.get("url") or discussion,
        content=story.get("text", "") or "",
        authors=[story["by"]] if story.get("by") else [],
        published_at=(
            datetime.fromtimestamp(story["time"], tz=timezone.utc)
            if story.get("time")
            else None
        ),
        raw={
            "hn_id": story["id"],
            "score": story.get("score"),
            "comments": story.get("descendants"),
            "discussion_url": discussion,
            "type": story.get("type"),
        },
    )


def _get_json(client: httpx.Client, path: str):
    response = client.get(f"{BASE_URL}/{path}", timeout=20.0)
    response.raise_for_status()
    return response.json()


def fetch(
    listing: str = "topstories",
    scan: int = 100,
    min_score: int = DEFAULT_MIN_SCORE,
    max_results: int = 25,
) -> list[Item]:
    """Scan a HN listing and keep AI-relevant stories above a score floor.

    `scan` bounds how many story ids are inspected: the listing endpoint gives
    ids only, so every candidate costs one request. Those are issued in
    parallel since they are independent.
    """
    items: list[Item] = []

    with httpx.Client() as client:
        try:
            story_ids = _get_json(client, f"{listing}.json")[:scan]
        except httpx.HTTPError as exc:
            log.error("hn: could not fetch %s listing: %s", listing, exc)
            return []

        def load(story_id: int) -> Optional[dict]:
            try:
                return _get_json(client, f"item/{story_id}.json")
            except httpx.HTTPError:
                log.warning("hn: failed to fetch story %s", story_id)
                return None

        with ThreadPoolExecutor(max_workers=10) as pool:
            stories = list(pool.map(load, story_ids))

    for story in stories:
        if not story or story.get("type") != "story":
            continue
        if (story.get("score") or 0) < min_score:
            continue
        if not is_relevant(story.get("title", "")):
            continue
        try:
            items.append(parse_story(story))
        except Exception:
            log.exception("failed to parse HN story %s; skipping", story.get("id"))

    items.sort(key=lambda i: i.raw.get("score") or 0, reverse=True)
    return items[:max_results]


def fetch_and_store(
    conn: sqlite3.Connection,
    listing: str = "topstories",
    scan: int = 100,
    min_score: int = DEFAULT_MIN_SCORE,
    max_results: int = 25,
) -> list[int]:
    items = fetch(
        listing=listing, scan=scan, min_score=min_score, max_results=max_results
    )
    ids = [db.upsert_item(conn, item) for item in items]
    log.info("hn: fetched %d items", len(ids))
    return ids
