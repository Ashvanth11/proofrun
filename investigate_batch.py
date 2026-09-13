"""Run a fixed list of questions and write the results as a markdown report.

    python investigate_batch.py --questions questions.yaml --out reports/x.md

Two things this does that the single-question CLI does not, both because a
batch is unattended and a mistake is multiplied by its length:

- it prunes containers and volumes left behind by a crashed run before it
  starts, so a previous failure does not eat this run's disk;
- it states its worst case and stops for confirmation before spending
  anything, per the cost rule in CLAUDE.md.

The report's cells all come from repository-controlled text, so everything is
escaped and truncated on the way in.
"""

import argparse
import logging
import sys
from pathlib import Path

import anthropic
import yaml

from ai_monitor.agent import critique as critique_mod
from ai_monitor.agent import investigate as inv
from ai_monitor.agent import investigate_runner as runner
from ai_monitor.agent import sandbox
from ai_monitor.config.settings import settings
from ai_monitor.storage import db

log = logging.getLogger("ai_monitor.batch")


def load_questions(path: Path) -> list[inv.Question]:
    """Read questions.yaml.

    Extra keys (expected_verdict, needs_execution, ...) are the eval set's
    business in Session 4 and are ignored here - this script only runs the
    questions; scoring them against criteria is a separate pass.
    """
    data = yaml.safe_load(path.read_text()) or {}
    entries = data.get("questions") or []
    if not entries:
        raise SystemExit(f"{path} contains no questions")

    questions = []
    for i, entry in enumerate(entries, 1):
        try:
            repo = sandbox.validate_repo(entry["repo"])
        except (KeyError, TypeError, sandbox.SandboxError) as exc:
            raise SystemExit(f"{path} question {i}: bad repo - {exc}") from exc
        if not entry.get("question"):
            raise SystemExit(f"{path} question {i}: no question text")
        questions.append(
            inv.Question(
                repo=repo,
                claim=entry.get("claim", ""),
                question=entry["question"],
            )
        )
    return questions


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run a batch of investigations")
    parser.add_argument("--questions", type=Path, default=Path("questions.yaml"))
    parser.add_argument("--out", type=Path, required=True, help="markdown report path")
    parser.add_argument(
        "--max-cost",
        type=float,
        default=runner.SMOKE_MAX_COST_USD,
        help="per-question cost ceiling for the loop (default: %(default)s)",
    )
    parser.add_argument("--max-steps", type=int, default=inv.DEFAULT_MAX_STEPS)
    parser.add_argument("--max-seconds", type=float, default=inv.DEFAULT_MAX_SECONDS)
    parser.add_argument("--limit", type=int, help="run only the first N questions")
    parser.add_argument("--no-web-search", action="store_true")
    parser.add_argument("--no-critique", action="store_true")
    parser.add_argument("--no-store", action="store_true")
    parser.add_argument("--yes", action="store_true", help="do not ask before spending")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if not settings.anthropic_api_key:
        log.error("ANTHROPIC_API_KEY is not set. Add it to .env.")
        return 1

    questions = load_questions(args.questions)
    if args.limit:
        questions = questions[: args.limit]

    caps = inv.default_caps(
        max_steps=args.max_steps,
        max_cost_usd=args.max_cost,
        max_seconds=args.max_seconds,
    )
    searches = 0 if args.no_web_search else inv.tools.DEFAULT_MAX_WEB_SEARCHES
    ceiling = runner.worst_case_usd(len(questions), caps, max_web_searches=searches)

    print(f"\n{len(questions)} question(s) from {args.questions}:")
    for question in questions:
        print(f"  - {question.repo}: {runner.clean(question.question, 110)}")
    print(
        f"\nWorst case: ${ceiling:.2f} "
        f"({len(questions)} x ${ceiling / len(questions):.2f}), "
        f"up to {len(questions) * args.max_seconds / 60:.0f} min, "
        f"2 GB of disk per question."
    )
    print(
        "The per-question loop cap is "
        f"${args.max_cost:.2f}; the rest is the wrap-up, extraction and "
        "critique calls the cap does not cover."
    )
    if not args.yes and not _confirm():
        print("Nothing spent.")
        return 0

    log.info("pruning anything a previous run left behind")
    sandbox.prune()

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    conn = None if args.no_store else db.connect()
    runs = []

    for i, question in enumerate(questions, 1):
        log.info("[%d/%d] %s", i, len(questions), question.repo)
        try:
            run = inv.investigate(
                question,
                client,
                caps=caps,
                allow_web_search=not args.no_web_search,
            )
        except sandbox.SandboxError as exc:
            # One bad repository must not cost the rest of the batch.
            log.error("%s: sandbox refused - %s", question.repo, exc)
            continue

        if not args.no_critique and run.report is not None:
            run, critique_usage = critique_mod.critique_and_revise_investigation(
                run, client
            )
            run.usage.input_tokens += critique_usage.input_tokens
            run.usage.output_tokens += critique_usage.output_tokens

        runs.append(run)
        if conn is not None:
            inv.store_investigation(conn, run)

        log.info(
            "[%d/%d] %s: %s, $%.3f, %.0fs",
            i,
            len(questions),
            question.repo,
            run.report.verdict if run.report else "no conclusion",
            run.usage.cost_usd,
            run.wall_seconds,
        )

    if conn is not None:
        conn.close()

    if not runs:
        log.error("no questions completed")
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(runner.markdown_report(runs))

    spent = sum(r.usage.cost_usd for r in runs)
    print(f"\n{len(runs)} run(s), ${spent:.2f} spent against a ${ceiling:.2f} ceiling.")
    print(f"report: {args.out}\n")
    return 0


def _confirm() -> bool:
    try:
        return input("Proceed? [y/N] ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        print()
        return False


if __name__ == "__main__":
    sys.exit(main())
