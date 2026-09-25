"""Publish automatic investigation results without overwriting historical traces."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ai_monitor.monitoring_feed import build_feed
from ai_monitor.storage.db import DEFAULT_DB_PATH
from export_traces import page, safe

LINK = '<p id="weekly-monitoring-link"><a href="monitoring.html">Weekly monitoring: latest repository investigations &rarr;</a></p>'


def link_from_index(path: Path) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    if 'id="weekly-monitoring-link"' not in text:
        text = text.replace('<div class="wrap">', '<div class="wrap">\n' + LINK, 1)
        path.write_text(text, encoding="utf-8")


def render(feed: dict) -> str:
    body = '<a class="back" href="index.html">&larr; Proofrun showcase and historical evaluations</a>'
    body += '<h1>Proofrun weekly monitoring</h1><p>Discover AI repositories. Investigate their claims. See what the evidence supports.</p>'
    last = feed.get("last_run")
    if last:
        body += f'<p class="sub">Last completed: {safe(last["finished_at"])} &middot; {safe(last["investigated"])} new investigation(s). Scheduled weekly; individual findings retain their original dates.</p>'
    else:
        body += '<p class="sub">Weekly monitoring is being prepared. No scheduled run has completed yet.</p>'
    if not feed["investigations"]:
        body += '<p>No automatic investigation results have been published yet. Historical direct-question investigations remain available in the showcase.</p>'
    for view in feed["investigations"]:
        body += f'<article class="card"><h2>{safe(view["repo"])}</h2><p>{safe(view.get("repository_description", ""))}</p>'
        body += f'<p class="sub">Investigated {safe(view["recorded_at"])}</p><p class="q">{safe(view["question"])}</p>'
        answer = "The evidence checks did not support a firm conclusion. See the original report below." if view["downgraded"] else view["summary"]
        body += f'<h3>What we found</h3><p>{safe(answer)}</p><h3>What we checked</h3><ul>'
        for entry in view["ledger"][:4]:
            kind = {"observed": "Test result", "inspected": "Repository check", "reported": "Source claim"}.get(entry["kind"], "Finding")
            body += f'<li><strong>{kind}:</strong> {safe(entry["statement"])}</li>'
        body += '</ul><h3>What remains unknown</h3><ul>'
        for limit in view["limitations"] or ["The result addresses only the question above. Other capabilities were not established."]:
            body += f'<li>{safe(limit)}</li>'
        if view["critique_status"] == "failed":
            body += '<li>The automated evidence review did not complete; this conclusion is provisional.</li>'
        body += '</ul><details><summary>Full report, sources, and execution details</summary>'
        body += f'<p>Original verdict: {safe(view["verdict"])}</p><p>{safe(view["summary"])}</p>'
        for entry in view["ledger"]:
            body += f'<p>{safe(entry["statement"])}<br><code>{safe(entry["source"])}</code></p>'
        for call in view["tool_calls"]:
            body += f'<p>Step {safe(call["step"])}: <code>{safe(call["tool"])}</code></p><pre>{safe(json.dumps(call["arguments"]), 3000)}</pre><p>{safe(call["result_summary"], 3000)}</p>'
        for issue in view["critique_issues"]:
            body += f'<p>Evidence review: {safe(issue)}</p>'
        body += '</details></article>'
    body += '<h2>Also discovered</h2>'
    if feed.get("discovery_week"):
        body += f'<p class="sub">Found during {safe(feed["discovery_week"])}. Project descriptions are not verified findings.</p>'
        for item in feed.get("discoveries", []):
            body += f'<article class="card"><h3>{safe(item["repo"])}</h3><p class="sub">Not investigated</p>'
            body += f'<p>{safe(item["repository_description"] or "No project description was supplied.")}</p>'
            body += f'<a href="https://github.com/{safe(item["repo"])}">View repository &rarr;</a></article>'
        if not feed.get("discoveries"):
            body += '<p>No additional uninvestigated repositories in this week’s saved batch.</p>'
    else:
        body += '<p>The next completed weekly run will list its other discovered repositories here.</p>'
    return page("Proofrun — weekly monitoring", body)


def export(db_path: Path, out: Path, last_run: dict | None = None) -> dict:
    feed = build_feed(db_path, last_run)
    out.mkdir(parents=True, exist_ok=True)
    (out / "monitoring.json").write_text(json.dumps(feed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "monitoring.html").write_text(render(feed), encoding="utf-8")
    link_from_index(out / "index.html")
    return feed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--out", type=Path, default=Path("site"))
    parser.add_argument("--run-record", type=Path)
    args = parser.parse_args(argv)
    last = json.loads(args.run_record.read_text()) if args.run_record else None
    export(args.db, args.out, last)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
