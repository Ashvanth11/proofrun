"""Repo-analysis agent: a goal-directed reasoning-action loop.

This is the part of the system that is genuinely agentic. The model is given
tools over the GitHub API and decides for itself how deep to look: many repos
are resolvable from metadata alone, some need a file listing, a few need a
README read. Nothing prescribes that ladder - the model chooses, and the loop
stops when the model says it has enough or when a hard cap fires.

Three things make the stopping condition real rather than decorative:

1. The model can end the loop by answering instead of calling a tool.
2. A step cap bounds the number of model turns.
3. A cost cap bounds spend, enforced in code rather than requested in a prompt.

Caps are enforced here, not in the prompt, because a prompt is a request and a
small model will happily ignore it - the loop must hold even when the model
misbehaves.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

import anthropic
from pydantic import BaseModel, Field

from ai_monitor.agent import tools
from ai_monitor.analysis.analyzer import Usage, render_interests
from ai_monitor.config.settings import InterestArea, settings
from ai_monitor.providers import OllamaError

log = logging.getLogger(__name__)

AGENT_MODEL = "claude-sonnet-5"

DEFAULT_MAX_STEPS = 8
DEFAULT_MAX_COST_USD = 0.10  # per repository

SYSTEM_PROMPT = """You assess GitHub repositories for relevance to a researcher's interest areas.

You have tools to inspect a repository. Use them in escalating order of cost:

1. get_repo_metadata - always start here. For most repositories this is enough.
2. list_files - only if the metadata is ambiguous about what the project does.
3. read_file - only when one specific file will answer a specific question,
   most often README.md.

Stop as soon as you can justify a score. Do not gather information you will not
use; going deeper on an obviously-irrelevant repository is wasted effort.

When you have enough, reply with your final assessment as plain text and make no
further tool calls. Your reply must state a relevance score from 0.0 to 1.0, which
interest areas match, and one sentence of justification."""

FINAL_PROMPT = """Based on everything gathered, give your final assessment now.
Make no further tool calls."""


class RepoAssessment(BaseModel):
    summary: str = Field(description="1-2 sentences on what the project does")
    relevance_score: float = Field(ge=0.0, le=1.0)
    matched_areas: list[str] = Field(default_factory=list)
    justification: str = Field(description="One sentence explaining the score")


class ToolCall(BaseModel):
    step: int
    tool: str
    arguments: dict
    is_error: bool
    result_summary: str


class AgentRun(BaseModel):
    """Full trace of one agent invocation - what it did and why it stopped."""

    repo: str
    steps_taken: int
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop_reason: str  # sufficient_info | step_cap | cost_cap | error
    assessment: Optional[RepoAssessment] = None
    usage: Usage
    transcript: list[dict] = Field(default_factory=list)

    @property
    def escalated_to(self) -> str:
        """Deepest rung of the ladder this run reached."""
        used = {c.tool for c in self.tool_calls}
        if "read_file" in used:
            return "read_file"
        if "list_files" in used:
            return "list_files"
        if "get_repo_metadata" in used:
            return "metadata"
        return "none"


def _summarize(result: dict, limit: int = 300) -> str:
    text = json.dumps(result, default=str)
    return text[:limit] + ("..." if len(text) > limit else "")


def _extract_assessment(
    text: str,
    client: Any,
    model: str,
    interests: dict[str, InterestArea],
) -> tuple[Optional[RepoAssessment], Usage]:
    """Turn the agent's free-text conclusion into the structured record.

    This is a separate cheap call rather than forcing structured output during
    the loop, because a model cannot both call tools and be constrained to a
    final schema in the same turn.
    """
    try:
        response = client.messages.parse(
            model=model,
            max_tokens=1024,
            system=(
                "Extract the assessment into the required fields. "
                f"Valid interest areas: {', '.join(interests)}."
            ),
            messages=[{"role": "user", "content": text}],
            output_format=RepoAssessment,
        )
    except (anthropic.APIError, OllamaError, ValueError) as exc:
        log.warning("could not extract structured assessment: %s", exc)
        return None, Usage(model=model)

    assessment = response.parsed_output
    assessment.matched_areas = [
        a for a in assessment.matched_areas if a in interests
    ]
    return assessment, Usage(
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        model=model,
    )


def analyze_repo(
    repo: str,
    client: Any,
    model: str = AGENT_MODEL,
    interests: Optional[dict[str, InterestArea]] = None,
    max_steps: int = DEFAULT_MAX_STEPS,
    max_cost_usd: float = DEFAULT_MAX_COST_USD,
) -> AgentRun:
    """Run the reasoning-action loop over one repository."""
    interests = interests if interests is not None else settings.interests
    usage = Usage(model=model)
    calls: list[ToolCall] = []

    messages: list[dict] = [
        {
            "role": "user",
            "content": (
                f"Interest areas:\n{render_interests(interests)}\n\n"
                f"Assess this repository: {repo}"
            ),
        }
    ]

    stop_reason = "step_cap"
    final_text = ""
    step = 0

    while step < max_steps:
        # Check the cost cap *before* spending, so the cap is a ceiling rather
        # than something noticed after it has already been exceeded.
        if usage.cost_usd >= max_cost_usd:
            stop_reason = "cost_cap"
            log.info("%s: cost cap hit at $%.4f", repo, usage.cost_usd)
            break

        step += 1
        try:
            response = client.messages.create(
                model=model,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                messages=messages,
                tools=tools.TOOL_SCHEMAS,
            )
        except (anthropic.APIError, OllamaError) as exc:
            log.warning("%s: model call failed at step %d: %s", repo, step, exc)
            stop_reason = "error"
            break

        usage.input_tokens += response.usage.input_tokens
        usage.output_tokens += response.usage.output_tokens

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        text = "".join(b.text for b in response.content if b.type == "text")

        if not tool_uses:
            # The model answered instead of calling a tool: it decided it has
            # enough. This is the loop's natural exit.
            stop_reason = "sufficient_info"
            final_text = text
            break

        messages.append({"role": "assistant", "content": response.content})

        results = []
        for block in tool_uses:
            result, is_error = tools.execute(block.name, block.input)
            calls.append(
                ToolCall(
                    step=step,
                    tool=block.name,
                    arguments=block.input,
                    is_error=is_error,
                    result_summary=_summarize(result),
                )
            )
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                    "is_error": is_error,
                }
            )
        messages.append({"role": "user", "content": results})

    # The loop ran out of budget mid-investigation. Ask once for a conclusion
    # from what it already has, rather than discarding the work.
    if stop_reason in {"step_cap", "cost_cap"} and not final_text:
        messages.append({"role": "user", "content": FINAL_PROMPT})
        try:
            response = client.messages.create(
                model=model, max_tokens=1024, system=SYSTEM_PROMPT, messages=messages
            )
            usage.input_tokens += response.usage.input_tokens
            usage.output_tokens += response.usage.output_tokens
            final_text = "".join(
                b.text for b in response.content if b.type == "text"
            )
        except (anthropic.APIError, OllamaError) as exc:
            log.warning("%s: final call failed: %s", repo, exc)

    assessment = None
    if final_text:
        assessment, extract_usage = _extract_assessment(
            final_text, client, model, interests
        )
        usage.input_tokens += extract_usage.input_tokens
        usage.output_tokens += extract_usage.output_tokens

    log.info(
        "%s: %d steps, stop=%s, escalated=%s, $%.4f",
        repo,
        step,
        stop_reason,
        {c.tool for c in calls} or "none",
        usage.cost_usd,
    )

    return AgentRun(
        repo=repo,
        steps_taken=step,
        tool_calls=calls,
        stop_reason=stop_reason,
        assessment=assessment,
        usage=usage,
        transcript=[{"role": "assistant", "text": final_text}] if final_text else [],
    )


def store_run(conn: sqlite3.Connection, item_id: int, run: AgentRun) -> None:
    conn.execute(
        """
        INSERT INTO agent_runs
            (item_id, steps_taken, tool_calls, stop_reason, cost_usd, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            steps_taken = excluded.steps_taken,
            tool_calls = excluded.tool_calls,
            stop_reason = excluded.stop_reason,
            cost_usd = excluded.cost_usd,
            created_at = excluded.created_at
        """,
        (
            item_id,
            run.steps_taken,
            json.dumps([c.model_dump() for c in run.tool_calls]),
            run.stop_reason,
            run.usage.cost_usd,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
