# The evidence ledger

Every claim an investigation makes carries a **kind** and a **source naming a
tool used in the run**. This is what a verdict rests on, and it is the
reason there are no confidence scores anywhere in the system.

| Kind | Means | Example source |
|---|---|---|
| `observed` | a command ran in the sandbox and exercised the thing | `sandbox_run(apm compile)` |
| `inspected` | a fact GitHub computed, read first-hand | `get_repo_metadata(license)` |
| `reported` | somebody's prose, including the project's own | `read_file(README.md)` |

## The three rules

They run **in code**, in `investigate.apply_integrity_rules`, after extraction
and again after any revision — because a revision is a fresh answer from the
model and can reintroduce exactly what the rules just removed.

1. **An entry whose source names no tool used in the run is
   dropped.** Free text is cheap to produce; a trace is not.
2. **The kind is capped by the citing tool, and may only be lowered.** A
   `read_file` can never yield `observed`. `cat README.md` inside a container is
   not execution — `exercises_something()` decides that, and every segment of a
   compound command has to be a read for the whole thing to count as one.
3. **`supported` and `refuted` require a first-hand entry on the matching
   side.** Otherwise the verdict is downgraded to `inconclusive` and the
downgrade is recorded on the run.

The source check matches a tool name used somewhere in the run. It does not
identify the exact invocation behind an entry or verify that its output
semantically supports the statement. A reviewer should inspect the trace for
those questions; historical ledgers retain their original format.

Rule 3 is what makes prompt injection expensive. A hostile README can tell the
model to report a claim as supported, and the model may comply — but README
contents are `reported`, so the verdict cannot reach `supported` without either
a command that ran inside a container with no secrets, no host access and no
network, or a fact GitHub computed that the author did not write.

## Why three kinds and not two

The original split was observed vs. reported. Repository metadata fits neither:
a licence field is first-hand — GitHub computed it, nobody's prose asserted it —
but nothing was executed.

Forcing it into `reported` made licence questions unanswerable at the correct
rung, since `supported` would then need an execution that has nothing to do with
licensing. Forcing it into `observed` would have made a file listing count as
proof that code runs.

`inspected` is deliberately narrow for that reason: wide enough that a licence
question can be answered from the licence field, narrow enough that it cannot be
answered from the README.

## Rule 3 bites, including when the agent is right

In eval run 2 the `langfuse` licence question was answered by reading `LICENSE`
and `ee/LICENSE` as *files* — `reported` — while never citing the metadata
`license` field that was one call away. No first-hand entry on the `for` side,
so the rules downgraded a correct answer to `inconclusive`.

The rule is working. The agent chose the weaker of two available sources, and it
shows up in the eval as a failed row, which is where it should show up. It is
still failing in run 3 for the same reason:
[eval-investigations.md](eval-investigations.md).

## The hole the bifrost run found

`inspected` originally meant "came from `get_repo_metadata`", and that tool
returned `description` and `topics` alongside the licence and the language.
Those two fields are written by the repository's author.

So an entry quoting a project's own marketing copy arrived as `inspected`, which
under rule 3 is enough to carry `supported` — a claim proving itself. The
`bifrost` investigation is the demonstration: asked whether the project is
really fifty times faster than LiteLLM, its single `for`-side entry cited
`get_repo_metadata(description)` and restated the benchmark claim being
investigated. The agent declined to call that `supported`, but nothing in the
rules required it to decline.

**The fix was a split, not a new rule.** `get_repo_metadata` now returns only
fields GitHub computed; a separate `get_repo_description` returns the author's
description and topics and is absent from `INSPECTING_TOOLS`, so rule 2 writes
`reported` for anything citing it.

A split rather than a heuristic because `cited_tools` matches *tool names* in a
source string the model writes. A rule that instead inspected which field the
source mentioned would depend on the model spelling `description` correctly, and
the model is the thing being constrained — a rule that rests on its cooperation
is not a rule.

The current `bifrost` run reaches the same verdict with no `inspected` entry at
all: [the trace](../site/maximhq-bifrost.html).

## Why not confidence scores

Stage 1 measured whether a model's judgment can stand in for a human's, and the
answer was no in a specific way:

| comparison | Pearson r |
|---|---|
| Haiku analyzer vs human | +0.349 |
| Sonnet judge vs human | +0.257 |
| **Sonnet judge vs Haiku analyzer** | **+0.908** |

The two models agreed with each other almost perfectly while both diverged from
the human. A confidence score is exactly that kind of self-assessment, so
shipping one would have dressed model consensus as calibration.

The deeper objection is that a confidence number is unfalsifiable per item.
"0.8 confident" cannot be checked against anything. "`apm compile` ran and
exited 0, and this file appeared" can be re-run, and can be *wrong* in a way a
reader can catch.

Full reasoning, alternatives and revisit conditions:
[decisions.md §7](decisions.md#7-evidence-kinds-not-confidence-scores). The
Stage 1 analysis: [eval-findings.md](eval-findings.md).
