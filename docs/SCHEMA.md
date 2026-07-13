# Graph schema

`graph.json` has an optional `schema_version` (currently `"1.0"`) plus three keys: `concepts`,
`nodes`, `edges`. Set `schema_version` so future tool versions can migrate your graph; `claimtrace
lint` warns if it is missing or does not match the tool.

```json
{ "schema_version": "1.0", "concepts": { ... }, "nodes": [ ... ], "edges": [ ... ] }
```

## concepts

A *concept* is a canonical choice your whole project hangs on — a dataset version, a model, a
reference, a parameterisation. `claimtrace impact --set <concept>=<value>` uses it to compute the
propagation list.

```json
"concepts": {
  "dataset_version": { "canonical": "v2", "legacy": "v1", "note": "..." }
}
```

A node's `backbone` field records which canonical values that node is built on. With one concept, the
legacy scalar form remains valid. With multiple concepts, it must be concept-keyed:

```json
"backbone": {"dataset_version": "v2", "model": "m1"}
```

A `current` node whose binding differs from that concept's `canonical` is **silent drift** —
`claimtrace check` errors. A scalar backbone in a multi-concept graph is ambiguous and also errors.

## nodes

```json
{ "id": "art:fit", "type": "artifact", "status": "current",
  "backbone": "v2", "path": "results/fit.json", "value": "OLS slope/intercept/r2" }
```

| field | meaning |
|---|---|
| `id` | unique, conventionally `type:slug` (e.g. `data:raw`, `claim:slope`) |
| `type` | see below |
| `status` | see below |
| `path` | optional; file path **relative to project root**. Checked for existence; hashed for staleness |
| `backbone` | optional; scalar for a single concept, or `{concept: value}` bindings for multiple concepts |
| `value` | optional; human-readable one-line description / the claim text |
| `note`, `date`, `script` | optional; `script` is informational (not existence-checked) — use it for dead code |
| `run_ids` | optional list of exact `run:<uuid>` receipt IDs explicitly associated with this semantic result; never inferred |

### node types
`question` · `hypothesis` · `prediction` · `data` · `artifact` · `code` · `figure` · `claim` ·
`conclusion` · `doc` · `doc_span` · `experiment` · `method` · `decision` · `reference` · `concept`.
Types are free-form strings — these are conventions, not an
enum — but a type outside this set gets no type-specific checks and is flagged by `claimtrace lint`
(`UNKNOWN_TYPE`). `figure` (configurable via `render_types`) is the type whose render-staleness is
checked.

### statuses
| status | meaning |
|---|---|
| `current` | live; part of the canonical project |
| `confirmed` | a verified result that lived |
| `stale` | known to need migration (shows under PENDING, not an error) |
| `null` | an attempt that returned a null result |
| `dead_end` | tried, abandoned |
| `retracted` | was claimed, then withdrawn |
| `superseded` | replaced by a newer node |
| `deprecated` | retired file/result kept for provenance |

`deprecated / retracted / dead_end / superseded` are **retired**: a `current` node may not depend
on them (`claimtrace check` raises `READS_RETIRED`).

## edges

```json
{ "from": "art:fit", "to": "claim:slope", "rel": "supports" }
```

Information flows `from → to`, i.e. **`to` depends on `from`** (except `reads`, where the code
depends on the artifact it reads).

### dependency relations (define the DAG)
`produces` · `renders` · `supports` · `cites` · `derives_from` · `reads` · `motivates` · `predicts` ·
`tested_by` · `concludes`

The research-trajectory conventions are `question --motivates→ hypothesis`,
`hypothesis --predicts→ prediction`,
`prediction --tested_by→ experiment/artifact`, evidence `--supports/refutes→ claim`, and
`claim --concludes→ conclusion`. These remain attributed semantic declarations; a run wrapper does
not create them.

### annotation relations (lab-notebook; queryable, not dependencies)
`supersedes` · `superseded_by` · `refutes` · `retracts` · `tried_before` · `related`

Annotations appear in `claimtrace journal` and `claimtrace node` but are skipped when computing
dependents/ancestors, so they never create false staleness.

## What `claimtrace check` enforces

| signal | meaning |
|---|---|
| `DUPLICATE_ID` | the same node `id` is declared more than once (later silently wins) |
| `DANGLING_EDGE` | an edge endpoint is not a declared node (a typo silently severs a dependency) |
| `SELF_EDGE` | an edge points at its own node |
| `CYCLE` | the dependency edges form a cycle (they must be a DAG) |
| `MALFORMED_EDGE` | an edge is missing `from` / `to` / `rel` |
| `MISSING_FILE` | a node's `path` doesn't exist |
| `AMBIGUOUS_BACKBONE` | a scalar `backbone` is used when the graph has multiple concepts |
| `UNKNOWN_BACKBONE_CONCEPT` | a mapped backbone binds a concept not declared in `concepts` |
| `SILENT_DRIFT` | a `current` node's `backbone` ≠ the concept's `canonical` |
| `READS_RETIRED` | a `current` node depends on a retired node |
| `CLAIM_CITES_OFFBACKBONE` | a claim depends on evidence with a conflicting binding for the same concept |
| `STALE_RENDER` | a render is older (mtime) than a file it depends on — unreliable after `git clone`; `STALE_DATA` is the robust signal |
| `MISSING_MANIFEST` | a materialized render has no content-hash manifest |
| `INVALID_MANIFEST` | a manifest is malformed, belongs to another node, or omits required hashes |
| `MANIFEST_INPUT_MISMATCH` | the manifest input set differs from the graph's declared transitive inputs |
| `MANIFEST_BACKBONE_MISMATCH` | the render was snapshotted under different canonical bindings |
| `STALE_DATA` | a render's snapshotted input hashes changed since `claimtrace snapshot` |
| `OUTPUT_DRIFT` | a render's own content differs from its snapshotted output hash |
| `UPSTREAM_STALE` | a current node is downstream of a content-changed input or output |
| PENDING | a node explicitly marked `stale` (informational, not an error) |

`claimtrace check --strict` additionally blocks on lint warnings, PENDING nodes, receipt-integrity
errors, missing run receipts for current materialized outputs, graph/run input-declaration
mismatches, duplicate active paths, and input/output drift from the latest successful receipt.
`claimtrace check --strict --json` emits the same audit as one deterministic JSON document. It does
not execute project verifiers.

The structural signals (`DUPLICATE_ID`, `DANGLING_EDGE`, `SELF_EDGE`, `CYCLE`, `MALFORMED_EDGE`) and
a malformed/BOM-tolerant graph file are validated before anything else, so a broken graph fails
loudly instead of silently mis-reporting OK.

Receipt reconciliation adds these machine-facing signals:

| signal | meaning |
|---|---|
| `INVALID_RUN_REFERENCE` / `MISSING_RUN_REFERENCE` | a semantic node's explicit `run_ids` entry is malformed or absent from the surviving event ledger |
| `SEMANTIC_RUN_OUTCOME_MISMATCH` | a `current`, `confirmed`, or `null` node cites a command that did not succeed |
| `NO_RUN_RECEIPT` | an active materialized output has no successful finalized receipt |
| `RUN_DECLARATION_INCOMPLETE` / `GRAPH_DECLARATION_INCOMPLETE` | immediate graph inputs and run-declared inputs differ |
| `RUN_INPUT_DRIFT` / `RUN_OUTPUT_DRIFT` | current bytes differ from the latest successful receipt bound to an active output |
| `RUN_INPUT_SNAPSHOT_MISMATCH` | a finish event's input baseline contradicts the snapshot committed by its paired start event |
| `RUN_COVERAGE_MISMATCH` | paired events disagree about the closed, partial capture scope |
| `RUN_CONTROL_PLANE_MUTATION` | a child changed claimtrace config, graph, or removed prior ledger events during its execution; always blocking |
| `RUN_CAPTURE_CONTRACT_FAILED` | another capture contract or precondition failed; warning normally, blocking under `--strict` |
| `UNBOUND_RUN_OUTPUT` | a successful declared output has no active graph path node |
| `POSSIBLE_UNDECLARED_OUTPUT` | the best-effort pre/post scan saw an undeclared file delta; it is not causal attribution |

## What `claimtrace lint` warns about

`lint` is advisory (exit 0 unless `--strict`). It catches things that silently *disable* a check
rather than break the graph:

| warning | meaning |
|---|---|
| `NO_SCHEMA_VERSION` / `SCHEMA_VERSION` | `schema_version` is missing or ≠ the tool's version |
| `UNKNOWN_TYPE` | a node `type` outside the standard set — it gets no type-specific checks |
| `UNKNOWN_STATUS` | a `status` outside the standard set — `READS_RETIRED` / journal grouping may skip it |
| `UNKNOWN_REL` | an edge `rel` outside the known vocabulary — treated as a dependency edge |
| `NO_BACKBONE` | a load-bearing node (`claim` or a `render_types` node) with no `backbone` — never drift-checked |
| `AMBIGUOUS_BACKBONE` | a scalar backbone cannot be assigned to one of several concepts |
| `STATUS_RELATION_MISMATCH` | a `null`, `dead_end`, or `retracted` source uses positive-only `supports`; use an honest annotation such as `related` unless the evidence is genuinely positive |

## Mechanical run receipts

`claimtrace run` writes two content-addressed events per attempted command:
`run.started` and `run.finished`. The event identifier is SHA-256 over canonical JSON excluding the
identifier itself; files live under the configured event directory as
`<events>/<first-two-hash-characters>/<full-hash>.json`. The append API refuses to overwrite
existing evidence.
If finalization is interrupted, an out-of-worktree active marker remains for strict checking rather
than a half-written event.

```bash
claimtrace run \
  --input data/clean.csv --input analysis/fit.py \
  --output results/fit.json \
  --param model=ols --seed numpy=123 \
  -- python analysis/fit.py
```

Each file role must be explicit: use one or more `--input`/`--output` flags, or the corresponding
`--no-inputs`/`--no-outputs` assertion. Paths are literal regular files relative to the project root;
globs, directories, links/reparse points, and external paths are rejected by default. The child is
launched as an argv list with `shell=False`. Direct `.bat`/`.cmd` launch is rejected on Windows.
Common secret-bearing argv flags are redacted from stored argv; add `--redact-flag` for project
specific flags and do not place secrets in `--param` or `--seed`. Environment variables are not
dumped into receipts.

Declared files receive stable SHA-256 snapshots with stat checks before and after hashing. Output
transitions are `created`, `content_changed`, `unchanged`, `deleted`, `missing`, or `unstable`.
`unchanged` is a valid successful receipt but is explicitly `not proven produced`. Project-wide
before/after deltas are evidence of an `unattributed_pre_post_window`, not proof that the child wrote
the file. The portable wrapper also cannot observe actual reads, background descendants, transient
create/delete activity, network/database state, or complete environment state; every receipt marks
overall lineage coverage `partial`. Event validation rejects unknown or stronger coverage tokens
rather than accepting claims the portable wrapper cannot support.
The best-effort project content scan is on by default. It skips the configured event store, linked
or reparse-point paths, and directories named `.git`, `.hg`, `.svn`, `.pytest_cache`, `.mypy_cache`,
`.ruff_cache`, `__pycache__`, `.venv`, `venv`, `node_modules`, `build`, or `dist`. Writes inside those
locations are not reported. `--no-scan-writes` avoids the scan cost and narrows the receipt to
declared file snapshots. Neither mode changes the limits on read observation or write causation.

The semantic graph remains separate. Strict reconciliation compares a successful receipt's declared
inputs with the graph's immediate file dependencies for each bound output. Equal sets mean
`declarations_agree`; they do not become observed runtime lineage. Failed, interrupted, malformed,
or contract-failed runs never mutate or validate semantic graph edges.

Materialized outputs bind to successful receipts by their unique active graph path. Any semantic
node, including a pathless or null result, may instead declare `run_ids` to attribute its verdict to
specific executions. These are distinct links in the report and visualization. `run_ids` are never
inferred; a `current`, `confirmed`, or `null` node cannot cite a failed or incomplete run.

Claimtrace appends events atomically and content addressing detects edits to surviving files. The
event directory has no independently anchored head, so deleting complete start/finish pairs is not
detectable from that directory alone. Commit it to Git or use an external ledger commitment when
deletion evidence is required.

## Config: render, input, receipt, and event types

In `claimtrace.config.json`, `render_types` (default `["figure"]`) are the node types whose
staleness is checked, and `input_types` (default `["data", "artifact", "code"]`) are the types that
count as an *input* when deciding whether a render is stale. Set these if your project types its
nodes differently (e.g. `render_types: ["report", "table"]`) so staleness is not silently skipped.
`run_output_types` (default `["artifact"]`) adds non-render materialized types that need successful
run receipts under strict checking. `events` (default `claimtrace/events`) selects the local
event-ledger directory.
