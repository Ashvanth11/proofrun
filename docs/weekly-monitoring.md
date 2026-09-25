# Weekly monitoring and shared publishing

Status, 2026-09-24 (local time): **active and deployed with Claude**. The first
manual bootstrap run completed at 2026-09-25 06:43 UTC: ten repositories were
discovered, one was investigated, and the public feed lists nine additional
repositories. The next scheduled check is Monday at 08:17 UTC. See the
[public monitoring page](https://ashvanth11.github.io/proofrun/monitoring.html).
The earlier Gemini comparison remains documented in
[Gemini evaluation](gemini-evaluation.md).

## What is ready

- One weekly check, Mondays at 08:17 UTC, in `weekly-brief.yml`.
- The runner uses the existing Anthropic pipeline: discover at most 10 recently
  active GitHub repositories, analyze them with Haiku, and investigate at most
  one eligible repository with Sonnet. It preserves the existing evidence
  review and sandbox.
- The agreed planning allowance is about $5/week. The investigation loop stops
  starting turns after its $1.50 threshold; overshoot, extraction, review, search,
  and discovery analysis are additional. This is not a guaranteed billing cap.
- Completed weeks are idempotent. Failed/incomplete weeks require inspection
  before retrying, so rerunning Actions cannot silently repeat paid work.
- SQLite state is stored in an Actions artifact for 90 days, including on failed
  attempts. A missing artifact blocks a run unless a manual bootstrap is
  explicitly selected. This replaces the old seven-day cache dependence.
- A public `monitoring.json` feed and `monitoring.html` page come from the same
  automatic investigation rows. Direct questions are excluded. Both include short
  repository descriptions and original investigation dates.
- Both surfaces also show **Also discovered**: repository links and short
  descriptions from the latest completed weekly batch, labeled **Not
  investigated**, without verdicts or relevance scores. Normally this means
  nine other repositories when ten are found and one is investigated. Counts
  can be smaller; previously investigated repositories are excluded, and a run
  investigating a backlog candidate can leave all ten discoveries listed.
  Failed batches are not published as completed discoveries. Older databases
  and feeds without captured batches remain readable.
- The Streamlit UI reads that public feed at most once per five minutes of page
  use, merges it with local automatic results, and falls back gracefully when
  offline. A local database is not required to read published weekly results.
- Publication starts from the existing `gh-pages` content and adds the monitoring
  page/feed and a link from the landing page, preserving historical pages.
  Re-exporting the historical traces preserves the monitoring link.

The checked-in `site/monitoring.html` is a pre-run preview. The public page is
generated from the saved Actions state on each completed run.

## Activation gates

The workflow runs only while BOTH repository variables remain set:

- `WEEKLY_MONITORING_ENABLED=true`
- `MONITORING_PROVIDER=anthropic`

Both variables are currently set. The `ANTHROPIC_API_KEY` Actions secret exists
and Pages uses the GitHub Actions publishing source. The user selected Claude
explicitly. Gemini has only an evaluation harness and is not a fallback.

Activation completed on 2026-09-24 (local time):

1. Published commit `7f8e73c` to `main`; remote offline CI passed.
2. Added the Anthropic key as an Actions secret. Its value was not printed or
   committed; only the secret name was verified.
3. Switched Pages from legacy branch publishing to **GitHub Actions**, retaining
   `https://ashvanth11.github.io/proofrun/` and historical `gh-pages` assets.
4. Set `MONITORING_PROVIDER=anthropic` and `WEEKLY_MONITORING_ENABLED=true`.
5. Dispatched one `bootstrap=true` run. [Actions run 36103968762](https://github.com/Ashvanth11/proofrun/actions/runs/36103968762)
   passed both monitor and deploy jobs. Its state artifact contains a completed
   `2026-W39` row, ten discoveries, and one `langfuse/langfuse` investigation.
   The public JSON validates against the feed schema and shows one investigation
   plus nine Not investigated entries. The result is inconclusive, with a
   completed evidence critique and clear limitations. The saved investigation's
   token cost estimate is $0.104846; discovery analysis and any search charges
   are additional, so this is not a complete invoice.

Later scheduled runs restore the saved state. Keep the state artifact available;
if it expires, the workflow fails closed until history is reviewed and a manual
bootstrap is explicitly selected.

Never turn on both this runner and an independent schedule using a separate DB;
that could investigate and bill for the same repositories twice. If a run fails,
inspect the saved DB and Actions logs before changing the failed week record.

## Local/offline commands

```bash
python export_monitoring.py --out site
python evaluate_gemini.py  # explains the evaluation; makes no API requests
python -m pytest tests/ -q
```

The optional `python evaluate_gemini.py --run` makes up to five live Gemini API
requests. It is not part of tests or scheduled monitoring. Keep the key in `.env`
and use a project confirmed as Free Tier; the key alone cannot prove billing mode.

## Platform limits

GitHub schedules can be delayed and can be disabled after prolonged inactivity
in a public repository. The page displays the actual last completion time,
not a promise of punctual delivery. Artifact expiration fails closed instead of
silently treating all repositories as new.

Sources: [scheduled workflow behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule),
[cache eviction](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching),
[Pages publishing configuration](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site).
