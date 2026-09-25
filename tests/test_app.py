"""Offline smoke checks for the recorded page and live UI orchestration."""

from contextlib import contextmanager

import anthropic
import httpx
import pytest

import app
from ai_monitor.agent import investigate as inv
from ai_monitor.agent import sandbox
from ai_monitor.analysis.analyzer import Usage
from ai_monitor.storage import db


@pytest.fixture(autouse=True)
def offline_public_feed(monkeypatch):
    from ai_monitor import monitoring_feed
    monkeypatch.setattr(monitoring_feed, "fetch_public_feed", lambda: (None, ""))


def fake_run(question, *, report=True):
    return inv.InvestigationRun(
        question=question,
        steps_taken=2,
        stop_reason="sufficient_info",
        usage=Usage(input_tokens=1000, output_tokens=100, model=inv.INVESTIGATE_MODEL),
        final_text="The model's conclusion.",
        report=(
            inv.Investigation(
                question=question.question,
                verdict="inconclusive",
                summary="The evidence did not settle it.",
                ledger=[
                    inv.Evidence(
                        statement="The README claims it works.",
                        side="unknown",
                        kind="reported",
                        source="read_file(README.md)",
                    )
                ],
            )
            if report else None
        ),
    )


class FakePage:
    def __init__(self):
        self.texts = []
        self.codes = []
        self.headings = []

    def subheader(self, value):
        self.headings.append(value)

    def caption(self, value):
        self.texts.append(value)

    def text(self, value):
        self.texts.append(value)

    def warning(self, value):
        self.texts.append(value)

    def markdown(self, value):
        self.headings.append(value)

    def code(self, value, language=None):
        self.codes.append(value)

    def columns(self, count, **kwargs):
        from contextlib import nullcontext
        return [nullcontext() for _ in range(count)]

    @contextmanager
    def expander(self, label):
        self.texts.append(label)
        yield


def test_recorded_examples_render_without_client_or_database(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("a recorded example attempted live I/O")

    monkeypatch.setattr(db, "connect", unexpected)
    monkeypatch.setattr(app.anthropic, "Anthropic", unexpected)
    examples = app.load_examples()
    assert {e["category"] for e in examples} == {"executed", "read_only", "could_not_test"}
    for example in examples:
        assert example["status"] == "recorded"
        assert example["recorded_at"] == "2026-09-14"
        assert example["tool_calls"] and example["ledger"]
        page = FakePage()
        app.render_view(page, example)
        assert any("Recorded example" in text for text in page.texts)
        assert any("Step " in text for text in page.texts)
        assert any("estimated token cost" in text for text in page.texts)
        assert page.codes


@pytest.mark.parametrize(
    "question,override,expected",
    [
        ("Does owner/name work?", "", "owner/name"),
        ("Does this work?", "owner/name", "owner/name"),
        ("Does owner/name work?", "other/repo", "other/repo"),
    ],
)
def test_question_detection_and_override(question, override, expected):
    assert app.prepare_question(question, override).repo == expected


@pytest.mark.parametrize(
    "question,override",
    [("", ""), ("Does this work?", ""), ("Does this work?", "../../etc/passwd")],
)
def test_invalid_input_fails_before_execution(question, override):
    with pytest.raises(app.InputError):
        app.prepare_question(question, override)


def test_missing_key_fails_without_claiming_a_run():
    state = {}
    with pytest.raises(app.InputError, match="ANTHROPIC_API_KEY"):
        app.run_live(app.prepare_question("Does owner/name work?"), state, api_key="")
    assert state == {}


def test_live_result_is_stored_and_repeat_click_reuses_it(tmp_path):
    question = app.prepare_question("Does owner/name work?")
    state = {}
    calls = []

    def investigator(request, client):
        calls.append((request, client))
        return fake_run(request)

    def critic(run, client):
        run.critique_status = "ok"
        return run, Usage(input_tokens=100, model=inv.INVESTIGATE_MODEL)

    def connect():
        return db.connect(tmp_path / "ui.db")

    options = dict(
        api_key="fake",
        client_factory=lambda api_key: object(),
        investigator=investigator,
        critic=critic,
        connection_factory=connect,
    )
    outcome, reused = app.run_live(question, state, **options)
    assert not reused and outcome.stored_id is not None
    assert outcome.run.critique_status == "ok"
    assert app.live_view(outcome)["ledger"][0]["kind"] == "reported"
    conn = connect()
    assert conn.execute("SELECT count(*) FROM investigations").fetchone()[0] == 1
    conn.close()

    same, reused = app.run_live(question, state, **options)
    assert reused and same is outcome and len(calls) == 1
    app.reset_live(state)
    another, reused = app.run_live(question, state, **options)
    assert not reused and another is not outcome and len(calls) == 2


def test_failed_paid_attempt_is_not_repeated_without_explicit_reset():
    question = app.prepare_question("Does owner/name work?")
    state = {}
    calls = []

    def broken(request, client):
        calls.append(request)
        raise sandbox.SandboxError("daemon unavailable")

    options = dict(api_key="fake", client_factory=lambda api_key: object(), investigator=broken)
    first, reused = app.run_live(question, state, **options)
    assert not reused and "Docker" in first.error
    same, reused = app.run_live(question, state, **options)
    assert reused and same is first and len(calls) == 1


def test_model_error_is_clear_and_repeat_click_does_not_retry():
    question = app.prepare_question("Does owner/name work?")
    calls = []

    def broken(request, client):
        calls.append(request)
        raise anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        )

    state = {}
    options = dict(api_key="fake", client_factory=lambda api_key: object(), investigator=broken)
    outcome, reused = app.run_live(question, state, **options)
    assert not reused and "Model request failed" in outcome.error
    _, reused = app.run_live(question, state, **options)
    assert reused and len(calls) == 1


def test_extraction_failure_keeps_trace_and_is_saved(tmp_path):
    question = app.prepare_question("Does owner/name work?")
    outcome, reused = app.run_live(
        question,
        {},
        api_key="fake",
        client_factory=lambda api_key: object(),
        investigator=lambda request, client: fake_run(request, report=False),
        connection_factory=lambda: db.connect(tmp_path / "ui.db"),
    )
    assert not reused and outcome.stored_id is not None
    view = app.live_view(outcome)
    assert view["verdict"] == "no_structured_verdict"
    assert view["final_text"] == "The model's conclusion."
    page = FakePage()
    app.render_view(page, view)
    assert "The model's conclusion." in page.codes


def test_live_path_can_call_existing_engine_with_fake_model_and_sandbox(tmp_path):
    from tests.test_investigate import QUESTION, ScriptedClient, report, sandbox_tools

    box, made = sandbox_tools()
    client = ScriptedClient(["Finished reading."], report=report())
    outcome, reused = app.run_live(
        QUESTION,
        {},
        api_key="fake",
        client_factory=lambda api_key: client,
        investigator=lambda request, model: inv.investigate(
            request, model, sandbox_tools=box, allow_web_search=False
        ),
        critic=lambda run, model: (run, Usage(model=inv.INVESTIGATE_MODEL)),
        connection_factory=lambda: db.connect(tmp_path / "ui.db"),
    )
    assert not reused and outcome.error == ""
    assert outcome.stored_id is not None
    assert outcome.run.report.verdict == "inconclusive"
    assert client.calls == 1 and client.parsed == ["Investigation"]
    assert made == {}  # no Docker sandbox was created


def test_streamlit_page_browses_samples_and_rejects_empty_submission():
    from streamlit.testing.v1 import AppTest

    page = AppTest.from_file(app.__file__).run(timeout=30)
    assert not page.exception
    assert page.title[0].value == "Proofrun"
    assert [tab.label for tab in page.tabs] == ["Monitoring", "Ask it yourself"]
    assert any("Recorded discovery snapshot" in x.value for x in page.caption)
    assert any("2026-08-28" in x.value for x in page.caption)
    assert any("2026-09-07" in x.value for x in page.caption)
    assert [x.label for x in page.selectbox] == ["Example question"]
    page.selectbox[0].set_value("Is Bifrost really 50× faster than LiteLLM?").run(timeout=30)
    assert page.selectbox[0].value["verdict"] == "could_not_test"
    assert not page.exception
    page.button[1].click().run(timeout=30)
    assert any("Enter a question" in x.value for x in page.error)
    assert not page.exception
    page.button[0].click().run(timeout=30)
    assert "maximhq/bifrost" in page.text_area[0].value
    assert page.text_input[0].value == "https://github.com/maximhq/bifrost"
    visible = " ".join(x.value for kind in (page.text, page.caption, page.markdown) for x in kind)
    assert "relevance" not in visible.lower()
    assert "override" not in visible.lower()
    assert any("Estimated spend allowance" in x.value for x in page.caption)
    assert not page.exception


def test_monitoring_snapshot_is_portable_and_browsing_does_not_run_services(monkeypatch):
    from streamlit.testing.v1 import AppTest

    def unexpected(*args, **kwargs):
        raise AssertionError("browsing must not start a model or database")

    monkeypatch.setattr(db, "connect", unexpected)
    monkeypatch.setattr(app.anthropic, "Anthropic", unexpected)
    snapshot = app.load_monitoring()
    assert len(snapshot["briefs"]) == 1
    assert len(snapshot["items"]) == 6
    assert all(item["relevance_score"] >= 0.4 for item in snapshot["items"])
    page = AppTest.from_file(app.__file__).run(timeout=30)
    assert not page.exception
    assert len(page.get("link_button")) == 6
    assert any("Full report" in x.label for x in page.expander)


@pytest.mark.parametrize("value", ["https://github.com/microsoft/apm", "https://github.com/microsoft/apm/", "https://github.com/microsoft/apm.git", "microsoft/apm"])
def test_repository_url_and_question_are_separate(value):
    question = app.prepare_question("Can it generate instructions?", value)
    assert question.repo == "microsoft/apm"
    assert question.question == "Can it generate instructions?"


@pytest.mark.parametrize("value", ["https://evil.test/microsoft/apm", "https://github.com.evil.test/a/b", "https://github.com/a/b/tree/main", "https://github.com/a/b?token=x", "file:///a/b", "https://user@github.com/a/b", "../repo"])
def test_repository_url_rejects_non_repository_inputs(value):
    with pytest.raises(app.InputError):
        app.parse_repository(value)


def test_monitoring_reads_only_discovery_linked_results_without_writing(tmp_path):
    path = tmp_path / "monitor.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO items (id,source,source_id,title,url,fetched_at) VALUES (1,'github','owner/name','Discovered repo','https://github.com/owner/name','2026-09-22')")
    run = fake_run(app.prepare_question("Does owner/name work?"))
    run.report.limitations = ["Only a small example was checked."]
    inv.store_investigation(conn, run, item_id=1)
    inv.store_investigation(conn, fake_run(app.prepare_question("Does other/repo work?")))
    conn.close()
    before = path.read_bytes()
    views, error = app.load_monitoring_runs(path)
    assert not error
    assert len(views) == 1
    assert views[0]["repo"] == "owner/name"
    assert views[0]["status"] == "monitoring"
    assert views[0]["limitations"] == ["Only a small example was checked."]
    assert path.read_bytes() == before
    page = FakePage()
    app.render_view(page, views[0])
    assert any("Only a small example" in x for x in page.texts)


def test_monitoring_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    assert app.load_monitoring_runs(path) == ([], "")
    assert not path.exists()


def test_new_report_limits_are_compatible_with_old_records_and_reviewed():
    from ai_monitor.agent.critique import format_investigation
    old = inv.Investigation(question="Does it work?", verdict="inconclusive")
    assert old.limitations == []
    old.limitations = ["No speed comparison was run."]
    assert "No speed comparison was run." in format_investigation(old)


def test_downgraded_result_does_not_lead_with_original_positive_summary():
    view = app.load_examples()[0]
    view["downgraded"] = True
    page = FakePage()
    app.render_view(page, view)
    assert view["presentation"]["answer"] not in page.texts
    assert any("did not support a firm conclusion" in x for x in page.texts)
