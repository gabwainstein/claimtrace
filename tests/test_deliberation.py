"""Adversarial tests for the provider-neutral deliberation ledger."""
import copy
import hashlib
import json
import threading
from contextlib import contextmanager

import pytest

from provsleuth.config import Config
from provsleuth.cli import main
from provsleuth.deliberation import (
    BALLOT_REQUEST_SCHEMA,
    CANDIDATE_SET_REQUEST_SCHEMA,
    PHASE_DECISION_REQUEST_SCHEMA,
    PROPOSAL_REQUEST_SCHEMA,
    DeliberationError,
    append_record,
    create_ballot,
    create_phase_decision,
    create_proposal,
    deliberation_status,
    evaluate_candidate_set,
    freeze_candidate_set,
    load_records,
)


def _project(tmp_path, *, with_logic=False):
    source = tmp_path / "paper.txt"
    source.write_text(
        "The treatment increased score in this sample.\n"
        "The authors report no population-level causal estimate.\n",
        encoding="utf-8",
    )
    graph = {
        "schema_version": "1.0", "concepts": {},
        "nodes": [{
            "id": "doc:paper-text", "type": "artifact", "status": "current",
            "path": "paper.txt",
        }],
        "edges": [],
    }
    (tmp_path / "provsleuth").mkdir()
    (tmp_path / "provsleuth" / "graph.json").write_text(
        json.dumps(graph), encoding="utf-8",
    )
    logic_config = {
        "derivations": "provsleuth/derivations", "vocabularies": [],
        "rule_packs": [], "allow_external_packs": False,
        "require_derivations": False,
    }
    if with_logic:
        logic_dir = tmp_path / "provsleuth" / "logic"
        logic_dir.mkdir()
        vocabulary = _vocabulary()
        (logic_dir / "vocabulary.json").write_text(
            json.dumps(vocabulary), encoding="utf-8",
        )
        logic_config["vocabularies"] = ["provsleuth/logic/vocabulary.json"]
    config = {
        "root": ".", "graph": "provsleuth/graph.json",
        "events": "provsleuth/events", "assessments": "provsleuth/assessments",
        "deliberation": {"records": "provsleuth/deliberations"},
        "execution": {
            "replays": "provsleuth/replays",
            "method_assessments": "provsleuth/method-assessments",
        },
        "logic": logic_config,
        "semantics": {
            "mappings": "provsleuth/semantics/mappings",
            "policies": "provsleuth/semantics/policies",
        },
    }
    config_path = tmp_path / "provsleuth.config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return Config(config_path)


def _anchor(cfg, text=None):
    data = cfg.resolve("paper.txt").read_bytes()
    if text is None:
        start, end = 0, len(data)
    else:
        needle = text.encode("utf-8")
        start = data.index(needle)
        end = start + len(needle)
    return {
        "node_id": "doc:paper-text", "start_byte": start, "end_byte": end,
        "span_sha256": hashlib.sha256(data[start:end]).hexdigest(),
    }


def _extraction_entry(cfg, *, claim=None, subject="claim:treatment"):
    claim = claim or "The treatment increased score in this sample."
    return {
        "schema_version": PROPOSAL_REQUEST_SCHEMA,
        "round_id": "round:1", "phase": "claim_extraction",
        "subject_key": subject, "source_anchor": _anchor(cfg),
        "payload": {
            "claim_text": claim, "claim_kind": "descriptive",
            "speech_act": "assertion", "polarity": "positive",
            "qualifiers": ["in this sample"],
        },
        "rationale": "Exact sentence selected for atomic review.",
        "provenance": {
            "model": "test-model", "prompt_sha256": "1" * 64,
        },
    }


def _append_proposal(cfg, entry, actor, group, second=0):
    proposal = create_proposal(
        cfg, entry, actor=actor, independence_group=group,
        recorded_at=f"2026-07-18T00:00:{second:02d}Z",
    )
    append_record(cfg, proposal)
    return proposal


def _freeze(cfg, *, actor="human:owner", second=20):
    document = freeze_candidate_set(cfg, {
        "schema_version": CANDIDATE_SET_REQUEST_SCHEMA,
        "round_id": "round:1", "phase": "claim_extraction",
        "subject_key": "claim:treatment",
    }, actor=actor, recorded_at=f"2026-07-18T00:00:{second:02d}Z")
    append_record(cfg, document)
    return document


def _ballot(cfg, candidate_set, candidate_ids, role, actor, group, *,
            decision="endorse", blocking=False, second=30):
    codes = [] if decision == "endorse" else [
        "MATERIAL_DISSENT" if decision == "reject" else "CRITICAL_ABSTENTION"
    ]
    entry = {
        "schema_version": BALLOT_REQUEST_SCHEMA,
        "candidate_set_id": candidate_set["candidate_set_id"], "role": role,
        "evaluations": [{
            "candidate_id": candidate_id, "decision": decision,
            "reason_codes": codes, "blocking": blocking,
            "rationale": "Independent role-bound assessment.",
        } for candidate_id in candidate_ids],
    }
    document = create_ballot(
        cfg, entry, actor=actor, independence_group=group,
        recorded_at=f"2026-07-18T00:00:{second:02d}Z",
    )
    append_record(cfg, document)
    return document


def _split_ballot(cfg, candidate_set, role, actor, group, decisions, *, second):
    evaluations = []
    for candidate_id in candidate_set["candidate_ids"]:
        decision = decisions.get(candidate_id, "endorse")
        if decision == "reject":
            reason_codes = ["MATERIAL_DISSENT"]
            blocking = True
        elif decision == "abstain":
            reason_codes = ["REQUIRED_ROLE_MISSING"]
            blocking = False
        else:
            reason_codes = []
            blocking = False
        evaluations.append({
            "candidate_id": candidate_id, "decision": decision,
            "reason_codes": reason_codes, "blocking": blocking,
            "rationale": "Candidate-specific independent assessment.",
        })
    document = create_ballot(cfg, {
        "schema_version": BALLOT_REQUEST_SCHEMA,
        "candidate_set_id": candidate_set["candidate_set_id"],
        "role": role, "evaluations": evaluations,
    }, actor=actor, independence_group=group,
       recorded_at=f"2026-07-18T00:00:{second:02d}Z")
    append_record(cfg, document)
    return document


def _review_ready_panel(cfg):
    proposal = _append_proposal(
        cfg, _extraction_entry(cfg), "agent:one", "group:one",
    )
    candidate_set = _freeze(cfg)
    for index, role in enumerate((
        "source_verifier", "coverage_reviewer", "adversarial_falsifier",
    )):
        _ballot(
            cfg, candidate_set, [proposal["candidate_id"]], role,
            f"reviewer:{index}", f"review-group:{index}", second=30 + index,
        )
    assert evaluate_candidate_set(
        cfg, candidate_set["candidate_set_id"],
    )["status"] == "recommended_for_human_review"
    return proposal, candidate_set


def _phase_decision(cfg, proposal, candidate_set, decision="approved", *,
                    actor="human:decider", second=40):
    return create_phase_decision(cfg, {
        "schema_version": PHASE_DECISION_REQUEST_SCHEMA,
        "candidate_set_id": candidate_set["candidate_set_id"],
        "candidate_id": proposal["candidate_id"],
        "decision": decision,
        "rationale": "Reviewed the complete frozen panel and recorded dissent.",
    }, actor=actor, recorded_at=f"2026-07-18T00:00:{second:02d}Z")


def _semantic_entry(cfg, extraction_candidate_id, *, round_id="round:1",
                    subject="claim:treatment"):
    return {
        "schema_version": PROPOSAL_REQUEST_SCHEMA,
        "round_id": round_id, "phase": "semantic_interpretation",
        "subject_key": subject, "source_anchor": _anchor(cfg),
        "payload": {
            "extraction_candidate_id": extraction_candidate_id,
            "normalized_claim": "Treatment increased score in this sample.",
            "frame": {
                "population": "this sample", "exposure": "treatment",
                "comparator": None, "outcome": "score", "direction": "increased",
                "magnitude": None, "time_scope": "reported study",
                "inference_level": "descriptive",
            },
            "modality": "observed",
            "quantifier": {"kind": "some", "range": None},
            "negation_scope": {"polarity": "positive", "scope": None},
            "conditions": ["in this sample"], "ambiguities": [],
            "non_equivalences": ["A sample description is not a population effect."],
        },
        "rationale": "Preserve the source scope before formalization.",
    }


def test_actor_independent_candidates_coalesce_without_multiplying_records(tmp_path):
    cfg = _project(tmp_path)
    first = _append_proposal(cfg, _extraction_entry(cfg), "agent:one", "group:one")
    second = _append_proposal(
        cfg, _extraction_entry(cfg), "agent:two", "group:two", second=1,
    )

    assert first["candidate_id"] == second["candidate_id"]
    assert first["proposal_id"] != second["proposal_id"]
    records, issues = load_records(cfg)
    assert issues == []
    assert len(records) == 2


def test_proposal_requires_exact_utf8_anchor_and_exact_source_text(tmp_path):
    cfg = _project(tmp_path)
    bad_hash = _extraction_entry(cfg)
    bad_hash["source_anchor"]["span_sha256"] = "0" * 64
    with pytest.raises(DeliberationError, match="span_sha256"):
        create_proposal(
            cfg, bad_hash, actor="agent:one", independence_group="group:one",
        )

    paraphrase = _extraction_entry(cfg)
    paraphrase["payload"]["claim_text"] = "Treatment improved the score."
    with pytest.raises(DeliberationError, match="exact substring"):
        create_proposal(
            cfg, paraphrase, actor="agent:one", independence_group="group:one",
        )


def test_freeze_captures_complete_union_and_closes_the_round(tmp_path):
    cfg = _project(tmp_path)
    first = _append_proposal(cfg, _extraction_entry(cfg), "agent:one", "group:one")
    alternative_entry = _extraction_entry(
        cfg, claim="The authors report no population-level causal estimate.",
    )
    alternative_entry["payload"].update({
        "claim_kind": "reporting", "polarity": "negative",
        "qualifiers": ["population-level"],
    })
    second = _append_proposal(
        cfg, alternative_entry, "agent:two", "group:two", second=1,
    )
    candidate_set = _freeze(cfg)
    assert candidate_set["candidate_ids"] == sorted({
        first["candidate_id"], second["candidate_id"],
    })

    late = create_proposal(
        cfg, _extraction_entry(cfg), actor="agent:late",
        independence_group="group:late", recorded_at="2026-07-18T00:00:21Z",
    )
    with pytest.raises(DeliberationError, match="ledger integrity"):
        append_record(cfg, late)


def test_three_required_external_roles_make_one_candidate_review_ready(tmp_path):
    cfg = _project(tmp_path)
    proposal = _append_proposal(cfg, _extraction_entry(cfg), "agent:one", "group:one")
    candidate_set = _freeze(cfg)
    for index, role in enumerate((
        "source_verifier", "coverage_reviewer", "adversarial_falsifier",
    )):
        _ballot(
            cfg, candidate_set, [proposal["candidate_id"]], role,
            f"reviewer:{index}", f"review-group:{index}", second=30 + index,
        )

    result = evaluate_candidate_set(cfg, candidate_set["candidate_set_id"])
    assert result["status"] == "recommended_for_human_review"
    assert result["recommended_candidate_id"] == proposal["candidate_id"]
    assert result["human_activation_required"] is True
    assert result["automatic_activation"] is False
    assert result["procedural_independence_only"] is True


def test_evaluation_holds_one_ledger_snapshot_while_a_ballot_waits_to_append(
        tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    proposal, candidate_set = _review_ready_panel(cfg)
    late_ballot = create_ballot(cfg, {
        "schema_version": BALLOT_REQUEST_SCHEMA,
        "candidate_set_id": candidate_set["candidate_set_id"],
        "role": "source_verifier",
        "evaluations": [{
            "candidate_id": proposal["candidate_id"], "decision": "endorse",
            "reason_codes": [], "blocking": False,
            "rationale": "A fourth attributed review arriving concurrently.",
        }],
    }, actor="reviewer:late", independence_group="review-group:late",
       recorded_at="2026-07-18T00:00:33Z")

    from provsleuth import deliberation as module
    evaluation_entered = threading.Event()
    release_evaluation = threading.Event()
    writer_attempted_lock = threading.Event()
    writer_finished = threading.Event()
    results = {}
    errors = []
    original_lock = module.events._event_lock
    original_findings = module._proposal_live_findings

    @contextmanager
    def observed_lock(path, timeout=30.0):
        if threading.current_thread().name == "deliberation-writer":
            writer_attempted_lock.set()
        with original_lock(path, timeout=timeout):
            yield

    def paused_findings(*args, **kwargs):
        evaluation_entered.set()
        if not release_evaluation.wait(5):
            raise AssertionError("test did not release the evaluation barrier")
        return original_findings(*args, **kwargs)

    def evaluate_reader():
        try:
            results["panel"] = evaluate_candidate_set(
                cfg, candidate_set["candidate_set_id"],
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def append_writer():
        try:
            append_record(cfg, late_ballot)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            writer_finished.set()

    monkeypatch.setattr(module.events, "_event_lock", observed_lock)
    monkeypatch.setattr(module, "_proposal_live_findings", paused_findings)
    reader = threading.Thread(target=evaluate_reader, name="deliberation-reader")
    writer = threading.Thread(target=append_writer, name="deliberation-writer")
    reader.start()
    assert evaluation_entered.wait(5)
    writer.start()
    assert writer_attempted_lock.wait(5)
    assert not writer_finished.is_set()
    release_evaluation.set()
    reader.join(5)
    writer.join(5)
    assert not reader.is_alive() and not writer.is_alive()
    assert errors == []
    assert late_ballot["ballot_id"] not in results["panel"]["ballot_ids"]
    records, issues = load_records(cfg)
    assert issues == []
    assert late_ballot["ballot_id"] in {
        item.get("ballot_id") for item in records
    }


def test_blocking_dissent_is_preserved_and_prevents_recommendation(tmp_path):
    cfg = _project(tmp_path)
    proposal = _append_proposal(cfg, _extraction_entry(cfg), "agent:one", "group:one")
    candidate_set = _freeze(cfg)
    _ballot(
        cfg, candidate_set, [proposal["candidate_id"]], "source_verifier",
        "reviewer:source", "group:source", second=30,
    )
    _ballot(
        cfg, candidate_set, [proposal["candidate_id"]], "coverage_reviewer",
        "reviewer:coverage", "group:coverage", second=31,
    )
    rejection = _ballot(
        cfg, candidate_set, [proposal["candidate_id"]], "adversarial_falsifier",
        "reviewer:falsifier", "group:falsifier", decision="reject",
        blocking=True, second=32,
    )

    result = evaluate_candidate_set(cfg, candidate_set["candidate_set_id"])
    assert result["status"] == "contested"
    assert result["recommended_candidate_id"] is None
    candidate = result["candidates"][0]
    assert candidate["blocking_evaluations"] == [rejection["ballot_id"]]
    recorded_rejection = next(
        item for item in candidate["evaluations"]
        if item["ballot_id"] == rejection["ballot_id"]
    )
    assert recorded_rejection["reason_codes"] == ["MATERIAL_DISSENT"]


def test_correlated_actor_labels_count_as_one_independence_group(tmp_path):
    cfg = _project(tmp_path)
    proposal = _append_proposal(cfg, _extraction_entry(cfg), "agent:one", "group:one")
    _append_proposal(
        cfg, _extraction_entry(cfg), "agent:two", "group:two", second=1,
    )
    candidate_set = _freeze(cfg)
    for index, role in enumerate((
        "source_verifier", "coverage_reviewer", "adversarial_falsifier",
    )):
        _ballot(
            cfg, candidate_set, [proposal["candidate_id"]], role,
            f"reviewer:{index}", "same-model-context", second=30 + index,
        )
    result = evaluate_candidate_set(cfg, candidate_set["candidate_set_id"])
    assert result["status"] == "insufficient_review"
    assert result["candidates"][0]["procedural_independence_gate_passed"] is True
    assert result["candidates"][0]["distinct_role_independence_gate_passed"] is False


def test_proposer_group_cannot_reappear_under_new_actor_as_external_review(tmp_path):
    cfg = _project(tmp_path)
    proposal = _append_proposal(cfg, _extraction_entry(cfg), "agent:one", "group:one")
    candidate_set = _freeze(cfg)
    reviews = (
        ("source_verifier", "reviewer:renamed", "group:one"),
        ("coverage_reviewer", "reviewer:coverage", "group:coverage"),
        ("adversarial_falsifier", "reviewer:falsifier", "group:falsifier"),
    )
    for index, (role, actor, group) in enumerate(reviews):
        _ballot(
            cfg, candidate_set, [proposal["candidate_id"]], role, actor, group,
            second=30 + index,
        )

    result = evaluate_candidate_set(cfg, candidate_set["candidate_set_id"])
    candidate = result["candidates"][0]
    assert result["status"] == "insufficient_review"
    assert candidate["missing_roles"] == ["source_verifier"]
    assert candidate["external_endorsement_independence_groups"] == [
        "group:coverage", "group:falsifier",
    ]


def test_critical_abstention_and_material_dissent_must_be_blocking(tmp_path):
    cfg = _project(tmp_path)
    proposal = _append_proposal(cfg, _extraction_entry(cfg), "agent:one", "group:one")
    candidate_set = _freeze(cfg)
    base = {
        "schema_version": BALLOT_REQUEST_SCHEMA,
        "candidate_set_id": candidate_set["candidate_set_id"],
        "role": "source_verifier",
        "evaluations": [{
            "candidate_id": proposal["candidate_id"], "decision": "abstain",
            "reason_codes": ["CRITICAL_ABSTENTION"], "blocking": False,
            "rationale": "The exact source cannot be adjudicated safely.",
        }],
    }
    with pytest.raises(DeliberationError, match="must be blocking"):
        create_ballot(
            cfg, base, actor="reviewer:one", independence_group="review-group:one",
        )

    dissent = copy.deepcopy(base)
    dissent["evaluations"][0].update({
        "decision": "reject", "reason_codes": ["MATERIAL_DISSENT"],
    })
    with pytest.raises(DeliberationError, match="must be blocking"):
        create_ballot(
            cfg, dissent, actor="reviewer:two", independence_group="review-group:two",
        )


def test_forged_content_address_cannot_bypass_live_mechanical_revalidation(tmp_path):
    cfg = _project(tmp_path)
    valid = create_proposal(
        cfg, _extraction_entry(cfg), actor="agent:one", independence_group="group:one",
        recorded_at="2026-07-18T00:00:00Z",
    )
    forged = copy.deepcopy(valid)
    forged["payload"]["claim_text"] = "This sentence is absent from the source."
    from provsleuth import deliberation as module
    forged["candidate_id"] = module._content_id(
        "deliberation-candidate", module._proposal_candidate_core(forged),
    )
    core = {key: forged[key] for key in forged if key != "proposal_id"}
    forged["proposal_id"] = module._content_id("deliberation-proposal", core)
    module.validate_record(forged)

    with pytest.raises(DeliberationError, match="live deterministic revalidation"):
        append_record(cfg, forged)

    # Direct filesystem insertion is also prevented from reaching a frozen panel.
    cfg.deliberation_path.mkdir(parents=True, exist_ok=True)
    path = cfg.deliberation_path / (forged["proposal_id"].rsplit(":", 1)[1] + ".json")
    path.write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(DeliberationError, match="live deterministic revalidation"):
        _freeze(cfg)


def test_multiple_eligible_alternatives_remain_contested_without_hash_tiebreak(tmp_path):
    cfg = _project(tmp_path)
    first = _append_proposal(cfg, _extraction_entry(cfg), "agent:one", "group:one")
    alternative = _extraction_entry(
        cfg, claim="The authors report no population-level causal estimate.",
    )
    alternative["payload"].update({
        "claim_kind": "reporting", "polarity": "negative",
        "qualifiers": ["population-level"],
    })
    second = _append_proposal(cfg, alternative, "agent:two", "group:two", second=1)
    candidate_set = _freeze(cfg)
    candidates = [first["candidate_id"], second["candidate_id"]]
    for index, role in enumerate((
        "source_verifier", "coverage_reviewer", "adversarial_falsifier",
    )):
        _ballot(
            cfg, candidate_set, candidates, role,
            f"reviewer:{index}", f"review-group:{index}", second=30 + index,
        )
    result = evaluate_candidate_set(cfg, candidate_set["candidate_set_id"])
    assert result["status"] == "contested"
    assert result["recommended_candidate_id"] is None
    assert result["findings"][0]["code"] == "MULTIPLE_ELIGIBLE_ALTERNATIVES"


@pytest.mark.parametrize(("other_state", "overall_status"), (
    ("contested", "contested"),
    ("insufficient_review", "insufficient_review"),
    ("blocked", "blocked"),
))
def test_unresolved_frozen_alternative_takes_precedence_over_one_eligible_candidate(
        tmp_path, other_state, overall_status):
    cfg = _project(tmp_path)
    second_source = tmp_path / "paper-two.txt"
    second_claim = "A second candidate reports a narrower estimate."
    second_source.write_text(second_claim + "\n", encoding="utf-8")
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    graph["nodes"].append({
        "id": "doc:paper-two", "type": "artifact", "status": "current",
        "path": "paper-two.txt",
    })
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")

    eligible = _append_proposal(
        cfg, _extraction_entry(cfg), "agent:eligible", "group:eligible",
    )
    alternative_entry = _extraction_entry(
        cfg, claim=second_claim, subject="claim:treatment",
    )
    data = second_source.read_bytes()
    alternative_entry["source_anchor"] = {
        "node_id": "doc:paper-two", "start_byte": 0, "end_byte": len(data),
        "span_sha256": hashlib.sha256(data).hexdigest(),
    }
    alternative_entry["payload"].update({
        "claim_kind": "reporting", "qualifiers": ["narrower estimate"],
    })
    alternative = _append_proposal(
        cfg, alternative_entry, "agent:alternative", "group:alternative", second=1,
    )
    candidate_set = _freeze(cfg)
    for index, role in enumerate((
        "source_verifier", "coverage_reviewer", "adversarial_falsifier",
    )):
        alternative_decision = "endorse"
        if index == 0 and other_state == "contested":
            alternative_decision = "reject"
        elif index == 0 and other_state == "insufficient_review":
            alternative_decision = "abstain"
        _split_ballot(
            cfg, candidate_set, role, f"reviewer:{index}", f"review-group:{index}",
            {alternative["candidate_id"]: alternative_decision}, second=30 + index,
        )
    if other_state == "blocked":
        second_source.write_text("The second source drifted.\n", encoding="utf-8")

    result = evaluate_candidate_set(cfg, candidate_set["candidate_set_id"])
    states = {item["candidate_id"]: item["state"] for item in result["candidates"]}
    assert states[eligible["candidate_id"]] == "eligible_for_human_review"
    assert states[alternative["candidate_id"]] == other_state
    assert result["status"] == overall_status
    assert result["recommended_candidate_id"] is None
    assert any(
        item["code"] == "UNRESOLVED_FROZEN_ALTERNATIVE"
        for item in result["findings"]
    )


@pytest.mark.parametrize("decision", ("approved", "rejected"))
def test_phase_decision_records_approval_or_rejection_and_controls_next_phase(
        tmp_path, decision):
    cfg = _project(tmp_path)
    proposal, candidate_set = _review_ready_panel(cfg)
    document = _phase_decision(cfg, proposal, candidate_set, decision)
    assert document["decision"] == decision
    assert document["candidate_id"] == proposal["candidate_id"]
    assert document["ballot_ids"] == evaluate_candidate_set(
        cfg, candidate_set["candidate_set_id"],
    )["ballot_ids"]
    assert document["human_identity_authenticated"] is False
    assert document["automatic_activation"] is False
    append_record(cfg, document)

    entry = _semantic_entry(cfg, proposal["candidate_id"])
    if decision == "approved":
        downstream = create_proposal(
            cfg, entry, actor="agent:semantic",
            independence_group="group:semantic",
            recorded_at="2026-07-18T00:00:41Z",
        )
        append_record(cfg, downstream)
        records, issues = load_records(cfg)
        assert issues == []
        assert downstream["proposal_id"] in {
            item.get("proposal_id") for item in records
        }
    else:
        with pytest.raises(DeliberationError, match="PHASE_REFERENCE_NOT_APPROVED"):
            create_proposal(
                cfg, entry, actor="agent:semantic",
                independence_group="group:semantic",
                recorded_at="2026-07-18T00:00:41Z",
            )


@pytest.mark.parametrize(("mismatch", "finding_code"), (
    ("phase", "PHASE_REFERENCE_WRONG_PHASE"),
    ("round", "PHASE_REFERENCE_ROUND_MISMATCH"),
    ("subject", "PHASE_REFERENCE_SUBJECT_MISMATCH"),
    ("snapshot", "PHASE_REFERENCE_SNAPSHOT_MISMATCH"),
))
def test_next_phase_reference_rejects_wrong_scope_or_snapshot(
        tmp_path, mismatch, finding_code):
    cfg = _project(tmp_path)
    proposal, candidate_set = _review_ready_panel(cfg)
    append_record(cfg, _phase_decision(cfg, proposal, candidate_set))
    entry = _semantic_entry(cfg, proposal["candidate_id"])

    if mismatch == "phase":
        valid = create_proposal(
            cfg, entry, actor="agent:semantic-one",
            independence_group="group:semantic-one",
            recorded_at="2026-07-18T00:00:41Z",
        )
        append_record(cfg, valid)
        entry = _semantic_entry(cfg, valid["candidate_id"])
    elif mismatch == "round":
        entry["round_id"] = "round:other"
    elif mismatch == "subject":
        entry["subject_key"] = "claim:other"
    else:
        graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
        graph["concepts"]["analysis_version"] = {"selected": "v2"}
        cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")

    with pytest.raises(DeliberationError, match=finding_code):
        create_proposal(
            cfg, entry, actor="agent:semantic-two",
            independence_group="group:semantic-two",
            recorded_at="2026-07-18T00:00:42Z",
        )


def test_phase_decision_forbids_second_decision_and_late_ballot(tmp_path):
    cfg = _project(tmp_path)
    proposal, candidate_set = _review_ready_panel(cfg)
    first = _phase_decision(cfg, proposal, candidate_set)
    append_record(cfg, first)

    with pytest.raises(DeliberationError, match="DUPLICATE_PHASE_DECISION"):
        _phase_decision(
            cfg, proposal, candidate_set, decision="rejected",
            actor="human:second-decider", second=41,
        )
    with pytest.raises(DeliberationError, match="late ballots are forbidden"):
        _ballot(
            cfg, candidate_set, [proposal["candidate_id"]], "source_verifier",
            "reviewer:late", "review-group:late", second=41,
        )


def test_directly_inserted_second_phase_decision_is_an_integrity_error(tmp_path):
    cfg = _project(tmp_path)
    proposal, candidate_set = _review_ready_panel(cfg)
    first = _phase_decision(cfg, proposal, candidate_set)
    append_record(cfg, first)

    from provsleuth import deliberation as module
    inserted = copy.deepcopy(first)
    inserted.update({
        "recorded_at": "2026-07-18T00:00:41Z",
        "decision": "rejected", "actor": "human:direct-insertion",
        "rationale": "A second decision inserted outside the append boundary.",
    })
    core = {key: inserted[key] for key in inserted if key != "decision_id"}
    inserted["decision_id"] = module._content_id("deliberation-decision", core)
    module.validate_record(inserted)
    path = cfg.deliberation_path / (
        inserted["decision_id"].rsplit(":", 1)[1] + ".json"
    )
    path.write_text(json.dumps(inserted), encoding="utf-8")

    _records, issues = load_records(cfg)
    assert any(item["code"] == "DUPLICATE_PHASE_DECISION" for item in issues)
    status = deliberation_status(cfg)
    assert status["integrity"] == "error"
    assert status["panels"][0]["status"] == "blocked"
    assert status["panels"][0]["recommended_candidate_id"] is None


def test_directly_inserted_decision_cannot_approve_an_insufficient_panel(tmp_path):
    cfg = _project(tmp_path)
    proposal = _append_proposal(
        cfg, _extraction_entry(cfg), "agent:one", "group:one",
    )
    candidate_set = _freeze(cfg)
    ballot = _ballot(
        cfg, candidate_set, [proposal["candidate_id"]], "source_verifier",
        "reviewer:source", "review-group:source", second=30,
    )
    from provsleuth import deliberation as module
    core = {
        "schema_version": module.PHASE_DECISION_SCHEMA,
        "record_type": "phase_decision",
        "recorded_at": "2026-07-18T00:00:40Z",
        "candidate_set_id": candidate_set["candidate_set_id"],
        "candidate_id": proposal["candidate_id"],
        "ballot_ids": [ballot["ballot_id"]],
        "decision": "approved",
        "actor": "reviewer:direct-insertion",
        "rationale": "Attempt to bypass missing review roles.",
        "human_identity_authenticated": False,
        "automatic_activation": False,
    }
    inserted = {
        "decision_id": module._content_id("deliberation-decision", core), **core,
    }
    module.validate_record(inserted)
    path = cfg.deliberation_path / (
        inserted["decision_id"].rsplit(":", 1)[1] + ".json"
    )
    path.write_text(json.dumps(inserted), encoding="utf-8")
    _records, issues = load_records(cfg)
    assert any(
        item["code"] == "PHASE_DECISION_PANEL_NOT_RECOMMENDED"
        for item in issues
    )
    status = deliberation_status(cfg)
    assert status["integrity"] == "error"
    assert status["panels"][0]["status"] == "blocked"


def test_recording_phase_decision_changes_only_the_deliberation_ledger(tmp_path):
    cfg = _project(tmp_path, with_logic=True)
    proposal, candidate_set = _review_ready_panel(cfg)
    decision = _phase_decision(cfg, proposal, candidate_set)

    def outside_ledger_bytes():
        return {
            path.relative_to(tmp_path).as_posix(): path.read_bytes()
            for path in tmp_path.rglob("*")
            if path.is_file() and cfg.deliberation_path not in path.parents
        }

    before = outside_ledger_bytes()
    append_record(cfg, decision)
    assert outside_ledger_bytes() == before


def test_proposer_cannot_review_owned_candidate_and_ballot_is_atomic(tmp_path):
    cfg = _project(tmp_path)
    proposal = _append_proposal(cfg, _extraction_entry(cfg), "agent:one", "group:one")
    candidate_set = _freeze(cfg)
    with pytest.raises(DeliberationError, match="every non-owned candidate"):
        create_ballot(cfg, {
            "schema_version": BALLOT_REQUEST_SCHEMA,
            "candidate_set_id": candidate_set["candidate_set_id"],
            "role": "source_verifier",
            "evaluations": [{
                "candidate_id": proposal["candidate_id"], "decision": "endorse",
                "reason_codes": [], "blocking": False, "rationale": "Self-review.",
            }],
        }, actor="agent:one", independence_group="group:one")


def test_source_drift_blocks_an_otherwise_complete_panel(tmp_path):
    cfg = _project(tmp_path)
    proposal = _append_proposal(cfg, _extraction_entry(cfg), "agent:one", "group:one")
    candidate_set = _freeze(cfg)
    for index, role in enumerate((
        "source_verifier", "coverage_reviewer", "adversarial_falsifier",
    )):
        _ballot(
            cfg, candidate_set, [proposal["candidate_id"]], role,
            f"reviewer:{index}", f"review-group:{index}", second=30 + index,
        )
    cfg.resolve("paper.txt").write_text("Changed source.\n", encoding="utf-8")
    result = evaluate_candidate_set(cfg, candidate_set["candidate_set_id"])
    assert result["status"] == "blocked"
    codes = {
        finding["code"]
        for finding in result["candidates"][0]["mechanical_findings"]
    }
    assert "SOURCE_ANCHOR_INVALID" in codes


def test_store_corruption_is_visible_and_suppresses_status_integrity(tmp_path):
    cfg = _project(tmp_path)
    _append_proposal(cfg, _extraction_entry(cfg), "agent:one", "group:one")
    (cfg.deliberation_path / "unexpected.txt").write_text("x", encoding="utf-8")
    records, issues = load_records(cfg)
    assert records
    assert issues[0]["code"] == "DELIBERATION_ENTRY_UNEXPECTED"
    status = deliberation_status(cfg)
    assert status["integrity"] == "error"


def test_store_integrity_error_suppresses_an_otherwise_recommended_panel(tmp_path):
    cfg = _project(tmp_path)
    proposal = _append_proposal(cfg, _extraction_entry(cfg), "agent:one", "group:one")
    candidate_set = _freeze(cfg)
    for index, role in enumerate((
        "source_verifier", "coverage_reviewer", "adversarial_falsifier",
    )):
        _ballot(
            cfg, candidate_set, [proposal["candidate_id"]], role,
            f"reviewer:{index}", f"review-group:{index}", second=30 + index,
        )
    assert evaluate_candidate_set(
        cfg, candidate_set["candidate_set_id"],
    )["status"] == "recommended_for_human_review"

    (cfg.deliberation_path / "unexpected.txt").write_text("x", encoding="utf-8")
    status = deliberation_status(cfg)
    panel = status["panels"][0]
    assert status["integrity"] == "error"
    assert panel["status"] == "blocked"
    assert panel["recommended_candidate_id"] is None
    assert any(
        item["code"] == "DELIBERATION_ENTRY_UNEXPECTED"
        for item in panel["findings"]
    )


def test_deliberation_cli_proposes_freezes_and_reports_without_activation(tmp_path, capsys):
    cfg = _project(tmp_path)
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(json.dumps(_extraction_entry(cfg)), encoding="utf-8")
    assert main([
        "--config", str(cfg.config_path), "deliberate-propose", str(proposal_path),
        "--actor", "agent:one", "--independence-group", "group:one", "--json",
    ]) == 0
    proposal_output = json.loads(capsys.readouterr().out)
    assert proposal_output["automatic_activation"] is False

    freeze_path = tmp_path / "freeze.json"
    freeze_path.write_text(json.dumps({
        "schema_version": CANDIDATE_SET_REQUEST_SCHEMA,
        "round_id": "round:1", "phase": "claim_extraction",
        "subject_key": "claim:treatment",
    }), encoding="utf-8")
    assert main([
        "--config", str(cfg.config_path), "deliberate-freeze", str(freeze_path),
        "--actor", "human:owner", "--json",
    ]) == 0
    frozen_output = json.loads(capsys.readouterr().out)
    candidate_set_id = frozen_output["record_id"]
    assert main([
        "--config", str(cfg.config_path), "deliberations", candidate_set_id, "--json",
    ]) == 1
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "insufficient_review"
    assert status["human_activation_required"] is True


def test_deliberate_decide_cli_records_routing_without_authentication_or_activation(
        tmp_path, capsys):
    cfg = _project(tmp_path)
    proposal, candidate_set = _review_ready_panel(cfg)
    request_path = tmp_path / "phase-decision.json"
    request_path.write_text(json.dumps({
        "schema_version": PHASE_DECISION_REQUEST_SCHEMA,
        "candidate_set_id": candidate_set["candidate_set_id"],
        "candidate_id": proposal["candidate_id"],
        "decision": "approved",
        "rationale": "Route the reviewed candidate to the next proposal phase.",
    }), encoding="utf-8")
    assert main([
        "--config", str(cfg.config_path), "deliberate-decide", str(request_path),
        "--actor", "reviewer:cli-decision", "--json",
    ]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["record"]["record_type"] == "phase_decision"
    assert output["record"]["human_identity_authenticated"] is False
    assert output["record"]["automatic_activation"] is False
    assert output["human_identity_authenticated"] is False
    panel = evaluate_candidate_set(cfg, candidate_set["candidate_set_id"])
    assert panel["phase_routing_state"] == "approved_for_next_phase"


def _vocabulary():
    return {
        "schema_version": "claimtrace.symbolic-vocabulary/1",
        "id": "study:vocabulary", "version": "1.0.0",
        "types": [{"id": "study:item", "base": "ct:symbol"}],
        "units": [],
        "predicates": [{
            "id": "study:observed", "kind": "input",
            "arguments": [
                {"name": "item", "type": "study:item", "unit": None},
                {"name": "ok", "type": "ct:boolean", "unit": None},
            ],
        }, {
            "id": "study:supported", "kind": "derived",
            "arguments": [{"name": "item", "type": "study:item", "unit": None}],
        }],
        "renderers": [],
    }


def test_rule_phase_requires_the_complete_competency_matrix(tmp_path):
    cfg = _project(tmp_path, with_logic=True)
    # This test reaches the rule payload validator directly; phase-chain references are
    # checked separately when the complete proposal is admitted to the ledger.
    from provsleuth import deliberation as module

    item = {"type": "study:item", "value": "x", "unit": None}
    boolean = {"type": "ct:boolean", "value": True, "unit": None}
    target = {
        "predicate": "study:supported", "polarity": "positive",
        "arguments": {"item": item},
    }
    rule_pack = {
        "schema_version": "claimtrace.symbolic-rules/1",
        "id": "study:rules", "version": "1.0.0",
        "vocabulary_id": "study:vocabulary",
        "rules": [{
            "id": "study:derive", "when": [{
                "predicate": "study:observed", "polarity": "positive",
                "arguments": {"item": {"var": "item"}, "ok": {"var": "ok"}},
            }],
            "where": [{
                "op": "eq", "left": {"var": "ok"},
                "right": {"const": boolean},
            }],
            "then": {
                "predicate": "study:supported", "polarity": "positive",
                "arguments": {"item": {"var": "item"}},
            },
        }],
    }
    payload = {
        "formalization_candidate_id": "deliberation-candidate:sha256:" + "0" * 64,
        "vocabulary_id": "study:vocabulary", "rule_pack": rule_pack,
        "warrant": "A source-grounded warrant is required.", "scope": "This dataset only.",
        "assumptions": [], "non_equivalences": ["Reporting is not truth."],
        "competency_cases": [{
            "id": "positive", "category": "positive", "input_atoms": [],
            "target": target, "expected_state": "unknown",
        }],
    }
    with pytest.raises(DeliberationError, match="omit mandatory categories"):
        module._normal_payload(
            cfg, "rule_validity", payload,
            {"excerpt": "A source-grounded warrant is required."},
        )


def _study_term(value):
    return {"type": "study:item", "value": value, "unit": None}


def _boolean_term(value):
    return {"type": "ct:boolean", "value": value, "unit": None}


def _observed_atom(item, ok, *, item_type="study:item", item_unit=None):
    return {
        "predicate": "study:observed", "polarity": "positive",
        "arguments": {
            "item": {"type": item_type, "value": item, "unit": item_unit},
            "ok": _boolean_term(ok),
        },
    }


def _complete_rule_payload():
    target = {
        "predicate": "study:supported", "polarity": "positive",
        "arguments": {"item": _study_term("x")},
    }
    variable_atom = {
        "predicate": "study:observed", "polarity": "positive",
        "arguments": {"item": {"var": "item"}, "ok": {"var": "ok"}},
    }
    rule_pack = {
        "schema_version": "claimtrace.symbolic-rules/1",
        "id": "study:rules", "version": "1.0.0",
        "vocabulary_id": "study:vocabulary",
        "rules": [{
            "id": "study:derive-positive", "when": [variable_atom],
            "where": [{
                "op": "eq", "left": {"var": "ok"},
                "right": {"const": _boolean_term(True)},
            }],
            "then": {
                "predicate": "study:supported", "polarity": "positive",
                "arguments": {"item": {"var": "item"}},
            },
        }, {
            "id": "study:derive-negative", "when": [variable_atom],
            "where": [{
                "op": "eq", "left": {"var": "ok"},
                "right": {"const": _boolean_term(False)},
            }],
            "then": {
                "predicate": "study:supported", "polarity": "negative",
                "arguments": {"item": {"var": "item"}},
            },
        }],
    }
    related_boundary = _observed_atom("x", True)
    related_boundary["polarity"] = "negative"
    related_counterexample = _observed_atom("x", True)
    related_counterexample["polarity"] = "negative"
    cases = [
        {"id": "positive", "category": "positive",
         "input_atoms": [_observed_atom("x", True)], "target": target,
         "expected_state": "derivable"},
        {"id": "negative", "category": "explicit_negative",
         "input_atoms": [_observed_atom("x", False)], "target": target,
         "expected_state": "refutable"},
        {"id": "missing", "category": "missing_premise",
         "input_atoms": [], "target": target, "expected_state": "unknown"},
        {"id": "boundary", "category": "boundary",
         "input_atoms": [related_boundary], "target": target,
         "expected_state": "unknown"},
        {"id": "unit", "category": "unit_mismatch",
         "input_atoms": [_observed_atom("x", True, item_unit="study:wrong-unit")],
         "target": target, "expected_state": "input_rejected"},
        {"id": "conflict", "category": "conflict",
         "input_atoms": [_observed_atom("x", True), _observed_atom("x", False)],
         "target": target, "expected_state": "conflict"},
        {"id": "counterexample", "category": "counterexample",
         "input_atoms": [_observed_atom("x", False), related_counterexample],
         "target": target, "expected_state": "refutable"},
    ]
    return {
        "formalization_candidate_id": "deliberation-candidate:sha256:" + "0" * 64,
        "vocabulary_id": "study:vocabulary", "rule_pack": rule_pack,
        "warrant": "The treatment increased score in this sample.",
        "scope": "This declared study only.", "assumptions": [],
        "non_equivalences": ["Derivability is not scientific truth."],
        "competency_cases": cases,
    }


def test_rule_competency_categories_have_domain_neutral_behavioral_constraints(tmp_path):
    cfg = _project(tmp_path, with_logic=True)
    from provsleuth import deliberation as module
    payload = _complete_rule_payload()
    normalized, mechanical = module._normal_payload(
        cfg, "rule_validity", payload,
        {"excerpt": "The treatment increased score in this sample."},
    )
    assert mechanical["passed"] is True
    assert all(item["passed"] for item in normalized["competency_cases"])

    relabeled = copy.deepcopy(payload)
    for case in relabeled["competency_cases"]:
        case["input_atoms"] = []
        case["expected_state"] = "unknown"
    with pytest.raises(DeliberationError, match="category 'positive' requires"):
        module._normal_payload(
            cfg, "rule_validity", relabeled,
            {"excerpt": "The treatment increased score in this sample."},
        )


def _rule_case(payload, category):
    return next(
        item for item in payload["competency_cases"]
        if item["category"] == category
    )


def _normalize_rule_payload(cfg, payload):
    from provsleuth import deliberation as module
    return module._normal_payload(
        cfg, "rule_validity", payload,
        {"excerpt": "The treatment increased score in this sample."},
    )


def test_legacy_boundary_rejects_an_unrelated_subject_substitution(tmp_path):
    cfg = _project(tmp_path, with_logic=True)
    payload = _complete_rule_payload()
    boundary = _rule_case(payload, "boundary")
    boundary["input_atoms"] = [_observed_atom("y", True)]
    with pytest.raises(DeliberationError):
        _normalize_rule_payload(cfg, payload)


def test_legacy_unit_mismatch_cannot_be_satisfied_by_a_type_mismatch(tmp_path):
    cfg = _project(tmp_path, with_logic=True)
    payload = _complete_rule_payload()
    unit_case = _rule_case(payload, "unit_mismatch")
    unit_case["input_atoms"] = [
        _observed_atom("x", True, item_type="ct:boolean"),
    ]
    with pytest.raises(DeliberationError):
        _normalize_rule_payload(cfg, payload)


def test_legacy_conflict_requires_the_exact_positive_negative_union(tmp_path):
    cfg = _project(tmp_path, with_logic=True)
    payload = _complete_rule_payload()
    conflict = _rule_case(payload, "conflict")
    conflict["input_atoms"].append(_observed_atom("y", True))
    with pytest.raises(DeliberationError):
        _normalize_rule_payload(cfg, payload)


def test_legacy_counterexample_rejects_irrelevant_extra_evidence(tmp_path):
    cfg = _project(tmp_path, with_logic=True)
    payload = _complete_rule_payload()
    counterexample = _rule_case(payload, "counterexample")
    counterexample["input_atoms"] = [
        _observed_atom("x", False), _observed_atom("y", True),
    ]
    with pytest.raises(DeliberationError):
        _normalize_rule_payload(cfg, payload)


def _formalization_payload():
    return {
        "interpretation_candidate_id": (
            "deliberation-candidate:sha256:" + "1" * 64
        ),
        "vocabulary_id": "study:vocabulary",
        "target": {
            "predicate": "study:supported", "polarity": "positive",
            "arguments": {"item": _study_term("x")},
        },
        "evidence_plan": {
            "schema_version": "claimtrace.symbolic-evidence-plan/1",
            "required_bindings": [{
                "result_id": "result:planned", "binding_id": "study:observed-binding",
            }],
        },
        "method_requirements": [{
            "method_id": "method:planned", "step_ids": ["fit"],
        }],
        "assumptions": ["The planned output follows the declared schema."],
        "non_equivalences": ["A planned test is not an observed result."],
    }


def test_formalization_resolves_planned_graph_bindings_and_method_steps(tmp_path):
    cfg = _project(tmp_path, with_logic=True)
    from provsleuth import deliberation as module
    payload = _formalization_payload()
    with pytest.raises(DeliberationError, match="unknown result node"):
        module._normal_payload(
            cfg, "formalization", payload, {"excerpt": "source"},
        )

    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    graph["nodes"].extend([{
        "id": "result:planned", "type": "artifact", "status": "planned",
        "path": "results/planned.json",
        "logic_bindings": [{
            "id": "study:observed-binding", "vocabulary_id": "study:vocabulary",
            "predicate": "study:observed", "polarity": "positive",
            "arguments": {
                "item": {"kind": "json_pointer", "pointer": "/item"},
                "ok": {"kind": "json_pointer", "pointer": "/ok"},
            },
        }],
    }, {
        "id": "method:planned", "type": "method", "status": "planned",
        "method_spec": {
            "schema_version": "claimtrace.method-spec/1",
            "steps": [{
                "id": "fit", "statement": "Fit the declared model.",
                "required": True,
            }],
        },
    }])
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    normalized, mechanical = module._normal_payload(
        cfg, "formalization", payload, {"excerpt": "source"},
    )
    assert normalized["evidence_plan"] == payload["evidence_plan"]
    assert "declared_graph_evidence_bindings" in mechanical["checks"]

    unknown_step = copy.deepcopy(payload)
    unknown_step["method_requirements"][0]["step_ids"] = ["unreported-step"]
    with pytest.raises(DeliberationError, match="unknown method step"):
        module._normal_payload(
            cfg, "formalization", unknown_step, {"excerpt": "source"},
        )
