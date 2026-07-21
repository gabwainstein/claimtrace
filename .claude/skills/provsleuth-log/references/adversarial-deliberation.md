# Adversarial claim and logic deliberation

Use this reference only when the user explicitly requests multi-agent deliberation over claim
extraction, semantic interpretation, formalization, or rule validity. ProvSleuth supplies a
provider-neutral immutable ledger and deterministic procedural gates. The surrounding agent
orchestrator supplies separate model calls or human reviewers.

The ledger is advisory. It does not authenticate actors, prove that contexts are independent,
decide scientific truth, or activate any graph, assessment, mapping, vocabulary, evidence plan,
rule pack, or derivation. Its strongest panel result is `recommended_for_human_review`. After that
result, the ledger can append one immutable, attributed phase decision, but the decision actor is
self-asserted, `human_identity_authenticated` remains `false`, and `automatic_activation` remains
`false`.

## Separate human-review boundary

Never turn a panel recommendation or approved phase decision directly into project policy. The
intended workflow requires a person to inspect the exact
source anchor, every frozen candidate, all ballots and blocking objections, the pinned project
snapshot, and any competency cases. ProvSleuth does not authenticate that person: actor labels,
phase-decision actors, and independence groups are self-asserted correlation metadata. If the
person chooses a candidate, apply it through the
separate reviewable boundary appropriate to the artifact:

- use `graph-propose` and a separately reviewed `graph-apply` for graph changes;
- use mapping proposal/review, inactive policy compilation, and an explicit human config edit for
  semantic-policy activation;
- review and edit project-owned vocabulary, target, evidence-plan, and rule-pack files through
  repository governance before running `derive`;
- use `assess` and a distinct review action for result-to-claim semantic support.

Do not let the same agent that generated a recommendation silently perform the human step. An agent
running this skill must stop at the recommendation and must not invoke `deliberate-decide`, claim to
be the human reviewer, or submit a decision on a person's behalf.
The panel output is evidence for review, not authorization. A phase decision is likewise an
attributed routing record, not
authorization or activation.

## Four sequential phases

| phase | required ballot roles | candidate purpose |
|---|---|---|
| `claim_extraction` | `source_verifier`, `coverage_reviewer`, `adversarial_falsifier` | preserve an atomic exact-source claim, its kind, speech act, polarity, and qualifiers |
| `semantic_interpretation` | `semantic_reviewer`, `scope_reviewer`, `adversarial_falsifier` | normalize meaning while preserving scope, modality, quantifier, negation, ambiguity, and non-equivalence |
| `formalization` | `evidence_mapper`, `logic_critic`, `adversarial_falsifier` | bind a reviewed interpretation to an existing configured vocabulary, typed target, all-of evidence plan, method requirements, and assumptions |
| `rule_validity` | `logic_critic`, `domain_reviewer`, `adversarial_falsifier` | propose a finite typed rule pack plus warrant, scope, non-equivalences, and adversarial competency cases |

Later phases require an approved stored phase decision for a candidate from the immediately
preceding phase, with the same `round_id`, `subject_key`, and exact frozen mechanical snapshot. Do
not skip a phase merely because one interpretation seems obvious. This may advance the immutable
planning record without activating a graph, semantic policy, vocabulary, evidence plan, rule pack,
or derivation. If any such project state changes, start a new round against the new snapshot.

## Cost-aware orchestration

Spend model calls only after deterministic preflight succeeds. Validate the source bytes, closed
request shape, project snapshot, exact graph identifiers, configured vocabulary, evidence bindings,
method steps, and any local ontology matches before asking an agent to interpret meaning. A failed
preflight stops the phase; an agent must not repair a missing identifier by guessing.

For one material claim, the minimum review-ready topology is one proposer context plus the three
distinct phase-role review contexts. One model call may return request documents for several subject
keys or frozen sets when it keeps one role and one correlation context, but append each record
separately. Do not multiply samples by default. Add another proposer or reviewer only when a frozen
alternative, ambiguity, dissent, high-risk inference, or missing role justifies escalation. Never
simulate separation by renaming the same call, shared conversation, or coordinated context. Do not
spend calls on later phases until the prior phase has a current recommendation and a separate
approved routing decision.

## Independence protocol

Before candidates are frozen, give each proposer only the pinned source/context it needs. Do not
show it other candidates if the workflow is supposed to test independent interpretation. Record
available model, revision, prompt, role-template, context-bundle, and retrieval-snapshot hashes;
omit unknown values rather than inventing them.

Use `independence_group` conservatively as a self-asserted correlation grouping:

- reuse one group for aliases, retries, or samples that share a coordinated context or review
  process;
- do not claim independent evidence merely because actor names differ;
- use different groups only when the surrounding workflow genuinely separated the relevant
  proposal or review contexts;
- report this as procedural, self-asserted correlation metadata, never authenticated independence.

ProvSleuth coalesces byte-equivalent candidate meaning but preserves each attributed proposal. It
requires multiple independence groups and phase-role coverage before recommending review. Those
gates reduce correlated error; they do not turn a vote into truth.

## 1. Propose exact candidates

The source node must exist, declare a UTF-8 text file, and the byte offsets must select the intended
passage without splitting a code point. Compute the optional `span_sha256` from those exact bytes;
never guess it. The request shape is closed:

```json
{
  "schema_version": "claimtrace.deliberation-proposal-request/1",
  "round_id": "round:paper-claim-1",
  "phase": "claim_extraction",
  "subject_key": "paper:claim-1",
  "source_anchor": {
    "node_id": "doc:paper-text",
    "start_byte": 120,
    "end_byte": 248,
    "span_sha256": "<computed-lowercase-sha256>"
  },
  "payload": {
    "claim_text": "<exact substring inside the selected bytes>",
    "claim_kind": "associational",
    "speech_act": "assertion",
    "polarity": "positive",
    "qualifiers": ["<explicit qualifier, if any>"]
  },
  "rationale": "Concise public reason for this candidate.",
  "provenance": {
    "model": "<reported model, if known>",
    "context_bundle_sha256": "<computed-lowercase-sha256>"
  }
}
```

Submit each independently generated proposal before revealing the candidate pool:

```bash
provsleuth --config /absolute/project/provsleuth.config.json deliberate-propose \
  proposal.json --actor <truthful-actor> --independence-group <truthful-group> --json
```

Phase payloads are closed:

- `claim_extraction`: `claim_text`, `claim_kind`, `speech_act`, `polarity`, `qualifiers`.
- `semantic_interpretation`: `extraction_candidate_id`, `normalized_claim`, the exact eight-field
  `frame`, `modality`, `{kind, range}` quantifier, `{polarity, scope}` negation scope,
  `conditions`, `ambiguities`, and `non_equivalences`.
- `formalization`: `interpretation_candidate_id`, exact configured `vocabulary_id`, typed `target`,
  closed all-of `evidence_plan`, `method_requirements`, `assumptions`, and non-empty
  `non_equivalences`.
- `rule_validity`: `formalization_candidate_id`, exact configured `vocabulary_id`, data-only
  `rule_pack`, `warrant`, `scope`, `assumptions`, non-empty `non_equivalences`, and
  `competency_cases`.

The v1 mandatory competency matrix is implemented through user- or agent-authored
`competency_cases` and reported by the `legacy_relational_competency_matrix` check. It is a legacy
matrix of relational fixtures and must contain `positive`, `explicit_negative`,
`missing_premise`, `boundary`,
`unit_mismatch`, `conflict`, and `counterexample`. ProvSleuth checks the declared expected state,
finite typed syntax, and cycles among derived predicates, then executes only the submitted cases.
It does not generate cases, explore the input domain, prove boundary completeness, or establish
full rule competency. Passing establishes only mechanical behavior under those authored fixtures,
not scientific validity.

## 2. Freeze the complete candidate union

After all proposal contexts have returned, freeze exactly one round, phase, and subject:

```json
{
  "schema_version": "claimtrace.deliberation-candidate-set-request/1",
  "round_id": "round:paper-claim-1",
  "phase": "claim_extraction",
  "subject_key": "paper:claim-1"
}
```

```bash
provsleuth --config /absolute/project/provsleuth.config.json deliberate-freeze \
  freeze.json --actor <coordinator-id> --json
```

Freeze fails on an empty, stale, already frozen, corrupt, or oversized set. Do not remove an
inconvenient candidate and refreeze. Start a new round if the project snapshot or intended candidate
pool changes.

## 3. Submit role-bound ballots

Give reviewers the frozen set, exact sources, and phase-specific task. A reviewer must evaluate
every candidate it did not propose and must not evaluate its own candidate. One actor submits only
one eligible role ballot per frozen set:

```json
{
  "schema_version": "claimtrace.deliberation-ballot-request/1",
  "candidate_set_id": "deliberation-set:sha256:<exact-digest>",
  "role": "source_verifier",
  "evaluations": [{
    "candidate_id": "deliberation-candidate:sha256:<exact-digest>",
    "decision": "reject",
    "reason_codes": ["CLAIM_NOT_ATOMIC"],
    "blocking": true,
    "rationale": "The candidate merges two separately testable assertions."
  }],
  "provenance": {
    "model": "<reported model, if known>",
    "prompt_sha256": "<computed-lowercase-sha256>"
  }
}
```

```bash
provsleuth --config /absolute/project/provsleuth.config.json deliberate-ballot \
  ballot.json --actor <truthful-reviewer> --independence-group <truthful-group> --json
```

Decisions are `endorse`, `reject`, or `abstain`. Reject and abstain require a closed reason code.
Use `blocking: true` for a defect that must be resolved before adoption; endorsements cannot block.
Preserve material dissent instead of pressuring reviewers toward a unanimous narrative.

## 4. Inspect status and route to a human

```bash
provsleuth --config /absolute/project/provsleuth.config.json deliberations \
  deliberation-set:sha256:<exact-digest> --json
```

Interpret states literally:

- `recommended_for_human_review`: exactly one frozen candidate exists and passed mechanical,
  independence, role, endorsement, and non-blocking gates; no other frozen alternative remains
  blocked, contested, or insufficient, and no policy changed.
- `contested`: material rejection/blocking exists or multiple alternatives remain eligible; there
  is intentionally no hash or lexical tie-break.
- `insufficient_review`: required independent groups, eligible role coverage, or non-abstaining
  evidence is missing.
- `blocked`: live drift, corrupt records, failed mechanical validation, or another hard finding
  prevents recommendation.

If unresolved, collect the missing genuinely separated review contexts or start a new round that
explicitly addresses the recorded findings; never erase dissent.

## 5. Append a separate phase decision

After a person reviews a `recommended_for_human_review` panel, they may provide this closed request:

```json
{
  "schema_version": "claimtrace.deliberation-phase-decision-request/1",
  "candidate_set_id": "deliberation-set:sha256:<exact-digest>",
  "candidate_id": "deliberation-candidate:sha256:<exact-digest>",
  "decision": "approved",
  "rationale": "Concise attributed reason for the routing decision."
}
```

```bash
provsleuth --config /absolute/project/provsleuth.config.json deliberate-decide \
  phase-decision.json --actor <separate-reviewer-id> --json
```

The actor must differ from every proposer and balloter for the frozen set. ProvSleuth accepts only
the panel's current recommended candidate, pins the complete ballot IDs, permits only one immutable
decision per set, and forbids later ballots. `rejected` records the outcome but does not unlock the
next phase. The stored `claimtrace.deliberation-phase-decision/1` record includes
`human_identity_authenticated: false` and `automatic_activation: false`. The actor label remains
self-asserted; the record does not prove that a human made the decision.

Re-run `deliberations <candidate-set-id> --json` and inspect `phase_decision_id`, the embedded
`phase_decision`, and `phase_routing_state`. The routing state is one of
`awaiting_attributed_phase_decision`, `approved_for_next_phase`,
`approved_for_external_application_review`, `rejected_by_attributed_reviewer`,
`recorded_decision_not_current`, `panel_unresolved`, or `panel_unavailable`. The targeted command's
exit code still describes panel status (0 recommended, 1 contested/insufficient, 2 blocked), not
whether the separate phase decision approved progression.

An approved decision is only an immutable routing gate. For the first three phases it may unlock a
proposal in the immediate next phase under the same round, subject, and snapshot. A rule-validity
approval routes the candidate to external review and isolated testing. It never activates project
state or proves scientific validity. Actual graph application, semantic-policy review and config
activation, repository-governed vocabulary or rule changes, assessment review, and `derive` remain
separate workflows. Any resulting state change invalidates the frozen snapshot and requires a new
round.
