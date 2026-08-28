import json
from types import SimpleNamespace

import pytest

from ai_monitor.analysis import analyzer
from ai_monitor.analysis.analyzer import AnalysisResult, Usage
from ai_monitor.config.settings import InterestArea
from ai_monitor.storage import db
from ai_monitor.storage.models import Item, Source

INTERESTS = {
    "agents": InterestArea(description="Agentic systems", keywords=["agent"]),
    "llm-observability": InterestArea(description="Tracing", keywords=["tracing"]),
}


class FakeMessages:
    """Stands in for client.messages, recording how many calls were made."""

    def __init__(self, result: AnalysisResult):
        self.result = result
        self.calls = 0

    def parse(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return SimpleNamespace(
            parsed_output=self.result,
            usage=SimpleNamespace(input_tokens=500, output_tokens=100),
        )


class FakeClient:
    def __init__(self, result: AnalysisResult):
        self.messages = FakeMessages(result)


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    yield conn
    conn.close()


@pytest.fixture
def item():
    return Item(
        source=Source.ARXIV,
        source_id="2501.00001",
        title="A Reflection Loop for Tool-Using Agents",
        url="https://arxiv.org/abs/2501.00001",
        content="We introduce a critique step for agent trajectories.",
    )


@pytest.fixture
def result():
    return AnalysisResult(
        summary="Introduces a critique step for agent trajectories.",
        relevance_score=0.85,
        matched_areas=["agents"],
        justification="Directly addresses agent self-correction.",
    )


def test_analyze_returns_result_and_usage(item, result):
    client = FakeClient(result)
    got, usage = analyzer.analyze(item, client=client, interests=INTERESTS)
    assert got.relevance_score == 0.85
    assert got.matched_areas == ["agents"]
    assert usage.input_tokens == 500
    assert usage.model == analyzer.ANALYZER_MODEL


def test_prompt_includes_interest_areas_and_item(item):
    prompt = analyzer.build_prompt(item, INTERESTS)
    assert "agents" in prompt
    assert "llm-observability" in prompt
    assert item.title in prompt
    assert item.content in prompt


def test_unknown_matched_areas_are_dropped(item):
    hallucinated = AnalysisResult(
        summary="s",
        relevance_score=0.5,
        matched_areas=["agents", "quantum-computing"],
        justification="j",
    )
    got, _ = analyzer.analyze(item, client=FakeClient(hallucinated), interests=INTERESTS)
    assert got.matched_areas == ["agents"]


def test_store_and_read_back(conn, item, result):
    item_id = db.upsert_item(conn, item)
    analyzer.store_analysis(
        conn, item_id, result, analyzer.content_hash(item), "claude-haiku-4-5"
    )
    row = conn.execute(
        "SELECT * FROM analyses WHERE item_id = ?", (item_id,)
    ).fetchone()
    assert row["relevance_score"] == 0.85
    assert json.loads(row["matched_areas"]) == ["agents"]
    assert row["model"] == "claude-haiku-4-5"


def test_unchanged_item_is_not_reanalyzed(conn, item, result):
    """The idempotency guarantee that stops us re-paying on every run."""
    item_id = db.upsert_item(conn, item)
    client = FakeClient(result)

    first = analyzer.analyze_and_store(
        conn, item_id, item, client=client, interests=INTERESTS
    )
    second = analyzer.analyze_and_store(
        conn, item_id, item, client=client, interests=INTERESTS
    )

    assert first is not None  # first call hit the API
    assert second is None  # second call was skipped
    assert client.messages.calls == 1


def test_changed_content_triggers_reanalysis(conn, item, result):
    item_id = db.upsert_item(conn, item)
    client = FakeClient(result)
    analyzer.analyze_and_store(conn, item_id, item, client=client, interests=INTERESTS)

    revised = item.model_copy(update={"content": "Substantially revised abstract."})
    analyzer.analyze_and_store(conn, item_id, revised, client=client, interests=INTERESTS)

    assert client.messages.calls == 2


def test_force_reanalyzes_unchanged_item(conn, item, result):
    item_id = db.upsert_item(conn, item)
    client = FakeClient(result)
    analyzer.analyze_and_store(conn, item_id, item, client=client, interests=INTERESTS)
    analyzer.analyze_and_store(
        conn, item_id, item, client=client, interests=INTERESTS, force=True
    )
    assert client.messages.calls == 2


def test_content_hash_ignores_unrelated_fields(item):
    same_text = item.model_copy(update={"url": "https://example.com/different"})
    assert analyzer.content_hash(item) == analyzer.content_hash(same_text)


def test_cost_calculation():
    usage = Usage(input_tokens=1_000_000, output_tokens=100_000, model="claude-haiku-4-5")
    assert usage.cost_usd == pytest.approx(1.00 + 0.5)


def test_cost_is_zero_for_unknown_model():
    assert Usage(input_tokens=1000, output_tokens=10, model="mystery").cost_usd == 0.0
