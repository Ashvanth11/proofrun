import logging
import sqlite3
from typing import Optional

import anthropic
from pydantic import BaseModel, Field

from ai_monitor.analysis.analyzer import Usage, render_interests
from ai_monitor.config.settings import InterestArea, settings
from ai_monitor.eval import golden_set

log = logging.getLogger(__name__)

JUDGE_MODEL = "claude-sonnet-5"

# The judge scores items independently of the analyzer. It is never shown the
# analyzer's score or summary: the point is an opinion that can disagree, and
# showing the analyzer's answer would anchor it into agreement.
SYSTEM_PROMPT = """You are evaluating how relevant AI/ML developments are to a researcher's stated interest areas.

Score relevance on this scale:
- 0.0-0.2: unrelated to any interest area
- 0.3-0.5: touches an area tangentially, or is routine incremental work
- 0.6-0.8: solidly within an interest area and worth reading
- 0.9-1.0: directly advances an interest area in a way the researcher should not miss

Judge the work on what it actually contributes, not on how it is described.
Most published work is routine; reserve high scores for genuine advances.
Explain your score in one or two sentences."""


class JudgeVerdict(BaseModel):
    relevance_score: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(description="One or two sentences justifying the score")


def build_prompt(
    title: str, content: str, source: str, interests: dict[str, InterestArea]
) -> str:
    return (
        f"Interest areas:\n{render_interests(interests)}\n\n"
        f"---\n"
        f"Source: {source}\n"
        f"Title: {title}\n"
        f"Content: {content}\n"
    )


def judge_one(
    title: str,
    content: str,
    source: str,
    client: Optional[anthropic.Anthropic] = None,
    interests: Optional[dict[str, InterestArea]] = None,
    model: str = JUDGE_MODEL,
) -> tuple[JudgeVerdict, Usage]:
    interests = interests if interests is not None else settings.interests
    client = client or anthropic.Anthropic()

    response = client.messages.parse(
        model=model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": build_prompt(title, content, source, interests),
            }
        ],
        output_format=JudgeVerdict,
    )
    usage = Usage(
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        model=model,
    )
    return response.parsed_output, usage


def judge_golden_set(
    conn: sqlite3.Connection,
    client: Optional[anthropic.Anthropic] = None,
    interests: Optional[dict[str, InterestArea]] = None,
    model: str = JUDGE_MODEL,
) -> Usage:
    """Score every labeled-but-unjudged item. Returns total usage."""
    pending = golden_set.needs_judging(conn)
    total = Usage(model=model)

    for row in pending:
        try:
            verdict, usage = judge_one(
                row["title"],
                row["content"],
                row["source"],
                client=client,
                interests=interests,
                model=model,
            )
        except anthropic.APIError:
            log.exception("judging failed for item %s", row["item_id"])
            continue

        golden_set.record_judge(
            conn, row["item_id"], verdict.relevance_score, verdict.reasoning
        )
        total.input_tokens += usage.input_tokens
        total.output_tokens += usage.output_tokens

    log.info("judged %d items, cost $%.4f", len(pending), total.cost_usd)
    return total
