"""Curated portfolio export. The complete monitoring feed remains unfiltered."""
from __future__ import annotations

import json
import re
import html
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from ai_monitor.agent.investigate import Investigation, validate_repo
from ai_monitor.agent.loop import ToolCall
from export_traces import page, safe
from ai_monitor.agent.investigate_runner import _CONTROL

SHOWCASE_DIR = Path(__file__).resolve().parents[1] / 'showcase'


def pre_text(value, limit=30000):
    """Escape executable text while retaining meaningful command/output layout."""
    text = _CONTROL.sub('', str(value))
    return html.escape(text[:limit]) + ('\n[Display truncated; full evidence is in the JSON download.]' if len(text) > limit else '')


class Finding(BaseModel):
    slug: str
    repo: str
    date: str
    title: str
    summary: str
    method: str
    checks: list[str]
    limitations: list[str]
    review_note: str
    estimated_cost_usd: float = Field(ge=0)
    source: str
    previous_source: str | None = None
    manually_reviewed: bool

    @field_validator('slug')
    @classmethod
    def safe_slug(cls, value):
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', value):
            raise ValueError('invalid finding slug')
        return value

    @field_validator('source', 'previous_source')
    @classmethod
    def safe_source(cls, value):
        if value is not None and not re.fullmatch(r'[a-z0-9-]+\.json', value):
            raise ValueError('invalid source filename')
        return value

    @field_validator('repo')
    @classmethod
    def safe_repo(cls, value):
        validate_repo(value)
        return value


def load_findings(root: Path = SHOWCASE_DIR) -> list[tuple[Finding, dict]]:
    manifest = root / 'findings.json'
    if not manifest.exists():
        return []
    data = json.loads(manifest.read_text())
    if data.get('schema_version') != 1:
        raise ValueError('unsupported showcase manifest')
    findings = []
    seen = set()
    for entry in data['findings']:
        finding = Finding.model_validate(entry)
        record = json.loads((root / finding.source).read_text())
        report = Investigation.model_validate(record['report'])
        if not finding.manually_reviewed or report.verdict not in {'supported', 'refuted'} or record.get('downgraded'):
            raise ValueError('showcase requires a reviewed conclusive finding')
        if record['repo'] != finding.repo or finding.slug in seen:
            raise ValueError('finding/source mismatch or duplicate slug')
        for call in record['tool_calls']:
            ToolCall.model_validate(call)
        seen.add(finding.slug)
        findings.append((finding, record))
    return findings


STYLES = """
.portfolio { --bg: #f8faf8; --fg: #172d30; --muted: #536765; --line: #d7e2dc; --card: #fff; --rep-bg: #edf3ed; --accent: #126c60; background: #f8faf8; color: #172d30; padding-top: 0; }
.portfolio .wrap { max-width: 72rem; }
.portfolio a { color: #126c60; text-underline-offset: .2em; }
.portfolio .site-nav { display: flex; align-items: center; justify-content: space-between; padding: 1.5rem 0; gap: 1.5rem; border-bottom: 1px solid #d8e2de; }
.portfolio .brand { display: flex; align-items: center; gap: .6rem; font-size: 1.2rem; font-weight: 750; letter-spacing: -.04em; color: #172d30; text-decoration: none; }
.portfolio .brand-mark { background: #163d39; color: #c8efdf; display: grid; place-items: center; width: 2rem; height: 2rem; border-radius: .5rem; font: 700 .9rem ui-monospace, monospace; }
.portfolio nav { display: flex; gap: 1.4rem; flex-wrap: wrap; font-size: .85rem; }
.portfolio nav a { text-decoration: none; color: #455c5a; }
.portfolio .intro { display: grid; grid-template-columns: 1.2fr 1fr; gap: 4rem; align-items: center; padding: 5rem 0 4rem; }
.portfolio .overline { color: #126c60; font-size: .7rem; font-weight: 700; text-transform: uppercase; letter-spacing: .14em; }
.portfolio h1 { font-size: clamp(2.8rem, 5.8vw, 4.8rem); line-height: 1.04; letter-spacing: -.055em; margin: 1rem 0 1.4rem; }
.portfolio h1 em { color: #25756b; font-family: Georgia, serif; font-weight: 400; letter-spacing: -.05em; }
.portfolio .intro-copy { font-size: 1.08rem; line-height: 1.7; color: #536765; max-width: 32rem; }
.portfolio .actions { display: flex; flex-wrap: wrap; align-items: center; gap: 1.25rem; margin-top: 2rem; }
.portfolio .primary { display: inline-block; padding: .8rem 1.15rem; background: #183f3a; border-radius: .45rem; color: #fff; text-decoration: none; font-size: .85rem; font-weight: 650; }
.portfolio .secondary { font-size: .85rem; text-decoration: none; }
.portfolio .console { border-radius: .85rem; overflow: hidden; background: #142c2b; color: #d3e5dc; box-shadow: 0 18px 45px #183f3a16; border: 1px solid #38544e; transform: rotate(1deg); }
.portfolio .console-bar { padding: .8rem 1.15rem; background: #1c3734; display: flex; justify-content: space-between; font: .68rem ui-monospace, monospace; color: #b4cbc1; }
.portfolio .console-content { padding: 1.4rem; font: .78rem/1.8 ui-monospace, monospace; }
.portfolio .console-label { color: #8bb4a4; text-transform: uppercase; font-size: .65rem; letter-spacing: .09em; }
.portfolio .console-row { padding: 1rem 0; border-bottom: 1px solid #35504a; }
.portfolio .console-row:last-child { border: 0; }
.portfolio .console-output { color: #b5f1ce; overflow-wrap: anywhere; }
.portfolio .console-caption { color: #96b3a8; font-size: .68rem; margin-top: 1rem; }
.portfolio .stackline { display: flex; justify-content: space-between; gap: 1rem; align-items: center; flex-wrap: wrap; padding: 1.15rem 0; border-block: 1px solid #d8e2de; color: #657772; font-size: .75rem; }
.portfolio .stackline span { display: inline-block; margin-right: 1rem; }
.portfolio .section-top { display: flex; justify-content: space-between; gap: 2rem; align-items: end; margin-top: 4.5rem; margin-bottom: 1.5rem; }
.portfolio h2 { color: #172d30; font-size: 1.75rem; text-transform: none; letter-spacing: -.04em; margin: .5rem 0; }
.portfolio .section-note { max-width: 25rem; color: #657772; font-size: .8rem; margin: 0; }
.portfolio .finding { background: #fff; border: 1px solid #d7e2dc; border-radius: .75rem; padding: 1.8rem 2rem; margin: 1rem 0; }
.portfolio .finding-top { display: flex; gap: 1rem; align-items: center; justify-content: space-between; flex-wrap: wrap; }
.portfolio .repo-name { color: #657772; font: .74rem ui-monospace, monospace; }
.portfolio .finding h3 { font-size: 1.5rem; letter-spacing: -.04em; margin: 1.1rem 0 .6rem; line-height: 1.25; }
.portfolio .finding-summary { color: #536765; max-width: 51rem; font-size: .95rem; }
.portfolio .status { font-size: .7rem; background: #e5f3e9; color: #276245; border: 1px solid #c9e4d1; border-radius: 2rem; padding: .25rem .7rem; font-weight: 650; }
.portfolio .proof-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin: 1.5rem 0; }
.portfolio .proof-item { background: #f4f7f4; border-radius: .4rem; padding: .85rem 1rem; font-size: .83rem; }
.portfolio .proof-item span { display: block; font-size: .65rem; text-transform: uppercase; letter-spacing: .08em; color: #6d7f75; margin-bottom: .3rem; }
.portfolio .finding-footer { border-top: 1px solid #e6ece7; padding-top: 1rem; display: flex; justify-content: space-between; gap: 1rem; flex-wrap: wrap; font-size: .75rem; color: #657772; }
.portfolio .finding-footer a { font-weight: 650; }
.portfolio .pipeline { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1.5rem; margin-top: 1.5rem; }
.portfolio .pipeline article { border-top: 2px solid #bdd8cb; padding-top: 1.2rem; }
.portfolio .pipeline b { color: #6f9381; font: .75rem ui-monospace, monospace; }
.portfolio .pipeline h3 { font-size: 1rem; margin: 1rem 0 .5rem; }
.portfolio .pipeline p { font-size: .85rem; line-height: 1.7; color: #657772; }
.portfolio .archive-link { display: flex; justify-content: space-between; gap: 2rem; align-items: center; border-block: 1px solid #d8e2de; padding: 1.4rem 0; margin-top: 4rem; font-size: .85rem; }
.portfolio .archive-link p { color: #657772; max-width: 42rem; margin: 0; }
.portfolio footer { display: flex; justify-content: space-between; gap: 1rem; flex-wrap: wrap; padding: 1.6rem 0 0; color: #6c7e76; font-size: .75rem; }
.portfolio .detail-intro { max-width: 50rem; padding: 3rem 0 1rem; }
.portfolio .detail-intro h1 { font-size: clamp(2.2rem, 5vw, 3.5rem); }
.portfolio .scope { margin: 2rem 0; padding: 1rem 1.4rem; border-left: 3px solid #94beaa; background: #edf4ee; }
.portfolio .scope h2 { font-size: 1.1rem; }
.portfolio .evidence-output { background: #142c2b; color: #c9ead7; padding: 1.2rem; border-radius: .4rem; }
.portfolio details { margin: 1rem 0; }
.portfolio .review-note { color: #657772; font-size: .85rem; }
.portfolio .trace { border-top: 1px solid #d8e2de; padding: 1rem 0; }
.portfolio .skip { position: absolute; left: -10000px; }
.portfolio .skip:focus { position: static; }
@media (prefers-color-scheme: dark) {
 .portfolio { color-scheme: light; }
 .portfolio pre:not(.evidence-output) { background: #edf3ed; color: #172d30; }
 .portfolio .badge { background: #e8f0e9; color: #276245; }
}
@media (max-width: 760px) {
 .portfolio .intro { grid-template-columns: 1fr; gap: 2rem; padding: 2.8rem 0; }
 .portfolio .console { transform: none; }
 .portfolio .section-top { flex-direction: column; align-items: flex-start; gap: .5rem; margin-top: 3rem; }
 .portfolio .pipeline { grid-template-columns: 1fr 1fr; }
 .portfolio .finding { padding: 1.4rem; }
 .portfolio .site-nav { align-items: flex-start; }
 .portfolio nav { gap: .6rem 1rem; justify-content: flex-end; }
 .portfolio .archive-link { flex-direction: column; align-items: flex-start; gap: .8rem; }
}
@media (max-width: 420px) {
 .portfolio .proof-grid { grid-template-columns: 1fr; }
 .portfolio .site-nav { flex-direction: column; gap: 1rem; }
 .portfolio .pipeline { grid-template-columns: 1fr; gap: 1rem; }
}
"""


def shell(title: str, body: str, *, nested=False) -> str:
    prefix = '../' if nested else ''
    nav = f'''<a class="skip" href="#content">Skip to content</a><header class="site-nav"><a class="brand" href="{prefix}index.html"><span class="brand-mark" aria-hidden="true">p/</span>Proofrun</a><nav aria-label="Main navigation"><a href="{prefix}index.html#selected-findings">Findings</a><a href="{prefix}index.html#method">How it works</a><a href="{prefix}monitoring.html">Run archive</a><a href="https://github.com/Ashvanth11/proofrun">GitHub ↗</a></nav></header>'''
    footer = '<footer><span>Proofrun · Repository claims, checked with evidence.</span><a href="https://github.com/Ashvanth11/proofrun">Source code &amp; documentation ↗</a></footer>'
    rendered = page(title, nav + '<main id="content">' + body + '</main>' + footer).replace('<body>', '<body class="portfolio">').replace('</style>', STYLES + '</style>')
    return rendered.replace('</head>', '<meta name="description" content="Proofrun investigates AI repository claims with isolated experiments, recorded outputs, and evidence review. Explore findings and their test traces.">\n</head>')


def render_home(findings: list[tuple[Finding, dict]]) -> str:
    body = '<!-- proofrun-weekly-landing --><!-- proofrun-curated-showcase -->'
    body += '<section class="intro"><div><p class="overline">An agent for testing AI tools</p><h1>AI claims.<br><em>Put to the test.</em></h1><p class="intro-copy">An agent that inspects repositories, runs isolated experiments, and explains exactly what the evidence supports.</p><div class="actions"><a class="primary" href="#selected-findings">Explore the findings ↓</a><a class="secondary" href="https://github.com/Ashvanth11/proofrun">Explore the code ↗</a></div></div>'
    if findings:
        item, record = next(((f, r) for f, r in findings if f.repo == 'pydantic/pydantic-ai'), findings[0])
        outputs = []
        for call in record['tool_calls']:
            if call['tool'] == 'sandbox_run' and not call['is_error']:
                try:
                    result = json.loads(call.get('result_text') or '{}')
                except ValueError:
                    continue
                if result.get('stdout'):
                    outputs.append(result['stdout'].strip())
        body += f'<aside class="console" aria-label="Recorded test output"><div class="console-bar"><span>proofrun / {safe(item.repo)}</span><span>● offline</span></div><div class="console-content"><div class="console-label">Observed in the execution sandbox</div>'
        for index, output in enumerate(outputs[:2]):
            label = ['Valid input → typed result', 'Invalid input → rejected as expected'][index] if item.repo == 'pydantic/pydantic-ai' else 'Recorded conversion output'
            body += f'<div class="console-row"><div class="console-label">{label}</div><div class="console-output">{safe(output, 400)}</div></div>'
        body += '<div class="console-caption">Actual command output. Full test inputs and limitations are linked below.</div></div></aside>'
    body += '</section><div class="stackline"><span>Built with Python · LangGraph · Docker · SQLite</span><span>Repository inspection → execution → evidence review</span></div>'
    body += '<section id="selected-findings"><div class="section-top"><div><p class="overline">Investigations</p><h2>Findings you can inspect.</h2></div><p class="section-note">Explore repository claims, the tests behind each conclusion, and what remains unverified.</p></div>'
    for finding, record in findings:
        verdict = Investigation.model_validate(record['report']).verdict
        body += f'<article class="finding"><div class="finding-top"><span class="repo-name">{safe(finding.repo)}</span><span class="status">{safe(verdict.title())} · reviewed scope</span></div><h3>{safe(finding.title)}</h3><p class="finding-summary">{safe(finding.summary)}</p><div class="proof-grid">'
        for index, check in enumerate(finding.checks[:2], 1):
            body += f'<div class="proof-item"><span>Check {index:02}</span>{safe(check)}</div>'
        body += f'</div><div class="finding-footer"><span>{safe(finding.date)} · {safe(finding.method)}</span><a href="findings/{safe(finding.slug)}.html">Read the experiment &amp; evidence →</a></div></article>'
    if not findings:
        body += '<p>Reviewed findings are being prepared. The run archive remains available.</p>'
    body += '</section><section id="method"><div class="section-top"><div><p class="overline">Under the hood</p><h2>A claim becomes an experiment.</h2></div><p class="section-note">Bounded investigation, isolated execution, and an evidence ledger make each conclusion inspectable.</p></div><div class="pipeline">'
    for n, title, description in [
        ('01', 'Find a testable claim', 'Read repository evidence and screen against the available runtime, resources, and credentials.'),
        ('02', 'Run in isolation', 'Use a resource-limited Docker sandbox. Setup can install dependencies; execution has no network.'),
        ('03', 'Keep the evidence', 'Retain commands, returned outputs, and evidence references so the report can be checked.'),
        ('04', 'Review the conclusion', 'Check whether each claim follows from the evidence. Keep untested scope explicit and preserve revisions.'),
    ]:
        body += f'<article><b>{n}</b><h3>{title}</h3><p>{description}</p></article>'
    body += '</div></section><section class="archive-link"><p>Explore the run archive for additional investigations, execution details, and historical results.</p><a href="monitoring.html">Browse the run archive →</a></section>'
    return shell('Proofrun — AI claims, put to the test', body)


def render_record(record: dict) -> str:
    report = Investigation.model_validate(record['report'])
    body = f'<p><strong>Stored verdict:</strong> {safe(report.verdict)} · {safe(record["created_at"])}</p><p>{safe(report.summary)}</p><h3>Evidence ledger</h3>'
    if record.get('redactions'):
        body = '<p class="notice">Public trace redactions: ' + ' '.join(safe(item) for item in record['redactions']) + '</p>' + body
    for entry in report.ledger:
        body += f'<p><strong>{safe(entry.kind)}</strong> · {safe(entry.statement)}<br><code>{safe(entry.source)}</code></p>'
    body += '<h3>Recorded limitations</h3><ul>'
    for limit in report.limitations:
        body += f'<li>{safe(limit)}</li>'
    body += '</ul>'
    body += '<h3>Tool trace</h3>'
    for call in record['tool_calls']:
        body += f'<details class="trace"><summary>{safe(call["tool"])}</summary><pre>{pre_text(json.dumps(call["arguments"], indent=2), 6000)}</pre><pre>{pre_text(call.get("result_text") or call["result_summary"])}</pre></details>'
    return body


def render_finding(finding: Finding, record: dict) -> str:
    body = f'<section class="detail-intro"><p class="overline">{safe(finding.repo)} · {safe(finding.date)}</p><h1>{safe(finding.title)}</h1><p class="intro-copy">{safe(finding.summary)}</p><p class="meta">{safe(finding.method)} · estimated token cost ${finding.estimated_cost_usd:.3f}</p></section><h2>What the experiment established</h2><ul>'
    for check in finding.checks:
        body += f'<li>{safe(check)}</li>'
    body += '</ul><h2>Recorded output</h2>'
    for call in record['tool_calls']:
        if call['tool'] == 'sandbox_run' and not call['is_error']:
            try:
                output = json.loads(call.get('result_text') or '{}').get('stdout', '')
            except ValueError:
                continue
            if output:
                body += f'<pre class="evidence-output">{pre_text(output, 12000)}</pre>'
    body += '<section class="scope"><h2>Scope of this finding</h2><ul>'
    for limitation in finding.limitations:
        body += f'<li>{safe(limitation)}</li>'
    body += f'</ul></section><p class="review-note">Presentation review: {safe(finding.review_note)}</p>'
    body += '<details><summary>Original automated report and tool trace — unedited</summary>' + render_record(record) + '</details>'
    body += f'<p><a href="{safe(finding.slug)}.json">Download the recorded evidence JSON</a> · <a href="https://github.com/{safe(finding.repo)}">Repository ↗</a></p>'
    if finding.previous_source:
        body += f'<p><a href="{safe(finding.slug)}-previous.html">Previous investigation and original outcome →</a></p>'
    return shell(f'Proofrun — {safe(finding.title)}', body, nested=True)


def export_showcase(out: Path, root: Path = SHOWCASE_DIR) -> None:
    findings = load_findings(root)
    (out / 'index.html').write_text(render_home(findings))
    evidence_dir = out / 'findings'
    evidence_dir.mkdir(exist_ok=True)
    for finding, record in findings:
        (evidence_dir / f'{finding.slug}.html').write_text(render_finding(finding, record))
        (evidence_dir / f'{finding.slug}.json').write_text(json.dumps(record, indent=2) + '\n')
        if finding.previous_source:
            previous = json.loads((root / finding.previous_source).read_text())
            previous_body = f'<section class="detail-intro"><p class="overline">Original investigation · preserved history</p><h1>{safe(previous["repo"])}</h1><p>{safe(previous["question"])}</p></section>' + render_record(previous)
            previous_body += f'<p><a href="{safe(finding.slug)}-previous.json">Download the original recorded evidence</a></p>'
            (evidence_dir / f'{finding.slug}-previous.html').write_text(shell('Proofrun — preserved original', previous_body, nested=True))
            (evidence_dir / f'{finding.slug}-previous.json').write_text(json.dumps(previous, indent=2) + '\n')
