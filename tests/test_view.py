"""Focused tests for the deterministic standalone research-trajectory view."""
import json
import re
import sys

import pytest

import provsleuth.view as view_module
from provsleuth.assessment import append_assessment, create_assessment, transition_review
from provsleuth.config import Config
from provsleuth.cli import main
from provsleuth.engine import GraphError
from provsleuth.events import run_command
from provsleuth.logic import (EVIDENCE_PLAN_SCHEMA, append_derivation,
                              create_derivation, load_rule_pack, load_vocabulary)
from provsleuth.report import build_report
from provsleuth.view import render_view


def _project(tmp_path, *, malicious_value=None):
    trace = tmp_path / "provsleuth"
    trace.mkdir(parents=True)
    (tmp_path / "data.txt").write_text("evidence", encoding="utf-8")
    (tmp_path / "analysis.py").write_text("# deterministic pipeline", encoding="utf-8")
    value = malicious_value if malicious_value is not None else "supported result"
    nodes = [
        {"id": "data:source", "type": "data", "status": "current", "path": "data.txt"},
        {"id": "code:pipeline", "type": "code", "status": "current", "path": "analysis.py"},
        {"id": "artifact:result", "type": "artifact", "status": "current", "path": "out.txt"},
        {"id": "claim:result", "type": "claim", "status": "confirmed", "value": value},
        {"id": "experiment:null", "type": "experiment", "status": "dead_end",
         "value": "negative branch"},
    ]
    edges = [
        {"from": "data:source", "to": "artifact:result", "rel": "produces"},
        {"from": "code:pipeline", "to": "artifact:result", "rel": "produces"},
        {"from": "artifact:result", "to": "claim:result", "rel": "supports"},
        {"from": "experiment:null", "to": "claim:result", "rel": "tried_before"},
    ]
    config_path = tmp_path / "provsleuth.config.json"
    config_path.write_text(json.dumps({
        "root": ".",
        "graph": "provsleuth/graph.json",
        "events": "provsleuth/events",
        "render_types": [],
        "input_types": ["data", "artifact", "code"],
        "run_output_types": ["artifact"],
    }), encoding="utf-8")
    (trace / "graph.json").write_text(json.dumps({
        "schema_version": "1.0",
        "concepts": {},
        "nodes": nodes,
        "edges": edges,
    }), encoding="utf-8")
    return Config(config_path)


def _payload(html):
    match = re.search(
        r'<script id="provsleuth-data" type="application/json">(.*?)</script>',
        html,
        flags=re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


def _javascript_function(html, signature):
    """Return one named JS function without truncating at nested callbacks."""
    start = html.index("function " + signature)
    opening = html.index("{", start)
    depth = 0
    quote = None
    escaped = False
    for index in range(opening, len(html)):
        character = html[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {'"', "'", "`"}:
            quote = character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return html[start:index + 1]
    raise AssertionError(f"unterminated JavaScript function: {signature}")


def _large_report(*, layer_count=8, rows_per_layer=32):
    """Deterministic graph large enough to require a bounded browser projection."""
    nodes = []
    trajectory_order = []
    node_ids = {}
    for layer in range(layer_count):
        if layer == 0:
            node_type = "data"
        elif layer == layer_count - 1:
            node_type = "claim"
        else:
            node_type = "artifact"
        for row in range(rows_per_layer):
            node_id = f"{node_type}:large-l{layer:02d}-r{row:03d}"
            node_ids[layer, row] = node_id
            trajectory_order.append(node_id)
            nodes.append({
                "id": node_id,
                "type": node_type,
                "status": "confirmed" if node_type == "claim" else "current",
                "value": f"large fixture layer {layer} row {row}",
            })

    edges = []
    for layer in range(layer_count - 1):
        for row in range(rows_per_layer):
            for target_row in (row, (row + 1) % rows_per_layer):
                edges.append({
                    "from": node_ids[layer, row],
                    "to": node_ids[layer + 1, target_row],
                    "rel": "produces",
                })
    # This visible annotation must remain in the payload but must not become
    # dependency ancestry during one-hop expansion.
    edges.append({
        "from": node_ids[0, 0],
        "to": node_ids[layer_count - 1, 0],
        "rel": "tried_before",
    })
    return {
        "graph": {
            "schema_version": "1.0",
            "concepts": {},
            "trajectory_order": trajectory_order,
            "nodes": nodes,
            "edges": edges,
        },
        "scope": {},
        "summary": {},
        "findings": [],
        "receipts": {"runs": []},
        "claim_basis": {"items": []},
        "method_assessments": {"items": []},
        "assessments": {"integrity": "ok", "items": []},
        "derivations": {"integrity": "ok", "items": []},
        "semantics": {},
    }


def _record_run(cfg):
    command = [
        sys.executable,
        "-c",
        "from pathlib import Path; Path('out.txt').write_text(Path('data.txt').read_text())",
    ]
    result = run_command(
        cfg,
        command,
        inputs=["data.txt", "analysis.py"],
        outputs=["out.txt"],
        cwd=str(cfg.root),
    )
    assert result["exit_code"] == 0
    return result


def _agent_input(verdict="supports_as_written", *, rationale="The result matches the claim."):
    frame = {
        "population": "widget sample",
        "exposure": "measured input",
        "comparator": "unit increase",
        "outcome": "measured output",
        "direction": "positive",
        "magnitude": "0.41 units",
        "time_scope": "single study",
        "inference_level": "associational",
    }
    value = {
        "verdict": verdict,
        "claim_frame": dict(frame),
        "result_frame": dict(frame),
        "alignment": {key: "match" for key in frame},
        "evidence_anchors": [{
            "result_id": "artifact:result",
            "kind": "json_pointer",
            "pointer": "/effect",
            "expected_value": 0.41,
        }],
        "rationale": rationale,
        "limitations": ["Synthetic fixture only."],
        "provenance": {"agent": "test-reviewer", "model": "fixture"},
    }
    return value


def _record_assessment(cfg, verdict="supports_as_written", *, accepted=False,
                       recorded_at="2026-07-13T08:00:00.000Z", agent_input=None):
    (cfg.root / "out.txt").write_text(json.dumps({"effect": 0.41}), encoding="utf-8")
    proposal = create_assessment(
        cfg, "claim:result", ["artifact:result"], agent_input or _agent_input(verdict),
        actor="agent:test", recorded_at=recorded_at,
    )
    append_assessment(cfg, proposal)
    if not accepted:
        return proposal
    decision = transition_review(
        cfg, proposal, "accepted", actor="scientist:test",
        recorded_at="2026-07-13T08:01:00.000Z",
    )
    append_assessment(cfg, decision)
    return decision


def _record_symbolic_proof(
        cfg, *, passed=True, include_negative_rule=True, evidence_plan=False):
    target = {
        "predicate": "view:validated", "polarity": "positive",
        "arguments": {
            "subject": {"type": "view:subject", "value": "widget", "unit": None},
        },
    }
    vocabulary = {
        "schema_version": "claimtrace.symbolic-vocabulary/1",
        "id": "view:vocabulary", "version": "1.0.0",
        "types": [{"id": "view:subject", "base": "ct:symbol"}],
        "units": [],
        "predicates": [
            {"id": "view:observed", "kind": "input", "arguments": [
                {"name": "subject", "type": "view:subject", "unit": None},
                {"name": "passed", "type": "ct:boolean", "unit": None},
            ]},
            {"id": "view:validated", "kind": "derived", "arguments": [
                {"name": "subject", "type": "view:subject", "unit": None},
            ]},
        ],
        "renderers": [
            {
                "id": "view:validated-en", "predicate": "view:validated",
                "polarity": "positive", "language": "en",
                "template": "{subject} satisfies the configured validation rule.",
            },
            {
                "id": "view:invalid-en", "predicate": "view:validated",
                "polarity": "negative", "language": "en",
                "template": "{subject} does not satisfy the configured validation rule.",
            },
        ],
    }
    rules = {
        "schema_version": "claimtrace.symbolic-rules/1",
        "id": "view:rules", "version": "1.0.0", "vocabulary_id": "view:vocabulary",
        "rules": [{
            "id": "view:validation-pass",
            "when": [{
                "predicate": "view:observed", "polarity": "positive",
                "arguments": {"subject": {"var": "subject"}, "passed": {"var": "passed"}},
            }],
            "where": [{
                "op": "eq", "left": {"var": "passed"},
                "right": {"const": {"type": "ct:boolean", "value": True, "unit": None}},
            }],
            "then": {
                "predicate": "view:validated", "polarity": "positive",
                "arguments": {"subject": {"var": "subject"}},
            },
        }],
    }
    if include_negative_rule:
        rules["rules"].append({
            "id": "view:validation-fail",
            "when": [{
                "predicate": "view:observed", "polarity": "positive",
                "arguments": {"subject": {"var": "subject"}, "passed": {"var": "passed"}},
            }],
            "where": [{
                "op": "eq", "left": {"var": "passed"},
                "right": {"const": {"type": "ct:boolean", "value": False, "unit": None}},
            }],
            "then": {
                "predicate": "view:validated", "polarity": "negative",
                "arguments": {"subject": {"var": "subject"}},
            },
        })
    logic_dir = cfg.base / "provsleuth" / "logic"
    logic_dir.mkdir()
    (logic_dir / "vocabulary.json").write_text(json.dumps(vocabulary), encoding="utf-8")
    (logic_dir / "rules.json").write_text(json.dumps(rules), encoding="utf-8")
    (cfg.root / "out.txt").write_text(
        json.dumps({"subject": "widget", "passed": passed, "failed": False}),
        encoding="utf-8",
    )
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    claim = next(item for item in graph["nodes"] if item["id"] == "claim:result")
    claim["logic"] = {
        "vocabulary_id": "view:vocabulary", "rule_pack_id": "view:rules",
        "target": target,
    }
    result = next(item for item in graph["nodes"] if item["id"] == "artifact:result")
    result["logic_bindings"] = [
        {
            "id": "view:result-observed", "vocabulary_id": "view:vocabulary",
            "predicate": "view:observed", "polarity": "positive",
            "arguments": {
                "subject": {"kind": "json_pointer", "pointer": "/subject"},
                "passed": {"kind": "json_pointer", "pointer": "/passed"},
            },
        },
        {
            "id": "view:result-failed", "vocabulary_id": "view:vocabulary",
            "predicate": "view:observed", "polarity": "positive",
            "arguments": {
                "subject": {"kind": "json_pointer", "pointer": "/subject"},
                "passed": {"kind": "json_pointer", "pointer": "/failed"},
            },
        },
    ]
    if evidence_plan:
        claim["logic_evidence_plan"] = {
            "schema_version": EVIDENCE_PLAN_SCHEMA,
            "required_bindings": [{
                "result_id": "artifact:result",
                "binding_id": "view:result-observed",
            }],
        }
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    config = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    config["logic"] = {
        "derivations": "provsleuth/derivations",
        "vocabularies": ["provsleuth/logic/vocabulary.json"],
        "rule_packs": ["provsleuth/logic/rules.json"],
    }
    cfg.config_path.write_text(json.dumps(config), encoding="utf-8")
    configured = Config(cfg.config_path)
    document = create_derivation(
        configured, "claim:result", ["artifact:result"],
        {
            "target": target,
            "facts": [{
                "atom": {
                    "predicate": "view:observed", "polarity": "positive",
                    "arguments": {
                        "subject": {"type": "view:subject", "value": "widget", "unit": None},
                        "passed": {"type": "ct:boolean", "value": passed, "unit": None},
                    },
                },
                "evidence": [{
                    "result_id": "artifact:result", "binding_id": "view:result-observed",
                }],
                "assumption": None,
            }],
            "note": "Deterministic view fixture.",
            "provenance": {"agent": "agent:view-test"},
        },
        vocabulary=vocabulary, rule_pack=rules, actor="agent:view-test",
        recorded_at="2026-07-14T03:00:00.000Z",
    )
    append_derivation(configured, document)
    return configured, document


def test_render_view_is_standalone_atomic_and_deterministic(tmp_path):
    cfg = _project(tmp_path)
    graph_before = cfg.graph_path.read_bytes()
    output = tmp_path / "reports" / "trajectory.html"

    first = render_view(cfg, output)
    first_bytes = output.read_bytes()
    second = render_view(cfg, output)

    assert first == second == {
        "path": str(output.resolve()),
        "nodes": 5,
        "edges": 4,
        "runs": 0,
        "layers": 3,
    }
    assert output.read_bytes() == first_bytes
    assert cfg.graph_path.read_bytes() == graph_before
    html = first_bytes.decode("utf-8")
    assert html.startswith("<!doctype html>")
    assert "Math.random" not in html
    assert "forceSimulation" not in html
    assert 'id="layer-select"' in html
    assert 'id="focus-select"' in html
    assert 'id="edge-select"' in html
    assert 'id="assessment-select"' in html
    assert 'id="semantic-policy-summary"' in html
    assert 'id="semantic-mapping-list"' in html
    assert '<button id="layout-reset" type="button">Auto-arrange</button>' in html
    assert 'class="move-controls" role="group"' in html
    assert 'id="layout-status"' in html
    assert 'role="region"' in html and 'aria-labelledby="detail-title"' in html
    assert 'role: "button"' not in html
    assert 'group.addEventListener("click"' in html
    assert 'group.addEventListener("pointerdown"' in html
    assert 'group.addEventListener("pointermove"' in html
    assert 'group.addEventListener("pointerup"' in html
    assert 'group.addEventListener("pointercancel"' in html
    assert 'group.addEventListener("lostpointercapture"' in html
    assert "ct-ancestor" in html and "ct-descendant" in html
    assert "ct-type-claim" in html and "ct-status-stale" in html
    payload = _payload(html)
    assert payload["semantic_policy"] == {
        "integrity": "ok",
        "require_active_policy": False,
        "current_mappings": [],
        "release_count": 0,
        "active_policy_configured": False,
        "configured_policy_id": None,
        "active_policy_active": False,
        "active_policy_valid": False,
        "active_policy_findings": [],
        "active_policy_id": None,
        "active_mapping_ids": [],
    }
    assert payload["summary"]["semantic_mappings"] == 0


def test_large_graph_payload_remains_complete_and_deterministic(tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    report = _large_report()
    monkeypatch.setattr(view_module, "build_report", lambda *_args, **_kwargs: report)
    output = tmp_path / "large-trajectory.html"

    first = render_view(cfg, output)
    first_bytes = output.read_bytes()
    second = render_view(cfg, output)
    payload = _payload(output.read_text(encoding="utf-8"))
    expected_node_ids = {item["id"] for item in report["graph"]["nodes"]}

    assert output.read_bytes() == first_bytes
    assert first == second
    assert first["nodes"] == len(report["graph"]["nodes"]) == 256
    assert first["edges"] == len(report["graph"]["edges"]) == 449
    assert payload["summary"]["semantic_nodes"] == 256
    assert payload["summary"]["semantic_edges"] == 449
    assert {item["node_id"] for item in payload["nodes"]} == expected_node_ids
    assert len({item["key"] for item in payload["nodes"]}) == 256
    assert len(payload["edges"]) == len({item["key"] for item in payload["edges"]}) == 449
    annotation = next(item for item in payload["edges"] if item["relation"] == "tried_before")
    assert annotation["kind"] == "annotation"
    assert annotation["traversable"] is False


def test_large_graph_exposes_bounded_projection_controls_and_status(tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    monkeypatch.setattr(
        view_module, "build_report", lambda *_args, **_kwargs: _large_report(),
    )
    output = tmp_path / "large-trajectory.html"
    render_view(cfg, output)
    html = output.read_text(encoding="utf-8")

    assert 'id="projection-overview"' in html
    assert 'id="projection-full"' in html
    assert 'id="expand-upstream"' in html
    assert 'id="expand-downstream"' in html
    assert 'id="projection-status"' in html
    assert 'role="status"' in html and 'aria-live="polite"' in html
    assert 'id="node-type-filters"' in html
    assert 'id="node-status-filters"' in html
    assert 'id="layer-filters"' in html
    assert 'id="edge-kind-filters"' in html
    assert 'role="group" aria-label="Visible graph controls"' in html
    assert 'aria-describedby="layout-status projection-status projection-boundary"' in html
    assert "Add one upstream hop" in html
    assert "Add one downstream hop" in html
    assert (
        "Filters and projection change only this view; the graph and provenance are unchanged."
        in html
    )
    assert "const visibleNodeKeys = new Set" in html
    assert "const visibleEdgeKeys = new Set" in html
    assert "function setProjectionMode(mode)" in html
    assert "function resetProjectionFilters()" in html
    assert 'projectionFull.addEventListener("click", function () {' in html
    assert "resetProjectionFilters();" in html
    assert "const fullLayoutPositions = new Map" in html
    status_body = _javascript_function(html, "updateProjectionStatus()")
    assert "visibleNodeKeys.size" in status_body
    assert "data.nodes.length" in status_body
    assert "visibleEdgeKeys.size" in status_body
    assert "data.edges.length" in status_body
    assert "applyVisibility(false)" in _javascript_function(
        html, "selectNode(key, shouldCenter)",
    )
    assert "applyVisibility(false)" in _javascript_function(html, "selectEdge(key)")


def test_auto_arrange_uses_active_filters_without_overwriting_full_layout(
        tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    monkeypatch.setattr(
        view_module, "build_report", lambda *_args, **_kwargs: _large_report(),
    )
    output = tmp_path / "large-trajectory.html"
    render_view(cfg, output)
    html = output.read_text(encoding="utf-8")

    filter_body = _javascript_function(html, "refreshAfterFilterChange()")
    assert "usesPersistentFullLayout()" in filter_body
    assert "restoreFullLayoutPositions()" in filter_body
    assert "applyVisibility(false)" in filter_body
    assert "applyVisibility(true)" in filter_body
    assert filter_body.count("fitViewport()") == 2
    assert "temporary filtered layout does not overwrite the saved full layout" in filter_body

    persistence_body = _javascript_function(html, "usesPersistentFullLayout()")
    assert 'projectionMode === "full"' in persistence_body
    assert "filtersAreDefault()" in persistence_body

    reset_body = _javascript_function(html, "resetLayout()")
    filtered_branch = reset_body[:reset_body.index("nodes.forEach")]
    assert "if (!usesPersistentFullLayout())" in filtered_branch
    assert "applyVisibility(true)" in filtered_branch
    assert "fitViewport()" in filtered_branch
    assert "fullLayoutPositions" not in filtered_branch
    assert "localStorage" not in filtered_branch
    assert "saved full layout" in filtered_branch
    assert "fullLayoutPositions.set" in reset_body
    assert "window.localStorage.removeItem(layoutStorageKey)" in reset_body

    finish_drag = _javascript_function(html, "finishDrag(event)")
    nudge = _javascript_function(html, "nudgeSelected(deltaX, deltaY, direction)")
    assert "if (usesPersistentFullLayout())" in finish_drag
    assert "if (usesPersistentFullLayout())" in nudge

    visibility_body = _javascript_function(html, "applyVisibility(shouldArrange)")
    assert "projectionFull.disabled = usesPersistentFullLayout()" in visibility_body
    assert "visibleNodeKeys.size === data.nodes.length" not in visibility_body

    canvas_body = _javascript_function(html, "updateCanvasSize()")
    assert "if (!visibleNodeKeys.has(key)) return" in canvas_body
    assert "visibleNodeKeys.size &&" not in canvas_body


def test_projection_expansion_is_one_hop_and_uses_dependency_adjacency(
        tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    monkeypatch.setattr(
        view_module, "build_report", lambda *_args, **_kwargs: _large_report(),
    )
    output = tmp_path / "large-trajectory.html"
    render_view(cfg, output)
    html = output.read_text(encoding="utf-8")

    one_hop_body = _javascript_function(html, "projectionOneHop(start, adjacency)")
    assert "adjacency.get(start)" in one_hop_body
    assert "while (" not in one_hop_body
    assert "closure(" not in one_hop_body
    assert "projectionOneHop(" not in one_hop_body[len(
        "function projectionOneHop(start, adjacency)"
    ):]

    expand_body = _javascript_function(html, "expandSelectedOneHop(adjacency)")
    assert "projectionOneHop(selected, adjacency)" in expand_body
    assert "visibleNodeKeys.add" in expand_body
    assert "setProjectionMode" not in expand_body
    assert 'expandUpstream.addEventListener("click"' in html
    assert "expandSelectedOneHop(incoming)" in html
    assert 'expandDownstream.addEventListener("click"' in html
    assert "expandSelectedOneHop(outgoing)" in html
    assert "if (edge.traversable)" in html


def test_projection_threshold_preserves_small_full_view_and_bounds_large_routing(
        tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    small_output = tmp_path / "small-trajectory.html"
    render_view(cfg, small_output)
    small_html = small_output.read_text(encoding="utf-8")
    small_payload = _payload(small_html)

    large_report = _large_report()
    monkeypatch.setattr(
        view_module, "build_report", lambda *_args, **_kwargs: large_report,
    )
    large_output = tmp_path / "large-trajectory.html"
    render_view(cfg, large_output)
    large_html = large_output.read_text(encoding="utf-8")
    large_payload = _payload(large_html)

    large_threshold_match = re.search(r"const largeGraphThreshold = (\d+);", large_html)
    fast_threshold_match = re.search(
        r"const fullViewFastRoutingThreshold = (\d+);", large_html,
    )
    assert large_threshold_match is not None
    assert fast_threshold_match is not None
    large_threshold = int(large_threshold_match.group(1))
    fast_threshold = int(fast_threshold_match.group(1))
    assert len(small_payload["nodes"]) <= large_threshold < len(large_payload["nodes"])
    assert 0 < fast_threshold < len(large_payload["edges"])
    assert 'const initialProjectionMode = largeGraph ? "overview" : "full";' in large_html
    assert "data.edges.length > largeGraphEdgeThreshold" in large_html
    assert "setProjectionMode(initialProjectionMode);" in large_html

    route_body = _javascript_function(large_html, "positionAllEdges()")
    assert "visibleEdgeKeys" in route_body
    assert "fullViewFastRoutingThreshold" in route_body
    assert "estimateDetailedRoutingWork" in route_body
    assert "routingEstimate.safe" in route_body
    assert "maximumDetailedGridPoints" in large_html
    assert "maximumDetailedRouteWork" in large_html
    assert "fastRouteForEdge" in route_body
    assert "function fastRouteForEdge(edge)" in large_html


def test_view_preserves_invalid_configured_policy_and_mapping_decision_context(
        tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    report = build_report(cfg, strict=False)
    mapping_id = "semantic-mapping:sha256:" + "a" * 64
    policy_id = "semantic-policy:sha256:" + "b" * 64
    finding = {
        "code": "SEMANTIC_POLICY_STALE",
        "severity": "error",
        "detail": "A selected mapping no longer matches its pinned local snapshot.",
    }
    report["semantics"] = {
        "integrity": "error",
        "policy": {"require_active_policy": False},
        "mapping_history": [{
            "id": mapping_id,
            "agent_input": {
                "relation": "skos:closeMatch",
                "target": {"iri": "https://example.org/onto/MemoryScore"},
                "rationale": "Definitions overlap but the local measure is narrower.",
                "limitations": ["The ontology omits the project instrument."],
                "provenance": {"agent": "agent:test"},
            },
            "mechanical_snapshot": {
                "local_term": {"term": {
                    "label": "Memory score",
                    "definition": "Score from the declared memory assessment.",
                }},
                "selected_candidate": {
                    "iri": "https://example.org/onto/MemoryScore",
                },
            },
        }],
        "current_mapping_evaluations": [{
            "mapping_id": mapping_id,
            "subject": {
                "terminology_id": "study:terms",
                "term_id": "study:memory-score",
            },
            "review": {"state": "accepted", "actor": "reviewer:human"},
            "current_derived": {
                "eligible_for_policy": False,
                "stale": True,
                "findings": [finding],
            },
        }],
        "policies": [],
        "active_policy": {
            "configured": True,
            "configured_policy_id": policy_id,
            "policy": None,
            "evaluation": None,
            "findings": [finding],
        },
        "active_mapping_ids": [],
    }
    monkeypatch.setattr(view_module, "build_report", lambda *_args, **_kwargs: report)

    output = tmp_path / "semantic-state.html"
    render_view(cfg, output)
    html = output.read_text(encoding="utf-8")
    payload = _payload(html)
    semantic_policy = payload["semantic_policy"]
    mapping = semantic_policy["current_mappings"][0]

    assert semantic_policy["active_policy_configured"] is True
    assert semantic_policy["configured_policy_id"] == policy_id
    assert semantic_policy["active_policy_active"] is False
    assert semantic_policy["active_policy_findings"] == [finding]
    assert mapping["local_term"]["definition"].startswith("Score from")
    assert mapping["rationale"].startswith("Definitions overlap")
    assert mapping["limitations"] == ["The ontology omits the project instrument."]
    assert mapping["provenance"]["agent"] == "agent:test"
    assert mapping["current_derived"]["findings"] == [finding]
    assert mapping["active"] is False
    assert "configured release is invalid or inactive" in html


def test_manual_layout_is_browser_local_sanitized_and_visual_only(tmp_path):
    cfg = _project(tmp_path)
    graph_before = cfg.graph_path.read_bytes()
    output = tmp_path / "trajectory.html"
    render_view(cfg, output)
    html = output.read_text(encoding="utf-8")
    payload = _payload(html)

    canonical = {
        node["key"]: (node["x"], node["y"], node["layer"], node["row"])
        for node in payload["nodes"]
    }
    assert canonical
    assert re.fullmatch(r"[0-9a-f]{64}", payload["layout_id"])
    assert "positions" not in payload
    assert cfg.graph_path.read_bytes() == graph_before
    assert "const defaultPositions = new Map" in html
    assert "window.localStorage.getItem(layoutStorageKey)" in html
    assert "window.localStorage.setItem(layoutStorageKey" in html
    assert "window.localStorage.removeItem(layoutStorageKey)" in html
    assert "version: 1" in html
    assert "layout_id: data.layout_id" in html
    assert "saved.layout_id !== data.layout_id" in html
    assert "Object.prototype.hasOwnProperty.call(saved.positions, key)" in html
    assert "Number.isFinite(value)" in html
    assert "maximumCanvasWidth" in html and "maximumCanvasHeight" in html
    assert "maximumCoordinate = 200000" not in html
    assert '"data-node-key": node.key' in html
    assert '"data-edge-key": edge.key' in html
    assert '"data-edge-label-for": edge.key' in html
    assert "positionAllEdges()" in html
    assert 'elements.hit.setAttribute("d", pathData)' in html
    assert "setPointerCapture" in html and "releasePointerCapture" in html
    assert "inverseMatrix: inverseMatrix" in html
    assert 'group.addEventListener("pointercancel", cancelDrag)' in html
    assert 'group.addEventListener("lostpointercapture", cancelDrag)' in html
    assert 'window.addEventListener("blur"' in html
    assert 'document.addEventListener("keydown"' not in html
    assert "browser-local and visual only" in html
    assert "the graph, provenance, and logical layers do not change" in html


def test_viewport_zoom_and_pan_are_transient_scoped_and_accessible(tmp_path):
    cfg = _project(tmp_path)
    graph_before = cfg.graph_path.read_bytes()
    output = tmp_path / "trajectory.html"
    render_view(cfg, output)
    html = output.read_text(encoding="utf-8")

    assert cfg.graph_path.read_bytes() == graph_before
    assert 'class="graph-scroll" role="region" tabindex="0"' in html
    assert 'aria-label="Graph view controls"' in html
    assert 'id="zoom-out"' in html
    assert 'id="zoom-reset"' in html
    assert 'id="zoom-in"' in html
    assert 'id="viewport-fit"' in html
    assert "height: clamp(420px, 68vh, 760px)" in html
    assert "cursor: grab" in html and "cursor: grabbing" in html
    assert "vector-effect: non-scaling-stroke" in html
    assert "minimumViewportZoom = 0.25" in html
    assert "maximumViewportZoom = 3" in html
    assert "viewportZoomStep = 1.2" in html
    assert "function applyZoomDimensions()" in html
    assert 'svg.style.width = Math.max(1, width * viewportZoom) + "px"' in html
    assert "function setViewportZoom(value, clientX, clientY)" in html
    assert "if (dragState || panState || !Number.isFinite(value)) return false" in html
    assert "function fitViewport()" in html
    assert "function centerNodeInViewport(node)" in html
    assert "centerNodeInViewport(selectedNode)" in html
    assert "function panTargetIsInteractive(target)" in html
    assert 'target.closest(".ct-node, .ct-edge-group")' in html
    assert "function beginPan(event)" in html
    assert "graphScroll.setPointerCapture(event.pointerId)" in html
    assert "graphScroll.releasePointerCapture(event.pointerId)" in html
    assert "function handleViewportWheel(event)" in html
    assert 'graphScroll.addEventListener("wheel", handleViewportWheel, {passive: false})' in html
    assert 'graphScroll.addEventListener("pointerdown", beginPan)' in html
    assert 'graphScroll.addEventListener("pointermove", continuePan)' in html
    assert 'graphScroll.addEventListener("pointerup", finishPan)' in html
    assert 'graphScroll.addEventListener("pointercancel", cancelPan)' in html
    assert 'graphScroll.addEventListener("lostpointercapture", cancelPan)' in html
    assert "if (panState) cancelPan({pointerId: panState.pointerId})" in html
    save_layout = html[
        html.index("function saveLayout") : html.index("function viewportClientCenter")
    ]
    assert "viewportZoom" not in save_layout
    assert "panState" not in save_layout


def test_edges_are_selectable_and_use_obstacle_aware_routes(tmp_path):
    cfg = _project(tmp_path)
    graph_before = cfg.graph_path.read_bytes()
    output = tmp_path / "trajectory.html"
    render_view(cfg, output)
    html = output.read_text(encoding="utf-8")

    assert cfg.graph_path.read_bytes() == graph_before
    assert '"class": "ct-edge-hit"' in html
    assert "stroke-width: 18px" in html
    assert 'markerUnits="strokeWidth"' not in html
    assert html.count('markerUnits="userSpaceOnUse"') == 6
    assert 'id="arrow-selected"' in html
    assert 'markerWidth="7" markerHeight="7"' in html
    assert '"data-edge-hit-key": edge.key' in html
    assert '"data-edge-group-key": edge.key' in html
    assert '"role": "button"' in html
    assert 'group.addEventListener("click", function () { selectEdge(edge.key); })' in html
    assert 'group.addEventListener("keydown"' in html
    assert 'event.key !== "Enter" && event.key !== " "' in html
    assert "function buildRoutingContext()" in html
    assert "function buildEdgePorts()" in html
    assert "function routeBetween(start, target, context)" in html
    assert "function nearbySegmentUsage(left, right, usedSegments)" in html
    assert "function segmentBlocked(left, right, obstacles)" in html
    assert "routingClearance = 10" in html
    assert "routingLaneSeparation = 20" in html
    assert "edgeApproachLength = 20" in html
    assert "edgeArrowGap = 5" in html
    assert "edgeCornerRadius = 8" in html
    assert "function clearEscapeDistance(node, side, coordinate, desiredDistance)" in html
    assert "canvasPadding - 2, edgeApproachLength + (escapeOffset || 0)" in html
    assert "appendRoutePoint(points, pair.target.tip)" in html
    assert "function roundedCornerClear(before, corner, after)" in html
    assert "const radius = Math.min(edgeCornerRadius, incoming / 2, outgoing / 2)" in html
    assert '" Q" + corner.x + "," + corner.y' in html
    assert "positionAllEdges();" in html
    assert "nodePositionOverlaps(key, boundedX, boundedY)" in html
    assert "selectedEdge = edges.has(key) ? key : null" in html
    assert 'element.classList.toggle("ct-edge-source"' in html
    assert 'element.classList.toggle("ct-edge-target"' in html
    assert 'elements.group.classList.toggle("ct-edge-selected"' in html
    assert '(exactSelection ? "selected" : edge.kind)' in html
    assert 'appendDetail(list, "Relation", edge.relation)' in html
    assert 'appendDetail(list, "Trajectory traversal"' in html
    assert "relationship focus highlights only its direct endpoints" in html


def test_layout_is_layered_and_annotations_do_not_define_ancestry(tmp_path):
    cfg = _project(tmp_path)
    output = tmp_path / "trajectory.html"
    render_view(cfg, output)
    payload = _payload(output.read_text(encoding="utf-8"))
    nodes = {node["key"]: node for node in payload["nodes"]}

    dependency_edges = [
        edge for edge in payload["edges"] if edge["kind"] == "dependency"
    ]
    assert dependency_edges
    assert all(
        nodes[edge["source"]]["layer"] < nodes[edge["target"]]["layer"]
        for edge in dependency_edges
    )
    annotation = next(
        edge for edge in payload["edges"] if edge["relation"] == "tried_before"
    )
    assert annotation["kind"] == "annotation"
    assert annotation["traversable"] is False
    support = next(edge for edge in payload["edges"]
                   if edge["key"].startswith("semantic:")
                   and edge.get("assessment_state") == "unassessed")
    assert support["relation"] == "declared support · unassessed"
    assert [node["layer"] for node in payload["nodes"]] == sorted(
        node["layer"] for node in payload["nodes"]
    )


def test_run_receipt_is_linked_to_bound_output_with_partial_label(tmp_path):
    cfg = _project(tmp_path)
    result = _record_run(cfg)
    output = tmp_path / "trajectory.html"
    summary = render_view(cfg, output)
    payload = _payload(output.read_text(encoding="utf-8"))
    nodes = {node["key"]: node for node in payload["nodes"]}

    assert summary["runs"] == 1
    receipt = next(node for node in payload["nodes"] if node["kind"] == "run")
    binding = next(edge for edge in payload["edges"] if edge["kind"] == "receipt")
    assert receipt["node_id"] == result["run_id"]
    assert receipt["event_store_integrity"] == "ok"
    assert receipt["link_integrity"] == "ok"
    assert receipt["link_issues"] == []
    assert receipt["evidence_eligible"] is True
    assert binding["source"] == receipt["key"]
    assert binding["target"] == "graph:artifact:result"
    assert binding["relation"] == "binds declared output"
    assert binding["declaration_comparison"] == "declarations_agree"
    assert receipt["layer"] < nodes[binding["target"]]["layer"]
    assert "not observed reads" in receipt["coverage"]
    assert "do not prove write causation" in payload["coverage_notice"]
    assert "Event-store integrity" in output.read_text(encoding="utf-8")
    assert "Run-link integrity" in output.read_text(encoding="utf-8")
    assert "Replay conflict" in output.read_text(encoding="utf-8")
    assert "Evidence eligible" in output.read_text(encoding="utf-8")
    assert nodes["graph:artifact:result"]["run_ids"] == [result["run_id"]]


def test_run_detail_distinguishes_materialized_intermediate_boundary_evidence(
        tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    _record_run(cfg)
    report = build_report(cfg)
    run = report["receipts"]["runs"][0]
    run["declared_intermediates"] = ["data/clean.csv"]
    run["intermediate_transitions"] = [{
        "path": "data/clean.csv", "transition": "created", "produced": True,
        "before": {"path": "data/clean.csv", "state": "missing"},
        "after": {"path": "data/clean.csv", "state": "stable",
                  "sha256": "a" * 64, "size": 12},
    }]
    run["bindings"].append({
        "binding_kind": "materialized_intermediate_path",
        "node_id": "artifact:result",
        "path": "data/clean.csv",
        "declaration_comparison": "contract_role_agrees",
        "output_evidence": "post_process_content_transition_detected",
        "stage_attribution": "not_observed",
        "current": True,
    })
    run["replays"] = [{
        "id": "replay:sha256:" + "b" * 64,
        "outcome": "byte_repeatable", "undeclared_write_paths": [],
        "current_derived": {
            "byte_repeatable_current": True, "review_ready_current": True,
        },
        "comparison": {
            "materialized_intermediates_equal": True,
            "all_materialized_intermediates_match_source_receipt": True,
        },
    }]
    monkeypatch.setattr(view_module, "build_report", lambda _cfg, strict=False: report)

    output = tmp_path / "trajectory.html"
    render_view(cfg, output)
    html = output.read_text(encoding="utf-8")
    payload = _payload(html)
    receipt = next(node for node in payload["nodes"] if node["kind"] == "run")
    intermediate_edge = next(
        edge for edge in payload["edges"]
        if edge.get("binding_kind") == "materialized_intermediate_path"
    )

    assert receipt["declared_outputs"] == ["out.txt"]
    assert receipt["declared_intermediates"] == ["data/clean.csv"]
    assert receipt["intermediate_transitions"][0]["after"]["sha256"] == "a" * 64
    assert receipt["bindings"][-1]["output_evidence"] == (
        "post_process_content_transition_detected"
    )
    assert intermediate_edge["current"] is True
    assert intermediate_edge["stage_attribution"] == "not_observed"
    assert intermediate_edge["output_evidence"] == (
        "post_process_content_transition_detected"
    )
    assert "Declared materialized intermediate paths" in html
    assert "Materialized intermediate file transitions" in html
    assert "Replay materialized-intermediate comparison" in html
    assert "content changed in process window; write causation not proven" in html
    assert "content changed in process window; stage causation not proven" in html
    assert "Boundary evidence" in html
    assert "Stage attribution" in html
    assert "Current binding" in html
    assert ", produced" not in html


def test_run_and_claim_details_expose_cooperative_stage_trace_without_stage_nodes(
        tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    _record_run(cfg)
    report = build_report(cfg)
    run = report["receipts"]["runs"][0]
    run["event_schema_version"] = "claimtrace.event/4"
    run["pipeline_contract"] = {
        "stages": [
            {
                "id": "clean", "method_id": "method:analysis",
                "method_step_id": "clean", "depends_on": [],
                "execution_observation": "declared_only_not_observed",
            },
            {
                "id": "fit", "method_id": "method:analysis",
                "method_step_id": "fit", "depends_on": ["clean"],
                "execution_observation": "declared_only_not_observed",
            },
        ],
    }
    run["stage_trace_plan"] = {
        "schema_version": "claimtrace.stage-trace-plan/1",
        "required_stage_ids": ["clean", "fit"],
    }
    run["stage_trace"] = {
        "schema_version": "claimtrace.stage-trace/1",
        "mode": "cooperative_child_checkpoint_log",
        "state": "cooperative_report_complete",
        "trust": "cooperative_child_self_report_not_independent_observation",
        "binding": {
            "nonce_sha256": "a" * 64,
            "transport": "controller_created_private_file",
            "raw_sha256": "b" * 64,
            "raw_size": 451,
        },
        "required_stage_ids": ["clean", "fit"],
        "checkpoints": [
            {
                "sequence": 1, "stage_id": "clean",
                "code_node_id": "code:pipeline",
                "callsite": {"path": "analysis.py", "line": 12},
                "observation": "program_emitted_checkpoint_reached",
            },
            {
                "sequence": 2, "stage_id": "fit",
                "code_node_id": "code:pipeline",
                "callsite": {"path": "analysis.py", "line": 27},
                "observation": "program_emitted_checkpoint_reached",
            },
        ],
        "checkpoint_sequence_sha256": "c" * 64,
        "issues": [],
    }
    run["replays"] = [{
        "id": "replay:sha256:" + "d" * 64,
        "outcome": "byte_repeatable",
        "undeclared_write_paths": [],
        "current_derived": {
            "byte_repeatable_current": True,
            "review_ready_current": True,
            "stage_trace_repeatable_current": True,
        },
        "comparison": {
            "materialized_intermediates_equal": True,
            "all_materialized_intermediates_match_source_receipt": True,
            "all_stage_traces_complete": True,
            "stage_traces_equal": True,
            "all_stage_traces_match_source_receipt": True,
        },
    }]
    report["claim_basis"] = {"items": [{
        "assessment_id": "assessment:fixture",
        "result_id": "artifact:result",
        "claim_id": "claim:result",
        "overall": "ready_under_reviewed_provenance",
        "execution_state": "current_producing_receipt",
        "contract_state": "current",
        "ancestry_state": "current",
        "materialized_intermediate_state": "not_required",
        "replay_state": "byte_repeatable_current",
        "method_state": "accepted_current_conformance",
        "stage_checkpoint_state": "cooperative_report_repeatable_current",
        "stage_execution_observation": (
            "cooperative_checkpoint_self_report_not_independent_observation"
        ),
    }]}
    monkeypatch.setattr(view_module, "build_report", lambda _cfg, strict=False: report)

    output = tmp_path / "stage-trace.html"
    render_view(cfg, output)
    html = output.read_text(encoding="utf-8")
    payload = _payload(html)
    receipt = next(node for node in payload["nodes"] if node["kind"] == "run")
    claim = next(
        node for node in payload["nodes"] if node["key"] == "graph:claim:result"
    )

    assert receipt["stage_trace"]["state"] == "cooperative_report_complete"
    assert [(item["stage_id"], item["callsite"]) for item in
            receipt["stage_trace"]["checkpoints"]] == [
        ("clean", {"path": "analysis.py", "line": 12}),
        ("fit", {"path": "analysis.py", "line": 27}),
    ]
    assert payload["summary"]["stage_checkpoint_runs"] == 1
    assert payload["summary"]["complete_stage_checkpoint_runs"] == 1
    assert not any(node["kind"] == "stage" for node in payload["nodes"])
    assert claim["claim_basis"][0]["stage_checkpoint_state"] == (
        "cooperative_report_repeatable_current"
    )
    assert "Cooperative checkpoint trace" in html
    assert "Program-emitted stage checkpoints" in html
    assert "Replay cooperative-stage trace comparison" in html
    assert '", checkpoint=" + (item.stage_checkpoint_state || "not available")' in html
    assert "child-program self-report that locked callsites were reached" in html
    assert "not independent observation of stage computation, scientific meaning, or in-memory values" in html


def test_view_labels_stale_historical_replacement_and_links_current_run(
        tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    historical_result = _record_run(cfg)
    replacement_result = _record_run(cfg)
    report = build_report(cfg)
    runs = {
        item["run_id"]: item for item in report["receipts"]["runs"]
    }
    historical = runs[historical_result["run_id"]]
    replacement = runs[replacement_result["run_id"]]
    replacement_replay_id = "replay:sha256:" + "e" * 64
    historical.update({
        "pipeline_contract_state": "stale_or_invalid",
        "current_gate_role": "historical_replaced",
        "replacement_run_id": replacement["run_id"],
        "replacement_replay_ids": [replacement_replay_id],
    })
    replacement["replays"] = [{
        "id": replacement_replay_id,
        "outcome": "byte_repeatable",
        "undeclared_write_paths": [],
        "current_derived": {
            "byte_repeatable_current": True,
            "review_ready_current": True,
        },
        "comparison": {},
    }]
    monkeypatch.setattr(view_module, "build_report", lambda _cfg, strict=False: report)

    output = tmp_path / "historical-replacement.html"
    render_view(cfg, output)
    html = output.read_text(encoding="utf-8")
    payload = _payload(html)
    historical_node = next(
        node for node in payload["nodes"]
        if node.get("node_id") == historical_result["run_id"]
    )

    assert historical_node["status"] == "succeeded"
    assert historical_node["pipeline_contract_state"] == "stale_or_invalid"
    assert historical_node["current_gate_role"] == "historical_replaced"
    assert historical_node["replacement_run_id"] == replacement_result["run_id"]
    assert historical_node["replacement_replay_ids"] == [replacement_replay_id]
    assert not any(edge["kind"] == "replacement" for edge in payload["edges"])
    assert '"historical replacement · stale"' in html
    assert 'appendDetail(list, "Current gate role"' in html
    assert 'appendRunLinkDetail(list, "Replacement run"' in html
    assert '"Replacement replay certificates"' in html
    assert 'linkButton.setAttribute("data-replacement-run-id", runId)' in html
    assert "selectNode(replacementKey)" in html
    assert "stale historical run is retained for audit" in html


def test_view_marks_integrity_quarantined_run_as_historical_only(
        tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    _record_run(cfg)
    report = build_report(cfg)
    run = report["receipts"]["runs"][0]
    run.update({
        "event_store_integrity": "error",
        "link_integrity": "error",
        "link_issues": [{
            "code": "RUN_INPUT_SNAPSHOT_MISMATCH",
            "detail": "stored start and finish input baselines differ",
        }],
        "evidence_eligible": False,
    })
    monkeypatch.setattr(view_module, "build_report", lambda _cfg, strict=False: report)

    output = tmp_path / "quarantined.html"
    render_view(cfg, output)
    html = output.read_text(encoding="utf-8")
    payload = _payload(html)
    receipt = next(node for node in payload["nodes"] if node["kind"] == "run")

    assert receipt["status"] == "integrity_error"
    assert receipt["evidence_eligible"] is False
    assert receipt["link_issues"][0]["code"] == "RUN_INPUT_SNAPSHOT_MISMATCH"
    assert "historical run is quarantined" in html
    assert "not attributed to a stage" in html
    assert "pathless or in-memory intermediates remain declared" in html


def test_explicit_semantic_run_reference_is_not_labelled_as_output_binding(tmp_path):
    cfg = _project(tmp_path)
    result = _record_run(cfg)
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    experiment = next(node for node in graph["nodes"] if node["id"] == "experiment:null")
    experiment["run_ids"] = [result["run_id"]]
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")

    output = tmp_path / "trajectory.html"
    render_view(cfg, output)
    payload = _payload(output.read_text(encoding="utf-8"))
    edge = next(item for item in payload["edges"]
                if item.get("binding_kind") == "explicit_run_reference")
    assert edge["target"] == "graph:experiment:null"
    assert edge["relation"] == "explicit semantic run reference"


def test_graph_level_findings_remain_inspectable_in_overview(tmp_path):
    cfg = _project(tmp_path)
    run_command(
        cfg,
        [sys.executable, "-c", "from pathlib import Path; Path('side.txt').write_text('x')"],
        inputs=[], outputs=["side.txt"], no_inputs=True, cwd=str(tmp_path),
    )
    output = tmp_path / "trajectory.html"
    render_view(cfg, output)
    html = output.read_text(encoding="utf-8")
    payload = _payload(html)

    finding = next(item for item in payload["global_findings"]
                   if item["code"] == "UNBOUND_RUN_OUTPUT")
    assert "side.txt" in finding["detail"]
    assert "data.global_findings" in html


def test_embedded_data_cannot_break_out_of_json_script(tmp_path):
    malicious = "</script><img src=x onerror=alert(1)>&\u2028end"
    cfg = _project(tmp_path, malicious_value=malicious)
    output = tmp_path / "trajectory.html"
    render_view(cfg, output)
    html = output.read_text(encoding="utf-8")

    assert "</script><img" not in html
    assert "<img src=x" not in html
    assert "\\u003c/script\\u003e" in html
    assert "\\u0026" in html
    assert ".innerHTML" not in html
    claim = next(
        node for node in _payload(html)["nodes"]
        if node["key"] == "graph:claim:result"
    )
    assert claim["value"] == malicious


def test_accepted_assessment_is_a_derived_node_between_result_and_claim(tmp_path):
    cfg = _project(tmp_path)
    accepted = _record_assessment(cfg, accepted=True)
    output = tmp_path / "trajectory.html"
    render_view(cfg, output)
    html = output.read_text(encoding="utf-8")
    payload = _payload(html)
    nodes = {node["key"]: node for node in payload["nodes"]}

    assert payload["schema"] == "claimtrace.view/6"
    rendered_review = next(
        item for item in payload["assessments"] if item["id"] == accepted["id"]
    )
    assert rendered_review["schema_version"] == accepted["schema_version"]
    review = nodes["review:" + accepted["id"]]
    assert review["kind"] == "assessment"
    assert review["status"] == "accepted"
    assert (nodes["graph:artifact:result"]["layer"] < review["layer"]
            < nodes["graph:claim:result"]["layer"])
    declared = next(edge for edge in payload["edges"]
                    if edge.get("assessment_state") == "covered"
                    and edge["kind"] == "dependency")
    assert declared["relation"] == "declared support · accepted assessment"
    assessment_edges = [edge for edge in payload["edges"] if edge["kind"] == "assessment"]
    assert len(assessment_edges) == 2
    assert all(edge["traversable"] for edge in assessment_edges)
    assert 'document.createElement("table")' in html
    assert "Claim as written" in html and "Result actually obtained" in html
    assert 'appendDetail(provenanceList, "Policy schema", review.schema_version)' in html


def test_structural_result_to_claim_edge_exposes_semantic_coverage(tmp_path):
    cfg = _project(tmp_path)
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    support = next(edge for edge in graph["edges"] if edge["rel"] == "supports")
    support["rel"] = "derives_from"
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    accepted = _record_assessment(cfg, accepted=True)

    output = tmp_path / "trajectory.html"
    render_view(cfg, output)
    payload = _payload(output.read_text(encoding="utf-8"))
    edge = next(
        item for item in payload["edges"]
        if item["kind"] == "dependency"
        and item["source"] == "graph:artifact:result"
        and item["target"] == "graph:claim:result"
    )

    assert edge["relation"] == "derives_from"
    assert edge["assessment_state"] == "covered"
    claim = next(item for item in payload["nodes"]
                 if item["key"] == "graph:claim:result")
    assert claim["claim_links"] == [{
        "from": "artifact:result",
        "to": "claim:result",
        "declared_relation": "derives_from",
        "status": "covered",
        "assessment_ids": [accepted["id"]],
        "assessed_relations": ["supports"],
    }]


def test_symbolic_derivation_is_one_nontraversable_composite_proof_node(tmp_path):
    cfg = _project(tmp_path)
    cfg, document = _record_symbolic_proof(cfg)
    output = tmp_path / "trajectory.html"
    render_view(cfg, output)
    html = output.read_text(encoding="utf-8")
    payload = _payload(html)
    nodes = {node["key"]: node for node in payload["nodes"]}

    assert payload["schema"] == "claimtrace.view/6"
    assert payload["derivation_integrity"] == "ok"
    proof = nodes["proof:" + document["id"]]
    assert proof["kind"] == "proof"
    assert proof["status"] == "derivable"
    assert proof["proof"]["used_result_ids"] == ["artifact:result"]
    proof_edges = [edge for edge in payload["edges"] if edge["kind"] == "proof"]
    assert len(proof_edges) == 2
    assert not any(edge["traversable"] for edge in proof_edges)
    assert [edge["relation"] for edge in proof_edges] == [
        "composite premise 1/1",
        "formal conclusion · target derivable under view:rules",
    ]
    assert "not a certificate of truth, scientific meaning, or evidentiary support" in html
    assert 'const proof = node.kind === "proof" ? (node.proof || {}) : null;' in html


def test_claim_owned_evidence_plan_is_visible_on_semantic_claim(tmp_path):
    cfg = _project(tmp_path)
    cfg, document = _record_symbolic_proof(cfg, evidence_plan=True)
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    claim = next(item for item in graph["nodes"] if item["id"] == "claim:result")
    plan = claim["logic_evidence_plan"]
    assert plan == document["mechanical_snapshot"]["claim"]["evidence_plan"]

    output = tmp_path / "planned-claim.html"
    render_view(cfg, output)
    html = output.read_text(encoding="utf-8")
    payload = _payload(html)
    rendered_claim = next(
        node for node in payload["nodes"] if node["key"] == "graph:claim:result"
    )

    assert rendered_claim["evidence_plan"] == plan
    assert '"claim-owned exact all-of"' in html
    assert '"Evidence plan"' in html
    assert '"Required bindings"' in html
    assert 'item.result_id + " / " + item.binding_id' in html


def test_proof_details_expose_stored_mechanical_evidence_plan(tmp_path):
    cfg = _project(tmp_path)
    cfg, document = _record_symbolic_proof(cfg, evidence_plan=True)
    stored_plan = document["mechanical_snapshot"]["claim"]["evidence_plan"]
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    claim = next(item for item in graph["nodes"] if item["id"] == "claim:result")
    claim["logic_evidence_plan"]["required_bindings"][0]["binding_id"] = (
        "view:result-failed"
    )
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    output = tmp_path / "planned-proof.html"

    render_view(cfg, output)
    html = output.read_text(encoding="utf-8")
    payload = _payload(html)
    proof = next(node for node in payload["nodes"] if node["kind"] == "proof")
    current_claim = next(
        node for node in payload["nodes"] if node["key"] == "graph:claim:result"
    )

    assert proof["proof"]["evidence_plan"] == stored_plan
    assert proof["proof"]["evidence_plan"] == {
        "schema_version": EVIDENCE_PLAN_SCHEMA,
        "required_bindings": [{
            "result_id": "artifact:result",
            "binding_id": "view:result-observed",
        }],
    }
    assert current_claim["evidence_plan"]["required_bindings"][0]["binding_id"] == (
        "view:result-failed"
    )
    assert proof["proof"]["stale"] is True
    assert '"Stored evidence plan"' in html
    assert '"Stored plan schema"' in html
    assert '"Stored required bindings"' in html


def test_refutable_symbolic_outcome_is_never_drawn_as_claim_support(tmp_path):
    cfg = _project(tmp_path)
    cfg, document = _record_symbolic_proof(cfg, passed=False)
    output = tmp_path / "refutable.html"
    render_view(cfg, output)
    payload = _payload(output.read_text(encoding="utf-8"))
    proof = next(node for node in payload["nodes"] if node["kind"] == "proof")
    proof_edges = [edge for edge in payload["edges"] if edge["kind"] == "proof"]

    assert document["derived"]["proof_state"] == "refutable"
    assert proof["status"] == "refutable"
    assert proof["value"] == "widget does not satisfy the configured validation rule."
    assert proof_edges[-1]["relation"] == (
        "formal refutation · target refutable under view:rules"
    )
    assert "The target is refutable because its explicit opposite is derivable" in (
        output.read_text(encoding="utf-8")
    )


def test_unknown_symbolic_evaluation_shows_checked_inputs_but_no_active_proof(tmp_path):
    cfg = _project(tmp_path)
    cfg, document = _record_symbolic_proof(
        cfg, passed=False, include_negative_rule=False,
    )
    output = tmp_path / "unknown.html"
    render_view(cfg, output)
    payload = _payload(output.read_text(encoding="utf-8"))
    proof = next(node for node in payload["nodes"] if node["kind"] == "proof")
    proof_edges = [edge for edge in payload["edges"] if edge["kind"] == "proof"]

    assert document["derived"]["proof_state"] == "unknown"
    assert proof["status"] == "inactive_unknown"
    assert proof_edges[0]["relation"] == "evaluated input 1/1"
    assert proof_edges[-1]["relation"] == "inactive formal outcome · unknown"


def test_duplicate_submissions_render_as_one_canonical_proof_node(tmp_path):
    cfg = _project(tmp_path)
    cfg, first = _record_symbolic_proof(cfg)
    vocabulary = load_vocabulary(cfg.base / "provsleuth" / "logic" / "vocabulary.json")
    rules = load_rule_pack(
        cfg.base / "provsleuth" / "logic" / "rules.json", vocabulary,
    )
    proposal = json.loads(json.dumps(first["agent_input"]))
    proposal["note"] = "Independent duplicate submission."
    second = create_derivation(
        cfg, "claim:result", ["artifact:result"], proposal,
        vocabulary=vocabulary, rule_pack=rules, actor="agent:duplicate",
        recorded_at="2026-07-14T03:00:01.000Z",
    )
    append_derivation(cfg, second)
    output = tmp_path / "deduplicated.html"
    render_view(cfg, output)
    payload = _payload(output.read_text(encoding="utf-8"))
    proofs = [node for node in payload["nodes"] if node["kind"] == "proof"]

    assert first["derived"]["proof_id"] == second["derived"]["proof_id"]
    assert len(proofs) == 1
    assert proofs[0]["derivation_ids"] == sorted([first["id"], second["id"]])
    assert payload["summary"]["derivations"] == 2
    assert payload["summary"]["proof_nodes"] == 1


def test_cross_derivation_opposites_render_as_one_claim_conflict_node(tmp_path):
    cfg = _project(tmp_path)
    cfg, positive = _record_symbolic_proof(cfg)
    vocabulary = load_vocabulary(cfg.base / "provsleuth" / "logic" / "vocabulary.json")
    rules = load_rule_pack(
        cfg.base / "provsleuth" / "logic" / "rules.json", vocabulary,
    )
    proposal = json.loads(json.dumps(positive["agent_input"]))
    proposal["facts"][0]["atom"]["arguments"]["passed"]["value"] = False
    proposal["facts"][0]["evidence"][0]["binding_id"] = "view:result-failed"
    negative = create_derivation(
        cfg, "claim:result", ["artifact:result"], proposal,
        vocabulary=vocabulary, rule_pack=rules, actor="agent:negative",
        recorded_at="2026-07-14T03:00:01.000Z",
    )
    append_derivation(cfg, negative)
    output = tmp_path / "conflict.html"
    render_view(cfg, output)
    payload = _payload(output.read_text(encoding="utf-8"))
    proofs = [node for node in payload["nodes"] if node["kind"] == "proof"]
    proof_edges = [edge for edge in payload["edges"] if edge["kind"] == "proof"]

    assert positive["derived"]["proof_state"] == "derivable"
    assert negative["derived"]["proof_state"] == "refutable"
    assert len(proofs) == 1
    assert proofs[0]["status"] == "claim_conflict"
    assert proofs[0]["type"] == "symbolic conflict"
    assert len(proofs[0]["proof"]["conditional_proofs"]) == 2
    assert proof_edges[-1]["relation"] == (
        "formal conflict · target and opposite conditionally derivable under view:rules"
    )
    assert payload["summary"]["active_proofs"] == 0
    assert payload["summary"]["claim_conflicts"] == 1
    html = output.read_text(encoding="utf-8")
    assert ".ct-type-symbolic-conflict polygon" in html
    assert ".ct-status-claim-conflict" in html


def test_related_assessment_is_visible_but_not_dependency_traversable(tmp_path):
    cfg = _project(tmp_path)
    agent_input = _agent_input("supports_narrower_claim")
    agent_input["claim_frame"]["inference_level"] = "causal"
    agent_input["alignment"]["inference_level"] = "mismatch"
    agent_input["recommended_claim"] = "The measured input was associated with output."
    accepted = _record_assessment(cfg, accepted=True, agent_input=agent_input)
    output = tmp_path / "trajectory.html"
    render_view(cfg, output)
    payload = _payload(output.read_text(encoding="utf-8"))
    review = next(
        node for node in payload["nodes"] if node["key"] == "review:" + accepted["id"]
    )
    assert review["status"] == "accepted"
    assessment_edges = [
        edge for edge in payload["edges"] if edge["kind"] == "assessment"
    ]
    assert assessment_edges and not any(edge["traversable"] for edge in assessment_edges)


def test_corrupt_assessment_store_keeps_history_but_renders_no_active_relation(tmp_path):
    cfg = _project(tmp_path)
    accepted = _record_assessment(cfg, accepted=True)
    predecessor_id = accepted["review"]["supersedes_assessment_id"]
    (cfg.assessments_path / (predecessor_id.rsplit(":", 1)[1] + ".json")).unlink()

    output = tmp_path / "trajectory.html"
    render_view(cfg, output)
    payload = _payload(output.read_text(encoding="utf-8"))

    assert payload["assessment_integrity"] == "error"
    review = next(item for item in payload["assessments"] if item["id"] == accepted["id"])
    assert review["review_state"] == "accepted"
    assert review["verdict"] == "supports_as_written"
    assert review["active_relation"] is None
    assert review["integrity_blocked"] is True
    assert any(node["key"] == "review:" + accepted["id"] for node in payload["nodes"])

    assessment_edges = [edge for edge in payload["edges"] if edge["kind"] == "assessment"]
    assert len(assessment_edges) == 2
    assert not any(edge["traversable"] for edge in assessment_edges)
    claim_edge = next(edge for edge in assessment_edges if edge["source"].startswith("review:"))
    assert claim_edge["relation"] == "inactive · assessment store integrity error"
    declared = next(
        edge for edge in payload["edges"]
        if edge["kind"] == "dependency" and edge.get("assessment_state")
    )
    assert declared["assessment_state"] == "unassessed"
    assert any(
        item["code"] == "ASSESSMENT_REVIEW_CHAIN"
        for item in payload["global_findings"]
    )


def test_multiple_unreviewed_assessments_remain_sorted_without_contesting_accepted_state(tmp_path):
    cfg = _project(tmp_path)
    first = _record_assessment(cfg, "supports_as_written")
    second = _record_assessment(
        cfg, "contradicts_as_written", recorded_at="2026-07-13T08:02:00.000Z",
    )
    output = tmp_path / "trajectory.html"
    render_view(cfg, output)
    payload = _payload(output.read_text(encoding="utf-8"))
    claim = next(node for node in payload["nodes"] if node["key"] == "graph:claim:result")

    assert claim["assessment_ids"] == sorted([first["id"], second["id"]])
    current_reviews = [node for node in payload["nodes"] if node["kind"] == "assessment"]
    assert len(current_reviews) == 2
    assert {node["status"] for node in current_reviews} == {"proposed"}
    assert payload["summary"]["contested_assessments"] == 0
    declared = next(edge for edge in payload["edges"]
                    if edge["kind"] == "dependency" and edge.get("assessment_state"))
    assert declared["assessment_state"] == "unassessed"


def test_assessment_text_is_script_safe_and_preserved(tmp_path):
    malicious = "</script><img src=x onerror=alert(1)>&\u2028review"
    cfg = _project(tmp_path)
    agent_input = _agent_input("insufficient", rationale=malicious)
    agent_input["limitations"] = [malicious]
    assessment = _record_assessment(cfg, agent_input=agent_input)
    output = tmp_path / "trajectory.html"
    render_view(cfg, output)
    html = output.read_text(encoding="utf-8")

    assert "</script><img" not in html
    assert "<img src=x" not in html
    assert "\\u003c/script\\u003e" in html
    record = next(item for item in _payload(html)["assessments"]
                  if item["id"] == assessment["id"])
    assert record["rationale"] == malicious
    assert record["limitations"] == [malicious]


def test_deliberation_is_a_separate_advisory_audit_panel_not_graph_ancestry():
    report = _large_report(layer_count=2, rows_per_layer=1)
    candidate_id = "deliberation-candidate:sha256:" + "1" * 64
    proposal_id = "deliberation-proposal:sha256:" + "2" * 64
    candidate_set_id = "deliberation-set:sha256:" + "3" * 64
    ballot_id = "deliberation-ballot:sha256:" + "4" * 64
    decision_id = "deliberation-decision:sha256:" + "5" * 64
    proposal = {
        "record_type": "proposal", "proposal_id": proposal_id,
        "candidate_id": candidate_id, "phase": "claim_extraction",
        "subject_key": "claim:reviewed", "payload": {
            "claim_text": "The measured value increased in the sampled population."
        },
    }
    candidate_set = {
        "record_type": "candidate_set", "candidate_set_id": candidate_set_id,
        "phase": "claim_extraction", "subject_key": "claim:reviewed",
        "candidate_ids": [candidate_id], "proposal_ids": [proposal_id],
    }
    ballot = {
        "record_type": "ballot", "ballot_id": ballot_id,
        "candidate_set_id": candidate_set_id, "role": "source_verifier",
        "actor": {"id": "agent:reviewer", "independence_group": "model:a"},
        "evaluations": [{"candidate_id": candidate_id, "decision": "endorse"}],
    }
    phase_decision = {
        "record_type": "phase_decision", "decision_id": decision_id,
        "candidate_set_id": candidate_set_id, "candidate_id": candidate_id,
        "ballot_ids": [ballot_id], "decision": "approved",
        "actor": "human:reviewer",
        "rationale": "Route the candidate to the next review phase only.",
        "human_identity_authenticated": False,
        "automatic_activation": False,
    }
    report["deliberations"] = {
        "schema_version": "claimtrace.deliberation-status/1",
        "integrity": "ok", "integrity_issues": [],
        "records": [phase_decision, ballot, candidate_set, proposal],
        "panels": [{
            "candidate_set_id": candidate_set_id,
            "phase": "claim_extraction", "subject_key": "claim:reviewed",
            "status": "recommended_for_human_review",
            "recommended_candidate_id": candidate_id,
            "human_activation_required": True, "automatic_activation": False,
        }],
        "open_groups": [],
        # The view must not trust upstream flags to relax this boundary.
        "human_activation_required": False,
        "automatic_activation": True,
        "scientific_truth_established": True,
    }

    payload = view_module._build_payload(report, "layout:test")

    assert payload["schema"] == "claimtrace.view/6"
    assert payload["deliberations"]["proposals"] == [proposal]
    assert payload["deliberations"]["candidate_sets"] == [candidate_set]
    assert payload["deliberations"]["ballots"] == [ballot]
    assert payload["deliberations"]["phase_decisions"] == [phase_decision]
    assert payload["deliberations"]["panels"][0]["status"] == (
        "recommended_for_human_review"
    )
    assert payload["deliberations"]["advisory_only"] is True
    assert payload["deliberations"]["human_activation_required"] is True
    assert payload["deliberations"]["automatic_activation"] is False
    assert payload["deliberations"]["scientific_truth_established"] is False
    assert payload["summary"]["deliberation_proposals"] == 1
    assert payload["summary"]["deliberation_candidate_sets"] == 1
    assert payload["summary"]["deliberation_ballots"] == 1
    assert payload["summary"]["deliberation_phase_decisions"] == 1
    assert payload["summary"]["deliberation_approved_phase_decisions"] == 1
    assert payload["summary"]["deliberation_rejected_phase_decisions"] == 0
    assert payload["summary"]["deliberation_recommended"] == 1
    assert not any(node.get("kind") == "deliberation" for node in payload["nodes"])
    assert not any(edge.get("kind") == "deliberation" for edge in payload["edges"])
    assert "never create dependency or support edges" in payload["coverage_notice"]

    html = view_module._render_html(payload)
    assert 'id="deliberation-details"' in html
    assert "Advisory audit layer only" in html
    assert "appendDeliberationSection(\"Ballots\"" in html
    assert '"Phase decisions", deliberation.phase_decisions' in html
    assert "not authenticated identity or independence" in html


def test_view_cli_requires_explicit_output_and_reports_summary(tmp_path, capsys):
    cfg = _project(tmp_path)
    output = tmp_path / "map.html"
    code = main([
        "--config", str(cfg.config_path), "view", "--output", str(output),
    ])
    captured = capsys.readouterr()
    assert code == 0
    assert output.exists()
    assert "5 semantic nodes / 4 semantic edges / 0 run receipts" in captured.out
    assert captured.err == ""


@pytest.mark.parametrize("target", [
    "provsleuth/graph.json", "data.txt", "provsleuth/events/view.html",
    "provsleuth/assessments/view.html", "provsleuth/deliberations/view.html",
    "provsleuth/derivations/view.html",
    "provsleuth/semantics/mappings/view.html", "provsleuth/semantics/policies/view.html",
    "out.txt.manifest.json",
])
def test_view_refuses_to_overwrite_provenance_or_graph_files(tmp_path, target):
    cfg = _project(tmp_path)
    with pytest.raises(GraphError, match="refusing to overwrite"):
        render_view(cfg, tmp_path / target)


def test_view_refuses_to_overwrite_configured_verifier(tmp_path):
    cfg = _project(tmp_path)
    verifier = tmp_path / "provsleuth" / "verifiers.py"
    verifier.write_text("# project verification policy\n", encoding="utf-8")
    config = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    config["verifiers"] = "provsleuth/verifiers.py"
    cfg.config_path.write_text(json.dumps(config), encoding="utf-8")
    cfg = Config(cfg.config_path)

    with pytest.raises(GraphError, match="refusing to overwrite"):
        render_view(cfg, verifier)
