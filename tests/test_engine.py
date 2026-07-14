"""Engine tests against the checked-in demos and a temp project for log()."""
import json
import shutil
from pathlib import Path

from claimtrace import engine
from claimtrace.config import Config
from claimtrace.report import build_report

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "widget_study" / "claimtrace.config.json"
PUBLIC_DEMOS = [
    ROOT / "examples" / "penguin_study" / "claimtrace.config.json",
    ROOT / "examples" / "eegbci_study" / "claimtrace.config.json",
]


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


def test_public_science_examples_cover_the_full_claim_trajectory():
    required_types = {
        "question", "hypothesis", "prediction", "data", "code", "artifact",
        "figure", "claim", "conclusion", "reference", "doc",
    }
    for config_path in PUBLIC_DEMOS:
        nodes, edges, _concepts = engine.load_graph(Config(config_path))
        assert required_types <= {node["type"] for node in nodes.values()}
        assert not {"supports", "refutes"} & {edge["rel"] for edge in edges}
        assert any(
            nodes[edge["from"]]["type"] in {"artifact", "figure", "data"}
            and nodes[edge["to"]]["type"] == "claim"
            and edge["rel"] == "derives_from"
            for edge in edges
        )
        assert any(
            node["type"] == "claim"
            and "logic" in node
            and "logic_evidence_plan" in node
            for node in nodes.values()
        )
        assert all(node.get("path") != "research-map.html" for node in nodes.values())


def test_penguin_demo_is_a_green_strict_semantic_and_symbolic_record():
    report = build_report(Config(PUBLIC_DEMOS[0]), strict=True)

    assert report["ok"] is True
    assert report["exit_code"] == 0
    assert report["assessments"]["integrity"] == "ok"
    assert {
        (item["from"], item["to"], item["rel"])
        for item in report["assessments"]["active_relations"]
    } == {
        ("art:slopes", "claim:estimated-slopes", "supports"),
        ("art:slopes", "claim:sign-reversal", "supports"),
    }
    assert report["derivations"]["integrity"] == "ok"
    assert {
        (item["claim_id"], item["proof_state"], item["claim_level_active"])
        for item in report["derivations"]["active_proofs"]
    } == {("claim:sign-reversal", "derivable", True)}


def test_eeg_demo_clean_checkout_boundary_keeps_semantic_history_but_requires_fetch(tmp_path):
    source = PUBLIC_DEMOS[1].parent
    destination = tmp_path / "eegbci_study"

    def ignore(_directory, names):
        ignored = {".venv", "research-map.html", "__pycache__"}
        ignored.update(name for name in names
                       if name.lower().endswith((".edf", ".pyc")))
        return ignored.intersection(names)

    shutil.copytree(source, destination, ignore=ignore)
    report = build_report(Config(destination / "claimtrace.config.json"), strict=True)

    assert report["ok"] is False
    assert report["assessments"]["integrity"] == "ok"
    assert report["assessments"]["active_relations"] == [{
        "assessment_id": "assessment:sha256:f92a070189e46fa63aff6fb0e5350827a3441273ddaef3b576f696964837607b",
        "from": "art:decoding",
        "to": "claim:above-null",
        "rel": "supports",
    }]
    assert report["assessments"]["required_dependencies"][0]["status"] == "covered"
    assert report["derivations"]["integrity"] == "ok"
    assert report["derivations"]["active_proofs"] == []
    missing_nodes = {
        item["node_id"] for item in report["findings"]
        if item["code"] == "MISSING_FILE"
    }
    assert missing_nodes == {"data:r06", "data:r10", "data:r14"}
    codes = {item["code"] for item in report["findings"]}
    assert {"DERIVATION_STALE", "MISSING_CLAIM_DERIVATION"} <= codes
    assert "UNASSESSED_CLAIM_DEPENDENCY" not in codes


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
