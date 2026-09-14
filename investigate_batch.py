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
from typing import Optional

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
        default=runner.DEFAULT_MAX_COST_USD,
        help="per-question cost ceiling for the loop (default: %(default)s)",
    )
    parser.add_argument("--max-steps", type=int, default=inv.DEFAULT_MAX_STEPS)
    parser.add_argument("--max-seconds", type=float, default=inv.DEFAULT_MAX_SECONDS)
    parser.add_argument("--limit", type=int, help="run only the first N questions")
    parser.add_argument(
        "--start-at",
        type=int,
        default=1,
        help="1-based index of the first question to run. This is how a batch "
        "stopped by --budget is resumed, and resuming is only honest if "
        "nothing about the agent changed in between: a batch answered by two "
        "different prompts is not one measurement",
    )
    parser.add_argument(
        "--budget",
        type=float,
        help="hard ceiling on total spend for the whole batch, in dollars. The "
        "batch stops before starting any question that could carry it past "
        "this, so the figure is a guarantee rather than a hope. Whatever "
        "finished is still stored and still reported",
    )
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

    all_questions = load_questions(args.questions)
    questions = all_questions[args.start_at - 1 :]
    if args.limit:
        questions = questions[: args.limit]
    if not questions:
        raise SystemExit(
            f"--start-at {args.start_at} leaves nothing to run "
            f"({len(all_questions)} questions in {args.questions})"
        )
    offset = args.start_at - 1

    caps = inv.default_caps(
        max_steps=args.max_steps,
        max_cost_usd=args.max_cost,
        max_seconds=args.max_seconds,
    )
    searches = 0 if args.no_web_search else inv.tools.DEFAULT_MAX_WEB_SEARCHES
    ceiling = runner.worst_case_usd(len(questions), caps, max_web_searches=searches)

    print(
        f"\n{len(questions)} question(s) from {args.questions}"
        + (f", starting at #{args.start_at}" if args.start_at > 1 else "")
        + ":"
    )
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
    per_question = ceiling / len(questions)
    if args.budget:
        print(
            f"Budget: ${args.budget:.2f}. The batch stops before starting any "
            f"question that could carry it past that, so it will not begin a "
            f"question once ${args.budget - per_question:.2f} is already spent."
        )
    if not args.yes and not _confirm():
        print("Nothing spent.")
        return 0

    log.info("pruning anything a previous run left behind")
    sandbox.prune()

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    conn = None if args.no_store else db.connect()
    runs = []

    spent = 0.0
    halted: Optional[str] = None
    resume_at = 0

    for i, question in enumerate(questions, 1):
        # Checked before the question rather than after it, for the same
        # reason the loop's cost cap is: once a question has run, its cost is
        # already incurred and a ceiling noticed afterwards is not a ceiling.
        if args.budget and spent + per_question > args.budget:
            halted = (
                f"stopped after {i - 1} of {len(questions)} questions: "
                f"${spent:.2f} spent, and the next could reach "
                f"${spent + per_question:.2f} against a ${args.budget:.2f} budget"
            )
            resume_at = offset + i
            log.warning("%s", halted)
            break

        log.info("[%d/%d] %s ($%.2f spent so far)", i, len(questions), question.repo, spent)
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
        spent += run.usage.cost_usd
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

    total = sum(r.usage.cost_usd for r in runs)
    print(f"\n{len(runs)} run(s), ${total:.2f} spent against a ${ceiling:.2f} ceiling.")
    print(f"report: {args.out}")
    if halted:
        print(f"\n*** BUDGET STOP *** {halted}")
        print(
            f"Resume with:  --start-at {resume_at} --budget <remaining>\n"
            "Resume only if nothing about the agent has changed since. A batch "
            "answered half by one prompt and half by a better one is not one "
            "measurement - if this run shows something worth fixing, fix it and "
            "re-run all of them.\n"
        )
        return 2
    print()
    return 0


def _confirm() -> bool:
    try:
        return input("Proceed? [y/N] ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        print()
        return False


if __name__ == "__main__":
    sys.exit(main())
