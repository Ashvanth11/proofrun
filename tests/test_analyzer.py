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


def test_switching_models_reanalyzes(conn, item, result):
    """Regression: comparing content alone silently kept the old model's scores.

    Switching backends (local model -> Haiku) leaves content unchanged, so a
    content-only check skips every item and the new model never runs - while
    the run reports "skipped (unchanged)" and looks healthy.
    """
    item_id = db.upsert_item(conn, item)

    local = FakeClient(
        AnalysisResult(summary="local", relevance_score=0.8, matched_areas=[],
                       justification="j")
    )
    analyzer.analyze_and_store(
        conn, item_id, item, client=local, interests=INTERESTS,
        model="ollama/llama3.1",
    )

    haiku = FakeClient(
        AnalysisResult(summary="haiku", relevance_score=0.3, matched_areas=[],
                       justification="j")
    )
    usage = analyzer.analyze_and_store(
        conn, item_id, item, client=haiku, interests=INTERESTS,
        model="claude-haiku-4-5",
    )

    assert haiku.messages.calls == 1  # the new model actually ran
    assert usage is not None
    row = conn.execute(
        "SELECT model, relevance_score, summary FROM analyses WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    assert row["model"] == "claude-haiku-4-5"
    assert row["relevance_score"] == 0.3
    assert row["summary"] == "haiku"


def test_same_model_still_skips(conn, item, result):
    """The cost saving must survive the fix - re-runs on one model stay free."""
    item_id = db.upsert_item(conn, item)
    client = FakeClient(result)

    analyzer.analyze_and_store(
        conn, item_id, item, client=client, interests=INTERESTS, model="claude-haiku-4-5"
    )
    second = analyzer.analyze_and_store(
        conn, item_id, item, client=client, interests=INTERESTS, model="claude-haiku-4-5"
    )

    assert second is None
    assert client.messages.calls == 1


def test_needs_analysis_ignores_model_when_unspecified(conn, item, result):
    """Callers that only care about content staleness pass no model."""
    item_id = db.upsert_item(conn, item)
    analyzer.store_analysis(
        conn, item_id, result, analyzer.content_hash(item), "ollama/llama3.1"
    )
    assert analyzer.needs_analysis(conn, item_id, analyzer.content_hash(item)) is False


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


def test_editing_the_prompt_invalidates_cached_analyses(conn, item, result):
    """Regression: a prompt fix that changes nothing is indistinguishable from
    a prompt fix that does not work.

    The analysis is a function of the content and the instructions that scored
    it, so the prompt is part of the cache key.
    """
    item_id = db.upsert_item(conn, item)
    client = FakeClient(result)

    analyzer.analyze_and_store(
        conn, item_id, item, client=client, interests=INTERESTS, model="m"
    )
    assert client.messages.calls == 1

    original = analyzer.SYSTEM_PROMPT
    try:
        analyzer.SYSTEM_PROMPT = original + "\n\nAn additional calibration rule."
        analyzer.analyze_and_store(
            conn, item_id, item, client=client, interests=INTERESTS, model="m"
        )
    finally:
        analyzer.SYSTEM_PROMPT = original

    assert client.messages.calls == 2  # the edited prompt actually re-ran


def test_identical_prompt_still_skips(conn, item, result):
    item_id = db.upsert_item(conn, item)
    client = FakeClient(result)
    for _ in range(2):
        analyzer.analyze_and_store(
            conn, item_id, item, client=client, interests=INTERESTS, model="m"
        )
    assert client.messages.calls == 1
