# Weekly monitoring and shared publishing

Status, 2026-09-24: Claude selected for the weekly workflow. Local checks pass;
publication, activation, and the first weekly run require separate remote
verification. The user explicitly approved publishing to public `main`, adding
the Anthropic Actions secret, and enabling the weekly schedule.
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

The generated `site/monitoring.html` currently states that no scheduled run has
completed. It is a local preview, not evidence of an active schedule.

## Activation gates

The workflow is disabled unless BOTH repository variables are set:

- `WEEKLY_MONITORING_ENABLED=true`
- `MONITORING_PROVIDER=anthropic`

The user selected Claude explicitly. Gemini has only an evaluation harness and
is not a fallback.

After provider selection and authorization to publish the reviewed changes:

1. Commit the necessary code and workflow to the default branch. Preserve
   unrelated local work.
2. Add the chosen provider's API key as a repository Actions secret. No secrets
   are embedded in the feed, source code, or public Pages artifact. GitHub currently
   has no repository Actions secrets configured (names checked 2026-09-24).
3. Switch Pages from branch publishing to **GitHub Actions**. This preserves
   `https://ashvanth11.github.io/proofrun/`; the existing `gh-pages` branch remains
   the source of historical assets. The prepared workflow uses `deploy-pages`.
4. Set the activation variables after verifying the Claude key and the reviewed
   workflow. The stored Sonnet investigation evaluation is prior evidence;
   verify the first weekly run separately.
5. Dispatch the first run with `bootstrap=true`, then verify its saved state,
   feed, public page, and UI. Later scheduled runs restore that state.

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
