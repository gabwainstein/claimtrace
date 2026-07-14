# Changelog

All notable changes to `claimtrace` are documented here. This project adheres to
[semantic versioning](https://semver.org/).

## Unreleased

### Added
- Add `claimtrace run` with explicit input/output roles, shell-free direct-child execution, stable
  pre/post SHA-256 snapshots, failed-run receipts, secret-flag redaction, best-effort Git/lockfile
  context, and an unattributed project-wide change window.
- Store start/finish receipts as content-addressed canonical JSON events with atomic cross-process
  append behavior and recoverable out-of-worktree active markers.
- Add `claimtrace check --strict --json`, a deterministic single-document graph/receipt report for
  agents and CI, including declared-input reconciliation, missing receipts, output drift, incomplete
  runs, modification of surviving event files, and possible undeclared outputs.
- Add a deterministic standalone research-trajectory view with semantic graph and mechanical receipt
  layers.
- Recognize `question`, `hypothesis`, `prediction`, and `conclusion` nodes plus `motivates`,
  `predicts`, `tested_by`, and `concludes` dependency relations.
- Let semantic nodes explicitly cite mechanical executions with `run_ids`, including pathless null
  and dead-end results, while rejecting missing receipts and incompatible successful-result status.
- Package the `claimtrace-log` agent skill and add overwrite-safe `claimtrace install-skill` support
  for shared `.agents` and Claude project-local layouts.
- Add immutable, content-addressed semantic assessments with schema-constrained external-agent
  input, exact JSON/text evidence anchors, claimtrace-computed node and artifact snapshots, and
  deterministic staleness, modality, alignment, and conflict findings.
- Add `claimtrace assess`, `claimtrace assessments`, and `claimtrace review` for a proposal-to-
  independent-review workflow, with accepted relations projected into strict reports and the
  standalone trajectory view.
- Add checked-in widget-study assessments showing accepted support for an associational claim and
  an accepted `supports_narrower_claim` judgement that keeps causal language at `related` rather
  than silently upgrading it to support.
- Add an optional data-only symbolic claim layer with typed project vocabularies, explicit-polarity
  function-free rules, complete result-to-fact bindings, graph-pinned formal targets, finite
  open-world/paraconsistent evaluation, and composite content-addressed proof certificates.
- Add `claimtrace.symbolic-selection/1` as the high-level fallback for claims without an evidence
  plan: users and agents select only existing result/binding IDs while Claimtrace materializes typed
  artifact values plus the claim-pinned target and policy. Retain the explicit atom format as a
  low-level import, debugging, and assumption interface.
- Add claim-owned exact all-of `claimtrace.symbolic-evidence-plan/1` declarations,
  claim-only `claimtrace.symbolic-plan-request/1` proposals, and `claimtrace evidence-plan` preview.
  Planned claims fail closed when grounded premises omit, add, or replace a required binding or add
  an assumption.
- Add `claimtrace derive`, `claimtrace derivations`, and `claimtrace explain`, structured live drift
  records, configurable streamed provenance hashing, strict optional derivation coverage, and
  symbolic proof/conflict nodes in the standalone trajectory view.
- Group equivalent active submissions by canonical `proof_id`, preserve their derivation history,
  and detect opposing active proofs for the same formal target as a hard claim-level conflict.

### Fixed
- Scope node backbones to named concepts when a graph has multiple independent canonical choices;
  retain scalar backbones for single-concept graphs and fail closed on ambiguous mappings.
- Return `impact` results in a stable topological order rather than a type-only order.
- Reconcile every render manifest against the graph's complete declared input set, reject missing or
  malformed manifests, detect canonical relabeling, verify the recorded output hash, and propagate
  content drift through current and confirmed downstream results.
- Check claim/evidence backbone compatibility across normalized transitive dependencies.
- Validate logged edges before mutation, serialize local writers with a cross-process lock, and
  replace the graph atomically so invalid or concurrent entries cannot be silently lost.
- Detect current input drift as well as output drift from the latest successful bound receipt;
  surface capture-contract and claimtrace control-plane mutations in the strict report.
- Reject internally contradictory receipts, including impossible outcome/return-code combinations,
  inconsistent file transitions, start/finish input-snapshot mismatches, and unsupported claims of
  complete or causally observed runtime coverage.
- Warn when `null`, `dead_end`, or `retracted` evidence uses the positive-only `supports` relation.
- Keep graph-level receipt findings inspectable in the standalone trajectory view.
- Treat successful `unchanged` outputs as validation-only receipts: they never become the canonical
  producing receipt, satisfy `NO_RUN_RECEIPT`, or hide drift in an earlier producing run's inputs.
- Require strict RFC 3339 UTC event timestamps and serialize capture/finalization per output path
  across threads and processes while allowing runs with disjoint outputs to proceed concurrently.
- Revalidate single-result and independent-first-reviewer invariants from stored assessment chains,
  suppress every active-looking semantic relation when store integrity fails, and serialize the
  review leaf check with its append across threads and processes.
- Reevaluate symbolic records against current claim/result nodes, complete artifact anchors,
  vocabulary/rule content, and scoped upstream provenance; deactivate proofs on drift, assumptions,
  ineligible statuses/types, store faults, irrelevant declared results, or cross-derivation
  contradiction.
- Validate every graph-owned formal target and result binding during `claimtrace check`, including
  live artifact extraction, and allow one result to expose profiles for multiple vocabularies.
- Bound symbolic parsing, inference, proof search, grounding memory, stored documents, structured
  drift, and provenance hashing so adversarial or accidentally oversized inputs fail closed.

### Tests
- Add regression coverage for multiple concepts, topological impact order, manifest completeness,
  missing manifests, output tampering, canonical relabeling, downstream stale propagation, rejected
  dangling log edges, and concurrent thread/process writers.
- Configure pytest's `src` path so the suite runs directly from a source checkout.
- Add adversarial coverage for surviving-event modification and concurrent appends, exact argv preservation,
  missing inputs/outputs, failed commands, unchanged-output semantics, external path policy, secret
  redaction, strict JSON determinism, graph/run declaration reconciliation, input/output drift,
  explicit semantic run links, status/relation compatibility, and control-plane mutation.
- Verify that the packaged skill matches both repository copies and that installation preserves
  differing project customizations unless `--force` is explicit.
- Add adversarial semantic-assessment coverage for causal/associational mismatch, incomplete
  alignment, invalid exact anchors, file and node drift, conflicts, immutable review chains, CLI
  field rejection, and strict-report coverage policy.
- Add symbolic regressions for exact typed grounding, automatic binding materialization, opposing
  polarity, unknown and assumption-dependent proofs, canonical proof grouping, cross-derivation
  conflict, asset/artifact/provenance drift, resource limits, store integrity, CLI output, and view
  projection.

### Migration
- Render manifests are now required for a green `claimtrace check`. Existing projects must re-render
  if needed and run `claimtrace snapshot` once after upgrading. This fail-closed change is queued for
  the next release; the checked-in package version remains `0.2.0` until that release is cut.
- Strict checking now expects successful finalized receipts for current `render_types` and
  `run_output_types`. Existing projects remain compatible with bare `claimtrace check`; adopt
  `claimtrace run` before enabling the strict gate.
- Semantic assessments are advisory by default. Projects that want strict coverage of direct
  `supports` and `refutes` edges can add an `assessments` path and set `require_assessments` to `true` after their
  existing links have been reviewed.
- Symbolic derivations are optional and advisory by default. Projects adopting them should first
  review and protect their graph bindings, vocabularies, rules, and prose-to-target mappings, then
  add a `logic` config object. Enable `require_derivations` only after active formal targets have
  current target-deriving proofs; refutations, conflicts, unknowns, and assumption-dependent proofs
  do not satisfy that policy.

## 0.2.0

First release under the name `claimtrace` (generalised and hardened from the internal `provkg`
prototype).

### Added
- **Structural-integrity checks** in `claimtrace check`: `DUPLICATE_ID`, `DANGLING_EDGE`,
  `SELF_EDGE`, `CYCLE`, `MALFORMED_EDGE` — a typo'd edge endpoint or an accidental cycle now fails
  loudly instead of silently mis-reporting OK.
- **`claimtrace lint`** — advisory warnings for non-standard node types/statuses/edge rels, a
  missing/mismatched `schema_version`, and load-bearing nodes with no `backbone` (which would
  otherwise never be drift-checked). `--strict` turns warnings into a non-zero exit.
- **`schema_version`** field in `graph.json` (currently `"1.0"`), with a forward-compat warning.
- **`input_types`** config option, so render-staleness inputs are no longer hard-coded to
  `data`/`artifact`/`code` — projects that type their nodes differently are no longer silently
  un-checked.
- **`python -m claimtrace`** entry point.
- Graceful, actionable errors on a missing / unparseable / structurally invalid graph (a clean
  message and exit code, never a raw traceback).
- UTF-8 BOM tolerance when reading `graph.json` / `claimtrace.config.json` (common on Windows).

### Changed
- Package, CLI, config file, and default graph directory renamed `provkg` → `claimtrace`.
- `snapshot` now imports the annotation-relation set from the engine instead of re-inlining it
  (single source of truth), and honours `input_types`.
- README documents the trust boundary (`verify` executes project code) and the deliberate
  mechanical-vs-scientific-adequacy line.

### Notes / not yet done (roadmap)
- Content hashing is still SHA-1 over raw bytes; normalized-content hashing (to avoid false
  `STALE_DATA` on non-deterministic figures/parquet) and a SHA-256 upgrade are planned.
- Generic receipts reconcile graph declarations with invoker declarations, but they do not observe
  actual reads or causally attribute writes. OS/runtime-specific tracing adapters remain future work.
