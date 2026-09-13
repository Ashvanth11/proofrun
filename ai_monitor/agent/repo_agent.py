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

Caps are enforced in code, not in the prompt, because a prompt is a request and
a small model will happily ignore it - the loop must hold even when the model
misbehaves.

The loop itself now lives in `ai_monitor.agent.loop`, shared with the
investigation agent. What stays here is what is specific to this agent: its
prompt, its tools, its schema, and its idea of a sensible budget.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

import anthropic
from pydantic import BaseModel, Field

from ai_monitor.agent import tools
from ai_monitor.agent.loop import Caps, ToolCall, run_loop
from ai_monitor.analysis.analyzer import Usage, render_interests
from ai_monitor.config.settings import InterestArea, settings
from ai_monitor.providers import OllamaError

log = logging.getLogger(__name__)

AGENT_MODEL = "claude-sonnet-5"

DEFAULT_MAX_STEPS = 8
DEFAULT_MAX_COST_USD = 0.10  # per repository
DEFAULT_MAX_SECONDS = 600.0

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


class AgentRun(BaseModel):
    """Full trace of one agent invocation - what it did and why it stopped."""

    repo: str
    steps_taken: int
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop_reason: str  # sufficient_info | step_cap | cost_cap | error
    assessment: Optional[RepoAssessment] = None
    usage: Usage
    transcript: list[dict] = Field(default_factory=list)

    # Set by the critique pass (ai_monitor.agent.critique), if it runs.
    critique: Optional[Any] = None
    original_assessment: Optional[RepoAssessment] = None
    revised: bool = False

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
    max_seconds: float = DEFAULT_MAX_SECONDS,
) -> AgentRun:
    """Run the reasoning-action loop over one repository."""
    interests = interests if interests is not None else settings.interests

    messages: list[dict] = [
        {
            "role": "user",
            "content": (
                f"Interest areas:\n{render_interests(interests)}\n\n"
                f"Assess this repository: {repo}"
            ),
        }
    ]

    result = run_loop(
        client,
        model=model,
        system=SYSTEM_PROMPT,
        messages=messages,
        tool_schemas=tools.TOOL_SCHEMAS,
        # Resolved through the module on every call rather than bound once, so
        # the test suite can substitute a network-free executor.
        execute=lambda name, arguments: tools.execute(name, arguments),
        caps=Caps(
            max_steps=max_steps,
            max_cost_usd=max_cost_usd,
            max_seconds=max_seconds,
        ),
        final_prompt=FINAL_PROMPT,
        label=repo,
    )

    usage = result.usage
    assessment = None
    if result.final_text:
        assessment, extract_usage = _extract_assessment(
            result.final_text, client, model, interests
        )
        usage.input_tokens += extract_usage.input_tokens
        usage.output_tokens += extract_usage.output_tokens

    log.info(
        "%s: %d steps, stop=%s, escalated=%s, $%.4f",
        repo,
        result.steps_taken,
        result.stop_reason,
        {c.tool for c in result.tool_calls} or "none",
        usage.cost_usd,
    )

    return AgentRun(
        repo=repo,
        steps_taken=result.steps_taken,
        tool_calls=result.tool_calls,
        stop_reason=result.stop_reason,
        assessment=assessment,
        usage=usage,
        transcript=(
            [{"role": "assistant", "text": result.final_text}]
            if result.final_text
            else []
        ),
    )


def store_run(conn: sqlite3.Connection, item_id: int, run: AgentRun) -> None:
    critique_text = ""
    if run.critique is not None:
        issues = getattr(run.critique, "issues", [])
        critique_text = json.dumps(
            {"grounded": getattr(run.critique, "grounded", None), "issues": issues}
        )

    conn.execute(
        """
        INSERT INTO agent_runs
            (item_id, steps_taken, tool_calls, stop_reason, critique, revised,
             cost_usd, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            steps_taken = excluded.steps_taken,
            tool_calls = excluded.tool_calls,
            stop_reason = excluded.stop_reason,
            critique = excluded.critique,
            revised = excluded.revised,
            cost_usd = excluded.cost_usd,
            created_at = excluded.created_at
        """,
        (
            item_id,
            run.steps_taken,
            json.dumps([c.model_dump() for c in run.tool_calls]),
            run.stop_reason,
            critique_text,
            int(run.revised),
            run.usage.cost_usd,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
