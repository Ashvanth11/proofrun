import json
from types import SimpleNamespace

import pytest

from ai_monitor.analysis.analyzer import AnalysisResult, store_analysis
from ai_monitor.storage import db
from ai_monitor.storage.models import Item, Source
from ai_monitor.synthesis import synthesizer


class RecordingClient:
    """Records every call so map-reduce structure can be asserted."""

    def __init__(self):
        self.messages = self
        self.systems = []
        self.contents = []

    def create(self, **kwargs):
        self.systems.append(kwargs["system"])
        self.contents.append(kwargs["messages"][0]["content"])
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="## A theme\n\nSupported by items.")],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=300),
        )


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    yield conn
    conn.close()


def add(conn, sid, title, score, areas):
    item_id = db.upsert_item(
        conn,
        Item(
            source=Source.ARXIV,
            source_id=sid,
            title=title,
            url=f"https://arxiv.org/abs/{sid}",
            content="Abstract.",
        ),
    )
    store_analysis(
        conn,
        item_id,
        AnalysisResult(
            summary=f"Summary of {title}",
            relevance_score=score,
            matched_areas=areas,
            justification="j",
        ),
        "hash",
        "model",
    )
    return item_id


def test_small_corpus_uses_a_single_call(conn):
    for i in range(5):
        add(conn, str(i), f"Paper {i}", 0.8, ["agents"])
    rows = synthesizer.fetch_analyzed_items(conn)

    client = RecordingClient()
    markdown, usage = synthesizer.synthesize(rows, client=client, model="m")

    assert len(client.systems) == 1
    assert client.systems[0] == synthesizer.SYSTEM_PROMPT
    assert usage.input_tokens == 1000


def test_large_corpus_maps_over_areas_then_combines(conn):
    for i in range(30):
        add(conn, f"a{i}", f"Agent paper {i}", 0.8, ["agents"])
    for i in range(30):
        add(conn, f"o{i}", f"Tracing paper {i}", 0.8, ["llm-observability"])
    rows = synthesizer.fetch_analyzed_items(conn, limit=100)

    client = RecordingClient()
    markdown, usage = synthesizer.synthesize(
        rows, client=client, model="m", grouping_threshold=40
    )

    # two area summaries plus one combine
    assert len(client.systems) == 3
    assert client.systems[:2] == [synthesizer.GROUP_PROMPT] * 2
    assert client.systems[2] == synthesizer.COMBINE_PROMPT
    # cost accumulates across every call
    assert usage.input_tokens == 3000


def test_grouping_threshold_is_the_switch(conn):
    for i in range(10):
        add(conn, str(i), f"Paper {i}", 0.8, ["agents"])
    rows = synthesizer.fetch_analyzed_items(conn)

    single = RecordingClient()
    synthesizer.synthesize(rows, client=single, model="m", grouping_threshold=50)
    assert len(single.systems) == 1

    mapped = RecordingClient()
    synthesizer.synthesize(rows, client=mapped, model="m", grouping_threshold=5)
    assert len(mapped.systems) > 1


def test_multi_area_item_appears_in_each_group(conn):
    """Themes cross areas; the combine step merges the resulting overlap."""
    add(conn, "1", "Agent tracing paper", 0.9, ["agents", "llm-observability"])
    rows = synthesizer.fetch_analyzed_items(conn)

    groups = synthesizer.group_by_area(rows)
    assert set(groups) == {"agents", "llm-observability"}
    assert len(groups["agents"]) == len(groups["llm-observability"]) == 1


def test_unmatched_items_are_grouped_not_dropped(conn):
    add(conn, "1", "Something odd", 0.5, [])
    groups = synthesizer.group_by_area(synthesizer.fetch_analyzed_items(conn))
    assert "uncategorized" in groups


def test_prompt_demands_themes_not_categories():
    """The distinction the whole upgrade rests on."""
    prompt = synthesizer.SYSTEM_PROMPT
    assert "THEMES" in prompt
    assert "not a theme" in prompt  # explicitly rules out bare categories
    assert "at least two items" in prompt
    assert "Do not manufacture connections" in prompt


def test_prompt_forbids_padding_when_items_do_not_connect():
    assert "write fewer themes" in synthesizer.SYSTEM_PROMPT


def test_items_reach_the_prompt_with_links_and_scores(conn):
    add(conn, "2501.99999", "A Reflection Loop", 0.85, ["agents"])
    rows = synthesizer.fetch_analyzed_items(conn)

    client = RecordingClient()
    synthesizer.synthesize(rows, client=client, model="m")

    content = client.contents[0]
    assert "A Reflection Loop" in content
    assert "https://arxiv.org/abs/2501.99999" in content
    assert "0.85" in content
    assert "agents" in content
