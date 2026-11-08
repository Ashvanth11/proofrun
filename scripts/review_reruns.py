"""Plan or explicitly execute isolated reruns; never update the source DB or publish.

Plan (no network/model/Docker calls):
  python scripts/review_reruns.py --source monitor.db --run-ids 39 50 52
  python scripts/review_reruns.py --source /tmp/snapshot/monitor.db --week 2026-W45

Execution additionally requires --execute --out <new-directory>. Original rows,
configuration, model usage, partial results and errors stay in that directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
import httpx
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_monitor.agent import critique, investigate as inv, investigate_runner as runner
from ai_monitor.agent.loop import Caps
from ai_monitor.agent.sandbox import IMAGE
from ai_monitor.analysis.analyzer import Usage, PRICING
from ai_monitor.config.settings import settings
from ai_monitor.storage import db
from ai_monitor.storage.models import Item, Source
from ai_monitor.watchers import github


def read_plan(source: Path, *, week=None, run_ids=None) -> tuple[dict, list[dict]]:
    """Freeze exact original questions, or a saved discovery batch and scores."""
    with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        if week:
            record = conn.execute('select * from weekly_runs where week=?', (week,)).fetchone()
            if record is None:
                raise ValueError(f'No saved week {week}')
            ids = [r[0] for r in conn.execute('select item_id from weekly_discoveries where week=?', (week,))]
            if not 1 <= len(ids) <= 10:
                raise ValueError('Expected one to ten saved discoveries')
            rows = [dict(r) for r in runner.candidates(conn, eligible_item_ids=ids, limit=10)]
            count = 1
            mode = 'saved_week_selection'
        else:
            if not run_ids or len(set(run_ids)) != len(run_ids):
                raise ValueError('Supply distinct historical investigation IDs')
            rows = []
            for run_id in run_ids:
                row = conn.execute('select * from investigations where id=?', (run_id,)).fetchone()
                if row is None:
                    raise ValueError(f'No investigation {run_id}')
                inv.validate_repo(row['repo'])
                rows.append(dict(row))
            count = len(rows)
            mode = 'same_question_reruns'
    caps = inv.default_caps(max_cost_usd=runner.DEFAULT_MAX_COST_USD)
    screening = len(rows) * (4000 * PRICING[inv.INVESTIGATE_MODEL]['input'] + inv.DERIVE_MAX_TOKENS * PRICING[inv.INVESTIGATE_MODEL]['output']) / 1_000_000 if week else 0
    plan = {
        'mode': mode, 'source': str(source.resolve()),
        'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'week': week, 'original_ids': run_ids or [],
        'cases': [{'repo': r.get('repo', r.get('source_id')), 'question': r.get('question'),
                   'original_verdict': r.get('verdict')} for r in rows],
        'model': inv.INVESTIGATE_MODEL, 'caps': caps.model_dump(mode='json'),
        'web_search': False, 'sdk_retries': 0, 'max_investigations': count,
        'estimated_allowance_usd': round(runner.worst_case_usd(count, caps, max_web_searches=0) + screening, 4),
        'limitations': ['Estimate is not a hard charge limit.',
                       'Repositories are fetched live; source drift prevents a controlled before/after comparison.',
                       'Saved-week mode reuses original discovery scores, not fresh discovery or relevance scoring.',
                       'No automatic publication; review each conclusion and its execution evidence first.'],
    }
    return plan, rows


class RecordedParseError(ValueError):
    def __init__(self, original, response):
        super().__init__(str(original))
        self.response_usage = response.get('usage')
        self.stop_reason = response.get('stop_reason', 'unknown')


def request_json(kwargs):
    """Normalize SDK content blocks exactly as data, including thinking signatures."""
    request = {k: (v.__name__ if k == 'output_format' else v) for k, v in kwargs.items()}
    def encode(value):
        if isinstance(value, BaseModel):
            return value.model_dump(mode='json', exclude_unset=True)
        raise TypeError(f'Cannot record {type(value).__name__}')
    return json.dumps(request, indent=2, default=encode)


class RecordedClient:
    """Save completed responses and actual usage, including screening/review."""
    def __init__(self, client, out: Path, model: str):
        self.client = client
        self.out = out
        self.usage = Usage(model=model)
        self.calls = 0
        self.http_responses = {}
        self.messages = SimpleNamespace(parse=self.parse, create=self.create)

    def record_http_response(self, response):
        """HTTPX hook runs before SDK structured parsing can discard the body."""
        response.read()
        if not response.is_success:
            return
        payload = response.json()
        self.out.joinpath(f'call-{self.calls:03}.http-response.json').write_text(json.dumps(payload, indent=2))
        if payload.get('usage'):
            self.http_responses[self.calls] = payload
            self.usage.input_tokens += payload['usage']['input_tokens']
            self.usage.output_tokens += payload['usage']['output_tokens']
            self.out.joinpath('usage.json').write_text(self.usage.model_dump_json(indent=2))

    def _call(self, method, kwargs):
        self.calls += 1
        stem = self.out / f'call-{self.calls:03}'
        # API credentials are owned by the SDK, never included in kwargs.
        stem.with_suffix('.request.json').write_text(request_json(kwargs))
        try:
            response = getattr(self.client.messages, method)(**kwargs)
        except ValueError as exc:
            self.out.joinpath(f'call-{self.calls:03}.parse-error.json').write_text(json.dumps({'error': str(exc)}))
            if self.calls in self.http_responses:
                raise RecordedParseError(exc, self.http_responses[self.calls]) from exc
            raise
        if self.calls not in self.http_responses:
            self.usage.input_tokens += response.usage.input_tokens
            self.usage.output_tokens += response.usage.output_tokens
        stem.with_suffix('.response.json').write_text(response.model_dump_json(indent=2))
        self.out.joinpath('usage.json').write_text(self.usage.model_dump_json(indent=2))
        return response

    def parse(self, **kwargs):
        return self._call('parse', kwargs)

    def create(self, **kwargs):
        return self._call('create', kwargs)


class RecordedReplayClient(RecordedClient):
    """Explicit continuation: replay an exact saved prefix without API requests.

    Request mismatch stops before new spend. Only the remaining calls reach
    the live client. This is never selected automatically after a failure.
    """
    def __init__(self, client, out, model, replay_from):
        super().__init__(client, out, model)
        self.replay_from = Path(replay_from)
        files = sorted(self.replay_from.glob('call-*.http-response.json'))
        self.replay_count = len(files)
        if not self.replay_count or [p.name for p in files] != [f'call-{i:03}.http-response.json' for i in range(1, self.replay_count + 1)]:
            raise ValueError('Replay requires a complete contiguous saved response prefix')

    def _call(self, method, kwargs):
        if self.calls >= self.replay_count:
            return super()._call(method, kwargs)
        from anthropic.types import Message
        from anthropic.lib._parse._response import parse_response
        index = self.calls + 1
        old = self.replay_from / f'call-{index:03}'
        current = request_json(kwargs)
        if json.loads(current) != json.loads(old.with_suffix('.request.json').read_text()):
            # A mismatch must escape screening's recoverable ValueError path.
            raise RuntimeError(f'Replay request {index} differs; refusing further paid calls')
        payload = json.loads(old.with_suffix('.http-response.json').read_text())
        message = Message.model_validate(payload)
        response = parse_response(response=message, output_format=kwargs['output_format']) if method == 'parse' else message
        self.calls = index
        self.usage.input_tokens += message.usage.input_tokens
        self.usage.output_tokens += message.usage.output_tokens
        stem = self.out / f'call-{index:03}'
        stem.with_suffix('.request.json').write_text(current)
        stem.with_suffix('.replayed-response.json').write_text(json.dumps(payload, indent=2))
        self.out.joinpath('usage.json').write_text(self.usage.model_dump_json(indent=2))
        self.out.joinpath('replay.json').write_text(json.dumps({
            'from': str(self.replay_from), 'replayed_calls': index,
            'note': 'Usage includes the original prefix once; replay made no model calls.',
        }, indent=2))
        return response


def execute(plan: dict, rows: list[dict], out: Path) -> None:
    import anthropic
    if not settings.anthropic_api_key:
        raise ValueError('ANTHROPIC_API_KEY is not configured')
    subprocess.run(['docker', 'info'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(['docker', 'image', 'inspect', IMAGE], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Exclusive creation prevents accidental duplicate paid work or overwrites.
    out.mkdir(parents=True, exist_ok=False)
    (out / 'plan.json').write_text(json.dumps(plan, indent=2))
    (out / 'originals.json').write_text(json.dumps(rows, indent=2))
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    (out / 'code.diff').write_text(subprocess.check_output(['git', 'diff'], text=True))
    (out / 'rerun-script.py').write_text(Path(__file__).read_text())
    root = Path(__file__).resolve().parents[1]
    fingerprints = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted((root / 'ai_monitor').rglob('*.py'))}
    (out / 'source-files.json').write_text(json.dumps(fingerprints, indent=2))
    (out / 'started.json').write_text(json.dumps({'at': datetime.now(timezone.utc).isoformat(), 'base_revision': revision}))
    http_client = httpx.Client()
    client = RecordedClient(anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=0,
                                             http_client=http_client), out, plan['model'])
    http_client.event_hooks['response'].append(client.record_http_response)
    conn = db.connect(out / 'reruns.db')
    caps = Caps.model_validate(plan['caps'])
    try:
        if plan['mode'] == 'saved_week_selection':
            ids = []
            for row in rows:
                item_id = db.upsert_item(conn, Item(source=Source.GITHUB, source_id=row['source_id'], title=row['title'],
                    url=row['url'], content=row['content'], raw=json.loads(row['raw_json'] or '{}')))
                conn.execute('insert into analyses (item_id,relevance_score) values (?,?)', (item_id, row['relevance_score']))
                ids.append(item_id)
            conn.commit()
            runner.run_investigations_on_candidates(conn, client, model=plan['model'], caps=caps,
                limit=1, select_best_claim=True, readme_loader=github.fetch_readme,
                eligible_item_ids=ids, allow_web_search=False)
        else:
            for row in rows:
                run = inv.investigate(inv.Question(repo=row['repo'], claim='', question=row['question']), client,
                                      model=plan['model'], caps=caps, allow_web_search=False)
                if run.report is not None:
                    run, usage = critique.critique_and_revise_investigation(run, client, model=plan['model'])
                    run.usage.input_tokens += usage.input_tokens
                    run.usage.output_tokens += usage.output_tokens
                inv.store_investigation(conn, run)  # fresh DB; no originals to overwrite
        (out / 'completed.json').write_text(json.dumps({'at': datetime.now(timezone.utc).isoformat()}))
    except Exception as exc:
        (out / 'failed.json').write_text(json.dumps({'error': type(exc).__name__, 'message': str(exc)}))
        raise
    finally:
        conn.close()
        http_client.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--week')
    selection.add_argument('--run-ids', nargs='+', type=int)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args(argv)
    plan, rows = read_plan(args.source, week=args.week, run_ids=args.run_ids)
    print(json.dumps(plan, indent=2))
    if args.execute:
        if args.out is None:
            parser.error('--execute requires --out pointing to a new directory')
        execute(plan, rows, args.out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
