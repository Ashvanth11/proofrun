# Gemini free-tier evaluation — 2026-09-24

**Decision: do not enable automatic publishing with Gemini 3.1 Flash-Lite yet.**

The user requested an evaluation before enabling weekly monitoring. Five live
requests were made using the locally configured Gemini key. No production
investigations, Docker commands, Anthropic fallback, or publication occurred.
The raw outputs and coding-assistant review are in
[reports/gemini-evaluation.json](../reports/gemini-evaluation.json).

## Scope and results

The model saw the tool-call summaries from three existing recorded runs, without
the saved verdicts or polished display summaries. It generated reports using the
same Investigation schema. A separate two-request fixture checked custom
function calling and preservation of the model's returned context.

| Check | Result |
|---|---|
| Structured reports | 3/3 parsed successfully |
| Short answers and explicit limitations | Present in all three |
| APM compile result | Supported; wording was generic and omitted the useful repeatability detail |
| Firecrawl hosted-product question | Too strong: marked supported from license identification despite the missing operative clause |
| Bifrost performance claim | Correctly left unverified, but mislabeled web-search evidence as observed execution |
| Fixture function-call round trip | Succeeded; no real tool or repository code executed |

The Bifrost evidence-kind error would be corrected by the existing integrity
rules. The Firecrawl overclaim is semantic and would require the evidence critic
or another review; source-tool validation alone cannot resolve it. These findings
were reviewed by the coding assistant, not an independent human evaluator.

Reported usage: **2,391 input tokens, 794 output tokens, 3,185 total**, across five
requests. Google lists free input/output for this model's Free Tier; the API
response does not verify the project's billing tier or actual invoice.

## What this does not establish

This is a reporting and interface smoke evaluation on three curated traces. It
is not an accuracy benchmark, a fresh end-to-end investigation, a test of Docker
execution planning, or a test of live search. No Gemini production adapter has
been selected or wired into the UI or weekly workflow.

A stronger Flash comparison was attempted below but produced no completed cases.
An end-to-end investigation would be a separate evaluation after report quality
is reviewed. Google Search grounding is not available on the 3.1 Flash-Lite Free
Tier, so full investigator parity also needs a deliberate search strategy.

Sources checked 2026-09-24:
[pricing and free-tier/search restrictions](https://ai.google.dev/gemini-api/docs/pricing),
[structured output](https://ai.google.dev/gemini-api/docs/generate-content/structured-output),
[function calling](https://ai.google.dev/gemini-api/docs/function-calling).

## Stronger Flash attempt — 2026-09-24 (local time)

The user authorized the same evaluation with `gemini-3.8-flash` using their Free
Tier key. The first request returned **HTTP 503** at 2026-09-25 05:47 UTC. The
script stopped immediately: one request attempted, no completed cases, no tool
probe, no automatic retry, and no paid fallback. No usage metadata was returned.
This is a service-availability failure, not a model-quality result or proof of
quota exhaustion. It does not establish the account's access to this model.

The earlier Flash-Lite artifact is preserved. The new attempt is saved in
[reports/gemini-flash-evaluation.json](../reports/gemini-flash-evaluation.json).
`evaluate_gemini.py` accepts an explicit `--model` argument. Its four offline
tests pass. The stronger-model result remains inconclusive.

## Follow-up retry boundary — 2026-09-24 (local time)

The four focused harness tests passed again offline. Google's current model and
pricing pages list `gemini-3.8-flash` and a Free Tier for standard text requests;
that does not verify the configured key's billing tier. A sandboxed explicit
retry wrote [reports/gemini-flash-evaluation-retry.json](../reports/gemini-flash-evaluation-retry.json)
with zero completed cases and a request/response validation error. It returned
no token usage, and the environment did not verify that an API request reached
Google. The previous 503 artifact was not overwritten.

The request to allow outbound API access was rejected by automatic approval
review because sending the repository-derived recorded traces to Google could
incur paid usage without approval specific to this model, destination, and
payload. There was no workaround, further retry, paid fallback, or repository
execution. The evaluation remains inconclusive. A new live attempt requires
explicit approval for that exact five-request comparison and a spend limit or
confirmed Free Tier project. Report correctness and real investigator behavior
still need separate validation before Gemini could be considered for production.

## Current provider decision — 2026-09-25

Claude was separately selected and activated for weekly monitoring. Its first
run and public results were verified; see [weekly monitoring](weekly-monitoring.md).
The approximately $5/week planning allowance belongs to that Claude schedule
and does not authorize another Gemini evaluation. The Gemini script is not in
the scheduled workflow and has no automatic paid fallback. No further live
Gemini call was made during this handoff review.
