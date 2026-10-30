"""Historical replay must not leak later results into the candidate gate."""
import hashlib
import json

import pytest

from ai_monitor.storage import db
from ai_monitor.storage.models import Item, Source
from scripts.backtest_weekly import backtest, replay_snapshot


def saved_week(tmp_path):
    path = tmp_path / "monitor.db"
    conn = db.connect(path)
    conn.executescript("""
        CREATE TABLE weekly_runs (
            week TEXT, status TEXT, started_at TEXT, finished_at TEXT, investigated INTEGER
        );
        CREATE TABLE weekly_discoveries (week TEXT, item_id INTEGER);
        INSERT INTO weekly_runs VALUES
            ('2026-W43','completed','2026-10-19T10:00:00','2026-10-19T11:00:00',1);
    """)
    for repo, score, language, investigated_at in [
        ("owner/earlier", 0.99, "Python", "2026-10-12T10:00:00"),
        ("owner/selected", 0.9, "TypeScript", "2026-10-19T10:30:00"),
        ("owner/alternative", 0.8, "Python", None),
    ]:
        item_id = db.upsert_item(conn, Item(
            source=Source.GITHUB, source_id=repo, title=repo,
            url=f"https://github.com/{repo}", raw={"language": language},
        ))
        conn.execute("INSERT INTO analyses (item_id,relevance_score) VALUES (?,?)", (item_id, score))
        conn.execute("INSERT INTO weekly_discoveries VALUES ('2026-W43',?)", (item_id,))
        if investigated_at:
            calls = [{
                "tool": "get_repo_metadata", "arguments": {"repo": repo},
                "is_error": False,
                "result_summary": json.dumps({"language": language, "size_kb": 200}),
            }]
            conn.execute(
                "INSERT INTO investigations (item_id,repo,question,verdict,created_at,tool_calls) VALUES (?,?,?,'inconclusive',?,?)",
                (item_id, repo, "Does it work?", investigated_at, json.dumps(calls)),
            )
    conn.commit()
    conn.close()
    return path


def test_replay_restores_selected_candidate_but_keeps_earlier_exclusions(tmp_path):
    path = saved_week(tmp_path)
    original_hash = hashlib.sha256(path.read_bytes()).digest()
    result = replay_snapshot(path)
    assert [r["repo"] for r in result["candidate_gate_results"]] == [
        "owner/selected", "owner/alternative",
    ]
    assert result["historical_candidates"] == 2
    assert result["rejected_by_discovery_metadata"] == 1
    assert result["remaining_for_claim_screening"] == 1
    assert result["original_investigations"][0]["in_reconstructed_candidates"]
    assert hashlib.sha256(path.read_bytes()).digest() == original_hash


def test_missing_discovery_size_is_not_backfilled_from_later_investigation(tmp_path):
    path = saved_week(tmp_path)
    conn = db.connect(path)
    conn.execute("UPDATE items SET raw_json=? WHERE source_id='owner/selected'", (json.dumps({"language": "Python"}),))
    conn.execute(
        "UPDATE investigations SET tool_calls=? WHERE repo='owner/selected'",
        (json.dumps([{
            "tool": "get_repo_metadata", "arguments": {"repo": "owner/selected"},
            "result_summary": json.dumps({"language": "Python", "size_kb": 1_500_000}),
        }]),),
    )
    conn.commit()
    conn.close()
    result = backtest([path])
    original = result["weeks"][0]["original_investigations"][0]
    assert original["discovery_replay_blocker"] is None
    assert original["blocker_with_retained_investigation_metadata"] == "needs_large_download"
    assert result["summary"]["replayed_conclusive_rate"] is None
    assert result["summary"]["original_full_result_records"] == 0
    with pytest.raises(ValueError, match="distinct week"):
        backtest([path, path])
