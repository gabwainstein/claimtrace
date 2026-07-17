"""Adversarial tests for method-to-code conformance assessments."""
import hashlib
import json

import pytest

from provsleuth.config import Config
from provsleuth.method_assessment import (
    MethodAssessmentError,
    append_method_assessment,
    append_method_review_transition,
    create_method_assessment,
    evaluate_method_assessment,
    load_method_assessments,
    method_assessment_statuses,
    transition_method_review,
    validate_method_assessment_document,
)


FIXED_TIME = "2026-07-15T01:00:00.000Z"


def _project(tmp_path):
    trace = tmp_path / "provsleuth"
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
            {"id": "data:raw", "type": "data", "status": "current",
             "path": "data/raw.csv"},
            {"id": "code:pipeline", "type": "code", "status": "current",
             "path": "analysis/pipeline.py"},
            {
                "id": "method:primary", "type": "method", "status": "current",
                "path": "methods.md",
                "method_spec": {
                    "schema_version": "claimtrace.method-spec/1",
                    "steps": [
                        {"id": "complete", "statement": "Remove incomplete rows.",
                         "required": True},
                        {"id": "fit", "statement": "Fit ordinary least squares.",
                         "required": True},
                    ],
                },
            },
            {"id": "art:fit", "type": "artifact", "status": "current",
             "path": "results/fit.json"},
        ],
        "edges": [],
    }
    graph_path = trace / "graph.json"
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    config_path = tmp_path / "provsleuth.config.json"
    config_path.write_text(json.dumps({
        "root": ".",
        "graph": "provsleuth/graph.json",
        "execution": {"method_assessments": "provsleuth/method-assessments"},
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
    return Config(config_path), graph, graph_path, contract, contract_path


def _agent_input(verdict="implements", alignments=None):
    return {
        "verdict": verdict,
        "step_alignments": alignments or [
            {"method_step_id": "complete", "stage_id": "complete",
             "alignment": "match"},
            {"method_step_id": "fit", "stage_id": "fit", "alignment": "match"},
        ],
        "rationale": (
            "The exact declared code anchors implement the two stated method steps."
        ),
        "limitations": [
            "This judgement does not observe the internal stages at runtime.",
        ],
        "provenance": {"agent": "agent:test", "model": "fixture"},
    }


def _make(cfg, agent_input=None, recorded_at=FIXED_TIME):
    return create_method_assessment(
        cfg,
        "provsleuth/primary.pipeline.json",
        "method:primary",
        agent_input or _agent_input(),
        actor="agent:test",
        declared_inputs=["data/raw.csv", "analysis/pipeline.py"],
        declared_outputs=["results/fit.json"],
        parameters={"model": "ols"},
        seeds={"numpy": "7"},
        recorded_at=recorded_at,
    )


def _accept(cfg, proposal):
    return transition_method_review(
        cfg, proposal, "accepted", actor="scientist:1",
        recorded_at="2026-07-15T01:01:00.000Z",
    )


def test_create_computes_exact_contract_method_code_and_anchor_snapshot(tmp_path):
    cfg, _graph, _graph_path, _contract, _contract_path = _project(tmp_path)
    proposal = _make(cfg)
    validate_method_assessment_document(proposal)

    snapshot = proposal["mechanical_snapshot"]["pipeline_contract"]
    assert proposal["subject"] == {
        "method_id": "method:primary",
        "pipeline_contract_id": snapshot["id"],
    }
    assert snapshot["roles"]["methods"][0]["method_spec"]["steps"][0] == {
        "id": "complete", "statement": "Remove incomplete rows.", "required": True,
    }
    assert snapshot["roles"]["code"][0]["file"]["sha256"] == hashlib.sha256(
        (cfg.root / "analysis" / "pipeline.py").read_bytes()
    ).hexdigest()
    assert all(anchor["valid"] is True for stage in snapshot["stages"]
               for anchor in stage["code_anchors"])
    assert proposal["derived"]["eligible_for_implements"] is True
    assert proposal["derived"]["implementation_current"] is False
    assert proposal["derived"]["stage_execution_observation"] == (
        "declared_only_not_observed"
    )
    assert proposal["derived"]["scientific_validity"] == "not_assessed"


def test_agent_input_is_closed_and_cannot_supply_mechanical_or_derived_fields(tmp_path):
    cfg, *_ = _project(tmp_path)
    agent_input = _agent_input()
    agent_input["mechanical_snapshot"] = {"forged": True}
    with pytest.raises(MethodAssessmentError, match="exactly verdict"):
        _make(cfg, agent_input)


def test_agent_provenance_identity_must_match_proposal_actor(tmp_path):
    cfg, *_ = _project(tmp_path)
    agent_input = _agent_input()
    agent_input["provenance"]["agent"] = "agent:someone-else"
    with pytest.raises(MethodAssessmentError, match="must equal"):
        _make(cfg, agent_input)


def test_every_exact_contract_stage_must_be_assessed_once(tmp_path):
    cfg, *_ = _project(tmp_path)
    agent_input = _agent_input()
    agent_input["step_alignments"] = agent_input["step_alignments"][:1]
    with pytest.raises(MethodAssessmentError, match="every exact contract stage"):
        _make(cfg, agent_input)

    duplicate = _agent_input()
    duplicate["step_alignments"].append(dict(duplicate["step_alignments"][0]))
    with pytest.raises(MethodAssessmentError, match="repeats complete/complete"):
        _make(cfg, duplicate)


def test_step_alignment_order_is_canonicalized_for_one_content_address(tmp_path):
    cfg, *_ = _project(tmp_path)
    first = _make(cfg)
    reversed_input = _agent_input()
    reversed_input["step_alignments"].reverse()
    second = _make(cfg, reversed_input)

    assert second == first
    assert [item["stage_id"] for item in second["agent_input"]["step_alignments"]] == [
        "complete", "fit",
    ]


def test_implements_requires_every_step_alignment_to_match(tmp_path):
    cfg, *_ = _project(tmp_path)
    agent_input = _agent_input()
    agent_input["step_alignments"][1]["alignment"] = "partial"
    proposal = _make(cfg, agent_input)

    assert proposal["derived"]["eligible_for_implements"] is False
    assert "METHOD_IMPLEMENTATION_ALIGNMENT_INCOMPLETE" in {
        item["code"] for item in proposal["derived"]["findings"]
    }
    with pytest.raises(MethodAssessmentError, match="cannot be accepted"):
        _accept(cfg, proposal)


def test_nonimplements_verdict_is_reviewable_but_never_current_implements(tmp_path):
    cfg, *_ = _project(tmp_path)
    agent_input = _agent_input("partially_implements")
    agent_input["step_alignments"][1]["alignment"] = "partial"
    proposal = _make(cfg, agent_input)
    accepted = _accept(cfg, proposal)

    assert accepted["derived"]["active_verdict"] == "partially_implements"
    assert accepted["derived"]["eligible_for_implements"] is False
    assert accepted["derived"]["implementation_current"] is False


def test_first_reviewer_must_be_a_distinct_self_asserted_actor(tmp_path):
    cfg, *_ = _project(tmp_path)
    proposal = _make(cfg)
    with pytest.raises(MethodAssessmentError, match="different self-asserted actor"):
        transition_method_review(cfg, proposal, "accepted", actor="agent:test")


def test_distinct_acceptance_makes_only_method_conformance_current(tmp_path):
    cfg, *_ = _project(tmp_path)
    accepted = _accept(cfg, _make(cfg))
    evaluation = evaluate_method_assessment(cfg, accepted)

    assert evaluation["active_verdict"] == "implements"
    assert evaluation["implementation_current"] is True
    assert evaluation["stage_execution_observation"] == "declared_only_not_observed"
    assert evaluation["hidden_intermediates"] == "not_observed"
    assert evaluation["scientific_validity"] == "not_assessed"
    assert {
        "METHOD_STAGE_EXECUTION_NOT_OBSERVED",
        "METHOD_SCIENTIFIC_VALIDITY_NOT_ASSESSED",
    } <= {item["code"] for item in evaluation["findings"]}


@pytest.mark.parametrize("mutation", ["method_node", "method_file", "code", "contract"])
def test_current_implements_fails_closed_on_every_locked_dependency_drift(
    tmp_path, mutation,
):
    cfg, graph, graph_path, contract, contract_path = _project(tmp_path)
    accepted = _accept(cfg, _make(cfg))

    if mutation == "method_node":
        method = next(item for item in graph["nodes"] if item["id"] == "method:primary")
        method["method_spec"]["steps"][0]["statement"] = "Drop rows with missing values."
        graph_path.write_text(json.dumps(graph), encoding="utf-8")
    elif mutation == "method_file":
        (cfg.root / "methods.md").write_text(
            "# Methods\nA materially changed method file.\n", encoding="utf-8"
        )
    elif mutation == "code":
        code_path = cfg.root / "analysis" / "pipeline.py"
        code_path.write_text(
            code_path.read_text(encoding="utf-8").replace("if row", "if row.strip()"),
            encoding="utf-8",
        )
    else:
        contract["name"] = "changed-primary-fit"
        contract_path.write_text(json.dumps(contract), encoding="utf-8")

    evaluation = evaluate_method_assessment(cfg, accepted)
    assert evaluation["stale"] is True
    assert evaluation["active_verdict"] is None
    assert evaluation["implementation_current"] is False
    assert "METHOD_CONFORMANCE_STALE" in {
        item["code"] for item in evaluation["findings"]
    }


def test_tampering_cannot_upgrade_declared_stage_execution_or_derived_policy(tmp_path):
    cfg, *_ = _project(tmp_path)
    proposal = _make(cfg)
    proposal["derived"]["stage_execution_observation"] = "observed"
    with pytest.raises(MethodAssessmentError, match="derived content"):
        validate_method_assessment_document(proposal)

    proposal = _make(cfg)
    proposal["mechanical_snapshot"]["pipeline_contract"]["stages"][0][
        "execution_observation"
    ] = "observed"
    with pytest.raises(MethodAssessmentError, match="pipeline snapshot"):
        validate_method_assessment_document(proposal)


def test_append_is_idempotent_content_addressed_and_refuses_existing_corruption(tmp_path):
    cfg, *_ = _project(tmp_path)
    proposal = _make(cfg)
    first = append_method_assessment(cfg, proposal)
    second = append_method_assessment(cfg, proposal)
    assert first == second
    assert first.read_text(encoding="utf-8").endswith("\n")

    first.write_text("{}", encoding="utf-8")
    with pytest.raises(MethodAssessmentError, match="existing method assessment"):
        append_method_assessment(cfg, proposal)


def test_strict_json_store_loading_reports_duplicate_keys_and_unexpected_files(tmp_path):
    cfg, *_ = _project(tmp_path)
    root = cfg.method_assessments_path
    root.mkdir(parents=True)
    (root / ("0" * 64 + ".json")).write_text(
        '{"id":"x","id":"y"}', encoding="utf-8"
    )
    (root / "README.txt").write_text("not evidence", encoding="utf-8")

    documents, issues = load_method_assessments(cfg)
    assert documents == []
    assert len(issues) == 2
    assert {item["code"] for item in issues} == {"METHOD_ASSESSMENT_STORE_INTEGRITY"}


def test_atomic_review_append_and_status_list_only_expose_chain_leaf(tmp_path):
    cfg, *_ = _project(tmp_path)
    proposal = _make(cfg)
    proposal_path = append_method_assessment(cfg, proposal)
    assert proposal_path.exists()
    accepted, accepted_path, evaluation = append_method_review_transition(
        cfg, proposal["id"], "accepted", actor="scientist:1",
        recorded_at="2026-07-15T01:01:00.000Z",
    )

    assert accepted_path.exists()
    assert evaluation["implementation_current"] is True
    documents, issues = load_method_assessments(cfg)
    assert issues == []
    assert {item["id"] for item in documents} == {proposal["id"], accepted["id"]}
    statuses, issues = method_assessment_statuses(cfg)
    assert issues == []
    assert [item["id"] for item in statuses] == [accepted["id"]]
    assert statuses[0]["implementation_current"] is True
    assert statuses[0]["stage_execution_observation"] == "declared_only_not_observed"


def test_store_integrity_issue_deactivates_otherwise_current_assessment(tmp_path):
    cfg, *_ = _project(tmp_path)
    proposal = _make(cfg)
    append_method_assessment(cfg, proposal)
    accepted, _path, _evaluation = append_method_review_transition(
        cfg, proposal["id"], "accepted", actor="scientist:1",
        recorded_at="2026-07-15T01:01:00.000Z",
    )
    (cfg.method_assessments_path / "unexpected.tmp").write_text("orphan", encoding="utf-8")

    documents, issues = load_method_assessments(cfg)
    evaluation = evaluate_method_assessment(
        cfg, accepted, assessments=documents, store_issues=issues,
    )
    assert issues
    assert evaluation["implementation_current"] is False
    assert evaluation["active_verdict"] is None
    assert "METHOD_ASSESSMENT_STORE_INTEGRITY" in {
        item["code"] for item in evaluation["findings"]
    }
