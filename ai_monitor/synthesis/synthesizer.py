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

SYSTEM_PROMPT = """You write a weekly brief on AI/ML developments for a researcher.

You are given the week's items with their relevance scores, sources, and matched
interest areas.

Your job is to identify 3-5 THEMES and explain them - not to list the items. A theme
is a claim about what is happening that several items support together. "Three groups
independently converged on treating agent memory as a retrieval problem" is a theme.
"Agent papers" is a category, not a theme, and is not useful.

For each theme:
- State what is happening, concretely.
- Cite the specific items that support it, with their links.
- Say why it matters - what it changes, or what it suggests about where things go.

Rules:
- A theme needs at least two items. One item on its own goes in "Notable individual
  items" at the end, briefly.
- Do not manufacture connections. If the week's items genuinely do not connect, say so
  and write fewer themes. A short honest brief beats a padded one.
- Never invent items, links, findings, or numbers not present in the input.
- Skip hype language. No "the landscape is rapidly evolving", no "exciting times".
- Write for someone who will decide what to read based on this. Be concrete about what
  each piece of work actually does.

Structure the output as markdown: a short header line with counts, then themes as `##`
sections, then a brief "Notable individual items" section if any warrant it, then a
one-line methodology footer."""

# Above this many items, a single call both strains the context window and
# produces mushier themes, so items are grouped by interest area and
# synthesized per group before being combined (map-reduce).
GROUPING_THRESHOLD = 40

GROUP_PROMPT = """You are summarizing one slice of a week's AI/ML developments, covering
a single interest area. Identify the themes within this slice using the same rules as a
full brief: a theme is a claim several items support, needs at least two items, and must
cite them with links. Output markdown `##` sections only - no preamble, no header, no
footer. Another step will combine your output with other slices."""

COMBINE_PROMPT = """You are assembling a weekly brief from per-area summaries that were
written independently.

Merge them into one coherent brief. Where two areas produced overlapping themes, combine
them into a single stronger theme rather than repeating both. Drop the weakest themes if
more than five survive - the brief should be readable, not exhaustive.

Keep every link. Do not invent items or findings. Output the final brief as markdown:
a short header line with counts, `##` theme sections, a brief "Notable individual items"
section if warranted, and a one-line methodology footer."""


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


def group_by_area(rows: list[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    """Bucket items by matched interest area.

    An item matching several areas appears in each - themes cross areas, and
    the combine step is responsible for merging the resulting overlap.
    """
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        areas = json.loads(row["matched_areas"]) or ["uncategorized"]
        for area in areas:
            groups.setdefault(area, []).append(row)
    return groups


def _call(
    client, model: str, system: str, content: str, max_tokens: int = 4096
) -> tuple[str, Usage]:
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    return text, Usage(
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        model=model,
    )


def synthesize(
    rows: list[sqlite3.Row],
    client: Optional[anthropic.Anthropic] = None,
    model: str = SYNTHESIS_MODEL,
    grouping_threshold: int = GROUPING_THRESHOLD,
) -> tuple[str, Usage]:
    """Write the brief, mapping over interest areas when the corpus is large."""
    client = client or anthropic.Anthropic()

    if len(rows) <= grouping_threshold:
        return _call(client, model, SYSTEM_PROMPT, build_prompt(rows))

    groups = group_by_area(rows)
    log.info(
        "synthesizing %d items across %d areas via map-reduce",
        len(rows),
        len(groups),
    )

    total = Usage(model=model)
    summaries = []
    for area, items in sorted(groups.items()):
        if not items:
            continue
        text, usage = _call(
            client,
            model,
            GROUP_PROMPT,
            f"Interest area: {area}\n\n{build_prompt(items)}",
        )
        total.input_tokens += usage.input_tokens
        total.output_tokens += usage.output_tokens
        summaries.append(f"### Area: {area}\n\n{text}")

    combined, usage = _call(
        client,
        model,
        COMBINE_PROMPT,
        f"Total items this week: {len(rows)}\n\n" + "\n\n---\n\n".join(summaries),
    )
    total.input_tokens += usage.input_tokens
    total.output_tokens += usage.output_tokens
    return combined, total


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
