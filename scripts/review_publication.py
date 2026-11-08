"""Review at most one newly completed weekly finding for automatic publication.

Run only after weekly_monitor.py succeeds. This never investigates a repository
or retries a saved review attempt. Raw review responses stay in private state.
"""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import anthropic
import httpx

from ai_monitor.publication import (review_for_publication, prepare_publication,
    execute_prepared_publication, load_state, write_state, STATE_NAME, INVESTIGATE_MODEL)
from ai_monitor.analysis.analyzer import Usage


def main(argv=None, *, http_client=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=Path('weekly-state/monitor.db'))
    parser.add_argument('--run-record', type=Path, default=Path('weekly-state/last-run.json'))
    parser.add_argument('--state-dir', type=Path)
    phase = parser.add_mutually_exclusive_group()
    phase.add_argument('--prepare-only', action='store_true')
    phase.add_argument('--execute-prepared', action='store_true')
    args = parser.parse_args(argv)
    state_dir = args.state_dir or args.db.parent / STATE_NAME
    run = json.loads(args.run_record.read_text())
    if args.prepare_only:
        print(json.dumps(prepare_publication(args.db, run, state_dir), indent=2))
        return 0
    key = os.environ.get('ANTHROPIC_API_KEY')
    if not key:
        parser.error('ANTHROPIC_API_KEY is missing; no review started')
    captures = []
    def capture(response):
        response.read()
        payload = response.json()
        captures.append(payload)
        # Only this attempt's raw response is private; exports read the approved
        # manifest and source snapshots, never this diagnostic file.
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / 'latest-review-response.json').write_text(json.dumps(payload, indent=2))
    with (http_client or httpx.Client(timeout=120)) as transport:
        transport.event_hooks['response'].append(capture)
        client = anthropic.Anthropic(api_key=key, max_retries=0, http_client=transport)
        operation = execute_prepared_publication if args.execute_prepared else review_for_publication
        result = operation(args.db, run, state_dir, client)
    if captures:
        # Preserve usage even when SDK structured parsing failed.
        (state_dir / 'latest-review-usage.json').write_text(json.dumps(captures[-1].get('usage'), indent=2))
        raw_usage = captures[-1].get('usage')
        if isinstance(raw_usage, dict) and 'input_tokens' in raw_usage and 'output_tokens' in raw_usage:
            usage = Usage(model=INVESTIGATE_MODEL, input_tokens=raw_usage['input_tokens'], output_tokens=raw_usage['output_tokens'])
            state = load_state(state_dir)
            result.update(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                          estimated_token_cost_usd=usage.cost_usd, model=INVESTIGATE_MODEL)
            state['reviews'][result['source_sha256']] = result
            write_state(state_dir, state)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
