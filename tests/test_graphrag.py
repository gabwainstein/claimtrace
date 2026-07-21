"""Focused tests for the deterministic, read-only GraphRAG projection."""
import copy
import hashlib
import json

import pytest

from provsleuth.graphrag import (
    CONTEXT_SCHEMA,
    DEFAULT_MAX_CONTEXT_BYTES,
    MAX_CONTEXT_BYTES,
    MAX_CONTEXT_EDGES,
    MAX_CONTEXT_HOPS,
    MAX_CONTEXT_NODES,
    PROJECTION_SCHEMA,
    GraphRAGError,
    bounded_context,
    build_projection,
)
from provsleuth.cli import main


def _canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _readdress_projection(projection):
    projection = copy.deepcopy(projection)
    projection.pop("projection_id", None)
    digest = hashlib.sha256(_canonical_bytes(projection)).hexdigest()
    projection["projection_id"] = f"graphrag:sha256:{digest}"
    return projection


def _report():
    semantic_nodes = [
        {"id": "data:source", "type": "data", "status": "current",
         "path": "data/source.csv"},
        {"id": "artifact:result", "type": "artifact", "status": "current",
         "path": "results/result.json", "value": "effect=0.4"},
        {"id": "method:fit", "type": "method", "status": "current",
         "value": "Fit the declared model."},
        {"id": "claim:effect", "type": "claim", "status": "confirmed",
         "value": "The measured effect is positive."},
    ]
    semantic_edges = [
        {"from": "data:source", "to": "artifact:result", "rel": "produces"},
        {"from": "artifact:result", "to": "claim:effect", "rel": "supports"},
    ]
    assessment = {
        "id": "assessment:sha256:semantic",
        "schema_version": "claimtrace.assessment/2",
        "recorded_at": "2026-07-18T01:00:00.000Z",
        "subject": {
            "claim_id": "claim:effect",
            "result_ids": ["artifact:result"],
        },
        "mechanical_snapshot": {},
        "agent_input": {
            "verdict": "supports_narrower_claim",
            "rationale": "Direction aligns, but the inference level is narrower.",
            "recommended_claim": "The measured association was positive.",
            "limitations": ["No causal identification."],
        },
        "review": {"state": "accepted", "actor": "reviewer:one"},
        "stored_derived": {"proposed_relation": "related"},
        "current_derived": {
            "effective_review_state": "accepted",
            "proposed_relation": "related",
            "active_relation": "related",
            "stale": False,
            "findings": [],
        },
        "is_current": True,
    }
    method_assessment = {
        "id": "method-assessment:sha256:fit",
        "recorded_at": "2026-07-18T01:01:00.000Z",
        "subject": {
            "method_id": "method:fit",
            "pipeline_contract_id": "pipeline:sha256:fit",
        },
        "review": {"state": "contested", "actor": "reviewer:two"},
        "agent_verdict": "partially_implements",
        "step_alignments": [
            {"method_step_id": "fit", "stage_id": "fit", "alignment": "partial"},
        ],
        "current_derived": {
            "effective_review_state": "contested",
            "stale": True,
            "implementation_current": False,
            "scientific_validity": "not_assessed",
            "stage_execution_observation": "declared_only_not_observed",
            "findings": [],
        },
        "is_current": True,
    }
    derivation = {
        "id": "derivation:sha256:effect",
        "recorded_at": "2026-07-18T01:02:00.000Z",
        "actor": "agent:logic",
        "subject": {
            "claim_id": "claim:effect",
            "result_ids": ["artifact:result"],
            "vocabulary_id": "study:vocabulary",
            "rule_pack_id": "study:rules",
        },
        "agent_input": {},
        "mechanical_snapshot": {},
        "stored_derived": {"proof_state": "derivable"},
        "effective": {
            "effective_state": "active",
            "proof_id": "proof:sha256:effect",
            "proof_state": "derivable",
            "active": True,
            "stale": False,
            "claim_bound": True,
            "outcome_relation": "target",
            "rendered_target": "positive_effect(study)",
            "rendered_outcomes": ["positive_effect(study)"],
        },
    }
    proof = {
        "derivation_id": derivation["id"],
        "derivation_ids": [derivation["id"]],
        "proof_id": "proof:sha256:effect",
        "claim_id": "claim:effect",
        "result_ids": ["artifact:result"],
        "vocabulary_id": "study:vocabulary",
        "rule_pack_id": "study:rules",
        "proof_state": "derivable",
        "claim_level_active": True,
        "target": {"predicate": "positive_effect", "arguments": ["study"]},
        "rendered_target": "positive_effect(study)",
        "outcome_relation": "target",
        "outcome_atoms": [],
        "rendered_outcomes": ["positive_effect(study)"],
        "assumptions": [],
        "execution_basis": {"state": "symbolically_active_execution_basis_incomplete"},
    }
    finding = {
        "severity": "warning",
        "code": "UNASSESSED_CLAIM_LINK",
        "node_id": "claim:effect",
        "detail": "The declared supports edge needs an exact semantic assessment.",
        "sources": ["assessments"],
        "blocking": True,
    }
    return {
        "report_schema_version": "1.7",
        "tool": {"name": "provsleuth", "version": "fixture"},
        "scope": {
            "scientific_validity": "not-assessed",
            "runtime_observation": "partial_reads_and_write_causation_not_observed",
        },
        "policy": {"strict": True, "blocking_severities": ["error", "warning"]},
        "ok": False,
        "exit_code": 1,
        "summary": {"nodes": 4, "edges": 2, "warnings": 1, "blocking": 1},
        "graph": {
            "schema_version": "1.0",
            "concepts": {},
            "nodes": semantic_nodes,
            "edges": semantic_edges,
            "trajectory_order": [item["id"] for item in semantic_nodes],
            "metadata": {},
        },
        "receipts": {
            "integrity": "ok",
            "runs": [{
                "run_id": "run:00000000-0000-0000-0000-000000000001",
                "name": "fit",
                "outcome": "succeeded",
                "evidence_eligible": True,
                "pipeline_contract_state": "current",
                "current_gate_role": "current_candidate",
                "event_store_integrity": "ok",
                "link_integrity": "ok",
                "receipt_integrity": "ok",
                "replay_conflict": False,
                "bindings": [{
                    "binding_kind": "output_path",
                    "node_id": "artifact:result",
                    "path": "results/result.json",
                    "current": True,
                    "integrity_state": "ok",
                    "declaration_comparison": "declarations_agree",
                    "output_evidence": "unattributed_pre_post_delta",
                    "stage_attribution": "not_attributed",
                }],
                "replays": [],
            }],
        },
        "assessments": {
            "integrity": "ok",
            "items": [assessment],
            "active_relations": [{
                "assessment_id": assessment["id"],
                "from": "artifact:result", "to": "claim:effect", "rel": "related",
            }],
            "declared_links": [{
                "from": "artifact:result",
                "to": "claim:effect",
                "declared_relation": "supports",
                "status": "assessed_not_as_written",
                "assessment_ids": [assessment["id"]],
                "assessed_relations": ["related"],
            }],
            "required_dependencies": [],
        },
        "method_assessments": {
            "schema_version": "claimtrace.method-assessment/1",
            "integrity": "ok",
            "items": [method_assessment],
        },
        "claim_basis": {
            "schema_version": "claimtrace.claim-basis-projection/1",
            "items": [],
        },
        "semantics": {
            "integrity": "ok", "active_mapping_ids": [],
            "active_policy": {"configured": False},
        },
        "derivations": {
            "integrity": "ok",
            "items": [derivation],
            "active_proofs": [proof],
        },
        "findings": [finding],
        "fatal": None,
    }


def _report_with_deliberation():
    report = _report()
    report["report_schema_version"] = "1.8"
    candidate_id = "deliberation-candidate:sha256:" + "1" * 64
    proposal_id = "deliberation-proposal:sha256:" + "2" * 64
    candidate_set_id = "deliberation-set:sha256:" + "3" * 64
    ballot_id = "deliberation-ballot:sha256:" + "4" * 64
    proposal = {
        "schema_version": "claimtrace.deliberation-proposal/1",
        "record_type": "proposal", "proposal_id": proposal_id,
        "candidate_id": candidate_id, "round_id": "round:one",
        "phase": "claim_extraction", "subject_key": "claim:effect",
        "source_anchor": {
            "node_id": "data:source", "path": "data/source.csv",
            "start_byte": 0, "end_byte": 12,
            "span_sha256": "5" * 64, "source_sha256": "6" * 64,
            "source_size": 12, "excerpt": "effect is 0.4",
        },
        "payload": {
            "claim_text": "effect is 0.4", "claim_kind": "descriptive",
            "speech_act": "assertion", "polarity": "positive",
            "qualifiers": [],
        },
        "mechanical_validation": {"passed": True},
        "actor": {"id": "agent:one", "independence_group": "group:one"},
        "rationale": "An exact candidate, not an accepted claim.",
    }
    candidate_set = {
        "schema_version": "claimtrace.deliberation-candidate-set/1",
        "record_type": "candidate_set", "candidate_set_id": candidate_set_id,
        "round_id": "round:one", "phase": "claim_extraction",
        "subject_key": "claim:effect", "proposal_ids": [proposal_id],
        "candidate_ids": [candidate_id],
    }
    ballot = {
        "schema_version": "claimtrace.deliberation-ballot/1",
        "record_type": "ballot", "ballot_id": ballot_id,
        "candidate_set_id": candidate_set_id, "role": "adversarial_falsifier",
        "actor": {"id": "reviewer:one", "independence_group": "group:review"},
        "evaluations": [{
            "candidate_id": candidate_id, "decision": "reject",
            "reason_codes": ["MATERIAL_DISSENT"], "blocking": True,
            "rationale": "The scope is broader than the source bytes.",
        }],
    }
    report["deliberations"] = {
        "schema_version": "claimtrace.deliberation-status/1",
        "record_schemas": {
            "proposal": "claimtrace.deliberation-proposal/1",
            "candidate_set": "claimtrace.deliberation-candidate-set/1",
            "ballot": "claimtrace.deliberation-ballot/1",
            "phase_decision": "claimtrace.deliberation-phase-decision/1",
        },
        "integrity": "ok", "integrity_issues": [],
        "records": [proposal, candidate_set, ballot],
        "panels": [{
            "candidate_set_id": candidate_set_id, "round_id": "round:one",
            "phase": "claim_extraction", "subject_key": "claim:effect",
            "status": "contested", "recommended_candidate_id": None,
            "human_activation_required": True, "automatic_activation": False,
            "candidates": [{"candidate_id": candidate_id, "state": "contested"}],
        }],
        "open_groups": [], "human_activation_required": True,
        "automatic_activation": False, "scientific_truth_established": False,
    }
    return report


def _report_with_phase_decision():
    report = _report_with_deliberation()
    candidate_id = "deliberation-candidate:sha256:" + "1" * 64
    candidate_set_id = "deliberation-set:sha256:" + "3" * 64
    proposal = next(
        item for item in report["deliberations"]["records"]
        if item["record_type"] == "proposal"
    )
    candidate_set = next(
        item for item in report["deliberations"]["records"]
        if item["record_type"] == "candidate_set"
    )
    ballots = []
    for digit, role in zip("478", (
            "source_verifier", "coverage_reviewer", "adversarial_falsifier")):
        ballots.append({
            "schema_version": "claimtrace.deliberation-ballot/1",
            "record_type": "ballot",
            "ballot_id": "deliberation-ballot:sha256:" + digit * 64,
            "candidate_set_id": candidate_set_id,
            "role": role,
            "actor": {
                "id": f"reviewer:{role}",
                "independence_group": f"group:{role}",
            },
            "evaluations": [{
                "candidate_id": candidate_id,
                "decision": "endorse",
                "reason_codes": [],
                "blocking": False,
                "rationale": "Role-bound review for routing only.",
            }],
        })
    decision = {
        "schema_version": "claimtrace.deliberation-phase-decision/1",
        "record_type": "phase_decision",
        "decision_id": "deliberation-decision:sha256:" + "9" * 64,
        "recorded_at": "2026-07-18T01:10:00.000Z",
        "candidate_set_id": candidate_set_id,
        "candidate_id": candidate_id,
        "ballot_ids": sorted(item["ballot_id"] for item in ballots),
        "decision": "approved",
        "actor": "human:reviewer",
        "rationale": "Route this candidate to the next review phase.",
        "human_identity_authenticated": False,
        "automatic_activation": False,
    }
    report["deliberations"]["records"] = [
        proposal, candidate_set, *ballots, decision,
    ]
    report["deliberations"]["panels"][0].update({
        "status": "recommended_for_human_review",
        "recommended_candidate_id": candidate_id,
        "ballot_ids": decision["ballot_ids"],
        "candidates": [{
            "candidate_id": candidate_id,
            "state": "eligible_for_human_review",
        }],
    })
    return report


def test_projection_preserves_source_ids_states_and_semantic_boundaries():
    projection = build_projection(_report())

    assert projection["schema_version"] == PROJECTION_SCHEMA
    assert projection["projection_id"].startswith("graphrag:sha256:")
    nodes = {item["id"]: item for item in projection["nodes"]}
    assert nodes["claim:effect"]["record"]["id"] == "claim:effect"
    assert nodes["run:00000000-0000-0000-0000-000000000001"]["kind"] == "run"
    assessment = nodes["assessment:sha256:semantic"]
    assert assessment["state"]["effective_review_state"] == "accepted"
    assert assessment["state"]["active_relation"] == "related"
    assert assessment["state"]["stale"] is False
    method = nodes["method-assessment:sha256:fit"]
    assert method["state"]["effective_review_state"] == "contested"
    assert method["state"]["stale"] is True
    assert method["state"]["scientific_validity"] == "not_assessed"
    proof = nodes["proof:sha256:effect"]
    assert proof["state"]["proof_state"] == "derivable"
    assert "not scientific truth or support" in proof["meaning"]

    supports = [edge for edge in projection["edges"] if edge["rel"] == "supports"]
    assert len(supports) == 1
    assert supports[0]["kind"] == "semantic"
    assert supports[0]["state"]["declaration_state"] == "declared"
    assert supports[0]["state"]["semantic_review"]["status"] == (
        "assessed_not_as_written"
    )
    # The accepted narrower assessment remains a neutral assessment path.  It does
    # not become a second direct result-to-claim support edge.
    assert {
        edge["rel"] for edge in projection["edges"]
        if edge["kind"] == "semantic_assessment"
    } == {"assessment_compares_result", "assessment_evaluates_claim"}
    assert projection["unresolved_references"] == []


def test_projection_is_order_stable_and_findings_are_content_addressed():
    first_report = _report()
    second_report = copy.deepcopy(first_report)
    for path in (
        ("graph", "nodes"), ("graph", "edges"), ("receipts", "runs"),
        ("assessments", "items"), ("method_assessments", "items"),
        ("derivations", "items"), ("derivations", "active_proofs"),
        (None, "findings"),
    ):
        parent = second_report if path[0] is None else second_report[path[0]]
        parent[path[1]].reverse()

    first = build_projection(first_report)
    second = build_projection(second_report)
    assert first == second
    finding_ids = [
        item["id"] for item in first["nodes"] if item["kind"] == "finding"
    ]
    assert len(finding_ids) == 1
    assert finding_ids[0].startswith("finding:sha256:")


def test_projection_rejects_unsupported_schema_fatal_reports_and_id_collisions():
    report = _report()
    report["report_schema_version"] = "99.0"
    with pytest.raises(GraphRAGError, match="unsupported ProvSleuth report schema"):
        build_projection(report)

    report = _report()
    report["fatal"] = {"code": "GRAPH_ERROR", "detail": "broken graph"}
    with pytest.raises(GraphRAGError, match="fatal check report"):
        build_projection(report)

    report = _report()
    report["graph"]["nodes"].append({
        "id": "run:00000000-0000-0000-0000-000000000001",
        "type": "artifact", "status": "current",
    })
    with pytest.raises(GraphRAGError, match="node id collision"):
        build_projection(report)


def test_projection_preserves_adversarial_deliberation_without_activating_it():
    projection = build_projection(_report_with_deliberation())
    nodes = {item["id"]: item for item in projection["nodes"]}
    candidate_id = "deliberation-candidate:sha256:" + "1" * 64
    proposal_id = "deliberation-proposal:sha256:" + "2" * 64
    candidate_set_id = "deliberation-set:sha256:" + "3" * 64
    ballot_id = "deliberation-ballot:sha256:" + "4" * 64

    assert nodes[candidate_id]["kind"] == "deliberation_candidate"
    assert nodes[candidate_id]["state"]["candidate_state"] == "contested"
    assert nodes[candidate_id]["state"]["automatic_activation"] is False
    assert nodes[proposal_id]["kind"] == "deliberation_proposal"
    assert nodes[candidate_set_id]["kind"] == "deliberation_set"
    assert nodes[ballot_id]["kind"] == "deliberation_ballot"

    relations = {
        item["rel"] for item in projection["edges"]
        if item["kind"] == "deliberation"
    }
    assert relations == {
        "anchors_deliberation_proposal", "proposes_exact_candidate",
        "candidate_about_subject", "member_of_frozen_candidate_set",
        "ballot_for_candidate_set", "evaluates_candidate",
    }
    assert all(
        item["rel"] not in {"supports", "refutes"}
        for item in projection["edges"] if item["kind"] == "deliberation"
    )
    assert projection["deliberations"]["scientific_truth_established"] is False
    assert projection["source"]["integrity"]["deliberations"] == "ok"

    context = bounded_context(
        projection, candidate_id, max_hops=2, max_nodes=64, max_edges=128,
    )
    context_ids = {item["id"] for item in context["nodes"]}
    assert {candidate_id, proposal_id, candidate_set_id, ballot_id} <= context_ids
    dissent = next(
        item for item in context["edges"]
        if item["from"] == ballot_id and item["to"] == candidate_id
    )
    assert dissent["state"]["decision"] == "reject"
    assert dissent["state"]["blocking"] is True


def test_projection_rejects_deliberation_that_claims_automatic_authority():
    report = _report_with_deliberation()
    report["deliberations"]["automatic_activation"] = True
    with pytest.raises(GraphRAGError, match="overstates its authority"):
        build_projection(report)


def test_projection_preserves_phase_decision_as_advisory_routing_only():
    projection = build_projection(_report_with_phase_decision())
    nodes = {item["id"]: item for item in projection["nodes"]}
    decision_id = "deliberation-decision:sha256:" + "9" * 64
    candidate_id = "deliberation-candidate:sha256:" + "1" * 64
    candidate_set_id = "deliberation-set:sha256:" + "3" * 64
    ballot_ids = {
        "deliberation-ballot:sha256:" + digit * 64 for digit in "478"
    }

    decision = nodes[decision_id]
    assert decision["kind"] == "deliberation_phase_decision"
    assert decision["authority"] == "attributed_phase_routing_decision"
    assert decision["state"]["decision"] == "approved"
    assert decision["state"]["human_identity_authenticated"] is False
    assert decision["state"]["automatic_activation"] is False
    assert "not authenticated authorization" in decision["meaning"]
    candidate = nodes[candidate_id]
    assert candidate["authority"] == "attributed_grouped_deliberation_candidate"
    assert "do not authenticate independence" in candidate["meaning"]

    decision_edges = [
        item for item in projection["edges"] if item["from"] == decision_id
    ]
    assert {
        (item["rel"], item["to"]) for item in decision_edges
    } == {
        ("phase_decision_for_candidate_set", candidate_set_id),
        ("phase_decision_routes_candidate", candidate_id),
        *{("phase_decision_pins_ballot", ballot_id) for ballot_id in ballot_ids},
    }
    assert all(item["kind"] == "deliberation" for item in decision_edges)
    assert all(item["state"]["automatic_activation"] is False
               for item in decision_edges)
    assert not any(
        item["rel"] in {"supports", "refutes", "activates"}
        for item in decision_edges
    )
    context = bounded_context(
        projection, decision_id, max_hops=1, max_nodes=64, max_edges=128,
    )
    assert {candidate_id, candidate_set_id, *ballot_ids} <= {
        item["id"] for item in context["nodes"]
    }
    assert "self-asserted actor" in projection["coverage_notice"]
    assert "activates project state" in projection["coverage_notice"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("automatic_activation", True, "automatic activation"),
        ("human_identity_authenticated", True, "authenticated human identity"),
    ],
)
def test_projection_rejects_phase_decision_authority_overclaims(
        field, value, message):
    report = _report_with_phase_decision()
    decision = next(
        item for item in report["deliberations"]["records"]
        if item["record_type"] == "phase_decision"
    )
    decision[field] = value

    with pytest.raises(GraphRAGError, match=message):
        build_projection(report)


def test_bounded_context_is_deterministic_bounded_and_explains_omissions():
    projection = build_projection(_report())
    first = bounded_context(
        projection, ["missing:seed", "claim:effect"],
        max_hops=1, max_nodes=3, max_edges=2,
    )
    shuffled_report = _report()
    shuffled_report["graph"]["nodes"].reverse()
    shuffled_report["graph"]["edges"].reverse()
    shuffled = build_projection(shuffled_report)
    second = bounded_context(
        shuffled, ["claim:effect", "missing:seed"],
        max_hops=1, max_nodes=3, max_edges=2,
    )

    assert first == second
    assert first["schema_version"] == CONTEXT_SCHEMA
    assert first["request"]["direction"] == "both_preserving_source_edge_direction"
    assert first["request"]["unknown_seed_ids"] == ["missing:seed"]
    assert len(first["nodes"]) == 3
    assert len(first["edges"]) == 2
    assert first["inclusion"]["nodes"][0] == {
        "id": "claim:effect", "distance": 0, "reason": "seed",
        "from_id": None, "via_edge_id": None,
    }
    assert first["exclusion"]["unknown_seed_ids"] == ["missing:seed"]
    assert first["exclusion"]["node_limit_reached"] is True
    assert first["exclusion"]["nodes"]["omitted"] == 0
    assert sum(first["exclusion"]["node_counts_by_reason"].values()) == (
        len(projection["nodes"]) - 3
    )


def test_bounded_context_edge_budget_and_nontraversable_edges_are_explicit():
    projection = build_projection(_report())
    zero_edges = bounded_context(
        projection, "claim:effect", max_hops=2, max_nodes=20, max_edges=0,
    )
    assert [item["id"] for item in zero_edges["nodes"]] == ["claim:effect"]
    assert zero_edges["edges"] == []
    assert zero_edges["exclusion"]["edge_limit_reached"] is True
    assert zero_edges["exclusion"]["node_counts_by_reason"]["edge_budget"] > 0

    modified = copy.deepcopy(projection)
    target_edge = next(
        item for item in modified["edges"]
        if item["from"] == "artifact:result" and item["to"] == "claim:effect"
    )
    target_edge["traversable"] = False
    modified = _readdress_projection(modified)
    context = bounded_context(
        modified, "claim:effect", max_hops=1, max_nodes=20, max_edges=20,
    )
    assert context["exclusion"]["edge_counts_by_reason"]["non_traversable"] == 1
    assert target_edge["id"] not in {item["id"] for item in context["edges"]}


def test_bounded_context_refuses_to_drop_requested_seeds_or_accept_bad_limits():
    projection = build_projection(_report())
    with pytest.raises(GraphRAGError, match="none of the requested seed ids"):
        bounded_context(projection, "missing:seed")
    with pytest.raises(GraphRAGError, match="silently omit a requested seed"):
        bounded_context(
            projection, ["claim:effect", "artifact:result"], max_nodes=1,
        )
    with pytest.raises(GraphRAGError, match="max_hops"):
        bounded_context(projection, "claim:effect", max_hops=True)
    with pytest.raises(GraphRAGError, match="traversable_only"):
        bounded_context(projection, "claim:effect", traversable_only="yes")


def test_byte_budget_omits_whole_records_and_explains_every_omission():
    projection = build_projection(_report())
    budget = 8000
    context = bounded_context(
        projection, "claim:effect", max_hops=3, max_nodes=64, max_edges=128,
        max_bytes=budget,
    )

    assert len(_canonical_bytes(context)) <= budget
    assert context["request"]["max_bytes"] == budget
    assert context["exclusion"]["byte_limit_reached"] is True
    assert (
        context["exclusion"]["node_counts_by_reason"].get("byte_budget", 0)
        + context["exclusion"]["edge_counts_by_reason"].get("byte_budget", 0)
    ) > 0
    projection_nodes = {item["id"]: item for item in projection["nodes"]}
    projection_edges = {item["id"]: item for item in projection["edges"]}
    # Byte limiting never slices text, state, or raw records.  A graph record is
    # either byte-for-byte JSON-equivalent to the projection record or absent.
    assert all(item == projection_nodes[item["id"]] for item in context["nodes"])
    assert all(item == projection_edges[item["id"]] for item in context["edges"])
    assert context["exclusion"]["nodes"]["omitted"] >= 0
    assert context["exclusion"]["edges"]["omitted"] >= 0


def test_byte_budget_fails_when_complete_seed_record_cannot_fit():
    projection = build_projection(_report())
    with pytest.raises(GraphRAGError, match="seed records.*exceed max_bytes"):
        bounded_context(projection, "claim:effect", max_bytes=1)


@pytest.mark.parametrize("kwargs, expected", [
    ({"max_hops": MAX_CONTEXT_HOPS + 1}, "max_hops"),
    ({"max_nodes": MAX_CONTEXT_NODES + 1}, "max_nodes"),
    ({"max_edges": MAX_CONTEXT_EDGES + 1}, "max_edges"),
    ({"max_bytes": MAX_CONTEXT_BYTES + 1}, "max_bytes"),
])
def test_context_hard_caps_are_fail_closed(kwargs, expected):
    projection = build_projection(_report())
    assert DEFAULT_MAX_CONTEXT_BYTES <= MAX_CONTEXT_BYTES
    with pytest.raises(GraphRAGError, match=expected):
        bounded_context(projection, "claim:effect", **kwargs)


@pytest.mark.parametrize("field", ["declared_links", "required_dependencies"])
def test_projection_rejects_malformed_semantic_link_records(field):
    report = _report()
    report["assessments"][field] = ["malformed"]
    with pytest.raises(GraphRAGError, match=field):
        build_projection(report)


@pytest.mark.parametrize("mutation, expected", [
    ("bindings_not_list", "bindings must be a list"),
    ("assessment_result_ids_not_list", "result_ids must be a list"),
    ("derivation_result_ids_not_list", "result_ids must be a list"),
    ("proof_derivation_ids_not_list", "derivation_ids must be a list"),
])
def test_projection_rejects_other_malformed_iterated_records(mutation, expected):
    report = _report()
    if mutation == "bindings_not_list":
        report["receipts"]["runs"][0]["bindings"] = {}
    elif mutation == "assessment_result_ids_not_list":
        report["assessments"]["items"][0]["subject"]["result_ids"] = "artifact:result"
    elif mutation == "derivation_result_ids_not_list":
        report["derivations"]["items"][0]["subject"]["result_ids"] = "artifact:result"
    else:
        report["derivations"]["active_proofs"][0]["derivation_ids"] = (
            "derivation:sha256:effect"
        )
    with pytest.raises(GraphRAGError, match=expected):
        build_projection(report)


def test_context_id_commits_projection_state_and_tampering_fails_closed():
    first_projection = build_projection(_report())
    first = bounded_context(first_projection, "claim:effect")

    changed_report = _report()
    changed_report["assessments"]["items"][0]["agent_input"]["rationale"] = (
        "A changed attributed rationale."
    )
    second_projection = build_projection(changed_report)
    second = bounded_context(second_projection, "claim:effect")
    assert first_projection["projection_id"] != second_projection["projection_id"]
    assert first["context_id"] != second["context_id"]
    assert second["source_projection_id"] == second_projection["projection_id"]
    first_core = copy.deepcopy(first)
    first_core.pop("context_id")
    expected_context_id = "graphrag-context:sha256:" + hashlib.sha256(
        _canonical_bytes(first_core)
    ).hexdigest()
    assert first["context_id"] == expected_context_id

    tampered = copy.deepcopy(first_projection)
    tampered["nodes"][0]["text"] = "tampered"
    with pytest.raises(GraphRAGError, match="projection_id"):
        bounded_context(tampered, "claim:effect")


def test_context_refuses_unresolved_projection_by_default():
    report = _report()
    report["receipts"]["runs"][0]["bindings"].append({
        "binding_kind": "output_path",
        "node_id": "artifact:missing",
        "path": "results/missing.json",
        "current": True,
    })
    projection = build_projection(report)
    assert projection["unresolved_references"]
    with pytest.raises(GraphRAGError, match="unresolved references"):
        bounded_context(projection, "claim:effect")
    diagnostic = bounded_context(
        projection, "claim:effect", allow_unresolved=True,
    )
    assert diagnostic["request"]["allow_unresolved"] is True


def test_graphrag_cli_exports_projection_and_bounded_context(tmp_path):
    assert main(["init", str(tmp_path), "--example"]) == 0
    config = tmp_path / "provsleuth.config.json"
    assert main([
        "--config", str(config), "graphrag-export",
        "--output", "graphrag.json",
    ]) == 0
    projection = json.loads((tmp_path / "graphrag.json").read_text(encoding="utf-8"))
    assert projection["projection_id"].startswith("graphrag:sha256:")

    assert main([
        "--config", str(config), "graphrag-context", "data:example",
        "--max-hops", "1", "--output", "context.json",
    ]) == 0
    context = json.loads((tmp_path / "context.json").read_text(encoding="utf-8"))
    assert context["request"]["known_seed_ids"] == ["data:example"]
    assert context["source_projection_id"] == projection["projection_id"]
