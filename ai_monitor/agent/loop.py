"""The reasoning-action loop, extracted so more than one agent can use it.

`repo_agent.analyze_repo` grew this loop around one hardcoded system prompt,
one tool list, and one output schema. The investigation agent needs the same
control flow with different values in all three slots, so the loop moves here
and both agents become thin callers. The proof that the extraction changed no
behaviour is that `tests/test_repo_agent.py` passes unchanged.

What the loop owns, and what it deliberately does not:

- **It owns stopping.** Every cap is checked here, in code, before the spend it
  bounds. A prompt is a request; a small or hostile-steered model will ignore
  it, so the loop has to hold on its own.
- **It does not own tools.** Callers pass an `execute` callable. The repo agent
  hands it the module-level registry; the investigation agent hands it a
  closure over one live `Sandbox`, because sandbox tools are per-run state and
  a module-level registry cannot express that.
- **It does not own the conclusion.** The loop returns free text. Turning that
  into a schema is a separate cheap call, because a model cannot both call
  tools and be constrained to a final schema in the same turn.

Server-side tools (web search) are Anthropic's to execute, not ours. Their
blocks are recorded in the trace so the ledger can cite them, but they are
never passed to `execute` and never need a `tool_result` sent back.
"""

import json
import logging
import time
from collections import Counter
from typing import Any, Callable, Optional

import anthropic
from pydantic import BaseModel, Field

from ai_monitor.analysis.analyzer import Usage
from ai_monitor.providers import OllamaError

log = logging.getLogger(__name__)

FINAL_PROMPT = """Based on everything gathered, give your final assessment now.
Make no further tool calls."""

# What a caller gets back from a tool: (result, is_error).
Executor = Callable[[str, dict], tuple[dict, bool]]


class StopLoop(Exception):
    """A tool result that also ends the run.

    The disk cap is the reason this exists. It is measured inside the sandbox
    executor - the only code that knows a volume exists - but it has to stop
    the *loop*, which knows nothing about disks. Raising carries both the
    stop reason and the result the model should still see in its trace.
    """

    def __init__(self, stop_reason: str, result: Optional[dict] = None) -> None:
        super().__init__(stop_reason)
        self.stop_reason = stop_reason
        self.result = result or {"error": stop_reason}


class ToolCap(BaseModel):
    """A ceiling on a *group* of tools, so overlapping budgets compose.

    The sandbox budget is "12 calls, of which 4 may be setup": that is two
    caps over overlapping groups, not one number. A per-tool cap is just a
    group of one.
    """

    tools: frozenset[str]
    limit: int
    stop_reason: str


class Caps(BaseModel):
    """Every ceiling the loop enforces. Checked before spending, never after."""

    max_steps: int = 20
    max_cost_usd: float = 2.00
    max_seconds: float = 900.0
    tool_caps: list[ToolCap] = Field(default_factory=list)


class ToolCall(BaseModel):
    """One tool invocation, as it appears in the trace.

    `server=True` marks a tool Anthropic executed on its side (web search). It
    is in the trace for the same reason the others are - a ledger entry has to
    be checkable against a call that actually happened - but nothing local ran.
    """

    step: int
    tool: str
    arguments: dict
    is_error: bool
    result_summary: str
    server: bool = False


class LoopResult(BaseModel):
    steps_taken: int
    stop_reason: str  # sufficient_info | step_cap | cost_cap | time_cap | error | <tool cap>
    final_text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage
    messages: list[dict] = Field(default_factory=list)


def summarize(result: Any, limit: int = 300) -> str:
    text = json.dumps(result, default=str)
    return text[:limit] + ("..." if len(text) > limit else "")


def _refusals(counts: Counter, caps: Caps, name: str) -> Optional[ToolCap]:
    """The first cap that would be breached by calling `name` once more."""
    for cap in caps.tool_caps:
        if name in cap.tools and sum(counts[t] for t in cap.tools) >= cap.limit:
            return cap
    return None


def run_loop(
    client: Any,
    model: str,
    system: str,
    messages: list[dict],
    tool_schemas: list[dict],
    execute: Executor,
    caps: Caps,
    final_prompt: str = FINAL_PROMPT,
    max_tokens: int = 2048,
    label: str = "",
    now: Callable[[], float] = time.monotonic,
) -> LoopResult:
    """Run the reasoning-action loop until the model stops or a cap fires.

    `messages` is mutated in place and returned on the result, so the caller
    (and the critique pass) sees the same transcript the model saw.
    """
    usage = Usage(model=model)
    calls: list[ToolCall] = []
    counts: Counter = Counter()
    deadline = now() + caps.max_seconds

    stop_reason = "step_cap"
    final_text = ""
    step = 0

    while step < caps.max_steps:
        # Both ceilings are checked *before* the turn they would pay for, so a
        # cap is a limit on starting work rather than something noticed once
        # the budget is already gone.
        if usage.cost_usd >= caps.max_cost_usd:
            stop_reason = "cost_cap"
            log.info("%s: cost cap hit at $%.4f", label, usage.cost_usd)
            break
        if now() >= deadline:
            stop_reason = "time_cap"
            log.info("%s: wall-clock cap hit after %.0fs", label, caps.max_seconds)
            break

        step += 1
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                tools=tool_schemas,
            )
        except (anthropic.APIError, OllamaError) as exc:
            log.warning("%s: model call failed at step %d: %s", label, step, exc)
            stop_reason = "error"
            break

        usage.input_tokens += response.usage.input_tokens
        usage.output_tokens += response.usage.output_tokens

        calls.extend(_record_server_calls(response.content, step))

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        text = "".join(b.text for b in response.content if b.type == "text")

        if not tool_uses:
            # `pause_turn` means a server-side tool is mid-flight: the turn is
            # incomplete, not finished, so continue without answering it.
            if getattr(response, "stop_reason", None) == "pause_turn":
                messages.append({"role": "assistant", "content": response.content})
                continue
            # Otherwise the model answered instead of calling a tool: it
            # decided it has enough. This is the loop's natural exit.
            stop_reason = "sufficient_info"
            final_text = text
            break

        messages.append({"role": "assistant", "content": response.content})

        results = []
        halt: Optional[str] = None

        for block in tool_uses:
            breached = _refusals(counts, caps, block.name)
            if breached is not None:
                # Answer the block rather than breaking here: an assistant
                # message ending in an unanswered tool_use is a malformed
                # conversation, and the wrap-up call below still has to send it.
                result, is_error = (
                    {
                        "error": (
                            f"{block.name} budget exhausted "
                            f"({breached.limit} calls); conclude from what you have"
                        )
                    },
                    True,
                )
                halt = breached.stop_reason
            else:
                try:
                    result, is_error = execute(block.name, block.input)
                except StopLoop as stop:
                    result, is_error = stop.result, True
                    halt = stop.stop_reason
                counts[block.name] += 1

            calls.append(
                ToolCall(
                    step=step,
                    tool=block.name,
                    arguments=block.input,
                    is_error=is_error,
                    result_summary=summarize(result),
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

        if halt is not None:
            stop_reason = halt
            log.info("%s: %s", label, halt)
            break

    # The loop ran out of budget mid-investigation. Ask once for a conclusion
    # from what it already has, rather than discarding the work.
    if stop_reason not in {"sufficient_info", "error"} and not final_text:
        messages.append({"role": "user", "content": final_prompt})
        try:
            response = client.messages.create(
                model=model, max_tokens=max_tokens, system=system, messages=messages
            )
            usage.input_tokens += response.usage.input_tokens
            usage.output_tokens += response.usage.output_tokens
            final_text = "".join(
                b.text for b in response.content if b.type == "text"
            )
        except (anthropic.APIError, OllamaError) as exc:
            log.warning("%s: final call failed: %s", label, exc)

    return LoopResult(
        steps_taken=step,
        stop_reason=stop_reason,
        final_text=final_text,
        tool_calls=calls,
        usage=usage,
        messages=messages,
    )


def _record_server_calls(content: list, step: int) -> list[ToolCall]:
    """Put Anthropic-executed tool calls into the trace without executing them.

    Web search arrives as a `server_tool_use` block plus a matching result
    block in the same assistant turn. Nothing local runs, but the ledger's
    citation check needs the call to exist somewhere checkable.
    """
    results = {
        getattr(b, "tool_use_id", None): b
        for b in content
        if getattr(b, "type", "").endswith("_tool_result")
    }

    calls = []
    for block in content:
        if getattr(block, "type", "") != "server_tool_use":
            continue
        result = results.get(block.id)
        body = getattr(result, "content", None)
        calls.append(
            ToolCall(
                step=step,
                tool=block.name,
                arguments=dict(block.input or {}),
                # A server-tool failure is an object with an error_code; a
                # success is a list of results.
                is_error=isinstance(body, dict) and "error_code" in body,
                result_summary=summarize(body),
                server=True,
            )
        )
    return calls
