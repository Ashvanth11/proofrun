# Proofrun eval

10 of 13 questions pass every criterion. $4.92, 24 min.

Verdicts: 4 could_not_test, 2 inconclusive, 1 refuted, 6 supported.

Evidence: 15 observed, 15 inspected, 33 reported. Grounding critique ran on 13 of 13.

A question passes only if every criterion holds: the verdict is one it allows, the sandbox did what it should have, every ledger entry cites a real tool call, the run stopped because it was finished rather than capped, and no verdict had to be downgraded.

| Question | Category | Verdict | Obs | Insp | Rep | Cmds | Cost | Pass | Failed |
|---|---|---|---:|---:|---:|---:|---:|:--:|---|
| Is firecrawl/firecrawl licensed under terms that would oblige a company embedding it in a ... | reading | supported | 0 | 1 | 1 | 0 | $0.109 | yes | - |
| Is langfuse/langfuse actually open source under a recognised OSI licence, or is some of wh... | reading | inconclusive | 0 | 1 | 4 | 0 | $0.203 | NO | not_downgraded |
| Does promptfoo/promptfoo actually ship red-teaming plugins in its source tree, or is red t... | reading | supported | 0 | 3 | 1 | 0 | $0.117 | yes | - |
| Does deeplethe/utopia actually contain a trained world model, or is "world model" a label ... | overclaim | refuted | 0 | 1 | 2 | 0 | $0.288 | yes | - |
| Does microsoft/apm actually turn an apm.yml manifest into configured agent files with a si... | execution | supported | 3 | 0 | 1 | 4 | $0.475 | yes | - |
| Can zenml-io/zenml be installed from source and initialise a local project with a working ... | execution | could_not_test | 3 | 2 | 3 | 1 | $0.272 | NO | stop_reason |
| Can Arize-ai/phoenix's core tracing be started locally, with no Arize account or hosted se... | execution | supported | 3 | 1 | 2 | 2 | $0.711 | yes | - |
| Does MakazhanAlpamys/Soup install from source and expose a working command-line interface ... | execution | supported | 4 | 2 | 2 | 3 | $0.997 | yes | - |
| Can comet-ml/opik's Python SDK be installed and used to record a trace locally, without a ... | execution | supported | 2 | 0 | 2 | 0 | $0.530 | yes | - |
| Does 0xPlaygrounds/rig actually build and run a working LLM agent from its own quickstart ... | could_not_test | could_not_test | 0 | 2 | 3 | 0 | $0.358 | yes | - |
| Is maximhq/bifrost actually fifty times faster than LiteLLM, as its description claims? | could_not_test | could_not_test | 0 | 0 | 3 | 0 | $0.163 | yes | - |
| Does BerriAI/litellm actually translate a call for a non-OpenAI provider into the OpenAI r... | could_not_test | inconclusive | 0 | 1 | 4 | 0 | $0.354 | yes | - |
| Does conorbronsdon/avoid-ai-writing ship a detector that actually runs and flags AI-writin... | could_not_test | could_not_test | 0 | 1 | 5 | 0 | $0.340 | NO | blockers |

## Where it failed

- **not_downgraded**: 1
- **stop_reason**: 1
- **blockers**: 1

### langfuse/langfuse
- `not_downgraded` - the integrity rules had to downgrade the verdict

### zenml-io/zenml
- `stop_reason` - stopped on 'sandbox_cap' - a capped run is unfinished, not answered

### conorbronsdon/avoid-ai-writing
- `blockers` - could_not_test named ['other'], none of which is in ['unsupported_language']
