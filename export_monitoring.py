"""Publish automatic investigation results without overwriting historical traces."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ai_monitor.monitoring_feed import build_feed, load_local_runs
from ai_monitor.storage.db import DEFAULT_DB_PATH
from export_traces import page, safe
from ai_monitor.showcase import export_showcase

LINK = '<p id="weekly-monitoring-link"><a href="index.html">&larr; Proofrun showcase</a></p>'
OLD_LINK = '<p id="weekly-monitoring-link"><a href="monitoring.html">Weekly monitoring: latest repository investigations &rarr;</a></p>'
OLD_TRACE_BACK = 'href="index.html">&larr; all investigations'
NEW_TRACE_BACK = 'href="history.html">&larr; all investigations'


def link_to_monitoring(path: Path) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    previous_home = '<p id="weekly-monitoring-link"><a href="index.html">&larr; Weekly monitoring</a></p>'
    if previous_home in text:
        path.write_text(text.replace(previous_home, LINK, 1), encoding="utf-8")
        return
    if OLD_LINK in text:
        path.write_text(text.replace(OLD_LINK, LINK, 1), encoding="utf-8")
    elif 'id="weekly-monitoring-link"' not in text:
        text = text.replace('<div class="wrap">', '<div class="wrap">\n' + LINK, 1)
        path.write_text(text, encoding="utf-8")


def relink_historical_traces(out: Path) -> None:
    for path in out.glob("*.html"):
        if path.name in {"index.html", "history.html", "monitoring.html"}:
            continue
        text = path.read_text(encoding="utf-8")
        if OLD_TRACE_BACK in text:
            path.write_text(text.replace(OLD_TRACE_BACK, NEW_TRACE_BACK, 1), encoding="utf-8")


VERDICTS = {
    "supported": "Supported", "refuted": "Refuted", "inconclusive": "Inconclusive",
    "could_not_test": "Could not test", "no_structured_verdict": "No conclusion",
}


def result_body(view: dict) -> str:
    answer = ("The evidence checks did not support a firm conclusion. See the original report below."
              if view["downgraded"] else view["summary"])
    review = view.get("final_review") or {
        "status": "not_run" if view.get("revised") else view["critique_status"],
        "grounded": None if view.get("revised") else view.get("critique_grounded"),
        "issues": [] if view.get("revised") else view.get("critique_issues", []),
    }
    body = f'<p class="q">{safe(view["question"])}</p><h4>What we found</h4><p>{safe(answer)}</p>'
    if review["status"] == "ok" and review["grounded"] and not review["issues"]:
        body += '<p class="meta">Final automated grounding check passed. This is a review of the evidence, not independent replication.</p>'
    else:
        label = "Final evidence review has unresolved issues." if review["status"] == "ok" else "Final evidence review is unavailable."
        body += f'<p class="notice">{label} Treat the conclusion as provisional.</p>'
    body += '<div class="finding-columns"><section><h4>What we checked</h4><ul>'
    for entry in view["ledger"][:4]:
        kind = {"observed": "Test result", "inspected": "Repository check", "reported": "Source claim"}.get(entry["kind"], "Finding")
        body += f'<li><strong>{kind}:</strong> {safe(entry["statement"])}</li>'
    if not view["ledger"]:
        body += '<li>No usable evidence was recorded.</li>'
    body += '</ul></section><section><h4>What remains unknown</h4><ul>'
    for limit in view["limitations"] or ["The result addresses only this question. Other capabilities were not established."]:
        body += f'<li>{safe(limit)}</li>'
    for blocker in view.get("blockers", []):
        body += f'<li>Blocker: {safe(blocker.replace("_", " "))}</li>'
    body += '</ul></section></div><details><summary>Full report, sources, and execution details</summary>'
    body += f'<p>Recorded verdict: {safe(view["verdict"])}</p><p>{safe(view["summary"])}</p>'
    for entry in view["ledger"]:
        body += f'<p><strong>{safe(entry["kind"])}</strong> · {safe(entry["statement"])}<br><code>{safe(entry["source"])}</code></p>'
    for call in view["tool_calls"]:
        body += f'<details><summary>Step {safe(call["step"])}: {safe(call["tool"])}</summary><pre>{safe(json.dumps(call["arguments"]), 3000)}</pre>'
        result = call.get("result_text") or call["result_summary"]
        body += f'<pre>{safe(result, 20000)}</pre></details>'
    for issue in view["critique_issues"]:
        body += f'<p>Draft review: {safe(issue)}</p>'
    for issue in review["issues"]:
        body += f'<p>Final review: {safe(issue)}</p>'
    return body + '</details>'


def render(feed: dict, *, has_history: bool = False) -> str:
    views = feed["investigations"]
    body = '<!-- proofrun-weekly-landing --><header class="masthead"><a href="index.html"><strong>Proofrun</strong></a><nav aria-label="Main navigation"><a href="index.html">Findings</a><a href="#findings">All monitoring results</a><a href="#discoveries">Discoveries</a>'
    if has_history:
        body += '<a href="history.html">Historical benchmark traces</a>'
    body += '<a href="https://github.com/Ashvanth11/proofrun">Source repository</a></nav></header>'
    body += '<main><div class="hero"><p class="eyebrow">Operational history</p><h1>The run archive.</h1><p>Recent saved monitoring results, including inconclusive and blocked investigations. The homepage presents detailed case studies and their recorded evidence.</p></div>'
    executed = sum(any(c["tool"] == "sandbox_run" and not c.get("is_error") for c in v["tool_calls"]) for v in views)
    firm = sum(v["verdict"] in {"supported", "refuted"} for v in views)
    body += f'<div class="stats" aria-label="Recent saved findings"><div><strong>{len(views)}</strong><span>recent investigations</span></div><div><strong>{executed}</strong><span>with sandbox runs</span></div><div><strong>{firm}</strong><span>conclusive verdicts</span></div></div>'
    last = feed.get("last_run")
    if last:
        body += f'<p class="meta">Last completed: {safe(last["finished_at"][:10])} · {safe(last["investigated"])} new investigation(s). Individual findings retain their original dates.</p>'
    else:
        body += '<p class="meta">No completed weekly run is recorded.</p>'
    attempt = feed.get("latest_attempt")
    if attempt and attempt["status"] != "completed":
        body += f'<p class="notice">Latest attempt: {safe(attempt["week"])} · {safe(attempt["status"])}. No completed result from this attempt is available.</p>'
    body += '<h2 id="findings">Recent findings</h2><p class="legend">A test result comes from execution; a repository check comes from metadata; a source claim is something its author reports. A blocked or inconclusive finding leaves the claim unresolved.</p>'
    if not views:
        body += '<p>No automatic investigation results have been published yet. Historical direct-question investigations remain available in the showcase.</p>'
    for index, view in enumerate(views):
        verdict = view["verdict"]
        badge = f'<span class="badge {safe(verdict)}">{safe(VERDICTS.get(verdict, verdict))}</span>'
        body += f'<article class="card result"><div class="result-head"><h3><a href="https://github.com/{safe(view["repo"])}">{safe(view["repo"])}</a></h3>{badge}</div>'
        body += f'<p>{safe(view.get("repository_description", ""))}</p><p class="meta">Investigated {safe(view["recorded_at"])}</p>'
        if index:
            body += '<details><summary>Read question, findings, and evidence</summary>'
        body += result_body(view)
        if index:
            body += '</details>'
        body += '</article>'
    body += '<h2 id="discoveries">Also discovered</h2>'
    if feed.get("discovery_week"):
        body += f'<p class="meta">Found during {safe(feed["discovery_week"])}. Project descriptions are not verified findings.</p><div class="discovery-grid">'
        for item in feed.get("discoveries", []):
            body += f'<article class="card"><h3>{safe(item["repo"])}</h3><span class="badge">Not investigated</span><p>{safe(item["repository_description"] or "No project description was supplied.")}</p><a href="https://github.com/{safe(item["repo"])}">View repository &rarr;</a></article>'
        body += '</div>'
        if not feed.get("discoveries"):
            body += '<p>No additional uninvestigated repositories in this week’s saved batch.</p>'
    else:
        body += '<p>The next completed weekly run will list its other discovered repositories here.</p>'
    body += '</main><footer class="footer">Scheduled weekly. Direct questions run in the local Streamlit app. <a href="https://github.com/Ashvanth11/proofrun#setup">Local app setup</a> · <a href="https://github.com/Ashvanth11/proofrun#screenshot-walkthrough">Screenshot walkthrough</a><p>Checks link findings to recorded evidence; they do not prove every statement correct.</p></footer>'
    return page("Proofrun — weekly monitoring", body)


def export(db_path: Path, out: Path, last_run: dict | None = None) -> dict:
    feed = build_feed(db_path, last_run)
    out.mkdir(parents=True, exist_ok=True)
    index = out / "index.html"
    history = out / "history.html"
    if not history.exists() and index.exists():
        previous = index.read_text(encoding="utf-8")
        if "proofrun-weekly-landing" not in previous:
            history.write_text(previous, encoding="utf-8")
    if history.exists():
        link_to_monitoring(history)
        relink_historical_traces(out)
    archive_views, error = load_local_runs(db_path, limit=None)
    if error:
        raise ValueError(error)
    landing = render({**feed, "investigations": archive_views}, has_history=history.exists())
    (out / "monitoring.json").write_text(json.dumps(feed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "monitoring.html").write_text(landing, encoding="utf-8")
    export_showcase(out)
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
