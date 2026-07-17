"""Tests for strict and machine-readable graph plus receipt reports."""
import json
import sys

import provsleuth.assessment as assessment_module
import provsleuth.report as report_module
import pytest
from provsleuth.assessment import (LEGACY_SCHEMA_VERSION, SCHEMA_VERSION,
                                   SUPPORTED_SCHEMA_VERSIONS, append_assessment,
                                   create_assessment, transition_review)
from provsleuth.cli import main
from provsleuth.config import Config
from provsleuth.events import run_command
from provsleuth.report import build_report, dumps_report
from provsleuth.semantics import (append_mapping, append_mapping_review,
                                  append_semantic_policy, create_mapping_proposal,
                                  create_ontology_lock, create_semantic_policy,
                                  search_ontology_candidates)


def _project(tmp_path, nodes, edges=()):
    trace = tmp_path / "provsleuth"
    trace.mkdir(parents=True)
    config = tmp_path / "provsleuth.config.json"
    config.write_text(json.dumps({
        "root": ".",
        "graph": "provsleuth/graph.json",
        "events": "provsleuth/events",
        "render_types": ["figure"],
        "input_types": ["data", "artifact", "code"],
        "run_output_types": ["artifact"],
    }), encoding="utf-8")
    (trace / "graph.json").write_text(json.dumps({
        "schema_version": "1.0", "concepts": {},
        "nodes": list(nodes), "edges": list(edges),
    }), encoding="utf-8")
    return Config(config)


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, indent=2) + "\n")


def _configure_semantics(cfg, semantics):
    config = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    config["semantics"] = semantics
    _write_json(cfg.config_path, config)
    return Config(cfg.config_path)


def _semantic_project(tmp_path):
    (tmp_path / "data.txt").write_text("x", encoding="utf-8")
    cfg = _project(tmp_path, [
        {"id": "data:x", "type": "data", "status": "current", "path": "data.txt"},
    ])
    semantic_root = tmp_path / "provsleuth" / "semantics"
    ontology_root = semantic_root / "ontology"
    terminology_path = semantic_root / "local-terms.json"
    raw_path = ontology_root / "ontology.ttl"
    index_path = ontology_root / "index.json"
    lock_path = ontology_root / "ontology.lock.json"
    _write_json(terminology_path, {
        "schema_version": "claimtrace.local-terminology/1",
        "id": "study:terms",
        "version": "1.0.0",
        "terms": [{
            "id": "study:memory-score",
            "kind": "concept",
            "label": "Memory score",
            "definition": "Score produced by the declared memory assessment.",
            "aliases": ["recall score"],
        }],
    })
    ontology_root.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(b"@prefix ex: <https://example.org/onto/> .\n")
    _write_json(index_path, {
        "schema_version": "claimtrace.ontology-index/1",
        "ontology_id": "example:ontology",
        "version": "2026-07-15",
        "terms": [{
            "iri": "https://example.org/onto/MemoryScore",
            "kind": "class",
            "labels": [{"text": "memory score", "language": "en"}],
            "synonyms": [],
            "definitions": [{
                "text": "A score measuring memory.", "language": "en",
            }],
            "deprecated": False,
            "parents": [],
        }],
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
    cfg = _configure_semantics(cfg, {
        "terminologies": ["provsleuth/semantics/local-terms.json"],
        "ontology_locks": ["provsleuth/semantics/ontology/ontology.lock.json"],
        "mappings": "provsleuth/semantics/mappings",
        "policies": "provsleuth/semantics/policies",
        "active_policy": None,
        "allow_external_sources": False,
        "require_active_policy": False,
        "language": "en",
        "max_candidates": 25,
    })
    return cfg, {
        "raw": raw_path,
        "mappings": cfg.semantic_mappings_path,
        "policies": cfg.semantic_policies_path,
    }


def _mapping_input(cfg):
    candidates = search_ontology_candidates(cfg, "memory score")
    candidate = candidates["candidates"][0]
    return {
        "relation": "skos:closeMatch",
        "target": {
            "ontology_lock_id": candidate["ontology_lock_id"],
            "iri": candidate["iri"],
        },
        "candidate_query": "memory score",
        "candidate_set_id": candidates["id"],
        "rationale": "The terms overlap, but the project score retains local context.",
        "limitations": ["The ontology does not encode the project instrument."],
        "provenance": {"agent": "agent:test", "model": "fixture"},
    }


def _activate_semantic_policy(cfg):
    proposal = create_mapping_proposal(
        cfg, "study:terms", "study:memory-score", _mapping_input(cfg),
        actor="agent:test", recorded_at="2026-07-15T01:00:00.000Z",
    )
    append_mapping(cfg, proposal)
    accepted, _path = append_mapping_review(
        cfg, proposal["id"], "accepted", actor="reviewer:human",
        recorded_at="2026-07-15T01:01:00.000Z",
    )
    policy = create_semantic_policy(
        cfg, [accepted["id"]], actor="owner:human", note="Reviewed mapping release.",
        recorded_at="2026-07-15T01:02:00.000Z",
    )
    append_semantic_policy(cfg, policy)
    config = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    config["semantics"]["active_policy"] = policy["id"]
    _write_json(cfg.config_path, config)
    return Config(cfg.config_path), proposal, accepted, policy


def _set_run_ids(cfg, node_id, run_ids):
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    next(node for node in graph["nodes"] if node["id"] == node_id)["run_ids"] = run_ids
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")


def _semantic_input(verdict="supports_as_written", *, causal_mismatch=False):
    claim_level = "causal" if causal_mismatch else "associational"
    result_level = "associational"
    common_frame = {
        "population": "study sample",
        "exposure": "measured exposure",
        "comparator": None,
        "outcome": "memory",
        "direction": "positive",
        "magnitude": "slope 0.41",
        "time_scope": "single study",
    }
    value = {
        "verdict": verdict,
        "claim_frame": {**common_frame, "inference_level": claim_level},
        "result_frame": {**common_frame, "inference_level": result_level},
        "alignment": {
            "population": "match", "exposure": "match", "comparator": "not_applicable",
            "outcome": "match", "direction": "match", "magnitude": "match",
            "time_scope": "match",
            "inference_level": "mismatch" if causal_mismatch else "match",
        },
        "evidence_anchors": [{
            "result_id": "exp:fit", "kind": "json_pointer", "pointer": "/slope",
            "expected_value": 0.41,
        }],
        "rationale": "The recorded estimate and declared scope were compared.",
        "limitations": ["The interpretation is limited to the recorded analysis."],
        "provenance": {"agent": "test-reviewer", "model": "fixture"},
    }
    if verdict == "supports_narrower_claim":
        value["recommended_claim"] = "Exposure was associated with memory."
    return value


def _contradiction_input():
    value = _semantic_input("contradicts_as_written")
    value["result_frame"]["direction"] = "negative"
    value["alignment"]["direction"] = "mismatch"
    return value


def _as_legacy_v1(document):
    legacy = json.loads(json.dumps(document))
    legacy["schema_version"] = LEGACY_SCHEMA_VERSION
    legacy["derived"] = assessment_module._derive(
        legacy["mechanical_snapshot"], legacy["agent_input"], legacy["review"],
        schema_version=LEGACY_SCHEMA_VERSION,
    )
    core = {key: value for key, value in legacy.items() if key != "id"}
    legacy["id"] = assessment_module._assessment_id(core)
    return legacy


def test_bare_check_keeps_human_output(tmp_path, capsys):
    (tmp_path / "data.txt").write_text("x", encoding="utf-8")
    cfg = _project(tmp_path, [
        {"id": "data:x", "type": "data", "status": "current", "path": "data.txt"},
    ])
    assert main(["--config", str(cfg.config_path), "check"]) == 0
    captured = capsys.readouterr()
    assert "provsleuth check: OK" in captured.out
    assert captured.err == ""


def test_no_receipt_is_nonblocking_normally_and_blocking_strict(tmp_path):
    (tmp_path / "out.txt").write_text("x", encoding="utf-8")
    cfg = _project(tmp_path, [
        {"id": "artifact:x", "type": "artifact", "status": "current", "path": "out.txt"},
    ])
    normal = build_report(cfg, strict=False)
    strict = build_report(cfg, strict=True)
    finding = next(item for item in normal["findings"] if item["code"] == "NO_RUN_RECEIPT")
    assert normal["ok"] is True
    assert finding["blocking"] is False
    assert strict["ok"] is False
    assert next(item for item in strict["findings"] if item["code"] == "NO_RUN_RECEIPT")["blocking"] is True


def test_successful_unchanged_output_receipt_is_validation_only(tmp_path):
    (tmp_path / "out.txt").write_text("stable", encoding="utf-8")
    cfg = _project(tmp_path, [
        {"id": "artifact:x", "type": "artifact", "status": "current", "path": "out.txt"},
    ])
    result = run_command(
        cfg, [sys.executable, "-c", "pass"],
        inputs=[], outputs=["out.txt"], no_inputs=True, cwd=str(tmp_path),
    )
    assert result["output_transitions"][0]["produced"] is False
    report = build_report(cfg, strict=True)
    binding = report["receipts"]["runs"][0]["bindings"][0]
    assert binding["binding_kind"] == "output_validation"
    assert binding["output_evidence"] == "unchanged_not_proven_produced"
    finding = next(item for item in report["findings"] if item["code"] == "NO_RUN_RECEIPT")
    assert finding["blocking"] is True
    assert "producing run receipt" in finding["detail"]
    assert report["ok"] is False


def test_unchanged_validation_does_not_replace_earlier_producing_receipt(tmp_path):
    (tmp_path / "data.txt").write_text("v1", encoding="utf-8")
    cfg = _project(tmp_path, [
        {"id": "data:x", "type": "data", "status": "current", "path": "data.txt"},
        {"id": "artifact:x", "type": "artifact", "status": "current", "path": "out.txt"},
    ], [
        {"from": "data:x", "to": "artifact:x", "rel": "produces"},
    ])
    producer = run_command(
        cfg,
        [sys.executable, "-c",
         "from pathlib import Path; Path('out.txt').write_text(Path('data.txt').read_text())"],
        inputs=["data.txt"], outputs=["out.txt"], cwd=str(tmp_path),
    )
    (tmp_path / "data.txt").write_text("v2", encoding="utf-8")
    validation = run_command(
        cfg, [sys.executable, "-c", "pass"],
        inputs=["data.txt"], outputs=["out.txt"], cwd=str(tmp_path),
    )

    report = build_report(cfg, strict=True)
    runs = {run["run_id"]: run for run in report["receipts"]["runs"]}
    assert [(item["binding_kind"], item["output_evidence"])
            for item in runs[producer["run_id"]]["bindings"]] == [
        ("output_path", "content_transition_detected"),
    ]
    assert [(item["binding_kind"], item["output_evidence"])
            for item in runs[validation["run_id"]]["bindings"]] == [
        ("output_validation", "unchanged_not_proven_produced"),
    ]
    finding = next(item for item in report["findings"] if item["code"] == "RUN_INPUT_DRIFT")
    assert producer["run_id"] in finding["detail"]
    assert validation["run_id"] not in finding["detail"]
    assert report["ok"] is False


def test_receipt_binds_to_graph_and_compares_declarations(tmp_path):
    (tmp_path / "data.txt").write_text("data", encoding="utf-8")
    (tmp_path / "pipeline.py").write_text("# pinned pipeline", encoding="utf-8")
    nodes = [
        {"id": "data:x", "type": "data", "status": "current", "path": "data.txt"},
        {"id": "code:x", "type": "code", "status": "current", "path": "pipeline.py"},
        {"id": "artifact:x", "type": "artifact", "status": "current", "path": "out.txt"},
    ]
    edges = [
        {"from": "data:x", "to": "artifact:x", "rel": "produces"},
        {"from": "code:x", "to": "artifact:x", "rel": "produces"},
    ]
    cfg = _project(tmp_path, nodes, edges)
    command = [
        sys.executable, "-c",
        "from pathlib import Path; Path('out.txt').write_text(Path('data.txt').read_text())",
    ]
    result = run_command(
        cfg, command,
        inputs=["data.txt", "pipeline.py"], outputs=["out.txt"], cwd=str(tmp_path),
    )
    assert result["exit_code"] == 0

    report = build_report(cfg)
    run = report["receipts"]["runs"][0]
    assert run["bindings"][0]["node_id"] == "artifact:x"
    assert run["bindings"][0]["declaration_comparison"] == "declarations_agree"
    codes = {item["code"] for item in report["findings"]}
    assert "NO_RUN_RECEIPT" not in codes
    assert "PARTIAL_LINEAGE_COVERAGE" not in codes
    assert run["lineage_coverage"]["overall"] == "partial"
    assert report["scope"]["runtime_observation"] == "partial_reads_and_write_causation_not_observed"
    assert report["ok"] is True


def test_compact_receipt_keeps_materialized_intermediates_separate_from_outputs():
    run_id = "run:12345678-1234-4123-8123-123456789abc"
    snapshot = {
        "path": "data/clean.csv", "state": "stable", "sha256": "a" * 64,
        "file_version_id": "file:sha256:" + "a" * 64, "size": 12,
    }
    raw_run = {
        "run_id": run_id,
        "start": {
            "schema_version": "claimtrace.event/3",
            "id": "event:sha256:" + "1" * 64,
            "recorded_at": "2026-07-15T01:00:00Z",
            "payload": {"name": "fit", "plan_id": "plan:fixture", "plan": {
                "declared_inputs": [{"path": "data/raw.csv"}],
                "declared_outputs": ["results/fit.json"],
                "declared_intermediates": ["data/clean.csv"],
            }},
        },
        "finish": {
            "schema_version": "claimtrace.event/3",
            "id": "event:sha256:" + "2" * 64,
            "recorded_at": "2026-07-15T01:00:01Z",
            "payload": {
                "outcome": "succeeded", "output_transitions": [],
                "input_transitions": [],
                "intermediate_transitions": [{
                    "path": "data/clean.csv", "transition": "created",
                    "produced": True,
                    "before": {"path": "data/clean.csv", "state": "missing"},
                    "after": snapshot,
                }],
            },
        },
    }

    compact = report_module._compact_run(raw_run)

    assert compact["declared_outputs"] == ["results/fit.json"]
    assert compact["declared_intermediates"] == ["data/clean.csv"]
    assert compact["output_transitions"] == []
    assert compact["intermediate_transitions"] == [{
        "path": "data/clean.csv", "transition": "created", "produced": True,
        "before": {"path": "data/clean.csv", "state": "missing"},
        "after": snapshot,
    }]


def test_legacy_contract_state_resolves_with_its_stored_snapshot_schema(
        tmp_path, monkeypatch):
    cfg = _project(tmp_path, [])
    stored = {
        "schema_version": "claimtrace.pipeline-contract-snapshot/1",
        "id": "pipeline-contract:sha256:" + "a" * 64,
        "source": {"path": "provsleuth/legacy.pipeline.json"},
    }
    start = {
        "schema_version": "claimtrace.event/2",
        "payload": {"plan": {
            "pipeline_contract": stored,
            "declared_inputs": [], "declared_outputs": [],
            "parameters": {}, "seeds": {},
        }},
    }
    seen = {}
    monkeypatch.setattr(report_module, "validate_pipeline_snapshot", lambda value: value["id"])
    monkeypatch.setattr(
        report_module, "pipeline_snapshots_equivalent",
        lambda stored, current: stored["id"] == current["id"],
    )

    def resolve(_cfg, path, **kwargs):
        seen.update({"path": path, **kwargs})
        return {"id": stored["id"]}

    monkeypatch.setattr(report_module, "resolve_pipeline_contract", resolve)

    assert report_module._pipeline_state(cfg, start) == ("current", None)
    assert seen["snapshot_schema"] == "claimtrace.pipeline-contract-snapshot/1"


def test_null_result_can_explicitly_link_to_successful_mechanical_run(tmp_path):
    cfg = _project(tmp_path, [
        {"id": "artifact:x", "type": "artifact", "status": "current", "path": "out.txt"},
        {"id": "exp:null", "type": "experiment", "status": "null", "run_ids": []},
    ])
    result = run_command(
        cfg,
        [sys.executable, "-c", "from pathlib import Path; Path('out.txt').write_text('no effect')"],
        inputs=[], outputs=["out.txt"], no_inputs=True, cwd=str(tmp_path),
    )
    _set_run_ids(cfg, "exp:null", [result["run_id"]])
    report = build_report(cfg, strict=True)
    run = report["receipts"]["runs"][0]
    bindings = {(item["node_id"], item["binding_kind"]) for item in run["bindings"]}
    assert ("artifact:x", "output_path") in bindings
    assert ("exp:null", "explicit_run_reference") in bindings
    assert not any(item["code"] == "SEMANTIC_RUN_OUTCOME_MISMATCH"
                   for item in report["findings"])
    assert report["ok"] is True


def test_missing_or_failed_semantic_run_reference_is_hard_error(tmp_path):
    cfg = _project(tmp_path, [
        {"id": "exp:confirmed", "type": "experiment", "status": "confirmed", "run_ids": []},
        {"id": "exp:missing", "type": "experiment", "status": "null",
         "run_ids": ["run:12345678-1234-4123-8123-123456789abc"]},
    ])
    failed = run_command(
        cfg, [sys.executable, "-c", "raise SystemExit(7)"],
        inputs=[], outputs=[], no_inputs=True, no_outputs=True, cwd=str(tmp_path),
    )
    _set_run_ids(cfg, "exp:confirmed", [failed["run_id"]])
    report = build_report(cfg)
    codes = {item["code"] for item in report["findings"] if item["severity"] == "error"}
    assert "MISSING_RUN_REFERENCE" in codes
    assert "SEMANTIC_RUN_OUTCOME_MISMATCH" in codes
    assert report["ok"] is False


def test_declared_vs_graph_mismatch_is_explicit(tmp_path):
    (tmp_path / "data.txt").write_text("data", encoding="utf-8")
    (tmp_path / "pipeline.py").write_text("# pipeline", encoding="utf-8")
    cfg = _project(tmp_path, [
        {"id": "data:x", "type": "data", "status": "current", "path": "data.txt"},
        {"id": "code:x", "type": "code", "status": "current", "path": "pipeline.py"},
        {"id": "artifact:x", "type": "artifact", "status": "current", "path": "out.txt"},
    ], [
        {"from": "data:x", "to": "artifact:x", "rel": "produces"},
        {"from": "code:x", "to": "artifact:x", "rel": "produces"},
    ])
    result = run_command(
        cfg,
        [sys.executable, "-c", "from pathlib import Path; Path('out.txt').write_text('x')"],
        inputs=["data.txt"], outputs=["out.txt"], cwd=str(tmp_path),
    )
    assert result["exit_code"] == 0
    report = build_report(cfg)
    finding = next(item for item in report["findings"] if item["code"] == "RUN_DECLARATION_INCOMPLETE")
    assert finding["node_id"] == "artifact:x"
    assert "pipeline.py" in finding["detail"]


def test_output_drift_from_latest_successful_receipt_is_hard_error(tmp_path):
    cfg = _project(tmp_path, [
        {"id": "artifact:x", "type": "artifact", "status": "current", "path": "out.txt"},
    ])
    run_command(
        cfg,
        [sys.executable, "-c", "from pathlib import Path; Path('out.txt').write_text('v1')"],
        inputs=[], outputs=["out.txt"], no_inputs=True, cwd=str(tmp_path),
    )
    (tmp_path / "out.txt").write_text("v2", encoding="utf-8")
    report = build_report(cfg)
    assert report["ok"] is False
    assert any(item["code"] == "RUN_OUTPUT_DRIFT" and item["blocking"] for item in report["findings"])


def test_input_drift_from_latest_successful_receipt_is_hard_error(tmp_path):
    (tmp_path / "data.txt").write_text("v1", encoding="utf-8")
    cfg = _project(tmp_path, [
        {"id": "data:x", "type": "data", "status": "current", "path": "data.txt"},
        {"id": "artifact:x", "type": "artifact", "status": "current", "path": "out.txt"},
    ], [
        {"from": "data:x", "to": "artifact:x", "rel": "produces"},
    ])
    run_command(
        cfg,
        [sys.executable, "-c",
         "from pathlib import Path; Path('out.txt').write_text(Path('data.txt').read_text())"],
        inputs=["data.txt"], outputs=["out.txt"], cwd=str(tmp_path),
    )
    (tmp_path / "data.txt").write_text("v2", encoding="utf-8")

    report = build_report(cfg, strict=True)
    run = report["receipts"]["runs"][0]
    assert run["input_transitions"][0]["transition"] == "unchanged"
    assert report["ok"] is False
    assert any(item["code"] == "RUN_INPUT_DRIFT" and item["blocking"]
               for item in report["findings"])


def test_control_plane_contract_failure_is_a_hard_report_error(tmp_path):
    cfg = _project(tmp_path, [])
    graph_rel = cfg.graph_path.relative_to(tmp_path).as_posix()
    result = run_command(
        cfg,
        [sys.executable, "-c",
         ("from pathlib import Path; p=Path(" + repr(graph_rel) + "); "
          "p.write_text(p.read_text(encoding='utf-8') + ' ', encoding='utf-8')")],
        inputs=[], outputs=[], no_inputs=True, no_outputs=True,
        cwd=str(tmp_path), scan_writes=False,
    )
    assert result["outcome"] == "contract_failed"

    report = build_report(cfg, strict=True)
    finding = next(item for item in report["findings"]
                   if item["code"] == "RUN_CONTROL_PLANE_MUTATION")
    assert finding["severity"] == "error"
    assert finding["blocking"] is True
    assert result["run_id"] in finding["detail"]
    assert report["ok"] is False


def test_possible_undeclared_output_uses_unattributed_wording(tmp_path):
    cfg = _project(tmp_path, [
        {"id": "artifact:x", "type": "artifact", "status": "current", "path": "out.txt"},
    ])
    run_command(
        cfg,
        [sys.executable, "-c",
         "from pathlib import Path; Path('out.txt').write_text('ok'); Path('side.txt').write_text('side')"],
        inputs=[], outputs=["out.txt"], no_inputs=True, cwd=str(tmp_path),
    )
    report = build_report(cfg)
    finding = next(item for item in report["findings"] if item["code"] == "POSSIBLE_UNDECLARED_OUTPUT")
    assert "unattributed pre/post window" in finding["detail"]
    assert "side.txt" in finding["detail"]


def test_json_report_is_one_deterministic_document(tmp_path, capsys):
    (tmp_path / "data.txt").write_text("µ", encoding="utf-8")
    cfg = _project(tmp_path, [
        {"id": "data:x", "type": "data", "status": "current", "path": "data.txt", "note": "µ"},
    ])
    first = dumps_report(build_report(cfg))
    second = dumps_report(build_report(cfg))
    assert first == second
    assert first.endswith("\n") and not first.endswith("\n\n")

    assert main(["--config", str(cfg.config_path), "check", "--json"]) == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["report_schema_version"] == "1.7"
    assert parsed["semantics"]["mapping_history"] == []
    assert parsed["semantics"]["active_policy"]["configured"] is False
    assert parsed["fatal"] is None
    assert captured.err == ""


def test_semantic_report_separates_review_history_from_explicit_activation(tmp_path):
    cfg, _paths = _semantic_project(tmp_path)
    proposal = create_mapping_proposal(
        cfg, "study:terms", "study:memory-score", _mapping_input(cfg),
        actor="agent:test", recorded_at="2026-07-15T01:00:00.000Z",
    )
    append_mapping(cfg, proposal)

    proposed_report = build_report(cfg, strict=True)
    semantics = proposed_report["semantics"]
    assert semantics["integrity"] == "ok"
    assert semantics["mapping_history"][0]["stored_derived"][
        "snapshot_eligible_for_policy"] is False
    assert semantics["current_mapping_evaluations"][0]["current_derived"][
        "eligible_for_policy"] is False
    assert semantics["active_policy"] == {
        "configured": False, "configured_policy_id": None,
        "policy": None, "evaluation": None, "findings": [],
    }
    assert semantics["active_mapping_ids"] == []
    pending = next(
        item for item in proposed_report["findings"]
        if item["code"] == "SEMANTIC_MAPPING_REVIEW_PENDING"
    )
    assert pending["blocking"] is True

    accepted, _path = append_mapping_review(
        cfg, proposal["id"], "accepted", actor="reviewer:human",
        recorded_at="2026-07-15T01:01:00.000Z",
    )
    policy = create_semantic_policy(
        cfg, [accepted["id"]], actor="owner:human", note="Reviewed mapping release.",
        recorded_at="2026-07-15T01:02:00.000Z",
    )
    append_semantic_policy(cfg, policy)
    config = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    config["semantics"]["active_policy"] = policy["id"]
    _write_json(cfg.config_path, config)
    cfg = Config(cfg.config_path)

    report = build_report(cfg, strict=True)
    semantics = report["semantics"]
    assert report["ok"] is True
    assert semantics["integrity"] == "ok"
    assert [item["id"] for item in semantics["mapping_history"]] == [
        proposal["id"], accepted["id"],
    ]
    assert semantics["current_mapping_evaluations"] == [{
        "mapping_id": accepted["id"],
        "subject": accepted["subject"],
        "review": accepted["review"],
        "current_derived": {
            "effective_review_state": "accepted",
            "eligible_for_policy": True,
            "stale": False,
            "findings": [],
        },
    }]
    assert semantics["policies"] == [policy]
    assert semantics["active_policy"]["evaluation"]["active"] is True
    assert semantics["active_mapping_ids"] == [accepted["id"]]
    assert report["scope"]["semantic_normalization"] == (
        "attributed_mapping_review_states_under_pinned_local_snapshots_not_support"
    )
    assert report["assessments"]["active_relations"] == []
    assert report["derivations"]["active_proofs"] == []


def test_semantic_report_suppresses_activation_when_store_snapshot_changes(
        tmp_path, monkeypatch):
    cfg, _paths = _semantic_project(tmp_path)
    cfg, _proposal, _accepted, _policy = _activate_semantic_policy(cfg)
    real_load_mappings = report_module.load_mappings
    calls = 0

    def changed_on_final_read(config):
        nonlocal calls
        calls += 1
        documents, issues = real_load_mappings(config)
        if calls == 2:
            issues = [*issues, {
                "code": "SEMANTIC_MAPPING_INTEGRITY",
                "path": "mappings",
                "detail": "synthetic concurrent change",
            }]
        return documents, issues

    monkeypatch.setattr(report_module, "load_mappings", changed_on_final_read)
    report = build_report(cfg, strict=False)

    assert calls == 2
    assert report["semantics"]["integrity"] == "error"
    assert report["semantics"]["active_mapping_ids"] == []
    assert report["semantics"]["active_policy"]["evaluation"]["active"] is False
    assert any(
        item["code"] == "SEMANTIC_REPORT_SNAPSHOT_CHANGED"
        for item in report["findings"]
    )


def test_semantic_report_suppresses_activation_when_assets_change(
        tmp_path, monkeypatch):
    cfg, paths = _semantic_project(tmp_path)
    cfg, _proposal, _accepted, _policy = _activate_semantic_policy(cfg)
    real_evaluate_active = report_module._evaluate_active_semantic_policy_from_snapshot
    mutated = False

    def mutate_after_active_evaluation(config, **kwargs):
        nonlocal mutated
        result = real_evaluate_active(config, **kwargs)
        terminology_path = config.semantic_terminology_paths[0]
        terminology = json.loads(terminology_path.read_text(encoding="utf-8"))
        terminology["version"] = "changed-during-report"
        _write_json(terminology_path, terminology)
        mutated = True
        return result

    monkeypatch.setattr(
        report_module, "_evaluate_active_semantic_policy_from_snapshot",
        mutate_after_active_evaluation,
    )
    report = build_report(cfg, strict=False)

    assert mutated is True
    assert report["semantics"]["integrity"] == "error"
    assert report["semantics"]["active_mapping_ids"] == []
    assert report["semantics"]["active_policy"]["evaluation"]["active"] is False
    finding = next(
        item for item in report["findings"]
        if item["code"] == "SEMANTIC_REPORT_SNAPSHOT_CHANGED"
    )
    assert "semantic assets changed" in finding["detail"]


@pytest.mark.parametrize(("surface", "expected_code"), [
    ("ontology", "SEMANTIC_ASSET_INTEGRITY"),
    ("mapping_store", "SEMANTIC_MAPPING_INTEGRITY"),
    ("policy_store", "SEMANTIC_POLICY_INTEGRITY"),
])
def test_semantic_report_fails_closed_on_corrupt_assets_and_stores(
        tmp_path, surface, expected_code):
    cfg, paths = _semantic_project(tmp_path)
    cfg, _proposal, _accepted, _policy = _activate_semantic_policy(cfg)
    if surface == "ontology":
        paths["raw"].write_bytes(paths["raw"].read_bytes() + b"# drift\n")
    elif surface == "mapping_store":
        paths["mappings"].mkdir(parents=True, exist_ok=True)
        (paths["mappings"] / "unexpected.txt").write_text("x", encoding="utf-8")
    else:
        paths["policies"].mkdir(parents=True, exist_ok=True)
        (paths["policies"] / "unexpected.txt").write_text("x", encoding="utf-8")

    report = build_report(cfg, strict=False)
    assert report["ok"] is False
    assert report["semantics"]["integrity"] == "error"
    assert report["semantics"]["active_mapping_ids"] == []
    assert report["semantics"]["active_policy"]["evaluation"] is None or (
        report["semantics"]["active_policy"]["evaluation"]["active"] is False
    )
    finding = next(item for item in report["findings"]
                   if item["code"] == expected_code)
    assert finding["severity"] == "error"
    assert finding["blocking"] is True


def test_required_semantic_policy_is_advisory_until_strict(tmp_path):
    (tmp_path / "data.txt").write_text("x", encoding="utf-8")
    cfg = _project(tmp_path, [
        {"id": "data:x", "type": "data", "status": "current", "path": "data.txt"},
    ])
    cfg = _configure_semantics(cfg, {"require_active_policy": True})

    normal = build_report(cfg, strict=False)
    strict = build_report(cfg, strict=True)
    normal_finding = next(item for item in normal["findings"]
                          if item["code"] == "MISSING_ACTIVE_SEMANTIC_POLICY")
    strict_finding = next(item for item in strict["findings"]
                          if item["code"] == "MISSING_ACTIVE_SEMANTIC_POLICY")
    assert normal["semantics"]["policy"]["require_active_policy"] is True
    assert normal_finding["severity"] == "warning"
    assert normal_finding["blocking"] is False
    assert normal["ok"] is True
    assert strict_finding["blocking"] is True
    assert strict["ok"] is False


def test_malformed_graph_gets_fatal_json_envelope(tmp_path, capsys):
    cfg = _project(tmp_path, [])
    cfg.graph_path.write_text("{ broken", encoding="utf-8")
    assert main(["--config", str(cfg.config_path), "check", "--strict", "--json"]) == 2
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["fatal"]["code"] == "GRAPH_ERROR"
    assert parsed["receipts"] is None
    assert parsed["semantics"] is None
    assert parsed["derivations"] is None
    assert parsed["exit_code"] == 2
    assert captured.err == ""


def test_corrupt_event_is_a_blocking_integrity_finding(tmp_path):
    cfg = _project(tmp_path, [])
    corrupt = cfg.events_path / "aa" / ("0" * 64 + ".json")
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("{broken", encoding="utf-8")
    report = build_report(cfg)
    finding = next(item for item in report["findings"] if item["code"] == "EVENT_INTEGRITY")
    assert finding["severity"] == "error"
    assert finding["blocking"] is True
    assert report["ok"] is False


def test_strict_check_does_not_execute_verifiers(tmp_path):
    cfg = _project(tmp_path, [])
    sentinel = tmp_path / "verifier-ran"
    verifier = tmp_path / "provsleuth" / "verifiers.py"
    verifier.write_text(
        "from pathlib import Path\nPath(r'" + str(sentinel) + "').touch()\n",
        encoding="utf-8",
    )
    cfg.data["verifiers"] = "provsleuth/verifiers.py"
    assert build_report(cfg, strict=True)["exit_code"] == 0
    assert not sentinel.exists()


def test_declared_support_is_visibly_unassessed_and_policy_can_require_review(tmp_path):
    (tmp_path / "result.json").write_text(json.dumps({"slope": 0.41}), encoding="utf-8")
    nodes = [
        {"id": "exp:fit", "type": "experiment", "status": "current", "path": "result.json"},
        {"id": "claim:memory", "type": "claim", "status": "current", "value": "Exposure is associated with memory."},
    ]
    edges = [{"from": "exp:fit", "to": "claim:memory", "rel": "supports"}]
    cfg = _project(tmp_path, nodes, edges)
    finding = next(
        item for item in build_report(cfg, strict=True)["findings"]
        if item["code"] == "UNASSESSED_CLAIM_LINK"
    )
    assert finding["severity"] == "info"
    assert finding["blocking"] is False

    config = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    config["require_assessments"] = True
    cfg.config_path.write_text(json.dumps(config), encoding="utf-8")
    required = build_report(Config(cfg.config_path), strict=True)
    finding = next(item for item in required["findings"]
                   if item["code"] == "UNASSESSED_CLAIM_LINK")
    assert finding["severity"] == "warning"
    assert finding["blocking"] is True


def test_structural_claim_dependency_is_visibly_unassessed_and_policy_can_require_review(tmp_path):
    (tmp_path / "result.json").write_text(json.dumps({"slope": 0.41}), encoding="utf-8")
    nodes = [
        {"id": "exp:fit", "type": "experiment", "status": "current", "path": "result.json"},
        {"id": "claim:memory", "type": "claim", "status": "current",
         "value": "Exposure is associated with memory."},
    ]
    edges = [{"from": "exp:fit", "to": "claim:memory", "rel": "derives_from"}]
    cfg = _project(tmp_path, nodes, edges)

    advisory = build_report(cfg, strict=True)
    finding = next(item for item in advisory["findings"]
                   if item["code"] == "UNASSESSED_CLAIM_DEPENDENCY")
    assert finding["severity"] == "info"
    assert finding["blocking"] is False
    assert advisory["assessments"]["required_dependencies"] == [{
        "from": "exp:fit",
        "to": "claim:memory",
        "declared_relation": "derives_from",
        "status": "unassessed",
        "assessment_ids": [],
        "assessed_relations": [],
    }]

    config = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    config["require_assessments"] = True
    cfg.config_path.write_text(json.dumps(config), encoding="utf-8")
    required = build_report(Config(cfg.config_path), strict=True)
    finding = next(item for item in required["findings"]
                   if item["code"] == "UNASSESSED_CLAIM_DEPENDENCY")
    assert finding["severity"] == "warning"
    assert finding["blocking"] is True
    assert required["ok"] is False


def test_accepted_assessment_covers_structural_claim_dependency(tmp_path):
    (tmp_path / "result.json").write_text(json.dumps({"slope": 0.41}), encoding="utf-8")
    cfg = _project(tmp_path, [
        {"id": "exp:fit", "type": "experiment", "status": "current", "path": "result.json"},
        {"id": "claim:memory", "type": "claim", "status": "current",
         "value": "Exposure is associated with memory."},
    ], [{"from": "exp:fit", "to": "claim:memory", "rel": "derives_from"}])
    config = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    config["require_assessments"] = True
    cfg.config_path.write_text(json.dumps(config), encoding="utf-8")
    cfg = Config(cfg.config_path)

    proposal = create_assessment(
        cfg, "claim:memory", ["exp:fit"], _semantic_input(), actor="agent:test",
        recorded_at="2026-07-13T08:00:00.000Z",
    )
    append_assessment(cfg, proposal)
    accepted = transition_review(
        cfg, proposal, "accepted", actor="scientist:1", assessments=[proposal],
        recorded_at="2026-07-13T08:01:00.000Z",
    )
    append_assessment(cfg, accepted)

    report = build_report(cfg, strict=True)
    assert report["ok"] is False
    assert report["claim_basis"]["items"][0]["overall"] == (
        "semantic_only_not_execution_grounded"
    )
    assert any(
        item["code"] == "CLAIM_EXECUTION_BASIS_INCOMPLETE"
        for item in report["findings"]
    )
    assert report["assessments"]["required_dependencies"] == [{
        "from": "exp:fit",
        "to": "claim:memory",
        "declared_relation": "derives_from",
        "status": "covered",
        "assessment_ids": [accepted["id"]],
        "assessed_relations": ["supports"],
    }]


def test_accepted_assessment_covers_declared_support_link(tmp_path):
    (tmp_path / "result.json").write_text(json.dumps({"slope": 0.41}), encoding="utf-8")
    cfg = _project(tmp_path, [
        {"id": "exp:fit", "type": "experiment", "status": "current", "path": "result.json"},
        {"id": "claim:memory", "type": "claim", "status": "current", "value": "Exposure is associated with memory."},
    ], [{"from": "exp:fit", "to": "claim:memory", "rel": "supports"}])
    proposal = create_assessment(
        cfg, "claim:memory", ["exp:fit"], _semantic_input(), actor="agent:test",
        recorded_at="2026-07-13T08:00:00.000Z",
    )
    append_assessment(cfg, proposal)
    accepted = transition_review(
        cfg, proposal, "accepted", actor="scientist:1", assessments=[proposal],
        recorded_at="2026-07-13T08:01:00.000Z",
    )
    append_assessment(cfg, accepted)

    report = build_report(cfg, strict=False)
    assert not any(item["code"] == "UNASSESSED_CLAIM_LINK" for item in report["findings"])
    assert report["assessments"]["active_relations"] == [{
        "assessment_id": accepted["id"],
        "from": "exp:fit", "to": "claim:memory", "rel": "supports",
    }]
    assert report["assessments"]["declared_links"] == [{
        "from": "exp:fit",
        "to": "claim:memory",
        "declared_relation": "supports",
        "status": "covered",
        "assessment_ids": [accepted["id"]],
        "assessed_relations": ["supports"],
    }]
    current = [item for item in report["assessments"]["items"] if item["is_current"]]
    assert [item["id"] for item in current] == [accepted["id"]]


def test_report_projects_mixed_assessment_schemas_explicitly(tmp_path):
    (tmp_path / "result.json").write_text(json.dumps({"slope": 0.41}), encoding="utf-8")
    cfg = _project(tmp_path, [
        {"id": "exp:fit", "type": "experiment", "status": "current", "path": "result.json"},
        {"id": "claim:memory", "type": "claim", "status": "current",
         "value": "Exposure is associated with memory."},
    ])
    current = create_assessment(
        cfg, "claim:memory", ["exp:fit"], _semantic_input(), actor="agent:v2",
        recorded_at="2026-07-13T08:01:00.000Z",
    )
    legacy = _as_legacy_v1(create_assessment(
        cfg, "claim:memory", ["exp:fit"], _semantic_input(), actor="agent:v1",
        recorded_at="2026-07-13T08:00:00.000Z",
    ))
    append_assessment(cfg, legacy)
    append_assessment(cfg, current)

    assessments = build_report(cfg, strict=False)["assessments"]
    assert assessments["integrity"] == "ok"
    assert assessments["assessment_schema_version"] == SCHEMA_VERSION
    assert assessments["current_assessment_schema_version"] == SCHEMA_VERSION
    assert assessments["supported_assessment_schema_versions"] == sorted(
        SUPPORTED_SCHEMA_VERSIONS
    )
    assert {item["schema_version"] for item in assessments["items"]} == {
        LEGACY_SCHEMA_VERSION, SCHEMA_VERSION,
    }


def test_strict_report_activates_specific_result_for_qualitative_claim(tmp_path):
    cfg = _project(tmp_path, [
        {"id": "exp:fit", "type": "artifact", "status": "current", "path": "result.json"},
        {"id": "claim:memory", "type": "claim", "status": "current",
         "value": "Exposure is positively associated with memory."},
    ], [{"from": "exp:fit", "to": "claim:memory", "rel": "supports"}])
    run = run_command(
        cfg,
        [sys.executable, "-c",
         "from pathlib import Path; Path('result.json').write_text('{\"slope\": 0.41}')"],
        inputs=[], outputs=["result.json"], no_inputs=True, cwd=str(tmp_path),
    )
    assert run["exit_code"] == 0

    agent_input = _semantic_input()
    agent_input["claim_frame"]["magnitude"] = None
    agent_input["alignment"]["magnitude"] = "not_stated"
    proposal = create_assessment(
        cfg, "claim:memory", ["exp:fit"], agent_input, actor="agent:test",
        recorded_at="2026-07-13T08:00:00.000Z",
    )
    append_assessment(cfg, proposal)
    accepted = transition_review(
        cfg, proposal, "accepted", actor="scientist:1", assessments=[proposal],
        recorded_at="2026-07-13T08:01:00.000Z",
    )
    append_assessment(cfg, accepted)

    report = build_report(cfg, strict=True)
    assert report["ok"] is True
    assert report["assessments"]["active_relations"] == [{
        "assessment_id": accepted["id"],
        "from": "exp:fit", "to": "claim:memory", "rel": "supports",
    }]
    assert "CLAIM_ALIGNMENT_INCOMPLETE" not in {
        item["code"] for item in report["findings"]
    }


def test_proposed_narrowing_is_pending_and_stays_out_of_active_support(tmp_path):
    (tmp_path / "result.json").write_text(json.dumps({"slope": 0.41}), encoding="utf-8")
    cfg = _project(tmp_path, [
        {"id": "exp:fit", "type": "experiment", "status": "current", "path": "result.json"},
        {"id": "claim:memory", "type": "claim", "status": "current", "value": "Exposure causally improves memory."},
    ])
    proposal = create_assessment(
        cfg, "claim:memory", ["exp:fit"],
        _semantic_input("supports_narrower_claim", causal_mismatch=True),
        actor="agent:test", recorded_at="2026-07-13T08:00:00.000Z",
    )
    append_assessment(cfg, proposal)
    report = build_report(cfg, strict=True)
    assert report["assessments"]["active_relations"] == []
    pending = next(item for item in report["findings"]
                   if item["code"] == "ASSESSMENT_REVIEW_PENDING")
    assert pending["blocking"] is True
    mismatch = next(item for item in report["findings"]
                    if item["code"] == "CAUSAL_MODALITY_MISMATCH")
    assert mismatch["severity"] == "warning"
    assert mismatch["node_id"] == "claim:memory"


def test_accepted_assessment_becomes_stale_when_result_changes(tmp_path):
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"slope": 0.41}), encoding="utf-8")
    cfg = _project(tmp_path, [
        {"id": "exp:fit", "type": "experiment", "status": "current", "path": "result.json"},
        {"id": "claim:memory", "type": "claim", "status": "current", "value": "Exposure is associated with memory."},
    ])
    proposal = create_assessment(
        cfg, "claim:memory", ["exp:fit"], _semantic_input(), actor="agent:test",
        recorded_at="2026-07-13T08:00:00.000Z",
    )
    append_assessment(cfg, proposal)
    accepted = transition_review(
        cfg, proposal, "accepted", actor="scientist:1", assessments=[proposal],
        recorded_at="2026-07-13T08:01:00.000Z",
    )
    append_assessment(cfg, accepted)
    result.write_text(json.dumps({"slope": -0.2}), encoding="utf-8")

    report = build_report(cfg, strict=True)
    stale = next(item for item in report["findings"] if item["code"] == "ASSESSMENT_STALE")
    assert stale["severity"] == "error"
    assert stale["blocking"] is True
    assert stale["node_id"] == "claim:memory"
    assert report["assessments"]["active_relations"] == []


def test_corrupt_review_chain_cannot_activate_or_cover_support(tmp_path):
    (tmp_path / "result.json").write_text(json.dumps({"slope": 0.41}), encoding="utf-8")
    cfg = _project(tmp_path, [
        {"id": "exp:fit", "type": "experiment", "status": "current", "path": "result.json"},
        {"id": "claim:memory", "type": "claim", "status": "current",
         "value": "Exposure is associated with memory."},
    ], [{"from": "exp:fit", "to": "claim:memory", "rel": "supports"}])
    proposal = create_assessment(
        cfg, "claim:memory", ["exp:fit"], _semantic_input(), actor="agent:test",
        recorded_at="2026-07-13T08:00:00.000Z",
    )
    accepted = transition_review(
        cfg, proposal, "accepted", actor="scientist:1", assessments=[proposal],
        recorded_at="2026-07-13T08:01:00.000Z",
    )
    append_assessment(cfg, accepted)  # deliberately omit the predecessor

    report = build_report(cfg, strict=True)
    assert report["assessments"]["integrity"] == "error"
    assert report["assessments"]["active_relations"] == []
    assessment = report["assessments"]["items"][0]
    assert assessment["current_derived"]["active_relation"] is None
    assert assessment["stored_derived"]["active_relation"] == "supports"
    assert report["assessments"]["declared_links"][0]["status"] == "unassessed"
    assert {item["code"] for item in report["findings"]} >= {
        "ASSESSMENT_REVIEW_CHAIN", "UNASSESSED_CLAIM_LINK",
    }


def test_accepted_refutation_conflicts_with_declared_support(tmp_path):
    (tmp_path / "result.json").write_text(json.dumps({"slope": 0.41}), encoding="utf-8")
    cfg = _project(tmp_path, [
        {"id": "exp:fit", "type": "experiment", "status": "current", "path": "result.json"},
        {"id": "claim:memory", "type": "claim", "status": "current",
         "value": "Exposure increases memory."},
    ], [{"from": "exp:fit", "to": "claim:memory", "rel": "supports"}])
    proposal = create_assessment(
        cfg, "claim:memory", ["exp:fit"], _contradiction_input(), actor="agent:test",
        recorded_at="2026-07-13T08:00:00.000Z",
    )
    append_assessment(cfg, proposal)
    accepted = transition_review(
        cfg, proposal, "accepted", actor="scientist:1", assessments=[proposal],
        recorded_at="2026-07-13T08:01:00.000Z",
    )
    append_assessment(cfg, accepted)

    report = build_report(cfg, strict=False)
    conflict = next(
        item for item in report["findings"]
        if item["code"] == "DECLARED_CLAIM_LINK_CONFLICT"
    )
    assert conflict["severity"] == "error" and conflict["blocking"] is True
    assert not any(item["code"] == "UNASSESSED_CLAIM_LINK" for item in report["findings"])
    assert report["assessments"]["declared_links"][0]["status"] == "assessed_conflict"
    assert report["assessments"]["active_relations"][0]["rel"] == "refutes"


def test_unreviewed_conflicting_proposal_does_not_deactivate_accepted_support(tmp_path):
    (tmp_path / "result.json").write_text(json.dumps({"slope": 0.41}), encoding="utf-8")
    cfg = _project(tmp_path, [
        {"id": "exp:fit", "type": "experiment", "status": "current", "path": "result.json"},
        {"id": "claim:memory", "type": "claim", "status": "current",
         "value": "Exposure is associated with memory."},
    ], [{"from": "exp:fit", "to": "claim:memory", "rel": "supports"}])
    support = create_assessment(
        cfg, "claim:memory", ["exp:fit"], _semantic_input(), actor="agent:support",
        recorded_at="2026-07-13T08:00:00.000Z",
    )
    append_assessment(cfg, support)
    accepted = transition_review(
        cfg, support, "accepted", actor="scientist:1", assessments=[support],
        recorded_at="2026-07-13T08:01:00.000Z",
    )
    append_assessment(cfg, accepted)
    contrary = create_assessment(
        cfg, "claim:memory", ["exp:fit"], _contradiction_input(), actor="agent:contrary",
        recorded_at="2026-07-13T08:02:00.000Z",
    )
    append_assessment(cfg, contrary)

    report = build_report(cfg, strict=False)
    assert report["assessments"]["active_relations"] == [{
        "assessment_id": accepted["id"],
        "from": "exp:fit", "to": "claim:memory", "rel": "supports",
    }]
    assert report["assessments"]["declared_links"][0]["status"] == "covered"
    assert not any(item["code"] == "ASSESSMENT_CONTESTED" for item in report["findings"])
    assert any(item["code"] == "ASSESSMENT_REVIEW_PENDING" for item in report["findings"])


def test_accepted_negative_review_marks_declared_support_as_assessed_mismatch(tmp_path):
    (tmp_path / "result.json").write_text(json.dumps({"slope": 0.41}), encoding="utf-8")
    cfg = _project(tmp_path, [
        {"id": "exp:fit", "type": "experiment", "status": "current", "path": "result.json"},
        {"id": "claim:memory", "type": "claim", "status": "current",
         "value": "Exposure is associated with memory."},
    ], [{"from": "exp:fit", "to": "claim:memory", "rel": "supports"}])
    proposal = create_assessment(
        cfg, "claim:memory", ["exp:fit"], _semantic_input("insufficient"),
        actor="agent:test", recorded_at="2026-07-13T08:00:00.000Z",
    )
    append_assessment(cfg, proposal)
    accepted = transition_review(
        cfg, proposal, "accepted", actor="scientist:1", assessments=[proposal],
        recorded_at="2026-07-13T08:01:00.000Z",
    )
    append_assessment(cfg, accepted)
    report = build_report(cfg, strict=False)
    assert report["assessments"]["active_relations"] == []
    assert report["assessments"]["declared_links"][0]["status"] == "assessed_not_as_written"
    mismatch = next(
        item for item in report["findings"]
        if item["code"] == "ASSESSED_CLAIM_LINK_MISMATCH"
    )
    assert mismatch["severity"] == "info"
