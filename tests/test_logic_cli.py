"""Focused command-line tests for project-owned symbolic logic assets."""
import json

from claimtrace.cli import main
from claimtrace.config import Config


def _term(type_id, value):
    return {"type": type_id, "value": value, "unit": None}


def _target():
    return {
        "predicate": "gate:ready",
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
                "id": "gate:check",
                "kind": "input",
                "arguments": [
                    {"name": "release", "type": "gate:release", "unit": None},
                    {"name": "ok", "type": "ct:boolean", "unit": None},
                ],
            },
            {
                "id": "gate:ready",
                "kind": "derived",
                "arguments": [
                    {"name": "release", "type": "gate:release", "unit": None},
                ],
            },
        ],
        "renderers": [{
            "id": "gate:ready-en",
            "predicate": "gate:ready",
            "polarity": "positive",
            "language": "en",
            "template": "{release} satisfies the configured gate.",
        }],
    }


def _rules():
    negative = {
        "predicate": "gate:ready",
        "polarity": "negative",
        "arguments": {"release": {"var": "release"}},
    }
    return {
        "schema_version": "claimtrace.symbolic-rules/1",
        "id": "gate:rules",
        "version": "1.0.0",
        "vocabulary_id": "gate:vocabulary",
        "rules": [{
            "id": "gate:ready-if-check-ok",
            "when": [{
                "predicate": "gate:check",
                "polarity": "positive",
                "arguments": {"release": {"var": "release"}, "ok": {"var": "ok"}},
            }],
            "where": [{
                "op": "eq",
                "left": {"var": "ok"},
                "right": {"const": _term("ct:boolean", True)},
            }],
            "then": {
                "predicate": "gate:ready",
                "polarity": "positive",
                "arguments": {"release": {"var": "release"}},
            },
        }, {
            "id": "gate:not-ready-if-check-fails",
            "when": [{
                "predicate": "gate:check",
                "polarity": "positive",
                "arguments": {"release": {"var": "release"}, "ok": {"var": "ok"}},
            }],
            "where": [{
                "op": "eq",
                "left": {"var": "ok"},
                "right": {"const": _term("ct:boolean", False)},
            }],
            "then": negative,
        }],
    }


def _proposal():
    return {
        "claim_id": "claim:ready",
        "result_ids": ["art:check"],
        "vocabulary_id": "gate:vocabulary",
        "rule_pack_id": "gate:rules",
        "agent_input": {
            "target": _target(),
            "facts": [{
                "atom": {
                    "predicate": "gate:check",
                    "polarity": "positive",
                    "arguments": {
                        "release": _term("gate:release", "release-1"),
                        "ok": _term("ct:boolean", True),
                    },
                },
                "evidence": [
                    {"result_id": "art:check", "binding_id": "gate:check-complete"},
                ],
                "assumption": None,
            }],
            "note": "A deliberately domain-neutral release gate.",
            "provenance": {"agent": "agent:test"},
        },
    }


def _selection_proposal():
    return {
        "schema_version": "claimtrace.symbolic-selection/1",
        "claim_id": "claim:ready",
        "bindings": [
            {"result_id": "art:check", "binding_id": "gate:check-complete"},
        ],
        "note": "Select a reviewed project binding; Claimtrace materializes the fact.",
        "provenance": {"agent": "agent:test"},
    }


def _project(tmp_path):
    (tmp_path / "claimtrace" / "logic").mkdir(parents=True)
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "check.json").write_text(
        json.dumps({"release": "release-1", "ok": True, "not_ok": False}),
        encoding="utf-8",
    )
    (tmp_path / "claimtrace" / "logic" / "vocabulary.json").write_text(
        json.dumps(_vocabulary()), encoding="utf-8",
    )
    (tmp_path / "claimtrace" / "logic" / "rules.json").write_text(
        json.dumps(_rules()), encoding="utf-8",
    )
    (tmp_path / "claimtrace" / "graph.json").write_text(json.dumps({
        "schema_version": "1.0",
        "concepts": {},
        "nodes": [
            {
                "id": "claim:ready",
                "type": "claim",
                "status": "current",
                "value": "release-1 satisfies the configured gate",
                "logic": {
                    "vocabulary_id": "gate:vocabulary", "rule_pack_id": "gate:rules",
                    "target": _target(),
                },
            },
            {
                "id": "art:check",
                "type": "artifact",
                "status": "current",
                "path": "results/check.json",
                "logic_bindings": [
                    {"id": "gate:check-complete", "vocabulary_id": "gate:vocabulary",
                     "predicate": "gate:check", "polarity": "positive",
                     "arguments": {
                         "release": {"kind": "json_pointer", "pointer": "/release"},
                         "ok": {"kind": "json_pointer", "pointer": "/ok"},
                     }},
                    {"id": "gate:check-failed", "vocabulary_id": "gate:vocabulary",
                     "predicate": "gate:check", "polarity": "positive",
                     "arguments": {
                         "release": {"kind": "json_pointer", "pointer": "/release"},
                         "ok": {"kind": "json_pointer", "pointer": "/not_ok"},
                     }},
                ],
            },
        ],
        "edges": [],
    }), encoding="utf-8")
    config_path = tmp_path / "claimtrace.config.json"
    config_path.write_text(json.dumps({
        "root": ".",
        "graph": "claimtrace/graph.json",
        "logic": {
            "derivations": "claimtrace/derivations",
            "vocabularies": ["claimtrace/logic/vocabulary.json"],
            "rule_packs": ["claimtrace/logic/rules.json"],
        },
    }), encoding="utf-8")
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(json.dumps(_proposal()), encoding="utf-8")
    return Config(config_path), proposal_path


def test_cli_derive_list_filter_and_explain_round_trip(tmp_path, capsys):
    cfg, proposal_path = _project(tmp_path)
    common = ["--config", str(cfg.config_path)]

    assert main([*common, "derive", str(proposal_path), "--actor", "agent:test", "--json"]) == 0
    recorded = json.loads(capsys.readouterr().out)
    assert recorded["stored_derived"] == recorded["current_derived"]
    assert recorded["current_derived"]["proof_state"] == "derivable"
    assert recorded["current_derived"]["active"] is True
    derivation_id = recorded["id"]

    assert main([*common, "explain", derivation_id, "--json"]) == 0
    explained = json.loads(capsys.readouterr().out)
    assert explained["id"] == derivation_id
    assert explained["current_derived"]["proof_steps"][0]["rule_id"] == (
        "gate:ready-if-check-ok"
    )

    assert main([*common, "derivations", "--state", "derivable", "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["integrity"] == "ok"
    assert [item["id"] for item in listing["items"]] == [derivation_id]

    assert main([*common, "derivations", "--state", "unknown", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["items"] == []


def test_cli_binding_selection_materializes_and_explains_by_proof_id(tmp_path, capsys):
    cfg, proposal_path = _project(tmp_path)
    proposal_path.write_text(json.dumps(_selection_proposal()), encoding="utf-8")
    common = ["--config", str(cfg.config_path)]

    assert main([*common, "derive", str(proposal_path), "--actor", "agent:test", "--json"]) == 0
    recorded = json.loads(capsys.readouterr().out)
    assert recorded["agent_input"]["target"] == _target()
    assert recorded["agent_input"]["facts"][0]["atom"]["arguments"]["ok"]["value"] is True
    proof_id = recorded["current_derived"]["proof_id"]

    assert main([*common, "explain", proof_id, "--json"]) == 0
    explained = json.loads(capsys.readouterr().out)
    assert explained["current_derived"]["proof_id"] == proof_id
    assert explained["equivalent_derivation_ids"] == [recorded["id"]]


def test_cli_binding_selection_rejects_agent_injected_policy_fields(tmp_path, capsys):
    cfg, proposal_path = _project(tmp_path)
    proposal = _selection_proposal()
    proposal["target"] = _target()
    proposal_path.write_text(json.dumps(proposal), encoding="utf-8")

    assert main([
        "--config", str(cfg.config_path), "derive", str(proposal_path),
        "--actor", "agent:test",
    ]) == 2
    error = capsys.readouterr().err
    assert "target or policy fields cannot be added" in error
    assert not cfg.derivations_path.exists()


def test_cli_rejects_computed_sections_and_does_not_append(tmp_path, capsys):
    cfg, proposal_path = _project(tmp_path)
    proposal = _proposal()
    proposal["derived"] = {"proof_state": "derivable"}
    proposal_path.write_text(json.dumps(proposal), encoding="utf-8")

    assert main([
        "--config", str(cfg.config_path), "derive", str(proposal_path),
        "--actor", "agent:test",
    ]) == 2
    assert "mechanical_snapshot and derived are computed by claimtrace" in capsys.readouterr().err
    assert not cfg.derivations_path.exists()


def test_cli_rejects_non_string_asset_selection_without_crashing(tmp_path, capsys):
    cfg, proposal_path = _project(tmp_path)
    proposal = _proposal()
    proposal["vocabulary_id"] = []
    proposal_path.write_text(json.dumps(proposal), encoding="utf-8")

    assert main([
        "--config", str(cfg.config_path), "derive", str(proposal_path),
        "--actor", "agent:test",
    ]) == 2
    assert "vocabulary_id must be a non-empty string" in capsys.readouterr().err
    assert not cfg.derivations_path.exists()


def test_cli_rejects_duplicate_configured_asset_ids(tmp_path, capsys):
    cfg, proposal_path = _project(tmp_path)
    duplicate_path = tmp_path / "claimtrace" / "logic" / "duplicate.json"
    duplicate_path.write_text(json.dumps(_vocabulary()), encoding="utf-8")
    config = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    config["logic"]["vocabularies"].append("claimtrace/logic/duplicate.json")
    cfg.config_path.write_text(json.dumps(config), encoding="utf-8")

    assert main([
        "--config", str(cfg.config_path), "derive", str(proposal_path),
        "--actor", "agent:test",
    ]) == 2
    assert "duplicate configured vocabulary id" in capsys.readouterr().err
    assert not cfg.derivations_path.exists()


def test_cli_preflight_blocks_append_into_corrupt_store(tmp_path, capsys):
    cfg, proposal_path = _project(tmp_path)
    cfg.derivations_path.mkdir()
    (cfg.derivations_path / "corrupt.json").write_text("{}", encoding="utf-8")

    assert main([
        "--config", str(cfg.config_path), "derive", str(proposal_path),
        "--actor", "agent:test",
    ]) == 2
    assert "derivation store integrity failed before append" in capsys.readouterr().err
    assert list(cfg.derivations_path.glob("*.json")) == [cfg.derivations_path / "corrupt.json"]


def test_cli_listing_suppresses_active_when_store_integrity_fails(tmp_path, capsys):
    cfg, proposal_path = _project(tmp_path)
    common = ["--config", str(cfg.config_path)]
    assert main([*common, "derive", str(proposal_path), "--actor", "agent:test"]) == 0
    capsys.readouterr()
    (cfg.derivations_path / "corrupt.json").write_text("{}", encoding="utf-8")

    assert main([*common, "derivations", "--json"]) == 2
    listing = json.loads(capsys.readouterr().out)
    assert listing["integrity"] == "error"
    assert len(listing["items"]) == 1
    assert listing["items"][0]["current_derived"]["active"] is False


def test_cli_explain_fails_closed_on_unconfigured_live_assets(tmp_path, capsys):
    cfg, proposal_path = _project(tmp_path)
    common = ["--config", str(cfg.config_path)]
    assert main([*common, "derive", str(proposal_path), "--actor", "agent:test", "--json"]) == 0
    derivation_id = json.loads(capsys.readouterr().out)["id"]
    config = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    config["logic"]["rule_packs"] = []
    cfg.config_path.write_text(json.dumps(config), encoding="utf-8")

    assert main([*common, "explain", derivation_id, "--json"]) == 2
    assert "unconfigured rule pack" in capsys.readouterr().err


def test_cli_surfaces_cross_derivation_claim_conflict(tmp_path, capsys):
    cfg, proposal_path = _project(tmp_path)
    common = ["--config", str(cfg.config_path)]
    assert main([*common, "derive", str(proposal_path), "--actor", "agent:positive"]) == 0
    capsys.readouterr()

    negative = _proposal()
    fact = negative["agent_input"]["facts"][0]
    fact["atom"]["arguments"]["ok"]["value"] = False
    fact["evidence"][0]["binding_id"] = "gate:check-failed"
    proposal_path.write_text(json.dumps(negative), encoding="utf-8")
    assert main([
        *common, "derive", str(proposal_path), "--actor", "agent:negative", "--json",
    ]) == 1
    recorded = json.loads(capsys.readouterr().out)
    assert recorded["current_derived"]["proof_state"] == "refutable"
    assert recorded["current_derived"]["active"] is True
    assert recorded["claim_level"]["state"] == "conflict"
    assert recorded["claim_level"]["active"] is False

    assert main([*common, "derivations", "--json"]) == 1
    listing = json.loads(capsys.readouterr().out)
    assert {item["claim_level"]["state"] for item in listing["items"]} == {"conflict"}
    assert not any(item["claim_level"]["active"] for item in listing["items"])
