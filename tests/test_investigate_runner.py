"""Gate, rendering, and cost-estimate tests.

The rendering tests are security tests wearing ordinary clothes: every string
in a report came from a README, a command's stdout, or a web page, and it ends
up in a markdown table that a human reads and eventually pastes into a README.
"""

import investigate as cli
import investigate_batch as batch
import pytest

from ai_monitor.agent import investigate as inv
from ai_monitor.agent import investigate_runner as runner
from ai_monitor.agent.loop import ToolCall
from ai_monitor.analysis.analyzer import Usage
from ai_monitor.storage import db
from ai_monitor.storage.models import Item, Source

QUESTION = inv.Question(repo="owner/name", claim="c", question="Does owner/name X?")


def make_run(verdict="supported", downgraded=False, ledger=(), calls=(), cost=0.0):
    return inv.InvestigationRun(
        question=QUESTION,
        steps_taken=3,
        stop_reason="sufficient_info",
        usage=Usage(input_tokens=int(cost * 500_000), model="claude-sonnet-5"),
        wall_seconds=42.0,
        downgraded=downgraded,
        tool_calls=list(calls),
        report=inv.Investigation(
            question=QUESTION.question,
            verdict=verdict,
            ledger=list(ledger),
            facts=inv.Facts(headline_capability="x", setup_seconds=12.0),
            summary="A summary.",
        ),
    )


def seed(conn, repo, score, content="# readme", investigated=False):
    item_id = db.upsert_item(
        conn,
        Item(
            source=Source.GITHUB,
            source_id=repo,
            title=repo,
            url=f"https://github.com/{repo}",
            content=content,
        ),
    )
    conn.execute(
        "INSERT INTO analyses (item_id, relevance_score, analyzed_at) VALUES (?,?,?)",
        (item_id, score, "2026-09-13"),
    )
    if investigated:
        conn.execute(
            "INSERT INTO investigations (item_id, repo, question) VALUES (?,?,?)",
            (item_id, repo, "q"),
        )
    conn.commit()
    return item_id


# --- the gate ------------------------------------------------------------


def test_only_repos_above_the_threshold_qualify(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    seed(conn, "a/high", 0.9)
    seed(conn, "a/low", 0.3)

    rows = runner.candidates(conn, threshold=0.6)
    assert [r["source_id"] for r in rows] == ["a/high"]
    conn.close()


def test_an_already_investigated_repo_is_not_investigated_again(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    seed(conn, "a/done", 0.9, investigated=True)
    seed(conn, "a/fresh", 0.8)

    assert [r["source_id"] for r in runner.candidates(conn)] == ["a/fresh"]
    assert len(runner.candidates(conn, force=True)) == 2
    conn.close()


def test_the_highest_scoring_repos_come_first(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    seed(conn, "a/mid", 0.7)
    seed(conn, "a/top", 0.95)
    seed(conn, "a/also", 0.8)

    rows = runner.candidates(conn, limit=2)
    assert [r["source_id"] for r in rows] == ["a/top", "a/also"]
    conn.close()


def test_non_github_and_duplicate_items_are_excluded(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    seed(conn, "a/good", 0.9)
    arxiv_id = db.upsert_item(
        conn,
        Item(source=Source.ARXIV, source_id="2501.1", title="t", url="http://x"),
    )
    conn.execute(
        "INSERT INTO analyses (item_id, relevance_score) VALUES (?,?)", (arxiv_id, 0.99)
    )
    dupe = seed(conn, "a/dupe", 0.99)
    conn.execute("UPDATE items SET canonical_id = ? WHERE id = ?", (1, dupe))
    conn.commit()

    assert [r["source_id"] for r in runner.candidates(conn)] == ["a/good"]
    conn.close()


def test_an_item_with_no_testable_claim_is_recorded_not_skipped(tmp_path):
    """"Four of nine READMEs claim nothing checkable" is a finding."""
    conn = db.connect(tmp_path / "t.db")
    item_id = seed(conn, "a/vague", 0.9, content="A list of links.")

    class NoClaim:
        def __init__(self):
            self.messages = self

        def parse(self, **kwargs):
            from types import SimpleNamespace

            return SimpleNamespace(
                parsed_output=inv.DerivedQuestion(has_testable_claim=False),
                usage=SimpleNamespace(input_tokens=100, output_tokens=10),
            )

    count, _ = runner.run_investigations_on_candidates(
        conn, NoClaim(), model="ollama/test"
    )

    assert count == 0
    row = conn.execute(
        "SELECT * FROM investigations WHERE item_id = ?", (item_id,)
    ).fetchone()
    assert row["verdict"] == "could_not_test"
    assert row["stop_reason"] == "no_testable_claim"
    assert "no_testable_claim" in row["blockers"]
    # and it is not offered again next run
    assert runner.candidates(conn) == []
    conn.close()


# --- escaping ------------------------------------------------------------


def test_pipes_from_a_readme_cannot_break_out_of_a_table_cell():
    assert runner.clean("a | b") == "a \\| b"


def test_control_characters_are_stripped():
    """The ESC and NUL go; the inert text they were steering does not matter."""
    out = runner.clean("safe\x1b[31mred\x00")
    assert "\x1b" not in out and "\x00" not in out
    assert out.startswith("safe")


def test_newlines_cannot_add_rows_to_a_table():
    assert "\n" not in runner.clean("row one\nrow two")


def test_long_text_is_truncated():
    assert len(runner.clean("x" * 5000, limit=100)) == 103  # 100 + "..."


def test_clean_handles_none_and_non_strings():
    assert runner.clean(None) == ""
    assert runner.clean({"a": 1})


def test_a_hostile_statement_is_escaped_in_the_table():
    run = make_run(
        ledger=[
            inv.Evidence(
                statement="ok | --- | DROP\nrow",
                side="for",
                kind="observed",
                source="sandbox_run",
            )
        ]
    )
    row = runner.table_row(run)
    assert _unescaped_pipes(row) == 11  # exactly the table's own delimiters
    assert "\n" not in row


def test_a_hostile_question_is_escaped_in_the_table():
    run = make_run()
    run.question.question = "Does x | y |\n| evil | row |?"
    assert _unescaped_pipes(runner.table_row(run)) == 11


def _unescaped_pipes(row: str) -> int:
    """Count only the pipes that still act as cell delimiters."""
    return row.replace("\\|", "").count("|")


# --- rendering -----------------------------------------------------------


def test_the_trajectory_names_the_verdict_and_the_evidence_kinds():
    run = make_run(
        ledger=[
            inv.Evidence(
                statement="printed resumed", side="for", kind="observed",
                source="sandbox_run(demo)",
            ),
            inv.Evidence(
                statement="README says so", side="for", kind="reported",
                source="read_file(README.md)",
            ),
        ],
        calls=[
            ToolCall(step=1, tool="read_file", arguments={"path": "README.md"},
                     is_error=False, result_summary="{}"),
            ToolCall(step=2, tool="sandbox_run", arguments={"command": "demo"},
                     is_error=False, result_summary="{}"),
        ],
    )
    text = runner.format_trajectory(run)

    assert "VERDICT   SUPPORTED" in text
    assert "OBSERVED" in text and "reported" in text
    assert "step 2: sandbox_run" in text
    assert "1 observed / 0 inspected / 1 reported" in text


def test_a_downgraded_verdict_says_so_in_the_trajectory():
    text = runner.format_trajectory(make_run(verdict="inconclusive", downgraded=True))
    assert "downgraded" in text


def test_a_run_with_no_conclusion_still_renders():
    run = make_run()
    run.report = None
    text = runner.format_trajectory(run)
    assert "no conclusion" in text


def test_a_failed_call_is_marked_in_the_trajectory():
    run = make_run(
        calls=[
            ToolCall(step=1, tool="read_file", arguments={"path": "nope"},
                     is_error=True, result_summary='{"error": "not found"}')
        ]
    )
    assert "x step 1: read_file" in runner.format_trajectory(run)


def test_a_web_search_is_marked_as_such():
    run = make_run(
        calls=[
            ToolCall(step=1, tool="web_search", arguments={"query": "q"},
                     is_error=False, result_summary="[]", server=True)
        ]
    )
    assert "[web]" in runner.format_trajectory(run)


def test_the_markdown_report_has_a_row_per_run_and_totals():
    runs = [make_run(cost=0.5), make_run(verdict="could_not_test", cost=0.25)]
    report = runner.markdown_report(runs)

    assert report.count("\n| ") >= 2
    assert "2 questions" in report
    assert "could_not_test" in report
    assert "## Trajectories" in report


# --- the cost estimate ---------------------------------------------------


def test_the_worst_case_exceeds_the_cost_cap():
    """The cap bounds the loop; the wrap-up, extraction and critique are extra."""
    caps = inv.default_caps(max_cost_usd=0.75)
    assert runner.worst_case_usd(1, caps) > 0.75


def test_the_worst_case_scales_with_the_number_of_questions():
    caps = inv.default_caps(max_cost_usd=0.75)
    one = runner.worst_case_usd(1, caps)
    assert runner.worst_case_usd(3, caps) == pytest.approx(3 * one)


def test_a_lower_cap_produces_a_lower_worst_case():
    low = runner.worst_case_usd(1, inv.default_caps(max_cost_usd=0.75))
    high = runner.worst_case_usd(1, inv.default_caps(max_cost_usd=2.00))
    assert low < high


def test_web_searches_are_priced_into_the_worst_case():
    caps = inv.default_caps()
    with_search = runner.worst_case_usd(1, caps, max_web_searches=5)
    without = runner.worst_case_usd(1, caps, max_web_searches=0)
    assert with_search - without == pytest.approx(0.05)


def test_an_unpriced_model_reports_zero_rather_than_guessing():
    assert runner.worst_case_usd(1, model="ollama/test") == 0.0


# --- the CLIs ------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Does owner/name resume after a kill?", "owner/name"),
        ("does BerriAI/litellm map 100+ providers?", "BerriAI/litellm"),
        ("Can micro.soft/a-p_m compile?", "micro.soft/a-p_m"),
    ],
)
def test_the_repo_is_found_in_the_question(text, expected):
    assert cli.find_repo(text) == expected


def test_a_question_naming_no_repo_is_refused():
    with pytest.raises(SystemExit):
        cli.find_repo("Does this thing work at all?")


@pytest.mark.parametrize(
    "text",
    [
        "Check ../../etc/passwd please",
        "Look at /etc/passwd",
        "see https://example.com/owner/name for details",
    ],
)
def test_a_path_segment_is_not_mistaken_for_a_repository(text):
    """"etc/passwd" is a valid owner/name shape; it is not a repository."""
    with pytest.raises(SystemExit):
        cli.find_repo(text)


def test_the_entry_point_default_is_below_the_library_ceiling():
    """Entry points cap tighter than the library's absolute ceiling."""
    assert runner.DEFAULT_MAX_COST_USD < inv.DEFAULT_MAX_COST_USD


def test_questions_yaml_loads_and_every_repo_validates(tmp_path):
    from pathlib import Path

    questions = batch.load_questions(Path("questions.yaml"))
    assert len(questions) >= 3
    assert all(q.repo and q.question for q in questions)


def test_a_batch_file_with_a_bad_repo_is_refused(tmp_path):
    path = tmp_path / "q.yaml"
    path.write_text("questions:\n  - repo: ../evil\n    question: q\n")
    with pytest.raises(SystemExit):
        batch.load_questions(path)


def test_a_batch_file_with_no_question_text_is_refused(tmp_path):
    path = tmp_path / "q.yaml"
    path.write_text("questions:\n  - repo: a/b\n")
    with pytest.raises(SystemExit):
        batch.load_questions(path)


def test_an_empty_batch_file_is_refused(tmp_path):
    path = tmp_path / "q.yaml"
    path.write_text("questions: []\n")
    with pytest.raises(SystemExit):
        batch.load_questions(path)
