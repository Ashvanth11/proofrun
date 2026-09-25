# Evaluating the investigation agent

13 questions across 13 repositories, scored on six criteria that are asserts
over stored data rather than judgements. No model is involved in scoring.

A question passes only when **all** of these hold:

| Criterion | Holds when |
|---|---|
| `verdict` | the verdict is one the question allows |
| `execution` | the sandbox did what the question says it should have |
| `blockers` | where `blockers_any_of` is given, one of those blockers is named |
| `citations` | every ledger entry names a tool used somewhere in the run |
| `stop_reason` | the run finished rather than hitting a cap |
| `not_downgraded` | the integrity rules did not have to lower the verdict |

Two of those deserve explanation.

**A capped run is not a wrong answer, it is an unfinished one.** Counting it as
a pass would reward a cap set too low — the score would improve as the budget
shrank, which is backwards.

**A downgraded verdict means the agent claimed standing its trace did not
support.** The run may still have landed on an allowed verdict, but it got there
by a route the rules had to correct, and that is a failure of the thing being
measured.

The criteria were written **before** the first batch ran. Scoring code written
after you have seen results is scoring code you have already, quietly, fitted to
them.

## Three runs

| | run 1 | run 2 | run 3 |
|---|---|---|---|
| passing | 6/13 | 8/13 | **10/13** |
| estimated token cost | $4.98 | $4.31 | $4.92 |
| wall clock | 18 min | 18 min | 24 min |
| evidence | — | 16 / 15 / 27 | 15 observed / 15 inspected / 33 reported |

**No pass has ever regressed.** Every row that passed in run 1 passed in runs 2
and 3; every run-2 pass held in run 3.

No question has been edited after seeing results, across all three runs.

Every step of every run:
[the trace pages](../site/history.html). Scored reports:
[run 2](../reports/eval-investigations-2026-09-14-run2.md),
[run 3](../reports/eval-investigations-2026-09-14-run3.md).

## What still fails

| repo | criterion | what happened |
|---|---|---|
| `langfuse` | `not_downgraded` | Answered a licence question by reading `LICENSE` as a file rather than citing the metadata field, so rule 3 downgraded a correct answer. See [evidence-ledger.md](evidence-ledger.md). |
| `zenml` | `stop_reason` | Burned all 12 sandbox calls and stopped on `sandbox_cap`. |
| `avoid-ai-writing` | `blockers` | Correctly declined to run a JavaScript project, but named the blocker `other` where the question allows only `unsupported_language`. |

## Runs 1 → 2: four fixes, two of which did not work

**The two that failed** were prompt edits asking the agent to justify a clone
before making one. `rig` and `avoid-ai-writing` kept cloning repositories to
discover facts their metadata had already stated. Nothing measurable changed.
That is a prompt-level fix failing against a behaviour that needed a code-level
one, and it is the reason Session A exists.

**The two that worked** are worth the detail, because the bug sat three layers
from the symptom. The critic was flagging the agent for asserting a licence it
had supposedly invented, then forcing a revision that deleted the true entry.

Cause: `loop.summarize` truncated every tool result to 300 characters, and
`result_summary` is the critic's *entire* view of what a tool returned —
`critique.format_investigation_evidence` renders that and nothing else. A
repository's `license` field sat behind its `description` and `topics`, past the
cut. The critic could not see it, concluded it was fabricated, and the revision
stripped it.

Two changes fixed both `not_downgraded` failures: a 1,000-character window, and
reordering `get_repo_metadata` so the fields a verdict can turn on come before
the decorative ones.

Two run-1 failures initially blamed on badly-written questions turned out to be
this bug.

## Runs 2 → 3: the limit moves into code

Two failures in run 2 were repositories that cloned a project to learn something
their metadata already stated. Since the prompt version had not held, the limit
moved into code: `RUNNABLE_LANGUAGES`, checked in `Sandbox.create` beside the
size gate, refusing any repository GitHub reports as non-Python before a
container starts. It reads `language` from the same `get_repo_metadata` request
that already supplied `size_kb` and was discarding it.

The refusal names the language and the token `unsupported_language`, and carries
no exit code — so the scorer still counts zero sandbox commands and an
`execution: forbidden` question stays passable.

**The honest result: the gate never fired.** Both rows stopped reaching for the
sandbox, but the agent read `"language": "Rust"` from the metadata and never
attempted a clone. What changed the behaviour was most likely the *tool
description* naming the restriction, not the refusal behind it. The gate makes
the limit a guarantee rather than a hope, and it remains unexercised against a
live model.

**`avoid-ai-writing` traded one failure for another.** It declined to run a
JavaScript project — the expensive wrong behaviour is gone — but reported the
blocker as `other`. The refusal message is what teaches the model the token
`unsupported_language`, and a model that never triggers the refusal never sees
it. The fix removed the signal that produced the right answer.

That is recorded rather than iterated away, and the question's expectation was
not relaxed to fit it.

The other run-3 change closed the `description` hole described in
[evidence-ledger.md](evidence-ledger.md).

## How a batch is run

```bash
python investigate_batch.py --questions questions.yaml \
    --out reports/batch.md --budget 6
python evaluate.py --investigations
```

The batch prunes containers and volumes left by a crashed run, prints a
conservative cost estimate, and stops for confirmation. It then **refuses to
start a question when its estimate would carry the total past `--budget`**,
checked before the question rather than after it, and exits non-zero if it
stops early. Actual charges may differ, so this is not a guaranteed spending
limit. The recorded evaluation below is historical.

Run 3 stopped that way at 10 of 13 and was resumed with `--start-at 11`. A
resume is only one measurement if nothing about the agent changed in between;
both source files predated the batch launch, which was verified before
resuming.

## Known limits of this eval

- **n=13, three runs.** Enough to find bugs, not enough to distinguish a real
  +2 from noise. No variance estimate, single model.
- **Written by the same person who wrote the agent.** Mitigated by fixing the
  questions before the first run and editing none of them after — but it is not
  an independent benchmark and should not be read as one.
- **The question set is frozen.** Changing it would make runs incomparable.
