"""Collapse the same development appearing in several sources into one item.

Two passes, in order of confidence:

1. **Exact identity.** An extracted arXiv id or GitHub repo is definitive - an
   HN story linking to arxiv.org/abs/2501.00001 *is* that paper.
2. **Title similarity.** For items with no shared identity, token-set overlap
   above a threshold. Deliberately conservative: a false merge silently hides a
   real item from the brief, which is worse than a duplicate the reader can see
   and ignore.

Duplicates are marked with `canonical_id` rather than deleted. Nothing fetched
is ever thrown away, the decision stays auditable, and it can be undone.
"""

import logging
import sqlite3
from typing import Optional

from ai_monitor.orchestrator.canonical import canonical_identity, title_similarity

log = logging.getLogger(__name__)

# Token-set overlap above which two titles are treated as the same work.
# High because the cost of a wrong merge (an item vanishing) exceeds the cost
# of a missed merge (a visible duplicate).
TITLE_THRESHOLD = 0.75

# Which source wins when the same thing appears in several. arXiv and GitHub
# are primary artifacts; an HN story is commentary pointing at one of them.
SOURCE_PRIORITY = {"arxiv": 0, "github": 1, "hn": 2}


def _priority(row: sqlite3.Row) -> tuple:
    """Sort key picking the canonical item: primary source, then oldest id."""
    return (SOURCE_PRIORITY.get(row["source"], 99), row["id"])


def find_duplicates(rows: list[sqlite3.Row]) -> dict[int, int]:
    """Map duplicate item id -> canonical item id.

    Pure function over rows so it can be tested without a database.
    """
    by_identity: dict[str, list[sqlite3.Row]] = {}
    unidentified: list[sqlite3.Row] = []

    for row in rows:
        identity = canonical_identity(row["source"], row["source_id"], row["url"])
        if identity:
            by_identity.setdefault(identity, []).append(row)
        else:
            unidentified.append(row)

    mapping: dict[int, int] = {}

    # Pass 1: exact shared identity.
    for identity, group in by_identity.items():
        if len(group) < 2:
            continue
        canonical, *duplicates = sorted(group, key=_priority)
        for dup in duplicates:
            mapping[dup["id"]] = canonical["id"]
            log.debug(
                "dedup: %s -> %s via %s", dup["source_id"], canonical["source_id"], identity
            )

    # Pass 2: title similarity, among items that pass 1 left alone. Compare
    # against group representatives too, so an HN story with no extractable
    # link can still attach to the paper it discusses.
    representatives = [
        sorted(group, key=_priority)[0] for group in by_identity.values()
    ]
    candidates = representatives + unidentified

    for i, row in enumerate(unidentified):
        if row["id"] in mapping:
            continue
        for other in candidates:
            if other["id"] == row["id"] or other["id"] in mapping:
                continue
            if title_similarity(row["title"], other["title"]) < TITLE_THRESHOLD:
                continue

            canonical, dup = sorted([row, other], key=_priority)
            if dup["id"] == canonical["id"]:
                continue
            mapping[dup["id"]] = canonical["id"]
            log.debug(
                "dedup: %s -> %s via title similarity",
                dup["source_id"],
                canonical["source_id"],
            )
            break

    return mapping


def apply(conn: sqlite3.Connection, mapping: dict[int, int]) -> int:
    """Mark duplicates. Returns the number of items collapsed."""
    for dup_id, canonical_id in mapping.items():
        conn.execute(
            "UPDATE items SET canonical_id = ? WHERE id = ?", (canonical_id, dup_id)
        )
    conn.commit()
    return len(mapping)


def run(conn: sqlite3.Connection, limit: Optional[int] = None) -> int:
    """Find and mark duplicates across all stored items."""
    query = "SELECT id, source, source_id, title, url FROM items ORDER BY id"
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = conn.execute(query).fetchall()

    mapping = find_duplicates(rows)
    count = apply(conn, mapping)
    if count:
        log.info("dedup: collapsed %d duplicate items", count)
    return count
