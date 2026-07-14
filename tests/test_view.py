"""Focused tests for the deterministic standalone research-trajectory view."""
import json
import re
import sys

import pytest

from claimtrace.assessment import append_assessment, create_assessment, transition_review
from claimtrace.config import Config
from claimtrace.cli import main
from claimtrace.engine import GraphError
from claimtrace.events import run_command
from claimtrace.logic import (EVIDENCE_PLAN_SCHEMA, append_derivation,
                              create_derivation, load_rule_pack, load_vocabulary)
from claimtrace.view import render_view


def _project(tmp_path, *, malicious_value=None):
    trace = tmp_path / "claimtrace"
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
    config_path = tmp_path / "claimtrace.config.json"
    config_path.write_text(json.dumps({
        "root": ".",
        "graph": "claimtrace/graph.json",
        "events": "claimtrace/events",
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
        r'<script id="claimtrace-data" type="application/json">(.*?)</script>',
        html,
        flags=re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


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
    logic_dir = cfg.base / "claimtrace" / "logic"
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
        "derivations": "claimtrace/derivations",
        "vocabularies": ["claimtrace/logic/vocabulary.json"],
        "rule_packs": ["claimtrace/logic/rules.json"],
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
    assert 'id="assessment-select"' in html
    assert 'role="region"' in html and 'aria-labelledby="detail-title"' in html
    assert 'role: "button"' not in html
    assert 'group.addEventListener("click"' in html
    assert "ct-ancestor" in html and "ct-descendant" in html
    assert "ct-type-claim" in html and "ct-status-stale" in html


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
    assert binding["source"] == receipt["key"]
    assert binding["target"] == "graph:artifact:result"
    assert binding["relation"] == "binds declared output"
    assert binding["declaration_comparison"] == "declarations_agree"
    assert receipt["layer"] < nodes[binding["target"]]["layer"]
    assert "not observed reads" in receipt["coverage"]
    assert "do not prove write causation" in payload["coverage_notice"]
    assert nodes["graph:artifact:result"]["run_ids"] == [result["run_id"]]


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

    assert payload["schema"] == "claimtrace.view/3"
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


def test_symbolic_derivation_is_one_nontraversable_composite_proof_node(tmp_path):
    cfg = _project(tmp_path)
    cfg, document = _record_symbolic_proof(cfg)
    output = tmp_path / "trajectory.html"
    render_view(cfg, output)
    html = output.read_text(encoding="utf-8")
    payload = _payload(html)
    nodes = {node["key"]: node for node in payload["nodes"]}

    assert payload["schema"] == "claimtrace.view/3"
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
    vocabulary = load_vocabulary(cfg.base / "claimtrace" / "logic" / "vocabulary.json")
    rules = load_rule_pack(
        cfg.base / "claimtrace" / "logic" / "rules.json", vocabulary,
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
    vocabulary = load_vocabulary(cfg.base / "claimtrace" / "logic" / "vocabulary.json")
    rules = load_rule_pack(
        cfg.base / "claimtrace" / "logic" / "rules.json", vocabulary,
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
    "claimtrace/graph.json", "data.txt", "claimtrace/events/view.html",
    "claimtrace/assessments/view.html", "claimtrace/derivations/view.html",
    "out.txt.manifest.json",
])
def test_view_refuses_to_overwrite_provenance_or_graph_files(tmp_path, target):
    cfg = _project(tmp_path)
    with pytest.raises(GraphError, match="refusing to overwrite"):
        render_view(cfg, tmp_path / target)


def test_view_refuses_to_overwrite_configured_verifier(tmp_path):
    cfg = _project(tmp_path)
    verifier = tmp_path / "claimtrace" / "verifiers.py"
    verifier.write_text("# project verification policy\n", encoding="utf-8")
    config = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    config["verifiers"] = "claimtrace/verifiers.py"
    cfg.config_path.write_text(json.dumps(config), encoding="utf-8")
    cfg = Config(cfg.config_path)

    with pytest.raises(GraphError, match="refusing to overwrite"):
        render_view(cfg, verifier)
