"""Tests for strict and machine-readable graph plus receipt reports."""
import json
import sys

from claimtrace.assessment import append_assessment, create_assessment, transition_review
from claimtrace.cli import main
from claimtrace.config import Config
from claimtrace.events import run_command
from claimtrace.report import build_report, dumps_report


def _project(tmp_path, nodes, edges=()):
    trace = tmp_path / "claimtrace"
    trace.mkdir(parents=True)
    config = tmp_path / "claimtrace.config.json"
    config.write_text(json.dumps({
        "root": ".",
        "graph": "claimtrace/graph.json",
        "events": "claimtrace/events",
        "render_types": ["figure"],
        "input_types": ["data", "artifact", "code"],
        "run_output_types": ["artifact"],
    }), encoding="utf-8")
    (trace / "graph.json").write_text(json.dumps({
        "schema_version": "1.0", "concepts": {},
        "nodes": list(nodes), "edges": list(edges),
    }), encoding="utf-8")
    return Config(config)


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


def test_bare_check_keeps_human_output(tmp_path, capsys):
    (tmp_path / "data.txt").write_text("x", encoding="utf-8")
    cfg = _project(tmp_path, [
        {"id": "data:x", "type": "data", "status": "current", "path": "data.txt"},
    ])
    assert main(["--config", str(cfg.config_path), "check"]) == 0
    captured = capsys.readouterr()
    assert "claimtrace check: OK" in captured.out
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
    assert parsed["report_schema_version"] == "1.1"
    assert parsed["fatal"] is None
    assert captured.err == ""


def test_malformed_graph_gets_fatal_json_envelope(tmp_path, capsys):
    cfg = _project(tmp_path, [])
    cfg.graph_path.write_text("{ broken", encoding="utf-8")
    assert main(["--config", str(cfg.config_path), "check", "--strict", "--json"]) == 2
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["fatal"]["code"] == "GRAPH_ERROR"
    assert parsed["receipts"] is None
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
    verifier = tmp_path / "claimtrace" / "verifiers.py"
    verifier.write_text(
        "from pathlib import Path\nPath(r'" + str(sentinel) + "').touch()\n",
        encoding="utf-8",
    )
    cfg.data["verifiers"] = "claimtrace/verifiers.py"
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
