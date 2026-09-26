# Proofrun portfolio release notes

Prepared 2026-09-25. Publication and live link verification are tracked in
[weekly monitoring](weekly-monitoring.md) and the internal release handoff.

- Added a one-page Streamlit app with Monitoring and Ask it yourself modes,
  three credential-free recorded examples, and a viewer for locally saved
  direct questions. New direct investigations use the existing engine.
- Added a bounded Claude weekly monitoring path: up to ten discoveries,
  at most one investigation, durable weekly state, and a shared public feed.
  Other discovered repositories are labeled **Not investigated**. The first
  bootstrap run completed with ten discoveries and one investigation.
- Refreshed the static showcase and README with a five-image walkthrough,
  dated results, evidence and limitations, historical traces, and local setup.
- Hardened CLI flag validation, cost wording, evidence presentation, targeted
  file reading, and critique handling. The local offline suite passes with
  538 tests and one optional Docker-image test skipped.

The 10/13 investigation score is from a small historical, self-authored set of
process checks; it is not a factual accuracy claim. A ledger checks source-tool
names and evidence kinds, not exact invocation provenance or semantic truth.
Costs are estimates. Gemini evaluation remains inconclusive and separate from
the selected Claude weekly provider.
