import pytest

from ai_monitor import keywords


@pytest.mark.parametrize(
    "text,expected",
    [
        # Plurals must match: missing these silently drops relevant items.
        ("An agent framework", True),
        ("Multiple agents cooperating", True),
        ("benchmark results", True),
        ("several benchmarks", True),
        # Word boundaries must hold: matching these drags in unrelated items.
        ("An urgent problem", False),
        ("Management of resources", False),
        ("She said hello", False),
    ],
)
def test_plural_tolerance_without_losing_boundaries(text, expected):
    pattern = keywords.build_pattern(["agent", "benchmark", "ai"])
    assert keywords.matches(text, pattern) is expected


def test_short_terms_do_not_prefix_match():
    """`ai\\w*` would match aid/air; only a plural s is allowed."""
    pattern = keywords.build_pattern(["ai"])
    assert keywords.matches("first aid kit", pattern) is False
    assert keywords.matches("clean air act", pattern) is False
    assert keywords.matches("ai research", pattern) is True


def test_no_open_ended_suffix_matching():
    pattern = keywords.build_pattern(["agent"])
    assert keywords.matches("agentry is archaic", pattern) is False


def test_multiword_phrases_matched_literally():
    pattern = keywords.build_pattern(["tool use", "context window"])
    assert keywords.matches("a study of tool use in LLMs", pattern) is True
    assert keywords.matches("the context window grew", pattern) is True


def test_matching_is_case_insensitive():
    pattern = keywords.build_pattern(["llm"])
    assert keywords.matches("LLM benchmarks", pattern) is True
    assert keywords.matches("Llm results", pattern) is True


def test_find_matches_returns_distinct_sorted_terms():
    pattern = keywords.build_pattern(["agent", "tracing"])
    found = keywords.find_matches("Agent tracing for agents and AGENT tools", pattern)
    assert found == ["agent", "agents", "tracing"]


def test_empty_inputs_are_safe():
    assert keywords.build_pattern([]).search("anything") is None
    assert keywords.matches("", keywords.build_pattern(["agent"])) is False
    assert keywords.find_matches("", keywords.build_pattern(["agent"])) == []


def test_blank_terms_are_ignored():
    pattern = keywords.build_pattern(["agent", "", "   "])
    assert keywords.matches("agent", pattern) is True
    assert keywords.matches("literally anything else", pattern) is False


def test_hn_filter_now_catches_plurals():
    """Regression: the HN filter previously missed 'agents'."""
    from ai_monitor.watchers import hn

    assert hn.is_relevant("Show HN: A framework for LLM agents") is True
    assert hn.is_relevant("The urgent case for planning reform") is False


def test_routing_now_catches_plurals():
    """Regression: routing previously dropped items mentioning 'agents'."""
    from ai_monitor.config.settings import InterestArea
    from ai_monitor.orchestrator import routing

    interests = {"agents": InterestArea(description="Agents", keywords=["agent"])}
    decision = routing.route_item(1, "github", "A library", "Content about agents.", interests)
    assert decision.analyze is True
    assert "agents" in decision.matched_keywords
