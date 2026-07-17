"""Focused tests for grounded semantic assessment records."""
import concurrent.futures
import hashlib
import json
import multiprocessing
import threading

import pytest

import provsleuth.assessment as assessment_module
from provsleuth.assessment import (
    LEGACY_SCHEMA_VERSION,
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    AssessmentError,
    append_assessment,
    append_review_transition,
    create_assessment,
    detect_conflicts,
    evaluate_assessment,
    load_assessments,
    transition_review,
    validate_assessment_document,
)
from provsleuth.cli import main
from provsleuth.config import Config


FIXED_TIME = "2026-07-13T08:00:00.000Z"


def _process_review(config_path, assessment_id, actor, gate, output):
    cfg = Config(config_path)
    gate.wait()
    try:
        transition, _path, _evaluation = append_review_transition(
            cfg, assessment_id, "accepted", actor=actor,
        )
        output.put(("ok", transition["id"]))
    except AssessmentError as exc:
        output.put(("error", str(exc)))


def _project(tmp_path):
    (tmp_path / "provsleuth").mkdir()
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "fit.json").write_text(
        json.dumps({"slope": 0.41, "r_squared": 0.19}), encoding="utf-8"
    )
    (tmp_path / "provsleuth.config.json").write_text(json.dumps({
        "root": ".",
        "graph": "provsleuth/graph.json",
    }), encoding="utf-8")
    (tmp_path / "provsleuth" / "graph.json").write_text(json.dumps({
        "schema_version": "1.0",
        "nodes": [
            {
                "id": "claim:causal",
                "type": "claim",
                "status": "current",
                "value": "Treatment causally increases memory score.",
            },
            {
                "id": "art:fit",
                "type": "artifact",
                "status": "current",
                "path": "results/fit.json",
            },
        ],
        "edges": [],
        "concepts": {},
    }), encoding="utf-8")
    return Config(tmp_path / "provsleuth.config.json")


def _agent_input(verdict="supports_as_written", expected_slope=0.41):
    alignment = {
        "population": "match",
        "exposure": "match",
        "comparator": "not_applicable",
        "outcome": "match",
        "direction": "match",
        "magnitude": "partial",
        "time_scope": "match",
        "inference_level": "mismatch",
    }
    value = {
        "verdict": verdict,
        "claim_frame": {
            "population": "study participants",
            "exposure": "treatment exposure",
            "comparator": None,
            "outcome": "memory score",
            "direction": "positive",
            "magnitude": "causal effect size",
            "time_scope": "single study",
            "inference_level": "causal",
        },
        "result_frame": {
            "population": "study participants",
            "exposure": "treatment exposure",
            "comparator": None,
            "outcome": "memory score",
            "direction": "positive",
            "magnitude": "observed slope",
            "time_scope": "single study",
            "inference_level": "associational",
        },
        "alignment": alignment,
        "evidence_anchors": [{
            "result_id": "art:fit",
            "kind": "json_pointer",
            "pointer": "/slope",
            "expected_value": expected_slope,
        }],
        "rationale": "The fitted slope is positive, but the design is associational.",
        "limitations": ["The analysis does not identify a causal effect."],
        "provenance": {"agent": "test-reviewer", "model": "fixture"},
    }
    if verdict == "supports_narrower_claim":
        value["recommended_claim"] = (
            "Treatment exposure was positively associated with memory score."
        )
    return value


def _make(cfg, agent_input=None, recorded_at=FIXED_TIME):
    return create_assessment(
        cfg, "claim:causal", ["art:fit"], agent_input or _agent_input(),
        actor="agent:test", recorded_at=recorded_at,
    )


def _readdress(document):
    core = {key: value for key, value in document.items() if key != "id"}
    digest = hashlib.sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()
    document["id"] = f"assessment:sha256:{digest}"
    return document


def _as_legacy_v1(document):
    legacy = json.loads(json.dumps(document))
    legacy["schema_version"] = LEGACY_SCHEMA_VERSION
    legacy["derived"] = assessment_module._derive(
        legacy["mechanical_snapshot"], legacy["agent_input"], legacy["review"],
        schema_version=LEGACY_SCHEMA_VERSION,
    )
    return _readdress(legacy)


def _valid_contradiction_input():
    value = _agent_input("contradicts_as_written")
    value["result_frame"]["inference_level"] = "causal"
    value["alignment"]["inference_level"] = "match"
    value["result_frame"]["direction"] = "negative"
    value["alignment"]["direction"] = "mismatch"
    value["result_frame"]["magnitude"] = value["claim_frame"]["magnitude"]
    value["alignment"]["magnitude"] = "match"
    return value


def _qualitative_direction_input(verdict="supports_as_written"):
    """A qualitative directional claim assessed against a numeric result."""
    value = _agent_input(verdict)
    value["claim_frame"]["inference_level"] = "associational"
    value["result_frame"]["inference_level"] = "associational"
    value["alignment"]["inference_level"] = "match"
    value["claim_frame"]["magnitude"] = None
    value["result_frame"]["magnitude"] = "slope 0.41"
    value["alignment"]["magnitude"] = "not_stated"
    return value


def test_causal_claim_is_not_supported_by_association(tmp_path):
    assessment = _make(_project(tmp_path))
    codes = {item["code"] for item in assessment["derived"]["findings"]}
    assert "CAUSAL_MODALITY_MISMATCH" in codes
    assert assessment["derived"]["proposed_relation"] is None
    assert assessment["derived"]["active_relation"] is None


def test_predictive_claim_is_not_supported_by_descriptive_result(tmp_path):
    cfg = _project(tmp_path)
    agent_input = _agent_input("supports_as_written")
    agent_input["claim_frame"]["inference_level"] = "predictive"
    agent_input["result_frame"]["inference_level"] = "descriptive"
    agent_input["alignment"]["inference_level"] = "match"
    agent_input["result_frame"]["magnitude"] = agent_input["claim_frame"]["magnitude"]
    agent_input["alignment"]["magnitude"] = "match"
    assessment = _make(cfg, agent_input)
    codes = {item["code"] for item in assessment["derived"]["findings"]}
    assert codes >= {"INFERENCE_LEVEL_INADEQUATE", "ALIGNMENT_INCONSISTENT"}
    assert assessment["derived"]["proposed_relation"] is None


def test_narrower_claim_is_related_not_support_for_original(tmp_path):
    cfg = _project(tmp_path)
    assessment = _make(cfg, _agent_input("supports_narrower_claim"))
    assert assessment["derived"]["proposed_relation"] == "related"
    causal = next(
        item for item in assessment["derived"]["findings"]
        if item["code"] == "CAUSAL_MODALITY_MISMATCH"
    )
    assert causal["severity"] == "warning"
    accepted = transition_review(
        cfg, assessment, "accepted", actor="scientist:1",
        recorded_at="2026-07-13T08:01:00.000Z",
    )
    assert accepted["review"]["supersedes_assessment_id"] == assessment["id"]
    assert accepted["derived"]["active_relation"] == "related"
    assert accepted["derived"]["proposed_relation"] != "supports"


def test_support_as_written_requires_complete_alignment(tmp_path):
    cfg = _project(tmp_path)
    agent_input = _agent_input("supports_as_written")
    agent_input["claim_frame"]["inference_level"] = "associational"
    agent_input["result_frame"]["inference_level"] = "associational"
    agent_input["alignment"]["inference_level"] = "match"
    assessment = _make(cfg, agent_input)
    finding = next(
        item for item in assessment["derived"]["findings"]
        if item["code"] == "CLAIM_ALIGNMENT_INCOMPLETE"
    )
    assert finding["severity"] == "error"
    assert assessment["derived"]["proposed_relation"] is None
    with pytest.raises(AssessmentError, match="CLAIM_ALIGNMENT_INCOMPLETE"):
        transition_review(cfg, assessment, "accepted", actor="scientist:1")


def test_numeric_result_can_support_qualitative_directional_claim(tmp_path):
    cfg = _project(tmp_path)
    proposal = _make(cfg, _qualitative_direction_input())

    assert proposal["schema_version"] == SCHEMA_VERSION
    assert proposal["derived"]["proposed_relation"] == "supports"
    assert "CLAIM_ALIGNMENT_INCOMPLETE" not in {
        item["code"] for item in proposal["derived"]["findings"]
    }

    accepted = transition_review(
        cfg, proposal, "accepted", actor="scientist:1",
        recorded_at="2026-07-13T08:01:00.000Z",
    )
    assert accepted["derived"]["active_relation"] == "supports"


def test_qualitative_magnitude_exception_rejects_inapplicable_direction(tmp_path):
    cfg = _project(tmp_path)
    agent_input = _qualitative_direction_input()
    agent_input["claim_frame"]["direction"] = None
    agent_input["result_frame"]["direction"] = None
    agent_input["alignment"]["direction"] = "not_applicable"
    proposal = _make(cfg, agent_input)

    incomplete = next(
        item for item in proposal["derived"]["findings"]
        if item["code"] == "CLAIM_ALIGNMENT_INCOMPLETE"
    )
    assert "magnitude" in incomplete["detail"]
    assert proposal["derived"]["proposed_relation"] is None
    with pytest.raises(AssessmentError, match="CLAIM_ALIGNMENT_INCOMPLETE"):
        transition_review(cfg, proposal, "accepted", actor="scientist:1")


def test_qualitative_magnitude_exception_rejects_unstated_result_direction(tmp_path):
    cfg = _project(tmp_path)
    agent_input = _qualitative_direction_input()
    agent_input["result_frame"]["direction"] = None
    agent_input["alignment"]["direction"] = "not_stated"
    proposal = _make(cfg, agent_input)

    incomplete = next(
        item for item in proposal["derived"]["findings"]
        if item["code"] == "CLAIM_ALIGNMENT_INCOMPLETE"
    )
    assert "direction" in incomplete["detail"]
    assert "magnitude" in incomplete["detail"]
    assert proposal["derived"]["proposed_relation"] is None


def test_v1_qualitative_magnitude_policy_remains_valid_immutable_history(tmp_path):
    cfg = _project(tmp_path)
    proposal = _as_legacy_v1(_make(cfg, _qualitative_direction_input()))

    validate_assessment_document(proposal)
    assert proposal["derived"]["proposed_relation"] is None
    assert "CLAIM_ALIGNMENT_INCOMPLETE" in {
        item["code"] for item in proposal["derived"]["findings"]
    }
    append_assessment(cfg, proposal)
    loaded, issues = load_assessments(cfg)
    assert issues == []
    assert loaded == [proposal]

    evaluation = evaluate_assessment(cfg, proposal, loaded)
    assert evaluation == proposal["derived"]
    assert evaluation["proposed_relation"] is None


def test_v1_review_successor_preserves_schema_and_policy(tmp_path):
    cfg = _project(tmp_path)
    agent_input = _qualitative_direction_input()
    agent_input["claim_frame"]["magnitude"] = "slope 0.41"
    agent_input["alignment"]["magnitude"] = "match"
    proposal = _as_legacy_v1(_make(cfg, agent_input))
    append_assessment(cfg, proposal)

    accepted, _path, evaluation = append_review_transition(
        cfg, proposal["id"], "accepted", actor="scientist:1",
        recorded_at="2026-07-13T08:01:00.000Z",
    )
    assert accepted["schema_version"] == LEGACY_SCHEMA_VERSION
    assert accepted["derived"]["active_relation"] == "supports"
    assert evaluation["active_relation"] == "supports"
    documents, issues = load_assessments(cfg)
    assert issues == []
    assert {item["schema_version"] for item in documents} == {LEGACY_SCHEMA_VERSION}


def test_v1_derived_tampering_is_not_accepted_as_policy_history(tmp_path):
    cfg = _project(tmp_path)
    proposal = _as_legacy_v1(_make(cfg, _qualitative_direction_input()))
    proposal["derived"]["proposed_relation"] = "supports"
    _readdress(proposal)

    with pytest.raises(AssessmentError, match="derived content"):
        validate_assessment_document(proposal)


def test_missing_result_magnitude_cannot_support_stated_claim_magnitude(tmp_path):
    cfg = _project(tmp_path)
    agent_input = _qualitative_direction_input()
    agent_input["claim_frame"]["magnitude"] = "slope 0.41"
    agent_input["result_frame"]["magnitude"] = None
    proposal = _make(cfg, agent_input)

    incomplete = next(
        item for item in proposal["derived"]["findings"]
        if item["code"] == "CLAIM_ALIGNMENT_INCOMPLETE"
    )
    assert "magnitude" in incomplete["detail"]
    assert proposal["derived"]["proposed_relation"] is None
    with pytest.raises(AssessmentError, match="CLAIM_ALIGNMENT_INCOMPLETE"):
        transition_review(cfg, proposal, "accepted", actor="scientist:1")


def test_missing_claim_and_result_magnitudes_remain_blocked(tmp_path):
    cfg = _project(tmp_path)
    agent_input = _qualitative_direction_input()
    agent_input["result_frame"]["magnitude"] = None
    proposal = _make(cfg, agent_input)

    incomplete = next(
        item for item in proposal["derived"]["findings"]
        if item["code"] == "CLAIM_ALIGNMENT_INCOMPLETE"
    )
    assert "magnitude" in incomplete["detail"]
    assert proposal["derived"]["proposed_relation"] is None


def test_partial_stated_magnitudes_remain_blocked(tmp_path):
    cfg = _project(tmp_path)
    agent_input = _qualitative_direction_input()
    agent_input["claim_frame"]["magnitude"] = "positive effect size"
    agent_input["result_frame"]["magnitude"] = "slope 0.41"
    agent_input["alignment"]["magnitude"] = "partial"
    proposal = _make(cfg, agent_input)

    assert "CLAIM_ALIGNMENT_INCOMPLETE" in {
        item["code"] for item in proposal["derived"]["findings"]
    }
    assert proposal["derived"]["proposed_relation"] is None


def test_non_magnitude_not_stated_remains_blocking(tmp_path):
    cfg = _project(tmp_path)
    agent_input = _qualitative_direction_input()
    agent_input["claim_frame"]["time_scope"] = None
    agent_input["alignment"]["time_scope"] = "not_stated"
    proposal = _make(cfg, agent_input)

    incomplete = next(
        item for item in proposal["derived"]["findings"]
        if item["code"] == "CLAIM_ALIGNMENT_INCOMPLETE"
    )
    assert "time_scope" in incomplete["detail"]
    assert proposal["derived"]["proposed_relation"] is None


def test_numeric_result_can_refute_qualitative_directional_claim(tmp_path):
    cfg = _project(tmp_path)
    agent_input = _qualitative_direction_input("contradicts_as_written")
    agent_input["result_frame"]["direction"] = "negative"
    agent_input["alignment"]["direction"] = "mismatch"
    proposal = _make(cfg, agent_input)

    assert proposal["derived"]["proposed_relation"] == "refutes"
    assert "CLAIM_ALIGNMENT_INCOMPLETE" not in {
        item["code"] for item in proposal["derived"]["findings"]
    }
    accepted = transition_review(
        cfg, proposal, "accepted", actor="scientist:1",
        recorded_at="2026-07-13T08:01:00.000Z",
    )
    assert accepted["derived"]["active_relation"] == "refutes"


def test_invalid_anchor_fails_closed_and_cannot_be_accepted(tmp_path):
    cfg = _project(tmp_path)
    assessment = _make(cfg, _agent_input(expected_slope=999))
    codes = {item["code"] for item in assessment["derived"]["findings"]}
    assert "EVIDENCE_ANCHOR_INVALID" in codes
    assert assessment["derived"]["proposed_relation"] is None
    with pytest.raises(AssessmentError, match="EVIDENCE_ANCHOR_INVALID"):
        transition_review(cfg, assessment, "accepted", actor="scientist:1")


def test_claim_or_evidence_drift_stales_assessment(tmp_path):
    cfg = _project(tmp_path)
    assessment = _make(cfg, _agent_input("supports_narrower_claim"))
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    graph["nodes"][0]["value"] = "Treatment may increase memory score."
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    (cfg.root / "results" / "fit.json").write_text(
        json.dumps({"slope": -0.12, "r_squared": 0.01}), encoding="utf-8"
    )
    evaluation = evaluate_assessment(cfg, assessment)
    stale = [item for item in evaluation["findings"] if item["code"] == "ASSESSMENT_STALE"]
    assert evaluation["stale"] is True
    assert "claim node changed" in stale[0]["detail"]
    assert "result artifact changed" in stale[0]["detail"]
    assert evaluation["active_relation"] is None


def test_content_address_is_deterministic_and_derived_is_not_trusted(tmp_path):
    cfg = _project(tmp_path)
    first = _make(cfg)
    second = _make(cfg)
    assert first == second
    assert first["id"].startswith("assessment:sha256:")
    assert first["mechanical_snapshot"]["claim"]["node_version_id"].startswith("node:sha256:")
    assert first["mechanical_snapshot"]["results"][0]["node_version_id"].startswith(
        "node:sha256:"
    )
    first["derived"]["active_relation"] = "supports"
    with pytest.raises(AssessmentError, match="derived content"):
        validate_assessment_document(first)


def test_recorded_at_requires_strict_rfc3339_t_separator(tmp_path):
    cfg = _project(tmp_path)
    with pytest.raises(AssessmentError, match="RFC 3339"):
        _make(cfg, recorded_at="2026-07-13 08:00:00Z")


def test_conflicting_assessments_become_contested(tmp_path):
    cfg = _project(tmp_path)
    supports_narrower = _make(cfg, _agent_input("supports_narrower_claim"))
    accepted_support = transition_review(
        cfg, supports_narrower, "accepted", actor="scientist:1",
        assessments=[supports_narrower], recorded_at="2026-07-13T08:01:00.000Z",
    )
    contradicts = _make(
        cfg, _valid_contradiction_input(),
        recorded_at="2026-07-13T08:02:00.000Z",
    )
    proposals_and_acceptance = [supports_narrower, accepted_support, contradicts]
    assert detect_conflicts(proposals_and_acceptance) == {}
    assert evaluate_assessment(
        cfg, accepted_support, proposals_and_acceptance,
    )["active_relation"] == "related"
    with pytest.raises(AssessmentError, match="ASSESSMENT_CONTESTED"):
        transition_review(
            cfg, contradicts, "accepted", actor="scientist:2",
            assessments=proposals_and_acceptance,
        )

    # A manually assembled store with two accepted leaves fails closed.
    accepted_contradiction = transition_review(
        cfg, contradicts, "accepted", actor="scientist:2",
        assessments=[contradicts], recorded_at="2026-07-13T08:03:00.000Z",
    )
    documents = [
        supports_narrower, accepted_support, contradicts, accepted_contradiction,
    ]
    conflicts = detect_conflicts(documents)
    assert conflicts[accepted_support["id"]] == [accepted_contradiction["id"]]
    evaluation = evaluate_assessment(cfg, accepted_support, documents)
    assert evaluation["effective_review_state"] == "contested"
    assert "ASSESSMENT_CONTESTED" in {item["code"] for item in evaluation["findings"]}
    assert evaluation["active_relation"] is None


def test_rejected_assessment_is_not_an_active_conflict(tmp_path):
    cfg = _project(tmp_path)
    supports_narrower = _make(cfg, _agent_input("supports_narrower_claim"))
    contradicts = _make(
        cfg, _agent_input("contradicts_as_written"),
        recorded_at="2026-07-13T08:02:00.000Z",
    )
    rejected = transition_review(
        cfg, contradicts, "rejected", actor="scientist:1",
        recorded_at="2026-07-13T08:03:00.000Z",
    )
    assert detect_conflicts([supports_narrower, contradicts, rejected]) == {}


@pytest.mark.parametrize("verdict", ["insufficient", "ambiguous", "unrelated"])
def test_negative_assessment_can_be_accepted_without_activating_an_edge(tmp_path, verdict):
    cfg = _project(tmp_path)
    agent_input = _agent_input(verdict)
    assessment = _make(cfg, agent_input)
    accepted = transition_review(
        cfg, assessment, "accepted", actor="scientist:1",
        recorded_at="2026-07-13T08:01:00.000Z",
    )
    validate_assessment_document(accepted)
    assert accepted["derived"]["active_relation"] is None


def test_contradiction_requires_directional_and_modality_alignment(tmp_path):
    cfg = _project(tmp_path)
    all_match = _agent_input("contradicts_as_written")
    all_match["claim_frame"]["inference_level"] = "associational"
    all_match["result_frame"]["inference_level"] = "associational"
    all_match["alignment"]["inference_level"] = "match"
    all_match["result_frame"]["magnitude"] = all_match["claim_frame"]["magnitude"]
    all_match["alignment"]["magnitude"] = "match"
    assessment = _make(cfg, all_match)
    assert "CONTRADICTION_DIRECTION_NOT_ESTABLISHED" in {
        item["code"] for item in assessment["derived"]["findings"]
    }
    assert assessment["derived"]["proposed_relation"] is None

    valid = _make(cfg, _valid_contradiction_input())
    assert valid["derived"]["proposed_relation"] == "refutes"

    weak = _valid_contradiction_input()
    weak["result_frame"]["inference_level"] = "associational"
    weak["alignment"]["inference_level"] = "mismatch"
    weak_assessment = _make(cfg, weak)
    assert weak_assessment["derived"]["proposed_relation"] is None
    assert "CAUSAL_MODALITY_MISMATCH" in {
        item["code"] for item in weak_assessment["derived"]["findings"]
    }


def test_frames_are_complete_and_assessment_v1_is_single_result(tmp_path):
    cfg = _project(tmp_path)
    incomplete = _agent_input("supports_narrower_claim")
    del incomplete["claim_frame"]["population"]
    with pytest.raises(AssessmentError, match="every fixed frame field"):
        _make(cfg, incomplete)
    with pytest.raises(AssessmentError, match="exactly one result_id"):
        create_assessment(
            cfg, "claim:causal", ["art:fit", "art:fit"], _agent_input(),
            actor="agent:test", recorded_at=FIXED_TIME,
        )


def test_first_review_cannot_reuse_the_proposer_actor(tmp_path):
    cfg = _project(tmp_path)
    proposal = _make(cfg, _agent_input("supports_narrower_claim"))
    with pytest.raises(AssessmentError, match="different actor identities"):
        transition_review(cfg, proposal, "accepted", actor="agent:test")


def test_loaded_documents_enforce_single_result_and_independent_first_reviewer(tmp_path):
    cfg = _project(tmp_path)
    proposal = _make(cfg, _agent_input("supports_narrower_claim"))

    forged_multi_result = json.loads(json.dumps(proposal))
    forged_multi_result["subject"]["result_ids"] = ["art:fit", "art:other"]
    core = {key: value for key, value in forged_multi_result.items() if key != "id"}
    digest = hashlib.sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()
    forged_multi_result["id"] = f"assessment:sha256:{digest}"
    with pytest.raises(AssessmentError, match="exactly one result_id"):
        validate_assessment_document(forged_multi_result)

    accepted = transition_review(
        cfg, proposal, "accepted", actor="scientist:1",
        recorded_at="2026-07-13T08:01:00.000Z",
    )
    accepted["review"]["actor"] = proposal["review"]["actor"]
    core = {key: value for key, value in accepted.items() if key != "id"}
    digest = hashlib.sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()
    accepted["id"] = f"assessment:sha256:{digest}"
    validate_assessment_document(accepted)
    append_assessment(cfg, proposal)
    append_assessment(cfg, accepted)
    _documents, issues = load_assessments(cfg)
    assert any(
        item["code"] == "ASSESSMENT_REVIEW_CHAIN"
        and "same self-asserted actor identity" in item["detail"]
        for item in issues
    )


def test_text_line_anchor_and_append_only_store(tmp_path):
    cfg = _project(tmp_path)
    text = b"header\nobserved slope = 0.41\nfooter\n"
    (cfg.root / "results" / "fit.txt").write_bytes(text)
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    graph["nodes"][1]["path"] = "results/fit.txt"
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    agent_input = _agent_input("supports_narrower_claim")
    agent_input["evidence_anchors"] = [{
        "result_id": "art:fit",
        "kind": "text_lines",
        "start_line": 2,
        "end_line": 2,
        "text_sha256": hashlib.sha256(b"observed slope = 0.41\n").hexdigest(),
    }]
    assessment = _make(cfg, agent_input)
    assert "EVIDENCE_ANCHOR_INVALID" not in {
        item["code"] for item in assessment["derived"]["findings"]
    }
    path = append_assessment(cfg, assessment)
    assert append_assessment(cfg, assessment) == path
    loaded, issues = load_assessments(cfg)
    assert issues == []
    assert loaded == [assessment]


def test_review_store_detects_a_missing_predecessor(tmp_path):
    cfg = _project(tmp_path)
    proposal = _make(cfg, _agent_input("supports_narrower_claim"))
    accepted = transition_review(
        cfg, proposal, "accepted", actor="scientist:1",
        recorded_at="2026-07-13T08:01:00.000Z",
    )
    append_assessment(cfg, accepted)
    loaded, issues = load_assessments(cfg)
    assert loaded == [accepted]
    assert [item["code"] for item in issues] == ["ASSESSMENT_REVIEW_CHAIN"]
    assert proposal["id"] in issues[0]["detail"]


def test_review_store_rejects_schema_switch_within_chain(tmp_path):
    cfg = _project(tmp_path)
    agent_input = _qualitative_direction_input()
    agent_input["claim_frame"]["magnitude"] = "slope 0.41"
    agent_input["alignment"]["magnitude"] = "match"
    proposal = _as_legacy_v1(_make(cfg, agent_input))
    successor = transition_review(
        cfg, proposal, "accepted", actor="scientist:1", assessments=[proposal],
        recorded_at="2026-07-13T08:01:00.000Z",
    )
    successor["schema_version"] = SCHEMA_VERSION
    successor["derived"] = assessment_module._derive(
        successor["mechanical_snapshot"], successor["agent_input"], successor["review"],
        schema_version=SCHEMA_VERSION,
    )
    _readdress(successor)
    validate_assessment_document(successor)

    append_assessment(cfg, proposal)
    append_assessment(cfg, successor)
    _documents, issues = load_assessments(cfg)
    schema_issue = next(
        item for item in issues
        if item["code"] == "ASSESSMENT_REVIEW_CHAIN"
        and "changed assessment schema" in item["detail"]
    )
    assert f"{LEGACY_SCHEMA_VERSION} -> {SCHEMA_VERSION}" in schema_issue["detail"]


def test_review_store_detects_branching_successors(tmp_path):
    cfg = _project(tmp_path)
    proposal = _make(cfg, _agent_input("supports_narrower_claim"))
    first = transition_review(
        cfg, proposal, "accepted", actor="scientist:1",
        assessments=[proposal], recorded_at="2026-07-13T08:01:00.000Z",
    )
    second = transition_review(
        cfg, proposal, "accepted", actor="scientist:2",
        assessments=[proposal], recorded_at="2026-07-13T08:02:00.000Z",
    )
    for document in (proposal, first, second):
        append_assessment(cfg, document)
    _loaded, issues = load_assessments(cfg)
    branch_issues = [
        item for item in issues if "review chain branches" in item["detail"]
    ]
    assert len(branch_issues) == 2


def test_atomic_review_append_prevents_threaded_branch(tmp_path):
    cfg = _project(tmp_path)
    proposal = _make(cfg, _agent_input("supports_narrower_claim"))
    append_assessment(cfg, proposal)
    barrier = threading.Barrier(2)

    def review(actor):
        barrier.wait()
        try:
            transition, _path, _evaluation = append_review_transition(
                cfg, proposal["id"], "accepted", actor=actor,
            )
            return "ok", transition["id"]
        except AssessmentError as exc:
            return "error", str(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(review, ("scientist:1", "scientist:2")))
    assert sorted(item[0] for item in results) == ["error", "ok"]
    assert "not a current review-chain leaf" in next(
        item[1] for item in results if item[0] == "error"
    )
    documents, issues = load_assessments(cfg)
    assert issues == []
    assert len(documents) == 2


def test_atomic_review_append_prevents_cross_process_branch(tmp_path):
    cfg = _project(tmp_path)
    proposal = _make(cfg, _agent_input("supports_narrower_claim"))
    append_assessment(cfg, proposal)
    context = multiprocessing.get_context("spawn")
    gate = context.Event()
    output = context.Queue()
    processes = [
        context.Process(
            target=_process_review,
            args=(str(cfg.config_path), proposal["id"], actor, gate, output),
        )
        for actor in ("scientist:1", "scientist:2")
    ]
    for process in processes:
        process.start()
    gate.set()
    results = [output.get(timeout=20) for _process in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert sorted(item[0] for item in results) == ["error", "ok"]
    assert "not a current review-chain leaf" in next(
        item[1] for item in results if item[0] == "error"
    )
    documents, issues = load_assessments(cfg)
    assert issues == []
    assert len(documents) == 2


def test_cli_assess_list_and_review_round_trip(tmp_path, capsys):
    cfg = _project(tmp_path)
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(json.dumps({
        "claim_id": "claim:causal",
        "result_ids": ["art:fit"],
        "agent_input": _agent_input("supports_narrower_claim"),
    }), encoding="utf-8")

    assert main([
        "--config", str(cfg.config_path), "assess", str(proposal_path),
        "--actor", "agent:test", "--json",
    ]) == 0
    proposed = json.loads(capsys.readouterr().out)
    assert proposed["schema_version"] == SCHEMA_VERSION
    assert proposed["review"]["state"] == "proposed"
    assert proposed["current_derived"]["proposed_relation"] == "related"

    assert main([
        "--config", str(cfg.config_path), "review", proposed["id"],
        "--state", "accepted", "--actor", "scientist:1", "--json",
    ]) == 0
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["schema_version"] == SCHEMA_VERSION
    assert accepted["review"]["state"] == "accepted"
    assert accepted["current_derived"]["active_relation"] == "related"

    assert main([
        "--config", str(cfg.config_path), "review", proposed["id"],
        "--state", "accepted", "--actor", "scientist:2",
    ]) == 2
    assert "not a current review-chain leaf" in capsys.readouterr().err

    assert main([
        "--config", str(cfg.config_path), "assessments", "--json",
    ]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["integrity"] == "ok"
    assert listing["current_assessment_schema_version"] == SCHEMA_VERSION
    assert listing["supported_assessment_schema_versions"] == sorted(
        SUPPORTED_SCHEMA_VERSIONS
    )
    assert [item["id"] for item in listing["items"]] == [accepted["id"]]
    assert listing["items"][0]["schema_version"] == SCHEMA_VERSION
    assert listing["items"][0]["is_current"] is True


def test_cli_writes_v2_and_lists_mixed_schema_store_deterministically(tmp_path, capsys):
    cfg = _project(tmp_path)
    legacy = _as_legacy_v1(
        _make(cfg, _agent_input("supports_narrower_claim"), recorded_at=FIXED_TIME)
    )
    append_assessment(cfg, legacy)
    proposal_path = tmp_path / "qualitative-proposal.json"
    proposal_path.write_text(json.dumps({
        "claim_id": "claim:causal",
        "result_ids": ["art:fit"],
        "agent_input": _qualitative_direction_input(),
    }), encoding="utf-8")

    assert main([
        "--config", str(cfg.config_path), "assess", str(proposal_path),
        "--actor", "agent:v2", "--json",
    ]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["schema_version"] == SCHEMA_VERSION
    assert created["current_derived"]["proposed_relation"] == "supports"

    assert main([
        "--config", str(cfg.config_path), "review", created["id"],
        "--state", "accepted", "--actor", "scientist:1", "--json",
    ]) == 0
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["schema_version"] == SCHEMA_VERSION
    assert accepted["current_derived"]["active_relation"] == "supports"

    command = [
        "--config", str(cfg.config_path), "assessments", "--all", "--json",
    ]
    assert main(command) == 0
    first_output = capsys.readouterr().out
    assert main(command) == 0
    second_output = capsys.readouterr().out
    assert second_output == first_output
    listing = json.loads(first_output)
    assert listing["integrity"] == "ok"
    assert listing["current_assessment_schema_version"] == SCHEMA_VERSION
    assert listing["supported_assessment_schema_versions"] == sorted(
        SUPPORTED_SCHEMA_VERSIONS
    )
    assert {item["schema_version"] for item in listing["items"]} == {
        LEGACY_SCHEMA_VERSION, SCHEMA_VERSION,
    }


def test_cli_listing_suppresses_current_relation_when_store_integrity_fails(
        tmp_path, capsys):
    cfg = _project(tmp_path)
    proposal = _make(cfg, _agent_input("supports_narrower_claim"))
    accepted = transition_review(
        cfg, proposal, "accepted", actor="scientist:1", assessments=[proposal],
        recorded_at="2026-07-13T08:01:00.000Z",
    )
    append_assessment(cfg, accepted)  # deliberately omit the predecessor

    assert main([
        "--config", str(cfg.config_path), "assessments", "--json",
    ]) == 2
    listing = json.loads(capsys.readouterr().out)
    assert listing["integrity"] == "error"
    assert {item["code"] for item in listing["issues"]} == {
        "ASSESSMENT_REVIEW_CHAIN",
    }
    assert len(listing["items"]) == 1
    item = listing["items"][0]
    assert item["id"] == accepted["id"]
    assert item["review"]["state"] == "accepted"
    assert item["current_derived"]["effective_review_state"] == "accepted"
    assert item["current_derived"]["active_relation"] is None


def test_cli_rejects_agent_supplied_mechanical_or_derived_sections(tmp_path, capsys):
    cfg = _project(tmp_path)
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(json.dumps({
        "claim_id": "claim:causal",
        "result_ids": ["art:fit"],
        "agent_input": _agent_input("supports_narrower_claim"),
        "derived": {"active_relation": "supports"},
    }), encoding="utf-8")
    assert main([
        "--config", str(cfg.config_path), "assess", str(proposal_path),
        "--actor", "agent:test",
    ]) == 2
    captured = capsys.readouterr()
    assert "mechanical_snapshot and derived are computed by provsleuth" in captured.err
    assert not cfg.assessments_path.exists()
