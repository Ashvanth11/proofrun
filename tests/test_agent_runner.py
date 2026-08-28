from types import SimpleNamespace

import pytest

from ai_monitor.agent import runner
from ai_monitor.agent.critique import Critique
from ai_monitor.agent.repo_agent import RepoAssessment
from ai_monitor.analysis.analyzer import AnalysisResult, store_analysis
from ai_monitor.config.settings import InterestArea
from ai_monitor.providers import _TextBlock
from ai_monitor.storage import db
from ai_monitor.storage.models import Item, Source

INTERESTS = {"agents": InterestArea(description="Agents", keywords=["agent"])}


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    yield conn
    conn.close()


class StubClient:
    """Answers immediately without tool calls, so the loop exits on step 1."""

    def __init__(self):
        self.messages = self
        self.create_calls = 0

    def create(self, **kwargs):
        self.create_calls += 1
        return SimpleNamespace(
            content=[_TextBlock("Score 0.7, matches agents.")],
            usage=SimpleNamespace(input_tokens=800, output_tokens=90),
            stop_reason="end_turn",
        )

    def parse(self, **kwargs):
        output_format = kwargs["output_format"]
        parsed = (
            Critique(grounded=True, issues=[])
            if output_format is Critique
            else RepoAssessment(
                summary="A tool.",
                relevance_score=0.7,
                matched_areas=["agents"],
                justification="j",
            )
        )
        return SimpleNamespace(
            parsed_output=parsed,
            usage=SimpleNamespace(input_tokens=200, output_tokens=40),
        )


def add_repo(conn, name, score, source=Source.GITHUB):
    item_id = db.upsert_item(
        conn,
        Item(
            source=source,
            source_id=name,
            title=name,
            url=f"https://github.com/{name}",
            content="A repository.",
        ),
    )
    store_analysis(
        conn,
        item_id,
        AnalysisResult(
            summary="s", relevance_score=score, matched_areas=[], justification="j"
        ),
        "hash",
        "claude-haiku-4-5",
    )
    return item_id


# --- gating --------------------------------------------------------------


def test_only_repos_above_threshold_are_candidates(conn):
    add_repo(conn, "high/relevance", 0.8)
    add_repo(conn, "low/relevance", 0.1)

    names = [r["source_id"] for r in runner.candidates(conn, threshold=0.4)]
    assert names == ["high/relevance"]


def test_non_github_items_are_never_candidates(conn):
    """The agent's tools only work against repositories."""
    add_repo(conn, "2501.00001", 0.9, source=Source.ARXIV)
    add_repo(conn, "12345", 0.9, source=Source.HN)
    add_repo(conn, "a/repo", 0.9)

    names = [r["source_id"] for r in runner.candidates(conn)]
    assert names == ["a/repo"]


def test_unanalyzed_repos_are_not_candidates(conn):
    """Gating depends on the analyzer having scored the item first."""
    db.upsert_item(
        conn,
        Item(source=Source.GITHUB, source_id="un/scored", title="x", url="http://x"),
    )
    assert runner.candidates(conn) == []


def test_duplicates_are_excluded(conn):
    canonical = add_repo(conn, "original/repo", 0.9)
    dup = add_repo(conn, "mirror/repo", 0.9)
    conn.execute("UPDATE items SET canonical_id = ? WHERE id = ?", (canonical, dup))
    conn.commit()

    names = [r["source_id"] for r in runner.candidates(conn)]
    assert names == ["original/repo"]


def test_candidates_ordered_by_score(conn):
    add_repo(conn, "mid/repo", 0.6)
    add_repo(conn, "top/repo", 0.95)
    add_repo(conn, "low/repo", 0.45)

    names = [r["source_id"] for r in runner.candidates(conn, threshold=0.4)]
    assert names == ["top/repo", "mid/repo", "low/repo"]


def test_limit_caps_investigations(conn):
    for i in range(5):
        add_repo(conn, f"repo/{i}", 0.9)
    assert len(runner.candidates(conn, limit=2)) == 2


# --- idempotency ---------------------------------------------------------


def test_already_investigated_repos_are_skipped(conn):
    """A re-run must not re-pay for an investigation already done."""
    add_repo(conn, "a/repo", 0.9)
    client = StubClient()

    first, _ = runner.run_agent_on_candidates(
        conn, client, model="ollama/test", interests=INTERESTS
    )
    calls_after_first = client.create_calls

    second, _ = runner.run_agent_on_candidates(
        conn, client, model="ollama/test", interests=INTERESTS
    )

    assert first == 1
    assert second == 0
    assert client.create_calls == calls_after_first


def test_force_reinvestigates(conn):
    add_repo(conn, "a/repo", 0.9)
    client = StubClient()

    runner.run_agent_on_candidates(conn, client, model="ollama/test", interests=INTERESTS)
    count, _ = runner.run_agent_on_candidates(
        conn, client, model="ollama/test", interests=INTERESTS, force=True
    )
    assert count == 1


# --- execution -----------------------------------------------------------


def test_run_stores_trace_and_accumulates_cost(conn):
    add_repo(conn, "a/repo", 0.9)
    client = StubClient()

    count, usage = runner.run_agent_on_candidates(
        conn, client, model="claude-haiku-4-5", interests=INTERESTS
    )

    assert count == 1
    assert usage.input_tokens > 0
    assert usage.cost_usd > 0
    row = conn.execute("SELECT * FROM agent_runs").fetchone()
    assert row["stop_reason"] == "sufficient_info"


def test_nothing_above_threshold_makes_no_calls(conn):
    add_repo(conn, "low/repo", 0.1)
    client = StubClient()

    count, usage = runner.run_agent_on_candidates(
        conn, client, model="ollama/test", threshold=0.4, interests=INTERESTS
    )

    assert count == 0
    assert client.create_calls == 0
    assert usage.cost_usd == 0.0


def test_critique_can_be_disabled(conn):
    add_repo(conn, "a/repo", 0.9)
    client = StubClient()

    runner.run_agent_on_candidates(
        conn,
        client,
        model="ollama/test",
        interests=INTERESTS,
        with_critique=False,
    )
    row = conn.execute("SELECT * FROM agent_runs").fetchone()
    assert row["critique"] == ""
