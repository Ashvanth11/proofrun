import pytest

from ai_monitor.analysis.analyzer import AnalysisResult
from ai_monitor.orchestrator import graph
from ai_monitor.storage import db
from ai_monitor.storage.models import Item, Source
from ai_monitor.watchers import arxiv, github, hn


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    yield conn
    conn.close()


class FakeMessages:
    def __init__(self):
        self.calls = 0

    def parse(self, **kwargs):
        from types import SimpleNamespace

        self.calls += 1
        return SimpleNamespace(
            parsed_output=AnalysisResult(
                summary="s", relevance_score=0.8, matched_areas=[], justification="j"
            ),
            usage=SimpleNamespace(input_tokens=100, output_tokens=20),
        )

    def create(self, **kwargs):
        from types import SimpleNamespace

        self.calls += 1
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="## Brief")],
            usage=SimpleNamespace(input_tokens=500, output_tokens=200),
        )


class FakeClient:
    def __init__(self):
        self.messages = FakeMessages()


def fake_watcher(source: Source, prefix: str, count: int):
    def fetch(max_results=25):
        return [
            Item(
                source=source,
                source_id=f"{prefix}-{i}",
                title=f"{prefix} item {i}",
                url=f"https://example.com/{prefix}/{i}",
                content="Content about agents.",
            )
            for i in range(count)
        ]

    return fetch


@pytest.fixture
def patched_watchers(monkeypatch):
    monkeypatch.setattr(arxiv, "fetch", fake_watcher(Source.ARXIV, "arx", 2))
    monkeypatch.setattr(github, "fetch", fake_watcher(Source.GITHUB, "gh", 2))
    monkeypatch.setattr(hn, "fetch", fake_watcher(Source.HN, "hn", 1))


def test_graph_shape_is_fan_out_fan_in(conn):
    compiled = graph.build_graph(conn, None, "m", "m")
    mermaid = compiled.get_graph().draw_mermaid()

    for source in ["arxiv", "github", "hn"]:
        assert f"__start__ --> {source}" in mermaid  # parallel fan-out
        assert f"{source} --> store" in mermaid  # fan-in
    assert "store --> analyze" in mermaid
    assert "analyze --> synthesize" in mermaid


def test_graph_honors_source_selection(conn):
    compiled = graph.build_graph(conn, None, "m", "m", sources=["arxiv"])
    mermaid = compiled.get_graph().draw_mermaid()
    assert "arxiv" in mermaid
    assert "github" not in mermaid
    assert "hn" not in mermaid


def test_full_run_collects_all_sources(conn, patched_watchers, tmp_path, monkeypatch):
    from ai_monitor.synthesis import synthesizer

    monkeypatch.setattr(synthesizer, "REPORTS_DIR", tmp_path / "reports")

    compiled = graph.build_graph(conn, FakeClient(), "ollama/test", "ollama/test")
    final = compiled.invoke(graph.initial_state())

    assert len(final["item_ids"]) == 5  # 2 + 2 + 1 merged from parallel branches
    assert final["analyzed"] == 5
    assert final["failed"] == 0
    assert final["source_errors"] == []
    assert final["brief_path"] is not None


def test_failing_source_does_not_kill_the_run(
    conn, patched_watchers, tmp_path, monkeypatch
):
    """One dead source must degrade the run, not abort it."""
    from ai_monitor.synthesis import synthesizer

    monkeypatch.setattr(synthesizer, "REPORTS_DIR", tmp_path / "reports")

    def broken(max_results=25):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(github, "fetch", broken)

    compiled = graph.build_graph(conn, FakeClient(), "ollama/test", "ollama/test")
    final = compiled.invoke(graph.initial_state())

    assert len(final["item_ids"]) == 3  # arxiv + hn survived
    assert len(final["source_errors"]) == 1
    assert "github" in final["source_errors"][0]
    assert final["brief_path"] is not None  # the run still produced a brief


def test_rerun_skips_already_analyzed(conn, patched_watchers, tmp_path, monkeypatch):
    """The idempotency guarantee must hold through the graph, not just directly."""
    from ai_monitor.synthesis import synthesizer

    monkeypatch.setattr(synthesizer, "REPORTS_DIR", tmp_path / "reports")

    client = FakeClient()
    compiled = graph.build_graph(conn, client, "ollama/test", "ollama/test")

    first = compiled.invoke(graph.initial_state())
    calls_after_first = client.messages.calls

    second = compiled.invoke(graph.initial_state())

    assert first["analyzed"] == 5
    assert second["analyzed"] == 0
    assert second["skipped"] == 5
    # only the synthesis call was made the second time
    assert client.messages.calls == calls_after_first + 1


def test_mermaid_export_writes_file(conn, tmp_path):
    compiled = graph.build_graph(conn, None, "m", "m")
    path = graph.export_mermaid(compiled, str(tmp_path / "docs" / "pipeline.mmd"))
    assert "analyze" in open(path).read()
