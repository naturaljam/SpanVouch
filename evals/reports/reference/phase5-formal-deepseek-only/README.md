# Phase 5 Formal DeepSeek-only Evidence

This is the minimal public aggregate evidence bundle for the v0.7 Technical
Report. It was copied from the read-only Phase 5 formal analysis output and
contains no credentials, raw provider payloads, per-request traces, or Qwen
responses.

## Provenance

- Evaluated-results manifest SHA-256:
  `bc09f1b134de9370a3b5209fa5e959bce01abbcdf05c8456af1f069fc4cd3088`
- Analysis input SHA-256:
  `dfb07f4d09a5a55eb098397ac6cb8628125a3f630517d92a0f6399f2755fe4f6`
- Analysis seed: `20260720`
- Bootstrap draws: `10000`
- Scheduled/evaluated: `2148/2148`
- Missing cells: `0`

The copied source manifests and per-file SHA-256 values are recorded in
`manifest.json`. The source analysis manifest is retained as
`source-analysis-manifest.json`; the input binding is retained as
`source-analysis-input-manifest.json`.

The source analysis manifest records `claim-gates.json` by its canonical JSON
content hash (`21d960...1205`), excluding the trailing newline. The public
bundle manifest additionally records the raw tracked-file hash
(`ede1b0...43cb`) so both the semantic JSON identity and exact repository bytes
remain verifiable.

## Observed formal results

The formal run is DeepSeek-only. B0--B3 completed. B4 (isolated Qwen) and B5
(deterministic plus isolated Qwen) were `policy-skipped`; no Qwen result exists.

For the SupportLab paired comparison, B3 versus B2 has an observed risk
difference of `-0.102391` with a 95% interval `[-0.165462, -0.046525]`, and an
observed coverage difference of `-0.254190` with a 95% interval
`[-0.358543, -0.155989]`. For the OpsLab preliminary replication, the observed
risk difference is `-0.116192` with a 95% interval `[-0.206115, -0.032821]`.

These are observed DeepSeek-only risk/coverage tradeoffs. They are not causal
proof, cross-model evidence, an independence guarantee, or an empirical
target-risk certificate. The H1--H5 claim gates remain `unresolved`.

`main-results.md`, the CSV files, `claim-gates.json`, and `risk-coverage.svg`
are copied verbatim from the aggregate analysis directory. Reproduction still
requires the separately authorized provider run; this bundle itself is
offline-readable and does not invoke a provider.
