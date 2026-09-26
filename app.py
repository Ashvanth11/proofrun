"""One-page local UI for recorded examples and direct investigations.

Run with ``streamlit run app.py``. Importing this module does not import
Streamlit or start a client, so the execution path can be tested offline.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, MutableMapping
from urllib.parse import urlsplit

import anthropic

from ai_monitor.agent import critique as critique_mod
from ai_monitor.agent import investigate as inv
from ai_monitor.agent import investigate_runner as runner
from ai_monitor.agent import sandbox
from ai_monitor.config.settings import settings
from ai_monitor.providers import OllamaError
from ai_monitor.storage import db
from ai_monitor.monitoring_feed import load_local_runs as load_monitoring_runs
from ai_monitor.monitoring_feed import fetch_public_feed, merge_runs, load_weekly_discoveries
from investigate import find_repo

log = logging.getLogger(__name__)
EXAMPLES_PATH = Path(__file__).resolve().parent / "samples" / "investigations.json"
MONITORING_PATH = EXAMPLES_PATH.with_name("monitoring.json")


class InputError(ValueError):
    """Input or configuration that should be explained before any paid call."""


@dataclass
class LiveOutcome:
    request: inv.Question
    run: inv.InvestigationRun | None = None
    stored_id: int | None = None
    storage_error: str = ""
    error: str = ""
    elapsed_seconds: float = 0.0


def load_examples(path: Path = EXAMPLES_PATH) -> list[dict[str, Any]]:
    """Load curated snapshots. No database, API client, or Docker is involved."""
    examples = json.loads(path.read_text(encoding="utf-8"))
    if len(examples) != 3 or {x["category"] for x in examples} != {
        "executed", "read_only", "could_not_test"
    }:
        raise ValueError("recorded examples must cover all three release cases")
    return examples


def detect_repo(question: str) -> str | None:
    try:
        return find_repo(question)
    except SystemExit:
        return None


def parse_repository(value: str) -> str:
    """Accept a GitHub repository URL or owner/name, without fetching it."""
    value = value.strip()
    if "://" in value:
        try:
            url = urlsplit(value)
        except ValueError as exc:
            raise InputError("Paste a valid GitHub repository URL.") from exc
        if url.scheme != "https" or url.netloc.lower() != "github.com" or url.query or url.fragment:
            raise InputError("Paste a GitHub repository URL, such as https://github.com/microsoft/apm.")
        value = url.path.strip("/")
    if value.endswith(".git"):
        value = value[:-4]
    try:
        return sandbox.validate_repo(value)
    except sandbox.SandboxError as exc:
        raise InputError("Use a repository URL like https://github.com/microsoft/apm, or microsoft/apm.") from exc


def prepare_question(text: str, repository: str = "") -> inv.Question:
    question = text.strip()
    if not question:
        raise InputError("Enter a question before investigating.")
    if len(question) > 2000:
        raise InputError("Keep the question under 2,000 characters.")
    repo = parse_repository(repository) if repository.strip() else detect_repo(question)
    if repo is None:
        raise InputError("Paste the GitHub repository URL above your question.")
    return inv.Question(repo=repo, claim=question, question=question)


def fingerprint(question: inv.Question) -> str:
    value = f"{question.repo.casefold()}\n{question.question}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def reset_live(state: MutableMapping[str, Any]) -> None:
    """Only an explicit reset permits a second run of identical input."""
    state.pop("live_fingerprint", None)
    state.pop("live_outcome", None)


def run_live(
    question: inv.Question,
    state: MutableMapping[str, Any],
    *,
    api_key: str | None = None,
    client_factory=anthropic.Anthropic,
    investigator=inv.investigate,
    critic=critique_mod.critique_and_revise_investigation,
    connection_factory=db.connect,
    store=inv.store_investigation,
) -> tuple[LiveOutcome, bool]:
    """Run once per session/input, including after a partial paid failure.

    ``True`` in the return value means an earlier outcome was reused. The
    fingerprint is recorded before client creation to guard repeated clicks.
    """
    run_id = fingerprint(question)
    if state.get("live_fingerprint") == run_id:
        previous = state.get("live_outcome")
        if previous is not None:
            return previous, True
        return LiveOutcome(question, error="An investigation is already running for this question."), True

    key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY") or settings.anthropic_api_key
    if not key:
        raise InputError("ANTHROPIC_API_KEY is missing. Add it to .env before a live run.")

    state["live_fingerprint"] = run_id
    started = time.monotonic()
    outcome = LiveOutcome(question)
    try:
        client = client_factory(api_key=key)
        outcome.run = investigator(question, client)
        if outcome.run.report is not None:
            outcome.run, critique_usage = critic(outcome.run, client)
            outcome.run.usage.input_tokens += critique_usage.input_tokens
            outcome.run.usage.output_tokens += critique_usage.output_tokens

        try:
            conn = connection_factory()
            try:
                outcome.stored_id = store(conn, outcome.run)
            finally:
                conn.close()
        except Exception:
            log.exception("completed investigation could not be stored")
            outcome.storage_error = "The result is visible here, but could not be saved to SQLite."
    except (sandbox.SandboxError, OSError):
        log.exception("sandbox or Docker failure")
        outcome.error = "Sandbox or Docker failed. Check that Docker is running and the sandbox image is built."
    except (anthropic.APIError, OllamaError):
        log.exception("model request failed")
        outcome.error = "Model request failed. Check the API key, account access, and connection, then explicitly allow another run."
    except Exception:
        log.exception("investigation failed")
        outcome.error = "Investigation failed. Check the local terminal log before explicitly allowing another run."
    finally:
        outcome.elapsed_seconds = time.monotonic() - started
        state["live_outcome"] = outcome
    return outcome, False


def live_view(outcome: LiveOutcome) -> dict[str, Any]:
    """Normalize a live engine result to the same fields as saved examples."""
    run = outcome.run
    if run is None:
        raise ValueError("no run to render")
    report = run.report
    critique = run.critique
    return {
        "title": "Live investigation",
        "status": "live",
        "recorded_at": "",
        "repo": run.question.repo,
        "repository_description": report.repository_description if report else "",
        "question": run.question.question,
        "verdict": report.verdict if report else "no_structured_verdict",
        "summary": report.summary if report else "The investigation finished without a structured verdict.",
        "blockers": report.blockers if report else [],
        "ledger": [item.model_dump() for item in report.ledger] if report else [],
        "tool_calls": [call.model_dump() for call in run.tool_calls],
        "observed_output": report.facts.observed_output if report else "",
        "final_text": run.final_text if report is None else "",
        "critique_status": run.critique_status,
        "critique_grounded": getattr(critique, "grounded", None),
        "critique_issues": getattr(critique, "issues", []),
        "estimated_cost_usd": run.usage.cost_usd,
        "elapsed_seconds": outcome.elapsed_seconds,
        "steps_taken": run.steps_taken,
        "stop_reason": run.stop_reason,
        "downgraded": run.downgraded,
        "revised": run.revised,
        "limitations": report.limitations if report else [],
    }


VERDICT_LABELS = {
    "supported": "Supported by this investigation",
    "refuted": "Evidence contradicts the claim",
    "inconclusive": "The evidence does not settle it",
    "could_not_test": "Could not verify the claim",
    "no_structured_verdict": "No conclusion available",
}


def render_technical_details(st: Any, view: dict[str, Any]) -> None:
    """Preserve the original report and trace behind a deliberate disclosure."""
    with st.expander("Full report, sources, and execution details"):
        st.text("Original question: " + view["question"])
        st.text("Original verdict: " + view["verdict"])
        st.text(view["summary"])
        st.text("Blockers: " + (", ".join(view["blockers"]) or "none"))
        st.text(f"{view['steps_taken']} steps · stop: {view['stop_reason']} · estimated token cost: ${view['estimated_cost_usd']:.4f} · elapsed: {view['elapsed_seconds']:.1f}s")
        st.markdown("#### Evidence and sources")
        for entry in view["ledger"]:
            st.text(f"{entry['kind']} · {entry['side']} · {entry['source']}")
            st.text(entry["statement"])
        if view.get("observed_output"):
            st.code(view["observed_output"], language=None)
        for call in view["tool_calls"]:
            with st.expander(f"Step {call['step']} · {call['tool']}"):
                st.code(json.dumps(call["arguments"], ensure_ascii=False, indent=2), language="json")
                st.code(call["result_summary"], language=None)
        st.markdown("#### Evidence review")
        st.text(f"Status: {view['critique_status']} · grounded: {view['critique_grounded']} · revised: {view['revised']}")
        for issue in view["critique_issues"]:
            st.text(issue)
        if view.get("final_text"):
            st.code(view["final_text"], language=None)
        st.caption("Source checks match evidence to tools used. They do not prove every statement is correct.")


def render_view(st: Any, view: dict[str, Any]) -> None:
    presentation = view.get("presentation", {})
    st.subheader(view["repo"])
    if view.get("repository_description"):
        st.text(view["repository_description"])
    st.caption(f"Recorded example · {view['recorded_at']}" if view["status"] == "recorded" else f"Investigation result · {view.get('recorded_at') or 'this session'}")
    st.text(presentation.get("title") or view["question"])
    st.markdown("#### What we found")
    if view.get("downgraded"):
        st.text("The evidence checks did not support a firm conclusion. Read the full report for the original assessment.")
    else:
        st.text(presentation.get("answer") or view["summary"])
    st.caption(VERDICT_LABELS.get(view["verdict"], "No conclusion available"))
    checks, unknowns = st.columns(2, gap="large")
    with checks:
        st.markdown("#### What we checked")
        if presentation.get("findings"):
            for finding in presentation["findings"]:
                st.text("• " + finding)
        else:
            labels = {"observed": "Test result", "inspected": "Repository check", "reported": "Source claim"}
            for entry in view["ledger"][:4]:
                st.caption(labels.get(entry["kind"], "Finding"))
                st.text(entry["statement"])
            if not view["ledger"]:
                st.text("No usable evidence was recorded.")
    with unknowns:
        st.markdown("#### What remains unknown")
        limits = presentation.get("limits") or view.get("limitations") or []
        if isinstance(limits, str):
            limits = [limits]
        for limitation in limits:
            st.text("• " + limitation)
        if not limits:
            if view["blockers"]:
                st.text("The investigation was blocked: " + ", ".join(x.replace("_", " ") for x in view["blockers"]) + ".")
            else:
                st.text("This result addresses the question above; other capabilities were not established by this investigation.")
    if view.get("critique_issues"):
        st.caption("The evidence review flagged qualifications. They are preserved in the full report below.")
    if view.get("critique_status") == "failed":
        st.warning("The automated evidence review did not complete. Treat this conclusion as provisional.")
    render_technical_details(st, view)


def load_monitoring(path: Path = MONITORING_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_past_questions(path: Path | None = None) -> tuple[list[dict[str, Any]], str]:
    """List saved direct questions without creating or modifying the database."""
    path = path or db.DEFAULT_DB_PATH
    if not path.exists():
        return [], ""
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, repo, question, verdict, created_at FROM investigations "
                "WHERE item_id IS NULL ORDER BY created_at DESC, id DESC LIMIT 100"
            ).fetchall()
        return [dict(row) for row in rows], ""
    except sqlite3.Error:
        log.exception("could not list past questions")
        return [], "Saved questions could not be loaded."


def load_past_run(run_id: int, path: Path | None = None) -> tuple[dict[str, Any] | None, str]:
    """Read one saved direct investigation for the past-questions viewer."""
    path = path or db.DEFAULT_DB_PATH
    if not path.exists():
        return None, "Saved result is no longer available."
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM investigations WHERE id = ? AND item_id IS NULL", (run_id,)
            ).fetchone()
        if row is None:
            return None, "Saved result is no longer available."
        report = inv.Investigation.model_validate_json(row["report"]) if row["report"] else None
        critique = json.loads(row["critique"] or "{}")
        return {
            "repo": row["repo"], "question": row["question"],
            "status": "saved", "recorded_at": (row["created_at"] or "")[:10],
            "repository_description": report.repository_description if report else "",
            "verdict": report.verdict if report else "no_structured_verdict",
            "summary": report.summary if report else "The investigation finished without a structured verdict.",
            "blockers": report.blockers if report else json.loads(row["blockers"] or "[]"),
            "ledger": [entry.model_dump() for entry in report.ledger] if report else [],
            "limitations": report.limitations if report else [],
            "tool_calls": json.loads(row["tool_calls"] or "[]"),
            "observed_output": report.facts.observed_output if report else "",
            "final_text": row["final_text"] or "",
            "critique_status": row["critique_status"],
            "critique_grounded": critique.get("grounded"),
            "critique_issues": critique.get("issues", []),
            "estimated_cost_usd": row["cost_usd"] or 0,
            "elapsed_seconds": row["wall_seconds"] or 0,
            "steps_taken": row["steps_taken"] or 0,
            "stop_reason": row["stop_reason"],
            "downgraded": bool(row["downgraded"]), "revised": bool(row["revised"]),
        }, ""
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        log.exception("could not load past investigation")
        return None, "Saved result could not be read."


def past_question_label(row: dict[str, Any]) -> str:
    """Distinguish repeated runs of the same question in the selector."""
    date = (row["created_at"] or "")[:16].replace("T", " ")
    return f"#{row['id']} · {date} UTC · {row['repo']} · {row['question']} ({row['verdict'] or 'no verdict'})"


def render_past_questions(st: Any) -> bool:
    questions, error = load_past_questions()
    selected_view = None
    with st.expander("View past questions"):
        if error:
            st.warning(error)
        elif not questions:
            st.caption("No direct investigations have been saved locally yet.")
        else:
            selected = st.selectbox(
                "Saved question",
                questions,
                index=None,
                placeholder="Choose a past question",
                format_func=past_question_label,
            )
            st.caption("Showing up to 100 recent direct runs. Browsing a saved result does not start a new investigation.")
            if selected is not None:
                selected_view, view_error = load_past_run(selected["id"])
                if view_error:
                    st.warning(view_error)
    if selected_view is not None:
        st.markdown("### Saved investigation")
        render_view(st, selected_view)
    return selected_view is not None




def render_monitoring(st: Any) -> None:
    st.caption("Discover a repository → choose a question → inspect or test → report the findings")
    views, error = load_monitoring_runs()
    public_feed, feed_error = st.cache_data(ttl=300)(fetch_public_feed)()
    if public_feed is not None:
        views = merge_runs(views, public_feed["investigations"])
        last_run = public_feed.get("last_run")
        if last_run:
            st.caption(f"Weekly monitoring · last completed {last_run['finished_at']} · {last_run['investigated']} new investigation(s)")
        else:
            st.caption("Weekly monitoring is being prepared. No scheduled run has completed yet.")
    elif feed_error:
        st.caption("Published weekly results are unavailable right now. Showing saved local results and examples.")
    if error:
        st.warning(error)
    if views:
        st.markdown("### Latest monitoring investigations")
        st.caption("Results from automatic monitoring. While this app is in use, it checks the published feed at most once every five minutes. Weekly investigations run on their separate schedule; each result keeps its original date.")
    else:
        st.markdown("### Explore investigation examples")
        st.caption("No automatic monitoring investigations have been saved here yet. These real recorded runs began with submitted questions and illustrate the findings the shared investigation engine produces.")
        views = load_examples()
    for view in views:
        with st.container(border=True):
            render_view(st, view)
    discovery_batch = public_feed if public_feed and public_feed.get("discovery_week") else load_weekly_discoveries()
    st.markdown("### Also discovered")
    if discovery_batch.get("discovery_week"):
        st.caption(f"Found during {discovery_batch['discovery_week']}. Descriptions introduce the projects; their claims have not been checked.")
        investigated = {view["repo"].casefold() for view in views if view["status"] == "monitoring"}
        discoveries = [item for item in discovery_batch.get("discoveries", []) if item["repo"].casefold() not in investigated]
        for item in discoveries:
            with st.container(border=True):
                st.text(item["repo"])
                st.caption("Not investigated")
                st.text(item["repository_description"] or "No project description was supplied.")
                st.link_button("View repository ↗", "https://github.com/" + item["repo"])
        if not discoveries:
            st.caption("No additional uninvestigated repositories in this week's saved batch.")
    else:
        st.caption("The next completed weekly run will list its other discovered repositories here.")
    snapshot = load_monitoring()
    with st.expander("Earlier discoveries and monitoring briefs"):
        st.caption(f"Recorded discovery snapshot · exported {snapshot['recorded_at']}. These items have not been linked to an investigation in this snapshot.")
        for brief in snapshot["briefs"]:
            st.caption("Brief saved " + brief["created_at"][:10])
            st.text(f"A digest of {brief['item_count']} stored papers and projects.")
            st.caption("Selected across stored dates, without investigation conclusions.")
            for line in brief["markdown"].splitlines():
                if line.startswith("### "):
                    st.text("• " + line[4:])
        for item in snapshot["items"]:
            st.text(item["title"])
            st.caption("Discovered item · analyzed " + item["analyzed_at"][:10])
            st.text(item["summary"])
            if item["url"].startswith(("https://", "http://")):
                st.link_button("Read source ↗", item["url"])


def render_ask(st: Any) -> None:
    st.subheader("Put a repository's claim to the test")
    st.write("Paste a GitHub repository, then ask what you want to know. Proofrun reads the project and runs a focused test when needed.")
    examples = load_examples()
    with st.expander("Start with an example"):
        selected = st.selectbox("Example question", examples, format_func=lambda item: item["presentation"]["title"])
        if st.button("Use this example"):
            st.session_state["question_input"] = selected["question"]
            st.session_state["repository_url"] = "https://github.com/" + selected["repo"]
    repository = st.text_input("GitHub repository URL", key="repository_url", placeholder="https://github.com/microsoft/apm", help="The project you want to investigate. You can also enter owner/name.")
    question_text = st.text_area("What would you like to find out?", key="question_input", placeholder="Can it generate agent instructions from a single configuration file?")
    request = None
    if question_text.strip() and repository.strip():
        try:
            request = prepare_question(question_text, repository)
            st.caption(f"Spend allowance: ${runner.worst_case_usd(1):.2f}. Most runs cost less; this is an estimate, not a hard cap.")
        except InputError as exc:
            st.error(str(exc))
    st.caption("A new investigation uses your Anthropic API key and may run code in your local Docker sandbox.")
    submitted = st.button("Investigate", type="primary")
    if submitted:
        if not repository.strip():
            st.error("Paste the GitHub repository URL to choose a project.")
        if not question_text.strip():
            st.error("Enter a question before investigating.")
    if submitted and request is not None:
        try:
            with st.spinner("Reading the project, checking the claim, and reviewing the evidence…"):
                outcome, reused = run_live(request, st.session_state)
            if reused:
                st.info("Showing your saved result for this question. No new investigation was started.")
        except InputError as exc:
            st.error(str(exc))
    outcome = st.session_state.get("live_outcome")
    st.divider()
    if outcome is not None:
        if outcome.error:
            st.error(outcome.error)
        if outcome.storage_error:
            st.warning(outcome.storage_error)
        if outcome.run is not None:
            render_view(st, live_view(outcome))
        if st.button("Allow another run of this question"):
            reset_live(st.session_state)
            st.rerun()
    else:
        st.caption("EXAMPLE RESULT · Choose another example above to explore a different investigation.")
        render_view(st, selected)
    st.divider()
    render_past_questions(st)


def main() -> None:
    import streamlit as st

    st.set_page_config(page_title="Proofrun", page_icon="◈", layout="wide")
    st.caption("DISCOVER. INVESTIGATE. UNDERSTAND.")
    st.title("Proofrun")
    st.markdown("### AI projects make promises. See what holds up.")
    st.write("Proofrun discovers AI repositories and investigates their claims by reading the code, checking sources, and running focused tests when possible. You get a clear answer, the evidence behind it, and the limits of what was checked.")
    left, right = st.columns(2)
    with left:
        with st.container(border=True):
            st.markdown("**Monitoring**")
            st.write("Follow projects discovered by the monitor and see what their investigations found.")
    with right:
        with st.container(border=True):
            st.markdown("**Ask it yourself**")
            st.write("Bring a GitHub repository and a question. Get an investigation focused on what matters to you.")
    monitoring, ask = st.tabs(["Monitoring", "Ask it yourself"])
    with monitoring:
        render_monitoring(st)
    with ask:
        render_ask(st)


if __name__ == "__main__":
    main()
