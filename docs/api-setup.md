# Setting up the Anthropic API

## First, the thing that trips people up

**The Anthropic Console is a separate product from claude.ai.** They can use the
same email and still have separate balances.

- **claude.ai** — your Claude Code / chat subscription. Its "usage credits"
  (including promotional ones) cover *your* use of Claude, and top up your plan
  when you exceed its limits.
- **console.anthropic.com** — the developer API. This is what your Python code
  calls. It has its own billing and its own balance.

Credit on one does **not** pay for the other. A promotional credit sitting on
claude.ai cannot fund this project's API calls, no matter how large it is.

---

## Steps

### 1. Create a Console account

Go to **console.anthropic.com** and sign in. You can use the same email as your
claude.ai account — it will still be a distinct workspace with distinct billing.

If prompted to create an organization, do so. A personal one is fine.

### 2. Add a payment method and buy credits

In the Console, go to **Billing** (or Plans & Billing).

Add a card, then purchase credits. **$25 is plenty** for this project — the
estimate for the whole build was $25–50, and most of the work has already been
done for free on the local model, so your real remaining spend is far smaller
(see [What you'll actually spend](#what-youll-actually-spend) below).

Anthropic uses a prepaid credit model: you buy a balance and calls draw it down.
It does not silently bill beyond what you have loaded, which is the behaviour you
want here.

### 3. Set a spend limit (do this — it takes a minute)

Still in Billing, look for a **spend limit** or **usage limit** setting and set a
monthly cap. Something like $20 is sensible.

This is your safety net. The main way a project like this loses money
unexpectedly is a bug in a loop — and while this codebase enforces a per-repo
cost cap in the agent, a Console-level limit protects you from anything the
application-level cap doesn't catch. Turn **auto-reload off** unless you
specifically want it.

### 4. Create an API key

Go to **API Keys** and create one. Give it a name you'll recognise later
(`ai-monitor-local`, say).

**Copy it immediately** — the Console shows the full key exactly once. It looks
like `sk-ant-api03-...` followed by a long string.

If you lose it, delete that key and make a new one; there's no way to view an
existing key again.

### 5. Put it in `.env`

From the project root:

```bash
cp .env.example .env       # if you haven't already
```

Then edit `.env` and set:

```
ANTHROPIC_API_KEY=sk-ant-api03-your-actual-key-here
```

No quotes, no spaces around the `=`.

`.env` is already in `.gitignore`, so it will not be committed. **Never** paste
the key into a source file, a commit, or an issue.

### 6. Verify it works — for free

```bash
python check_api.py
```

This uses the token-counting endpoint, which authenticates your key **without
generating any tokens**, so it costs nothing. It tells you whether the key is
valid before you spend anything.

---

## What you'll actually spend

The expensive work — building and debugging the pipeline — is already done, and
was done on the local model at zero cost. What remains is small:

| Task | Model | Rough cost |
|---|---|---|
| Re-analyze 48 labeled items | Haiku 4.5 | ~$0.02 |
| Judge 48 items for the eval | Sonnet 5 | ~$0.15 |
| One weekly brief (synthesis) | Sonnet 5 | ~$0.04 |
| Agent investigating ~5 repos | Sonnet 5 | ~$0.50 |

A full weekly run with the agent enabled lands around **$1–2**. The $25 is
mostly headroom, not expected spend.

Pricing at time of writing: Haiku 4.5 is $1/$5 per million input/output tokens,
Sonnet 5 is $2/$10. Re-check current pricing in the Console — it changes, and any
cost figure you put in a writeup should come from your own token logs rather than
from this table.

---

## Then run it

```bash
# Re-analyze the labeled items on Haiku (the local scores get replaced -
# the model is part of the cache key, so this genuinely re-runs)
python run.py --provider anthropic --skip-fetch

# Judge the golden set and report both comparisons
python evaluate.py
```

That produces `reports/eval-report.md` with the numbers for your README.

The question this answers is specific: the local model scored **Pearson
r = 0.151** against your labels. If Haiku's correlation is materially better, the
small local model was the limitation. If it isn't, the rubric is — meaning your
scoring criteria and the prompt's differ in ways no model swap fixes. Either
answer is a real finding.

---

## Optional: a GitHub token

Not required. It raises the GitHub API limit from 60 to 5,000 requests/hour,
which only matters when the agent is reading repository files repeatedly during
debugging.

Create one at **github.com/settings/tokens**. For reading public repositories it
needs **no scopes at all** — a classic token with nothing checked, or a
fine-grained token with read-only public access. Do not grant write scopes; the
code only ever reads.

Add it to `.env` as `GITHUB_TOKEN=...`.

---

## If something goes wrong

**`AuthenticationError` / 401** — the key is wrong, or you're using a claude.ai
credential rather than a Console API key. Check it starts with `sk-ant-api`.

**`PermissionDeniedError` / 403** — the key is valid but lacks access, or the
workspace has no credit. Check the Console balance.

**"credit balance is too low"** — you have a key but no purchased credits.
Step 2.

**The key works but nothing re-analyzes** — that was a real bug, now fixed: the
model and the system prompt are both part of the cache key, so switching from
`ollama/llama3.1` to Haiku re-runs every item. If you still see "skipped
(unchanged)" everywhere, you're probably passing `--provider ollama`.

**For scheduled runs:** the key goes in the GitHub repo's
*Settings → Secrets and variables → Actions* as `ANTHROPIC_API_KEY`, not in the
repo itself. The workflow in `.github/workflows/weekly-brief.yml` reads it from
there.
