"""Offline publication gates, single-attempt accounting, and artifact recovery."""
import io
import json
import sqlite3
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_monitor import publication as pub, showcase
from ai_monitor.agent.critique import ClaimCheck
from ai_monitor.storage import db
from ai_monitor.storage.models import Item, Source
from scripts.restore_monitor_state import restore_publication_state
import export_monitoring


@pytest.fixture
def case(tmp_path):
    source = json.loads((showcase.SHOWCASE_DIR / 'markitdown-2026-11-07.json').read_text())
    path = tmp_path / 'monitor.db'
    conn = db.connect(path)
    item_id = db.upsert_item(conn, Item(source=Source.GITHUB, source_id=source['repo'],
        title=source['repo'], url='https://github.com/' + source['repo']))
    columns = ['repo','question','verdict','created_at','revised','downgraded','steps_taken',
               'stop_reason','cost_usd','critique_status','report','tool_calls','critique']
    values = [json.dumps(source[key]) if key in {'report','tool_calls','critique'} else source[key] for key in columns]
    conn.execute('INSERT INTO investigations (item_id,' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in range(len(values)+1)) + ')', [item_id,*values])
    conn.execute('CREATE TABLE weekly_discoveries (week text,item_id integer,repo text,description text)')
    conn.execute('INSERT INTO weekly_discoveries VALUES (?,?,?,?)', ('2026-W45',item_id,source['repo'],'A document converter'))
    conn.commit();conn.close()
    date = datetime.fromisoformat(source['created_at'])
    run = {'week':'2026-W45','status':'completed','investigated':1,
           'started_at':(date-timedelta(minutes=1)).isoformat(),
           'finished_at':(date+timedelta(minutes=1)).isoformat()}
    record = pub.read_current_record(path, run)
    finding = showcase.load_findings()[0][0]
    draft = pub.DraftFinding.model_validate(finding.model_dump())
    ref = next(c['invocation_id'] for c in source['tool_calls'] if c['tool']=='sandbox_run')
    decision = pub.PublicationDecision(approved=True, actual_library_exercised=True, test_matches_claim=True,
        draft=draft, claim_checks=[ClaimCheck(statement=text,supported=True,evidence_refs=[ref],
            reason='Offline scripted reviewer decision for testing orchestration, not model accuracy.')
            for text in sorted(pub.copy_statements(draft))])
    return path,run,record,decision


class Client:
    def __init__(self, decision=None, error=None):
        self.messages=self;self.decision=decision;self.error=error;self.calls=[]
    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(parsed_output=self.decision,usage=SimpleNamespace(input_tokens=1000,output_tokens=500))


def test_approved_finding_survives_artifact_restore_without_rebuying_review(case,tmp_path):
    path,run,record,decision=case
    client=Client(decision)
    state=tmp_path/pub.STATE_NAME
    first=pub.review_for_publication(path,run,state,client)
    assert first['status']=='approved' and len(client.calls)==1
    assert pub.review_for_publication(path,run,state,client)==first
    assert len(client.calls)==1
    assert client.calls[0]['max_tokens']==pub.REVIEW_MAX_TOKENS
    archive_bytes=io.BytesIO()
    with zipfile.ZipFile(archive_bytes,'w') as archive:
        for file in state.iterdir():
            archive.writestr(f'{pub.STATE_NAME}/{file.name}',file.read_bytes())
        archive.writestr('publication-state/private-extra.json','do not restore')
    restored=tmp_path/'restored';restored.mkdir()
    with zipfile.ZipFile(io.BytesIO(archive_bytes.getvalue())) as archive:
        restore_publication_state(archive,restored)
    assert not (restored/pub.STATE_NAME/'private-extra.json').exists()
    no_calls=Client(error=AssertionError('must not call a model'))
    assert pub.review_for_publication(path,run,restored/pub.STATE_NAME,no_calls)['status']=='approved'
    assert not no_calls.calls
    approved=showcase.load_findings(restored/pub.STATE_NAME,require_automatic=True)
    assert len(approved)==1 and not approved[0][0].manually_reviewed
    out=tmp_path/'site';out.mkdir()
    showcase.export_showcase(out,automatic_root=restored/pub.STATE_NAME)
    assert 'automated review' in (out/'index.html').read_text()
    assert (out/'findings'/f'{approved[0][0].slug}.json').exists()
    review=json.loads((out/'findings'/f'{approved[0][0].slug}-review.json').read_text())
    assert review['review']['decision']['approved'] is True
    assert 'Automated publication review' in (out/'findings'/f'{approved[0][0].slug}.html').read_text()


@pytest.mark.parametrize('failure',['unsupported','uncited','missing_copy','model_rejected','wrong_test'])
def test_unapproved_copy_never_enters_showcase(case,tmp_path,failure):
    path,run,record,decision=case
    if failure=='unsupported':decision.claim_checks[0].supported=False
    if failure=='uncited':decision.claim_checks[0].evidence_refs=['nonexistent-call']
    if failure=='missing_copy':decision.claim_checks.pop()
    if failure=='model_rejected':decision.approved=False
    if failure=='wrong_test':decision.test_matches_claim=False
    client=Client(decision)
    state=tmp_path/pub.STATE_NAME
    assert pub.review_for_publication(path,run,state,client)['status']=='rejected'
    assert not pub.load_state(state)['findings']
    pub.review_for_publication(path,run,state,client)
    assert len(client.calls)==1


@pytest.mark.parametrize('failure',['provider','missing','interrupted'])
def test_failure_or_interruption_cannot_retry_a_paid_review(case,tmp_path,failure):
    path,run,record,decision=case
    error = ValueError('truncated response') if failure=='provider' else KeyboardInterrupt() if failure=='interrupted' else None
    client=Client(None,error)
    state=tmp_path/pub.STATE_NAME
    if failure=='interrupted':
        with pytest.raises(KeyboardInterrupt):pub.review_for_publication(path,run,state,client)
    else:
        pub.review_for_publication(path,run,state,client)
    second=Client(error=AssertionError('no retry allowed'))
    result=pub.review_for_publication(path,run,state,second)
    assert result['status'] in {'failed','rejected','started'}
    assert not second.calls


def test_only_new_completed_week_can_trigger_review(case,tmp_path):
    path,run,record,decision=case
    client=Client(error=AssertionError('must not call'))
    state=tmp_path/pub.STATE_NAME
    for change in [{'status':'failed'},{'investigated':0}]:
        assert pub.review_for_publication(path,{**run,**change},state,client)['status']=='skipped'
    with pytest.raises(ValueError,match='attribution'):
        pub.review_for_publication(path,{**run,'week':'2026-W46'},state,client)
    assert not client.calls


@pytest.mark.parametrize('problem',['inconclusive','missing_report','no_final_review','legacy_evidence','no_runtime'])
def test_noneligible_findings_are_skipped_without_model_cost(case,tmp_path,problem):
    path,run,record,decision=case
    conn=sqlite3.connect(path)
    if problem=='inconclusive':
        report=record['report'];report['verdict']='inconclusive'
        conn.execute("UPDATE investigations SET verdict='inconclusive',report=?",(json.dumps(report),))
    elif problem=='missing_report':conn.execute("UPDATE investigations SET report=NULL,verdict=NULL")
    elif problem=='no_final_review':conn.execute("UPDATE investigations SET critique='{}'")
    else:
        calls=record['tool_calls']
        if problem=='legacy_evidence':calls[0]['result_text']=None
        else:calls=[call for call in calls if call['tool']!='sandbox_run']
        conn.execute('UPDATE investigations SET tool_calls=?',(json.dumps(calls),))
    conn.commit();conn.close()
    client=Client(error=AssertionError('must not call'))
    assert pub.review_for_publication(path,run,tmp_path/pub.STATE_NAME,client)['status']=='skipped'
    assert not client.calls


def test_edited_copy_or_evidence_invalidates_automatic_approval(case,tmp_path):
    path,run,record,decision=case
    state=tmp_path/pub.STATE_NAME
    pub.review_for_publication(path,run,state,Client(decision))
    manifest=json.loads((state/'findings.json').read_text())
    finding=manifest['findings'][0]
    assert pub.automatic_approval_valid(finding,record)
    finding['summary']='An unreviewed broader claim'
    assert not pub.automatic_approval_valid(finding,record)
    finding['summary']=decision.draft.summary
    record['report']['summary']='Altered source evidence'
    assert not pub.automatic_approval_valid(finding,record)


def test_restore_rejects_path_traversal_and_accepts_old_artifacts(tmp_path):
    out=tmp_path/'restored';out.mkdir()
    for entries,raises in [({'monitor.db':'old database'},False),
        ({'publication-state/findings.json':json.dumps({'schema_version':1,'findings':[{'source':'../../escape'}],'reviews':{}})},True)]:
        data=io.BytesIO()
        with zipfile.ZipFile(data,'w') as archive:
            for name,body in entries.items():archive.writestr(name,body)
        with zipfile.ZipFile(io.BytesIO(data.getvalue())) as archive:
            if raises:
                with pytest.raises(ValueError,match='unsafe'):restore_publication_state(archive,out)
            else:restore_publication_state(archive,out)
    assert not (tmp_path/'escape').exists()


def test_checkpoint_restore_never_restores_permission_to_repeat_review(case,tmp_path):
    path,run,record,decision=case
    state=tmp_path/pub.STATE_NAME
    prepared=pub.prepare_publication(path,run,state)
    assert prepared['status']=='prepared'
    assert (state/'pending-review.json').exists()
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w') as archive:
        for file in state.iterdir():archive.writestr(f'{pub.STATE_NAME}/{file.name}',file.read_bytes())
    restored=tmp_path/'restored-checkpoint';restored.mkdir()
    with zipfile.ZipFile(io.BytesIO(data.getvalue())) as archive:restore_publication_state(archive,restored)
    root=restored/pub.STATE_NAME
    assert not (root/'pending-review.json').exists()
    client=Client(error=AssertionError('restored checkpoint must not buy a retry'))
    assert pub.review_for_publication(path,run,root,client)['status']=='prepared'
    assert pub.execute_prepared_publication(path,run,root,client)['reason']=='no_new_review_ticket'
    assert not client.calls


def test_cli_records_usage_before_sdk_json_parse_failure(case,tmp_path,monkeypatch):
    import httpx
    from scripts import review_publication as cli
    path,run,record,decision=case
    run_path=tmp_path/'last-run.json';run_path.write_text(json.dumps(run))
    state=tmp_path/pub.STATE_NAME
    monkeypatch.setenv('ANTHROPIC_API_KEY','test-only')
    requests=[]
    def respond(request):
        requests.append(request)
        return httpx.Response(200,json={
            'id':'msg_test','type':'message','role':'assistant','model':pub.INVESTIGATE_MODEL,
            'stop_reason':'max_tokens','stop_sequence':None,
            'content':[{'type':'text','text':'{"approved": true, "unfinished'}],
            'usage':{'input_tokens':123,'output_tokens':8192},
        })
    args=['--db',str(path),'--run-record',str(run_path),'--state-dir',str(state)]
    cli.main(args,http_client=httpx.Client(transport=httpx.MockTransport(respond)))
    assert len(requests)==1
    usage=json.loads((state/'latest-review-usage.json').read_text())
    assert usage['output_tokens']==8192
    entry=next(iter(pub.load_state(state)['reviews'].values()))
    assert entry['status']=='failed' and entry['input_tokens']==123
    cli.main(args,http_client=httpx.Client(transport=httpx.MockTransport(respond)))
    assert len(requests)==1


def test_scheduled_review_checkpoints_before_any_paid_call_and_exports_after_saving():
    import yaml
    workflow=yaml.safe_load(Path('.github/workflows/weekly-brief.yml').read_text())
    steps=workflow['jobs']['monitor']['steps']
    prepare=next(i for i,s in enumerate(steps) if '--prepare-only' in s.get('run',''))
    checkpoint=next(i for i,s in enumerate(steps) if s.get('with',{}).get('name')=='proofrun-monitor-state-checkpoint')
    execute=next(i for i,s in enumerate(steps) if '--execute-prepared' in s.get('run',''))
    save=next(i for i,s in enumerate(steps) if s.get('with',{}).get('name')=='proofrun-monitor-state')
    export=next(i for i,s in enumerate(steps) if 'export_monitoring.py' in s.get('run',''))
    assert prepare<checkpoint<execute<save<export
    assert steps[execute]['if'].startswith('success()')
    assert 'AUTOMATIC_PUBLICATION_REVIEW_ENABLED' in steps[execute]['if']
    assert steps[save]['if']=='always()'
