import argparse
import logging
import sys

import anthropic

from ai_monitor.analysis import analyzer
from ai_monitor.analysis.analyzer import Usage
from ai_monitor import providers
from ai_monitor.config.settings import settings
from ai_monitor.storage import db
from ai_monitor.storage.models import Item
from ai_monitor.synthesis import synthesizer
from ai_monitor.watchers import arxiv

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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AI developments monitor")
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
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    conn = db.connect()

    if not args.skip_fetch:
        ids = arxiv.fetch_and_store(conn, max_results=args.max_results)
        log.info("fetched %d items (%d total in db)", len(ids), db.count_items(conn))

    if args.dry_run:
        log.info("dry run: skipping analysis and synthesis")
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

    path, synth_usage = synthesizer.run(
        conn, client=client, min_score=args.min_score, model=synthesis_model
    )
    if path is None:
        log.warning("no brief written")
        return 0

    log.info(
        "brief: %s | synthesis cost $%.4f | run total $%.4f",
        path,
        synth_usage.cost_usd,
        total.cost_usd + synth_usage.cost_usd,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
