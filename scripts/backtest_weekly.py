"""Replay deterministic selection gates against saved weekly DB snapshots.

No network, model calls, Docker, or writes to source snapshots. This is a
partial replay: historical alternate GitHub search results, full README inputs,
and full old tool results cannot be recreated from these databases.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_monitor.agent.investigate_runner import candidates, weekly_candidate_blocker


def metadata_from_trace(calls: list[dict], repo: str) -> dict:
    for call in calls:
        if call["tool"] != "get_repo_metadata" or call.get("is_error"):
            continue
        if call.get("arguments", {}).get("repo") != repo:
            continue
        try:
            data = json.loads(call.get("result_text") or call["result_summary"])
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return {k: data[k] for k in ("language", "size_kb") if k in data}
    return {}


def replay_snapshot(path: Path) -> dict:
    source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        source.backup(conn)
        week = conn.execute(
            "SELECT * FROM weekly_runs WHERE status='completed' ORDER BY week DESC LIMIT 1"
        ).fetchone()
        if week is None:
            raise ValueError(f"No completed week in {path}")
        selected = list(conn.execute(
            "SELECT v.* FROM investigations v JOIN weekly_discoveries d ON d.item_id=v.item_id "
            "WHERE d.week=? AND v.created_at>=? AND v.created_at<=?",
            (week["week"], week["started_at"], week["finished_at"]),
        ))
        if len(selected) != week["investigated"]:
            raise ValueError(f"Investigation attribution mismatch for {week['week']}")
        originals = []
        for row in selected:
            calls = json.loads(row["tool_calls"] or "[]")
            originals.append({
                "repo": row["repo"], "item_id": row["item_id"],
                "question": row["question"], "verdict": row["verdict"],
                "blockers": json.loads(row["blockers"] or "[]"),
                "retained_metadata": metadata_from_trace(calls, row["repo"]),
                "tool_calls": len(calls),
                "full_result_records": sum(call.get("result_text") is not None for call in calls),
                "opaque_search_records": sum(
                    call["tool"] == "web_search" and "encrypted_content" in call.get("result_summary", "")
                    for call in calls
                ),
                "sandbox_run_calls": sum(call["tool"] == "sandbox_run" for call in calls),
            })
            # Reconstruct the candidate gate immediately before this week's
            # selected investigation. Earlier investigations remain excluded.
            conn.execute("DELETE FROM investigations WHERE id=?", (row["id"],))
        item_ids = [r[0] for r in conn.execute(
            "SELECT item_id FROM weekly_discoveries WHERE week=?", (week["week"],)
        )]
        eligible = candidates(conn, threshold=0.6, limit=10, eligible_item_ids=item_ids)
        gate_rows = []
        for row in eligible:
            raw = json.loads(row["raw_json"] or "{}")
            gate_rows.append({
                "repo": row["source_id"], "relevance_score": row["relevance_score"],
                "historical_discovery_metadata": {
                    k: raw[k] for k in ("language", "size_kb") if k in raw
                },
                "replay_blocker": weekly_candidate_blocker(raw),
                "size_metadata_missing": raw.get("size_kb") is None,
            })
        by_repo = {r["repo"]: r for r in gate_rows}
        for original in originals:
            replay = by_repo.get(original["repo"])
            original["in_reconstructed_candidates"] = replay is not None
            original["discovery_replay_blocker"] = replay["replay_blocker"] if replay else None
            original["blocker_with_retained_investigation_metadata"] = weekly_candidate_blocker(
                original["retained_metadata"]
            )
            original["offline_claim_screening"] = "not_replayed_missing_historical_inputs"
        return {
            "week": week["week"], "snapshot": str(path.resolve()),
            "historical_discoveries": len(item_ids),
            "historical_candidates": len(gate_rows),
            "rejected_by_discovery_metadata": sum(r["replay_blocker"] is not None for r in gate_rows),
            "remaining_for_claim_screening": sum(r["replay_blocker"] is None for r in gate_rows),
            "original_investigations": originals,
            "candidate_gate_results": gate_rows,
        }
    finally:
        conn.close()
        source.close()


def backtest(paths: list[Path]) -> dict:
    weeks = sorted((replay_snapshot(path) for path in paths), key=lambda r: r["week"])
    if len({w["week"] for w in weeks}) != len(weeks):
        raise ValueError("Supply one snapshot per distinct week")
    original = [r for w in weeks for r in w["original_investigations"]]
    return {
        "mode": "offline_partial_replay",
        "limitations": [
            "Replays each original discovery batch, not the new Python-only search's alternative results.",
            "Preserves the original historical analyses and earlier-investigation exclusions.",
            "Old discovery records omit size; investigation metadata is reported separately, not injected into the discovery replay.",
            "No historical full README screening inputs or full tool responses are retained; model screening and revised grounding cannot be backtested here.",
            "Surviving candidates are not verified feasible tests or predicted conclusive outcomes.",
        ],
        "summary": {
            "weeks": len(weeks), "original_investigations": len(original),
            "originals_rejected_using_discovery_metadata": sum(r["discovery_replay_blocker"] is not None for r in original),
            "originals_blocked_using_retained_investigation_metadata": sum(r["blocker_with_retained_investigation_metadata"] is not None for r in original),
            "original_sandbox_run_calls": sum(r["sandbox_run_calls"] for r in original),
            "original_tool_records": sum(r["tool_calls"] for r in original),
            "original_full_result_records": sum(r["full_result_records"] for r in original),
            "replayed_conclusive_rate": None,
        },
        "weeks": weeks,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshots", nargs="+", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    payload = json.dumps(backtest(args.snapshots), indent=2) + "\n"
    if args.out:
        args.out.write_text(payload)
    else:
        print(payload, end="")


if __name__ == "__main__":
    main()
