"""Adversarial tests for opaque multi-stage pipeline contracts."""
import copy
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

import claimtrace.events as events_module
import claimtrace.pipeline as pipeline_module
from claimtrace.config import Config
from claimtrace.events import (
    EventError,
    append_event,
    load_events,
    make_event,
    materialize_runs,
    run_command,
)
from claimtrace.pipeline import (
    LEGACY_SNAPSHOT_SCHEMA,
    PREVIOUS_SNAPSHOT_SCHEMA,
    SNAPSHOT_SCHEMA,
    PipelineError,
    canonical_sha256,
    finalize_stage_trace,
    pipeline_snapshots_equivalent,
    prepare_stage_trace,
    resolve_pipeline_contract,
    validate_pipeline_snapshot,
)


def _project(tmp_path):
    trace = tmp_path / "claimtrace"
    trace.mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "analysis").mkdir()
    (tmp_path / "results").mkdir()
    (tmp_path / "methods.md").write_text(
        "# Methods\nDrop incomplete rows, then fit ordinary least squares.\n",
        encoding="utf-8",
    )
    (tmp_path / "data" / "raw.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    code = (
        "from pathlib import Path\n"
        "rows = Path('data/raw.csv').read_text().splitlines()\n"
        "complete = [row for row in rows[1:] if row]\n"
        "slope = len(complete)\n"
        "Path('results/fit.json').write_text(str(slope))\n"
    )
    code_path = tmp_path / "analysis" / "pipeline.py"
    code_path.write_text(code, encoding="utf-8")
    graph = {
        "schema_version": "1.0",
        "concepts": {},
        "nodes": [
            {"id": "data:raw", "type": "data", "status": "current", "path": "data/raw.csv"},
            {"id": "code:pipeline", "type": "code", "status": "current", "path": "analysis/pipeline.py"},
            {
                "id": "method:primary", "type": "method", "status": "current",
                "path": "methods.md",
                "method_spec": {
                    "schema_version": "claimtrace.method-spec/1",
                    "steps": [
                        {"id": "complete", "statement": "Remove incomplete rows.", "required": True},
                        {"id": "fit", "statement": "Fit ordinary least squares.", "required": True},
                    ],
                },
            },
            {"id": "art:fit", "type": "artifact", "status": "current", "path": "results/fit.json"},
        ],
        "edges": [],
    }
    (trace / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    (tmp_path / "claimtrace.config.json").write_text(json.dumps({
        "root": ".", "graph": "claimtrace/graph.json", "events": "claimtrace/events",
    }), encoding="utf-8")
    lines = code_path.read_bytes().splitlines(keepends=True)
    def digest(start, end):
        return hashlib.sha256(b"".join(lines[start - 1:end])).hexdigest()
    contract = {
        "schema_version": "claimtrace.pipeline-contract/1",
        "name": "primary-fit",
        "entrypoint_code_node_id": "code:pipeline",
        "code_node_ids": ["code:pipeline"],
        "input_node_ids": ["data:raw"],
        "output_node_ids": ["art:fit"],
        "required_parameters": ["model"],
        "required_seeds": ["numpy"],
        "stages": [
            {
                "id": "complete", "depends_on": [],
                "method_id": "method:primary", "method_step_id": "complete",
                "consumes_node_ids": ["data:raw"], "produces_node_ids": [],
                "code_anchors": [{
                    "code_node_id": "code:pipeline", "kind": "text_lines",
                    "start_line": 2, "end_line": 3, "text_sha256": digest(2, 3),
                }],
            },
            {
                "id": "fit", "depends_on": ["complete"],
                "method_id": "method:primary", "method_step_id": "fit",
                "consumes_node_ids": [], "produces_node_ids": ["art:fit"],
                "code_anchors": [{
                    "code_node_id": "code:pipeline", "kind": "text_lines",
                    "start_line": 4, "end_line": 5, "text_sha256": digest(4, 5),
                }],
            },
        ],
    }
    contract_path = trace / "primary.pipeline.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    return Config(tmp_path / "claimtrace.config.json"), contract, contract_path


def _materialized_project(tmp_path, *, random_intermediate=False):
    """Extend the base fixture with a path-bearing nonterminal stage output."""
    cfg, contract, contract_path = _project(tmp_path)
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    graph["nodes"].append({
        "id": "art:clean", "type": "artifact", "status": "current",
        "path": "data/clean.csv",
    })
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    intermediate_write = (
        "Path('data/clean.csv').write_bytes(__import__('os').urandom(32))\n"
        if random_intermediate else
        "Path('data/clean.csv').write_text('\\n'.join(complete))\n"
    )
    code = (
        "from pathlib import Path\n"
        "rows = Path('data/raw.csv').read_text().splitlines()\n"
        "complete = [row for row in rows[1:] if row]\n"
        + intermediate_write
        + "clean = Path('data/clean.csv').read_bytes()\n"
        "slope = len(complete)\n"
        "Path('results/fit.json').write_text(str(slope))\n"
    )
    code_path = cfg.root / "analysis" / "pipeline.py"
    code_path.write_text(code, encoding="utf-8")
    lines = code_path.read_bytes().splitlines(keepends=True)

    def digest(start, end):
        return hashlib.sha256(b"".join(lines[start - 1:end])).hexdigest()

    contract["stages"][0]["produces_node_ids"] = ["art:clean"]
    contract["stages"][0]["code_anchors"][0].update({
        "start_line": 2, "end_line": 4, "text_sha256": digest(2, 4),
    })
    contract["stages"][1]["consumes_node_ids"] = ["art:clean"]
    contract["stages"][1]["code_anchors"][0].update({
        "start_line": 5, "end_line": 7, "text_sha256": digest(5, 7),
    })
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    return cfg, contract, contract_path


def _instrumented_project(tmp_path):
    cfg, contract, contract_path = _project(tmp_path)
    code = (
        "from pathlib import Path\n"
        "from claimtrace.pipeline import stage_checkpoint\n"
        "rows = Path('data/raw.csv').read_text().splitlines()\n"
        "complete = [row for row in rows[1:] if row]\n"
        "stage_checkpoint('complete')\n"
        "slope = len(complete)\n"
        "Path('results/fit.json').write_text(str(slope))\n"
        "stage_checkpoint('fit')\n"
    )
    code_path = cfg.root / "analysis" / "pipeline.py"
    code_path.write_text(code, encoding="utf-8")
    lines = code_path.read_bytes().splitlines(keepends=True)

    def digest(start, end):
        return hashlib.sha256(b"".join(lines[start - 1:end])).hexdigest()

    contract["stages"][0]["code_anchors"][0].update({
        "start_line": 3, "end_line": 5, "text_sha256": digest(3, 5),
    })
    contract["stages"][1]["code_anchors"][0].update({
        "start_line": 6, "end_line": 8, "text_sha256": digest(6, 8),
    })
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    return cfg, contract, contract_path


def _resolve(cfg):
    return resolve_pipeline_contract(
        cfg, "claimtrace/primary.pipeline.json",
        declared_inputs=["data/raw.csv", "analysis/pipeline.py"],
        declared_outputs=["results/fit.json"],
        parameters={"model": "ols"}, seeds={"numpy": "7"},
    )


def _resolve_schema(cfg, snapshot_schema):
    return resolve_pipeline_contract(
        cfg, "claimtrace/primary.pipeline.json",
        declared_inputs=["data/raw.csv", "analysis/pipeline.py"],
        declared_outputs=["results/fit.json"],
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        snapshot_schema=snapshot_schema,
    )


def _advance_mtime(path):
    before = path.stat()
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
    assert path.stat().st_mtime_ns != before.st_mtime_ns


def _readdress(snapshot):
    core = {key: value for key, value in snapshot.items() if key != "id"}
    snapshot["id"] = "pipeline-contract:sha256:" + canonical_sha256(core)
    return snapshot


def _readdress_start_event(start, *, argv, cwd=None):
    payload = copy.deepcopy(start["payload"])
    plan = payload["plan"]
    plan["argv"] = list(argv)
    if cwd is not None:
        plan["cwd"] = cwd
    plan["computation_id"] = (
        "computation:sha256:"
        + events_module.canonical_sha256(events_module._fingerprint_computation(plan))
    )
    payload["plan_id"] = (
        "recipe:sha256:"
        + events_module.canonical_sha256(events_module._fingerprint_plan(plan))
    )
    return make_event(
        "run.started", start["run_id"], payload,
        recorded_at=start["recorded_at"], schema_version=start["schema_version"],
    )


def _readdress_finish_event(finish, start):
    payload = copy.deepcopy(finish["payload"])
    payload["start_event_id"] = start["id"]
    payload["plan_id"] = start["payload"]["plan_id"]
    result_basis = {
        "plan_id": payload["plan_id"],
        "outcome": payload["outcome"],
        "direct_child_returncode": payload["direct_child_returncode"],
        "input_transitions": [
            events_module._fingerprint_transition(item)
            for item in payload["input_transitions"]
        ],
        "output_transitions": [
            events_module._fingerprint_transition(item)
            for item in payload["output_transitions"]
        ],
        "contract_errors": payload["contract_errors"],
    }
    if "intermediate_transitions" in payload:
        result_basis["intermediate_transitions"] = [
            events_module._fingerprint_transition(item)
            for item in payload["intermediate_transitions"]
        ]
    payload["result_id"] = (
        "result:sha256:" + events_module.canonical_sha256(result_basis)
    )
    return make_event(
        "run.finished", finish["run_id"], payload,
        recorded_at=finish["recorded_at"], schema_version=finish["schema_version"],
    )


def _malformed_snapshot(snapshot, mutation):
    forged = copy.deepcopy(snapshot)
    if mutation == "roles":
        forged["roles"]["inputs"].append(
            copy.deepcopy(forged["roles"]["inputs"][0])
        )
    elif mutation == "source":
        forged["source"]["file_version_id"] = "file:sha256:" + "f" * 64
    elif mutation == "file_snapshot":
        forged["roles"]["code"][0]["file"]["state"] = "missing"
    elif mutation == "stages":
        forged["stages"][0]["unexpected"] = True
    elif mutation == "dag":
        forged["stages"][0]["depends_on"] = ["fit"]
    elif mutation == "anchors":
        forged["stages"][0]["code_anchors"][0]["valid"] = False
    elif mutation == "dataflow":
        forged["stages"][0]["consumes_node_ids"] = []
    elif mutation == "entrypoint":
        forged["entrypoint_code_node_id"] = "code:undeclared"
    elif mutation == "required_keys":
        forged["required_parameters"] = ["model", "model"]
    else:  # pragma: no cover - the parameter list below is closed
        raise AssertionError(mutation)
    return _readdress(forged)


def test_contract_pins_code_method_and_roles_without_claiming_stage_observation(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    first = _resolve(cfg)
    second = _resolve(cfg)
    assert first == second
    assert validate_pipeline_snapshot(first) == first["id"]
    assert first["coverage"] == {
        "boundary_files": (
            "declared_paths_and_graph_roles_input_bytes_captured_by_run_receipt"
        ),
        "code": "exact_files_text_anchors_and_declared_entrypoint",
        "methods": "exact_nodes_and_optional_files",
        "stage_execution": "declared_only_not_observed",
        "hidden_intermediates": "not_observed",
        "materialized_intermediates": (
            "declared_file_paths_for_process_boundary_capture_not_stage_attributed"
        ),
        "in_memory_intermediates": "not_observed",
        "semantic_equivalence": "requires_separate_review",
    }
    assert {stage["execution_observation"] for stage in first["stages"]} == {
        "declared_only_not_observed"
    }
    assert [item["node_id"] for item in first["roles"]["code"]] == ["code:pipeline"]
    assert [item["node_id"] for item in first["roles"]["methods"]] == ["method:primary"]
    assert first["roles"]["intermediates"] == []


def test_v3_omits_volatile_code_and_method_mtimes(tmp_path):
    cfg, _contract, _path = _project(tmp_path)

    snapshot = _resolve(cfg)

    assert snapshot["schema_version"] == SNAPSHOT_SCHEMA
    assert "mtime_ns" not in snapshot["roles"]["code"][0]["file"]
    assert "mtime_ns" not in snapshot["roles"]["methods"][0]["file"]
    assert validate_pipeline_snapshot(snapshot) == snapshot["id"]


def test_v3_snapshot_is_identical_after_mtime_only_changes(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    first = _resolve(cfg)

    _advance_mtime(cfg.root / "analysis" / "pipeline.py")
    _advance_mtime(cfg.root / "methods.md")
    second = _resolve(cfg)

    assert second == first
    assert pipeline_snapshots_equivalent(first, second) is True


def test_v2_snapshot_keeps_intermediates_and_is_portable_across_mtimes(tmp_path):
    cfg, _contract, _path = _materialized_project(tmp_path)
    stored = _resolve_schema(cfg, PREVIOUS_SNAPSHOT_SCHEMA)

    assert "intermediates" in stored["roles"]
    assert stored["coverage"]["materialized_intermediates"] == (
        "declared_file_paths_for_process_boundary_capture_not_stage_attributed"
    )
    assert "mtime_ns" in stored["roles"]["code"][0]["file"]
    assert "mtime_ns" in stored["roles"]["methods"][0]["file"]
    assert validate_pipeline_snapshot(stored) == stored["id"]

    _advance_mtime(cfg.root / "analysis" / "pipeline.py")
    _advance_mtime(cfg.root / "methods.md")
    current = _resolve_schema(cfg, PREVIOUS_SNAPSHOT_SCHEMA)

    assert current["id"] != stored["id"]
    assert pipeline_snapshots_equivalent(stored, current) is True


def test_v2_snapshot_content_change_is_not_mtime_equivalent(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    stored = _resolve_schema(cfg, PREVIOUS_SNAPSHOT_SCHEMA)
    method_path = cfg.root / "methods.md"
    method_path.write_text(
        "# Methods\nMaterially changed method prose.\n", encoding="utf-8",
    )
    current = _resolve_schema(cfg, PREVIOUS_SNAPSHOT_SCHEMA)

    assert validate_pipeline_snapshot(current) == current["id"]
    assert pipeline_snapshots_equivalent(stored, current) is False


def test_v1_snapshot_keeps_exact_mtime_currentness(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    stored = _resolve_schema(cfg, LEGACY_SNAPSHOT_SCHEMA)

    _advance_mtime(cfg.root / "analysis" / "pipeline.py")
    current = _resolve_schema(cfg, LEGACY_SNAPSHOT_SCHEMA)

    assert current["id"] != stored["id"]
    assert pipeline_snapshots_equivalent(stored, current) is False


def test_snapshot_currentness_never_crosses_schema_versions(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    v2 = _resolve_schema(cfg, PREVIOUS_SNAPSHOT_SCHEMA)
    v3 = _resolve_schema(cfg, SNAPSHOT_SCHEMA)

    assert pipeline_snapshots_equivalent(v2, v3) is False


def test_malformed_v2_snapshot_is_rejected_before_mtime_projection(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    stored = _resolve_schema(cfg, PREVIOUS_SNAPSHOT_SCHEMA)
    forged = copy.deepcopy(stored)
    forged["roles"]["code"][0]["file"].pop("mtime_ns")
    forged = _readdress(forged)

    with pytest.raises(PipelineError, match="invalid file snapshot shape"):
        pipeline_snapshots_equivalent(forged, stored)


@pytest.mark.parametrize("mutation", ["path", "node", "source", "anchor", "size"])
def test_v2_equivalence_rejects_every_non_mtime_contract_change(
        tmp_path, mutation):
    cfg, _contract, _path = _project(tmp_path)
    stored = _resolve_schema(cfg, PREVIOUS_SNAPSHOT_SCHEMA)
    changed = copy.deepcopy(stored)
    if mutation == "path":
        code = changed["roles"]["code"][0]
        code["path"] = "analysis/other.py"
        code["file"]["path"] = "analysis/other.py"
    elif mutation == "node":
        code = changed["roles"]["code"][0]
        code["node_sha256"] = "f" * 64
        code["node_version_id"] = "node:sha256:" + "f" * 64
    elif mutation == "source":
        changed["source"]["sha256"] = "f" * 64
        changed["source"]["file_version_id"] = "file:sha256:" + "f" * 64
    elif mutation == "anchor":
        changed["stages"][0]["code_anchors"][0]["text_sha256"] = "f" * 64
    elif mutation == "size":
        changed["roles"]["code"][0]["file"]["size"] += 1
    else:  # pragma: no cover - the parameter list above is closed
        raise AssertionError(mutation)
    changed = _readdress(changed)

    assert validate_pipeline_snapshot(changed) == changed["id"]
    assert pipeline_snapshots_equivalent(stored, changed) is False


def test_path_bearing_internal_stage_output_is_a_materialized_intermediate_role(tmp_path):
    cfg, _contract, _path = _materialized_project(tmp_path)

    snapshot = _resolve(cfg)

    assert snapshot["schema_version"] == SNAPSHOT_SCHEMA
    intermediate = snapshot["roles"]["intermediates"]
    assert len(intermediate) == 1
    assert intermediate[0]["node_id"] == "art:clean"
    assert intermediate[0]["path"] == "data/clean.csv"
    assert intermediate[0]["materialization"] == "declared_file_boundary"
    assert validate_pipeline_snapshot(snapshot) == snapshot["id"]


def test_pathless_internal_stage_output_remains_explicitly_unobserved(tmp_path):
    cfg, contract, contract_path = _project(tmp_path)
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    graph["nodes"].append({
        "id": "state:complete", "type": "artifact", "status": "current",
    })
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    contract["stages"][0]["produces_node_ids"] = ["state:complete"]
    contract["stages"][1]["consumes_node_ids"] = ["state:complete"]
    contract_path.write_text(json.dumps(contract), encoding="utf-8")

    snapshot = _resolve(cfg)

    assert len(snapshot["roles"]["intermediates"]) == 1
    intermediate = snapshot["roles"]["intermediates"][0]
    assert intermediate["node_id"] == "state:complete"
    assert intermediate["materialization"] == "unobserved_in_memory_or_ephemeral"
    assert "path" not in intermediate


def test_inactive_internal_stage_node_is_rejected(tmp_path):
    cfg, _contract, _path = _materialized_project(tmp_path)
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    intermediate = next(item for item in graph["nodes"] if item["id"] == "art:clean")
    intermediate["status"] = "superseded"
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")

    with pytest.raises(PipelineError, match=r"graph node art:clean is not active"):
        _resolve(cfg)


def test_path_bearing_internal_stage_node_path_must_be_canonical(tmp_path):
    cfg, _contract, _path = _materialized_project(tmp_path)
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    intermediate = next(item for item in graph["nodes"] if item["id"] == "art:clean")
    intermediate["path"] = "data/../data/clean.csv"
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")

    with pytest.raises(PipelineError, match=r"art:clean path must be a canonical"):
        _resolve(cfg)


def test_contract_role_mismatch_fails_closed(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    with pytest.raises(PipelineError, match="input paths differ"):
        resolve_pipeline_contract(
            cfg, "claimtrace/primary.pipeline.json",
            declared_inputs=["analysis/pipeline.py"],
            declared_outputs=["results/fit.json"],
            parameters={"model": "ols"}, seeds={"numpy": "7"},
        )


def test_input_and_terminal_output_same_path_is_rejected_before_launch(
        tmp_path, monkeypatch):
    cfg, _contract, _path = _project(tmp_path)
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    output = next(item for item in graph["nodes"] if item["id"] == "art:fit")
    output["path"] = "data/raw.csv"
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    launched = False

    def forbidden_launch(*_args, **_kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("the child must not launch")

    monkeypatch.setattr(events_module.subprocess, "Popen", forbidden_launch)
    with pytest.raises(
            EventError,
            match=r"normalized paths overlap across input and terminal output roles"):
        run_command(
            cfg, [sys.executable, "analysis/pipeline.py"],
            inputs=["data/raw.csv", "analysis/pipeline.py"],
            outputs=["data/raw.csv"], cwd=str(cfg.root),
            parameters={"model": "ols"}, seeds={"numpy": "7"},
            pipeline_contract="claimtrace/primary.pipeline.json",
            scan_writes=False,
        )
    assert launched is False


def test_code_anchor_drift_fails_before_run(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    code = cfg.root / "analysis" / "pipeline.py"
    code.write_text(code.read_text(encoding="utf-8").replace("if row", "if row.strip()"),
                    encoding="utf-8")
    with pytest.raises(PipelineError, match="does not match current code bytes"):
        _resolve(cfg)


def test_required_method_step_must_have_exactly_one_stage(tmp_path):
    cfg, contract, contract_path = _project(tmp_path)
    contract["stages"] = contract["stages"][:1]
    contract["output_node_ids"] = ["art:fit"]
    contract["stages"][0]["produces_node_ids"] = ["art:fit"]
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    with pytest.raises(PipelineError, match="required method steps are absent"):
        _resolve(cfg)


def test_stage_cycle_is_rejected(tmp_path):
    cfg, contract, contract_path = _project(tmp_path)
    contract["stages"][0]["depends_on"] = ["fit"]
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    with pytest.raises(PipelineError, match="dependency cycle"):
        _resolve(cfg)


def test_stored_snapshot_cannot_upgrade_declared_stage_to_observed(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    snapshot = _resolve(cfg)
    snapshot["coverage"]["stage_execution"] = "observed"
    with pytest.raises(PipelineError, match="id does not match"):
        validate_pipeline_snapshot(snapshot)


@pytest.mark.parametrize(("mutation", "match"), [
    ("roles", "role inputs is not canonical"),
    ("source", "source has invalid metadata"),
    ("file_snapshot", "not a stable SHA-256 file snapshot"),
    ("stages", r"stages\[0\] has unknown or missing fields"),
    ("dag", "dependency cycle"),
    ("anchors", r"code_anchors\[0\] is invalid"),
    ("dataflow", "external inputs differ from input roles"),
    ("entrypoint", "entrypoint is not a code role"),
    ("required_keys", "required_parameters is not canonical"),
])
def test_recomputed_content_address_does_not_bypass_deep_snapshot_validation(
        tmp_path, mutation, match):
    cfg, _contract, _path = _project(tmp_path)
    forged = _malformed_snapshot(_resolve(cfg), mutation)

    with pytest.raises(PipelineError, match=match):
        validate_pipeline_snapshot(forged)


@pytest.mark.parametrize(("mutation", "match"), [
    ("missing_role", "roles are invalid"),
    ("bad_token", "materialization is invalid"),
    ("omitted_internal", "intermediate roles differ from internal stage outputs"),
    ("path_overlap", "normalized paths overlap across terminal output and intermediate roles"),
])
def test_readdressed_materialized_intermediate_role_corruption_is_rejected(
        tmp_path, mutation, match):
    cfg, _contract, _path = _materialized_project(tmp_path)
    forged = copy.deepcopy(_resolve(cfg))
    if mutation == "missing_role":
        forged["roles"].pop("intermediates")
    elif mutation == "bad_token":
        forged["roles"]["intermediates"][0]["materialization"] = "observed_stage"
    elif mutation == "omitted_internal":
        forged["roles"]["intermediates"] = []
    elif mutation == "path_overlap":
        forged["roles"]["intermediates"][0]["path"] = "results/fit.json"
    else:  # pragma: no cover - parameter list is closed
        raise AssertionError(mutation)
    forged = _readdress(forged)

    with pytest.raises(PipelineError, match=match):
        validate_pipeline_snapshot(forged)


@pytest.mark.parametrize(("mutation", "match"), [
    ("node_id", "snapshot node ids overlap across input and terminal output roles"),
    ("path", "snapshot normalized paths overlap across input and terminal output roles"),
])
def test_readdressed_snapshot_cross_role_overlap_is_rejected(
        tmp_path, mutation, match):
    cfg, _contract, _path = _project(tmp_path)
    forged = copy.deepcopy(_resolve(cfg))
    if mutation == "node_id":
        forged["roles"]["outputs"][0] = copy.deepcopy(
            forged["roles"]["inputs"][0]
        )
    elif mutation == "path":
        forged["roles"]["outputs"][0]["path"] = (
            forged["roles"]["inputs"][0]["path"]
        )
    else:  # pragma: no cover - parameter list is closed
        raise AssertionError(mutation)
    forged = _readdress(forged)

    with pytest.raises(PipelineError, match=match):
        validate_pipeline_snapshot(forged)


def test_contract_event_rejects_readdressed_malformed_pipeline_snapshot(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    result = run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="claimtrace/primary.pipeline.json",
        scan_writes=False,
    )
    assert result["outcome"] == "succeeded"
    events, issues = load_events(cfg.events_path)
    assert issues == []
    start = next(event for event in events if event["type"] == "run.started")
    payload = copy.deepcopy(start["payload"])
    plan = payload["plan"]
    plan["pipeline_contract"] = _malformed_snapshot(
        plan["pipeline_contract"], "dataflow",
    )
    plan["computation_id"] = (
        "computation:sha256:"
        + events_module.canonical_sha256(events_module._fingerprint_computation(plan))
    )
    payload["plan_id"] = (
        "recipe:sha256:"
        + events_module.canonical_sha256(events_module._fingerprint_plan(plan))
    )
    forged_event = make_event(
        start["type"], start["run_id"], payload,
        recorded_at=start["recorded_at"], schema_version=start["schema_version"],
    )

    with pytest.raises(EventError, match="pipeline contract is invalid.*external inputs"):
        append_event(tmp_path / "forged-events", forged_event)


def test_parameter_and_seed_keys_are_exact_policy(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    with pytest.raises(PipelineError, match="parameter keys differ"):
        resolve_pipeline_contract(
            cfg, "claimtrace/primary.pipeline.json",
            declared_inputs=["data/raw.csv", "analysis/pipeline.py"],
            declared_outputs=["results/fit.json"],
            parameters={}, seeds={"numpy": "7"},
        )


def test_contract_bound_run_writes_event_v3_and_portable_computation_id(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    result = run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="claimtrace/primary.pipeline.json",
        scan_writes=False,
    )
    assert result["outcome"] == "succeeded"
    assert result["pipeline_contract_id"].startswith("pipeline-contract:sha256:")
    assert result["computation_id"].startswith("computation:sha256:")
    events, issues = load_events(cfg.events_path)
    assert issues == []
    runs, issues = materialize_runs(events)
    assert issues == []
    assert {event["schema_version"] for event in events} == {"claimtrace.event/3"}
    plan = runs[0]["start"]["payload"]["plan"]
    assert plan["computation_id"] == result["computation_id"]
    assert plan["declared_intermediates"] == []
    assert runs[0]["finish"]["payload"]["intermediate_transitions"] == []
    assert runs[0]["finish"]["payload"]["lineage_coverage"][
        "declared_intermediates"
    ] == "pre_and_post_hashed"
    assert plan["pipeline_contract"]["coverage"]["stage_execution"] == (
        "declared_only_not_observed"
    )


def test_instrumented_run_writes_event_v4_with_complete_cooperative_trace(
        tmp_path, monkeypatch):
    cfg, _contract, _path = _instrumented_project(tmp_path)
    monkeypatch.setenv(
        "PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"),
    )

    result = run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="claimtrace/primary.pipeline.json",
        stage_checkpoints=True, scan_writes=False,
    )

    assert result["outcome"] == "succeeded"
    assert result["stage_trace"]["state"] == "cooperative_report_complete"
    assert [
        item["stage_id"] for item in result["stage_trace"]["checkpoints"]
    ] == ["complete", "fit"]
    assert all(
        item["observation"] == "program_emitted_checkpoint_reached"
        for item in result["stage_trace"]["checkpoints"]
    )
    stored, event_issues = load_events(cfg.events_path)
    runs, run_issues = materialize_runs(stored)
    assert event_issues == run_issues == []
    assert {item["schema_version"] for item in stored} == {"claimtrace.event/4"}
    assert runs[0]["finish"]["payload"]["stage_trace"] == result["stage_trace"]
    assert runs[0]["start"]["payload"]["plan"]["pipeline_contract"][
        "coverage"
    ]["stage_execution"] == "declared_only_not_observed"


def test_non_checkpoint_run_scrubs_inherited_reserved_trace_environment(
        tmp_path, monkeypatch):
    cfg, _contract, _path = _instrumented_project(tmp_path)
    monkeypatch.setenv(
        "PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"),
    )
    # A partial inherited binding would make stage_checkpoint fail if it reached
    # the ordinary child. The controller must reserve and remove these variables.
    monkeypatch.setenv("CLAIMTRACE_STAGE_TRACE_PATH", str(tmp_path / "outer.jsonl"))

    result = run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="claimtrace/primary.pipeline.json",
        scan_writes=False,
    )

    assert result["outcome"] == "succeeded"
    assert result["stage_trace"] is None


def test_descendant_emitted_checkpoints_do_not_satisfy_direct_child_trace(
        tmp_path, monkeypatch):
    cfg, contract, contract_path = _instrumented_project(tmp_path)
    code = (
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n"
        "from claimtrace.pipeline import stage_checkpoint\n"
        "if os.environ.get('CLAIMTRACE_DESCENDANT') == '1':\n"
        "    rows = Path('data/raw.csv').read_text().splitlines()\n"
        "    complete = [row for row in rows[1:] if row]\n"
        "    stage_checkpoint('complete')\n"
        "    slope = len(complete)\n"
        "    Path('results/fit.json').write_text(str(slope))\n"
        "    stage_checkpoint('fit')\n"
        "else:\n"
        "    child_env = os.environ.copy()\n"
        "    child_env['CLAIMTRACE_DESCENDANT'] = '1'\n"
        "    raise SystemExit(subprocess.run([sys.executable, __file__], env=child_env).returncode)\n"
    )
    code_path = cfg.root / "analysis" / "pipeline.py"
    code_path.write_text(code, encoding="utf-8")
    lines = code_path.read_bytes().splitlines(keepends=True)
    contract["stages"][0]["code_anchors"][0].update({
        "start_line": 7, "end_line": 9,
        "text_sha256": hashlib.sha256(b"".join(lines[6:9])).hexdigest(),
    })
    contract["stages"][1]["code_anchors"][0].update({
        "start_line": 10, "end_line": 12,
        "text_sha256": hashlib.sha256(b"".join(lines[9:12])).hexdigest(),
    })
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    monkeypatch.setenv(
        "PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"),
    )

    result = run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="claimtrace/primary.pipeline.json",
        stage_checkpoints=True, scan_writes=False,
    )

    assert result["direct_child_returncode"] == 0
    assert result["outcome"] == "contract_failed"
    assert result["stage_trace"]["state"] == "cooperative_report_invalid"
    assert any(
        item["code"] == "STAGE_TRACE_REPORTER_PROCESS_MISMATCH"
        for item in result["stage_trace"]["issues"]
    )


def test_exception_after_marker_creation_removes_private_trace_channel(
        tmp_path, monkeypatch):
    cfg, _contract, _path = _instrumented_project(tmp_path)
    monkeypatch.setenv(
        "PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"),
    )

    def fail_scan(*_args, **_kwargs):
        raise RuntimeError("synthetic scan failure")

    monkeypatch.setattr(events_module, "_scan_project", fail_scan)
    with pytest.raises(RuntimeError, match="synthetic scan failure"):
        run_command(
            cfg, [sys.executable, "analysis/pipeline.py"],
            inputs=["data/raw.csv", "analysis/pipeline.py"],
            outputs=["results/fit.json"], cwd=str(cfg.root),
            parameters={"model": "ols"}, seeds={"numpy": "7"},
            pipeline_contract="claimtrace/primary.pipeline.json",
            stage_checkpoints=True, scan_writes=True,
        )

    marker_directory = events_module._marker_directory(cfg.root)
    assert list(marker_directory.glob("*.stages.jsonl")) == []
    markers = list(marker_directory.glob("*.json"))
    assert len(markers) == 1
    events_module.remove_active_marker(markers[0])


def test_result_id_excludes_fresh_checkpoint_binding_material(
        tmp_path, monkeypatch):
    cfg, _contract, _path = _instrumented_project(tmp_path)
    monkeypatch.setenv(
        "PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"),
    )

    def traced_run():
        return run_command(
            cfg, [sys.executable, "analysis/pipeline.py"],
            inputs=["data/raw.csv", "analysis/pipeline.py"],
            outputs=["results/fit.json"], cwd=str(cfg.root),
            parameters={"model": "ols"}, seeds={"numpy": "7"},
            pipeline_contract="claimtrace/primary.pipeline.json",
            stage_checkpoints=True, scan_writes=False,
        )

    first = traced_run()
    (cfg.root / "results" / "fit.json").unlink()
    second = traced_run()

    assert first["outcome"] == second["outcome"] == "succeeded"
    assert first["result_id"] == second["result_id"]
    assert (
        first["stage_trace"]["binding"]["nonce_sha256"]
        != second["stage_trace"]["binding"]["nonce_sha256"]
    )
    assert all(
        isinstance(item["stage_trace"]["binding"]["reporter_pid"], int)
        and item["stage_trace"]["binding"]["reporter_pid"] > 0
        for item in (first, second)
    )


def test_wait_error_after_spawn_kills_and_reaps_direct_child(
        tmp_path, monkeypatch):
    cfg, _contract, _path = _project(tmp_path)

    class WaitFailsProcess:
        pid = 42424

        def __init__(self):
            self.killed = False
            self.wait_calls = 0

        def wait(self, timeout=None):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise OSError("synthetic wait failure")
            return -9

        def poll(self):
            return -9 if self.killed else None

        def kill(self):
            self.killed = True

    process = WaitFailsProcess()
    git_snapshot = events_module.capture_git(cfg.root)
    monkeypatch.setattr(
        events_module, "capture_git", lambda _root: git_snapshot,
    )
    monkeypatch.setattr(
        events_module.subprocess, "Popen", lambda *_args, **_kwargs: process,
    )

    result = run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        scan_writes=False,
    )

    assert result["outcome"] == "launch_error"
    assert process.killed is True
    assert process.wait_calls == 2


def test_configured_checkpoint_policy_only_applies_to_contracted_runs(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    cfg.require_stage_checkpoints = True

    result = run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        scan_writes=False,
    )

    assert result["outcome"] == "succeeded"
    assert result["pipeline_contract_id"] is None
    assert result["stage_trace"] is None
    stored, issues = load_events(cfg.events_path)
    assert issues == []
    assert {item["schema_version"] for item in stored} == {"claimtrace.event/1"}


def test_explicit_checkpoints_without_pipeline_contract_are_rejected(tmp_path):
    cfg, _contract, _path = _project(tmp_path)

    with pytest.raises(
            EventError, match="stage checkpoints require --pipeline-contract"):
        run_command(
            cfg, [sys.executable, "analysis/pipeline.py"],
            inputs=["data/raw.csv", "analysis/pipeline.py"],
            outputs=["results/fit.json"], cwd=str(cfg.root),
            stage_checkpoints=True, scan_writes=False,
        )


def test_zero_exit_with_missing_cooperative_checkpoint_fails_closed(
        tmp_path, monkeypatch):
    cfg, contract, contract_path = _instrumented_project(tmp_path)
    code_path = cfg.root / "analysis" / "pipeline.py"
    code = code_path.read_text(encoding="utf-8").replace(
        "stage_checkpoint('fit')\n", "",
    )
    code_path.write_text(code, encoding="utf-8")
    lines = code_path.read_bytes().splitlines(keepends=True)
    contract["stages"][0]["code_anchors"][0]["text_sha256"] = hashlib.sha256(
        b"".join(lines[2:5])
    ).hexdigest()
    contract["stages"][1]["code_anchors"][0].update({
        "start_line": 6,
        "end_line": 7,
        "text_sha256": hashlib.sha256(b"".join(lines[5:7])).hexdigest(),
    })
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    monkeypatch.setenv(
        "PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"),
    )

    result = run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="claimtrace/primary.pipeline.json",
        stage_checkpoints=True, scan_writes=False,
    )

    assert result["direct_child_returncode"] == 0
    assert result["outcome"] == "contract_failed"
    assert result["stage_trace"]["state"] == "cooperative_report_incomplete"
    assert any(
        item["code"] == "STAGE_TRACE_MISSING_STAGES"
        for item in result["stage_trace"]["issues"]
    )


@pytest.mark.parametrize(("records", "issue_code"), [
    ([('complete', 5), ('complete', 5), ('fit', 8)],
     "STAGE_TRACE_DUPLICATE_STAGE"),
    ([('fit', 8), ('complete', 5)], "STAGE_TRACE_DEPENDENCY_ORDER"),
    ([('unknown', 5), ('complete', 5), ('fit', 8)],
     "STAGE_TRACE_UNKNOWN_STAGE"),
])
def test_checkpoint_protocol_rejects_duplicate_out_of_order_and_unknown_stages(
        tmp_path, records, issue_code):
    cfg, _contract, _path = _instrumented_project(tmp_path)
    snapshot = _resolve(cfg)
    capture = prepare_stage_trace(
        (tmp_path / "private" / "trace.jsonl").resolve(), snapshot, cfg.root,
    )
    raw = []
    for stage_id, line in records:
        raw.append(json.dumps({
            "schema_version": "claimtrace.stage-checkpoint/1",
            "nonce": capture["nonce"],
            "pipeline_contract_id": snapshot["id"],
            "stage_id": stage_id,
            "reporter_pid": 4242,
            "callsite": {"path": "analysis/pipeline.py", "line": line},
        }, sort_keys=True, separators=(",", ":")))
    capture["path"].write_text("\n".join(raw) + "\n", encoding="utf-8")

    trace = finalize_stage_trace(
        capture, snapshot, launched=True, expected_reporter_pid=4242,
    )

    assert trace["state"] == "cooperative_report_invalid"
    assert any(item["code"] == issue_code for item in trace["issues"])


def test_readdressed_event_v3_cannot_replace_entrypoint_with_unrelated_command(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="claimtrace/primary.pipeline.json",
        scan_writes=False,
    )
    stored, issues = load_events(cfg.events_path)
    assert issues == []
    start = next(item for item in stored if item["type"] == "run.started")
    finish = next(item for item in stored if item["type"] == "run.finished")
    forged_start = _readdress_start_event(
        start, argv=[sys.executable, "-c", "print('unrelated')"],
    )
    forged_finish = _readdress_finish_event(finish, forged_start)

    # Every portable computation, recipe, result, start-link, and event address was
    # recomputed. The semantic argv/entrypoint invariant must still reject the start.
    events_module.validate_event(forged_finish)
    with pytest.raises(EventError, match="entrypoint code path"):
        events_module.validate_event(forged_start)
    store = tmp_path / "readdressed-unrelated"
    append_event(store, forged_finish)
    with pytest.raises(EventError, match="entrypoint code path"):
        append_event(store, forged_start)
    forged_digest = forged_start["id"].removeprefix("event:sha256:")
    forged_path = events_module._event_path(store, forged_digest)
    forged_path.parent.mkdir(parents=True, exist_ok=True)
    forged_path.write_text(json.dumps(forged_start), encoding="utf-8")
    loaded, load_issues = load_events(store)
    runs, run_issues = materialize_runs(loaded)
    assert any(
        item["code"] == "EVENT_INTEGRITY" and "entrypoint code path" in item["detail"]
        for item in load_issues
    )
    assert len(runs) == 1
    assert any(item["code"] == "RUN_FINISH_WITHOUT_START" for item in run_issues)


@pytest.mark.parametrize(("argv_path", "cwd"), [
    ("./analysis/../analysis/pipeline.py", "."),
    (r"analysis\pipeline.py", "."),
    ("./pipeline.py", "analysis"),
    ("../analysis/pipeline.py", "analysis"),
])
def test_stored_entrypoint_argv_safe_aliases_resolve_from_recorded_cwd(
        tmp_path, argv_path, cwd):
    cfg, _contract, _path = _project(tmp_path)
    run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="claimtrace/primary.pipeline.json",
        scan_writes=False,
    )
    stored, issues = load_events(cfg.events_path)
    assert issues == []
    start = next(item for item in stored if item["type"] == "run.started")

    aliased = _readdress_start_event(
        start, argv=[sys.executable, argv_path], cwd=cwd,
    )

    assert events_module.validate_event(aliased)


@pytest.mark.parametrize(("argv_path", "cwd", "match"), [
    ("analysis/pipeline.py", "analysis", "entrypoint code path"),
    ("../analysis/pipeline.py", ".", "entrypoint code path"),
    ("analysis/pipeline.py", "analysis/..", "plan cwd must be a canonical"),
])
def test_stored_entrypoint_argv_rejects_wrong_or_escaping_cwd_resolution(
        tmp_path, argv_path, cwd, match):
    cfg, _contract, _path = _project(tmp_path)
    run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="claimtrace/primary.pipeline.json",
        scan_writes=False,
    )
    stored, issues = load_events(cfg.events_path)
    assert issues == []
    start = next(item for item in stored if item["type"] == "run.started")
    forged = _readdress_start_event(
        start, argv=[sys.executable, argv_path], cwd=cwd,
    )

    with pytest.raises(EventError, match=match):
        events_module.validate_event(forged)


def test_event_v3_requires_closed_intermediate_fields_and_exact_snapshot_role(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="claimtrace/primary.pipeline.json",
        scan_writes=False,
    )
    stored, issues = load_events(cfg.events_path)
    assert issues == []
    start = next(item for item in stored if item["type"] == "run.started")
    finish = next(item for item in stored if item["type"] == "run.finished")

    missing_start = copy.deepcopy(start)
    missing_start["payload"]["plan"].pop("declared_intermediates")
    missing_start = make_event(
        "run.started", start["run_id"], missing_start["payload"],
        recorded_at=start["recorded_at"], schema_version=events_module.CONTRACT_EVENT_SCHEMA,
    )
    with pytest.raises(EventError, match="run.started plan keys differ from schema"):
        append_event(tmp_path / "missing-start", missing_start)

    missing_finish = copy.deepcopy(finish)
    missing_finish["payload"].pop("intermediate_transitions")
    missing_finish = make_event(
        "run.finished", finish["run_id"], missing_finish["payload"],
        recorded_at=finish["recorded_at"], schema_version=events_module.CONTRACT_EVENT_SCHEMA,
    )
    with pytest.raises(EventError, match="run.finished payload keys differ from schema"):
        append_event(tmp_path / "missing-finish", missing_finish)

    mismatched_payload = copy.deepcopy(start["payload"])
    mismatched_plan = mismatched_payload["plan"]
    mismatched_plan["declared_intermediates"] = ["results/not-a-role.bin"]
    mismatched_plan["computation_id"] = (
        "computation:sha256:"
        + events_module.canonical_sha256(
            events_module._fingerprint_computation(mismatched_plan)
        )
    )
    mismatched_payload["plan_id"] = (
        "recipe:sha256:"
        + events_module.canonical_sha256(events_module._fingerprint_plan(mismatched_plan))
    )
    mismatched_start = make_event(
        "run.started", start["run_id"], mismatched_payload,
        recorded_at=start["recorded_at"], schema_version=events_module.CONTRACT_EVENT_SCHEMA,
    )
    with pytest.raises(EventError, match="do not match the pipeline contract"):
        append_event(tmp_path / "role-mismatch", mismatched_start)

    crossing_payload = copy.deepcopy(start["payload"])
    crossing_plan = crossing_payload["plan"]
    crossing_plan.pop("declared_intermediates")
    crossing_plan["computation_id"] = (
        "computation:sha256:"
        + events_module.canonical_sha256(
            events_module._fingerprint_computation(crossing_plan)
        )
    )
    crossing_payload["plan_id"] = (
        "recipe:sha256:"
        + events_module.canonical_sha256(events_module._fingerprint_plan(crossing_plan))
    )
    crossing_start = make_event(
        "run.started", start["run_id"], crossing_payload,
        recorded_at=start["recorded_at"],
        schema_version=events_module.LEGACY_CONTRACT_EVENT_SCHEMA,
    )
    with pytest.raises(
            EventError, match="event/2 requires a .*snapshot/1 contract"):
        append_event(tmp_path / "schema-crossing", crossing_start)


def test_event_v3_rejects_legacy_snapshot_v1_even_when_readdressed(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="claimtrace/primary.pipeline.json",
        scan_writes=False,
    )
    stored, issues = load_events(cfg.events_path)
    assert issues == []
    start = next(item for item in stored if item["type"] == "run.started")
    payload = copy.deepcopy(start["payload"])
    plan = payload["plan"]
    plan["pipeline_contract"] = resolve_pipeline_contract(
        cfg, "claimtrace/primary.pipeline.json",
        declared_inputs=["data/raw.csv", "analysis/pipeline.py"],
        declared_outputs=["results/fit.json"],
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        snapshot_schema="claimtrace.pipeline-contract-snapshot/1",
    )
    plan["computation_id"] = (
        "computation:sha256:"
        + events_module.canonical_sha256(events_module._fingerprint_computation(plan))
    )
    payload["plan_id"] = (
        "recipe:sha256:"
        + events_module.canonical_sha256(events_module._fingerprint_plan(plan))
    )
    forged = make_event(
        "run.started", start["run_id"], payload,
        recorded_at=start["recorded_at"],
        schema_version=events_module.CONTRACT_EVENT_SCHEMA,
    )

    with pytest.raises(EventError, match="event/3 requires a .*snapshot/2 contract"):
        append_event(tmp_path / "reverse-schema-crossing", forged)


@pytest.mark.parametrize("stage_checkpoints", [False, True])
def test_event_v3_and_v4_accept_stored_snapshot_v2(
    tmp_path, monkeypatch, stage_checkpoints,
):
    project = _instrumented_project if stage_checkpoints else _project
    cfg, _contract, _path = project(tmp_path)
    if stage_checkpoints:
        monkeypatch.setenv(
            "PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"),
        )
    original_resolve = pipeline_module.resolve_pipeline_contract

    def resolve_v2(*args, **kwargs):
        kwargs.setdefault("snapshot_schema", PREVIOUS_SNAPSHOT_SCHEMA)
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "resolve_pipeline_contract", resolve_v2)
    result = run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="claimtrace/primary.pipeline.json",
        stage_checkpoints=stage_checkpoints, scan_writes=False,
    )

    assert result["outcome"] == "succeeded"
    stored, issues = load_events(cfg.events_path)
    assert issues == []
    start = next(item for item in stored if item["type"] == "run.started")
    assert start["schema_version"] == (
        events_module.STAGE_CONTRACT_EVENT_SCHEMA
        if stage_checkpoints else events_module.CONTRACT_EVENT_SCHEMA
    )
    assert (
        start["payload"]["plan"]["pipeline_contract"]["schema_version"]
        == PREVIOUS_SNAPSHOT_SCHEMA
    )


def test_event_v3_hashes_and_links_materialized_intermediate_file(tmp_path):
    cfg, _contract, _path = _materialized_project(tmp_path)

    result = run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="claimtrace/primary.pipeline.json",
    )

    assert result["outcome"] == "succeeded"
    assert len(result["intermediate_transitions"]) == 1
    transition = result["intermediate_transitions"][0]
    assert transition["path"] == "data/clean.csv"
    assert transition["transition"] == "created"
    assert transition["produced"] is True
    assert transition["before"]["state"] == "missing"
    assert transition["after"]["state"] == "stable"
    assert transition["after"]["file_version_id"].startswith("file:sha256:")

    stored, event_issues = load_events(cfg.events_path)
    runs, run_issues = materialize_runs(stored)
    assert event_issues == run_issues == []
    plan = runs[0]["start"]["payload"]["plan"]
    finish = runs[0]["finish"]["payload"]
    assert plan["declared_intermediates"] == ["data/clean.csv"]
    assert finish["intermediate_transitions"] == [transition]
    delta = next(item for item in finish["window_deltas"]
                 if item["path"] == "data/clean.csv")
    assert delta["declared_output"] is True


def test_event_v3_records_stable_preexisting_intermediate_as_unchanged(tmp_path):
    cfg, _contract, _path = _materialized_project(tmp_path)
    clean = cfg.root / "data" / "clean.csv"
    clean.write_text("1,2", encoding="utf-8")

    result = run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="claimtrace/primary.pipeline.json",
        scan_writes=False,
    )

    transition = result["intermediate_transitions"][0]
    assert result["outcome"] == "succeeded"
    assert transition["transition"] == "unchanged"
    assert transition["produced"] is False
    assert transition["before"]["state"] == transition["after"]["state"] == "stable"
    assert transition["before"]["sha256"] == transition["after"]["sha256"]


def test_event_v3_missing_materialized_intermediate_is_contract_failure(tmp_path):
    cfg, _contract, _path = _materialized_project(tmp_path)
    code = cfg.root / "analysis" / "pipeline.py"
    code.write_text(
        code.read_text(encoding="utf-8")
        + "Path('data/clean.csv').unlink()\n",
        encoding="utf-8",
    )

    result = run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="claimtrace/primary.pipeline.json",
        scan_writes=False,
    )

    assert result["outcome"] == "contract_failed"
    assert result["intermediate_transitions"][0]["transition"] == "missing"
    assert any("materialized intermediate data/clean.csv ended as missing" == item
               for item in result["contract_errors"])


def test_event_v3_does_not_promote_pathless_intermediate_to_file_receipt(tmp_path):
    cfg, _contract, _path = _materialized_project(tmp_path)
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    intermediate = next(item for item in graph["nodes"] if item["id"] == "art:clean")
    intermediate.pop("path")
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")

    result = run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="claimtrace/primary.pipeline.json",
    )

    assert result["outcome"] == "succeeded"
    assert result["intermediate_transitions"] == []
    stored, issues = load_events(cfg.events_path)
    assert issues == []
    start = next(item for item in stored if item["type"] == "run.started")
    finish = next(item for item in stored if item["type"] == "run.finished")
    assert start["payload"]["plan"]["declared_intermediates"] == []
    assert finish["payload"]["intermediate_transitions"] == []
    delta = next(item for item in finish["payload"]["window_deltas"]
                 if item["path"] == "data/clean.csv")
    assert delta["declared_output"] is False


def test_legacy_contract_event_v2_pair_remains_readable(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="claimtrace/primary.pipeline.json",
        scan_writes=False,
    )
    current, issues = load_events(cfg.events_path)
    assert issues == []
    current_start = next(item for item in current if item["type"] == "run.started")
    current_finish = next(item for item in current if item["type"] == "run.finished")

    plan = copy.deepcopy(current_start["payload"]["plan"])
    plan.pop("declared_intermediates")
    plan["pipeline_contract"] = resolve_pipeline_contract(
        cfg, "claimtrace/primary.pipeline.json",
        declared_inputs=["data/raw.csv", "analysis/pipeline.py"],
        declared_outputs=["results/fit.json"],
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        snapshot_schema="claimtrace.pipeline-contract-snapshot/1",
    )
    plan["computation_id"] = (
        "computation:sha256:"
        + events_module.canonical_sha256(events_module._fingerprint_computation(plan))
    )
    plan_id = (
        "recipe:sha256:"
        + events_module.canonical_sha256(events_module._fingerprint_plan(plan))
    )
    start_payload = copy.deepcopy(current_start["payload"])
    start_payload["plan"] = plan
    start_payload["plan_id"] = plan_id
    legacy_start = make_event(
        "run.started", current_start["run_id"], start_payload,
        recorded_at=current_start["recorded_at"],
        schema_version=events_module.LEGACY_CONTRACT_EVENT_SCHEMA,
    )

    finish_payload = copy.deepcopy(current_finish["payload"])
    finish_payload["start_event_id"] = legacy_start["id"]
    finish_payload["plan_id"] = plan_id
    finish_payload.pop("intermediate_transitions")
    finish_payload["lineage_coverage"].pop("declared_intermediates")
    result_basis = {
        "plan_id": plan_id,
        "outcome": finish_payload["outcome"],
        "direct_child_returncode": finish_payload["direct_child_returncode"],
        "input_transitions": [
            events_module._fingerprint_transition(item)
            for item in finish_payload["input_transitions"]
        ],
        "output_transitions": [
            events_module._fingerprint_transition(item)
            for item in finish_payload["output_transitions"]
        ],
        "contract_errors": finish_payload["contract_errors"],
    }
    finish_payload["result_id"] = (
        "result:sha256:" + events_module.canonical_sha256(result_basis)
    )
    legacy_finish = make_event(
        "run.finished", current_finish["run_id"], finish_payload,
        recorded_at=current_finish["recorded_at"],
        schema_version=events_module.LEGACY_CONTRACT_EVENT_SCHEMA,
    )

    legacy_store = tmp_path / "legacy-events"
    append_event(legacy_store, legacy_start)
    append_event(legacy_store, legacy_finish)
    loaded, event_issues = load_events(legacy_store)
    runs, run_issues = materialize_runs(loaded)
    assert event_issues == run_issues == []
    assert len(runs) == 1
    assert {item["schema_version"] for item in loaded} == {"claimtrace.event/2"}

    forged_legacy_start = _readdress_start_event(
        legacy_start, argv=[sys.executable, "-c", "print('unrelated legacy')"],
    )
    with pytest.raises(EventError, match="entrypoint code path"):
        events_module.validate_event(forged_legacy_start)


def test_contract_run_requires_entrypoint_as_exact_argv_path(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    # Contract validation itself remains valid; the events layer rejects this
    # unrelated executable as an entrypoint before it launches.
    _resolve(cfg)
    with pytest.raises(EventError, match="entrypoint code path"):
        run_command(
            cfg, [sys.executable, "-c", "raise SystemExit('must not run')"],
            inputs=["data/raw.csv", "analysis/pipeline.py"],
            outputs=["results/fit.json"], cwd=str(cfg.root),
            parameters={"model": "ols"}, seeds={"numpy": "7"},
            pipeline_contract="claimtrace/primary.pipeline.json",
            scan_writes=False,
        )


def test_contract_run_rejects_absolute_entrypoint_argv_before_launch(
        tmp_path, monkeypatch):
    cfg, _contract, _path = _project(tmp_path)
    launched = False

    def forbidden_launch(*_args, **_kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("the child must not launch")

    monkeypatch.setattr(events_module.subprocess, "Popen", forbidden_launch)
    with pytest.raises(EventError, match="absolute paths.*not accepted"):
        run_command(
            cfg, [sys.executable, str(cfg.root / "analysis" / "pipeline.py")],
            inputs=["data/raw.csv", "analysis/pipeline.py"],
            outputs=["results/fit.json"], cwd=str(cfg.root),
            parameters={"model": "ols"}, seeds={"numpy": "7"},
            pipeline_contract="claimtrace/primary.pipeline.json",
            scan_writes=False,
        )
    assert launched is False


def test_method_or_contract_drift_during_child_makes_run_contract_fail(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    code = cfg.root / "analysis" / "pipeline.py"
    code.write_text(
        code.read_text(encoding="utf-8")
        + "\nPath('methods.md').write_text('changed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    result = run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="claimtrace/primary.pipeline.json",
        scan_writes=False,
    )
    assert result["outcome"] == "contract_failed"
    assert any("pipeline contract" in item and "changed" in item
               for item in result["contract_errors"])
