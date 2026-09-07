"""Deciding which items are worth spending a model call on.

The analyzer costs money per item. Items with no lexical overlap with any
interest area are almost never relevant, and a keyword check is free. So a
cheap pre-filter runs first and drops the obvious misses before the LLM sees
them.

The filter is deliberately permissive. A false drop is invisible - the item
never appears anywhere, and nothing signals that it was skipped - while a false
keep costs one cheap Haiku call. Those costs are not symmetric, so the filter
errs heavily toward keeping.
"""

import logging
import re
import sqlite3
from typing import Optional

from pydantic import BaseModel

from ai_monitor import keywords
from ai_monitor.config.settings import InterestArea, settings

log = logging.getLogger(__name__)

# Sources whose items are always analyzed regardless of keyword hits, because
# something upstream already narrowed them:
#
#   arxiv - fetched from AI categories, and abstracts use vocabulary the
#           configured keywords will not always cover.
#   hn    - the watcher already keyword-filters titles for AI relevance at
#           fetch time. Applying a second, different keyword filter here is
#           redundant and actively harmful: HN titles name entities rather
#           than describe work ("GPT-6 Astra", "Nvidia agrees to acquire
#           Hugging Face"), so a vocabulary filter drops exactly the
#           ecosystem news the interest areas now ask for.
#
# GitHub is not on this list: its search query narrows by topic, but
# descriptions are prose and the keyword check still earns its place there.
ALWAYS_ANALYZE = {"arxiv", "hn"}


class RoutingDecision(BaseModel):
    item_id: int
    analyze: bool
    reason: str
    matched_keywords: list[str] = []


def _keyword_pattern(interests: dict[str, InterestArea]) -> re.Pattern:
    return keywords.build_pattern(
        kw for area in interests.values() for kw in area.keywords
    )


def matched_keywords(
    text: str, interests: Optional[dict[str, InterestArea]] = None
) -> list[str]:
    interests = interests if interests is not None else settings.interests
    return keywords.find_matches(text or "", _keyword_pattern(interests))


def route_item(
    item_id: int,
    source: str,
    title: str,
    content: str,
    interests: Optional[dict[str, InterestArea]] = None,
) -> RoutingDecision:
    """Decide whether an item is worth an analyzer call."""
    if source in ALWAYS_ANALYZE:
        return RoutingDecision(
            item_id=item_id, analyze=True, reason=f"{source} is pre-filtered upstream"
        )

    hits = matched_keywords(f"{title}\n{content}", interests)
    if hits:
        return RoutingDecision(
            item_id=item_id,
            analyze=True,
            reason=f"matched {len(hits)} interest keyword(s)",
            matched_keywords=hits,
        )

    return RoutingDecision(
        item_id=item_id, analyze=False, reason="no interest keywords present"
    )


def route(
    conn: sqlite3.Connection,
    interests: Optional[dict[str, InterestArea]] = None,
) -> tuple[list[int], list[RoutingDecision]]:
    """Partition unanalyzed items into those worth analyzing and those not.

    Returns (item_ids_to_analyze, all_decisions). Already-analyzed items are
    excluded; content_hash handles those separately.
    """
    rows = conn.execute(
        """
        SELECT i.id, i.source, i.title, i.content
        FROM items i
        LEFT JOIN analyses a ON a.item_id = i.id
        WHERE a.id IS NULL AND i.canonical_id IS NULL
        """
    ).fetchall()

    decisions = [
        route_item(r["id"], r["source"], r["title"], r["content"], interests)
        for r in rows
    ]
    keep = [d.item_id for d in decisions if d.analyze]

    dropped = len(decisions) - len(keep)
    if dropped:
        log.info(
            "routing: %d items to analyze, %d dropped by keyword pre-filter",
            len(keep),
            dropped,
        )
    return keep, decisions
