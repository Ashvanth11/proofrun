"""Ask one question about one repository and get an answer backed by evidence.

    python investigate.py "Does owner/name actually resume a workflow after the
                           process is killed, as its README claims?"

This is reactive mode: same loop, same tools, same caps as the autonomous path
in `run.py --investigate`, with the question supplied instead of derived.
"""

import argparse
import logging
import re
import sys

import anthropic

from ai_monitor.agent import critique as critique_mod
from ai_monitor.agent import investigate as inv
from ai_monitor.agent import investigate_runner as runner
from ai_monitor.agent import sandbox
from ai_monitor.config.settings import settings
from ai_monitor.storage import db

log = logging.getLogger("ai_monitor.investigate")

# owner/name as it appears inside a sentence. Whatever it finds is handed to
# sandbox.validate_repo, which is the real gate - but the lookbehind matters
# on its own: without it "../../etc/passwd" yields the perfectly valid-looking
# "etc/passwd", and the run would go off and investigate the wrong thing.
# A path segment is not a repository name, so a match may not follow / . - or
# a word character.
REPO_IN_TEXT = re.compile(
    r"(?<![\w./-])([A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*)\b"
)


def find_repo(question: str) -> str:
    """Pull the repository out of the question, or fail with something useful."""
    for match in REPO_IN_TEXT.finditer(question):
        try:
            return sandbox.validate_repo(match.group(1))
        except sandbox.SandboxError:
            continue
    raise SystemExit(
        "Could not find a repository in the question. Name it as owner/name, "
        "or pass --repo owner/name."
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Investigate one question about a GitHub repository"
    )
    parser.add_argument("question", help="one plain-English sentence naming a repo")
    parser.add_argument(
        "--repo", help="owner/name, if the question does not name it plainly"
    )
    parser.add_argument(
        "--claim",
        default="",
        help="the specific claim being checked, if it is not the question itself",
    )
    parser.add_argument(
        "--max-cost",
        type=float,
        default=runner.DEFAULT_MAX_COST_USD,
        help="cost ceiling for the loop, in dollars (default: %(default)s). This "
        "bounds the loop, not the run: see investigate_runner.worst_case_usd",
    )
    parser.add_argument("--max-steps", type=int, default=inv.DEFAULT_MAX_STEPS)
    parser.add_argument(
        "--max-seconds", type=float, default=inv.DEFAULT_MAX_SECONDS
    )
    parser.add_argument(
        "--no-web-search", action="store_true", help="do not offer the web search tool"
    )
    parser.add_argument(
        "--no-critique", action="store_true", help="skip the grounding critique pass"
    )
    parser.add_argument(
        "--no-store", action="store_true", help="print the result without storing it"
    )
    parser.add_argument(
        "--yes", action="store_true", help="do not ask before spending"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if not settings.anthropic_api_key:
        log.error("ANTHROPIC_API_KEY is not set. Add it to .env.")
        return 1

    try:
        repo = sandbox.validate_repo(args.repo) if args.repo else find_repo(args.question)
    except sandbox.SandboxError as exc:
        log.error("%s", exc)
        return 1

    caps = inv.default_caps(
        max_steps=args.max_steps,
        max_cost_usd=args.max_cost,
        max_seconds=args.max_seconds,
    )
    ceiling = runner.worst_case_usd(
        1, caps, max_web_searches=0 if args.no_web_search else 5
    )

    print(f"\nRepository: {repo}")
    print(f"Question:   {args.question}")
    print(
        f"Worst case: ${ceiling:.2f} "
        f"(loop cap ${args.max_cost:.2f} plus the calls it does not cover), "
        f"up to {args.max_seconds / 60:.0f} min and 2 GB of disk."
    )
    if not args.yes and not _confirm():
        print("Nothing spent.")
        return 0

    question = inv.Question(
        repo=repo, claim=args.claim or args.question, question=args.question
    )
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    sandbox.prune()  # clear anything a crashed run left behind
    try:
        run = inv.investigate(
            question,
            client,
            caps=caps,
            allow_web_search=not args.no_web_search,
        )
    except sandbox.SandboxError as exc:
        log.error("sandbox: %s", exc)
        return 1

    if not args.no_critique and run.report is not None:
        run, critique_usage = critique_mod.critique_and_revise_investigation(run, client)
        run.usage.input_tokens += critique_usage.input_tokens
        run.usage.output_tokens += critique_usage.output_tokens

    print(runner.format_trajectory(run))

    if not args.no_store:
        conn = db.connect()
        row_id = inv.store_investigation(conn, run)
        conn.close()
        print(f"stored as investigations.id = {row_id}\n")

    return 0


def _confirm() -> bool:
    try:
        return input("Proceed? [y/N] ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        print()
        return False


if __name__ == "__main__":
    sys.exit(main())
