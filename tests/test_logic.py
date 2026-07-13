"""Tests for portable, deterministic symbolic derivations."""
import copy
import contextlib
import hashlib
import json
import shutil

import pytest

import claimtrace.logic as logic_module
from claimtrace.config import Config
from claimtrace.logic import (
    LogicError,
    append_derivation,
    create_derivation,
    create_derivation_from_bindings,
    evaluate_derivation,
    load_derivations,
    load_rule_pack,
    load_vocabulary,
    validate_derivation_document,
)


FIXED_TIME = "2026-07-14T01:00:00.000Z"


def _term(type_id, value, unit=None):
    return {"type": type_id, "value": value, "unit": unit}


def _target():
    return {
        "predicate": "gate:configured_gate_passed",
        "polarity": "positive",
        "arguments": {"release": _term("gate:release", "release-1")},
    }


def _vocabulary():
    return {
        "schema_version": "claimtrace.symbolic-vocabulary/1",
        "id": "gate:vocabulary",
        "version": "1.0.0",
        "types": [{"id": "gate:release", "base": "ct:symbol"}],
        "units": [],
        "predicates": [
            {
                "id": "gate:test_completed",
                "kind": "input",
                "arguments": [
                    {"name": "release", "type": "gate:release", "unit": None},
                    {"name": "failed", "type": "ct:integer", "unit": None},
                ],
            },
            {
                "id": "gate:scan_completed",
                "kind": "input",
                "arguments": [
                    {"name": "release", "type": "gate:release", "unit": None},
                    {"name": "critical", "type": "ct:integer", "unit": None},
                ],
            },
            {
                "id": "gate:configured_gate_passed",
                "kind": "derived",
                "arguments": [
                    {"name": "release", "type": "gate:release", "unit": None},
                ],
            },
        ],
        "renderers": [
            {
                "id": "gate:pass-en",
                "predicate": "gate:configured_gate_passed",
                "polarity": "positive",
                "language": "en",
                "template": "{release} satisfies the configured test and scan gates.",
            },
            {
                "id": "gate:fail-en",
                "predicate": "gate:configured_gate_passed",
                "polarity": "negative",
                "language": "en",
                "template": "{release} does not satisfy the configured test and scan gates.",
            },
        ],
    }


def _var(name):
    return {"var": name}


def _const(type_id, value):
    return {"const": _term(type_id, value)}


def _rules():
    test = {
        "predicate": "gate:test_completed",
        "polarity": "positive",
        "arguments": {"release": _var("release"), "failed": _var("failed")},
    }
    scan = {
        "predicate": "gate:scan_completed",
        "polarity": "positive",
        "arguments": {"release": _var("release"), "critical": _var("critical")},
    }
    positive = {
        "predicate": "gate:configured_gate_passed",
        "polarity": "positive",
        "arguments": {"release": _var("release")},
    }
    negative = copy.deepcopy(positive)
    negative["polarity"] = "negative"
    return {
        "schema_version": "claimtrace.symbolic-rules/1",
        "id": "gate:rules",
        "version": "1.0.0",
        "vocabulary_id": "gate:vocabulary",
        "rules": [
            {
                "id": "gate:pass",
                "when": [test, scan],
                "where": [
                    {"op": "eq", "left": _var("failed"), "right": _const("ct:integer", "0")},
                    {"op": "eq", "left": _var("critical"), "right": _const("ct:integer", "0")},
                ],
                "then": positive,
            },
            {
                "id": "gate:test-failed",
                "when": [test],
                "where": [
                    {"op": "gt", "left": _var("failed"), "right": _const("ct:integer", "0")},
                ],
                "then": negative,
            },
            {
                "id": "gate:scan-failed",
                "when": [scan],
                "where": [
                    {"op": "gt", "left": _var("critical"), "right": _const("ct:integer", "0")},
                ],
                "then": negative,
            },
        ],
    }


def _fact(predicate, result_id, binding_id, **values):
    return {
        "atom": {
            "predicate": predicate,
            "polarity": "positive",
            "arguments": values,
        },
        "evidence": [{"result_id": result_id, "binding_id": binding_id}],
        "assumption": None,
    }


def _agent_input(*, failed="0", critical="0", include_scan=True, conflict=False):
    facts = [_fact(
        "gate:test_completed", "art:test",
        "gate:test-completed",
        release=_term("gate:release", "release-1"),
        failed=_term("ct:integer", failed),
    )]
    if include_scan:
        facts.append(_fact(
            "gate:scan_completed", "art:scan",
            "gate:scan-completed",
            release=_term("gate:release", "release-1"),
            critical=_term("ct:integer", critical),
        ))
    if conflict:
        facts.append(_fact(
            "gate:test_completed", "art:test",
            "gate:test-conflict",
            release=_term("gate:release", "release-1"),
            failed=_term("ct:integer", "2"),
        ))
    return {
        "target": _target(),
        "facts": facts,
        "note": "Portable release-policy example.",
        "provenance": {"agent": "agent:test"},
    }


def _project(tmp_path, *, failed=0, critical=0, logic=True):
    (tmp_path / "claimtrace").mkdir()
    (tmp_path / "claimtrace" / "logic").mkdir()
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "test.json").write_text(
        json.dumps({"release": "release-1", "failed": failed, "failed_conflict": 2}),
        encoding="utf-8"
    )
    (tmp_path / "results" / "scan.json").write_text(
        json.dumps({"release": "release-1", "critical": critical}), encoding="utf-8"
    )
    claim = {
        "id": "claim:gate", "type": "claim", "status": "current",
        "value": "release-1 satisfies the configured test and scan gates",
    }
    if logic:
        claim["logic"] = {
            "vocabulary_id": "gate:vocabulary", "rule_pack_id": "gate:rules",
            "target": _target(),
        }
    test_bindings = [
        {"id": "gate:test-completed", "vocabulary_id": "gate:vocabulary",
         "predicate": "gate:test_completed", "polarity": "positive",
         "arguments": {
             "release": {"kind": "json_pointer", "pointer": "/release"},
             "failed": {"kind": "json_pointer", "pointer": "/failed"},
         }},
        {"id": "gate:test-conflict", "vocabulary_id": "gate:vocabulary",
         "predicate": "gate:test_completed", "polarity": "positive",
         "arguments": {
             "release": {"kind": "json_pointer", "pointer": "/release"},
             "failed": {"kind": "json_pointer", "pointer": "/failed_conflict"},
         }},
    ]
    scan_bindings = [
        {"id": "gate:scan-completed", "vocabulary_id": "gate:vocabulary",
         "predicate": "gate:scan_completed", "polarity": "positive",
         "arguments": {
             "release": {"kind": "json_pointer", "pointer": "/release"},
             "critical": {"kind": "json_pointer", "pointer": "/critical"},
         }},
    ]
    graph = {
        "schema_version": "1.0", "concepts": {},
        "nodes": [
            claim,
            {"id": "art:test", "type": "artifact", "status": "current",
             "path": "results/test.json", "logic_bindings": test_bindings},
            {"id": "art:scan", "type": "artifact", "status": "current",
             "path": "results/scan.json", "logic_bindings": scan_bindings},
        ],
        "edges": [],
    }
    (tmp_path / "claimtrace" / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    vocabulary_path = tmp_path / "claimtrace" / "logic" / "vocabulary.json"
    rules_path = tmp_path / "claimtrace" / "logic" / "rules.json"
    vocabulary_path.write_text(json.dumps(_vocabulary()), encoding="utf-8")
    rules_path.write_text(json.dumps(_rules()), encoding="utf-8")
    (tmp_path / "claimtrace.config.json").write_text(json.dumps({
        "root": ".", "graph": "claimtrace/graph.json",
        "logic": {
            "derivations": "claimtrace/derivations",
            "vocabularies": ["claimtrace/logic/vocabulary.json"],
            "rule_packs": ["claimtrace/logic/rules.json"],
        },
    }), encoding="utf-8")
    return Config(tmp_path / "claimtrace.config.json")


def _make(cfg, agent_input=None, result_ids=None, vocabulary=None, rules=None):
    return create_derivation(
        cfg, "claim:gate", result_ids or ["art:scan", "art:test"],
        agent_input or _agent_input(),
        vocabulary=vocabulary or _vocabulary(), rule_pack=rules or _rules(),
        actor="agent:test", recorded_at=FIXED_TIME,
    )


def test_binding_selection_materializes_project_target_and_artifact_values(tmp_path):
    cfg = _project(tmp_path)
    automatic = create_derivation_from_bindings(
        cfg,
        "claim:gate",
        [
            {"result_id": "art:scan", "binding_id": "gate:scan-completed"},
            {"result_id": "art:test", "binding_id": "gate:test-completed"},
        ],
        actor="agent:auto",
        provenance={"agent": "agent:auto"},
        note="Only project-approved binding identities were selected.",
        recorded_at=FIXED_TIME,
    )
    verbose = _make(cfg)

    assert automatic["agent_input"]["target"] == _target()
    assert automatic["agent_input"]["facts"] == verbose["agent_input"]["facts"]
    assert automatic["subject"]["result_ids"] == ["art:scan", "art:test"]
    assert automatic["derived"]["proof_id"] == verbose["derived"]["proof_id"]
    assert automatic["derived"]["active"] is True


def test_binding_selection_rejects_injected_or_duplicate_profiles(tmp_path):
    cfg = _project(tmp_path)
    common = {
        "actor": "agent:auto", "provenance": {"agent": "agent:auto"},
    }
    with pytest.raises(LogicError, match="exactly result_id and binding_id"):
        create_derivation_from_bindings(
            cfg, "claim:gate",
            [{"result_id": "art:test", "binding_id": "gate:test-completed",
              "target": _target()}],
            **common,
        )
    selection = {"result_id": "art:test", "binding_id": "gate:test-completed"}
    with pytest.raises(LogicError, match="must not contain duplicates"):
        create_derivation_from_bindings(
            cfg, "claim:gate", [selection, selection], **common,
        )
    with pytest.raises(LogicError, match="does not declare binding"):
        create_derivation_from_bindings(
            cfg, "claim:gate",
            [{"result_id": "art:test", "binding_id": "gate:not-declared"}],
            **common,
        )


def test_release_gate_derives_one_composite_portable_proof(tmp_path):
    cfg = _project(tmp_path)
    document = _make(cfg)
    assert document["derived"]["proof_state"] == "derivable"
    assert document["derived"]["active"] is True
    assert document["derived"]["rendered_target"].startswith("release-1 satisfies")
    assert document["derived"]["outcome_relation"] == "target_derived"
    assert document["derived"]["rendered_outcomes"] == [
        "release-1 satisfies the configured test and scan gates.",
    ]
    assert len(document["subject"]["result_ids"]) == 2
    validate_derivation_document(document)
    path = append_derivation(cfg, document)
    assert path.name == document["id"].split(":")[-1] + ".json"
    loaded, issues = load_derivations(cfg)
    assert issues == []
    assert loaded == [document]
    assert evaluate_derivation(
        cfg, document, vocabulary=_vocabulary(), rule_pack=_rules(),
    )["proof_state"] == "derivable"


def test_missing_scan_is_unknown_not_pass(tmp_path):
    cfg = _project(tmp_path)
    document = _make(
        cfg, _agent_input(include_scan=False), result_ids=["art:test"],
    )
    assert document["derived"]["proof_state"] == "unknown"
    assert document["derived"]["active"] is False


def test_known_failure_refutes_target(tmp_path):
    cfg = _project(tmp_path, failed=2)
    document = _make(
        cfg, _agent_input(failed="2", include_scan=False), result_ids=["art:test"],
    )
    assert document["derived"]["proof_state"] == "refutable"
    assert document["derived"]["active"] is True


def test_explicit_conflict_is_paraconsistent_and_inactive(tmp_path):
    cfg = _project(tmp_path)
    document = _make(cfg, _agent_input(conflict=True))
    assert document["derived"]["proof_state"] == "conflict"
    assert document["derived"]["active"] is False
    assert {item["code"] for item in document["derived"]["findings"]} >= {
        "SYMBOLIC_CONFLICT",
    }


def test_unformalized_claim_is_a_candidate_not_active(tmp_path):
    cfg = _project(tmp_path, logic=False)
    document = _make(cfg)
    assert document["derived"]["proof_state"] == "derivable"
    assert document["derived"]["claim_bound"] is False
    assert document["derived"]["active"] is False
    assert "CLAIM_LOGIC_UNDECLARED" in {
        item["code"] for item in document["derived"]["findings"]
    }


def test_artifact_and_rule_changes_stale_old_proof(tmp_path):
    cfg = _project(tmp_path)
    document = _make(cfg)
    (cfg.root / "results" / "test.json").write_text(
        json.dumps({"failed": 1, "failed_conflict": 2}), encoding="utf-8"
    )
    stale = evaluate_derivation(
        cfg, document, vocabulary=_vocabulary(), rule_pack=_rules(),
    )
    assert stale["stale"] is True
    assert stale["active"] is False
    assert any(
        item["kind"] == "result" and item["id"] == "art:test"
        for item in stale["drift"]
    )
    changed_rules = _rules()
    changed_rules["version"] = "1.0.1"
    stale_rules = evaluate_derivation(
        cfg, document, vocabulary=_vocabulary(), rule_pack=changed_rules,
    )
    assert stale_rules["stale"] is True
    assert any(item["kind"] == "rule_pack" for item in stale_rules["drift"])


def test_proof_identity_survives_project_copy_and_input_order(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    cfg = _project(source)
    first = _make(cfg)
    copied = tmp_path / "copied"
    shutil.copytree(source, copied)
    second_input = _agent_input()
    second_input["facts"].reverse()
    second = _make(Config(copied / "claimtrace.config.json"), second_input)
    assert first["derived"]["proof_id"] == second["derived"]["proof_id"]
    assert first["id"] == second["id"]


def test_invalid_anchor_is_excluded_and_fails_closed(tmp_path):
    cfg = _project(tmp_path)
    bad = _agent_input()
    bad["facts"][0]["atom"]["arguments"]["failed"]["value"] = "1"
    document = _make(cfg, bad)
    assert document["derived"]["active"] is False
    assert "EVIDENCE_ANCHOR_INVALID" in {
        item["code"] for item in document["derived"]["findings"]
    }


def test_corrupt_store_is_reported_without_loading_document(tmp_path):
    cfg = _project(tmp_path)
    document = _make(cfg)
    path = append_derivation(cfg, document)
    path.write_text("{}", encoding="utf-8")
    loaded, issues = load_derivations(cfg)
    assert loaded == []
    assert [item["code"] for item in issues] == ["DERIVATION_INTEGRITY"]


def test_vocabulary_rejects_duplicate_unknown_and_unsafe_template_fields():
    duplicate = _vocabulary()
    duplicate["predicates"].append(copy.deepcopy(duplicate["predicates"][0]))
    with pytest.raises(LogicError, match="duplicate predicate"):
        load_vocabulary(duplicate)
    unknown = _vocabulary()
    unknown["predicates"][0]["arguments"][0]["type"] = "missing:type"
    with pytest.raises(LogicError, match="unknown type"):
        load_vocabulary(unknown)
    unsafe = _vocabulary()
    unsafe["renderers"][0]["template"] = "{release.__class__}"
    with pytest.raises(LogicError, match="simple .* placeholders"):
        load_vocabulary(unsafe)


def test_rules_reject_unknown_operator_unbound_variables_and_input_heads():
    bad_op = _rules()
    bad_op["rules"][0]["where"][0]["op"] = "eval"
    with pytest.raises(LogicError, match="unsupported rule comparator"):
        load_rule_pack(bad_op, _vocabulary())
    unbound = _rules()
    unbound["rules"][0]["then"]["arguments"]["release"] = {"var": "ghost"}
    with pytest.raises(LogicError, match="unbound rule variable"):
        load_rule_pack(unbound, _vocabulary())
    input_head = _rules()
    input_head["rules"][0]["then"] = copy.deepcopy(input_head["rules"][0]["when"][0])
    with pytest.raises(LogicError, match="heads must use derived"):
        load_rule_pack(input_head, _vocabulary())


def test_decimal_terms_must_be_strings_and_are_normalized(tmp_path):
    vocabulary = _vocabulary()
    vocabulary["predicates"][0]["arguments"][1]["type"] = "ct:decimal"
    rules = _rules()
    for rule in rules["rules"]:
        for atom in rule["when"]:
            if atom["predicate"] == "gate:test_completed":
                atom["arguments"]["failed"] = _var("failed")
        for condition in rule["where"]:
            if condition["left"] == _var("failed"):
                condition["right"] = _const("ct:decimal", "0.00")
    normalized = load_rule_pack(rules, vocabulary)
    constant = normalized["rules"][0]["where"][0]["right"]["const"]
    assert constant["value"] == "0"
    broken = copy.deepcopy(rules)
    broken["rules"][0]["where"][0]["right"] = {
        "const": {"type": "ct:decimal", "value": 0.0, "unit": None},
    }
    with pytest.raises(LogicError, match="decimal values must be JSON strings"):
        load_rule_pack(broken, vocabulary)


def test_text_anchor_is_byte_exact(tmp_path):
    cfg = _project(tmp_path)
    text_path = cfg.root / "results" / "test.txt"
    text_path.write_bytes(b"release=release-1\nfailed=0\n")
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    test_node = next(item for item in graph["nodes"] if item["id"] == "art:test")
    test_node["path"] = "results/test.txt"
    test_node["logic_bindings"] = [{
        "id": "gate:test-completed", "vocabulary_id": "gate:vocabulary",
        "predicate": "gate:test_completed", "polarity": "positive",
        "arguments": {
            "release": {"kind": "text_lines", "start_line": 1, "end_line": 1,
                        "prefix": "release=", "suffix": "\n"},
            "failed": {"kind": "text_lines", "start_line": 2, "end_line": 2,
                       "prefix": "failed=", "suffix": "\n"},
        },
    }]
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    document = _make(cfg, _agent_input())
    assert "EVIDENCE_ANCHOR_INVALID" not in {
        item["code"] for item in document["derived"]["findings"]
    }
    binding_check = next(
        item for item in document["mechanical_snapshot"]["anchor_checks"]
        if item["binding_id"] == "gate:test-completed"
    )
    failed_check = next(
        item for item in binding_check["argument_checks"] if item["argument"] == "failed"
    )
    assert failed_check["observed_text_sha256"] == hashlib.sha256(
        b"failed=0\n"
    ).hexdigest()


def test_json_numeric_strings_do_not_ground_numeric_terms(tmp_path):
    cfg = _project(tmp_path)
    (cfg.root / "results" / "test.json").write_text(json.dumps({
        "release": "release-1", "failed": "0", "failed_conflict": 2,
    }), encoding="utf-8")
    document = _make(cfg)
    assert document["derived"]["active"] is False
    assert any(
        item["code"] == "EVIDENCE_ANCHOR_INVALID"
        for item in document["derived"]["findings"]
    )


def test_agent_cannot_invent_or_relabel_project_binding(tmp_path):
    cfg = _project(tmp_path)
    proposed = _agent_input()
    proposed["facts"][0]["evidence"][0]["binding_id"] = "gate:invented-pointer"
    document = _make(cfg, proposed)
    assert document["derived"]["active"] is False
    with pytest.raises(LogicError, match="project binding_id"):
        broken = _agent_input()
        broken["facts"][0]["evidence"][0] = {
            "result_id": "art:test", "binding_id": "gate:test-completed",
            "pointer": "/unrelated_zero",
        }
        _make(cfg, broken)


def test_every_evidence_backed_fact_argument_requires_project_binding(tmp_path):
    cfg = _project(tmp_path)
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    test_node = next(item for item in graph["nodes"] if item["id"] == "art:test")
    test_node["logic_bindings"][0]["arguments"].pop("release")
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    document = _make(cfg)
    assert document["derived"]["active"] is False
    assert document["derived"]["proof_state"] == "unknown"


def test_assumption_dependent_derivation_is_visible_but_inactive(tmp_path):
    cfg = _project(tmp_path)
    proposed = _agent_input(include_scan=False)
    proposed["facts"].append({
        "atom": {
            "predicate": "gate:scan_completed", "polarity": "positive",
            "arguments": {
                "release": _term("gate:release", "release-1"),
                "critical": _term("ct:integer", "0"),
            },
        },
        "evidence": [],
        "assumption": "The scan is assumed to have no critical findings.",
    })
    document = _make(cfg, proposed, result_ids=["art:test"])
    assert document["derived"]["proof_state"] == "derivable"
    assert document["derived"]["active"] is False
    assert document["derived"]["assumptions"]


def test_irrelevant_subject_result_is_not_drawn_into_refuting_slice(tmp_path):
    cfg = _project(tmp_path, failed=2)
    document = _make(cfg, _agent_input(failed="2"))
    assert document["derived"]["proof_state"] == "refutable"
    assert document["derived"]["used_result_ids"] == ["art:test"]
    assert document["derived"]["active"] is False
    assert any(
        item["code"] == "UNUSED_RESULT_PREMISE"
        for item in document["derived"]["findings"]
    )


def test_append_rechecks_live_grounding_and_configured_assets(tmp_path):
    cfg = _project(tmp_path)
    document = _make(cfg)
    (cfg.root / "results" / "test.json").write_text(json.dumps({
        "release": "release-1", "failed": 9, "failed_conflict": 2,
    }), encoding="utf-8")
    with pytest.raises(LogicError, match="live grounded inputs"):
        append_derivation(cfg, document)


def test_claim_formal_target_pins_rule_pack_identity(tmp_path):
    cfg = _project(tmp_path)
    other_rules = _rules()
    other_rules["id"] = "gate:permissive-rules"
    document = _make(cfg, rules=other_rules)
    assert document["derived"]["claim_bound"] is False
    assert document["derived"]["active"] is False


def test_proof_id_commits_to_project_binding_selection(tmp_path):
    cfg = _project(tmp_path)
    first = _make(cfg)
    test_path = cfg.root / "results" / "test.json"
    data = json.loads(test_path.read_text(encoding="utf-8"))
    data["also_failed"] = 0
    test_path.write_text(json.dumps(data), encoding="utf-8")
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    test_node = next(item for item in graph["nodes"] if item["id"] == "art:test")
    test_node["logic_bindings"].append({
        "id": "gate:test-alternate", "vocabulary_id": "gate:vocabulary",
        "predicate": "gate:test_completed", "polarity": "positive",
        "arguments": {
            "release": {"kind": "json_pointer", "pointer": "/release"},
            "failed": {"kind": "json_pointer", "pointer": "/also_failed"},
        },
    })
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    alternate = _agent_input()
    alternate["facts"][0]["evidence"][0]["binding_id"] = "gate:test-alternate"
    second = _make(cfg, alternate)
    assert first["derived"]["proof_id"] != second["derived"]["proof_id"]


def test_retired_results_and_structurally_invalid_graphs_never_activate(tmp_path):
    cfg = _project(tmp_path)
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    next(item for item in graph["nodes"] if item["id"] == "art:scan")["status"] = "retracted"
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    retired = _make(cfg)
    assert retired["derived"]["active"] is False
    assert any(
        item["code"] == "LOGIC_SUBJECT_INELIGIBLE"
        for item in retired["derived"]["findings"]
    )
    graph["nodes"].append(copy.deepcopy(graph["nodes"][0]))
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    with pytest.raises(LogicError, match="DUPLICATE_ID"):
        _make(cfg)


def test_absolute_artifact_paths_and_invalid_store_shapes_fail_closed(tmp_path):
    cfg = _project(tmp_path)
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    test_node = next(item for item in graph["nodes"] if item["id"] == "art:test")
    test_node["path"] = str((cfg.root / "results" / "test.json").resolve())
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    with pytest.raises(LogicError, match="project-relative"):
        _make(cfg)

    clean = tmp_path / "clean"
    clean.mkdir()
    clean_cfg = _project(clean)
    clean_cfg.derivations_path.write_text("not a directory", encoding="utf-8")
    documents, issues = load_derivations(clean_cfg)
    assert documents == []
    assert [item["code"] for item in issues] == ["DERIVATION_INTEGRITY"]


def test_constraint_types_must_match_exactly_without_implicit_coercion():
    vocabulary = _vocabulary()
    vocabulary["types"].append({"id": "gate:other-release", "base": "ct:symbol"})
    rules = _rules()
    rules["rules"][0]["where"][0] = {
        "op": "eq", "left": _var("release"),
        "right": _const("gate:other-release", "release-1"),
    }
    with pytest.raises(LogicError, match="incompatible types"):
        load_rule_pack(rules, vocabulary)


def test_best_proof_prefers_grounded_composite_path_regardless_of_rule_names(tmp_path):
    cfg = _project(tmp_path)
    vocabulary = _vocabulary()
    vocabulary["predicates"].append({
        "id": "gate:assumed_ready", "kind": "input",
        "arguments": [{"name": "release", "type": "gate:release", "unit": None}],
    })
    proposed = _agent_input()
    proposed["facts"].append({
        "atom": {
            "predicate": "gate:assumed_ready", "polarity": "positive",
            "arguments": {"release": _term("gate:release", "release-1")},
        },
        "evidence": [], "assumption": "A weaker policy assumption.",
    })

    for assume_id, grounded_id in (
            ("gate:00-assume", "gate:zz-ground"),
            ("gate:zz-assume", "gate:00-ground")):
        rules = _rules()
        rules["rules"][0]["id"] = grounded_id
        rules["rules"].append({
            "id": assume_id,
            "when": [{
                "predicate": "gate:assumed_ready", "polarity": "positive",
                "arguments": {"release": _var("release")},
            }],
            "where": [],
            "then": {
                "predicate": "gate:configured_gate_passed", "polarity": "positive",
                "arguments": {"release": _var("release")},
            },
        })
        document = _make(cfg, proposed, vocabulary=vocabulary, rules=rules)
        assert document["derived"]["active"] is True
        assert document["derived"]["assumptions"] == []
        assert document["derived"]["used_result_ids"] == ["art:scan", "art:test"]


def test_refutation_exports_the_opposite_outcome_not_the_target_as_conclusion(tmp_path):
    cfg = _project(tmp_path, failed=2)
    document = _make(
        cfg, _agent_input(failed="2", include_scan=False), result_ids=["art:test"],
    )

    assert document["derived"]["outcome_relation"] == "target_refuted"
    assert document["derived"]["outcome_atoms"][0]["polarity"] == "negative"
    assert document["derived"]["rendered_outcomes"] == [
        "release-1 does not satisfy the configured test and scan gates.",
    ]

    vocabulary = _vocabulary()
    vocabulary["renderers"] = [
        item for item in vocabulary["renderers"] if item["polarity"] == "positive"
    ]
    fallback = _make(
        cfg, _agent_input(failed="2", include_scan=False), result_ids=["art:test"],
        vocabulary=vocabulary,
    )
    assert fallback["derived"]["rendered_outcomes"][0].startswith(
        "negative gate:configured_gate_passed("
    )


def test_unused_agent_fact_does_not_change_canonical_proof_id(tmp_path):
    cfg = _project(tmp_path)
    vocabulary = _vocabulary()
    vocabulary["predicates"].append({
        "id": "gate:noise", "kind": "input",
        "arguments": [{"name": "value", "type": "ct:integer", "unit": None}],
    })
    baseline = _make(cfg, vocabulary=vocabulary)
    noisy_input = _agent_input()
    noisy_input["facts"].append({
        "atom": {
            "predicate": "gate:noise", "polarity": "positive",
            "arguments": {"value": _term("ct:integer", "7")},
        },
        "evidence": [], "assumption": "An irrelevant proposed premise.",
    })
    noisy = _make(cfg, noisy_input, vocabulary=vocabulary)

    assert noisy["derived"]["proof_id"] == baseline["derived"]["proof_id"]
    assert noisy["id"] != baseline["id"]
    assert noisy["derived"]["unused_input_fact_ids"]
    assert any(
        item["code"] == "UNUSED_INPUT_PREMISE"
        for item in noisy["derived"]["findings"]
    )


def test_unknown_certificate_commits_to_every_evaluated_input(tmp_path):
    cfg = _project(tmp_path)
    vocabulary = _vocabulary()
    vocabulary["predicates"].append({
        "id": "gate:noise", "kind": "input",
        "arguments": [{"name": "value", "type": "ct:integer", "unit": None}],
    })
    baseline_input = _agent_input(include_scan=False)
    baseline = _make(
        cfg, baseline_input, result_ids=["art:test"], vocabulary=vocabulary,
    )
    extra_input = _agent_input(include_scan=False)
    extra_input["facts"].append({
        "atom": {
            "predicate": "gate:noise", "polarity": "positive",
            "arguments": {"value": _term("ct:integer", "9")},
        },
        "evidence": [], "assumption": "An evaluated but insufficient premise.",
    })
    extra = _make(cfg, extra_input, result_ids=["art:test"], vocabulary=vocabulary)

    assert baseline["derived"]["proof_state"] == "unknown"
    assert extra["derived"]["proof_state"] == "unknown"
    assert baseline["derived"]["proof_id"] != extra["derived"]["proof_id"]


@pytest.mark.parametrize("status", ["null", "curent"])
def test_non_live_claim_status_never_activates_formal_outcome(tmp_path, status):
    cfg = _project(tmp_path)
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    graph["nodes"][0]["status"] = status
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")

    document = _make(cfg)

    assert document["derived"]["active"] is False
    assert any(
        item["code"] == "LOGIC_SUBJECT_INELIGIBLE"
        for item in document["derived"]["findings"]
    )


def test_result_status_and_type_use_a_conservative_allowlist(tmp_path):
    cfg = _project(tmp_path)
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    scan = next(item for item in graph["nodes"] if item["id"] == "art:scan")
    scan["status"] = "null"
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    assert _make(cfg)["derived"]["active"] is True

    scan["status"] = "curent"
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    assert _make(cfg)["derived"]["active"] is False

    scan["status"] = "current"
    scan["type"] = "code"
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    assert _make(cfg)["derived"]["active"] is False


def test_scoped_provenance_failures_block_and_upstream_versions_stale(tmp_path):
    cfg = _project(tmp_path)
    source_path = cfg.root / "results" / "source.txt"
    source_path.write_text("v1", encoding="utf-8")
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    graph["nodes"].append({
        "id": "data:source", "type": "data", "status": "retracted",
        "path": "results/source.txt",
    })
    graph["edges"].append({
        "from": "data:source", "to": "art:test", "rel": "derives_from",
    })
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")

    blocked = _make(cfg)
    assert blocked["derived"]["active"] is False
    assert any(
        item["code"] == "PROVENANCE_SUBJECT_INVALID"
        for item in blocked["derived"]["findings"]
    )

    graph["nodes"][-1]["status"] = "current"
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    active = _make(cfg)
    assert active["derived"]["active"] is True
    source_path.write_text("v2", encoding="utf-8")
    current = evaluate_derivation(
        cfg, active, vocabulary=_vocabulary(), rule_pack=_rules(),
    )
    assert current["stale"] is True
    assert current["active"] is False


def test_claim_ancestors_are_part_of_the_provenance_certificate(tmp_path):
    cfg = _project(tmp_path)
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    graph["nodes"].append({
        "id": "data:claim-source", "type": "data", "status": "current",
        "path": "results/missing-claim-source.txt",
    })
    graph["edges"].append({
        "from": "data:claim-source", "to": "claim:gate", "rel": "derives_from",
    })
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")

    document = _make(cfg)

    assert document["derived"]["active"] is False
    provenance = document["mechanical_snapshot"]["provenance_check"]
    assert "data:claim-source" in provenance["scope_node_ids"]
    assert any(item["code"] == "MISSING_FILE" for item in provenance["problems"])


def test_complete_binding_and_artifact_are_parsed_once_per_result(tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    read_counts = {}
    parse_counts = {}
    original_read = logic_module._safe_regular_file_bytes
    original_parse = logic_module._json_with_decimal_strings

    def counted_read(path, label, limit):
        name = str(path)
        read_counts[name] = read_counts.get(name, 0) + 1
        return original_read(path, label, limit)

    def counted_parse(data, source):
        parse_counts[source] = parse_counts.get(source, 0) + 1
        return original_parse(data, source)

    monkeypatch.setattr(logic_module, "_safe_regular_file_bytes", counted_read)
    monkeypatch.setattr(logic_module, "_json_with_decimal_strings", counted_parse)
    _make(cfg, _agent_input(conflict=True))

    assert read_counts[str(cfg.root / "results" / "test.json")] == 1
    assert read_counts[str(cfg.root / "results" / "scan.json")] == 1
    assert parse_counts["results/test.json"] == 1
    assert parse_counts["results/scan.json"] == 1


def test_grounding_accepts_json_scientific_notation_for_decimal(tmp_path):
    cfg = _project(tmp_path)
    vocabulary = _vocabulary()
    vocabulary["predicates"][0]["arguments"][1]["type"] = "ct:decimal"
    rules = _rules()
    for rule in rules["rules"]:
        for condition in rule["where"]:
            if condition["left"] == _var("failed"):
                condition["right"] = _const("ct:decimal", "0")
    proposal = _agent_input()
    proposal["facts"][0]["atom"]["arguments"]["failed"] = _term("ct:decimal", "0")
    (cfg.root / "results" / "test.json").write_text(
        '{"release":"release-1","failed":0e0,"failed_conflict":2}',
        encoding="utf-8",
    )

    document = _make(cfg, proposal, vocabulary=vocabulary, rules=rules)

    assert document["derived"]["active"] is True


def test_invalid_json_pointer_and_unicode_surrogate_fail_closed(tmp_path):
    cfg = _project(tmp_path)
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    test_node = next(item for item in graph["nodes"] if item["id"] == "art:test")
    test_node["logic_bindings"][0]["arguments"]["failed"]["pointer"] = "/~2"
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    assert _make(cfg)["derived"]["active"] is False

    bad = _agent_input()
    bad["note"] = "\ud800"
    with pytest.raises(LogicError, match="Unicode scalar"):
        _make(cfg, bad)

    test_node["logic_bindings"][0]["arguments"]["failed"]["pointer"] = "/failed"
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    valid = _agent_input()
    valid["note"] = "Valid non-BMP text 🧠"
    assert _make(cfg, valid)["derived"]["active"] is True


def test_numeric_and_symbolic_work_limits_fail_before_unbounded_work(tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    too_long = _agent_input()
    too_long["facts"][0]["atom"]["arguments"]["failed"]["value"] = "1" * 5000
    with pytest.raises(LogicError, match="integer values"):
        _make(cfg, too_long)

    monkeypatch.setattr(logic_module, "MAX_JOIN_WORK", 1)
    with pytest.raises(LogicError, match="join work limit"):
        _make(cfg)


def test_forged_anchor_identity_and_provenance_version_are_rejected(tmp_path):
    cfg = _project(tmp_path)
    document = _make(cfg)
    forged = copy.deepcopy(document)
    check = forged["mechanical_snapshot"]["anchor_checks"][0]
    check["result_id"] = "art:test" if check["result_id"] != "art:test" else "art:scan"
    with pytest.raises(LogicError, match="anchor check"):
        validate_derivation_document(forged)

    forged = copy.deepcopy(document)
    claim_version = next(
        item for item in forged["mechanical_snapshot"]["provenance_check"]["node_versions"]
        if item["node_id"] == "claim:gate"
    )
    claim_version["node_sha256"] = "f" * 64
    claim_version["node_version_id"] = "node:sha256:" + "f" * 64
    with pytest.raises(LogicError, match="snapshots disagree"):
        validate_derivation_document(forged)


def test_binding_order_is_canonical_and_aggregate_grounding_budget_is_enforced(
        tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    first = _make(cfg)
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    test_node = next(item for item in graph["nodes"] if item["id"] == "art:test")
    test_node["logic_bindings"].reverse()
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    reordered = _make(cfg)
    assert reordered["id"] == first["id"]

    monkeypatch.setattr(logic_module, "MAX_ARTIFACT_TOTAL_BYTES", 1)
    with pytest.raises(LogicError, match="aggregate 256 MiB"):
        _make(cfg)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value["predicates"][0].update(kind=["input"]), "invalid kind"),
        (lambda value: value["predicates"][0].update(kind={"input": True}), "invalid kind"),
        (lambda value: value["renderers"][0].update(polarity=["positive"]), "polarity"),
        (lambda value: value["predicates"][0].update(id=["gate:test"]), "predicate.id"),
    ],
)
def test_vocabulary_wrong_shape_scalars_fail_as_logic_errors(mutate, message):
    vocabulary = _vocabulary()
    mutate(vocabulary)
    with pytest.raises(LogicError, match=message):
        load_vocabulary(vocabulary)


def test_rule_constraint_units_must_be_declared_and_scalar():
    rules = _rules()
    constant = rules["rules"][0]["where"][0]["right"]["const"]
    constant["unit"] = "gate:not-declared"
    with pytest.raises(LogicError, match="unknown unit"):
        load_rule_pack(rules, _vocabulary())

    constant["unit"] = []
    with pytest.raises(LogicError, match="unknown unit"):
        load_rule_pack(rules, _vocabulary())


def test_text_line_boolean_bounds_and_boolean_snapshot_sizes_are_rejected(tmp_path):
    cfg = _project(tmp_path)
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    binding = graph["nodes"][1]["logic_bindings"][0]
    binding["arguments"]["release"] = {
        "kind": "text_lines", "start_line": True, "end_line": 1,
        "prefix": "", "suffix": "",
    }
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    with pytest.raises(LogicError, match="bounds"):
        create_derivation_from_bindings(
            cfg, "claim:gate",
            [{"result_id": "art:test", "binding_id": "gate:test-completed"}],
            actor="agent:auto", provenance={"agent": "agent:auto"},
        )

    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    cfg = _project(snapshot_root)
    document = _make(cfg)
    document["mechanical_snapshot"]["results"][0]["artifact"]["size"] = True
    with pytest.raises(LogicError, match="stable artifact snapshot"):
        validate_derivation_document(document)


def test_direct_append_rejects_a_preexisting_corrupt_ledger(tmp_path):
    cfg = _project(tmp_path)
    document = _make(cfg)
    cfg.derivations_path.mkdir()
    (cfg.derivations_path / "bad.json").write_text("{}", encoding="utf-8")

    with pytest.raises(LogicError, match="store integrity"):
        append_derivation(cfg, document)


def test_append_rejects_documents_larger_than_the_reader_limit(tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    document = _make(cfg)
    monkeypatch.setattr(logic_module, "MAX_DERIVATION_DOCUMENT_BYTES", 100)

    with pytest.raises(LogicError, match="storage size limit"):
        append_derivation(cfg, document)
    assert not cfg.derivations_path.exists()


def test_append_rejects_a_derivation_store_file_count_overflow(tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    first = _make(cfg)
    append_derivation(cfg, first)
    second = create_derivation(
        cfg, "claim:gate", ["art:scan", "art:test"], _agent_input(),
        vocabulary=_vocabulary(), rule_pack=_rules(), actor="agent:second",
        recorded_at=FIXED_TIME,
    )
    monkeypatch.setattr(logic_module, "MAX_DERIVATION_FILES", 1)

    with pytest.raises(LogicError, match="file-count limit"):
        append_derivation(cfg, second)
    loaded, issues = load_derivations(cfg)
    assert issues == []
    assert loaded == [first]

    second_path = cfg.derivations_path / f"{second['id'].rsplit(':', 1)[1]}.json"
    second_path.write_text(
        json.dumps(second, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    loaded, issues = load_derivations(cfg)
    assert loaded == []
    assert issues[0]["code"] == "DERIVATION_INTEGRITY"
    assert "file-count limit" in issues[0]["detail"]


def test_append_and_load_fail_closed_on_derivation_store_byte_overflow(
        tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    document = _make(cfg)
    payload_size = len(
        (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        .encode("utf-8")
    )
    monkeypatch.setattr(logic_module, "MAX_DERIVATION_STORE_BYTES", payload_size - 1)

    with pytest.raises(LogicError, match="aggregate byte limit"):
        append_derivation(cfg, document)
    assert not list(cfg.derivations_path.glob("*.json"))

    cfg.derivations_path.mkdir(exist_ok=True)
    destination = cfg.derivations_path / f"{document['id'].rsplit(':', 1)[1]}.json"
    destination.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    loaded, issues = load_derivations(cfg)
    assert loaded == []
    assert issues[0]["code"] == "DERIVATION_INTEGRITY"
    assert "aggregate byte limit" in issues[0]["detail"]


def test_load_recounts_stable_bytes_when_a_store_file_grows_after_preflight(
        tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    document = _make(cfg)
    cfg.derivations_path.mkdir()
    destination = cfg.derivations_path / f"{document['id'].rsplit(':', 1)[1]}.json"
    payload = (
        json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    destination.write_bytes(payload)
    monkeypatch.setattr(
        logic_module, "MAX_DERIVATION_STORE_BYTES", len(payload) + 16,
    )
    original = logic_module._safe_regular_file_bytes
    grew = False

    def grow_after_directory_preflight(path, label, limit):
        nonlocal grew
        if label == "derivation document" and not grew:
            grew = True
            with path.open("ab") as handle:
                handle.write(b" " * 64)
        return original(path, label, limit)

    monkeypatch.setattr(
        logic_module, "_safe_regular_file_bytes", grow_after_directory_preflight,
    )
    loaded, issues = load_derivations(cfg)

    assert loaded == []
    assert issues[0]["code"] == "DERIVATION_INTEGRITY"
    assert "aggregate byte limit" in issues[0]["detail"]


def test_append_rechecks_grounded_inputs_after_acquiring_the_store_lock(
        tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    document = _make(cfg)

    @contextlib.contextmanager
    def mutate_before_lock_yields(_root):
        (cfg.root / "results" / "test.json").write_text(
            json.dumps({"release": "release-1", "failed": 1, "failed_conflict": 2}),
            encoding="utf-8",
        )
        yield

    monkeypatch.setattr(logic_module, "_store_lock", mutate_before_lock_yields)
    with pytest.raises(LogicError, match="changed before storage"):
        append_derivation(cfg, document)
    assert not list(cfg.derivations_path.glob("*.json"))


def test_append_reloads_configured_policy_assets_under_the_store_lock(
        tmp_path, monkeypatch):
    cfg = _project(tmp_path)
    document = _make(cfg)
    original = logic_module._configured_assets
    calls = 0

    def mutate_policy_on_locked_load(config, candidate):
        nonlocal calls
        calls += 1
        if calls == 2:
            rules_path = config.logic_rule_pack_paths[0]
            rules = json.loads(rules_path.read_text(encoding="utf-8"))
            rules["version"] = "2.0.0"
            rules_path.write_text(json.dumps(rules), encoding="utf-8")
        return original(config, candidate)

    monkeypatch.setattr(
        logic_module, "_configured_assets", mutate_policy_on_locked_load,
    )
    with pytest.raises(LogicError, match="snapshots do not match"):
        append_derivation(cfg, document)
    assert not list(cfg.derivations_path.glob("*.json"))


def test_binding_selection_wraps_invalid_utf8_as_logic_error(tmp_path):
    cfg = _project(tmp_path)
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    test_node = next(item for item in graph["nodes"] if item["id"] == "art:test")
    test_node["logic_bindings"][0]["arguments"] = {
        "release": {
            "kind": "text_lines", "start_line": 1, "end_line": 1,
            "prefix": "", "suffix": "\n",
        },
        "failed": {
            "kind": "text_lines", "start_line": 1, "end_line": 1,
            "prefix": "", "suffix": "\n",
        },
    }
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    (cfg.root / "results" / "test.json").write_bytes(b"\xff\n")

    with pytest.raises(LogicError, match="valid UTF-8"):
        create_derivation_from_bindings(
            cfg, "claim:gate",
            [{"result_id": "art:test", "binding_id": "gate:test-completed"}],
            actor="agent:auto", provenance={"agent": "agent:auto"},
        )


@pytest.mark.parametrize(
    ("location", "bad_value"),
    [
        ("scope", []),
        ("scope", {}),
        ("scope", True),
        ("vocabulary", []),
        ("result", []),
        ("node_version", []),
        ("argument_check", []),
    ],
)
def test_malformed_nested_snapshot_containers_fail_as_logic_errors(
        tmp_path, location, bad_value):
    cfg = _project(tmp_path)
    document = _make(cfg)
    snapshot = document["mechanical_snapshot"]
    if location == "scope":
        snapshot["provenance_check"]["scope_node_ids"][0] = bad_value
    elif location == "vocabulary":
        snapshot["vocabulary"] = bad_value
    elif location == "result":
        snapshot["results"][0] = bad_value
    elif location == "node_version":
        snapshot["provenance_check"]["node_versions"][0] = bad_value
    else:
        snapshot["anchor_checks"][0]["argument_checks"][0] = bad_value

    with pytest.raises(LogicError):
        validate_derivation_document(document)
