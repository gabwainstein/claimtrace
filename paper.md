---
title: 'claimtrace: a dependency-aware provenance and verification engine for scientific data analysis'
tags:
  - Python
  - reproducibility
  - provenance
  - knowledge graph
  - research software
authors:
  - name: Gabriel Wainstein
    affiliation: 1
affiliations:
  - name: (add affiliation)
    index: 1
date: 28 June 2026
bibliography: paper.bib
---

# Summary

`claimtrace` is a dependency-free Python engine for representing a scientific analysis as a typed
knowledge graph spanning questions, hypotheses, predictions, data, code, preprocessing, results,
figures, claims, and conclusions. It combines that declared semantic trajectory with
content-addressed mechanical records so that changes to data, code, methods, or canonical analytic
choices can be propagated to every graph-declared dependent artifact and claim.

The tool deliberately separates evidence layers that are often conflated. Graph edges and pipeline
stages are declarations. Run receipts record process-boundary file snapshots and direct-child
outcomes. Fresh-workspace replay compares declared terminal outputs and path-bearing materialized
intermediates byte-for-byte. Optional cooperative checkpoints record and replay whether the child
program reported reaching exact locked stage callsites in DAG order. Symbolic rules establish
conditional derivability under project-owned premises. Semantic and method-to-code assessments are
attributed, reviewable judgements over content-locked inputs. None of these layers alone establishes
scientific truth or universal determinism.

# Statement of need

Long-running analyses accumulate inconsistencies when a dataset is recut, a preprocessing bug is
fixed, a model is replaced, or a method description changes while some dependent figures, tables,
or claims remain on the superseded branch. File-oriented automation can rebuild declared outputs,
but scientific projects also need a reviewable connection between those outputs, the methods that
produced them, and the prose claims that use them.

`claimtrace` addresses this gap with a small local representation that can accompany a study before
code or data exist and become stricter as the project matures. The deterministic engine owns
hashing, schema validation, graph traversal, exact role reconciliation, replay comparison, drift
detection, and fail-closed store integrity. Agent-facing proposal APIs accept bounded semantic
input while the engine constructs the mechanical snapshot. A review successor must use a different
self-asserted actor string; this is attribution and task separation, not authenticated independence.
Interpretations are never accepted as computed proofs. This division makes agent assistance
optional and keeps every semantic judgement attributable and inspectable.

# Functionality

- `claimtrace init` creates a planning-safe empty project; `init --example` creates an explicitly
  requested runnable toy project.
- `claimtrace check --strict --json` validates the graph and reconciles graph paths, receipts,
  contracts, replay certificates, semantic reviews, method reviews, symbolic records, and store
  integrity for agents and continuous integration.
- `claimtrace graph-propose` and `graph-apply` provide content-addressed, drift-checked graph
  transactions rather than silent agent mutation.
- `claimtrace run --pipeline-contract ...` records an event-v3 process-boundary receipt. A closed
  pipeline contract maps exact method steps to a stage DAG, graph roles, code files, and
  SHA-256-pinned line anchors. Path-bearing nonterminal outputs are captured automatically as
  materialized intermediates; pathless or in-memory stages remain declared but unobserved.
- `claimtrace run --stage-checkpoints` and `claimtrace.pipeline.stage_checkpoint` optionally add an
  event-v4 cooperative trace. Exact stage coverage, dependency order, uniqueness, execution binding,
  anchored caller location, and the exact launched direct-child PID are machine-checked and fail
  closed; inherited reserved trace variables are scrubbed before each fresh binding.
- `claimtrace replay` executes at least two fresh-workspace attempts and compares terminal outputs,
  materialized intermediates, stdout, stderr, and visible undeclared workspace file-path deltas;
  replay-v3 also compares cooperative callsite sequences with the source receipt.
- `claimtrace assess-method` plus review under a distinct self-asserted actor string records whether
  exact code anchors implement exact written method steps. Claim readiness requires exact producer
  ancestry and claim-owned method requirements, not graph proximity or prose inference.
- `claimtrace assess` and review maintain immutable result-to-claim meaning assessments, while
  ontology locking and reviewed mappings support optional deterministic terminology normalization.
- `claimtrace derive` evaluates project-owned restricted symbolic rules and reports conditional
  derivability separately from semantic support and scientific validity.
- `claimtrace impact`, `upstream`, and `downstream` expose the propagation consequences of a
  canonical change; `log` and `journal` retain null results, dead ends, and retractions.
- `claimtrace snapshot` and project-specific `verify` functions lock render dependencies and
  recompute headline values from disk.
- `claimtrace view` renders the same deterministic report as an interactive layered trajectory with
  draggable nodes, wheel zoom, background pan, clickable relationships, and detailed provenance
  and integrity states.
- `claimtrace release` creates and verifies an exact project manifest suitable for an external Git,
  archive, signature, transparency, or blockchain commitment. Release-v1 validation preserves the
  canonical pre-checkpoint schema inventory while new manifests enumerate checkpoint record, plan,
  and trace schemas.

# Evidence and limitation boundaries

Boundary replay is not hermetic execution. The direct child is not sandboxed from the network,
external filesystem paths, databases, clocks, schedulers, accelerators, or every host-library and
kernel difference. File transitions are whole-process pre/post observations and do not prove read,
write, or stage causation. Directory-only changes, background descendants, transient or reverted
writes, and pathless or in-memory values are not observed. Recorded parameter and seed declarations
are not automatically injected into arbitrary code. A cooperative checkpoint is emitted by the
child that inherits its trace binding; it can report a callsite without performing the intended
computation. It is therefore not independent stage observation, value capture, method validation,
or scientific support. Protocol v1 also rejects checkpoint API calls from descendant workers or a
persistent notebook kernel; a parent controller must checkpoint after its workers join. PID and
nonce binding prevent accidental mixing, not cooperative raw-record forgery.

Review readiness is therefore intentionally narrower than scientific validity. A ready provenance
path means that the accepted semantic relation, producing receipt, current event-v3 contract (or
event-v4 plus matching replay-v3 when cooperative checkpoints are required),
whole-command replay, exact producer ancestry, path-bearing ancestry intermediates, and accepted
method conformance are mutually current under the documented partial coverage. It does not certify
the method as scientifically appropriate, the result as generalizable, or the claim as true. Store
corruption, linked or unexpected ledger entries, contradictory current replay certificates, drift,
or missing review layers suppress current readiness while preserving the historical records for
audit. Natural drift in old run, replay, and method records becomes nonblocking history only after a
strictly later exact-output-role replacement has a current contract, current bindings, review-ready
replay (including checkpoint replay when required), and current method conformance. Integrity faults,
non-repeatability, replay conflicts, and undeclared writes remain blocking.

# Acknowledgements

Developed with the assistance of Claude Code and OpenAI Codex.

# References
