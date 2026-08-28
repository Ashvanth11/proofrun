import sqlite3
from datetime import datetime, timezone
from typing import Optional

# An item is "relevant" above this score. Used to turn the continuous scores
# into the precision/recall view in the eval report.
RELEVANT_THRESHOLD = 0.5


def record_label(
    conn: sqlite3.Connection,
    item_id: int,
    human_score: float,
    human_notes: str = "",
) -> None:
    """Store your own judgement of an item's relevance."""
    conn.execute(
        """
        INSERT INTO eval_items (item_id, human_score, human_notes, evaluated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            human_score = excluded.human_score,
            human_notes = excluded.human_notes,
            evaluated_at = excluded.evaluated_at
        """,
        (item_id, human_score, human_notes, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def record_judge(
    conn: sqlite3.Connection,
    item_id: int,
    judge_score: float,
    judge_reasoning: str,
) -> None:
    """Store the judge model's independent score for an already-labeled item."""
    conn.execute(
        """
        UPDATE eval_items
        SET judge_score = ?, judge_reasoning = ?, agreement = ABS(human_score - ?)
        WHERE item_id = ?
        """,
        (judge_score, judge_reasoning, judge_score, item_id),
    )
    conn.commit()


def unlabeled_items(
    conn: sqlite3.Connection, limit: int = 50
) -> list[sqlite3.Row]:
    """Analyzed items with no human label yet - the queue for labeling.

    The analyzer's own score is deliberately not selected here: seeing it
    before labeling would anchor your judgement and corrupt the golden set.
    """
    return conn.execute(
        """
        SELECT i.id, i.source, i.source_id, i.title, i.url, i.content
        FROM items i
        JOIN analyses a ON a.item_id = i.id
        LEFT JOIN eval_items e ON e.item_id = i.id
        WHERE e.id IS NULL AND i.canonical_id IS NULL
        ORDER BY i.fetched_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def labeled_items(
    conn: sqlite3.Connection, require_judge: bool = False
) -> list[sqlite3.Row]:
    """The golden set joined to analyzer and judge scores for comparison."""
    query = """
        SELECT i.id AS item_id, i.title, i.url, i.source,
               e.human_score, e.human_notes, e.judge_score, e.judge_reasoning,
               a.relevance_score AS analyzer_score, a.summary, a.model
        FROM eval_items e
        JOIN items i ON i.id = e.item_id
        JOIN analyses a ON a.item_id = e.item_id
        WHERE e.human_score IS NOT NULL
    """
    if require_judge:
        query += " AND e.judge_score IS NOT NULL"
    query += " ORDER BY e.human_score DESC"
    return conn.execute(query).fetchall()


def needs_judging(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT i.id AS item_id, i.title, i.content, i.source
        FROM eval_items e
        JOIN items i ON i.id = e.item_id
        WHERE e.human_score IS NOT NULL AND e.judge_score IS NULL
        """
    ).fetchall()


def stats(conn: sqlite3.Connection) -> dict[str, Optional[float]]:
    row = conn.execute(
        """
        SELECT COUNT(*) AS labeled,
               SUM(CASE WHEN judge_score IS NOT NULL THEN 1 ELSE 0 END) AS judged,
               AVG(human_score) AS mean_human
        FROM eval_items WHERE human_score IS NOT NULL
        """
    ).fetchone()
    return {
        "labeled": row["labeled"],
        "judged": row["judged"] or 0,
        "mean_human": row["mean_human"],
    }
