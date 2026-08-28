"""LangGraph orchestration of the pipeline.

The shape is genuinely fan-out/fan-in: three independent watchers run in
parallel, their results converge, every item is analyzed, and one synthesis
step runs over the whole set. That is what the graph buys - parallel source
fetching with per-source failure isolation, and a state object that makes the
run's accounting (counts, cost, errors) explicit rather than threaded through
function arguments.

The pipeline also exists as plain functions in run.py. This module is the same
pipeline expressed as a graph; keeping both is deliberate, since the plain
version stays the simplest way to debug a single stage.
"""

import logging
import operator
import sqlite3
from typing import Annotated, Any, Optional

from typing_extensions import TypedDict

import anthropic
from langgraph.graph import END, START, StateGraph

from ai_monitor.analysis import analyzer
from ai_monitor.providers import OllamaError
from ai_monitor.storage import db
from ai_monitor.storage.models import Item
from ai_monitor.synthesis import synthesizer
from ai_monitor.watchers import arxiv, github, hn

log = logging.getLogger(__name__)

FETCH_ERRORS = (Exception,)  # a dead source must not kill the run


class PipelineState(TypedDict, total=False):
    """State threaded through the graph.

    The Annotated reducers matter: the three watcher nodes write concurrently,
    and without a reducer LangGraph rejects parallel writes to one key.
    """

    fetched: Annotated[list[Item], operator.add]
    item_ids: list[int]
    source_errors: Annotated[list[str], operator.add]
    analyzed: int
    skipped: int
    failed: int
    input_tokens: int
    output_tokens: int
    brief_path: Optional[str]
    synthesis_tokens: tuple


def _row_to_item(row) -> Item:
    return Item(
        source=row["source"],
        source_id=row["source_id"],
        title=row["title"],
        url=row["url"],
        content=row["content"],
    )


def build_graph(
    conn: sqlite3.Connection,
    client: Any,
    analysis_model: str,
    synthesis_model: str,
    sources: Optional[list[str]] = None,
    max_results: int = 25,
    min_score: float = 0.4,
):
    """Compile the pipeline graph with its dependencies bound.

    Dependencies are closed over rather than carried in state: a database
    connection and an HTTP client are not serializable, and state should hold
    the run's data, not its wiring.
    """
    sources = sources or ["arxiv", "github", "hn"]

    def _watcher(name: str, fetch):
        """Wrap a watcher so one failing source degrades instead of aborting.

        Watcher nodes fetch only - they never touch the database. LangGraph
        runs these branches in a thread pool, and a SQLite connection cannot
        be used from a thread other than the one that created it. Parallelism
        buys us the concurrent network I/O; the writes happen on one thread in
        the store node, where they are fast and safe.
        """

        def node(state: PipelineState) -> dict:
            try:
                items = fetch(max_results=max_results)
                return {"fetched": items}
            except FETCH_ERRORS as exc:
                log.warning("%s watcher failed: %s", name, exc)
                return {"fetched": [], "source_errors": [f"{name}: {exc}"]}

        return node

    def store_node(state: PipelineState) -> dict:
        """Single-threaded fan-in point where every fetched item is persisted."""
        ids = [db.upsert_item(conn, item) for item in state.get("fetched", [])]
        log.info("stored %d items (%d total)", len(ids), db.count_items(conn))
        return {"item_ids": ids}

    def analyze_node(state: PipelineState) -> dict:
        analyzed = skipped = failed = 0
        input_tokens = output_tokens = 0

        for row in db.get_items(conn):
            try:
                usage = analyzer.analyze_and_store(
                    conn,
                    row["id"],
                    _row_to_item(row),
                    client=client,
                    model=analysis_model,
                )
            except (anthropic.APIError, OllamaError):
                log.exception("analysis failed for %s", row["source_id"])
                failed += 1
                continue

            if usage is None:
                skipped += 1
            else:
                analyzed += 1
                input_tokens += usage.input_tokens
                output_tokens += usage.output_tokens

        return {
            "analyzed": analyzed,
            "skipped": skipped,
            "failed": failed,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }

    def synthesize_node(state: PipelineState) -> dict:
        path, usage = synthesizer.run(
            conn, client=client, min_score=min_score, model=synthesis_model
        )
        return {
            "brief_path": str(path) if path else None,
            "synthesis_tokens": (
                (usage.input_tokens, usage.output_tokens) if usage else (0, 0)
            ),
        }

    graph = StateGraph(PipelineState)

    watchers = {"arxiv": arxiv.fetch, "github": github.fetch, "hn": hn.fetch}
    active = [s for s in sources if s in watchers]
    for name in active:
        graph.add_node(name, _watcher(name, watchers[name]))
        # Fan-out: every watcher runs in parallel from the entry point.
        graph.add_edge(START, name)

    graph.add_node("store", store_node)
    graph.add_node("analyze", analyze_node)
    graph.add_node("synthesize", synthesize_node)

    # Fan-in: storage waits for every watcher, then runs on a single thread.
    for name in active:
        graph.add_edge(name, "store")

    graph.add_edge("store", "analyze")
    graph.add_edge("analyze", "synthesize")
    graph.add_edge("synthesize", END)

    return graph.compile()


def initial_state() -> PipelineState:
    return {
        "fetched": [],
        "item_ids": [],
        "source_errors": [],
        "analyzed": 0,
        "skipped": 0,
        "failed": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "brief_path": None,
        "synthesis_tokens": (0, 0),
    }


def export_diagram(compiled, path: str = "docs/pipeline.png") -> Optional[str]:
    """Write the graph visualization for the README. Best-effort."""
    from pathlib import Path

    try:
        png = compiled.get_graph().draw_mermaid_png()
    except Exception as exc:  # needs network or graphviz depending on backend
        log.warning("could not render graph png: %s", exc)
        return None

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png)
    return str(out)


def export_mermaid(compiled, path: str = "docs/pipeline.mmd") -> str:
    """Write the graph as mermaid source - no network, always works."""
    from pathlib import Path

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    text = compiled.get_graph().draw_mermaid()
    out.write_text(text)
    return str(out)
