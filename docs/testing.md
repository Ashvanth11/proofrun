# Tests

```bash
python -m pytest tests/ -q     # 474 tests, no network, no Docker, no API calls
```

Every external service is stubbed, the sandbox included — the suite exercises
the container contract by asserting on the `docker` argv that *would* be run,
so the guarantees are checked without Docker installed.

## Bugs the suite caught

Each one a *quiet* failure, which is the kind worth having tests for. A loud
failure announces itself; these all looked like success.

- **SQLite connections cannot cross threads.** LangGraph parallelizes watcher
  branches; a broad `except` was converting the resulting error into a generic
  "watcher failed", so every run would have silently degraded.
- **GitHub ANDs repeated `topic:` qualifiers.** A five-topic query asked for
  repositories carrying all five and matched nothing.
- **Editing the prompt did not invalidate cached analyses**, which made a prompt
  fix indistinguishable from a prompt fix that does not work — every item was
  skipped as unchanged.
- **Switching models silently skipped re-analysis**, leaving every item at the
  old score while the run reported "skipped (unchanged)" and looked healthy.
- **Word-boundary matching dropped plurals.** `\bagent\b` did not match
  "agents" — the worse failure direction, since nothing signals a false drop.
- **`find_repo("../../etc/passwd")` returned `etc/passwd`**, a valid `owner/name`
  shape sitting inside a traversal path.
- **A structured-output ceiling of 2,048 tokens destroyed whole runs.** A ledger
  with six entries overran it, the JSON was truncated mid-string, pydantic
  rejected it, and an investigation already paid for was lost. It happened in
  extraction and again, independently, in the critique pass.
- **A tool description went stale.** It advertised a 200 MB clone limit after the
  gate moved to 1 GB. It now interpolates the constant, with a test.

## Tests that pin a guarantee rather than a behaviour

Some of the suite exists to stop a *claim* in the README from quietly becoming
false.

- **The sandbox argv carries no environment.** No `-e`, no `--env`, no
  `os.environ` passthrough, so `ANTHROPIC_API_KEY` and `GITHUB_TOKEN` are not
  reachable from inside a container even by a script looking for them.
- **Subprocess is never called with `shell=True`**, and the repository name is
  matched against a strict `owner/name` pattern before it reaches any argv.
- **A model that never stops calling tools still terminates**, on each cap
  independently.
- **Spend halts at the ceiling** rather than being noticed after the fact.
- **The clone tool's description tracks the actual gates**, both the size limit
  and the language set, so the model cannot be taught a rule the code stopped
  enforcing.
- **The exported trace pages emit only an allowlist of tags** and reference
  nothing over the network.

## Escaping is tested against a parser, not a substring

The trace-export tests assert on what an **HTML parser** sees rather than on
what a substring search finds.

Several real pages contain the literal text `<img src=https://img.shields.io/…`
from README badges. A test grepping for `src=` would fail on those pages while a
page carrying a genuinely injected tag could pass. So the tests parse the output
and assert on the tag set and on every `src`/`href` value.

Removing the escaping in `export_traces.safe` fails nine of them.

## Two anchoring guards

The analyzer eval measures agreement between a model and a human, and both
halves of that are easy to contaminate:

- [`label.py`](../label.py) **hides the analyzer's score while you label.**
- **The judge never sees the analyzer's output.**

Each has a test asserting it. Without them both collapse into agreement and
measure nothing. See [eval-findings.md](eval-findings.md) for what the measured
version found.
