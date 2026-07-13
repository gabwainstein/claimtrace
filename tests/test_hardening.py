"""Tests for the Phase-0 hardening: structural integrity, graceful errors, and lint."""
import json

import pytest

from claimtrace import engine
from claimtrace.config import Config


def _project(tmp_path, graph, config=None):
    (tmp_path / "claimtrace").mkdir()
    (tmp_path / "claimtrace.config.json").write_text(
        json.dumps(config or {"root": ".", "graph": "claimtrace/graph.json"}))
    (tmp_path / "claimtrace" / "graph.json").write_text(
        graph if isinstance(graph, str) else json.dumps(graph))
    return Config(tmp_path / "claimtrace.config.json")


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


def test_missing_nodes_key_raises_graph_error(tmp_path):
    cfg = _project(tmp_path, {"edges": []})
    with pytest.raises(engine.GraphError):
        engine.load_graph(cfg)


def test_node_without_id_raises_graph_error(tmp_path):
    cfg = _project(tmp_path, {"nodes": [{"type": "data"}], "edges": []})
    with pytest.raises(engine.GraphError):
        engine.load_graph(cfg)


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


def test_lint_flags_load_bearing_node_without_backbone(tmp_path):
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "concepts": {"dataset_version": {"canonical": "v1"}},
        "nodes": [{"id": "claim:x", "type": "claim", "status": "current"}],
        "edges": []})
    kinds = {k for k, _, _ in engine.lint_issues(cfg)}
    assert "NO_BACKBONE" in kinds
