import json
from datetime import date
from types import SimpleNamespace

import pytest

from ai_monitor.analysis.analyzer import AnalysisResult, store_analysis
from ai_monitor.storage import db
from ai_monitor.storage.models import Item, Source
from ai_monitor.synthesis import synthesizer


class FakeMessages:
    def __init__(self, markdown: str):
        self.markdown = markdown
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.markdown)],
            usage=SimpleNamespace(input_tokens=2000, output_tokens=800),
        )


class FakeClient:
    def __init__(self, markdown="## This Week\n\nSomething happened."):
        self.messages = FakeMessages(markdown)


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    yield conn
    conn.close()


def add_analyzed(conn, source_id, title, score, areas):
    item = Item(
        source=Source.ARXIV,
        source_id=source_id,
        title=title,
        url=f"https://arxiv.org/abs/{source_id}",
        content="Some abstract.",
    )
    item_id = db.upsert_item(conn, item)
    store_analysis(
        conn,
        item_id,
        AnalysisResult(
            summary=f"Summary of {title}",
            relevance_score=score,
            matched_areas=areas,
            justification="Because.",
        ),
        "hash",
        "claude-haiku-4-5",
    )
    return item_id


def test_fetch_respects_min_score(conn):
    add_analyzed(conn, "1", "High relevance", 0.9, ["agents"])
    add_analyzed(conn, "2", "Low relevance", 0.1, [])

    assert len(synthesizer.fetch_analyzed_items(conn)) == 2
    rows = synthesizer.fetch_analyzed_items(conn, min_score=0.5)
    assert len(rows) == 1
    assert rows[0]["title"] == "High relevance"


def test_fetch_excludes_duplicates(conn):
    """Items collapsed into a canonical item must not appear twice in a brief."""
    canonical = add_analyzed(conn, "1", "Original", 0.9, ["agents"])
    dup = add_analyzed(conn, "2", "Duplicate", 0.9, ["agents"])
    conn.execute("UPDATE items SET canonical_id = ? WHERE id = ?", (canonical, dup))
    conn.commit()

    rows = synthesizer.fetch_analyzed_items(conn)
    assert [r["title"] for r in rows] == ["Original"]


def test_prompt_includes_scores_and_areas(conn):
    add_analyzed(conn, "1", "A Reflection Loop", 0.85, ["agents"])
    prompt = synthesizer.build_prompt(synthesizer.fetch_analyzed_items(conn))
    assert "A Reflection Loop" in prompt
    assert "0.85" in prompt
    assert "agents" in prompt


def test_run_writes_brief_and_records_it(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(synthesizer, "REPORTS_DIR", tmp_path / "reports")
    add_analyzed(conn, "1", "Something", 0.8, ["agents"])

    client = FakeClient("## Brief\n\nContent here.")
    path, usage = synthesizer.run(conn, client=client)

    assert path.read_text() == "## Brief\n\nContent here."
    assert usage.model == synthesizer.SYNTHESIS_MODEL
    row = conn.execute("SELECT * FROM briefs").fetchone()
    assert row["item_count"] == 1
    assert row["markdown"] == "## Brief\n\nContent here."


def test_run_with_no_items_makes_no_api_call(conn):
    client = FakeClient()
    path, usage = synthesizer.run(conn)
    assert path is None and usage is None
    assert client.messages.calls == 0


def test_week_of_format():
    assert synthesizer.week_of(date(2026, 8, 27)) == "2026-W35"
