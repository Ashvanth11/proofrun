import json
from types import SimpleNamespace

import pytest

from ai_monitor.agent import repo_agent, tools
from ai_monitor.agent.repo_agent import RepoAssessment
from ai_monitor.config.settings import InterestArea
from ai_monitor.providers import _TextBlock, _ToolUseBlock
from ai_monitor.storage import db
from ai_monitor.storage.models import Item, Source

INTERESTS = {
    "agents": InterestArea(description="Agentic systems", keywords=["agent"]),
    "llm-observability": InterestArea(description="Tracing", keywords=["tracing"]),
}


class ScriptedClient:
    """Replays a scripted sequence of model turns.

    Each entry is either a list of tool names to call, or a string final answer.
    """

    def __init__(self, script, tokens_per_call=(1000, 100)):
        self.script = list(script)
        self.tokens = tokens_per_call
        self.calls = 0
        self.messages = self
        self.seen_tools = []

    def create(self, **kwargs):
        self.calls += 1
        self.seen_tools.append(kwargs.get("tools"))

        # A provider cannot return tool calls when no tools were offered - the
        # final "wrap up" call deliberately omits them.
        if not kwargs.get("tools"):
            turn = "Final answer from partial information: score 0.5"
        else:
            turn = self.script.pop(0) if self.script else "Final answer: score 0.5"
        if isinstance(turn, str):
            blocks = [_TextBlock(turn)]
        else:
            blocks = [
                _ToolUseBlock(id=f"c{i}", name=name, input=args)
                for i, (name, args) in enumerate(turn)
            ]

        return SimpleNamespace(
            content=blocks,
            usage=SimpleNamespace(
                input_tokens=self.tokens[0], output_tokens=self.tokens[1]
            ),
            stop_reason="tool_use" if not isinstance(turn, str) else "end_turn",
        )

    def parse(self, **kwargs):
        return SimpleNamespace(
            parsed_output=RepoAssessment(
                summary="A tracing tool.",
                relevance_score=0.8,
                matched_areas=["llm-observability"],
                justification="Directly addresses observability.",
            ),
            usage=SimpleNamespace(input_tokens=200, output_tokens=50),
        )


@pytest.fixture
def stub_tools(monkeypatch):
    """Make tool execution deterministic and free of network calls."""
    calls = []

    def fake_execute(name, arguments):
        calls.append((name, arguments))
        if name == "get_repo_metadata":
            return {"full_name": arguments.get("repo"), "stars": 100}, False
        if name == "list_files":
            return {"entries": [{"name": "README.md", "type": "file"}]}, False
        if name == "read_file":
            return {"path": arguments.get("path"), "content": "# A project"}, False
        return {"error": "unknown"}, True

    monkeypatch.setattr(tools, "execute", fake_execute)
    return calls


# --- stopping conditions -------------------------------------------------


def test_model_answering_ends_the_loop(stub_tools):
    """The natural exit: the model stops calling tools and answers."""
    client = ScriptedClient(
        [
            [("get_repo_metadata", {"repo": "a/b"})],
            "Score 0.8, matches llm-observability.",
        ]
    )
    run = repo_agent.analyze_repo("a/b", client, model="ollama/test", interests=INTERESTS)

    assert run.stop_reason == "sufficient_info"
    assert run.steps_taken == 2
    assert run.assessment.relevance_score == 0.8


def test_step_cap_stops_a_runaway_model(stub_tools):
    """A model that never stops calling tools must still terminate."""
    forever = [[("get_repo_metadata", {"repo": "a/b"})]] * 50
    client = ScriptedClient(forever)

    run = repo_agent.analyze_repo(
        "a/b", client, model="ollama/test", interests=INTERESTS, max_steps=3
    )

    assert run.stop_reason == "step_cap"
    assert run.steps_taken == 3
    assert len(run.tool_calls) == 3


def test_cost_cap_stops_before_exceeding_budget(stub_tools):
    """The cap is a ceiling: the loop stops once spend reaches it."""
    forever = [[("read_file", {"repo": "a/b", "path": "README.md"})]] * 50
    # 1M input + 100k output per call on Haiku pricing = $1.50/call
    client = ScriptedClient(forever, tokens_per_call=(1_000_000, 100_000))

    run = repo_agent.analyze_repo(
        "a/b",
        client,
        model="claude-haiku-4-5",
        interests=INTERESTS,
        max_steps=50,
        max_cost_usd=2.0,
    )

    assert run.stop_reason == "cost_cap"
    assert run.steps_taken < 50  # stopped well before the step cap


def test_cost_cap_is_enforced_in_code_not_the_prompt(stub_tools):
    """A model ignoring instructions must still be stopped."""
    client = ScriptedClient([[("read_file", {"repo": "a/b", "path": "x"})]] * 50,
                            tokens_per_call=(1_000_000, 0))
    run = repo_agent.analyze_repo(
        "a/b", client, model="claude-haiku-4-5", interests=INTERESTS,
        max_steps=50, max_cost_usd=3.0,
    )
    assert run.stop_reason == "cost_cap"
    assert run.usage.cost_usd >= 3.0  # stopped at the boundary, didn't run on


def test_capped_run_still_produces_an_assessment(stub_tools):
    """Hitting a cap must not throw away the work already paid for."""
    client = ScriptedClient([[("get_repo_metadata", {"repo": "a/b"})]] * 10)
    run = repo_agent.analyze_repo(
        "a/b", client, model="ollama/test", interests=INTERESTS, max_steps=2
    )
    assert run.stop_reason == "step_cap"
    assert run.assessment is not None


def test_model_failure_does_not_raise(stub_tools):
    class Broken:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            from ai_monitor.providers import OllamaError

            raise OllamaError("model unavailable")

    run = repo_agent.analyze_repo(
        "a/b", Broken(), model="ollama/test", interests=INTERESTS
    )
    assert run.stop_reason == "error"
    assert run.assessment is None


# --- escalation ----------------------------------------------------------


def test_escalation_is_model_driven_not_prescribed(stub_tools):
    """Nothing forces the ladder; the model chooses how deep to go."""
    shallow = ScriptedClient(
        [[("get_repo_metadata", {"repo": "a/b"})], "Obviously irrelevant, 0.1"]
    )
    deep = ScriptedClient(
        [
            [("get_repo_metadata", {"repo": "a/b"})],
            [("list_files", {"repo": "a/b", "path": ""})],
            [("read_file", {"repo": "a/b", "path": "README.md"})],
            "Score 0.9",
        ]
    )

    shallow_run = repo_agent.analyze_repo(
        "a/b", shallow, model="ollama/test", interests=INTERESTS
    )
    deep_run = repo_agent.analyze_repo(
        "a/b", deep, model="ollama/test", interests=INTERESTS
    )

    assert shallow_run.escalated_to == "metadata"
    assert deep_run.escalated_to == "read_file"
    assert len(deep_run.tool_calls) == 3


def test_tool_schemas_are_offered_every_turn(stub_tools):
    client = ScriptedClient([[("get_repo_metadata", {"repo": "a/b"})], "done"])
    repo_agent.analyze_repo("a/b", client, model="ollama/test", interests=INTERESTS)
    names = {t["name"] for t in client.seen_tools[0]}
    assert names == {
        "get_repo_metadata",
        "get_repo_description",
        "list_files",
        "read_file",
    }


def test_tool_errors_are_fed_back_not_fatal(monkeypatch):
    """A missing file is information for the model, not a crash."""
    monkeypatch.setattr(
        tools, "execute", lambda n, a: ({"error": "not found"}, True)
    )
    client = ScriptedClient(
        [[("read_file", {"repo": "a/b", "path": "nope.md"})], "Score 0.3"]
    )
    run = repo_agent.analyze_repo(
        "a/b", client, model="ollama/test", interests=INTERESTS
    )

    assert run.stop_reason == "sufficient_info"
    assert run.tool_calls[0].is_error is True


def test_hallucinated_areas_dropped(stub_tools, monkeypatch):
    client = ScriptedClient(["Score 0.8"])

    def parse_with_bad_area(**kwargs):
        return SimpleNamespace(
            parsed_output=RepoAssessment(
                summary="s",
                relevance_score=0.8,
                matched_areas=["agents", "quantum-computing"],
                justification="j",
            ),
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )

    client.parse = parse_with_bad_area
    run = repo_agent.analyze_repo(
        "a/b", client, model="ollama/test", interests=INTERESTS
    )
    assert run.assessment.matched_areas == ["agents"]


# --- persistence ---------------------------------------------------------


def test_run_is_stored_with_full_trace(tmp_path, stub_tools):
    conn = db.connect(tmp_path / "t.db")
    item_id = db.upsert_item(
        conn,
        Item(
            source=Source.GITHUB,
            source_id="a/b",
            title="a/b",
            url="https://github.com/a/b",
        ),
    )
    client = ScriptedClient(
        [[("get_repo_metadata", {"repo": "a/b"})], "Score 0.8"]
    )
    run = repo_agent.analyze_repo(
        "a/b", client, model="ollama/test", interests=INTERESTS
    )
    repo_agent.store_run(conn, item_id, run)

    row = conn.execute("SELECT * FROM agent_runs WHERE item_id = ?", (item_id,)).fetchone()
    assert row["stop_reason"] == "sufficient_info"
    assert row["steps_taken"] == 2
    stored = json.loads(row["tool_calls"])
    assert stored[0]["tool"] == "get_repo_metadata"
    conn.close()


def test_storing_twice_updates_in_place(tmp_path, stub_tools):
    conn = db.connect(tmp_path / "t.db")
    item_id = db.upsert_item(
        conn,
        Item(source=Source.GITHUB, source_id="a/b", title="a/b", url="http://x"),
    )
    client = ScriptedClient(["Score 0.8"])
    run = repo_agent.analyze_repo("a/b", client, model="ollama/test", interests=INTERESTS)

    repo_agent.store_run(conn, item_id, run)
    repo_agent.store_run(conn, item_id, run)

    assert conn.execute("SELECT COUNT(*) n FROM agent_runs").fetchone()["n"] == 1
    conn.close()
