"""Deterministic graph transaction tests."""
import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from claimtrace.config import Config
from claimtrace import graph_changes


def _project(tmp_path: Path, graph: dict) -> Config:
    (tmp_path / "claimtrace").mkdir()
    (tmp_path / "claimtrace.config.json").write_text(
        json.dumps({"root": ".", "graph": "claimtrace/graph.json"}),
        encoding="utf-8",
    )
    (tmp_path / "claimtrace" / "graph.json").write_text(
        json.dumps(graph, indent=2) + "\n",
        encoding="utf-8",
    )
    return Config(tmp_path / "claimtrace.config.json")


def _base_graph() -> dict:
    return {
        "schema_version": "1.0",
        "concepts": {
            "dataset": {"canonical": "v1"},
            "remove_me": {"canonical": "old"},
        },
        "nodes": [
            {"id": "data:raw", "type": "data", "status": "current"},
            {"id": "code:clean", "type": "code", "status": "current"},
            {"id": "art:old", "type": "artifact", "status": "current"},
        ],
        "edges": [
            {"from": "data:raw", "to": "code:clean", "rel": "reads"},
            {"from": "code:clean", "to": "art:old", "rel": "produces"},
        ],
    }


def _request(cfg: Config, changes: dict, description="reviewed graph update") -> dict:
    raw = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    return {
        "schema_version": graph_changes.GRAPH_CHANGE_REQUEST_SCHEMA,
        "base_graph_hash": graph_changes.canonical_graph_hash(raw),
        "description": description,
        "changes": changes,
    }


def test_build_and_apply_bounded_graph_change_then_retry_idempotently(tmp_path):
    cfg = _project(tmp_path, _base_graph())
    request = _request(cfg, {
        "add_nodes": [
            {"id": "art:clean", "type": "artifact", "status": "current"},
        ],
        "replace_nodes": [
            {"id": "code:clean", "type": "code", "status": "confirmed"},
        ],
        "remove_nodes": ["art:old"],
        "add_edges": [
            {"from": "code:clean", "to": "art:clean", "rel": "produces"},
        ],
        "remove_edges": [
            {"from": "code:clean", "to": "art:old", "rel": "produces"},
        ],
        "set_concepts": {
            "dataset": {"canonical": "v2"},
            "analysis": {"canonical": "primary"},
        },
        "remove_concepts": ["remove_me"],
    })

    proposal = graph_changes.build_graph_change_proposal(cfg, request)

    assert proposal["proposal_id"].startswith("graph-change:sha256:")
    assert proposal["base_graph_hash"] == request["base_graph_hash"]
    assert proposal["result_graph_hash"] != request["base_graph_hash"]
    assert graph_changes.validate_graph_change_proposal(proposal) == proposal

    result = graph_changes.apply_graph_change_proposal(cfg, proposal)
    assert result == {
        "status": "applied",
        "proposal_id": proposal["proposal_id"],
        "graph_hash": proposal["result_graph_hash"],
    }
    written = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    assert {node["id"] for node in written["nodes"]} == {
        "data:raw", "code:clean", "art:clean",
    }
    assert next(
        node for node in written["nodes"] if node["id"] == "code:clean"
    )["status"] == "confirmed"
    assert written["concepts"] == {
        "analysis": {"canonical": "primary"},
        "dataset": {"canonical": "v2"},
    }
    assert written["edges"] == [
        {"from": "data:raw", "rel": "reads", "to": "code:clean"},
        {"from": "code:clean", "rel": "produces", "to": "art:clean"},
    ]

    bytes_after_first_apply = cfg.graph_path.read_bytes()
    retry = graph_changes.apply_graph_change_proposal(cfg, proposal)
    assert retry["status"] == "already_applied"
    assert retry["graph_hash"] == proposal["result_graph_hash"]
    assert cfg.graph_path.read_bytes() == bytes_after_first_apply


def test_change_order_is_normalized_and_content_address_is_reproducible(tmp_path):
    graph = _base_graph()
    cfg = _project(tmp_path, graph)
    changes = {
        "add_nodes": [
            {"id": "method:z", "type": "method"},
            {"id": "method:a", "type": "method"},
        ],
        "set_concepts": {
            "zeta": {"canonical": "z"},
            "alpha": {"canonical": "a"},
        },
    }
    first = graph_changes.build_graph_change_proposal(cfg, _request(cfg, changes))
    changes["add_nodes"].reverse()
    reversed_concepts = list(reversed(list(changes["set_concepts"].items())))
    changes["set_concepts"] = dict(reversed_concepts)
    second = graph_changes.build_graph_change_proposal(cfg, _request(cfg, changes))

    assert first == second
    assert [node["id"] for node in first["changes"]["add_nodes"]] == [
        "method:a", "method:z",
    ]
    assert list(first["changes"]["set_concepts"]) == ["alpha", "zeta"]


def test_canonical_graph_hash_ignores_object_key_order_and_formatting(tmp_path):
    left = _base_graph()
    right = json.loads(json.dumps(left, sort_keys=True, separators=(",", ":")))
    assert graph_changes.canonical_graph_hash(left) == graph_changes.canonical_graph_hash(right)

    cfg = _project(tmp_path, left)
    cfg.graph_path.write_text(
        "\ufeff" + json.dumps(right, separators=(",", ":")), encoding="utf-8"
    )
    proposal = graph_changes.build_graph_change_proposal(
        cfg,
        {
            "schema_version": graph_changes.GRAPH_CHANGE_REQUEST_SCHEMA,
            "base_graph_hash": graph_changes.canonical_graph_hash(left),
            "changes": {"set_concepts": {"dataset": {"canonical": "v2"}}},
        },
    )
    assert proposal["base_graph_hash"] == graph_changes.canonical_graph_hash(left)


def test_proposal_requires_exact_current_base_hash(tmp_path):
    cfg = _project(tmp_path, _base_graph())
    request = _request(cfg, {"add_nodes": [{"id": "claim:x", "type": "claim"}]})
    request["base_graph_hash"] = "graph:sha256:" + "0" * 64

    before = cfg.graph_path.read_bytes()
    with pytest.raises(graph_changes.GraphChangeError, match="base hash does not match"):
        graph_changes.build_graph_change_proposal(cfg, request)
    assert cfg.graph_path.read_bytes() == before


def test_structurally_invalid_result_is_rejected_before_proposal(tmp_path):
    cfg = _project(tmp_path, _base_graph())
    request = _request(cfg, {"remove_nodes": ["art:old"]})

    before = cfg.graph_path.read_bytes()
    with pytest.raises(graph_changes.GraphChangeError, match="DANGLING_EDGE"):
        graph_changes.build_graph_change_proposal(cfg, request)
    assert cfg.graph_path.read_bytes() == before


def test_cycle_is_rejected_before_proposal(tmp_path):
    cfg = _project(tmp_path, _base_graph())
    request = _request(cfg, {
        "add_edges": [
            {"from": "art:old", "to": "code:clean", "rel": "produces"},
        ],
    })

    with pytest.raises(graph_changes.GraphChangeError, match="CYCLE"):
        graph_changes.build_graph_change_proposal(cfg, request)


def test_apply_fails_closed_if_graph_drifted_after_proposal(tmp_path):
    cfg = _project(tmp_path, _base_graph())
    proposal = graph_changes.build_graph_change_proposal(
        cfg,
        _request(cfg, {"add_nodes": [{"id": "claim:x", "type": "claim"}]}),
    )
    drifted = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    drifted["nodes"].append({"id": "exp:other", "type": "experiment"})
    cfg.graph_path.write_text(json.dumps(drifted), encoding="utf-8")
    before = cfg.graph_path.read_bytes()

    with pytest.raises(graph_changes.GraphChangeError, match="graph drifted"):
        graph_changes.apply_graph_change_proposal(cfg, proposal)
    assert cfg.graph_path.read_bytes() == before


def test_apply_rejects_tampered_proposal_before_writing(tmp_path):
    cfg = _project(tmp_path, _base_graph())
    proposal = graph_changes.build_graph_change_proposal(
        cfg,
        _request(cfg, {"add_nodes": [{"id": "claim:x", "type": "claim"}]}),
    )
    tampered = copy.deepcopy(proposal)
    tampered["changes"]["add_nodes"][0]["id"] = "claim:tampered"
    before = cfg.graph_path.read_bytes()

    with pytest.raises(graph_changes.GraphChangeError, match="content-address mismatch"):
        graph_changes.apply_graph_change_proposal(cfg, tampered)
    assert cfg.graph_path.read_bytes() == before


def test_apply_recomputes_result_even_if_forged_proposal_has_consistent_address(tmp_path):
    cfg = _project(tmp_path, _base_graph())
    proposal = graph_changes.build_graph_change_proposal(
        cfg,
        _request(cfg, {"add_nodes": [{"id": "claim:x", "type": "claim"}]}),
    )
    forged = copy.deepcopy(proposal)
    forged["result_graph_hash"] = "graph:sha256:" + "f" * 64
    core = {key: value for key, value in forged.items() if key != "proposal_id"}
    encoded = json.dumps(
        core, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    forged["proposal_id"] = (
        "graph-change:sha256:" + hashlib.sha256(encoded).hexdigest()
    )
    before = cfg.graph_path.read_bytes()

    with pytest.raises(graph_changes.GraphChangeError, match="result hash mismatch"):
        graph_changes.apply_graph_change_proposal(cfg, forged)
    assert cfg.graph_path.read_bytes() == before


def test_concurrent_same_base_proposals_cannot_lose_an_update(tmp_path):
    cfg = _project(tmp_path, _base_graph())
    proposals = [
        graph_changes.build_graph_change_proposal(
            cfg,
            _request(cfg, {"add_nodes": [{"id": node_id, "type": "claim"}]}),
        )
        for node_id in ("claim:first", "claim:second")
    ]

    def apply_one(proposal):
        try:
            return graph_changes.apply_graph_change_proposal(cfg, proposal)["status"]
        except graph_changes.GraphChangeError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(apply_one, proposals))

    assert outcomes.count("applied") == 1
    assert sum("graph drifted" in outcome for outcome in outcomes) == 1
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    added = {node["id"] for node in graph["nodes"]} & {"claim:first", "claim:second"}
    assert len(added) == 1


def test_request_schema_rejects_conflicts_unknown_fields_and_excess_operations(tmp_path):
    cfg = _project(tmp_path, _base_graph())
    conflicting = _request(cfg, {
        "add_nodes": [{"id": "claim:x", "type": "claim"}],
        "remove_nodes": ["claim:x"],
    })
    with pytest.raises(graph_changes.GraphChangeError, match="more than one"):
        graph_changes.build_graph_change_proposal(cfg, conflicting)

    unknown = _request(cfg, {"rename_nodes": []})
    with pytest.raises(graph_changes.GraphChangeError, match="unknown field"):
        graph_changes.build_graph_change_proposal(cfg, unknown)

    excessive = _request(cfg, {
        "remove_nodes": [f"node:{index}" for index in range(
            graph_changes.MAX_GRAPH_CHANGE_OPERATIONS + 1
        )],
    })
    with pytest.raises(graph_changes.GraphChangeError, match="maximum is"):
        graph_changes.build_graph_change_proposal(cfg, excessive)


def test_no_op_request_is_rejected(tmp_path):
    cfg = _project(tmp_path, _base_graph())
    unchanged = next(
        node for node in _base_graph()["nodes"] if node["id"] == "code:clean"
    )
    request = _request(cfg, {"replace_nodes": [unchanged]})

    with pytest.raises(graph_changes.GraphChangeError, match="no-op"):
        graph_changes.build_graph_change_proposal(cfg, request)


def test_atomic_replace_failure_preserves_original_graph(tmp_path, monkeypatch):
    cfg = _project(tmp_path, _base_graph())
    proposal = graph_changes.build_graph_change_proposal(
        cfg,
        _request(cfg, {"add_nodes": [{"id": "claim:x", "type": "claim"}]}),
    )
    before = cfg.graph_path.read_bytes()

    def fail_replace(_source, _destination):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(graph_changes.os, "replace", fail_replace)
    with pytest.raises(graph_changes.GraphChangeError, match="cannot atomically write"):
        graph_changes.apply_graph_change_proposal(cfg, proposal)

    assert cfg.graph_path.read_bytes() == before
    assert list(cfg.graph_path.parent.glob("graph.json.*.tmp")) == []
