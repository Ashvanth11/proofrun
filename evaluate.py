"""Run the eval harness: judge the golden set, then report agreement."""

import argparse
import logging
import sys
from pathlib import Path

import anthropic

from ai_monitor.config.settings import settings
from ai_monitor.eval import golden_set, investigations, judge, run_eval
from ai_monitor.storage import db

log = logging.getLogger("ai_monitor.eval")


def _score_investigations(conn, questions_path: Path) -> int:
    """Score stored investigation runs. No model is involved and nothing is
    spent: every criterion is an assert over what is already in the database."""
    try:
        results, path = investigations.run(conn, questions_path=questions_path)
    except (ValueError, FileNotFoundError) as exc:
        log.error("%s", exc)
        return 1

    stats = investigations.summarize(results)
    print(f"\nProofrun eval: {stats['passed']}/{stats['n']} questions pass")
    print(
        f"${stats['cost_usd']:.2f}, {stats['wall_seconds'] / 60:.0f} min, "
        f"{stats['observed']} observed / {stats['inspected']} inspected / "
        f"{stats['reported']} reported\n"
    )
    for result in results:
        mark = "PASS" if result.passed else "FAIL"
        failed = ", ".join(c.name for c in result.failures)
        print(
            f"  {mark}  {result.repo:<32} {str(result.verdict):<16}"
            + (f"({failed})" if failed else "")
        )
    if stats["failures_by_check"]:
        print("\nfailures by criterion:")
        for name, count in sorted(
            stats["failures_by_check"].items(), key=lambda kv: -kv[1]
        ):
            print(f"  {name}: {count}")
    if path:
        print(f"\nreport: {path}\n")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate analyzer calibration")
    parser.add_argument(
        "--skip-judge",
        action="store_true",
        help="report on existing scores only; make no API calls",
    )
    parser.add_argument(
        "--investigations",
        action="store_true",
        help="score the investigation agent against questions.yaml instead of "
        "the analyzer golden set. Reads stored runs only; makes no API calls",
    )
    parser.add_argument(
        "--questions",
        type=Path,
        default=investigations.QUESTIONS_PATH,
        help="eval set to score against (with --investigations)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    conn = db.connect()

    if args.investigations:
        return _score_investigations(conn, args.questions)

    stats = golden_set.stats(conn)

    if not stats["labeled"]:
        log.error("golden set is empty - run `python label.py` first")
        return 1

    log.info(
        "golden set: %d labeled, %d already judged", stats["labeled"], stats["judged"]
    )

    if not args.skip_judge:
        pending = golden_set.needs_judging(conn)
        if not pending:
            log.info("no unjudged items")
        elif not settings.anthropic_api_key:
            log.error(
                "ANTHROPIC_API_KEY is not set. Add it to .env, or use --skip-judge "
                "to report on existing scores without making API calls."
            )
            return 1
        else:
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            usage = judge.judge_golden_set(conn, client=client)
            log.info("judging cost $%.4f", usage.cost_usd)

    result, path = run_eval.run(conn)
    a = result["analyzer_vs_human"]

    print(f"\nAnalyzer vs your labels ({a.n} items)")
    print(f"  mean absolute error : {a.mae:.3f}")
    print(f"  Pearson r           : {a.pearson_r if a.pearson_r is None else f'{a.pearson_r:.3f}'}")
    print(f"  binary agreement    : {a.binary_agreement:.1%}")
    print(f"  mean bias           : {a.mean_bias:+.3f}")

    if result["judge_vs_human"]:
        j = result["judge_vs_human"]
        print(f"\nJudge vs your labels ({j.n} items)")
        print(f"  mean absolute error : {j.mae:.3f}")
        print(f"  binary agreement    : {j.binary_agreement:.1%}")

    print(f"\nFull report: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
