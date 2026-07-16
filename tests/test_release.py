"""Focused tests for deterministic exact-byte project release manifests."""
import copy
import hashlib
import json
import sys

import pytest

import claimtrace.release as release
from claimtrace.config import Config
from claimtrace.events import CONTRACT_EVENT_SCHEMA, SUPPORTED_EVENT_SCHEMAS, run_command
from claimtrace.engine import METHOD_REQUIREMENTS_SCHEMA
from claimtrace.method_assessment import (
    SCHEMA_VERSION as METHOD_ASSESSMENT_SCHEMA,
    append_method_assessment,
    create_method_assessment,
)
from claimtrace.pipeline import (
    CONTRACT_SCHEMA as PIPELINE_CONTRACT_SCHEMA,
    SNAPSHOT_SCHEMA as PIPELINE_SNAPSHOT_SCHEMA,
    STAGE_CHECKPOINT_SCHEMA,
    STAGE_TRACE_PLAN_SCHEMA,
    STAGE_TRACE_SCHEMA,
    SUPPORTED_SNAPSHOT_SCHEMAS,
)
from claimtrace.replay import (
    REPLAY_SCHEMA,
    SUPPORTED_REPLAY_SCHEMAS,
    replay_run,
)
from test_pipeline import _project as _pipeline_project


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _project(tmp_path, *, with_assets=False):
    trace = tmp_path / "claimtrace"
    trace.mkdir()
    (tmp_path / "data.csv").write_text("x\n1\n", encoding="utf-8")
    (tmp_path / "analysis.py").write_text("print('analysis')\n", encoding="utf-8")
    (tmp_path / "result.json").write_text('{"estimate":1}\n', encoding="utf-8")
    graph = {
        "schema_version": "1.0",
        "concepts": {},
        "nodes": [
            {"id": "data:raw", "type": "data", "status": "current", "path": "data.csv"},
            {"id": "code:analysis", "type": "code", "status": "current", "path": "analysis.py"},
            {"id": "art:result", "type": "artifact", "status": "current", "path": "result.json"},
        ],
        "edges": [
            {"from": "code:analysis", "to": "data:raw", "rel": "reads"},
            {"from": "code:analysis", "to": "art:result", "rel": "produces"},
        ],
    }
    _write_json(trace / "graph.json", graph)
    config = {
        "root": ".",
        "graph": "claimtrace/graph.json",
        "events": "claimtrace/events",
        "assessments": "claimtrace/assessments",
    }
    if with_assets:
        terminology = {
            "schema_version": "claimtrace.local-terminology/1",
            "id": "study:terms",
            "version": "1.0.0",
            "terms": [{
                "id": "study:measure",
                "kind": "concept",
                "label": "Study measure",
                "definition": "The measure declared by this test project.",
                "aliases": [],
            }],
        }
        vocabulary = {
            "schema_version": "claimtrace.symbolic-vocabulary/1",
            "id": "study:vocabulary",
            "version": "1.0.0",
            "types": [],
            "units": [],
            "predicates": [
                {"id": "study:observed", "kind": "input", "arguments": [
                    {"name": "item", "type": "ct:symbol", "unit": None},
                ]},
                {"id": "study:derived", "kind": "derived", "arguments": [
                    {"name": "item", "type": "ct:symbol", "unit": None},
                ]},
            ],
            "renderers": [],
        }
        rules = {
            "schema_version": "claimtrace.symbolic-rules/1",
            "id": "study:rules",
            "version": "1.0.0",
            "vocabulary_id": "study:vocabulary",
            "rules": [{
                "id": "study:derive",
                "when": [{
                    "predicate": "study:observed",
                    "polarity": "positive",
                    "arguments": {"item": {"var": "item"}},
                }],
                "where": [],
                "then": {
                    "predicate": "study:derived",
                    "polarity": "positive",
                    "arguments": {"item": {"var": "item"}},
                },
            }],
        }
        _write_json(trace / "semantics" / "terms.json", terminology)
        _write_json(trace / "logic" / "vocabulary.json", vocabulary)
        _write_json(trace / "logic" / "rules.json", rules)
        config["semantics"] = {
            "terminologies": ["claimtrace/semantics/terms.json"],
        }
        config["logic"] = {
            "vocabularies": ["claimtrace/logic/vocabulary.json"],
            "rule_packs": ["claimtrace/logic/rules.json"],
        }
    _write_json(tmp_path / "claimtrace.config.json", config)
    return Config(tmp_path / "claimtrace.config.json")


def _file(manifest, path):
    return next(item for item in manifest["files"] if item["path"] == path)


def test_release_manifest_is_deterministic_and_covers_exact_project_bytes(tmp_path):
    cfg = _project(tmp_path, with_assets=True)

    first = release.create_release_manifest(cfg)
    second = release.create_release_manifest(cfg)

    assert first == second
    release.validate_release_manifest(first)
    assert first["schema_version"] == "claimtrace.project-release/1"
    assert first["release_id"].startswith("release:sha256:")
    assert _file(first, "claimtrace.config.json")["sha256"] == hashlib.sha256(
        (tmp_path / "claimtrace.config.json").read_bytes()
    ).hexdigest()
    assert _file(first, "claimtrace/graph.json")["roles"] == ["graph"]
    assert _file(first, "data.csv")["logical_ids"] == ["data:raw"]
    assert _file(first, "analysis.py")["logical_ids"] == ["code:analysis"]
    assert _file(first, "claimtrace/semantics/terms.json")["logical_ids"] == [
        "study:terms"
    ]
    assert _file(first, "claimtrace/logic/vocabulary.json")["logical_ids"] == [
        "study:vocabulary"
    ]
    assert _file(first, "claimtrace/logic/rules.json")["logical_ids"] == [
        "study:rules"
    ]
    assert any("cannot prove" in item for item in first["scope"]["limitations"])


def test_release_manifest_includes_content_addressed_event_files_and_ids(tmp_path):
    cfg = _project(tmp_path)
    result = run_command(
        cfg,
        [
            sys.executable, "-c",
            "from pathlib import Path; Path('result.json').write_text('{\"estimate\":2}\\n')",
        ],
        inputs=[], outputs=["result.json"], no_inputs=True,
        cwd=str(tmp_path), scan_writes=False,
    )
    assert result["outcome"] == "succeeded"

    manifest = release.create_release_manifest(cfg)
    event_files = [item for item in manifest["files"] if "event" in item["roles"]]

    assert len(event_files) == 2
    assert all(item["logical_ids"][0].startswith("event:sha256:") for item in event_files)
    event_store = next(
        item for item in manifest["scope"]["inventory"]["stores"]
        if item["role"] == "event"
    )
    assert event_store["record_count"] == 2


def test_release_includes_current_pipeline_source_from_events_without_method_review(
        tmp_path):
    cfg, _contract, contract_path = _pipeline_project(tmp_path)
    source = run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="claimtrace/primary.pipeline.json",
        scan_writes=False,
    )
    assert source["outcome"] == "succeeded"

    manifest = release.create_release_manifest(cfg)
    contract_file = _file(
        manifest, contract_path.relative_to(cfg.root).as_posix(),
    )

    assert contract_file["roles"] == ["pipeline_contract_current_source"]
    assert not any(
        item["record_count"] for item in manifest["scope"]["inventory"]["stores"]
        if item["role"] == "method_assessment"
    )


def test_release_labels_evolved_pipeline_path_as_current_source_not_historical_copy(
        tmp_path):
    cfg, contract, contract_path = _pipeline_project(tmp_path)
    historical_sha256 = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    source = run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="claimtrace/primary.pipeline.json",
        scan_writes=False,
    )
    assert source["outcome"] == "succeeded"

    contract["name"] = "primary-fit-v2"
    _write_json(contract_path, contract)
    current_sha256 = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    assert current_sha256 != historical_sha256

    manifest = release.create_release_manifest(cfg)
    contract_file = _file(
        manifest, contract_path.relative_to(cfg.root).as_posix(),
    )

    assert contract_file["roles"] == ["pipeline_contract_current_source"]
    assert contract_file["sha256"] == current_sha256
    assert any(
        "not a copy of every historical raw source" in item
        for item in manifest["scope"]["limitations"]
    )


def test_release_includes_validated_execution_provenance_and_schema_inventory(tmp_path):
    cfg, _contract, _contract_path = _pipeline_project(tmp_path)
    source = run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="claimtrace/primary.pipeline.json",
        scan_writes=False,
    )
    assert source["outcome"] == "succeeded"
    certificate = replay_run(cfg, source["run_id"], attempts=2)["certificate"]
    proposal = create_method_assessment(
        cfg, "claimtrace/primary.pipeline.json", "method:primary",
        {
            "verdict": "implements",
            "step_alignments": [
                {
                    "method_step_id": "complete", "stage_id": "complete",
                    "alignment": "match",
                },
                {
                    "method_step_id": "fit", "stage_id": "fit",
                    "alignment": "match",
                },
            ],
            "rationale": "The declared code anchors implement both method steps.",
            "limitations": [
                "This assessment does not observe internal stages at runtime."
            ],
            "provenance": {"agent": "agent:test", "model": "fixture"},
        },
        actor="agent:test",
        declared_inputs=["data/raw.csv", "analysis/pipeline.py"],
        declared_outputs=["results/fit.json"],
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        recorded_at="2026-07-15T01:00:00.000Z",
    )
    append_method_assessment(cfg, proposal)

    manifest = release.create_release_manifest(cfg)
    release.validate_release_manifest(manifest)

    replay_file = next(
        item for item in manifest["files"]
        if "replay_certificate" in item["roles"]
    )
    method_file = next(
        item for item in manifest["files"]
        if "method_assessment" in item["roles"]
    )
    contract_file = _file(manifest, "claimtrace/primary.pipeline.json")
    assert replay_file["logical_ids"] == [certificate["id"]]
    assert method_file["logical_ids"] == [proposal["id"]]
    assert contract_file["roles"] == ["pipeline_contract_current_source"]
    stores = {
        item["role"]: item["record_count"]
        for item in manifest["scope"]["inventory"]["stores"]
    }
    assert stores["replay_certificate"] == 1
    assert stores["method_assessment"] == 1
    assert manifest["schemas"] == {
        **release._schema_versions(),
    }
    assert manifest["schemas"]["event_current"] == CONTRACT_EVENT_SCHEMA
    assert manifest["schemas"]["event_stage_checkpoint_current"] == (
        "claimtrace.event/4"
    )
    assert manifest["schemas"]["event_supported"] == sorted(SUPPORTED_EVENT_SCHEMAS)
    assert manifest["schemas"]["pipeline_contract"] == PIPELINE_CONTRACT_SCHEMA
    assert (
        manifest["schemas"]["pipeline_contract_snapshot_current"]
        == PIPELINE_SNAPSHOT_SCHEMA
    )
    assert manifest["schemas"]["pipeline_contract_snapshot_supported"] == sorted(
        SUPPORTED_SNAPSHOT_SCHEMAS
    )
    assert manifest["schemas"]["replay_certificate_current"] == REPLAY_SCHEMA
    assert manifest["schemas"]["replay_certificate_stage_checkpoint_current"] == (
        "claimtrace.replay-certificate/3"
    )
    assert manifest["schemas"]["replay_certificate_supported"] == sorted(
        SUPPORTED_REPLAY_SCHEMAS
    )
    assert manifest["schemas"]["method_assessment"] == METHOD_ASSESSMENT_SCHEMA
    assert manifest["schemas"]["method_requirements"] == METHOD_REQUIREMENTS_SCHEMA
    assert manifest["schemas"]["stage_checkpoint_record"] == STAGE_CHECKPOINT_SCHEMA
    assert manifest["schemas"]["stage_trace_plan"] == STAGE_TRACE_PLAN_SCHEMA
    assert manifest["schemas"]["stage_trace"] == STAGE_TRACE_SCHEMA


@pytest.mark.parametrize(
    ("store_name", "relative_path", "error"),
    [
        (
            "replays", "aa/not-a-certificate.json",
            "replay-certificate-store integrity failed",
        ),
        (
            "method-assessments", "not-an-assessment.json",
            "method-assessment-store integrity failed",
        ),
    ],
)
def test_release_fails_closed_on_invalid_execution_store_json(
        tmp_path, store_name, relative_path, error):
    cfg = _project(tmp_path)
    path = tmp_path / "claimtrace" / store_name / relative_path
    _write_json(path, {})

    with pytest.raises(release.ReleaseError, match=error):
        release.create_release_manifest(cfg)


def test_release_validation_rejects_forged_schema_inventory(tmp_path):
    cfg = _project(tmp_path)
    manifest = release.create_release_manifest(cfg)
    manifest["schemas"]["event_supported"] = ["claimtrace.event/1"]
    core = {key: manifest[key] for key in manifest if key != "release_id"}
    manifest["release_id"] = "release:sha256:" + release.canonical_sha256(core)

    with pytest.raises(release.ReleaseError, match="not canonical"):
        release.validate_release_manifest(manifest)


def test_pre_checkpoint_schema_inventory_remains_valid_across_tool_upgrade(tmp_path):
    cfg = _project(tmp_path)
    manifest = release.create_release_manifest(cfg)
    manifest["schemas"] = copy.deepcopy(
        release._PRE_STAGE_CHECKPOINT_SCHEMA_VERSIONS
    )
    core = {key: manifest[key] for key in manifest if key != "release_id"}
    manifest["release_id"] = "release:sha256:" + release.canonical_sha256(core)

    release.validate_release_manifest(manifest)
    verification = release.verify_release_manifest(cfg, manifest)

    assert verification["valid"] is True
    assert verification["diff"]["changed_metadata_fields"] == ["schemas"]


def test_release_creation_fails_when_project_changes_between_collection_passes(
        tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    original = release._collect_once
    calls = 0

    def collect_and_mutate(config):
        nonlocal calls
        state = original(config)
        calls += 1
        if calls == 1:
            (tmp_path / "result.json").write_text('{"estimate":9}\n', encoding="utf-8")
        return state

    monkeypatch.setattr(release, "_collect_once", collect_and_mutate)
    with pytest.raises(release.ReleaseError, match="between the two"):
        release.create_release_manifest(cfg)
    assert calls == 2


def test_verify_and_diff_detect_file_drift_and_manifest_omission(tmp_path):
    cfg = _project(tmp_path)
    before = release.create_release_manifest(cfg)
    (tmp_path / "result.json").write_text('{"estimate":3}\n', encoding="utf-8")

    verification = release.verify_release_manifest(cfg, before)
    assert verification["valid"] is False
    assert verification["errors"] == []
    assert any(
        item["path"] == "result.json" and item["fields"] == ["sha256"]
        for item in verification["diff"]["changed_files"]
    )

    after = release.create_release_manifest(cfg)
    difference = release.diff_release_manifests(before, after)
    assert difference["equal"] is False
    assert difference["before_release_id"] == before["release_id"]
    assert difference["after_release_id"] == after["release_id"]

    omitted = copy.deepcopy(after)
    omitted["files"] = [item for item in omitted["files"] if item["path"] != "analysis.py"]
    omitted["scope"]["inventory"]["graph_path_node_ids"].remove("code:analysis")
    omitted["scope"]["inventory"]["role_counts"] = [
        {
            "role": role,
            "file_count": sum(role in item["roles"] for item in omitted["files"]),
        }
        for role in sorted({
            role for item in omitted["files"] for role in item["roles"]
        })
    ]
    core = {key: omitted[key] for key in omitted if key != "release_id"}
    omitted["release_id"] = "release:sha256:" + release.canonical_sha256(core)
    release.validate_release_manifest(omitted)
    omission_check = release.verify_release_manifest(cfg, omitted)
    assert omission_check["valid"] is False
    assert [item["path"] for item in omission_check["diff"]["added_files"]] == [
        "analysis.py"
    ]


def test_verify_reports_missing_graph_artifact_without_claiming_success(tmp_path):
    cfg = _project(tmp_path)
    manifest = release.create_release_manifest(cfg)
    (tmp_path / "result.json").unlink()

    result = release.verify_release_manifest(cfg, manifest)

    assert result["valid"] is False
    assert result["current_release_id"] is None
    assert result["diff"] is None
    assert result["errors"] and "cannot open release file" in result["errors"][0]
