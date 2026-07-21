# Changelog

All notable changes to ProvSleuth are documented here. This project adheres to
[semantic versioning](https://semver.org/).

## Unreleased

### Fixed
- Clamp graph node paths to the project root. `Config.resolve` previously returned an absolute node
  path verbatim and joined a relative one without normalization, so graph content could make the
  engine read, hash, and record a file outside the project, including as an `external` entry inside
  a signed release manifest. Absolute and root-escaping node paths are now refused, normalization is
  lexical so symlink and junction rejection still applies, and `check` reports the offending node as
  `INVALID_NODE_PATH` instead of aborting the run. Configured external assets are unaffected.
- Reject a settled result that depends on unexecuted work. Promoting `planned` to a standard
  notebook status removed its `UNKNOWN_STATUS` lint without adding a dependency rule, so a `current`
  or `confirmed` node could depend on a `planned` node with nothing reported. Such an edge is now
  `DEPENDS_ON_PLANNED`; a `planned` node may still depend on other planned work, so planning-mode
  graphs remain clean.

## 0.5.0 - 2026-07-18

### Added
- Add a deterministic read-only `claimtrace.graphrag/1` projection of the canonical report and a
  byte-, node-, edge-, and hop-bounded `claimtrace.graphrag-context/1` neighborhood for external
  retrievers. Content addresses commit live review/proof state, unresolved references fail closed
  by default, and declared support, attributed review, and conditional proof remain distinct.
- Add a provider-neutral adversarial deliberation ledger and `deliberate-propose`,
  `deliberate-freeze`, `deliberate-ballot`, `deliberate-decide`, and `deliberations` commands for
  source-anchored claim extraction, semantic interpretation, formalization, and rule-validity
  candidates. Frozen complete candidate unions, role-bound ballots, self-asserted correlation
  groups, dissent, drift checks, and authored rule competency fixtures are deterministic. A panel is
  `recommended_for_human_review` only when exactly one frozen candidate exists and is eligible; any
  unresolved frozen alternative suppresses recommendation.
- Add immutable `claimtrace.deliberation-phase-decision/1` approval/rejection records that pin the
  complete ballot set. An approval gates only the immediate next phase under the same round,
  subject, and frozen snapshot; a rejection, missing decision, or snapshot drift blocks progression.
  Decision actors and correlation groups are self-asserted, no human identity is authenticated, and
  neither a recommendation nor decision activates graph, semantic, logical, or derivation state.
- Document that the v1 mandatory competency matrix, implemented as authored `competency_cases` and
  the `legacy_relational_competency_matrix` check, is a legacy relational fixture matrix rather than
  generated or exhaustive full-competency validation.
- Package the deliberation protocol with the synchronized `provsleuth-log` skill, including the
  explicit separate-review, unauthenticated-identity, and no-activation boundaries. Release-v2
  manifests include exact proposal, frozen-set, ballot, phase-decision, and status schemas while
  legacy release-v1 manifests retain their original scope.

## 0.4.0 - 2026-07-17

### Changed
- Rename the distribution, Python package, command, project scaffold, visualizer branding, and
  agent skill from Claimtrace to ProvSleuth.
- Use `provsleuth.config.json` and `provsleuth/` for newly initialized projects while continuing to
  discover legacy `claimtrace.config.json` projects with their original implicit store paths.
  Same-directory dual configs fail closed, and explicit `--config` remains authoritative.
- Emit `PROVSLEUTH_*` cooperative-checkpoint environment variables while accepting the legacy
  `CLAIMTRACE_*` variables and rejecting conflicting dual values. Runtime lock names remain shared
  with Claimtrace-era processes so old and new versions cannot bypass each other.
- Move browser-local graph arrangements to a ProvSleuth key with one-time recovery of compatible
  Claimtrace-era layouts.

### Compatibility
- Keep all persisted `claimtrace.*` schema values, logical identifiers, content-addressed records,
  checked-in legacy demo stores, and historical release manifests byte-compatible. ProvSleuth does
  not ship a `claimtrace` import package or command alias, avoiding the namespace collision that
  motivated the rename.

## 0.3.0 - 2026-07-16

### Added
- Make `claimtrace init` planning-safe by default with an empty valid graph, no invented data path,
  and no configured executable verifier; retain a complete runnable toy CSV and verifier only
  behind explicit `claimtrace init --example`.
- Add closed method-step and claim-method requirement declarations plus opaque multistage pipeline
  contracts that pin exact graph roles, code/method bytes, code-line anchors, parameter/seed keys,
  and a declared-only stage DAG.
- Add current event-v3 contract-bound receipts and replay-v2 certificates with automatic
  path-bearing materialized-intermediate roles. Receipts keep pre/post whole-process intermediate
  transitions separate from terminal outputs; fresh-workspace replay includes both roles in exact
  source/attempt byte comparison, so a differing final materialized-intermediate file cannot be
  hidden by stable terminal-output bytes. Transient, reverted, pathless, and in-memory changes
  remain outside this observation. Legacy event-v2, snapshot-v1, and replay-v1 records remain
  strictly readable but
  contain no inferred intermediate evidence. Undeclared workspace writes remain ineligible for
  claim readiness. Replay evaluation also revalidates the complete source event pair and exact
  unredacted command metadata; secret-bearing overrides remain redacted but are deliberately
  ineligible for current/review-ready source-command provenance.
- Add optional cooperative stage checkpoints for instrumentable opaque programs. The public
  `stage_checkpoint(stage_id)` API is a no-op outside traced children; `--stage-checkpoints` or
  `execution.require_stage_checkpoints` writes event-v4 receipts and replay-v3 certificates. The
  controller fails closed on missing, duplicate, unknown, out-of-DAG-order, incorrectly bound, or
  unanchored callsites and replay compares the normalized source/attempt sequence. Reserved trace
  environment variables are always scrubbed before launch and then freshly bound; checkpoint
  records carry a `reporter_pid` that must match the exact launched direct child. Protocol v1
  therefore rejects descendant/worker/kernel API reports; parent controllers checkpoint only after
  workers join. A 1 MiB raw-trace cap keeps replay-v3 certificates bounded. Result identity excludes
  nonce/raw/PID binding material while the finish event commits it. These controls prevent accidental
  mixing but not cooperative raw-record forgery, and remain explicitly child self-report rather than
  independent observation or scientific support.
- Add immutable external-agent method-to-code conformance proposals, distinct-actor review, live
  drift suppression, and a joined claim-provenance projection that keeps semantic meaning,
  execution, replay, method review, symbolic derivability, and scientific validity separate.
- Add deterministic `graph-hash`, `graph-propose`, and atomic `graph-apply` transactions so an agent
  can draft a bounded graph change without silently mutating or rebasing the scientific graph.
- Add exact `claimtrace.project-release/1` manifests with double collection, verification, and diff,
  including event, replay, semantic, method-review, normalization, and symbolic provenance stores,
  plus explicitly labeled current files at pipeline-contract paths referenced by those records.
  Release-v1 validation accepts both the canonical pre-checkpoint schema inventory and the current
  inventory; new manifests also enumerate the stage-checkpoint record, plan, and trace schemas.
- Advance the strict report read model to 1.7 and standalone view to schema 5. Run details separate
  terminal outputs from materialized-intermediate paths, post-process transitions, source/replay
  comparison, exact graph binding, and optional cooperative stage-trace state while stating that
  none of this independently observes computation or attributes bytes to a stage.
  Preserve contract stages, replay status, method conformance, joined claim-provenance readiness,
  draggable layout, wheel zoom, background pan, rounded obstacle-aware edges, and edge endpoint
  focus.
- Keep naturally stale run/replay/method history visible but demote its drift findings only after a
  strictly later, exact-output-role replacement has a current contract, current graph bindings, and
  review-ready replay (including repeatable cooperative checkpoints when policy requires them), plus
  current accepted method conformance for stale method history. Integrity faults, non-repeatability,
  replay conflicts, capture failures, and undeclared workspace writes remain blocking.
- Upgrade newly written render manifests to schema v2 with SHA-256 while retaining strict read-only
  validation of legacy unversioned SHA-1 locks.
- Add a 512 MiB default semantic ontology hashing budget with an explicit bounded
  `semantics.max_ontology_bytes` opt-in, and pin the Unicode data version in deterministic
  candidate profiles.
- Add optional deterministic semantic normalization with strict project terminology, exact-byte
  ontology/index locks, offline exact candidate search, immutable separate-actor SKOS mapping
  reviews, and explicit content-addressed policy releases that are never activated implicitly.
- Add `lock-ontology`, `ontology-candidates`, `map-term`, `mappings`, `review-mapping`,
  `compile-semantic-policy`, and `semantic-status`, plus report-schema 1.4 projections for asset
  integrity, review history, live drift/conflicts, stored releases, and explicit activation.
- Bundle a semantic-authoring reference with the `claimtrace-log` skill so agents can propose
  normalization under locked candidates while remaining unable to invent IRIs, self-review, or
  activate policy.
- Add a replayable Palmer Penguins semantic-normalization walkthrough that starts with optional
  empty ledgers, then demonstrates exact candidate discovery, bounded agent input, separate review,
  inactive policy compilation, explicit activation, and a green strict gate.
- Add pinned GitHub Actions release checks across Python 3.9-3.14 on Linux plus endpoint coverage on
  Windows and macOS, including wheel/sdist and packaged-skill smoke tests.
- Add a stdlib-only Palmer Penguins public demo with pinned CC0 sources, byte-identical
  raw-to-curated verification, pooled and species-conditioned slope claims, semantic review, and a
  project-owned symbolic sign rule.
- Add a compact PhysioNet EEGBCI neuroscience demo with hash-pinned source acquisition,
  leakage-aware leave-one-run-out CSP + LDA, a deterministic within-run permutation null, narrow
  claims, and explicit upstream data-license attribution.
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
  separate-actor review workflow, with accepted relations projected into strict reports and the
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
- Make `claimtrace verify` fail closed with exit code 2 when no verifier is configured or a
  configured verifier registers zero checks; exit code 0 now means at least one registered check
  ran and every check passed.
- Enforce a project-owned 256-level JSON nesting limit before decoding untrusted configuration,
  CLI, and event documents, so acceptance and diagnostics do not depend on CPython recursion
  behavior across Python 3.9-3.14.
- Make pipeline-contract snapshot v3 exclude clone-local `mtime_ns` from code and method file
  identities. Historical v2 snapshots still validate their original exact content addresses, then
  ignore only those timestamp fields for currentness; hashes, sizes, nodes, methods, stages,
  anchors, roles, and all immutable ledger bindings remain fail-closed.
- Stop replay workspace link checks at the controlled workspace root while still rejecting links
  at or below that boundary; this prevents trusted macOS `/var` aliases above the workspace from
  appearing as undeclared analysis files.
- Make the Penguins SVG writer Python 3.9-compatible without changing its output bytes, append a
  fresh producing receipt, and regenerate its render manifest. Keep the checked-in replay's exact
  executable identity fail-closed and test its strict state relative to the inspecting host.
- Run branch CI once through the pull-request event while retaining direct push CI on `main`,
  avoiding duplicate full matrices for the same proposed commit.
- Pin the release workflow to the current non-yanked `build` frontend release instead of the
  subsequently yanked 1.5.1 archive.
- Derive claim readiness from the claimed terminal result's unique producer stage and transitive
  stage ancestry. Require an exact claim-owned method/step set, current conformance for every
  ancestry stage, and current bindings for path-bearing ancestry intermediates. Ancestry selection
  is branch-local while replay remains whole-contract and conformance remains whole-method within
  that contract.
- Reject crossed event/snapshot schema generations, inactive or missing stage nodes, noncanonical
  internal paths, and code/input/output/materialized-intermediate node or path collisions before a
  contract-bound child can launch.
- Revalidate stored contract-bound argv against the exact project-relative entrypoint and recorded
  cwd, so recomputed event IDs cannot attach an unrelated command to a pipeline snapshot.
- Recompute replay attempt outcomes from return codes, launch errors, and output stability; describe
  the workspace scan as file-path coverage and explicitly exclude directory-only changes.
- Quarantine all runs on event-store corruption, only the affected run on start/finish-link faults,
  and all replay currentness on replay-store corruption while retaining inspectable history.
- Reject linked, non-directory, nested, and unexpected semantic/method assessment-store entries;
  retain declared lexical store roots so configuration cannot resolve away a junction before
  validation.
- Treat event-v2 sources as historical non-current replay coverage and suppress a positive replay
  certificate when another current certificate for the same source run contradicts review
  readiness.
- Preserve binding currentness, boundary-evidence basis, and missing stage attribution in the
  standalone view, and describe transitions as process-window content changes rather than proven
  production by a command or stage.
- Re-resolve legacy event-v2 contract receipts with their stored snapshot-v1 schema instead of
  making them stale merely because current contract resolution emits snapshot-v2.
- Discover pipeline-contract source paths from the stored event `type`, so an event-only
  contract-bound run cannot omit its current source path from a project release; label that role as
  current-path content rather than implying that it preserves every historical raw contract file.
- Keep incomplete symbolic execution provenance visible but make it a strict blocking warning only
  when a project enables a contract, replay, or method-conformance execution gate; conditional proof
  state remains unchanged in either case.
- Bound and descriptor-stabilize CLI JSON inputs, make `init` and `install-skill` reject linked or
  escaping publication paths, preserve no-force collision semantics under concurrent writers,
  serialize complete scaffold and skill-bundle publication, post-verify every managed skill file,
  and escape terminal and bidirectional controls in every human-readable and JSON CLI output path.
- Harden event, active-marker, and semantic-store concurrency with bounded scans, private lock
  roots, stable file identities, content-addressed no-replace writes, and directory durability
  barriers where the platform supports them; apply semantic lock deadlines to both in-process and
  OS waits, and route graph mutations through the hardened private runtime lock.
- Evaluate mapping and active-policy status from one in-memory semantic-asset snapshot and suppress
  activation when assets or stores change during report construction, including change-and-revert
  races.
- Revalidate complete mapping snapshots under the mapping-store lock, serialize mapping/policy
  publication in a fixed cross-store order, and use atomic no-replace content-addressed writes.
- Bound aggregate semantic assets, ontology bytes, terms, search matches, and stores; reject
  incompatible entity kinds, truncated candidates, conflicting ontology identities, and
  non-total candidate ordering.
- Label ontology indexes as project-supplied unverified assertions rather than verified RDF/OWL
  extraction, and use `declared_imports_available` rather than claiming a complete import closure.
- Include configured semantic assets and stores in the run control-plane fingerprint so a child
  cannot silently change active meaning during an analysis receipt.
- Write the EEG demo's tracked JSON artifacts with explicit LF newlines so Git line-ending
  normalization on Windows cannot invalidate the checked-in receipt hashes after a clean checkout.
- Use the distinct PyPI distribution name `claimtrace-provenance` while retaining the `claimtrace`
  import and CLI, and document the remaining namespace/command collision: it must not share an
  environment with the unrelated PyPI distribution named `claimtrace`.
- Make `require_assessments` cover structural result-to-claim `derives_from` dependencies as well
  as direct `supports`/`refutes` declarations, so removing a demo's semantic ledger fails strict
  checking instead of silently leaving the policy inert.
- Advance the strict report schema to 1.3 for structural assessment coverage and preserve stale
  derivation submissions as visible history without blocking when an active equivalent proof exists.
- Make the EEG demo verifier independently refit every observed CSP + LDA fold and all 199 seeded
  within-run permutations from the prepared epochs, comparing every ordered score with the result
  artifact.
- Preserve each semantic assessment's policy schema in the standalone visualization and display it
  in the review status and provenance details.
- Version semantic-assessment policy explicitly: new records use schema v2, legacy v1 records keep
  v1 evaluation semantics, review successors preserve their predecessor's schema, and mixed stores
  expose each item's version without rewriting immutable history.
- Allow v2 quantitative results to support or refute a qualitative directional claim when only the
  claim magnitude is unstated; continue to fail closed for missing result magnitude, partial or
  mismatched magnitudes, and unstated non-magnitude dimensions.
- Scope node backbones to named concepts when a graph has multiple independent canonical choices;
  retain scalar backbones for single-concept graphs and fail closed on ambiguous mappings.
- Return `impact` results in a stable topological order rather than a type-only order.
- Reconcile every render manifest against the graph's complete declared input set, reject missing or
  malformed manifests, detect canonical relabeling, verify the recorded output hash, and propagate
  content drift through current and confirmed downstream results.
- Write render manifests as explicit `claimtrace.render-manifest/2` documents with SHA-256 output
  and input hashes; continue checking unversioned SHA-1 locks as a legacy read-only format while
  rejecting unknown schemas, mixed hash fields, malformed digests, and duplicate inputs.
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
- Revalidate single-result and different-first-reviewer-actor invariants from stored assessment chains,
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
- Add adversarial semantic-normalization coverage for byte drift, case-sensitive IRIs, forged
  snapshots, review separation, no-replace races, kind conflicts, resource budgets, release
  eligibility/activation, CLI round trips, report fail-closure, and runtime policy mutation.
- Add real-example trajectory coverage plus adversarial v1/v2 semantic-policy tests for mixed
  stores, schema-preserving reviews, qualitative-claim specificity, legacy policy stability, and
  derived-field tampering.
- Add regression coverage for multiple concepts, topological impact order, manifest completeness,
  missing manifests, output tampering, canonical relabeling, downstream stale propagation, rejected
  dangling log edges, and concurrent thread/process writers.
- Add render-manifest compatibility coverage for SHA-256 writes, legacy SHA-1 verification and
  migration, legacy drift detection, unknown schemas, mixed hashes, and malformed digests.
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
- Semantic assessment schema v2 is now emitted for new proposals. Existing v1 documents remain
  valid and are evaluated under the original complete-alignment policy; they are never silently
  reinterpreted. A review transition stays on its predecessor's schema, while a changed scientific
  judgement should be submitted as a new v2 proposal.
- Render manifests are now required for a green `claimtrace check`. Existing projects must re-render
  if needed and run `claimtrace snapshot` once after upgrading. This fail-closed change takes effect
  in version `0.3.0`.
- New snapshots use versioned SHA-256 render manifests. Existing unversioned SHA-1 manifests remain
  checkable: run `claimtrace check` first, then run `claimtrace snapshot` to migrate only the files
  that passed the legacy lock, and run the check again. The legacy format is never written.
- Strict checking now expects successful finalized receipts for current `render_types` and
  `run_output_types`. Existing projects remain compatible with bare `claimtrace check`; adopt
  `claimtrace run` before enabling the strict gate.
- Semantic assessments are advisory by default. Projects that want strict coverage can add an
  `assessments` path and set `require_assessments` to `true` after reviewing both direct
  `supports`/`refutes` links and structural result-to-claim `derives_from` dependencies.
- Semantic normalization is optional and advisory by default. Configure project terminology and
  reviewed local ontology/index locks, create and separately review mappings, compile an explicit
  release from exact accepted leaf IDs, then pin that release in `semantics.active_policy`. Enable
  `require_active_policy` only after that migration. Existing symbolic proof schema v1 does not
  automatically inherit or commit this release.
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
