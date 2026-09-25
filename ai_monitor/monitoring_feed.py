"""Shared, credential-free investigation feed for the local UI and Pages."""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

import httpx
from ai_monitor.agent import investigate as inv
from ai_monitor.storage import db
from typing import Any

log = logging.getLogger(__name__)
PUBLIC_FEED_URL = "https://ashvanth11.github.io/proofrun/monitoring.json"

def load_local_runs(path: Path = db.DEFAULT_DB_PATH) -> tuple[list[dict[str, Any]], str]:
    """Read only actual discovery-linked investigations; never initialize a DB."""
    if not path.exists():
        return [], ""
    conn = None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT v.*, i.title AS discovery_title, i.content AS discovery_description
            FROM investigations v JOIN items i ON i.id = v.item_id
            ORDER BY v.created_at DESC, v.id DESC LIMIT 6
        """).fetchall()
        views = []
        for row in rows:
            report = inv.Investigation.model_validate_json(row["report"]) if row["report"] else None
            critique = json.loads(row["critique"] or "{}")
            views.append({
                "repo": row["repo"], "question": row["question"],
                "status": "monitoring", "recorded_at": (row["created_at"] or "")[:10],
                "discovery_title": row["discovery_title"],
                "repository_description": (report.repository_description if report else "") or (row["discovery_description"] or "").split("\n\nTopics:")[0],
                "verdict": report.verdict if report else "no_structured_verdict",
                "summary": report.summary if report else "The investigation finished without a usable conclusion.",
                "blockers": report.blockers if report else [],
                "ledger": [e.model_dump() for e in report.ledger] if report else [],
                "limitations": report.limitations if report else [],
                "tool_calls": json.loads(row["tool_calls"] or "[]"),
                "observed_output": report.facts.observed_output if report else "",
                "final_text": row["final_text"] or "",
                "critique_status": row["critique_status"],
                "critique_grounded": critique.get("grounded"),
                "critique_issues": critique.get("issues", []),
                "estimated_cost_usd": row["cost_usd"] or 0,
                "elapsed_seconds": row["wall_seconds"] or 0,
                "steps_taken": row["steps_taken"] or 0,
                "stop_reason": row["stop_reason"],
                "downgraded": bool(row["downgraded"]), "revised": bool(row["revised"]),
            })
        return views, ""
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        log.exception("could not read monitoring investigations")
        return [], "Saved monitoring results could not be read. Showing recorded examples below."
    finally:
        if conn is not None:
            conn.close()



def load_weekly_discoveries(path: Path = db.DEFAULT_DB_PATH, week: str | None = None) -> dict:
    """Read the captured batch of a completed week, excluding investigated repos."""
    empty = {"discovery_week": None, "discoveries": []}
    if not path.exists():
        return empty
    conn = None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"weekly_runs", "weekly_discoveries", "investigations"} <= tables:
            return empty  # Older databases have no weekly batch history.
        record = conn.execute(
            "SELECT week FROM weekly_runs WHERE status='completed' "
            + ("AND week=? " if week else "")
            + "ORDER BY finished_at DESC LIMIT 1", (week,) if week else (),
        ).fetchone()
        if record is None:
            return empty
        rows = conn.execute("""
            SELECT d.repo, d.description FROM weekly_discoveries d
            WHERE d.week=? AND NOT EXISTS (
                SELECT 1 FROM investigations v
                WHERE v.item_id IS NOT NULL AND (v.item_id=d.item_id OR lower(v.repo)=lower(d.repo))
            ) ORDER BY d.repo LIMIT 10
        """, (record["week"],)).fetchall()
        return {"discovery_week": record["week"], "discoveries": [
            {"repo": row["repo"], "repository_description": row["description"],
             "status": "not_investigated"} for row in rows
        ]}
    except sqlite3.Error:
        log.exception("could not read weekly discoveries")
        return empty
    finally:
        if conn is not None:
            conn.close()


def validate_feed(data: Any) -> dict:
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("unsupported monitoring feed")
    if not isinstance(data.get("investigations"), list) or len(data["investigations"]) > 50:
        raise ValueError("invalid investigation list")
    for view in data["investigations"]:
        inv.validate_repo(view["repo"])
        report = inv.Investigation.model_validate({
            "question": view["question"], "verdict": "inconclusive" if view["verdict"] == "no_structured_verdict" else view["verdict"],
            "ledger": view["ledger"], "summary": view["summary"],
            "limitations": view.get("limitations", []),
        })
        if view.get("status") != "monitoring":
            raise ValueError("feed must contain automatic investigations")
        # Validate the whole shape consumed by both renderers, including details.
        for key in ("recorded_at", "critique_status", "stop_reason"):
            if not isinstance(view[key], str):
                raise ValueError("invalid result metadata")
        for key in ("steps_taken", "estimated_cost_usd", "elapsed_seconds"):
            if not isinstance(view[key], (int, float)) or view[key] < 0:
                raise ValueError("invalid result measurements")
        if not isinstance(view["tool_calls"], list) or not isinstance(view["critique_issues"], list):
            raise ValueError("invalid trace")
        for call in view["tool_calls"]:
            inv.ToolCall.model_validate(call)
        view.setdefault("blockers", report.blockers)
        view.setdefault("critique_grounded", None)
        view.setdefault("revised", False)
    discoveries = data.get("discoveries", [])
    if not isinstance(discoveries, list) or len(discoveries) > 10:
        raise ValueError("invalid discovery list")
    if discoveries and not isinstance(data.get("discovery_week"), str):
        raise ValueError("discovery week missing")
    for item in discoveries:
        inv.validate_repo(item["repo"])
        if item.get("status") != "not_investigated" or not isinstance(item.get("repository_description"), str):
            raise ValueError("invalid discovery")
    last = data.get("last_run")
    if last is not None:
        if last.get("status") != "completed" or not isinstance(last["investigated"], int):
            raise ValueError("invalid monitoring run")
        datetime.fromisoformat(last["finished_at"])
    return data


def fetch_public_feed() -> tuple[dict | None, str]:
    """Read only the fixed public URL; no credentials or user input are sent."""
    try:
        with httpx.stream("GET", PUBLIC_FEED_URL, timeout=5.0, follow_redirects=False) as response:
            response.raise_for_status()
            payload = bytearray()
            for chunk in response.iter_bytes():
                payload.extend(chunk)
                if len(payload) > 2_000_000:
                    raise ValueError("monitoring feed too large")
        return validate_feed(json.loads(payload)), ""
    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
        return None, "Published monitoring feed could not be loaded."


def merge_runs(local: list[dict], published: list[dict]) -> list[dict]:
    merged = {}
    for view in sorted(published + local, key=lambda v: v["recorded_at"]):
        merged[(view["repo"], view["question"])] = view
    return sorted(merged.values(), key=lambda v: v["recorded_at"], reverse=True)[:6]


def build_feed(path: Path, last_run: dict | None) -> dict:
    views, error = load_local_runs(path)
    if error:
        raise ValueError(error)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "last_run": last_run,
        "investigations": views,
        **load_weekly_discoveries(path, last_run.get("week") if last_run else None),
    }
