"""Tests for the Phase-0 hardening: structural integrity, graceful errors, and lint."""
import json
import multiprocessing as mp
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from claimtrace import engine
from claimtrace.cli import main
from claimtrace.config import Config
from claimtrace.snapshot import snapshot


def _process_log(config_path, node_id, start, results):
    cfg = Config(config_path)
    start.wait()
    results.put(engine.log_entry(cfg, {
        "node": {"id": node_id, "type": "experiment", "status": "null"},
        "edges": [],
    }))


def _project(tmp_path, graph, config=None):
    (tmp_path / "claimtrace").mkdir()
    (tmp_path / "claimtrace.config.json").write_text(
        json.dumps(config or {"root": ".", "graph": "claimtrace/graph.json"}))
    (tmp_path / "claimtrace" / "graph.json").write_text(
        graph if isinstance(graph, str) else json.dumps(graph))
    return Config(tmp_path / "claimtrace.config.json")


def test_packaged_skill_installs_both_layouts_without_silent_overwrite(tmp_path, capsys):
    repository = Path(__file__).resolve().parents[1]
    canonical = repository / "src" / "claimtrace" / "templates" / "claimtrace-log" / "SKILL.md"
    agents_source = repository / ".agents" / "skills" / "claimtrace-log" / "SKILL.md"
    claude_source = repository / ".claude" / "skills" / "claimtrace-log" / "SKILL.md"
    expected = canonical.read_text(encoding="utf-8")
    assert agents_source.read_text(encoding="utf-8") == expected
    assert claude_source.read_text(encoding="utf-8") == expected
    assert "do not add a direct `supports` edge" in expected
    assert "assess <proposal.json> --actor <agent-id> --json" in expected
    assert "Never provide `mechanical_snapshot`, `derived`" in expected
    assert '`provenance.agent` is required' in expected
    assert 'never use the literal string `"not stated"`' in expected
    assert '"rel": "supports"' not in expected

    assert main(["install-skill", "--dir", str(tmp_path)]) == 0
    agents = tmp_path / ".agents" / "skills" / "claimtrace-log" / "SKILL.md"
    claude = tmp_path / ".claude" / "skills" / "claimtrace-log" / "SKILL.md"
    assert agents.read_text(encoding="utf-8") == expected
    assert claude.read_text(encoding="utf-8") == expected
    assert "installed" in capsys.readouterr().out

    agents.write_text("project-specific skill\n", encoding="utf-8")
    assert main([
        "install-skill", "--dir", str(tmp_path), "--target", "agents",
    ]) == 1
    assert agents.read_text(encoding="utf-8") == "project-specific skill\n"
    assert "refusing to overwrite" in capsys.readouterr().err

    assert main([
        "install-skill", "--dir", str(tmp_path), "--target", "agents", "--force",
    ]) == 0
    assert agents.read_text(encoding="utf-8") == expected


def test_logic_config_is_project_scoped_and_requires_explicit_external_opt_in(tmp_path):
    graph = {"schema_version": "1.0", "nodes": [], "edges": [], "concepts": {}}
    cfg = _project(tmp_path, graph, config={
        "root": ".",
        "graph": "claimtrace/graph.json",
        "logic": {
            "derivations": "claimtrace/derivations",
            "vocabularies": ["claimtrace/logic/vocabulary.json"],
            "rule_packs": ["claimtrace/logic/rules.json"],
            "require_derivations": True,
        },
    })
    assert cfg.derivations_path == (tmp_path / "claimtrace" / "derivations").resolve()
    assert cfg.logic_vocabulary_paths == [
        (tmp_path / "claimtrace" / "logic" / "vocabulary.json").resolve()
    ]
    assert cfg.require_derivations is True

    config = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    config["logic"]["vocabularies"] = ["../external-vocabulary.json"]
    cfg.config_path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(SystemExit, match="logic path escapes"):
        Config(cfg.config_path)

    config["logic"]["allow_external_packs"] = True
    cfg.config_path.write_text(json.dumps(config), encoding="utf-8")
    allowed = Config(cfg.config_path)
    assert allowed.logic_vocabulary_paths == [
        (tmp_path.parent / "external-vocabulary.json").resolve()
    ]

    config["logic"]["vocabularies"] = [
        "claimtrace/logic/vocabulary.json", "claimtrace/logic/vocabulary.json",
    ]
    cfg.config_path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(SystemExit, match="duplicate paths"):
        Config(cfg.config_path)


# --------------------------------------------------------------------------- structural integrity

def test_dangling_edge_is_caught(tmp_path):
    cfg = _project(tmp_path, {
        "nodes": [{"id": "a", "type": "data", "status": "current"}],
        "edges": [{"from": "a", "to": "ghost", "rel": "produces"}]})
    kinds = {k for k, _, _ in engine.structural_issues(cfg)}
    assert "DANGLING_EDGE" in kinds


def test_duplicate_id_is_caught(tmp_path):
    cfg = _project(tmp_path, {
        "nodes": [{"id": "a", "type": "data", "status": "current"},
                  {"id": "a", "type": "artifact", "status": "current"}],
        "edges": []})
    issues = engine.structural_issues(cfg)
    assert any(k == "DUPLICATE_ID" and nid == "a" for k, nid, _ in issues)


def test_cycle_is_detected(tmp_path):
    # a -> b -> c -> a  (all `produces`, so each depends on the previous)
    cfg = _project(tmp_path, {
        "nodes": [{"id": x, "type": "artifact", "status": "current"} for x in ("a", "b", "c")],
        "edges": [{"from": "a", "to": "b", "rel": "produces"},
                  {"from": "b", "to": "c", "rel": "produces"},
                  {"from": "c", "to": "a", "rel": "produces"}]})
    kinds = {k for k, _, _ in engine.structural_issues(cfg)}
    assert "CYCLE" in kinds


def test_self_edge_is_caught(tmp_path):
    cfg = _project(tmp_path, {
        "nodes": [{"id": "a", "type": "data", "status": "current"}],
        "edges": [{"from": "a", "to": "a", "rel": "produces"}]})
    kinds = {k for k, _, _ in engine.structural_issues(cfg)}
    assert "SELF_EDGE" in kinds


def test_dag_has_no_cycle(tmp_path):
    cfg = _project(tmp_path, {
        "nodes": [{"id": x, "type": "artifact", "status": "current"} for x in ("a", "b", "c")],
        "edges": [{"from": "a", "to": "b", "rel": "produces"},
                  {"from": "b", "to": "c", "rel": "produces"}]})
    assert engine.structural_issues(cfg) == []


def test_research_trajectory_vocabulary_is_standard(tmp_path):
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "nodes": [
            {"id": "q", "type": "question", "status": "current"},
            {"id": "h", "type": "hypothesis", "status": "current"},
            {"id": "p", "type": "prediction", "status": "current"},
            {"id": "e", "type": "experiment", "status": "confirmed"},
            {"id": "c", "type": "conclusion", "status": "confirmed"},
        ],
        "edges": [
            {"from": "q", "to": "h", "rel": "motivates"},
            {"from": "h", "to": "p", "rel": "predicts"},
            {"from": "p", "to": "e", "rel": "tested_by"},
            {"from": "e", "to": "c", "rel": "concludes"},
        ],
    })
    kinds = {kind for kind, _node, _detail in engine.lint_issues(cfg)}
    assert "UNKNOWN_TYPE" not in kinds
    assert "UNKNOWN_REL" not in kinds


def test_structural_issues_surface_in_check(tmp_path):
    cfg = _project(tmp_path, {
        "nodes": [{"id": "a", "type": "data", "status": "current"}],
        "edges": [{"from": "a", "to": "ghost", "rel": "produces"}]})
    problems, _ = engine.compute_check(cfg)
    assert any(k == "DANGLING_EDGE" for k, _, _ in problems)


# --------------------------------------------------------------------------- graceful errors

def test_malformed_json_raises_graph_error(tmp_path):
    cfg = _project(tmp_path, "{ this is not valid json ")
    with pytest.raises(engine.GraphError):
        engine.load_graph(cfg)


@pytest.mark.parametrize("graph", [
    '{"nodes": [], "nodes": [], "edges": []}',
    '{"nodes": [{"id": "x", "value": NaN}], "edges": []}',
])
def test_noncanonical_json_is_rejected(tmp_path, graph):
    cfg = _project(tmp_path, graph)
    with pytest.raises(engine.GraphError):
        engine.load_graph(cfg)


def test_missing_nodes_key_raises_graph_error(tmp_path):
    cfg = _project(tmp_path, {"edges": []})
    with pytest.raises(engine.GraphError):
        engine.load_graph(cfg)


def test_node_without_id_raises_graph_error(tmp_path):
    cfg = _project(tmp_path, {"nodes": [{"type": "data"}], "edges": []})
    with pytest.raises(engine.GraphError):
        engine.load_graph(cfg)


@pytest.mark.parametrize("node", [
    {"id": [], "type": "data"},
    {"id": "x", "type": "data", "path": ["not", "a", "path"]},
])
def test_invalid_node_field_types_raise_graph_error(tmp_path, node):
    cfg = _project(tmp_path, {"nodes": [node], "edges": []})
    with pytest.raises(engine.GraphError):
        engine.load_graph(cfg)


def test_non_string_edge_fields_are_malformed_not_a_traceback(tmp_path):
    cfg = _project(tmp_path, {
        "nodes": [{"id": "a", "type": "data", "status": "current"}],
        "edges": [{"from": ["a"], "to": "a", "rel": "produces"}],
    })
    issues = engine.structural_issues(cfg)
    assert any(kind == "MALFORMED_EDGE" for kind, _node, _detail in issues)
    problems, _pending = engine.compute_check(cfg)
    assert any(kind == "MALFORMED_EDGE" for kind, _node, _detail in problems)


# --------------------------------------------------------------------------- lint

def test_lint_flags_unknown_type_and_missing_schema_version(tmp_path):
    cfg = _project(tmp_path, {
        "nodes": [{"id": "a", "type": "widget", "status": "current"}],
        "edges": []})
    kinds = {k for k, _, _ in engine.lint_issues(cfg)}
    assert "UNKNOWN_TYPE" in kinds
    assert "NO_SCHEMA_VERSION" in kinds


def test_lint_flags_unknown_rel(tmp_path):
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "nodes": [{"id": "a", "type": "data", "status": "current"},
                  {"id": "b", "type": "artifact", "status": "current"}],
        "edges": [{"from": "a", "to": "b", "rel": "frobnicates"}]})
    kinds = {k for k, _, _ in engine.lint_issues(cfg)}
    assert "UNKNOWN_REL" in kinds


@pytest.mark.parametrize("status", ["null", "dead_end", "retracted"])
def test_lint_blocks_non_supporting_status_from_supports_edge(tmp_path, status):
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "nodes": [
            {"id": "exp:x", "type": "experiment", "status": status},
            {"id": "claim:x", "type": "claim", "status": "current"},
        ],
        "edges": [{"from": "exp:x", "to": "claim:x", "rel": "supports"}],
    })
    issues = engine.lint_issues(cfg)
    assert any(kind == "STATUS_RELATION_MISMATCH" for kind, _node, _detail in issues)


def test_lint_flags_load_bearing_node_without_backbone(tmp_path):
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "concepts": {"dataset_version": {"canonical": "v1"}},
        "nodes": [{"id": "claim:x", "type": "claim", "status": "current"}],
        "edges": []})
    kinds = {k for k, _, _ in engine.lint_issues(cfg)}
    assert "NO_BACKBONE" in kinds


# --------------------------------------------------------------------------- deterministic false-green regressions

def test_multiple_concepts_are_scoped_by_backbone_mapping(tmp_path):
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "concepts": {
            "dataset_version": {"canonical": "v2"},
            "model_version": {"canonical": "m1"},
        },
        "nodes": [
            {"id": "data:x", "type": "data", "status": "current",
             "backbone": {"dataset_version": "v2"}},
            {"id": "art:model", "type": "artifact", "status": "current",
             "backbone": {"model_version": "m1"}},
        ],
        "edges": []})
    problems, _ = engine.compute_check(cfg)
    assert not {"SILENT_DRIFT", "AMBIGUOUS_BACKBONE"} & {k for k, _, _ in problems}
    impacted, _ = engine.impact(cfg, "dataset_version", "v3")
    assert [nid for nid, _, _ in impacted] == ["data:x"]


def test_scalar_backbone_is_rejected_when_multiple_concepts_exist(tmp_path):
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "concepts": {"dataset_version": {"canonical": "v2"},
                     "model_version": {"canonical": "m1"}},
        "nodes": [{"id": "data:x", "type": "data", "status": "current", "backbone": "v2"}],
        "edges": []})
    problems, _ = engine.compute_check(cfg)
    assert any(k == "AMBIGUOUS_BACKBONE" for k, _, _ in problems)
    assert any(k == "AMBIGUOUS_BACKBONE" for k, _, _ in engine.lint_issues(cfg))


def test_impact_is_topologically_ordered(tmp_path):
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "concepts": {"model": {"canonical": "m1"}},
        "nodes": [
            {"id": "art:fit", "type": "artifact", "status": "current", "backbone": "m1"},
            {"id": "code:fit", "type": "code", "status": "current", "backbone": "m1"},
        ],
        "edges": [{"from": "code:fit", "to": "art:fit", "rel": "produces"}]})
    impacted, _ = engine.impact(cfg, "model", "m2")
    assert [nid for nid, _, _ in impacted] == ["code:fit", "art:fit"]


def test_claim_support_binding_is_checked_in_dependency_direction(tmp_path):
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "concepts": {"dataset_version": {"canonical": "v2"}},
        "nodes": [
            {"id": "art:old", "type": "artifact", "status": "confirmed", "backbone": "v1"},
            {"id": "claim:new", "type": "claim", "status": "current", "backbone": "v2"},
        ],
        "edges": [{"from": "art:old", "to": "claim:new", "rel": "supports"}]})
    problems, _ = engine.compute_check(cfg)
    assert any(k == "CLAIM_CITES_OFFBACKBONE" and nid == "claim:new"
               for k, nid, _ in problems)


def test_claim_checks_transitive_evidence_bindings(tmp_path):
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "concepts": {"dataset_version": {"canonical": "v2"}},
        "nodes": [
            {"id": "art:old", "type": "artifact", "status": "confirmed", "backbone": "v1"},
            {"id": "art:new", "type": "artifact", "status": "current", "backbone": "v2"},
            {"id": "claim:new", "type": "claim", "status": "current", "backbone": "v2"},
        ],
        "edges": [
            {"from": "art:old", "to": "art:new", "rel": "derives_from"},
            {"from": "art:new", "to": "claim:new", "rel": "supports"},
        ]})
    problems, _ = engine.compute_check(cfg)
    assert any(k == "CLAIM_CITES_OFFBACKBONE" and nid == "claim:new" and "art:old" in detail
               for k, nid, detail in problems)


def _render_project(tmp_path, claim_status="current"):
    (tmp_path / "data.csv").write_text("x\n1\n")
    (tmp_path / "analysis.py").write_text("print('ok')\n")
    (tmp_path / "figure.svg").write_text("<svg/>\n")
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "concepts": {"dataset_version": {"canonical": "v1"}},
        "nodes": [
            {"id": "data:x", "type": "data", "status": "current", "backbone": "v1",
             "path": "data.csv"},
            {"id": "code:plot", "type": "code", "status": "current", "path": "analysis.py"},
            {"id": "fig:x", "type": "figure", "status": "current", "backbone": "v1",
             "path": "figure.svg"},
            {"id": "claim:x", "type": "claim", "status": claim_status, "backbone": "v1"},
        ],
        "edges": [
            {"from": "data:x", "to": "fig:x", "rel": "renders"},
            {"from": "code:plot", "to": "fig:x", "rel": "renders"},
            {"from": "fig:x", "to": "claim:x", "rel": "supports"},
        ]})
    return cfg


def test_missing_manifest_is_an_error(tmp_path):
    cfg = _render_project(tmp_path)
    problems, _ = engine.compute_check(cfg)
    assert any(k == "MISSING_MANIFEST" for k, _, _ in problems)


def test_manifest_must_cover_every_declared_input(tmp_path):
    cfg = _render_project(tmp_path)
    assert snapshot(cfg) == 0
    manifest = tmp_path / "figure.svg.manifest.json"
    record = json.loads(manifest.read_text())
    record["inputs"] = [item for item in record["inputs"] if item["path"] != "analysis.py"]
    manifest.write_text(json.dumps(record))
    problems, _ = engine.compute_check(cfg)
    assert any(k == "MANIFEST_INPUT_MISMATCH" and "analysis.py" in detail
               for k, _, detail in problems)


def test_malformed_manifest_fails_closed(tmp_path):
    cfg = _render_project(tmp_path)
    (tmp_path / "figure.svg.manifest.json").write_text("{not json")
    problems, _ = engine.compute_check(cfg)
    assert any(k == "INVALID_MANIFEST" for k, _, _ in problems)


def test_output_hash_is_checked(tmp_path):
    cfg = _render_project(tmp_path)
    assert snapshot(cfg) == 0
    (tmp_path / "figure.svg").write_text("<svg>tampered</svg>\n")
    problems, _ = engine.compute_check(cfg)
    assert any(k == "OUTPUT_DRIFT" for k, _, _ in problems)
    assert any(k == "UPSTREAM_STALE" and nid == "claim:x" for k, nid, _ in problems)


def test_manifest_backbone_relabel_is_detected(tmp_path):
    cfg = _render_project(tmp_path)
    assert snapshot(cfg) == 0
    graph = json.loads(cfg.graph_path.read_text())
    graph["concepts"]["dataset_version"]["canonical"] = "v2"
    for node in graph["nodes"]:
        if node.get("backbone"):
            node["backbone"] = "v2"
    cfg.graph_path.write_text(json.dumps(graph))
    problems, _ = engine.compute_check(cfg)
    assert any(k == "MANIFEST_BACKBONE_MISMATCH" and nid == "fig:x"
               for k, nid, _ in problems)
    assert any(k == "UPSTREAM_STALE" and nid == "claim:x" for k, nid, _ in problems)


def test_changed_input_propagates_to_claim(tmp_path):
    cfg = _render_project(tmp_path)
    assert snapshot(cfg) == 0
    (tmp_path / "data.csv").write_text("x\n2\n")
    problems, _ = engine.compute_check(cfg)
    assert any(k == "STALE_DATA" and nid == "fig:x" for k, nid, _ in problems)
    assert any(k == "UPSTREAM_STALE" and nid == "claim:x" for k, nid, _ in problems)


def test_changed_input_propagates_to_confirmed_claim(tmp_path):
    cfg = _render_project(tmp_path, claim_status="confirmed")
    assert snapshot(cfg) == 0
    (tmp_path / "data.csv").write_text("x\n2\n")
    problems, _ = engine.compute_check(cfg)
    assert any(k == "UPSTREAM_STALE" and nid == "claim:x" for k, nid, _ in problems)


def test_log_rejects_invalid_edge_without_writing(tmp_path):
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "nodes": [{"id": "data:x", "type": "data", "status": "current"}],
        "edges": []})
    before = cfg.graph_path.read_text()
    ok, message = engine.log_entry(cfg, {
        "node": {"id": "exp:x", "type": "experiment", "status": "null"},
        "edges": [{"from": "exp:x", "to": "missing", "rel": "related"}]})
    assert not ok and "DANGLING_EDGE" in message
    assert cfg.graph_path.read_text() == before


def test_concurrent_logs_do_not_lose_entries(tmp_path):
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "nodes": [{"id": "data:x", "type": "data", "status": "current"}],
        "edges": []})

    def log_one(i):
        return engine.log_entry(cfg, {
            "node": {"id": f"exp:{i}", "type": "experiment", "status": "null"},
            "edges": []})

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(log_one, range(12)))
    assert all(ok for ok, _message in results)
    nodes, _edges, _concepts = engine.load_graph(cfg)
    assert {f"exp:{i}" for i in range(12)} <= set(nodes)


def test_cross_process_logs_do_not_lose_entries(tmp_path):
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "nodes": [{"id": "data:x", "type": "data", "status": "current"}],
        "edges": []})
    ctx = mp.get_context("spawn")
    start, results = ctx.Event(), ctx.Queue()
    processes = [ctx.Process(target=_process_log,
                             args=(str(cfg.config_path), f"exp:process-{i}", start, results))
                 for i in range(4)]
    for process in processes:
        process.start()
    start.set()
    outcomes = [results.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert all(ok for ok, _message in outcomes)
    nodes, _edges, _concepts = engine.load_graph(cfg)
    assert {f"exp:process-{i}" for i in range(4)} <= set(nodes)
