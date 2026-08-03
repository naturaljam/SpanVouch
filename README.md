<p align="center">
  <img src="assets/spanvouch-logo.png" width="220" alt="SpanVouch logo">
</p>

<h1 align="center">SpanVouch</h1>

<p align="center"><strong>Evidence-backed failure diagnosis infrastructure for production AI agents.</strong></p>

<p align="center">
  <a href="https://github.com/naturaljam/SpanVouch/actions/workflows/ci.yml">CI</a> &middot;
  <a href="https://www.python.org/">Python 3.12</a> &middot;
  <a href="LICENSE">MIT License</a> &middot;
  <a href="paper/IVAD.pdf">IVAD Technical Report</a> &middot;
  <a href="https://github.com/naturaljam/SpanVouch/releases/tag/v0.7.0">v0.7.0</a>
</p>

<p align="center">
  <a href="README.md"><kbd>English</kbd></a>
  <a href="README.zh-CN.md"><kbd>简体中文</kbd></a>
</p>

SpanVouch converts agent execution traces into evidence-bearing diagnosis decisions. It binds causal claims to immutable evidence, rejects structurally invalid reports, separates verification from diagnosis, limits automated revision, preserves human authority, and records every decision in recoverable state. The deterministic path runs offline; provider-backed reasoning requires explicit authorization.

```text
immutable trace -> sanitized evidence -> structured diagnosis
                -> deterministic eligibility -> separated verification
                -> bounded revision or abstention -> human decision
                -> durable state + reproducible artifact
```

[Read the IVAD Technical Report](paper/IVAD.pdf) | [Browse the LaTeX source](paper/source/) | [Download release v0.7.0](https://github.com/naturaljam/SpanVouch/releases/tag/v0.7.0)

## Why agent diagnosis needs an evidence layer

Agent failures do not behave like ordinary exceptions. A decisive error may occur several valid actions before the final symptom, cross model and tool boundaries, or propagate through a multi-agent workflow. A fluent explanation can still cite irrelevant evidence, omit counter-evidence, or repeat a correlated model error.

| Production situation | Research problem | Engineering requirement |
| --- | --- | --- |
| Long-horizon traces obscure the causal step | Localization is not the same as a trustworthy diagnosis | Preserve stable span identity and re-addressable evidence |
| A valid citation may be irrelevant or insufficient | Evidence integrity does not imply semantic support | Check deterministic validity and semantic support in separate channels |
| A second model may share the diagnoser's failure mode | Reviewer agreement does not imply independence | Control verifier inputs, provenance, and failure separation |
| Thresholds trade fewer errors for lower acceptance | Risk must be stated over accepted diagnoses | Freeze selection rules and return no operating point when none is feasible |
| Reviews outlive processes, retries, and deployments | A correct algorithm can still fail operationally | Persist state, enforce idempotency, and recover without duplicating decisions |

## The job SpanVouch performs

SpanVouch turns a failed execution into an operational decision that another engineer can inspect and reproduce. The system must:

- resolve every causal claim to an immutable trajectory field and canonical hash
- reject identity, integrity, temporal, scope, and evidence-coverage violations before semantic review
- keep the diagnoser and optional semantic verifier under separate controls
- allow at most one evidence-guided revision before abstention or human review
- preserve reviewer authority, idempotency, leases, event history, and compare-and-swap state transitions
- bind datasets, configurations, code identity, runtime provenance, outputs, and reports into verifiable artifacts

This task defines the boundary between a plausible model response and an auditable engineering decision.

## Technical core

SpanVouch implements the full diagnosis lifecycle through versioned, independently testable layers.

| Layer | Technical mechanism | Failure behavior |
| --- | --- | --- |
| Trace contract | TraceIR, canonical JSON, stable span selectors, SHA-256 identities | Reject malformed, ambiguous, or mutable evidence inputs |
| Diagnostic context | Sanitized trajectory projection and typed evidence catalog | Exclude credentials, hidden reasoning, labels, and provider-private fields |
| Diagnosis | Rules-first engine with optional provider adapters and bounded causal chains | Fail closed on unsupported output, missing evidence, or invalid provenance |
| Deterministic verification | Identity, hash, temporal, structural, scope, conflict, and coverage checks | Block automatic acceptance regardless of model confidence |
| Separated semantic verification | Optional verifier with controlled context, provider, prompt, and visible rationale | Produce typed findings, request one revision, or defer to a human |
| Review and recovery | Durable SQLite traces and review state, immutable events, leases, idempotency keys, and optimistic concurrency | Resume interrupted work without losing evidence or duplicating decisions |
| Evaluation artifacts | Frozen corpora, manifests, provenance ledgers, deterministic reports, and claim gates | Stop when code, data, authorization, budget, or output identity disagrees |
| Risk-aware acceptance | Frozen finite candidates, simultaneous exact-binomial bounds, minimum accepted groups, and one untouched test evaluation | Return no feasible operating point instead of relaxing the target |

The public contract surface contains six versioned roots: trace, diagnostic context, diagnosis, verification, review, and artifact manifest. Framework and provider adapters remain outside the core dependency direction, so no orchestration framework or model vendor owns the decision contract.

## IVAD research foundation

[IVAD: Evidence-Constrained and Risk-Controlled Failure Diagnosis for AI Agents](paper/IVAD.pdf) is the project Technical Report introducing Independently Verified Agent Diagnosis (IVAD), the protocol that SpanVouch realizes. IVAD asks a narrower and more operational question than failure attribution alone: when may a system accept an evidence-bearing diagnosis, and when must it revise, abstain, or defer?

The Technical Report contributes three connected ideas:

- **Evidence-bearing decision object**: a diagnosis contains bounded causal claims, stable evidence references, status, provenance, and explicit unresolved evidence
- **Separated trust channels**: deterministic integrity, optional semantic support, bounded revision, and human authority cannot silently override one another
- **Group-selective risk protocol**: a frozen finite policy family receives simultaneous one-sided exact-binomial bounds and an explicit no-feasible-point outcome

The formal statement assumes a frozen loss and pipeline, independently sampled preregistered groups, a finite candidate family, simultaneous bounds, a positive minimum accepted-group count, deterministic selection, and one evaluation on untouched test data. SpanVouch supplies the contracts, state machine, adapters, and artifact identities required to execute that protocol.

- [Read the Technical Report](paper/IVAD.pdf)
- [Build from the reproducible LaTeX source](paper/source/)
- [Review report construction and CC BY 4.0 licensing](paper/README.md)
- [Download the versioned PDF and source archive](https://github.com/naturaljam/SpanVouch/releases/tag/v0.7.0)

The v0.7 formal evidence bundle is [published in the repository](evals/reports/reference/phase5-formal-deepseek-only/). It binds aggregate results to evaluated-results SHA-256 `bc09f1b134de9370a3b5209fa5e959bce01abbcdf05c8456af1f069fc4cd3088` and records 2,148 scheduled/evaluated cells with 0 missing cells. B0-B3 completed under DeepSeek-only conditions; B4 and B5 are `policy-skipped` with no Qwen results. H1-H5 remain unresolved.

## Validated engineering evidence

The local Windows candidate verification for v0.7.0 records the following results. This is not a fully green release gate: three tests fail only because Git's CRLF checkout changes byte-for-byte frozen reference assets; the same line-ending limitation was already documented in the v0.6 handoff.

| Validation surface | Observed result |
| --- | --- |
| Evidence-contract benchmark | 36 candidates; 20/20 valid reports accepted; 16/16 injected defects intercepted; 0/20 false blocks |
| Release suite | 1,807 tests collected; 1,803 passed; 1 skipped; 3 CRLF-only frozen-reference failures; 92.14% statement coverage |
| Offline evaluation matrix | 24/24 cells completed across SupportLab, OpsLab, LangGraph, and AutoGen |
| Adapter and parity checks | 4 adapter executions and 2 framework-parity comparisons completed |
| Provider safety | 0 provider calls and 0 GPU calls in the checked-in offline matrix |

The passing measurements establish deterministic contract behavior, injected-defect interception, process recovery, and the exercised delivery paths. The three known failures prevent treating this local candidate as a fully green release gate until the repository's line-ending policy for the older frozen references is resolved. The offline matrix uses declared fake-provider conditions to exercise interfaces and orchestration without presenting those runs as semantic-effectiveness evidence. Provider-backed semantic gains and empirical target-risk attainment require their corresponding result-bearing artifacts.

## What you can build

SpanVouch supplies the decision and evidence layer for systems that must explain agent failures without trusting free-form model output.

| Use case | SpanVouch contribution |
| --- | --- |
| Agent quality platform | Normalize traces, diagnose failures, enforce evidence policy, and compare regressions |
| Production incident review | Preserve the causal record, verifier findings, revisions, and human decision timeline |
| Tool-call governance | Bind claims to exact calls and expose scope, provenance, secret, and integrity violations |
| Framework evaluation | Run the same diagnosis contract across LangGraph, AutoGen, SupportLab, and OpsLab adapters |
| Enterprise audit workflow | Integrate policy gates, authenticated review, immutable events, and durable artifacts |

## Integration surfaces

The release exposes a Python package, command-line interface (CLI), FastAPI application, six JSON Schema contract roots, durable SQLite trace and review storage, Docker Compose delivery, framework adapters, provider adapters, and frozen evaluation assets. The core path stays deterministic and provider-neutral.

| Surface | Included capability |
| --- | --- |
| Python and CLI | Dataset generation, diagnosis evaluation, review evaluation, and review operations |
| HTTP API | Trace ingestion, diagnosis, review creation, recovery, inspection, and human decisions |
| Contracts | Versioned JSON Schema for every public decision object |
| Runtime | Durable traces and review state, leases, idempotency, immutable events, and restart recovery |
| Evaluation | Frozen datasets, multi-framework adapters, manifests, reports, and claim gates |
| Deployment | Locked Python environment, unprivileged container, and persistent Compose volume |

## Run SpanVouch locally

Install Python 3.12 and [uv 0.8.x](https://docs.astral.sh/uv/). Docker Compose v2 is optional.

```bash
git clone https://github.com/naturaljam/SpanVouch.git
cd SpanVouch
uv sync --frozen --group dev
uv run spanvouch dataset generate --output .cache/readme-check --seed 20260715
uv run spanvouch evaluate diagnosis --output .cache/rules.json
uv run spanvouch evaluate review --output .cache/review-rules.json
```

Verify a release handoff entirely offline from the checkout:

```bash
uv run spanvouch release verify --repo-root . --expected-version 0.7.0
```

Start the API:

```bash
uv run uvicorn spanvouch.api.app:app --host 127.0.0.1 --port 8000
```

OpenAPI is available at `http://127.0.0.1:8000/docs` while the service runs.

## Production security workflow

SpanVouch v0.5 adds the reproducible evidence handoff expected from a self-hosted production service: manifest-bound analysis preparation, DeepSeek-only formal execution accounting, explicit policy skips, and fail-closed claim gates.

All routes except `/health` and `/ready` require `Authorization: Bearer <api-key>`. API keys are shown once, stored only as salted `scrypt` digests, and can be rotated or revoked without changing project data. System administrators use the management API or CLI to create isolated projects and project-bound operator, reviewer, or viewer keys.

```bash
bootstrap="$(uv run spanvouch admin bootstrap --database .data/spanvouch.db)"
export SPANVOUCH_API_KEY="$(python -c 'import json,sys; print(json.loads(sys.argv[1])["api_key"])' "$bootstrap")"

uv run spanvouch admin project create --name production-agents
uv run spanvouch admin project list

uv run spanvouch admin key create \
  --project-id "$PROJECT_ID" \
  --roles operator,reviewer

uv run spanvouch admin key rotate --key-id "$KEY_ID"
uv run spanvouch admin key revoke --key-id "$KEY_ID"
```

Set `SPANVOUCH_AUDIT_SIGNING_KEY_PATH` to an Ed25519 private-key PEM and optionally set `SPANVOUCH_AUDIT_EXPORT_DIR` before creating signed audit bundles. The private key is read from disk for signing only; it is never returned by the API or written into an export.

```bash
uv run spanvouch admin audit export --project-id "$PROJECT_ID"
uv run spanvouch admin audit verify --bundle .data/audit-exports/"$EXPORT_ID"
```

Each export contains `manifest.json`, `events.jsonl`, `checkpoints.json`, `public-key.pem`, and `README.md`. Offline verification checks file hashes, event-chain continuity, checkpoint signatures, the public-key binding, and the terminal event hash without database access or provider credentials.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/health` | Report service health |
| POST | `/v1/traces` | Ingest a TraceIR document |
| POST | `/v1/traces/{trace_id}/diagnoses` | Diagnose a trace |
| POST | `/v1/traces/{trace_id}/diagnosis-reviews` | Create a review case |
| GET | `/v1/diagnosis-reviews/{case_id}` | Read the case timeline |
| POST | `/v1/diagnosis-reviews/{case_id}/resume` | Resume recoverable work |
| POST | `/v1/diagnosis-reviews/{case_id}/decisions` | Record a human decision |
| POST | `/v1/admin/projects` | Create an isolated project |
| POST | `/v1/admin/projects/{project_id}/api-keys` | Create a project API key |
| POST | `/v1/admin/api-keys/{key_id}/rotate` | Rotate an API key |
| POST | `/v1/admin/api-keys/{key_id}/revoke` | Revoke an API key |
| POST | `/v1/admin/projects/{project_id}/audit-exports` | Create a signed audit export |
| GET | `/v1/admin/audit-exports/{export_id}` | Inspect an audit export record |

The diagnosis endpoint is `POST /v1/traces/{trace_id}/diagnoses`. Rules-based diagnosis needs no provider key. Provider-backed diagnosis also requires `DEEPSEEK_API_KEY` and `--allow-live-api`.

## Run an offline review end to end

Use the frozen SupportLab trace at `evals/datasets/supportlab-v1/traces.jsonl`. Post the selected trace to `POST /v1/traces`, then create, inspect, and decide the review case:

```bash
trace_id="$(curl --fail --silent --show-error -H 'content-type: application/json' \
  -H "Authorization: Bearer $SPANVOUCH_API_KEY" \
  --data-binary @.cache/spanvouch-demo-trace.json http://127.0.0.1:8000/v1/traces \
  | python -c 'import json,sys; print(json.load(sys.stdin)["trace_id"])')"
created="$(uv run spanvouch review create --trace-id "$trace_id" --idempotency-key demo-create-001)"
case_id="$(python -c 'import json,sys; print(json.loads(sys.argv[1])["case"]["case_id"])' "$created")"
version="$(python -c 'import json,sys; print(json.loads(sys.argv[1])["case"]["version"])' "$created")"
uv run spanvouch review show --case-id "$case_id"
uv run spanvouch review decide --case-id "$case_id" --action confirm --expected-version "$version" \
  --reviewer-label local-reviewer --idempotency-key demo-decision-001
```

## Run with Docker

Docker Compose builds the locked image, starts the API as an unprivileged user, and stores traces and review state in a persistent SQLite volume.

```bash
docker compose up --build --detach --wait api
curl --fail http://127.0.0.1:8000/health
bootstrap="$(docker compose exec -T api spanvouch admin bootstrap --database /data/spanvouch.db)"
export SPANVOUCH_API_KEY="$(python -c 'import json,sys; print(json.loads(sys.argv[1])["api_key"])' "$bootstrap")"
docker compose down
```

## Control provider access

Rules-based diagnosis and deterministic verification never require a provider key. DeepSeek diagnosis and hybrid semantic verification require `DEEPSEEK_API_KEY` plus the explicit `--allow-live-api` flag. Live calls may incur cost and remain outside continuous integration (CI). Place the API behind an authenticated gateway before exposing it beyond localhost.

## Commercial deployment

The MIT-licensed core supports private deployment, agent quality and audit platforms, policy integration, multi-team or multi-model adapters, managed hosting, and enterprise support. Versioned contracts and provider-neutral workflow boundaries let teams replace models or frameworks without rewriting the evidence and decision layer.

## Repository layout

```text
src/spanvouch/   contracts, trace, diagnosis, verification, review, API, CLI
schemas/v1/      versioned public JSON Schema contracts
tests/           unit, contract, architecture, integration, and E2E tests
evals/           frozen datasets, configurations, and reference reports
paper/           IVAD Technical Report, reproducible source, and build notes
```

## Cite IVAD and SpanVouch

GitHub reads [`CITATION.cff`](CITATION.cff) and exposes the repository's citation metadata. Cite the IVAD Technical Report when your work depends on the protocol, formalization, or evaluation design:

```bibtex
@techreport{liu2026ivad,
  title  = {IVAD: Evidence-Constrained and Risk-Controlled Failure Diagnosis for AI Agents},
  author = {Liu, Hanzhe},
  year   = {2026},
  url    = {https://github.com/naturaljam/SpanVouch/blob/main/paper/IVAD.pdf}
}
```

## Contribute and license

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md) before opening an issue or pull request. SpanVouch software is available under the [MIT License](LICENSE). The IVAD Technical Report, figures, and source use [CC BY 4.0](paper/README.md).
