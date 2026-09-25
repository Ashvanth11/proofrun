import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import anthropic
from pydantic import BaseModel, Field

from ai_monitor.config.settings import InterestArea, settings
from ai_monitor.storage.models import Item

log = logging.getLogger(__name__)

ANALYZER_MODEL = "claude-haiku-4-5"

# USD per million tokens, per model. Verify against current pricing before
# quoting these numbers anywhere.
PRICING = {
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
}

SYSTEM_PROMPT = """You score AI/ML developments for a researcher tracking specific interest areas.

For each item you are given a title and abstract/description, plus the researcher's interest areas.

Score relevance on this scale:
- 0.0-0.2: unrelated to any interest area
- 0.3-0.5: touches an area tangentially, or is routine incremental work
- 0.6-0.8: solidly within an interest area and worth reading
- 0.9-1.0: directly advances an interest area in a way the researcher should not miss

Only list matched_areas that the item genuinely addresses. An item matching nothing
gets an empty list and a low score. Do not inflate scores; most items are routine.

Score the significance of the development itself, not the amount of detail you were
given about it. Some sources supply a full abstract; others supply only a title and a
link, and a short entry is not evidence of an unimportant development. A major model
release or acquisition described in one line is still major. Where the content is
thin, judge from the title and say in the justification that you had limited detail -
do not discount the score for it.

Keep the summary to 1-2 sentences describing what the work actually does, not why it
is exciting. Keep the justification to one sentence explaining the score."""


class AnalysisResult(BaseModel):
    summary: str = Field(description="1-2 sentence factual summary of the work")
    relevance_score: float = Field(ge=0.0, le=1.0)
    matched_areas: list[str] = Field(
        default_factory=list, description="Names of interest areas this item matches"
    )
    justification: str = Field(description="One sentence explaining the score")


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""

    @property
    def cost_usd(self) -> float:
        if self.model.startswith("ollama/"):
            return 0.0  # local inference
        rates = PRICING.get(self.model)
        if not rates:
            raise ValueError(f"no pricing for model {self.model!r}; estimated cost is unknown")
        return (
            self.input_tokens * rates["input"]
            + self.output_tokens * rates["output"]
        ) / 1_000_000


def render_interests(interests: dict[str, InterestArea]) -> str:
    lines = []
    for name, area in interests.items():
        keywords = ", ".join(area.keywords)
        lines.append(
            f"- {name}: {area.description.strip()}\n  keyword hints: {keywords}"
        )
    return "\n".join(lines)


def build_prompt(item: Item, interests: dict[str, InterestArea]) -> str:
    return (
        f"Interest areas:\n{render_interests(interests)}\n\n"
        f"---\n"
        f"Source: {item.source.value}\n"
        f"Title: {item.title}\n"
        f"Content: {item.content}\n"
    )


def content_hash(
    item: Item,
    interests: Optional[dict[str, InterestArea]] = None,
    prompt: str = "",
) -> str:
    """Hash of everything that determines an analysis, so re-runs skip safely.

    This hashes the *entire model input* - system prompt, interest areas, and
    item text - rather than the item alone. An analysis is a function of all
    three, and any of them changing must invalidate the cached result.

    Getting this wrong is a quiet failure that bit this project three times:
    editing the prompt, switching models, and (caught here before it shipped)
    editing interests.yaml would each have left every item "skipped
    (unchanged)", making a real change indistinguishable from one that did
    nothing. Hashing the rendered input rather than maintaining a version
    number means there is no bump to forget. The model is compared separately,
    in needs_analysis, since it is stored as its own column.
    """
    interests = interests if interests is not None else settings.interests
    payload = f"{prompt or SYSTEM_PROMPT}\n{build_prompt(item, interests)}"
    return hashlib.sha256(payload.encode()).hexdigest()


def analyze(
    item: Item,
    client: Optional[anthropic.Anthropic] = None,
    interests: Optional[dict[str, InterestArea]] = None,
    model: str = ANALYZER_MODEL,
) -> tuple[AnalysisResult, Usage]:
    """Score one item against the interest areas with a single structured call."""
    interests = interests if interests is not None else settings.interests
    client = client or anthropic.Anthropic()

    response = client.messages.parse(
        model=model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_prompt(item, interests)}],
        output_format=AnalysisResult,
    )

    result = response.parsed_output
    usage = Usage(
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        model=model,
    )

    unknown = set(result.matched_areas) - set(interests)
    if unknown:
        log.warning("dropping unrecognized areas %s for %s", unknown, item.source_id)
        result.matched_areas = [a for a in result.matched_areas if a in interests]

    return result, usage


def store_analysis(
    conn: sqlite3.Connection,
    item_id: int,
    result: AnalysisResult,
    item_hash: str,
    model: str,
) -> None:
    conn.execute(
        """
        INSERT INTO analyses
            (item_id, summary, relevance_score, matched_areas, justification,
             model, content_hash, analyzed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            summary = excluded.summary,
            relevance_score = excluded.relevance_score,
            matched_areas = excluded.matched_areas,
            justification = excluded.justification,
            model = excluded.model,
            content_hash = excluded.content_hash,
            analyzed_at = excluded.analyzed_at
        """,
        (
            item_id,
            result.summary,
            result.relevance_score,
            json.dumps(result.matched_areas),
            result.justification,
            model,
            item_hash,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def needs_analysis(
    conn: sqlite3.Connection,
    item_id: int,
    item_hash: str,
    model: str = "",
) -> bool:
    """False when this exact content was already analyzed by this model.

    The model is part of the identity of an analysis, not just the content.
    Comparing content alone means switching backends (say, from a local model
    to Haiku) silently keeps the old scores: the content is unchanged, so every
    item is skipped and the new model never runs. That failure is invisible -
    the run reports "skipped (unchanged)" and looks healthy.
    """
    row = conn.execute(
        "SELECT content_hash, model FROM analyses WHERE item_id = ?", (item_id,)
    ).fetchone()
    if row is None or row["content_hash"] != item_hash:
        return True
    # An empty model means the caller does not care which model produced the
    # stored analysis (used where only content staleness matters).
    return bool(model) and row["model"] != model


def analyze_and_store(
    conn: sqlite3.Connection,
    item_id: int,
    item: Item,
    client: Optional[anthropic.Anthropic] = None,
    interests: Optional[dict[str, InterestArea]] = None,
    model: str = ANALYZER_MODEL,
    force: bool = False,
) -> Optional[Usage]:
    """Analyze an item unless this model already analyzed this exact content.

    Returns the Usage for the call made, or None when the cached analysis was reused.
    """
    item_hash = content_hash(item, interests)
    if not force and not needs_analysis(conn, item_id, item_hash, model):
        log.debug("skipping already-analyzed item %s", item.source_id)
        return None

    result, usage = analyze(item, client=client, interests=interests, model=model)
    store_analysis(conn, item_id, result, item_hash, model)
    return usage
