import json
import logging
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import anthropic

from ai_monitor.analysis.analyzer import Usage

log = logging.getLogger(__name__)

SYNTHESIS_MODEL = "claude-sonnet-5"
REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"

# Phase 2 placeholder: one call over the week's items. Phase 7 replaces this
# with themed synthesis and map-reduce grouping for larger corpora.
SYSTEM_PROMPT = """You write a weekly brief on AI/ML developments for a researcher.

You are given the week's items with their relevance scores and matched interest areas.

Write markdown covering what actually happened this week. Group related items together
rather than listing them one by one. Lead with what matters most. Be concrete about what
each piece of work does; skip hype language and filler like "the landscape is evolving".

Do not invent items, links, or findings that are not in the input."""


def fetch_analyzed_items(
    conn: sqlite3.Connection, min_score: float = 0.0, limit: int = 100
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT i.title, i.url, i.source, a.summary, a.relevance_score,
               a.matched_areas, a.justification
        FROM analyses a
        JOIN items i ON i.id = a.item_id
        WHERE a.relevance_score >= ? AND i.canonical_id IS NULL
        ORDER BY a.relevance_score DESC
        LIMIT ?
        """,
        (min_score, limit),
    ).fetchall()


def build_prompt(rows: list[sqlite3.Row]) -> str:
    lines = []
    for row in rows:
        areas = ", ".join(json.loads(row["matched_areas"])) or "none"
        lines.append(
            f"- [{row['source']}] {row['title']}\n"
            f"  url: {row['url']}\n"
            f"  score: {row['relevance_score']:.2f} | areas: {areas}\n"
            f"  summary: {row['summary']}"
        )
    return "This week's items:\n\n" + "\n\n".join(lines)


def week_of(when: Optional[date] = None) -> str:
    when = when or date.today()
    year, week, _ = when.isocalendar()
    return f"{year}-W{week:02d}"


def synthesize(
    rows: list[sqlite3.Row],
    client: Optional[anthropic.Anthropic] = None,
    model: str = SYNTHESIS_MODEL,
) -> tuple[str, Usage]:
    client = client or anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_prompt(rows)}],
    )
    markdown = "".join(b.text for b in response.content if b.type == "text")
    usage = Usage(
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        model=model,
    )
    return markdown, usage


def store_brief(
    conn: sqlite3.Connection, week: str, markdown: str, item_count: int
) -> Path:
    conn.execute(
        "INSERT INTO briefs (week_of, markdown, item_count, created_at) VALUES (?, ?, ?, ?)",
        (week, markdown, item_count, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()

    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / f"{week}.md"
    path.write_text(markdown)
    return path


def run(
    conn: sqlite3.Connection,
    client: Optional[anthropic.Anthropic] = None,
    min_score: float = 0.0,
    model: str = SYNTHESIS_MODEL,
) -> tuple[Optional[Path], Optional[Usage]]:
    rows = fetch_analyzed_items(conn, min_score=min_score)
    if not rows:
        log.warning("no analyzed items to synthesize")
        return None, None

    markdown, usage = synthesize(rows, client=client, model=model)
    path = store_brief(conn, week_of(), markdown, len(rows))
    log.info("wrote brief to %s (%d items)", path, len(rows))
    return path, usage
