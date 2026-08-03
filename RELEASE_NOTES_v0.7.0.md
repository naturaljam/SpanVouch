# SpanVouch v0.7.0

SpanVouch v0.7.0 publishes the IVAD Technical Report update and the latest
aggregate formal evidence under the existing DeepSeek-only boundary.

## Release contents

- Recasts the IVAD publication-facing artifact as a Technical Report while
  retaining the title `IVAD: Evidence-Constrained and Risk-Controlled Failure
  Diagnosis for AI Agents`.
- Publishes a minimal, hash-bound aggregate evidence bundle for the formal run:
  2,148 scheduled/evaluated plans and 0 missing cells.
- Records B0-B3 as completed DeepSeek-only conditions; B4/B5 remain
  `policy-skipped` with no Qwen results.
- Reports the observed B3-vs-B2 risk/coverage tradeoff and preliminary OpsLab
  observation without causal, cross-model, or certificate language.
- Keeps H1-H5 claim gates `unresolved`, preserves the v0.5 experiment boundary,
  and adds no provider, network, paid call, or runtime dependency.

This local preparation does not create a tag, GitHub Release, remote push, or
paid provider invocation.
