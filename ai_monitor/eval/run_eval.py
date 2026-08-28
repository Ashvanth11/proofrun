"""Evaluate the analyzer against a hand-labeled golden set.

Two comparisons matter, and they answer different questions:

1. analyzer vs human - is the production scorer calibrated? This is the
   number that decides whether to change the analyzer prompt.
2. judge vs human   - is the LLM judge a trustworthy stand-in for hand
   labels? Only once this holds can the judge be used to evaluate new
   items without labeling them by hand.
"""

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ai_monitor.eval import golden_set, metrics
from ai_monitor.eval.metrics import Agreement

log = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"


def evaluate(conn: sqlite3.Connection) -> dict:
    rows = golden_set.labeled_items(conn)
    if not rows:
        raise ValueError("golden set is empty - run label.py first")

    human = [r["human_score"] for r in rows]
    analyzer_scores = [r["analyzer_score"] for r in rows]

    result = {
        "n_labeled": len(rows),
        "analyzer_vs_human": metrics.compare(analyzer_scores, human),
        "analyzer_calibration": metrics.calibration_buckets(analyzer_scores, human),
        "judge_vs_human": None,
        "judge_vs_analyzer": None,
        "n_judged": 0,
        "worst_disagreements": _worst(rows),
    }

    judged = [r for r in rows if r["judge_score"] is not None]
    if judged:
        j_human = [r["human_score"] for r in judged]
        j_scores = [r["judge_score"] for r in judged]
        j_analyzer = [r["analyzer_score"] for r in judged]
        result["n_judged"] = len(judged)
        result["judge_vs_human"] = metrics.compare(j_scores, j_human)
        result["judge_vs_analyzer"] = metrics.compare(j_analyzer, j_scores)

    return result


def _worst(rows: list[sqlite3.Row], limit: int = 5) -> list[dict]:
    """The items the analyzer got most wrong - where prompt fixes come from."""
    scored = sorted(
        rows,
        key=lambda r: abs(r["analyzer_score"] - r["human_score"]),
        reverse=True,
    )
    return [
        {
            "title": r["title"],
            "url": r["url"],
            "human": r["human_score"],
            "analyzer": r["analyzer_score"],
            "delta": r["analyzer_score"] - r["human_score"],
            "note": r["human_notes"] or "",
            "summary": r["summary"],
        }
        for r in scored[:limit]
    ]


def _fmt(value: Optional[float], places: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{places}f}"


def _agreement_table(name: str, a: Agreement) -> str:
    return (
        f"### {name}\n\n"
        f"| metric | value |\n|---|---|\n"
        f"| items | {a.n} |\n"
        f"| mean absolute error | {_fmt(a.mae)} |\n"
        f"| RMSE | {_fmt(a.rmse)} |\n"
        f"| Pearson r | {_fmt(a.pearson_r)} |\n"
        f"| binary agreement (relevant vs not) | {_fmt(a.binary_agreement)} |\n"
        f"| precision | {_fmt(a.precision)} |\n"
        f"| recall | {_fmt(a.recall)} |\n"
        f"| mean bias | {a.mean_bias:+.3f} |\n"
    )


def render_report(result: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    judged_note = (
        f", {result['n_judged']} also scored by the judge model"
        if result["n_judged"]
        else ""
    )
    parts = [
        "# Analyzer Evaluation\n",
        f"Generated {now}. Golden set: {result['n_labeled']} hand-labeled items"
        f"{judged_note}.\n",
        "Mean bias is the signed average of (score - human score): positive means "
        "the scorer is too generous.\n",
        _agreement_table("Analyzer vs human labels", result["analyzer_vs_human"]),
    ]

    if result["judge_vs_human"]:
        parts.append(
            _agreement_table("Judge vs human labels", result["judge_vs_human"])
        )
        parts.append(
            "\nJudge-vs-human is the number that says whether the judge can stand in "
            "for hand labeling on future items. Analyzer-vs-human is the number that "
            "says whether the analyzer prompt needs work.\n"
        )

    parts.append("\n### Analyzer calibration by score band\n")
    parts.append("| analyzer band | n | mean analyzer | mean human | bias |\n|---|---|---|---|---|")
    for b in result["analyzer_calibration"]:
        if not b["n"]:
            parts.append(f"| {b['range']} | 0 | - | - | - |")
        else:
            parts.append(
                f"| {b['range']} | {b['n']} | {b['mean_predicted']:.2f} | "
                f"{b['mean_reference']:.2f} | {b['mean_bias']:+.2f} |"
            )

    parts.append("\n\n### Largest disagreements\n")
    parts.append("Where the analyzer diverged most from your labels - the source of prompt fixes.\n")
    for d in result["worst_disagreements"]:
        parts.append(
            f"- **{d['title']}** ({d['delta']:+.2f}: analyzer {d['analyzer']:.2f}, "
            f"you {d['human']:.2f})  \n"
            f"  {d['summary']}"
            + (f"  \n  _your note: {d['note']}_" if d["note"] else "")
        )

    return "\n".join(parts) + "\n"


def run(conn: sqlite3.Connection, write: bool = True) -> tuple[dict, Optional[Path]]:
    result = evaluate(conn)
    report = render_report(result)

    path = None
    if write:
        REPORTS_DIR.mkdir(exist_ok=True)
        path = REPORTS_DIR / "eval-report.md"
        path.write_text(report)
        log.info("wrote eval report to %s", path)

    return result, path
