import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_monitor.agent import investigate as inv
from ai_monitor.storage import db
from scripts import review_reruns as reruns
from tests.test_investigate_runner import make_run, seed


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_plan_and_execution_preserve_source_and_save_new_result(tmp_path, monkeypatch):
    source = tmp_path / 'source.db'
    conn = db.connect(source)
    original = make_run()
    run_id = inv.store_investigation(conn, original)
    conn.close()
    before = digest(source)
    plan, rows = reruns.read_plan(source, run_ids=[run_id])
    assert digest(source) == before
    assert json.loads(json.dumps(plan))['max_investigations'] == 1
    monkeypatch.setattr(reruns.settings, 'anthropic_api_key', 'test-only')
    monkeypatch.setattr(reruns.subprocess, 'run', lambda *a, **kw: None)
    monkeypatch.setattr(reruns.subprocess, 'check_output', lambda *a, **kw: 'test-revision')
    monkeypatch.setattr('anthropic.Anthropic', lambda **kw: SimpleNamespace())
    monkeypatch.setattr(reruns.inv, 'investigate', lambda *a, **kw: make_run(verdict='inconclusive'))
    from ai_monitor.analysis.analyzer import Usage
    monkeypatch.setattr(reruns.critique, 'critique_and_revise_investigation', lambda run, *a, **kw: (run, Usage()))
    out = tmp_path / 'rerun'
    reruns.execute(plan, rows, out)
    assert digest(source) == before
    assert json.loads((out / 'originals.json').read_text())[0]['verdict'] == 'supported'
    with sqlite3.connect(out / 'reruns.db') as conn:
        assert conn.execute('select verdict from investigations').fetchone()[0] == 'inconclusive'
    assert (out / 'completed.json').exists()
    with pytest.raises(FileExistsError):
        reruns.execute(plan, rows, out)


def test_recorded_client_retains_usage_when_parsed_output_is_missing(tmp_path):
    from pydantic import BaseModel
    class Response(BaseModel):
        parsed_output: None = None
        usage: dict
        stop_reason: str = 'max_tokens'
    response = SimpleNamespace(
        parsed_output=None, usage=SimpleNamespace(input_tokens=12, output_tokens=4),
        model_dump_json=lambda **kw: Response(usage={'input_tokens': 12, 'output_tokens': 4}).model_dump_json(**kw),
    )
    client = reruns.RecordedClient(SimpleNamespace(messages=SimpleNamespace(parse=lambda **kw: response)), tmp_path, inv.INVESTIGATE_MODEL)
    assert client.parse(output_format=inv.DerivedQuestion).parsed_output is None
    assert json.loads((tmp_path / 'usage.json').read_text())['input_tokens'] == 12
    assert client.calls == 1


def test_saved_week_plan_excludes_previously_investigated_repos(tmp_path):
    path = tmp_path / 'weekly.db'
    conn = db.connect(path)
    old = seed(conn, 'a/old', .9, investigated=True)
    new = seed(conn, 'a/new', .8)
    conn.execute('create table weekly_runs (week text, status text)')
    conn.execute('create table weekly_discoveries (week text, item_id integer)')
    conn.execute("insert into weekly_runs values ('2026-W45','failed')")
    conn.executemany("insert into weekly_discoveries values ('2026-W45',?)", [(old,), (new,)])
    conn.commit()
    conn.close()
    before = digest(path)
    plan, rows = reruns.read_plan(path, week='2026-W45')
    assert [r['source_id'] for r in rows] == ['a/new']
    assert plan['max_investigations'] == 1
    assert digest(path) == before


def test_raw_response_and_usage_survive_sdk_json_parse_failure(tmp_path):
    import anthropic
    import httpx
    payload = {
        'id': 'msg_offline', 'type': 'message', 'role': 'assistant',
        'model': inv.INVESTIGATE_MODEL, 'stop_reason': 'max_tokens', 'stop_sequence': None,
        'content': [{'type': 'text', 'text': '{"has_testable_claim": true, "claim": "unfinished'}],
        'usage': {'input_tokens': 123, 'output_tokens': 1024},
    }
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=payload)
    http_client = httpx.Client(transport=httpx.MockTransport(respond))
    sdk = anthropic.Anthropic(api_key='test-only', max_retries=0, http_client=http_client)
    client = reruns.RecordedClient(sdk, tmp_path, inv.INVESTIGATE_MODEL)
    http_client.event_hooks['response'].append(client.record_http_response)
    with pytest.raises(inv.ScreeningError) as caught:
        inv.derive_question(SimpleNamespace(source_id='owner/name', content='A real claim'), client)
    error = caught.value
    assert error.reason == 'invalid_structured_output'
    assert error.usage_available is True
    assert error.usage.input_tokens == 123
    assert error.stop_reason == 'max_tokens'
    assert len(requests) == 1
    assert requests[0]['max_tokens'] == inv.DERIVE_MAX_TOKENS
    assert json.loads((tmp_path / 'call-001.http-response.json').read_text()) == payload
    assert json.loads((tmp_path / 'usage.json').read_text())['output_tokens'] == 1024
    sdk.close()


def test_request_recorder_handles_sdk_thinking_and_tool_blocks(tmp_path):
    from anthropic.types import ThinkingBlock, ToolUseBlock
    blocks = [ThinkingBlock(type='thinking', thinking='Offline test', signature='test-signature'),
              ToolUseBlock(type='tool_use', id='tool_test', name='get_repo_metadata', input={'repo': 'a/b'})]
    request = {'messages': [{'role': 'assistant', 'content': blocks}]}
    normalized = json.loads(reruns.request_json(request))
    assert normalized['messages'][0]['content'][0]['signature'] == 'test-signature'
    assert normalized['messages'][0]['content'][1]['input'] == {'repo': 'a/b'}


def test_saved_response_replay_makes_no_live_call_and_refuses_changed_requests(tmp_path):
    old = tmp_path / 'old'
    old.mkdir()
    out = tmp_path / 'continued'
    out.mkdir()
    request = {'model': inv.INVESTIGATE_MODEL, 'max_tokens': 4096,
               'messages': [{'role': 'user', 'content': 'Original question'}]}
    payload = {'id': 'msg_saved', 'type': 'message', 'role': 'assistant',
               'model': inv.INVESTIGATE_MODEL, 'stop_reason': 'end_turn', 'stop_sequence': None,
               'content': [{'type': 'text', 'text': 'Saved answer'}],
               'usage': {'input_tokens': 12, 'output_tokens': 4}}
    (old / 'call-001.request.json').write_text(json.dumps(request))
    (old / 'call-001.http-response.json').write_text(json.dumps(payload))
    def forbidden(**kw):
        raise AssertionError('Replay must not call a model')
    client = reruns.RecordedReplayClient(SimpleNamespace(messages=SimpleNamespace(create=forbidden)), out, inv.INVESTIGATE_MODEL, old)
    response = client.create(**request)
    assert response.content[0].text == 'Saved answer'
    assert client.usage.input_tokens == 12
    assert not list(out.glob('*.http-response.json'))
    other = reruns.RecordedReplayClient(SimpleNamespace(), out, inv.INVESTIGATE_MODEL, old)
    with pytest.raises(RuntimeError, match='differs'):
        other.create(**{**request, 'max_tokens': 2048})
