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

A node's `backbone` field records which canonical value that node is built on. A `current` node
whose `backbone` differs from the concept's `canonical` is **silent drift** — `claimtrace check` errors.

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
| `backbone` | optional; which canonical value this node is built on |
| `value` | optional; human-readable one-line description / the claim text |
| `note`, `date`, `script` | optional; `script` is informational (not existence-checked) — use it for dead code |

### node types
`data` · `artifact` · `code` · `figure` · `claim` · `doc` · `doc_span` · `experiment` · `method` ·
`decision` · `reference` · `concept`. Types are free-form strings — these are conventions, not an
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
`produces` · `renders` · `supports` · `cites` · `derives_from` · `reads`

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
| `SILENT_DRIFT` | a `current` node's `backbone` ≠ the concept's `canonical` |
| `READS_RETIRED` | a `current` node depends on a retired node |
| `CLAIM_CITES_OFFBACKBONE` | a claim cites an artifact on a different backbone |
| `STALE_RENDER` | a render is older (mtime) than a file it depends on — unreliable after `git clone`; `STALE_DATA` is the robust signal |
| `STALE_DATA` | a render's snapshotted input hashes changed since `claimtrace snapshot` |
| PENDING | a node explicitly marked `stale` (informational, not an error) |

The structural signals (`DUPLICATE_ID`, `DANGLING_EDGE`, `SELF_EDGE`, `CYCLE`, `MALFORMED_EDGE`) and
a malformed/BOM-tolerant graph file are validated before anything else, so a broken graph fails
loudly instead of silently mis-reporting OK.

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

## Config: `render_types` and `input_types`

In `claimtrace.config.json`, `render_types` (default `["figure"]`) are the node types whose
staleness is checked, and `input_types` (default `["data", "artifact", "code"]`) are the types that
count as an *input* when deciding whether a render is stale. Set these if your project types its
nodes differently (e.g. `render_types: ["report", "table"]`) so staleness is not silently skipped.
