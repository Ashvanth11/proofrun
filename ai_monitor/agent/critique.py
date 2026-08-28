"""Self-correction pass over an agent's assessment.

The check that matters is grounding: did the agent's conclusion follow from
what its tools actually returned, or did it assert things it never saw? An
agent that reads only metadata and then confidently describes a project's
architecture has hallucinated, and the trace is what proves it.

The critic is shown the evidence and the conclusion, never the reasoning that
connected them - it should judge whether the claim is supported, not be talked
into agreeing by the original argument.
"""

import logging
from typing import Any, Optional

import anthropic
from pydantic import BaseModel, Field

from ai_monitor.agent.repo_agent import AgentRun, RepoAssessment
from ai_monitor.analysis.analyzer import Usage
from ai_monitor.providers import OllamaError

log = logging.getLogger(__name__)

CRITIQUE_MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """You review an automated assessment of a GitHub repository for grounding.

You are given the evidence that was actually gathered (tool results) and the
conclusion that was drawn from it. Judge only whether the conclusion is
supported by that evidence.

Flag an issue when:
- the summary describes things the evidence does not show
- the relevance score is not justified by what was found
- an interest area is claimed without supporting evidence
- the conclusion is confident despite thin evidence

Do not flag an issue merely because the evidence is thin: a low-confidence
conclusion drawn from limited evidence is correct behavior, as long as the
conclusion does not overreach. Most assessments are fine; say so when they are."""

REVISION_PROMPT = """Revise the assessment to fix the issues identified. Keep everything
that was supported; correct only what was not."""


class Critique(BaseModel):
    grounded: bool = Field(
        description="True if the conclusion follows from the evidence"
    )
    issues: list[str] = Field(
        default_factory=list, description="Specific unsupported claims, if any"
    )


def format_evidence(run: AgentRun) -> str:
    """Render what the agent actually saw, in the order it saw it."""
    if not run.tool_calls:
        return "(no tools were called - the assessment used no gathered evidence)"

    lines = []
    for call in run.tool_calls:
        status = " [FAILED]" if call.is_error else ""
        lines.append(
            f"step {call.step}: {call.tool}({call.arguments}){status}\n"
            f"  returned: {call.result_summary}"
        )
    return "\n".join(lines)


def format_assessment(assessment: RepoAssessment) -> str:
    return (
        f"summary: {assessment.summary}\n"
        f"relevance_score: {assessment.relevance_score}\n"
        f"matched_areas: {assessment.matched_areas or 'none'}\n"
        f"justification: {assessment.justification}"
    )


def critique(
    run: AgentRun,
    client: Any,
    model: str = CRITIQUE_MODEL,
) -> tuple[Optional[Critique], Usage]:
    if run.assessment is None:
        return None, Usage(model=model)

    prompt = (
        f"Repository: {run.repo}\n\n"
        f"EVIDENCE GATHERED\n{format_evidence(run)}\n\n"
        f"CONCLUSION DRAWN\n{format_assessment(run.assessment)}"
    )

    try:
        response = client.messages.parse(
            model=model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_format=Critique,
        )
    except (anthropic.APIError, OllamaError, ValueError) as exc:
        log.warning("critique failed for %s: %s", run.repo, exc)
        return None, Usage(model=model)

    return response.parsed_output, Usage(
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        model=model,
    )


def revise(
    run: AgentRun,
    issues: list[str],
    client: Any,
    model: str = CRITIQUE_MODEL,
) -> tuple[Optional[RepoAssessment], Usage]:
    prompt = (
        f"Repository: {run.repo}\n\n"
        f"EVIDENCE GATHERED\n{format_evidence(run)}\n\n"
        f"ORIGINAL ASSESSMENT\n{format_assessment(run.assessment)}\n\n"
        f"ISSUES FOUND\n" + "\n".join(f"- {i}" for i in issues) + f"\n\n{REVISION_PROMPT}"
    )

    try:
        response = client.messages.parse(
            model=model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_format=RepoAssessment,
        )
    except (anthropic.APIError, OllamaError, ValueError) as exc:
        log.warning("revision failed for %s: %s", run.repo, exc)
        return None, Usage(model=model)

    return response.parsed_output, Usage(
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        model=model,
    )


def critique_and_revise(
    run: AgentRun,
    client: Any,
    model: str = CRITIQUE_MODEL,
) -> tuple[AgentRun, Usage]:
    """Critique an assessment and revise it once if it is not grounded.

    Exactly one revision pass: a critic and a reviser can disagree forever,
    and the second opinion is where nearly all the value is.
    """
    total = Usage(model=model)

    verdict, usage = critique(run, client, model=model)
    total.input_tokens += usage.input_tokens
    total.output_tokens += usage.output_tokens

    if verdict is None:
        return run, total

    run.critique = verdict
    if verdict.grounded or not verdict.issues:
        return run, total

    log.info("%s: critique found %d issue(s), revising", run.repo, len(verdict.issues))
    revised, usage = revise(run, verdict.issues, client, model=model)
    total.input_tokens += usage.input_tokens
    total.output_tokens += usage.output_tokens

    if revised is not None:
        run.original_assessment = run.assessment
        run.assessment = revised
        run.revised = True

    return run, total
