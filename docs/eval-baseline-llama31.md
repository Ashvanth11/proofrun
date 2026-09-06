# Analyzer Evaluation

Generated 2026-09-06 07:21 UTC. Golden set: 48 hand-labeled items.

Mean bias is the signed average of (score - human score): positive means the scorer is too generous.

### Analyzer vs human labels

| metric | value |
|---|---|
| items | 48 |
| mean absolute error | 0.240 |
| RMSE | 0.295 |
| Pearson r | 0.150 |
| binary agreement (relevant vs not) | 0.625 |
| precision | 1.000 |
| recall | 0.600 |
| mean bias | -0.144 |


### Analyzer calibration by score band

| analyzer band | n | mean analyzer | mean human | bias |
|---|---|---|---|---|
| 0.0-0.3 | 1 | 0.20 | 0.50 | -0.30 |
| 0.3-0.6 | 20 | 0.30 | 0.68 | -0.37 |
| 0.6-0.9 | 19 | 0.67 | 0.70 | -0.03 |
| 0.9-1.0 | 8 | 0.90 | 0.72 | +0.18 |


### Largest disagreements

Where the analyzer diverged most from your labels - the source of prompt fixes.

- **deeplethe/utopia** (-0.60: analyzer 0.30, you 0.90)  
  Utopia is an open-source enterprise world model that integrates various components for building a comprehensive knowledge graph.
- **GPT-6 Astra** (-0.60: analyzer 0.30, you 0.90)  
  GPT-6 Astra is a system card for OpenAI's GPT-6 model, which has made significant gains in the Artificial Analysis Coding Agent Index.
- **Discovery of a new OpenAI agent message board** (-0.60: analyzer 0.30, you 0.90)  
  A German website was hijacked by OpenAI agents in 2026, highlighting a previously undisclosed AI breakout.
- **Nvidia agrees to acquire Hugging Face for $13B** (-0.60: analyzer 0.30, you 0.90)  
  Nvidia agrees to acquire Hugging Face, an open-source model repository, for $13B.
- **zenml-io/zenml** (-0.50: analyzer 0.30, you 0.80)  
  ZenML is an open-source AI platform that integrates pipelines and agents for automating data science workflows.

---

## After the thin-content prompt fix

Superseded by `reports/eval-report.md`. Retained as the before-state for the
first calibration pass. Same 48 hand-labeled items, same model
(ollama/llama3.1), single variable changed: the analyzer system prompt was
told to score the significance of a development rather than the amount of
detail supplied about it.

| metric | before | after |
|---|---|---|
| mean absolute error | 0.240 | 0.223 |
| Pearson r | 0.150 | 0.151 |
| binary agreement | 62.5% | 72.9% |
| mean bias | -0.144 | -0.040 |

Per source:

| source | n | bias before | bias after | MAE before | MAE after |
|---|---|---|---|---|---|
| arxiv | 20 | -0.08 | +0.03 | 0.185 | 0.195 |
| github | 18 | -0.11 | +0.02 | 0.222 | 0.189 |
| hn | 10 | -0.32 | -0.28 | 0.380 | 0.340 |

### Reading this honestly

**Bias was corrected; correlation was not.** Overall bias fell from -0.144 to
-0.040 and per-source bias is near zero for arXiv and GitHub. Binary agreement
rose 10 points, because correcting the bias moved items to the correct side of
the relevance threshold.

Pearson r did not move (0.150 -> 0.151). Correlation measures whether the
analyzer *ranks* items the way the human does, and shifting every score upward
changes no rankings. llama3.1 still cannot discriminate within a source.

**The hypothesis was partly wrong.** HN was the target of the fix and improved
least (bias -0.32 -> -0.28). So the HN error is not purely a thin-content
artifact; the model scores HN items poorly for reasons the prompt change did
not address. Looking only at the headline MAE would have made this look like a
clean success.

### What the human annotations revealed

Six labeled items carried notes, and they show criteria absent from the prompt
entirely:

- *"interesting in a way that it is different from other items, many of which
  are similar"* - novelty **relative to the batch**. A per-item call scores each
  item in isolation and structurally cannot see this.
- *"opinion piece that is not quite fit with the scope"*, *"paywall article...
  seems basic"* - genre and depth filtering.
- *"too technical for me"* - personal accessibility.

Two of these are addressable in a prompt. Batch-relative novelty is not, without
giving the analyzer cross-item context - which is a design change, not a prompt
change.

### Next experiment

Re-run this comparison with Haiku 4.5 rather than llama3.1. The open question is
whether r ~= 0.15 is a property of the small local model or of the task as
currently framed. If Haiku's correlation is materially better, the local model is
the limitation; if it is not, the rubric is.
