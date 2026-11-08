"""The curated front page must preserve access to unfiltered evidence."""
import json
from pathlib import Path

import pytest

from ai_monitor import showcase
from tests.test_export_traces import HOSTILE, Parsed


def test_selected_home_is_curated_and_original_inconclusive_report_remains_available(tmp_path):
    showcase.export_showcase(tmp_path)
    home = (tmp_path / 'index.html').read_text()
    assert 'microsoft/markitdown' in home and 'pydantic/pydantic-ai' in home
    assert 'langwatch/langwatch' not in home and 'mlflow/mlflow' not in home
    assert 'href="monitoring.html"' in home
    previous = (tmp_path / 'findings/markitdown-docx-2026-11-07-previous.html').read_text()
    assert 'inconclusive' in previous and 'Original investigation' in previous
    assert 'Public trace redactions' in previous
    detail = (tmp_path / 'findings/markitdown-docx-2026-11-07.html').read_text()
    assert 'markitdown-docx-2026-11-07-previous.html' in detail
    assert '# My Test Heading' in detail and 'Alpha' in detail
    assert '\n| --- | --- |\n' in detail  # Recorded Markdown keeps its structure.


@pytest.mark.parametrize('change', ['inconclusive', 'unreviewed', 'traversal'])
def test_unreviewed_or_unresolved_results_cannot_be_featured(tmp_path, change):
    finding, record = showcase.load_findings()[0]
    entry = finding.model_dump()
    if change == 'inconclusive':
        record['report']['verdict'] = 'inconclusive'
    elif change == 'unreviewed':
        entry['manually_reviewed'] = False
    else:
        entry['source'] = '../outside.json'
    (tmp_path / finding.source).write_text(json.dumps(record))
    (tmp_path / 'findings.json').write_text(json.dumps({'schema_version': 1, 'findings': [entry]}))
    with pytest.raises(ValueError):
        showcase.load_findings(tmp_path)


def test_finding_and_original_trace_escape_repository_controlled_text():
    finding, record = showcase.load_findings()[0]
    finding.title = HOSTILE
    finding.summary = HOSTILE
    record['report']['summary'] = HOSTILE
    record['tool_calls'][0]['result_text'] = HOSTILE
    parsed = Parsed()
    parsed.feed(showcase.render_finding(finding, record))
    assert 'script' not in parsed.tags and 'img' not in parsed.tags
    assert 'iframe' not in parsed.tags
    assert HOSTILE in ''.join(parsed.text)
