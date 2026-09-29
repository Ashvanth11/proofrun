"""Bounded weekly repository discovery, using the existing investigation engine.

Use Haiku for discovery analysis and Sonnet for the one investigation. No
provider switching or paid fallback happens automatically.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from ai_monitor.agent import investigate, investigate_runner
from ai_monitor.analysis import analyzer
from ai_monitor.storage import db
from ai_monitor.watchers import github


def run_week(conn, client, *, now=None, fetch=github.fetch, analyze=analyzer.analyze_and_store,
             investigate_candidates=investigate_runner.run_investigations_on_candidates) -> dict:
    now = now or datetime.now(timezone.utc)
    week = now.strftime("%G-W%V")
    conn.execute("""CREATE TABLE IF NOT EXISTS weekly_runs (
        week TEXT PRIMARY KEY, status TEXT NOT NULL, started_at TEXT NOT NULL,
        finished_at TEXT, discovered INTEGER DEFAULT 0, investigated INTEGER DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS weekly_discoveries (
        week TEXT NOT NULL, item_id INTEGER NOT NULL,
        repo TEXT NOT NULL, description TEXT NOT NULL,
        PRIMARY KEY (week, item_id)
    )""")
    previous = conn.execute("SELECT * FROM weekly_runs WHERE week = ?", (week,)).fetchone()
    if previous:
        if previous["status"] == "completed":
            return dict(previous)
        raise RuntimeError("This week's attempt is incomplete. Review its saved state before retrying; automatic paid retries are disabled.")
    conn.execute("INSERT INTO weekly_runs (week,status,started_at) VALUES (?,'running',?)", (week, now.isoformat()))
    conn.commit()
    try:
        # A single source and bounded fresh batch keep bootstrapping predictable.
        items = fetch(max_results=10, days=7)
        weekly_item_ids = []
        for item in items[:10]:
            item_id = db.upsert_item(conn, item)
            weekly_item_ids.append(item_id)
            description = (item.raw.get("description") or item.content.split("\n\nTopics:")[0]).strip()
            conn.execute("INSERT OR IGNORE INTO weekly_discoveries (week,item_id,repo,description) VALUES (?,?,?,?)",
                         (week, item_id, item.source_id, description[:600]))
            conn.commit()
            analyze(conn, item_id, item, client=client, model=analyzer.ANALYZER_MODEL)
        before = conn.execute("SELECT count(*) FROM investigations WHERE item_id IS NOT NULL").fetchone()[0]
        investigate_candidates(conn, client, model=investigate.INVESTIGATE_MODEL, threshold=0.6,
                               limit=1, caps=investigate.default_caps(max_cost_usd=1.50),
                               readme_loader=github.fetch_readme, select_best_claim=True,
                               eligible_item_ids=weekly_item_ids)
        after = conn.execute("SELECT count(*) FROM investigations WHERE item_id IS NOT NULL").fetchone()[0]
        finished = datetime.now(timezone.utc).isoformat()
        conn.execute("UPDATE weekly_runs SET status='completed',finished_at=?,discovered=?,investigated=? WHERE week=?",
                     (finished, min(len(items), 10), after - before, week))
        conn.commit()
        return dict(conn.execute("SELECT * FROM weekly_runs WHERE week=?", (week,)).fetchone())
    except Exception:
        conn.execute("UPDATE weekly_runs SET status='failed',finished_at=? WHERE week=?", (datetime.now(timezone.utc).isoformat(), week))
        conn.commit()
        raise


def rerun_saved_week(
    conn, client, *, now=None,
    investigate_candidates=investigate_runner.run_investigations_on_candidates,
) -> dict:
    """Manually replace this week's no-claim result using its saved discoveries."""
    now = now or datetime.now(timezone.utc)
    week = now.strftime("%G-W%V")
    previous = conn.execute("SELECT * FROM weekly_runs WHERE week=?", (week,)).fetchone()
    if previous is None or previous["status"] != "completed":
        raise RuntimeError("A completed current week is required for a manual rerun")
    discoveries = conn.execute(
        "SELECT item_id FROM weekly_discoveries WHERE week=? ORDER BY item_id", (week,)
    ).fetchall()
    item_ids = [row["item_id"] for row in discoveries]
    if not 1 <= len(item_ids) <= 10:
        raise RuntimeError("The saved weekly batch is missing or exceeds the discovery limit")
    prior = conn.execute(
        "SELECT v.id, v.stop_reason FROM investigations v "
        "JOIN weekly_discoveries d ON d.item_id=v.item_id WHERE d.week=?", (week,)
    ).fetchall()
    if len(prior) != 1 or prior[0]["stop_reason"] != "no_testable_claim":
        raise RuntimeError("Manual rerun is limited to one saved no-claim result")

    conn.execute(
        "UPDATE weekly_runs SET status='running',started_at=?,finished_at=NULL WHERE week=?",
        (now.isoformat(), week),
    )
    conn.commit()
    try:
        count, _ = investigate_candidates(
            conn, client, model=investigate.INVESTIGATE_MODEL, threshold=0.6,
            limit=1, caps=investigate.default_caps(max_cost_usd=1.50),
            readme_loader=github.fetch_readme, select_best_claim=True,
            eligible_item_ids=item_ids, force=True,
        )
        # The old placeholder remains available in the previous Actions artifact.
        # Remove it from the current state if a different repo was selected.
        conn.execute(
            "DELETE FROM investigations WHERE id=? AND stop_reason='no_testable_claim'",
            (prior[0]["id"],),
        )
        conn.execute(
            "UPDATE weekly_runs SET status='completed',finished_at=?,investigated=? WHERE week=?",
            (datetime.now(timezone.utc).isoformat(), count, week),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM weekly_runs WHERE week=?", (week,)).fetchone())
    except Exception:
        conn.execute(
            "UPDATE weekly_runs SET status='failed',finished_at=? WHERE week=?",
            (datetime.now(timezone.utc).isoformat(), week),
        )
        conn.commit()
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=Path("weekly-state/monitor.db"))
    parser.add_argument("--rerun-saved-week", action="store_true",
                        help="Manually replace this week's saved no-claim result")
    args = parser.parse_args(argv)
    load_dotenv()
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        parser.error("ANTHROPIC_API_KEY is missing; no monitoring started")
    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(args.db)
    try:
        client = anthropic.Anthropic(api_key=key)
        record = (rerun_saved_week if args.rerun_saved_week else run_week)(conn, client)
    finally:
        conn.close()
    args.db.with_name("last-run.json").write_text(json.dumps(record, indent=2) + "\n")
    print(f"{record['week']}: {record['investigated']} new investigation(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
