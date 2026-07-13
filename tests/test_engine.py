"""Engine tests against the synthetic widget-study demo + a temp project for log()."""
import json
from pathlib import Path

from claimtrace import engine
from claimtrace.config import Config

DEMO = Path(__file__).resolve().parents[1] / "examples" / "widget_study" / "claimtrace.config.json"


def cfg():
    return Config(DEMO)


def test_load_graph_and_concepts():
    nodes, edges, concepts = engine.load_graph(cfg())
    assert "data:raw" in nodes
    assert concepts["dataset_version"]["canonical"] == "v2"


def test_downstream_reaches_claim_and_figure():
    res = engine.downstream(cfg(), "data:raw")
    ids = {nb for nb, _, _ in res}
    assert {"art:clean", "art:fit", "fig:fit", "claim:slope"} <= ids


def test_upstream_of_claim_reaches_raw():
    res = engine.upstream(cfg(), "claim:slope")
    ids = {nb for nb, _, _ in res}
    assert "art:fit" in ids and "data:raw" in ids


def test_impact_flags_every_v2_node():
    items, canon = engine.impact(cfg(), "dataset_version", "v3")
    ids = {nid for nid, _, _ in items}
    assert canon == "v2"
    assert {"data:raw", "art:clean", "art:fit", "fig:fit", "claim:slope"} <= ids


def test_annotation_edge_not_a_dependency():
    # exp:loglog --tried_before--> claim:slope must NOT make claim:slope a dependent of exp:loglog
    res = engine.downstream(cfg(), "exp:loglog")
    assert res == []  # tried_before is an annotation, skipped by _adj


def test_check_has_no_silent_drift():
    problems, _ = engine.compute_check(cfg())
    kinds = {k for k, _, _ in problems}
    assert "SILENT_DRIFT" not in kinds  # demo graph is internally consistent on v2


def test_log_appends_node(tmp_path):
    proj = tmp_path
    (proj / "claimtrace").mkdir()
    (proj / "claimtrace.config.json").write_text(json.dumps({"root": ".", "graph": "claimtrace/graph.json"}))
    (proj / "claimtrace" / "graph.json").write_text(json.dumps(
        {"concepts": {}, "nodes": [{"id": "data:x", "type": "data", "status": "current"}], "edges": []}))
    c = Config(proj / "claimtrace.config.json")
    ok, msg = engine.log_entry(c, {
        "node": {"id": "exp:try1", "type": "experiment", "status": "null", "value": "no effect"},
        "edges": [{"from": "exp:try1", "to": "data:x", "rel": "related"}]})
    assert ok
    nodes, edges, _ = engine.load_graph(c)
    assert "exp:try1" in nodes and len(edges) == 1
