"""Report integration tests for portable symbolic derivations."""
import json

from claimtrace.config import Config
from claimtrace.logic import (
    append_derivation,
    create_derivation,
    load_rule_pack,
    load_vocabulary,
)
from claimtrace.report import build_report


FIXED_TIME = "2026-07-14T02:00:00.000Z"


def _term(type_id, value):
    return {"type": type_id, "value": value, "unit": None}


def _target():
    return {
        "predicate": "gate:accepted",
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
                "id": "gate:release_observed",
                "kind": "input",
                "arguments": [
                    {"name": "release", "type": "gate:release", "unit": None},
                ],
            },
            {
                "id": "gate:pass_observed",
                "kind": "input",
                "arguments": [
                    {"name": "passed", "type": "ct:boolean", "unit": None},
                ],
            },
            {
                "id": "gate:accepted",
                "kind": "derived",
                "arguments": [
                    {"name": "release", "type": "gate:release", "unit": None},
                ],
            },
        ],
        "renderers": [{
            "id": "gate:accepted-en",
            "predicate": "gate:accepted",
            "polarity": "positive",
            "language": "en",
            "template": "{release} satisfies the configured gate.",
        }],
    }


def _rules():
    return {
        "schema_version": "claimtrace.symbolic-rules/1",
        "id": "gate:rules",
        "version": "1.0.0",
        "vocabulary_id": "gate:vocabulary",
        "rules": [{
            "id": "gate:accept",
            "when": [
                {
                    "predicate": "gate:release_observed",
                    "polarity": "positive",
                    "arguments": {"release": {"var": "release"}},
                },
                {
                    "predicate": "gate:pass_observed",
                    "polarity": "positive",
                    "arguments": {
                        "passed": {"const": _term("ct:boolean", True)},
                    },
                },
            ],
            "where": [],
            "then": {
                "predicate": "gate:accepted",
                "polarity": "positive",
                "arguments": {"release": {"var": "release"}},
            },
        }],
    }


def _agent_input():
    return {
        "target": _target(),
        "facts": [
            {
                "atom": {
                    "predicate": "gate:release_observed",
                    "polarity": "positive",
                    "arguments": {
                        "release": _term("gate:release", "release-1"),
                    },
                },
                "evidence": [{
                    "result_id": "result:a", "binding_id": "gate:a-release",
                }],
                "assumption": None,
            },
            {
                "atom": {
                    "predicate": "gate:pass_observed",
                    "polarity": "positive",
                    "arguments": {"passed": _term("ct:boolean", True)},
                },
                "evidence": [{
                    "result_id": "result:b", "binding_id": "gate:b-passed",
                }],
                "assumption": None,
            },
        ],
        "note": "A deliberately domain-neutral policy example.",
        "provenance": {"agent": "agent:test"},
    }


def _project(tmp_path, *, require_derivations=False):
    trace = tmp_path / "claimtrace"
    logic_dir = trace / "logic"
    logic_dir.mkdir(parents=True)
    (tmp_path / "a.json").write_text(
        json.dumps({"release": "release-1"}), encoding="utf-8",
    )
    (tmp_path / "b.json").write_text(
        json.dumps({"passed": True}), encoding="utf-8",
    )
    vocabulary_path = logic_dir / "vocabulary.json"
    rules_path = logic_dir / "rules.json"
    vocabulary_path.write_text(json.dumps(_vocabulary()), encoding="utf-8")
    rules_path.write_text(json.dumps(_rules()), encoding="utf-8")
    graph = {
        "schema_version": "1.0",
        "concepts": {},
        "nodes": [
            {
                "id": "claim:gate", "type": "claim", "status": "current",
                "value": "release-1 satisfies the configured gate",
                "logic": {
                    "vocabulary_id": "gate:vocabulary", "rule_pack_id": "gate:rules",
                    "target": _target(),
                },
            },
            {
                "id": "claim:plain", "type": "claim", "status": "current",
                "value": "This prose-only claim has no formal target.",
            },
            {
                "id": "result:a", "type": "artifact", "status": "current",
                "path": "a.json",
                "logic_bindings": [{
                    "id": "gate:a-release", "vocabulary_id": "gate:vocabulary",
                    "predicate": "gate:release_observed", "polarity": "positive",
                    "arguments": {
                        "release": {"kind": "json_pointer", "pointer": "/release"},
                    },
                }],
            },
            {
                "id": "result:b", "type": "artifact", "status": "current",
                "path": "b.json",
                "logic_bindings": [{
                    "id": "gate:b-passed", "vocabulary_id": "gate:vocabulary",
                    "predicate": "gate:pass_observed", "polarity": "positive",
                    "arguments": {
                        "passed": {"kind": "json_pointer", "pointer": "/passed"},
                    },
                }],
            },
        ],
        "edges": [],
    }
    (trace / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    config_path = tmp_path / "claimtrace.config.json"
    config_path.write_text(json.dumps({
        "root": ".",
        "graph": "claimtrace/graph.json",
        "logic": {
            "derivations": "claimtrace/derivations",
            "vocabularies": ["claimtrace/logic/vocabulary.json"],
            "rule_packs": ["claimtrace/logic/rules.json"],
            "require_derivations": require_derivations,
        },
    }), encoding="utf-8")
    return Config(config_path), vocabulary_path, rules_path


def _append_proof(cfg, vocabulary_path, rules_path):
    vocabulary = load_vocabulary(vocabulary_path)
    rules = load_rule_pack(rules_path, vocabulary)
    document = create_derivation(
        cfg,
        "claim:gate",
        ["result:a", "result:b"],
        _agent_input(),
        vocabulary=vocabulary,
        rule_pack=rules,
        actor="agent:test",
        recorded_at=FIXED_TIME,
    )
    append_derivation(cfg, document)
    return document


def test_report_projects_one_composite_active_proof_for_all_results(tmp_path):
    cfg, vocabulary_path, rules_path = _project(tmp_path, require_derivations=True)
    document = _append_proof(cfg, vocabulary_path, rules_path)

    report = build_report(cfg, strict=True)

    assert report["report_schema_version"] == "1.2"
    assert report["scope"]["symbolic_logic"] == (
        "conditional_derivability_under_project_rules_not_truth"
    )
    assert report["derivations"]["integrity"] == "ok"
    assert report["derivations"]["active_proofs"] == [{
        "derivation_id": document["id"],
        "derivation_ids": [document["id"]],
        "proof_id": document["derived"]["proof_id"],
        "claim_id": "claim:gate",
        "result_ids": ["result:a", "result:b"],
        "vocabulary_id": "gate:vocabulary",
        "rule_pack_id": "gate:rules",
        "proof_state": "derivable",
        "target": _target(),
        "rendered_target": "release-1 satisfies the configured gate.",
        "outcome_relation": "target_derived",
        "outcome_atoms": [_target()],
        "rendered_outcomes": ["release-1 satisfies the configured gate."],
        "assumptions": [],
        "claim_level_active": True,
    }]
    item = report["derivations"]["items"][0]
    assert item["stored_derived"]["active"] is True
    assert item["effective"]["active"] is True
    assert item["effective"]["effective_state"] == "active"
    assert not any(
        finding["code"] == "MISSING_CLAIM_DERIVATION"
        for finding in report["findings"]
    )


def test_corrupt_derivation_store_globally_suppresses_activation_but_keeps_history(tmp_path):
    cfg, vocabulary_path, rules_path = _project(tmp_path)
    _append_proof(cfg, vocabulary_path, rules_path)
    (cfg.derivations_path / ("f" * 64 + ".json")).write_text(
        "{broken", encoding="utf-8",
    )

    report = build_report(cfg)

    assert report["derivations"]["integrity"] == "error"
    assert report["derivations"]["active_proofs"] == []
    assert len(report["derivations"]["items"]) == 1
    item = report["derivations"]["items"][0]
    assert item["stored_derived"]["active"] is True
    assert item["effective"]["active"] is False
    assert item["effective"]["effective_state"] == "integrity_error"
    finding = next(
        item for item in report["findings"] if item["code"] == "DERIVATION_INTEGRITY"
    )
    assert finding["severity"] == "error" and finding["blocking"] is True


def test_corrupt_configured_rule_pack_globally_suppresses_activation(tmp_path):
    cfg, vocabulary_path, rules_path = _project(tmp_path)
    _append_proof(cfg, vocabulary_path, rules_path)
    rules_path.write_text("{broken", encoding="utf-8")

    report = build_report(cfg)

    assert report["derivations"]["integrity"] == "error"
    assert report["derivations"]["active_proofs"] == []
    item = report["derivations"]["items"][0]
    assert item["stored_derived"]["active"] is True
    assert item["effective"]["effective_state"] == "integrity_error"
    assert any(
        item["code"] == "LOGIC_ASSET_INTEGRITY" and item["blocking"]
        for item in report["findings"]
    )


def test_report_rejects_undeclared_formal_target_predicate_before_derivation(tmp_path):
    cfg, _vocabulary_path, _rules_path = _project(tmp_path)
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    claim = next(item for item in graph["nodes"] if item["id"] == "claim:gate")
    claim["logic"]["target"]["predicate"] = "gate:not_declared"
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")

    report = build_report(cfg)

    findings = [
        item for item in report["findings"]
        if item["code"] == "LOGIC_DECLARATION_INVALID"
    ]
    assert [(item["node_id"], item["severity"]) for item in findings] == [
        ("claim:gate", "error"),
    ]
    assert "unknown predicate" in findings[0]["detail"]
    assert findings[0]["blocking"] is True
    assert report["derivations"]["integrity"] == "error"
    assert report["ok"] is False


def test_report_rejects_malformed_and_incompatible_result_bindings(tmp_path):
    cfg, _vocabulary_path, _rules_path = _project(tmp_path)
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    result_a = next(item for item in graph["nodes"] if item["id"] == "result:a")
    result_b = next(item for item in graph["nodes"] if item["id"] == "result:b")
    result_a["logic_bindings"][0]["arguments"] = {}
    result_b["logic_bindings"][0]["predicate"] = "gate:accepted"
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")

    report = build_report(cfg)

    findings = [
        item for item in report["findings"]
        if item["code"] == "LOGIC_DECLARATION_INVALID"
    ]
    assert [item["node_id"] for item in findings] == ["result:a", "result:b"]
    assert "map every predicate argument exactly once" in findings[0]["detail"]
    assert "must reference an input predicate" in findings[1]["detail"]
    assert all(item["severity"] == "error" and item["blocking"] for item in findings)
    assert report["derivations"]["integrity"] == "error"
    assert report["ok"] is False


def test_report_rejects_binding_that_cannot_ground_the_current_artifact(tmp_path):
    cfg, _vocabulary_path, _rules_path = _project(tmp_path)
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    result_a = next(item for item in graph["nodes"] if item["id"] == "result:a")
    result_a["logic_bindings"][0]["arguments"]["release"]["pointer"] = "/missing"
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")

    report = build_report(cfg)

    findings = [
        item for item in report["findings"]
        if item["code"] == "LOGIC_DECLARATION_INVALID"
    ]
    assert [item["node_id"] for item in findings] == ["result:a"]
    assert "JSON Pointer key not found: missing" in findings[0]["detail"]
    assert findings[0]["blocking"] is True
    assert report["derivations"]["integrity"] == "error"
    assert report["ok"] is False


def test_one_result_can_expose_bindings_for_multiple_vocabularies(tmp_path):
    cfg, vocabulary_path, rules_path = _project(tmp_path)
    auxiliary = {
        "schema_version": "claimtrace.symbolic-vocabulary/1",
        "id": "aux:vocabulary",
        "version": "1.0.0",
        "types": [],
        "units": [],
        "predicates": [{
            "id": "aux:pass_observed", "kind": "input",
            "arguments": [{"name": "passed", "type": "ct:boolean", "unit": None}],
        }],
        "renderers": [],
    }
    auxiliary_path = cfg.root / "claimtrace" / "logic" / "auxiliary.json"
    auxiliary_path.write_text(json.dumps(auxiliary), encoding="utf-8")
    config = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    config["logic"]["vocabularies"].append("claimtrace/logic/auxiliary.json")
    cfg.config_path.write_text(json.dumps(config), encoding="utf-8")

    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    result_b = next(item for item in graph["nodes"] if item["id"] == "result:b")
    result_b["logic_bindings"].append({
        "id": "aux:b-passed", "vocabulary_id": "aux:vocabulary",
        "predicate": "aux:pass_observed", "polarity": "positive",
        "arguments": {
            "passed": {"kind": "json_pointer", "pointer": "/passed"},
        },
    })
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    cfg = Config(cfg.config_path)

    document = _append_proof(cfg, vocabulary_path, rules_path)
    report = build_report(cfg)

    assert document["derived"]["active"] is True
    assert not any(
        item["code"] == "LOGIC_DECLARATION_INVALID"
        for item in report["findings"]
    )
    assert report["derivations"]["integrity"] == "ok"


def test_require_derivations_is_claim_level_and_only_blocks_in_strict_mode(tmp_path):
    cfg, _vocabulary_path, _rules_path = _project(tmp_path, require_derivations=True)

    normal = build_report(cfg, strict=False)
    strict = build_report(cfg, strict=True)

    normal_missing = [
        item for item in normal["findings"] if item["code"] == "MISSING_CLAIM_DERIVATION"
    ]
    strict_missing = [
        item for item in strict["findings"] if item["code"] == "MISSING_CLAIM_DERIVATION"
    ]
    assert [item["node_id"] for item in normal_missing] == ["claim:gate"]
    assert normal_missing[0]["blocking"] is False
    assert strict_missing[0]["blocking"] is True
    assert normal["ok"] is True
    assert strict["ok"] is False


def test_refutable_target_does_not_satisfy_require_derivations(tmp_path):
    cfg, vocabulary_path, rules_path = _project(tmp_path, require_derivations=True)
    rule_pack = _rules()
    rule_pack["rules"].append({
        "id": "gate:reject",
        "when": [
            {
                "predicate": "gate:release_observed",
                "polarity": "positive",
                "arguments": {"release": {"var": "release"}},
            },
            {
                "predicate": "gate:pass_observed",
                "polarity": "positive",
                "arguments": {
                    "passed": {"const": _term("ct:boolean", False)},
                },
            },
        ],
        "where": [],
        "then": {
            "predicate": "gate:accepted",
            "polarity": "negative",
            "arguments": {"release": {"var": "release"}},
        },
    })
    rules_path.write_text(json.dumps(rule_pack), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps({"passed": False}), encoding="utf-8")
    proposal = _agent_input()
    proposal["facts"][1]["atom"]["arguments"]["passed"] = _term(
        "ct:boolean", False,
    )
    vocabulary = load_vocabulary(vocabulary_path)
    rules = load_rule_pack(rules_path, vocabulary)
    document = create_derivation(
        cfg,
        "claim:gate",
        ["result:a", "result:b"],
        proposal,
        vocabulary=vocabulary,
        rule_pack=rules,
        actor="agent:test",
        recorded_at=FIXED_TIME,
    )
    append_derivation(cfg, document)

    report = build_report(cfg, strict=True)

    assert document["derived"]["proof_state"] == "refutable"
    assert document["derived"]["active"] is True
    assert [item["proof_state"] for item in report["derivations"]["active_proofs"]] == [
        "refutable",
    ]
    missing = [
        item for item in report["findings"] if item["code"] == "MISSING_CLAIM_DERIVATION"
    ]
    assert [item["node_id"] for item in missing] == ["claim:gate"]
    assert missing[0]["blocking"] is True
    assert report["ok"] is False


def test_stale_derivation_is_inactive_without_marking_store_integrity_bad(tmp_path):
    cfg, vocabulary_path, rules_path = _project(tmp_path)
    _append_proof(cfg, vocabulary_path, rules_path)
    (tmp_path / "a.json").write_text(
        json.dumps({"release": "release-2"}), encoding="utf-8",
    )

    report = build_report(cfg)

    assert report["derivations"]["integrity"] == "ok"
    assert report["derivations"]["active_proofs"] == []
    item = report["derivations"]["items"][0]
    assert item["effective"]["active"] is False
    assert item["effective"]["effective_state"] == "inactive"
    assert any(item["code"] == "DERIVATION_STALE" for item in report["findings"])


def test_report_groups_duplicate_submissions_by_canonical_proof_id(tmp_path):
    cfg, vocabulary_path, rules_path = _project(tmp_path)
    first = _append_proof(cfg, vocabulary_path, rules_path)
    vocabulary = load_vocabulary(vocabulary_path)
    rules = load_rule_pack(rules_path, vocabulary)
    proposal = _agent_input()
    proposal["note"] = "Same proof, independently submitted."
    second = create_derivation(
        cfg, "claim:gate", ["result:a", "result:b"], proposal,
        vocabulary=vocabulary, rule_pack=rules, actor="agent:second",
        recorded_at="2026-07-14T02:00:01.000Z",
    )
    append_derivation(cfg, second)

    report = build_report(cfg)

    assert first["derived"]["proof_id"] == second["derived"]["proof_id"]
    assert len(report["derivations"]["items"]) == 2
    assert len(report["derivations"]["active_proofs"]) == 1
    assert report["derivations"]["active_proofs"][0]["derivation_ids"] == sorted([
        first["id"], second["id"],
    ])


def test_opposite_active_proofs_form_one_claim_level_conflict(tmp_path):
    cfg, vocabulary_path, rules_path = _project(tmp_path, require_derivations=True)
    rule_pack = _rules()
    rule_pack["rules"].append({
        "id": "gate:reject",
        "when": [
            {
                "predicate": "gate:release_observed", "polarity": "positive",
                "arguments": {"release": {"var": "release"}},
            },
            {
                "predicate": "gate:pass_observed", "polarity": "positive",
                "arguments": {
                    "passed": {"const": _term("ct:boolean", False)},
                },
            },
        ],
        "where": [],
        "then": {
            "predicate": "gate:accepted", "polarity": "negative",
            "arguments": {"release": {"var": "release"}},
        },
    })
    rules_path.write_text(json.dumps(rule_pack), encoding="utf-8")
    (tmp_path / "c.json").write_text(json.dumps({"passed": False}), encoding="utf-8")
    graph = json.loads(cfg.graph_path.read_text(encoding="utf-8"))
    graph["nodes"].append({
        "id": "result:c", "type": "artifact", "status": "current", "path": "c.json",
        "logic_bindings": [{
            "id": "gate:c-pass", "vocabulary_id": "gate:vocabulary",
            "predicate": "gate:pass_observed", "polarity": "positive",
            "arguments": {"passed": {"kind": "json_pointer", "pointer": "/passed"}},
        }],
    })
    cfg.graph_path.write_text(json.dumps(graph), encoding="utf-8")
    vocabulary = load_vocabulary(vocabulary_path)
    rules = load_rule_pack(rules_path, vocabulary)
    positive = create_derivation(
        cfg, "claim:gate", ["result:a", "result:b"], _agent_input(),
        vocabulary=vocabulary, rule_pack=rules, actor="agent:positive",
        recorded_at=FIXED_TIME,
    )
    append_derivation(cfg, positive)
    negative_input = _agent_input()
    negative_input["facts"][1]["atom"]["arguments"]["passed"] = _term(
        "ct:boolean", False,
    )
    negative_input["facts"][1]["evidence"][0] = {
        "result_id": "result:c", "binding_id": "gate:c-pass",
    }
    negative = create_derivation(
        cfg, "claim:gate", ["result:a", "result:c"], negative_input,
        vocabulary=vocabulary, rule_pack=rules, actor="agent:negative",
        recorded_at="2026-07-14T02:00:01.000Z",
    )
    append_derivation(cfg, negative)

    report = build_report(cfg, strict=True)

    assert {item["proof_state"] for item in report["derivations"]["active_proofs"]} == {
        "derivable", "refutable",
    }
    assert not any(
        item["claim_level_active"] for item in report["derivations"]["active_proofs"]
    )
    assert any(
        item["code"] == "SYMBOLIC_CROSS_DERIVATION_CONFLICT"
        for item in report["findings"]
    )
    assert any(item["code"] == "MISSING_CLAIM_DERIVATION" for item in report["findings"])
    assert report["ok"] is False
