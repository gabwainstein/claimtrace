"""Deterministic, JSON-safe read model for graph checks and future visualizations.

The report deliberately separates structural/provenance findings from numeric verification and
scientific validity. It is a projection of declared provenance plus the disk checks performed by
``compute_check``; it does not turn a green graph into a scientific-truth claim.
"""
from __future__ import annotations

import copy
import json
import os
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from . import __version__
from .assessment import (SCHEMA_VERSION as ASSESSMENT_SCHEMA_VERSION,
                         SUPPORTED_SCHEMA_VERSIONS as SUPPORTED_ASSESSMENT_SCHEMA_VERSIONS,
                         CLAIM_TYPES as ASSESSMENT_CLAIM_TYPES,
                         RESULT_TYPES as ASSESSMENT_RESULT_TYPES,
                         evaluate_assessment, load_assessments)
from .engine import (_ordered_subset, build_adj, compute_check, direct_inputs,
                     lint_issues, load_graph, load_raw)
from .events import (CONTRACT_EVENT_SCHEMA, CONTRACT_EVENT_SCHEMAS, EVENT_SCHEMA,
                     STAGE_CONTRACT_EVENT_SCHEMA,
                     SUPPORTED_EVENT_SCHEMAS, RUN_ID_RE, load_active_markers,
                     load_events, materialize_runs, snapshot_file)
from .logic import (DERIVATION_SCHEMA, LogicError, derivations_path,
                    evaluate_derivation, load_derivations, load_logic_asset, load_rule_pack,
                    load_vocabulary, validate_graph_logic_declarations)
from .semantics import (MAPPING_SCHEMA, POLICY_SCHEMA, SemanticError,
                        configured_semantic_assets, current_mapping_leaves,
                        detect_mapping_conflicts,
                        evaluate_active_semantic_policy,
                        _evaluate_active_semantic_policy_from_snapshot,
                        _evaluate_mappings_from_assets, evaluate_mappings,
                        load_mappings, load_policies)
from .method_assessment import (SCHEMA_VERSION as METHOD_ASSESSMENT_SCHEMA,
                                evaluate_method_assessment, load_method_assessments)
from .pipeline import (LEGACY_SNAPSHOT_SCHEMA, PipelineError,
                       pipeline_snapshots_equivalent, resolve_pipeline_contract,
                       validate_pipeline_snapshot,
                       validate_stage_trace_against_snapshot)
from .replay import (REPLAY_SCHEMA, STAGE_REPLAY_SCHEMA, SUPPORTED_REPLAY_SCHEMAS,
                     evaluate_replay_certificate, load_replay_certificates)

REPORT_SCHEMA_VERSION = "1.7"
_SEVERITY_RANK = {"error": 0, "warning": 1, "pending": 2, "info": 3}


def _canonical_json(value):
    """Return a stable JSON representation suitable for ordering JSON-derived values."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _item_sort_key(item, primary_keys):
    """Sort a JSON object by selected fields, with its full canonical form as a tie-breaker."""
    if not isinstance(item, dict):
        return tuple("" for _ in primary_keys) + (_canonical_json(item),)
    return tuple(_canonical_json(item.get(key)) for key in primary_keys) + (
        _canonical_json(item),)


def _base_report(strict):
    blocking = ["error", "warning", "pending"] if strict else ["error"]
    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "tool": {"name": "claimtrace", "version": __version__},
        "scope": {
            "dependency_coverage": "graph_and_run_declarations",
            "runtime_observation": "partial_reads_and_write_causation_not_observed",
            "numeric_verification": "not-run",
            "scientific_validity": "not-assessed",
            "semantic_assessment": "external-agent-judgement-policy-checked-not-truth",
            "symbolic_logic": "conditional_derivability_under_project_rules_not_truth",
            "semantic_normalization": (
                "attributed_mapping_review_states_under_pinned_local_snapshots_not_support"
            ),
            "execution_repeatability": (
                "fresh_workspace_declared_file_boundary_not_universal_determinism"
            ),
            "method_conformance": (
                "external_agent_judgement_distinct_review_exact_code_and_method_bytes"
            ),
        },
        "policy": {"strict": bool(strict), "blocking_severities": blocking},
    }


def _normalise_node_id(node_id):
    """Use JSON null for graph-level findings and strings for declared node identifiers."""
    if node_id in (None, "-"):
        return None
    return str(node_id)


def _add_finding(found, *, severity, code, node_id, detail, source):
    """Add or merge one finding; an error dominates an identical lint warning."""
    node_id = _normalise_node_id(node_id)
    code = str(code)
    detail = str(detail)
    key = (code, node_id, detail)
    existing = found.get(key)
    if existing is None:
        found[key] = {
            "severity": severity,
            "code": code,
            "node_id": node_id,
            "detail": detail,
            "sources": {source},
        }
        return
    if _SEVERITY_RANK[severity] < _SEVERITY_RANK[existing["severity"]]:
        existing["severity"] = severity
    existing["sources"].add(source)


def _findings(problems, warnings, pending, strict, extra=()):
    found = {}
    for code, node_id, detail in problems:
        _add_finding(found, severity="error", code=code, node_id=node_id,
                     detail=detail, source="check")
    for code, node_id, detail in warnings:
        _add_finding(found, severity="warning", code=code, node_id=node_id,
                     detail=detail, source="lint")
    for node_id, _node_type, movement, _path in pending:
        _add_finding(found, severity="pending", code="PENDING", node_id=node_id,
                     detail=movement, source="node_status")
    for item in extra:
        _add_finding(
            found,
            severity=item["severity"],
            code=item["code"],
            node_id=item.get("node_id"),
            detail=item["detail"],
            source=item.get("source", "receipts"),
        )

    out = []
    for finding in found.values():
        finding["sources"] = sorted(finding["sources"])
        finding["blocking"] = (finding["severity"] == "error" or
                               strict and finding["severity"] in ("warning", "pending"))
        out.append(finding)
    out.sort(key=lambda item: (
        _SEVERITY_RANK[item["severity"]],
        item["code"],
        "" if item["node_id"] is None else item["node_id"],
        item["detail"],
    ))
    return out


def _graph_projection(raw):
    """Return a canonical graph copy plus an independent stable trajectory order."""
    nodes_by_id, edges, concepts = load_graph(None, raw=raw)
    down, _up = build_adj(edges)
    trajectory_order = _ordered_subset(nodes_by_id, nodes_by_id, down)
    metadata = {key: copy.deepcopy(raw[key]) for key in sorted(raw)
                if key not in {"schema_version", "concepts", "nodes", "edges"}}
    return {
        "schema_version": copy.deepcopy(raw.get("schema_version")),
        "concepts": {key: copy.deepcopy(concepts[key]) for key in sorted(concepts)},
        "nodes": sorted((copy.deepcopy(item) for item in raw["nodes"]),
                        key=lambda item: _item_sort_key(item, ("id", "type", "status"))),
        "edges": sorted((copy.deepcopy(item) for item in edges),
                        key=lambda item: _item_sort_key(item, ("from", "to", "rel"))),
        "trajectory_order": trajectory_order,
        "metadata": metadata,
    }


def _path_identity(cfg, value):
    path = Path(value)
    resolved = path.resolve(strict=False) if path.is_absolute() else cfg.resolve(value).resolve(strict=False)
    return os.path.normcase(str(resolved))


def _compact_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        return None
    return {key: copy.deepcopy(snapshot[key]) for key in
            ("path", "state", "sha256", "file_version_id", "size", "method", "reason", "error")
            if key in snapshot}


def _compact_run(run):
    start, finish = run.get("start"), run.get("finish")
    plan = start.get("payload", {}).get("plan", {}) if start else {}
    finish_payload = finish.get("payload", {}) if finish else {}
    input_transitions = []
    for item in finish_payload.get("input_transitions", []):
        input_transitions.append({
            "path": item.get("path"),
            "transition": item.get("transition"),
            "before": _compact_snapshot(item.get("before")),
            "after": _compact_snapshot(item.get("after")),
        })
    transitions = []
    for item in finish_payload.get("output_transitions", []):
        transitions.append({
            "path": item.get("path"),
            "transition": item.get("transition"),
            "produced": bool(item.get("produced")),
            "before": _compact_snapshot(item.get("before")),
            "after": _compact_snapshot(item.get("after")),
        })
    intermediate_transitions = []
    for item in finish_payload.get("intermediate_transitions", []):
        intermediate_transitions.append({
            "path": item.get("path"),
            "transition": item.get("transition"),
            "produced": bool(item.get("produced")),
            "before": _compact_snapshot(item.get("before")),
            "after": _compact_snapshot(item.get("after")),
        })
    return {
        "run_id": run["run_id"],
        "event_schema_version": (
            start.get("schema_version") if start else finish.get("schema_version") if finish else None
        ),
        "start_event_id": start.get("id") if start else None,
        "finish_event_id": finish.get("id") if finish else None,
        "started_at": start.get("recorded_at") if start else None,
        "finished_at": finish.get("recorded_at") if finish else None,
        "name": start.get("payload", {}).get("name") if start else None,
        "plan_id": (start.get("payload", {}).get("plan_id") if start else None),
        "computation_id": plan.get("computation_id"),
        "pipeline_contract_id": (
            plan.get("pipeline_contract", {}).get("id")
            if isinstance(plan.get("pipeline_contract"), dict) else None
        ),
        "pipeline_contract": copy.deepcopy(plan.get("pipeline_contract")),
        "stage_trace_plan": copy.deepcopy(plan.get("stage_trace_plan")),
        "stage_trace_binding": copy.deepcopy(
            start.get("payload", {}).get("stage_trace_binding") if start else None
        ),
        "stage_trace": copy.deepcopy(finish_payload.get("stage_trace")),
        "pipeline_contract_state": "not_evaluated",
        "result_id": finish_payload.get("result_id"),
        "argv": copy.deepcopy(plan.get("argv", [])),
        "argv_capture": plan.get("argv_capture"),
        "cwd": plan.get("cwd"),
        "parameters": copy.deepcopy(plan.get("parameters", {})),
        "seeds": copy.deepcopy(plan.get("seeds", {})),
        "declared_inputs": [item.get("path") for item in plan.get("declared_inputs", [])],
        "declared_outputs": copy.deepcopy(plan.get("declared_outputs", [])),
        "declared_intermediates": copy.deepcopy(plan.get("declared_intermediates", [])),
        "outcome": finish_payload.get("outcome"),
        "direct_child_returncode": finish_payload.get("direct_child_returncode"),
        "contract_errors": copy.deepcopy(finish_payload.get("contract_errors", [])),
        "input_transitions": input_transitions,
        "output_transitions": transitions,
        "intermediate_transitions": intermediate_transitions,
        "window_deltas": copy.deepcopy(finish_payload.get("window_deltas", [])),
        "receipt_integrity": finish_payload.get("receipt_integrity"),
        "lineage_coverage": copy.deepcopy(finish_payload.get("lineage_coverage")),
        "replays": [],
        "bindings": [],
    }


def _pipeline_state(cfg, start):
    if not start:
        return "missing_receipt", "run start event is missing"
    if start.get("schema_version") not in CONTRACT_EVENT_SCHEMAS:
        return "legacy_uncontracted", None
    plan = start.get("payload", {}).get("plan", {})
    snapshot = plan.get("pipeline_contract")
    try:
        validate_pipeline_snapshot(snapshot)
        current = resolve_pipeline_contract(
            cfg, snapshot["source"]["path"],
            declared_inputs=[item["path"] for item in plan.get("declared_inputs", [])],
            declared_outputs=list(plan.get("declared_outputs", [])),
            parameters=dict(plan.get("parameters", {})),
            seeds=dict(plan.get("seeds", {})),
            snapshot_schema=snapshot["schema_version"],
        )
    except (KeyError, PipelineError, TypeError, ValueError) as exc:
        return "stale_or_invalid", str(exc)
    if not pipeline_snapshots_equivalent(snapshot, current):
        return "stale_or_invalid", "current pipeline snapshot has a different content ID"
    return "current", None


def _pipeline_output_role_key(run):
    snapshot = run.get("pipeline_contract")
    if not isinstance(snapshot, dict):
        return ()
    roles = snapshot.get("roles")
    if not isinstance(roles, dict):
        return ()
    outputs = roles.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        return ()
    key = []
    for item in outputs:
        if (not isinstance(item, dict)
                or not isinstance(item.get("node_id"), str)
                or not isinstance(item.get("path"), str)):
            return ()
        key.append((item["node_id"], item["path"]))
    return tuple(sorted(key))


def _run_has_current_output_role_bindings(run, output_role_key):
    bound = {
        (item.get("node_id"), item.get("path"))
        for item in run.get("bindings", [])
        if item.get("binding_kind") == "output_path"
        and item.get("current") is True
        and item.get("declaration_comparison") == "declarations_agree"
    }
    return bool(output_role_key) and bound == set(output_role_key)


def _replacement_ready_run(cfg, run):
    if (run.get("event_schema_version") not in {
            CONTRACT_EVENT_SCHEMA, STAGE_CONTRACT_EVENT_SCHEMA}
            or not run.get("evidence_eligible")
            or run.get("outcome") != "succeeded"
            or run.get("pipeline_contract_state") != "current"
            or run.get("replay_conflict")):
        return False
    output_role_key = _pipeline_output_role_key(run)
    if not _run_has_current_output_role_bindings(run, output_role_key):
        return False
    ready_replays = [
        item for item in run.get("replays", [])
        if item.get("current_derived", {}).get("review_ready_current") is True
    ]
    if not ready_replays:
        return False
    if cfg.require_stage_checkpoints:
        return (
            run.get("event_schema_version") == STAGE_CONTRACT_EVENT_SCHEMA
            and any(
                item.get("current_derived", {}).get(
                    "stage_trace_repeatable_current"
                ) is True
                for item in ready_replays
            )
        )
    return True


def _parse_recorded_at(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None


def _receipt_projection(cfg, raw):
    events, event_issues = load_events(cfg.events_path)
    runs, run_issues = materialize_runs(events)
    markers, marker_issues = load_active_markers(cfg.root)
    replay_documents, replay_issues = load_replay_certificates(cfg.replays_path)
    nodes, edges, _concepts = load_graph(cfg, raw=raw)
    extra = []

    for issue in event_issues:
        extra.append({"severity": "error", "code": issue["code"], "node_id": None,
                      "detail": f"{issue['event_path']}: {issue['detail']}"})
    for issue in run_issues:
        extra.append({"severity": "error", "code": issue["code"], "node_id": None,
                      "detail": f"{issue['run_id']}: {issue['detail']}"})
    for issue in marker_issues:
        extra.append({"severity": "error", "code": issue["code"], "node_id": None,
                      "detail": f"{issue['marker']}: {issue['detail']}"})
    for issue in replay_issues:
        extra.append({
            "severity": "error", "code": issue["code"], "node_id": None,
            "detail": f"{issue['path']}: {issue['detail']}", "source": "replays",
        })
    for marker in markers:
        extra.append({"severity": "pending", "code": "RUN_INCOMPLETE", "node_id": None,
                      "detail": f"{marker['run_id']} has an active out-of-worktree start marker"})

    active_by_path = {}
    for node_id, node in nodes.items():
        if not node.get("path") or node.get("status") not in {"current", "confirmed"}:
            continue
        active_by_path.setdefault(_path_identity(cfg, node["path"]), []).append(node_id)
    for identity, node_ids in sorted(active_by_path.items()):
        if len(node_ids) > 1:
            display = nodes[node_ids[0]].get("path", identity)
            extra.append({
                "severity": "error", "code": "DUPLICATE_ACTIVE_PATH", "node_id": None,
                "detail": f"{display} is claimed by active nodes {', '.join(sorted(node_ids))}",
            })

    projected_runs = [_compact_run(run) for run in runs]
    projected_by_id = {run["run_id"]: run for run in projected_runs}
    raw_by_id = {run["run_id"]: run for run in runs}
    run_issues_by_id = defaultdict(list)
    for issue in run_issues:
        run_issues_by_id[issue["run_id"]].append({
            "code": issue["code"], "detail": issue["detail"],
        })
    event_store_integrity = "error" if event_issues else "ok"
    for projected_run in projected_runs:
        link_issues = run_issues_by_id.get(projected_run["run_id"], [])
        projected_run.update({
            "event_store_integrity": event_store_integrity,
            "link_integrity": "error" if link_issues else "ok",
            "link_issues": copy.deepcopy(link_issues),
            "evidence_eligible": not event_issues and not link_issues,
        })
    for raw_run in runs:
        state, detail = _pipeline_state(cfg, raw_run.get("start"))
        projected_by_id[raw_run["run_id"]]["pipeline_contract_state"] = state
        if state == "stale_or_invalid":
            extra.append({
                "severity": "warning", "code": "RUN_PIPELINE_CONTRACT_STALE",
                "node_id": None,
                "detail": f"{raw_run['run_id']}: {detail}", "source": "receipts",
                "_related_run_id": raw_run["run_id"],
                "_natural_historical_drift": (
                    detail == "current pipeline snapshot has a different content ID"
                ),
            })

    for certificate in replay_documents:
        raw_run = raw_by_id.get(certificate["source_run_id"])
        if raw_run is None:
            evaluation = evaluate_replay_certificate(cfg, certificate)
        else:
            evaluation = evaluate_replay_certificate(
                cfg, certificate, start=raw_run.get("start"), finish=raw_run.get("finish"),
            )
        suppression_findings = []
        if event_issues:
            suppression_findings.append({
                "code": "REPLAY_EVENT_STORE_INTEGRITY",
                "detail": "event-store integrity is not established",
            })
        if replay_issues:
            suppression_findings.append({
                "code": "REPLAY_STORE_INTEGRITY",
                "detail": "replay-store integrity is not established",
            })
        source_link_issues = run_issues_by_id.get(certificate["source_run_id"], [])
        if source_link_issues:
            suppression_findings.append({
                "code": "REPLAY_SOURCE_RUN_LINK_INTEGRITY",
                "detail": "source run has materialization/link integrity issues: "
                + ", ".join(item["code"] for item in source_link_issues),
            })
        if suppression_findings:
            evaluation = copy.deepcopy(evaluation)
            evaluation.update({
                "current": False,
                "byte_repeatable_current": False,
                "review_ready_current": False,
                "stage_trace_repeatable_current": False,
                "findings": sorted(
                    [*evaluation["findings"], *suppression_findings],
                    key=lambda item: (item["code"], item["detail"]),
                ),
            })
        projected = {
            "id": certificate["id"],
            "schema_version": certificate.get("schema_version"),
            "observed_at": certificate["observed_at"],
            "outcome": certificate["outcome"],
            "attempt_count": len(certificate["attempts"]),
            "comparison": copy.deepcopy(certificate["comparison"]),
            "coverage": copy.deepcopy(certificate["coverage"]),
            "current_derived": evaluation,
            "undeclared_write_paths": sorted({
                item["path"] for attempt in certificate["attempts"]
                for item in attempt["undeclared_writes"]
            }),
            "source_materialized_intermediates": [
                _compact_snapshot(item)
                for item in certificate.get("source_materialized_intermediates", [])
            ],
            "source_stage_trace": copy.deepcopy(
                certificate.get("source_stage_trace")
            ),
        }
        if raw_run is not None:
            projected_by_id[certificate["source_run_id"]]["replays"].append(projected)
        else:
            extra.append({
                "severity": "error", "code": "REPLAY_SOURCE_STALE", "node_id": None,
                "detail": f"{certificate['id']}: source run is absent", "source": "replays",
            })
        for finding in evaluation["findings"]:
            extra.append({
                "severity": "warning" if cfg.require_replay else "info",
                "code": finding["code"], "node_id": None,
                "detail": f"{certificate['id']}: {finding['detail']}", "source": "replays",
                "_related_run_id": certificate["source_run_id"],
            })
        if projected["undeclared_write_paths"]:
            extra.append({
                "severity": "warning" if cfg.require_replay else "info",
                "code": "REPLAY_UNDECLARED_WORKSPACE_WRITE", "node_id": None,
                "detail": (
                    f"{certificate['id']}: fresh attempts wrote undeclared workspace paths: "
                    + ", ".join(projected["undeclared_write_paths"])
                ),
                "source": "replays",
            })
        if certificate["outcome"] != "byte_repeatable":
            extra.append({
                "severity": "warning" if cfg.require_replay else "info",
                "code": "REPLAY_NOT_BYTE_REPEATABLE", "node_id": None,
                "detail": f"{certificate['id']}: outcome is {certificate['outcome']}",
                "source": "replays",
            })
    for run in projected_runs:
        current_replays = [
            item for item in run["replays"]
            if item["current_derived"]["current"]
        ]
        contradictory = [
            item for item in current_replays
            if not item["current_derived"]["review_ready_current"]
        ]
        positive = [
            item for item in current_replays
            if item["current_derived"]["review_ready_current"]
        ]
        conflict_ids = sorted({
            item["id"] for item in [*contradictory, *positive]
        }) if contradictory and positive else []
        run["replay_conflict"] = bool(conflict_ids)
        run["replay_conflict_certificate_ids"] = conflict_ids
        if conflict_ids:
            detail = (
                "current replay certificates for the same source run disagree on "
                "review readiness: " + ", ".join(conflict_ids)
            )
            for item in positive:
                evaluation = item["current_derived"]
                evaluation["review_ready_current"] = False
                evaluation["findings"] = sorted(
                    [*evaluation["findings"], {
                        "code": "REPLAY_CURRENT_EVIDENCE_CONFLICT",
                        "detail": detail,
                    }],
                    key=lambda finding: (finding["code"], finding["detail"]),
                )
            extra.append({
                "severity": "error",
                "code": "REPLAY_CURRENT_EVIDENCE_CONFLICT",
                "node_id": None,
                "detail": f"{run['run_id']}: {detail}",
                "source": "replays",
            })
    for run in projected_runs:
        run["replays"].sort(key=lambda item: (item["observed_at"], item["id"]))

    # A failed scientific/analytic command is legitimate historical evidence. A provenance
    # contract failure is different: strict checking must surface it, and mutation of claimtrace's
    # own control plane is always a hard integrity error even for a run with no declared outputs.
    for raw_run in runs:
        finish = raw_run.get("finish")
        if not finish:
            continue
        payload = finish.get("payload", {})
        outcome = payload.get("outcome")
        contract_errors = payload.get("contract_errors", [])
        detail = "; ".join(str(item) for item in contract_errors) or f"run outcome is {outcome}"
        control_mutated = bool(
            payload.get("control_plane", {}).get("forbidden_change_during_child")
        )
        if control_mutated:
            extra.append({
                "severity": "error",
                "code": "RUN_CONTROL_PLANE_MUTATION",
                "node_id": None,
                "detail": f"{raw_run['run_id']}: {detail}",
            })
        elif outcome in {"contract_failed", "capture_precondition_failed"}:
            extra.append({
                "severity": "warning",
                "code": "RUN_CAPTURE_CONTRACT_FAILED",
                "node_id": None,
                "detail": f"{raw_run['run_id']}: {detail}",
            })

    # Semantic notebook nodes may explicitly cite the mechanical runs that produced their verdict.
    # This is an attributed link, not an inferred file binding, and it is valid for null/dead-end
    # results as well as positive ones.
    for node_id, node in sorted(nodes.items()):
        references = node.get("run_ids", [])
        for run_id in sorted(set(references)):
            if not RUN_ID_RE.fullmatch(run_id):
                extra.append({
                    "severity": "error", "code": "INVALID_RUN_REFERENCE", "node_id": node_id,
                    "detail": f"invalid run id in run_ids: {run_id!r}",
                })
                continue
            raw_run = raw_by_id.get(run_id)
            if not raw_run:
                extra.append({
                    "severity": "error", "code": "MISSING_RUN_REFERENCE", "node_id": node_id,
                    "detail": f"referenced receipt {run_id} is not present in the event ledger",
                })
                continue
            projected_by_id[run_id]["bindings"].append({
                "binding_kind": "explicit_run_reference",
                "node_id": node_id,
                "path": node.get("path"),
                "declaration_comparison": None,
                "output_evidence": None,
                "current": projected_by_id[run_id]["evidence_eligible"],
                "integrity_state": (
                    "current" if projected_by_id[run_id]["evidence_eligible"]
                    else "quarantined"
                ),
            })
            finish = raw_run.get("finish")
            outcome = finish.get("payload", {}).get("outcome") if finish else None
            if node.get("status") in {"current", "confirmed", "null"} and outcome != "succeeded":
                extra.append({
                    "severity": "error", "code": "SEMANTIC_RUN_OUTCOME_MISMATCH",
                    "node_id": node_id,
                    "detail": (f"status {node.get('status')} references {run_id} with command "
                               f"outcome {outcome or 'incomplete'}"),
                })
    producing_receipts = {}
    validation_receipts = {}
    materialized_receipts = {}
    for raw_run in runs:
        start, finish = raw_run.get("start"), raw_run.get("finish")
        if not start or not finish:
            continue
        evidence_eligible = projected_by_id[raw_run["run_id"]]["evidence_eligible"]
        payload = finish["payload"]
        outcome = payload.get("outcome")
        for item in payload.get("output_transitions", []):
            path = item.get("path")
            if not isinstance(path, str):
                continue
            identity = _path_identity(cfg, path)
            if (evidence_eligible and outcome == "succeeded"
                    and item.get("after", {}).get("state") == "stable"):
                key = (finish.get("recorded_at", ""), finish["id"])
                if (item.get("produced") is True
                        and item.get("transition") in {"created", "content_changed"}):
                    producing_receipts.setdefault(identity, []).append((key, raw_run, item))
                elif (item.get("produced") is False
                      and item.get("transition") == "unchanged"):
                    validation_receipts.setdefault(identity, []).append((key, raw_run, item))
            if not item.get("declared_output", True):
                continue
        for item in payload.get("intermediate_transitions", []):
            path = item.get("path")
            if not isinstance(path, str):
                continue
            identity = _path_identity(cfg, path)
            if (evidence_eligible and outcome == "succeeded"
                    and item.get("after", {}).get("state") == "stable"
                    and item.get("produced") is True
                    and item.get("transition") in {"created", "content_changed"}):
                key = (finish.get("recorded_at", ""), finish["id"])
                materialized_receipts.setdefault(identity, []).append(
                    (key, raw_run, item)
                )
        for delta in payload.get("window_deltas", []):
            if not delta.get("declared_output"):
                extra.append({
                    "severity": "warning", "code": "POSSIBLE_UNDECLARED_OUTPUT", "node_id": None,
                    "detail": (f"{raw_run['run_id']} saw {delta.get('transition')} at "
                               f"{delta.get('path')} in an unattributed pre/post window"),
                })

    # An unchanged pre-existing file is useful validation evidence, but it does not
    # establish which run produced those bytes. Keep that evidence explicitly attached
    # to the graph without allowing it to satisfy the producing-receipt requirement.
    for identity, candidates in sorted(validation_receipts.items()):
        candidates.sort(key=lambda item: item[0])
        node_ids = active_by_path.get(identity, [])
        if len(node_ids) != 1:
            if not node_ids:
                for _key, raw_run, output_item in candidates:
                    extra.append({
                        "severity": "warning", "code": "UNBOUND_RUN_OUTPUT", "node_id": None,
                        "detail": (f"{raw_run['run_id']} validated declared output "
                                   f"{output_item.get('path')} with no active graph node"),
                    })
            continue
        node_id = node_ids[0]
        for _key, raw_run, _output_item in candidates:
            projected_by_id[raw_run["run_id"]]["bindings"].append({
                "binding_kind": "output_validation",
                "node_id": node_id,
                "path": nodes[node_id].get("path"),
                "declaration_comparison": None,
                "output_evidence": "unchanged_not_proven_produced",
            })

    bound_nodes = set()
    for identity, candidates in sorted(producing_receipts.items()):
        candidates.sort(key=lambda item: item[0])
        _key, raw_run, output_item = candidates[-1]
        node_ids = active_by_path.get(identity, [])
        if not node_ids:
            extra.append({
                "severity": "warning", "code": "UNBOUND_RUN_OUTPUT", "node_id": None,
                "detail": f"{raw_run['run_id']} has declared output {output_item.get('path')} with no active graph node",
            })
            continue
        if len(node_ids) != 1:
            continue
        node_id = node_ids[0]
        bound_nodes.add(node_id)
        start_plan = raw_run["start"]["payload"].get("plan", {})
        declared = {item.get("path") for item in start_plan.get("declared_inputs", [])
                    if isinstance(item.get("path"), str)}
        pipeline_snapshot = start_plan.get("pipeline_contract")
        if (isinstance(pipeline_snapshot, dict)
                and isinstance(pipeline_snapshot.get("roles"), dict)):
            expected = {
                item.get("path")
                for role in ("code", "inputs")
                for item in pipeline_snapshot["roles"].get(role, [])
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            }
        else:
            expected = set(direct_inputs(cfg, nodes, edges, node_id))
        declared_ids = {_path_identity(cfg, path): path for path in declared}
        expected_ids = {_path_identity(cfg, path): path for path in expected}
        missing = sorted(expected_ids[key] for key in set(expected_ids) - set(declared_ids))
        extra_declared = sorted(declared_ids[key] for key in set(declared_ids) - set(expected_ids))
        agreement = "declarations_agree" if not missing and not extra_declared else "declarations_differ"
        binding = {
            "binding_kind": "output_path",
            "node_id": node_id,
            "path": nodes[node_id].get("path"),
            "declaration_comparison": agreement,
            "output_evidence": "content_transition_detected",
            "graph_declared_inputs": sorted(expected),
            "run_declared_inputs": sorted(declared),
            "current": True,
        }
        projected_by_id[raw_run["run_id"]]["bindings"].append(binding)
        if missing:
            extra.append({
                "severity": "warning", "code": "RUN_DECLARATION_INCOMPLETE", "node_id": node_id,
                "detail": "graph inputs absent from run declaration: " + ", ".join(missing),
            })
            binding["current"] = False
        if extra_declared:
            extra.append({
                "severity": "warning", "code": "GRAPH_DECLARATION_INCOMPLETE", "node_id": node_id,
                "detail": "run inputs absent from graph declaration: " + ", ".join(extra_declared),
            })
            binding["current"] = False
        for captured in start_plan.get("declared_inputs", []):
            path = captured.get("path")
            if not isinstance(path, str):
                continue
            current_input = snapshot_file(cfg.resolve(path), path)
            captured_stable = captured.get("state") == "stable"
            current_stable = current_input.get("state") == "stable"
            if (captured_stable and current_stable
                    and captured.get("sha256") == current_input.get("sha256")):
                continue
            extra.append({
                "severity": "error", "code": "RUN_INPUT_DRIFT", "node_id": node_id,
                "detail": (f"{path} differs from input snapshot captured by latest successful "
                           f"output receipt {raw_run['run_id']} "
                           f"(captured={captured.get('state')}, current={current_input.get('state')})"),
            })
            binding["current"] = False
        after = output_item.get("after", {})
        current_path = cfg.resolve(nodes[node_id]["path"])
        current = snapshot_file(current_path, nodes[node_id]["path"])
        output_current = (
            after.get("state") == current.get("state") == "stable"
            and after.get("sha256") == current.get("sha256")
            and after.get("size") == current.get("size")
        )
        if not output_current:
            binding["current"] = False
            extra.append({
                "severity": "error", "code": "RUN_OUTPUT_DRIFT", "node_id": node_id,
                "detail": f"{nodes[node_id]['path']} differs from latest successful output receipt {raw_run['run_id']}",
            })

    # A materialized intermediate is a separately declared file boundary. Its post-process
    # bytes can bind to the graph path and satisfy file-receipt coverage, but this does not
    # attribute the write to the authored producing stage or make it a terminal claim result.
    for identity, candidates in sorted(materialized_receipts.items()):
        candidates.sort(key=lambda item: item[0])
        _key, raw_run, intermediate_item = candidates[-1]
        node_ids = active_by_path.get(identity, [])
        if not node_ids:
            extra.append({
                "severity": "warning", "code": "UNBOUND_MATERIALIZED_INTERMEDIATE",
                "node_id": None,
                "detail": (
                    f"{raw_run['run_id']} has materialized intermediate "
                    f"{intermediate_item.get('path')} with no active graph node"
                ),
            })
            continue
        if len(node_ids) != 1:
            continue
        node_id = node_ids[0]
        bound_nodes.add(node_id)
        binding = {
            "binding_kind": "materialized_intermediate_path",
            "node_id": node_id,
            "path": nodes[node_id].get("path"),
            "declaration_comparison": "contract_role_agrees",
            "output_evidence": "post_process_content_transition_detected",
            "receipt_snapshot": _compact_snapshot(intermediate_item.get("after")),
            "stage_attribution": "not_observed",
            "current": True,
        }
        projected_by_id[raw_run["run_id"]]["bindings"].append(binding)
        after = intermediate_item.get("after", {})
        current = snapshot_file(cfg.resolve(nodes[node_id]["path"]), nodes[node_id]["path"])
        if not (
                after.get("state") == current.get("state") == "stable"
                and after.get("sha256") == current.get("sha256")
                and after.get("size") == current.get("size")):
            binding["current"] = False
            extra.append({
                "severity": "error", "code": "RUN_INTERMEDIATE_DRIFT", "node_id": node_id,
                "detail": (
                    f"{nodes[node_id]['path']} differs from latest successful materialized-"
                    f"intermediate receipt {raw_run['run_id']}"
                ),
            })

    expected_types = set(cfg.render_types) | set(cfg.run_output_types)
    for node_id, node in sorted(nodes.items()):
        if (node.get("status") not in {"current", "confirmed"} or not node.get("path")
                or node.get("type") not in expected_types or node_id in bound_nodes):
            continue
        extra.append({
            "severity": "warning", "code": "NO_RUN_RECEIPT", "node_id": node_id,
            "detail": f"{node['path']} has no successful finalized producing run receipt",
        })

    replacement_candidates = [
        run for run in projected_runs if _replacement_ready_run(cfg, run)
    ]
    historical_replacements = {}
    for historical in projected_runs:
        if historical.get("pipeline_contract_state") != "stale_or_invalid":
            continue
        output_role_key = _pipeline_output_role_key(historical)
        finished_at = _parse_recorded_at(historical.get("finished_at"))
        if not output_role_key or finished_at is None:
            continue
        eligible = []
        for candidate in replacement_candidates:
            started_at = _parse_recorded_at(candidate.get("started_at"))
            if (started_at is not None and started_at > finished_at
                    and _pipeline_output_role_key(candidate) == output_role_key):
                eligible.append(candidate)
        if not eligible:
            continue
        replacement = max(
            eligible,
            key=lambda item: (item.get("started_at") or "", item["run_id"]),
        )
        historical_replacements[historical["run_id"]] = replacement
        ready_replay_ids = sorted(
            item["id"] for item in replacement.get("replays", [])
            if item.get("current_derived", {}).get("review_ready_current") is True
        )
        historical["current_gate_role"] = "historical_replaced"
        historical["replacement_run_id"] = replacement["run_id"]
        historical["replacement_replay_ids"] = ready_replay_ids
        for replay in historical.get("replays", []):
            replay["current_gate_role"] = "historical_replaced"
            replay["replacement_run_id"] = replacement["run_id"]
            replay["replacement_replay_ids"] = ready_replay_ids

    natural_historical_drift_codes = {
        "RUN_PIPELINE_CONTRACT_STALE",
        "REPLAY_INPUT_DRIFT",
        "REPLAY_CONTRACT_DRIFT",
        "REPLAY_ENVIRONMENT_MISMATCH",
    }
    for finding in extra:
        if (finding.get("code") in natural_historical_drift_codes
                and finding.get("_related_run_id") in historical_replacements
                and (
                    finding.get("code") != "RUN_PIPELINE_CONTRACT_STALE"
                    or finding.get("_natural_historical_drift") is True
                )):
            finding["severity"] = "info"

    for run in projected_runs:
        run["bindings"].sort(key=lambda item: (item["node_id"], item.get("binding_kind", "")))
    projected_runs.sort(key=lambda item: (item.get("finished_at") or item.get("started_at") or "", item["run_id"]))
    return {
        "event_schema_version": EVENT_SCHEMA,
        "supported_event_schema_versions": sorted(SUPPORTED_EVENT_SCHEMAS),
        "contract_event_schema_version": CONTRACT_EVENT_SCHEMA,
        "stage_contract_event_schema_version": STAGE_CONTRACT_EVENT_SCHEMA,
        "replay_schema_version": REPLAY_SCHEMA,
        "stage_replay_schema_version": STAGE_REPLAY_SCHEMA,
        "supported_replay_schema_versions": sorted(SUPPORTED_REPLAY_SCHEMAS),
        "integrity": "error" if event_issues or run_issues or marker_issues or replay_issues else "ok",
        "event_store_integrity": event_store_integrity,
        "run_link_integrity": "error" if run_issues else "ok",
        "replay_integrity": "error" if replay_issues else "ok",
        "policy": {
            "require_contracts": cfg.require_execution_contracts,
            "require_replay": cfg.require_replay,
            "replay_attempts": cfg.replay_attempts,
        },
        "active_runs": [marker["run_id"] for marker in markers],
        "runs": projected_runs,
    }, extra


def _assessment_projection(cfg, raw):
    """Project immutable semantic reviews without treating them as scientific truth."""
    documents, integrity_issues = load_assessments(cfg)
    nodes, edges, _concepts = load_graph(cfg, raw=raw)
    extra = []
    for issue in integrity_issues:
        extra.append({
            "severity": "error",
            "code": issue["code"],
            "node_id": None,
            "detail": f"{issue['path']}: {issue['detail']}",
            "source": "assessments",
        })

    superseded = {
        item["review"].get("supersedes_assessment_id")
        for item in documents
        if item["review"].get("supersedes_assessment_id")
    }
    items = []
    active_relations = []
    accepted_by_pair = defaultdict(list)
    for document in sorted(documents, key=lambda item: item["id"]):
        is_current = (
            document["id"] not in superseded
            and document["review"]["state"] != "superseded"
        )
        evaluation = evaluate_assessment(cfg, document, documents)
        # Passing only valid documents to ``evaluate_assessment`` avoids re-reading
        # the store, so that function cannot see rejected files or broken chains.
        # This projection is the trust boundary: one store issue invalidates every
        # current relation while immutable stored derivations remain as history.
        if integrity_issues:
            evaluation = copy.deepcopy(evaluation)
            evaluation["active_relation"] = None
        item = {
            "id": document["id"],
            "schema_version": document["schema_version"],
            "recorded_at": document["recorded_at"],
            "subject": copy.deepcopy(document["subject"]),
            "mechanical_snapshot": copy.deepcopy(document["mechanical_snapshot"]),
            "agent_input": copy.deepcopy(document["agent_input"]),
            "review": copy.deepcopy(document["review"]),
            "stored_derived": copy.deepcopy(document["derived"]),
            "current_derived": copy.deepcopy(evaluation),
            "is_current": is_current,
        }
        items.append(item)
        if not is_current:
            continue

        review_state = evaluation["effective_review_state"]
        claim_id = document["subject"]["claim_id"]
        if review_state == "proposed":
            extra.append({
                "severity": "pending",
                "code": "ASSESSMENT_REVIEW_PENDING",
                "node_id": claim_id,
                "detail": f"{document['id']} awaits acceptance or rejection",
                "source": "assessments",
            })
        if review_state in {"proposed", "accepted", "contested"}:
            for finding in evaluation["findings"]:
                severity = finding["severity"]
                # Accepted narrowing records preserve their acknowledged mismatch as
                # inspectable context without making the accepted `related` link fail strict.
                if review_state == "accepted" and severity == "warning":
                    severity = "info"
                extra.append({
                    "severity": severity,
                    "code": finding["code"],
                    "node_id": claim_id,
                    "detail": f"{document['id']}: {finding['detail']}",
                    "source": "assessments",
                })
        evaluation_errors = [
            finding for finding in evaluation["findings"]
            if finding["severity"] == "error"
        ]
        relation = evaluation.get("active_relation")
        if relation and not integrity_issues:
            for result_id in document["subject"]["result_ids"]:
                active_relations.append({
                    "assessment_id": document["id"],
                    "from": result_id,
                    "to": claim_id,
                    "rel": relation,
                })
        if (review_state == "accepted" and not evaluation_errors
                and not integrity_issues):
            for result_id in document["subject"]["result_ids"]:
                accepted_by_pair[(result_id, claim_id)].append({
                    "assessment_id": document["id"],
                    "verdict": document["agent_input"]["verdict"],
                    "active_relation": relation,
                })

    declared_links = []
    for edge in edges:
        declared_relation = edge.get("rel")
        if declared_relation not in {"supports", "refutes"}:
            continue
        pair = (edge.get("from"), edge.get("to"))
        assessments = sorted(
            accepted_by_pair.get(pair, []), key=lambda item: item["assessment_id"]
        )
        covered = [
            item for item in assessments
            if item["active_relation"] == declared_relation
        ]
        opposite_relation = "refutes" if declared_relation == "supports" else "supports"
        opposite = [
            item for item in assessments
            if item["active_relation"] == opposite_relation
        ]
        if covered:
            status = "covered"
        elif opposite:
            status = "assessed_conflict"
        elif assessments:
            status = "assessed_not_as_written"
        else:
            status = "unassessed"
        declared_links.append({
            "from": pair[0],
            "to": pair[1],
            "declared_relation": declared_relation,
            "status": status,
            "assessment_ids": [item["assessment_id"] for item in assessments],
            "assessed_relations": sorted({
                item["active_relation"] or "none" for item in assessments
            }),
        })
        if status == "covered":
            continue
        if status == "assessed_conflict":
            extra.append({
                "severity": "error",
                "code": "DECLARED_CLAIM_LINK_CONFLICT",
                "node_id": edge.get("to"),
                "detail": (
                    f"declared {declared_relation} {edge.get('from')} -> {edge.get('to')} "
                    f"conflicts with accepted {opposite_relation} assessment(s): "
                    + ", ".join(item["assessment_id"] for item in opposite)
                ),
                "source": "assessments",
            })
        elif status == "assessed_not_as_written":
            extra.append({
                "severity": "warning" if cfg.require_assessments else "info",
                "code": "ASSESSED_CLAIM_LINK_MISMATCH",
                "node_id": edge.get("to"),
                "detail": (
                    f"declared {declared_relation} {edge.get('from')} -> {edge.get('to')} "
                    "was assessed, but not as that relation: "
                    + ", ".join(
                        f"{item['assessment_id']}={item['active_relation'] or 'none'}"
                        for item in assessments
                    )
                ),
                "source": "assessments",
            })
        else:
            extra.append({
                "severity": "warning" if cfg.require_assessments else "info",
                "code": "UNASSESSED_CLAIM_LINK",
                "node_id": edge.get("to"),
                "detail": (
                    f"declared {declared_relation} {edge.get('from')} -> {edge.get('to')} "
                    "has no current accepted semantic assessment"
                ),
                "source": "assessments",
            })

    required_dependencies = []
    for edge in edges:
        if edge.get("rel") != "derives_from":
            continue
        result_id = edge.get("from")
        claim_id = edge.get("to")
        result_node = nodes.get(result_id, {})
        claim_node = nodes.get(claim_id, {})
        if (result_node.get("type") not in ASSESSMENT_RESULT_TYPES
                or claim_node.get("type") not in ASSESSMENT_CLAIM_TYPES):
            continue
        assessments = sorted(
            accepted_by_pair.get((result_id, claim_id), []),
            key=lambda item: item["assessment_id"],
        )
        relation_bearing = [
            item for item in assessments
            if item["active_relation"] in {"supports", "refutes", "related"}
        ]
        if relation_bearing:
            status = "covered"
        elif assessments:
            status = "assessed_without_relation"
        else:
            status = "unassessed"
        required_dependencies.append({
            "from": result_id,
            "to": claim_id,
            "declared_relation": "derives_from",
            "status": status,
            "assessment_ids": [item["assessment_id"] for item in assessments],
            "assessed_relations": sorted({
                item["active_relation"] or "none" for item in assessments
            }),
        })
        if status == "covered":
            continue
        severity = "warning" if cfg.require_assessments else "info"
        if status == "assessed_without_relation":
            extra.append({
                "severity": severity,
                "code": "ASSESSED_CLAIM_DEPENDENCY_WITHOUT_RELATION",
                "node_id": claim_id,
                "detail": (
                    f"structural claim dependency {result_id} -> {claim_id} was "
                    "reviewed, but no accepted semantic relation is active: "
                    + ", ".join(
                        f"{item['assessment_id']}={item['active_relation'] or 'none'}"
                        for item in assessments
                    )
                ),
                "source": "assessments",
            })
        else:
            extra.append({
                "severity": severity,
                "code": "UNASSESSED_CLAIM_DEPENDENCY",
                "node_id": claim_id,
                "detail": (
                    f"structural claim dependency {result_id} -> {claim_id} has no "
                    "current accepted semantic assessment"
                ),
                "source": "assessments",
            })

    active_relations.sort(key=lambda item: (
        item["from"], item["to"], item["rel"], item["assessment_id"],
    ))
    declared_links.sort(key=lambda item: (
        item["from"], item["to"], item["declared_relation"],
    ))
    required_dependencies.sort(key=lambda item: (
        item["from"], item["to"], item["declared_relation"],
    ))
    return {
        "assessment_schema_version": ASSESSMENT_SCHEMA_VERSION,
        "current_assessment_schema_version": ASSESSMENT_SCHEMA_VERSION,
        "supported_assessment_schema_versions": sorted(
            SUPPORTED_ASSESSMENT_SCHEMA_VERSIONS
        ),
        "integrity": "error" if integrity_issues else "ok",
        "policy": {"require_assessments": cfg.require_assessments},
        "items": items,
        "active_relations": active_relations,
        "declared_links": declared_links,
        "required_dependencies": required_dependencies,
    }, extra


def _method_assessment_projection(cfg):
    documents, integrity_issues = load_method_assessments(cfg)
    snapshots_by_id = {
        item["id"]: item["mechanical_snapshot"]["pipeline_contract"]
        for item in documents
    }
    extra = []
    for issue in integrity_issues:
        extra.append({
            "severity": "error", "code": issue["code"], "node_id": None,
            "detail": f"{issue['path']}: {issue['detail']}",
            "source": "method_assessments",
        })
    superseded = {
        item["review"].get("supersedes_assessment_id") for item in documents
        if item["review"].get("supersedes_assessment_id")
    }
    items = []
    for document in sorted(documents, key=lambda item: item["id"]):
        is_current = (
            document["id"] not in superseded
            and document["review"]["state"] != "superseded"
        )
        evaluation = evaluate_method_assessment(
            cfg, document, assessments=documents, store_issues=integrity_issues,
        )
        items.append({
            "id": document["id"],
            "recorded_at": document["recorded_at"],
            "subject": copy.deepcopy(document["subject"]),
            "review": copy.deepcopy(document["review"]),
            "agent_verdict": document["agent_input"]["verdict"],
            "step_alignments": copy.deepcopy(document["agent_input"]["step_alignments"]),
            "current_derived": copy.deepcopy(evaluation),
            "is_current": is_current,
        })

    current_implementation_methods = {
        item["subject"]["method_id"]
        for item in items
        if item["is_current"]
        and item["current_derived"]["implementation_current"]
    }
    for item in items:
        if not item["is_current"]:
            continue
        method_id = item["subject"]["method_id"]
        evaluation = item["current_derived"]
        historical_replaced = (
            evaluation["stale"]
            and method_id in current_implementation_methods
        )
        if historical_replaced:
            item["current_gate_role"] = "historical_replaced"
        if evaluation["effective_review_state"] == "proposed":
            extra.append({
                "severity": "info" if historical_replaced else "pending",
                "code": "METHOD_ASSESSMENT_REVIEW_PENDING",
                "node_id": method_id,
                "detail": f"{item['id']} awaits a distinct review decision",
                "source": "method_assessments",
            })
        for finding in evaluation["findings"]:
            if finding["severity"] == "info":
                continue
            natural_historical_drift = (
                historical_replaced
                and finding["code"] == "METHOD_CONFORMANCE_STALE"
            )
            extra.append({
                "severity": (
                    "info" if natural_historical_drift
                    else "warning" if cfg.require_method_assessments
                    else "info"
                ),
                "code": finding["code"], "node_id": method_id,
                "detail": f"{item['id']}: {finding['detail']}",
                "source": "method_assessments",
            })
    projection = {
        "schema_version": METHOD_ASSESSMENT_SCHEMA,
        "integrity": "error" if integrity_issues else "ok",
        "policy": {"require_method_assessments": cfg.require_method_assessments},
        "items": items,
    }
    return projection, extra, snapshots_by_id


def _claim_stage_ancestry(snapshot, result_id):
    """Return the exact declared stage ancestry for one terminal result node."""
    stages = snapshot.get("stages", [])
    producers = [
        stage for stage in stages if result_id in stage.get("produces_node_ids", [])
    ]
    if len(producers) != 1:
        return [], (
            f"terminal result {result_id} has {len(producers)} producing stages in the "
            "current pipeline snapshot"
        )
    by_id = {stage["id"]: stage for stage in stages}
    ancestry_ids = set()
    stack = [producers[0]["id"]]
    while stack:
        stage_id = stack.pop()
        if stage_id in ancestry_ids:
            continue
        stage = by_id.get(stage_id)
        if stage is None:
            return [], f"pipeline ancestry references missing stage {stage_id}"
        ancestry_ids.add(stage_id)
        stack.extend(stage["depends_on"])
    return [stage for stage in stages if stage["id"] in ancestry_ids], None


def _method_assessment_covers_stage(item, stage):
    """Check one current implementation assessment against one exact stage mapping."""
    return bool(
        item["subject"]["method_id"] == stage["method_id"]
        and item["current_derived"]["implementation_current"]
        and any(
            alignment["stage_id"] == stage["id"]
            and alignment["method_step_id"] == stage["method_step_id"]
            and alignment["alignment"] == "match"
            for alignment in item["step_alignments"]
        )
    )


def _claim_ancestry_intermediates(run, ancestry_stages, nodes):
    """Project current file-boundary evidence only for intermediates on ancestry."""
    snapshot = run["pipeline_contract"]
    roles = {
        item["node_id"]: item
        for item in snapshot["roles"].get("intermediates", [])
        if item.get("materialization") == "declared_file_boundary"
    }
    produced_on_ancestry = {
        node_id for stage in ancestry_stages
        for node_id in stage["produces_node_ids"]
    }
    if snapshot.get("schema_version") == LEGACY_SNAPSHOT_SCHEMA:
        terminal_ids = {
            item["node_id"] for item in snapshot["roles"].get("outputs", [])
        }
        unavailable = []
        for node_id in sorted(produced_on_ancestry - terminal_ids):
            node = nodes.get(node_id, {})
            if not node.get("path"):
                continue
            unavailable.append({
                "node_id": node_id,
                "path": node["path"],
                "binding_state": "legacy_capture_unavailable",
                "receipt_snapshot": None,
            })
        if unavailable:
            return "legacy_capture_unavailable", unavailable
    required = [roles[node_id] for node_id in sorted(produced_on_ancestry & set(roles))]
    bindings = {
        item["node_id"]: item for item in run["bindings"]
        if item.get("binding_kind") == "materialized_intermediate_path"
    }
    projected = []
    for role in required:
        binding = bindings.get(role["node_id"])
        receipt_snapshot = binding.get("receipt_snapshot") if binding else None
        digest = (
            receipt_snapshot.get("sha256")
            if isinstance(receipt_snapshot, dict) else None
        )
        size = (
            receipt_snapshot.get("size")
            if isinstance(receipt_snapshot, dict) else None
        )
        snapshot_present = bool(
            isinstance(receipt_snapshot, dict)
            and receipt_snapshot.get("state") == "stable"
            and receipt_snapshot.get("path") == role["path"]
            and isinstance(digest, str) and len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest)
            and not isinstance(size, bool) and isinstance(size, int) and size >= 0
            and receipt_snapshot.get("file_version_id") == f"file:sha256:{digest}"
        )
        current = bool(
            binding and binding.get("path") == role["path"]
            and binding.get("current") and snapshot_present
        )
        projected.append({
            "node_id": role["node_id"],
            "path": role["path"],
            "binding_state": "current" if current else "missing_or_stale",
            "receipt_snapshot": copy.deepcopy(receipt_snapshot),
        })
    if not projected:
        return "not_required", projected
    if all(item["binding_state"] == "current" for item in projected):
        return "current", projected
    return "missing_or_stale", projected


def _claim_basis_projection(
        cfg, raw, receipts, assessments, method_assessments,
        method_snapshots_by_id):
    """Join semantic meaning to current execution/replay/method evidence without inference."""
    nodes, _edges, _concepts = load_graph(cfg, raw=raw)
    producing_by_node = {}
    for run in receipts["runs"]:
        for binding in run["bindings"]:
            if binding.get("binding_kind") == "output_path":
                producing_by_node[binding["node_id"]] = (run, binding)
    method_items = [
        item for item in method_assessments["items"] if item["is_current"]
    ]
    method_run_match_cache = {}

    def assessment_matches_run(item, run):
        key = (item["id"], run["run_id"])
        if key in method_run_match_cache:
            return method_run_match_cache[key]
        assessment_snapshot = method_snapshots_by_id.get(item["id"])
        run_snapshot = run.get("pipeline_contract")
        try:
            matches = (
                isinstance(assessment_snapshot, dict)
                and pipeline_snapshots_equivalent(
                    assessment_snapshot, run_snapshot,
                )
            )
        except (PipelineError, TypeError, ValueError):
            matches = False
        method_run_match_cache[key] = matches
        return matches

    items = []
    extra = []
    for relation in assessments["active_relations"]:
        result_id, claim_id = relation["from"], relation["to"]
        run_binding = producing_by_node.get(result_id)
        run = run_binding[0] if run_binding else None
        binding = run_binding[1] if run_binding else None
        execution_current = bool(
            run and run.get("evidence_eligible") and binding
            and binding.get("current") and run.get("outcome") == "succeeded"
        )
        if not run:
            execution_state = "missing_producing_receipt"
        elif not execution_current:
            execution_state = "producing_receipt_stale_or_incomplete"
        elif run.get("event_schema_version") in CONTRACT_EVENT_SCHEMAS:
            execution_state = "current_contract_bound_producing_receipt"
        else:
            execution_state = "current_legacy_uncontracted_producing_receipt"

        contract_current = bool(
            run and run.get("event_schema_version") in CONTRACT_EVENT_SCHEMAS
            and run.get("pipeline_contract_state") == "current"
        )
        if not run or run.get("event_schema_version") not in CONTRACT_EVENT_SCHEMAS:
            contract_state = "missing_or_legacy"
        else:
            contract_state = run.get("pipeline_contract_state")
        current_replays = [
            item for item in (run.get("replays", []) if run else [])
            if item["current_derived"]["review_ready_current"]
        ]
        if current_replays:
            replay_state = "byte_repeatable_current"
        elif run and any(
                item["current_derived"]["byte_repeatable_current"]
                and item.get("undeclared_write_paths")
                for item in run.get("replays", [])):
            replay_state = "byte_repeatable_with_undeclared_workspace_writes"
        elif run and run.get("replays"):
            replay_state = "present_but_not_current_or_repeatable"
        elif contract_current:
            replay_state = "missing"
        else:
            replay_state = "unavailable_without_current_contract"

        if not contract_current:
            stage_checkpoint_state = "unavailable_without_current_contract"
        elif run.get("event_schema_version") != STAGE_CONTRACT_EVENT_SCHEMA:
            stage_checkpoint_state = "cooperative_not_requested"
        else:
            stage_trace = run.get("stage_trace")
            try:
                validate_stage_trace_against_snapshot(
                    stage_trace, run["pipeline_contract"],
                )
            except (PipelineError, TypeError, ValueError):
                stage_checkpoint_state = "cooperative_report_invalid"
            else:
                if stage_trace["state"] != "cooperative_report_complete":
                    stage_checkpoint_state = stage_trace["state"]
                elif any(
                        item["current_derived"].get(
                            "stage_trace_repeatable_current", False,
                        )
                        for item in run.get("replays", [])):
                    stage_checkpoint_state = "cooperative_report_repeatable_current"
                else:
                    stage_checkpoint_state = "cooperative_report_complete_source_only"

        ancestry_stages = []
        ancestry_error = None
        if contract_current:
            ancestry_stages, ancestry_error = _claim_stage_ancestry(
                run["pipeline_contract"], result_id,
            )
        ancestry_state = (
            "current"
            if contract_current and ancestry_error is None
            else "invalid" if contract_current
            else "unavailable_without_current_contract"
        )
        producer_stage_id = next((
            stage["id"] for stage in ancestry_stages
            if result_id in stage["produces_node_ids"]
        ), None)
        ancestry_method_steps = [{
            "stage_id": stage["id"],
            "method_id": stage["method_id"],
            "method_step_id": stage["method_step_id"],
        } for stage in ancestry_stages]
        ancestry_pairs = {
            (item["method_id"], item["method_step_id"])
            for item in ancestry_method_steps
        }
        if contract_current and ancestry_error is None:
            intermediate_state, ancestry_intermediates = (
                _claim_ancestry_intermediates(run, ancestry_stages, nodes)
            )
        else:
            intermediate_state, ancestry_intermediates = "unavailable", []
        intermediate_current = intermediate_state in {"not_required", "current"}

        claim = nodes.get(claim_id, {})
        requirements = claim.get("method_requirements")
        declared_pairs = set()
        if isinstance(requirements, dict):
            declared_pairs = {
                (requirement["method_id"], step_id)
                for requirement in requirements["methods"]
                for step_id in requirement["step_ids"]
            }
        missing_claim_pairs = sorted(ancestry_pairs - declared_pairs)
        off_ancestry_declared_pairs = sorted(declared_pairs - ancestry_pairs)
        missing_assessment_steps = []
        method_assessment_ids = []
        if not isinstance(requirements, dict):
            method_state = "claim_method_requirements_missing"
        elif not contract_current:
            method_state = "unavailable_without_current_contract"
        elif ancestry_error is not None:
            method_state = "unavailable_without_valid_result_ancestry"
        elif missing_claim_pairs:
            method_state = "claim_requirements_missing_ancestry_steps"
        elif off_ancestry_declared_pairs:
            method_state = "claim_requirements_include_off_ancestry_steps"
        else:
            relevant_method_items = [
                item for item in method_items
                if assessment_matches_run(item, run)
            ]
            for stage in ancestry_stages:
                matches = [
                    item for item in relevant_method_items
                    if _method_assessment_covers_stage(item, stage)
                ]
                if matches:
                    method_assessment_ids.extend(item["id"] for item in matches)
                else:
                    missing_assessment_steps.append({
                        "stage_id": stage["id"],
                        "method_id": stage["method_id"],
                        "method_step_id": stage["method_step_id"],
                    })
            method_state = (
                "accepted_current_conformance"
                if not missing_assessment_steps
                else "method_conformance_missing_or_not_current"
            )

        directional = relation["rel"] in {"supports", "refutes"}
        complete = (
            directional and execution_current and contract_current
            and ancestry_state == "current" and intermediate_current
            and replay_state == "byte_repeatable_current"
            and method_state == "accepted_current_conformance"
            and (
                not cfg.require_stage_checkpoints
                or stage_checkpoint_state == "cooperative_report_repeatable_current"
            )
        )
        policy_pass = directional and execution_current
        if cfg.require_execution_contracts:
            policy_pass = policy_pass and contract_current
        if contract_current:
            policy_pass = (
                policy_pass and ancestry_state == "current" and intermediate_current
            )
        if cfg.require_replay:
            policy_pass = policy_pass and replay_state == "byte_repeatable_current"
        if cfg.require_method_assessments:
            policy_pass = policy_pass and method_state == "accepted_current_conformance"
        if cfg.require_stage_checkpoints:
            policy_pass = (
                policy_pass
                and stage_checkpoint_state == "cooperative_report_repeatable_current"
            )
        if complete:
            overall = "ready_under_reviewed_provenance"
        elif not directional:
            overall = "semantic_context_only"
        elif execution_current:
            overall = "execution_grounded_but_provenance_incomplete"
        else:
            overall = "semantic_only_not_execution_grounded"
        item = {
            "assessment_id": relation["assessment_id"],
            "result_id": result_id,
            "claim_id": claim_id,
            "semantic_state": f"accepted_current_{relation['rel']}",
            "execution_state": execution_state,
            "run_id": run.get("run_id") if run else None,
            "computation_id": run.get("computation_id") if run else None,
            "pipeline_contract_id": run.get("pipeline_contract_id") if run else None,
            "contract_state": contract_state,
            "producer_stage_id": producer_stage_id,
            "ancestry_state": ancestry_state,
            "ancestry_stage_ids": [stage["id"] for stage in ancestry_stages],
            "ancestry_method_steps": ancestry_method_steps,
            "materialized_intermediate_state": intermediate_state,
            "ancestry_materialized_intermediates": ancestry_intermediates,
            "replay_state": replay_state,
            "replay_certificate_ids": [item["id"] for item in current_replays],
            "method_state": method_state,
            "required_method_ids": sorted({
                item["method_id"] for item in ancestry_method_steps
            }),
            "missing_claim_method_steps": [
                {"method_id": method_id, "method_step_id": step_id}
                for method_id, step_id in missing_claim_pairs
            ],
            "missing_method_assessment_steps": missing_assessment_steps,
            "off_ancestry_declared_method_steps": [
                {"method_id": method_id, "method_step_id": step_id}
                for method_id, step_id in off_ancestry_declared_pairs
            ],
            "method_assessment_ids": sorted(set(method_assessment_ids)),
            "stage_checkpoint_state": stage_checkpoint_state,
            "stage_execution_observation": (
                "cooperative_checkpoint_self_report_not_independent_observation"
                if run and run.get("event_schema_version") == STAGE_CONTRACT_EVENT_SCHEMA
                else "declared_only_not_observed" if contract_current else "not_available"
            ),
            "overall": overall,
            "configured_policy_pass": bool(policy_pass),
            "scientific_validity": "not_assessed",
            "limitations": [
                (
                    "cooperative checkpoints show program control flow reached locked "
                    "callsites; stage computation and in-memory values were not independently "
                    "observed"
                    if run and run.get("event_schema_version")
                    == STAGE_CONTRACT_EVENT_SCHEMA
                    else "internal stages and in-memory intermediates were not runtime-observed"
                ),
                (
                    "replay does not isolate network access, prevent writes outside the fresh "
                    "workspace, or capture all host environment state"
                ),
                "method conformance and semantic review do not establish scientific truth",
            ],
        }
        items.append(item)
        if not execution_current:
            extra.append({
                "severity": "warning", "code": "CLAIM_EXECUTION_BASIS_INCOMPLETE",
                "node_id": claim_id,
                "detail": f"{result_id} -> {claim_id} has accepted semantics but no current producing receipt",
                "source": "claim_basis",
            })
        if not contract_current:
            extra.append({
                "severity": "warning" if cfg.require_execution_contracts else "info",
                "code": "EXECUTION_CONTRACT_MISSING_OR_STALE", "node_id": claim_id,
                "detail": f"{result_id} -> {claim_id}: contract state is {contract_state}",
                "source": "claim_basis",
            })
        elif ancestry_error is not None:
            extra.append({
                "severity": "warning", "code": "CLAIM_RESULT_ANCESTRY_INVALID",
                "node_id": claim_id,
                "detail": f"{result_id} -> {claim_id}: {ancestry_error}",
                "source": "claim_basis",
            })
        if contract_current and not intermediate_current:
            incomplete_paths = [
                item["path"] for item in ancestry_intermediates
                if item["binding_state"] != "current"
            ]
            extra.append({
                "severity": "warning",
                "code": "CLAIM_ANCESTRY_INTERMEDIATE_INCOMPLETE",
                "node_id": claim_id,
                "detail": (
                    f"{result_id} -> {claim_id}: ancestry intermediate coverage is "
                    f"{intermediate_state} for: " + ", ".join(incomplete_paths)
                ),
                "source": "claim_basis",
            })
        if replay_state != "byte_repeatable_current":
            extra.append({
                "severity": "warning" if cfg.require_replay else "info",
                "code": "REPLAY_MISSING_OR_NOT_CURRENT", "node_id": claim_id,
                "detail": f"{result_id} -> {claim_id}: replay state is {replay_state}",
                "source": "claim_basis",
            })
        if method_state != "accepted_current_conformance":
            extra.append({
                "severity": "warning" if cfg.require_method_assessments else "info",
                "code": "CLAIM_METHOD_BASIS_INCOMPLETE", "node_id": claim_id,
                "detail": f"{result_id} -> {claim_id}: method state is {method_state}",
                "source": "claim_basis",
            })
        if (cfg.require_stage_checkpoints
                and stage_checkpoint_state != "cooperative_report_repeatable_current"):
            extra.append({
                "severity": "warning",
                "code": "CLAIM_STAGE_CHECKPOINT_BASIS_INCOMPLETE",
                "node_id": claim_id,
                "detail": (
                    f"{result_id} -> {claim_id}: cooperative stage checkpoint state is "
                    f"{stage_checkpoint_state}"
                ),
                "source": "claim_basis",
            })
    items.sort(key=lambda item: (
        item["claim_id"], item["result_id"], item["semantic_state"], item["assessment_id"],
    ))
    return {
        "schema_version": "claimtrace.claim-basis-projection/1",
        "policy": {
            "require_contracts": cfg.require_execution_contracts,
            "require_replay": cfg.require_replay,
            "require_method_assessments": cfg.require_method_assessments,
            "require_stage_checkpoints": cfg.require_stage_checkpoints,
        },
        "readiness_meaning": (
            "reviewed current provenance under partial runtime coverage; not scientific truth"
        ),
        "items": items,
    }, extra


def _logic_asset_path(cfg, path):
    """Return a stable project-relative asset label when possible."""
    try:
        return Path(path).resolve(strict=False).relative_to(cfg.base).as_posix()
    except ValueError:
        return str(Path(path).resolve(strict=False))


def _semantic_projection(cfg):
    """Project reviewed term mappings without turning them into scientific evidence.

    Mapping acceptance and explicit policy activation are separate.  A valid active
    policy makes selected normalizations available to consumers; it neither creates a
    result-to-claim support edge nor changes the symbolic derivability projection.
    """
    extra = []
    integrity_error = False
    loaded_assets = None
    assets = {
        "configured_terminologies": sorted(
            _logic_asset_path(cfg, path) for path in cfg.semantic_terminology_paths
        ),
        "configured_ontology_locks": sorted(
            _logic_asset_path(cfg, path) for path in cfg.semantic_ontology_lock_paths
        ),
        "terminologies": [],
        "ontology_locks": [],
    }
    try:
        loaded_assets = configured_semantic_assets(cfg)
        assets["terminologies"] = sorted((
            {
                "id": terminology["id"],
                "version": terminology["version"],
                "term_count": len(terminology["terms"]),
                "path": _logic_asset_path(
                    cfg, loaded_assets["terminology_paths"][terminology_id]
                ),
            }
            for terminology_id, terminology in loaded_assets["terminologies"].items()
        ), key=lambda item: (item["id"], item["version"], item["path"]))
        assets["ontology_locks"] = sorted((
            {
                **copy.deepcopy(lock),
                "path": _logic_asset_path(
                    cfg, loaded_assets["ontology_lock_paths"][lock_id]
                ),
            }
            for lock_id, lock in loaded_assets["ontology_locks"].items()
        ), key=lambda item: (item["ontology_id"], item["version"], item["id"]))
    except SemanticError as exc:
        integrity_error = True
        extra.append({
            "severity": "error",
            "code": "SEMANTIC_ASSET_INTEGRITY",
            "node_id": None,
            "detail": str(exc),
            "source": "semantics",
        })

    documents, mapping_issues = load_mappings(cfg)
    if mapping_issues:
        integrity_error = True
    for issue in mapping_issues:
        extra.append({
            "severity": "error",
            "code": issue["code"],
            "node_id": None,
            "detail": f"{issue['path']}: {issue['detail']}",
            "source": "semantics",
        })

    leaves = current_mapping_leaves(documents)
    leaf_ids = {item["id"] for item in leaves}
    conflicts = detect_mapping_conflicts(documents)
    mapping_history = []
    for document in documents:
        mapping_history.append({
            "id": document["id"],
            "schema_version": document["schema_version"],
            "recorded_at": document["recorded_at"],
            "subject": copy.deepcopy(document["subject"]),
            "mechanical_snapshot": copy.deepcopy(document["mechanical_snapshot"]),
            "agent_input": copy.deepcopy(document["agent_input"]),
            "review": copy.deepcopy(document["review"]),
            "stored_derived": copy.deepcopy(document["derived"]),
            "is_current": document["id"] in leaf_ids,
        })

    current_mapping_evaluations = []
    live_evaluations = (
        evaluate_mappings(cfg, leaves, conflicts=conflicts)
        if loaded_assets is None
        else _evaluate_mappings_from_assets(
            leaves, loaded_assets, conflicts=conflicts,
        )
    )
    for document, evaluation in zip(leaves, live_evaluations):
        if mapping_issues:
            evaluation = copy.deepcopy(evaluation)
            evaluation["eligible_for_policy"] = False
            evaluation["findings"].append({
                "severity": "error",
                "code": "SEMANTIC_MAPPING_INTEGRITY",
                "detail": "mapping-store integrity is not established",
            })
        current_mapping_evaluations.append({
            "mapping_id": document["id"],
            "subject": copy.deepcopy(document["subject"]),
            "review": copy.deepcopy(document["review"]),
            "current_derived": copy.deepcopy(evaluation),
        })
        review_state = evaluation["effective_review_state"]
        if review_state == "proposed":
            extra.append({
                "severity": "pending",
                "code": "SEMANTIC_MAPPING_REVIEW_PENDING",
                "node_id": None,
                "detail": f"{document['id']} awaits acceptance or rejection",
                "source": "semantics",
            })
        elif review_state == "contested" and not any(
                item["code"] == "SEMANTIC_MAPPING_CONFLICT"
                for item in evaluation["findings"]):
            extra.append({
                "severity": "warning",
                "code": "SEMANTIC_MAPPING_CONTESTED",
                "node_id": None,
                "detail": f"{document['id']} has a contested current review",
                "source": "semantics",
            })
        if review_state in {"proposed", "accepted", "contested"}:
            for finding in evaluation["findings"]:
                extra.append({
                    "severity": finding["severity"],
                    "code": finding["code"],
                    "node_id": None,
                    "detail": f"{document['id']}: {finding['detail']}",
                    "source": "semantics",
                })

    policies, policy_issues = load_policies(cfg)
    if policy_issues:
        integrity_error = True
    for issue in policy_issues:
        extra.append({
            "severity": "error",
            "code": issue["code"],
            "node_id": None,
            "detail": f"{issue['path']}: {issue['detail']}",
            "source": "semantics",
        })

    active_policy = (
        evaluate_active_semantic_policy(cfg)
        if loaded_assets is None
        else _evaluate_active_semantic_policy_from_snapshot(
            cfg, assets=loaded_assets, policies=policies,
            policy_issues=policy_issues, mappings=documents,
            mapping_issues=mapping_issues,
        )
    )
    active_findings = active_policy["findings"]
    if any(item["severity"] == "error" for item in active_findings):
        integrity_error = True
    for finding in active_findings:
        extra.append({
            "severity": finding["severity"],
            "code": finding["code"],
            "node_id": None,
            "detail": finding["detail"],
            "source": "semantics",
        })

    active_mapping_ids = []
    evaluation = active_policy["evaluation"]
    if evaluation is not None and evaluation["active"] and not integrity_error:
        active_mapping_ids = list(evaluation["active_mapping_ids"])

    snapshot_problems = []
    if loaded_assets is None:
        snapshot_problems.append(
            "a coherent semantic asset snapshot was unavailable"
        )
    if evaluation is not None and evaluation.get("active"):
        policy_ids = {item["id"] for item in policies}
        evaluation_by_id = {
            item["mapping_id"]: item["current_derived"]
            for item in current_mapping_evaluations
        }
        if evaluation.get("policy_id") not in policy_ids:
            snapshot_problems.append("active policy was not in the report policy snapshot")
        if any(
                mapping_id not in evaluation_by_id
                or not evaluation_by_id[mapping_id].get("eligible_for_policy")
                for mapping_id in evaluation.get("active_mapping_ids", [])):
            snapshot_problems.append(
                "active mappings disagree with the report mapping snapshot"
            )
    if loaded_assets is not None:
        try:
            final_assets = configured_semantic_assets(cfg)
            if _canonical_json(final_assets) != _canonical_json(loaded_assets):
                snapshot_problems.append(
                    "semantic assets changed while the report was being built"
                )
        except SemanticError:
            snapshot_problems.append(
                "semantic assets became unavailable while the report was being built"
            )
    final_mappings, final_mapping_issues = load_mappings(cfg)
    final_policies, final_policy_issues = load_policies(cfg)
    if ([item["id"] for item in final_mappings] != [item["id"] for item in documents]
            or _canonical_json(final_mapping_issues) != _canonical_json(mapping_issues)
            or [item["id"] for item in final_policies] != [item["id"] for item in policies]
            or _canonical_json(final_policy_issues) != _canonical_json(policy_issues)):
        snapshot_problems.append("semantic stores changed while the report was being built")
    if snapshot_problems:
        integrity_error = True
        active_mapping_ids = []
        finding = {
            "severity": "error",
            "code": "SEMANTIC_REPORT_SNAPSHOT_CHANGED",
            "node_id": None,
            "detail": "; ".join(snapshot_problems),
            "source": "semantics",
        }
        extra.append(finding)
        active_policy = copy.deepcopy(active_policy)
        active_policy["findings"] = [
            *active_policy.get("findings", []),
            {key: finding[key] for key in ("code", "severity", "detail")},
        ]
        if active_policy.get("evaluation") is not None:
            active_policy["evaluation"]["active"] = False
            active_policy["evaluation"]["valid"] = False
            active_policy["evaluation"]["active_mapping_ids"] = []
            active_policy["evaluation"]["findings"] = [
                *active_policy["evaluation"].get("findings", []),
                {key: finding[key] for key in ("code", "severity", "detail")},
            ]
    if cfg.require_active_semantic_policy and not active_policy["configured"]:
        extra.append({
            "severity": "warning",
            "code": "MISSING_ACTIVE_SEMANTIC_POLICY",
            "node_id": None,
            "detail": (
                "semantic normalization policy is required but no explicit active policy "
                "is configured"
            ),
            "source": "semantics",
        })

    return {
        "mapping_schema_version": MAPPING_SCHEMA,
        "policy_schema_version": POLICY_SCHEMA,
        "integrity": "error" if integrity_error else "ok",
        "policy": {
            "require_active_policy": cfg.require_active_semantic_policy,
        },
        "assets": assets,
        "mapping_history": mapping_history,
        "current_mapping_evaluations": current_mapping_evaluations,
        "policies": copy.deepcopy(policies),
        "active_policy": copy.deepcopy(active_policy),
        "active_mapping_ids": active_mapping_ids,
    }, extra


def _read_logic_asset(cfg, path):
    """Read one configured JSON asset through the shared bounded safe reader."""
    path = Path(path)
    label = _logic_asset_path(cfg, path)
    try:
        return load_logic_asset(path)
    except LogicError as exc:
        raise LogicError(f"cannot read logic asset {label}: {exc}") from exc


def _logic_assets(cfg):
    """Load configured vocabularies and rule packs as one fail-closed policy set."""
    vocabularies = {}
    rule_packs = {}
    extra = []

    def issue(path, detail):
        extra.append({
            "severity": "error",
            "code": "LOGIC_ASSET_INTEGRITY",
            "node_id": None,
            "detail": f"{_logic_asset_path(cfg, path)}: {detail}",
            "source": "derivations",
        })

    for path in cfg.logic_vocabulary_paths:
        try:
            vocabulary = load_vocabulary(path)
            previous = vocabularies.get(vocabulary["id"])
            if previous is not None:
                raise LogicError(
                    f"duplicate configured vocabulary id {vocabulary['id']!r}"
                )
            vocabularies[vocabulary["id"]] = vocabulary
        except LogicError as exc:
            issue(path, exc)

    for path in cfg.logic_rule_pack_paths:
        try:
            raw_pack = _read_logic_asset(cfg, path)
            if not isinstance(raw_pack, dict):
                raise LogicError("symbolic rule pack must be a JSON object")
            vocabulary_id = raw_pack.get("vocabulary_id")
            vocabulary = vocabularies.get(vocabulary_id)
            if vocabulary is None:
                raise LogicError(
                    f"rule pack references unconfigured vocabulary {vocabulary_id!r}"
                )
            rule_pack = load_rule_pack(raw_pack, vocabulary)
            if rule_pack["id"] in rule_packs:
                raise LogicError(
                    f"duplicate configured rule-pack id {rule_pack['id']!r}"
                )
            rule_packs[rule_pack["id"]] = rule_pack
        except LogicError as exc:
            issue(path, exc)

    projected = {
        "vocabularies": sorted(
            ({"id": item["id"], "version": item["version"]}
             for item in vocabularies.values()),
            key=lambda item: (item["id"], item["version"]),
        ),
        "rule_packs": sorted(
            ({
                "id": item["id"], "version": item["version"],
                "vocabulary_id": item["vocabulary_id"],
            } for item in rule_packs.values()),
            key=lambda item: (item["id"], item["version"], item["vocabulary_id"]),
        ),
    }
    return vocabularies, rule_packs, projected, extra


def _derivation_replacement_key(item):
    """Identify the logical proof represented by a derivation report item.

    Mechanical snapshot hashes deliberately do not participate: line-ending or other
    byte-level provenance drift can change a proof id without changing the logic.  The
    subject result ids and evidence bindings do participate, so a proof over different
    evidence cannot silently replace stale history.
    """
    effective = item["effective"]
    input_facts = []
    for fact in item["agent_input"]["facts"]:
        input_facts.append({
            "atom": copy.deepcopy(fact["atom"]),
            "evidence": [
                {
                    "result_id": anchor["result_id"],
                    "binding_id": anchor["binding_id"],
                }
                for anchor in fact["evidence"]
            ],
            "assumption": fact["assumption"],
        })
    return _canonical_json({
        "claim_id": item["subject"]["claim_id"],
        "result_ids": copy.deepcopy(item["subject"]["result_ids"]),
        "vocabulary_id": item["subject"]["vocabulary_id"],
        "rule_pack_id": item["subject"]["rule_pack_id"],
        "target": copy.deepcopy(effective["target"]),
        "proof_state": effective["proof_state"],
        "input_facts": input_facts,
        "input_fact_ids": copy.deepcopy(effective.get("input_fact_ids", [])),
        "used_input_fact_ids": copy.deepcopy(
            effective.get("used_input_fact_ids", []),
        ),
        "unused_input_fact_ids": copy.deepcopy(
            effective.get("unused_input_fact_ids", []),
        ),
        "outcome_relation": effective["outcome_relation"],
        "outcome_atoms": copy.deepcopy(effective["outcome_atoms"]),
        "supporting_fact_id": effective.get("supporting_fact_id"),
        "refuting_fact_id": effective.get("refuting_fact_id"),
        "proof_steps": copy.deepcopy(effective.get("proof_steps", [])),
        "assumptions": copy.deepcopy(effective.get("assumptions", [])),
    })


def _derivation_recorded_at(item):
    """Return the validated RFC 3339 timestamp as a comparable UTC datetime."""
    value = item["recorded_at"]
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _derivation_projection(cfg, raw):
    """Project conditional symbolic derivations without converting them into claim links."""
    vocabularies, rule_packs, assets, asset_findings = _logic_assets(cfg)
    declaration_findings = [
        {
            "severity": "error",
            "code": "LOGIC_DECLARATION_INVALID",
            "node_id": issue["node_id"],
            "detail": f"{issue['declaration']}: {issue['detail']}",
            "source": "derivations",
        }
        for issue in validate_graph_logic_declarations(
            cfg, raw, vocabularies, rule_packs,
        )
    ]
    store_findings = []
    store = derivations_path(cfg)
    if store.is_symlink() or (store.exists() and not store.is_dir()):
        documents = []
        store_findings.append({
            "severity": "error",
            "code": "DERIVATION_INTEGRITY",
            "node_id": None,
            "detail": (
                f"{_logic_asset_path(cfg, store)}: derivation store must be a regular directory "
                "and must not be a symbolic link"
            ),
            "source": "derivations",
        })
    else:
        documents, integrity_issues = load_derivations(cfg)
        for issue in integrity_issues:
            store_findings.append({
                "severity": "error",
                "code": issue["code"],
                "node_id": None,
                "detail": f"{issue['path']}: {issue['detail']}",
                "source": "derivations",
            })

    reference_findings = []
    resolved = {}
    for document in documents:
        subject = document["subject"]
        vocabulary = vocabularies.get(subject["vocabulary_id"])
        rule_pack = rule_packs.get(subject["rule_pack_id"])
        if vocabulary is None:
            reference_findings.append({
                "severity": "error",
                "code": "LOGIC_ASSET_INTEGRITY",
                "node_id": subject["claim_id"],
                "detail": (
                    f"{document['id']}: vocabulary {subject['vocabulary_id']!r} "
                    "is not configured"
                ),
                "source": "derivations",
            })
            continue
        if rule_pack is None:
            reference_findings.append({
                "severity": "error",
                "code": "LOGIC_ASSET_INTEGRITY",
                "node_id": subject["claim_id"],
                "detail": (
                    f"{document['id']}: rule pack {subject['rule_pack_id']!r} "
                    "is not configured"
                ),
                "source": "derivations",
            })
            continue
        if rule_pack["vocabulary_id"] != vocabulary["id"]:
            reference_findings.append({
                "severity": "error",
                "code": "LOGIC_ASSET_INTEGRITY",
                "node_id": subject["claim_id"],
                "detail": (
                    f"{document['id']}: configured rule pack and vocabulary do not match"
                ),
                "source": "derivations",
            })
            continue
        resolved[document["id"]] = (vocabulary, rule_pack)

    integrity_findings = [
        *asset_findings, *declaration_findings, *store_findings, *reference_findings,
    ]
    integrity_error = bool(integrity_findings)
    items = []
    active_proofs = []
    for document in sorted(documents, key=lambda item: item["id"]):
        subject = document["subject"]
        pair = resolved.get(document["id"])
        if pair is None:
            effective = copy.deepcopy(document["derived"])
        else:
            try:
                effective = evaluate_derivation(
                    cfg, document, vocabulary=pair[0], rule_pack=pair[1],
                )
            except LogicError as exc:
                integrity_error = True
                integrity_findings.append({
                    "severity": "error",
                    "code": "DERIVATION_INTEGRITY",
                    "node_id": subject["claim_id"],
                    "detail": f"{document['id']}: {exc}",
                    "source": "derivations",
                })
                effective = copy.deepcopy(document["derived"])

        effective = copy.deepcopy(effective)
        if integrity_error:
            effective["active"] = False
            effective["effective_state"] = "integrity_error"
        else:
            effective["effective_state"] = "active" if effective["active"] else "inactive"
        items.append({
            "id": document["id"],
            "recorded_at": document["recorded_at"],
            "actor": document["actor"],
            "subject": copy.deepcopy(subject),
            "agent_input": copy.deepcopy(document["agent_input"]),
            "mechanical_snapshot": copy.deepcopy(document["mechanical_snapshot"]),
            "stored_derived": copy.deepcopy(document["derived"]),
            "effective": effective,
        })

    # A later document can expose an integrity fault. Apply the global fail-closed gate
    # after all evaluations so no earlier item remains active by iteration order.
    if integrity_error:
        for item in items:
            item["effective"]["active"] = False
            item["effective"]["effective_state"] = "integrity_error"
    else:
        for item in items:
            effective = item["effective"]
            if not effective["active"]:
                continue
            subject = item["subject"]
            active_proofs.append({
                "derivation_id": item["id"],
                "derivation_ids": [item["id"]],
                "proof_id": effective["proof_id"],
                "claim_id": subject["claim_id"],
                "result_ids": copy.deepcopy(effective.get("used_result_ids", [])),
                "vocabulary_id": subject["vocabulary_id"],
                "rule_pack_id": subject["rule_pack_id"],
                "proof_state": effective["proof_state"],
                "target": copy.deepcopy(effective["target"]),
                "rendered_target": effective["rendered_target"],
                "outcome_relation": effective["outcome_relation"],
                "outcome_atoms": copy.deepcopy(effective["outcome_atoms"]),
                "rendered_outcomes": copy.deepcopy(effective["rendered_outcomes"]),
                "assumptions": copy.deepcopy(effective["assumptions"]),
            })

    # Immutable submissions remain visible with their effective findings, but an old
    # stale submission must not make the current report fail when a later submission
    # is active for the same logical proof.  Mechanical provenance hashes (and thus
    # proof ids) can change under byte-only normalization, so replacement equivalence
    # uses exact semantic proof content and evidence identities.  Compute this only
    # after the global integrity gate: an integrity fault suppresses every active
    # replacement and therefore cannot hide a stale finding.
    latest_active_by_key = {}
    for item in items:
        if not item["effective"]["active"]:
            continue
        key = _derivation_replacement_key(item)
        recorded_at = _derivation_recorded_at(item)
        latest_active_by_key[key] = max(
            recorded_at,
            latest_active_by_key.get(key, recorded_at),
        )
    evaluation_findings = []
    for item in items:
        effective = item["effective"]
        replacement_time = latest_active_by_key.get(
            _derivation_replacement_key(item),
        )
        has_active_equivalent = (
            not effective["active"]
            and replacement_time is not None
            and replacement_time > _derivation_recorded_at(item)
        )
        for finding in effective.get("findings", []):
            if finding["code"] == "DERIVATION_STALE" and has_active_equivalent:
                continue
            evaluation_findings.append({
                "severity": finding["severity"],
                "code": finding["code"],
                "node_id": item["subject"]["claim_id"],
                "detail": f"{item['id']}: {finding['detail']}",
                "source": "derivations",
            })

    proof_groups = {}
    for proof in active_proofs:
        existing = proof_groups.get(proof["proof_id"])
        if existing is None:
            proof_groups[proof["proof_id"]] = proof
            continue
        existing["derivation_ids"].extend(proof["derivation_ids"])
        existing["derivation_ids"] = sorted(set(existing["derivation_ids"]))
        existing["derivation_id"] = existing["derivation_ids"][0]
    active_proofs = list(proof_groups.values())

    target_groups = defaultdict(list)
    for proof in active_proofs:
        key = (
            proof["claim_id"], proof["vocabulary_id"], proof["rule_pack_id"],
            _canonical_json(proof["target"]),
        )
        target_groups[key].append(proof)
    conflict_findings = []
    for key, proofs in target_groups.items():
        states = {proof["proof_state"] for proof in proofs}
        conflicted = {"derivable", "refutable"} <= states
        for proof in proofs:
            proof["claim_level_active"] = not conflicted
        if conflicted:
            conflict_findings.append({
                "severity": "error",
                "code": "SYMBOLIC_CROSS_DERIVATION_CONFLICT",
                "node_id": key[0],
                "detail": (
                    "active conditional proofs derive both the formal target and its explicit "
                    "opposite under the same vocabulary and rule pack"
                ),
                "source": "derivations",
            })

    logic_claims = sorted(
        node["id"] for node in raw["nodes"]
        if (node.get("type") in {"claim", "hypothesis", "prediction", "conclusion"}
            and node.get("status") in (None, "current", "confirmed")
            and isinstance(node.get("logic"), dict)
            and "target" in node["logic"])
    )
    derived_claims = {
        item["claim_id"] for item in active_proofs
        if item["proof_state"] == "derivable" and item["claim_level_active"]
    }
    policy_findings = []
    if cfg.require_derivations:
        for claim_id in sorted(set(logic_claims) - derived_claims):
            policy_findings.append({
                "severity": "warning",
                "code": "MISSING_CLAIM_DERIVATION",
                "node_id": claim_id,
                "detail": (
                    "formalized claim has no active target-deriving symbolic proof under the "
                    "configured vocabulary and rule pack; a refutation does not satisfy this policy"
                ),
                "source": "derivations",
            })

    active_proofs.sort(key=lambda item: (
        item["claim_id"], item["proof_state"], item["proof_id"], item["derivation_id"],
    ))
    return {
        "derivation_schema_version": DERIVATION_SCHEMA,
        "integrity": "error" if integrity_error else "ok",
        "policy": {"require_derivations": cfg.require_derivations},
        "assets": assets,
        "items": items,
        "active_proofs": active_proofs,
    }, [
        *integrity_findings, *evaluation_findings, *conflict_findings, *policy_findings,
    ]


def _annotate_derivation_execution_basis(
        derivations, claim_basis, *, require_complete_execution_basis=False):
    by_pair = defaultdict(list)
    for item in claim_basis["items"]:
        by_pair[(item["claim_id"], item["result_id"])].append(item)
    findings = []
    for proof in derivations["active_proofs"]:
        used_results = sorted(set(proof.get("result_ids", [])))
        links = []
        for result_id in used_results:
            links.extend(by_pair.get((proof["claim_id"], result_id), []))
        links.sort(key=lambda item: (item["result_id"], item["assessment_id"]))
        if not used_results:
            state = "not_applicable_no_result_premises"
        elif ({item["result_id"] for item in links} == set(used_results)
              and all(item["overall"] == "ready_under_reviewed_provenance"
                      for item in links)):
            state = "reviewed_execution_basis_current"
        else:
            state = "symbolically_active_execution_basis_incomplete"
            findings.append({
                "severity": (
                    "warning" if require_complete_execution_basis else "info"
                ),
                "code": "SYMBOLIC_EXECUTION_BASIS_INCOMPLETE",
                "node_id": proof["claim_id"],
                "detail": (
                    f"{proof['proof_id']} remains conditionally {proof['proof_state']} under "
                    "project rules, but its result premises lack complete current execution, "
                    "replay, and method-conformance provenance"
                ),
                "source": "claim_basis",
            })
        proof["execution_basis"] = {
            "state": state,
            "claim_basis_assessment_ids": sorted({
                item["assessment_id"] for item in links
            }),
            "does_not_change_symbolic_proof_state": True,
        }
    return findings


def build_report(cfg, *, strict=False, raw=None):
    """Build one deterministic report without printing, mutation, or verifier execution.

    A caller may pass a graph previously returned by ``load_raw``. Otherwise this function reads
    the graph exactly once and shares that snapshot with the hard checks, lint, and projection.
    Strictness is only a blocking policy: all evidence remains present in both modes.
    """
    graph = raw if raw is not None else load_raw(cfg)
    problems, pending = compute_check(cfg, raw=graph)
    warnings = lint_issues(cfg, raw=graph)
    receipts, receipt_findings = _receipt_projection(cfg, graph)
    assessments, assessment_findings = _assessment_projection(cfg, graph)
    (
        method_assessments,
        method_findings,
        method_snapshots_by_id,
    ) = _method_assessment_projection(cfg)
    claim_basis, claim_basis_findings = _claim_basis_projection(
        cfg, graph, receipts, assessments, method_assessments,
        method_snapshots_by_id,
    )
    semantics, semantic_findings = _semantic_projection(cfg)
    derivations, derivation_findings = _derivation_projection(cfg, graph)
    symbolic_basis_findings = _annotate_derivation_execution_basis(
        derivations, claim_basis,
        require_complete_execution_basis=(
            cfg.require_execution_contracts
            or cfg.require_replay
            or cfg.require_method_assessments
        ),
    )
    findings = _findings(
        problems, warnings, pending, bool(strict),
        [*receipt_findings, *assessment_findings, *method_findings,
         *claim_basis_findings, *semantic_findings, *derivation_findings,
         *symbolic_basis_findings],
    )
    counts = Counter(item["severity"] for item in findings)
    blocking = sum(1 for item in findings if item["blocking"])

    report = _base_report(strict)
    report.update({
        "ok": blocking == 0,
        "exit_code": 0 if blocking == 0 else 1,
        "summary": {
            "nodes": len(graph["nodes"]),
            "edges": len(graph.get("edges", [])),
            "concepts": len(graph.get("concepts", {})),
            "errors": counts["error"],
            "warnings": counts["warning"],
            "pending": counts["pending"],
            "info": counts["info"],
            "blocking": blocking,
        },
        "graph": _graph_projection(graph),
        "receipts": receipts,
        "assessments": assessments,
        "method_assessments": method_assessments,
        "claim_basis": claim_basis,
        "semantics": semantics,
        "derivations": derivations,
        "findings": findings,
        "fatal": None,
    })
    return report


def build_fatal_report(detail, *, strict=False, code="GRAPH_ERROR"):
    """Build the stable exit-2 envelope used when no complete audit can be produced."""
    report = _base_report(strict)
    report.update({
        "ok": False,
        "exit_code": 2,
        "summary": None,
        "graph": None,
        "receipts": None,
        "assessments": None,
        "method_assessments": None,
        "claim_basis": None,
        "semantics": None,
        "derivations": None,
        "findings": [],
        "fatal": {"code": str(code), "detail": str(detail)},
    })
    return report


def dumps_report(report):
    """Serialize a report canonically as terminal-safe ASCII JSON plus newline."""
    return json.dumps(
        report, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ) + "\n"
