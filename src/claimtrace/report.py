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
from .events import (EVENT_SCHEMA, RUN_ID_RE, load_active_markers, load_events,
                     materialize_runs, snapshot_file)
from .logic import (DERIVATION_SCHEMA, LogicError, derivations_path,
                    evaluate_derivation, load_derivations, load_logic_asset, load_rule_pack,
                    load_vocabulary, validate_graph_logic_declarations)

REPORT_SCHEMA_VERSION = "1.3"
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
    return {
        "run_id": run["run_id"],
        "start_event_id": start.get("id") if start else None,
        "finish_event_id": finish.get("id") if finish else None,
        "started_at": start.get("recorded_at") if start else None,
        "finished_at": finish.get("recorded_at") if finish else None,
        "name": start.get("payload", {}).get("name") if start else None,
        "plan_id": (start.get("payload", {}).get("plan_id") if start else None),
        "result_id": finish_payload.get("result_id"),
        "argv": copy.deepcopy(plan.get("argv", [])),
        "argv_capture": plan.get("argv_capture"),
        "cwd": plan.get("cwd"),
        "parameters": copy.deepcopy(plan.get("parameters", {})),
        "seeds": copy.deepcopy(plan.get("seeds", {})),
        "declared_inputs": [item.get("path") for item in plan.get("declared_inputs", [])],
        "declared_outputs": copy.deepcopy(plan.get("declared_outputs", [])),
        "outcome": finish_payload.get("outcome"),
        "direct_child_returncode": finish_payload.get("direct_child_returncode"),
        "contract_errors": copy.deepcopy(finish_payload.get("contract_errors", [])),
        "input_transitions": input_transitions,
        "output_transitions": transitions,
        "window_deltas": copy.deepcopy(finish_payload.get("window_deltas", [])),
        "receipt_integrity": finish_payload.get("receipt_integrity"),
        "lineage_coverage": copy.deepcopy(finish_payload.get("lineage_coverage")),
        "bindings": [],
    }


def _receipt_projection(cfg, raw):
    events, event_issues = load_events(cfg.events_path)
    runs, run_issues = materialize_runs(events)
    markers, marker_issues = load_active_markers(cfg.root)
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
    for raw_run in runs:
        start, finish = raw_run.get("start"), raw_run.get("finish")
        if not start or not finish:
            continue
        payload = finish["payload"]
        outcome = payload.get("outcome")
        for item in payload.get("output_transitions", []):
            path = item.get("path")
            if not isinstance(path, str):
                continue
            identity = _path_identity(cfg, path)
            if outcome == "succeeded" and item.get("after", {}).get("state") == "stable":
                key = (finish.get("recorded_at", ""), finish["id"])
                if (item.get("produced") is True
                        and item.get("transition") in {"created", "content_changed"}):
                    producing_receipts.setdefault(identity, []).append((key, raw_run, item))
                elif (item.get("produced") is False
                      and item.get("transition") == "unchanged"):
                    validation_receipts.setdefault(identity, []).append((key, raw_run, item))
            if not item.get("declared_output", True):
                continue
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
        expected = set(direct_inputs(cfg, nodes, edges, node_id))
        declared_ids = {_path_identity(cfg, path): path for path in declared}
        expected_ids = {_path_identity(cfg, path): path for path in expected}
        missing = sorted(expected_ids[key] for key in set(expected_ids) - set(declared_ids))
        extra_declared = sorted(declared_ids[key] for key in set(declared_ids) - set(expected_ids))
        agreement = "declarations_agree" if not missing and not extra_declared else "declarations_differ"
        projected_by_id[raw_run["run_id"]]["bindings"].append({
            "binding_kind": "output_path",
            "node_id": node_id,
            "path": nodes[node_id].get("path"),
            "declaration_comparison": agreement,
            "output_evidence": "content_transition_detected",
            "graph_declared_inputs": sorted(expected),
            "run_declared_inputs": sorted(declared),
        })
        if missing:
            extra.append({
                "severity": "warning", "code": "RUN_DECLARATION_INCOMPLETE", "node_id": node_id,
                "detail": "graph inputs absent from run declaration: " + ", ".join(missing),
            })
        if extra_declared:
            extra.append({
                "severity": "warning", "code": "GRAPH_DECLARATION_INCOMPLETE", "node_id": node_id,
                "detail": "run inputs absent from graph declaration: " + ", ".join(extra_declared),
            })
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
        after = output_item.get("after", {})
        current_path = cfg.resolve(nodes[node_id]["path"])
        current = snapshot_file(current_path, nodes[node_id]["path"])
        if after.get("state") == "stable" and current.get("state") == "stable" and after.get("sha256") != current.get("sha256"):
            extra.append({
                "severity": "error", "code": "RUN_OUTPUT_DRIFT", "node_id": node_id,
                "detail": f"{nodes[node_id]['path']} differs from latest successful output receipt {raw_run['run_id']}",
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

    for run in projected_runs:
        run["bindings"].sort(key=lambda item: (item["node_id"], item.get("binding_kind", "")))
    projected_runs.sort(key=lambda item: (item.get("finished_at") or item.get("started_at") or "", item["run_id"]))
    return {
        "event_schema_version": EVENT_SCHEMA,
        "integrity": "error" if event_issues or run_issues or marker_issues else "ok",
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


def _logic_asset_path(cfg, path):
    """Return a stable project-relative asset label when possible."""
    try:
        return Path(path).resolve(strict=False).relative_to(cfg.base).as_posix()
    except ValueError:
        return str(Path(path).resolve(strict=False))


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
    derivations, derivation_findings = _derivation_projection(cfg, graph)
    findings = _findings(
        problems, warnings, pending, bool(strict),
        [*receipt_findings, *assessment_findings, *derivation_findings],
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
        "derivations": None,
        "findings": [],
        "fatal": {"code": str(code), "detail": str(detail)},
    })
    return report


def dumps_report(report):
    """Serialize a report canonically as one UTF-8-friendly JSON document plus newline."""
    return json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ) + "\n"
