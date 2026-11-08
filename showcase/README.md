# Selected findings

This directory contains the public evidence snapshots and curated presentation
for Proofrun's homepage. It is separate from the unfiltered operational archive.
No provider request transcripts, thinking blocks, credentials, local database
paths, or private planning notes belong here.

`findings.json` selects dated case studies. A record is eligible only when its
stored verdict is supported/refuted, it was not downgraded, and a separate
scope review checked the actual tool results. Set `manually_reviewed` only after
that pass. This flag denotes the extra evidence review; it does not establish
independent replication or a human reviewer. Every selected record needs a
narrow summary, concrete checks, explicit limitations, and a review note.
Each card also supplies `repo_description`, `claim`, and `test_description` so
readers can understand the project and the experiment before opening the trace.

Repository discovery is populated independently from saved monitoring state.
The newest captured batch can be displayed even if later screening failed.
Descriptions remain attributed to GitHub, and star counts are recorded
popularity snapshots rather than an estimate of growth or current trending.

The automatic report remains unedited in its source JSON. Presentation can
narrow unsupported wording, but must not fabricate a stronger result. Preserve
the previous source when rerunning a historical question. Export generates
separate dated pages and JSON downloads for both the new and old records.
Machine-specific identifiers in public traces may be redacted, with every
redaction described in the snapshot's `redactions` field and on its evidence
page. The local original database remains unchanged.

The homepage intentionally selects conclusive reviewed work. Do not describe
that selection as the full result distribution or calculate a success rate from
it. Inconclusive and blocked results remain in `monitoring.html`; historical
benchmark traces remain in `history.html`. The JSON monitoring feed remains
unfiltered by verdict.

To regenerate from a saved monitoring artifact:

```sh
python export_monitoring.py --db /path/to/monitor.db --run-record /path/to/last-run.json --out site
```

This exports locally. It does not dispatch models or publish. Check sources,
links, escaping, desktop/mobile appearance, and scope before publishing.

## Automatic case studies

The weekly workflow can add approved records from the durable artifact's
`publication-state/` directory. It uses a separate publication review and marks
these records `manually_reviewed=false` with an `automated_review` stamp. The
stamp binds the reviewed draft to a source hash, policy version, model, date
and per-claim evidence checks. Export rejects changed copy or evidence, invalid
references, nonconclusive reports, and missing approvals. The homepage labels
this route **automated review**; it does not claim human review.

Automatic state is restored alongside the monitoring database and survives
subsequent deploys; it is not committed into this directory by the workflow.
Existing source-controlled case studies remain available. Private raw reviewer
responses, failed drafts and one-use tickets are never exported.
