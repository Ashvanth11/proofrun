# Evaluation findings

48 hand-labeled items. Three scorers compared: the production analyzer
(Haiku 4.5), an independent judge (Sonnet 5), and a human. Total API cost to
produce these numbers: **$0.31**.

The headline is that the harness disproved one of its own design assumptions.

---

## Results

| comparison | Pearson r | MAE |
|---|---|---|
| Haiku analyzer vs human | +0.349 | 0.226 |
| Sonnet judge vs human | +0.257 | 0.298 |
| **Sonnet judge vs Haiku analyzer** | **+0.908** | **0.145** |

Model against model: near-perfect agreement. Either model against the human:
weak. That single contrast drives everything below.

### The local-model baseline, for reference

Development ran on a local model (llama3.1 via Ollama). Same 48 items:

| scorer | Pearson r vs human | distinct score values used |
|---|---|---|
| llama3.1 8B | +0.151 | 7 |
| Haiku 4.5 | +0.349 | 16 |

Moving to Haiku **more than doubled** correlation and roughly doubled score
granularity - llama3.1 clustered on round numbers (0.3, 0.6, 0.8, 0.9) while
Haiku uses values like 0.15, 0.35, 0.65, 0.95. So the small local model *was* a
real limitation for ranking, and this was worth measuring rather than assuming.

---

## The finding: it is the rubric, not the model

If model capability were the remaining limitation, the two models would disagree
with **each other** as well as with the human. They do not - they agree at
r = 0.908 while both diverge from the human at r ≈ 0.26-0.35.

Two models of different sizes, given the same rubric, converge on the same
answers. They are reliably measuring something. That something is not what the
human is measuring.

### Consequence: the judge cannot replace hand-labeling

The eval harness was built on the premise that a strong judge, once validated
against human labels, could score new items without further hand-labeling.

**That premise fails here.** The Sonnet judge agrees with the human *less* than
the Haiku analyzer does (r 0.257 vs 0.349, MAE 0.298 vs 0.226). Using it as a
human proxy would measure model consensus, not human judgment - and would do so
while looking rigorous.

This is worth stating plainly because it is the kind of assumption that usually
goes unchecked. A judge that agrees with your analyzer feels like validation. It
is not; it is two systems sharing a rubric.

---

## Why the human and the models disagree

Ten of 48 items (21%) are cases where both models agree closely with each other
and differ from the human by more than 0.3. Reading them individually separates
two very different causes.

### Rubric gaps - these are real bugs

**"Nvidia agrees to acquire Hugging Face for $13B"** — human 0.9, models ~0.2.

The models are *correct given the rubric*. A $13B acquisition advances none of
the four configured areas (`agents`, `evals-and-safety`, `new-architectures`,
`llm-observability`). It is also obviously major AI news that any user of this
monitor would want surfaced.

The configuration has no category for industry and ecosystem events. That is a
genuine gap, not a personal preference, and it would affect any user.

Same pattern: **"Discovery of a new OpenAI agent message board"** — human 0.9,
models ~0.4.

### Taste - these should not be chased

**"Parameterised graph theory for tensor networks"** — human 0.5, models 0.00,
human note: *"too technical for me"*. The models are right that it is off-topic;
the human score encodes accessibility, which is a property of the reader.

**`Arize-ai/phoenix`** — human 0.6, models **0.90**. Here the models score
*higher*, and they are right: Phoenix is an LLM observability platform, squarely
inside a stated interest area. The human marked it down, plausibly because it
was already familiar. That is novelty *relative to what the reader already
knows* - which a per-item scorer cannot see by construction.

**"CEO fired developers to make room for AI"** — human 0.7, models ~0.2, human
note: *"refreshingly funny desc and novel idea"*. Entertainment value is not in
the rubric and arguably should not be.

---

## What this means for the design

**Do not calibrate the analyzer toward one person's labels.** The r = 0.908
model-model agreement shows the models measure significance-against-the-rubric
*reliably*. Converging on 48 labels from a single session would trade that
reliability for something noisier, less generalizable, and partly encoding
"this labeler already knew about it" - which is not a property of the item.

**Do use the labels as a diagnostic.** They located a real configuration gap
that would otherwise have stayed invisible. That is what a golden set is for:
finding where the specification is wrong, not serving as a target to converge on.

### The distinction worth building around

The current `relevance_score` conflates two questions:

1. **Is this development significant, against the stated interest areas?**
   Objective enough that two different models agree at r = 0.908. The model
   should own this.
2. **Does this particular reader want to read it?** Depends on what they already
   know, their tolerance for technical depth, and genre preference. Only the
   reader can answer it, and a per-item call structurally cannot.

Collapsing both into one number is why the human correlation looks poor: the
scorers are answering question 1 while the human was partly answering question 2.

Separating them - the model scoring significance, with personal preference as a
distinct and cheap re-ranking layer - is a better design and dissolves the
tension rather than tuning against it.

---

## Actions

**Do now:**
- Add an interest area for industry and ecosystem events (acquisitions, major
  releases, funding, notable incidents). This is a specification fix with
  evidence behind it.

**Do not do:**
- Tune the analyzer prompt to raise agreement with these 48 labels. That is
  Goodharting a metric that partly measures reader familiarity.
- Use the Sonnet judge as a substitute for hand labels. Measured here as worse
  than the analyzer at predicting human scores.

**Consider later:**
- Split scoring into significance (model) and preference (reader), rather than
  one conflated number.
- Cross-item context so novelty relative to the week's batch becomes expressible.
  This is a design change, not a prompt change.

---

## Caveats

- **n = 48**, single labeler, single session. Enough to detect a gap this large;
  not enough for fine-grained calibration claims.
- Human scores span only 0.4-0.9 while models span 0.0-0.95. The compressed
  human range mechanically limits achievable correlation and partly explains the
  negative bias.
- Labels were collected against llama3.1-era analyses. They are independent of
  which model scored the items - they are judgments about the items themselves -
  so they remain valid, but the labeling session was not blind to item ordering.
