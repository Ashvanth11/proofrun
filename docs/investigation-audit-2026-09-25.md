# Saved investigation evidence audit — 2026-09-25

Reviewed all 47 rows in the local `monitor.db` investigation table, their saved
tool summaries, report blockers, and critique metadata. Also checked the saved
public Langfuse result in `site/monitoring.json`. This is an offline trace audit,
not a new factual evaluation of each repository or a rerun of any model.

| Pattern in saved results | Count | General fix or disposition |
|---|---:|---|
| At least one `read_file` response marked truncated | 43 of 47 runs, 69 calls | `read_file` now supports `find` and `start_char` to fetch a relevant later section or continue reading. A truncation alone does not make a verdict wrong. |
| Critique recorded one or more issues | 35 of 47 runs | The critic already prompted revisions in many cases. Its issue record is not an independent human audit or proof that the final answer is correct. |
| Critique issue mentioned truncation or details absent from the saved evidence | 30 of 47 runs | Saved tool summaries now retain both the beginning and end of long results. Targeted file reads put the searched passage at the beginning of the summary. |
| `no_testable_claim` used despite a concrete question | 4 runs: 4, 37, 44, 47 | Integrity rules now remove that blocker from actual investigations. The autonomous pre-investigation no-claim record remains valid. |
| Sandbox setup/run called before a successful clone | 12 runs | Existing tool guard refused the commands. Tool and investigator instructions now say to stop after a clone refusal. This avoids wasted turns when followed; it cannot force a model to comply. |
| Literal `""` supplied as the root directory path | 3 runs: 22, 34, 35 | `list_files` now normalizes the quoted empty value to the root path. |
| Web search returned `null` but was cited in the report | 2 runs: 44, 47 | Empty searches can no longer support ledger entries; the prompt also forbids inferring sources from them. The critique had already identified invented search details in these saved runs. |
| Clone refused at the 1 GB repository size gate | 3 LiteLLM runs: 19, 32, 45 | Expected safety limit; no change to the gate. These are poor candidates for a small execution demo. |

Examples that should not be presented as verified demonstrations without a new
run: Hindsight row 47 could not locate or test MCP code; Bifrost row 44 had no
usable web-search result for its performance claim; Utopia row 37's critique
flagged unsupported README quotations before revision. The published Langfuse
result also states that its README read stopped before feature details and that
no server was run. These are honest limited results, not evidence that the
repositories lack the claimed features.

The audit cannot reconstruct full historical tool responses from the stored
1,000-character summaries or prove that a revised claim is semantically
supported by its cited output. No saved verdict, historical evaluation score,
or published page was rewritten. The new safeguards apply to future runs only.

## Follow-up: Hindsight run 48

After the 47-row audit, a new Hindsight investigation used targeted reads to
reach the MCP section and the `retain` and `recall` source definitions. It
reported `supported`, with explicit limits saying no server was started or MCP
round trip tested. Its critique returned `grounded: true` **and** one concrete
issue: a ledger sentence attributes a local MCP command to a README excerpt
that does not show it. The command is visible in `hindsight-api/pyproject.toml`.
The old review condition skipped revision whenever `grounded` was true, even if
issues were present. Ten of the now 48 local investigations have that same
contradictory critique shape. Both assessment and investigation review now
request one revision when issues are present, regardless of the grounded flag.
This change was tested offline and does not rewrite run 48.
