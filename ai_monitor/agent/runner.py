"""Decides which repositories the agent investigates, and runs it on those.

The agent is expensive relative to the analyzer - several model calls and one
GitHub API request per tool call, versus a single structured call. Running it on
every repository spends that on items the analyzer has already judged
irrelevant, which is waste concentrated on exactly the items least worth it.

So the agent is gated on the analyzer's score. See docs/decisions.md #1 for the
reasoning and the conditions that would justify removing the gate.
"""

import logging
import sqlite3
from typing import Any, Optional

from ai_monitor.agent import critique as critique_mod
from ai_monitor.agent import repo_agent
from ai_monitor.analysis.analyzer import Usage
from ai_monitor.config.settings import InterestArea

log = logging.getLogger(__name__)

DEFAULT_AGENT_THRESHOLD = 0.4


def candidates(
    conn: sqlite3.Connection,
    threshold: float = DEFAULT_AGENT_THRESHOLD,
    limit: int = 10,
    force: bool = False,
) -> list[sqlite3.Row]:
    """GitHub items worth the agent's attention.

    Filters on three things: the item is a repository, the analyzer scored it
    above the threshold, and (unless forced) the agent has not already run on
    it. The last is the same idempotency principle as content_hash - a re-run
    should not re-pay for work already done.
    """
    query = """
        SELECT i.id, i.source_id, a.relevance_score
        FROM items i
        JOIN analyses a ON a.item_id = i.id
        LEFT JOIN agent_runs r ON r.item_id = i.id
        WHERE i.source = 'github'
          AND i.canonical_id IS NULL
          AND a.relevance_score >= ?
    """
    if not force:
        query += " AND r.id IS NULL"
    query += " ORDER BY a.relevance_score DESC LIMIT ?"

    return conn.execute(query, (threshold, limit)).fetchall()


def run_agent_on_candidates(
    conn: sqlite3.Connection,
    client: Any,
    model: str,
    threshold: float = DEFAULT_AGENT_THRESHOLD,
    limit: int = 10,
    interests: Optional[dict[str, InterestArea]] = None,
    max_steps: int = repo_agent.DEFAULT_MAX_STEPS,
    max_cost_usd: float = repo_agent.DEFAULT_MAX_COST_USD,
    with_critique: bool = True,
    force: bool = False,
) -> tuple[int, Usage]:
    """Investigate every qualifying repository. Returns (count, total usage)."""
    rows = candidates(conn, threshold=threshold, limit=limit, force=force)
    if not rows:
        log.info("no repositories above threshold %.2f for the agent", threshold)
        return 0, Usage(model=model)

    log.info(
        "agent: %d repositories above threshold %.2f", len(rows), threshold
    )

    total = Usage(model=model)
    completed = 0

    for row in rows:
        run = repo_agent.analyze_repo(
            row["source_id"],
            client,
            model=model,
            interests=interests,
            max_steps=max_steps,
            max_cost_usd=max_cost_usd,
        )
        total.input_tokens += run.usage.input_tokens
        total.output_tokens += run.usage.output_tokens

        if with_critique and run.assessment is not None:
            run, critique_usage = critique_mod.critique_and_revise(
                run, client, model=model
            )
            total.input_tokens += critique_usage.input_tokens
            total.output_tokens += critique_usage.output_tokens

        repo_agent.store_run(conn, row["id"], run)
        completed += 1

    log.info(
        "agent: investigated %d repositories, cost $%.4f", completed, total.cost_usd
    )
    return completed, total
