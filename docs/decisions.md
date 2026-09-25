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

## 3. Hugging Face Papers not added as a fourth source

**Date:** 2026-09-05
**Status:** declined for Stage 1; candidate for Stage 2 in a different form
**Revisit if:** the project moves to ranking rather than filtering, and a
community-attention prior would improve that ranking.

### Decision

Sources stay at arXiv, GitHub, and Hacker News. HF Papers is not added.

### Why

HF Papers is largely arXiv papers with community upvotes layered on. As a fourth
watcher it would mostly re-fetch items the arXiv watcher already has, and dedup
would correctly collapse them - paying for a source whose output mostly
disappears.

Three further reasons against it now:

- It re-expands scope that v2 deliberately cut. PyPI and HF Hub were dropped to
  make room for the agent loop and eval harness; adding a source back is breadth
  where the plan chose depth.
- Source coverage is not the bottleneck. Three sources is already a credible
  multi-source claim. The gap in the README is a measured number in the
  evaluation section, not a fourth ETL job.
- It repeats demonstrated work. A fourth watcher is another ~150 lines of
  something already shown three times, and differentiates nothing.

### The better form, if revisited

Not as a source - as **signal enrichment on existing arXiv items**. An arXiv
paper with 200 HF upvotes is a different proposition from one with zero, and
that prior could inform routing and agent gating. Framed that way it becomes an
experiment with a measurable result ("does a community-attention prior improve
ranking against the golden set?") rather than another ETL job. That is a
stronger Stage 2 item than a fourth watcher would be.

Note: the HF Papers API surface was never verified - whether upvote counts are
exposed programmatically needs checking before committing to this.

---

## 4. No tracing backend (Langfuse / Phoenix) for now

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

## 5. Watcher nodes fetch only; all writes happen single-threaded

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

---

## 6. Execution happens in a disposable container with network off

**Date:** 2026-09-13
**Status:** decided, implemented (`ai_monitor/agent/sandbox.py`)
**Revisit if:** questions start requiring a toolchain other than Python, or an
investigation needs a service that outlives a single command.

### Decision

The investigation agent may run code, but only through three verbs, each a
fresh `docker run --rm` against one named volume mounted at `/work`:

| Verb | Network | Timeout | Purpose |
|---|---|---|---|
| `sandbox_clone` | bridge | 300s | `git clone --depth 1` into the volume |
| `sandbox_setup` | bridge | 300s | install dependencies |
| `sandbox_run` | **none** | 120s | execute the thing being investigated |

Every container also gets `--read-only`, `--user 1000:1000`, `--cap-drop ALL`,
`--security-opt no-new-privileges`, `--memory 2g`, `--cpus 2`,
`--pids-limit 256`, `--env-file /dev/null`, and a `/tmp` tmpfs. The mounted
volume is the only writable path that persists.

**Network is a property of the verb, not an argument the model can pass.**

### Why

The threat model is not hypothetical. The repository under investigation is
chosen by a pipeline that reads public feeds, so *anyone who can publish a
repo can choose what this agent executes*. Two distinct risks follow, and they
need different controls:

1. **The code is hostile.** It tries to read the host filesystem, mine, or
   phone home. The container limits host access, and `--network none` during
   `sandbox_run` blocks network egress for that verb. Clone and setup retain
   network access, so setup code still has an outbound path.
2. **The text is hostile.** A README, a command's own stdout, or a web result
   can carry instructions aimed at the agent reading them. No container helps
   here. This is why the evidence ledger exists (decision 7) and why every cap
   is enforced in code rather than requested in the prompt — a prompt is a
   request, and the attacker is writing to the same context window.

The residual risk is named rather than hidden: `sandbox_setup` has network by
necessity, so arbitrary code *does* get one window with an outbound path. That
window is bounded (4 setup calls, 300s each, no host environment, no
credentials mounted) but it is real. Closing it entirely would mean
pre-building every dependency, which would reduce the agent to repos whose
dependencies were guessed in advance.

### Consequences

- **Python-only, and now enforced.** The read-only root defeats toolchain
  installers that want to write outside `/work`; one run installed rustup
  during setup and then could not use it. That made "Python only" true by
  accident rather than by design, so it was stated as a *prompt* rule — and
  the prompt rule did not hold. In eval run 2, `rig` (Rust) and
  `avoid-ai-writing` (JavaScript) each cloned a repository to discover a fact
  its metadata had already stated, on questions where reaching for a container
  was itself the wrong move; two separate prompt edits asking the model to
  justify a clone before making one changed nothing measurable.

  **So the limit moved into code**, as `RUNNABLE_LANGUAGES` checked in
  `Sandbox.create` beside the size gate: a refusal before any Docker call,
  reading `language` from the same `get_repo_metadata` request that already
  supplied `size_kb` and was discarding it. The refusal reaches the model as a
  tool error naming the language and the token `unsupported_language`, which
  the extraction prompt already maps onto the blocker of that name, and it
  carries no exit code — so the eval still scores it as zero sandbox commands
  and an `execution: forbidden` question stays passable.

  The general lesson is the one worth keeping: **a rule the prompt states and
  the code does not enforce is a request.** It held for the two questions where
  the model had no reason to disagree and failed on the two where it did. This
  is the same argument the caps already make, applied a rung lower.

  `None` — GitHub could not detect a language — is allowed through, exactly as
  `size_kb=None` skips the size gate. An absent fact is not evidence of a bad
  one, and the alternative is a metadata hiccup silently narrowing the agent.
- **No state survives a call except the volume.** A fresh container per verb
  means the agent cannot start a background server in one call and curl it in
  the next. Anything needing a live service is untestable here.
- **Two size controls, doing different jobs.** `MAX_REPO_KB` (1 GB) is a coarse
  pre-flight refusal from GitHub's history-inclusive `size_kb`, which is a poor
  predictor of a `--depth 1` clone. The real enforcement is `DISK_CAP_MB`
  (2 GB), measured on the volume after every call, which stops the loop.

### Alternatives considered

- **No sandbox; read and reason only.** Simplest and safest, and it was the
  Stage 1 design. Rejected because it makes `observed` evidence impossible by
  construction — every claim collapses to "the README says so", which is
  exactly the thing this project exists to distinguish.
- **Run in a venv on the host.** Rejected outright. Arbitrary code from
  strangers, with the user's home directory and API keys in reach.
- **gVisor or a microVM (Firecracker).** Genuinely stronger isolation against
  kernel escapes. Rejected for now as heavy and awkward on the macOS
  development machine, for a threat that is a rung above what this project
  plausibly faces. The three-verb contract is the part that would survive such
  a migration; only the executor beneath it would change.
- **An egress proxy instead of `--network none`.** Allowlist the hosts setup
  legitimately needs (PyPI, GitHub) and deny the rest, which would let
  `sandbox_run` keep a network for projects that need one. Strictly more
  capable, and the natural next step if a question ever requires it. Rejected
  now because it trades a control that is trivially verifiable — the flag is
  either `none` or it is not, and a test asserts it — for one whose correctness
  depends on proxy configuration that would itself need testing. `none` is the
  claim that is cheap to make honestly.
- **Anthropic's Managed Agents, with its hosted sandbox.** Removes the container
  work entirely and is almost certainly better isolated than anything built
  here. Rejected for this cycle for two reasons: the sandbox contract *is* a
  substantial part of what this project is demonstrating, and handing execution
  to a managed service would make the disk and network guarantees someone
  else's to describe rather than mine to assert and test. A reasonable choice
  for a production version of this, and a fair interview question.
- **The Claude Agent SDK's own loop and tooling.** Same trade. The loop, its
  caps, and the trace are the engineering content here; adopting a framework
  loop would leave the ledger as the only original part.

---

## 7. Evidence kinds, not confidence scores

**Date:** 2026-09-13
**Status:** decided, implemented (`ai_monitor/agent/investigate.py`)
**Revisit if:** a claim appears that none of the three kinds describes honestly.

### Decision

No confidence number appears anywhere in an investigation. Every claim in the
ledger instead carries a **kind** and a **source naming a tool used in the run**:

| Kind | Means | Example source |
|---|---|---|
| `observed` | a command ran and exercised the thing | `sandbox_run(apm install)` |
| `inspected` | a fact computed by GitHub, read first-hand | `get_repo_metadata(license)` |
| `reported` | somebody's prose, including the project's own | `read_file(README.md)` |

Three rules then run in code, after extraction and again after any revision:

1. An entry whose source names no tool used in the run is dropped.
2. The kind is capped by the citing tool, and may only be lowered — a
   `read_file` can never yield `observed`, and `cat README.md` inside a
   container is not execution.
3. `supported` and `refuted` require a first-hand entry (`observed` or
   `inspected`) on the matching side. Otherwise the verdict is downgraded to
   `inconclusive`.

The source match is at tool-name level across the run. It does not bind the
entry to a specific invocation or prove semantic support from the cited output.

### Why

Stage 1 measured whether a model's judgment can be trusted as a proxy for a
human's, and the answer was no in a specific and damning way:

| comparison | Pearson r |
|---|---|
| Haiku analyzer vs human | +0.349 |
| Sonnet judge vs human | +0.257 |
| **Sonnet judge vs Haiku analyzer** | **+0.908** |

The two models agreed with each other almost perfectly while both diverged from
the human. A confidence score is exactly that kind of self-assessment, so
shipping one would have dressed model consensus as calibration. Full analysis:
[eval-findings.md](eval-findings.md).

The deeper objection is that a confidence number is unfalsifiable per item.
"0.8 confident" cannot be checked against anything. "`apm install` ran and
exited 0, and these files appeared" can be checked by re-running it, and can be
*wrong* in a way a reader can catch.

**Why three kinds and not two.** The original split was observed vs. reported.
Repository metadata fits neither: a licence field is first-hand — GitHub
computed it, nobody's prose asserted it — but nothing was executed. Forcing it
into `reported` made licence questions unanswerable at the correct rung, since
`supported` would need an execution that has nothing to do with licensing.
Forcing it into `observed` would have made a file listing count as proof that
code runs.

### The hole the bifrost run found, and how it was closed

`inspected` originally meant "came from `get_repo_metadata`", and that tool
returned `description` and `topics` alongside the licence and the language.
Those two fields are written by the repository's author. So an entry quoting a
project's own marketing copy arrived as `inspected`, which under rule 3 is
enough to carry `supported` — a claim proving itself. The `bifrost`
investigation is the demonstration: its single `for`-side entry cited
`get_repo_metadata(description)` and restated the very benchmark claim being
investigated. The agent declined to call it `supported`, but nothing in the
rules required that.

The fix is a **split, not a new rule**: `get_repo_metadata` now returns only
fields GitHub computed, and a separate `get_repo_description` returns the
author's description and topics. That tool is simply absent from
`INSPECTING_TOOLS`, so the existing cap in rule 2 writes `reported` for
anything citing it, alongside the README it paraphrases.

A split rather than a heuristic because `cited_tools` matches *tool names* in
a source string the model writes. A rule that instead inspected which field
the source mentioned would depend on the model spelling `description`
correctly — and the model is the thing being constrained, so a rule that rests
on its cooperation is not a rule. This is the same argument as the language
gate in §6, one layer up: the enforcement has to sit somewhere the model does
not reach.

### Consequences

- **The agent can be right and still downgraded, and that counts as a
  failure.** In the second eval run the licence question about `langfuse` was
  answered by reading `LICENSE` and `ee/LICENSE` as *files* — `reported` — while
  never citing the metadata `license` field that was one call away. No
  first-hand entry, so rule 3 downgraded a correct answer. The rule is working;
  the agent chose the weaker of two available sources. It shows up in the eval
  as a failed row, which is where it should show up.
- **Verdicts become cheap to audit.** Every row of a ledger names a call in the
  stored trace, so "did it actually check this?" is a lookup rather than a
  judgment call.
- **The eval needs an `execution` mode per question, not a boolean.** See
  alternatives.

### Alternatives considered

- **Confidence scores.** Rejected on the measured evidence above. This is the
  decision the Stage 1 eval paid for.
- **Free-text caveats in a summary.** What the model already does naturally, and
  unmeasurable: there is no way to score "did it hedge appropriately".
- **A single `verified: true/false` boolean per run.** Tried first, in the eval
  harness. It collapsed the three rungs into one and made every question whose
  correct answer is `could_not_test` unpassable by construction — a correct
  refusal produces no `observed` entry, so the boolean scored honesty as
  failure. Replaced by a per-question mode of `required` / `forbidden` /
  `attempt`, which lets the eval state that reaching for a container was the
  *wrong* move on a question metadata already settles.
