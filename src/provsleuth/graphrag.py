"""Deterministic, read-only GraphRAG projection of a ProvSleuth check report.

This module deliberately provides graph-shaped retrieval data, not a retrieval model.
It performs no embedding, generation, database write, network request, or scientific
inference.  In particular, declared ``supports``/``refutes`` edges remain declarations;
review records and symbolic proofs are represented as separate nodes so a consumer
cannot mistake their presence for scientific truth.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter, defaultdict, deque


PROJECTION_SCHEMA = "claimtrace.graphrag/1"
CONTEXT_SCHEMA = "claimtrace.graphrag-context/1"
SUPPORTED_REPORT_SCHEMA_VERSIONS = ("1.7", "1.8")
EXCLUSION_RECORD_LIMIT = 64
DEFAULT_MAX_CONTEXT_BYTES = 256 * 1024
MAX_CONTEXT_HOPS = 8
MAX_CONTEXT_NODES = 512
MAX_CONTEXT_EDGES = 1024
MAX_CONTEXT_BYTES = 1024 * 1024


class GraphRAGError(ValueError):
    """Raised when a report cannot be projected without losing its meaning."""


def _canonical_json(value):
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise GraphRAGError(f"value is not canonical JSON: {exc}") from exc


def _content_id(prefix, value):
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}:sha256:{digest}"


def _as_object(value, label):
    if not isinstance(value, dict):
        raise GraphRAGError(f"{label} must be an object")
    return value


def _as_list(value, label):
    if not isinstance(value, list):
        raise GraphRAGError(f"{label} must be a list")
    return value


def _stable_id(value, label):
    if not isinstance(value, str) or not value:
        raise GraphRAGError(f"{label} must be a non-empty string")
    return value


def _id_list(value, label):
    values = _as_list(value, label)
    result = []
    seen = set()
    for index, item in enumerate(values):
        identifier = _stable_id(item, f"{label}[{index}]")
        if identifier in seen:
            raise GraphRAGError(f"{label} contains duplicate id {identifier!r}")
        seen.add(identifier)
        result.append(identifier)
    return result


def _text(*parts):
    values = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, str):
            value = part.strip()
        else:
            value = _canonical_json(part)
        if value and value not in values:
            values.append(value)
    return "\n".join(values)


def _state(record, keys):
    """Copy named state fields exactly; absent values stay explicitly null."""
    return {key: copy.deepcopy(record.get(key)) for key in keys}


def _node(identifier, kind, record, *, state, text, authority, meaning):
    return {
        "id": identifier,
        "kind": kind,
        "authority": authority,
        "meaning": meaning,
        "text": text,
        "state": state,
        "record": copy.deepcopy(record),
    }


def _add_node(nodes_by_id, node):
    identifier = _stable_id(node.get("id"), "projection node id")
    if identifier in nodes_by_id:
        previous = nodes_by_id[identifier]
        raise GraphRAGError(
            "node id collision across report records: "
            f"{identifier!r} ({previous.get('kind')} and {node.get('kind')})"
        )
    nodes_by_id[identifier] = node


def _assessment_state(item, integrity):
    review = _as_object(item.get("review"), "semantic assessment review")
    current = _as_object(
        item.get("current_derived"), "semantic assessment current_derived"
    )
    agent_input = _as_object(
        item.get("agent_input"), "semantic assessment agent_input"
    )
    return {
        "integrity": integrity,
        "review_state": copy.deepcopy(review.get("state")),
        "effective_review_state": copy.deepcopy(
            current.get("effective_review_state")
        ),
        "is_current": copy.deepcopy(item.get("is_current")),
        "stale": copy.deepcopy(current.get("stale")),
        "verdict": copy.deepcopy(agent_input.get("verdict")),
        "proposed_relation": copy.deepcopy(current.get("proposed_relation")),
        "active_relation": copy.deepcopy(current.get("active_relation")),
    }


def _method_assessment_state(item, integrity):
    review = _as_object(item.get("review"), "method assessment review")
    current = _as_object(
        item.get("current_derived"), "method assessment current_derived"
    )
    return {
        "integrity": integrity,
        "review_state": copy.deepcopy(review.get("state")),
        "effective_review_state": copy.deepcopy(
            current.get("effective_review_state")
        ),
        "is_current": copy.deepcopy(item.get("is_current")),
        "stale": copy.deepcopy(current.get("stale")),
        "agent_verdict": copy.deepcopy(item.get("agent_verdict")),
        "implementation_current": copy.deepcopy(
            current.get("implementation_current")
        ),
        "scientific_validity": copy.deepcopy(current.get("scientific_validity")),
        "stage_execution_observation": copy.deepcopy(
            current.get("stage_execution_observation")
        ),
    }


def _derivation_state(item, integrity):
    effective = item.get("effective")
    if effective is None:
        effective = item.get("stored_derived")
    effective = _as_object(effective, "symbolic derivation effective state")
    return {
        "integrity": integrity,
        "effective_state": copy.deepcopy(effective.get("effective_state")),
        "proof_state": copy.deepcopy(effective.get("proof_state")),
        "active": copy.deepcopy(effective.get("active")),
        "stale": copy.deepcopy(effective.get("stale")),
        "claim_bound": copy.deepcopy(effective.get("claim_bound")),
        "outcome_relation": copy.deepcopy(effective.get("outcome_relation")),
    }


def _make_edge(source, target, relation, kind, *, record, state, traversable,
               authority, meaning, identifier=None):
    source = _stable_id(source, f"{kind} edge source")
    target = _stable_id(target, f"{kind} edge target")
    relation = _stable_id(relation, f"{kind} edge relation")
    payload = {
        "from": source,
        "to": target,
        "rel": relation,
        "kind": kind,
        "authority": authority,
        "meaning": meaning,
        "traversable": bool(traversable),
        "state": copy.deepcopy(state),
        "record": copy.deepcopy(record),
    }
    if identifier is None:
        identifier = _content_id("graphrag-edge", payload)
    else:
        identifier = _stable_id(identifier, f"{kind} edge id")
    return {"id": identifier, **payload}


def _add_edge(edges_by_id, edge, nodes_by_id, unresolved):
    missing = [node_id for node_id in (edge["from"], edge["to"])
               if node_id not in nodes_by_id]
    if missing:
        for node_id in missing:
            unresolved.append({
                "owner_id": edge["from"],
                "missing_id": node_id,
                "relation": edge["rel"],
                "edge_kind": edge["kind"],
                "reason": "referenced_node_absent_from_projection",
            })
        return
    identifier = edge["id"]
    if identifier in edges_by_id:
        raise GraphRAGError(f"projection edge id collision: {identifier!r}")
    edges_by_id[identifier] = edge


def _edge_sort_key(edge):
    ranks = {
        "semantic": 0,
        "run_binding": 1,
        "semantic_assessment": 2,
        "method_assessment": 3,
        "derivation": 4,
        "proof": 5,
        "deliberation": 6,
        "finding": 7,
    }
    return (
        ranks.get(edge.get("kind"), 99),
        edge.get("from", ""),
        edge.get("to", ""),
        edge.get("rel", ""),
        edge.get("id", ""),
    )


def _node_sort_key(node):
    ranks = {
        "semantic": 0,
        "run": 1,
        "assessment": 2,
        "method_assessment": 3,
        "derivation": 4,
        "proof": 5,
        "deliberation_candidate": 6,
        "deliberation_proposal": 7,
        "deliberation_set": 8,
        "deliberation_ballot": 9,
        "finding": 10,
    }
    return (ranks.get(node.get("kind"), 99), node.get("id", ""))


def _assessment_link_records(assessments, field):
    records = _as_list(
        assessments.get(field), f"report.assessments.{field}"
    )
    indexed = {}
    for index, item in enumerate(records):
        label = f"report.assessments.{field}[{index}]"
        item = _as_object(item, label)
        source = _stable_id(item.get("from"), f"{label}.from")
        target = _stable_id(item.get("to"), f"{label}.to")
        relation = _stable_id(
            item.get("declared_relation"), f"{label}.declared_relation"
        )
        _stable_id(item.get("status"), f"{label}.status")
        _id_list(item.get("assessment_ids"), f"{label}.assessment_ids")
        _id_list(item.get("assessed_relations"), f"{label}.assessed_relations")
        key = (source, target, relation)
        if key in indexed:
            raise GraphRAGError(
                f"report.assessments.{field} contains duplicate relation {key!r}"
            )
        indexed[key] = copy.deepcopy(item)
    return indexed


def _validate_active_relations(assessments):
    records = _as_list(
        assessments.get("active_relations"),
        "report.assessments.active_relations",
    )
    seen = set()
    for index, item in enumerate(records):
        label = f"report.assessments.active_relations[{index}]"
        item = _as_object(item, label)
        values = tuple(
            _stable_id(item.get(field), f"{label}.{field}")
            for field in ("assessment_id", "from", "to", "rel")
        )
        if values in seen:
            raise GraphRAGError(
                "report.assessments.active_relations contains a duplicate record"
            )
        seen.add(values)


def _claim_subject(item, label):
    subject = _as_object(item.get("subject"), f"{label}.subject")
    claim_id = _stable_id(subject.get("claim_id"), f"{label}.subject.claim_id")
    result_ids = _id_list(
        subject.get("result_ids"), f"{label}.subject.result_ids"
    )
    return subject, claim_id, result_ids


def build_projection(report):
    """Build a deterministic graph projection from one canonical check report.

    Exact source identifiers are retained for graph nodes, runs, assessments, method
    assessments, derivations, and proofs.  Findings receive a content address only when
    the report does not provide an identifier.  Any cross-layer identifier collision is
    rejected rather than silently renamed.
    """
    report = _as_object(report, "report")
    source_schema = report.get("report_schema_version")
    if source_schema not in SUPPORTED_REPORT_SCHEMA_VERSIONS:
        raise GraphRAGError(
            "unsupported ProvSleuth report schema "
            f"{source_schema!r}; supported: {', '.join(SUPPORTED_REPORT_SCHEMA_VERSIONS)}"
        )
    if report.get("fatal") is not None:
        raise GraphRAGError("cannot project a fatal check report without a complete graph")

    graph = _as_object(report.get("graph"), "report.graph")
    raw_nodes = _as_list(graph.get("nodes"), "report.graph.nodes")
    raw_edges = _as_list(graph.get("edges"), "report.graph.edges")
    receipts = _as_object(report.get("receipts"), "report.receipts")
    assessments = _as_object(report.get("assessments"), "report.assessments")
    method_assessments = _as_object(
        report.get("method_assessments"), "report.method_assessments"
    )
    derivations = _as_object(report.get("derivations"), "report.derivations")
    claim_basis = _as_object(report.get("claim_basis"), "report.claim_basis")
    semantics = _as_object(report.get("semantics"), "report.semantics")
    if source_schema == "1.8":
        deliberations = _as_object(
            report.get("deliberations"), "report.deliberations",
        )
    else:
        legacy_deliberations = report.get("deliberations")
        if legacy_deliberations is not None:
            raise GraphRAGError("report schema 1.7 must not contain deliberations")
        deliberations = {
            "integrity": "not_present_in_report_schema_1.7",
            "integrity_issues": [], "records": [], "panels": [], "open_groups": [],
            "human_activation_required": True, "automatic_activation": False,
            "scientific_truth_established": False,
        }
    findings = _as_list(report.get("findings"), "report.findings")
    _as_object(graph.get("concepts"), "report.graph.concepts")
    _id_list(graph.get("trajectory_order"), "report.graph.trajectory_order")
    _as_object(graph.get("metadata"), "report.graph.metadata")

    nodes_by_id = {}
    edges_by_id = {}
    unresolved = []

    for raw_node in raw_nodes:
        raw_node = _as_object(raw_node, "semantic graph node")
        identifier = _stable_id(raw_node.get("id"), "semantic graph node id")
        state = {
            "declared_status": copy.deepcopy(raw_node.get("status")),
            "declared_type": copy.deepcopy(raw_node.get("type")),
            "declaration_only": True,
        }
        _add_node(nodes_by_id, _node(
            identifier,
            "semantic",
            raw_node,
            state=state,
            text=_text(
                identifier, raw_node.get("value"), raw_node.get("note"),
                raw_node.get("path"),
            ),
            authority="declared_semantic_graph",
            meaning="declared research entity; not independently verified by projection",
        ))

    assessment_link_states = _assessment_link_records(
        assessments, "declared_links"
    )
    for key, item in _assessment_link_records(
            assessments, "required_dependencies").items():
        if key in assessment_link_states:
            raise GraphRAGError(
                "the same assessed relation appears in declared_links and "
                f"required_dependencies: {key!r}"
            )
        assessment_link_states[key] = item
    _validate_active_relations(assessments)

    for raw_edge in raw_edges:
        raw_edge = _as_object(raw_edge, "semantic graph edge")
        source = _stable_id(raw_edge.get("from"), "semantic graph edge from")
        target = _stable_id(raw_edge.get("to"), "semantic graph edge to")
        relation = _stable_id(raw_edge.get("rel"), "semantic graph edge rel")
        stable_edge_id = raw_edge.get("id")
        edge = _make_edge(
            source,
            target,
            relation,
            "semantic",
            record=raw_edge,
            state={
                "declaration_state": "declared",
                "semantic_review": assessment_link_states.get(
                    (source, target, relation)
                ),
                "scientific_validity": "not_assessed_by_projection",
            },
            traversable=True,
            authority="declared_semantic_graph",
            meaning=(
                "declared relation; a supports/refutes label is not upgraded to "
                "reviewed scientific support by this projection"
            ),
            identifier=stable_edge_id,
        )
        _add_edge(edges_by_id, edge, nodes_by_id, unresolved)

    runs = _as_list(receipts.get("runs"), "report.receipts.runs")
    for run_index, run in enumerate(runs):
        run = _as_object(run, "run projection")
        identifier = _stable_id(run.get("run_id"), "run id")
        bindings = _as_list(
            run.get("bindings"), f"report.receipts.runs[{run_index}].bindings"
        )
        _as_list(
            run.get("replays"), f"report.receipts.runs[{run_index}].replays"
        )
        for binding_index, binding in enumerate(bindings):
            binding = _as_object(
                binding,
                f"report.receipts.runs[{run_index}].bindings[{binding_index}]",
            )
            _stable_id(
                binding.get("node_id"),
                f"report.receipts.runs[{run_index}].bindings[{binding_index}].node_id",
            )
        state = _state(run, (
            "outcome", "evidence_eligible", "pipeline_contract_state",
            "current_gate_role", "event_store_integrity", "link_integrity",
            "receipt_integrity", "replay_conflict",
        ))
        state["receipt_store_integrity"] = copy.deepcopy(receipts.get("integrity"))
        _add_node(nodes_by_id, _node(
            identifier,
            "run",
            run,
            state=state,
            text=_text(identifier, run.get("name"), run.get("outcome"),
                       run.get("pipeline_contract_state")),
            authority="mechanical_receipt_projection",
            meaning=(
                "partial process-boundary receipt; inputs are declared and writes are "
                "not causally attributed"
            ),
        ))

    assessment_items = _as_list(
        assessments.get("items"), "report.assessments.items"
    )
    for item_index, item in enumerate(assessment_items):
        item = _as_object(item, "semantic assessment")
        identifier = _stable_id(item.get("id"), "semantic assessment id")
        label = f"report.assessments.items[{item_index}]"
        _claim_subject(item, label)
        agent_input = _as_object(item.get("agent_input"), f"{label}.agent_input")
        _as_object(item.get("review"), f"{label}.review")
        _as_object(item.get("mechanical_snapshot"), f"{label}.mechanical_snapshot")
        _as_object(item.get("stored_derived"), f"{label}.stored_derived")
        _as_object(item.get("current_derived"), f"{label}.current_derived")
        if not isinstance(item.get("is_current"), bool):
            raise GraphRAGError(f"{label}.is_current must be a boolean")
        _add_node(nodes_by_id, _node(
            identifier,
            "assessment",
            item,
            state=_assessment_state(item, assessments.get("integrity")),
            text=_text(
                identifier, agent_input.get("verdict"), agent_input.get("rationale"),
                agent_input.get("recommended_claim"), agent_input.get("limitations"),
            ),
            authority="attributed_semantic_assessment",
            meaning=(
                "attributed and review-state-bearing semantic judgement; not scientific truth"
            ),
        ))

    method_items = _as_list(
        method_assessments.get("items"), "report.method_assessments.items"
    )
    for item_index, item in enumerate(method_items):
        item = _as_object(item, "method assessment")
        identifier = _stable_id(item.get("id"), "method assessment id")
        label = f"report.method_assessments.items[{item_index}]"
        subject = _as_object(item.get("subject"), f"{label}.subject")
        _stable_id(subject.get("method_id"), f"{label}.subject.method_id")
        _stable_id(
            subject.get("pipeline_contract_id"),
            f"{label}.subject.pipeline_contract_id",
        )
        _as_object(item.get("review"), f"{label}.review")
        _as_object(item.get("current_derived"), f"{label}.current_derived")
        step_alignments = _as_list(
            item.get("step_alignments"), f"{label}.step_alignments"
        )
        for alignment_index, alignment in enumerate(step_alignments):
            _as_object(
                alignment, f"{label}.step_alignments[{alignment_index}]"
            )
        if not isinstance(item.get("is_current"), bool):
            raise GraphRAGError(f"{label}.is_current must be a boolean")
        _add_node(nodes_by_id, _node(
            identifier,
            "method_assessment",
            item,
            state=_method_assessment_state(
                item, method_assessments.get("integrity")
            ),
            text=_text(
                identifier, item.get("agent_verdict"), item.get("step_alignments"),
            ),
            authority="attributed_method_assessment",
            meaning=(
                "review-state-bearing code-to-method judgement; runtime stage execution "
                "and scientific validity remain separate"
            ),
        ))

    derivation_items = _as_list(
        derivations.get("items"), "report.derivations.items"
    )
    for item_index, item in enumerate(derivation_items):
        item = _as_object(item, "symbolic derivation")
        identifier = _stable_id(item.get("id"), "symbolic derivation id")
        label = f"report.derivations.items[{item_index}]"
        _claim_subject(item, label)
        _as_object(item.get("agent_input"), f"{label}.agent_input")
        _as_object(item.get("mechanical_snapshot"), f"{label}.mechanical_snapshot")
        _as_object(item.get("stored_derived"), f"{label}.stored_derived")
        effective = _as_object(item.get("effective"), f"{label}.effective")
        _add_node(nodes_by_id, _node(
            identifier,
            "derivation",
            item,
            state=_derivation_state(item, derivations.get("integrity")),
            text=_text(
                identifier, effective.get("rendered_target"),
                effective.get("proof_state"), effective.get("rendered_outcomes"),
            ),
            authority="conditional_symbolic_derivation",
            meaning="conditional derivability under declared project rules; not truth or support",
        ))

    active_proofs = _as_list(
        derivations.get("active_proofs"), "report.derivations.active_proofs"
    )
    for proof_index, proof in enumerate(active_proofs):
        proof = _as_object(proof, "active symbolic proof")
        identifier = _stable_id(proof.get("proof_id"), "symbolic proof id")
        label = f"report.derivations.active_proofs[{proof_index}]"
        _stable_id(proof.get("claim_id"), f"{label}.claim_id")
        _id_list(proof.get("result_ids"), f"{label}.result_ids")
        _id_list(proof.get("derivation_ids"), f"{label}.derivation_ids")
        execution_basis = proof.get("execution_basis")
        if execution_basis is not None:
            execution_basis = _as_object(
                execution_basis, f"{label}.execution_basis"
            )
        _add_node(nodes_by_id, _node(
            identifier,
            "proof",
            proof,
            state={
                "integrity": copy.deepcopy(derivations.get("integrity")),
                "proof_state": copy.deepcopy(proof.get("proof_state")),
                "claim_level_active": copy.deepcopy(proof.get("claim_level_active")),
                "execution_basis_state": copy.deepcopy(
                    (execution_basis or {}).get("state")
                ),
            },
            text=_text(
                identifier, proof.get("rendered_target"), proof.get("proof_state"),
                proof.get("rendered_outcomes"),
            ),
            authority="active_conditional_symbolic_proof",
            meaning="active conditional proof under named rules; not scientific truth or support",
        ))

    deliberation_records = _as_list(
        deliberations.get("records"), "report.deliberations.records",
    )
    deliberation_panels = _as_list(
        deliberations.get("panels"), "report.deliberations.panels",
    )
    _as_list(
        deliberations.get("integrity_issues"),
        "report.deliberations.integrity_issues",
    )
    _as_list(
        deliberations.get("open_groups"), "report.deliberations.open_groups",
    )
    for field in (
            "human_activation_required", "automatic_activation",
            "scientific_truth_established"):
        if not isinstance(deliberations.get(field), bool):
            raise GraphRAGError(f"report.deliberations.{field} must be a boolean")
    if deliberations["human_activation_required"] is not True:
        raise GraphRAGError("deliberation projection cannot waive human activation")
    if (deliberations["automatic_activation"] is not False
            or deliberations["scientific_truth_established"] is not False):
        raise GraphRAGError("deliberation projection overstates its authority")

    panel_by_set = {}
    candidate_panel_states = {}
    for panel_index, panel in enumerate(deliberation_panels):
        label = f"report.deliberations.panels[{panel_index}]"
        panel = _as_object(panel, label)
        set_id = _stable_id(panel.get("candidate_set_id"), f"{label}.candidate_set_id")
        if set_id in panel_by_set:
            raise GraphRAGError(f"duplicate deliberation panel {set_id!r}")
        panel_by_set[set_id] = panel
        if panel.get("human_activation_required") is not True:
            raise GraphRAGError(f"{label} cannot waive human activation")
        if panel.get("automatic_activation") is not False:
            raise GraphRAGError(f"{label} cannot claim automatic activation")
        candidates = _as_list(panel.get("candidates"), f"{label}.candidates")
        for candidate_index, candidate in enumerate(candidates):
            candidate_label = f"{label}.candidates[{candidate_index}]"
            candidate = _as_object(candidate, candidate_label)
            candidate_id = _stable_id(
                candidate.get("candidate_id"), f"{candidate_label}.candidate_id",
            )
            if candidate_id in candidate_panel_states:
                raise GraphRAGError(
                    f"candidate appears in more than one frozen panel: {candidate_id!r}"
                )
            candidate_panel_states[candidate_id] = {
                "candidate_set_id": set_id,
                "panel_status": copy.deepcopy(panel.get("status")),
                "candidate_state": copy.deepcopy(candidate.get("state")),
                "recommended_for_human_review": (
                    panel.get("recommended_candidate_id") == candidate_id
                ),
                "human_activation_required": True,
                "automatic_activation": False,
            }

    proposals_by_candidate = defaultdict(list)
    proposal_records = []
    set_records = []
    ballot_records = []
    phase_decision_records = []
    for record_index, record in enumerate(deliberation_records):
        label = f"report.deliberations.records[{record_index}]"
        record = _as_object(record, label)
        record_type = record.get("record_type")
        if record_type == "proposal":
            identifier = _stable_id(record.get("proposal_id"), f"{label}.proposal_id")
            candidate_id = _stable_id(
                record.get("candidate_id"), f"{label}.candidate_id",
            )
            _stable_id(record.get("phase"), f"{label}.phase")
            _stable_id(record.get("subject_key"), f"{label}.subject_key")
            _as_object(record.get("source_anchor"), f"{label}.source_anchor")
            _as_object(record.get("payload"), f"{label}.payload")
            _as_object(record.get("actor"), f"{label}.actor")
            proposals_by_candidate[candidate_id].append(record)
            proposal_records.append(record)
            _add_node(nodes_by_id, _node(
                identifier, "deliberation_proposal", record,
                state={
                    "phase": copy.deepcopy(record.get("phase")),
                    "candidate_id": candidate_id,
                    "mechanical_validation_passed": copy.deepcopy(
                        (record.get("mechanical_validation") or {}).get("passed")
                    ),
                    "human_activation_required": True,
                    "automatic_activation": False,
                },
                text=_text(
                    identifier, record.get("subject_key"), record.get("payload"),
                    record.get("rationale"),
                ),
                authority="attributed_deliberation_proposal",
                meaning=(
                    "source-anchored attributed candidate; not accepted meaning, truth, "
                    "or executable policy"
                ),
            ))
        elif record_type == "candidate_set":
            identifier = _stable_id(
                record.get("candidate_set_id"), f"{label}.candidate_set_id",
            )
            _id_list(record.get("candidate_ids"), f"{label}.candidate_ids")
            set_records.append(record)
            panel = panel_by_set.get(identifier) or {}
            _add_node(nodes_by_id, _node(
                identifier, "deliberation_set", record,
                state={
                    "phase": copy.deepcopy(record.get("phase")),
                    "panel_status": copy.deepcopy(panel.get("status")),
                    "recommended_candidate_id": copy.deepcopy(
                        panel.get("recommended_candidate_id")
                    ),
                    "human_activation_required": True,
                    "automatic_activation": False,
                },
                text=_text(identifier, record.get("subject_key"), panel.get("status")),
                authority="frozen_deliberation_candidate_union",
                meaning=(
                    "immutable review set and procedural panel status; no winner is activated"
                ),
            ))
        elif record_type == "ballot":
            identifier = _stable_id(record.get("ballot_id"), f"{label}.ballot_id")
            _stable_id(record.get("candidate_set_id"), f"{label}.candidate_set_id")
            evaluations = _as_list(record.get("evaluations"), f"{label}.evaluations")
            for evaluation_index, evaluation in enumerate(evaluations):
                evaluation = _as_object(
                    evaluation, f"{label}.evaluations[{evaluation_index}]",
                )
                _stable_id(
                    evaluation.get("candidate_id"),
                    f"{label}.evaluations[{evaluation_index}].candidate_id",
                )
            ballot_records.append(record)
            _add_node(nodes_by_id, _node(
                identifier, "deliberation_ballot", record,
                state={
                    "role": copy.deepcopy(record.get("role")),
                    "human_activation_required": True,
                    "automatic_activation": False,
                },
                text=_text(identifier, record.get("role"), evaluations),
                authority="attributed_deliberation_ballot",
                meaning=(
                    "role-bound attributed critique; not a democratic truth vote"
                ),
            ))
        elif record_type == "phase_decision":
            identifier = _stable_id(
                record.get("decision_id"), f"{label}.decision_id",
            )
            candidate_set_id = _stable_id(
                record.get("candidate_set_id"), f"{label}.candidate_set_id",
            )
            candidate_id = _stable_id(
                record.get("candidate_id"), f"{label}.candidate_id",
            )
            ballot_ids = _id_list(record.get("ballot_ids"), f"{label}.ballot_ids")
            actor = _stable_id(record.get("actor"), f"{label}.actor")
            decision = record.get("decision")
            if decision not in {"approved", "rejected"}:
                raise GraphRAGError(f"{label}.decision is unsupported")
            if record.get("human_identity_authenticated") is not False:
                raise GraphRAGError(
                    f"{label} cannot claim authenticated human identity"
                )
            if record.get("automatic_activation") is not False:
                raise GraphRAGError(f"{label} cannot claim automatic activation")
            phase_decision_records.append(record)
            _add_node(nodes_by_id, _node(
                identifier, "deliberation_phase_decision", record,
                state={
                    "decision": copy.deepcopy(decision),
                    "candidate_set_id": candidate_set_id,
                    "candidate_id": candidate_id,
                    "ballot_ids": ballot_ids,
                    "human_identity_authenticated": False,
                    "human_activation_required": True,
                    "automatic_activation": False,
                },
                text=_text(
                    identifier, decision, actor, record.get("rationale"),
                ),
                authority="attributed_phase_routing_decision",
                meaning=(
                    "attributed procedural routing decision under a self-asserted actor "
                    "label; not authenticated authorization, scientific truth, support, "
                    "or project-state activation"
                ),
            ))
        else:
            raise GraphRAGError(f"{label}.record_type is unsupported")

    for candidate_id, proposals in sorted(proposals_by_candidate.items()):
        first = sorted(proposals, key=lambda item: item["proposal_id"])[0]
        state = candidate_panel_states.get(candidate_id, {
            "candidate_set_id": None,
            "panel_status": "open",
            "candidate_state": "open",
            "recommended_for_human_review": False,
            "human_activation_required": True,
            "automatic_activation": False,
        })
        candidate_record = {
            "candidate_id": candidate_id,
            "round_id": first.get("round_id"),
            "phase": first.get("phase"),
            "subject_key": first.get("subject_key"),
            "source_anchor": copy.deepcopy(first.get("source_anchor")),
            "payload": copy.deepcopy(first.get("payload")),
            "proposal_ids": sorted(item["proposal_id"] for item in proposals),
            "panel_state": copy.deepcopy(state),
        }
        _add_node(nodes_by_id, _node(
            candidate_id, "deliberation_candidate", candidate_record,
            state=state,
            text=_text(
                candidate_id, first.get("subject_key"), first.get("payload"),
                state.get("candidate_state"),
            ),
            authority="attributed_grouped_deliberation_candidate",
            meaning=(
                "content-equivalent exact candidate grouped across attributed proposals; "
                "group labels are self-asserted and do not authenticate independence; "
                "the candidate is never accepted or true merely by appearing here"
            ),
        ))

    for finding_index, finding in enumerate(findings):
        finding = _as_object(finding, "report finding")
        label = f"report.findings[{finding_index}]"
        _stable_id(finding.get("code"), f"{label}.code")
        _stable_id(finding.get("severity"), f"{label}.severity")
        if not isinstance(finding.get("blocking"), bool):
            raise GraphRAGError(f"{label}.blocking must be a boolean")
        node_id = finding.get("node_id")
        if node_id is not None:
            _stable_id(node_id, f"{label}.node_id")
        if finding.get("id") is None:
            identifier = _content_id("finding", finding)
        else:
            identifier = _stable_id(finding.get("id"), "finding id")
        _add_node(nodes_by_id, _node(
            identifier,
            "finding",
            finding,
            state=_state(finding, ("severity", "blocking", "code", "node_id")),
            text=_text(identifier, finding.get("code"), finding.get("detail")),
            authority="canonical_check_finding",
            meaning="diagnostic finding from the canonical check report",
        ))

    # Add cross-layer edges only after every node has been registered.  Their labels
    # describe record membership, never inferred scientific support.
    for run_index, run in enumerate(runs):
        run_id = run["run_id"]
        bindings = _as_list(
            run.get("bindings"), f"report.receipts.runs[{run_index}].bindings"
        )
        for binding_index, binding in enumerate(bindings):
            binding = _as_object(
                binding,
                f"report.receipts.runs[{run_index}].bindings[{binding_index}]",
            )
            node_id = _stable_id(
                binding.get("node_id"),
                f"report.receipts.runs[{run_index}].bindings[{binding_index}].node_id",
            )
            binding_kind = str(binding.get("binding_kind") or "output_path")
            edge = _make_edge(
                run_id, node_id, "receipt_binding", "run_binding",
                record=binding,
                state={
                    **_state(binding, (
                        "binding_kind", "current", "integrity_state",
                        "declaration_comparison", "output_evidence", "stage_attribution",
                    )),
                    "run_outcome": copy.deepcopy(run.get("outcome")),
                },
                traversable=True,
                authority="mechanical_receipt_projection",
                meaning=(
                    f"{binding_kind} receipt association; not observed read or "
                    "stage-caused write"
                ),
            )
            _add_edge(edges_by_id, edge, nodes_by_id, unresolved)

    for item_index, item in enumerate(assessment_items):
        assessment_id = item["id"]
        label = f"report.assessments.items[{item_index}]"
        _subject, claim_id, result_ids = _claim_subject(item, label)
        current = _as_object(item.get("current_derived"), f"{label}.current_derived")
        shared_state = {
            "effective_review_state": copy.deepcopy(
                current.get("effective_review_state")
            ),
            "stale": copy.deepcopy(current.get("stale")),
            "is_current": copy.deepcopy(item.get("is_current")),
            "active_relation": copy.deepcopy(current.get("active_relation")),
            "integrity": copy.deepcopy(assessments.get("integrity")),
        }
        for result_id in result_ids:
            edge = _make_edge(
                result_id, assessment_id, "assessment_compares_result",
                "semantic_assessment", record=item,
                state=shared_state, traversable=True,
                authority="attributed_semantic_assessment",
                meaning="result selected for an attributed semantic comparison",
            )
            _add_edge(edges_by_id, edge, nodes_by_id, unresolved)
        edge = _make_edge(
            assessment_id, claim_id, "assessment_evaluates_claim",
            "semantic_assessment", record=item,
            state=shared_state, traversable=True,
            authority="attributed_semantic_assessment",
            meaning=(
                "assessment targets this claim; active_relation is retained only as state"
            ),
        )
        _add_edge(edges_by_id, edge, nodes_by_id, unresolved)

    for item_index, item in enumerate(method_items):
        assessment_id = item["id"]
        subject = _as_object(
            item.get("subject"),
            f"report.method_assessments.items[{item_index}].subject",
        )
        method_id = _stable_id(
            subject.get("method_id"),
            f"report.method_assessments.items[{item_index}].subject.method_id",
        )
        edge = _make_edge(
            assessment_id, method_id, "assesses_method_conformance",
            "method_assessment", record=item,
            state=_method_assessment_state(item, method_assessments.get("integrity")),
            traversable=True,
            authority="attributed_method_assessment",
            meaning="assessment of declared method/code conformance; not execution or validity",
        )
        _add_edge(edges_by_id, edge, nodes_by_id, unresolved)

    for item_index, item in enumerate(derivation_items):
        derivation_id = item["id"]
        label = f"report.derivations.items[{item_index}]"
        _subject, claim_id, result_ids = _claim_subject(item, label)
        edge_state = _derivation_state(item, derivations.get("integrity"))
        for result_id in result_ids:
            edge = _make_edge(
                result_id, derivation_id, "selected_conditional_premise", "derivation",
                record=item, state=edge_state, traversable=True,
                authority="conditional_symbolic_derivation",
                meaning="selected formal premise under declared rule policy",
            )
            _add_edge(edges_by_id, edge, nodes_by_id, unresolved)
        edge = _make_edge(
            derivation_id, claim_id, "evaluates_formal_claim_target", "derivation",
            record=item, state=edge_state, traversable=True,
            authority="conditional_symbolic_derivation",
            meaning="conditional formal outcome for the claim target; not scientific support",
        )
        _add_edge(edges_by_id, edge, nodes_by_id, unresolved)

    for proof_index, proof in enumerate(active_proofs):
        proof_id = proof["proof_id"]
        label = f"report.derivations.active_proofs[{proof_index}]"
        proof_state = {
            "proof_state": copy.deepcopy(proof.get("proof_state")),
            "claim_level_active": copy.deepcopy(proof.get("claim_level_active")),
            "integrity": copy.deepcopy(derivations.get("integrity")),
        }
        for derivation_id in _id_list(
                proof.get("derivation_ids"), f"{label}.derivation_ids"):
            edge = _make_edge(
                derivation_id, proof_id, "groups_into_conditional_proof", "proof",
                record=proof, state=proof_state, traversable=True,
                authority="active_conditional_symbolic_proof",
                meaning="derivation grouped into the active conditional proof",
            )
            _add_edge(edges_by_id, edge, nodes_by_id, unresolved)
        for result_id in _id_list(proof.get("result_ids"), f"{label}.result_ids"):
            edge = _make_edge(
                result_id, proof_id, "conditional_formal_premise", "proof",
                record=proof, state=proof_state, traversable=True,
                authority="active_conditional_symbolic_proof",
                meaning="formal premise used by a conditional proof",
            )
            _add_edge(edges_by_id, edge, nodes_by_id, unresolved)
        claim_id = _stable_id(proof.get("claim_id"), f"{label}.claim_id")
        edge = _make_edge(
            proof_id, claim_id, "conditional_formal_outcome", "proof",
            record=proof, state=proof_state, traversable=True,
            authority="active_conditional_symbolic_proof",
            meaning="conditional rule outcome; not truth or scientific support",
        )
        _add_edge(edges_by_id, edge, nodes_by_id, unresolved)

    reference_fields = {
        "semantic_interpretation": "extraction_candidate_id",
        "formalization": "interpretation_candidate_id",
        "rule_validity": "formalization_candidate_id",
    }
    for proposal in proposal_records:
        proposal_id = proposal["proposal_id"]
        candidate_id = proposal["candidate_id"]
        source_id = proposal["source_anchor"].get("node_id")
        edge = _make_edge(
            source_id, proposal_id, "anchors_deliberation_proposal", "deliberation",
            record=proposal["source_anchor"],
            state={"exact_byte_anchor": True, "human_activation_required": True},
            traversable=True,
            authority="mechanical_source_anchor",
            meaning="exact source bytes selected for an attributed candidate",
        )
        _add_edge(edges_by_id, edge, nodes_by_id, unresolved)
        edge = _make_edge(
            proposal_id, candidate_id, "proposes_exact_candidate", "deliberation",
            record=proposal,
            state={"phase": proposal.get("phase"), "automatic_activation": False},
            traversable=True,
            authority="attributed_deliberation_proposal",
            meaning=(
                "proposal contributes to this content-equivalent exact candidate; "
                "attribution does not authenticate reviewer independence"
            ),
        )
        _add_edge(edges_by_id, edge, nodes_by_id, unresolved)
        subject = proposal.get("subject_key")
        if subject in nodes_by_id:
            edge = _make_edge(
                candidate_id, subject, "candidate_about_subject", "deliberation",
                record={"subject_key": subject},
                state={"automatic_activation": False}, traversable=True,
                authority="deliberation_subject_reference",
                meaning="candidate concerns this subject; it does not support the subject",
            )
            _add_edge(edges_by_id, edge, nodes_by_id, unresolved)
        reference_field = reference_fields.get(proposal.get("phase"))
        if reference_field:
            referenced = proposal.get("payload", {}).get(reference_field)
            edge = _make_edge(
                referenced, candidate_id, "refined_by_candidate", "deliberation",
                record={"reference_field": reference_field},
                state={"automatic_activation": False}, traversable=True,
                authority="deliberation_phase_reference",
                meaning="later-phase candidate explicitly refines the earlier exact candidate",
            )
            _add_edge(edges_by_id, edge, nodes_by_id, unresolved)

    for candidate_set in set_records:
        set_id = candidate_set["candidate_set_id"]
        for candidate_id in candidate_set["candidate_ids"]:
            edge = _make_edge(
                candidate_id, set_id, "member_of_frozen_candidate_set", "deliberation",
                record={"candidate_id": candidate_id, "candidate_set_id": set_id},
                state=candidate_panel_states.get(candidate_id, {}), traversable=True,
                authority="frozen_deliberation_candidate_union",
                meaning="candidate was present when the complete review set was frozen",
            )
            _add_edge(edges_by_id, edge, nodes_by_id, unresolved)

    for ballot in ballot_records:
        ballot_id = ballot["ballot_id"]
        set_id = ballot["candidate_set_id"]
        edge = _make_edge(
            ballot_id, set_id, "ballot_for_candidate_set", "deliberation",
            record={"role": ballot.get("role")},
            state={"automatic_activation": False}, traversable=True,
            authority="attributed_deliberation_ballot",
            meaning="role-bound ballot submitted for this frozen set",
        )
        _add_edge(edges_by_id, edge, nodes_by_id, unresolved)
        for evaluation in ballot["evaluations"]:
            edge = _make_edge(
                ballot_id, evaluation["candidate_id"], "evaluates_candidate",
                "deliberation", record=evaluation,
                state={
                    "decision": evaluation.get("decision"),
                    "blocking": evaluation.get("blocking"),
                    "automatic_activation": False,
                },
                traversable=True,
                authority="attributed_deliberation_ballot",
                meaning="attributed critique of an exact candidate; not a truth vote",
            )
            _add_edge(edges_by_id, edge, nodes_by_id, unresolved)

    for decision in phase_decision_records:
        decision_id = decision["decision_id"]
        decision_state = {
            "decision": copy.deepcopy(decision.get("decision")),
            "human_identity_authenticated": False,
            "human_activation_required": True,
            "automatic_activation": False,
        }
        edge = _make_edge(
            decision_id, decision["candidate_set_id"],
            "phase_decision_for_candidate_set", "deliberation",
            record=decision, state=decision_state, traversable=True,
            authority="attributed_phase_routing_decision",
            meaning=(
                "attributed routing decision over this frozen review set; it is not "
                "authenticated authorization and activates no project state"
            ),
        )
        _add_edge(edges_by_id, edge, nodes_by_id, unresolved)
        edge = _make_edge(
            decision_id, decision["candidate_id"],
            "phase_decision_routes_candidate", "deliberation",
            record=decision, state=decision_state, traversable=True,
            authority="attributed_phase_routing_decision",
            meaning=(
                "records an approved or rejected procedural route for this exact "
                "candidate; not scientific support, truth, or activation"
            ),
        )
        _add_edge(edges_by_id, edge, nodes_by_id, unresolved)
        for ballot_id in decision["ballot_ids"]:
            edge = _make_edge(
                decision_id, ballot_id, "phase_decision_pins_ballot",
                "deliberation",
                record={"decision_id": decision_id, "ballot_id": ballot_id},
                state=decision_state, traversable=True,
                authority="attributed_phase_routing_decision",
                meaning=(
                    "the routing decision pins this exact attributed ballot as review "
                    "context; the ballot is not an authenticated vote"
                ),
            )
            _add_edge(edges_by_id, edge, nodes_by_id, unresolved)

    finding_nodes = [node for node in nodes_by_id.values() if node["kind"] == "finding"]
    for node in finding_nodes:
        finding = node["record"]
        target = finding.get("node_id")
        if target is None:
            continue
        if not isinstance(target, str) or not target:
            raise GraphRAGError(f"finding {node['id']!r} has an invalid node_id")
        edge = _make_edge(
            node["id"], target, "finding_about", "finding",
            record=finding,
            state=_state(finding, ("severity", "blocking", "code")),
            traversable=True,
            authority="canonical_check_finding",
            meaning="diagnostic finding attached to the declared graph node",
        )
        _add_edge(edges_by_id, edge, nodes_by_id, unresolved)

    nodes = sorted(nodes_by_id.values(), key=_node_sort_key)
    edges = sorted(edges_by_id.values(), key=_edge_sort_key)
    unresolved.sort(key=_canonical_json)
    kind_counts = Counter(node["kind"] for node in nodes)
    edge_kind_counts = Counter(edge["kind"] for edge in edges)

    projection = {
        "schema_version": PROJECTION_SCHEMA,
        "source": {
            "report_schema_version": source_schema,
            "ok": copy.deepcopy(report.get("ok")),
            "exit_code": copy.deepcopy(report.get("exit_code")),
            "scope": copy.deepcopy(report.get("scope")),
            "policy": copy.deepcopy(report.get("policy")),
            "summary": copy.deepcopy(report.get("summary")),
            "integrity": {
                "receipts": copy.deepcopy(receipts.get("integrity")),
                "assessments": copy.deepcopy(assessments.get("integrity")),
                "method_assessments": copy.deepcopy(
                    method_assessments.get("integrity")
                ),
                "derivations": copy.deepcopy(derivations.get("integrity")),
                "semantics": copy.deepcopy(semantics.get("integrity")),
                "deliberations": copy.deepcopy(deliberations.get("integrity")),
            },
        },
        "semantic_graph": {
            "schema_version": copy.deepcopy(graph.get("schema_version")),
            "concepts": copy.deepcopy(graph.get("concepts")),
            "trajectory_order": copy.deepcopy(graph.get("trajectory_order")),
            "metadata": copy.deepcopy(graph.get("metadata")),
        },
        "nodes": nodes,
        "edges": edges,
        "unresolved_references": unresolved,
        "summary": {
            "nodes": len(nodes),
            "edges": len(edges),
            "node_kinds": {key: kind_counts[key] for key in sorted(kind_counts)},
            "edge_kinds": {
                key: edge_kind_counts[key] for key in sorted(edge_kind_counts)
            },
            "unresolved_references": len(unresolved),
        },
        "claim_basis": copy.deepcopy(claim_basis),
        "semantic_policy": copy.deepcopy(semantics),
        "deliberations": copy.deepcopy(deliberations),
        "coverage_notice": (
            "Read-only deterministic graph projection. Declared semantic edges remain "
            "declarations. Assessment nodes retain attributed review and stale states. "
            "Symbolic proof nodes express conditional derivability under project rules, "
            "not truth or scientific support. Deliberation nodes preserve source-anchored "
            "proposals, frozen candidate unions, role-bound critiques, abstentions, "
            "dissent, and attributed phase-routing decisions under self-asserted actor "
            "and group labels. None authenticates identity, establishes scientific truth "
            "or support, or activates project state. Run "
            "nodes are partial boundary receipts, not observed reads, causally attributed "
            "writes, or universal determinism."
        ),
    }
    projection["projection_id"] = _content_id("graphrag", projection)
    return projection


def _validate_limit(value, label, *, minimum, maximum):
    if (isinstance(value, bool) or not isinstance(value, int)
            or value < minimum or value > maximum):
        raise GraphRAGError(
            f"{label} must be an integer between {minimum} and {maximum}"
        )


def _normalise_seed_ids(seed_ids):
    if isinstance(seed_ids, str):
        values = [seed_ids]
    else:
        try:
            values = list(seed_ids)
        except TypeError as exc:
            raise GraphRAGError("seed_ids must be a string or iterable of strings") from exc
    for value in values:
        _stable_id(value, "seed id")
    return sorted(set(values))


def _bounded_records(records, *, limit=EXCLUSION_RECORD_LIMIT):
    _validate_limit(
        limit, "exclusion record limit", minimum=0,
        maximum=EXCLUSION_RECORD_LIMIT,
    )
    ordered = sorted(records, key=_canonical_json)
    return {
        "records": ordered[:limit],
        "listed": min(len(ordered), limit),
        "omitted": max(0, len(ordered) - limit),
        "record_limit": limit,
    }


def _finalize_context(core):
    """Attach an ID committing every context field and return canonical byte size."""
    context = copy.deepcopy(core)
    context["context_id"] = _content_id("graphrag-context", core)
    size = len(_canonical_json(context).encode("utf-8"))
    return context, size


def bounded_context(projection, seed_ids, *, max_hops=2, max_nodes=64,
                    max_edges=128, traversable_only=True,
                    allow_unresolved=False,
                    max_bytes=DEFAULT_MAX_CONTEXT_BYTES):
    """Return a deterministic bounded undirected k-hop retrieval context.

    Traversal is undirected so a claim seed can retrieve upstream evidence.  The
    original edge direction remains unchanged in the returned edge records.  Node
    discovery edges are prioritized before additional edges between already included
    nodes, ensuring every non-seed node has an explicit included path.
    """
    projection = _as_object(projection, "projection")
    if projection.get("schema_version") != PROJECTION_SCHEMA:
        raise GraphRAGError(
            f"unsupported GraphRAG projection schema {projection.get('schema_version')!r}"
        )
    supplied_projection_id = _stable_id(
        projection.get("projection_id"), "projection id",
    )
    projection_core = copy.deepcopy(projection)
    projection_core.pop("projection_id", None)
    expected_projection_id = _content_id("graphrag", projection_core)
    if supplied_projection_id != expected_projection_id:
        raise GraphRAGError("projection content does not match projection_id")
    _validate_limit(
        max_hops, "max_hops", minimum=0, maximum=MAX_CONTEXT_HOPS,
    )
    _validate_limit(
        max_nodes, "max_nodes", minimum=1, maximum=MAX_CONTEXT_NODES,
    )
    _validate_limit(
        max_edges, "max_edges", minimum=0, maximum=MAX_CONTEXT_EDGES,
    )
    _validate_limit(
        max_bytes, "max_bytes", minimum=1, maximum=MAX_CONTEXT_BYTES,
    )
    if not isinstance(traversable_only, bool):
        raise GraphRAGError("traversable_only must be a boolean")
    if not isinstance(allow_unresolved, bool):
        raise GraphRAGError("allow_unresolved must be a boolean")
    unresolved = _as_list(
        projection.get("unresolved_references"),
        "projection.unresolved_references",
    )
    for index, item in enumerate(unresolved):
        label = f"projection.unresolved_references[{index}]"
        item = _as_object(item, label)
        for field in ("owner_id", "missing_id", "relation", "edge_kind", "reason"):
            _stable_id(item.get(field), f"{label}.{field}")
    if unresolved and not allow_unresolved:
        raise GraphRAGError(
            "projection has unresolved references; refusing to build a retrieval "
            "context without allow_unresolved=True"
        )

    nodes = _as_list(projection.get("nodes"), "projection.nodes")
    edges = _as_list(projection.get("edges"), "projection.edges")
    node_by_id = {}
    for node in nodes:
        node = _as_object(node, "projection node")
        identifier = _stable_id(node.get("id"), "projection node id")
        if identifier in node_by_id:
            raise GraphRAGError(f"duplicate projection node id {identifier!r}")
        node_by_id[identifier] = node
    edge_by_id = {}
    for edge in edges:
        edge = _as_object(edge, "projection edge")
        identifier = _stable_id(edge.get("id"), "projection edge id")
        if identifier in edge_by_id:
            raise GraphRAGError(f"duplicate projection edge id {identifier!r}")
        for endpoint in ("from", "to"):
            node_id = _stable_id(edge.get(endpoint), f"projection edge {endpoint}")
            if node_id not in node_by_id:
                raise GraphRAGError(
                    f"projection edge {identifier!r} references absent node {node_id!r}"
                )
        if not isinstance(edge.get("traversable"), bool):
            raise GraphRAGError(
                f"projection edge {identifier!r} traversable must be a boolean"
            )
        edge_by_id[identifier] = edge

    requested_seeds = _normalise_seed_ids(seed_ids)
    known_seeds = [item for item in requested_seeds if item in node_by_id]
    unknown_seeds = [item for item in requested_seeds if item not in node_by_id]
    if not known_seeds:
        raise GraphRAGError(
            "none of the requested seed ids exist in the projection"
        )
    if len(known_seeds) > max_nodes:
        raise GraphRAGError(
            "max_nodes is smaller than the number of known seed ids; refusing to "
            "silently omit a requested seed"
        )

    permitted = {
        edge_id: edge for edge_id, edge in edge_by_id.items()
        if not traversable_only or edge["traversable"]
    }
    adjacency = defaultdict(list)
    for edge_id, edge in permitted.items():
        adjacency[edge["from"]].append((edge["to"], edge_id))
        adjacency[edge["to"]].append((edge["from"], edge_id))
    for node_id in adjacency:
        adjacency[node_id].sort(key=lambda item: (item[0], item[1]))

    # Unbudgeted distances make exclusions explicit even when a budget prunes a path.
    reachable_distance = {seed: 0 for seed in known_seeds}
    queue = deque(known_seeds)
    while queue:
        source = queue.popleft()
        distance = reachable_distance[source]
        if distance >= max_hops:
            continue
        for target, _edge_id in adjacency.get(source, []):
            if target in reachable_distance:
                continue
            reachable_distance[target] = distance + 1
            queue.append(target)

    included = set(known_seeds)
    distance = {seed: 0 for seed in known_seeds}
    node_reason = {
        seed: {"id": seed, "distance": 0, "reason": "seed",
               "from_id": None, "via_edge_id": None}
        for seed in known_seeds
    }
    selected_edges = set()
    edge_reason = {}
    frontier = list(known_seeds)
    blocked_nodes = {}

    for hop in range(1, max_hops + 1):
        candidates = {}
        for source in sorted(frontier):
            for target, edge_id in adjacency.get(source, []):
                if target in included:
                    continue
                option = (source, edge_id)
                if target not in candidates or option < candidates[target]:
                    candidates[target] = option
        next_frontier = []
        for target in sorted(candidates):
            source, edge_id = candidates[target]
            if len(included) >= max_nodes:
                blocked_nodes[target] = "node_budget"
                continue
            if len(selected_edges) >= max_edges:
                blocked_nodes[target] = "edge_budget"
                continue
            included.add(target)
            distance[target] = hop
            selected_edges.add(edge_id)
            node_reason[target] = {
                "id": target,
                "distance": hop,
                "reason": "k_hop",
                "from_id": source,
                "via_edge_id": edge_id,
            }
            edge_reason[edge_id] = {
                "id": edge_id,
                "reason": "discovery_path",
                "from_id": source,
                "to_id": target,
                "distance": hop,
            }
            next_frontier.append(target)
        frontier = next_frontier
        if not frontier:
            break

    for edge_id, edge in sorted(permitted.items()):
        if edge_id in selected_edges:
            continue
        if edge["from"] not in included or edge["to"] not in included:
            continue
        if len(selected_edges) >= max_edges:
            continue
        selected_edges.add(edge_id)
        edge_reason[edge_id] = {
            "id": edge_id,
            "reason": "connects_included_nodes",
            "from_id": edge["from"],
            "to_id": edge["to"],
            "distance": max(distance[edge["from"]], distance[edge["to"]]),
        }

    request = {
        "seed_ids": requested_seeds,
        "known_seed_ids": known_seeds,
        "unknown_seed_ids": unknown_seeds,
        "max_hops": max_hops,
        "max_nodes": max_nodes,
        "max_edges": max_edges,
        "max_bytes": max_bytes,
        "traversable_only": traversable_only,
        "allow_unresolved": allow_unresolved,
        "direction": "both_preserving_source_edge_direction",
    }
    byte_removed_nodes = set()
    byte_removed_edges = set()

    def assemble(exclusion_record_limit):
        excluded_node_records = []
        for node_id in sorted(set(node_by_id) - included):
            if node_id in byte_removed_nodes:
                reason = "byte_budget"
            elif node_id in blocked_nodes:
                reason = blocked_nodes[node_id]
            elif node_id in reachable_distance:
                if len(included) >= max_nodes:
                    reason = "node_budget"
                elif len(selected_edges) >= max_edges:
                    reason = "edge_budget"
                else:
                    reason = "path_pruned_by_budget"
            else:
                reason = "outside_hop_boundary_or_disconnected"
            excluded_node_records.append({
                "id": node_id,
                "reason": reason,
                "reachable_distance": reachable_distance.get(node_id),
            })

        excluded_edge_records = []
        for edge_id, edge in sorted(edge_by_id.items()):
            if edge_id in selected_edges:
                continue
            if (edge_id in byte_removed_edges
                    or edge["from"] in byte_removed_nodes
                    or edge["to"] in byte_removed_nodes):
                reason = "byte_budget"
            elif traversable_only and not edge["traversable"]:
                reason = "non_traversable"
            elif edge["from"] not in included or edge["to"] not in included:
                reason = "endpoint_excluded"
            elif len(selected_edges) >= max_edges:
                reason = "edge_budget"
            else:
                reason = "not_selected"
            excluded_edge_records.append({
                "id": edge_id,
                "from": edge["from"],
                "to": edge["to"],
                "reason": reason,
            })

        node_exclusion_counts = Counter(
            item["reason"] for item in excluded_node_records
        )
        edge_exclusion_counts = Counter(
            item["reason"] for item in excluded_edge_records
        )
        included_nodes = sorted(
            (copy.deepcopy(node_by_id[node_id]) for node_id in included),
            key=lambda item: (distance[item["id"]], item["id"]),
        )
        included_edges = sorted(
            (copy.deepcopy(edge_by_id[edge_id]) for edge_id in selected_edges),
            key=_edge_sort_key,
        )
        inclusion_nodes = sorted(
            (copy.deepcopy(node_reason[node_id]) for node_id in included),
            key=lambda item: (item["distance"], item["id"]),
        )
        inclusion_edges = sorted(
            (copy.deepcopy(edge_reason[edge_id]) for edge_id in selected_edges),
            key=lambda item: item["id"],
        )
        metadata_byte_limited = exclusion_record_limit < EXCLUSION_RECORD_LIMIT
        core = {
            "schema_version": CONTEXT_SCHEMA,
            "source_projection_schema_version": PROJECTION_SCHEMA,
            "source_projection_id": supplied_projection_id,
            "request": request,
            "nodes": included_nodes,
            "edges": included_edges,
            "inclusion": {
                "nodes": inclusion_nodes,
                "edges": inclusion_edges,
                "counts": {
                    "nodes": len(included_nodes), "edges": len(included_edges),
                },
            },
            "exclusion": {
                "unknown_seed_ids": unknown_seeds,
                "node_counts_by_reason": {
                    key: node_exclusion_counts[key]
                    for key in sorted(node_exclusion_counts)
                },
                "edge_counts_by_reason": {
                    key: edge_exclusion_counts[key]
                    for key in sorted(edge_exclusion_counts)
                },
                "nodes": _bounded_records(
                    excluded_node_records, limit=exclusion_record_limit,
                ),
                "edges": _bounded_records(
                    excluded_edge_records, limit=exclusion_record_limit,
                ),
                "node_limit_reached": bool(
                    node_exclusion_counts.get("node_budget")
                ),
                "edge_limit_reached": bool(
                    node_exclusion_counts.get("edge_budget")
                    or edge_exclusion_counts.get("edge_budget")
                ),
                "byte_limit_reached": bool(
                    byte_removed_nodes or byte_removed_edges
                    or metadata_byte_limited
                ),
                "metadata_record_limit_reason": (
                    "byte_budget" if metadata_byte_limited else None
                ),
            },
            "coverage_notice": (
                "Bounded undirected graph neighborhood with original edge directions "
                "retained. Inclusion is deterministic and lexical within each hop. "
                "Retrieval does not validate, endorse, or generate scientific claims. "
                "Node and edge records are included whole or excluded whole; record "
                "contents are never truncated. Exclusion metadata lists whole records "
                f"up to {EXCLUSION_RECORD_LIMIT} per category, or fewer when required "
                "by max_bytes, with exact omitted counts."
            ),
        }
        return _finalize_context(core)

    def best_current_fit():
        candidate, candidate_size = assemble(EXCLUSION_RECORD_LIMIT)
        if candidate_size <= max_bytes:
            return candidate
        smallest, smallest_size = assemble(0)
        if smallest_size > max_bytes:
            return None
        best = smallest
        low, high = 1, EXCLUSION_RECORD_LIMIT - 1
        while low <= high:
            middle = (low + high) // 2
            candidate, candidate_size = assemble(middle)
            if candidate_size <= max_bytes:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        return best

    # Establish the irreducible request before pruning optional context.  This uses
    # complete seed records and mandatory metadata, with no optional edge or metadata
    # listing.  A seed is never silently dropped or partially serialized.
    full_included = set(included)
    full_selected_edges = set(selected_edges)
    full_node_reason = copy.deepcopy(node_reason)
    full_edge_reason = copy.deepcopy(edge_reason)
    included.clear()
    included.update(known_seeds)
    selected_edges.clear()
    node_reason.clear()
    node_reason.update({
        seed: full_node_reason[seed] for seed in known_seeds
    })
    edge_reason.clear()
    byte_removed_nodes.update(full_included - set(known_seeds))
    byte_removed_edges.update(full_selected_edges)
    _seed_context, seed_size = assemble(0)
    if seed_size > max_bytes:
        raise GraphRAGError(
            "seed records and mandatory context metadata exceed max_bytes; "
            "refusing to truncate or omit a requested seed"
        )

    # Restore the count-bounded candidate, then preserve as much whole-record context
    # as fits.  Exclusion listings are ancillary and are reduced before graph records.
    included.clear()
    included.update(full_included)
    selected_edges.clear()
    selected_edges.update(full_selected_edges)
    node_reason.clear()
    node_reason.update(full_node_reason)
    edge_reason.clear()
    edge_reason.update(full_edge_reason)
    byte_removed_nodes.clear()
    byte_removed_edges.clear()
    fitted = best_current_fit()
    if fitted is not None:
        return fitted

    optional_edges = sorted(
        (
            edge_id for edge_id in selected_edges
            if edge_reason[edge_id]["reason"] == "connects_included_nodes"
        ),
        key=lambda edge_id: _edge_sort_key(edge_by_id[edge_id]),
        reverse=True,
    )
    for edge_id in optional_edges:
        selected_edges.remove(edge_id)
        edge_reason.pop(edge_id)
        byte_removed_edges.add(edge_id)
        fitted = best_current_fit()
        if fitted is not None:
            return fitted

    removable_nodes = sorted(
        (node_id for node_id in included if node_id not in known_seeds),
        key=lambda node_id: (distance[node_id], node_id),
        reverse=True,
    )
    for node_id in removable_nodes:
        incident_edges = sorted(
            edge_id for edge_id in selected_edges
            if (edge_by_id[edge_id]["from"] == node_id
                or edge_by_id[edge_id]["to"] == node_id)
        )
        for edge_id in incident_edges:
            selected_edges.remove(edge_id)
            edge_reason.pop(edge_id)
            byte_removed_edges.add(edge_id)
        included.remove(node_id)
        node_reason.pop(node_id)
        byte_removed_nodes.add(node_id)
        fitted = best_current_fit()
        if fitted is not None:
            return fitted

    # The earlier irreducible check guarantees this branch is unreachable unless an
    # internal accounting invariant changes.
    raise GraphRAGError("max_bytes accounting failed after reducing context to seeds")


__all__ = [
    "CONTEXT_SCHEMA",
    "DEFAULT_MAX_CONTEXT_BYTES",
    "EXCLUSION_RECORD_LIMIT",
    "GraphRAGError",
    "MAX_CONTEXT_BYTES",
    "MAX_CONTEXT_EDGES",
    "MAX_CONTEXT_HOPS",
    "MAX_CONTEXT_NODES",
    "PROJECTION_SCHEMA",
    "SUPPORTED_REPORT_SCHEMA_VERSIONS",
    "bounded_context",
    "build_projection",
]
