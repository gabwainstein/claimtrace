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
| `logic_bindings` | optional project-owned complete fact profiles for result nodes; each pins one vocabulary, input predicate, polarity, and extractor for every argument |
| `logic` | optional formal target for a claim-like node; contains exactly `vocabulary_id`, `rule_pack_id`, and a typed derived `target` atom |
| `logic_evidence_plan` | optional exact all-of premise policy for a formalized claim-like node; uses `claimtrace.symbolic-evidence-plan/1` and lists required result/binding IDs |

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

Information flows `from → to`, i.e. **`to` depends on `from`**, for every dependency relation except
the historical `reads` representation. For `reads`, use
`{"from":"code:fit","to":"data:raw","rel":"reads"}`: Claimtrace interprets this as
`code:fit` depending on `data:raw`. This reversal is relation-specific; copying that orientation to
`produces`, `supports`, or another relation would reverse the dependency.

### dependency relations (define the DAG)
`produces` · `renders` · `supports` · `cites` · `derives_from` · `reads` · `motivates` · `predicts` ·
`tested_by` · `concludes`

The research-trajectory conventions are `question --motivates→ hypothesis`,
`hypothesis --predicts→ prediction`,
`prediction --tested_by→ experiment/artifact`, evidence `--supports/refutes→ claim`, and
`claim --concludes→ conclusion`. These remain declared semantic dependencies; a run wrapper does
not create them.

### annotation relations (lab-notebook; queryable, not dependencies)
`supersedes` · `superseded_by` · `refutes` · `retracts` · `tried_before` · `related`

Annotations appear in `claimtrace journal` and `claimtrace node` but are skipped when computing
dependents/ancestors, so they never create false staleness.

## Semantic assessment documents

Semantic assessments live under the configured `assessments` directory (default
`claimtrace/assessments`). They are separate from `graph.json`: the graph records declared
dependencies, while an assessment records one external agent's structured interpretation of exact
result evidence against one claim-like node. New records use
`claimtrace.semantic-assessment/2`; legacy `claimtrace.semantic-assessment/1` records remain
readable and retain their original policy semantics.

An assessment subject contains one `claim_id` and exactly one `result_id` in both supported
versions. Multiple results require separate assessments; the schemas deliberately avoid expanding
a joint judgement into false per-result edges.
Claim-like node types are `claim`, `hypothesis`, `prediction`, and `conclusion`; result-like node
types are `artifact`, `figure`, `experiment`, and `data`.

Each document has these top-level sections:

| section | author and meaning |
|---|---|
| `id` | claimtrace-computed `assessment:sha256:<digest>` over the canonical document content excluding `id` |
| `schema_version`, `recorded_at`, `subject` | schema identity, strict RFC 3339 UTC time, and exact assessed nodes |
| `agent_input` | the external agent's schema-constrained semantic judgement |
| `mechanical_snapshot` | claimtrace-computed node hashes, result-node hashes, artifact byte hashes, and anchor checks |
| `review` | attributed state and optional predecessor; proposals and decisions are separate immutable documents |
| `derived` | claimtrace-computed policy findings, proposed/active relation, effective state, and recorded staleness flag |

The input to `claimtrace assess` must contain exactly `claim_id`, `result_ids`, and `agent_input`.
It must not contain `mechanical_snapshot`, `derived`, `review`, or a preselected assessment ID.

### External-agent input

`agent_input` requires:

- `verdict`: one of `supports_as_written`, `supports_narrower_claim`,
  `contradicts_as_written`, `insufficient`, `ambiguous`, or `unrelated`;
- `claim_frame` and `result_frame`: all eight explicit fields `population`, `exposure`, `comparator`,
  `outcome`, `direction`, `magnitude`, `time_scope`, and `inference_level`; unavailable or
  inapplicable non-inference values are represented by JSON `null` rather than omission;
- `alignment`: every one of those eight dimensions, each set to `match`, `partial`, `mismatch`,
  `not_stated`, or `not_applicable`;
- `evidence_anchors`: at least one exact anchor, with every assessed result represented;
- `rationale`: a concise explanation, plus a `limitations` list;
- `provenance`: required `agent`, with optional `model`, `skill_version`, and `prompt_sha256`;
- optional `recommended_claim`, which is required when the verdict is `supports_narrower_claim`.

The allowed inference levels are `descriptive`, `associational`, `predictive`, `causal`,
`mechanistic`, and `not_stated`. A causal or mechanistic claim frame paired with weaker result
evidence is a deterministic modality mismatch. It can never yield `supports` as written. With a
`supports_narrower_claim` verdict and otherwise valid evidence it can yield only `related`, while
`contradicts_as_written` can yield `refutes` only for an explicit direction mismatch with adequate
matching scope and modality elsewhere. Associational evidence cannot refute a causal claim.
Predictive claims require predictive result evidence; causal claims require causal or mechanistic
evidence; mechanistic claims require mechanistic evidence. A declared `match` that contradicts these
rules is an `ALIGNMENT_INCONSISTENT` hard block.

Schema v2 permits one narrowly directional specificity shape: both frames must state a direction,
their direction alignment must be `match`, the claim frame may leave `magnitude` null, the result
frame may state a magnitude, and magnitude alignment may be `not_stated`. The result is then more
specific than the qualitative claim. An explicit directional `mismatch` remains eligible only for
`contradicts_as_written`, whose separate contradiction policy requires that mismatch. This does not
relax `partial` or mismatched magnitudes, a stated claim magnitude with a missing result magnitude,
both magnitudes missing, or `not_stated` on any other dimension. Schema v1 retains its original
behavior and treats every `partial` or `not_stated` alignment as incomplete.

Evidence anchors are exact, not fuzzy citations:

```json
{ "result_id": "art:fit", "kind": "json_pointer", "pointer": "/slope", "expected_value": 2.0147 }
```

A `json_pointer` anchor compares the canonical JSON value at an RFC 6901 pointer with
`expected_value`. A `text_lines` anchor instead supplies 1-based inclusive `start_line` and
`end_line` plus `text_sha256`; claimtrace hashes the exact selected bytes, preserving line endings.
An unresolved or changed anchor produces `EVIDENCE_ANCHOR_INVALID`.

### Mechanical and derived fields

Claimtrace snapshots the complete graph-node JSON for the claim and each result as SHA-256 node
version IDs. For a result with a path, it also records the relative path, file SHA-256, byte size,
and file version ID. Mechanical fields are structurally validated on load, and stored `derived`
content is recomputed from the snapshot under the policy identified by that document's schema and
compared exactly; an external agent cannot override either section. Review successors preserve the
predecessor's schema. A mixed v1/v2 store is valid, but a review chain that switches schema is not.
CLI and report projections retain each item's `schema_version` so the governing policy remains
visible.

Listing or reporting reevaluates the stored document against the live graph, artifact bytes, and
other accepted current assessments. A changed claim node, result node, or result artifact produces
`ASSESSMENT_STALE`. Different accepted verdicts for the exact same claim/result subject produce
`ASSESSMENT_CONTESTED`; an unreviewed proposal cannot deactivate an accepted relation. Store
corruption, a missing predecessor, a branching chain, an invalid review transition, or a review that
changes immutable subject/input/snapshot fields fails closed and suppresses all assessment-derived
relations until repaired.

### Review workflow

`claimtrace assess ENTRY --actor ID` always appends a `proposed` document. A separate actor then
uses `claimtrace review ASSESSMENT_ID --state accepted|rejected|contested|superseded --actor ID`.
The review appends a new content-addressed document with `supersedes_assessment_id`; it never edits
the proposal. Use `claimtrace assessments` for current leaves and `claimtrace assessments --all`
for the complete chain.

The first reviewer actor string must differ from the proposal actor string. These strings provide
attribution, not authentication: the local JSON store has no signatures or access-control boundary.
Use a protected or signed approval system when authorization rather than traceable attribution is
required.

Allowed state transitions are:

- `proposed` to `accepted`, `rejected`, `contested`, or `superseded`;
- `accepted` to `contested` or `superseded`;
- `rejected` to `superseded`;
- `contested` to `accepted`, `rejected`, or `superseded`;
- `superseded` is terminal.

Only a current, accepted, non-stale, non-conflicting assessment with an eligible derived relation
creates an active relation in the report and visualization. `supports_as_written` activates
`supports`; `supports_narrower_claim` activates `related`; `contradicts_as_written` activates
`refutes`. Other verdicts record the judgement but do not create an active relation. Acceptance is
an attributed review decision, not proof that the analysis is valid or the scientific claim is
true.

A direct `supports` or `refutes` edge in `graph.json` remains a declaration. With no current accepted
assessment it emits `UNASSESSED_CLAIM_LINK`; an accepted but non-matching judgement emits
`ASSESSED_CLAIM_LINK_MISMATCH`. Both are informational by default and become strict blockers when
`require_assessments` is true. An accepted opposite-polarity relation emits the hard error
`DECLARED_CLAIM_LINK_CONFLICT`. A narrower or negative assessment therefore records that the pair
was reviewed without validating a direct support edge as written.

A structural `derives_from` edge from a result-like node (`artifact`, `figure`, `experiment`, or
`data`) to a claim-like node is also an explicit semantic-coverage requirement. It emits
`UNASSESSED_CLAIM_DEPENDENCY` when no current accepted assessment exists and
`ASSESSED_CLAIM_DEPENDENCY_WITHOUT_RELATION` when review exists but activates no semantic relation.
Either is informational by default and becomes a strict blocker when `require_assessments` is true.
Accepted `supports`, `refutes`, and `related` relations cover this structural dependency because
`derives_from` itself asserts lineage, not evidentiary polarity.

Assessment staleness covers the exact claim node, result node, and result artifact bytes. It does
not snapshot the entire upstream provenance closure, graph concepts, or surrounding edges; graph
and receipt checks cover those declarations separately.

## Symbolic derivation documents

Symbolic derivations are an optional, domain-neutral layer for claims that can be expressed with
explicit typed rules. The project supplies data-only JSON assets; no Python, imports, remote calls,
regular expressions, or executable expressions are accepted as rule features. The supported core
types are `ct:string`, `ct:symbol`, `ct:node_id`, `ct:integer`, `ct:decimal`, and `ct:boolean`.
Projects may declare named types over those bases and their own unit identifiers. Integer and
decimal ground values are canonical JSON strings; booleans remain JSON booleans.

A vocabulary uses schema `claimtrace.symbolic-vocabulary/1` and contains exactly
`schema_version`, `id`, `version`, `types`, `units`, `predicates`, and `renderers`. Predicates are
either `input` or `derived`; each argument fixes its name, type, and nullable unit. Renderers map an
exact predicate and polarity to a plain-language template. A rule pack uses schema
`claimtrace.symbolic-rules/1`, pins one `vocabulary_id`, and contains finite function-free rules:
each rule has exactly `id`, `when`, `where`, and `then`. `when` is a list of typed pattern atoms,
`where` supports only `eq`, `ne`, `gt`, `gte`, `lt`, and `lte`, and `then` is one derived atom.
There is no negation-as-failure; negative polarity is explicit data.

### Graph-owned formal targets and fact profiles

A claim, hypothesis, prediction, or conclusion can pin its formal interpretation:

```json
{
  "id": "claim:gate",
  "type": "claim",
  "status": "current",
  "value": "release-1 satisfies the configured test gate",
  "logic": {
    "vocabulary_id": "gate:vocabulary",
    "rule_pack_id": "gate:rules",
    "target": {
      "predicate": "gate:configured_gate_passed",
      "polarity": "positive",
      "arguments": {
        "release": {"type": "gate:release", "value": "release-1", "unit": null}
      }
    }
  },
  "logic_evidence_plan": {
    "schema_version": "claimtrace.symbolic-evidence-plan/1",
    "required_bindings": [
      {"result_id": "art:test", "binding_id": "gate:test-completed"}
    ]
  }
}
```

The prose `value` and formal `logic.target` remain distinct declarations. A matching formal proof
does not establish that the target faithfully expresses the prose meaning.

Each evidence-producing result declares project-reviewed, complete fact-binding profiles. One
profile fixes the vocabulary, one input predicate, its polarity, and an extractor for every
predicate argument:

```json
{
  "id": "art:test",
  "type": "artifact",
  "status": "current",
  "path": "results/test.json",
  "logic_bindings": [{
    "id": "gate:test-completed",
    "vocabulary_id": "gate:vocabulary",
    "predicate": "gate:test_completed",
    "polarity": "positive",
    "arguments": {
      "release": {"kind": "json_pointer", "pointer": "/release"},
      "failed": {"kind": "json_pointer", "pointer": "/failed"}
    }
  }]
}
```

`json_pointer` uses an RFC 6901 pointer into a JSON artifact. `text_lines` instead uses exact
1-based inclusive `start_line` and `end_line` plus literal `prefix` and `suffix`; the remaining text
is parsed as the typed argument. All arguments in one profile are read from the same result
artifact snapshot. Because the project pins predicate, polarity, and the complete tuple, an agent
cannot create a new locator, assemble one atom from unrelated rows or files, or reinterpret a
positive profile as negative. A result may expose profiles for multiple configured vocabularies;
binding IDs remain unique within that result, and a derivation can select only profiles belonging
to the vocabulary pinned by its claim. The report validates every declared target and profile,
including live extraction against the current artifact, even before a profile is selected.

### Claim-owned exact evidence plans

A formalized claim-like node can declare a top-level `logic_evidence_plan` object with exactly
`schema_version` and `required_bindings`:

```json
{
  "schema_version": "claimtrace.symbolic-evidence-plan/1",
  "required_bindings": [
    {"result_id": "art:test", "binding_id": "gate:test-completed"}
  ]
}
```

`required_bindings` is a non-empty, bounded list of unique objects containing exactly `result_id`
and `binding_id`; order is canonicalized. The plan is valid only on a `claim`, `hypothesis`,
`prediction`, or `conclusion` with a complete `logic` declaration. Every result must be a supported
result-node type and expose the named complete binding for the claim-pinned vocabulary. Graph checks
report malformed, unknown, or incompatible plans as blocking logic-declaration errors.

`claimtrace evidence-plan CLAIM_ID --json` resolves the plan together with its claim-pinned
`vocabulary_id` and `rule_pack_id`. The preferred derivation proposal for a planned claim contains
exactly:

```json
{
  "schema_version": "claimtrace.symbolic-plan-request/1",
  "claim_id": "claim:gate",
  "note": "Materialize the project-reviewed gate plan.",
  "provenance": {"agent": "analysis-agent"}
}
```

Claimtrace loads the plan at execution time and materializes every required binding. The request has
no binding list and cannot supply a target, policy ID, locator, atom, assumption, proof step, or
computed field. A planned claim's high-level selection must exactly match the plan; a low-level
proposal with a missing, additional, or substituted evidence anchor, or any assumption, is recorded
with an evidence-plan mismatch and cannot be active. Changes to the plan participate in the claim
snapshot and make earlier certificates stale; order-only changes are canonicalized.

### Binding-selection fallback

For a claim without `logic_evidence_plan`, the high-level input to
`claimtrace derive ENTRY --actor ID` is a `claimtrace.symbolic-selection/1` object with exactly these
fields:

```json
{
  "schema_version": "claimtrace.symbolic-selection/1",
  "claim_id": "claim:gate",
  "bindings": [
    {"result_id": "art:test", "binding_id": "gate:test-completed"}
  ],
  "note": "Use the project-reviewed gate profile.",
  "provenance": {"agent": "analysis-agent"}
}
```

`bindings` is a non-empty list of unique objects containing exactly `result_id` and `binding_id`.
Order is canonicalized. The referenced claim must carry a complete `logic` declaration and each
selected result must carry the named complete `logic_bindings` profile. Claimtrace derives the
sorted result set; loads the vocabulary, rule pack, and target from the claim; extracts every typed
argument from the selected result bytes; and creates the low-level grounded facts. A selection
proposal cannot add a target, policy ID, pointer, atom, polarity, assumption, closure fact, proof
step, proof state, proof ID, snapshot, or activation flag.

This selection shape is also accepted for a planned claim only when its canonical binding list
exactly equals the plan. Prefer the claim-only plan request so the caller never transcribes that
list.

`note` is nullable bounded public text. `provenance` contains a required bounded `agent` string,
optional bounded `model` and `skill_version` strings, and an optional SHA-256 `prompt_sha256`.
These fields and CLI `--actor` are attributed strings, not authenticated identities.

### Low-level/import and assumption proposal

The exact alternative top-level shape is `claim_id`, sorted unique `result_ids`, `vocabulary_id`,
`rule_pack_id`, and `agent_input`. This is an intentional low-level interface for imports,
debugging, and explicit assumptions; agents should prefer the binding-selection schema for grounded
facts. `agent_input` contains exactly a typed derived `target`, a non-empty `facts` list, nullable
public `note`, and `provenance`. Each fact contains one typed input `atom`, `evidence`, and
`assumption`:

- A grounded fact has `assumption: null` and exactly one evidence object containing only
  `result_id` and an existing complete `binding_id`.
- An assumed fact has an empty evidence list and a bounded explicit assumption string.
- Every subject result must ground at least one fact. Every atom must exactly match the configured
  predicate signature, types, units, polarity, and extracted artifact value.

Even this low-level form cannot submit artifact pointers, rule changes, closure facts, proof steps,
proof state, proof ID, mechanical snapshots, or an `active` flag. Claimtrace validates complete
profiles, computes the finite closure and backward proof slice, and stores an immutable
content-addressed `claimtrace.symbolic-derivation/1` record. Assumption-dependent proofs remain
inspectable but inactive.

The computed target state is:

| state | meaning under the pinned rule pack |
|---|---|
| `derivable` | the requested target polarity follows |
| `refutable` | only the explicit opposite polarity follows |
| `conflict` | both polarities follow |
| `unknown` | neither polarity follows; missing information is not treated as false |

This is open-world, paraconsistent evaluation: a conflict is surfaced and does not entail unrelated
facts. A derivable or refutable proof can be active only when its claim target matches, grounding is
valid, all used nodes remain eligible, no error finding exists, and no used premise is an
assumption. An assumption-dependent proof remains recorded and inspectable but inactive. When a
derivation lists multiple results, they remain the premises of one composite proof. The backward
slice records the result IDs actually used and rejects a non-unknown proof that merely lists an
irrelevant result; no per-result `supports` edges are inferred.

Eligible formal-target nodes have type `claim`, `hypothesis`, `prediction`, or `conclusion` and
status absent, `current`, or `confirmed`. Eligible grounded result nodes have type `artifact`,
`figure`, `experiment`, or `data` and status absent, `current`, `confirmed`, or `null`. The scoped
provenance closure includes the claim, selected results, and their dependency ancestors. A pending
stale node, retired ancestor, invalid status, structural/check problem, unreadable declared file, or
invalid anchor makes that snapshot ineligible. Graph structural errors are checked globally before
the scoped provenance evaluation and block the snapshot even when their node is outside the scope.

The proof certificate is canonical for the proof actually used, not for the submission history.
Reports group equivalent active submissions by `proof_id` and retain their sorted
`derivation_ids`. Separately active `derivable` and `refutable` proofs for the same claim, exact
target, vocabulary, and rule pack produce `SYMBOLIC_CROSS_DERIVATION_CONFLICT`; each conditional
proof remains inspectable, but both receive `claim_level_active: false`. A single derivation whose
closure contains both polarities has proof state `conflict`. Neither form satisfies
`require_derivations`.

Listing and reporting reevaluate stored records against the live graph, result bytes, vocabulary,
and rule pack. Content drift or store-integrity failure suppresses activation. Use
`claimtrace derivations` to list effective states and
`claimtrace explain DERIVATION_OR_PROOF_ID` to inspect one certificate and its equivalent
submissions. Drift is a bounded list of identity-level records, with `kind` values for the
claim, result, vocabulary, rule pack, provenance node, provenance edge, scoped provenance health,
and binding anchor; unavailable live inputs are explicit. Each record exposes stored/current
digests and version IDs rather than only an opaque aggregate mismatch. At most 1,000 drift items are
returned, with a deterministic `truncated` record for any remainder.

A symbolic certificate means conditional derivability under declared project policy. It does not
certify scientific truth, premise validity, semantic support, equivalence between the formal target
and claim prose, or equivalence between a binding and the intended scientific construct. Automatic
extraction validates the configured mapping mechanically; it does not validate that the mapping
chose the scientifically correct construct. Semantic assessments cover result-to-prose meaning
only. Prose-to-target and binding-to-predicate mappings remain repository policy that needs separate
review. Describe that review as independent only when the surrounding workflow establishes it;
Claimtrace does not yet store a dedicated review record for either mapping.

### Symbolic trust and authorization boundary

“Project-owned” and “project-reviewed” describe a repository workflow, not access control enforced
by Claimtrace. Anyone who can edit `graph.json`, a binding, vocabulary, or rule pack can change the
formal interpretation and request a fresh proof. Policy asset IDs are stable logical names, not
content-hash authorization pins. Existing derivations snapshot asset content and become stale after
a change, but a new derivation is valid under the new live content unless repository policy rejects
that edit. Protect meaning-bearing files with review rules, `CODEOWNERS`, signed commits, CI hash
pins, or another external authorization mechanism. `--actor` and `provenance.agent` values are
self-asserted strings.

The graph-to-predicate binding, polarity, extractors, rules, and prose-to-target mapping are the
semantic policy. Deterministic materialization prevents a selection proposal from changing them; it
does not make the mapping scientifically correct. A user or reviewer must inspect those declarations
and use semantic assessments for result-to-prose meaning.

An exact claim-owned plan is exhaustive only relative to its reviewed `required_bindings` list. It
prevents the derivation requester from omitting, adding, or replacing entries in that list, but it
does not prove that the plan author included every scientifically relevant result. An authorized
edit can still bias the plan, and exact schema v1 does not automatically discover newly added
results. `require_derivations` requires an active certificate under the current plan; it does not
establish universe-wide evidence completeness.

Deterministic graph-query plans are deferred. Before such a schema can be safe, it must specify how
corroborating bindings that materialize the same logical atom remain distinguishable, how
query-matched evidence unused by the backward proof slice avoids spuriously making the proof
inactive, and how a reproducible resolution certificate records the graph snapshot plus every
inclusion and exclusion decision. Schema v1 therefore uses only an explicit exact all-of list.

Derivation and assessment documents, reviews, and mechanical events are append-only only through
the Claimtrace API. Content addressing catches edits to surviving files and broken surviving
references, but each local store lacks an independently anchored head. Complete ledgers or chains
can be deleted or omitted without guaranteed detection. `require_derivations` and
`require_assessments` detect some missing policy coverage, not general ledger completeness. Commit
stores to Git or an external append-only commitment when deletion evidence is required.

### Deterministic symbolic resource limits

The evaluator fails closed at these schema-v1 limits:

| resource | limit |
|---|---:|
| vocabulary types / units | 1,000 each |
| predicates / arguments per predicate / renderers | 2,000 / 64 / 4,000 |
| rules / body atoms per rule / constraints per rule | 2,000 / 32 / 128 |
| complete binding profiles per result | 2,000 |
| identifiers / variable and argument names | 200 / 64 characters |
| vocabulary or rule version / renderer language | 100 characters each |
| typed string value / explicit assumption | 2,000 characters each |
| renderer template / public note | 4,000 characters each |
| actor or provenance string | 500 characters |
| low-level input facts, high-level binding selections, or evidence-plan required bindings | 5,000 |
| distinct subject result IDs | 5,000 |
| closure facts / inference rounds | 10,000 / 100 |
| rule firings / proof candidates | 50,000 each |
| indexed-join work / proof-search work | 200,000 each |
| canonical integer or decimal lexical length | 1,000 characters |
| artifact decimal/scientific lexical length / absolute adjusted exponent | 200 / 10,000 |
| one grounded result artifact / all grounded artifacts | 64 MiB / 256 MiB |
| one vocabulary or rule asset | 16 MiB |
| one stored derivation document | 64 MiB |
| derivation-ledger files / aggregate bytes | 10,000 / 256 MiB |
| structured drift records | 1,000 including truncation marker |
| all scoped provenance files hashed per snapshot | 64 GiB by default |

The provenance budget is `logic.max_provenance_bytes`, a positive 64-bit integer count of bytes.
It stream-hashes scoped upstream files instead of retaining all of them in memory; changing the
budget does not change the 64 MiB per-result or 256 MiB aggregate grounding limits.

The current ledger readers, report builder, and standalone trajectory view load project-local JSON
and aggregate it in memory. They target a research project, not an Arkham/MetaSleuth-scale data
platform. Larger deployments need indexed persistence and incremental/adapted reporting around the
same schemas and deterministic evaluator.

## What `claimtrace check` enforces

| signal | meaning |
|---|---|
| `DUPLICATE_ID` | the same node `id` is declared more than once (later silently wins) |
| `DANGLING_EDGE` | an edge endpoint is not a declared node (a typo silently severs a dependency) |
| `SELF_EDGE` | an edge points at its own node |
| `CYCLE` | the dependency edges form a cycle (they must be a DAG) |
| `MALFORMED_EDGE` | an edge is missing `from` / `to` / `rel` |
| `LOGIC_DECLARATION_INVALID` | a graph-owned formal target or result binding is malformed, incompatible with configured assets, or cannot ground the current artifact |
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

Receipts retain observed executable and working-directory paths, which can disclose usernames or
workspace layout in a public ledger. Flag redaction does not anonymize those paths. Audit them
before publication or capture the public ledger in a neutral environment; do not rewrite surviving
content-addressed event files.

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

## Config: render, input, receipt, event, assessment, and logic policy

In `claimtrace.config.json`, `render_types` (default `["figure"]`) are the node types whose
staleness is checked, and `input_types` (default `["data", "artifact", "code"]`) are the types that
count as an *input* when deciding whether a render is stale. Set these if your project types its
nodes differently (e.g. `render_types: ["report", "table"]`) so staleness is not silently skipped.
`run_output_types` (default `["artifact"]`) adds non-render materialized types that need successful
run receipts under strict checking. `events` (default `claimtrace/events`) selects the local
event-ledger directory.

`assessments` (default `claimtrace/assessments`) selects the content-addressed semantic-assessment
store. `require_assessments` defaults to `false`; when `true`, it strict-blocks an uncovered direct
`supports`/`refutes` declaration and an uncovered structural result-to-claim `derives_from`
dependency. Direct declarations require matching polarity; structural dependencies require an
accepted active semantic relation. This policy does not turn an accepted assessment into
scientific truth; it only requires that the attributed semantic review exists and remains grounded
to the current nodes and artifact bytes.

The optional `logic` object configures the portable symbolic layer:

```json
{
  "logic": {
    "derivations": "claimtrace/derivations",
    "vocabularies": ["claimtrace/logic/vocabulary.json"],
    "rule_packs": ["claimtrace/logic/rules.json"],
    "allow_external_packs": false,
    "require_derivations": false,
    "max_provenance_bytes": 68719476736
  }
}
```

`derivations` is the project-local immutable record store. `vocabularies` and `rule_packs` are lists
of configured JSON policy assets; duplicate paths are rejected. Paths are project-relative and may
not escape the project by default. `allow_external_packs: true` permits explicitly configured
vocabulary and rule-pack paths outside the project, but never permits an external derivation store
or executable rules. It should be enabled only when those external assets are separately trusted
and version-controlled.

`require_derivations` defaults to `false`. When true, strict policy requires each active configured
claim target to have a current active `derivable` certificate. A `refutable`, `conflict`, `unknown`,
stale, assumption-dependent, or otherwise inactive record does not satisfy that requirement. This
setting requires a conditional proof under project policy; it does not turn that proof into truth or
semantic support.

`max_provenance_bytes` defaults to 68,719,476,736 bytes (64 GiB) and must be a positive 64-bit
integer. It bounds the total declared-file bytes stream-hashed across the claim/result upstream
provenance scope for one snapshot. It is independent of the smaller grounding limits listed above.
