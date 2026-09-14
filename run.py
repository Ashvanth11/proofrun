import argparse
import logging
import sys

import anthropic

from ai_monitor.analysis import analyzer
from ai_monitor.analysis.analyzer import Usage
from ai_monitor import providers
from ai_monitor.agent import investigate
from ai_monitor.agent import investigate_runner
from ai_monitor.agent import runner as agent_runner
from ai_monitor.config.settings import settings
from ai_monitor.orchestrator import graph
from ai_monitor.storage import db
from ai_monitor.storage.models import Item
from ai_monitor.synthesis import synthesizer
from ai_monitor.watchers import arxiv, github, hn

log = logging.getLogger("ai_monitor")


def _row_to_item(row) -> Item:
    """Rebuild the Item the analyzer needs from a stored row."""
    return Item(
        source=row["source"],
        source_id=row["source_id"],
        title=row["title"],
        url=row["url"],
        content=row["content"],
    )


def _run_graph(conn, client, args, analysis_model, synthesis_model) -> int:
    """Run the pipeline through LangGraph, with watchers fanning out in parallel."""
    compiled = graph.build_graph(
        conn,
        client,
        analysis_model=analysis_model,
        synthesis_model=synthesis_model,
        sources=args.sources,
        max_results=args.max_results,
        min_score=args.min_score,
    )
    final = compiled.invoke(graph.initial_state())

    for err in final.get("source_errors", []):
        log.warning("source degraded - %s", err)

    analysis = Usage(
        input_tokens=final["input_tokens"],
        output_tokens=final["output_tokens"],
        model=analysis_model,
    )
    synth_in, synth_out = final.get("synthesis_tokens", (0, 0))
    synthesis = Usage(
        input_tokens=synth_in, output_tokens=synth_out, model=synthesis_model
    )

    log.info(
        "stored %d | analyzed %d, skipped %d, failed %d | cost $%.4f",
        len(final.get("item_ids", [])),
        final["analyzed"],
        final["skipped"],
        final["failed"],
        analysis.cost_usd + synthesis.cost_usd,
    )
    if final.get("brief_path"):
        log.info("brief: %s", final["brief_path"])
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Proofrun: the AI developments monitor and its agents"
    )
    parser.add_argument(
        "--provider",
        choices=["anthropic", "ollama"],
        default="anthropic",
        help="model backend; ollama runs locally for free (slower, less calibrated)",
    )
    parser.add_argument(
        "--ollama-model",
        default=providers.DEFAULT_OLLAMA_MODEL,
        help="model name when --provider ollama",
    )
    parser.add_argument(
        "--graph",
        action="store_true",
        help="run via the LangGraph pipeline (parallel watchers) instead of "
        "the sequential function pipeline",
    )
    parser.add_argument(
        "--sources",
        nargs="+",
        choices=["arxiv", "github", "hn"],
        default=["arxiv"],
        help="which watchers to run",
    )
    parser.add_argument("--max-results", type=int, default=25)
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.4,
        help="minimum relevance score for an item to reach the brief",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and store only; make no API calls",
    )
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument(
        "--agent",
        action="store_true",
        help="run the repo-analysis agent on GitHub items scoring above "
        "--agent-threshold (costs several model calls and GitHub requests each)",
    )
    parser.add_argument(
        "--agent-threshold",
        type=float,
        default=agent_runner.DEFAULT_AGENT_THRESHOLD,
        help="minimum analyzer score for a repo to be worth investigating",
    )
    parser.add_argument(
        "--agent-limit",
        type=int,
        default=5,
        help="maximum repositories to investigate per run",
    )
    parser.add_argument(
        "--investigate",
        action="store_true",
        help="run the investigation agent on GitHub items scoring above "
        "--investigate-threshold: derives a question from each README and "
        "answers it, running the code in a sandbox when it has to. Much more "
        "expensive than --agent - model turns, a container, and a download each",
    )
    parser.add_argument(
        "--investigate-threshold",
        type=float,
        default=investigate_runner.DEFAULT_THRESHOLD,
        help="minimum analyzer score for a repo to be worth investigating",
    )
    parser.add_argument(
        "--investigate-limit",
        type=int,
        default=investigate_runner.DEFAULT_LIMIT,
        help="maximum repositories to investigate per run",
    )
    parser.add_argument(
        "--investigate-max-cost",
        type=float,
        default=investigate_runner.DEFAULT_MAX_COST_USD,
        help="per-question cost ceiling for the investigation loop "
        "(default: %(default)s). Bounds the loop, not the whole run",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    conn = db.connect()

    if args.dry_run:
        for source in args.sources:
            watcher = {"arxiv": arxiv, "github": github, "hn": hn}[source]
            try:
                ids = watcher.fetch_and_store(conn, max_results=args.max_results)
                log.info("%s: stored %d items", source, len(ids))
            except Exception as exc:
                log.warning("%s watcher failed: %s", source, exc)
        log.info("dry run: %d items in db, skipping analysis", db.count_items(conn))
        return 0

    try:
        client, model_override = providers.build_client(
            args.provider,
            api_key=settings.anthropic_api_key,
            ollama_model=args.ollama_model,
        )
    except (ValueError, providers.OllamaError) as exc:
        log.error("%s", exc)
        return 1

    analysis_model = model_override or analyzer.ANALYZER_MODEL
    synthesis_model = model_override or synthesizer.SYNTHESIS_MODEL

    if args.graph:
        return _run_graph(conn, client, args, analysis_model, synthesis_model)

    if not args.skip_fetch:
        for source in args.sources:
            watcher = {"arxiv": arxiv, "github": github, "hn": hn}[source]
            try:
                ids = watcher.fetch_and_store(conn, max_results=args.max_results)
                log.info("%s: stored %d items", source, len(ids))
            except Exception as exc:
                log.warning("%s watcher failed: %s", source, exc)
        log.info("%d items in db", db.count_items(conn))

    total = Usage(model=analysis_model)
    analyzed = skipped = failed = 0

    for row in db.get_items(conn):
        try:
            usage = analyzer.analyze_and_store(
                conn,
                row["id"],
                _row_to_item(row),
                client=client,
                model=analysis_model,
            )
        except (anthropic.APIError, providers.OllamaError):
            log.exception("analysis failed for %s", row["source_id"])
            failed += 1
            continue

        if usage is None:
            skipped += 1
        else:
            analyzed += 1
            total.input_tokens += usage.input_tokens
            total.output_tokens += usage.output_tokens

    log.info(
        "analyzed %d, skipped %d (unchanged), failed %d | analysis cost $%.4f",
        analyzed,
        skipped,
        failed,
        total.cost_usd,
    )

    agent_usage = Usage(model=analysis_model)
    if args.agent:
        _, agent_usage = agent_runner.run_agent_on_candidates(
            conn,
            client,
            model=analysis_model,
            threshold=args.agent_threshold,
            limit=args.agent_limit,
        )

    investigate_usage = Usage(model=analysis_model)
    if args.investigate:
        if args.provider != "anthropic":
            log.warning(
                "--investigate needs the sandbox and server-side web search; "
                "running it against %s is not supported", args.provider
            )
        else:
            _, investigate_usage = investigate_runner.run_investigations_on_candidates(
                conn,
                client,
                model=analysis_model,
                threshold=args.investigate_threshold,
                limit=args.investigate_limit,
                caps=investigate.default_caps(
                    max_cost_usd=args.investigate_max_cost
                ),
            )

    path, synth_usage = synthesizer.run(
        conn, client=client, min_score=args.min_score, model=synthesis_model
    )
    if path is None:
        log.warning("no brief written")
        return 0

    log.info(
        "brief: %s | synthesis $%.4f | investigation $%.4f | run total $%.4f",
        path,
        synth_usage.cost_usd,
        investigate_usage.cost_usd,
        total.cost_usd
        + agent_usage.cost_usd
        + investigate_usage.cost_usd
        + synth_usage.cost_usd,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
