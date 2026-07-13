"""Focused tests for the deterministic standalone research-trajectory view."""
import json
import re
import sys

import pytest

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


@pytest.mark.parametrize("target", ["claimtrace/graph.json", "data.txt", "claimtrace/events/view.html"])
def test_view_refuses_to_overwrite_provenance_or_graph_files(tmp_path, target):
    cfg = _project(tmp_path)
    with pytest.raises(GraphError, match="refusing to overwrite"):
        render_view(cfg, tmp_path / target)
