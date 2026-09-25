# Proofrun

**Proofrun answers "does this repository actually do what its README says?" — by
running it.**

You ask a question about a repository. It decides whether reading settles the
matter or whether it has to clone the code and execute it in a sandbox, then
answers with a ledger where every claim names a source tool and is tagged by
what kind of evidence it is.

---

## Try it

Open the local UI with two switchable modes. **Monitoring** surfaces saved
investigations from the automatic discovery pipeline: what was checked, what
was found, and what remains unknown. **Ask it yourself** accepts a GitHub
repository URL and a question in separate fields.

```bash
streamlit run app.py
```

The monitoring tab reads discovery-linked investigation results from local
SQLite and the public weekly feed without starting an investigation. If none exist, it shows three clearly labeled
recorded investigations initiated by direct questions, demonstrating the same
investigation engine. These examples work without credentials, Docker, or a
local database. Earlier discovery summaries remain in a secondary section.
An **Also discovered** section lists the other repositories from the latest
completed weekly batch, with descriptions and links, labeled **Not investigated**.
The weekly workflow uses the existing Claude API: Haiku scores up to ten
discoveries and Sonnet investigates at most one. See
[weekly monitoring setup](docs/weekly-monitoring.md) for activation and current
publication status. The [Gemini evaluation](docs/gemini-evaluation.md) remains
historical comparison evidence.

Results lead with a short answer and understandable findings. Original reports,
evidence sources, commands, review issues, and execution costs remain available
under “Full report, sources, and execution details.” New reports also include
explicit limitations, reviewed alongside the conclusion, without an additional
model call. Existing stored reports remain readable.

The **Investigate** button runs a new question locally and saves its result to
SQLite. Live use needs `ANTHROPIC_API_KEY`; Docker and the sandbox image are
needed when the agent executes repository code. The displayed spend allowance
and token cost are estimates. A repeated submission in the same UI session
shows the previous outcome until you explicitly allow another run.

Ask about one repository:

```bash
python investigate.py "Is firecrawl/firecrawl licensed under terms that would \
  oblige a company embedding it in a hosted product to publish their source?"
```

```
VERDICT   SUPPORTED
SUMMARY   The repository is confirmed via both GitHub's license metadata
          and the actual LICENSE file text to be licensed under AGPL-3.0.
          The LICENSE file content retrieved was truncated before the
          specific clause requiring network server operators to provide
          source code [...] The verdict of 'supported' rests on correct
          identification of the license type rather than on a verified
          quotation of the specific clause.

3 steps, stop=sufficient_info, 0 observed / 1 inspected / 1 reported
estimated token cost $0.1087, 24s
```

Or run the whole question set and score it:

```bash
python investigate_batch.py --questions questions.yaml \
    --out reports/batch.md --budget 6
python evaluate.py --investigations
```

```
Proofrun eval: 10/13 questions pass
estimated token cost $4.92, 24 min, 15 observed / 15 inspected / 33 reported
```

---

## What you get back

Asked whether `microsoft/apm` really turns an `apm.yml` manifest into configured
agent files with one command, it read the metadata, decided it had to run the
thing, and built its own test case:

```
s3   sandbox_clone()                       git clone --depth 1 → 54.8 MB
s4   sandbox_setup(pip install -e .)       24.4s, exit 0 → apm-cli 0.30.0
s5   sandbox_run(apm compile ...)          ERROR
s9   sandbox_run(write a minimal apm.yml   ← its own test case, from scratch
     in an empty dir, compile it)
s10  sandbox_run(cat AGENTS.md; find .)    ← did the file really appear?
s11  sandbox_run(copy only the manifest    ← does it reproduce away from
     to a third dir, recompile)               the repo that claimed it?

verdict: supported · 12 steps · estimated token cost $0.475 · 87s
```

Nobody told it to design that experiment. It wrote a manifest in an empty
directory, compiled it, confirmed the output appeared, then copied *only the
manifest* somewhere unrelated and compiled again — testing whether "one file
reproduces it everywhere" survives leaving the repository that said it.

[Full trace, ledger and critique](https://ashvanth11.github.io/proofrun/microsoft-apm.html) ·
[every run, every step](https://ashvanth11.github.io/proofrun/)

---

## Two ways in

**Ask it yourself.** One question, one repository, answer on stdout and in the
database.

```bash
python investigate.py "Does owner/name actually ...?"
```

**Or let the monitor ask.** The sequential pipeline fetches arXiv, GitHub and
Hacker News items, scores them against your interest areas, and writes a themed
brief from up to 100 stored scored items across all dates. Repositories that
score well get a question derived from their README, and the investigation
agent answers it. The brief does not incorporate those investigation verdicts.
`--agent` instead runs a lighter read-only repo agent. `--graph` runs the
watchers in parallel but does not support either agent or `--skip-fetch`.
The weekly workflow discovers up to 10 repositories and investigates at most
one, with a shared UI/Pages feed. It uses the Claude API and requires explicit
repository activation variables.

With Anthropic, direct questions and the weekly investigation use Sonnet;
Haiku scores the weekly discoveries and is used by the legacy monitor's
automatic investigation path. Sonnet writes the brief. Ollama
uses the selected local model for analysis and synthesis; automatic
investigation requires Anthropic's web-search support and the local Docker
sandbox.

```bash
python run.py --investigate --investigate-threshold 0.6
```

---

## What makes the answer trustworthy

Every claim carries a kind and a source tool name:

| Kind | Means |
|---|---|
| `observed` | a command ran in the sandbox and exercised the thing |
| `inspected` | a fact GitHub computed — licence, language, which files exist |
| `reported` | somebody's prose, including the project's own |

Three rules then run in code, after the model has answered:

- A claim citing a tool name absent from the trace is dropped.
- A claim can never be stronger than the tool it cites. Reading a file is not
  running one.
- A verdict of supported or refuted needs at least one first-hand claim, or it
  drops to inconclusive.

That last rule is what makes a hostile README expensive. It can tell the model
to report a claim as proven, and the model may comply — but its text can only
ever be `reported`, so the verdict cannot reach `supported` without something
the agent actually did.

The ledger validates tool names and caps evidence kinds by tool. It does not
bind an entry to the exact invocation or prove that output supports the claim.
Review the trace and output when the distinction matters.

There are no confidence scores. [Why, and what it costs](docs/evidence-ledger.md).

---

## What it won't do

- **Run anything but Python.** The container has python, pip, git, curl and a C
  toolchain. A Rust or Go repository is refused before it is cloned.
- **Reproduce a benchmark.** "Fifty times faster than X" needs both systems and
  a load generator. It will tell you it cannot check that, rather than guess.
- **Pretend.** When it cannot answer, it says `could_not_test` and names what
  stopped it.

---

## How well it works

**10 of 13** recorded questions pass every criterion, at about $4.92 estimated
token cost and 24 minutes of recorded runtime for the full set. These are
process and expectation checks, not a measure of factual accuracy.

| | run 1 | run 2 | run 3 |
|---|---|---|---|
| passing | 6/13 | 8/13 | **10/13** |
| estimated token cost | $4.98 | $4.31 | $4.92 |

No pass has ever regressed, and no question has been edited after seeing
results. The three that still fail are listed with their causes rather than
omitted: [the eval, in full](docs/eval-investigations.md).

---

## Under the hood

**The ladder.** `get_repo_metadata` → `get_repo_description` → `list_files` →
`read_file` → `web_search` → `sandbox_clone` → `sandbox_setup` → `sandbox_run`.
It lives in the tool descriptions, not in code that forces an order — the model
picks the rung, and the eval scores whether it picked the right one.

**The sandbox.** Three verbs, each a fresh `docker run --rm` against one named
volume at `/work`:

| Verb | Network | Timeout |
|---|---|---|
| `sandbox_clone` | bridge | 300s |
| `sandbox_setup` | bridge | 300s |
| `sandbox_run` | **none** | 120s |

Plus `--read-only`, `--user 1000:1000`, `--cap-drop ALL`, `--memory 2g`,
`--pids-limit 256`, `--env-file /dev/null`. Network is a property of the verb,
not an argument the model can pass, and no environment ever crosses the
boundary. The repository under investigation comes from a public feed, so
anyone who can publish one can choose what this executes.
[Threat model and alternatives](docs/decisions.md#6-execution-happens-in-a-disposable-container-with-network-off).

**The caps.** Steps (20), loop token cost, wall clock (900s), sandbox calls (12,
of which 4 may be setup), and disk (2 GB). Cost and wall clock are checked
between model turns, so a call can overshoot. Disk is measured after a command,
so that command can exceed the threshold before the agent stops. A cap that
fires is scored as a failed run.

**Cost control.** A free keyword pre-filter before any model call; content-hash
idempotency for unchanged analysis; and a batch budget checked against a per-question spend
estimate before each question starts. Token costs use configured rates and usage
counts, so they are estimates. The loop cap excludes the overshoot turn,
wrap-up, extraction, critique and web-search charges; the batch estimate includes
allowances for these but is not a guaranteed total-spend limit.

[Architecture, the pipeline, and what is and isn't an agent here](docs/architecture.md).

---

## Setup

Use Python 3.11 for the pinned dependencies and offline CI.

```bash
python3.11 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # add ANTHROPIC_API_KEY, optionally GITHUB_TOKEN
python check_api.py           # verifies the key, generates no tokens
docker build -t ai-monitor-sandbox:latest sandbox/    # only needed for execution
python -m pytest tests/ -q    # offline suite; no network, Docker daemon, or API key
```

Getting an API key, and why the Console is separate from claude.ai:
[docs/api-setup.md](docs/api-setup.md).

### Publishing the traces

`site/` is a complete static site — no build step, no JavaScript, no external
assets. It opens straight from disk:

```bash
python export_traces.py && open site/index.html
```

To publish it on GitHub Pages, push `site/` as the root of a `gh-pages`
branch:

```bash
git subtree push --prefix site origin gh-pages
```

Then **Settings → Pages → Build and deployment → Source → Deploy from a
branch**, set **Branch** to `gh-pages` and the folder to **`/ (root)`**, and
**Save**.

The branch exists because Pages only serves from a repository's root or its
`/docs` folder, and `/docs` here holds the written documentation. The
`.nojekyll` file at the root of `gh-pages` tells Pages to serve the files
byte-for-byte instead of running Jekyll over them, which would otherwise ignore
any path beginning with an underscore. After a new run, regenerate, commit, and
push the subtree again.

---

## Docs

| | |
|---|---|
| [decisions.md](docs/decisions.md) | Design decisions with revisit conditions, including ones later corrected |
| [evidence-ledger.md](docs/evidence-ledger.md) | The three kinds, the three rules, and why there are no confidence scores |
| [eval-investigations.md](docs/eval-investigations.md) | The six criteria, three runs, and every failure |
| [architecture.md](docs/architecture.md) | The pipeline, the loop, the layout, known limitations |
| [testing.md](docs/testing.md) | What the suite caught, and what it pins |
| [eval-findings.md](docs/eval-findings.md) | Stage 1: the models agree with each other, not with the human |
| [api-setup.md](docs/api-setup.md) | Keys, the Console, and billing |
