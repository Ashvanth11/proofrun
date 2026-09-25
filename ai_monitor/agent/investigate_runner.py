"""Which items get investigated, how a run is rendered, and what it may cost.

Three jobs that all sit between the agent and a human:

**The gate.** Same principle as `runner.candidates`: the expensive thing runs
only on items the cheap thing already liked, and never twice. Investigating is
a great deal more expensive than assessing - model turns, GitHub requests, a
container, and a download - so the threshold is higher and the limit lower.

**The rendering.** Everything a run carries is derived from attacker-controlled
text: README contents, command output, web results. It reaches a markdown table
that ends up in a report and eventually a README, so it is escaped and
truncated here, once, rather than at each call site.

**The estimate.** The loop cap excludes its overshoot turn, wrap-up, extraction,
critique and search. `worst_case_usd` budgets for these with configured output
limits, but is an estimate, not a guaranteed total-spend bound.
"""

import logging
import re
import sqlite3
from typing import Any, Optional

from ai_monitor.agent import critique as critique_mod
from ai_monitor.agent import investigate as inv
from ai_monitor.agent.loop import Caps
from ai_monitor.analysis.analyzer import PRICING, Usage

log = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 0.6
DEFAULT_LIMIT = 3

# The cap every entry point defaults to. It is a safety net, not a budget:
# measured questions in the 2026-09-13 smoke run cost $0.20-$0.35 at 6-10
# steps, so it does not fire on well-behaved work and the money is not saved
# by lowering it. What lowering it *does* buy is truncated runs - and a fired
# cap scores as a failure under the eval's "stop reason is sufficient_info"
# criterion, which makes this a correctness lever rather than a budget one.
# Cost grows quadratically in turns (every turn resends the transcript), so the
# headroom is for the long tail, not the median.
DEFAULT_MAX_COST_USD = 1.50

# $10 per 1,000 searches, from platform.claude.com/docs/en/about-claude/pricing
# (checked 2026-09-13). Failed searches are not billed; this assumes none fail.
WEB_SEARCH_USD = 0.01


# --- the gate ------------------------------------------------------------


def candidates(
    conn: sqlite3.Connection,
    threshold: float = DEFAULT_THRESHOLD,
    limit: int = DEFAULT_LIMIT,
    force: bool = False,
) -> list[sqlite3.Row]:
    """GitHub items worth an investigation.

    The language and size gates are deliberately absent here. Stored `raw_json`
    does not carry them reliably, and an agent can still answer plenty of
    questions by reading when it cannot clone - so those gates live in the
    sandbox module and fire at clone time instead.
    """
    query = """
        SELECT i.id, i.source, i.source_id, i.title, i.url, i.content,
               a.relevance_score
        FROM items i
        JOIN analyses a ON a.item_id = i.id
        LEFT JOIN investigations v ON v.item_id = i.id
        WHERE i.source = 'github'
          AND i.canonical_id IS NULL
          AND a.relevance_score >= ?
    """
    if not force:
        query += " AND v.id IS NULL"
    query += " ORDER BY a.relevance_score DESC LIMIT ?"

    return conn.execute(query, (threshold, limit)).fetchall()


def run_investigations_on_candidates(
    conn: sqlite3.Connection,
    client: Any,
    model: str,
    threshold: float = DEFAULT_THRESHOLD,
    limit: int = DEFAULT_LIMIT,
    caps: Optional[Caps] = None,
    force: bool = False,
    with_critique: bool = True,
) -> tuple[int, Usage]:
    """Autonomous mode: derive a question per item, then answer it.

    Returns (count investigated, total usage). Items whose README asserts
    nothing testable are stored as `could_not_test`/`no_testable_claim` rather
    than skipped silently - "four of nine READMEs claim nothing checkable" is a
    finding, and storing it also stops the next run re-deriving the same
    nothing.
    """
    rows = candidates(conn, threshold=threshold, limit=limit, force=force)
    if not rows:
        log.info("no repositories above threshold %.2f for investigation", threshold)
        return 0, Usage(model=model)

    log.info("investigating up to %d repositories", len(rows))
    total = Usage(model=model)
    completed = 0

    for row in rows:
        question, usage = inv.derive_question(row, client, model=model)
        total.input_tokens += usage.input_tokens
        total.output_tokens += usage.output_tokens

        if question is None:
            _store_no_claim(conn, row, model)
            continue

        run = inv.investigate(question, client, model=model, caps=caps)
        total.input_tokens += run.usage.input_tokens
        total.output_tokens += run.usage.output_tokens

        if with_critique and run.report is not None:
            run, critique_usage = critique_mod.critique_and_revise_investigation(
                run, client, model=model
            )
            total.input_tokens += critique_usage.input_tokens
            total.output_tokens += critique_usage.output_tokens

        inv.store_investigation(conn, run, item_id=row["id"])
        completed += 1

    log.info("investigated %d repositories, estimated token cost $%.4f", completed, total.cost_usd)
    return completed, total


def _store_no_claim(conn: sqlite3.Connection, row: sqlite3.Row, model: str) -> None:
    run = inv.InvestigationRun(
        question=inv.Question(
            repo=row["source_id"], claim="", question="(no testable claim)"
        ),
        steps_taken=0,
        stop_reason="no_testable_claim",
        usage=Usage(model=model),
        report=inv.Investigation(
            question="(no testable claim)",
            verdict="could_not_test",
            blockers=["no_testable_claim"],
            summary="The README asserts nothing that could be checked.",
        ),
    )
    inv.store_investigation(conn, run, item_id=row["id"])
    log.info("%s: no testable claim, recorded", row["source_id"])


# --- what a batch may cost -----------------------------------------------


def worst_case_usd(
    count: int,
    caps: Optional[Caps] = None,
    max_web_searches: int = inv.tools.DEFAULT_MAX_WEB_SEARCHES,
    model: str = inv.INVESTIGATE_MODEL,
) -> float:
    """Conservative estimate for `count` questions, not a spend guarantee.

    The cost cap is checked *before* each turn, which is what makes it a
    ceiling on starting work rather than a number noticed too late. The
    consequence is that four calls per question are not covered by it:

      1. the turn that crosses the cap - its price is unknown until it returns
      2. the wrap-up call, which runs after a cap fires to salvage the work
      3. extraction into the Investigation schema
      4. the critique, and one revision if it fires

    Each is bounded by `max_tokens` and the largest context the step cap
    permits, so the overrun is bounded - but it is not zero, and a batch that
    quoted only `count x cap` would be understating what it is about to spend.
    """
    caps = caps or inv.default_caps()
    rates = PRICING.get(model)
    if not rates:
        if model.startswith("ollama/"):
            return 0.0  # local inference
        raise ValueError(f"no pricing for model {model!r}; estimate is unknown")

    # Largest context the loop can reach: each turn adds at most one full
    # response (max_tokens) plus one truncated tool result.
    per_turn_growth = 4096 + 1500
    max_context = 1700 + caps.max_steps * per_turn_growth
    turn_ceiling = (max_context * rates["input"] + 4096 * rates["output"]) / 1_000_000

    # Extraction and critique both send a bounded prompt and a bounded reply.
    extraction_call = (4000 * rates["input"] + inv.EXTRACTION_MAX_TOKENS * rates["output"]) / 1_000_000
    critique_call = (4000 * rates["input"] + critique_mod.CRITIQUE_MAX_TOKENS * rates["output"]) / 1_000_000

    per_question = (
        caps.max_cost_usd
        + 2 * turn_ceiling  # the overshoot turn, and the wrap-up
        + extraction_call
        + 2 * critique_call  # critique, and one revision
        + max_web_searches * WEB_SEARCH_USD
    )
    return count * per_question


# --- rendering -----------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean(text: Any, limit: int = 200) -> str:
    """Make repo-derived text safe to put in a markdown table.

    Every string that reaches a report came from a README, a command's stdout,
    or a web page. Pipes would break out of a table cell, control characters
    would corrupt a terminal, and length is unbounded - so all three are dealt
    with before the text is anyone's problem.
    """
    out = _CONTROL.sub("", str(text if text is not None else ""))
    out = out.replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    out = " ".join(out.split())
    return out[:limit] + ("..." if len(out) > limit else "")


def format_trajectory(run: Any) -> str:
    """The reactive CLI's output: what it did, what it found, what it cost."""
    report = run.report
    lines = [
        "",
        f"QUESTION  {run.question.question}",
        f"REPO      {run.question.repo}",
    ]
    if run.question.claim:
        lines.append(f"CLAIM     {clean(run.question.claim, 300)}")

    lines += ["", "TRAJECTORY"]
    if not run.tool_calls:
        lines.append("  (no tools were called)")
    for call in run.tool_calls:
        mark = "x" if call.is_error else "-"
        where = " [web]" if call.server else ""
        args = clean(call.arguments, 90)
        lines.append(f"  {mark} step {call.step}: {call.tool}({args}){where}")

    lines += ["", "LEDGER"]
    if report is None or not report.ledger:
        lines.append("  (empty)")
    else:
        for entry in report.ledger:
            tag = {"observed": "OBSERVED", "inspected": "inspected"}.get(
                entry.kind, "reported"
            )
            lines.append(
                f"  [{entry.side:>7} / {tag:>9}] {clean(entry.statement, 300)}"
            )
            lines.append(f"            from: {clean(entry.source, 120)}")

    if report is not None:
        facts = report.facts
        lines += [
            "",
            "FACTS",
            f"  setup          {facts.setup_seconds:.1f}s over "
            f"{facts.setup_commands} command(s), "
            f"install {'succeeded' if facts.install_succeeded else 'not run/failed'}",
            f"  disk           {facts.clone_mb:.0f} MB cloned, "
            f"{facts.volume_mb:.0f} MB total",
            f"  needs          {', '.join(facts.needs) or 'nothing special'}",
            f"  exercised      {clean(facts.headline_capability, 200) or '-'}",
        ]
        if facts.observed_output:
            lines.append(f"  output         {clean(facts.observed_output, 300)}")

        lines += [
            "",
            f"VERDICT   {report.verdict.upper()}"
            + ("  (downgraded: no first-hand evidence)" if run.downgraded else ""),
        ]
        if report.blockers:
            lines.append(f"BLOCKED   {', '.join(report.blockers)}")
        lines.append(f"SUMMARY   {clean(report.summary, 600)}")
    else:
        lines += ["", "VERDICT   none - the run produced no conclusion"]

    lines += [
        "",
        f"{run.steps_taken} steps, stop={run.stop_reason}, "
        f"{run.observed_count} observed / {run.inspected_count} inspected / "
        f"{run.reported_count} reported"
        + (f", {run.dropped_entries} entries dropped" if run.dropped_entries else ""),
        f"estimated token cost ${run.usage.cost_usd:.4f}, {run.wall_seconds:.0f}s",
        "",
    ]
    return "\n".join(lines)


TABLE_HEADER = (
    "| Question | Verdict | Obs | Insp | Rep | Blockers | Setup | Est. token cost | Wall | Steps |\n"
    "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"
)


def table_row(run: Any) -> str:
    report = run.report
    verdict = report.verdict if report else "none"
    if run.downgraded:
        verdict += " (v)"
    blockers = ", ".join(report.blockers) if report and report.blockers else "-"
    setup = f"{report.facts.setup_seconds:.0f}s" if report else "-"
    return (
        f"| {clean(run.question.question, 120)} "
        f"| {verdict} "
        f"| {run.observed_count} "
        f"| {run.inspected_count} "
        f"| {run.reported_count} "
        f"| {clean(blockers, 60)} "
        f"| {setup} "
        f"| ${run.usage.cost_usd:.3f} "
        f"| {run.wall_seconds:.0f}s "
        f"| {run.steps_taken} |"
    )


def markdown_report(runs: list[Any], title: str = "Investigation batch") -> str:
    total_cost = sum(r.usage.cost_usd for r in runs)
    total_wall = sum(r.wall_seconds for r in runs)
    verdicts: dict[str, int] = {}
    for run in runs:
        key = run.report.verdict if run.report else "none"
        verdicts[key] = verdicts.get(key, 0) + 1

    lines = [
        f"# {title}",
        "",
        f"{len(runs)} questions, estimated token cost ${total_cost:.2f}, {total_wall / 60:.0f} min.",
        "",
        "Verdicts: "
        + ", ".join(f"{n} {v}" for v, n in sorted(verdicts.items()))
        + ".",
        "",
        "`Obs`/`Insp`/`Rep` are observed, inspected and reported ledger entries: "
        "observed means a command ran in the sandbox and printed it; inspected "
        "means a fact GitHub computed (licence field, language, file listing); "
        "reported means someone wrote it. `(v)` marks a verdict downgraded for "
        "lacking first-hand (observed or inspected) evidence.",
        "",
        TABLE_HEADER,
    ]
    lines += [table_row(run) for run in runs]
    lines += ["", "## Trajectories", ""]
    for run in runs:
        lines += ["```", format_trajectory(run).strip(), "```", ""]
    return "\n".join(lines)
