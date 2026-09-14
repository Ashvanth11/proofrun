#!/usr/bin/env python
"""Export stored investigations as static HTML anyone can open without cloning.

A reviewer should be able to read one investigation end to end - the question,
every tool call including the ones that failed, the ledger with each entry's
kind, the verdict and why it is that verdict - from a URL. That is what this
writes: one index and one page per investigation, no server, no API key, no
build step.

**Everything rendered here is attacker-controlled.** A README, a command's
stdout, a repository description and a web result all reach these pages, and
the repositories under investigation are chosen from public feeds. The markdown
batch report already treats this as a threat (`investigate_runner.clean`
escapes pipes so a README cannot forge a table row); HTML has a larger blast
radius, because text that reaches a browser unescaped is script.

So `safe()` below is the single choke point and every string passes through it.
It is deliberately *not* `investigate_runner.clean`: that function escapes
pipes for markdown and would hand `<script>` to the browser untouched. What is
reused is the part that generalises - stripping control characters and bounding
length - with `html.escape` in place of the markdown-specific quoting.

No JavaScript, no external stylesheet, no fonts, no images: a page that fetches
nothing renders identically from `file://` and from GitHub Pages, and cannot
leak a reader's visit to a third party. Tests assert the absence.
"""

import argparse
import html
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ai_monitor.agent.investigate_runner import _CONTROL
from ai_monitor.eval import investigations as ev
from ai_monitor.storage import db

log = logging.getLogger("ai_monitor.export")

SITE_DIR = Path(__file__).resolve().parent / "site"

# Display limits. The stored `result_summary` is already bounded at 1,000
# characters by `loop.SUMMARY_CHARS`; these bound what a page shows, so one
# pathological field cannot push the rest of a trace off the screen.
ARGS_CHARS = 300
SUMMARY_CHARS = 1000
STATEMENT_CHARS = 600
PROSE_CHARS = 2000


def safe(text: Any, limit: int = PROSE_CHARS) -> str:
    """The only way repo-derived text is allowed to reach a template.

    Control characters out, whitespace collapsed, length bounded, then HTML
    escaped. Truncation happens *before* escaping so the limit counts visible
    characters rather than entities, and so a cut can never land inside one.
    """
    out = _CONTROL.sub("", str(text if text is not None else ""))
    out = " ".join(out.split())
    if len(out) > limit:
        out = out[:limit] + "..."
    return html.escape(out, quote=True)


def slug(repo: str) -> str:
    """A filename from `owner/name` that cannot escape the output directory.

    The repository name reaches us from a model that has been reading hostile
    text all run, so this is allow-listed rather than sanitised: everything
    outside [a-z0-9] becomes a hyphen, which leaves no `/`, no `..`, no `~`.
    """
    out = re.sub(r"[^a-z0-9]+", "-", str(repo).lower()).strip("-")
    return out or "investigation"


CSS = """
:root {
  --bg: #fbfbfa; --fg: #1a1a19; --muted: #6b6b66; --line: #e0e0db;
  --card: #ffffff; --accent: #2a5d8f; --bad: #a33; --good: #2e6b3e;
  --obs: #1d4f2b; --obs-bg: #e6f2e9; --insp: #1f4d73; --insp-bg: #e6eff7;
  --rep: #6b6b66; --rep-bg: #f0f0ec;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16171a; --fg: #e6e6e3; --muted: #9a9a94; --line: #2c2e33;
    --card: #1d1f23; --accent: #7fb0e0; --bad: #e08585; --good: #7fc08f;
    --obs: #9fe0b0; --obs-bg: #1c3325; --insp: #9fc9ec; --insp-bg: #1a2c3d;
    --rep: #9a9a94; --rep-bg: #24262b;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1.25rem 4rem; background: var(--bg); color: var(--fg);
  font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
.wrap { max-width: 60rem; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; }
h2 {
  font-size: .8rem; text-transform: uppercase; letter-spacing: .09em;
  color: var(--muted); margin: 2.5rem 0 .75rem; font-weight: 600;
}
a { color: var(--accent); }
.sub { color: var(--muted); margin: 0 0 2rem; }
.back { display: inline-block; margin-bottom: 1.5rem; font-size: .85rem; }
code, pre, .mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: .82rem;
}
.scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table { border-collapse: collapse; width: 100%; font-size: .85rem; }
th, td {
  text-align: left; padding: .5rem .6rem; border-bottom: 1px solid var(--line);
  vertical-align: top;
}
th { color: var(--muted); font-weight: 600; white-space: nowrap; }
td.n, th.n { text-align: right; }
tr.row:hover td { background: var(--card); }
td a { text-decoration: none; }
td a:hover { text-decoration: underline; }
.yes { color: var(--good); font-weight: 600; }
.no { color: var(--bad); font-weight: 600; }
.card {
  background: var(--card); border: 1px solid var(--line); border-radius: 6px;
  padding: 1rem 1.1rem; margin-bottom: .6rem;
}
.q { font-size: 1.05rem; margin: 0 0 .5rem; }
.verdict {
  display: inline-block; padding: .3rem .7rem; border-radius: 4px;
  font-weight: 700; letter-spacing: .04em; background: var(--rep-bg);
}
.verdict.supported, .verdict.refuted { background: var(--obs-bg); color: var(--obs); }
.step {
  border-left: 3px solid var(--line); padding: .45rem 0 .45rem .8rem;
  margin-bottom: .5rem;
}
.step.err { border-left-color: var(--bad); }
.step .hd { font-weight: 600; }
.step .out { color: var(--muted); display: block; margin-top: .2rem; word-break: break-word; }
.tag {
  display: inline-block; min-width: 5.5rem; text-align: center;
  padding: .1rem .45rem; border-radius: 3px; font-size: .7rem;
  font-weight: 700; text-transform: uppercase; letter-spacing: .06em;
}
.tag.observed { background: var(--obs-bg); color: var(--obs); }
.tag.inspected { background: var(--insp-bg); color: var(--insp); }
.tag.reported { background: var(--rep-bg); color: var(--rep); }
.entry { padding: .5rem 0; border-bottom: 1px solid var(--line); }
.entry:last-child { border-bottom: 0; }
.entry .src { color: var(--muted); display: block; margin-top: .2rem; }
.side { color: var(--muted); font-size: .75rem; text-transform: uppercase; }
dl.facts { margin: 0; }
dl.facts dt { color: var(--muted); font-size: .75rem; text-transform: uppercase; }
dl.facts dd { margin: 0 0 .6rem; }
.legend { font-size: .8rem; color: var(--muted); margin-bottom: 1.5rem; }
.err-flag { color: var(--bad); font-weight: 700; font-size: .7rem; }
ul.issues { margin: .4rem 0 0; padding-left: 1.1rem; }
ul.issues li { margin-bottom: .4rem; }
"""


def page(title: str, body: str) -> str:
    """The skeleton. Note what is absent: no script, no link, no remote asset."""
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{title}</title>\n<style>{CSS}</style>\n</head>\n"
        f'<body>\n<div class="wrap">\n{body}\n</div>\n</body>\n</html>\n'
    )


def _loads(raw: Any, fallback: Any) -> Any:
    try:
        value = json.loads(raw) if raw else fallback
    except (TypeError, ValueError):
        return fallback
    return value if value is not None else fallback


# --- the index -----------------------------------------------------------


def render_index(results: list[Any], stats: dict, generated: str) -> str:
    head = (
        "<h1>Proofrun &mdash; investigation traces</h1>\n"
        f'<p class="sub">{stats["passed"]} of {stats["n"]} questions pass every '
        f'criterion &middot; ${stats["cost_usd"]:.2f} &middot; '
        f'{stats["wall_seconds"] / 60:.0f} min &middot; '
        f'{stats["observed"]} observed / {stats["inspected"]} inspected / '
        f'{stats["reported"]} reported<br>generated {safe(generated, 40)}</p>\n'
        '<p class="legend"><span class="tag observed">observed</span> a command '
        "ran in the sandbox and printed it &nbsp; "
        '<span class="tag inspected">inspected</span> a fact GitHub computed '
        "&nbsp; <span class="
        '"tag reported">reported</span> someone wrote it. A question passes '
        "only if every criterion holds.</p>\n"
    )

    rows = []
    for result in results:
        target = f"{slug(result.repo)}.html"
        failed = ", ".join(c.name for c in result.failures)
        mark = (
            '<span class="yes">pass</span>'
            if result.passed
            else f'<span class="no">fail</span> {safe(failed, 80)}'
        )
        rows.append(
            "<tr class='row'>"
            f'<td><a href="{safe(target, 120)}">{safe(result.question, 150)}</a></td>'
            f"<td>{safe(result.category, 30)}</td>"
            f"<td>{safe(result.verdict, 30)}</td>"
            f'<td class="n">{result.observed}</td>'
            f'<td class="n">{result.inspected}</td>'
            f'<td class="n">{result.reported}</td>'
            f'<td class="n">{result.sandbox_commands}</td>'
            f'<td class="n">${result.cost_usd:.3f}</td>'
            f"<td>{mark}</td>"
            "</tr>"
        )

    table = (
        '<div class="scroll"><table>\n<tr>'
        "<th>Question</th><th>Category</th><th>Verdict</th>"
        '<th class="n">Obs</th><th class="n">Insp</th><th class="n">Rep</th>'
        '<th class="n">Cmds</th><th class="n">Cost</th><th>Pass</th>'
        "</tr>\n" + "\n".join(rows) + "\n</table></div>\n"
    )
    return page("Proofrun &mdash; investigation traces", head + table)


# --- one investigation ---------------------------------------------------


def render_investigation(result: Any, row: Any) -> str:
    report = _loads(row["report"] if row is not None else None, {}) or {}
    calls = _loads(row["tool_calls"] if row is not None else None, [])
    blockers = _loads(row["blockers"] if row is not None else None, [])
    downgraded = bool(row["downgraded"]) if row is not None else False

    verdict = report.get("verdict") or result.verdict or "none"
    parts = [
        '<a class="back" href="index.html">&larr; all investigations</a>',
        f'<h1 class="q">{safe(result.question, 400)}</h1>',
        f'<p class="sub"><span class="mono">{safe(result.repo, 120)}</span>'
        f' &middot; {safe(result.category, 40)}</p>',
    ]

    # --- verdict band
    meta = [
        f"blockers: {safe(', '.join(blockers), 200) if blockers else 'none'}",
        "downgraded: no first-hand evidence" if downgraded else "not downgraded",
        f"stopped: {safe(row['stop_reason'] if row is not None else '-', 40)}",
        f"{result.steps} steps",
        f"${result.cost_usd:.3f}",
        f"{result.wall_seconds:.0f}s",
    ]
    failed = ", ".join(c.name for c in result.failures)
    scored = (
        '<span class="yes">passes every criterion</span>'
        if result.passed
        else f'<span class="no">fails</span> {safe(failed, 120)}'
    )
    parts += [
        "<h2>Verdict</h2>",
        f'<div class="card"><span class="verdict {safe(verdict, 30)}">'
        f"{safe(verdict, 30).upper()}</span>"
        f'<p class="sub" style="margin:.6rem 0 0">{" &middot; ".join(meta)}</p>'
        f"<p style='margin:.4rem 0 0'>{scored}</p></div>",
    ]

    if report.get("summary"):
        parts += [
            "<h2>Summary</h2>",
            f'<div class="card">{safe(report["summary"], PROSE_CHARS)}</div>',
        ]

    # --- trajectory: every call, errors shown rather than hidden
    parts.append("<h2>Trajectory</h2>")
    if not calls:
        parts.append('<div class="card">No tools were called.</div>')
    else:
        steps = []
        for call in calls:
            err = bool(call.get("is_error"))
            flag = ' <span class="err-flag">error</span>' if err else ""
            where = " [web]" if call.get("server") else ""
            steps.append(
                f'<div class="step{" err" if err else ""} mono">'
                f'<span class="hd">step {safe(call.get("step"), 8)} '
                f'{safe(call.get("tool"), 60)}</span>'
                f"({safe(call.get('arguments'), ARGS_CHARS)}){safe(where, 10)}{flag}"
                f'<span class="out">&rarr; '
                f'{safe(call.get("result_summary"), SUMMARY_CHARS)}</span></div>'
            )
        parts.append(f'<div class="card">{"".join(steps)}</div>')

    # --- ledger
    parts.append("<h2>Ledger</h2>")
    ledger = report.get("ledger") or []
    if not ledger:
        parts.append('<div class="card">The ledger is empty.</div>')
    else:
        entries = []
        for entry in ledger:
            kind = str(entry.get("kind") or "reported")
            css = kind if kind in {"observed", "inspected", "reported"} else "reported"
            entries.append(
                f'<div class="entry"><span class="tag {css}">{safe(kind, 20)}</span> '
                f'<span class="side">{safe(entry.get("side"), 20)}</span> '
                f'{safe(entry.get("statement"), STATEMENT_CHARS)}'
                f'<span class="src mono">from: '
                f'{safe(entry.get("source"), 200)}</span></div>'
            )
        parts.append(f'<div class="card">{"".join(entries)}</div>')

    # --- facts
    facts = report.get("facts") or {}
    if facts:
        needs = facts.get("needs") or []
        rows = [
            ("setup", f"{facts.get('setup_seconds', 0) or 0:.1f}s over "
                      f"{facts.get('setup_commands', 0) or 0} command(s), install "
                      f"{'succeeded' if facts.get('install_succeeded') else 'not run or failed'}"),
            ("disk", f"{facts.get('clone_mb', 0) or 0:.0f} MB cloned, "
                     f"{facts.get('volume_mb', 0) or 0:.0f} MB total"),
            ("needs", ", ".join(str(n) for n in needs) or "nothing special"),
            ("exercised", facts.get("headline_capability") or "-"),
        ]
        if facts.get("observed_output"):
            rows.append(("output", facts["observed_output"]))
        body = "".join(
            f"<dt>{safe(k, 40)}</dt><dd>{safe(v, PROSE_CHARS)}</dd>" for k, v in rows
        )
        parts += ["<h2>Facts</h2>", f'<div class="card"><dl class="facts">{body}</dl></div>']

    # --- critique
    status = row["critique_status"] if row is not None else "not_run"
    if status and status != "not_run":
        critique = _loads(row["critique"], {}) or {}
        grounded = critique.get("grounded")
        revised = bool(row["revised"]) if row is not None else False
        head = (
            f"ran &middot; {'grounded' if grounded else 'not grounded'} &middot; "
            f"{'revised' if revised else 'no revision'}"
        )
        issues = critique.get("issues") or []
        items = "".join(
            f"<li>{safe(issue, PROSE_CHARS)}</li>" for issue in issues
        )
        listing = f'<ul class="issues">{items}</ul>' if items else ""
        parts += [
            "<h2>Grounding critique</h2>",
            f'<div class="card"><p class="sub" style="margin:0">{head}</p>{listing}</div>',
        ]

    return page(safe(result.repo, 80) + " &mdash; Proofrun", "\n".join(parts))


# --- driver --------------------------------------------------------------


def export(
    conn: sqlite3.Connection,
    out_dir: Path = SITE_DIR,
    questions_path: Path = ev.QUESTIONS_PATH,
) -> list[Path]:
    """Write the index and one page per question. Returns what was written."""
    questions = ev.load_questions(questions_path)
    results = ev.score(conn, questions)
    stats = ev.summarize(results)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    out_dir.mkdir(parents=True, exist_ok=True)
    written = []

    for question, result in zip(questions, results):
        row = ev.latest_run(conn, question["repo"], question["question"])
        path = out_dir / f"{slug(result.repo)}.html"
        path.write_text(render_investigation(result, row), encoding="utf-8")
        written.append(path)

    index = out_dir / "index.html"
    index.write_text(render_index(results, stats, generated), encoding="utf-8")
    written.append(index)

    log.info(
        "wrote %d pages to %s (%d/%d passing)",
        len(written), out_dir, stats["passed"], stats["n"],
    )
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=SITE_DIR)
    parser.add_argument("--questions", type=Path, default=ev.QUESTIONS_PATH)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    conn = db.connect()
    try:
        written = export(conn, args.out, args.questions)
    except (ValueError, FileNotFoundError) as exc:
        log.error("%s", exc)
        return 1
    finally:
        conn.close()

    print(f"\n{len(written)} pages written to {args.out}")
    print(f"open {args.out / 'index.html'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
