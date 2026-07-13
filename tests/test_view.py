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

    assert payload["schema"] == "claimtrace.view/2"
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
    "claimtrace/assessments/view.html",
])
def test_view_refuses_to_overwrite_provenance_or_graph_files(tmp_path, target):
    cfg = _project(tmp_path)
    with pytest.raises(GraphError, match="refusing to overwrite"):
        render_view(cfg, tmp_path / target)
