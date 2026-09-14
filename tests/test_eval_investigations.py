"""Tests for the investigation scorer.

Written against fabricated runs, before the batch existed, so that each
criterion is pinned by a case that fails it as well as one that passes it. A
criterion only tested by things that pass is not a criterion, it is a hope.
"""

import json

import pytest

from ai_monitor.eval import investigations as ev
from ai_monitor.storage import db


def store(
    conn,
    repo="a/b",
    question="Does a/b X?",
    verdict="supported",
    ledger=(),
    blockers=(),
    tool_calls=(),
    stop_reason="sufficient_info",
    downgraded=0,
    critique_status="ok",
):
    report = {
        "question": question,
        "verdict": verdict,
        "blockers": list(blockers),
        "ledger": [
            {"statement": "s", "side": side, "kind": kind, "source": source}
            for side, kind, source in ledger
        ],
        "facts": {},
        "summary": "",
    }
    conn.execute(
        """
        INSERT INTO investigations
            (repo, question, verdict, blockers, report, tool_calls, stop_reason,
             downgraded, critique_status, cost_usd, wall_seconds, steps_taken)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            repo,
            question,
            verdict,
            json.dumps(list(blockers)),
            json.dumps(report),
            json.dumps(list(tool_calls)),
            stop_reason,
            downgraded,
            critique_status,
            0.25,
            40.0,
            6,
        ),
    )
    conn.commit()


def call(tool, executed=True, is_error=False):
    """A stored tool call. `executed` marks one that actually ran a container."""
    summary = '{"exit_code": 0, "stdout": "ok"}' if executed else '{"error": "refused"}'
    return {
        "step": 1,
        "tool": tool,
        "arguments": {"command": "x"},
        "is_error": is_error,
        "result_summary": summary,
    }


def question(**overrides):
    q = {
        "repo": "a/b",
        "question": "Does a/b X?",
        "category": "execution",
        "expect": {"verdict": ["supported"], "execution": "required"},
    }
    q.update(overrides)
    return q


def score_one(conn, q):
    return ev.score(conn, [q])[0]


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "t.db")
    yield connection
    connection.close()


# --- the execution modes -------------------------------------------------


def test_required_needs_an_observed_entry(conn):
    store(conn, ledger=[("for", "observed", "sandbox_run(x)")],
          tool_calls=[call("sandbox_run")])
    assert score_one(conn, question()).passed


def test_required_fails_on_inspected_evidence_alone(conn):
    """A licence fact does not show that a capability works."""
    store(conn, ledger=[("for", "inspected", "get_repo_metadata(a/b)")],
          tool_calls=[call("get_repo_metadata", executed=False)])
    result = score_one(conn, question())

    assert not result.passed
    assert [c.name for c in result.failures] == ["execution"]


def test_forbidden_passes_when_the_sandbox_was_never_touched(conn):
    store(conn, verdict="supported",
          ledger=[("for", "inspected", "get_repo_metadata(a/b)")],
          tool_calls=[call("get_repo_metadata", executed=False)])
    q = question(expect={"verdict": ["supported"], "execution": "forbidden"})
    assert score_one(conn, q).passed


def test_forbidden_fails_when_it_reached_for_a_container(conn):
    """Reaching for the expensive rung when it was not needed is the failure."""
    store(conn, verdict="supported",
          ledger=[("for", "observed", "sandbox_run(x)")],
          tool_calls=[call("sandbox_run")])
    q = question(expect={"verdict": ["supported"], "execution": "forbidden"})
    result = score_one(conn, q)

    assert not result.passed
    assert "ran 1 command" in result.failures[0].detail


def test_a_clone_refused_by_the_gate_does_not_count_as_using_the_sandbox(conn):
    """An oversize repo turned away never starts a container.

    litellm is in the set precisely to test the gate, and it would be absurd
    for the gate firing to count against the question that tests it.
    """
    store(conn, verdict="could_not_test", blockers=["needs_large_download"],
          ledger=[("unknown", "inspected", "get_repo_metadata(a/b)")],
          tool_calls=[call("sandbox_clone", executed=False, is_error=True),
                      call("get_repo_metadata", executed=False)])
    q = question(
        expect={
            "verdict": ["could_not_test"],
            "execution": "forbidden",
            "blockers_any_of": ["needs_large_download"],
        }
    )
    assert score_one(conn, q).passed


def test_a_clone_refused_by_the_language_gate_also_counts_as_no_sandbox_use(conn):
    """rig and avoid-ai-writing are the rows this has to make passable.

    Both are `execution: forbidden` on questions their metadata settles. The
    language gate turns the clone away before any container starts, and that
    refusal must not be scored as having reached for the sandbox - otherwise
    the fix for the failure would itself keep the row failing.
    """
    store(
        conn,
        verdict="could_not_test",
        blockers=["unsupported_language"],
        ledger=[("unknown", "inspected", "get_repo_metadata(a/b)")],
        tool_calls=[
            call("sandbox_clone", executed=False, is_error=True),
            call("get_repo_metadata", executed=False),
        ],
    )
    q = question(
        expect={
            "verdict": ["could_not_test"],
            "execution": "forbidden",
            "blockers_any_of": ["unsupported_language"],
        }
    )
    assert score_one(conn, q).passed


def test_attempt_accepts_a_real_execution(conn):
    store(conn, ledger=[("for", "observed", "sandbox_run(x)")],
          tool_calls=[call("sandbox_run")])
    q = question(expect={"verdict": ["supported"], "execution": "attempt"})
    assert score_one(conn, q).passed


def test_attempt_accepts_an_honest_named_blocker(conn):
    """The reason `attempt` exists: a correct refusal yields no observed entry.

    Under the old boolean flag every could_not_test question was unpassable by
    construction, which measured the flag rather than the agent.
    """
    store(conn, verdict="could_not_test", blockers=["install_failed"],
          ledger=[("unknown", "inspected", "get_repo_metadata(a/b)")],
          tool_calls=[call("get_repo_metadata", executed=False)])
    q = question(
        expect={
            "verdict": ["supported", "could_not_test"],
            "execution": "attempt",
            "blockers_any_of": ["install_failed", "timeout"],
        }
    )
    assert score_one(conn, q).passed


def test_attempt_rejects_giving_up_without_naming_anything(conn):
    store(conn, verdict="inconclusive", blockers=[],
          ledger=[("unknown", "reported", "read_file(README.md)")],
          tool_calls=[call("read_file", executed=False)])
    q = question(
        expect={
            "verdict": ["supported", "inconclusive"],
            "execution": "attempt",
            "blockers_any_of": ["install_failed"],
        }
    )
    result = score_one(conn, q)
    assert not result.passed
    assert "execution" in [c.name for c in result.failures]


def test_attempt_rejects_a_could_not_test_with_the_wrong_blocker(conn):
    store(conn, verdict="could_not_test", blockers=["needs_gpu"],
          tool_calls=[call("get_repo_metadata", executed=False)])
    q = question(
        expect={
            "verdict": ["could_not_test"],
            "execution": "attempt",
            "blockers_any_of": ["unsupported_language"],
        }
    )
    assert not score_one(conn, q).passed


# --- the other criteria --------------------------------------------------


def test_a_disallowed_verdict_fails(conn):
    store(conn, verdict="refuted",
          ledger=[("against", "observed", "sandbox_run(x)")],
          tool_calls=[call("sandbox_run")])
    result = score_one(conn, question())

    assert not result.passed
    assert "verdict" in [c.name for c in result.failures]


def test_an_entry_citing_nothing_real_fails(conn):
    store(conn, ledger=[("for", "observed", "sandbox_run(x)"),
                        ("for", "reported", "my own analysis")],
          tool_calls=[call("sandbox_run")])
    result = score_one(conn, question())

    assert not result.passed
    assert "citations" in [c.name for c in result.failures]


@pytest.mark.parametrize(
    "stop", ["step_cap", "cost_cap", "time_cap", "sandbox_cap", "disk_cap"]
)
def test_a_capped_run_fails_however_good_its_answer(conn, stop):
    """A capped run is unfinished, not answered.

    Counting it as a pass would reward a cap set too low - the number would
    improve as the budget shrank, which is exactly backwards.
    """
    store(conn, ledger=[("for", "observed", "sandbox_run(x)")],
          tool_calls=[call("sandbox_run")], stop_reason=stop)
    result = score_one(conn, question())

    assert not result.passed
    assert "stop_reason" in [c.name for c in result.failures]


def test_a_downgraded_verdict_fails_even_when_allowed(conn):
    """Landing on an allowed verdict by a route the rules had to correct."""
    store(conn, verdict="supported",
          ledger=[("for", "observed", "sandbox_run(x)")],
          tool_calls=[call("sandbox_run")], downgraded=1)
    result = score_one(conn, question())

    assert not result.passed
    assert "not_downgraded" in [c.name for c in result.failures]


def test_a_question_with_no_stored_run_fails_rather_than_disappearing(conn):
    result = score_one(conn, question(repo="never/run"))
    assert not result.passed
    assert [c.name for c in result.failures] == ["ran"]


def test_a_rewritten_question_does_not_credit_the_old_run(conn):
    """Matching on repo alone would score a stale answer as a fresh one."""
    store(conn, question="Does a/b do the OLD thing?")
    result = score_one(conn, question(question="Does a/b do the NEW thing?"))
    assert [c.name for c in result.failures] == ["ran"]


def test_the_newest_run_for_a_question_is_the_one_scored(conn):
    store(conn, verdict="inconclusive")
    store(conn, verdict="supported",
          ledger=[("for", "observed", "sandbox_run(x)")],
          tool_calls=[call("sandbox_run")])
    assert score_one(conn, question()).verdict == "supported"


# --- the eval set itself -------------------------------------------------


def test_the_shipped_question_set_is_well_formed():
    questions = ev.load_questions()
    assert len(questions) >= 10
    for q in questions:
        assert q["expect"]["execution"] in ev.EXECUTION_MODES
        assert q["expect"]["verdict"], f"{q['repo']} allows no verdict"
        assert q["question"].strip()


def test_a_bad_execution_mode_is_refused_not_ignored(tmp_path):
    path = tmp_path / "q.yaml"
    path.write_text(
        "questions:\n  - repo: a/b\n    question: q\n"
        "    expect:\n      verdict: [supported]\n      execution: sometimes\n"
    )
    with pytest.raises(ValueError, match="execution must be one of"):
        ev.load_questions(path)


def test_every_could_not_test_question_can_actually_pass():
    """A question whose criteria contradict each other measures nothing.

    `execution: required` with a `could_not_test` expectation is unpassable:
    a refusal never produces an observed entry.
    """
    for q in ev.load_questions():
        expect = q["expect"]
        if expect["verdict"] == ["could_not_test"]:
            assert expect["execution"] != "required", (
                f"{q['repo']} expects could_not_test but requires execution"
            )


# --- reporting -----------------------------------------------------------


def test_the_report_names_the_failing_criterion(conn):
    store(conn, verdict="refuted", tool_calls=[call("sandbox_run")])
    results = ev.score(conn, [question()])
    report = ev.render_report(results)

    assert "Where it failed" in report
    assert "verdict" in report
    assert "NO" in report


def test_the_summary_counts_every_evidence_kind(conn):
    store(
        conn,
        ledger=[("for", "observed", "sandbox_run(x)"),
                ("for", "inspected", "get_repo_metadata(a/b)"),
                ("for", "reported", "read_file(README.md)")],
        tool_calls=[call("sandbox_run"), call("get_repo_metadata", executed=False),
                    call("read_file", executed=False)],
    )
    stats = ev.summarize(ev.score(conn, [question()]))
    assert (stats["observed"], stats["inspected"], stats["reported"]) == (1, 1, 1)


def test_a_hostile_question_cannot_break_the_report_table(conn):
    store(conn, question="Does a/b | --- | evil?", verdict="supported",
          ledger=[("for", "observed", "sandbox_run(x)")],
          tool_calls=[call("sandbox_run")])
    results = ev.score(conn, [question(question="Does a/b | --- | evil?")])
    row = [ln for ln in ev.render_report(results).splitlines() if ln.startswith("| Does")][0]
    assert row.replace("\\|", "").count("|") == 11


def test_critique_status_is_reported(conn):
    store(conn, ledger=[("for", "observed", "sandbox_run(x)")],
          tool_calls=[call("sandbox_run")], critique_status="failed")
    stats = ev.summarize(ev.score(conn, [question()]))
    assert stats["critiques_failed"] == 1
    assert stats["critiques_run"] == 0
