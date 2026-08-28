from datetime import datetime, timezone

import httpx
import pytest

from ai_monitor.storage.models import Source
from ai_monitor.watchers import github, hn

# --- Hacker News --------------------------------------------------------

STORY = {
    "id": 49473483,
    "type": "story",
    "title": "Show HN: An agentic framework for local LLMs",
    "url": "https://example.com/post",
    "score": 240,
    "by": "someone",
    "time": 1787882246,
    "descendants": 65,
}


def test_parse_story_maps_core_fields():
    item = hn.parse_story(STORY)
    assert item.source == Source.HN
    assert item.source_id == "49473483"
    assert item.url == "https://example.com/post"
    assert item.authors == ["someone"]
    assert item.published_at.tzinfo == timezone.utc
    assert item.raw["score"] == 240
    assert item.raw["discussion_url"] == "https://news.ycombinator.com/item?id=49473483"


def test_story_without_url_falls_back_to_discussion():
    """Ask HN and text posts have no outbound link."""
    item = hn.parse_story({**STORY, "url": None})
    assert item.url == "https://news.ycombinator.com/item?id=49473483"


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Show HN: I built an LLM agent framework", True),
        ("Anthropic releases a new Claude model", True),
        ("Gemini Omni 1.1 Flash", True),
        ("Stripe said to abandon $50B pursuit of PayPal", False),
        ("The urgent case for urban planning reform", False),
        ("She said the meeting went well", False),
        ("", False),
    ],
)
def test_keyword_filter(title, expected):
    """Word boundaries matter: 'urgent' must not match 'agent', 'said' not ' ai '."""
    assert hn.is_relevant(title) is expected


def test_fetch_filters_and_sorts(monkeypatch):
    stories = {
        1: {**STORY, "id": 1, "title": "An LLM agent tool", "score": 50},
        2: {**STORY, "id": 2, "title": "Cooking with cast iron", "score": 900},
        3: {**STORY, "id": 3, "title": "Claude gets better at code", "score": 300},
        4: {**STORY, "id": 4, "title": "New transformer variant", "score": 5},
        5: {**STORY, "id": 5, "title": "A job posting", "score": 400, "type": "job"},
    }

    def fake_get_json(client, path):
        if path.startswith("topstories"):
            return list(stories)
        return stories[int(path.split("/")[1].split(".")[0])]

    monkeypatch.setattr(hn, "_get_json", fake_get_json)
    items = hn.fetch(min_score=20)

    titles = [i.title for i in items]
    assert titles == ["Claude gets better at code", "An LLM agent tool"]
    assert "Cooking with cast iron" not in titles  # not AI-relevant
    assert "New transformer variant" not in titles  # below score floor
    assert "A job posting" not in titles  # not type=story


def test_fetch_survives_listing_failure(monkeypatch):
    def boom(client, path):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(hn, "_get_json", boom)
    assert hn.fetch() == []  # a dead source returns nothing, it doesn't raise


# --- GitHub -------------------------------------------------------------

REPO = {
    "full_name": "langfuse/langfuse",
    "html_url": "https://github.com/langfuse/langfuse",
    "description": "Open source LLM engineering platform",
    "topics": ["observability", "llmops"],
    "language": "TypeScript",
    "stargazers_count": 33835,
    "forks_count": 3000,
    "pushed_at": "2026-08-27T10:00:00Z",
    "created_at": "2023-05-18T10:00:00Z",
    "owner": {"login": "langfuse"},
}


def test_parse_repo_maps_core_fields():
    item = github.parse_repo(REPO)
    assert item.source == Source.GITHUB
    assert item.source_id == "langfuse/langfuse"
    assert item.raw["stars"] == 33835
    assert item.authors == ["langfuse"]
    assert item.published_at.year == 2023


def test_content_includes_topics_for_analysis():
    """Search results carry no README, so topics are most of the signal."""
    content = github.parse_repo(REPO).content
    assert "Open source LLM engineering platform" in content
    assert "observability" in content and "llmops" in content


def test_repo_without_description_still_parses():
    item = github.parse_repo({**REPO, "description": None, "topics": []})
    assert item.content == ""
    assert item.source_id == "langfuse/langfuse"


def test_query_uses_one_topic():
    """GitHub ANDs topic qualifiers, so multi-topic queries match nothing."""
    q = github.build_query("llm", 100, datetime(2026, 8, 14, tzinfo=timezone.utc))
    assert q.count("topic:") == 1
    assert "topic:llm" in q
    assert "stars:>=100" in q
    assert "pushed:>2026-08-14" in q


def test_fetch_dedupes_across_topics(monkeypatch):
    """A repo carrying several of our topics must appear once."""
    other = {**REPO, "full_name": "promptfoo/promptfoo", "stargazers_count": 24633}
    calls = []

    def fake_search(query, per_page, timeout):
        calls.append(query)
        return [REPO, other] if "ai-agents" in query else [REPO]

    monkeypatch.setattr(github, "_search", fake_search)
    items = github.fetch(topics=["ai-agents", "llm", "llmops"])

    assert len(calls) == 3  # one request per topic
    assert [i.source_id for i in items] == ["langfuse/langfuse", "promptfoo/promptfoo"]


def test_one_failing_topic_does_not_lose_the_others(monkeypatch):
    def fake_search(query, per_page, timeout):
        if "llm" in query and "llmops" not in query:
            raise github.GitHubError("transient failure")
        return [REPO]

    monkeypatch.setattr(github, "_search", fake_search)
    items = github.fetch(topics=["ai-agents", "llm", "llmops"])
    assert len(items) == 1


def test_rate_limit_aborts_rather_than_retrying_every_topic(monkeypatch):
    """Once the limit is exhausted, remaining queries would fail too."""
    def fake_search(query, per_page, timeout):
        raise github.GitHubError("GitHub search rate limit exhausted. Set GITHUB_TOKEN")

    monkeypatch.setattr(github, "_search", fake_search)
    with pytest.raises(github.GitHubError, match="rate limit"):
        github.fetch(topics=["ai-agents", "llm"])


def test_results_sorted_by_stars(monkeypatch):
    small = {**REPO, "full_name": "a/small", "stargazers_count": 10}
    big = {**REPO, "full_name": "b/big", "stargazers_count": 99999}
    monkeypatch.setattr(github, "_search", lambda q, p, t: [small, big])
    items = github.fetch(topics=["llm"])
    assert [i.source_id for i in items] == ["b/big", "a/small"]
