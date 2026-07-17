"""Focused adversarial tests for deterministic semantic normalization policy."""
import copy
import hashlib
import json
import multiprocessing as mp
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

import provsleuth.semantics as semantics
import provsleuth.cli as provsleuth_cli
from provsleuth.cli import main
from provsleuth.config import Config
from provsleuth.semantics import (
    SemanticError,
    append_mapping,
    append_mapping_review,
    append_semantic_policy,
    configured_semantic_assets,
    create_mapping_proposal,
    create_mapping_review,
    create_ontology_lock,
    create_semantic_policy,
    current_mapping_leaves,
    detect_mapping_conflicts,
    evaluate_mapping,
    evaluate_mappings,
    evaluate_semantic_policy,
    load_local_terminology,
    load_mappings,
    load_ontology_bundle,
    load_ontology_index,
    search_ontology_candidates,
    validate_mapping_document,
)


FIXED_TIME = "2026-07-15T01:00:00.000Z"
REVIEW_TIME = "2026-07-15T01:01:00.000Z"


class SemanticConfig:
    pass


def _process_mapping_review(config_path, mapping_id, state, start, results):
    start.wait()
    try:
        document, _path = append_mapping_review(
            Config(config_path), mapping_id, state,
            actor=f"reviewer:{state}", recorded_at=REVIEW_TIME,
        )
        results.put(("ok", document["id"]))
    except Exception as exc:  # pragma: no cover - detail crosses process boundary
        results.put((type(exc).__name__, str(exc)))


def _local_terminology():
    return {
        "schema_version": "claimtrace.local-terminology/1",
        "id": "study:terms",
        "version": "1.0.0",
        "terms": [
            {
                "id": "study:memory-score",
                "kind": "concept",
                "label": "Memory score",
                "definition": "Score produced by the declared memory assessment.",
                "aliases": ["recall score"],
            },
            {
                "id": "study:attention-score",
                "kind": "concept",
                "label": "Attention score",
                "definition": "Score produced by the declared attention assessment.",
                "aliases": [],
            },
        ],
    }


def _ontology_terms(*, duplicate_memory=False, memory_kind="class"):
    terms = [
        {
            "iri": "https://example.org/onto/MemoryScore",
            "kind": memory_kind,
            "labels": [{"text": "memory score", "language": "en"}],
            "synonyms": [{"text": "recall measure", "language": "en"}],
            "definitions": [{"text": "A score measuring memory.", "language": "en"}],
            "deprecated": False,
            "parents": ["https://example.org/onto/Measurement"],
        },
        {
            "iri": "https://example.org/onto/AttentionScore",
            "kind": "class",
            "labels": [{"text": "attention score", "language": "en"}],
            "synonyms": [],
            "definitions": [],
            "deprecated": False,
            "parents": [],
        },
    ]
    if duplicate_memory:
        terms.append({
            "iri": "https://example.org/onto/MemoryMeasurement",
            "kind": "class",
            "labels": [{"text": "Memory   Score", "language": None}],
            "synonyms": [],
            "definitions": [{"text": "A broader memory measurement.", "language": "en"}],
            "deprecated": False,
            "parents": [],
        })
    return terms


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _project(tmp_path, *, duplicate_memory=False, max_candidates=25,
             memory_kind="class"):
    semantic_root = tmp_path / "provsleuth" / "semantics"
    ontology_root = semantic_root / "ontology"
    terminology_path = semantic_root / "local-terms.json"
    raw_path = ontology_root / "ontology.ttl"
    index_path = ontology_root / "index.json"
    lock_path = ontology_root / "ontology.lock.json"
    mappings_path = semantic_root / "mappings"
    policies_path = semantic_root / "policies"
    _write_json(terminology_path, _local_terminology())
    ontology_root.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(b"@prefix ex: <https://example.org/onto/> .\n")
    _write_json(index_path, {
        "schema_version": "claimtrace.ontology-index/1",
        "ontology_id": "example:ontology",
        "version": "2026-07-15",
        "terms": _ontology_terms(
            duplicate_memory=duplicate_memory, memory_kind=memory_kind),
    })
    lock = create_ontology_lock(
        ontology_id="example:ontology",
        ontology_iri="https://example.org/onto/",
        version="2026-07-15",
        version_iri="https://example.org/onto/releases/2026-07-15",
        license_iri="https://creativecommons.org/publicdomain/zero/1.0/",
        imports=[],
        declared_imports_available=True,
        documents=[raw_path],
        index_path=index_path,
        base=ontology_root,
    )
    _write_json(lock_path, lock)
    cfg = SemanticConfig()
    cfg.base = tmp_path
    cfg.semantic_terminology_paths = [terminology_path]
    cfg.semantic_ontology_lock_paths = [lock_path]
    cfg.semantic_mappings_path = mappings_path
    cfg.semantic_policies_path = policies_path
    cfg.semantic_active_policy = None
    cfg.semantic_language = "en"
    cfg.semantic_max_candidates = max_candidates
    return cfg, {
        "terminology": terminology_path,
        "raw": raw_path,
        "index": index_path,
        "lock": lock_path,
    }


def _agent_input(cfg, *, query="memory score", relation="skos:closeMatch", iri=None):
    candidates = search_ontology_candidates(cfg, query)
    if iri is None and relation != "unmapped":
        iri = candidates["candidates"][0]["iri"]
    target = None if relation == "unmapped" else {
        "ontology_lock_id": candidates["candidates"][0]["ontology_lock_id"],
        "iri": iri,
    }
    return {
        "relation": relation,
        "target": target,
        "candidate_query": query,
        "candidate_set_id": candidates["id"],
        "rationale": "The definitions overlap, but the local measurement is narrower in context.",
        "limitations": ["The ontology does not encode the project instrument."],
        "provenance": {"agent": "agent:test", "model": "fixture"},
    }


def _proposal(cfg, **kwargs):
    return create_mapping_proposal(
        cfg, "study:terms", "study:memory-score", _agent_input(cfg, **kwargs),
        actor="agent:test", recorded_at=FIXED_TIME,
    )


def test_strict_terminology_and_index_reject_unknown_and_duplicate_values():
    terminology = _local_terminology()
    assert load_local_terminology(terminology)["terms"][0]["id"] == "study:attention-score"
    unknown = copy.deepcopy(terminology)
    unknown["unexpected"] = True
    with pytest.raises(SemanticError, match="unknown or missing"):
        load_local_terminology(unknown)
    duplicate = copy.deepcopy(terminology)
    duplicate["terms"].append(copy.deepcopy(duplicate["terms"][0]))
    with pytest.raises(SemanticError, match="duplicate"):
        load_local_terminology(duplicate)
    malformed_kind = copy.deepcopy(terminology)
    malformed_kind["terms"][0]["kind"] = {}
    with pytest.raises(SemanticError, match="unsupported kind"):
        load_local_terminology(malformed_kind)

    index = {
        "schema_version": "claimtrace.ontology-index/1",
        "ontology_id": "example:ontology",
        "version": "1",
        "terms": _ontology_terms(),
    }
    assert len(load_ontology_index(index)["terms"]) == 2
    index["terms"][1]["iri"] = index["terms"][0]["iri"]
    with pytest.raises(SemanticError, match="duplicate ontology term IRI"):
        load_ontology_index(index)


def test_ontology_lock_commits_metadata_and_detects_exact_byte_drift(tmp_path):
    _cfg, paths = _project(tmp_path)
    lock, index = load_ontology_bundle(paths["lock"])
    assert lock["ontology_iri"] == "https://example.org/onto/"
    assert lock["declared_imports_available"] is True
    assert lock["index"]["assertion"] == (
        "project-supplied-index-not-verified-extraction")
    assert index["ontology_id"] == lock["ontology_id"]

    forged = json.loads(paths["lock"].read_text(encoding="utf-8"))
    forged["license_iri"] = "https://example.org/different-license"
    with pytest.raises(SemanticError, match="id does not match"):
        load_ontology_bundle(forged, base=paths["lock"].parent)

    paths["raw"].write_bytes(paths["raw"].read_bytes() + b"# drift\n")
    with pytest.raises(SemanticError, match="bytes drifted"):
        load_ontology_bundle(paths["lock"])


def test_ontology_document_hashing_is_streamed_in_bounded_chunks(tmp_path, monkeypatch):
    path = tmp_path / "large.owl"
    path.write_bytes(b"x" * (2 * 1024 * 1024 + 123))
    requests = []
    original = semantics.os.read

    def observed_read(descriptor, size):
        requests.append(size)
        return original(descriptor, size)

    monkeypatch.setattr(semantics.os, "read", observed_read)
    snapshot = semantics._snapshot_file(path, "large ontology")
    assert snapshot["size"] == path.stat().st_size
    assert len(requests) >= 3
    assert max(requests) <= 1024 * 1024


def test_ontology_lock_creation_rejects_aggregate_size_before_hashing(
        tmp_path, monkeypatch):
    raw = tmp_path / "ontology.ttl"
    index = tmp_path / "index.json"
    raw.write_bytes(b"ontology bytes")
    _write_json(index, {
        "schema_version": "claimtrace.ontology-index/1",
        "ontology_id": "example:ontology",
        "version": "1",
        "terms": _ontology_terms(),
    })
    monkeypatch.setattr(semantics, "MAX_LOCK_TOTAL_BYTES", 1)

    def must_not_hash(*_args, **_kwargs):
        raise AssertionError("aggregate preflight must run before hashing")

    monkeypatch.setattr(semantics, "_snapshot_file", must_not_hash)
    with pytest.raises(SemanticError, match="aggregate byte limit"):
        create_ontology_lock(
            ontology_id="example:ontology",
            ontology_iri="https://example.org/onto/",
            version="1",
            documents=[raw], index_path=index, base=tmp_path,
        )


def test_declared_import_availability_requires_configured_identity_matched_lock(tmp_path):
    cfg, paths = _project(tmp_path)
    lock = json.loads(paths["lock"].read_text(encoding="utf-8"))
    core = {key: value for key, value in lock.items() if key != "id"}
    core["imports"] = [{
        "ontology_iri": "https://example.org/imported/",
        "version_iri": None,
        "ontology_lock_id": "ontology-lock:sha256:" + "1" * 64,
    }]
    lock = {"id": semantics._ontology_lock_id(core), **core}
    _write_json(paths["lock"], lock)
    with pytest.raises(SemanticError, match="manifest-listed import"):
        configured_semantic_assets(cfg)


def test_candidate_search_is_exact_normalized_and_totally_ordered(tmp_path):
    cfg, _paths = _project(tmp_path, duplicate_memory=True)
    first = search_ontology_candidates(cfg, "  MeMoRy\tSCORE ")
    second = search_ontology_candidates(cfg, "  MeMoRy\tSCORE ")
    assert first == second
    assert [item["iri"] for item in first["candidates"]] == [
        "https://example.org/onto/MemoryScore",
        "https://example.org/onto/MemoryMeasurement",
    ]
    assert all(item["matched_on"] == "preferred_label" for item in first["candidates"])
    assert search_ontology_candidates(cfg, "memory scor")["candidates"] == []
    synonym = search_ontology_candidates(cfg, "RECALL   MEASURE")
    assert synonym["candidates"][0]["matched_on"] == "synonym"


def test_iri_identity_search_is_case_sensitive(tmp_path):
    cfg, _paths = _project(tmp_path)
    exact = search_ontology_candidates(cfg, "https://example.org/onto/MemoryScore")
    assert [item["matched_on"] for item in exact["candidates"]] == ["iri"]
    changed_case = search_ontology_candidates(cfg, "https://example.org/onto/memoryscore")
    assert changed_case["candidates"] == []


def test_mapping_selection_is_recomputed_and_agent_cannot_inject_fields(tmp_path):
    cfg, _paths = _project(tmp_path)
    proposal = _proposal(cfg)
    validate_mapping_document(proposal)
    assert proposal["derived"]["snapshot_eligible_for_policy"] is False
    assert "active" not in proposal["derived"]

    invented = _agent_input(cfg)
    invented["target"]["iri"] = "https://example.org/onto/Invented"
    with pytest.raises(SemanticError, match="not one unique member"):
        create_mapping_proposal(
            cfg, "study:terms", "study:memory-score", invented,
            actor="agent:test", recorded_at=FIXED_TIME,
        )
    injected = _agent_input(cfg)
    injected["derived"] = {"active": True}
    with pytest.raises(SemanticError, match="unknown or missing"):
        create_mapping_proposal(
            cfg, "study:terms", "study:memory-score", injected,
            actor="agent:test", recorded_at=FIXED_TIME,
        )


def test_truncated_candidate_set_never_becomes_policy_eligible(tmp_path):
    cfg, _paths = _project(tmp_path, duplicate_memory=True, max_candidates=1)
    proposal = _proposal(cfg)
    assert proposal["mechanical_snapshot"]["candidate_set"]["truncated"] is True
    accepted = create_mapping_review(
        proposal, "accepted", actor="reviewer:human", recorded_at=REVIEW_TIME)
    assert accepted["derived"]["snapshot_eligible_for_policy"] is False
    assert any(
        item["code"] == "SEMANTIC_CANDIDATE_SET_TRUNCATED"
        for item in accepted["derived"]["findings"]
    )


def test_kind_mismatch_is_reviewable_but_never_policy_eligible(tmp_path):
    cfg, _paths = _project(tmp_path, memory_kind="individual")
    proposal = _proposal(cfg)
    accepted = create_mapping_review(
        proposal, "accepted", actor="reviewer:human", recorded_at=REVIEW_TIME)
    assert accepted["derived"]["snapshot_eligible_for_policy"] is False
    assert any(
        item["code"] == "SEMANTIC_MAPPING_KIND_MISMATCH"
        for item in accepted["derived"]["findings"]
    )
    assert evaluate_mapping(cfg, accepted)["eligible_for_policy"] is False


def test_mapping_review_is_separate_immutable_and_atomic(tmp_path):
    cfg, _paths = _project(tmp_path)
    proposal = _proposal(cfg)
    append_mapping(cfg, proposal)
    with pytest.raises(SemanticError, match="different actor"):
        append_mapping_review(
            cfg, proposal["id"], "accepted", actor="agent:test", recorded_at=REVIEW_TIME)
    accepted, path = append_mapping_review(
        cfg, proposal["id"], "accepted", actor="reviewer:human", recorded_at=REVIEW_TIME)
    assert path.exists()
    assert accepted["derived"]["snapshot_eligible_for_policy"] is True
    assert "active" not in accepted["derived"]
    documents, issues = load_mappings(cfg)
    assert issues == []
    assert len(documents) == 2
    with pytest.raises(SemanticError, match="not a current review leaf"):
        append_mapping_review(
            cfg, proposal["id"], "rejected", actor="reviewer:other",
            recorded_at="2026-07-15T01:02:00.000Z",
        )


def test_mapping_evaluation_detects_full_candidate_and_local_term_drift(tmp_path):
    cfg, paths = _project(tmp_path)
    proposal = _proposal(cfg)
    accepted = create_mapping_review(
        proposal, "accepted", actor="reviewer:human", recorded_at=REVIEW_TIME)
    assert evaluate_mapping(cfg, accepted)["eligible_for_policy"] is True
    terminology = json.loads(paths["terminology"].read_text(encoding="utf-8"))
    terminology["terms"][0]["definition"] += " Changed."
    _write_json(paths["terminology"], terminology)
    evaluation = evaluate_mapping(cfg, accepted)
    assert evaluation["stale"] is True
    assert evaluation["eligible_for_policy"] is False


def test_mapping_batch_reuses_candidate_queries_and_rechecks_asset_stability(
        tmp_path, monkeypatch):
    cfg, _paths = _project(tmp_path)
    proposal = _proposal(cfg)
    accepted = create_mapping_review(
        proposal, "accepted", actor="reviewer:human", recorded_at=REVIEW_TIME,
    )
    asset_calls = 0
    search_calls = 0
    original_assets = semantics.configured_semantic_assets
    original_search = semantics._candidate_set_from_assets

    def counted_assets(value):
        nonlocal asset_calls
        asset_calls += 1
        return original_assets(value)

    def counted_search(*args, **kwargs):
        nonlocal search_calls
        search_calls += 1
        return original_search(*args, **kwargs)

    monkeypatch.setattr(semantics, "configured_semantic_assets", counted_assets)
    monkeypatch.setattr(semantics, "_candidate_set_from_assets", counted_search)
    evaluations = evaluate_mappings(cfg, [proposal, accepted])
    assert [item["effective_review_state"] for item in evaluations] == [
        "proposed", "accepted",
    ]
    assert asset_calls == 2
    assert search_calls == 1


def test_mapping_append_recomputes_complete_snapshot_under_store_lock(tmp_path):
    cfg, paths = _project(tmp_path)
    proposal = _proposal(cfg)
    terminology = json.loads(paths["terminology"].read_text(encoding="utf-8"))
    terminology["terms"][0]["definition"] += " Changed before append."
    _write_json(paths["terminology"], terminology)
    with pytest.raises(SemanticError, match="semantic inputs are stale"):
        append_mapping(cfg, proposal)


def test_atomic_append_never_replaces_a_racing_destination(tmp_path, monkeypatch):
    cfg, _paths = _project(tmp_path)
    proposal = _proposal(cfg)
    original_link = semantics.os.link
    competitor = b"competitor-won-the-create-race\n"

    def raced_link(source, destination):
        Path(destination).write_bytes(competitor)
        return original_link(source, destination)

    monkeypatch.setattr(semantics.os, "link", raced_link)
    with pytest.raises(SemanticError, match="refusing to overwrite"):
        append_mapping(cfg, proposal)
    destination = cfg.semantic_mappings_path / (proposal["id"].rsplit(":", 1)[1] + ".json")
    assert destination.read_bytes() == competitor


def test_disagreeing_current_accepted_mappings_conflict(tmp_path):
    cfg, _paths = _project(tmp_path, duplicate_memory=True)
    first = _proposal(cfg, relation="skos:exactMatch",
                      iri="https://example.org/onto/MemoryMeasurement")
    second = _proposal(cfg, relation="skos:closeMatch",
                       iri="https://example.org/onto/MemoryScore")
    first = create_mapping_review(
        first, "accepted", actor="reviewer:a", recorded_at=REVIEW_TIME)
    second = create_mapping_review(
        second, "accepted", actor="reviewer:b", recorded_at=REVIEW_TIME)
    conflicts = detect_mapping_conflicts([first, second])
    assert conflicts[first["id"]] == [second["id"]]
    assert evaluate_mapping(
        cfg, first, conflict_ids=conflicts[first["id"]])[
            "eligible_for_policy"] is False


def test_conflicting_lock_identity_and_aggregate_term_budget_fail_closed(
        tmp_path, monkeypatch):
    cfg, paths = _project(tmp_path)
    other = tmp_path / "other-ontology"
    other.mkdir()
    other_raw = other / "ontology.ttl"
    other_index = other / "index.json"
    other_lock_path = other / "ontology.lock.json"
    other_raw.write_bytes(paths["raw"].read_bytes() + b"# different exact bytes\n")
    other_index.write_bytes(paths["index"].read_bytes())
    other_lock = create_ontology_lock(
        ontology_id="example:ontology",
        ontology_iri="https://example.org/onto/",
        version="2026-07-15",
        version_iri="https://example.org/onto/releases/2026-07-15",
        license_iri="https://creativecommons.org/publicdomain/zero/1.0/",
        declared_imports_available=True,
        documents=[other_raw], index_path=other_index, base=other,
    )
    _write_json(other_lock_path, other_lock)
    cfg.semantic_ontology_lock_paths.append(other_lock_path)
    with pytest.raises(SemanticError, match="conflicting configured ontology locks"):
        configured_semantic_assets(cfg)

    cfg.semantic_ontology_lock_paths = [paths["lock"]]
    monkeypatch.setattr(semantics, "MAX_CONFIGURED_TERMS", 1)
    with pytest.raises(SemanticError, match="term count exceeds"):
        configured_semantic_assets(cfg)


def test_policy_compiles_only_explicit_current_accepted_nonstale_leaves(tmp_path):
    cfg, paths = _project(tmp_path)
    proposal = _proposal(cfg)
    append_mapping(cfg, proposal)
    with pytest.raises(SemanticError, match="not accepted"):
        create_semantic_policy(
            cfg, [proposal["id"]], actor="owner:human", note="Initial mapping policy.",
            recorded_at=REVIEW_TIME,
        )
    accepted, _path = append_mapping_review(
        cfg, proposal["id"], "accepted", actor="reviewer:human", recorded_at=REVIEW_TIME)
    policy = create_semantic_policy(
        cfg, [accepted["id"]], actor="owner:human", note="Initial mapping policy.",
        recorded_at="2026-07-15T01:02:00.000Z",
    )
    append_semantic_policy(cfg, policy)
    evaluation = evaluate_semantic_policy(cfg, policy)
    assert evaluation["valid"] is True
    assert evaluation["selected"] is False
    assert evaluation["active"] is False
    cfg.semantic_active_policy = policy["id"]
    evaluation = evaluate_semantic_policy(cfg, policy)
    assert evaluation["active"] is True
    assert evaluation["active_mapping_ids"] == [accepted["id"]]

    terminology = json.loads(paths["terminology"].read_text(encoding="utf-8"))
    terminology["version"] = "2.0.0"
    _write_json(paths["terminology"], terminology)
    evaluation = evaluate_semantic_policy(cfg, policy)
    assert evaluation["valid"] is False
    assert evaluation["active"] is False


def test_store_corruption_fails_closed_and_blocks_append(tmp_path):
    cfg, _paths = _project(tmp_path)
    proposal = _proposal(cfg)
    append_mapping(cfg, proposal)
    (cfg.semantic_mappings_path / "unexpected.txt").write_text("x", encoding="utf-8")
    documents, issues = load_mappings(cfg)
    assert documents
    assert any(item["code"] == "SEMANTIC_MAPPING_INTEGRITY" for item in issues)
    with pytest.raises(SemanticError, match="integrity failed"):
        append_mapping(cfg, _proposal(cfg))


def _write_cli_config(tmp_path):
    _write_json(tmp_path / "provsleuth" / "graph.json", {
        "schema_version": "1.0", "concepts": {}, "nodes": [], "edges": [],
    })
    config_path = tmp_path / "provsleuth.config.json"
    _write_json(config_path, {
        "root": ".",
        "graph": "provsleuth/graph.json",
        "semantics": {
            "terminologies": ["provsleuth/semantics/local-terms.json"],
            "ontology_locks": [
                "provsleuth/semantics/ontology/ontology.lock.json",
            ],
            "mappings": "provsleuth/semantics/mappings",
            "policies": "provsleuth/semantics/policies",
            "active_policy": None,
            "require_active_policy": False,
        },
    })
    return config_path


def test_cli_mapping_review_release_and_explicit_activation_round_trip(tmp_path, capsys):
    cfg, paths = _project(tmp_path)
    config_path = _write_cli_config(tmp_path)
    common = ["--config", str(config_path)]

    proposal_path = tmp_path / "mapping-request.json"
    _write_json(proposal_path, {
        "terminology_id": "study:terms",
        "term_id": "study:memory-score",
        "agent_input": _agent_input(cfg),
    })
    assert main([*common, "map-term", str(proposal_path),
                 "--actor", "agent:test", "--json"]) == 0
    proposal = json.loads(capsys.readouterr().out)
    assert proposal["current_derived"]["effective_review_state"] == "proposed"

    assert main([*common, "review-mapping", proposal["id"], "--state", "accepted",
                 "--actor", "agent:test", "--json"]) == 2
    assert "different actor" in capsys.readouterr().err
    assert main([*common, "review-mapping", proposal["id"], "--state", "accepted",
                 "--actor", "reviewer:human", "--json"]) == 0
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["current_derived"]["eligible_for_policy"] is True

    assert main([*common, "mappings"]) == 0
    mapping_text = capsys.readouterr().out
    assert "stale: no" in mapping_text
    assert "eligible for policy: yes" in mapping_text

    release_path = tmp_path / "semantic-release.json"
    _write_json(release_path, {
        "mapping_ids": [accepted["id"]],
        "note": "Reviewed semantic normalization for this analysis.",
    })
    assert main([*common, "compile-semantic-policy", str(release_path),
                 "--actor", "owner:human", "--json"]) == 0
    release = json.loads(capsys.readouterr().out)
    assert release["evaluation"]["valid"] is True
    assert release["evaluation"]["active"] is False
    assert release["activation_instruction"]["automatic"] is False

    assert main([*common, "semantic-status", "--policy", release["id"], "--json"]) == 0
    explicitly_checked = json.loads(capsys.readouterr().out)
    checked_release = next(
        item for item in explicitly_checked["policies"]["items"]
        if item["policy"]["id"] == release["id"]
    )
    assert checked_release["evaluation"]["valid"] is True
    assert checked_release["evaluation"]["active"] is False

    assert main([*common, "semantic-status", "--json"]) == 0
    inactive = json.loads(capsys.readouterr().out)
    assert inactive["active_policy"]["configured"] is False

    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["semantics"]["active_policy"] = release["id"]
    _write_json(config_path, config)
    assert main([*common, "semantic-status", "--json"]) == 0
    active = json.loads(capsys.readouterr().out)
    assert active["active_policy"]["evaluation"]["active"] is True

    terminology = json.loads(paths["terminology"].read_text(encoding="utf-8"))
    terminology["version"] = "2.0.0"
    _write_json(paths["terminology"], terminology)
    assert main([*common, "semantic-status", "--json"]) == 2
    stale = json.loads(capsys.readouterr().out)
    assert stale["active_policy"]["evaluation"]["active"] is False


def test_cli_map_term_reuses_candidate_search_profile_overrides(tmp_path, capsys):
    cfg, _paths = _project(tmp_path)
    config_path = _write_cli_config(tmp_path)
    common = ["--config", str(config_path)]
    candidates = search_ontology_candidates(cfg, "memory score", limit=10)
    request = tmp_path / "override-mapping.json"
    agent_input = _agent_input(cfg)
    agent_input["candidate_set_id"] = candidates["id"]
    _write_json(request, {
        "terminology_id": "study:terms",
        "term_id": "study:memory-score",
        "agent_input": agent_input,
    })

    assert main([*common, "map-term", str(request),
                 "--actor", "agent:test", "--json"]) == 2
    assert "candidate_set_id does not match" in capsys.readouterr().err
    assert main([*common, "map-term", str(request), "--limit", "10",
                 "--actor", "agent:test", "--json"]) == 0


def test_cli_semantic_status_blocks_inactive_stale_mapping(tmp_path, capsys):
    cfg, paths = _project(tmp_path)
    config_path = _write_cli_config(tmp_path)
    proposal = _proposal(cfg)
    append_mapping(cfg, proposal)
    append_mapping_review(
        cfg, proposal["id"], "accepted", actor="reviewer:human",
        recorded_at=REVIEW_TIME,
    )
    terminology = json.loads(paths["terminology"].read_text(encoding="utf-8"))
    terminology["version"] = "2.0.0"
    _write_json(paths["terminology"], terminology)

    assert main([
        "--config", str(config_path), "semantic-status", "--json",
    ]) == 2
    status = json.loads(capsys.readouterr().out)
    assert status["ok"] is False
    assert any(item["code"] == "SEMANTIC_MAPPING_STALE"
               for item in status["findings"])


def test_cli_compile_policy_returns_nonzero_when_record_is_not_activatable(
        tmp_path, capsys, monkeypatch):
    cfg, _paths = _project(tmp_path)
    config_path = _write_cli_config(tmp_path)
    proposal = _proposal(cfg)
    append_mapping(cfg, proposal)
    accepted, _path = append_mapping_review(
        cfg, proposal["id"], "accepted", actor="reviewer:human",
        recorded_at=REVIEW_TIME,
    )
    request = tmp_path / "policy.json"
    _write_json(request, {"mapping_ids": [accepted["id"]], "note": "Release."})
    real_evaluate = provsleuth_cli.evaluate_semantic_policy

    def invalid_after_record(config, policy):
        result = real_evaluate(config, policy)
        return {
            **result, "valid": False, "active": False, "active_mapping_ids": [],
            "findings": [*result["findings"], {
                "code": "SEMANTIC_POLICY_TEST_INVALIDATED", "severity": "error",
                "detail": "semantic inputs changed immediately after publication",
            }],
        }

    monkeypatch.setattr(provsleuth_cli, "evaluate_semantic_policy", invalid_after_record)
    assert main([
        "--config", str(config_path), "compile-semantic-policy", str(request),
        "--actor", "owner:human", "--json",
    ]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["evaluation"]["valid"] is False
    assert Path(output["path"]).exists()


def test_cli_semantic_status_suppresses_mixed_asset_snapshot(
        tmp_path, capsys, monkeypatch):
    cfg, paths = _project(tmp_path)
    config_path = _write_cli_config(tmp_path)
    proposal = _proposal(cfg)
    append_mapping(cfg, proposal)
    accepted, _path = append_mapping_review(
        cfg, proposal["id"], "accepted", actor="reviewer:human",
        recorded_at=REVIEW_TIME,
    )
    policy = create_semantic_policy(
        cfg, [accepted["id"]], actor="owner:human", note="Active release.",
        recorded_at="2026-07-15T01:02:00.000Z",
    )
    append_semantic_policy(cfg, policy)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["semantics"]["active_policy"] = policy["id"]
    _write_json(config_path, config)
    real_active = provsleuth_cli._evaluate_active_semantic_policy_from_snapshot
    mutated = False

    def mutate_after_active(configured, **kwargs):
        nonlocal mutated
        result = real_active(configured, **kwargs)
        terminology = json.loads(paths["terminology"].read_text(encoding="utf-8"))
        terminology["version"] = "changed-during-status"
        _write_json(paths["terminology"], terminology)
        mutated = True
        return result

    monkeypatch.setattr(
        provsleuth_cli, "_evaluate_active_semantic_policy_from_snapshot",
        mutate_after_active,
    )
    assert main([
        "--config", str(config_path), "semantic-status", "--json",
    ]) == 2
    status = json.loads(capsys.readouterr().out)
    assert mutated is True
    assert status["active_policy"]["evaluation"]["active"] is False
    assert any(item["code"] == "SEMANTIC_STATUS_SNAPSHOT_CHANGED"
               for item in status["findings"])


def test_init_scaffolds_explicit_semantic_ontology_budget(tmp_path, capsys):
    project = tmp_path / "new-project"
    assert main(["init", str(project)]) == 0
    capsys.readouterr()
    config = json.loads(
        (project / "provsleuth.config.json").read_text(encoding="utf-8")
    )
    assert config["semantics"]["max_ontology_bytes"] == 536870912


def test_store_path_swap_after_config_load_cannot_escape_project(tmp_path):
    _fixture_cfg, _paths = _project(tmp_path)
    config_path = _write_cli_config(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["semantics"]["mappings"] = "stores/mappings"
    _write_json(config_path, raw)
    cfg = Config(config_path)
    proposal = create_mapping_proposal(
        cfg, "study:terms", "study:memory-score", _agent_input(cfg),
        actor="agent:test", recorded_at=FIXED_TIME,
    )
    external = tmp_path.parent / (tmp_path.name + "-external-semantic-store")
    external.mkdir()
    store_parent = tmp_path / "stores"
    try:
        os.symlink(external, store_parent, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        if os.name != "nt":
            pytest.skip(f"directory links are unavailable on this platform: {exc}")
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(store_parent), str(external)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if created.returncode != 0:
            pytest.skip(f"directory links and junctions are unavailable: {exc}")

    try:
        with pytest.raises(SemanticError, match="link or reparse point"):
            append_mapping(cfg, proposal)
        assert list(external.iterdir()) == []
    finally:
        if store_parent.is_symlink():
            store_parent.unlink()
        else:
            os.rmdir(store_parent)


def test_concurrent_cross_process_reviews_leave_one_linear_successor(tmp_path):
    _fixture_cfg, _paths = _project(tmp_path)
    config_path = _write_cli_config(tmp_path)
    cfg = Config(config_path)
    proposal = create_mapping_proposal(
        cfg, "study:terms", "study:memory-score", _agent_input(cfg),
        actor="agent:test", recorded_at=FIXED_TIME,
    )
    append_mapping(cfg, proposal)
    context = mp.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_process_mapping_review,
            args=(str(config_path), proposal["id"], state, start, results),
        )
        for state in ("accepted", "rejected")
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    outcomes = [results.get(timeout=5) for _ in processes]

    assert sum(item[0] == "ok" for item in outcomes) == 1
    assert sum(item[0] == "SemanticError" for item in outcomes) == 1
    documents, issues = load_mappings(cfg)
    assert issues == []
    assert len(current_mapping_leaves(documents)) == 1


def test_store_lock_timeout_includes_in_process_wait(tmp_path):
    process_lock = threading.Lock()
    process_lock.acquire()
    started = time.monotonic()
    try:
        with pytest.raises(SemanticError, match="timed out waiting"):
            with semantics._store_lock(
                    tmp_path / "mapping-store", process_lock,
                    "semantic mapping test", timeout=0.05):
                pass
    finally:
        process_lock.release()

    assert time.monotonic() - started < 0.5


def test_plain_candidate_output_escapes_terminal_controls(tmp_path, capsys):
    _cfg, paths = _project(tmp_path)
    index = json.loads(paths["index"].read_text(encoding="utf-8"))
    index["terms"][0]["labels"][0]["text"] = (
        "\x1b]8;;https://attacker.invalid\x07\x9bmemory score"
    )
    _write_json(paths["index"], index)
    lock = create_ontology_lock(
        ontology_id="example:ontology",
        ontology_iri="https://example.org/onto/",
        version="2026-07-15",
        version_iri="https://example.org/onto/releases/2026-07-15",
        license_iri="https://creativecommons.org/publicdomain/zero/1.0/",
        imports=[],
        declared_imports_available=True,
        documents=[paths["raw"]],
        index_path=paths["index"],
        base=paths["lock"].parent,
    )
    _write_json(paths["lock"], lock)
    config_path = _write_cli_config(tmp_path)

    assert main([
        "--config", str(config_path), "ontology-candidates", "recall measure",
    ]) == 0
    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "\x07" not in output
    assert "\x9b" not in output
    assert "\\u001b]8;;https://attacker.invalid\\u0007\\u009bmemory score" in output

    assert main([
        "--config", str(config_path), "ontology-candidates", "recall measure",
        "--json",
    ]) == 0
    json_output = capsys.readouterr().out
    assert "\x9b" not in json_output
    assert "\\u009bmemory score" in json_output
    json.loads(json_output)


def test_cli_rejects_unhashable_mapping_tokens_without_traceback(tmp_path, capsys):
    cfg, _paths = _project(tmp_path)
    config_path = _write_cli_config(tmp_path)
    request = tmp_path / "malformed-mapping.json"
    agent_input = _agent_input(cfg)
    agent_input["relation"] = {}
    _write_json(request, {
        "terminology_id": "study:terms",
        "term_id": "study:memory-score",
        "agent_input": agent_input,
    })
    assert main([
        "--config", str(config_path), "map-term", str(request),
        "--actor", "agent:test",
    ]) == 2
    captured = capsys.readouterr()
    assert "allowed SKOS mapping relation" in captured.err
    assert "Traceback" not in captured.err


def test_cli_ontology_lock_uses_honest_index_and_import_assertions(tmp_path, capsys):
    _cfg, paths = _project(tmp_path)
    config_path = _write_cli_config(tmp_path)
    request = paths["lock"].parent / "lock-request.json"
    _write_json(request, {
        "ontology_id": "example:ontology",
        "ontology_iri": "https://example.org/onto/",
        "version": "2026-07-15",
        "version_iri": "https://example.org/onto/releases/2026-07-15",
        "license_iri": "https://creativecommons.org/publicdomain/zero/1.0/",
        "documents": ["ontology.ttl"],
        "index": "index.json",
        "imports": [],
        "declared_imports_available": True,
    })
    output = paths["lock"]
    output.unlink()
    assert main([
        "--config", str(config_path), "lock-ontology", str(request),
        "--output", str(output), "--json",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["lock"]["declared_imports_available"] is True
    assert result["lock"]["index"]["assertion"] == (
        "project-supplied-index-not-verified-extraction"
    )

    unconfigured = paths["lock"].parent / "not-configured.lock.json"
    assert main([
        "--config", str(config_path), "lock-ontology", str(request),
        "--output", str(unconfigured), "--json",
    ]) == 2
    assert "must exactly match" in capsys.readouterr().err
    assert not unconfigured.exists()


def test_ontology_lock_publication_is_atomic_create_only(tmp_path, monkeypatch):
    destination = tmp_path / "ontology.lock.json"
    original_link = provsleuth_cli.os.link

    def raced_link(source, target):
        Path(target).write_text("competitor\n", encoding="utf-8")
        return original_link(source, target)

    monkeypatch.setattr(provsleuth_cli.os, "link", raced_link)
    with pytest.raises(SemanticError, match="refusing to overwrite"):
        provsleuth_cli._atomic_create_text(destination, "intended\n")
    assert destination.read_text(encoding="utf-8") == "competitor\n"


def test_ontology_bundle_parses_the_exact_index_bytes_it_hashed(tmp_path, monkeypatch):
    _cfg, paths = _project(tmp_path)
    original_bytes = paths["index"].read_bytes()
    changed = json.loads(original_bytes.decode("utf-8"))
    changed["terms"][0]["definitions"][0]["text"] = "Raced definition."
    original_reader = semantics._safe_file_bytes
    mutated = False

    def read_then_mutate(path, label, limit):
        nonlocal mutated
        data = original_reader(path, label, limit)
        if label == "ontology index" and not mutated:
            mutated = True
            _write_json(paths["index"], changed)
        return data

    monkeypatch.setattr(semantics, "_safe_file_bytes", read_then_mutate)
    lock, index = load_ontology_bundle(paths["lock"])

    assert mutated is True
    assert lock["index"]["sha256"] == hashlib.sha256(original_bytes).hexdigest()
    memory = next(
        item for item in index["terms"]
        if item["iri"] == "https://example.org/onto/MemoryScore"
    )
    assert memory["definitions"][0]["text"] == "A score measuring memory."
    assert hashlib.sha256(paths["index"].read_bytes()).hexdigest() != lock["index"]["sha256"]


def test_ontology_bundle_rejects_aggregate_budget_before_hashing(tmp_path, monkeypatch):
    _cfg, paths = _project(tmp_path)
    called = False

    def unexpected_hash(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("hashing should not start after aggregate preflight failure")

    monkeypatch.setattr(semantics, "_snapshot_file", unexpected_hash)
    with pytest.raises(SemanticError, match="aggregate byte limit"):
        load_ontology_bundle(paths["lock"], max_total_bytes=1)
    assert called is False


def test_ontology_bundle_rejects_actual_size_before_hashing_malformed_pin(
        tmp_path, monkeypatch):
    _cfg, paths = _project(tmp_path)
    lock = json.loads(paths["lock"].read_text(encoding="utf-8"))
    lock["documents"][0]["size"] = 1
    core = {key: value for key, value in lock.items() if key != "id"}
    lock["id"] = semantics._ontology_lock_id(core)
    _write_json(paths["lock"], lock)
    called = False

    def unexpected_hash(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("hashing should not start after actual-size preflight failure")

    monkeypatch.setattr(semantics, "_snapshot_file", unexpected_hash)
    with pytest.raises(SemanticError, match="bytes drifted"):
        load_ontology_bundle(paths["lock"])
    assert called is False


def test_lock_creation_enforces_preflight_size_as_hash_limit(tmp_path, monkeypatch):
    ontology = tmp_path / "ontology"
    ontology.mkdir()
    raw = ontology / "raw.owl"
    raw.write_bytes(b"x")
    index = ontology / "index.json"
    _write_json(index, {
        "schema_version": "claimtrace.ontology-index/1",
        "ontology_id": "example:ontology",
        "version": "1",
        "terms": [],
    })
    real_snapshot = semantics._snapshot_file
    observed_limits = []

    def grow_then_snapshot(path, label, *, limit=semantics.MAX_LOCK_DOCUMENT_BYTES):
        observed_limits.append(limit)
        if Path(path) == raw:
            raw.write_bytes(b"xy")
        return real_snapshot(path, label, limit=limit)

    monkeypatch.setattr(semantics, "_snapshot_file", grow_then_snapshot)
    with pytest.raises(SemanticError, match="byte limit"):
        create_ontology_lock(
            ontology_id="example:ontology",
            ontology_iri="https://example.org/ontology",
            version="1",
            documents=[raw],
            index_path=index,
            base=ontology,
        )
    assert observed_limits == [1]


def test_mapping_store_detects_entry_added_during_enumeration(tmp_path, monkeypatch):
    cfg, _paths = _project(tmp_path)
    proposal = _proposal(cfg)
    append_mapping(cfg, proposal)
    accepted, _path = append_mapping_review(
        cfg, proposal["id"], "accepted", actor="reviewer:human",
        recorded_at=REVIEW_TIME,
    )
    contested = create_mapping_review(
        accepted, "contested", actor="reviewer:second",
        recorded_at="2026-07-15T01:02:00.000Z",
    )
    destination = cfg.semantic_mappings_path / (contested["id"].rsplit(":", 1)[1] + ".json")
    original_reader = semantics._safe_file_bytes
    injected = False

    def read_then_append(path, label, limit):
        nonlocal injected
        data = original_reader(path, label, limit)
        if label == "semantic mapping document" and not injected:
            injected = True
            _write_json(destination, contested)
        return data

    monkeypatch.setattr(semantics, "_safe_file_bytes", read_then_append)
    _documents, issues = load_mappings(cfg)
    assert injected is True
    assert any("changed while it was being read" in item["detail"] for item in issues)


def test_old_unicode_candidate_profile_is_valid_history_but_live_stale(tmp_path):
    cfg, _paths = _project(tmp_path)
    historical = copy.deepcopy(_proposal(cfg))
    candidate_set = historical["mechanical_snapshot"]["candidate_set"]
    candidate_set["unicode_data_version"] = "0.0.0"
    candidate_core = {key: value for key, value in candidate_set.items() if key != "id"}
    candidate_set["id"] = "candidates:sha256:" + semantics.canonical_sha256(candidate_core)
    historical["agent_input"]["candidate_set_id"] = candidate_set["id"]
    historical["derived"] = semantics._derive_mapping(
        historical["mechanical_snapshot"], historical["agent_input"], historical["review"],
    )
    mapping_core = {key: value for key, value in historical.items() if key != "id"}
    historical["id"] = semantics._mapping_id(mapping_core)

    validate_mapping_document(historical)
    evaluation = evaluate_mapping(cfg, historical)
    assert evaluation["stale"] is True
    assert evaluation["eligible_for_policy"] is False
    with pytest.raises(SemanticError, match="older Unicode normalization profile"):
        create_mapping_review(historical, "accepted", actor="reviewer:human")


def test_current_profile_rejects_noncanonical_actor_alias(tmp_path):
    cfg, _paths = _project(tmp_path)
    malformed = copy.deepcopy(_proposal(cfg))
    malformed["review"]["actor"] = "e\u0301"
    with pytest.raises(SemanticError, match="actor is not canonical"):
        validate_mapping_document(malformed)


def test_policy_evaluation_requires_release_to_remain_in_store(tmp_path):
    cfg, _paths = _project(tmp_path)
    proposal = _proposal(cfg)
    append_mapping(cfg, proposal)
    accepted, _path = append_mapping_review(
        cfg, proposal["id"], "accepted", actor="reviewer:human",
        recorded_at=REVIEW_TIME,
    )
    policy = create_semantic_policy(
        cfg, [accepted["id"]], actor="owner:human", note="Stored release.",
        recorded_at="2026-07-15T01:02:00.000Z",
    )
    path = append_semantic_policy(cfg, policy)
    path.unlink()
    evaluation = evaluate_semantic_policy(cfg, policy)
    assert evaluation["valid"] is False
    assert any(item["code"] == "SEMANTIC_POLICY_UNAVAILABLE"
               for item in evaluation["findings"])


def test_historical_unicode_policy_is_readable_but_cannot_activate(tmp_path):
    cfg, _paths = _project(tmp_path)
    proposal = _proposal(cfg)
    append_mapping(cfg, proposal)
    accepted, _path = append_mapping_review(
        cfg, proposal["id"], "accepted", actor="reviewer:human",
        recorded_at=REVIEW_TIME,
    )
    historical = create_semantic_policy(
        cfg, [accepted["id"]], actor="owner:human", note="Historical profile.",
        recorded_at="2026-07-15T01:02:00.000Z",
    )
    historical["unicode_data_version"] = "0.0.0"
    core = {key: value for key, value in historical.items() if key != "id"}
    historical["id"] = semantics._policy_id(core)
    validate_semantic_policy = semantics.validate_semantic_policy
    validate_semantic_policy(historical)

    with pytest.raises(SemanticError, match="Unicode normalization profile"):
        append_semantic_policy(cfg, historical)

    cfg.semantic_policies_path.mkdir(parents=True, exist_ok=True)
    stored = cfg.semantic_policies_path / (historical["id"].rsplit(":", 1)[1] + ".json")
    _write_json(stored, historical)
    cfg.semantic_active_policy = historical["id"]
    active = semantics.evaluate_active_semantic_policy(cfg)
    assert active["configured"] is True
    assert active["evaluation"]["active"] is False
    assert any(item["code"] == "SEMANTIC_POLICY_UNICODE_PROFILE"
               for item in active["findings"])


def test_batch_candidate_lookup_scans_terms_once_for_unique_queries(tmp_path, monkeypatch):
    cfg, _paths = _project(tmp_path)
    memory = _proposal(cfg)
    attention = create_mapping_proposal(
        cfg, "study:terms", "study:attention-score",
        _agent_input(cfg, query="attention score"),
        actor="agent:test", recorded_at=FIXED_TIME,
    )
    calls = 0
    original = semantics._candidate_from_term

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(semantics, "_candidate_from_term", counted)
    evaluations = evaluate_mappings(cfg, [memory, attention])
    assert len(evaluations) == 2
    assert calls == 2


def test_batch_candidate_variants_share_a_bounded_evaluation_budget(
        tmp_path, monkeypatch):
    cfg, _paths = _project(tmp_path)
    assets = configured_semantic_assets(cfg)
    monkeypatch.setattr(semantics, "MAX_BATCH_LOOKUP_REFERENCES", 1)

    with pytest.raises(SemanticError, match="candidate-evaluation work limit"):
        semantics._build_batch_candidate_lookup(
            assets["ontology_locks"], assets["ontology_indexes"],
            ["memory score", " Memory   Score "],
        )


def test_batch_language_limit_profiles_share_a_bounded_work_budget(
        tmp_path, monkeypatch):
    cfg, _paths = _project(tmp_path)
    assets = configured_semantic_assets(cfg)
    lookup = semantics._build_batch_candidate_lookup(
        assets["ontology_locks"], assets["ontology_indexes"], ["memory score"],
    )
    monkeypatch.setattr(semantics, "MAX_BATCH_LOOKUP_REFERENCES", 1)

    with pytest.raises(SemanticError, match="candidate-profile work limit"):
        semantics._bound_candidate_evaluation_profiles(lookup, {
            ("memory score", "en", 25),
            ("memory score", None, 25),
        })


def test_configured_asset_raw_bytes_have_an_aggregate_preparse_budget(
        tmp_path, monkeypatch):
    cfg, paths = _project(tmp_path)
    invalid_second = tmp_path / "provsleuth" / "semantics" / "second.json"
    invalid_second.write_text(" " * 100, encoding="utf-8")
    cfg.semantic_terminology_paths.append(invalid_second)
    monkeypatch.setattr(
        semantics, "MAX_CONFIGURED_ASSET_BYTES", paths["terminology"].stat().st_size + 50,
    )
    with pytest.raises(SemanticError, match="aggregate byte limit"):
        configured_semantic_assets(cfg)


def test_configured_terminology_cannot_grow_past_preflight_size(
        tmp_path, monkeypatch):
    cfg, paths = _project(tmp_path)
    original_size = paths["terminology"].stat().st_size
    real_loader = semantics.load_local_terminology
    observed_limit = None

    def grow_then_load(source, *, max_bytes=semantics.MAX_ASSET_BYTES):
        nonlocal observed_limit
        observed_limit = max_bytes
        with Path(source).open("ab") as stream:
            stream.write(b" ")
        return real_loader(source, max_bytes=max_bytes)

    monkeypatch.setattr(semantics, "load_local_terminology", grow_then_load)
    with pytest.raises(SemanticError, match="byte limit"):
        configured_semantic_assets(cfg)
    assert observed_limit == original_size


def test_configured_ontology_document_count_has_an_aggregate_limit(
        tmp_path, monkeypatch):
    cfg, paths = _project(tmp_path)
    second_lock = paths["lock"].with_name("second.lock.json")
    second_lock.write_bytes(paths["lock"].read_bytes())
    cfg.semantic_ontology_lock_paths.append(second_lock)
    monkeypatch.setattr(semantics, "MAX_CONFIGURED_ONTOLOGY_DOCUMENTS", 1)

    with pytest.raises(SemanticError, match="document count exceeds the aggregate"):
        configured_semantic_assets(cfg)


def test_semantic_reader_detects_descriptor_ctime_change(tmp_path, monkeypatch):
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    real_fstat = semantics.os.fstat
    calls = 0

    def changed_ctime(descriptor):
        nonlocal calls
        calls += 1
        value = real_fstat(descriptor)
        if calls == 2:
            class Changed:
                pass
            changed = Changed()
            for name in (
                    "st_dev", "st_ino", "st_mode", "st_size", "st_mtime",
                    "st_mtime_ns", "st_ctime", "st_ctime_ns"):
                setattr(changed, name, getattr(value, name))
            changed.st_ctime_ns += 1
            return changed
        return value

    monkeypatch.setattr(semantics.os, "fstat", changed_ctime)
    with pytest.raises(SemanticError, match="changed while it was being read"):
        semantics.stable_semantic_file_bytes(source, "source", 10)


def test_cli_malformed_lock_paths_fail_without_traceback(tmp_path, capsys):
    _cfg, paths = _project(tmp_path)
    config_path = _write_cli_config(tmp_path)
    request = tmp_path / "malformed-lock-request.json"
    _write_json(request, {
        "ontology_id": "example:ontology",
        "ontology_iri": "https://example.org/onto/",
        "version": "2026-07-15",
        "documents": [{}],
        "index": "index.json",
    })
    paths["lock"].unlink()
    assert main([
        "--config", str(config_path), "lock-ontology", str(request),
        "--output", str(paths["lock"]),
    ]) == 2
    captured = capsys.readouterr()
    assert "local file path" in captured.err
    assert "Traceback" not in captured.err
