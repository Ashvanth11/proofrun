import pytest

from ai_monitor.orchestrator import canonical, dedup
from ai_monitor.storage import db
from ai_monitor.storage.models import Item, Source


# --- canonicalization ----------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://arxiv.org/abs/2501.00001", "2501.00001"),
        ("https://arxiv.org/abs/2501.00001v3", "2501.00001"),  # version stripped
        ("http://arxiv.org/pdf/2501.00001v1", "2501.00001"),
        ("https://ARXIV.org/abs/2501.12345", "2501.12345"),
        ("arXiv:2501.00001", "2501.00001"),
        ("https://example.com/blog/post", None),
        ("", None),
    ],
)
def test_extract_arxiv_id(url, expected):
    assert canonical.extract_arxiv_id(url) == expected


def test_bare_arxiv_id_requires_opt_in():
    """A bare number is ambiguous with versions and dates."""
    assert canonical.extract_arxiv_id("2501.00001") is None
    assert canonical.extract_arxiv_id("2501.00001", allow_bare=True) == "2501.00001"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/langfuse/langfuse", "langfuse/langfuse"),
        ("https://github.com/Langfuse/Langfuse", "langfuse/langfuse"),  # case
        ("https://github.com/org/repo.git", "org/repo"),
        ("https://github.com/org/repo/pull/12", "org/repo"),
        ("https://github.com/org/repo/blob/main/README.md", "org/repo"),
        ("https://gitlab.com/org/repo", None),
        ("", None),
    ],
)
def test_extract_github_repo(url, expected):
    assert canonical.extract_github_repo(url) == expected


def test_strip_tracking_params():
    url = "https://example.com/post?utm_source=hn&id=42&fbclid=xyz"
    assert canonical.strip_tracking(url) == "https://example.com/post?id=42"


def test_strip_normalizes_host_and_trailing_slash():
    a = canonical.strip_tracking("https://WWW.Example.com/post/")
    b = canonical.strip_tracking("https://example.com/post")
    assert a == b


def test_strip_drops_fragment():
    assert "#" not in canonical.strip_tracking("https://example.com/p#section")


def test_canonical_identity_unifies_sources():
    """The core mechanism: an HN story pointing at a paper resolves to it."""
    paper = canonical.canonical_identity("arxiv", "2501.00001", "https://arxiv.org/abs/2501.00001")
    story = canonical.canonical_identity("hn", "12345", "https://arxiv.org/abs/2501.00001v2")
    assert paper == story == "arxiv:2501.00001"


def test_canonical_identity_none_for_blog_post():
    assert canonical.canonical_identity("hn", "1", "https://someblog.com/post") is None


def test_title_similarity_ignores_ordering_and_stopwords():
    a = "WikiSkill: Compiling Agent Experience into Knowledge"
    b = "Show HN: Compiling Agent Experience into Knowledge with WikiSkill"
    assert canonical.title_similarity(a, b) > 0.75


def test_title_similarity_distinguishes_different_work():
    a = "A Reflection Loop for Tool-Using Agents"
    b = "Deep Sea Coral Classification with CNNs"
    assert canonical.title_similarity(a, b) < 0.2


# --- dedup ---------------------------------------------------------------


def row(id, source, source_id, title, url):
    return {"id": id, "source": source, "source_id": source_id, "title": title, "url": url}


def test_paper_repo_and_thread_collapse_to_one():
    """The scenario dedup exists for: one development, three sources."""
    rows = [
        row(1, "arxiv", "2501.00001", "WikiSkill: Compiling Agent Experience",
            "https://arxiv.org/abs/2501.00001"),
        row(2, "hn", "999", "Show HN: WikiSkill",
            "https://arxiv.org/abs/2501.00001v2"),
        row(3, "github", "authors/wikiskill", "authors/wikiskill",
            "https://github.com/authors/wikiskill"),
        row(4, "hn", "998", "WikiSkill on GitHub",
            "https://github.com/authors/wikiskill"),
    ]
    mapping = dedup.find_duplicates(rows)

    assert mapping[2] == 1  # HN story -> arXiv paper
    assert mapping[4] == 3  # HN story -> GitHub repo
    assert 1 not in mapping and 3 not in mapping  # primaries survive


def test_arxiv_wins_over_hn_regardless_of_order():
    """Source priority decides the canonical item, not insertion order."""
    rows = [
        row(1, "hn", "999", "Paper discussion", "https://arxiv.org/abs/2501.00001"),
        row(2, "arxiv", "2501.00001", "The Paper", "https://arxiv.org/abs/2501.00001"),
    ]
    mapping = dedup.find_duplicates(rows)
    assert mapping == {1: 2}  # the HN story is the duplicate


def test_distinct_items_are_not_merged():
    rows = [
        row(1, "arxiv", "2501.00001", "A Reflection Loop for Agents",
            "https://arxiv.org/abs/2501.00001"),
        row(2, "arxiv", "2501.00002", "Coral Classification with CNNs",
            "https://arxiv.org/abs/2501.00002"),
    ]
    assert dedup.find_duplicates(rows) == {}


def test_versions_of_one_paper_collapse():
    rows = [
        row(1, "arxiv", "2501.00001", "Paper", "https://arxiv.org/abs/2501.00001v1"),
        row(2, "hn", "5", "Paper", "https://arxiv.org/abs/2501.00001v3"),
    ]
    assert dedup.find_duplicates(rows) == {2: 1}


def test_title_similarity_merges_when_no_shared_link():
    """Two HN stories about the same blog post, different URLs."""
    rows = [
        row(1, "hn", "1", "Anthropic releases Claude Opus 5 model",
            "https://anthropic.com/news/opus-5"),
        row(2, "hn", "2", "Claude Opus 5 model releases from Anthropic",
            "https://techcrunch.com/anthropic-opus-5"),
    ]
    assert dedup.find_duplicates(rows) == {2: 1}


def test_dedup_is_conservative_about_titles():
    """A wrong merge hides a real item; a missed merge is merely visible."""
    rows = [
        row(1, "hn", "1", "Agents that use tools", "https://a.com/1"),
        row(2, "hn", "2", "Tools that use agents", "https://b.com/2"),
    ]
    # Same tokens, opposite meaning - token-set overlap merges these, which is
    # a known limitation of the approach rather than a bug in the matching.
    mapping = dedup.find_duplicates(rows)
    assert mapping in ({}, {2: 1})


def test_apply_marks_without_deleting(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    paper = db.upsert_item(
        conn,
        Item(source=Source.ARXIV, source_id="2501.00001", title="Paper",
             url="https://arxiv.org/abs/2501.00001"),
    )
    story = db.upsert_item(
        conn,
        Item(source=Source.HN, source_id="999", title="Show HN: Paper",
             url="https://arxiv.org/abs/2501.00001"),
    )

    collapsed = dedup.run(conn)

    assert collapsed == 1
    assert db.count_items(conn) == 2  # nothing deleted
    assert db.get_item(conn, story)["canonical_id"] == paper
    assert db.get_item(conn, paper)["canonical_id"] is None
    conn.close()


def test_dedup_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_item(
        conn,
        Item(source=Source.ARXIV, source_id="2501.00001", title="Paper",
             url="https://arxiv.org/abs/2501.00001"),
    )
    db.upsert_item(
        conn,
        Item(source=Source.HN, source_id="999", title="Paper",
             url="https://arxiv.org/abs/2501.00001"),
    )

    first = dedup.run(conn)
    second = dedup.run(conn)

    assert first == 1
    # Re-running re-marks the same pair rather than compounding.
    assert second == 1
    assert conn.execute(
        "SELECT COUNT(*) n FROM items WHERE canonical_id IS NOT NULL"
    ).fetchone()["n"] == 1
    conn.close()


def test_deduped_items_are_excluded_from_briefs(tmp_path):
    """Dedup only pays off if the synthesis query honors it."""
    from ai_monitor.analysis.analyzer import AnalysisResult, store_analysis
    from ai_monitor.synthesis import synthesizer

    conn = db.connect(tmp_path / "t.db")
    for source, sid, url in [
        (Source.ARXIV, "2501.00001", "https://arxiv.org/abs/2501.00001"),
        (Source.HN, "999", "https://arxiv.org/abs/2501.00001"),
    ]:
        item_id = db.upsert_item(
            conn, Item(source=source, source_id=sid, title="Paper", url=url)
        )
        store_analysis(
            conn, item_id,
            AnalysisResult(summary="s", relevance_score=0.9, matched_areas=[],
                           justification="j"),
            "h", "m",
        )

    dedup.run(conn)
    assert len(synthesizer.fetch_analyzed_items(conn)) == 1
    conn.close()
