# Design decisions

Decisions worth revisiting, with the reasoning that produced them. Recorded so a
future reader (or a future you) can tell a deliberate choice from an accident,
and can re-open one when its assumptions stop holding.

---

## 1. The agent is gated behind a relevance threshold

**Date:** 2026-08-28
**Status:** decided, implemented in Phase 5 T3
**Revisit if:** agent-derived scores turn out to disagree with analyzer scores often
enough that cheap pre-filtering is discarding genuinely relevant repos.

### Decision

The repo-analysis agent does not run on every GitHub item. It runs only on items
the cheap analyzer already scored at or above a threshold (default 0.4).

### Why

The question that prompted this was "why do we need GitHub access at that rate?" —
and working the arithmetic showed the premise was weak:

- The GitHub **watcher** costs 3 requests per run (one per topic), against the
  search API's own budget. Irrelevant to the core limit.
- The **agent** costs one core-API request per tool call: 1 request when metadata
  settles it, up to ~4 when it escalates to a file read. Average ~2.
- A run with ~8 GitHub repos therefore costs ~16 core requests, comfortably
  inside the anonymous limit of 60/hour.

So the rate limit does not bind during normal operation, and the earlier claim
that `GITHUB_TOKEN` was "effectively required" was overstated.

The real problem the limit exposed is different and worth fixing on its own
merits: **nothing was stopping the agent from deeply investigating repositories
the analyzer had already judged irrelevant.** Spending 2–4 API requests plus
several model calls to investigate a repo the analyzer scored 0.1 wastes
requests, tokens, and wall-clock time. Gating on relevance addresses the actual
waste; the rate limit was only the symptom that made it visible.

### Consequences

- Agent invocations drop to the fraction of items that clear the threshold.
- `GITHUB_TOKEN` becomes a **convenience for iterative development** — where
  repeated debug runs over the same repos can exhaust 60/hour and produce
  confusing mid-loop tool failures — rather than a prerequisite for running the
  system.
- The threshold introduces a dependency: the agent's coverage is only as good as
  the analyzer's calibration. A miscalibrated analyzer silently starves the agent
  of work. This is a reason the eval harness (Phase 3) matters more than it
  first appears, and is the main thing that would trigger a revisit.

### Alternatives considered

- **Run the agent on everything.** Simplest, and correct if API requests and
  tokens were free. They are not, and the waste is concentrated on exactly the
  items least worth the spend.
- **Require `GITHUB_TOKEN`.** Raises the ceiling to 5,000/hour but treats the
  symptom; the wasted model calls remain, and it adds a setup step for anyone
  cloning the repo.

---

## 2. Local Ollama as a development backend, Claude for quality

**Date:** 2026-08-28
**Status:** decided, implemented (`ai_monitor/providers.py`)
**Revisit if:** local model quality stops being sufficient to exercise control flow.

### Decision

The pipeline runs against either the Anthropic API or a local Ollama model,
selected with `--provider`. Local models are used for iterating on prompts and
control flow; the eval judge, final synthesis, and any number reported in a
writeup run on Claude.

### Why

Development iteration is the expensive-in-aggregate part of building this: many
runs, each cheap, adding up. Local inference makes that free. Quality judgments
are the opposite — few calls, each one load-bearing.

Using a weak local model as the eval **judge** would be self-defeating: the
harness exists to measure calibration, and a poorly-calibrated judge measures
nothing. The same applies to any figure quoted as a result.

Verified before committing to this: llama3.1 adheres correctly to
schema-constrained JSON output (Ollama constrains generation to the schema, so
malformed JSON is mechanically impossible), and scores sanely in direction. Its
weakness is calibration granularity — it clusters on round numbers, and in one
observed agent run returned `relevance_score=1.0` with
`justification="Direct match for LLM observability"` but `matched_areas=[]`.

### Consequences

- Zero-cost development; both starred milestones were reached before any API
  spend.
- The eval harness can **measure** the local-vs-Claude gap, turning "I used a
  local model to save money" into a quantified quality tradeoff.
- ~24.5s per item locally versus a few seconds on Haiku, so local runs are for
  correctness, not for speed of iteration.

---

## 3. No tracing backend (Langfuse / Phoenix) for now

**Date:** 2026-08-28
**Status:** deferred
**Revisit if:** debugging a multi-step agent run becomes hard from logs alone, or a
visual trace is wanted for the README.

### Decision

No third-party observability stack. The system relies on its own instrumentation:
per-run token counts and cost by stage, and the full agent trace (every tool call,
its arguments, a result summary, the stop reason, and cost) persisted to
`agent_runs`.

### Why

The two candidates each carry a cost that is not obviously repaid at this stage.
Arize Phoenix is local-first and needs no account, but is a heavy install
(pandas, numpy, opentelemetry, sqlalchemy). Langfuse's SDK is small, but reporting
requires either a hosted account or a self-run Docker/ClickHouse stack.

What a tracing UI would add over what exists is presentation, not capability: the
data is already captured and queryable. The gap it would close - visual step-through
of an agent run - is real but not yet painful, since runs are short and traces are
small enough to read directly.

### Consequences

- Nothing new to install, run, or keep credentials for.
- A README screenshot of a trace UI is not available; the `agent_runs` table has to
  speak for itself.
- If agent runs get longer or more branching, reading traces from SQL will get
  tedious, which is the signal to revisit.

---

## 4. Watcher nodes fetch only; all writes happen single-threaded

**Date:** 2026-08-28
**Status:** decided, implemented (`ai_monitor/orchestrator/graph.py`)

### Decision

LangGraph watcher nodes call `fetch()` and return items in state. A separate
`store` node performs every database write.

### Why

Found by a failing test, not by reasoning: LangGraph runs parallel branches in a
thread pool, and a SQLite connection cannot be used from a thread other than the
one that created it. The watcher wrapper's broad `except` was quietly converting
that into "watcher failed", so every run would have silently degraded.

Loosening the thread check (`check_same_thread=False`) would have hidden the
problem rather than fixed it, and invites concurrent-write races. The parallelism
is worth having on the network I/O, which is genuinely slow; the writes are fast
and gain nothing from concurrency.

### Consequences

- Watchers are simpler and more testable — they no longer need a connection.
- Storage is a single fan-in point, which is also the natural place for dedup
  (Phase 7) to sit.
