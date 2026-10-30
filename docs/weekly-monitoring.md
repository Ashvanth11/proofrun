# Weekly monitoring and shared publishing

Status, 2026-09-24 (local time): **active and deployed with Claude**. The first
manual bootstrap run completed at 2026-09-25 06:43 UTC: ten repositories were
discovered, one was investigated, and the public feed lists nine additional
repositories. The next scheduled check is Monday at 08:17 UTC. See the
[public landing page](https://ashvanth11.github.io/proofrun/).
The earlier Gemini comparison remains documented in
[Gemini evaluation](gemini-evaluation.md).

## What is ready

- One weekly check, Mondays at 08:17 UTC, in `weekly-brief.yml`.
- The runner discovers at most 10 recently active GitHub repositories and
  analyzes relevance with Haiku. For eligible repositories in that week's batch,
  it fetches the actual README and uses Sonnet to derive and rate a testable
  claim. It investigates the strongest claim once with Sonnet, preserving the
  existing evidence review and sandbox. If none has a testable claim, the week
  finishes with zero investigations and no fabricated no-claim report.
- The agreed planning allowance is about $5/week. Claim screening can make up
  to ten short Sonnet calls in addition to discovery and one investigation.
  The investigation loop stops
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
- Publication starts from the existing `gh-pages` content, moves the historical
  index to `history.html`, and serves weekly monitoring at `/proofrun/`.
  `monitoring.html` remains an alternate URL. Re-exporting traces updates the
  historical index without replacing the weekly landing page.

The public page is generated from the saved Actions state on each completed
run. The local Streamlit app checks that public JSON at most once every five
minutes while someone uses it; this does not schedule model runs or cause
GitHub Pages to publish more often.

### Capability verification following the October 29 audit

Scheduled runs use these rules from the default branch:

- Weekly discovery searches Python repositories to match the execution sandbox.
  Other discovery callers retain their existing language coverage.
  Weekly selection rejects known non-Python repositories and repositories above
  the sandbox clone-size limit before paid claim screening. Eligible claims
  must include a proposed falsifiable offline capability test and declare no
  unavailable resources (credentials, runtime network, GPU, external services,
  other toolchains, or large downloads). The model's feasibility assessment is
  a screening step, not proof that the experiment will succeed. Unknown metadata
  still follows the sandbox's existing policy and is checked during investigation.
- Screening outcomes, rejection reasons, proposed plans, and model-declared
  resource needs are saved in `claim_screenings` in the state database. A week
  may complete with zero investigations if no feasible claim is found. The
  existing discovery feed still lists the uninvestigated repositories.
- Tool traces retain the complete local result as returned by the tool, plus
  the invocation ID. Compact summaries remain for display. Grounding review
  uses the retained full result; it does not truncate it again. Older summaries
  and opaque server-search content are labeled incomplete or unavailable.
  Structured server metadata is preserved, but encrypted content is not treated
  as a readable passage. This does not decrypt or independently fetch sources.
- Unresolved reports with named blockers use `could_not_test`. The critique
  still assesses the initial report and may revise it once; it does not certify
  the revised report's grounding with a second model pass.

The investigation and screening call limits remain unchanged. Fuller evidence
can increase review input-token costs; existing estimates are not billing caps.

For an offline partial backtest against downloaded historical state artifacts:

```bash
python scripts/backtest_weekly.py /path/to/week1/monitor.db /path/to/week2/monitor.db
```

Use one snapshot per completed week. This replays the actual deterministic gates
with that week's saved analyses and earlier-investigation exclusions. It opens
sources read-only and works on in-memory copies. It does not call models or
recreate alternate discovery results, missing README inputs, or missing full
tool evidence; its output deliberately leaves a new conclusive-result rate unknown.

For a site-only layout or copy change between weekly runs, manually dispatch
`Publish monitoring Pages`. It restores the saved completed state and deploys
the page without a model key, repository discovery, investigation, or Docker.
It has no schedule and fails closed if the state artifact is missing.

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

For a completed current week whose only saved investigation is a
`no_testable_claim` placeholder, manually dispatch **Weekly monitoring** with
`rerun_current_week=true`. This reuses that week's saved discoveries and
relevance analyses, screens their actual READMEs, and performs at most one new
investigation. The workflow backs up the prior DB and run record in its state
artifact before making paid calls. It replaces the placeholder in the current
feed only after the new selection finishes. Ordinary dispatches remain
idempotent; the rerun option is unavailable for a substantive investigation or
an earlier week.

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
