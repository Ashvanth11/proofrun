# Proofrun eval

6 of 13 questions pass every criterion. $4.98, 18 min.

Verdicts: 5 could_not_test, 4 inconclusive, 1 refuted, 3 supported.

Evidence: 12 observed, 14 inspected, 28 reported. Grounding critique ran on 13 of 13.

A question passes only if every criterion holds: the verdict is one it allows, the sandbox did what it should have, every ledger entry cites a real tool call, the run stopped because it was finished rather than capped, and no verdict had to be downgraded.

| Question | Category | Verdict | Obs | Insp | Rep | Cmds | Cost | Pass | Failed |
|---|---|---|---:|---:|---:|---:|---:|:--:|---|
| Is firecrawl/firecrawl licensed under terms that would oblige a company embedding it in a ... | reading | inconclusive | 0 | 0 | 2 | 0 | $0.120 | NO | verdict, not_downgraded |
| Is langfuse/langfuse actually open source under a recognised OSI licence, or is some of wh... | reading | inconclusive | 0 | 1 | 3 | 0 | $0.165 | NO | not_downgraded |
| Does promptfoo/promptfoo actually ship red-teaming plugins in its source tree, or is red t... | reading | supported | 0 | 3 | 3 | 0 | $0.210 | yes | - |
| Does deeplethe/utopia actually contain a trained world model, or is "world model" a label ... | overclaim | inconclusive | 0 | 3 | 1 | 0 | $0.315 | NO | verdict |
| Does microsoft/apm actually turn an apm.yml manifest into configured agent files with a si... | execution | supported | 3 | 0 | 1 | 1 | $0.412 | yes | - |
| Can zenml-io/zenml be installed from source and initialise a local project with a working ... | execution | could_not_test | 1 | 1 | 1 | 1 | $0.297 | NO | stop_reason |
| Can Arize-ai/phoenix's core tracing be started locally, with no Arize account or hosted se... | execution | inconclusive | 0 | 1 | 2 | 1 | $0.449 | NO | verdict, execution |
| Does MakazhanAlpamys/Soup install from source and expose a working command-line interface ... | execution | supported | 4 | 0 | 2 | 1 | $0.698 | yes | - |
| Can comet-ml/opik's Python SDK be installed and used to record a trace locally, without a ... | execution | refuted | 3 | 0 | 1 | 0 | $0.480 | yes | - |
| Does 0xPlaygrounds/rig actually build and run a working LLM agent from its own quickstart ... | could_not_test | could_not_test | 0 | 1 | 2 | 2 | $0.289 | NO | execution |
| Is maximhq/bifrost actually fifty times faster than LiteLLM, as its description claims? | could_not_test | could_not_test | 0 | 1 | 2 | 0 | $0.514 | yes | - |
| Does BerriAI/litellm actually translate a call for a non-OpenAI provider into the OpenAI r... | could_not_test | could_not_test | 0 | 1 | 3 | 0 | $0.460 | yes | - |
| Does conorbronsdon/avoid-ai-writing ship a detector that actually runs and flags AI-writin... | could_not_test | could_not_test | 1 | 2 | 5 | 2 | $0.572 | NO | execution |

## Where it failed

- **verdict**: 3
- **execution**: 3
- **not_downgraded**: 2
- **stop_reason**: 1

### firecrawl/firecrawl
- `verdict` - got 'inconclusive', expected one of ['supported']
- `not_downgraded` - the integrity rules had to downgrade the verdict

### langfuse/langfuse
- `not_downgraded` - the integrity rules had to downgrade the verdict

### deeplethe/utopia
- `verdict` - got 'inconclusive', expected one of ['refuted', 'could_not_test']

### zenml-io/zenml
- `stop_reason` - stopped on 'sandbox_cap' - a capped run is unfinished, not answered

### Arize-ai/phoenix
- `verdict` - got 'inconclusive', expected one of ['supported', 'could_not_test']
- `execution` - needed either an observed entry or could_not_test with a named blocker; got verdict='inconclusive' blockers=['other']

### 0xPlaygrounds/rig
- `execution` - should not have used the sandbox, ran 2 command(s)

### conorbronsdon/avoid-ai-writing
- `execution` - should not have used the sandbox, ran 2 command(s)
