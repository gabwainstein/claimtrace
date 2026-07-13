"""Deterministic standalone research-trajectory visualization.

The graph remains the semantic declaration of lineage. Run receipts are rendered as a
separate mechanical layer and are linked only to outputs that the report explicitly binds.
No receipt is presented as proof of observed reads or write causation.
"""
from __future__ import annotations

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
            return (
                "Partial: inputs are declared, not observed reads; "
                "pre/post changes do not prove write causation."
            )
    return (
        "Partial: inputs are declared, not observed reads; "
        "pre/post changes do not prove write causation."
    )


def _build_payload(report):
    graph = report.get("graph") or {}
    raw_nodes = {item["id"]: item for item in graph.get("nodes", [])}
    order = _semantic_order(graph)
    semantic_layers = _semantic_layers(graph, order)
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

    if any(
        semantic_layers.get(node_id) == 0
        for bindings in run_bindings.values()
        for node_id in bindings
    ):
        semantic_layers = {key: value + 1 for key, value in semantic_layers.items()}

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
            "findings": sorted(
                findings_by_node[node_id],
                key=lambda item: (
                    str(item.get("severity")), str(item.get("code")),
                    str(item.get("detail")),
                ),
            ),
            "run_ids": sorted(node_runs[node_id]),
            "coverage": None,
            "layer": semantic_layers.get(node_id, 0),
            "_rank": order_rank[node_id],
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
        visual_nodes.append({
            "key": "receipt:" + run_id,
            "node_id": run_id,
            "kind": "run",
            "type": "run receipt",
            "status": str(run.get("outcome") or "incomplete"),
            "label": str(run.get("name") or run_id),
            "path": run.get("cwd"),
            "value": None,
            "note": None,
            "date": run.get("finished_at") or run.get("started_at"),
            "script": None,
            "backbone": None,
            "findings": [],
            "run_ids": [],
            "coverage": _coverage_label(run),
            "declared_inputs": list(run.get("declared_inputs", [])),
            "declared_outputs": list(run.get("declared_outputs", [])),
            "output_transitions": output_transitions,
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
            1 if item["kind"] == "run" else 0,
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
        if dependency:
            source, target = _dependency_direction(edge)
        edges.append({
            "key": "semantic:%06d" % index,
            "source": "graph:" + source,
            "target": "graph:" + target,
            "relation": relation,
            "kind": "dependency" if dependency else "annotation",
            "traversable": dependency,
        })

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
            if node_id not in raw_nodes or node_id in seen:
                continue
            seen.add(node_id)
            binding_kind = binding.get("binding_kind", "output_path")
            edges.append({
                "key": "receipt:%06d" % receipt_index,
                "source": "receipt:" + run_id,
                "target": "graph:" + node_id,
                "relation": ("explicit semantic run reference"
                             if binding_kind == "explicit_run_reference"
                             else "binds declared output"),
                "kind": "receipt",
                "traversable": True,
                "binding_kind": binding_kind,
                "declaration_comparison": binding.get("declaration_comparison"),
            })
            receipt_index += 1

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
        "schema": "claimtrace.view/1",
        "scope": report.get("scope") or {},
        "summary": {
            "semantic_nodes": len(raw_nodes),
            "semantic_edges": sum(1 for edge in edges if edge["kind"] != "receipt"),
            "runs": len(runs),
            "findings": len(report.get("findings", [])),
            "errors": summary.get("errors", 0),
            "warnings": summary.get("warnings", 0),
            "pending": summary.get("pending", 0),
        },
        "nodes": visual_nodes,
        "edges": edges,
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
            "Semantic edges are declared lineage. Run receipts are mechanical records. "
            "Declared inputs are not observed reads, and pre/post changes do not prove "
            "write causation."
        ),
    }


_HTML_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>claimtrace research trajectory</title>
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
    --other: #303846;
  }
}
* { box-sizing: border-box; }
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
.controls {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: end;
  margin-bottom: 12px;
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
.workspace {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 310px;
  gap: 16px;
  align-items: start;
}
.graph-scroll {
  overflow: auto;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
}
#trajectory {
  display: block;
  min-width: 100%;
}
.details {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px;
}
.details dl { margin: 0; display: grid; grid-template-columns: 88px 1fr; gap: 7px 9px; }
.details dt { color: var(--muted); }
.details dd { margin: 0; overflow-wrap: anywhere; }
.detail-note { color: var(--muted); line-height: 1.4; }
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
.swatch.retired { background: var(--other); border: 2px dashed var(--danger); }
.swatch.data { background: var(--data); }
.swatch.code { background: var(--code); }
.swatch.artifact { background: var(--artifact); }
.swatch.claim { background: var(--claim); }
.ct-layer-label { fill: var(--muted); font-size: 12px; font-weight: 600; }
.ct-edge { fill: none; stroke: var(--edge); stroke-width: 1.5; opacity: .7; }
.ct-edge-dependency { stroke: var(--dependency); }
.ct-edge-annotation { stroke: var(--annotation); stroke-dasharray: 3 5; }
.ct-edge-receipt { stroke: var(--receipt); stroke-dasharray: 8 5; stroke-width: 2; }
.ct-edge-label { fill: var(--muted); font-size: 10px; paint-order: stroke; stroke: var(--surface); stroke-width: 4px; }
.ct-node { cursor: pointer; transition: opacity .12s ease; }
.ct-node rect { stroke: var(--border); stroke-width: 1.5; }
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
.ct-type-other rect { fill: var(--other); }
.ct-status-confirmed rect { stroke: var(--downstream); stroke-width: 2.5; }
.ct-status-stale rect { stroke: var(--warning); stroke-width: 2.5; stroke-dasharray: 7 4; }
.ct-status-null rect { stroke: var(--annotation); stroke-width: 2.5; stroke-dasharray: 2 4; }
.ct-status-dead-end rect, .ct-status-retracted rect,
.ct-status-deprecated rect, .ct-status-superseded rect {
  stroke: var(--danger); stroke-width: 2.5; stroke-dasharray: 7 4;
}
.ct-dim { opacity: .13; }
.ct-selected { opacity: 1 !important; }
.ct-selected rect { stroke: var(--selected) !important; stroke-width: 4px !important; }
.ct-ancestor { opacity: 1 !important; }
.ct-ancestor rect { stroke: var(--upstream) !important; stroke-width: 3px !important; }
.ct-descendant { opacity: 1 !important; }
.ct-descendant rect { stroke: var(--downstream) !important; stroke-width: 3px !important; }
.ct-edge.ct-related { opacity: 1; stroke-width: 2.7; }
.ct-edge.ct-upstream { stroke: var(--upstream); }
.ct-edge.ct-downstream { stroke: var(--downstream); }
.ct-layer-muted { opacity: .10; }
@media (max-width: 900px) {
  main { padding: 12px; }
  .workspace { grid-template-columns: 1fr; }
  .details { order: -1; }
}
</style>
</head>
<body>
<main>
  <h1>Research trajectory</h1>
  <p id="summary" class="summary"></p>
  <p id="coverage" class="scope"></p>
  <div class="controls">
    <label for="layer-select">Layer
      <select id="layer-select"><option value="">All layers</option></select>
    </label>
    <label for="focus-select">Focus
      <select id="focus-select"><option value="">Choose a node or run</option></select>
    </label>
  </div>
  <div class="workspace">
    <div class="graph-scroll">
      <svg id="trajectory" role="img" aria-labelledby="trajectory-title trajectory-description">
        <title id="trajectory-title">Claimtrace research trajectory</title>
        <desc id="trajectory-description">A deterministic layered graph of declared semantic lineage and partial mechanical run receipts.</desc>
        <defs>
          <marker id="arrow-dependency" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto" markerUnits="strokeWidth">
            <path d="M0,0 L8,4 L0,8 z" fill="var(--dependency)"></path>
          </marker>
          <marker id="arrow-annotation" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto" markerUnits="strokeWidth">
            <path d="M0,0 L8,4 L0,8 z" fill="var(--annotation)"></path>
          </marker>
          <marker id="arrow-receipt" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto" markerUnits="strokeWidth">
            <path d="M0,0 L8,4 L0,8 z" fill="var(--receipt)"></path>
          </marker>
        </defs>
        <g id="layer-labels"></g>
        <g id="edge-layer"></g>
        <g id="node-layer"></g>
      </svg>
    </div>
    <aside class="details" aria-live="polite">
      <h2 id="detail-title">Trajectory overview</h2>
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
    <span class="legend-item"><span class="swatch retired"></span>stale or retired status</span>
  </div>
</main>
"""


_HTML_SCRIPT = """
<script id="claimtrace-data" type="application/json">__CLAIMTRACE_DATA__</script>
<script>
(function () {
  "use strict";
  const data = JSON.parse(document.getElementById("claimtrace-data").textContent);
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.getElementById("trajectory");
  const edgeLayer = document.getElementById("edge-layer");
  const nodeLayer = document.getElementById("node-layer");
  const labelLayer = document.getElementById("layer-labels");
  const layerSelect = document.getElementById("layer-select");
  const focusSelect = document.getElementById("focus-select");
  const detailTitle = document.getElementById("detail-title");
  const detailContent = document.getElementById("detail-content");
  const nodes = new Map(data.nodes.map(function (node) { return [node.key, node]; }));
  const nodeElements = new Map();
  const edgeElements = new Map();
  const outgoing = new Map();
  const incoming = new Map();
  let selected = null;

  function svgElement(tag, attributes, text) {
    const element = document.createElementNS(NS, tag);
    Object.keys(attributes || {}).forEach(function (name) {
      element.setAttribute(name, String(attributes[name]));
    });
    if (text !== undefined) element.textContent = text;
    return element;
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
      "run-receipt"
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

  data.edges.forEach(function (edge) {
    if (edge.traversable) {
      addConnection(outgoing, edge.source, {node: edge.target, edge: edge.key});
      addConnection(incoming, edge.target, {node: edge.source, edge: edge.key});
    }
  });

  svg.setAttribute("viewBox", "0 0 " + data.width + " " + data.height);
  svg.setAttribute("width", data.width);
  svg.setAttribute("height", data.height);
  document.getElementById("summary").textContent =
    data.summary.semantic_nodes + " semantic nodes · " +
    data.summary.semantic_edges + " semantic edges · " +
    data.summary.runs + " run receipts · " +
    data.summary.errors + " errors · " + data.summary.warnings + " warnings";
  document.getElementById("coverage").textContent = data.coverage_notice;

  data.layers.forEach(function (layer) {
    const option = document.createElement("option");
    option.value = String(layer);
    option.textContent = "Layer " + layer;
    layerSelect.appendChild(option);
    const x = data.nodes.filter(function (node) { return node.layer === layer; })[0].x;
    labelLayer.appendChild(svgElement("text", {
      x: x + 112, y: 26, "text-anchor": "middle", "class": "ct-layer-label"
    }, "Layer " + layer));
  });

  data.edges.forEach(function (edge, index) {
    const source = nodes.get(edge.source);
    const target = nodes.get(edge.target);
    if (!source || !target) return;
    let x1, y1, x2, y2, pathData, labelX, labelY;
    if (target.x > source.x) {
      x1 = source.x + source.width;
      y1 = source.y + source.height / 2;
      x2 = target.x;
      y2 = target.y + target.height / 2;
      const middle = (x1 + x2) / 2;
      pathData = "M" + x1 + "," + y1 + " C" + middle + "," + y1 + " " + middle + "," + y2 + " " + x2 + "," + y2;
      labelX = middle;
      labelY = (y1 + y2) / 2 - 4;
    } else {
      x1 = source.x + source.width / 2;
      y1 = source.y + source.height;
      x2 = target.x + target.width / 2;
      y2 = target.y + target.height;
      const bend = Math.max(y1, y2) + 28 + (index % 5) * 8;
      pathData = "M" + x1 + "," + y1 + " C" + x1 + "," + bend + " " + x2 + "," + bend + " " + x2 + "," + y2;
      labelX = (x1 + x2) / 2;
      labelY = bend - 4;
    }
    const path = svgElement("path", {
      d: pathData,
      "class": "ct-edge ct-edge-" + edge.kind,
      "marker-end": "url(#arrow-" + edge.kind + ")"
    });
    path.appendChild(svgElement("title", {}, edge.relation));
    edgeLayer.appendChild(path);
    edgeLayer.appendChild(svgElement("text", {
      x: labelX, y: labelY, "text-anchor": "middle",
      "class": "ct-edge-label"
    }, edge.kind === "receipt" ? "receipt" : edge.relation));
    edgeElements.set(edge.key, path);
  });

  function selectNode(key) {
    selected = key || null;
    focusSelect.value = selected || "";
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
      role: "button",
      "aria-label": node.kind === "run"
        ? "Run receipt " + node.label + ", " + node.status + ", partial coverage"
        : node.type + " " + node.label + ", status " + node.status
    });
    group.appendChild(svgElement("rect", {
      width: node.width, height: node.height, rx: node.kind === "run" ? 18 : 7
    }));
    group.appendChild(svgElement("text", {
      x: 13, y: 23, "class": "ct-title"
    }, short(node.label, 30)));
    const issueText = node.findings && node.findings.length
      ? " · ! " + node.findings.length : "";
    group.appendChild(svgElement("text", {
      x: 13, y: 44, "class": "ct-meta"
    }, short(node.type + " · " + node.status + issueText, 36)));
    const finalLine = node.kind === "run"
      ? "partial lineage · " + (node.output_transitions || []).filter(function (item) { return item.produced; }).length + " produced"
      : (node.path || node.value || "");
    group.appendChild(svgElement("text", {
      x: 13, y: 64, "class": "ct-path"
    }, short(finalLine, 36)));
    group.appendChild(svgElement("title", {}, node.kind === "run"
      ? node.label + " — " + node.coverage
      : node.label + (node.value ? " — " + node.value : "")));
    group.addEventListener("click", function () { selectNode(node.key); });
    nodeLayer.appendChild(group);
    nodeElements.set(node.key, group);
  });

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
    const selectedLayer = layerSelect.value === "" ? null : Number(layerSelect.value);
    nodeElements.forEach(function (element, key) {
      const node = nodes.get(key);
      element.classList.toggle("ct-dim", Boolean(selected) &&
        key !== selected && !upstream.nodes.has(key) && !downstream.nodes.has(key));
      element.classList.toggle("ct-selected", key === selected);
      element.classList.toggle("ct-ancestor", upstream.nodes.has(key));
      element.classList.toggle("ct-descendant", downstream.nodes.has(key));
      element.classList.toggle("ct-layer-muted", selectedLayer !== null && node.layer !== selectedLayer);
    });
    edgeElements.forEach(function (element, key) {
      element.classList.toggle("ct-related", upstream.edges.has(key) || downstream.edges.has(key));
      element.classList.toggle("ct-upstream", upstream.edges.has(key));
      element.classList.toggle("ct-downstream", downstream.edges.has(key));
      element.classList.toggle("ct-dim", Boolean(selected) &&
        !upstream.edges.has(key) && !downstream.edges.has(key));
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

  function updateDetails() {
    detailContent.replaceChildren();
    if (!selected || !nodes.has(selected)) {
      detailTitle.textContent = "Trajectory overview";
      const note = document.createElement("p");
      note.className = "detail-note";
      note.textContent = "Choose a focus or click a node. Upstream dependencies and downstream consequences are highlighted without treating annotation edges as dependencies.";
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
    detailTitle.textContent = node.label;
    const list = document.createElement("dl");
    detailContent.appendChild(list);
    appendDetail(list, "Kind", node.kind === "run" ? "mechanical run receipt" : node.type);
    appendDetail(list, "Status", node.status);
    appendDetail(list, "Path", node.path);
    appendDetail(list, "Value", node.value);
    appendDetail(list, "Backbone", typeof node.backbone === "object" ? JSON.stringify(node.backbone) : node.backbone);
    appendDetail(list, "Date", node.date);
    if (node.kind === "semantic") {
      appendDetail(list, "Bound runs", node.run_ids);
      appendDetail(list, "Findings", (node.findings || []).map(function (item) {
        return item.severity + ": " + item.code + " — " + item.detail;
      }));
    } else {
      appendDetail(list, "Exit code", node.returncode);
      appendDetail(list, "Inputs", node.declared_inputs);
      appendDetail(list, "Outputs", node.output_transitions.map(function (item) {
        return item.path + " (" + item.transition + (item.produced ? ", produced" : "") + ")";
      }));
      appendDetail(list, "Bindings", node.bindings.map(function (item) {
        const basis = item.binding_kind === "explicit_run_reference"
          ? "explicit run reference"
          : (item.declaration_comparison || "not compared");
        return item.node_id + " (" + basis + ")";
      }));
      appendDetail(list, "Coverage", node.coverage);
    }
  }

  layerSelect.addEventListener("change", updateHighlights);
  focusSelect.addEventListener("change", function () { selectNode(focusSelect.value); });
  updateDetails();
})();
</script>
</body>
</html>
"""


def _render_html(payload):
    return _HTML_HEAD + _HTML_SCRIPT.replace(
        "__CLAIMTRACE_DATA__", _json_for_html(payload)
    )


def render_view(cfg, output_path):
    """Write a deterministic standalone HTML view and return a compact CLI summary."""
    report = build_report(cfg, strict=False)
    payload = _build_payload(report)
    html = _render_html(payload)
    destination = Path(output_path).expanduser().resolve()
    protected = {cfg.config_path.resolve(), cfg.graph_path.resolve()}
    for node in (report.get("graph") or {}).get("nodes", []):
        if node.get("path"):
            protected.add(cfg.resolve(node["path"]).resolve(strict=False))
    try:
        inside_events = destination.is_relative_to(cfg.events_path.resolve())
    except AttributeError:  # Python 3.9
        try:
            destination.relative_to(cfg.events_path.resolve())
            inside_events = True
        except ValueError:
            inside_events = False
    if destination in protected or inside_events:
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
