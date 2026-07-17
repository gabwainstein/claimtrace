"""Cross-layer claim readiness never confuses semantics, replay, and method review."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from provsleuth.assessment import (
    append_assessment,
    create_assessment,
    transition_review,
)
from provsleuth.config import Config
from provsleuth.events import (
    _fingerprint_transition,
    append_event,
    load_events,
    make_event,
    materialize_runs,
    run_command,
)
from provsleuth.method_assessment import (
    append_method_assessment,
    create_method_assessment,
    transition_method_review,
)
from provsleuth.pipeline import canonical_sha256
from provsleuth.replay import (
    append_replay_certificate,
    load_replay_certificates,
    replay_run,
)
from provsleuth.report import _claim_ancestry_intermediates, build_report
from provsleuth.view import render_view
from test_method_assessment import _agent_input as _method_agent_input
from test_pipeline import _instrumented_project, _materialized_project, _project
from test_report import _semantic_input
from test_view import _payload


def _content_addressed_path(store, identifier):
    digest = identifier.rsplit(":", 1)[1]
    return store / digest[:2] / f"{digest}.json"


def test_legacy_snapshot_exposes_uncaptured_path_bearing_ancestry_intermediate():
    run = {
        "pipeline_contract": {
            "schema_version": "claimtrace.pipeline-contract-snapshot/1",
            "roles": {
                "inputs": [],
                "outputs": [{"node_id": "art:fit", "path": "results/fit.json"}],
            },
        },
        "bindings": [],
    }
    ancestry = [{
        "id": "clean", "produces_node_ids": ["art:clean"],
    }, {
        "id": "fit", "produces_node_ids": ["art:fit"],
    }]
    nodes = {
        "art:clean": {"id": "art:clean", "path": "data/clean.csv"},
        "art:fit": {"id": "art:fit", "path": "results/fit.json"},
    }

    state, items = _claim_ancestry_intermediates(run, ancestry, nodes)

    assert state == "legacy_capture_unavailable"
    assert items == [{
        "node_id": "art:clean",
        "path": "data/clean.csv",
        "binding_state": "legacy_capture_unavailable",
        "receipt_snapshot": None,
    }]


def _make_directory_link(target, link):
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError) as exc:
        if os.name != "nt":
            pytest.skip(f"directory links are unavailable on this platform: {exc}")
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if created.returncode != 0:
            pytest.skip(f"directory links and junctions are unavailable: {exc}")


def _remove_directory_link(link):
    if link.is_symlink():
        link.unlink()
    else:
        os.rmdir(link)


def _complete_project(
        tmp_path, *, undeclared_write=False, materialized=False,
        random_intermediate=False, split_methods=False, review_fit=True,
        include_fit_requirement=True, unrelated_branch=False,
        include_off_ancestry_requirement=False, stage_checkpoints=False):
    if stage_checkpoints and materialized:
        raise ValueError("instrumented materialized fixture is not defined")
    if stage_checkpoints:
        cfg, contract, contract_path = _instrumented_project(tmp_path)
    elif materialized:
        cfg, contract, contract_path = _materialized_project(
            tmp_path, random_intermediate=random_intermediate,
        )
    else:
        cfg, contract, contract_path = _project(tmp_path)
    if undeclared_write:
        code_path = cfg.root / "analysis" / "pipeline.py"
        code_path.write_text(
            code_path.read_text(encoding="utf-8")
            + "Path('scratch.txt').write_text('stable scratch')\n",
            encoding="utf-8",
        )
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    if split_methods:
        primary = next(item for item in graph["nodes"] if item["id"] == "method:primary")
        primary.update({
            "id": "method:preprocess", "path": "methods-preprocess.md",
            "method_spec": {
                "schema_version": "claimtrace.method-spec/1",
                "steps": [{
                    "id": "complete", "statement": "Remove incomplete rows.",
                    "required": True,
                }],
            },
        })
        graph["nodes"].append({
            "id": "method:fit", "type": "method", "status": "current",
            "path": "methods-fit.md",
            "method_spec": {
                "schema_version": "claimtrace.method-spec/1",
                "steps": [{
                    "id": "fit", "statement": "Fit ordinary least squares.",
                    "required": True,
                }],
            },
        })
        (cfg.root / "methods-preprocess.md").write_text(
            "# Preprocessing\nRemove incomplete rows.\n", encoding="utf-8",
        )
        (cfg.root / "methods-fit.md").write_text(
            "# Fit\nFit ordinary least squares.\n", encoding="utf-8",
        )
        contract["stages"][0]["method_id"] = "method:preprocess"
        contract["stages"][1]["method_id"] = "method:fit"
        contract_path.write_text(json.dumps(contract), encoding="utf-8")
        claim_methods = [
            {"method_id": "method:preprocess", "step_ids": ["complete"]},
        ]
        if include_fit_requirement:
            claim_methods.append({"method_id": "method:fit", "step_ids": ["fit"]})
    else:
        claim_methods = [{
            "method_id": "method:primary", "step_ids": ["complete", "fit"],
        }]
    declared_outputs = ["results/fit.json"]
    if unrelated_branch:
        code_path = cfg.root / "analysis" / "pipeline.py"
        code_path.write_text(
            code_path.read_text(encoding="utf-8")
            + "Path('results/other.json').write_text('other')\n",
            encoding="utf-8",
        )
        code_lines = code_path.read_bytes().splitlines(keepends=True)
        other_line = len(code_lines)
        graph["nodes"].extend([
            {
                "id": "method:other", "type": "method", "status": "current",
                "path": "methods-other.md",
                "method_spec": {
                    "schema_version": "claimtrace.method-spec/1",
                    "steps": [{
                        "id": "summarize", "statement": "Write the unrelated summary.",
                        "required": True,
                    }],
                },
            },
            {
                "id": "art:other", "type": "artifact", "status": "current",
                "path": "results/other.json",
            },
        ])
        (cfg.root / "methods-other.md").write_text(
            "# Other\nWrite the unrelated summary.\n", encoding="utf-8",
        )
        contract["output_node_ids"].append("art:other")
        contract["stages"].append({
            "id": "other", "depends_on": [],
            "method_id": "method:other", "method_step_id": "summarize",
            "consumes_node_ids": ["data:raw"],
            "produces_node_ids": ["art:other"],
            "code_anchors": [{
                "code_node_id": "code:pipeline", "kind": "text_lines",
                "start_line": other_line, "end_line": other_line,
                "text_sha256": hashlib.sha256(code_lines[-1]).hexdigest(),
            }],
        })
        declared_outputs.append("results/other.json")
        if include_off_ancestry_requirement:
            claim_methods.append({
                "method_id": "method:other", "step_ids": ["summarize"],
            })
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    graph["nodes"].append({
        "id": "claim:fit", "type": "claim", "status": "current",
        "value": "The recorded analysis result is positive.",
        "method_requirements": {
            "schema_version": "claimtrace.method-requirements/1",
            "methods": claim_methods,
        },
    })
    graph["edges"] = (
        [
            {"from": "data:raw", "to": "art:clean", "rel": "produces"},
            {"from": "code:pipeline", "to": "art:clean", "rel": "produces"},
            {"from": "art:clean", "to": "art:fit", "rel": "produces"},
            {"from": "code:pipeline", "to": "art:fit", "rel": "produces"},
            {"from": "art:fit", "to": "claim:fit", "rel": "derives_from"},
        ] if materialized else [
            {"from": "data:raw", "to": "art:fit", "rel": "produces"},
            {"from": "code:pipeline", "to": "art:fit", "rel": "produces"},
            {"from": "art:fit", "to": "claim:fit", "rel": "derives_from"},
        ]
    )
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    config = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    config["require_assessments"] = True
    config["execution"] = {
        "replays": "provsleuth/replays",
        "method_assessments": "provsleuth/method-assessments",
        "require_contracts": True,
        "require_replay": True,
        "require_method_assessments": True,
        "require_stage_checkpoints": stage_checkpoints,
        "replay_attempts": 2,
    }
    cfg.config_path.write_text(json.dumps(config), encoding="utf-8")
    cfg = Config(cfg.config_path)

    run = run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=declared_outputs, cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="provsleuth/primary.pipeline.json", scan_writes=False,
    )
    replay_run(cfg, run["run_id"])

    methods_to_review = [
        ("method:primary", [
            {"method_step_id": "complete", "stage_id": "complete", "alignment": "match"},
            {"method_step_id": "fit", "stage_id": "fit", "alignment": "match"},
        ])
    ]
    if split_methods:
        methods_to_review = [("method:preprocess", [{
            "method_step_id": "complete", "stage_id": "complete", "alignment": "match",
        }])]
        if review_fit:
            methods_to_review.append(("method:fit", [{
                "method_step_id": "fit", "stage_id": "fit", "alignment": "match",
            }]))
    method_reviews = []
    for method_id, alignments in methods_to_review:
        agent_input = _method_agent_input()
        agent_input["step_alignments"] = alignments
        agent_input["rationale"] = (
            "The exact declared code anchor implements the selected method step or steps."
        )
        method = create_method_assessment(
            cfg, "provsleuth/primary.pipeline.json", method_id,
            agent_input, actor="agent:test",
            declared_inputs=["data/raw.csv", "analysis/pipeline.py"],
            declared_outputs=declared_outputs,
            parameters={"model": "ols"}, seeds={"numpy": "7"},
            recorded_at="2026-07-15T01:00:00.000Z",
        )
        append_method_assessment(cfg, method)
        method_review = transition_method_review(
            cfg, method, "accepted", actor="scientist:method",
            recorded_at="2026-07-15T01:01:00.000Z",
        )
        append_method_assessment(cfg, method_review)
        method_reviews.append(method_review)

    semantic_input = _semantic_input()
    semantic_input["evidence_anchors"] = [{
        "result_id": "art:fit", "kind": "text_lines",
        "start_line": 1, "end_line": 1,
        "text_sha256": hashlib.sha256(b"1").hexdigest(),
    }]
    semantic = create_assessment(
        cfg, "claim:fit", ["art:fit"], semantic_input,
        actor="test-reviewer", recorded_at="2026-07-15T01:02:00.000Z",
    )
    append_assessment(cfg, semantic)
    semantic_review = transition_review(
        cfg, semantic, "accepted", actor="scientist:semantic",
        assessments=[semantic], recorded_at="2026-07-15T01:03:00.000Z",
    )
    append_assessment(cfg, semantic_review)
    return cfg, run, method_reviews[-1], semantic_review


def test_complete_cross_layer_basis_is_ready_without_overclaiming_stage_observation(tmp_path):
    cfg, run, method_review, semantic_review = _complete_project(tmp_path)

    report = build_report(cfg, strict=True)

    basis = report["claim_basis"]["items"]
    assert len(basis) == 1
    assert basis[0]["assessment_id"] == semantic_review["id"]
    assert basis[0]["run_id"] == run["run_id"]
    assert basis[0]["method_assessment_ids"] == [method_review["id"]]
    assert basis[0]["replay_state"] == "byte_repeatable_current"
    assert basis[0]["method_state"] == "accepted_current_conformance"
    assert basis[0]["producer_stage_id"] == "fit"
    assert basis[0]["ancestry_stage_ids"] == ["complete", "fit"]
    assert basis[0]["ancestry_method_steps"] == [
        {
            "stage_id": "complete", "method_id": "method:primary",
            "method_step_id": "complete",
        },
        {
            "stage_id": "fit", "method_id": "method:primary",
            "method_step_id": "fit",
        },
    ]
    assert basis[0]["materialized_intermediate_state"] == "not_required"
    assert basis[0]["stage_execution_observation"] == "declared_only_not_observed"
    assert basis[0]["producer_stage_id"] == "fit"
    assert basis[0]["ancestry_state"] == "current"
    assert basis[0]["materialized_intermediate_state"] == "not_required"
    assert basis[0]["overall"] == "ready_under_reviewed_provenance"
    assert basis[0]["scientific_validity"] == "not_assessed"
    assert basis[0]["configured_policy_pass"] is True
    assert report["ok"] is True

    output = tmp_path / "trajectory.html"
    render_view(cfg, output)
    html = output.read_text(encoding="utf-8")
    payload = _payload(html)
    claim_node = next(
        node for node in payload["nodes"] if node.get("node_id") == "claim:fit"
    )
    assert claim_node["claim_basis"][0]["producer_stage_id"] == "fit"
    assert "Claim producer stages" in html
    assert "Exact method ancestry" in html
    assert "Ancestry materialized intermediates" in html
    assert "Off-ancestry claim method steps" in html


def test_review_ready_replacement_keeps_stale_history_visible_but_nonblocking(
        tmp_path):
    cfg, old_run, old_method_review, _semantic_review = _complete_project(tmp_path)
    code_path = cfg.root / "analysis" / "pipeline.py"
    code_path.write_text(
        code_path.read_text(encoding="utf-8") + "# implementation revision\n",
        encoding="utf-8",
    )
    (cfg.root / "results" / "fit.json").unlink()
    new_run = run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="provsleuth/primary.pipeline.json", scan_writes=False,
    )
    replay_run(cfg, new_run["run_id"])

    replacement_input = _method_agent_input()
    replacement_input["provenance"]["agent"] = "agent:replacement"
    replacement = create_method_assessment(
        cfg, "provsleuth/primary.pipeline.json", "method:primary",
        replacement_input, actor="agent:replacement",
        declared_inputs=["data/raw.csv", "analysis/pipeline.py"],
        declared_outputs=["results/fit.json"],
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        recorded_at="2026-07-16T01:00:00.000Z",
    )
    append_method_assessment(cfg, replacement)
    replacement_review = transition_method_review(
        cfg, replacement, "accepted", actor="scientist:replacement",
        recorded_at="2026-07-16T01:01:00.000Z",
    )
    append_method_assessment(cfg, replacement_review)

    report = build_report(cfg, strict=True)

    assert report["ok"] is True
    assert report["claim_basis"]["items"][0]["run_id"] == new_run["run_id"]
    old_projected = next(
        item for item in report["receipts"]["runs"]
        if item["run_id"] == old_run["run_id"]
    )
    assert old_projected["pipeline_contract_state"] == "stale_or_invalid"
    assert old_projected["current_gate_role"] == "historical_replaced"
    assert old_projected["replacement_run_id"] == new_run["run_id"]
    assert old_projected["replays"][0]["current_derived"]["current"] is False
    assert old_projected["replays"][0]["current_gate_role"] == (
        "historical_replaced"
    )
    old_method = next(
        item for item in report["method_assessments"]["items"]
        if item["id"] == old_method_review["id"]
    )
    assert old_method["current_derived"]["stale"] is True
    assert old_method["current_gate_role"] == "historical_replaced"
    historical_codes = {
        "RUN_PIPELINE_CONTRACT_STALE",
        "REPLAY_INPUT_DRIFT",
        "REPLAY_CONTRACT_DRIFT",
        "METHOD_CONFORMANCE_STALE",
    }
    historical_findings = [
        item for item in report["findings"] if item["code"] in historical_codes
    ]
    assert {item["code"] for item in historical_findings} == historical_codes
    assert all(
        item["severity"] == "info" and item["blocking"] is False
        for item in historical_findings
    )


def test_unreplayed_new_run_does_not_demote_stale_historical_receipt(tmp_path):
    cfg, old_run, _old_method_review, _semantic_review = _complete_project(tmp_path)
    code_path = cfg.root / "analysis" / "pipeline.py"
    code_path.write_text(
        code_path.read_text(encoding="utf-8") + "# unreplayed revision\n",
        encoding="utf-8",
    )
    (cfg.root / "results" / "fit.json").unlink()
    new_run = run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="provsleuth/primary.pipeline.json", scan_writes=False,
    )
    assert new_run["outcome"] == "succeeded"

    report = build_report(cfg, strict=True)

    old_projected = next(
        item for item in report["receipts"]["runs"]
        if item["run_id"] == old_run["run_id"]
    )
    assert "current_gate_role" not in old_projected
    finding = next(
        item for item in report["findings"]
        if item["code"] == "RUN_PIPELINE_CONTRACT_STALE"
        and old_run["run_id"] in item["detail"]
    )
    assert finding["severity"] == "warning"
    assert finding["blocking"] is True


def test_required_cooperative_stage_trace_is_repeatable_but_not_observation(
        tmp_path, monkeypatch):
    monkeypatch.setenv(
        "PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"),
    )
    cfg, run, _method_review, _semantic_review = _complete_project(
        tmp_path, stage_checkpoints=True,
    )

    report = build_report(cfg, strict=True)
    basis = report["claim_basis"]["items"][0]

    assert run["stage_trace"]["state"] == "cooperative_report_complete"
    assert basis["stage_checkpoint_state"] == (
        "cooperative_report_repeatable_current"
    )
    assert basis["stage_execution_observation"] == (
        "cooperative_checkpoint_self_report_not_independent_observation"
    )
    assert basis["overall"] == "ready_under_reviewed_provenance"
    assert basis["configured_policy_pass"] is True
    assert basis["scientific_validity"] == "not_assessed"
    assert report["ok"] is True


def test_unreviewed_fit_method_on_result_ancestry_blocks_readiness(tmp_path):
    cfg, _run, _method_review, _semantic_review = _complete_project(
        tmp_path, split_methods=True, review_fit=False,
    )

    report = build_report(cfg, strict=True)
    basis = report["claim_basis"]["items"][0]

    assert basis["ancestry_stage_ids"] == ["complete", "fit"]
    assert basis["required_method_ids"] == ["method:fit", "method:preprocess"]
    assert basis["method_state"] == "method_conformance_missing_or_not_current"
    assert basis["missing_method_assessment_steps"] == [{
        "stage_id": "fit", "method_id": "method:fit", "method_step_id": "fit",
    }]
    assert basis["overall"] != "ready_under_reviewed_provenance"
    assert basis["configured_policy_pass"] is False
    assert report["ok"] is False


def test_claim_cannot_omit_fit_method_step_from_result_ancestry(tmp_path):
    cfg, _run, _method_review, _semantic_review = _complete_project(
        tmp_path, split_methods=True, review_fit=True, include_fit_requirement=False,
    )

    report = build_report(cfg, strict=True)
    basis = report["claim_basis"]["items"][0]

    assert basis["method_state"] == "claim_requirements_missing_ancestry_steps"
    assert basis["missing_claim_method_steps"] == [{
        "method_id": "method:fit", "method_step_id": "fit",
    }]
    assert basis["overall"] != "ready_under_reviewed_provenance"
    assert basis["configured_policy_pass"] is False
    assert report["ok"] is False


def test_two_method_fully_reviewed_ancestry_remains_ready(tmp_path):
    cfg, _run, _method_review, _semantic_review = _complete_project(
        tmp_path, split_methods=True, review_fit=True,
    )

    report = build_report(cfg, strict=True)
    basis = report["claim_basis"]["items"][0]

    assert basis["method_state"] == "accepted_current_conformance"
    assert len(basis["method_assessment_ids"]) == 2
    assert basis["missing_claim_method_steps"] == []
    assert basis["missing_method_assessment_steps"] == []
    assert basis["overall"] == "ready_under_reviewed_provenance"
    assert basis["configured_policy_pass"] is True
    assert basis["scientific_validity"] == "not_assessed"
    assert report["ok"] is True


def test_unrelated_contract_branch_is_out_of_scope_for_claim_readiness(tmp_path):
    cfg, _run, _method_review, _semantic_review = _complete_project(
        tmp_path, unrelated_branch=True,
    )

    report = build_report(cfg, strict=True)
    basis = report["claim_basis"]["items"][0]

    assert basis["ancestry_stage_ids"] == ["complete", "fit"]
    assert "other" not in basis["ancestry_stage_ids"]
    assert basis["required_method_ids"] == ["method:primary"]
    assert basis["off_ancestry_declared_method_steps"] == []
    assert basis["overall"] == "ready_under_reviewed_provenance"
    assert basis["configured_policy_pass"] is True
    assert report["ok"] is True


def test_claim_owned_requirement_for_off_ancestry_step_blocks_readiness(tmp_path):
    cfg, _run, _method_review, _semantic_review = _complete_project(
        tmp_path, unrelated_branch=True, include_off_ancestry_requirement=True,
    )

    report = build_report(cfg, strict=True)
    basis = report["claim_basis"]["items"][0]

    assert basis["ancestry_stage_ids"] == ["complete", "fit"]
    assert basis["method_state"] == "claim_requirements_include_off_ancestry_steps"
    assert basis["off_ancestry_declared_method_steps"] == [{
        "method_id": "method:other", "method_step_id": "summarize",
    }]
    assert basis["overall"] != "ready_under_reviewed_provenance"
    assert basis["configured_policy_pass"] is False
    assert report["ok"] is False


def test_method_drift_deactivates_readiness_without_erasing_semantic_review(tmp_path):
    cfg, _run, _method_review, semantic_review = _complete_project(tmp_path)
    (cfg.root / "methods.md").write_text("# Changed methods\n", encoding="utf-8")

    report = build_report(cfg, strict=True)

    assert report["assessments"]["active_relations"][0]["assessment_id"] == (
        semantic_review["id"]
    )
    basis = report["claim_basis"]["items"][0]
    assert basis["overall"] != "ready_under_reviewed_provenance"
    assert basis["configured_policy_pass"] is False
    assert report["ok"] is False


def test_repeatable_undeclared_workspace_write_never_becomes_claim_ready(tmp_path):
    cfg, _run, _method_review, _semantic_review = _complete_project(
        tmp_path, undeclared_write=True,
    )

    report = build_report(cfg, strict=True)

    replay = report["receipts"]["runs"][0]["replays"][0]
    assert replay["current_derived"]["byte_repeatable_current"] is True
    assert replay["current_derived"]["review_ready_current"] is False
    assert replay["undeclared_write_paths"] == ["scratch.txt"]
    basis = report["claim_basis"]["items"][0]
    assert basis["replay_state"] == (
        "byte_repeatable_with_undeclared_workspace_writes"
    )
    assert basis["overall"] != "ready_under_reviewed_provenance"
    assert basis["configured_policy_pass"] is False
    assert any(
        item["code"] == "REPLAY_UNDECLARED_WORKSPACE_WRITE"
        and item["severity"] == "warning"
        for item in report["findings"]
    )
    assert report["ok"] is False


def test_materialized_intermediate_is_separate_and_ready_when_bytes_repeat(tmp_path):
    cfg, _run, _method_review, _semantic_review = _complete_project(
        tmp_path, materialized=True,
    )

    report = build_report(cfg, strict=True)
    projected_run = report["receipts"]["runs"][0]
    replay = projected_run["replays"][0]
    intermediate_binding = next(
        item for item in projected_run["bindings"]
        if item["binding_kind"] == "materialized_intermediate_path"
    )

    assert projected_run["declared_intermediates"] == ["data/clean.csv"]
    assert projected_run["intermediate_transitions"][0]["path"] == "data/clean.csv"
    assert projected_run["output_transitions"][0]["path"] == "results/fit.json"
    assert intermediate_binding["node_id"] == "art:clean"
    assert intermediate_binding["stage_attribution"] == "not_observed"
    assert replay["comparison"]["materialized_intermediates_equal"] is True
    assert replay["comparison"][
        "all_materialized_intermediates_match_source_receipt"
    ] is True
    assert replay["source_materialized_intermediates"][0]["path"] == "data/clean.csv"
    assert replay["source_materialized_intermediates"][0]["sha256"] == (
        projected_run["intermediate_transitions"][0]["after"]["sha256"]
    )
    assert report["claim_basis"]["items"][0]["overall"] == (
        "ready_under_reviewed_provenance"
    )
    assert report["ok"] is True

    output = tmp_path / "trajectory.html"
    render_view(cfg, output)
    payload = _payload(output.read_text(encoding="utf-8"))
    receipt = next(item for item in payload["nodes"] if item["kind"] == "run")
    assert receipt["declared_intermediates"] == ["data/clean.csv"]
    assert receipt["intermediate_transitions"][0]["path"] == "data/clean.csv"
    edge = next(
        item for item in payload["edges"]
        if item.get("binding_kind") == "materialized_intermediate_path"
    )
    assert edge["relation"] == "binds materialized intermediate"


def test_nondeterministic_materialized_intermediate_blocks_replay_readiness(tmp_path):
    cfg, _run, _method_review, _semantic_review = _complete_project(
        tmp_path, materialized=True, random_intermediate=True,
    )

    report = build_report(cfg, strict=True)
    replay = report["receipts"]["runs"][0]["replays"][0]

    assert replay["outcome"] == "repeatability_mismatch"
    assert replay["comparison"]["declared_outputs_equal"] is True
    assert replay["comparison"]["all_declared_outputs_match_source_receipt"] is True
    assert replay["comparison"]["materialized_intermediates_equal"] is False
    assert replay["current_derived"]["review_ready_current"] is False
    assert report["claim_basis"]["items"][0]["overall"] != (
        "ready_under_reviewed_provenance"
    )
    assert report["claim_basis"]["items"][0]["configured_policy_pass"] is False
    assert report["ok"] is False


def test_materialized_intermediate_drift_deactivates_claim_readiness(tmp_path):
    cfg, _run, _method_review, _semantic_review = _complete_project(
        tmp_path, materialized=True,
    )
    (cfg.root / "data" / "clean.csv").write_text("drifted\n", encoding="utf-8")

    report = build_report(cfg, strict=True)
    basis = report["claim_basis"]["items"][0]

    assert basis["ancestry_stage_ids"] == ["complete", "fit"]
    assert basis["materialized_intermediate_state"] == "missing_or_stale"
    assert basis["ancestry_materialized_intermediates"][0]["node_id"] == "art:clean"
    assert basis["ancestry_materialized_intermediates"][0]["binding_state"] == (
        "missing_or_stale"
    )
    assert basis["overall"] != "ready_under_reviewed_provenance"
    assert basis["configured_policy_pass"] is False
    assert any(
        item["code"] == "CLAIM_ANCESTRY_INTERMEDIATE_INCOMPLETE"
        for item in report["findings"]
    )
    assert report["ok"] is False


def test_readdressed_run_input_baseline_mismatch_quarantines_all_bindings(tmp_path):
    cfg, run, _method_review, _semantic_review = _complete_project(tmp_path)
    events, event_issues = load_events(cfg.events_path)
    assert event_issues == []
    finish = next(item for item in events if item["type"] == "run.finished")
    forged_payload = json.loads(json.dumps(finish["payload"]))
    for transition in forged_payload["input_transitions"]:
        forged = dict(transition["before"])
        forged["sha256"] = "f" * 64
        forged["file_version_id"] = "file:sha256:" + "f" * 64
        transition.update({
            "transition": "unchanged", "before": forged, "after": dict(forged),
        })
    result_basis = {
        "plan_id": forged_payload["plan_id"],
        "outcome": forged_payload["outcome"],
        "direct_child_returncode": forged_payload["direct_child_returncode"],
        "input_transitions": [
            _fingerprint_transition(item)
            for item in forged_payload["input_transitions"]
        ],
        "output_transitions": [
            _fingerprint_transition(item)
            for item in forged_payload["output_transitions"]
        ],
        "contract_errors": forged_payload["contract_errors"],
        "intermediate_transitions": [
            _fingerprint_transition(item)
            for item in forged_payload["intermediate_transitions"]
        ],
    }
    forged_payload["result_id"] = "result:sha256:" + canonical_sha256(result_basis)
    forged_finish = make_event(
        "run.finished", finish["run_id"], forged_payload,
        recorded_at=finish["recorded_at"], schema_version=finish["schema_version"],
    )
    _content_addressed_path(cfg.events_path, finish["id"]).unlink()
    append_event(cfg.events_path, forged_finish)

    certificates, replay_issues = load_replay_certificates(cfg.replays_path)
    assert replay_issues == [] and len(certificates) == 1
    certificate = json.loads(json.dumps(certificates[0]))
    old_certificate_id = certificate["id"]
    certificate["source_finish_event_id"] = forged_finish["id"]
    core = {key: value for key, value in certificate.items() if key != "id"}
    certificate["id"] = "replay:sha256:" + canonical_sha256(core)
    _content_addressed_path(cfg.replays_path, old_certificate_id).unlink()
    append_replay_certificate(cfg.replays_path, certificate)

    stored_events, event_issues = load_events(cfg.events_path)
    _runs, run_issues = materialize_runs(stored_events)
    assert event_issues == []
    assert {item["code"] for item in run_issues} == {
        "RUN_INPUT_SNAPSHOT_MISMATCH",
    }
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    graph["nodes"].append({
        "id": "exp:history", "type": "experiment", "status": "confirmed",
        "run_ids": [run["run_id"]],
    })
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")

    report = build_report(cfg, strict=True)
    projected_run = next(
        item for item in report["receipts"]["runs"] if item["run_id"] == run["run_id"]
    )
    assert report["receipts"]["event_store_integrity"] == "ok"
    assert report["receipts"]["run_link_integrity"] == "error"
    assert projected_run["link_integrity"] == "error"
    assert projected_run["link_issues"] == [{
        "code": "RUN_INPUT_SNAPSHOT_MISMATCH",
        "detail": (
            "finish before snapshot for analysis/pipeline.py differs from the start "
            "declared-input snapshot"
        ),
    }, {
        "code": "RUN_INPUT_SNAPSHOT_MISMATCH",
        "detail": (
            "finish before snapshot for data/raw.csv differs from the start "
            "declared-input snapshot"
        ),
    }]
    assert projected_run["evidence_eligible"] is False
    assert not any(
        item["binding_kind"] in {
            "output_path", "output_validation", "materialized_intermediate_path",
        }
        for item in projected_run["bindings"]
    )
    explicit = next(
        item for item in projected_run["bindings"]
        if item["binding_kind"] == "explicit_run_reference"
    )
    assert explicit["node_id"] == "exp:history"
    assert explicit["current"] is False
    assert explicit["integrity_state"] == "quarantined"
    replay = projected_run["replays"][0]
    assert replay["current_derived"]["review_ready_current"] is False
    assert any(
        item["code"] == "REPLAY_SOURCE_RUN_LINK_INTEGRITY"
        for item in replay["current_derived"]["findings"]
    )
    basis = report["claim_basis"]["items"][0]
    assert basis["overall"] != "ready_under_reviewed_provenance"
    assert basis["configured_policy_pass"] is False
    assert report["ok"] is False


def test_corrupt_replay_store_suppresses_otherwise_valid_certificate(tmp_path):
    cfg, run, _method_review, _semantic_review = _complete_project(tmp_path)
    (cfg.replays_path / "corrupt.json").write_text("{", encoding="utf-8")

    report = build_report(cfg, strict=True)
    projected_run = next(
        item for item in report["receipts"]["runs"] if item["run_id"] == run["run_id"]
    )
    replay = projected_run["replays"][0]

    assert report["receipts"]["replay_integrity"] == "error"
    assert projected_run["evidence_eligible"] is True
    assert replay["current_derived"]["current"] is False
    assert replay["current_derived"]["byte_repeatable_current"] is False
    assert replay["current_derived"]["review_ready_current"] is False
    assert any(
        item["code"] == "REPLAY_STORE_INTEGRITY"
        for item in replay["current_derived"]["findings"]
    )
    basis = report["claim_basis"]["items"][0]
    assert basis["execution_state"] == "current_contract_bound_producing_receipt"
    assert basis["replay_state"] == "present_but_not_current_or_repeatable"
    assert basis["overall"] != "ready_under_reviewed_provenance"
    assert basis["configured_policy_pass"] is False
    assert report["ok"] is False


def test_current_replay_mismatch_suppresses_current_positive_certificate(tmp_path):
    cfg, run, _method_review, _semantic_review = _complete_project(tmp_path)
    certificates, replay_issues = load_replay_certificates(cfg.replays_path)
    assert replay_issues == [] and len(certificates) == 1
    positive_id = certificates[0]["id"]

    mismatch = json.loads(json.dumps(certificates[0]))
    mismatch["observed_at"] = "2026-07-15T12:00:00Z"
    changed = mismatch["attempts"][1]["outputs"][0]
    changed["sha256"] = "f" * 64
    changed["file_version_id"] = "file:sha256:" + "f" * 64
    mismatch["comparison"]["declared_outputs_equal"] = False
    mismatch["comparison"]["all_declared_outputs_match_source_receipt"] = False
    mismatch["outcome"] = "repeatability_mismatch"
    core = {key: value for key, value in mismatch.items() if key != "id"}
    mismatch["id"] = "replay:sha256:" + canonical_sha256(core)
    append_replay_certificate(cfg.replays_path, mismatch)

    report = build_report(cfg, strict=True)
    projected_run = next(
        item for item in report["receipts"]["runs"] if item["run_id"] == run["run_id"]
    )

    assert projected_run["replay_conflict"] is True
    assert projected_run["replay_conflict_certificate_ids"] == sorted([
        positive_id, mismatch["id"],
    ])
    assert all(
        item["current_derived"]["review_ready_current"] is False
        for item in projected_run["replays"]
    )
    assert any(
        item["code"] == "REPLAY_CURRENT_EVIDENCE_CONFLICT"
        and item["severity"] == "error"
        for item in report["findings"]
    )
    basis = report["claim_basis"]["items"][0]
    assert basis["replay_state"] == "present_but_not_current_or_repeatable"
    assert basis["overall"] != "ready_under_reviewed_provenance"
    assert basis["configured_policy_pass"] is False
    assert report["ok"] is False


def test_linked_replay_store_child_suppresses_otherwise_valid_certificate(tmp_path):
    cfg, run, _method_review, _semantic_review = _complete_project(tmp_path)
    external = tmp_path / "external-replay-store"
    external.mkdir()
    linked = cfg.replays_path / "linked"
    _make_directory_link(external, linked)
    try:
        certificates, replay_issues = load_replay_certificates(cfg.replays_path)
        report = build_report(cfg, strict=True)
    finally:
        _remove_directory_link(linked)

    projected_run = next(
        item for item in report["receipts"]["runs"] if item["run_id"] == run["run_id"]
    )
    replay = projected_run["replays"][0]

    assert len(certificates) == 1
    assert report["receipts"]["replay_integrity"] == "error"
    assert any(
        item["code"] == "REPLAY_CERTIFICATE_INVALID"
        and item["path"] == "linked"
        for item in replay_issues
    )
    assert replay["current_derived"]["current"] is False
    assert replay["current_derived"]["byte_repeatable_current"] is False
    assert replay["current_derived"]["review_ready_current"] is False
    assert any(
        item["code"] == "REPLAY_STORE_INTEGRITY"
        for item in replay["current_derived"]["findings"]
    )
    basis = report["claim_basis"]["items"][0]
    assert basis["overall"] != "ready_under_reviewed_provenance"
    assert basis["configured_policy_pass"] is False
    assert report["ok"] is False


def test_unexpected_assessment_store_entry_suppresses_active_relation(tmp_path):
    cfg, _run, _method_review, _semantic_review = _complete_project(tmp_path)
    (cfg.assessments_path / "unexpected").mkdir()

    report = build_report(cfg, strict=True)

    assert report["assessments"]["integrity"] == "error"
    assert report["assessments"]["active_relations"] == []
    assert any(
        item["code"] == "ASSESSMENT_INTEGRITY"
        and "unexpected" in item["detail"]
        for item in report["findings"]
    )
    assessment = next(
        item for item in report["assessments"]["items"] if item["is_current"]
    )
    assert assessment["current_derived"]["active_relation"] is None
    assert report["claim_basis"]["items"] == []
    assert report["ok"] is False


def test_linked_assessment_store_root_suppresses_active_relation(tmp_path):
    cfg, _run, _method_review, _semantic_review = _complete_project(tmp_path)
    original = tmp_path / "assessment-store-original"
    cfg.assessments_path.rename(original)
    _make_directory_link(original, cfg.assessments_path)
    try:
        linked_cfg = Config(cfg.config_path)
        report = build_report(linked_cfg, strict=True)
    finally:
        _remove_directory_link(cfg.assessments_path)
        original.rename(cfg.assessments_path)

    assert report["assessments"]["integrity"] == "error"
    assert report["assessments"]["active_relations"] == []
    assert any(
        item["code"] == "ASSESSMENT_INTEGRITY"
        for item in report["findings"]
    )
    assert report["claim_basis"]["items"] == []
    assert report["ok"] is False


def test_linked_method_assessment_store_root_suppresses_conformance(tmp_path):
    cfg, _run, _method_review, _semantic_review = _complete_project(tmp_path)
    original = tmp_path / "method-assessment-store-original"
    cfg.method_assessments_path.rename(original)
    _make_directory_link(original, cfg.method_assessments_path)
    try:
        linked_cfg = Config(cfg.config_path)
        report = build_report(linked_cfg, strict=True)
    finally:
        _remove_directory_link(cfg.method_assessments_path)
        original.rename(cfg.method_assessments_path)

    assert report["method_assessments"]["integrity"] == "error"
    assert report["method_assessments"]["items"] == []
    basis = report["claim_basis"]["items"][0]
    assert basis["overall"] != "ready_under_reviewed_provenance"
    assert basis["configured_policy_pass"] is False
    assert report["ok"] is False


def test_corrupt_event_store_quarantines_all_otherwise_valid_runs(tmp_path):
    cfg, run, _method_review, _semantic_review = _complete_project(tmp_path)
    (cfg.events_path / "corrupt.json").write_text("{", encoding="utf-8")

    report = build_report(cfg, strict=True)
    projected_run = next(
        item for item in report["receipts"]["runs"] if item["run_id"] == run["run_id"]
    )

    assert report["receipts"]["event_store_integrity"] == "error"
    assert projected_run["event_store_integrity"] == "error"
    assert projected_run["link_integrity"] == "ok"
    assert projected_run["evidence_eligible"] is False
    assert not any(
        item["binding_kind"] in {
            "output_path", "output_validation", "materialized_intermediate_path",
        }
        for item in projected_run["bindings"]
    )
    replay = projected_run["replays"][0]
    assert replay["current_derived"]["review_ready_current"] is False
    assert any(
        item["code"] == "REPLAY_EVENT_STORE_INTEGRITY"
        for item in replay["current_derived"]["findings"]
    )
    basis = report["claim_basis"]["items"][0]
    assert basis["overall"] != "ready_under_reviewed_provenance"
    assert basis["configured_policy_pass"] is False
    assert report["ok"] is False
