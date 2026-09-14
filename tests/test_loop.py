"""Loop tests.

The loop's job is to stop. Every test here is a variation on the same
adversarial premise as `test_repo_agent.py`: a model that never stops calling
tools must still terminate, on every cap, without the prompt's cooperation.

Nothing here touches the network or Docker - the model is scripted and the
executor is a function.
"""

from types import SimpleNamespace

import pytest

from ai_monitor.agent import loop
from ai_monitor.agent.loop import Caps, StopLoop, ToolCap, run_loop
from ai_monitor.providers import _TextBlock, _ToolUseBlock


class _ServerToolUse:
    """A tool Anthropic ran on its side; nothing local executes it."""

    type = "server_tool_use"

    def __init__(self, id, name, input):
        self.id = id
        self.name = name
        self.input = input


class _ServerToolResult:
    def __init__(self, tool_use_id, content, type="web_search_tool_result"):
        self.type = type
        self.tool_use_id = tool_use_id
        self.content = content


class ScriptedClient:
    """Replays scripted model turns. Same shape as test_repo_agent's."""

    def __init__(self, script, tokens_per_call=(1000, 100), stop_reasons=None):
        self.script = list(script)
        self.tokens = tokens_per_call
        self.stop_reasons = list(stop_reasons or [])
        self.calls = 0
        self.seen_tools = []
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        self.seen_tools.append(kwargs.get("tools"))

        if not kwargs.get("tools"):  # the wrap-up call offers none
            turn = "Final answer from partial information."
        else:
            turn = self.script.pop(0) if self.script else "Final answer."

        if isinstance(turn, str):
            blocks = [_TextBlock(turn)]
        elif isinstance(turn, list) and turn and not isinstance(turn[0], tuple):
            blocks = turn  # raw content blocks, for the server-tool cases
        else:
            blocks = [
                _ToolUseBlock(id=f"c{self.calls}_{i}", name=name, input=args)
                for i, (name, args) in enumerate(turn)
            ]

        stop = (
            self.stop_reasons.pop(0)
            if self.stop_reasons
            else ("tool_use" if not isinstance(turn, str) else "end_turn")
        )
        return SimpleNamespace(
            content=blocks,
            usage=SimpleNamespace(
                input_tokens=self.tokens[0], output_tokens=self.tokens[1]
            ),
            stop_reason=stop,
        )


class Clock:
    """A monotonic clock that advances a fixed amount per reading."""

    def __init__(self, step=100.0):
        self.step = step
        self.t = 0.0

    def __call__(self):
        now = self.t
        self.t += self.step
        return now


def ok_executor(name, arguments):
    return {"ran": name}, False


def drive(client, executor=ok_executor, caps=None, schemas=None, **kwargs):
    return run_loop(
        client,
        model="ollama/test",
        system="s",
        messages=[{"role": "user", "content": "go"}],
        tool_schemas=schemas or [{"name": "t", "description": "d", "input_schema": {}}],
        execute=executor,
        caps=caps or Caps(),
        **kwargs,
    )


FOREVER = [[("t", {"x": 1})]] * 100


# --- stopping conditions -------------------------------------------------


def test_model_answering_ends_the_loop():
    result = drive(ScriptedClient([[("t", {})], "Done."]))
    assert result.stop_reason == "sufficient_info"
    assert result.steps_taken == 2
    assert result.final_text == "Done."


def test_step_cap_stops_a_runaway_model():
    result = drive(ScriptedClient(FOREVER), caps=Caps(max_steps=3))
    assert result.stop_reason == "step_cap"
    assert result.steps_taken == 3
    assert len(result.tool_calls) == 3


def test_cost_cap_stops_before_exceeding_budget():
    client = ScriptedClient(FOREVER, tokens_per_call=(1_000_000, 0))
    result = run_loop(
        client,
        model="claude-haiku-4-5",  # $1.00 per call at these token counts
        system="s",
        messages=[{"role": "user", "content": "go"}],
        tool_schemas=[{"name": "t", "description": "d", "input_schema": {}}],
        execute=ok_executor,
        caps=Caps(max_steps=50, max_cost_usd=3.0),
    )
    assert result.stop_reason == "cost_cap"
    assert result.steps_taken == 3
    assert result.usage.cost_usd >= 3.0  # stopped at the boundary, didn't run on


def test_time_cap_stops_a_model_that_would_run_all_day():
    """The cap the repo agent never had: wall clock, checked before the turn."""
    result = drive(
        ScriptedClient(FOREVER),
        caps=Caps(max_steps=50, max_seconds=250.0),
        now=Clock(step=100.0),
    )
    assert result.stop_reason == "time_cap"
    assert result.steps_taken == 2


def test_time_cap_does_not_fire_when_there_is_time_left():
    result = drive(
        ScriptedClient([[("t", {})], "Done."]),
        caps=Caps(max_seconds=10_000.0),
        now=Clock(step=1.0),
    )
    assert result.stop_reason == "sufficient_info"


def test_model_failure_does_not_raise():
    class Broken:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            from ai_monitor.providers import OllamaError

            raise OllamaError("model unavailable")

    result = drive(Broken())
    assert result.stop_reason == "error"
    assert result.final_text == ""


def test_a_capped_run_still_asks_for_a_conclusion():
    """Hitting a cap must not throw away the work already paid for."""
    result = drive(ScriptedClient(FOREVER), caps=Caps(max_steps=2))
    assert result.stop_reason == "step_cap"
    assert result.final_text  # the wrap-up call ran


def test_an_errored_run_does_not_pay_for_a_wrap_up():
    """A broken model cannot be asked to summarize; do not spend trying."""

    class Broken:
        def __init__(self):
            self.messages = self
            self.calls = 0

        def create(self, **kwargs):
            from ai_monitor.providers import OllamaError

            self.calls += 1
            raise OllamaError("down")

    client = Broken()
    drive(client)
    assert client.calls == 1


# --- per-tool caps -------------------------------------------------------


def sandbox_caps(total=12, setup=4, steps=50):
    return Caps(
        max_steps=steps,
        tool_caps=[
            ToolCap(
                tools=frozenset({"sandbox_clone", "sandbox_setup", "sandbox_run"}),
                limit=total,
                stop_reason="sandbox_cap",
            ),
            ToolCap(
                tools=frozenset({"sandbox_setup"}),
                limit=setup,
                stop_reason="sandbox_cap",
            ),
        ],
    )


def test_tool_cap_stops_a_model_that_keeps_running_commands():
    client = ScriptedClient([[("sandbox_run", {"command": "x"})]] * 100)
    result = drive(client, caps=sandbox_caps(total=3))

    assert result.stop_reason == "sandbox_cap"
    ran = [c for c in result.tool_calls if not c.is_error]
    assert len(ran) == 3


def test_the_tighter_group_cap_binds_first():
    """4 setup calls inside a budget of 12 - the setup cap is what fires."""
    client = ScriptedClient([[("sandbox_setup", {"command": "pip install"})]] * 100)
    result = drive(client, caps=sandbox_caps(total=12, setup=2))

    assert result.stop_reason == "sandbox_cap"
    assert len([c for c in result.tool_calls if not c.is_error]) == 2


def test_an_uncapped_tool_is_not_charged_to_the_sandbox_budget():
    client = ScriptedClient([[("read_file", {"path": "README.md"})]] * 5 + ["Done."])
    result = drive(client, caps=sandbox_caps(total=1))
    assert result.stop_reason == "sufficient_info"


def test_a_refused_call_is_still_answered():
    """An assistant turn ending in an unanswered tool_use is malformed.

    The wrap-up call has to send this transcript back, so the refusal has to
    arrive as a tool_result like any other outcome.
    """
    client = ScriptedClient([[("sandbox_run", {"command": "x"})]] * 100)
    result = drive(client, caps=sandbox_caps(total=1))

    # ... user(go), assistant(tool_use), user(tool_result), user(final prompt)
    results_message = result.messages[-2]
    assert results_message["role"] == "user"
    assert results_message["content"][0]["type"] == "tool_result"
    assert results_message["content"][0]["is_error"] is True
    assert "budget exhausted" in result.tool_calls[-1].result_summary


# --- StopLoop (the disk cap's route out) ---------------------------------


def test_a_tool_can_end_the_run_and_still_be_recorded():
    def full_disk(name, arguments):
        raise StopLoop("disk_cap", {"error": "volume reached 2100 MB"})

    client = ScriptedClient([[("sandbox_run", {"command": "x"})]] * 100)
    result = drive(client, executor=full_disk, caps=sandbox_caps())

    assert result.stop_reason == "disk_cap"
    assert result.steps_taken == 1
    assert "2100 MB" in result.tool_calls[-1].result_summary
    assert result.final_text  # still asked for a conclusion


# --- server-side tools ---------------------------------------------------


def test_server_tool_blocks_are_recorded_but_never_executed():
    executed = []

    def spy(name, arguments):
        executed.append(name)
        return {}, False

    turn = [
        _ServerToolUse(id="s1", name="web_search", input={"query": "does it resume"}),
        _ServerToolResult("s1", [{"title": "an issue", "url": "http://x"}]),
        _TextBlock("Found something."),
    ]
    result = drive(ScriptedClient([turn]), executor=spy)

    assert executed == []
    assert [c.tool for c in result.tool_calls] == ["web_search"]
    assert result.tool_calls[0].server is True
    assert result.tool_calls[0].is_error is False


def test_a_failed_server_tool_is_marked_as_an_error():
    """Server tools fail with a 200 and an error object, not an exception."""
    turn = [
        _ServerToolUse(id="s1", name="web_search", input={"query": "q"}),
        _ServerToolResult("s1", {"error_code": "max_uses_exceeded"}),
        _TextBlock("No luck."),
    ]
    result = drive(ScriptedClient([turn]))
    assert result.tool_calls[0].is_error is True


def test_pause_turn_continues_instead_of_concluding():
    """A paused turn is unfinished, not an answer."""
    paused = [_ServerToolUse(id="s1", name="web_search", input={"query": "q"})]
    client = ScriptedClient(
        [paused, "Now I can answer."], stop_reasons=["pause_turn", "end_turn"]
    )
    result = drive(client)

    assert result.stop_reason == "sufficient_info"
    assert result.steps_taken == 2
    assert result.final_text == "Now I can answer."


# --- plumbing ------------------------------------------------------------


def test_tools_are_offered_every_turn_but_not_on_the_wrap_up():
    client = ScriptedClient(FOREVER)
    drive(client, caps=Caps(max_steps=2))
    assert client.seen_tools[0] and client.seen_tools[1]
    assert client.seen_tools[-1] is None  # the wrap-up must not invite more calls


def test_tool_errors_are_fed_back_not_fatal():
    result = drive(
        ScriptedClient([[("t", {})], "Done."]),
        executor=lambda n, a: ({"error": "not found"}, True),
    )
    assert result.stop_reason == "sufficient_info"
    assert result.tool_calls[0].is_error is True


def test_parallel_tool_calls_are_answered_in_one_user_message():
    """Splitting them trains the model out of calling tools in parallel."""
    client = ScriptedClient([[("t", {"i": 1}), ("t", {"i": 2})], "Done."])
    result = drive(client)

    results_message = result.messages[2]
    assert len(results_message["content"]) == 2
    assert len(result.tool_calls) == 2


def test_output_is_truncated_in_the_trace():
    """Bounded, but wide enough that the critic can see a metadata record."""
    result = drive(
        ScriptedClient([[("t", {})], "Done."]),
        executor=lambda n, a: ({"content": "x" * 10_000}, False),
    )
    summary = result.tool_calls[0].result_summary
    assert len(summary) < loop.SUMMARY_CHARS + 10
    assert summary.endswith("...")


@pytest.mark.parametrize("cap", ["step_cap", "cost_cap", "time_cap"])
def test_every_cap_is_enforced_in_code_not_the_prompt(cap):
    """The prompt says nothing about limits; a model ignoring them still stops."""
    caps = {
        "step_cap": Caps(max_steps=2),
        "cost_cap": Caps(max_steps=50, max_cost_usd=0.5),
        "time_cap": Caps(max_steps=50, max_seconds=150.0),
    }[cap]
    client = ScriptedClient(FOREVER, tokens_per_call=(1_000_000, 0))
    result = run_loop(
        client,
        model="claude-haiku-4-5",
        system="no limits are mentioned here",
        messages=[{"role": "user", "content": "go"}],
        tool_schemas=[{"name": "t", "description": "d", "input_schema": {}}],
        execute=ok_executor,
        caps=caps,
        now=Clock(step=100.0),
    )
    assert result.stop_reason == cap


def test_summarize_handles_values_json_cannot():
    assert "object" in loop.summarize({"x": object()})
