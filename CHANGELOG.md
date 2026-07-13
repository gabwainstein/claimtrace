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

### Migration
- Render manifests are now required for a green `claimtrace check`. Existing projects must re-render
  if needed and run `claimtrace snapshot` once after upgrading. This fail-closed change is queued for
  the next release; the checked-in package version remains `0.2.0` until that release is cut.
- Strict checking now expects successful finalized receipts for current `render_types` and
  `run_output_types`. Existing projects remain compatible with bare `claimtrace check`; adopt
  `claimtrace run` before enabling the strict gate.

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
