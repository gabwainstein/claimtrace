"""Engine tests against the checked-in demos and a temp project for log()."""
import json
import shutil
from pathlib import Path

import pytest

from provsleuth import engine
from provsleuth import events as events_module
from provsleuth import replay as replay_module
from provsleuth.config import Config
from provsleuth.report import build_report

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


def test_penguin_demo_integrity_and_environment_relative_strict_state():
    config = Config(PUBLIC_DEMOS[0])
    report = build_report(config, strict=True)

    problems, pending = engine.compute_check(config)
    assert problems == []
    assert pending == []
    assert report["fatal"] is None
    assert report["receipts"]["event_store_integrity"] == "ok"
    assert report["receipts"]["run_link_integrity"] == "ok"
    assert report["receipts"]["replay_integrity"] == "ok"
    assert report["assessments"]["integrity"] == "ok"
    assert report["method_assessments"]["integrity"] == "ok"
    assert report["semantics"]["integrity"] == "ok"
    assert report["derivations"]["integrity"] == "ok"

    claim_run_ids = {item["run_id"] for item in report["claim_basis"]["items"]}
    assert len(claim_run_ids) == 1
    claim_run_id = next(iter(claim_run_ids))
    records, integrity_issues = events_module.load_events(config.events_path)
    assert integrity_issues == []
    start = next(
        item for item in records
        if item["run_id"] == claim_run_id and item["type"] == "run.started"
    )
    plan = start["payload"]["plan"]
    assert plan["cwd"] == "."
    recorded_executable = plan["environment"]["executable"]
    current_executable = replay_module._executable_identity(
        plan["argv"], config.root.resolve(),
    )
    exact_executable_match = all(
        recorded_executable.get(key) == current_executable.get(key)
        for key in ("state", "resolved", "sha256")
    )
    run = next(
        item for item in report["receipts"]["runs"]
        if item["run_id"] == claim_run_id
    )
    assert len(run["replays"]) == 1
    replay_evaluation = run["replays"][0]["current_derived"]

    if exact_executable_match:
        assert report["ok"] is True
        assert report["exit_code"] == 0
        assert replay_evaluation["review_ready_current"] is True
        assert {
            item["overall"] for item in report["claim_basis"]["items"]
        } == {"ready_under_reviewed_provenance"}
    else:
        assert report["ok"] is False
        assert report["exit_code"] == 1
        assert replay_evaluation["current"] is False
        assert replay_evaluation["byte_repeatable_current"] is False
        assert replay_evaluation["review_ready_current"] is False
        assert replay_evaluation["stage_trace_repeatable_current"] is False
        assert replay_evaluation["findings"] == [{
            "code": "REPLAY_ENVIRONMENT_MISMATCH",
            "detail": "source executable identity changed",
        }]
        assert {
            (
                item["replay_state"], item["stage_checkpoint_state"],
                item["overall"], item["configured_policy_pass"],
            )
            for item in report["claim_basis"]["items"]
        } == {(
            "present_but_not_current_or_repeatable",
            "cooperative_report_complete_source_only",
            "execution_grounded_but_provenance_incomplete",
            False,
        )}
        assert all(
            item["replay_certificate_ids"] == []
            for item in report["claim_basis"]["items"]
        )

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


def test_penguin_v2_contract_remains_current_after_fresh_file_copy(tmp_path):
    source = PUBLIC_DEMOS[0].parent
    copied = tmp_path / "penguin_study"
    shutil.copytree(source, copied, copy_function=shutil.copyfile)

    report = build_report(Config(copied / "claimtrace.config.json"), strict=True)
    run_id = next(iter({item["run_id"] for item in report["claim_basis"]["items"]}))
    run = next(item for item in report["receipts"]["runs"] if item["run_id"] == run_id)

    assert run["pipeline_contract"]["schema_version"] == (
        "claimtrace.pipeline-contract-snapshot/2"
    )
    assert run["pipeline_contract_state"] == "current"
    assert all(
        finding["code"] != "REPLAY_CONTRACT_DRIFT"
        for replay in run["replays"]
        for finding in replay["current_derived"]["findings"]
    )
    assert {
        item["method_state"] for item in report["claim_basis"]["items"]
    } == {"accepted_current_conformance"}
    assert any(
        item["current_derived"]["implementation_current"]
        for item in report["method_assessments"]["items"]
        if item["review"]["state"] == "accepted"
    )


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
        "assessment_id": "assessment:sha256:b3f564577237bbd997deddd7750f7f6053b6f112f371cdf5f3ea9a2643348212",
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


def _mini_project(tmp_path, nodes, edges=()):
    (tmp_path / "provsleuth").mkdir(exist_ok=True)
    (tmp_path / "provsleuth.config.json").write_text(json.dumps(
        {"root": ".", "graph": "provsleuth/graph.json"}))
    (tmp_path / "provsleuth" / "graph.json").write_text(json.dumps(
        {"concepts": {}, "nodes": list(nodes), "edges": list(edges)}))
    return Config(tmp_path / "provsleuth.config.json")


def test_node_path_outside_project_root_is_refused_and_reported(tmp_path):
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()
    c = _mini_project(project, [
        {"id": "data:abs", "type": "data", "status": "current", "path": str(outside)},
        {"id": "data:up", "type": "data", "status": "current", "path": "../outside-secret.txt"},
    ])

    # The resolver never hands back a path the project does not own.
    for escaping in (str(outside), "../outside-secret.txt", "a/../../outside-secret.txt"):
        assert c.within_root(escaping) is False
        with pytest.raises(SystemExit):
            c.resolve(escaping)
    assert c.within_root("results/fit.json") is True

    # Checking reports the bad paths instead of aborting, and never reads them.
    problems, _pending = engine.compute_check(c)
    flagged = {nid for code, nid, _detail in problems if code == "INVALID_NODE_PATH"}
    assert flagged == {"data:abs", "data:up"}
    assert not any(code == "MISSING_FILE" for code, _nid, _d in problems)


def test_live_node_may_not_depend_on_planned_work(tmp_path):
    c = _mini_project(tmp_path, [
        {"id": "data:raw", "type": "data", "status": "planned"},
        {"id": "art:fit", "type": "artifact", "status": "confirmed"},
    ], [{"from": "data:raw", "to": "art:fit", "rel": "produces"}])
    problems, _pending = engine.compute_check(c)
    assert [(code, nid) for code, nid, _d in problems
            if code == "DEPENDS_ON_PLANNED"] == [("DEPENDS_ON_PLANNED", "art:fit")]


def test_planned_node_may_depend_on_planned_work(tmp_path):
    c = _mini_project(tmp_path, [
        {"id": "data:raw", "type": "data", "status": "planned"},
        {"id": "art:fit", "type": "artifact", "status": "planned"},
    ], [{"from": "data:raw", "to": "art:fit", "rel": "produces"}])
    problems, _pending = engine.compute_check(c)
    assert not any(code == "DEPENDS_ON_PLANNED" for code, _nid, _d in problems)


def test_log_appends_node(tmp_path):
    proj = tmp_path
    (proj / "provsleuth").mkdir()
    (proj / "provsleuth.config.json").write_text(json.dumps({"root": ".", "graph": "provsleuth/graph.json"}))
    (proj / "provsleuth" / "graph.json").write_text(json.dumps(
        {"concepts": {}, "nodes": [{"id": "data:x", "type": "data", "status": "current"}], "edges": []}))
    c = Config(proj / "provsleuth.config.json")
    ok, msg = engine.log_entry(c, {
        "node": {"id": "exp:try1", "type": "experiment", "status": "null", "value": "no effect"},
        "edges": [{"from": "exp:try1", "to": "data:x", "rel": "related"}]})
    assert ok
    nodes, edges, _ = engine.load_graph(c)
    assert "exp:try1" in nodes and len(edges) == 1
