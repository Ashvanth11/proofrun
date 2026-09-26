import json
from types import SimpleNamespace

import pytest

from ai_monitor.agent import critique as critique_mod
from ai_monitor.agent.critique import Critique
from ai_monitor.agent.repo_agent import AgentRun, RepoAssessment, ToolCall, store_run
from ai_monitor.analysis.analyzer import Usage
from ai_monitor.storage import db
from ai_monitor.storage.models import Item, Source


def make_run(grounded_evidence=True, assessment=None):
    calls = []
    if grounded_evidence:
        calls.append(
            ToolCall(
                step=1,
                tool="get_repo_metadata",
                arguments={"repo": "a/b"},
                is_error=False,
                result_summary='{"description": "LLM tracing platform", "stars": 33835}',
            )
        )
    return AgentRun(
        repo="a/b",
        steps_taken=2,
        tool_calls=calls,
        stop_reason="sufficient_info",
        assessment=assessment
        or RepoAssessment(
            summary="An LLM tracing platform.",
            relevance_score=0.85,
            matched_areas=["llm-observability"],
            justification="Directly addresses tracing.",
        ),
        usage=Usage(model="ollama/test"),
    )


class CritiqueClient:
    """Returns a scripted critique, then a scripted revision."""

    def __init__(self, grounded=True, issues=None, revision=None):
        self.grounded = grounded
        self.issues = issues or []
        self.revision = revision
        self.calls = []
        self.messages = self

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        output_format = kwargs["output_format"]

        if output_format is Critique:
            parsed = Critique(grounded=self.grounded, issues=self.issues)
        else:
            parsed = self.revision or RepoAssessment(
                summary="Revised summary.",
                relevance_score=0.4,
                matched_areas=[],
                justification="Evidence did not support the original claim.",
            )

        return SimpleNamespace(
            parsed_output=parsed,
            usage=SimpleNamespace(input_tokens=400, output_tokens=60),
        )


def test_grounded_assessment_is_not_revised():
    run = make_run()
    client = CritiqueClient(grounded=True)

    result, usage = critique_mod.critique_and_revise(run, client, model="ollama/test")

    assert result.revised is False
    assert result.assessment.relevance_score == 0.85  # unchanged
    assert len(client.calls) == 1  # critique only, no revision call


def test_ungrounded_assessment_triggers_one_revision():
    run = make_run()
    client = CritiqueClient(
        grounded=False,
        issues=["Summary claims a plugin architecture; metadata does not show this."],
    )

    result, usage = critique_mod.critique_and_revise(run, client, model="ollama/test")

    assert result.revised is True
    assert result.assessment.relevance_score == 0.4  # the revised value
    assert result.original_assessment.relevance_score == 0.85  # original kept
    assert len(client.calls) == 2  # critique then revise


def test_grounded_label_does_not_hide_specific_critique_issues():
    run = make_run()
    client = CritiqueClient(grounded=True, issues=["Source attribution is wrong."])

    result, _ = critique_mod.critique_and_revise(run, client, model="ollama/test")

    assert result.revised is True
    assert len(client.calls) == 2


def test_revision_happens_at_most_once():
    """A critic and a reviser could otherwise disagree indefinitely."""
    run = make_run()
    client = CritiqueClient(grounded=False, issues=["still wrong"])

    critique_mod.critique_and_revise(run, client, model="ollama/test")

    assert len(client.calls) == 2  # never a third round


def test_critic_sees_evidence_but_not_the_agents_reasoning():
    """The critic must judge the claim, not be argued into agreeing."""
    run = make_run()
    client = CritiqueClient(grounded=True)
    critique_mod.critique_and_revise(run, client, model="ollama/test")

    prompt = client.calls[0]["messages"][0]["content"]
    assert "EVIDENCE GATHERED" in prompt
    assert "get_repo_metadata" in prompt
    assert "LLM tracing platform" in prompt  # the tool result
    assert "CONCLUSION DRAWN" in prompt


def test_no_tools_called_is_stated_explicitly():
    """An assessment with zero evidence is the strongest grounding signal."""
    run = make_run(grounded_evidence=False)
    assert "no tools were called" in critique_mod.format_evidence(run)


def test_failed_tool_calls_are_marked_in_evidence():
    run = make_run()
    run.tool_calls.append(
        ToolCall(
            step=2,
            tool="read_file",
            arguments={"repo": "a/b", "path": "README.md"},
            is_error=True,
            result_summary='{"error": "not found"}',
        )
    )
    evidence = critique_mod.format_evidence(run)
    assert "[FAILED]" in evidence


def test_run_without_assessment_is_skipped():
    run = make_run()
    run.assessment = None
    client = CritiqueClient()

    verdict, usage = critique_mod.critique(run, client, model="ollama/test")

    assert verdict is None
    assert client.calls == []  # nothing to critique, no call made


def test_critique_failure_leaves_run_untouched():
    class Broken:
        def __init__(self):
            self.messages = self

        def parse(self, **kwargs):
            from ai_monitor.providers import OllamaError

            raise OllamaError("unavailable")

    run = make_run()
    result, usage = critique_mod.critique_and_revise(run, Broken(), model="ollama/test")

    assert result.revised is False
    assert result.assessment.relevance_score == 0.85


def test_critique_is_persisted(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    item_id = db.upsert_item(
        conn,
        Item(source=Source.GITHUB, source_id="a/b", title="a/b", url="http://x"),
    )
    run = make_run()
    client = CritiqueClient(grounded=False, issues=["overreaching summary"])
    critique_mod.critique_and_revise(run, client, model="ollama/test")

    store_run(conn, item_id, run)

    row = conn.execute("SELECT * FROM agent_runs WHERE item_id = ?", (item_id,)).fetchone()
    assert row["revised"] == 1
    stored = json.loads(row["critique"])
    assert stored["grounded"] is False
    assert "overreaching summary" in stored["issues"]
    conn.close()
