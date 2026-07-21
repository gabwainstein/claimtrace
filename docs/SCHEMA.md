# Graph schema

`graph.json` has an optional `schema_version` (currently `"1.0"`) plus three keys: `concepts`,
`nodes`, `edges`. Set `schema_version` so future tool versions can migrate your graph; `provsleuth
lint` warns if it is missing or does not match the tool.

ProvSleuth was previously named Claimtrace. Existing `claimtrace.*` schema identifiers are frozen
legacy protocol names and remain valid for compatibility; new executable, import, config, and
default store names use `provsleuth`.

```json
{ "schema_version": "1.0", "concepts": { ... }, "nodes": [ ... ], "edges": [ ... ] }
```

## concepts

A *concept* is a canonical choice your whole project hangs on — a dataset version, a model, a
reference, a parameterisation. `provsleuth impact --set <concept>=<value>` uses it to compute the
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
`provsleuth check` errors. A scalar backbone in a multi-concept graph is ambiguous and also errors.

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
| `method_spec` | optional only on a `method` node; `claimtrace.method-spec/1` with exact stable step IDs, statements, and required flags |
| `method_requirements` | optional only on a claim-like node; `claimtrace.method-requirements/1` naming exact method/step IDs that the claim relies on |

### node types
`question` · `hypothesis` · `prediction` · `data` · `artifact` · `code` · `figure` · `claim` ·
`conclusion` · `doc` · `doc_span` · `experiment` · `method` · `decision` · `reference` · `concept`.
Types are free-form strings — these are conventions, not an
enum — but a type outside this set gets no type-specific checks and is flagged by `provsleuth lint`
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
on them (`provsleuth check` raises `READS_RETIRED`).

## edges

```json
{ "from": "art:fit", "to": "claim:slope", "rel": "supports" }
```

Information flows `from → to`, i.e. **`to` depends on `from`**, for every dependency relation except
the historical `reads` representation. For `reads`, use
`{"from":"code:fit","to":"data:raw","rel":"reads"}`: ProvSleuth interprets this as
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

Annotations appear in `provsleuth journal` and `provsleuth node` but are skipped when computing
dependents/ancestors, so they never create false staleness.

## Adversarial deliberation documents

Adversarial claim and rule deliberation lives under `deliberation.records` (default
`provsleuth/deliberations`). It is separate from the graph, semantic-assessment store, semantic
policy, and symbolic derivations. The ledger records attributed proposals, an exact frozen candidate
union, role-bound ballots, and at most one phase decision per frozen set; it never activates a
candidate or authenticates a person.

Four immutable stored-record schemas are supported:

| record | schema | content address |
|---|---|---|
| proposal | `claimtrace.deliberation-proposal/1` | `deliberation-proposal:sha256:<digest>`; equivalent candidate meaning also receives a shared `deliberation-candidate:sha256:<digest>` |
| frozen candidate set | `claimtrace.deliberation-candidate-set/1` | `deliberation-set:sha256:<digest>` |
| ballot | `claimtrace.deliberation-ballot/1` | `deliberation-ballot:sha256:<digest>` |
| phase decision | `claimtrace.deliberation-phase-decision/1` | `deliberation-decision:sha256:<digest>` |

`claimtrace.deliberation-status/1` is a recomputed projection, not an activation record. The report
projects the complete records, evaluated panels, open proposal groups, and integrity issues under
`deliberations`, with `human_activation_required: true`, `automatic_activation: false`, and
`scientific_truth_established: false`. `human_activation_required` names the separate project
governance boundary; it does not mean ProvSleuth authenticated a human.

### Proposal phases and grounding

`provsleuth deliberate-propose` accepts the closed
`claimtrace.deliberation-proposal-request/1` shape: `schema_version`, `round_id`, `phase`,
`subject_key`, `source_anchor`, `payload`, `rationale`, and optional `provenance`. CLI `--actor` and
`--independence-group` are stored separately. Actor and group strings are self-asserted
attribution, not authentication or proof of independent review.

`source_anchor` names an existing graph node with a UTF-8 text `path`, zero-based `start_byte` and
exclusive `end_byte`, plus an optional matching lowercase `span_sha256`. The stored proposal adds
the exact excerpt, complete-source SHA-256 and size, and pins the current canonical graph hash,
active semantic-policy ID, and configured vocabulary/rule-pack hashes. Live evaluation rechecks
those exact bytes and project snapshot; drift blocks recommendation.

The four phases are sequential:

| phase | closed payload | required ballot roles |
|---|---|---|
| `claim_extraction` | exact-substring `claim_text`, `claim_kind`, `speech_act`, `polarity`, `qualifiers` | `source_verifier`, `coverage_reviewer`, `adversarial_falsifier` |
| `semantic_interpretation` | `extraction_candidate_id`, `normalized_claim`, exact eight-dimension `frame`, `modality`, `quantifier`, `negation_scope`, `conditions`, `ambiguities`, `non_equivalences` | `semantic_reviewer`, `scope_reviewer`, `adversarial_falsifier` |
| `formalization` | `interpretation_candidate_id`, exact configured `vocabulary_id`, typed `target`, closed all-of `evidence_plan`, `method_requirements`, `assumptions`, non-empty `non_equivalences` | `evidence_mapper`, `logic_critic`, `adversarial_falsifier` |
| `rule_validity` | `formalization_candidate_id`, exact configured `vocabulary_id`, data-only `rule_pack`, `warrant`, `scope`, `assumptions`, non-empty `non_equivalences`, `competency_cases` | `logic_critic`, `domain_reviewer`, `adversarial_falsifier` |

Each later-phase candidate must reference a stored proposal candidate from the immediately preceding
phase and that candidate's approved, still-current phase decision. The upstream set and downstream
proposal must share the exact `round_id`, `subject_key`, and frozen mechanical snapshot. A missing or
rejected decision, skipped phase, changed snapshot, or decision that is no longer current blocks the
proposal. This advances only the immutable planning record; changing the graph, semantic policy,
vocabulary, evidence plan, rule pack, or other pinned project state requires a new round.

Rule candidates are checked for finite typed syntax, acyclic dependencies among derived predicates,
and the mandatory competency categories `positive`, `explicit_negative`, `missing_premise`,
`boundary`, `unit_mismatch`, `conflict`, and `counterexample`. The v1 mandatory competency matrix is
implemented through user- or agent-authored `competency_cases` and recorded by the
`legacy_relational_competency_matrix` mechanical check; it is a legacy relational fixture matrix.
Each case stores its declared expected and mechanically observed proof state, and ProvSleuth executes
only the submitted cases. It does not generate cases, explore the input domain, prove boundary
completeness, or establish full competency. A passing matrix is bounded mechanical behavior under
those authored fixtures, not evidence that the rule warrant or scientific meaning is valid.

### Frozen union and ballots

`provsleuth deliberate-freeze` accepts exactly `schema_version`, `round_id`, `phase`, and
`subject_key` under `claimtrace.deliberation-candidate-set-request/1`. It freezes all current
proposals for that key, coalescing equivalent candidate meaning while retaining every proposal ID.
An empty, stale, corrupt, oversized, or already frozen set is rejected. The stored record marks
`candidate_union_complete: true` and `human_activation_required: true`.

`provsleuth deliberate-ballot` accepts
`claimtrace.deliberation-ballot-request/1`: `schema_version`, exact `candidate_set_id`, an eligible
phase `role`, `evaluations`, and optional provenance. Every evaluation contains exactly
`candidate_id`, `decision`, sorted unique closed `reason_codes`, `blocking`, and `rationale`.
Decisions are `endorse`, `reject`, and `abstain`; rejection and abstention require a reason code,
and an endorsement cannot block. One actor may submit one ballot per set and must evaluate every
non-owned frozen candidate and no owned candidate. Ballots cannot be appended after a phase decision
has frozen the exact reviewed ballot set.

### Deterministic recommendation gate

Ballot decisions are aggregated by `independence_group`, not actor count. These groups and all actor
labels are self-asserted correlation metadata, not authenticated identities or proof of independent
review. A group is an endorsement
group only when all of its evaluations endorse, a reject group when any evaluation rejects, and
otherwise an abstention group. One candidate is eligible for human review only when all of these
conditions hold:

- the ledger and live source/project snapshot have no blocking finding, the phase payload is
  mechanically valid, and no evaluation is marked blocking;
- at least three participant groups are represented;
- either at least two proposer groups plus one external endorsement group, or at least one proposer
  group plus two external endorsement groups, are present;
- every required role endorses, and those roles can be assigned to distinct endorsing independence
  groups;
- at least two thirds of non-abstaining groups are endorsement groups.

`recommended_for_human_review` requires exactly one frozen candidate and that candidate must be
eligible. Another frozen alternative that is blocked, contested, or insufficient suppresses the
recommendation. Multiple eligible alternatives yield `contested` with no content-hash tie-break.
Material rejection or blocking also prevents recommendation; missing procedural coverage yields
`insufficient_review`; integrity, drift, or mechanical failure yields `blocked`. These are scheduling
and review states only.

### Immutable phase decision

`provsleuth deliberate-decide ENTRY --actor ID [--json]` accepts the closed
`claimtrace.deliberation-phase-decision-request/1` shape: `schema_version`, exact
`candidate_set_id`, exact `candidate_id`, `decision`, and `rationale`. `decision` is `approved` or
`rejected`; the CLI supplies `actor` separately.

The command runs under the deliberation-store lock and accepts only the set's current
`recommended_for_human_review` candidate after the complete ballot set exists. The decision actor
must differ from every proposer and balloter. Only one immutable decision is permitted for a set,
and the stored record pins all reviewed `ballot_ids`. The stored record contains exactly
`decision_id`, `schema_version`, `record_type`, `recorded_at`, `candidate_set_id`, `candidate_id`,
`ballot_ids`, `decision`, `actor`, `rationale`, `human_identity_authenticated`, and
`automatic_activation`; `record_type` is `phase_decision`, while both policy flags are `false`.

The actor is self-asserted attribution. ProvSleuth does not authenticate a human, and the phase
decision is not authorization for or activation of any graph, assessment, mapping, semantic policy,
vocabulary, evidence plan, rule pack, or symbolic derivation. An approved decision only routes the
candidate to the immediate next deliberation phase under the unchanged round, subject, and snapshot;
a rejected decision unlocks nothing. Rule-validity approval routes only to external review and
isolated testing. Actual project changes remain separate workflows and invalidate the frozen
snapshot for further phase progression.

The status projection exposes `phase_decision_id`, the complete `phase_decision`, and
`phase_routing_state`. Routing states distinguish `awaiting_attributed_phase_decision`,
`approved_for_next_phase`, `approved_for_external_application_review`,
`rejected_by_attributed_reviewer`, `recorded_decision_not_current`, `panel_unresolved`, and
`panel_unavailable`. For a targeted candidate set, `provsleuth deliberations` exits 0 for
`recommended_for_human_review`, 1 for `contested` or `insufficient_review`, and 2 for `blocked`.
That code reports panel status; callers must inspect `phase_routing_state` rather than treating exit
0 as an approved phase decision.

## Semantic assessment documents

Semantic assessments live under the configured `assessments` directory (default
`provsleuth/assessments`). They are separate from `graph.json`: the graph records declared
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
| `id` | ProvSleuth-computed `assessment:sha256:<digest>` over the canonical document content excluding `id` |
| `schema_version`, `recorded_at`, `subject` | schema identity, strict RFC 3339 UTC time, and exact assessed nodes |
| `agent_input` | the external agent's schema-constrained semantic judgement |
| `mechanical_snapshot` | ProvSleuth-computed node hashes, result-node hashes, artifact byte hashes, and anchor checks |
| `review` | attributed state and optional predecessor; proposals and decisions are separate immutable documents |
| `derived` | ProvSleuth-computed policy findings, proposed/active relation, effective state, and recorded staleness flag |

The input to `provsleuth assess` must contain exactly `claim_id`, `result_ids`, and `agent_input`.
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
`end_line` plus `text_sha256`; ProvSleuth hashes the exact selected bytes, preserving line endings.
An unresolved or changed anchor produces `EVIDENCE_ANCHOR_INVALID`.

### Mechanical and derived fields

ProvSleuth snapshots the complete graph-node JSON for the claim and each result as SHA-256 node
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

`provsleuth assess ENTRY --actor ID` always appends a `proposed` document. A separate actor then
uses `provsleuth review ASSESSMENT_ID --state accepted|rejected|contested|superseded --actor ID`.
The review appends a new content-addressed document with `supersedes_assessment_id`; it never edits
the proposal. Use `provsleuth assessments` for current leaves and `provsleuth assessments --all`
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

## Semantic-normalization documents

Semantic normalization is an optional policy layer. It does not create graph dependencies,
scientific-support relations, or symbolic premises. Six strict JSON schemas keep local meaning,
ontology search, attributed judgement, review, and activation separate.

### Local terminology

`claimtrace.local-terminology/1` contains exactly `schema_version`, `id`, `version`, and `terms`.
Each term contains exactly:

```json
{
  "id": "study:memory-score",
  "kind": "concept",
  "label": "Memory score",
  "definition": "Score produced by the declared memory assessment.",
  "aliases": ["recall score"]
}
```

Term kinds are `concept`, `relation`, `unit`, and `individual`. IDs are unique within the asset;
labels, definitions, and aliases are bounded non-empty strings. The terminology version and the
canonical complete asset hash enter every mapping snapshot.

### Ontology index and exact-byte lock

`claimtrace.ontology-index/1` contains exactly `schema_version`, `ontology_id`, `version`, and
`terms`. Every term contains exactly `iri`, `kind`, `labels`, `synonyms`, `definitions`,
`deprecated`, and `parents`. Text annotations are `{"text": "...", "language": "en"}` objects;
`parents` contains exact IRIs. Allowed kinds are `class`, `object_property`, `data_property`,
`annotation_property`, `individual`, `concept`, `relation`, `unit`, and `unknown`.

`claimtrace.ontology-lock/1` contains exactly:

- content-addressed `id` plus `schema_version`;
- `ontology_id`, `ontology_iri`, and declared `version`;
- nullable `version_iri` and `license_iri`;
- non-empty `documents`, each with portable relative `path`, exact `sha256`, and `size`;
- `index` with those same file fields plus the fixed assertion
  `project-supplied-index-not-verified-extraction`;
- `imports`, each containing `ontology_iri`, nullable `version_iri`, and an exact
  `ontology_lock_id`;
- `declared_imports_available`, a boolean asserting only whether every listed import must be among
  the configured locks.

Loading streams and hashes every locked document, hashes and validates the bounded index, checks
identity/version agreement, rejects conflicting configured releases with the same ontology
identity, and enforces aggregate configured-byte and term-count limits. It does not parse RDF/OWL,
derive the index from source documents, discover omitted `owl:imports`, or validate an ontology's
logical consistency. Consequently the index and import list remain unverified project assertions,
even when their exact bytes are intact; review must be established by an external repository or
signed-approval workflow rather than inferred from this field.

The CLI creates a lock only at an exact project-local path already listed in
`semantics.ontology_locks`. Publication is atomic create-if-absent and never replaces differing
bytes. A new release therefore needs a new configured path and produces a new content ID.

The CLI input is a separate path-based request, resolved relative to the output lock directory:

```json
{
  "ontology_id": "project:domain-ontology",
  "ontology_iri": "https://example.org/domain/",
  "version": "2026-07-15",
  "version_iri": null,
  "license_iri": null,
  "documents": ["domain.owl"],
  "index": "index.json",
  "imports": [],
  "declared_imports_available": false
}
```

The output path must already appear in `semantics.ontology_locks` and must not exist with different
bytes. The supplied index must use `claimtrace.ontology-index/1`; ProvSleuth does not generate or
semantically certify it in v1.

### Deterministic candidate sets

`claimtrace.ontology-candidates/1` is computed, not supplied as a mapping authority. It contains a
content-addressed `id`, schema and search-algorithm versions, original and normalized query,
language, requested limit, complete configured lock IDs, visible candidate list, total match count,
`truncated`, and the runtime `unicode_data_version` used for NFC, case folding, and whitespace
classification. It also carries the fixed `index_assertion`
`project-supplied-index-not-verified-extraction` plus the ordered limitations
`project_supplied_index_extraction_not_verified`, `no_rdf_owl_reasoning`, and
`exact_matching_only`. Candidate search supports only:

- exact, case-sensitive IRI identity; or
- exact preferred-label/synonym matching after NFC normalization, whitespace collapse, and
  Unicode case folding.

There is no fuzzy, embedding, remote, or reasoning fallback. Candidate order has a complete stable
tie-break, and every candidate pins its ontology lock, ontology identity/version, exact IRI, entity
kind, match source/language, display label, definitions, parents, deprecation flag, and canonical
term hash. A truncated candidate set cannot become policy-eligible. If discovery overrides the
configured language or limit, `map-term` must receive the same `--language` and `--limit` profile;
the candidate-set content ID makes any mismatch deterministic and rejectable.
Historical candidate sets retain their recorded Unicode version as valid history. A different
current runtime produces a different candidate profile, so live mapping evaluation marks the old
snapshot stale instead of treating its immutable record as corrupt.

### Mapping proposal and review chain

`claimtrace.semantic-mapping/1` contains exactly `id`, `schema_version`, `recorded_at`, `subject`,
`mechanical_snapshot`, `agent_input`, `review`, and `derived`.

- `subject` is exactly `terminology_id` plus `term_id`.
- `mechanical_snapshot` is ProvSleuth-computed and contains the complete local-term snapshot,
  bounded deterministic candidate set, and selected candidate or null.
- `review` contains `state`, attributed `actor`, and nullable `supersedes_mapping_id`.
- persisted `derived` contains `effective_review_state`, `stale`, `findings`, and
  `snapshot_eligible_for_policy`. The last field describes only the recorded snapshot. Live listing
  and reporting return the separate `eligible_for_policy` field after current re-evaluation.

The external `agent_input` contains exactly:

```json
{
  "relation": "skos:closeMatch",
  "target": {
    "ontology_lock_id": "ontology-lock:sha256:<digest>",
    "iri": "<exact candidate IRI>"
  },
  "candidate_query": "memory score",
  "candidate_set_id": "candidates:sha256:<digest>",
  "rationale": "Concise public rationale.",
  "limitations": ["A bounded public limitation."],
  "provenance": {
    "agent": "analysis-agent",
    "model": "optional-model-label",
    "skill_version": "optional-skill-version",
    "prompt_sha256": "<optional lowercase SHA-256>"
  }
}
```

Allowed relations are `skos:exactMatch`, `skos:closeMatch`, `skos:broadMatch`,
`skos:narrowMatch`, `skos:relatedMatch`, and `unmapped`. `unmapped` requires a null target. From the
local term toward the target, `broadMatch` means the target is broader and `narrowMatch` means the
target is narrower. The target must be one unique member of the recomputed candidate set. Local
kind compatibility is fixed as concept to class/concept, relation to property/relation, unit to
unit/individual, and individual to individual; mismatches are hard findings.

Proposals start at `proposed`. Review transitions are the same append-only state machine used by
semantic assessments: proposed to accepted/rejected/contested/superseded; accepted to
contested/superseded; rejected to superseded; contested to accepted/rejected/superseded; and
superseded terminal. The first reviewer actor string must differ from the proposer string. This is
attribution, not authentication.

Appending a proposal recomputes its complete snapshot under the store lock. Appending a review
preserves the immutable subject, input, and snapshot. Live evaluation detects changed local terms,
lock/index bytes, candidate alternatives, selected target, deprecation, review leaves, accepted
conflicts, and store faults. Any such problem prevents release eligibility.

### Explicit semantic policy release

`claimtrace.semantic-policy/1` contains exactly `id`, `schema_version`,
`unicode_data_version`, `recorded_at`, `actor`, sorted unique `mapping_ids`, and `note`. The Unicode
field pins the normalization tables under which the release was compiled; a release from another
runtime version remains valid immutable history but cannot become currently valid or active.
Compilation accepts only the exact caller-supplied IDs and
requires each one to be a current accepted, non-stale, conflict-free, policy-eligible leaf. It also
rejects multiple mappings for the same local subject. The mapping and policy stores are locked in a
fixed order during publication, and content-addressed records use atomic create-if-absent behavior.

Compilation never activates a release. `semantics.active_policy` is the sole explicit selector;
there is no latest-policy fallback and accepted mappings are never collected implicitly. Active
evaluation rechecks the complete selected mapping set and suppresses all active mapping IDs on any
integrity or drift failure. `require_active_policy` is advisory in a non-strict report and blocks
`provsleuth check --strict` when no valid active release exists.

Mapping acceptance means only an attributed normalization judgement under exact configured
snapshots. Policy activation means only that the project selected that reviewed release. Neither is
scientific support, ontology truth, logical equivalence, nor an `owl:sameAs` assertion. Symbolic
derivation schema v1 does not yet commit to a semantic-policy release.

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

`provsleuth evidence-plan CLAIM_ID --json` resolves the plan together with its claim-pinned
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

ProvSleuth loads the plan at execution time and materializes every required binding. The request has
no binding list and cannot supply a target, policy ID, locator, atom, assumption, proof step, or
computed field. A planned claim's high-level selection must exactly match the plan; a low-level
proposal with a missing, additional, or substituted evidence anchor, or any assumption, is recorded
with an evidence-plan mismatch and cannot be active. Changes to the plan participate in the claim
snapshot and make earlier certificates stale; order-only changes are canonicalized.

### Binding-selection fallback

For a claim without `logic_evidence_plan`, the high-level input to
`provsleuth derive ENTRY --actor ID` is a `claimtrace.symbolic-selection/1` object with exactly these
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
selected result must carry the named complete `logic_bindings` profile. ProvSleuth derives the
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
proof state, proof ID, mechanical snapshots, or an `active` flag. ProvSleuth validates complete
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
`provsleuth derivations` to list effective states and
`provsleuth explain DERIVATION_OR_PROOF_ID` to inspect one certificate and its equivalent
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
the semantic-normalization ledger can review term-to-ontology mappings, but symbolic proof schema
v1 does not yet bind one of those releases or review prose-to-target and binding-to-predicate
declarations directly.

### Symbolic trust and authorization boundary

“Project-owned” and “project-reviewed” describe a repository workflow, not access control enforced
by ProvSleuth. Anyone who can edit `graph.json`, a binding, vocabulary, or rule pack can change the
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
the ProvSleuth API. Content addressing catches edits to surviving files and broken surviving
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

## What `provsleuth check` enforces

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
| `STALE_DATA` | a render's snapshotted input hashes changed since `provsleuth snapshot` |
| `OUTPUT_DRIFT` | a render's own content differs from its snapshotted output hash |
| `UPSTREAM_STALE` | a current node is downstream of a content-changed input or output |
| PENDING | a node explicitly marked `stale` (informational, not an error) |

### Render-manifest schemas

`provsleuth snapshot` writes `claimtrace.render-manifest/2`. Its `output_sha256` and every input's
`sha256` are exact lowercase 64-hex digests. A manifest with no `schema_version` is treated only as
the legacy SHA-1 format (`output_sha1` plus per-input `sha1`) so existing projects can verify their
previous lock. ProvSleuth does not emit legacy manifests, infer an unknown schema, or accept mixed
hash fields. Safe migration is: verify the unversioned manifest with `provsleuth check`, run
`provsleuth snapshot` only after that succeeds, then check the new SHA-256 lock again.

`provsleuth check --strict` additionally blocks on lint warnings, PENDING nodes, receipt-integrity
errors, missing run receipts for current materialized outputs, graph/run input-declaration
mismatches, duplicate active paths, and input/output drift from the latest successful receipt.
`provsleuth check --strict --json` emits the same audit as one deterministic JSON document. It does
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
| `RUN_INPUT_DRIFT` / `RUN_OUTPUT_DRIFT` / `RUN_INTERMEDIATE_DRIFT` | current bytes differ from the latest successful receipt bound to an active terminal output or materialized intermediate |
| `RUN_INPUT_SNAPSHOT_MISMATCH` | a finish event's input baseline contradicts the snapshot committed by its paired start event; that run is quarantined from evidence bindings |
| `RUN_COVERAGE_MISMATCH` | paired events disagree about the closed, partial capture scope |
| `RUN_CONTROL_PLANE_MUTATION` | a child changed ProvSleuth config, graph, or removed prior ledger events during its execution; always blocking |
| `RUN_CAPTURE_CONTRACT_FAILED` | another capture contract or precondition failed; warning normally, blocking under `--strict` |
| `RUN_PIPELINE_CONTRACT_STALE` / `EXECUTION_CONTRACT_MISSING_OR_STALE` | a stored contract no longer resolves to the exact current contract/code/method bytes, or required claim provenance lacks one |
| `CLAIM_STAGE_CHECKPOINT_BASIS_INCOMPLETE` | project policy requires a complete cooperative source trace repeated by a current replay-v3 certificate, but that exact self-reported callsite sequence is absent, invalid, incomplete, stale, or non-repeatable |
| `REPLAY_SOURCE_STALE` / `REPLAY_NOT_BYTE_REPEATABLE` / `REPLAY_MISSING_OR_NOT_CURRENT` | replay evidence is corrupt/stale, did not match at the declared byte boundary, or is required but absent |
| `REPLAY_SOURCE_PAIR_INVALID` / `REPLAY_COMMAND_SOURCE_MISMATCH` | the source start/finish pair is inconsistent, or stored replay argv/cwd/capture metadata differs from the source plan |
| `REPLAY_LEGACY_SOURCE_COVERAGE` | an event-v2/snapshot-v1 source is readable historical evidence but cannot be current review-ready replay because it lacks current intermediate roles |
| `REPLAY_CURRENT_EVIDENCE_CONFLICT` | current certificates for one source run disagree on review readiness; positive certificates are suppressed and the conflict is blocking |
| `REPLAY_COMMAND_OVERRIDE_UNVERIFIABLE` | a secret-bearing override was executed but its exact argv was intentionally neither stored nor committed, so it is not current or review-ready source-command evidence |
| `REPLAY_EVENT_STORE_INTEGRITY` / `REPLAY_SOURCE_RUN_LINK_INTEGRITY` / `REPLAY_STORE_INTEGRITY` | event-store, source-run-link, or replay-store integrity is not established; affected replay evidence is retained historically but cannot be current or review-ready |
| `REPLAY_UNDECLARED_WORKSPACE_WRITE` | a fresh attempt left a visible undeclared workspace path; informational unless replay is required, but never eligible for reviewed claim readiness |
| `METHOD_ASSESSMENT_REVIEW_PENDING` / `CLAIM_METHOD_BASIS_INCOMPLETE` | method-to-code semantic review is awaiting a decision, the claim's method pairs differ from its exact producer ancestry, or required current stage conformance is absent |
| `CLAIM_RESULT_ANCESTRY_INVALID` / `CLAIM_ANCESTRY_INTERMEDIATE_INCOMPLETE` | the claimed terminal result has no unique valid producer ancestry, or a path-bearing intermediate on that ancestry lacks a current receipt binding |
| `CLAIM_EXECUTION_BASIS_INCOMPLETE` / `SYMBOLIC_EXECUTION_BASIS_INCOMPLETE` | accepted semantic or symbolic use of a result lacks the joined current execution basis; symbolic proof state remains separate |
| `UNBOUND_RUN_OUTPUT` | a successful declared output has no active graph path node |
| `UNBOUND_MATERIALIZED_INTERMEDIATE` | a successful declared materialized-intermediate transition has no active graph path node |
| `POSSIBLE_UNDECLARED_OUTPUT` | the best-effort pre/post scan saw an undeclared file delta; it is not causal attribution |

## What `provsleuth lint` warns about

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

`provsleuth run` writes two content-addressed events per attempted command:
`run.started` and `run.finished`. The event identifier is SHA-256 over canonical JSON excluding the
identifier itself; files live under the configured event directory as
`<events>/<first-two-hash-characters>/<full-hash>.json`. The append API refuses to overwrite
existing evidence.
If finalization is interrupted, an out-of-worktree active marker remains for strict checking rather
than a half-written event.

```bash
provsleuth run \
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

ProvSleuth appends events atomically and content addressing detects edits to surviving files. The
event directory has no independently anchored head, so deleting complete start/finish pairs is not
detectable from that directory alone. Commit it to Git or use an external ledger commitment when
deletion evidence is required.

## Method declarations and opaque pipeline contracts

A method node may contain the closed `claimtrace.method-spec/1` object:

```json
{
  "schema_version": "claimtrace.method-spec/1",
  "steps": [
    {"id": "clean", "statement": "Remove rows missing the model variables.", "required": true},
    {"id": "fit", "statement": "Fit the pre-specified regression.", "required": true}
  ]
}
```

Every step has exactly `id`, `statement`, and boolean `required`. Step IDs are unique stable
identifiers, not generated prose. A claim-like node may separately declare exactly which method
steps its wording depends on:

```json
{
  "schema_version": "claimtrace.method-requirements/1",
  "methods": [{"method_id": "method:primary", "step_ids": ["clean", "fit"]}]
}
```

Referenced methods must exist, be active method nodes, contain a valid method specification, and
declare every referenced step. ProvSleuth never infers method requirements from prose, filenames,
or graph proximity.

`claimtrace.pipeline-contract/1` is an authored description of one opaque command boundary. It
contains exactly:

- `schema_version`, a bounded `name`, and `entrypoint_code_node_id`;
- exact non-empty `code_node_ids` and `output_node_ids` lists plus an exact `input_node_ids` list,
  which may be empty;
- exact `required_parameters` and `required_seeds` key lists, which may be empty; and
- a non-empty `stages` list forming a DAG.

Each stage contains exactly `id`, `depends_on`, `method_id`, `method_step_id`,
`consumes_node_ids`, `produces_node_ids`, and `code_anchors`. A text anchor contains exact
`code_node_id`, `kind: "text_lines"`, one-based inclusive `start_line`/`end_line`, and
`text_sha256` over those exact line bytes. Every required method step in scope must map to exactly
one stage; every stage maps to a declared method step and at least one code anchor. Stage dependencies
must be acyclic, node roles must be compatible with the graph and command declarations, every
non-input consumed node must have a unique upstream producer, and terminal declared outputs must be
produced by the stage DAG.

Before execution, ProvSleuth resolves the authored contract to the current
`claimtrace.pipeline-contract-snapshot/3`. The content-addressed snapshot pins the contract file,
whole code and method nodes/files, exact anchor bytes, graph roles, required parameter/seed keys,
and coverage. Code and method file identities retain stable path, state, SHA-256, size, and file
version metadata; they deliberately exclude `mtime_ns`, which changes on an otherwise byte-identical
checkout. The event plan and computation identity separately commit the supplied parameter and seed
values. The snapshot automatically classifies every non-terminal stage output from the authored
DAG:

- an internal produced node with a verified project-relative `path` becomes a
  `declared_file_boundary` role in `roles.intermediates`; and
- a pathless internal produced node becomes `unobserved_in_memory_or_ephemeral`.

There is no second user-authored intermediate list and no filename heuristic. An ordinary
contract-bound command writes current `claimtrace.event/3` receipts; a checkpoint-enabled command
writes `claimtrace.event/4`. Both write a `computation:sha256:` identity over the declared
computation rather than the event timestamp or run UUID. The start plan records the
automatically resolved `declared_intermediates`. The finish records their pre/post whole-process
`intermediate_transitions` separately from terminal `output_transitions`, and commits those
transitions into the result identity. One project-relative direct-child argv token, resolved
lexically from the recorded project-relative cwd, must name the exact entrypoint path. Absolute
entrypoint tokens, module strings, and inferred imports are not portable event evidence and fail
closed. This proves path presence in argv, not interpreter semantics. Contract, code, method,
anchor, intermediate-role, or control-plane drift during the child marks the capture contract
failed.

The pipeline snapshot's internal-stage execution coverage remains
`declared_only_not_observed`, including when cooperative checkpoints are enabled. A materialized-
intermediate transition is a post-process file-boundary observation. It does not prove which stage
wrote the bytes, that the declared producer ran, that the file was not later rewritten, or that a
pathless/in-memory value had a particular value. It also does not establish that code and method
have the same scientific meaning. Cooperative checkpoint instrumentation can add the narrower
child-self-report described below; the separate review layer judges method-to-code meaning. Stored
`claimtrace.pipeline-contract-snapshot/1` and `/2` documents remain strictly readable and must first
validate their original, timestamp-bearing content address. When ProvSleuth compares a validated v2
snapshot with current project files, it excludes only the code/method `mtime_ns` fields from the
comparison. SHA-256, size, graph nodes, methods, anchors, roles, parameters, seeds, and every other
field remain exact and fail closed on drift. This prevents a fresh clone from manufacturing
contract drift while preserving the historical receipt bytes. Snapshot v1 retains its original
exact comparison; it and `claimtrace.event/2` contain no materialized-intermediate evidence and
therefore cannot be current review-ready claim provenance. Snapshot v2 retains its
declared-intermediate coverage.

### Cooperative stage checkpoints

Checkpointing is optional and requires a pipeline contract. Enable it for one run with
`--stage-checkpoints`, or for every contract-bound run in a project with
`execution.require_stage_checkpoints: true`. Instrument project code only with the small public API:

```python
from provsleuth.pipeline import stage_checkpoint

# Call after the declared stage body has completed and its local checks have passed.
stage_checkpoint("fit")
```

Outside a checkpoint-enabled ProvSleuth child, `stage_checkpoint` is a no-op that returns `False`.
Inside one, it appends a closed `claimtrace.stage-checkpoint/1` JSON record and returns `True` after
the write. The caller supplies only the contract stage ID; ProvSleuth captures the project-relative
caller path and line and injects a fresh execution nonce and exact pipeline-contract ID. The call
must itself lie inside that stage's locked code anchor, so adding it requires recomputing the anchor
line range and SHA-256 digest.

Event-v4 adds a closed `claimtrace.stage-trace-plan/1` to the start plan, a nonce commitment in the
start payload, and a normalized `claimtrace.stage-trace/1` in the finish payload. The controller
creates an exclusive private JSONL channel outside the project, injects its location and fresh
256-bit nonce into the direct child, and deletes the raw channel after normalization. Before every
ordinary run and replay launch, ProvSleuth removes all reserved checkpoint environment variables
in both namespaces from the inherited environment. A checkpoint-enabled launch emits only
`PROVSLEUTH_STAGE_TRACE_PATH`, `PROVSLEUTH_STAGE_TRACE_NONCE`,
`PROVSLEUTH_PIPELINE_CONTRACT_ID`, and `PROVSLEUTH_STAGE_SOURCE_ROOT`. The corresponding
`CLAIMTRACE_*` names remain accepted for legacy instrumented children; if both namespaces supply a
field with different values, validation fails closed. Caller-supplied or stale bindings therefore
cannot leak into an untraced child or be reused as the controller binding.
Validation is fail-closed over:

- exact nonce and pipeline-contract binding;
- strict UTF-8 JSON with no extra fields and bounded record/file size;
- an integer `reporter_pid` equal to the exact PID returned by the controller's direct-child launch;
- exactly one checkpoint for every contract stage and no unknown stage ID;
- dependency-respecting DAG order; and
- an exact caller path and line within one locked code anchor for that stage.

A missing, duplicate, unknown, out-of-order, unbound, malformed, or unanchored checkpoint produces
`cooperative_report_incomplete` or `cooperative_report_invalid`. If the child launched, that state
marks the run `contract_failed` even when its process return code is zero. A complete trace stores
the normalized observation `program_emitted_checkpoint_reached` and the state
`cooperative_report_complete`.

Checkpoint protocol v1 accepts reports only from that launched direct child. A descendant process,
multiprocessing worker, distributed worker, or already-running notebook kernel has a different PID
and cannot satisfy the protocol through `stage_checkpoint`. A direct-child controller script must
wait for workers to finish, validate their returned state, and emit its own checkpoint only after
the declared stage is complete. This deliberately avoids treating an arbitrary descendant report as
the command-level stage boundary; it does not observe what the workers did.

The raw JSONL channel is limited to 1 MiB (with a 64 KiB per-record bound). That limit is chosen so
the source trace plus the minimum two fresh replay traces and their JSON representation fit within
the bounded replay-v3 certificate. The event-v4 `result_id` hashes a normalized trace fingerprint
that excludes the execution nonce, raw-channel hash/size, and reporter PID. This keeps the result
identity stable across otherwise identical executions. The content-addressed `run.finished` event
hash covers the entire finish payload and therefore still commits those complete binding fields.

This is deliberately a **cooperative child self-report**, not independent runtime observation. The
child inherits the trace channel and binding material and can emit a checkpoint without performing
the intended computation. The receipt therefore does not attest that the stage's operations ran,
capture an in-memory value, attribute a file write, validate method meaning, or establish scientific
support. Environment scrubbing, the private channel, fresh nonce, and exact direct-child PID prevent
accidental or stale trace mixing and reject honest descendant API calls; they do not defend against
cooperating code that knows the inherited binding, bypasses the API, and appends a raw protocol
record naming the expected PID. The stored trust token is
`cooperative_child_self_report_not_independent_observation`.

## Replay certificates

`provsleuth replay RUN_ID` accepts a successful, complete contract-bound `claimtrace.event/2`,
`claimtrace.event/3`, or `claimtrace.event/4` run; event-v3 and event-v4 carry materialized-
intermediate roles, while event-v4 also carries the cooperative trace. It first rechecks
the content-addressed source start/finish pair, input baseline and roles, partial coverage agreement,
source input bytes, pipeline contract and all pinned code/method/anchor bytes, resolved executable
identity, and root lockfile hashes. It then executes at least two attempts in separate temporary
workspaces, copies only the declared project inputs, scans every workspace file path
before and after without following links, and deletes the temporary workspaces after recording
bounded fingerprints. Ordinary directory-only changes, including empty directories, are not
recorded. ProvSleuth itself designates outputs only inside the temporary workspace, but the child is
not sandboxed and can still write an absolute or otherwise external path. Replay over event-v2 is
retained as historical attempt evidence but returns non-review-ready; current claim provenance
requires an event-v3 source receipt, or event-v4 when cooperative stage evidence is required.

Replay-v2 and replay-v3 therefore record the exact coverage token
`all_workspace_file_paths_pre_post_scan_unattributed_no_follow`. Replay-v1 validation continues to
accept its historical `all_workspace_paths_pre_post_scan_unattributed_no_follow` token exactly for
compatibility; that older name does not add directory-entry evidence that the certificate never
captured.

Ordinary event-v3 replay writes `claimtrace.replay-certificate/2`. Event-v4 replay writes
`claimtrace.replay-certificate/3`. Both are content addressed as `replay:sha256:` and contain:

- exact source run/event, computation, and pipeline-contract identities;
- stored redacted argv metadata, cwd, timeout, and whether a secret command override was supplied
  without being stored or committed;
- the stable original terminal-output and materialized-intermediate fingerprints;
- each attempt's exit/timeout state, stdout/stderr SHA-256 and size, terminal-output and
  materialized-intermediate fingerprints, complete workspace file-path pre/post delta, and
  undeclared file writes; and
- an exact comparison plus one outcome: `byte_repeatable`, `repeatability_mismatch`, or
  `replay_failed`.

Replay-v3 additionally creates a fresh checkpoint binding for every attempt, requires every attempt
trace to be complete, and compares the normalized required-stage list and ordered checkpoint
records (stage ID, code node, caller path/line, and observation) across attempts and with the source
receipt. Its exact comparison flags are `all_stage_traces_complete`, `stage_traces_equal`, and
`all_stage_traces_match_source_receipt`; all three must be true for `byte_repeatable` and for
`stage_trace_repeatable_current`. Nonces and raw-channel hashes are intentionally excluded from the
cross-attempt equality because each attempt has a fresh binding. This shows repeatability of the
program-emitted callsite sequence only; it does not upgrade that sequence into independent stage
observation.

For an ordinary unredacted replay, certificate argv, cwd, and argv-capture mode must exactly equal
the paired source start plan. When source argv contains redaction, replay requires a supplied command
whose non-secret tokens and redacted-token shape match that plan. Replay-v2 and replay-v3 store only the redacted
argv and `secret_override_used_not_stored: true`; it deliberately stores no plain or guessable digest
of the secret-bearing override. It therefore cannot verify that the supplied secret values equal the
original run. The attempts can still have a `byte_repeatable` outcome scoped to that supplied
override, but generation returns exit code 3 and evaluation marks it non-current and non-review-ready
with `REPLAY_COMMAND_OVERRIDE_UNVERIFIABLE`. It must not be presented as exact source-command replay.

`byte_repeatable` requires every attempt to succeed, all declared terminal-output and materialized-
intermediate paths/states/SHA-256/sizes to agree across attempts, every attempt to match the source
receipt for both roles, and captured stdout, stderr, and visible undeclared workspace file-path
deltas to agree across fresh attempts. Thus, a random materialized intermediate makes the outcome
`repeatability_mismatch` even if the terminal output bytes are stable. A persistent undeclared
workspace file write remains visible and prevents that certificate from satisfying reviewed claim
provenance until the path is declared or removed, even when its bytes repeat. Network and external
filesystem access are not isolated; only direct-child exit is observed; host state is partial;
write deltas are unattributed; internal/in-memory stages are not observed; and recorded
parameter/seed declarations are not injected into arbitrary code. Materialized bytes are captured
only after each whole child process; neither receipt nor replay attributes them to an internal
stage. Replay-v3's cooperative trace does not change those limits. Legacy
`claimtrace.replay-certificate/1` documents remain strictly readable and evaluable,
but have no materialized-intermediate fingerprints or comparison flags and cannot be used to infer
them from terminal-output equality. A v1 certificate may remain byte-repeatable within its stored
terminal-output scope. It is not review-ready when its event-v3 source receipt declares a
materialized intermediate, because that required source/attempt comparison is absent; v1 over a
source receipt with no declared intermediate retains its existing eligibility if every other
condition passes **and that source receipt is event-v3**. An event-v2 source is always historical,
non-current replay coverage.

Report schema 1.8 retains `declared_intermediates` and compact
`intermediate_transitions` on each run separately from terminal outputs. Replay-v2 projections also
retain their intermediate comparison flags and compact source fingerprints; replay-v1 projections
use empty intermediate evidence rather than an inferred value. A declared materialized path binds
to its unique active graph node as `materialized_intermediate_path`, with
`stage_attribution: not_observed`. This can satisfy that path-bearing artifact's mechanical receipt
coverage but never promotes it to a terminal result-to-claim execution binding. Standalone view
schema `claimtrace.view/5` renders the materialized path, transition, digest, and replay comparison
as a separate run-detail group and repeats the no-stage-attribution boundary.

For event-v4, report schema 1.8 also projects the closed trace plan, start binding, normalized source
trace, replay-v3 comparison, and claim-level `stage_checkpoint_state`. A complete source trace
without matching current replay is `cooperative_report_complete_source_only`; matching current
replay-v3 evidence is `cooperative_report_repeatable_current`. The adjacent
`stage_execution_observation` remains
`cooperative_checkpoint_self_report_not_independent_observation`, and scientific validity remains
`not_assessed`.

### Historical replacement and strict current gates

Immutable historical drift remains visible, but a narrow class of natural drift findings can be
demoted to nonblocking `info` after a complete current replacement exists. For an older stale
contract-bound run, the replacement must:

- start strictly after the historical run finished;
- have the exact same pipeline output-node role set;
- be a successful evidence-eligible event-v3 or event-v4 run under the current contract;
- have current exact graph bindings for every output role;
- have no current replay conflict and have at least one current review-ready replay; and
- when `execution.require_stage_checkpoints` is true, be event-v4 with a review-ready replay-v3 whose
  cooperative stage trace is repeatable and current.

Only then are the old run and replay marked `current_gate_role: historical_replaced`, linked to the
replacement run/replay IDs, and natural historical `RUN_PIPELINE_CONTRACT_STALE`,
`REPLAY_INPUT_DRIFT`, `REPLAY_CONTRACT_DRIFT`, and `REPLAY_ENVIRONMENT_MISMATCH` findings demoted.
A later run without current output bindings or review-ready replay is not a replacement. A stale
method assessment becomes nonblocking historical information only when a current accepted
`implements` assessment covers the same method under the current snapshot.

This rule never erases records and never demotes event/replay/store integrity failures, malformed
links, capture contract failure, replay non-repeatability, current replay conflict, or undeclared
workspace writes. Those remain blocking according to their existing strict policy. The replacement
rule distinguishes expected old-byte drift from an unresolved current failure; it is not a general
"latest run wins" rule.

## Method-to-code conformance assessments

`claimtrace.method-conformance-assessment/1` is a create-only review chain. Its subject pins one
method and one pipeline-contract snapshot. ProvSleuth computes the complete mechanical snapshot;
the external proposal supplies exactly `verdict`, `step_alignments`, `rationale`, `limitations`, and
`provenance`.

Every contract stage for the selected method must have exactly one alignment to its exact method
step. Allowed alignments are the schema-defined finite tokens; an `implements` verdict is eligible
only when every alignment is `match` and the mechanical snapshot is valid. The proposal actor must
equal `provenance.agent`. A first reviewer must use a different self-asserted actor string and append
an immutable `accepted`, `rejected`, `contested`, or `superseded` successor. Actor strings are
attribution, not authenticated identity or proof of independence.

Current evaluation reloads the contract, graph, method, code, and anchors. Drift suppresses
`implementation_current` without rewriting history. An accepted all-match current assessment means
that an attributed semantic review judged the exact anchors to implement the exact written steps.
It does not observe internal runtime execution, decide that the method is scientifically sound, or
establish a result-to-claim relation.

The report-only `claimtrace.claim-basis-projection/1` starts at the unique stage that declares the
claimed terminal result and traverses its transitive `depends_on` ancestry. The claim's explicit
method/step set must equal the exact set on that ancestry, and accepted current conformance must
cover each stage under the same contract. Producer-ancestry selection, requirement equality, and
ancestry-intermediate binding are branch-local. The replay certificate remains whole-contract, so
any terminal output or materialized intermediate on any branch can block it. Method conformance is
whole-method within the contract; an off-ancestry stage that reuses the same method ID can therefore
block conformance. Split independent branches into separate commands/contracts and method IDs.
Every path-bearing intermediate produced on the ancestry must have a current receipt binding and
stable recorded snapshot. The
projection then joins these mechanical facts, without inference, to the accepted current
result-to-claim semantic relation, latest current producing receipt, current contract, and current
review-ready boundary replay. A replay is review-ready only when it is current and byte-repeatable,
its command is exactly bound to the source plan without an uncommitted secret override, and no
visible undeclared workspace write was recorded. Its strongest state is
`ready_under_reviewed_provenance`; `scientific_validity` always remains `not_assessed`.

The receipt projection exposes event-store integrity globally and start/finish-link integrity per
run. Any event-store issue quarantines every run; a link issue quarantines its affected run. Replay
store integrity is also global to current replay evaluation. Quarantined records remain visible but
cannot create current explicit, output, validation, intermediate, replay, or claim bindings.

## Reviewable graph transactions and exact releases

`claimtrace.graph-change-request/1` contains the exact current canonical `base_graph_hash`, an
optional bounded description, and a closed changes object with explicit `add_nodes`,
`replace_nodes`, `remove_nodes`, `add_edges`, `remove_edges`, `set_concepts`, and
`remove_concepts` operations. Missing operation lists/maps are normalized to empty. Proposal
building does not mutate the graph: it validates the request and complete result graph and writes a
content-addressed `claimtrace.graph-change-proposal/1` containing both base/result hashes and the
normalized operations. Application holds the graph lock and fails if the proposal changed, the base
graph drifted, or recomputation differs; it does not merge or rebase.

Version 1 proposal documents do not contain an authenticated approval or an in-tool reviewer
chain. Review and authorization therefore belong in repository governance such as a pull request,
signed commit, or equivalent external control. Arbitrary proposal output files are not
automatically discovered by the release inventory; retain a reviewed proposal in version control
or represent it as an explicitly modeled project document when it must be part of the release.

`claimtrace.project-release/2` is the current deterministic exact-byte inventory with a
`release:sha256:` ID. Creation performs two complete collections and fails if the inventories do
not match. Each file records a project-relative path or an exact configured external absolute path,
SHA-256, size, roles, and logical IDs. External entries are host-specific and can expose usernames
or workspace layout when a manifest is published. Scope
includes configured control files, graph-backed files, render manifests, surviving event and review
stores, adversarial deliberation records, semantic and symbolic assets, replay certificates, method
assessments, and pipeline-
contract current-source paths referenced by included events or method assessments. The
`pipeline_contract_current_source` role describes only the bytes currently at that path. Historical
pipeline snapshots retain their normalized declaration and source fingerprint, not a copy of the
prior raw contract JSON; use versioned paths, Git history, or an external content-addressed archive
when those prior bytes must remain recoverable. Verification
recollects the project and fails on modification, omission, or newly in-scope content; diff compares
two individually valid manifests.

Release-v2 adds the `deliberation_record` file role and exact proposal, frozen candidate-set,
ballot, phase-decision, and status schema declarations. Its scope includes every valid JSON record in
the configured deliberation store. Included actors, independence groups, ballots, recommendations,
and decisions remain self-asserted advisory records: the manifest authenticates neither identity nor
independence and does not activate any candidate.

Legacy `claimtrace.project-release/1` manifests remain valid only under one of their complete
historical canonical schema inventories and their original scope, which excludes deliberation.
Those accepted inventories cover the pre-stage-checkpoint, pre-portable-pipeline-snapshot, and final
release-v1 protocol states. Validation never accepts an arbitrary mixture or partial inventory and
never rewrites a v1 manifest to include later schemas or files.

A release manifest has no built-in signer and no independently anchored head. Its release ID may be
signed or committed to Git, a transparency log, or a blockchain by an external system. Such a
commitment detects later differences relative to the exact manifest; it does not establish prior
completeness, authenticate actor strings, or validate scientific meaning.

## Deterministic GraphRAG projection and bounded context

`provsleuth graphrag-export` builds `claimtrace.graphrag/1` from the canonical check report. It is
a read-only retrieval projection, not a semantic reasoner. Existing graph, run, assessment,
method-assessment, deliberation proposal/candidate/set/ballot/phase-decision, derivation, and proof
identifiers are preserved exactly; an identifier collision fails closed. Phase decisions use node
kind `deliberation_phase_decision`, authority `attributed_phase_routing_decision`, and the explicit
relations `phase_decision_for_candidate_set`, `phase_decision_routes_candidate`, and
`phase_decision_pins_ballot`. Findings without an existing ID receive a SHA-256 content address.
Declared `supports` and `refutes` edges remain explicitly declaration-only, accepted assessments
remain attributed judgments, deliberation candidates remain attributed grouped candidates rather
than authenticated-independent judgments, phase decisions remain procedural routing rather than
authenticated authorization, and symbolic proofs remain conditional consequences of the named
project rules. None becomes scientific truth, support, or activation through export.

The complete projection has a `projection_id` over its canonical content. A caller-supplied
projection is rehashed before retrieval, so changing a review state or record while retaining its
old ID is rejected. Cross-layer references that cannot be resolved remain listed in
`unresolved_references`; `graphrag-context` refuses to retrieve from such a projection unless a
caller explicitly opts into diagnostic use through the Python API.

`claimtrace.graphrag-context/1` is an undirected discovery neighborhood that preserves every
original edge direction in the returned records. It records known and unknown seeds, inclusion
paths, and exact exclusion counts. The default limits are two hops, 64 nodes, 128 edges, and
262,144 canonical UTF-8 bytes. Hard limits are eight hops, 512 nodes, 1,024 edges, and 1,048,576
bytes. Records are never text-truncated: a candidate record is omitted with a byte-budget reason,
and a required seed that cannot fit makes the request fail. `context_id` commits the source
projection ID, request, included records, and omission accounting, so an equal ID cannot conceal
different context bytes.

These bounds control deterministic serialization size, not an external model's tokenizer count.
An embedding index, vector database, reranker, or language model is deliberately outside the core.
Such a consumer may rank or summarize the returned records, but it must preserve their IDs, states,
authority labels, and exclusions rather than presenting generated prose as a ProvSleuth finding.

## Config: render, execution, assessment, deliberation, semantics, and logic policy

In `provsleuth.config.json`, `render_types` (default `["figure"]`) are the node types whose
staleness is checked, and `input_types` (default `["data", "artifact", "code"]`) are the types that
count as an *input* when deciding whether a render is stale. Set these if your project types its
nodes differently (e.g. `render_types: ["report", "table"]`) so staleness is not silently skipped.
`run_output_types` (default `["artifact"]`) adds non-render materialized types that need successful
run receipts under strict checking. `events` (default `provsleuth/events`) selects the local
event-ledger directory.

`assessments` (default `provsleuth/assessments`) selects the content-addressed semantic-assessment
store. `require_assessments` defaults to `false`; when `true`, it strict-blocks an uncovered direct
`supports`/`refutes` declaration and an uncovered structural result-to-claim `derives_from`
dependency. Direct declarations require matching polarity; structural dependencies require an
accepted active semantic relation. This policy does not turn an accepted assessment into
scientific truth; it only requires that the attributed semantic review exists and remains grounded
to the current nodes and artifact bytes.

The optional closed `deliberation` object selects the advisory multi-agent record store:

```json
{
  "deliberation": {
    "records": "provsleuth/deliberations"
  }
}
```

`records` is project-local and must be distinct from every other configured control or provenance
store. Deliberation has no `active`, `latest`, or `require_*` selector. Panel recommendations and
phase decisions appear in reports, releases, and the standalone audit view but never satisfy
semantic-assessment, normalization, or symbolic-derivation policy and never mutate project meaning.
Neither a bare graph hash, semantic-policy ID, rule-pack ID, approved phase decision, nor their
combination is activation proof; each actual activation or project mutation remains in its separate
reviewable workflow.

The optional closed `execution` object configures opaque-script evidence:

```json
{
  "execution": {
    "replays": "provsleuth/replays",
    "method_assessments": "provsleuth/method-assessments",
    "require_contracts": false,
    "require_replay": false,
    "require_method_assessments": false,
    "require_stage_checkpoints": false,
    "replay_attempts": 2
  }
}
```

Both stores are project-local and must be distinct from every other configured control/provenance
store. `replay_attempts` is an integer from 2 through 10. The four `require_*` switches default to
`false` for planning-safe and migration-safe adoption. When enabled, strict reports require the
corresponding current contract, review-ready boundary replay, accepted current method-conformance
evidence, or cooperative stage evidence for in-scope claim provenance. The checkpoint policy also
enables event-v4 capture on contract-bound runs; it cannot be used without a pipeline contract. Its
claim-level gate requires `cooperative_report_repeatable_current`: every stage self-reported exactly
once in a complete source trace, and replay-v3 reproduced that normalized callsite sequence.
Review-ready replay means current and byte-repeatable with no visible undeclared workspace write;
it still has the explicit non-hermetic and non-independent-stage limits recorded in its certificate.
A symbolic proof over result premises remains
conditionally valid under its rule pack when this execution basis is incomplete, but the report
marks the basis separately and makes it blocking when any of these execution gates is enabled.

The optional `semantics` object configures reviewed normalization:

```json
{
  "semantics": {
    "terminologies": ["provsleuth/semantics/local-terms.json"],
    "ontology_locks": ["provsleuth/semantics/domain.lock.json"],
    "mappings": "provsleuth/semantics/mappings",
    "policies": "provsleuth/semantics/policies",
    "active_policy": null,
    "allow_external_sources": false,
    "require_active_policy": false,
    "language": "en",
    "max_candidates": 25,
    "max_ontology_bytes": 536870912
  }
}
```

Terminology and ontology-lock paths are project-relative unless
`allow_external_sources: true`; that switch does not enable network access. Mapping and policy
stores always remain project-local and must be distinct. Duplicate configured asset paths are
rejected. `active_policy` is null or an exact stored `semantic-policy:sha256:<digest>` ID; paths and
mutable selector files are rejected. `max_candidates` is an integer from 1 to 1000; a result that reaches the limit and
omits additional exact matches is marked truncated and cannot be released. Set
`require_active_policy` only after an initial release is reviewed and explicitly pinned.
`max_ontology_bytes` defaults to 536,870,912 bytes (512 MiB) and may be explicitly raised to the
hard 274,877,906,944-byte (256 GiB) ceiling. It bounds aggregate raw ontology and index bytes before
hashing; a larger opt-in increases validation time and lock-contention exposure.

The optional `logic` object configures the portable symbolic layer:

```json
{
  "logic": {
    "derivations": "provsleuth/derivations",
    "vocabularies": ["provsleuth/logic/vocabulary.json"],
    "rule_packs": ["provsleuth/logic/rules.json"],
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
