"""Deterministic standalone research-trajectory visualization.

The graph remains the semantic declaration of lineage. Run receipts are rendered as a
separate mechanical layer and are linked only to outputs that the report explicitly binds.
No receipt is presented as proof of observed reads or write causation.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from collections import defaultdict
from pathlib import Path

from .engine import ANNOT_RELS, GraphError
from .report import build_report

NODE_WIDTH = 224
NODE_HEIGHT = 78
LAYER_GAP = 116
ROW_GAP = 28
MARGIN_X = 44
MARGIN_TOP = 64


def _json_for_html(value):
    """Serialize JSON without allowing data to terminate its script container."""
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    return (
        encoded.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _semantic_order(graph):
    nodes = {item["id"]: item for item in graph.get("nodes", [])}
    preferred = [item for item in graph.get("trajectory_order", []) if item in nodes]
    return preferred + sorted(set(nodes) - set(preferred))


def _semantic_layers(graph, order):
    """Assign longest-path layers using declared dependency edges only."""
    incoming = defaultdict(list)
    for edge in graph.get("edges", []):
        if edge.get("rel") not in ANNOT_RELS:
            source, target = _dependency_direction(edge)
            if not isinstance(source, str) or not isinstance(target, str):
                continue
            incoming[target].append(source)
    layers = {}
    for node_id in order:
        predecessors = [layers[source] for source in sorted(incoming[node_id])
                        if source in layers]
        layers[node_id] = max(predecessors, default=-1) + 1
    return layers


def _dependency_direction(edge):
    """Match the engine's dependency orientation; reads is stored dependent-to-input."""
    if edge.get("rel") == "reads":
        return edge.get("to"), edge.get("from")
    return edge.get("from"), edge.get("to")


def _coverage_label(run):
    coverage = run.get("lineage_coverage")
    if isinstance(coverage, dict):
        reads = coverage.get("reads")
        writes = coverage.get("writes")
        if reads or writes:
            materialized = (
                " Materialized intermediate files are post-process boundary snapshots, "
                "not stage-attributed writes."
                if run.get("declared_intermediates") else ""
            )
            return (
                "Partial: inputs are declared, not observed reads; "
                "pre/post changes do not prove write causation." + materialized
            )
    return (
        "Partial: inputs are declared, not observed reads; "
        "pre/post changes do not prove write causation."
    )


ASSESSMENT_DIMENSIONS = (
    "population", "exposure", "comparator", "outcome", "direction", "magnitude",
    "time_scope", "inference_level",
)


def _assessment_view_record(item, raw_nodes, *, allow_active_relations=True):
    """Return presentation data without re-deriving any assessment semantics."""
    subject = item.get("subject") or {}
    mechanical = item.get("mechanical_snapshot") or {}
    agent_input = item.get("agent_input") or {}
    review = item.get("review") or {}
    current = item.get("current_derived") or {}
    claim_id = subject.get("claim_id")
    result_ids = list(subject.get("result_ids") or [])
    claim_node = raw_nodes.get(claim_id, {})
    claim_snapshot = mechanical.get("claim") or {}
    result_snapshots = {
        snapshot.get("node_id"): snapshot
        for snapshot in mechanical.get("results") or []
        if isinstance(snapshot, dict)
    }
    anchor_checks = {
        check.get("anchor_index"): check
        for check in mechanical.get("anchor_checks") or []
        if isinstance(check, dict)
    }
    claim_frame = agent_input.get("claim_frame") or {}
    result_frame = agent_input.get("result_frame") or {}
    alignment = agent_input.get("alignment") or {}
    results = []
    for result_id in result_ids:
        result_node = raw_nodes.get(result_id, {})
        snapshot = result_snapshots.get(result_id, {})
        results.append({
            "id": result_id,
            "text": result_node.get("value") or result_node.get("path") or result_id,
            "path": result_node.get("path"),
            "node_version_id": snapshot.get("node_version_id"),
            "artifact": snapshot.get("artifact") or {},
        })
    anchors = []
    for index, anchor in enumerate(agent_input.get("evidence_anchors") or []):
        if not isinstance(anchor, dict):
            continue
        anchors.append({
            **anchor,
            "check": anchor_checks.get(index) or {},
            "artifact": (result_snapshots.get(anchor.get("result_id"), {})
                         .get("artifact") or {}),
        })
    return {
        "id": item.get("id"),
        "schema_version": item.get("schema_version"),
        "recorded_at": item.get("recorded_at"),
        "subject": {"claim_id": claim_id, "result_ids": result_ids},
        "claim": {
            "id": claim_id,
            "text": claim_node.get("value") or claim_id,
            "node_version_id": claim_snapshot.get("node_version_id"),
            "frame": claim_frame,
        },
        "results": results,
        "result_frame": result_frame,
        "alignment": [
            {
                "dimension": dimension,
                "claim": claim_frame.get(dimension),
                "result": result_frame.get(dimension),
                "state": alignment.get(dimension),
            }
            for dimension in ASSESSMENT_DIMENSIONS
        ],
        "verdict": agent_input.get("verdict"),
        "rationale": agent_input.get("rationale"),
        "recommended_claim": agent_input.get("recommended_claim"),
        "limitations": list(agent_input.get("limitations") or []),
        "evidence_anchors": anchors,
        "provenance": agent_input.get("provenance") or {},
        "review": review,
        "review_state": review.get("state"),
        "current_state": current.get("effective_review_state"),
        "proposed_relation": current.get("proposed_relation"),
        "active_relation": (
            current.get("active_relation") if allow_active_relations else None
        ),
        "integrity_blocked": not allow_active_relations,
        "stale": bool(current.get("stale")),
        "findings": list(current.get("findings") or []),
        "is_current": bool(item.get("is_current")),
    }


def _derivation_view_record(item, raw_nodes, *, allow_active_proofs=True):
    """Return a presentation-only projection of one composite symbolic proof."""
    subject = item.get("subject") or {}
    stored = item.get("stored_derived") or item.get("derived") or {}
    effective = item.get("effective") or stored
    mechanical = item.get("mechanical_snapshot") or {}
    claim_id = subject.get("claim_id")
    result_ids = list(subject.get("result_ids") or [])
    claim_snapshot = mechanical.get("claim") or {}
    evidence_plan = claim_snapshot.get("evidence_plan")
    result_snapshots = {
        snapshot.get("node_id"): snapshot
        for snapshot in mechanical.get("results") or []
        if isinstance(snapshot, dict)
    }
    vocabulary = mechanical.get("vocabulary") or {}
    rule_pack = mechanical.get("rule_pack") or {}
    effective_active = bool(effective.get("active")) and allow_active_proofs
    return {
        "id": item.get("id"),
        "proof_id": stored.get("proof_id") or effective.get("proof_id"),
        "stored_proof_id": stored.get("proof_id"),
        "current_evaluation_proof_id": effective.get("proof_id"),
        "recorded_at": item.get("recorded_at"),
        "actor": item.get("actor"),
        "subject": {"claim_id": claim_id, "result_ids": result_ids},
        "claim": {
            "id": claim_id,
            "text": (raw_nodes.get(claim_id) or {}).get("value") or claim_id,
            "node_version_id": claim_snapshot.get("node_version_id"),
        },
        # This is the plan captured in the proof certificate, not a fresh projection
        # from the current graph.  Keeping that distinction visible is important when
        # a later claim edit makes the proof stale.
        "evidence_plan": (
            copy.deepcopy(evidence_plan) if isinstance(evidence_plan, dict) else None
        ),
        "results": [
            {
                "id": result_id,
                "text": ((raw_nodes.get(result_id) or {}).get("value")
                         or (raw_nodes.get(result_id) or {}).get("path") or result_id),
                "node_version_id": (result_snapshots.get(result_id) or {}).get(
                    "node_version_id"
                ),
                "artifact": (result_snapshots.get(result_id) or {}).get("artifact") or {},
            }
            for result_id in result_ids
        ],
        "used_result_ids": list(effective.get("used_result_ids") or []),
        "target": effective.get("target") or stored.get("target"),
        "rendered_target": (
            effective.get("rendered_target") or stored.get("rendered_target") or claim_id
        ),
        "outcome_relation": (
            effective.get("outcome_relation") or stored.get("outcome_relation")
            or "undetermined"
        ),
        "outcome_atoms": list(
            effective.get("outcome_atoms") or stored.get("outcome_atoms") or []
        ),
        "rendered_outcomes": list(
            effective.get("rendered_outcomes") or stored.get("rendered_outcomes") or []
        ),
        "proof_state": effective.get("proof_state") or stored.get("proof_state") or "unknown",
        "stored_proof_state": stored.get("proof_state") or "unknown",
        "claim_bound": bool(effective.get("claim_bound")),
        "active": effective_active,
        "stale": bool(effective.get("stale")),
        "drift": list(effective.get("drift") or []),
        "integrity_blocked": not allow_active_proofs,
        "assumptions": list(effective.get("assumptions") or stored.get("assumptions") or []),
        "proof_steps": list(effective.get("proof_steps") or stored.get("proof_steps") or []),
        "findings": list(effective.get("findings") or []),
        "vocabulary": {
            "id": subject.get("vocabulary_id") or vocabulary.get("id"),
            "version": vocabulary.get("version"),
            "sha256": vocabulary.get("sha256"),
        },
        "rule_pack": {
            "id": subject.get("rule_pack_id") or rule_pack.get("id"),
            "version": rule_pack.get("version"),
            "sha256": rule_pack.get("sha256"),
        },
        "provenance": (item.get("agent_input") or {}).get("provenance") or {},
    }


def _build_payload(report, layout_id):
    graph = report.get("graph") or {}
    deliberation_projection = report.get("deliberations") or {}
    deliberation_records = sorted(
        (copy.deepcopy(item) for item in deliberation_projection.get("records") or []),
        key=lambda item: (
            str(item.get("record_type")),
            str(item.get("proposal_id") or item.get("candidate_set_id")
                or item.get("ballot_id") or item.get("decision_id")),
        ),
    )
    deliberations = {
        "schema_version": deliberation_projection.get("schema_version"),
        "integrity": deliberation_projection.get("integrity", "unknown"),
        "integrity_issues": sorted(
            (copy.deepcopy(item)
             for item in deliberation_projection.get("integrity_issues") or []),
            key=lambda item: (
                str(item.get("code")), str(item.get("path")),
                str(item.get("detail")),
            ),
        ),
        "proposals": [
            item for item in deliberation_records if item.get("record_type") == "proposal"
        ],
        "candidate_sets": [
            item for item in deliberation_records
            if item.get("record_type") == "candidate_set"
        ],
        "ballots": [
            item for item in deliberation_records if item.get("record_type") == "ballot"
        ],
        "phase_decisions": [
            item for item in deliberation_records
            if item.get("record_type") == "phase_decision"
        ],
        "panels": sorted(
            (copy.deepcopy(item) for item in deliberation_projection.get("panels") or []),
            key=lambda item: str(item.get("candidate_set_id")),
        ),
        "open_groups": sorted(
            (copy.deepcopy(item)
             for item in deliberation_projection.get("open_groups") or []),
            key=lambda item: (
                str(item.get("round_id")), str(item.get("phase")),
                str(item.get("subject_key")),
            ),
        ),
        # These are view-owned safety labels, not projections that callers may override.
        "advisory_only": True,
        "human_activation_required": True,
        "automatic_activation": False,
        "scientific_truth_established": False,
    }
    raw_nodes = {item["id"]: item for item in graph.get("nodes", [])}
    order = _semantic_order(graph)
    # Reserve an integer slot between ordinary dependency layers for review nodes.
    semantic_layers = {
        node_id: layer * 2 for node_id, layer in _semantic_layers(graph, order).items()
    }
    order_rank = {node_id: index for index, node_id in enumerate(order)}
    findings_by_node = defaultdict(list)
    global_findings = []
    for finding in report.get("findings", []):
        node_id = finding.get("node_id")
        if node_id in raw_nodes:
            findings_by_node[node_id].append({
                "severity": finding.get("severity"),
                "code": finding.get("code"),
                "detail": finding.get("detail"),
                "blocking": bool(finding.get("blocking")),
            })
        else:
            global_findings.append({
                "severity": finding.get("severity"),
                "code": finding.get("code"),
                "node_id": node_id,
                "detail": finding.get("detail"),
                "blocking": bool(finding.get("blocking")),
            })

    runs = list((report.get("receipts") or {}).get("runs", []))
    run_bindings = {}
    node_runs = defaultdict(list)
    for run in runs:
        run_id = run.get("run_id")
        bindings = sorted(
            {
                item.get("node_id")
                for item in run.get("bindings", [])
                if item.get("node_id") in raw_nodes
            }
        )
        run_bindings[run_id] = bindings
        for node_id in bindings:
            node_runs[node_id].append(run_id)

    claim_basis_records = list((report.get("claim_basis") or {}).get("items") or [])
    claim_basis_by_node = defaultdict(list)
    for item in claim_basis_records:
        for node_id in (item.get("result_id"), item.get("claim_id")):
            if node_id in raw_nodes:
                claim_basis_by_node[node_id].append(copy.deepcopy(item))
    for node_id in claim_basis_by_node:
        claim_basis_by_node[node_id].sort(key=lambda item: (
            str(item.get("claim_id")), str(item.get("result_id")),
            str(item.get("assessment_id")),
        ))

    method_projection = report.get("method_assessments") or {}
    method_assessments_by_node = defaultdict(list)
    for item in method_projection.get("items") or []:
        method_id = (item.get("subject") or {}).get("method_id")
        if method_id in raw_nodes:
            method_assessments_by_node[method_id].append(copy.deepcopy(item))
    for node_id in method_assessments_by_node:
        method_assessments_by_node[node_id].sort(key=lambda item: str(item.get("id")))

    if any(
        semantic_layers.get(node_id) == 0
        for bindings in run_bindings.values()
        for node_id in bindings
    ):
        semantic_layers = {key: value + 2 for key, value in semantic_layers.items()}

    assessment_projection = report.get("assessments") or {}
    assessment_integrity = assessment_projection.get("integrity")
    allow_active_relations = assessment_integrity == "ok"
    assessment_records = [
        _assessment_view_record(
            item,
            raw_nodes,
            allow_active_relations=allow_active_relations,
        )
        for item in sorted(
            assessment_projection.get("items") or [],
            key=lambda item: str(item.get("id")),
        )
    ]
    assessments_by_id = {
        item["id"]: item for item in assessment_records if isinstance(item.get("id"), str)
    }
    assessments_by_node = defaultdict(list)
    current_assessments_by_node = defaultdict(list)
    for item in assessment_records:
        assessment_id = item.get("id")
        subject = item.get("subject") or {}
        for node_id in [subject.get("claim_id"), *(subject.get("result_ids") or [])]:
            if node_id in raw_nodes and assessment_id:
                assessments_by_node[node_id].append(assessment_id)
                if item.get("is_current"):
                    current_assessments_by_node[node_id].append(assessment_id)
    for node_id in assessments_by_node:
        assessments_by_node[node_id].sort()
        current_assessments_by_node[node_id].sort()

    semantic_projection = report.get("semantics") or {}
    active_semantic = semantic_projection.get("active_policy") or {}
    active_semantic_evaluation = active_semantic.get("evaluation") or {}
    mapping_history = {
        item.get("id"): item
        for item in semantic_projection.get("mapping_history") or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    active_semantic_mapping_ids = list(
        semantic_projection.get("active_mapping_ids") or []
    )
    active_semantic_mapping_set = set(active_semantic_mapping_ids)
    semantic_mapping_records = []
    for item in semantic_projection.get("current_mapping_evaluations") or []:
        history = mapping_history.get(item.get("mapping_id")) or {}
        agent_input = history.get("agent_input") or {}
        mechanical = history.get("mechanical_snapshot") or {}
        local_term = (mechanical.get("local_term") or {}).get("term") or {}
        semantic_mapping_records.append({
            "mapping_id": item.get("mapping_id"),
            "subject": copy.deepcopy(item.get("subject") or {}),
            "local_term": copy.deepcopy(local_term),
            "relation": agent_input.get("relation"),
            "target": copy.deepcopy(agent_input.get("target")),
            "selected_candidate": copy.deepcopy(mechanical.get("selected_candidate")),
            "rationale": agent_input.get("rationale"),
            "limitations": copy.deepcopy(agent_input.get("limitations") or []),
            "provenance": copy.deepcopy(agent_input.get("provenance") or {}),
            "review": copy.deepcopy(item.get("review") or {}),
            "current_derived": copy.deepcopy(item.get("current_derived") or {}),
            "active": item.get("mapping_id") in active_semantic_mapping_set,
        })
    semantic_policy = {
        "integrity": semantic_projection.get("integrity", "unknown"),
        "require_active_policy": bool(
            (semantic_projection.get("policy") or {}).get("require_active_policy")
        ),
        "current_mappings": semantic_mapping_records,
        "release_count": len(semantic_projection.get("policies") or []),
        "active_policy_configured": bool(active_semantic.get("configured")),
        "configured_policy_id": active_semantic.get("configured_policy_id"),
        "active_policy_active": bool(active_semantic_evaluation.get("active")),
        "active_policy_valid": bool(active_semantic_evaluation.get("valid")),
        "active_policy_findings": copy.deepcopy(active_semantic.get("findings") or []),
        "active_policy_id": (
            (active_semantic.get("policy") or {}).get("id")
            if active_semantic_evaluation.get("active") else None
        ),
        "active_mapping_ids": active_semantic_mapping_ids,
    }

    derivation_projection = report.get("derivations") or {}
    derivation_integrity = derivation_projection.get("integrity")
    allow_active_proofs = derivation_integrity == "ok"
    derivation_history_records = [
        _derivation_view_record(
            item,
            raw_nodes,
            allow_active_proofs=allow_active_proofs,
        )
        for item in sorted(
            derivation_projection.get("items") or [],
            key=lambda item: str(item.get("id")),
        )
    ]
    active_by_proof = {
        item["proof_id"]: item
        for item in derivation_projection.get("active_proofs") or []
    }
    grouped_records = {}
    for item in derivation_history_records:
        active_summary = active_by_proof.get(item.get("proof_id"))
        key = (
            ("proof", item["proof_id"])
            if active_summary is not None else ("derivation", item["id"])
        )
        if key in grouped_records:
            continue
        projected = copy.deepcopy(item)
        projected["derivation_ids"] = (
            list(active_summary["derivation_ids"])
            if active_summary is not None else [item["id"]]
        )
        projected["conditional_active"] = bool(item.get("active"))
        projected["claim_level_active"] = (
            bool(active_summary["claim_level_active"])
            if active_summary is not None else False
        )
        projected["active"] = (
            projected["conditional_active"] and projected["claim_level_active"]
        )
        projected["execution_basis"] = (
            copy.deepcopy(active_summary.get("execution_basis"))
            if active_summary is not None else None
        )
        grouped_records[key] = projected
    derivation_records = list(grouped_records.values())

    conflict_members = defaultdict(list)
    for item in derivation_records:
        if item.get("conditional_active") and not item.get("claim_level_active"):
            key = (
                (item.get("subject") or {}).get("claim_id"),
                (item.get("vocabulary") or {}).get("id"),
                (item.get("rule_pack") or {}).get("id"),
                json.dumps(item.get("target"), sort_keys=True, separators=(",", ":")),
            )
            conflict_members[key].append(item)
    conflict_member_ids = {
        item["id"] for members in conflict_members.values() for item in members
        if {value.get("proof_state") for value in members} >= {"derivable", "refutable"}
    }
    conflict_records = []
    for key, members in sorted(conflict_members.items(), key=lambda item: item[0]):
        if {item.get("proof_state") for item in members} < {"derivable", "refutable"}:
            continue
        proof_ids = sorted(item["proof_id"] for item in members)
        digest = hashlib.sha256("\n".join(proof_ids).encode("utf-8")).hexdigest()
        first = members[0]
        results_by_id = {
            result["id"]: result for item in members for result in item.get("results") or []
        }
        conflict_records.append({
            **copy.deepcopy(first),
            "id": "cross-conflict:sha256:" + digest,
            "proof_id": "cross-conflict:sha256:" + digest,
            "stored_proof_id": None,
            "current_evaluation_proof_id": None,
            "proof_ids": proof_ids,
            "derivation_ids": sorted({
                derivation_id for item in members
                for derivation_id in item.get("derivation_ids") or []
            }),
            "subject": {
                "claim_id": key[0],
                "result_ids": sorted(results_by_id),
            },
            "results": [results_by_id[result_id] for result_id in sorted(results_by_id)],
            "used_result_ids": sorted({
                result_id for item in members
                for result_id in item.get("used_result_ids") or []
            }),
            "proof_state": "cross_conflict",
            "stored_proof_state": "cross_conflict",
            "outcome_relation": "target_and_opposite_active_across_derivations",
            "outcome_atoms": [
                atom for item in members for atom in item.get("outcome_atoms") or []
            ],
            "rendered_outcomes": sorted({
                value for item in members for value in item.get("rendered_outcomes") or []
                if value
            }),
            "active": False,
            "conditional_active": False,
            "claim_level_active": False,
            "assumptions": [],
            "proof_steps": [],
            "conditional_proofs": [
                {
                    "proof_id": item["proof_id"],
                    "proof_state": item["proof_state"],
                    "derivation_ids": item["derivation_ids"],
                    "used_result_ids": item["used_result_ids"],
                    "rendered_outcomes": item["rendered_outcomes"],
                }
                for item in sorted(members, key=lambda value: value["proof_id"])
            ],
            "findings": [{
                "severity": "error", "code": "SYMBOLIC_CROSS_DERIVATION_CONFLICT",
                "detail": (
                    "separate active conditional proofs derive the target and its opposite"
                ),
            }],
        })
    derivation_records = [
        item for item in derivation_records if item["id"] not in conflict_member_ids
    ] + conflict_records
    derivation_records.sort(key=lambda item: item["id"])
    derivations_by_id = {
        item["id"]: item for item in derivation_history_records
        if isinstance(item.get("id"), str)
    }
    derivations_by_node = defaultdict(list)
    for item in derivation_history_records:
        derivation_id = item.get("id")
        subject = item.get("subject") or {}
        for node_id in [subject.get("claim_id"), *(subject.get("result_ids") or [])]:
            if node_id in raw_nodes and derivation_id:
                derivations_by_node[node_id].append(derivation_id)
    for node_id in derivations_by_node:
        derivations_by_node[node_id].sort()

    declared_link_records = {
        (item.get("from"), item.get("to"), item.get("declared_relation")): item
        for item in assessment_projection.get("declared_links") or []
    }
    required_dependency_records = {
        (item.get("from"), item.get("to"), item.get("declared_relation")): item
        for item in assessment_projection.get("required_dependencies") or []
    }
    claim_links_by_node = defaultdict(list)
    for edge in graph.get("edges", []):
        if edge.get("rel") not in {"supports", "refutes"}:
            continue
        key = (edge.get("from"), edge.get("to"), edge.get("rel"))
        record = declared_link_records.get(key) or {
            "from": key[0], "to": key[1], "declared_relation": key[2],
            "status": "unassessed", "assessment_ids": [], "assessed_relations": [],
        }
        link = copy.deepcopy(record)
        for node_id in key[:2]:
            if node_id in raw_nodes:
                claim_links_by_node[node_id].append(link)
    for record in required_dependency_records.values():
        link = copy.deepcopy(record)
        for node_id in (record.get("from"), record.get("to")):
            if node_id in raw_nodes:
                claim_links_by_node[node_id].append(link)

    visual_nodes = []
    for node_id in order:
        node = raw_nodes[node_id]
        visual_nodes.append({
            "key": "graph:" + node_id,
            "node_id": node_id,
            "kind": "semantic",
            "type": str(node.get("type", "unknown")),
            "status": str(node.get("status", "unspecified")),
            "label": node_id,
            "path": node.get("path"),
            "value": node.get("value"),
            "note": node.get("note"),
            "date": node.get("date"),
            "script": node.get("script"),
            "backbone": node.get("backbone"),
            "evidence_plan": (
                copy.deepcopy(node.get("logic_evidence_plan"))
                if isinstance(node.get("logic_evidence_plan"), dict) else None
            ),
            "findings": sorted(
                findings_by_node[node_id],
                key=lambda item: (
                    str(item.get("severity")), str(item.get("code")),
                    str(item.get("detail")),
                ),
            ),
            "run_ids": sorted(node_runs[node_id]),
            "assessment_ids": assessments_by_node[node_id],
            "default_assessment_id": (
                current_assessments_by_node[node_id][0]
                if current_assessments_by_node[node_id]
                else (assessments_by_node[node_id][0] if assessments_by_node[node_id] else None)
            ),
            "derivation_ids": derivations_by_node[node_id],
            "claim_links": sorted(
                claim_links_by_node[node_id],
                key=lambda item: (
                    str(item.get("from")), str(item.get("to")),
                    str(item.get("declared_relation")),
                ),
            ),
            "claim_basis": claim_basis_by_node[node_id],
            "method_assessments": method_assessments_by_node[node_id],
            "coverage": None,
            "layer": semantic_layers.get(node_id, 0),
            "_rank": order_rank[node_id],
        })

    current_assessments = [item for item in assessment_records if item.get("is_current")]
    fallback_review_layer = max(semantic_layers.values(), default=-1) + 1
    assessment_node_keys = {}
    for assessment_rank, item in enumerate(current_assessments):
        assessment_id = item["id"]
        subject = item.get("subject") or {}
        result_layers = [
            semantic_layers[result_id]
            for result_id in subject.get("result_ids") or []
            if result_id in semantic_layers
        ]
        claim_layer = semantic_layers.get(subject.get("claim_id"))
        endpoints = result_layers + ([claim_layer] if claim_layer is not None else [])
        if endpoints and min(endpoints) < max(endpoints):
            layer = min(endpoints) + (max(endpoints) - min(endpoints)) // 2
        else:
            layer = fallback_review_layer
        key = "review:" + assessment_id
        assessment_node_keys[assessment_id] = key
        state = str(item.get("current_state") or item.get("review_state") or "proposed")
        verdict = str(item.get("verdict") or "unclassified")
        visual_nodes.append({
            "key": key,
            "node_id": assessment_id,
            "kind": "assessment",
            "type": "semantic assessment",
            "status": state,
            "label": "Review: " + verdict.replace("_", " "),
            "path": None,
            "value": item.get("rationale"),
            "note": item.get("recommended_claim"),
            "date": item.get("recorded_at"),
            "script": None,
            "backbone": None,
            "findings": list(item.get("findings") or []),
            "run_ids": [],
            "assessment_ids": [assessment_id],
            "default_assessment_id": assessment_id,
            "derivation_ids": [],
            "claim_links": [],
            "claim_basis": [],
            "method_assessments": [],
            "coverage": None,
            "layer": layer,
            "_rank": assessment_rank,
        })

    proof_node_keys = {}
    fallback_proof_layer = max(semantic_layers.values(), default=-1) + 1
    for proof_rank, item in enumerate(derivation_records):
        derivation_id = item["id"]
        subject = item.get("subject") or {}
        result_layers = [
            semantic_layers[result_id]
            for result_id in subject.get("result_ids") or []
            if result_id in semantic_layers
        ]
        claim_layer = semantic_layers.get(subject.get("claim_id"))
        endpoints = result_layers + ([claim_layer] if claim_layer is not None else [])
        if endpoints and min(endpoints) < max(endpoints):
            layer = min(endpoints) + (max(endpoints) - min(endpoints)) // 2
        else:
            layer = fallback_proof_layer
        key = "proof:" + derivation_id
        proof_node_keys[derivation_id] = key
        state = str(item.get("proof_state") or "unknown")
        if state == "cross_conflict":
            display_status = "claim_conflict"
        elif item.get("integrity_blocked"):
            display_status = "integrity_error"
        elif item.get("stale"):
            display_status = "stale"
        elif item.get("active"):
            display_status = state
        else:
            display_status = "inactive_" + state
        visual_nodes.append({
            "key": key,
            "node_id": derivation_id,
            "kind": "proof",
            "type": "symbolic conflict" if state == "cross_conflict" else "symbolic proof",
            "status": display_status,
            "label": "Formal: " + display_status.replace("_", " "),
            "path": None,
            "value": (
                "; ".join(value for value in item.get("rendered_outcomes") or [] if value)
                or item.get("rendered_target")
            ),
            "note": (
                "Conflicting active conditional outcomes" if state == "cross_conflict"
                else "Active formal outcome under the configured rule pack" if item.get("active")
                else "Inspect proof state and findings"
            ),
            "date": item.get("recorded_at"),
            "script": None,
            "backbone": None,
            "findings": list(item.get("findings") or []),
            "run_ids": [],
            "assessment_ids": [],
            "default_assessment_id": None,
            "derivation_ids": list(item.get("derivation_ids") or [derivation_id]),
            "claim_links": [],
            "claim_basis": [],
            "method_assessments": [],
            "coverage": None,
            "proof": item,
            "layer": layer,
            "_rank": proof_rank,
        })

    max_semantic_layer = max(semantic_layers.values(), default=-1)
    for run_rank, run in enumerate(runs):
        run_id = str(run.get("run_id"))
        bindings = run_bindings.get(run.get("run_id"), [])
        target_layers = [semantic_layers[node_id] for node_id in bindings]
        layer = max(0, min(target_layers) - 1) if target_layers else max_semantic_layer + 1
        output_transitions = []
        for transition in run.get("output_transitions", []):
            output_transitions.append({
                "path": transition.get("path"),
                "transition": transition.get("transition"),
                "produced": bool(transition.get("produced")),
            })
        intermediate_transitions = []
        for transition in run.get("intermediate_transitions", []):
            intermediate_transitions.append({
                "path": transition.get("path"),
                "transition": transition.get("transition"),
                "produced": bool(transition.get("produced")),
                "before": copy.deepcopy(transition.get("before")),
                "after": copy.deepcopy(transition.get("after")),
            })
        visual_nodes.append({
            "key": "receipt:" + run_id,
            "node_id": run_id,
            "kind": "run",
            "type": "run receipt",
            "status": (
                str(run.get("outcome") or "incomplete")
                if run.get("evidence_eligible", True) else "integrity_error"
            ),
            "label": str(run.get("name") or run_id),
            "path": run.get("cwd"),
            "value": None,
            "note": None,
            "date": run.get("finished_at") or run.get("started_at"),
            "script": None,
            "backbone": None,
            "findings": [],
            "run_ids": [],
            "assessment_ids": [],
            "default_assessment_id": None,
            "derivation_ids": [],
            "claim_links": [],
            "claim_basis": [],
            "method_assessments": [],
            "coverage": _coverage_label(run),
            "event_schema_version": run.get("event_schema_version"),
            "computation_id": run.get("computation_id"),
            "pipeline_contract_id": run.get("pipeline_contract_id"),
            "pipeline_contract_state": run.get("pipeline_contract_state"),
            "current_gate_role": run.get("current_gate_role"),
            "replacement_run_id": run.get("replacement_run_id"),
            "replacement_replay_ids": list(
                run.get("replacement_replay_ids") or []
            ),
            "stage_trace_plan": copy.deepcopy(run.get("stage_trace_plan")),
            "stage_trace": copy.deepcopy(run.get("stage_trace")),
            "event_store_integrity": run.get("event_store_integrity"),
            "link_integrity": run.get("link_integrity"),
            "link_issues": copy.deepcopy(run.get("link_issues") or []),
            "evidence_eligible": run.get("evidence_eligible"),
            "replay_conflict": bool(run.get("replay_conflict")),
            "replay_conflict_certificate_ids": list(
                run.get("replay_conflict_certificate_ids") or []
            ),
            "pipeline_stages": [
                {
                    "id": stage.get("id"),
                    "method_id": stage.get("method_id"),
                    "method_step_id": stage.get("method_step_id"),
                    "depends_on": list(stage.get("depends_on") or []),
                    "execution_observation": stage.get("execution_observation"),
                }
                for stage in ((run.get("pipeline_contract") or {}).get("stages") or [])
            ],
            "replays": copy.deepcopy(run.get("replays") or []),
            "declared_inputs": list(run.get("declared_inputs", [])),
            "declared_outputs": list(run.get("declared_outputs", [])),
            "declared_intermediates": list(run.get("declared_intermediates", [])),
            "output_transitions": output_transitions,
            "intermediate_transitions": intermediate_transitions,
            "bindings": [
                item for item in run.get("bindings", [])
                if item.get("node_id") in raw_nodes
            ],
            "returncode": run.get("direct_child_returncode"),
            "layer": layer,
            "_rank": run_rank,
        })

    by_layer = defaultdict(list)
    for node in visual_nodes:
        by_layer[node["layer"]].append(node)
    for layer, items in by_layer.items():
        items.sort(key=lambda item: (
            {"semantic": 0, "proof": 1, "assessment": 2, "run": 3}.get(
                item["kind"], 4
            ),
            item["_rank"],
            item["key"],
        ))
        for row, item in enumerate(items):
            item["x"] = MARGIN_X + layer * (NODE_WIDTH + LAYER_GAP)
            item["y"] = MARGIN_TOP + row * (NODE_HEIGHT + ROW_GAP)
            item["width"] = NODE_WIDTH
            item["height"] = NODE_HEIGHT
            item["row"] = row
            del item["_rank"]

    edges = []
    for index, edge in enumerate(graph.get("edges", [])):
        source = edge.get("from")
        target = edge.get("to")
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        if source not in raw_nodes or target not in raw_nodes:
            continue
        relation = str(edge.get("rel", "related"))
        dependency = edge.get("rel") not in ANNOT_RELS
        assessment_state = None
        if relation in {"supports", "refutes"}:
            record = declared_link_records.get((edge.get("from"), edge.get("to"), relation))
            assessment_state = (record or {}).get("status", "unassessed")
            state_label = {
                "covered": "accepted assessment",
                "assessed_conflict": "accepted assessment conflicts",
                "assessed_not_as_written": "assessed, not as written",
                "unassessed": "unassessed",
            }.get(assessment_state, assessment_state.replace("_", " "))
            declaration_label = "support" if relation == "supports" else "refutation"
            relation = f"declared {declaration_label} · {state_label}"
        elif relation == "derives_from":
            record = required_dependency_records.get((
                edge.get("from"), edge.get("to"), relation,
            ))
            if record:
                assessment_state = record.get("status", "unassessed")
        if dependency:
            source, target = _dependency_direction(edge)
        edges.append({
            "key": "semantic:%06d" % index,
            "source": "graph:" + source,
            "target": "graph:" + target,
            "relation": relation,
            "kind": "dependency" if dependency else "annotation",
            "traversable": dependency,
            "assessment_state": assessment_state,
        })

    assessment_edge_index = 0
    for item in current_assessments:
        assessment_id = item["id"]
        assessment_key = assessment_node_keys.get(assessment_id)
        claim_id = (item.get("subject") or {}).get("claim_id")
        if not assessment_key or claim_id not in raw_nodes:
            continue
        active_relation = item.get("active_relation")
        state = str(item.get("current_state") or item.get("review_state") or "proposed")
        # Only accepted support contributes dependency ancestry. Related/refuting assessments
        # remain visible annotations and never create false staleness-style trajectories.
        traversable = active_relation == "supports"
        for result_id in (item.get("subject") or {}).get("result_ids") or []:
            if result_id not in raw_nodes:
                continue
            edges.append({
                "key": "assessment:%06d" % assessment_edge_index,
                "source": "graph:" + result_id,
                "target": assessment_key,
                "relation": "result reviewed",
                "kind": "assessment",
                "traversable": traversable,
                "assessment_state": state,
            })
            assessment_edge_index += 1
        verdict_label = str(item.get("verdict") or "unclassified").replace("_", " ")
        if item.get("integrity_blocked"):
            relation_label = "inactive · assessment store integrity error"
        elif active_relation:
            relation_label = "accepted " + str(active_relation)
        else:
            relation_label = state + " · " + verdict_label
        edges.append({
            "key": "assessment:%06d" % assessment_edge_index,
            "source": assessment_key,
            "target": "graph:" + claim_id,
            "relation": relation_label,
            "kind": "assessment",
            "traversable": traversable,
            "assessment_state": state,
        })
        assessment_edge_index += 1

    proof_edge_index = 0
    for item in derivation_records:
        derivation_id = item.get("id")
        proof_key = proof_node_keys.get(derivation_id)
        subject = item.get("subject") or {}
        claim_id = subject.get("claim_id")
        if not proof_key or claim_id not in raw_nodes:
            continue
        proof_state = str(item.get("proof_state") or "unknown")
        selected_result_ids = (
            subject.get("result_ids") or []
            if proof_state == "unknown" else item.get("used_result_ids") or []
        )
        result_ids = [
            result_id for result_id in selected_result_ids
            if result_id in raw_nodes
        ]
        premise_count = len(result_ids)
        for premise_rank, result_id in enumerate(result_ids, start=1):
            edges.append({
                "key": "proof:%06d" % proof_edge_index,
                "source": "graph:" + result_id,
                "target": proof_key,
                "relation": (
                    "evaluated input %d/%d" if proof_state == "unknown"
                    else "conflicting conditional premise %d/%d"
                    if proof_state == "cross_conflict"
                    else "composite premise %d/%d"
                ) % (premise_rank, premise_count),
                "kind": "proof",
                "traversable": False,
                "derivation_state": item.get("proof_state"),
            })
            proof_edge_index += 1
        state = proof_state
        rule_pack_id = str(
            (item.get("rule_pack") or {}).get("id") or "configured rules"
        )
        if item.get("integrity_blocked"):
            relation = "inactive formal outcome · derivation integrity error"
            relation_label = "integrity-blocked formal outcome"
        elif state == "cross_conflict":
            relation = (
                "formal conflict · target and opposite conditionally derivable under "
                + rule_pack_id
            )
            relation_label = "claim-level formal conflict"
        elif item.get("active") and state == "derivable":
            relation = "formal conclusion · target derivable under " + rule_pack_id
            relation_label = "formal derivation"
        elif item.get("active") and state == "refutable":
            relation = "formal refutation · target refutable under " + rule_pack_id
            relation_label = "formal refutation"
        else:
            relation = "inactive formal outcome · " + state
            relation_label = "inactive formal outcome"
        edges.append({
            "key": "proof:%06d" % proof_edge_index,
            "source": proof_key,
            "target": "graph:" + claim_id,
            "relation": relation,
            "label": relation_label,
            "kind": "proof",
            "traversable": False,
            "derivation_state": item.get("proof_state"),
        })
        proof_edge_index += 1

    receipt_index = 0
    for run in runs:
        run_id = str(run.get("run_id"))
        seen = set()
        for binding in sorted(
            run.get("bindings", []),
            key=lambda item: (
                str(item.get("node_id")), str(item.get("path")),
                str(item.get("declaration_comparison")),
            ),
        ):
            node_id = binding.get("node_id")
            binding_kind = binding.get("binding_kind", "output_path")
            binding_key = (node_id, binding_kind)
            if node_id not in raw_nodes or binding_key in seen:
                continue
            seen.add(binding_key)
            relation = {
                "explicit_run_reference": "explicit semantic run reference",
                "materialized_intermediate_path": "binds materialized intermediate",
            }.get(binding_kind, "binds declared output")
            edges.append({
                "key": "receipt:%06d" % receipt_index,
                "source": "receipt:" + run_id,
                "target": "graph:" + node_id,
                "relation": relation,
                "kind": "receipt",
                "traversable": True,
                "binding_kind": binding_kind,
                "declaration_comparison": binding.get("declaration_comparison"),
                "output_evidence": binding.get("output_evidence"),
                "stage_attribution": binding.get("stage_attribution"),
                "current": binding.get("current"),
                "integrity_state": binding.get("integrity_state"),
            })
            receipt_index += 1

    # Compress reserved layout slots so projects without assessments retain their original layers.
    used_layers = sorted({node["layer"] for node in visual_nodes})
    compact_layers = {layer: index for index, layer in enumerate(used_layers)}
    for node in visual_nodes:
        node["layer"] = compact_layers[node["layer"]]

    by_layer = defaultdict(list)
    for node in visual_nodes:
        by_layer[node["layer"]].append(node)
    for layer, items in by_layer.items():
        items.sort(key=lambda item: (item["row"], item["key"]))
        for row, item in enumerate(items):
            item["x"] = MARGIN_X + layer * (NODE_WIDTH + LAYER_GAP)
            item["y"] = MARGIN_TOP + row * (NODE_HEIGHT + ROW_GAP)
            item["row"] = row

    visual_nodes.sort(key=lambda item: (
        item["layer"], item["row"], item["key"],
    ))
    layer_ids = sorted(by_layer)
    width = (
        MARGIN_X * 2 + NODE_WIDTH
        if not layer_ids
        else MARGIN_X * 2 + NODE_WIDTH + max(layer_ids) * (NODE_WIDTH + LAYER_GAP)
    )
    max_rows = max((len(items) for items in by_layer.values()), default=1)
    height = MARGIN_TOP + max_rows * (NODE_HEIGHT + ROW_GAP) + 28
    summary = report.get("summary") or {}
    return {
        "schema": "claimtrace.view/6",
        "layout_id": layout_id,
        "scope": report.get("scope") or {},
        "assessment_integrity": assessment_integrity or "unknown",
        "derivation_integrity": derivation_integrity or "unknown",
        "semantic_policy": semantic_policy,
        "summary": {
            "semantic_nodes": len(raw_nodes),
            "semantic_edges": sum(
                1 for edge in edges if edge["kind"] in {"dependency", "annotation"}
            ),
            "runs": len(runs),
            "byte_repeatable_runs": sum(
                1 for run in runs if any(
                    item.get("current_derived", {}).get("byte_repeatable_current")
                    for item in run.get("replays", [])
                )
            ),
            "stage_checkpoint_runs": sum(
                1 for run in runs if isinstance(run.get("stage_trace"), dict)
            ),
            "complete_stage_checkpoint_runs": sum(
                1 for run in runs
                if isinstance(run.get("stage_trace"), dict)
                and run["stage_trace"].get("state") == "cooperative_report_complete"
            ),
            "claim_bases": len(claim_basis_records),
            "ready_claim_bases": sum(
                item.get("overall") == "ready_under_reviewed_provenance"
                for item in claim_basis_records
            ),
            "method_assessments": sum(
                len(items) for items in method_assessments_by_node.values()
            ),
            "assessments": len(assessment_records),
            "current_assessments": len(current_assessments),
            "contested_assessments": sum(
                1 for item in current_assessments if item.get("current_state") == "contested"
            ),
            "assessment_edges": sum(1 for edge in edges if edge["kind"] == "assessment"),
            "semantic_mappings": len(semantic_policy["current_mappings"]),
            "semantic_releases": semantic_policy["release_count"],
            "active_semantic_mappings": len(semantic_policy["active_mapping_ids"]),
            "derivations": len(derivation_history_records),
            "proof_nodes": len(derivation_records),
            "active_proofs": sum(1 for item in derivation_records if item.get("active")),
            "claim_conflicts": sum(
                1 for item in derivation_records if item.get("proof_state") == "cross_conflict"
            ),
            "proof_edges": sum(1 for edge in edges if edge["kind"] == "proof"),
            "deliberation_proposals": len(deliberations["proposals"]),
            "deliberation_candidate_sets": len(deliberations["candidate_sets"]),
            "deliberation_ballots": len(deliberations["ballots"]),
            "deliberation_phase_decisions": len(deliberations["phase_decisions"]),
            "deliberation_approved_phase_decisions": sum(
                item.get("decision") == "approved"
                for item in deliberations["phase_decisions"]
            ),
            "deliberation_rejected_phase_decisions": sum(
                item.get("decision") == "rejected"
                for item in deliberations["phase_decisions"]
            ),
            "deliberation_panels": len(deliberations["panels"]),
            "deliberation_recommended": sum(
                item.get("status") == "recommended_for_human_review"
                for item in deliberations["panels"]
            ),
            "deliberation_contested": sum(
                item.get("status") == "contested"
                for item in deliberations["panels"]
            ),
            "findings": len(report.get("findings", [])),
            "errors": summary.get("errors", 0),
            "warnings": summary.get("warnings", 0),
            "pending": summary.get("pending", 0),
        },
        "nodes": visual_nodes,
        "edges": edges,
        "assessments": [assessments_by_id[key] for key in sorted(assessments_by_id)],
        "derivations": [derivations_by_id[key] for key in sorted(derivations_by_id)],
        "deliberations": deliberations,
        "global_findings": sorted(
            global_findings,
            key=lambda item: (
                str(item.get("severity")), str(item.get("code")),
                str(item.get("node_id")), str(item.get("detail")),
            ),
        ),
        "layers": layer_ids,
        "width": width,
        "height": height,
        "coverage_notice": (
            "Semantic edges are declared lineage. Symbolic proofs show conditional "
            "derivability under named project rules, not truth or scientific support. "
            "Semantic mappings are attributed review-state normalization under locked local assertions, "
            "not ontology truth or scientific support. "
            "Adversarial deliberation records are advisory proposals, frozen candidate sets, "
            "role-bound ballots, and attributed phase-routing decisions. Actor and group "
            "labels are self-asserted, not authenticated identity or independence. These "
            "records never create dependency or support edges, never establish scientific "
            "truth, and never activate project state. "
            "Run receipts are mechanical records. "
            "Fresh-workspace replay tests declared boundary bytes, not universal determinism. "
            "The replay child is not sandboxed from network or external filesystem writes. "
            "Materialized intermediate paths are hashed after the process boundary and can be "
            "source/replay compared, but are not attributed to a stage. Where present, "
            "cooperative stage checkpoints are program self-report that locked callsites were "
            "reached, not independent observation of stage computation or in-memory values. "
            "Pipeline stages without checkpoints remain declared only. Pathless or in-memory "
            "intermediates remain not runtime-observed even when checkpoints are present; "
            "pre/post changes do not prove write causation."
        ),
    }


_HTML_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ProvSleuth research trajectory</title>
<style>
:root {
  color-scheme: light dark;
  --bg: #f8fafc;
  --surface: #ffffff;
  --text: #172033;
  --muted: #59657a;
  --border: #cbd3df;
  --edge: #738096;
  --dependency: #52657f;
  --annotation: #8b799e;
  --receipt: #247e73;
  --selected: #0b63ce;
  --upstream: #8d5b00;
  --downstream: #08785d;
  --danger: #b42318;
  --warning: #9a6700;
  --data: #dceafa;
  --code: #e8e2fb;
  --artifact: #dff1ed;
  --figure: #fde8d8;
  --claim: #f8dfeb;
  --document: #e8ebef;
  --method: #f4edcf;
  --concept: #e3eff2;
  --run: #d8f0ea;
  --assessment: #efe3fb;
  --proof: #e4edf9;
  --other: #eceff3;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #111722;
    --surface: #192230;
    --text: #e7edf7;
    --muted: #acb6c7;
    --border: #465267;
    --edge: #8290a6;
    --dependency: #91a7c4;
    --annotation: #b49ac9;
    --receipt: #65c7b9;
    --selected: #6eafff;
    --upstream: #efb85b;
    --downstream: #5dd2af;
    --danger: #ff8b82;
    --warning: #f1c75b;
    --data: #213b59;
    --code: #352d56;
    --artifact: #203f3a;
    --figure: #543526;
    --claim: #533043;
    --document: #303846;
    --method: #494124;
    --concept: #263f48;
    --run: #1f443e;
    --assessment: #402c50;
    --proof: #263b57;
    --other: #303846;
  }
}
* { box-sizing: border-box; }
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
main { max-width: 1680px; margin: 0 auto; padding: 20px; }
h1 { margin: 0 0 4px; font-size: 1.45rem; font-weight: 650; }
h2 { margin: 0 0 10px; font-size: 1.05rem; }
p { margin: 0; }
.summary { color: var(--muted); margin-bottom: 14px; }
.scope {
  border-left: 4px solid var(--receipt);
  background: var(--surface);
  padding: 10px 12px;
  margin-bottom: 14px;
  line-height: 1.45;
}
.deliberation-audit {
  margin: 0 0 14px;
  border: 1px solid var(--border);
  border-left: 4px solid var(--warning);
  border-radius: 7px;
  background: var(--surface);
}
.deliberation-audit > summary {
  cursor: pointer;
  padding: 10px 12px;
  font-weight: 650;
}
.deliberation-boundary {
  margin: 0 12px 12px;
  color: var(--muted);
  line-height: 1.4;
}
.deliberation-content {
  display: grid;
  gap: 12px;
  padding: 0 12px 12px;
}
.deliberation-section h3 { margin: 0 0 6px; font-size: .95rem; }
.deliberation-card {
  margin: 6px 0;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
}
.deliberation-card > summary {
  cursor: pointer;
  padding: 8px 10px;
  overflow-wrap: anywhere;
}
.deliberation-card pre {
  margin: 0;
  padding: 10px;
  max-height: 360px;
  overflow: auto;
  border-top: 1px solid var(--border);
  color: var(--text);
  font-size: .76rem;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.controls {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: end;
  margin-bottom: 12px;
}
.projection-controls {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 7px;
  margin: -2px 0 12px;
  padding: 9px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
}
.projection-status {
  flex: 1 1 320px;
  color: var(--muted);
  font-size: .84rem;
  line-height: 1.35;
}
.projection-controls details {
  position: relative;
}
.projection-controls summary {
  cursor: pointer;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 7px 10px;
  color: var(--text);
  font-size: .82rem;
  list-style-position: inside;
  user-select: none;
}
.projection-controls details[open] summary { border-color: var(--selected); }
.filter-options {
  position: absolute;
  z-index: 8;
  top: calc(100% + 5px);
  right: 0;
  display: grid;
  gap: 5px;
  min-width: 210px;
  max-width: min(340px, 88vw);
  max-height: 320px;
  overflow: auto;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--surface);
  box-shadow: 0 8px 24px rgba(0, 0, 0, .25);
}
.filter-options label {
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--text);
  font-size: .8rem;
}
.filter-options input { margin: 0; }
.projection-boundary {
  flex-basis: 100%;
  color: var(--muted);
  font-size: .78rem;
}
label { display: grid; gap: 4px; color: var(--muted); font-size: .82rem; }
select {
  min-width: 190px;
  max-width: min(440px, 90vw);
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface);
  color: var(--text);
  padding: 7px 9px;
  font: inherit;
}
button {
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface);
  color: var(--text);
  padding: 7px 11px;
  font: inherit;
  cursor: pointer;
}
button:hover { border-color: var(--selected); }
button:focus-visible, select:focus-visible {
  outline: 3px solid var(--selected);
  outline-offset: 2px;
}
button:disabled { cursor: not-allowed; opacity: .48; }
.move-controls {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.move-controls button {
  min-width: 34px;
  padding: 7px 8px;
}
.layout-help {
  align-self: center;
  max-width: 560px;
  color: var(--muted);
  font-size: .82rem;
  line-height: 1.35;
}
.workspace {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(380px, 520px);
  gap: 16px;
  align-items: start;
}
.graph-panel { position: relative; min-width: 0; }
.graph-scroll {
  overflow: auto;
  height: clamp(420px, 68vh, 760px);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  overscroll-behavior: contain;
  cursor: grab;
}
#trajectory {
  display: block;
  cursor: grab;
  touch-action: none;
}
.graph-scroll.ct-panning,
.graph-scroll.ct-panning #trajectory {
  cursor: grabbing;
  user-select: none;
}
.viewport-controls {
  position: absolute;
  right: 12px;
  bottom: 12px;
  z-index: 4;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 5px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
  background: color-mix(in srgb, var(--surface) 92%, transparent);
  box-shadow: 0 4px 18px rgba(0, 0, 0, .24);
}
.viewport-controls button {
  min-width: 34px;
  padding: 7px 9px;
}
#zoom-reset { min-width: 58px; font-variant-numeric: tabular-nums; }
#viewport-fit { min-width: 42px; }
.details {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px;
  min-width: 0;
}
.details dl { margin: 0; display: grid; grid-template-columns: 118px 1fr; gap: 7px 9px; }
.details dt { color: var(--muted); }
.details dd { margin: 0; overflow-wrap: anywhere; }
.detail-run-link {
  max-width: 100%;
  padding: 2px 6px;
  color: var(--selected);
  text-align: left;
  overflow-wrap: anywhere;
}
.detail-note { color: var(--muted); line-height: 1.4; }
.assessment-control { margin: 0 0 12px; }
.assessment-control[hidden] { display: none; }
.review-panel { margin-top: 14px; border-top: 1px solid var(--border); padding-top: 14px; }
.review-panel h3 { margin: 0 0 7px; font-size: .95rem; }
.review-status {
  margin: 0 0 12px;
  border-left: 4px solid var(--assessment);
  padding-left: 9px;
  font-weight: 500;
}
.review-comparison {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 14px;
  margin-bottom: 12px;
}
.review-comparison section { min-width: 0; }
.review-comparison p, .review-copy { line-height: 1.42; overflow-wrap: anywhere; }
.frame-list { margin: 7px 0 0; padding-left: 17px; color: var(--muted); }
.frame-list li { margin-bottom: 3px; }
.alignment-table { width: 100%; border-collapse: collapse; margin: 4px 0 12px; }
.alignment-table th, .alignment-table td {
  border-bottom: 1px solid var(--border);
  padding: 6px 5px;
  text-align: left;
  vertical-align: top;
  overflow-wrap: anywhere;
}
.alignment-table th:last-child, .alignment-table td:last-child { width: 42%; }
.review-list { margin: 6px 0 12px; padding-left: 18px; }
.review-list li { margin-bottom: 5px; overflow-wrap: anywhere; }
.review-panel details { margin-top: 10px; }
.review-panel summary { cursor: pointer; font-weight: 500; }
.review-panel code { overflow-wrap: anywhere; }
.finding-list { margin: 12px 0 0; padding-left: 18px; color: var(--text); }
.finding-list li { margin: 0 0 8px; overflow-wrap: anywhere; }
.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 7px 14px;
  margin-top: 12px;
  color: var(--muted);
  font-size: .82rem;
}
.legend-item { display: inline-flex; align-items: center; gap: 6px; }
.swatch { width: 18px; height: 10px; border-radius: 3px; border: 1.5px solid var(--edge); }
.swatch.dependency { border-top: 2px solid var(--dependency); border-left: 0; border-right: 0; border-bottom: 0; border-radius: 0; }
.swatch.annotation { border: 0; border-top: 2px dotted var(--annotation); border-radius: 0; }
.swatch.receipt { background: var(--run); border: 2px dashed var(--receipt); }
.swatch.assessment { background: var(--assessment); border: 2px dashed var(--annotation); transform: rotate(45deg); }
.swatch.proof { background: var(--proof); border: 2px double var(--dependency); }
.swatch.retired { background: var(--other); border: 2px dashed var(--danger); }
.swatch.data { background: var(--data); }
.swatch.code { background: var(--code); }
.swatch.artifact { background: var(--artifact); }
.swatch.claim { background: var(--claim); }
.ct-layer-label { fill: var(--muted); font-size: 12px; font-weight: 600; }
.ct-edge-group { transition: opacity .12s ease; }
.ct-edge {
  fill: none;
  stroke: var(--edge);
  stroke-width: 1.5;
  stroke-linecap: round;
  stroke-linejoin: round;
  opacity: .7;
  pointer-events: none;
}
.ct-edge-hit {
  fill: none;
  stroke: transparent;
  stroke-width: 18px;
  stroke-linecap: round;
  stroke-linejoin: round;
  vector-effect: non-scaling-stroke;
  pointer-events: stroke;
  cursor: pointer;
}
.ct-edge-group:hover .ct-edge,
.ct-edge-group:focus .ct-edge,
.ct-edge-group:focus-within .ct-edge { opacity: 1; stroke-width: 2.5px; }
.ct-edge-group:focus { outline: none; }
.ct-edge-group.ct-edge-selected { opacity: 1 !important; }
.ct-edge-group.ct-edge-selected .ct-edge {
  stroke: var(--selected) !important;
  stroke-width: 3px !important;
  opacity: 1;
}
.ct-edge-group.ct-edge-selected .ct-edge-label {
  fill: var(--selected);
  font-weight: 700;
}
.ct-edge-dependency { stroke: var(--dependency); }
.ct-edge-annotation { stroke: var(--annotation); stroke-dasharray: 3 5; }
.ct-edge-receipt { stroke: var(--receipt); stroke-dasharray: 8 5; stroke-width: 2; }
.ct-edge-assessment { stroke: var(--annotation); stroke-dasharray: 5 4; stroke-width: 2; }
.ct-edge-proof { stroke: var(--dependency); stroke-dasharray: 2 4; stroke-width: 2; }
.ct-edge-state-unassessed,
.ct-edge-state-assessed-not-as-written { stroke: var(--warning); stroke-dasharray: 4 5; }
.ct-edge-state-covered { stroke: var(--downstream); stroke-width: 2.4; }
.ct-edge-state-assessed-conflict,
.ct-edge-state-contested { stroke: var(--danger); stroke-dasharray: 2 4; stroke-width: 2.4; }
.ct-edge-label {
  fill: var(--muted);
  font-size: 10px;
  paint-order: stroke;
  stroke: var(--surface);
  stroke-width: 4px;
  cursor: pointer;
}
.ct-node {
  cursor: grab;
  touch-action: none;
  user-select: none;
  transition: opacity .12s ease;
}
.ct-node.ct-dragging { cursor: grabbing; }
.ct-node.ct-dragging :is(rect, polygon) {
  stroke: var(--selected) !important;
  stroke-width: 4px !important;
}
.ct-node[hidden], .ct-edge-group[hidden], .ct-layer-label[hidden] { display: none; }
.ct-manual-layout .ct-layer-label { opacity: .46; }
.ct-node rect, .ct-node polygon { stroke: var(--border); stroke-width: 1.5; }
.ct-node text { pointer-events: none; fill: var(--text); }
.ct-node .ct-title { font-size: 13px; font-weight: 650; }
.ct-node .ct-meta, .ct-node .ct-path { font-size: 11px; fill: var(--muted); }
.ct-type-data rect { fill: var(--data); }
.ct-type-code rect { fill: var(--code); }
.ct-type-artifact rect { fill: var(--artifact); }
.ct-type-figure rect { fill: var(--figure); }
.ct-type-claim rect { fill: var(--claim); }
.ct-type-doc rect, .ct-type-doc-span rect { fill: var(--document); }
.ct-type-experiment rect, .ct-type-method rect, .ct-type-decision rect { fill: var(--method); }
.ct-type-concept rect, .ct-type-reference rect { fill: var(--concept); }
.ct-type-question rect, .ct-type-hypothesis rect, .ct-type-prediction rect { fill: var(--concept); }
.ct-type-conclusion rect { fill: var(--claim); }
.ct-type-preprocessing rect { fill: var(--code); }
.ct-type-output rect { fill: var(--artifact); }
.ct-type-run-receipt rect { fill: var(--run); stroke: var(--receipt); stroke-dasharray: 7 4; }
.ct-type-semantic-assessment polygon { fill: var(--assessment); stroke: var(--annotation); stroke-dasharray: 5 3; }
.ct-type-symbolic-proof polygon { fill: var(--proof); stroke: var(--dependency); stroke-width: 2; }
.ct-type-symbolic-conflict polygon { fill: var(--proof); stroke: var(--danger); stroke-width: 3; }
.ct-type-other rect { fill: var(--other); }
.ct-status-confirmed :is(rect, polygon), .ct-status-accepted :is(rect, polygon) { stroke: var(--downstream); stroke-width: 2.5; }
.ct-status-stale :is(rect, polygon), .ct-status-proposed :is(rect, polygon) { stroke: var(--warning); stroke-width: 2.5; stroke-dasharray: 7 4; }
.ct-status-contested :is(rect, polygon) { stroke: var(--danger); stroke-width: 3; stroke-dasharray: 2 4; }
.ct-status-rejected :is(rect, polygon) { stroke: var(--danger); stroke-width: 2.5; stroke-dasharray: 7 4; }
.ct-status-derivable :is(rect, polygon) { stroke: var(--downstream); stroke-width: 2.5; }
.ct-status-refutable :is(rect, polygon) { stroke: var(--danger); stroke-width: 2.5; }
.ct-status-conflict :is(rect, polygon) { stroke: var(--danger); stroke-width: 3; stroke-dasharray: 2 4; }
.ct-status-claim-conflict :is(rect, polygon),
.ct-status-integrity-error :is(rect, polygon) { stroke: var(--danger); stroke-width: 3; stroke-dasharray: 2 4; }
.ct-status-unknown :is(rect, polygon) { stroke: var(--warning); stroke-width: 2.5; stroke-dasharray: 7 4; }
.ct-status-inactive-derivable :is(rect, polygon),
.ct-status-inactive-refutable :is(rect, polygon),
.ct-status-inactive-conflict :is(rect, polygon),
.ct-status-inactive-unknown :is(rect, polygon) { stroke: var(--muted); stroke-width: 2; stroke-dasharray: 7 4; opacity: .78; }
.ct-status-null :is(rect, polygon) { stroke: var(--annotation); stroke-width: 2.5; stroke-dasharray: 2 4; }
.ct-status-dead-end rect, .ct-status-retracted rect,
.ct-status-deprecated rect, .ct-status-superseded rect {
  stroke: var(--danger); stroke-width: 2.5; stroke-dasharray: 7 4;
}
.ct-dim { opacity: .13; }
.ct-selected { opacity: 1 !important; }
.ct-selected :is(rect, polygon) { stroke: var(--selected) !important; stroke-width: 4px !important; }
.ct-ancestor { opacity: 1 !important; }
.ct-ancestor :is(rect, polygon) { stroke: var(--upstream) !important; stroke-width: 3px !important; }
.ct-descendant { opacity: 1 !important; }
.ct-descendant :is(rect, polygon) { stroke: var(--downstream) !important; stroke-width: 3px !important; }
.ct-edge-source, .ct-edge-target { opacity: 1 !important; }
.ct-edge-source :is(rect, polygon) { stroke: var(--upstream) !important; stroke-width: 4px !important; }
.ct-edge-target :is(rect, polygon) { stroke: var(--downstream) !important; stroke-width: 4px !important; }
.ct-edge.ct-related { opacity: 1; stroke-width: 2.7; }
.ct-edge.ct-upstream { stroke: var(--upstream); }
.ct-edge.ct-downstream { stroke: var(--downstream); }
.ct-layer-muted { opacity: .10; }
@media (max-width: 900px) {
  main { padding: 12px; }
  .workspace { grid-template-columns: 1fr; }
}
@media (max-width: 560px) {
  .review-comparison { grid-template-columns: 1fr; }
  .details dl { grid-template-columns: 76px 1fr; }
}
</style>
</head>
<body>
<main>
  <h1>Research trajectory</h1>
  <p id="summary" class="summary"></p>
  <p id="semantic-policy-summary" class="scope"></p>
  <details id="semantic-mapping-details">
    <summary>Semantic mapping decisions</summary>
    <ul id="semantic-mapping-list"></ul>
  </details>
  <details id="deliberation-details" class="deliberation-audit">
    <summary id="deliberation-summary">Adversarial claim deliberation</summary>
    <p id="deliberation-boundary" class="deliberation-boundary"></p>
    <div id="deliberation-content" class="deliberation-content"></div>
  </details>
  <p id="coverage" class="scope"></p>
  <div class="controls">
    <label for="layer-select">Layer
      <select id="layer-select"><option value="">All layers</option></select>
    </label>
    <label for="focus-select">Focus
      <select id="focus-select"><option value="">Choose a node or run</option></select>
    </label>
    <label for="edge-select">Relationship
      <select id="edge-select"><option value="">Choose an edge</option></select>
    </label>
    <span class="move-controls" role="group" aria-label="Move focused node">
      <button id="move-left" type="button" aria-label="Move focused node left" title="Move left" disabled>←</button>
      <button id="move-up" type="button" aria-label="Move focused node up" title="Move up" disabled>↑</button>
      <button id="move-down" type="button" aria-label="Move focused node down" title="Move down" disabled>↓</button>
      <button id="move-right" type="button" aria-label="Move focused node right" title="Move right" disabled>→</button>
    </span>
    <button id="layout-reset" type="button">Auto-arrange</button>
    <span id="layout-status" class="layout-help" role="status" aria-live="polite">
      Wheel to zoom; drag empty background to pan; drag nodes to rearrange them; click an edge to inspect it. Node positions are browser-local and visual only; the graph, provenance, and logical layers do not change.
    </span>
  </div>
  <div class="projection-controls" role="group" aria-label="Visible graph controls">
    <span id="projection-status" class="projection-status" role="status" aria-live="polite"></span>
    <button id="projection-overview" type="button">Overview</button>
    <button id="expand-upstream" type="button" disabled>Add one upstream hop</button>
    <button id="expand-downstream" type="button" disabled>Add one downstream hop</button>
    <button id="expand-both" type="button" disabled>Add one hop both ways</button>
    <button id="collapse-focus" type="button" disabled>Focus only</button>
    <button id="projection-full" type="button">Show all</button>
    <details>
      <summary>Node types</summary>
      <div id="node-type-filters" class="filter-options"></div>
    </details>
    <details>
      <summary>Statuses</summary>
      <div id="node-status-filters" class="filter-options"></div>
    </details>
    <details>
      <summary>Layers</summary>
      <div id="layer-filters" class="filter-options"></div>
    </details>
    <details>
      <summary>Edge classes</summary>
      <div id="edge-kind-filters" class="filter-options"></div>
    </details>
    <span id="projection-boundary" class="projection-boundary">Filters and projection change only this view; the graph and provenance are unchanged. A focused node or selected relationship remains visible even when a filter excludes it. Double-click a node to add one hop in both directions.</span>
  </div>
  <div class="workspace">
    <div class="graph-panel">
      <div class="graph-scroll" role="region" tabindex="0" aria-label="Research trajectory canvas. Use the mouse wheel to zoom and drag empty background to pan." aria-describedby="layout-status projection-status projection-boundary">
        <svg id="trajectory" role="img" preserveAspectRatio="xMinYMin meet" aria-labelledby="trajectory-title trajectory-description">
          <title id="trajectory-title">ProvSleuth research trajectory</title>
          <desc id="trajectory-description">The current visible projection of a deterministic layered graph of declared semantic lineage, conditional symbolic proofs, semantic reviews, and partial mechanical run receipts. Projection controls may hide nodes and relationships without removing them from provenance.</desc>
          <defs>
          <marker id="arrow-dependency" viewBox="0 0 7 7" markerWidth="7" markerHeight="7" refX="6.4" refY="3.5" orient="auto" markerUnits="userSpaceOnUse">
            <path d="M0.8,0.8 L6.4,3.5 L0.8,6.2 z" fill="var(--dependency)"></path>
          </marker>
          <marker id="arrow-annotation" viewBox="0 0 7 7" markerWidth="7" markerHeight="7" refX="6.4" refY="3.5" orient="auto" markerUnits="userSpaceOnUse">
            <path d="M0.8,0.8 L6.4,3.5 L0.8,6.2 z" fill="var(--annotation)"></path>
          </marker>
          <marker id="arrow-receipt" viewBox="0 0 7 7" markerWidth="7" markerHeight="7" refX="6.4" refY="3.5" orient="auto" markerUnits="userSpaceOnUse">
            <path d="M0.8,0.8 L6.4,3.5 L0.8,6.2 z" fill="var(--receipt)"></path>
          </marker>
          <marker id="arrow-assessment" viewBox="0 0 7 7" markerWidth="7" markerHeight="7" refX="6.4" refY="3.5" orient="auto" markerUnits="userSpaceOnUse">
            <path d="M0.8,0.8 L6.4,3.5 L0.8,6.2 z" fill="var(--annotation)"></path>
          </marker>
          <marker id="arrow-proof" viewBox="0 0 7 7" markerWidth="7" markerHeight="7" refX="6.4" refY="3.5" orient="auto" markerUnits="userSpaceOnUse">
            <path d="M0.8,0.8 L6.4,3.5 L0.8,6.2 z" fill="var(--dependency)"></path>
          </marker>
          <marker id="arrow-selected" viewBox="0 0 7 7" markerWidth="7" markerHeight="7" refX="6.4" refY="3.5" orient="auto" markerUnits="userSpaceOnUse">
            <path d="M0.8,0.8 L6.4,3.5 L0.8,6.2 z" fill="var(--selected)"></path>
          </marker>
          </defs>
          <g id="layer-labels"></g>
          <g id="edge-layer"></g>
          <g id="node-layer"></g>
        </svg>
      </div>
      <div class="viewport-controls" role="group" aria-label="Graph view controls">
        <button id="zoom-out" type="button" aria-label="Zoom out" title="Zoom out">&minus;</button>
        <button id="zoom-reset" type="button" aria-label="Reset zoom to 100%" title="Reset zoom to 100%">100%</button>
        <button id="zoom-in" type="button" aria-label="Zoom in" title="Zoom in">+</button>
        <button id="viewport-fit" type="button" aria-label="Fit graph in view" title="Fit graph in view">Fit</button>
      </div>
    </div>
    <aside class="details" role="region" aria-live="polite" aria-labelledby="detail-title">
      <h2 id="detail-title">Trajectory overview</h2>
      <label id="assessment-control" class="assessment-control" for="assessment-select" hidden>Assessment
        <select id="assessment-select"></select>
      </label>
      <div id="detail-content" class="detail-note"></div>
    </aside>
  </div>
  <div class="legend" aria-label="Visual encoding">
    <span class="legend-item"><span class="swatch data"></span>data</span>
    <span class="legend-item"><span class="swatch code"></span>code</span>
    <span class="legend-item"><span class="swatch artifact"></span>artifact / output</span>
    <span class="legend-item"><span class="swatch claim"></span>claim / conclusion</span>
    <span class="legend-item"><span class="swatch dependency"></span>declared dependency</span>
    <span class="legend-item"><span class="swatch annotation"></span>semantic annotation</span>
    <span class="legend-item"><span class="swatch receipt"></span>partial run receipt</span>
    <span class="legend-item"><span class="swatch assessment"></span>agent-assessed semantic review</span>
    <span class="legend-item"><span class="swatch proof"></span>conditional symbolic proof</span>
    <span class="legend-item"><span class="swatch retired"></span>stale or retired status</span>
  </div>
</main>
"""


_HTML_SCRIPT = """
<script id="provsleuth-data" type="application/json">__PROVSLEUTH_DATA__</script>
<script>
(function () {
  "use strict";
  const data = JSON.parse(document.getElementById("provsleuth-data").textContent);
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.getElementById("trajectory");
  const edgeLayer = document.getElementById("edge-layer");
  const nodeLayer = document.getElementById("node-layer");
  const labelLayer = document.getElementById("layer-labels");
  const layerSelect = document.getElementById("layer-select");
  const focusSelect = document.getElementById("focus-select");
  const edgeSelect = document.getElementById("edge-select");
  const graphScroll = document.querySelector(".graph-scroll");
  const zoomOutButton = document.getElementById("zoom-out");
  const zoomResetButton = document.getElementById("zoom-reset");
  const zoomInButton = document.getElementById("zoom-in");
  const viewportFitButton = document.getElementById("viewport-fit");
  const layoutReset = document.getElementById("layout-reset");
  const layoutStatus = document.getElementById("layout-status");
  const projectionStatus = document.getElementById("projection-status");
  const projectionOverview = document.getElementById("projection-overview");
  const projectionFull = document.getElementById("projection-full");
  const expandUpstream = document.getElementById("expand-upstream");
  const expandDownstream = document.getElementById("expand-downstream");
  const expandBoth = document.getElementById("expand-both");
  const collapseFocusButton = document.getElementById("collapse-focus");
  const nodeTypeFilters = document.getElementById("node-type-filters");
  const nodeStatusFilters = document.getElementById("node-status-filters");
  const layerFilters = document.getElementById("layer-filters");
  const edgeKindFilters = document.getElementById("edge-kind-filters");
  const moveButtons = {
    left: document.getElementById("move-left"),
    up: document.getElementById("move-up"),
    down: document.getElementById("move-down"),
    right: document.getElementById("move-right")
  };
  const detailTitle = document.getElementById("detail-title");
  const detailContent = document.getElementById("detail-content");
  const assessmentControl = document.getElementById("assessment-control");
  const assessmentSelect = document.getElementById("assessment-select");
  const nodes = new Map(data.nodes.map(function (node) { return [node.key, node]; }));
  const edges = new Map(data.edges.map(function (edge) { return [edge.key, edge]; }));
  const defaultPositions = new Map(data.nodes.map(function (node) {
    return [node.key, {x: node.x, y: node.y}];
  }));
  const fullLayoutPositions = new Map();
  const assessments = new Map((data.assessments || []).map(function (item) {
    return [item.id, item];
  }));
  const nodeElements = new Map();
  const edgeElements = new Map();
  const layerElements = new Map();
  const outgoing = new Map();
  const incoming = new Map();
  const projectionOutgoing = new Map();
  const projectionIncoming = new Map();
  const largeGraphThreshold = 120;
  const largeGraphEdgeThreshold = 240;
  const fullViewFastRoutingThreshold = 144;
  const detailedRoutingNodeThreshold = 72;
  const maximumDetailedGridPoints = 50000;
  const maximumDetailedRouteWork = 2000000;
  const largeGraph = data.nodes.length > largeGraphThreshold ||
    data.edges.length > largeGraphEdgeThreshold;
  const initialProjectionMode = largeGraph ? "overview" : "full";
  const visibleNodeKeys = new Set();
  const visibleEdgeKeys = new Set();
  const revealedNodeKeys = new Set();
  const enabledNodeTypes = new Set(data.nodes.map(function (node) {
    return String(node.type || "other");
  }));
  const enabledNodeStatuses = new Set(data.nodes.map(function (node) {
    return String(node.status || "unknown");
  }));
  const enabledLayers = new Set(data.nodes.map(function (node) {
    return Number(node.layer);
  }));
  const enabledEdgeKinds = new Set(data.edges.map(function (edge) {
    return String(edge.kind || "dependency");
  }));
  const baseCanvasWidth = Number(data.width) || 640;
  const baseCanvasHeight = Number(data.height) || 480;
  const canvasPadding = 48;
  const minimumCoordinate = 12;
  const maximumCanvasWidth = baseCanvasWidth + Math.max(
    4096, Math.min(16384, baseCanvasWidth * 2)
  );
  const maximumCanvasHeight = baseCanvasHeight + Math.max(
    4096, Math.min(16384, baseCanvasHeight * 2)
  );
  const dragThreshold = 4;
  const routingClearance = 10;
  const nodeSeparation = routingClearance * 2 + 2;
  const routingBendPenalty = 28;
  const routingCongestionPenalty = 64;
  const routingLaneSeparation = 20;
  const edgeApproachLength = 20;
  const edgeArrowGap = 5;
  const edgeCornerRadius = 8;
  const minimumViewportZoom = 0.25;
  const maximumViewportZoom = 3;
  const viewportZoomStep = 1.2;
  const layoutStorageSuffix = stableHash(
    data.schema + "|" + data.layout_id + "|" + window.location.pathname
  );
  const layoutStorageKey = "provsleuth.layout.v1." + layoutStorageSuffix;
  const legacyLayoutStorageKey = "claimtrace.layout.v1." + layoutStorageSuffix;
  let selected = null;
  let selectedEdge = null;
  let selectedAssessment = null;
  let dragState = null;
  let panState = null;
  let viewportZoom = 1;
  let suppressClickKey = null;
  let projectionMode = initialProjectionMode;
  let overviewNodeKeys = new Set();
  let projectionInitialized = false;
  let currentRoutingMode = "detailed";
  let fullLayoutIsManual = false;

  function svgElement(tag, attributes, text) {
    const element = document.createElementNS(NS, tag);
    Object.keys(attributes || {}).forEach(function (name) {
      element.setAttribute(name, String(attributes[name]));
    });
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function setSvgHidden(element, hidden) {
    if (!element) return;
    if (hidden) element.setAttribute("hidden", "");
    else element.removeAttribute("hidden");
  }

  function classToken(value) {
    const token = String(value || "other").toLowerCase().replace(/[^a-z0-9]+/g, "-");
    return token.replace(/^-+|-+$/g, "") || "other";
  }

  function typeClass(value) {
    const token = classToken(value);
    const known = new Set([
      "data", "code", "artifact", "figure", "claim", "doc", "doc-span",
      "experiment", "method", "decision", "concept", "reference",
      "hypothesis", "prediction", "conclusion", "preprocessing", "output",
      "run-receipt", "semantic-assessment", "symbolic-proof", "symbolic-conflict"
    ]);
    return known.has(token) ? token : "other";
  }

  function short(value, limit) {
    const text = value === null || value === undefined ? "" : String(value);
    return text.length <= limit ? text : text.slice(0, limit - 1) + "…";
  }

  function addConnection(map, key, value) {
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(value);
  }

  function stableHash(value) {
    let hash = 2166136261;
    for (let index = 0; index < value.length; index += 1) {
      hash ^= value.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0).toString(16).padStart(8, "0");
  }

  function sortedUnique(values, numeric) {
    const unique = Array.from(new Set(values));
    return unique.sort(numeric
      ? function (left, right) { return Number(left) - Number(right); }
      : function (left, right) { return String(left).localeCompare(String(right)); });
  }

  function buildOverviewNodeKeys() {
    const overviewTypes = new Set([
      "question", "hypothesis", "prediction", "claim", "conclusion",
      "figure", "decision"
    ]);
    const statusPriority = new Map([
      ["current", 0], ["confirmed", 0], ["accepted", 0], ["null", 1],
      ["contested", 2], ["stale", 3], ["proposed", 3], ["superseded", 4],
      ["retracted", 5], ["deprecated", 5], ["dead_end", 5]
    ]);
    let candidates = data.nodes.filter(function (node) {
      return node.kind === "semantic" && overviewTypes.has(String(node.type || ""));
    });
    if (!candidates.length) {
      candidates = data.nodes.filter(function (node) {
        return !(projectionOutgoing.get(node.key) || []).length;
      });
    }
    if (!candidates.length) candidates = data.nodes.slice();
    const compareCandidates = function (left, right) {
      const leftPriority = statusPriority.has(String(left.status))
        ? statusPriority.get(String(left.status)) : 3;
      const rightPriority = statusPriority.has(String(right.status))
        ? statusPriority.get(String(right.status)) : 3;
      return leftPriority - rightPriority || left.layer - right.layer ||
        left.row - right.row || left.key.localeCompare(right.key);
    };
    candidates.sort(compareCandidates);
    const limit = 72;
    const chosen = [];
    const chosenKeys = new Set();
    const addCandidate = function (node) {
      if (!node || chosen.length >= limit || chosenKeys.has(node.key)) return;
      chosen.push(node);
      chosenKeys.add(node.key);
    };
    const typeOrder = [
      "conclusion", "claim", "figure", "decision", "prediction",
      "hypothesis", "question"
    ];
    typeOrder.forEach(function (type) {
      addCandidate(candidates.find(function (node) { return node.type === type; }));
    });
    sortedUnique(candidates.map(function (node) { return node.layer; }), true)
      .forEach(function (layer) {
        addCandidate(candidates.find(function (node) { return node.layer === layer; }));
      });
    const candidatesByType = new Map(typeOrder.map(function (type) {
      return [type, candidates.filter(function (node) { return node.type === type; })];
    }));
    for (let index = 0; chosen.length < limit; index += 1) {
      let added = false;
      typeOrder.forEach(function (type) {
        const before = chosen.length;
        addCandidate((candidatesByType.get(type) || [])[index]);
        if (chosen.length > before) added = true;
      });
      if (!added) break;
    }
    candidates.forEach(addCandidate);
    return new Set(chosen.map(function (node) { return node.key; }));
  }

  function projectionOneHop(start, adjacency) {
    const keys = new Set();
    if (!start || !nodes.has(start)) return keys;
    keys.add(start);
    (adjacency.get(start) || []).forEach(function (link) { keys.add(link.node); });
    return keys;
  }

  function expandSelectedOneHop(adjacency) {
    if (!selected || !nodes.has(selected)) return false;
    projectionOneHop(selected, adjacency).forEach(function (key) {
      revealedNodeKeys.add(key);
      visibleNodeKeys.add(key);
    });
    projectionMode = "custom";
    applyVisibility(true);
    return true;
  }

  function filtersAreDefault() {
    return data.nodes.every(function (node) {
      return enabledNodeTypes.has(String(node.type || "other")) &&
        enabledNodeStatuses.has(String(node.status || "unknown")) &&
        enabledLayers.has(Number(node.layer));
    }) && data.edges.every(function (edge) {
      return enabledEdgeKinds.has(String(edge.kind || "dependency"));
    });
  }

  function usesPersistentFullLayout() {
    return projectionMode === "full" && filtersAreDefault();
  }

  function restoreFullLayoutPositions() {
    data.nodes.forEach(function (node) {
      const position = fullLayoutPositions.get(node.key) || defaultPositions.get(node.key);
      node.x = position.x;
      node.y = position.y;
    });
    data.layers.forEach(function (layer) {
      const layerNode = data.nodes.find(function (node) { return node.layer === layer; });
      const layerElement = layerElements.get(layer);
      const position = layerNode ? defaultPositions.get(layerNode.key) : null;
      if (layerElement && position) layerElement.setAttribute("x", position.x + 112);
    });
  }

  function refreshAfterFilterChange() {
    if (usesPersistentFullLayout()) {
      restoreFullLayoutPositions();
      setManualLayout(fullLayoutIsManual);
      applyVisibility(false);
      fitViewport();
      layoutStatus.textContent = fullLayoutIsManual
        ? "All filters are enabled; the saved full-graph arrangement is restored. The graph and provenance are unchanged."
        : "All filters are enabled; the automatic full-graph arrangement is restored. The graph and provenance are unchanged.";
      return;
    }
    setManualLayout(false);
    applyVisibility(true);
    fitViewport();
    layoutStatus.textContent =
      "Visible nodes were auto-arranged from the active filters. This temporary filtered layout does not overwrite the saved full layout, graph, or provenance.";
  }

  function setProjectionMode(mode) {
    const nextMode = mode === "full" ? "full" : "overview";
    projectionMode = nextMode;
    revealedNodeKeys.clear();
    const keys = nextMode === "full"
      ? data.nodes.map(function (node) { return node.key; })
      : Array.from(overviewNodeKeys);
    keys.forEach(function (key) { revealedNodeKeys.add(key); });
    if (usesPersistentFullLayout()) {
      if (projectionInitialized) restoreFullLayoutPositions();
      setManualLayout(fullLayoutIsManual);
      applyVisibility(false);
    } else {
      setManualLayout(false);
      applyVisibility(true);
    }
  }

  function populateFilter(container, values, enabled, prefix, numeric) {
    sortedUnique(values, numeric).forEach(function (value, index) {
      const label = document.createElement("label");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = true;
      input.id = prefix + "-" + index;
      input.value = String(value);
      input.addEventListener("change", function () {
        const key = numeric ? Number(input.value) : input.value;
        if (input.checked) enabled.add(key);
        else enabled.delete(key);
        refreshAfterFilterChange();
      });
      label.appendChild(input);
      label.appendChild(document.createTextNode(
        prefix === "layer-filter" ? "Layer " + value : String(value).replace(/_/g, " ")
      ));
      container.appendChild(label);
    });
  }

  function resetProjectionFilters() {
    enabledNodeTypes.clear();
    data.nodes.forEach(function (node) {
      enabledNodeTypes.add(String(node.type || "other"));
    });
    enabledNodeStatuses.clear();
    data.nodes.forEach(function (node) {
      enabledNodeStatuses.add(String(node.status || "unknown"));
    });
    enabledLayers.clear();
    data.nodes.forEach(function (node) { enabledLayers.add(Number(node.layer)); });
    enabledEdgeKinds.clear();
    data.edges.forEach(function (edge) {
      enabledEdgeKinds.add(String(edge.kind || "dependency"));
    });
    [nodeTypeFilters, nodeStatusFilters, layerFilters, edgeKindFilters]
      .forEach(function (container) {
        container.querySelectorAll('input[type="checkbox"]').forEach(function (input) {
          input.checked = true;
        });
      });
  }

  function arrangeVisibleNodes() {
    const byLayer = new Map();
    data.nodes.forEach(function (node) {
      if (!visibleNodeKeys.has(node.key)) return;
      if (!byLayer.has(node.layer)) byLayer.set(node.layer, []);
      byLayer.get(node.layer).push(node);
    });
    const usedLayers = Array.from(byLayer.keys()).sort(function (left, right) {
      return left - right;
    });
    usedLayers.forEach(function (layer, layerIndex) {
      const x = 44 + layerIndex * 340;
      const items = byLayer.get(layer).sort(function (left, right) {
        return left.row - right.row || left.key.localeCompare(right.key);
      });
      items.forEach(function (node, rowIndex) {
        node.x = x;
        node.y = 64 + rowIndex * 106;
      });
      const layerElement = layerElements.get(layer);
      if (layerElement) {
        layerElement.setAttribute("x", x + 112);
        setSvgHidden(layerElement, false);
      }
    });
  }

  function updateProjectionStatus() {
    const hiddenNodes = data.nodes.length - visibleNodeKeys.size;
    const hiddenEdges = data.edges.length - visibleEdgeKeys.size;
    const modeLabel = projectionMode === "full" ? "Full view" :
      projectionMode === "overview" ? "Compact overview" : "Focused expansion";
    const overviewBoundary = projectionMode === "overview"
      ? "This overview samples high-level node types and logical layers; statuses and connecting provenance paths can remain hidden. "
      : "";
    projectionStatus.textContent = modeLabel + ": showing " +
      visibleNodeKeys.size + "/" + data.nodes.length + " nodes and " +
      visibleEdgeKeys.size + "/" + data.edges.length + " relationships; " +
      hiddenNodes + " nodes and " + hiddenEdges + " relationships are hidden, not removed. " +
      overviewBoundary +
      (currentRoutingMode === "fast"
        ? "Large-view routing is simplified and edge labels are suppressed except for a selected relationship. Use Focus only or restrictive filters to return to detailed routing."
        : "Detailed obstacle-aware routing is active.");
  }

  function applyVisibility(shouldArrange) {
    visibleNodeKeys.clear();
    data.nodes.forEach(function (node) {
      if (!revealedNodeKeys.has(node.key)) return;
      if (!enabledNodeTypes.has(String(node.type || "other"))) return;
      if (!enabledNodeStatuses.has(String(node.status || "unknown"))) return;
      if (!enabledLayers.has(Number(node.layer))) return;
      visibleNodeKeys.add(node.key);
    });
    if (selected && nodes.has(selected)) visibleNodeKeys.add(selected);
    const selectedRelationship = selectedEdge ? edges.get(selectedEdge) : null;
    if (selectedRelationship) {
      visibleNodeKeys.add(selectedRelationship.source);
      visibleNodeKeys.add(selectedRelationship.target);
    }
    visibleEdgeKeys.clear();
    data.edges.forEach(function (edge) {
      const allowedKind = enabledEdgeKinds.has(String(edge.kind || "dependency"));
      const selectedExact = edge.key === selectedEdge;
      if ((allowedKind || selectedExact) && visibleNodeKeys.has(edge.source) &&
          visibleNodeKeys.has(edge.target)) visibleEdgeKeys.add(edge.key);
    });
    if (shouldArrange && visibleNodeKeys.size) arrangeVisibleNodes();
    nodeElements.forEach(function (element, key) {
      const visible = visibleNodeKeys.has(key);
      setSvgHidden(element, !visible);
      element.setAttribute("aria-hidden", visible ? "false" : "true");
      if (visible) {
        const node = nodes.get(key);
        element.setAttribute("transform", "translate(" + node.x + " " + node.y + ")");
      }
    });
    layerElements.forEach(function (element, layer) {
      setSvgHidden(element, !data.nodes.some(function (node) {
        return node.layer === layer && visibleNodeKeys.has(node.key);
      }));
    });
    updateCanvasSize();
    positionAllEdges();
    const canExpand = Boolean(selected && nodes.has(selected));
    expandUpstream.disabled = !canExpand;
    expandDownstream.disabled = !canExpand;
    expandBoth.disabled = !canExpand;
    collapseFocusButton.disabled = !canExpand;
    projectionOverview.disabled = projectionMode === "overview";
    projectionFull.disabled = usesPersistentFullLayout();
    updateProjectionStatus();
    updateHighlights();
  }

  function boundedCoordinate(value, size, maximumCanvasDimension) {
    if (typeof value !== "number" || !Number.isFinite(value)) return null;
    const upper = Math.max(
      minimumCoordinate, maximumCanvasDimension - size - canvasPadding
    );
    return Math.min(upper, Math.max(minimumCoordinate, Math.round(value)));
  }

  function setManualLayout(active) {
    svg.classList.toggle("ct-manual-layout", Boolean(active));
  }

  function rectanglesOverlap(left, right, gap) {
    return left.x < right.x + right.width + gap &&
      left.x + left.width + gap > right.x &&
      left.y < right.y + right.height + gap &&
      left.y + left.height + gap > right.y;
  }

  function proposedLayoutOverlaps(proposed) {
    const items = data.nodes.map(function (node) {
      const position = proposed.get(node.key) || node;
      return {
        key: node.key, x: position.x, y: position.y,
        width: node.width, height: node.height
      };
    });
    for (let left = 0; left < items.length; left += 1) {
      for (let right = left + 1; right < items.length; right += 1) {
        if (rectanglesOverlap(items[left], items[right], nodeSeparation)) return true;
      }
    }
    return false;
  }

  function nodePositionOverlaps(key, x, y) {
    const node = nodes.get(key);
    if (!node) return true;
    const candidate = {x: x, y: y, width: node.width, height: node.height};
    let overlaps = false;
    nodes.forEach(function (other, otherKey) {
      if (!overlaps && visibleNodeKeys.has(otherKey) && otherKey !== key &&
          rectanglesOverlap(candidate, other, nodeSeparation)) overlaps = true;
    });
    return overlaps;
  }

  function restoreSavedLayout() {
    let raw;
    let restoredFromLegacy = false;
    try {
      raw = window.localStorage.getItem(layoutStorageKey);
      if (raw === null) {
        raw = window.localStorage.getItem(legacyLayoutStorageKey);
        restoredFromLegacy = raw !== null;
      }
    } catch (error) {
      return "unavailable";
    }
    if (raw === null) return "none";
    let saved;
    try {
      saved = JSON.parse(raw);
    } catch (error) {
      return "invalid";
    }
    if (!saved || saved.version !== 1 || saved.layout_id !== data.layout_id ||
        !saved.positions ||
        typeof saved.positions !== "object" || Array.isArray(saved.positions)) {
      return "invalid";
    }
    const proposed = new Map();
    nodes.forEach(function (node, key) {
      if (!Object.prototype.hasOwnProperty.call(saved.positions, key)) return;
      const position = saved.positions[key];
      if (!position || typeof position !== "object" || Array.isArray(position)) return;
      const x = boundedCoordinate(position.x, node.width, maximumCanvasWidth);
      const y = boundedCoordinate(position.y, node.height, maximumCanvasHeight);
      if (x === null || y === null) return;
      proposed.set(key, {x: x, y: y});
    });
    if (!proposed.size || proposedLayoutOverlaps(proposed)) return "invalid";
    proposed.forEach(function (position, key) {
      const node = nodes.get(key);
      node.x = position.x;
      node.y = position.y;
    });
    if (restoredFromLegacy) {
      try {
        window.localStorage.setItem(layoutStorageKey, raw);
        window.localStorage.removeItem(legacyLayoutStorageKey);
      } catch (error) {
        // The arrangement is still usable for this page if browser migration fails.
      }
    }
    return "restored";
  }

  function saveLayout(action) {
    const positions = Object.create(null);
    nodes.forEach(function (node, key) {
      positions[key] = {x: Math.round(node.x), y: Math.round(node.y)};
      fullLayoutPositions.set(key, positions[key]);
    });
    fullLayoutIsManual = true;
    setManualLayout(true);
    try {
      window.localStorage.setItem(layoutStorageKey, JSON.stringify({
        version: 1,
        layout_id: data.layout_id,
        positions: positions
      }));
      layoutStatus.textContent = action +
        " Saved in this browser; the graph, provenance, and logical layers are unchanged.";
      return true;
    } catch (error) {
      layoutStatus.textContent = action +
        " Browser storage is unavailable, so this arrangement lasts only for this page. The graph and provenance are unchanged.";
      return false;
    }
  }

  function viewportClientCenter() {
    const rectangle = graphScroll.getBoundingClientRect();
    return {
      x: rectangle.left + graphScroll.clientLeft + graphScroll.clientWidth / 2,
      y: rectangle.top + graphScroll.clientTop + graphScroll.clientHeight / 2
    };
  }

  function updateZoomControls() {
    const percentage = Math.round(viewportZoom * 100) + "%";
    zoomResetButton.textContent = percentage;
    zoomResetButton.setAttribute(
      "aria-label", "Reset zoom to 100%. Current zoom " + percentage
    );
    zoomResetButton.title = "Reset zoom to 100% (currently " + percentage + ")";
    zoomOutButton.disabled = viewportZoom <= minimumViewportZoom;
    zoomInButton.disabled = viewportZoom >= maximumViewportZoom;
  }

  function applyZoomDimensions() {
    const width = Number(svg.getAttribute("width")) || baseCanvasWidth;
    const height = Number(svg.getAttribute("height")) || baseCanvasHeight;
    svg.style.width = Math.max(1, width * viewportZoom) + "px";
    svg.style.height = Math.max(1, height * viewportZoom) + "px";
    updateZoomControls();
  }

  function setViewportZoom(value, clientX, clientY) {
    if (dragState || panState || !Number.isFinite(value)) return false;
    const nextZoom = Math.round(Math.min(
      maximumViewportZoom, Math.max(minimumViewportZoom, value)
    ) * 10000) / 10000;
    if (nextZoom === viewportZoom) return false;
    const anchor = svgPoint({clientX: clientX, clientY: clientY});
    viewportZoom = nextZoom;
    applyZoomDimensions();
    if (anchor) {
      const matrix = svg.getScreenCTM();
      if (matrix) {
        const point = svg.createSVGPoint();
        point.x = anchor.x;
        point.y = anchor.y;
        const transformed = point.matrixTransform(matrix);
        graphScroll.scrollLeft += transformed.x - clientX;
        graphScroll.scrollTop += transformed.y - clientY;
      }
    }
    return true;
  }

  function zoomViewportBy(factor) {
    const center = viewportClientCenter();
    return setViewportZoom(
      viewportZoom * factor, center.x, center.y
    );
  }

  function fitViewport() {
    if (dragState || panState) return false;
    const width = Number(svg.getAttribute("width")) || baseCanvasWidth;
    const height = Number(svg.getAttribute("height")) || baseCanvasHeight;
    const availableWidth = Math.max(1, graphScroll.clientWidth - 24);
    const availableHeight = Math.max(1, graphScroll.clientHeight - 24);
    viewportZoom = Math.round(Math.min(
      maximumViewportZoom,
      Math.max(minimumViewportZoom, Math.min(
        availableWidth / width, availableHeight / height
      ))
    ) * 10000) / 10000;
    applyZoomDimensions();
    graphScroll.scrollLeft = 0;
    graphScroll.scrollTop = 0;
    return true;
  }

  function updateCanvasSize() {
    let width = largeGraph ? 640 : baseCanvasWidth;
    let height = largeGraph ? 480 : baseCanvasHeight;
    nodes.forEach(function (node, key) {
      if (!visibleNodeKeys.has(key)) return;
      width = Math.max(width, node.x + node.width + canvasPadding);
      height = Math.max(height, node.y + node.height + canvasPadding);
    });
    width = Math.min(maximumCanvasWidth, Math.ceil(width));
    height = Math.min(maximumCanvasHeight, Math.ceil(height));
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    svg.setAttribute("width", width);
    svg.setAttribute("height", height);
    applyZoomDimensions();
  }

  function prepareDragCanvas(node) {
    const currentWidth = Number(svg.getAttribute("width")) || baseCanvasWidth;
    const currentHeight = Number(svg.getAttribute("height")) || baseCanvasHeight;
    const width = Math.min(
      maximumCanvasWidth,
      Math.max(currentWidth, node.x + node.width + canvasPadding + 512)
    );
    const height = Math.min(
      maximumCanvasHeight,
      Math.max(currentHeight, node.y + node.height + canvasPadding + 384)
    );
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    svg.setAttribute("width", width);
    svg.setAttribute("height", height);
    applyZoomDimensions();
  }

  function pointKey(point) {
    return point.x + "|" + point.y;
  }

  function samePoint(left, right) {
    return left.x === right.x && left.y === right.y;
  }

  function appendRoutePoint(points, point) {
    const clean = {x: point.x, y: point.y};
    if (!points.length || !samePoint(points[points.length - 1], clean)) points.push(clean);
  }

  function simplifyRoute(points) {
    const simplified = [];
    points.forEach(function (point) {
      appendRoutePoint(simplified, point);
      while (simplified.length >= 3) {
        const first = simplified[simplified.length - 3];
        const middle = simplified[simplified.length - 2];
        const last = simplified[simplified.length - 1];
        if ((first.x === middle.x && middle.x === last.x) ||
            (first.y === middle.y && middle.y === last.y)) {
          simplified.splice(simplified.length - 2, 1);
        } else {
          break;
        }
      }
    });
    return simplified;
  }

  function clearEscapeDistance(node, side, coordinate, desiredDistance) {
    const activeNodes = arguments.length > 4 ? arguments[4] : data.nodes;
    let available = desiredDistance;
    (activeNodes || data.nodes).forEach(function (other) {
      if (other === node) return;
      if (side === "left" || side === "right") {
        if (coordinate <= other.y - routingClearance ||
            coordinate >= other.y + other.height + routingClearance) return;
        const anchor = side === "left" ? node.x : node.x + node.width;
        const boundary = side === "left"
          ? other.x + other.width + routingClearance
          : other.x - routingClearance;
        const distance = side === "left" ? anchor - boundary : boundary - anchor;
        if (distance >= 0) available = Math.min(available, distance);
        return;
      }
      if (coordinate <= other.x - routingClearance ||
          coordinate >= other.x + other.width + routingClearance) return;
      const anchor = side === "top" ? node.y : node.y + node.height;
      const boundary = side === "top"
        ? other.y + other.height + routingClearance
        : other.y - routingClearance;
      const distance = side === "top" ? anchor - boundary : boundary - anchor;
      if (distance >= 0) available = Math.min(available, distance);
    });
    return Math.max(routingClearance, available);
  }

  function portFor(node, side, laneOffset, escapeOffset, activeNodes) {
    const sideInset = 14;
    const desiredEscape = Math.min(
      canvasPadding - 2, edgeApproachLength + (escapeOffset || 0)
    );
    if (side === "left" || side === "right") {
      const y = Math.max(
        node.y + sideInset,
        Math.min(node.y + node.height - sideInset,
          node.y + node.height / 2 + laneOffset)
      );
      const anchorX = side === "left" ? node.x : node.x + node.width;
      const direction = side === "left" ? -1 : 1;
      const escapeDistance = clearEscapeDistance(
        node, side, y, desiredEscape, activeNodes
      );
      return {
        anchor: {x: anchorX, y: y},
        tip: {x: anchorX + direction * edgeArrowGap, y: y},
        escape: {
          x: side === "left" ? Math.max(2, anchorX - escapeDistance)
            : anchorX + escapeDistance,
          y: y
        }
      };
    }
    const x = Math.max(
      node.x + sideInset,
      Math.min(node.x + node.width - sideInset,
        node.x + node.width / 2 + laneOffset)
    );
    const anchorY = side === "top" ? node.y : node.y + node.height;
    const direction = side === "top" ? -1 : 1;
    const escapeDistance = clearEscapeDistance(
      node, side, x, desiredEscape, activeNodes
    );
    return {
      anchor: {x: x, y: anchorY},
      tip: {x: x, y: anchorY + direction * edgeArrowGap},
      escape: {
        x: x,
        y: side === "top" ? Math.max(2, anchorY - escapeDistance)
          : anchorY + escapeDistance
      }
    };
  }

  function edgePortSides(edge) {
    const source = nodes.get(edge.source);
    const target = nodes.get(edge.target);
    if (!source || !target) return null;
    if (source === target) {
      return {source: "right", target: "top"};
    }
    const deltaX = target.x + target.width / 2 - (source.x + source.width / 2);
    const deltaY = target.y + target.height / 2 - (source.y + source.height / 2);
    if (Math.abs(deltaX) >= Math.abs(deltaY)) {
      return deltaX >= 0
        ? {source: "right", target: "left"}
        : {source: "left", target: "right"};
    }
    return deltaY >= 0
      ? {source: "bottom", target: "top"}
      : {source: "top", target: "bottom"};
  }

  function buildEdgePorts() {
    const edgeItems = arguments.length > 0 ? arguments[0] : data.edges.filter(function (edge) {
      return visibleEdgeKeys.has(edge.key);
    });
    const activeNodes = arguments.length > 1 ? arguments[1] : data.nodes.filter(function (node) {
      return visibleNodeKeys.has(node.key);
    });
    const assignments = new Map();
    const groups = new Map();
    edgeItems.slice().sort(function (left, right) {
      return left.key.localeCompare(right.key);
    }).forEach(function (edge) {
      const sides = edgePortSides(edge);
      if (!sides) return;
      assignments.set(edge.key, {sides: sides});
      [
        {role: "source", nodeKey: edge.source, side: sides.source},
        {role: "target", nodeKey: edge.target, side: sides.target}
      ].forEach(function (endpoint) {
        const groupKey = endpoint.nodeKey + "|" + endpoint.side;
        if (!groups.has(groupKey)) groups.set(groupKey, []);
        groups.get(groupKey).push({edgeKey: edge.key, role: endpoint.role});
      });
    });
    groups.forEach(function (items, groupKey) {
      items.sort(function (left, right) {
        return left.edgeKey.localeCompare(right.edgeKey) ||
          left.role.localeCompare(right.role);
      });
      const separator = groupKey.lastIndexOf("|");
      const nodeKey = groupKey.slice(0, separator);
      const side = groupKey.slice(separator + 1);
      const node = nodes.get(nodeKey);
      if (!node) return;
      const sideInset = 14;
      const span = side === "left" || side === "right"
        ? Math.max(0, node.height - sideInset * 2)
        : Math.max(0, node.width - sideInset * 2);
      const spacing = items.length > 1
        ? Math.min(routingLaneSeparation, span / (items.length - 1)) : 0;
      const escapeSpacing = items.length > 1
        ? Math.min(14, 56 / (items.length - 1)) : 0;
      items.forEach(function (item, index) {
        const offset = (index - (items.length - 1) / 2) * spacing;
        const assignment = assignments.get(item.edgeKey);
        assignment[item.role] = portFor(
          node, side, offset, index * escapeSpacing, activeNodes
        );
      });
    });
    const ports = new Map();
    assignments.forEach(function (assignment, edgeKey) {
      if (assignment.source && assignment.target) {
        ports.set(edgeKey, {
          source: assignment.source,
          target: assignment.target
        });
      }
    });
    return ports;
  }

  function routingObstacles(activeNodes) {
    const canvasWidth = Number(svg.getAttribute("width")) || baseCanvasWidth;
    const canvasHeight = Number(svg.getAttribute("height")) || baseCanvasHeight;
    return activeNodes.map(function (node) {
      return {
        key: node.key,
        left: Math.max(2, node.x - routingClearance),
        right: Math.min(canvasWidth - 2, node.x + node.width + routingClearance),
        top: Math.max(2, node.y - routingClearance),
        bottom: Math.min(canvasHeight - 2, node.y + node.height + routingClearance)
      };
    });
  }

  function pointInsideObstacle(point, obstacles) {
    return obstacles.some(function (obstacle) {
      return point.x > obstacle.left && point.x < obstacle.right &&
        point.y > obstacle.top && point.y < obstacle.bottom;
    });
  }

  function segmentBlocked(left, right, obstacles) {
    if (left.x === right.x) {
      const low = Math.min(left.y, right.y);
      const high = Math.max(left.y, right.y);
      return obstacles.some(function (obstacle) {
        return left.x > obstacle.left && left.x < obstacle.right &&
          high > obstacle.top && low < obstacle.bottom;
      });
    }
    if (left.y === right.y) {
      const low = Math.min(left.x, right.x);
      const high = Math.max(left.x, right.x);
      return obstacles.some(function (obstacle) {
        return left.y > obstacle.top && left.y < obstacle.bottom &&
          high > obstacle.left && low < obstacle.right;
      });
    }
    return true;
  }

  function addRoutingNeighbor(neighbors, left, right, direction, obstacles) {
    if (segmentBlocked(left, right, obstacles)) return;
    const leftKey = pointKey(left);
    const rightKey = pointKey(right);
    const length = Math.abs(left.x - right.x) + Math.abs(left.y - right.y);
    if (!length) return;
    if (!neighbors.has(leftKey)) neighbors.set(leftKey, []);
    if (!neighbors.has(rightKey)) neighbors.set(rightKey, []);
    neighbors.get(leftKey).push({key: rightKey, direction: direction, length: length});
    neighbors.get(rightKey).push({key: leftKey, direction: direction, length: length});
  }

  function nearbySegmentUsage(left, right, usedSegments) {
    const horizontal = left.y === right.y;
    const low = horizontal
      ? Math.min(left.x, right.x) : Math.min(left.y, right.y);
    const high = horizontal
      ? Math.max(left.x, right.x) : Math.max(left.y, right.y);
    let usage = 0;
    usedSegments.forEach(function (segment) {
      const segmentHorizontal = segment.left.y === segment.right.y;
      if (horizontal !== segmentHorizontal) return;
      const distance = horizontal
        ? Math.abs(left.y - segment.left.y)
        : Math.abs(left.x - segment.left.x);
      if (distance >= routingLaneSeparation) return;
      const segmentLow = horizontal
        ? Math.min(segment.left.x, segment.right.x)
        : Math.min(segment.left.y, segment.right.y);
      const segmentHigh = horizontal
        ? Math.max(segment.left.x, segment.right.x)
        : Math.max(segment.left.y, segment.right.y);
      if (Math.min(high, segmentHigh) > Math.max(low, segmentLow)) usage += 1;
    });
    return usage;
  }

  function estimateDetailedRoutingWork(activeNodes, activeEdges) {
    const obstacles = routingObstacles(activeNodes);
    const ports = buildEdgePorts(activeEdges, activeNodes);
    const canvasWidth = Number(svg.getAttribute("width")) || baseCanvasWidth;
    const canvasHeight = Number(svg.getAttribute("height")) || baseCanvasHeight;
    const xValues = new Set([2, canvasWidth - 2]);
    const yValues = new Set([2, canvasHeight - 2]);
    obstacles.forEach(function (obstacle) {
      xValues.add(obstacle.left);
      xValues.add(obstacle.right);
      yValues.add(obstacle.top);
      yValues.add(obstacle.bottom);
    });
    ports.forEach(function (pair) {
      [pair.source.escape, pair.target.escape].forEach(function (point) {
        xValues.add(point.x);
        yValues.add(point.y);
      });
    });
    const gridPoints = xValues.size * yValues.size;
    const routeWork = gridPoints * Math.max(1, activeEdges.length);
    return {
      obstacles: obstacles,
      ports: ports,
      gridPoints: gridPoints,
      routeWork: routeWork,
      safe: gridPoints <= maximumDetailedGridPoints &&
        routeWork <= maximumDetailedRouteWork
    };
  }

  function buildRoutingContext() {
    const activeNodes = arguments.length > 0 ? arguments[0] : data.nodes.filter(function (node) {
      return visibleNodeKeys.has(node.key);
    });
    const activeEdges = arguments.length > 1 ? arguments[1] : data.edges.filter(function (edge) {
      return visibleEdgeKeys.has(edge.key);
    });
    const prepared = arguments.length > 2
      ? arguments[2] : estimateDetailedRoutingWork(activeNodes, activeEdges);
    const obstacles = prepared.obstacles;
    const canvasWidth = Number(svg.getAttribute("width")) || baseCanvasWidth;
    const canvasHeight = Number(svg.getAttribute("height")) || baseCanvasHeight;
    const xValues = new Set([2, canvasWidth - 2]);
    const yValues = new Set([2, canvasHeight - 2]);
    const ports = prepared.ports;
    obstacles.forEach(function (obstacle) {
      xValues.add(obstacle.left);
      xValues.add(obstacle.right);
      yValues.add(obstacle.top);
      yValues.add(obstacle.bottom);
    });
    ports.forEach(function (pair) {
      [pair.source.escape, pair.target.escape].forEach(function (point) {
        xValues.add(point.x);
        yValues.add(point.y);
      });
    });
    const xs = Array.from(xValues).sort(function (left, right) { return left - right; });
    const ys = Array.from(yValues).sort(function (left, right) { return left - right; });
    const points = new Map();
    const rows = new Map();
    const columns = new Map();
    ys.forEach(function (y) {
      xs.forEach(function (x) {
        const point = {x: x, y: y};
        if (pointInsideObstacle(point, obstacles)) return;
        const key = pointKey(point);
        points.set(key, point);
        if (!rows.has(y)) rows.set(y, []);
        if (!columns.has(x)) columns.set(x, []);
        rows.get(y).push(point);
        columns.get(x).push(point);
      });
    });
    const neighbors = new Map();
    rows.forEach(function (row) {
      row.sort(function (left, right) { return left.x - right.x; });
      for (let index = 1; index < row.length; index += 1) {
        addRoutingNeighbor(neighbors, row[index - 1], row[index], "h", obstacles);
      }
    });
    columns.forEach(function (column) {
      column.sort(function (left, right) { return left.y - right.y; });
      for (let index = 1; index < column.length; index += 1) {
        addRoutingNeighbor(neighbors, column[index - 1], column[index], "v", obstacles);
      }
    });
    neighbors.forEach(function (items) {
      items.sort(function (left, right) {
        return left.key.localeCompare(right.key) ||
          left.direction.localeCompare(right.direction);
      });
    });
    return {
      obstacles: obstacles, ports: ports, points: points, neighbors: neighbors,
      usedSegments: [], activeNodes: activeNodes
    };
  }

  function heapBefore(left, right) {
    return left.score < right.score ||
      (left.score === right.score && (left.cost < right.cost ||
        (left.cost === right.cost && left.state < right.state)));
  }

  function heapPush(heap, item) {
    heap.push(item);
    let index = heap.length - 1;
    while (index > 0) {
      const parent = Math.floor((index - 1) / 2);
      if (!heapBefore(heap[index], heap[parent])) break;
      const temporary = heap[parent];
      heap[parent] = heap[index];
      heap[index] = temporary;
      index = parent;
    }
  }

  function heapPop(heap) {
    if (!heap.length) return null;
    const first = heap[0];
    const last = heap.pop();
    if (heap.length) {
      heap[0] = last;
      let index = 0;
      while (true) {
        const left = index * 2 + 1;
        const right = left + 1;
        let smallest = index;
        if (left < heap.length && heapBefore(heap[left], heap[smallest])) smallest = left;
        if (right < heap.length && heapBefore(heap[right], heap[smallest])) smallest = right;
        if (smallest === index) break;
        const temporary = heap[index];
        heap[index] = heap[smallest];
        heap[smallest] = temporary;
        index = smallest;
      }
    }
    return first;
  }

  function routeBetween(start, target, context) {
    const startKey = pointKey(start);
    const targetKey = pointKey(target);
    if (!context.points.has(startKey) || !context.points.has(targetKey)) return null;
    if (startKey === targetKey) return [start];
    const startState = startKey + "@s";
    const distance = new Map([[startState, 0]]);
    const previous = new Map();
    const heap = [];
    heapPush(heap, {
      state: startState, point: startKey, direction: "s", cost: 0,
      score: Math.abs(start.x - target.x) + Math.abs(start.y - target.y)
    });
    let finalState = null;
    while (heap.length) {
      const current = heapPop(heap);
      if (current.cost !== distance.get(current.state)) continue;
      if (current.point === targetKey) {
        finalState = current.state;
        break;
      }
      (context.neighbors.get(current.point) || []).forEach(function (neighbor) {
        const bend = current.direction !== "s" && current.direction !== neighbor.direction
          ? routingBendPenalty : 0;
        const congestion = nearbySegmentUsage(
          context.points.get(current.point),
          context.points.get(neighbor.key),
          context.usedSegments
        );
        const cost = current.cost + neighbor.length + bend +
          congestion * routingCongestionPenalty;
        const state = neighbor.key + "@" + neighbor.direction;
        const known = distance.get(state);
        if (known !== undefined && known <= cost) return;
        distance.set(state, cost);
        previous.set(state, current.state);
        const point = context.points.get(neighbor.key);
        heapPush(heap, {
          state: state, point: neighbor.key, direction: neighbor.direction,
          cost: cost,
          score: cost + Math.abs(point.x - target.x) + Math.abs(point.y - target.y)
        });
      });
    }
    if (!finalState) return null;
    const reversed = [];
    let state = finalState;
    while (state) {
      const key = state.slice(0, -2);
      reversed.push(context.points.get(key));
      state = previous.get(state);
    }
    const route = reversed.reverse();
    for (let index = 1; index < route.length; index += 1) {
      context.usedSegments.push({
        left: route[index - 1], right: route[index]
      });
    }
    return route;
  }

  function routeForEdge(edge, context) {
    const pair = context.ports.get(edge.key);
    if (!pair) return null;
    const core = routeBetween(pair.source.escape, pair.target.escape, context);
    if (!core) return null;
    const points = [];
    appendRoutePoint(points, pair.source.anchor);
    appendRoutePoint(points, pair.source.escape);
    core.forEach(function (point) { appendRoutePoint(points, point); });
    appendRoutePoint(points, pair.target.escape);
    appendRoutePoint(points, pair.target.tip);
    return simplifyRoute(points);
  }

  function pointToward(origin, target, distance) {
    if (origin.x === target.x) {
      return {
        x: origin.x,
        y: origin.y + Math.sign(target.y - origin.y) * distance
      };
    }
    return {
      x: origin.x + Math.sign(target.x - origin.x) * distance,
      y: origin.y
    };
  }

  function roundedCornerClear(before, corner, after) {
    const activeNodes = arguments.length > 3 ? arguments[3] : data.nodes.filter(function (node) {
      return visibleNodeKeys.has(node.key);
    });
    const candidate = {
      x: Math.min(before.x, corner.x, after.x),
      y: Math.min(before.y, corner.y, after.y),
      width: Math.max(before.x, corner.x, after.x) -
        Math.min(before.x, corner.x, after.x),
      height: Math.max(before.y, corner.y, after.y) -
        Math.min(before.y, corner.y, after.y)
    };
    let clear = true;
    activeNodes.forEach(function (node) {
      if (clear && rectanglesOverlap(candidate, node, 2)) clear = false;
    });
    return clear;
  }

  function routePath(points, activeNodes) {
    if (!points || !points.length) return "";
    let path = "M" + points[0].x + "," + points[0].y;
    for (let index = 1; index < points.length - 1; index += 1) {
      const previous = points[index - 1];
      const corner = points[index];
      const next = points[index + 1];
      const incoming = Math.abs(previous.x - corner.x) +
        Math.abs(previous.y - corner.y);
      const outgoing = Math.abs(next.x - corner.x) +
        Math.abs(next.y - corner.y);
      const isTurn = (previous.x === corner.x && corner.y === next.y) ||
        (previous.y === corner.y && corner.x === next.x);
      const radius = Math.min(edgeCornerRadius, incoming / 2, outgoing / 2);
      if (!isTurn || radius < 1) {
        path += " L" + corner.x + "," + corner.y;
        continue;
      }
      const before = pointToward(corner, previous, radius);
      const after = pointToward(corner, next, radius);
      if (!roundedCornerClear(before, corner, after, activeNodes)) {
        path += " L" + corner.x + "," + corner.y;
        continue;
      }
      path += " L" + before.x + "," + before.y +
        " Q" + corner.x + "," + corner.y +
        " " + after.x + "," + after.y;
    }
    const last = points[points.length - 1];
    return path + " L" + last.x + "," + last.y;
  }

  function boxClearOfNodes(box, activeNodes) {
    const candidate = {
      x: box.left, y: box.top,
      width: box.right - box.left, height: box.bottom - box.top
    };
    let clear = true;
    activeNodes.forEach(function (node) {
      if (clear && rectanglesOverlap(candidate, node, 2)) clear = false;
    });
    return clear;
  }

  function edgeLabelPlacement(points, text, activeNodes) {
    const canvasWidth = Number(svg.getAttribute("width")) || baseCanvasWidth;
    const canvasHeight = Number(svg.getAttribute("height")) || baseCanvasHeight;
    const width = Math.min(220, Math.max(36, text.length * 5.8 + 10));
    const segments = [];
    for (let index = 1; index < points.length; index += 1) {
      const left = points[index - 1];
      const right = points[index];
      segments.push({
        left: left, right: right,
        horizontal: left.y === right.y,
        length: Math.abs(left.x - right.x) + Math.abs(left.y - right.y),
        index: index
      });
    }
    segments.sort(function (left, right) {
      return right.length - left.length ||
        Number(right.horizontal) - Number(left.horizontal) ||
        left.index - right.index;
    });
    for (const segment of segments) {
      const centerX = (segment.left.x + segment.right.x) / 2;
      const centerY = (segment.left.y + segment.right.y) / 2;
      const candidates = segment.horizontal
        ? [
            {x: centerX, y: centerY - 8, anchor: "middle",
             box: {left: centerX - width / 2, right: centerX + width / 2,
                   top: centerY - 20, bottom: centerY - 3}},
            {x: centerX, y: centerY + 17, anchor: "middle",
             box: {left: centerX - width / 2, right: centerX + width / 2,
                   top: centerY + 3, bottom: centerY + 20}}
          ]
        : [
            {x: centerX + 8, y: centerY + 3, anchor: "start",
             box: {left: centerX + 6, right: centerX + 6 + width,
                   top: centerY - 8, bottom: centerY + 9}},
            {x: centerX - 8, y: centerY + 3, anchor: "end",
             box: {left: centerX - 6 - width, right: centerX - 6,
                   top: centerY - 8, bottom: centerY + 9}}
          ];
      for (const candidate of candidates) {
        if (candidate.box.left < 2 || candidate.box.top < 2 ||
            candidate.box.right > canvasWidth - 2 ||
            candidate.box.bottom > canvasHeight - 2) continue;
        if (boxClearOfNodes(candidate.box, activeNodes)) return candidate;
      }
    }
    return null;
  }

  function edgeDisplayLabel(edge) {
    return edge.kind === "receipt" ? "receipt" : (edge.label || edge.relation);
  }

  function simplePort(node, side, targetRole) {
    let point;
    if (side === "left") point = {x: node.x, y: node.y + node.height / 2};
    else if (side === "right") point = {x: node.x + node.width, y: node.y + node.height / 2};
    else if (side === "top") point = {x: node.x + node.width / 2, y: node.y};
    else point = {x: node.x + node.width / 2, y: node.y + node.height};
    if (!targetRole) return point;
    if (side === "left") point.x -= edgeArrowGap;
    else if (side === "right") point.x += edgeArrowGap;
    else if (side === "top") point.y -= edgeArrowGap;
    else point.y += edgeArrowGap;
    return point;
  }

  function fastRouteForEdge(edge) {
    const source = nodes.get(edge.source);
    const target = nodes.get(edge.target);
    const sides = edgePortSides(edge);
    if (!source || !target || !sides) return null;
    const start = simplePort(source, sides.source, false);
    const finish = simplePort(target, sides.target, true);
    const routeIndex = parseInt(stableHash(edge.key).slice(-2), 16);
    const lane = ((routeIndex % 9) - 4) * 4;
    if (source === target) {
      const right = source.x + source.width + 28 + Math.abs(lane);
      const top = Math.max(8, source.y - 28 - Math.abs(lane));
      return simplifyRoute([
        start, {x: right, y: start.y}, {x: right, y: top},
        {x: finish.x, y: top}, finish
      ]);
    }
    if (sides.source === "left" || sides.source === "right") {
      const middleX = (start.x + finish.x) / 2 + lane;
      return simplifyRoute([
        start, {x: middleX, y: start.y}, {x: middleX, y: finish.y}, finish
      ]);
    }
    const middleY = (start.y + finish.y) / 2 + lane;
    return simplifyRoute([
      start, {x: start.x, y: middleY}, {x: finish.x, y: middleY}, finish
    ]);
  }

  function fastLabelPlacement(points) {
    let best = null;
    for (let index = 1; index < points.length; index += 1) {
      const left = points[index - 1];
      const right = points[index];
      const length = Math.abs(left.x - right.x) + Math.abs(left.y - right.y);
      if (!best || length > best.length) best = {left: left, right: right, length: length};
    }
    if (!best) return null;
    return {
      x: (best.left.x + best.right.x) / 2,
      y: (best.left.y + best.right.y) / 2 - 7,
      anchor: "middle"
    };
  }

  function positionEdge(edge, context, fastRouting, activeNodes, showLabels, index) {
    const elements = edgeElements.get(edge.key);
    if (!elements) return;
    const points = fastRouting
      ? fastRouteForEdge(edge)
      : routeForEdge(edge, context);
    if (!points) {
      setSvgHidden(elements.group, true);
      return;
    }
    setSvgHidden(elements.group, false);
    const pathData = routePath(points, activeNodes);
    elements.path.setAttribute("d", pathData);
    elements.hit.setAttribute("d", pathData);
    elements.group.setAttribute("data-route-points", JSON.stringify(points));
    const placement = fastRouting
      ? fastLabelPlacement(points)
      : edgeLabelPlacement(points, edgeDisplayLabel(edge), activeNodes);
    elements.group.setAttribute("data-label-available", placement ? "true" : "false");
    elements.group.setAttribute("data-label-default-visible", showLabels ? "true" : "false");
    setSvgHidden(elements.label,
      !placement || (!showLabels && edge.key !== selectedEdge));
    if (placement) {
      elements.label.setAttribute("x", placement.x);
      elements.label.setAttribute("y", placement.y);
      elements.label.setAttribute("text-anchor", placement.anchor);
    }
  }

  function positionAllEdges() {
    // positionEdge delegates large visible projections to fastRouteForEdge.
    edgeElements.forEach(function (elements) {
      setSvgHidden(elements.group, true);
      setSvgHidden(elements.label, true);
    });
    const activeNodes = data.nodes.filter(function (node) {
      return visibleNodeKeys.has(node.key);
    });
    const activeEdges = data.edges.filter(function (edge) {
      return visibleEdgeKeys.has(edge.key);
    });
    let fastRouting = activeNodes.length > detailedRoutingNodeThreshold ||
      activeEdges.length > fullViewFastRoutingThreshold;
    let routingEstimate = null;
    if (!fastRouting && activeEdges.length) {
      routingEstimate = estimateDetailedRoutingWork(activeNodes, activeEdges);
      fastRouting = !routingEstimate.safe;
    }
    currentRoutingMode = fastRouting ? "fast" : "detailed";
    const context = fastRouting || !activeEdges.length
      ? null : buildRoutingContext(activeNodes, activeEdges, routingEstimate);
    const showLabels = !fastRouting && activeEdges.length <= 80;
    activeEdges.slice().sort(function (left, right) {
      return left.key.localeCompare(right.key);
    }).forEach(function (edge, index) {
      positionEdge(edge, context, fastRouting, activeNodes, showLabels, index);
    });
  }

  function moveNode(key, x, y, resizeCanvas) {
    const node = nodes.get(key);
    const element = nodeElements.get(key);
    if (!node || !element) return false;
    const boundedX = boundedCoordinate(x, node.width, maximumCanvasWidth);
    const boundedY = boundedCoordinate(y, node.height, maximumCanvasHeight);
    if (boundedX === null || boundedY === null) return false;
    if (node.x === boundedX && node.y === boundedY) return false;
    if (nodePositionOverlaps(key, boundedX, boundedY)) return false;
    node.x = boundedX;
    node.y = boundedY;
    element.setAttribute("transform", "translate(" + node.x + " " + node.y + ")");
    if (resizeCanvas !== false) {
      updateCanvasSize();
      positionAllEdges();
    }
    return true;
  }

  function svgPoint(event, inverseMatrix) {
    let inverse = inverseMatrix;
    if (!inverse) {
      const matrix = svg.getScreenCTM();
      if (!matrix) return null;
      try {
        inverse = matrix.inverse();
      } catch (error) {
        return null;
      }
    }
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    return point.matrixTransform(inverse);
  }

  function centerNodeInViewport(node) {
    const matrix = svg.getScreenCTM();
    if (!matrix) return;
    const point = svg.createSVGPoint();
    point.x = node.x + node.width / 2;
    point.y = node.y + node.height / 2;
    const transformed = point.matrixTransform(matrix);
    const center = viewportClientCenter();
    graphScroll.scrollLeft += transformed.x - center.x;
    graphScroll.scrollTop += transformed.y - center.y;
  }

  function beginDrag(event, node, group) {
    if (dragState || event.button !== 0) return;
    prepareDragCanvas(node);
    const matrix = svg.getScreenCTM();
    if (!matrix) {
      updateCanvasSize();
      return;
    }
    let inverseMatrix;
    try {
      inverseMatrix = matrix.inverse();
    } catch (error) {
      updateCanvasSize();
      return;
    }
    const point = svgPoint(event, inverseMatrix);
    if (!point) {
      updateCanvasSize();
      return;
    }
    const nextDrag = {
      pointerId: event.pointerId,
      key: node.key,
      group: group,
      inverseMatrix: inverseMatrix,
      startPointX: point.x,
      startPointY: point.y,
      startNodeX: node.x,
      startNodeY: node.y,
      startClientX: event.clientX,
      startClientY: event.clientY,
      moved: false
    };
    try {
      group.setPointerCapture(event.pointerId);
    } catch (error) {
      updateCanvasSize();
      layoutStatus.textContent =
        "This node could not capture the pointer, so the move was not started. The graph is unchanged.";
      return;
    }
    dragState = nextDrag;
  }

  function continueDrag(event) {
    if (!dragState || dragState.pointerId !== event.pointerId) return;
    if (!dragState.moved && Math.hypot(
      event.clientX - dragState.startClientX,
      event.clientY - dragState.startClientY
    ) < dragThreshold) return;
    const point = svgPoint(event, dragState.inverseMatrix);
    if (!point) return;
    const x = dragState.startNodeX + point.x - dragState.startPointX;
    const y = dragState.startNodeY + point.y - dragState.startPointY;
    if (!moveNode(dragState.key, x, y, false)) return;
    dragState.moved = true;
    dragState.group.classList.add("ct-dragging");
    event.preventDefault();
  }

  function finishDrag(event) {
    if (!dragState || dragState.pointerId !== event.pointerId) return;
    const finished = dragState;
    dragState = null;
    finished.group.classList.remove("ct-dragging");
    if (finished.group.hasPointerCapture(event.pointerId)) {
      finished.group.releasePointerCapture(event.pointerId);
    }
    updateCanvasSize();
    positionAllEdges();
    if (!finished.moved) return;
    suppressClickKey = finished.key;
    window.setTimeout(function () {
      if (suppressClickKey === finished.key) suppressClickKey = null;
    }, 0);
    selectNode(finished.key, false);
    const node = nodes.get(finished.key);
    if (usesPersistentFullLayout()) {
      saveLayout("Moved " + short(node.label, 60) + " to x " + node.x + ", y " + node.y + ".");
    } else {
      layoutStatus.textContent =
        "Moved this node in the current projection only. The saved full layout, graph, and provenance are unchanged.";
    }
  }

  function cancelDrag(event) {
    if (!dragState || dragState.pointerId !== event.pointerId) return;
    const cancelled = dragState;
    dragState = null;
    cancelled.group.classList.remove("ct-dragging");
    if (cancelled.group.hasPointerCapture(event.pointerId)) {
      cancelled.group.releasePointerCapture(event.pointerId);
    }
    if (cancelled.moved) {
      moveNode(cancelled.key, cancelled.startNodeX, cancelled.startNodeY);
    } else {
      updateCanvasSize();
      positionAllEdges();
    }
    layoutStatus.textContent =
      "Move cancelled; the prior browser layout was restored and the graph was not changed.";
  }

  function nudgeSelected(deltaX, deltaY, direction) {
    const node = selected ? nodes.get(selected) : null;
    if (!node) return;
    if (moveNode(node.key, node.x + deltaX, node.y + deltaY)) {
      if (usesPersistentFullLayout()) {
        saveLayout("Moved " + short(node.label, 60) + " " + direction +
          " to x " + node.x + ", y " + node.y + ".");
      } else {
        layoutStatus.textContent =
          "Moved this node " + direction + " in the current projection only. The saved full layout, graph, and provenance are unchanged.";
      }
    }
  }

  function panTargetIsInteractive(target) {
    return Boolean(target && typeof target.closest === "function" &&
      target.closest(".ct-node, .ct-edge-group"));
  }

  function beginPan(event) {
    if (event.button !== 0 || dragState || panState ||
        panTargetIsInteractive(event.target)) return;
    const rectangle = graphScroll.getBoundingClientRect();
    const contentLeft = rectangle.left + graphScroll.clientLeft;
    const contentTop = rectangle.top + graphScroll.clientTop;
    if (event.clientX < contentLeft || event.clientY < contentTop ||
        event.clientX > contentLeft + graphScroll.clientWidth ||
        event.clientY > contentTop + graphScroll.clientHeight) return;
    const nextPan = {
      pointerId: event.pointerId,
      startClientX: event.clientX,
      startClientY: event.clientY,
      startScrollLeft: graphScroll.scrollLeft,
      startScrollTop: graphScroll.scrollTop,
      moved: false
    };
    try {
      graphScroll.setPointerCapture(event.pointerId);
    } catch (error) {
      return;
    }
    panState = nextPan;
    graphScroll.classList.add("ct-panning");
    graphScroll.focus({preventScroll: true});
    event.preventDefault();
  }

  function continuePan(event) {
    if (!panState || panState.pointerId !== event.pointerId) return;
    const deltaX = event.clientX - panState.startClientX;
    const deltaY = event.clientY - panState.startClientY;
    if (!panState.moved && Math.hypot(deltaX, deltaY) < dragThreshold) return;
    panState.moved = true;
    graphScroll.scrollLeft = panState.startScrollLeft - deltaX;
    graphScroll.scrollTop = panState.startScrollTop - deltaY;
    event.preventDefault();
  }

  function finishPan(event) {
    if (!panState || panState.pointerId !== event.pointerId) return;
    const finished = panState;
    panState = null;
    graphScroll.classList.remove("ct-panning");
    if (graphScroll.hasPointerCapture(event.pointerId)) {
      graphScroll.releasePointerCapture(event.pointerId);
    }
    if (finished.moved) event.preventDefault();
  }

  function cancelPan(event) {
    if (!panState || panState.pointerId !== event.pointerId) return;
    panState = null;
    graphScroll.classList.remove("ct-panning");
    if (graphScroll.hasPointerCapture(event.pointerId)) {
      graphScroll.releasePointerCapture(event.pointerId);
    }
  }

  function handleViewportWheel(event) {
    if (event.deltaY === 0) return;
    event.preventDefault();
    if (dragState || panState) return;
    const modeScale = event.deltaMode === 1 ? 16 :
      event.deltaMode === 2 ? graphScroll.clientHeight : 1;
    const delta = Math.max(-240, Math.min(240, event.deltaY * modeScale));
    setViewportZoom(
      viewportZoom * Math.exp(-delta * 0.0015),
      event.clientX,
      event.clientY
    );
  }

  function handleViewportKeydown(event) {
    if (event.target !== graphScroll) return;
    let handled = true;
    if (event.key === "+" || event.key === "=") {
      zoomViewportBy(viewportZoomStep);
    } else if (event.key === "-" || event.key === "_") {
      zoomViewportBy(1 / viewportZoomStep);
    } else if (event.key === "0") {
      const center = viewportClientCenter();
      setViewportZoom(1, center.x, center.y);
    } else if (event.key === "f" || event.key === "F") {
      fitViewport();
    } else if (event.key === "ArrowLeft") {
      graphScroll.scrollLeft -= 64;
    } else if (event.key === "ArrowRight") {
      graphScroll.scrollLeft += 64;
    } else if (event.key === "ArrowUp") {
      graphScroll.scrollTop -= 64;
    } else if (event.key === "ArrowDown") {
      graphScroll.scrollTop += 64;
    } else {
      handled = false;
    }
    if (handled) event.preventDefault();
  }

  data.edges.forEach(function (edge) {
    addConnection(projectionOutgoing, edge.source, {node: edge.target, edge: edge.key});
    addConnection(projectionIncoming, edge.target, {node: edge.source, edge: edge.key});
    if (edge.traversable) {
      addConnection(outgoing, edge.source, {node: edge.target, edge: edge.key});
      addConnection(incoming, edge.target, {node: edge.source, edge: edge.key});
    }
  });

  overviewNodeKeys = buildOverviewNodeKeys();
  populateFilter(nodeTypeFilters,
    data.nodes.map(function (node) { return String(node.type || "other"); }),
    enabledNodeTypes, "node-type-filter", false);
  populateFilter(nodeStatusFilters,
    data.nodes.map(function (node) { return String(node.status || "unknown"); }),
    enabledNodeStatuses, "node-status-filter", false);
  populateFilter(layerFilters,
    data.nodes.map(function (node) { return Number(node.layer); }),
    enabledLayers, "layer-filter", true);
  populateFilter(edgeKindFilters,
    data.edges.map(function (edge) { return String(edge.kind || "dependency"); }),
    enabledEdgeKinds, "edge-kind-filter", false);
  const restoredLayout = restoreSavedLayout();
  data.nodes.forEach(function (node) {
    fullLayoutPositions.set(node.key, {x: node.x, y: node.y});
  });
  fullLayoutIsManual = restoredLayout === "restored";
  setManualLayout(fullLayoutIsManual && initialProjectionMode === "full");
  document.getElementById("summary").textContent =
    data.summary.semantic_nodes + " semantic nodes · " +
    data.summary.semantic_edges + " semantic edges · " +
    data.summary.byte_repeatable_runs + "/" + data.summary.runs +
      " byte-repeatable run boundaries · " +
    (data.summary.stage_checkpoint_runs
      ? data.summary.complete_stage_checkpoint_runs + "/" +
        data.summary.stage_checkpoint_runs +
        " complete cooperative stage traces · "
      : "") +
    data.summary.ready_claim_bases + "/" + data.summary.claim_bases +
      " reviewed claim provenance paths ready · " +
    data.summary.current_assessments + " current semantic assessments · " +
    data.summary.active_proofs + "/" + data.summary.proof_nodes +
      " claim-level active formal outcomes · " +
    data.summary.claim_conflicts + " claim conflicts · " +
    data.summary.derivations + " derivation submissions · " +
    data.summary.errors + " errors · " + data.summary.warnings + " warnings";
  const semanticPolicy = data.semantic_policy || {};
  const semanticActivation = semanticPolicy.active_policy_active
    ? ("active explicit release with " +
       (semanticPolicy.active_mapping_ids || []).length + " mapping(s)")
    : (semanticPolicy.active_policy_configured
       ? ("configured release is invalid or inactive" +
          ((semanticPolicy.active_policy_findings || []).length
           ? " (" + semanticPolicy.active_policy_findings.length + " finding(s))"
           : ""))
       : semanticPolicy.require_active_policy
       ? "required but no valid active release"
       : "no active release configured");
  document.getElementById("semantic-policy-summary").textContent =
    "Semantic normalization: " + (semanticPolicy.integrity || "unknown") +
    " integrity; " + (data.summary.semantic_mappings || 0) +
    " current mapping record(s); " + (data.summary.semantic_releases || 0) +
    " release(s); " + semanticActivation + ".";
  const semanticMappingList = document.getElementById("semantic-mapping-list");
  const semanticMappings = semanticPolicy.current_mappings || [];
  if (!semanticMappings.length) {
    const item = document.createElement("li");
    item.textContent = "No semantic mapping review leaves are recorded.";
    semanticMappingList.appendChild(item);
  } else {
    semanticMappings.forEach(function (mapping) {
      const subject = mapping.subject || {};
      const review = mapping.review || {};
      const current = mapping.current_derived || {};
      const target = mapping.target && mapping.target.iri
        ? mapping.target.iri : "unmapped";
      const item = document.createElement("li");
      const headline = document.createElement("div");
      headline.textContent =
        (subject.terminology_id || "?") + "/" + (subject.term_id || "?") +
        " -> " + (mapping.relation || "?") + " -> " + target +
        "; review " + (review.state || "?") + " by " + (review.actor || "?") +
        "; live policy eligibility " +
        (current.eligible_for_policy ? "yes" : "no") +
        "; selected in active release " + (mapping.active ? "yes" : "no") + ".";
      item.appendChild(headline);
      const meaning = document.createElement("div");
      meaning.textContent =
        "Local definition: " + ((mapping.local_term || {}).definition || "not recorded") +
        "; proposal by " + ((mapping.provenance || {}).agent || "?") +
        "; rationale: " + (mapping.rationale || "not recorded") + ".";
      item.appendChild(meaning);
      if ((mapping.limitations || []).length) {
        const limitations = document.createElement("div");
        limitations.textContent = "Limitations: " + mapping.limitations.join("; ") + ".";
        item.appendChild(limitations);
      }
      if ((current.findings || []).length) {
        const findings = document.createElement("div");
        findings.textContent = "Live findings: " + current.findings.map(function (finding) {
          return (finding.code || "finding") + ": " + (finding.detail || "");
        }).join("; ");
        item.appendChild(findings);
      }
      semanticMappingList.appendChild(item);
    });
  }
  const deliberation = data.deliberations || {};
  const deliberationPanels = deliberation.panels || [];
  const deliberationSummary = document.getElementById("deliberation-summary");
  deliberationSummary.textContent =
    "Adversarial claim deliberation - " +
    (deliberation.proposals || []).length + " proposal(s), " +
    (deliberation.candidate_sets || []).length + " frozen set(s), " +
    (deliberation.ballots || []).length + " ballot(s), " +
    (deliberation.phase_decisions || []).length + " phase decision(s), " +
    deliberationPanels.length + " evaluated panel(s)";
  document.getElementById("deliberation-boundary").textContent =
    "Advisory audit layer only. Actor and group labels are self-asserted, not " +
    "authenticated identity or independence. No proposal, candidate set, ballot, " +
    "score, recommendation, or phase decision creates a dependency/support edge, " +
    "establishes scientific truth, or activates semantics or rules.";
  const deliberationContent = document.getElementById("deliberation-content");

  function appendDeliberationSection(title, records, label) {
    const section = document.createElement("section");
    section.className = "deliberation-section";
    const heading = document.createElement("h3");
    heading.textContent = title + " (" + records.length + ")";
    section.appendChild(heading);
    if (!records.length) {
      const empty = document.createElement("p");
      empty.className = "detail-note";
      empty.textContent = "None recorded.";
      section.appendChild(empty);
    } else {
      records.forEach(function (record) {
        const card = document.createElement("details");
        card.className = "deliberation-card";
        const summary = document.createElement("summary");
        summary.textContent = label(record);
        const body = document.createElement("pre");
        body.textContent = JSON.stringify(record, null, 2);
        card.appendChild(summary);
        card.appendChild(body);
        section.appendChild(card);
      });
    }
    deliberationContent.appendChild(section);
  }

  appendDeliberationSection("Evaluated panels", deliberationPanels, function (item) {
    return (item.status || "unknown") + " - " +
      (item.phase || "unknown phase") + " - " +
      (item.subject_key || item.candidate_set_id || "unknown subject") +
      (item.recommended_candidate_id
       ? " - recommendation for human review: " + item.recommended_candidate_id : "");
  });
  appendDeliberationSection(
    "Open proposal groups", deliberation.open_groups || [], function (item) {
      return (item.phase || "unknown phase") + " - " +
        (item.subject_key || "unknown subject") + " - " +
        (item.candidate_ids || []).length + " candidate(s)";
    }
  );
  appendDeliberationSection("Proposals", deliberation.proposals || [], function (item) {
    return (item.phase || "unknown phase") + " - " +
      (item.subject_key || "unknown subject") + " - " +
      (item.candidate_id || item.proposal_id || "unknown candidate");
  });
  appendDeliberationSection(
    "Frozen candidate sets", deliberation.candidate_sets || [], function (item) {
      return (item.phase || "unknown phase") + " - " +
        (item.subject_key || "unknown subject") + " - " +
        (item.candidate_ids || []).length + " preserved candidate(s)";
    }
  );
  appendDeliberationSection("Ballots", deliberation.ballots || [], function (item) {
    const actor = item.actor || {};
    return (item.role || "unknown role") + " - " +
      (actor.id || "unknown actor") + " - " +
      (item.evaluations || []).length + " evaluation(s)";
  });
  appendDeliberationSection(
    "Phase decisions", deliberation.phase_decisions || [], function (item) {
      return (item.decision || "unknown decision") + " - " +
        (item.actor || "unknown actor") + " - " +
        (item.candidate_id || "unknown candidate") + " - advisory routing only";
    }
  );
  appendDeliberationSection(
    "Ledger integrity findings", deliberation.integrity_issues || [], function (item) {
      return (item.code || "integrity finding") + " - " + (item.path || "ledger");
    }
  );
  document.getElementById("coverage").textContent = data.coverage_notice;
  if (restoredLayout === "restored") {
    layoutStatus.textContent = initialProjectionMode === "overview"
      ? "The saved full-graph arrangement is retained. This compact overview uses a temporary layout; the graph, provenance, and logical layers are unchanged."
      : "Browser-local arrangement restored. It changes only this view; the graph, provenance, and logical layers are unchanged.";
  } else if (restoredLayout === "invalid") {
    layoutStatus.textContent =
      "An invalid saved arrangement was ignored. Automatic layout is shown and the graph is unchanged.";
  } else if (restoredLayout === "unavailable") {
    layoutStatus.textContent =
      "Drag nodes to rearrange them. Browser storage is unavailable, so changes last only for this page; the graph is unchanged.";
  }

  data.layers.forEach(function (layer) {
    const option = document.createElement("option");
    option.value = String(layer);
    option.textContent = "Layer " + layer;
    layerSelect.appendChild(option);
    const layerNode = data.nodes.filter(function (node) { return node.layer === layer; })[0];
    const x = defaultPositions.get(layerNode.key).x;
    const layerElement = svgElement("text", {
      x: x + 112, y: 26, "text-anchor": "middle", "class": "ct-layer-label"
    }, "Logical layer " + layer);
    labelLayer.appendChild(layerElement);
    layerElements.set(layer, layerElement);
  });

  data.edges.forEach(function (edge) {
    const source = nodes.get(edge.source);
    const target = nodes.get(edge.target);
    if (!source || !target) return;
    const option = document.createElement("option");
    option.value = edge.key;
    option.textContent = short(source.label, 24) + " — " +
      short(edgeDisplayLabel(edge), 34) + " → " + short(target.label, 24);
    edgeSelect.appendChild(option);
    const group = svgElement("g", {
      "class": "ct-edge-group",
      "data-edge-group-key": edge.key,
      "tabindex": "0",
      "focusable": "true",
      "role": "button",
      "aria-label": "Relationship: " + source.label + " — " +
        edge.relation + " → " + target.label
    });
    const hit = svgElement("path", {
      d: "",
      "class": "ct-edge-hit",
      "data-edge-hit-key": edge.key
    });
    const path = svgElement("path", {
      d: "",
      "class": "ct-edge ct-edge-" + edge.kind +
        (edge.assessment_state ? " ct-edge-state-" + classToken(edge.assessment_state) : ""),
      "marker-end": "url(#arrow-" + edge.kind + ")",
      "data-edge-key": edge.key
    });
    path.appendChild(svgElement("title", {}, edge.relation));
    group.appendChild(hit);
    group.appendChild(path);
    const label = svgElement("text", {
      x: 0, y: 0, "text-anchor": "middle",
      "class": "ct-edge-label", "data-edge-label-for": edge.key
    }, edgeDisplayLabel(edge));
    group.appendChild(label);
    group.addEventListener("click", function () { selectEdge(edge.key); });
    group.addEventListener("keydown", function (event) {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      selectEdge(edge.key);
    });
    edgeLayer.appendChild(group);
    edgeElements.set(edge.key, {
      group: group, hit: hit, path: path, label: label
    });
  });

  function selectNode(key, shouldCenter) {
    selected = nodes.has(key) ? key : null;
    selectedEdge = null;
    focusSelect.value = selected || "";
    edgeSelect.value = "";
    const selectedNode = selected ? nodes.get(selected) : null;
    Object.keys(moveButtons).forEach(function (direction) {
      moveButtons[direction].disabled = !selectedNode;
    });
    selectedAssessment = selectedNode ? selectedNode.default_assessment_id : null;
    if (selectedNode && !revealedNodeKeys.has(selectedNode.key)) {
      revealedNodeKeys.clear();
      projectionOneHop(selectedNode.key, projectionIncoming).forEach(function (nodeKey) {
        revealedNodeKeys.add(nodeKey);
      });
      projectionOneHop(selectedNode.key, projectionOutgoing).forEach(function (nodeKey) {
        revealedNodeKeys.add(nodeKey);
      });
      projectionMode = "custom";
      applyVisibility(true);
    } else if (selectedNode && !visibleNodeKeys.has(selectedNode.key)) {
      applyVisibility(!usesPersistentFullLayout());
    } else {
      applyVisibility(false);
    }
    expandUpstream.disabled = !selectedNode;
    expandDownstream.disabled = !selectedNode;
    expandBoth.disabled = !selectedNode;
    collapseFocusButton.disabled = !selectedNode;
    if (selectedNode && graphScroll && shouldCenter !== false) {
      centerNodeInViewport(selectedNode);
    }
    updateHighlights();
    updateDetails();
  }

  function selectEdge(key) {
    selectedEdge = edges.has(key) ? key : null;
    selected = null;
    selectedAssessment = null;
    focusSelect.value = "";
    edgeSelect.value = selectedEdge || "";
    Object.keys(moveButtons).forEach(function (direction) {
      moveButtons[direction].disabled = true;
    });
    const selectedRelationship = selectedEdge ? edges.get(selectedEdge) : null;
    if (selectedRelationship && (!visibleNodeKeys.has(selectedRelationship.source) ||
        !visibleNodeKeys.has(selectedRelationship.target))) {
      revealedNodeKeys.clear();
      revealedNodeKeys.add(selectedRelationship.source);
      revealedNodeKeys.add(selectedRelationship.target);
      projectionMode = "custom";
      applyVisibility(true);
    } else {
      applyVisibility(false);
    }
    updateHighlights();
    updateDetails();
  }

  data.nodes.forEach(function (node) {
    const option = document.createElement("option");
    option.value = node.key;
    option.textContent = "L" + node.layer + " · " +
      (node.kind === "run" ? "run · " : node.type + " · ") + node.label;
    focusSelect.appendChild(option);

    const group = svgElement("g", {
      transform: "translate(" + node.x + " " + node.y + ")",
      "class": "ct-node ct-type-" + typeClass(node.type) + " ct-status-" + classToken(node.status),
      "aria-hidden": "true",
      "data-node-key": node.key
    });
    const isAssessment = node.kind === "assessment";
    const isProof = node.kind === "proof";
    if (isAssessment) {
      group.appendChild(svgElement("polygon", {
        points: (node.width / 2) + ",0 " + node.width + "," + (node.height / 2) +
          " " + (node.width / 2) + "," + node.height + " 0," + (node.height / 2)
      }));
    } else if (isProof) {
      group.appendChild(svgElement("polygon", {
        points: "10,0 " + (node.width - 10) + ",0 " + node.width + "," +
          (node.height / 2) + " " + (node.width - 10) + "," + node.height +
          " 10," + node.height + " 0," + (node.height / 2)
      }));
    } else {
      group.appendChild(svgElement("rect", {
        width: node.width, height: node.height, rx: node.kind === "run" ? 18 : 7
      }));
    }
    group.appendChild(svgElement("text", {
      x: isAssessment ? node.width / 2 : 13, y: 23, "class": "ct-title",
      "text-anchor": isAssessment ? "middle" : "start"
    }, short(node.label, isAssessment ? 24 : 30)));
    const issueText = node.findings && node.findings.length
      ? " · ! " + node.findings.length : "";
    group.appendChild(svgElement("text", {
      x: isAssessment ? node.width / 2 : 13, y: 44, "class": "ct-meta",
      "text-anchor": isAssessment ? "middle" : "start"
    }, short((isAssessment ? "agent-assessed" : isProof ? "rule-derived" : node.type) +
      " · " + node.status + issueText,
      isAssessment ? 27 : 36)));
    const finalLine = isAssessment
      ? node.status + " review"
      : isProof
      ? node.value || "conditional conclusion"
      : node.kind === "run"
      ? (node.current_gate_role === "historical_replaced"
        ? "historical replacement · stale"
        : stageTraceSummary(node) ||
        (((node.replays || []).some(function (item) {
            return Boolean((item.current_derived || {}).byte_repeatable_current);
          }) ? "byte-repeatable boundary" : "partial lineage") +
          " · " + (node.pipeline_stages || []).length + " declared stages"))
      : (node.path || node.value || "");
    group.appendChild(svgElement("text", {
      x: isAssessment ? node.width / 2 : 13, y: 64, "class": "ct-path",
      "text-anchor": isAssessment ? "middle" : "start"
    }, short(finalLine, isAssessment ? 25 : 36)));
    group.appendChild(svgElement("title", {}, node.kind === "run"
      ? node.label + " — " + node.coverage
      : node.label + (node.value ? " — " + node.value : "")));
    group.addEventListener("pointerdown", function (event) { beginDrag(event, node, group); });
    group.addEventListener("pointermove", continueDrag);
    group.addEventListener("pointerup", finishDrag);
    group.addEventListener("pointercancel", cancelDrag);
    group.addEventListener("lostpointercapture", cancelDrag);
    group.addEventListener("click", function () {
      if (suppressClickKey === node.key) {
        suppressClickKey = null;
        return;
      }
      selectNode(node.key);
    });
    group.addEventListener("dblclick", function (event) {
      event.preventDefault();
      selectNode(node.key, false);
      projectionOneHop(selected, incoming).forEach(function (key) {
        revealedNodeKeys.add(key);
      });
      projectionOneHop(selected, outgoing).forEach(function (key) {
        revealedNodeKeys.add(key);
      });
      projectionMode = "custom";
      applyVisibility(true);
      centerNodeInViewport(node);
    });
    nodeLayer.appendChild(group);
    nodeElements.set(node.key, group);
  });

  setProjectionMode(initialProjectionMode);
  projectionInitialized = true;
  window.requestAnimationFrame(function () { fitViewport(); });

  function closure(start, adjacency) {
    const reached = new Set();
    const relatedEdges = new Set();
    const queue = [start];
    while (queue.length) {
      const current = queue.shift();
      (adjacency.get(current) || []).forEach(function (link) {
        relatedEdges.add(link.edge);
        if (!reached.has(link.node) && link.node !== start) {
          reached.add(link.node);
          queue.push(link.node);
        }
      });
    }
    return {nodes: reached, edges: relatedEdges};
  }

  function updateHighlights() {
    const upstream = selected ? closure(selected, incoming) : {nodes: new Set(), edges: new Set()};
    const downstream = selected ? closure(selected, outgoing) : {nodes: new Set(), edges: new Set()};
    const edgeSelection = selectedEdge ? edges.get(selectedEdge) : null;
    const selectedLayer = layerSelect.value === "" ? null : Number(layerSelect.value);
    nodeElements.forEach(function (element, key) {
      const node = nodes.get(key);
      const edgeEndpoint = Boolean(edgeSelection) &&
        (key === edgeSelection.source || key === edgeSelection.target);
      const nodeUnrelated = Boolean(selected) &&
        key !== selected && !upstream.nodes.has(key) && !downstream.nodes.has(key);
      element.classList.toggle("ct-dim",
        edgeSelection ? !edgeEndpoint : nodeUnrelated);
      element.classList.toggle("ct-selected", key === selected);
      element.classList.toggle("ct-ancestor", upstream.nodes.has(key));
      element.classList.toggle("ct-descendant", downstream.nodes.has(key));
      element.classList.toggle("ct-edge-source",
        Boolean(edgeSelection) && key === edgeSelection.source);
      element.classList.toggle("ct-edge-target",
        Boolean(edgeSelection) && key === edgeSelection.target);
      element.classList.toggle("ct-layer-muted", selectedLayer !== null && node.layer !== selectedLayer);
    });
    edgeElements.forEach(function (elements, key) {
      const edge = edges.get(key);
      const exactSelection = Boolean(edgeSelection) && key === selectedEdge;
      elements.path.classList.toggle("ct-related", upstream.edges.has(key) || downstream.edges.has(key));
      elements.path.classList.toggle("ct-upstream", upstream.edges.has(key));
      elements.path.classList.toggle("ct-downstream", downstream.edges.has(key));
      elements.path.setAttribute(
        "marker-end", "url(#arrow-" +
          (exactSelection ? "selected" : edge.kind) + ")"
      );
      elements.group.classList.toggle("ct-edge-selected", exactSelection);
      elements.group.classList.toggle("ct-dim", edgeSelection
        ? !exactSelection
        : Boolean(selected) && !upstream.edges.has(key) && !downstream.edges.has(key));
      const labelAvailable = elements.group.getAttribute("data-label-available") === "true";
      const labelDefaultVisible =
        elements.group.getAttribute("data-label-default-visible") === "true";
      setSvgHidden(elements.label, elements.group.hasAttribute("hidden") ||
        !labelAvailable || (!labelDefaultVisible && !exactSelection));
    });
  }

  function appendDetail(container, label, value) {
    if (value === null || value === undefined || value === "") return;
    if (Array.isArray(value) && value.length === 0) return;
    const term = document.createElement("dt");
    term.textContent = label;
    const description = document.createElement("dd");
    description.textContent = Array.isArray(value) ? value.join(", ") : String(value);
    container.appendChild(term);
    container.appendChild(description);
  }

  function appendRunLinkDetail(container, label, runId) {
    if (!runId) return;
    const term = document.createElement("dt");
    term.textContent = label;
    const description = document.createElement("dd");
    const replacementKey = "receipt:" + runId;
    if (nodes.has(replacementKey)) {
      const linkButton = document.createElement("button");
      linkButton.type = "button";
      linkButton.className = "detail-run-link";
      linkButton.textContent = runId;
      linkButton.setAttribute("data-replacement-run-id", runId);
      linkButton.setAttribute("aria-label", "Open replacement run " + runId);
      linkButton.addEventListener("click", function () {
        selectNode(replacementKey);
      });
      description.appendChild(linkButton);
    } else {
      description.textContent = runId + " (run node unavailable in this view)";
    }
    container.appendChild(term);
    container.appendChild(description);
  }

  function humanLabel(value) {
    return String(value === null || value === undefined ? "not stated" : value)
      .replace(/_/g, " ");
  }

  function stageTraceSummary(node) {
    const trace = node && node.stage_trace;
    if (!trace || typeof trace !== "object") return null;
    const checkpointCount = Array.isArray(trace.checkpoints)
      ? trace.checkpoints.length : 0;
    const requiredCount = Array.isArray(trace.required_stage_ids)
      ? trace.required_stage_ids.length : 0;
    const state = humanLabel(trace.state).replace(/^cooperative report /, "");
    return checkpointCount + "/" + requiredCount + " checkpoints · " + state;
  }

  function evidencePlanMode(plan) {
    return plan && typeof plan === "object" ? "claim-owned exact all-of" : null;
  }

  function evidencePlanBindings(plan) {
    if (!plan || !Array.isArray(plan.required_bindings)) return [];
    return plan.required_bindings.filter(function (item) {
      return item && typeof item.result_id === "string" &&
        typeof item.binding_id === "string";
    }).map(function (item) {
      return item.result_id + " / " + item.binding_id;
    }).sort();
  }

  function displayValue(value) {
    if (value === null || value === undefined || value === "") return "not stated";
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  }

  function appendFrame(container, frame) {
    const entries = Object.keys(frame || {}).filter(function (key) {
      return frame[key] !== null && frame[key] !== undefined && frame[key] !== "";
    });
    if (!entries.length) return;
    const list = document.createElement("ul");
    list.className = "frame-list";
    entries.forEach(function (key) {
      const item = document.createElement("li");
      item.textContent = humanLabel(key) + ": " + displayValue(frame[key]);
      list.appendChild(item);
    });
    container.appendChild(list);
  }

  function appendReviewCopy(container, label, value) {
    if (value === null || value === undefined || value === "") return;
    const paragraph = document.createElement("p");
    paragraph.className = "review-copy";
    const heading = document.createElement("strong");
    heading.textContent = label + ": ";
    paragraph.appendChild(heading);
    paragraph.appendChild(document.createTextNode(displayValue(value)));
    container.appendChild(paragraph);
  }

  function appendReviewList(container, headingText, values, formatter) {
    if (!values || !values.length) return;
    const heading = document.createElement("h3");
    heading.textContent = headingText;
    container.appendChild(heading);
    const list = document.createElement("ul");
    list.className = "review-list";
    values.forEach(function (value) {
      const item = document.createElement("li");
      item.textContent = formatter ? formatter(value) : String(value);
      list.appendChild(item);
    });
    container.appendChild(list);
  }

  function configureAssessmentSelector(node) {
    const ids = (node.assessment_ids || []).filter(function (id) {
      return assessments.has(id);
    });
    assessmentSelect.replaceChildren();
    if (!ids.length) {
      assessmentControl.hidden = true;
      selectedAssessment = null;
      return null;
    }
    ids.forEach(function (id) {
      const review = assessments.get(id);
      const option = document.createElement("option");
      option.value = id;
      option.textContent = humanLabel(review.current_state) + " · " +
        humanLabel(review.verdict) + (review.is_current ? " · current" : " · history");
      assessmentSelect.appendChild(option);
    });
    if (!ids.includes(selectedAssessment)) {
      selectedAssessment = ids.includes(node.default_assessment_id)
        ? node.default_assessment_id : ids[0];
    }
    assessmentSelect.value = selectedAssessment;
    assessmentControl.hidden = ids.length < 2;
    return assessments.get(selectedAssessment);
  }

  function appendAssessmentPanel(review) {
    const panel = document.createElement("section");
    panel.className = "review-panel";
    panel.setAttribute("aria-labelledby", "semantic-review-heading");
    const heading = document.createElement("h3");
    heading.id = "semantic-review-heading";
    heading.textContent = "Semantic assessment";
    panel.appendChild(heading);

    const status = document.createElement("p");
    status.className = "review-status";
    status.textContent = "Agent-assessed · policy " + (review.schema_version || "unknown") +
      " · current state " + humanLabel(review.current_state) +
      " · review decision " + humanLabel(review.review_state) +
      " · " + (review.is_current ? "current record" : "historical record") +
      (review.stale ? " · stale" : "") +
      (review.integrity_blocked ? " · inactive: assessment store integrity error" : "");
    panel.appendChild(status);
    const boundary = document.createElement("p");
    boundary.className = "detail-note";
    boundary.textContent = "This is an attributed, policy-checked interpretation; it is not a deterministic proof of scientific truth.";
    panel.appendChild(boundary);

    const comparison = document.createElement("div");
    comparison.className = "review-comparison";
    const claim = document.createElement("section");
    const claimHeading = document.createElement("h3");
    claimHeading.textContent = "Claim as written";
    claim.appendChild(claimHeading);
    const claimText = document.createElement("p");
    claimText.textContent = review.claim.text || review.claim.id;
    claim.appendChild(claimText);
    appendFrame(claim, review.claim.frame || {});
    comparison.appendChild(claim);

    const result = document.createElement("section");
    const resultHeading = document.createElement("h3");
    resultHeading.textContent = "Result actually obtained";
    result.appendChild(resultHeading);
    if ((review.results || []).length === 1) {
      const resultText = document.createElement("p");
      resultText.textContent = review.results[0].text;
      result.appendChild(resultText);
    } else {
      const resultList = document.createElement("ul");
      resultList.className = "review-list";
      (review.results || []).forEach(function (item) {
        const row = document.createElement("li");
        row.textContent = item.id + ": " + item.text;
        resultList.appendChild(row);
      });
      result.appendChild(resultList);
    }
    appendFrame(result, review.result_frame || {});
    comparison.appendChild(result);
    panel.appendChild(comparison);

    const alignmentHeading = document.createElement("h3");
    alignmentHeading.textContent = "Meaning alignment";
    panel.appendChild(alignmentHeading);
    const table = document.createElement("table");
    table.className = "alignment-table";
    const caption = document.createElement("caption");
    caption.className = "sr-only";
    caption.textContent = "External-agent alignment assessment for each claim dimension";
    table.appendChild(caption);
    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    ["Dimension", "Assessment"].forEach(function (label) {
      const cell = document.createElement("th");
      cell.scope = "col";
      cell.textContent = label;
      headRow.appendChild(cell);
    });
    head.appendChild(headRow);
    table.appendChild(head);
    const body = document.createElement("tbody");
    (review.alignment || []).forEach(function (row) {
      const tableRow = document.createElement("tr");
      const dimension = document.createElement("th");
      dimension.scope = "row";
      dimension.textContent = humanLabel(row.dimension);
      const assessment = document.createElement("td");
      assessment.textContent = humanLabel(row.state) + " · claim: " +
        displayValue(row.claim) + " · result: " + displayValue(row.result);
      tableRow.appendChild(dimension);
      tableRow.appendChild(assessment);
      body.appendChild(tableRow);
    });
    table.appendChild(body);
    panel.appendChild(table);

    appendReviewCopy(panel, "Verdict", humanLabel(review.verdict));
    appendReviewCopy(panel, "Rationale", review.rationale);
    appendReviewCopy(panel, "Recommended narrower claim", review.recommended_claim);
    appendReviewList(panel, "Limitations", review.limitations || []);
    appendReviewList(panel, "Current findings", review.findings || [], function (item) {
      return item.severity + ": " + item.code + " — " + item.detail;
    });

    const provenance = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "Evidence anchors and provenance";
    provenance.appendChild(summary);
    appendReviewList(provenance, "Evidence anchors", review.evidence_anchors || [], function (anchor) {
      const locator = anchor.kind === "json_pointer"
        ? "JSON pointer " + anchor.pointer + " expected " + displayValue(anchor.expected_value)
        : "text lines " + anchor.start_line + "–" + anchor.end_line;
      const check = anchor.check || {};
      return anchor.result_id + " · " + locator + " · " +
        (check.valid ? "valid" : "invalid") + (check.detail ? ": " + check.detail : "");
    });
    const provenanceList = document.createElement("dl");
    appendDetail(provenanceList, "Assessment", review.id);
    appendDetail(provenanceList, "Policy schema", review.schema_version);
    appendDetail(provenanceList, "Recorded", review.recorded_at);
    appendDetail(provenanceList, "Agent", review.provenance.agent);
    appendDetail(provenanceList, "Model", review.provenance.model);
    appendDetail(provenanceList, "Skill", review.provenance.skill_version);
    appendDetail(provenanceList, "Prompt hash", review.provenance.prompt_sha256);
    appendDetail(provenanceList, "Review actor", (review.review || {}).actor);
    appendDetail(provenanceList, "Claim version", review.claim.node_version_id);
    appendDetail(provenanceList, "Result versions", (review.results || []).map(function (item) {
      const fileVersion = (item.artifact || {}).file_version_id;
      return item.id + " · " + (fileVersion || item.node_version_id || "not captured");
    }));
    provenance.appendChild(provenanceList);
    panel.appendChild(provenance);
    detailContent.appendChild(panel);
  }

  function appendDeclaredLinkNotice(node) {
    const unresolved = (node.claim_links || []).filter(function (item) {
      return item.status !== "covered";
    });
    if (!unresolved.length) return;
    const priority = {
      assessed_conflict: 0,
      assessed_not_as_written: 1,
      assessed_without_relation: 2,
      unassessed: 3
    };
    unresolved.sort(function (left, right) {
      const leftRank = priority[left.status] === undefined ? 9 : priority[left.status];
      const rightRank = priority[right.status] === undefined ? 9 : priority[right.status];
      return leftRank - rightRank;
    });
    const link = unresolved[0];
    const panel = document.createElement("section");
    panel.className = "review-panel";
    const heading = document.createElement("h3");
    heading.textContent = "Semantic assessment";
    panel.appendChild(heading);
    const status = document.createElement("p");
    status.className = "review-status";
    status.textContent = "Declared " + humanLabel(link.declared_relation) + " · " + ({
      assessed_conflict: "accepted assessment conflicts",
      assessed_not_as_written: "assessed, not as written",
      assessed_without_relation: "assessed, no active semantic relation",
      unassessed: "unassessed"
    }[link.status] || humanLabel(link.status));
    panel.appendChild(status);
    const note = document.createElement("p");
    note.className = "detail-note";
    note.textContent = link.status === "assessed_conflict"
      ? "The graph declaration has the opposite polarity from a current accepted semantic assessment."
      : link.status === "assessed_not_as_written"
      ? "A current accepted assessment exists, but it does not validate this relation as written."
      : link.status === "assessed_without_relation"
      ? "A current accepted assessment exists, but its verdict activates no semantic relation."
      : "No current accepted semantic assessment covers this result-to-claim pair.";
    panel.appendChild(note);
    detailContent.appendChild(panel);
  }

  function updateDetails() {
    detailContent.replaceChildren();
    if (selectedEdge && edges.has(selectedEdge)) {
      assessmentControl.hidden = true;
      detailContent.className = "";
      const edge = edges.get(selectedEdge);
      const source = nodes.get(edge.source);
      const target = nodes.get(edge.target);
      detailTitle.textContent = edgeDisplayLabel(edge);
      const statement = document.createElement("p");
      statement.className = "review-status";
      statement.textContent = source.label + " — " + edge.relation + " → " + target.label;
      detailContent.appendChild(statement);
      const list = document.createElement("dl");
      detailContent.appendChild(list);
      appendDetail(list, "Relation", edge.relation);
      appendDetail(list, "Kind", humanLabel(edge.kind));
      appendDetail(list, "From", source.label + " (" + edge.source + ")");
      appendDetail(list, "To", target.label + " (" + edge.target + ")");
      appendDetail(list, "Trajectory traversal",
        edge.traversable ? "yes — included in dependency ancestry" :
          "no — shown without creating dependency ancestry");
      if (edge.assessment_state) {
        appendDetail(list, "Assessment state", humanLabel(edge.assessment_state));
      }
      if (edge.derivation_state) {
        appendDetail(list, "Derivation state", humanLabel(edge.derivation_state));
      }
      if (edge.binding_kind) {
        appendDetail(list, "Binding kind", humanLabel(edge.binding_kind));
      }
      if (edge.declaration_comparison) {
        appendDetail(
          list, "Declaration comparison", humanLabel(edge.declaration_comparison)
        );
      }
      if (edge.output_evidence) {
        appendDetail(list, "Boundary evidence", humanLabel(edge.output_evidence));
      }
      if (edge.stage_attribution) {
        appendDetail(list, "Stage attribution", humanLabel(edge.stage_attribution));
      }
      if (edge.current !== null && edge.current !== undefined) {
        appendDetail(list, "Current binding", edge.current ? "yes" : "no");
      }
      if (edge.integrity_state) {
        appendDetail(list, "Binding integrity", humanLabel(edge.integrity_state));
      }
      const boundary = document.createElement("p");
      boundary.className = "detail-note";
      boundary.textContent = ({
        dependency: "This is declared semantic lineage.",
        annotation: "This is a visible semantic annotation; it is not traversed as dependency ancestry.",
        receipt: "This is a partial mechanical run binding, not proof of observed reads or write causation.",
        assessment: "This is an attributed semantic-review relationship, not a deterministic proof of meaning.",
        proof: "This is a conditional symbolic relationship under project rules, not scientific truth."
      }[edge.kind] || "This is a declared graph relationship.") +
        " Selecting it does not change the graph or certify the scientific claim.";
      detailContent.appendChild(boundary);
      return;
    }
    if (!selected || !nodes.has(selected)) {
      assessmentControl.hidden = true;
      detailTitle.textContent = "Trajectory overview";
      const note = document.createElement("p");
      note.className = "detail-note";
      note.textContent = "Choose a focus, click a node, or click a relationship. Node focus highlights the dependency ancestry currently visible in this projection; relationship focus highlights only its direct endpoints.";
      detailContent.appendChild(note);
      if ((data.global_findings || []).length) {
        const list = document.createElement("ul");
        list.className = "finding-list";
        data.global_findings.forEach(function (item) {
          const row = document.createElement("li");
          row.textContent = item.severity + ": " + item.code + " — " + item.detail;
          list.appendChild(row);
        });
        detailContent.appendChild(list);
      }
      return;
    }
    const node = nodes.get(selected);
    const proof = node.kind === "proof" ? (node.proof || {}) : null;
    detailContent.className = "";
    detailTitle.textContent = node.label;
    const list = document.createElement("dl");
    detailContent.appendChild(list);
    appendDetail(list, "Kind", node.kind === "run" ? "mechanical run receipt" : node.type);
    appendDetail(list, "Status", node.status);
    appendDetail(list, "Path", node.path);
    appendDetail(list, "Value", node.value);
    appendDetail(list, "Backbone", typeof node.backbone === "object" ? JSON.stringify(node.backbone) : node.backbone);
    appendDetail(list, "Date", node.date);
    if (node.kind === "semantic" || node.kind === "assessment") {
      appendDetail(list, "Evidence plan", evidencePlanMode(node.evidence_plan));
      appendDetail(list, "Plan schema", (node.evidence_plan || {}).schema_version);
      appendDetail(list, "Required bindings", evidencePlanBindings(node.evidence_plan));
      appendDetail(list, "Bound runs", node.run_ids);
      appendDetail(list, "Claim provenance readiness", (node.claim_basis || []).map(function (item) {
        return item.result_id + " -> " + item.claim_id + ": " + item.overall +
          " (execution=" + item.execution_state + ", contract=" + item.contract_state +
          ", ancestry=" + item.ancestry_state +
          ", intermediate=" + item.materialized_intermediate_state +
          ", replay=" + item.replay_state + ", method=" + item.method_state +
          ", checkpoint=" + (item.stage_checkpoint_state || "not available") + ")";
      }));
      appendDetail(list, "Claim cooperative checkpoint state", (node.claim_basis || []).map(function (item) {
        return item.result_id + " -> " + item.claim_id + ": " +
          (item.stage_checkpoint_state || "not available");
      }));
      appendDetail(list, "Claim stage execution observation", (node.claim_basis || []).map(function (item) {
        return item.result_id + " -> " + item.claim_id + ": " +
          (item.stage_execution_observation || "not available");
      }));
      appendDetail(list, "Claim producer stages", (node.claim_basis || []).map(function (item) {
        return item.result_id + " -> " + item.claim_id + ": " +
          (item.producer_stage_id || "not available");
      }));
      appendDetail(list, "Exact method ancestry", (node.claim_basis || []).reduce(function (items, basis) {
        return items.concat((basis.ancestry_method_steps || []).map(function (step) {
          return basis.result_id + ": " + step.stage_id + " -> " +
            step.method_id + "/" + step.method_step_id;
        }));
      }, []));
      appendDetail(list, "Ancestry materialized intermediates", (node.claim_basis || []).reduce(function (items, basis) {
        return items.concat((basis.ancestry_materialized_intermediates || []).map(function (intermediate) {
          return basis.result_id + ": " + intermediate.path + " (" +
            intermediate.binding_state + ")";
        }));
      }, []));
      appendDetail(list, "Missing claim method steps", (node.claim_basis || []).reduce(function (items, basis) {
        return items.concat((basis.missing_claim_method_steps || []).map(function (step) {
          return basis.result_id + ": " + step.method_id + "/" + step.method_step_id;
        }));
      }, []));
      appendDetail(list, "Off-ancestry claim method steps", (node.claim_basis || []).reduce(function (items, basis) {
        return items.concat((basis.off_ancestry_declared_method_steps || []).map(function (step) {
          return basis.result_id + ": " + step.method_id + "/" + step.method_step_id;
        }));
      }, []));
      appendDetail(list, "Method conformance reviews", (node.method_assessments || []).map(function (item) {
        const current = item.current_derived || {};
        return item.id + ": " + current.effective_review_state +
          ", implementation=" + (current.implementation_current ? "current" : "not current");
      }));
      appendDetail(list, "Findings", (node.findings || []).map(function (item) {
        return item.severity + ": " + item.code + " — " + item.detail;
      }));
    } else if (node.kind === "proof") {
      appendDetail(list, "Target", proof.rendered_target);
      appendDetail(list, "Formal outcome", proof.rendered_outcomes || []);
      appendDetail(list, "Outcome relation", humanLabel(proof.outcome_relation));
      appendDetail(list, "Proof state", humanLabel(proof.proof_state));
      appendDetail(list, "Active", proof.active ? "yes" : "no");
      appendDetail(list, "Claim bound", proof.claim_bound ? "yes" : "no");
      appendDetail(list, "Stale", proof.stale ? "yes" : "no");
      appendDetail(list, "Changed inputs", (proof.drift || []).map(function (item) {
        return item.kind + ": " + item.id + " (" +
          (item.stored_version || item.stored_sha256 || "absent") + " -> " +
          (item.current_version || item.current_sha256 || "absent") + ")";
      }));
      appendDetail(list, "Used premise results", proof.used_result_ids || []);
      appendDetail(list, "Execution basis", (proof.execution_basis || {}).state);
      appendDetail(list, "Claim-basis semantic reviews", (proof.execution_basis || {}).claim_basis_assessment_ids || []);
      appendDetail(list, "Symbolic state unchanged by execution basis", (proof.execution_basis || {}).does_not_change_symbolic_proof_state ? "yes" : "not recorded");
      appendDetail(list, "Stored evidence plan", evidencePlanMode(proof.evidence_plan));
      appendDetail(list, "Stored plan schema", (proof.evidence_plan || {}).schema_version);
      appendDetail(list, "Stored required bindings", evidencePlanBindings(proof.evidence_plan));
      appendDetail(list, "Submitted result scope", (proof.results || []).map(function (item) {
        return item.id;
      }));
      appendDetail(list, "Rule pack", (proof.rule_pack || {}).id);
      appendDetail(list, "Vocabulary", (proof.vocabulary || {}).id);
      appendDetail(list, "Assumptions", proof.assumptions || []);
      appendDetail(list, "Derivation", proof.id);
      appendDetail(list, "Submission history", proof.derivation_ids || []);
      appendDetail(list, "Proof or group", proof.proof_id);
      appendDetail(list, "Stored proof", proof.stored_proof_id || "not a single certificate");
      appendDetail(list, "Current evaluation proof", proof.current_evaluation_proof_id);
      appendDetail(list, "Conditional proofs", (proof.conditional_proofs || []).map(function (item) {
        return item.proof_state + ": " + item.proof_id;
      }));
      appendDetail(list, "Representative actor", proof.actor);
      appendDetail(list, "Representative agent", (proof.provenance || {}).agent);
      appendDetail(list, "Findings", (proof.findings || []).map(function (item) {
        return item.severity + ": " + item.code + " — " + item.detail;
      }));
    } else if (node.kind === "run") {
      appendDetail(list, "Exit code", node.returncode);
      appendDetail(list, "Event schema", node.event_schema_version);
      appendDetail(list, "Computation", node.computation_id);
      appendDetail(list, "Pipeline contract", node.pipeline_contract_id);
      appendDetail(list, "Contract state", node.pipeline_contract_state);
      appendDetail(list, "Current gate role", node.current_gate_role
        ? (node.current_gate_role === "historical_replaced"
          ? "historical replacement" : humanLabel(node.current_gate_role))
        : null);
      appendRunLinkDetail(list, "Replacement run", node.replacement_run_id);
      appendDetail(list, "Replacement replay certificates",
        node.replacement_replay_ids || []);
      appendDetail(list, "Event-store integrity", node.event_store_integrity);
      appendDetail(list, "Run-link integrity", node.link_integrity);
      appendDetail(list, "Evidence eligible", node.evidence_eligible ? "yes" : "no");
      appendDetail(list, "Replay conflict", node.replay_conflict ? "yes" : "no");
      appendDetail(list, "Conflicting replay certificates",
        node.replay_conflict_certificate_ids || []);
      appendDetail(list, "Run-link issues", (node.link_issues || []).map(function (item) {
        return item.code + ": " + item.detail;
      }));
      appendDetail(list, "Declared internal stages", (node.pipeline_stages || []).map(function (item) {
        const dependencies = (item.depends_on || []).length ? item.depends_on.join(", ") : "entry";
        return item.id + ": " + item.method_id + "/" + item.method_step_id +
          "; depends on " + dependencies + "; " + item.execution_observation;
      }));
      const stageTrace = node.stage_trace && typeof node.stage_trace === "object"
        ? node.stage_trace : null;
      appendDetail(list, "Cooperative checkpoint trace", stageTrace
        ? humanLabel(stageTrace.state) + " (" +
          (Array.isArray(stageTrace.checkpoints) ? stageTrace.checkpoints.length : 0) +
          "/" +
          (Array.isArray(stageTrace.required_stage_ids)
            ? stageTrace.required_stage_ids.length : 0) + " checkpoints)"
        : "not requested or recorded");
      appendDetail(list, "Program-emitted stage checkpoints", stageTrace
        ? (stageTrace.checkpoints || []).map(function (item) {
          const callsite = item.callsite || {};
          return "#" + item.sequence + " " + item.stage_id + " @ " +
            (callsite.path || "unknown path") + ":" +
            (callsite.line || "unknown line") + " (" +
            (item.code_node_id || "unknown code node") + ")";
        }) : []);
      appendDetail(list, "Cooperative checkpoint issues", stageTrace
        ? (stageTrace.issues || []).map(function (item) {
          return item.code + ": " + item.detail;
        }) : []);
      appendDetail(list, "Inputs", node.declared_inputs);
      appendDetail(list, "Outputs", node.output_transitions.map(function (item) {
        const observation = item.produced
          ? "; content changed in process window; write causation not proven"
          : "; no content change observed in process window";
        return item.path + " (" + item.transition + observation + ")";
      }));
      appendDetail(list, "Declared materialized intermediate paths", node.declared_intermediates || []);
      appendDetail(list, "Materialized intermediate file transitions", (node.intermediate_transitions || []).map(function (item) {
        const after = item.after || {};
        const digest = after.sha256 ? "; after sha256=" + after.sha256 : "";
        const observation = item.produced
          ? "; content changed in process window; stage causation not proven"
          : "; no content change observed in process window";
        return item.path + " (" + item.transition + observation + digest + ")";
      }));
      appendDetail(list, "Replay certificates", (node.replays || []).map(function (item) {
        const current = item.current_derived || {};
        return item.id + ": " + item.outcome +
          (current.byte_repeatable_current ? " (boundary current" : " (not current") +
          (current.review_ready_current ? ", review-ready)" : ", not review-ready)");
      }));
      appendDetail(list, "Replay undeclared writes", (node.replays || []).reduce(function (items, replay) {
        return items.concat(replay.undeclared_write_paths || []);
      }, []));
      appendDetail(list, "Replay materialized-intermediate comparison", (node.replays || []).map(function (item) {
        const comparison = item.comparison || {};
        if (!("materialized_intermediates_equal" in comparison)) {
          return item.id + ": not recorded by this legacy replay schema";
        }
        return item.id + ": attempts equal=" + comparison.materialized_intermediates_equal +
          ", all match source receipt=" + comparison.all_materialized_intermediates_match_source_receipt;
      }));
      appendDetail(list, "Replay cooperative-stage trace comparison", (node.replays || []).map(function (item) {
        const comparison = item.comparison || {};
        if (!("all_stage_traces_complete" in comparison)) {
          return item.id + ": not recorded by this replay schema";
        }
        return item.id + ": all complete=" + comparison.all_stage_traces_complete +
          ", attempts equal=" + comparison.stage_traces_equal +
          ", all match source receipt=" +
          comparison.all_stage_traces_match_source_receipt +
          ", current=" + Boolean(
            (item.current_derived || {}).stage_trace_repeatable_current
          );
      }));
      appendDetail(list, "Bindings", node.bindings.map(function (item) {
        const basis = item.binding_kind === "explicit_run_reference"
          ? "explicit run reference"
          : (item.declaration_comparison || "not compared");
        const evidence = item.output_evidence
          ? "; boundary evidence=" + item.output_evidence : "";
        const stage = item.stage_attribution
          ? "; stage attribution=" + item.stage_attribution : "";
        const current = item.current === null || item.current === undefined
          ? "" : "; current=" + (item.current ? "yes" : "no");
        return item.node_id + " (" + basis + evidence + stage + current + ")";
      }));
      appendDetail(list, "Coverage", node.coverage);
    }
    if (node.kind === "proof") {
      assessmentControl.hidden = true;
      const boundary = document.createElement("p");
      boundary.className = "scope";
      const stateBoundary = {
        derivable: "The formal target is derivable under the named project rule pack.",
        refutable: "The target is refutable because its explicit opposite is derivable under the named project rule pack.",
        conflict: "Both the formal target and its explicit opposite are derivable under the named project rule pack.",
        cross_conflict: "Separate active conditional proofs derive the formal target and its explicit opposite under the same named project rule pack.",
        unknown: "Neither the formal target nor its explicit opposite is derivable under the named project rule pack."
      }[proof.proof_state] || "A formal outcome was computed under the named project rule pack.";
      boundary.textContent = stateBoundary + " This is not a certificate of truth, scientific meaning, or evidentiary support.";
      detailContent.appendChild(boundary);
    } else if (node.kind === "run") {
      assessmentControl.hidden = true;
      const boundary = document.createElement("p");
      boundary.className = "scope";
      const checkpointBoundary = node.stage_trace
        ? "Cooperative checkpoints are child-program self-report that locked callsites were reached. They are not independent observation of stage computation, scientific meaning, or in-memory values. "
        : "Internal stages and pathless or in-memory intermediates remain declared, not runtime-observed. ";
      const gateBoundary = node.current_gate_role === "historical_replaced"
        ? "This stale historical run is retained for audit and replaced at the current evidence gate by the linked later run and replay certificates. Its event-store and run-link integrity remain displayed separately. "
        : (node.evidence_eligible
          ? "This run is mechanically eligible under the current event-store and start/finish-link checks. "
          : "This historical run is quarantined by an event-store or start/finish-link integrity failure and cannot supply current evidence. ");
      boundary.textContent = gateBoundary +
        "The receipt and any replay certificate cover declared input/output bytes and visible fresh-workspace effects at the process boundary. A path-bearing internal output is automatically classified as a materialized intermediate and hashed after the process; that file-boundary observation is not attributed to a stage. " +
        checkpointBoundary +
        "The child is not sandboxed from network or external filesystem writes; byte-repeatable replay is not proof of universal determinism or scientific validity.";
      detailContent.appendChild(boundary);
    } else {
      const review = configureAssessmentSelector(node);
      if (review) appendAssessmentPanel(review);
      else appendDeclaredLinkNotice(node);
    }
  }

  function resetLayout() {
    if (!usesPersistentFullLayout()) {
      setManualLayout(false);
      applyVisibility(true);
      fitViewport();
      layoutStatus.textContent =
        "Visible nodes were auto-arranged from the current projection and active filters. The saved full layout, graph, and provenance are unchanged.";
      return;
    }
    nodes.forEach(function (node, key) {
      const position = defaultPositions.get(key);
      node.x = position.x;
      node.y = position.y;
      fullLayoutPositions.set(key, {x: position.x, y: position.y});
      const element = nodeElements.get(key);
      if (element) {
        element.setAttribute("transform", "translate(" + node.x + " " + node.y + ")");
      }
    });
    restoreFullLayoutPositions();
    applyVisibility(false);
    fullLayoutIsManual = false;
    setManualLayout(false);
    try {
      window.localStorage.removeItem(layoutStorageKey);
      window.localStorage.removeItem(legacyLayoutStorageKey);
      layoutStatus.textContent =
        "Automatic layout restored and the browser-local arrangement cleared. The graph and provenance were not changed.";
    } catch (error) {
      layoutStatus.textContent =
        "Automatic layout restored for this page, but browser storage could not be cleared; a reload may restore the prior arrangement. The graph is unchanged.";
    }
    fitViewport();
  }

  const nudgeStep = 24;
  moveButtons.left.addEventListener("click", function () {
    nudgeSelected(-nudgeStep, 0, "left");
  });
  moveButtons.up.addEventListener("click", function () {
    nudgeSelected(0, -nudgeStep, "up");
  });
  moveButtons.down.addEventListener("click", function () {
    nudgeSelected(0, nudgeStep, "down");
  });
  moveButtons.right.addEventListener("click", function () {
    nudgeSelected(nudgeStep, 0, "right");
  });
  zoomOutButton.addEventListener("click", function () {
    zoomViewportBy(1 / viewportZoomStep);
  });
  zoomResetButton.addEventListener("click", function () {
    const center = viewportClientCenter();
    setViewportZoom(1, center.x, center.y);
  });
  zoomInButton.addEventListener("click", function () {
    zoomViewportBy(viewportZoomStep);
  });
  viewportFitButton.addEventListener("click", fitViewport);
  graphScroll.addEventListener("wheel", handleViewportWheel, {passive: false});
  graphScroll.addEventListener("pointerdown", beginPan);
  graphScroll.addEventListener("pointermove", continuePan);
  graphScroll.addEventListener("pointerup", finishPan);
  graphScroll.addEventListener("pointercancel", cancelPan);
  graphScroll.addEventListener("lostpointercapture", cancelPan);
  graphScroll.addEventListener("keydown", handleViewportKeydown);
  layoutReset.addEventListener("click", resetLayout);
  projectionOverview.addEventListener("click", function () {
    setProjectionMode("overview");
    fitViewport();
  });
  projectionFull.addEventListener("click", function () {
    resetProjectionFilters();
    setProjectionMode("full");
    fitViewport();
  });
  expandUpstream.addEventListener("click", function () {
    if (expandSelectedOneHop(incoming)) fitViewport();
  });
  expandDownstream.addEventListener("click", function () {
    if (expandSelectedOneHop(outgoing)) fitViewport();
  });
  expandBoth.addEventListener("click", function () {
    if (!selected) return;
    projectionOneHop(selected, incoming).forEach(function (key) {
      revealedNodeKeys.add(key);
    });
    projectionOneHop(selected, outgoing).forEach(function (key) {
      revealedNodeKeys.add(key);
    });
    projectionMode = "custom";
    applyVisibility(true);
    fitViewport();
  });
  collapseFocusButton.addEventListener("click", function () {
    if (!selected) return;
    revealedNodeKeys.clear();
    revealedNodeKeys.add(selected);
    projectionMode = "custom";
    applyVisibility(true);
    centerNodeInViewport(nodes.get(selected));
  });
  window.addEventListener("blur", function () {
    if (dragState) cancelDrag({pointerId: dragState.pointerId});
    if (panState) cancelPan({pointerId: panState.pointerId});
  });

  layerSelect.addEventListener("change", updateHighlights);
  focusSelect.addEventListener("change", function () { selectNode(focusSelect.value); });
  edgeSelect.addEventListener("change", function () { selectEdge(edgeSelect.value); });
  assessmentSelect.addEventListener("change", function () {
    selectedAssessment = assessmentSelect.value;
    updateDetails();
  });
  updateDetails();
})();
</script>
</body>
</html>
"""


def _render_html(payload):
    return _HTML_HEAD + _HTML_SCRIPT.replace(
        "__PROVSLEUTH_DATA__", _json_for_html(payload)
    )


def render_view(cfg, output_path):
    """Write a deterministic standalone HTML view and return a compact CLI summary."""
    report = build_report(cfg, strict=False)
    layout_source = os.path.normcase(str(cfg.config_path.resolve())).encode("utf-8")
    payload = _build_payload(report, hashlib.sha256(layout_source).hexdigest())
    html = _render_html(payload)
    destination = Path(output_path).expanduser().resolve()
    protected = {cfg.config_path.resolve(), cfg.graph_path.resolve()}
    if getattr(cfg, "verifiers", None) is not None:
        protected.add(Path(cfg.verifiers).resolve(strict=False))
    for asset in [
        *getattr(cfg, "logic_vocabulary_paths", []),
        *getattr(cfg, "logic_rule_pack_paths", []),
        *getattr(cfg, "semantic_terminology_paths", []),
        *getattr(cfg, "semantic_ontology_lock_paths", []),
    ]:
        protected.add(Path(asset).resolve(strict=False))
    for lock in (((report.get("semantics") or {}).get("assets") or {})
                 .get("ontology_locks") or []):
        declared_lock_path = Path(lock["path"])
        lock_path = (
            declared_lock_path if declared_lock_path.is_absolute()
            else cfg.base / declared_lock_path
        ).resolve(strict=False)
        lock_base = lock_path.parent
        for item in lock.get("documents") or []:
            protected.add((lock_base / item["path"]).resolve(strict=False))
        if isinstance(lock.get("index"), dict) and lock["index"].get("path"):
            protected.add((lock_base / lock["index"]["path"]).resolve(strict=False))
    for node in (report.get("graph") or {}).get("nodes", []):
        if node.get("path"):
            declared = cfg.resolve(node["path"]).resolve(strict=False)
            protected.add(declared)
            protected.add(Path(str(declared) + ".manifest.json").resolve(strict=False))
    inside_provenance = False
    for store in (
        cfg.events_path, cfg.assessments_path, cfg.deliberation_path,
        cfg.derivations_path,
        cfg.semantic_mappings_path, cfg.semantic_policies_path,
    ):
        try:
            inside_store = destination.is_relative_to(store.resolve())
        except AttributeError:  # Python 3.9
            try:
                destination.relative_to(store.resolve())
                inside_store = True
            except ValueError:
                inside_store = False
        inside_provenance = inside_provenance or inside_store
    if destination in protected or inside_provenance:
        raise GraphError(f"refusing to overwrite provenance or graph-declared file: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=destination.parent,
            prefix=destination.name + ".", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(html)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return {
        "path": str(destination),
        "nodes": payload["summary"]["semantic_nodes"],
        "edges": payload["summary"]["semantic_edges"],
        "runs": payload["summary"]["runs"],
        "layers": len(payload["layers"]),
    }
