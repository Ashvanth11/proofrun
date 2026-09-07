import pytest

from ai_monitor.analysis.analyzer import AnalysisResult, store_analysis
from ai_monitor.config.settings import InterestArea
from ai_monitor.orchestrator import routing
from ai_monitor.storage import db
from ai_monitor.storage.models import Item, Source

INTERESTS = {
    "agents": InterestArea(
        description="Agentic systems", keywords=["agent", "tool use", "agentic"]
    ),
    "llm-observability": InterestArea(
        description="Tracing", keywords=["tracing", "observability"]
    ),
}


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    yield conn
    conn.close()


def test_matching_item_is_routed_to_analysis():
    d = routing.route_item(
        1, "github", "An agent framework", "Supports tool use.", INTERESTS
    )
    assert d.analyze is True
    assert "agent" in d.matched_keywords


def test_unrelated_item_is_dropped():
    d = routing.route_item(
        1, "github", "org/cast-iron", "A guide to seasoning cookware.", INTERESTS
    )
    assert d.analyze is False
    assert d.reason == "no interest keywords present"


def test_arxiv_bypasses_the_filter():
    """arXiv results are already narrowed by category upstream."""
    d = routing.route_item(
        1, "arxiv", "Coral reef classification", "Underwater imagery.", INTERESTS
    )
    assert d.analyze is True
    assert "pre-filtered upstream" in d.reason


def test_multiword_keywords_match():
    d = routing.route_item(1, "github", "org/study", "A study of tool use in LLMs", INTERESTS)
    assert d.analyze is True
    assert "tool use" in d.matched_keywords


def test_word_boundaries_prevent_false_matches():
    """'agent' must not fire on 'urgent' - the same trap as the HN filter."""
    d = routing.route_item(
        1, "github", "org/urgent", "An urgent management problem.", INTERESTS
    )
    assert d.analyze is False


def test_content_is_searched_not_just_title():
    d = routing.route_item(
        1, "github", "mystery-project", "A library for distributed tracing.", INTERESTS
    )
    assert d.analyze is True
    assert "tracing" in d.matched_keywords


def test_route_partitions_unanalyzed_items(conn):
    keep_id = db.upsert_item(
        conn,
        Item(source=Source.GITHUB, source_id="a/agent-lib", title="An agent library",
             url="http://a", content="tool use"),
    )
    drop_id = db.upsert_item(
        conn,
        Item(source=Source.GITHUB, source_id="org/sourdough", title="org/sourdough",
             url="http://b", content="baking starters"),
    )

    keep, decisions = routing.route(conn, INTERESTS)

    assert keep == [keep_id]
    assert len(decisions) == 2
    assert {d.item_id: d.analyze for d in decisions} == {keep_id: True, drop_id: False}


def test_already_analyzed_items_are_not_rerouted(conn):
    """content_hash already handles those; routing is for new items."""
    item_id = db.upsert_item(
        conn,
        Item(source=Source.GITHUB, source_id="a/b", title="An agent library",
             url="http://a", content="agentic"),
    )
    store_analysis(
        conn, item_id,
        AnalysisResult(summary="s", relevance_score=0.8, matched_areas=[],
                       justification="j"),
        "hash", "model",
    )

    keep, decisions = routing.route(conn, INTERESTS)
    assert keep == []
    assert decisions == []


def test_duplicates_are_not_routed(conn):
    canonical = db.upsert_item(
        conn,
        Item(source=Source.ARXIV, source_id="2501.1", title="Agent paper", url="http://a"),
    )
    dup = db.upsert_item(
        conn,
        Item(source=Source.HN, source_id="1", title="Agent paper", url="http://b",
             content="agentic"),
    )
    conn.execute("UPDATE items SET canonical_id = ? WHERE id = ?", (canonical, dup))
    conn.commit()

    keep, _ = routing.route(conn, INTERESTS)
    assert keep == [canonical]


def test_empty_content_does_not_crash():
    d = routing.route_item(1, "github", "", "", INTERESTS)
    assert d.analyze is False


def test_hn_bypasses_the_filter():
    """HN is already keyword-filtered by the watcher at fetch time.

    Its titles name entities rather than describe work, so a second
    vocabulary filter drops exactly the ecosystem news the interest areas
    ask for - "GPT-6 Astra" contains no interest keyword at all.
    """
    for title in [
        "Nvidia agrees to acquire Hugging Face for $13B",
        "GPT-6 Astra",
        "Discovery of a new OpenAI agent message board",
    ]:
        assert routing.route_item(1, "hn", title, "", INTERESTS).analyze is True


def test_github_still_filtered():
    """GitHub descriptions are prose, so the keyword check still earns its place."""
    assert routing.route_item(
        1, "github", "org/cookbook", "Recipes for sourdough bread.", INTERESTS
    ).analyze is False
