"""Provider-neutral adversarial deliberation for scientific claim semantics.

Agents may propose exact, source-anchored candidates and submit role-bound ballots.
ProvSleuth records and mechanically validates those inputs; it never lets a panel
activate a graph change, semantic mapping, evidence plan, or rule pack.  The strongest
positive state is therefore ``recommended_for_human_review``.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import engine, events, logic, semantics
from .config import strict_json_loads
from .engine import load_raw
from .graph_changes import canonical_graph_hash


PROPOSAL_REQUEST_SCHEMA = "claimtrace.deliberation-proposal-request/1"
PROPOSAL_SCHEMA = "claimtrace.deliberation-proposal/1"
CANDIDATE_SET_REQUEST_SCHEMA = "claimtrace.deliberation-candidate-set-request/1"
CANDIDATE_SET_SCHEMA = "claimtrace.deliberation-candidate-set/1"
BALLOT_REQUEST_SCHEMA = "claimtrace.deliberation-ballot-request/1"
BALLOT_SCHEMA = "claimtrace.deliberation-ballot/1"
PHASE_DECISION_REQUEST_SCHEMA = "claimtrace.deliberation-phase-decision-request/1"
PHASE_DECISION_SCHEMA = "claimtrace.deliberation-phase-decision/1"
STATUS_SCHEMA = "claimtrace.deliberation-status/1"

PHASES = (
    "claim_extraction",
    "semantic_interpretation",
    "formalization",
    "rule_validity",
)

REQUIRED_ROLES = {
    "claim_extraction": (
        "source_verifier", "coverage_reviewer", "adversarial_falsifier",
    ),
    "semantic_interpretation": (
        "semantic_reviewer", "scope_reviewer", "adversarial_falsifier",
    ),
    "formalization": (
        "evidence_mapper", "logic_critic", "adversarial_falsifier",
    ),
    "rule_validity": (
        "logic_critic", "domain_reviewer", "adversarial_falsifier",
    ),
}
REVIEW_ROLES = frozenset(role for roles in REQUIRED_ROLES.values() for role in roles)
DECISIONS = frozenset({"endorse", "reject", "abstain"})

REASON_CODES = frozenset({
    "SOURCE_ANCHOR_INVALID", "CLAIM_NOT_ATOMIC", "NEGATION_SCOPE_CONFLICT",
    "QUANTIFIER_SCOPE_CONFLICT", "MODALITY_FORCE_CONFLICT",
    "REPORTING_TRUTH_CONFLATION", "THEOREM_PROOF_CONFLATION",
    "ASSUMPTION_SCOPE_MISSING", "EVIDENCE_ROLE_LEAKAGE",
    "EVIDENCE_PLAN_INCOMPLETE", "CLAIM_COVERAGE_INCOMPLETE",
    "RULE_CIRCULAR_SUPPORT", "RULE_TARGET_AS_PREMISE",
    "RULE_UNGROUNDED_PREMISE", "RULE_CONCLUSION_SCOPE_EXPANSION",
    "RULE_SUMMARY_BOOLEAN_OPAQUE", "RULE_TEST_MATRIX_INCOMPLETE",
    "REVIEWER_CORRELATED", "REQUIRED_ROLE_MISSING", "CRITICAL_ABSTENTION",
    "MATERIAL_DISSENT", "HUMAN_SIGNOFF_REQUIRED", "OTHER_BOUNDED_OBJECTION",
})

MANDATORY_COMPETENCY_CATEGORIES = frozenset({
    "positive", "explicit_negative", "missing_premise", "boundary",
    "unit_mismatch", "conflict", "counterexample",
})

CLAIM_KINDS = frozenset({
    "reporting", "descriptive", "associational", "causal", "predictive",
    "mechanistic", "methodological", "theorem", "limitation", "other",
})
SPEECH_ACTS = frozenset({
    "assertion", "hypothesis", "prediction", "question", "conclusion",
    "limitation",
})
MODALITIES = frozenset({
    "observed", "estimated", "suggested", "possible", "probable", "necessary",
    "sufficient", "intended", "unknown",
})
QUANTIFIERS = frozenset({
    "all", "some", "none", "exactly", "at_least", "at_most", "range",
    "unspecified",
})

PROPOSAL_ID_RE = re.compile(r"^deliberation-proposal:sha256:([0-9a-f]{64})$")
CANDIDATE_ID_RE = re.compile(r"^deliberation-candidate:sha256:([0-9a-f]{64})$")
CANDIDATE_SET_ID_RE = re.compile(r"^deliberation-set:sha256:([0-9a-f]{64})$")
BALLOT_ID_RE = re.compile(r"^deliberation-ballot:sha256:([0-9a-f]{64})$")
PHASE_DECISION_ID_RE = re.compile(
    r"^deliberation-decision:sha256:([0-9a-f]{64})$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_STORE_BYTES = 512 * 1024 * 1024
MAX_RECORDS = 50_000
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_SPAN_BYTES = 64 * 1024
MAX_TEXT = 16_384
MAX_SHORT_TEXT = 2_048
MAX_LIST = 256
MAX_CANDIDATES = 256


class DeliberationError(RuntimeError):
    """A deliberation input or ledger cannot be trusted mechanically."""


def _canonical_bytes(value) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise DeliberationError(f"value is not canonical JSON: {exc}") from exc


def _sha256(value) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _content_id(prefix, value) -> str:
    return f"{prefix}:sha256:{_sha256(value)}"


def _bounded_text(value, label, limit=MAX_TEXT, *, nonempty=True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise DeliberationError(f"{label} must be a non-empty string")
    if len(value) > limit:
        raise DeliberationError(f"{label} exceeds the {limit}-character limit")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise DeliberationError(f"{label} contains a lone Unicode surrogate")
    return value


def _identifier(value, label) -> str:
    value = _bounded_text(value, label, 512)
    if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value):
        raise DeliberationError(f"{label} contains terminal controls")
    return value


def _text_list(value, label, *, required=False, limit=MAX_LIST) -> list[str]:
    if not isinstance(value, list) or len(value) > limit or (required and not value):
        qualifier = "non-empty " if required else ""
        raise DeliberationError(f"{label} must be a bounded {qualifier}list")
    normalized = [_bounded_text(item, f"{label} item", MAX_SHORT_TEXT) for item in value]
    if len(normalized) != len(set(normalized)):
        raise DeliberationError(f"{label} must not contain duplicates")
    return sorted(normalized)


def _timestamp(value=None) -> str:
    if value is None:
        value = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
    if not isinstance(value, str) or not value.endswith("Z"):
        raise DeliberationError("recorded_at must be an RFC3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise DeliberationError("recorded_at is not a valid RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise DeliberationError("recorded_at must use UTC")
    return value


def _timestamp_value(value):
    _timestamp(value)
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _phase(value) -> str:
    if value not in PHASES:
        raise DeliberationError(f"phase must be one of {', '.join(PHASES)}")
    return value


def _actor(actor, independence_group, provenance=None) -> dict:
    actor = _identifier(actor, "actor")
    group = _identifier(independence_group, "independence_group")
    provenance = {} if provenance is None else provenance
    if not isinstance(provenance, dict):
        raise DeliberationError("provenance must be an object")
    allowed = {
        "model", "model_revision", "role_template_sha256", "context_bundle_sha256",
        "retrieval_snapshot_sha256", "prompt_sha256", "sampling",
    }
    if set(provenance) - allowed:
        raise DeliberationError("provenance has unknown fields")
    normalized = {}
    for key in sorted(set(provenance) - {"sampling"}):
        value = _bounded_text(provenance[key], f"provenance.{key}", MAX_SHORT_TEXT)
        if key.endswith("sha256") and not SHA256_RE.fullmatch(value):
            raise DeliberationError(f"provenance.{key} must be a lowercase SHA-256")
        normalized[key] = value
    if "sampling" in provenance:
        sampling = provenance["sampling"]
        if not isinstance(sampling, dict) or len(sampling) > 32:
            raise DeliberationError("provenance.sampling must be a bounded object")
        if len(_canonical_bytes(sampling)) > MAX_SHORT_TEXT:
            raise DeliberationError("provenance.sampling is too large")
        normalized["sampling"] = copy.deepcopy(sampling)
    return {"id": actor, "independence_group": group, "provenance": normalized}


def _logic_snapshot(cfg) -> dict:
    try:
        vocabularies, rule_packs = logic.configured_logic_assets(cfg)
    except logic.LogicError as exc:
        raise DeliberationError(f"configured logic assets are invalid: {exc}") from exc
    return {
        "vocabularies": [
            {"id": key, "sha256": _sha256(value)}
            for key, value in sorted(vocabularies.items())
        ],
        "rule_packs": [
            {"id": key, "sha256": _sha256(value)}
            for key, value in sorted(rule_packs.items())
        ],
    }


def _semantic_policy_snapshot(cfg) -> dict:
    """Resolve the configured policy to one presently valid active release."""
    try:
        status = semantics.evaluate_active_semantic_policy(cfg)
    except (OSError, ValueError, semantics.SemanticError) as exc:
        raise DeliberationError(f"cannot evaluate active semantic policy: {exc}") from exc
    configured = bool(status.get("configured"))
    if not configured:
        if getattr(cfg, "require_active_semantic_policy", False):
            raise DeliberationError(
                "the project requires an active semantic policy but none is configured"
            )
        return {
            "configured_policy_id": None,
            "active": False,
            "policy_sha256": None,
            "active_mapping_ids": [],
        }
    policy = status.get("policy")
    evaluation = status.get("evaluation")
    if (not isinstance(policy, dict) or not isinstance(evaluation, dict)
            or evaluation.get("active") is not True or status.get("findings")):
        details = "; ".join(
            str(item.get("detail", item))
            for item in status.get("findings") or [] if isinstance(item, dict)
        ) or "configured semantic policy is unavailable, stale, or invalid"
        raise DeliberationError(f"active semantic policy is not valid: {details}")
    mapping_ids = evaluation.get("active_mapping_ids")
    if (not isinstance(mapping_ids, list)
            or mapping_ids != sorted(set(mapping_ids))):
        raise DeliberationError("active semantic policy mapping projection is invalid")
    return {
        "configured_policy_id": policy["id"],
        "active": True,
        "policy_sha256": _sha256(policy),
        "active_mapping_ids": list(mapping_ids),
    }


def _project_snapshot(cfg, raw=None) -> dict:
    raw = load_raw(cfg) if raw is None else raw
    return {
        "graph_hash": canonical_graph_hash(raw),
        "semantic_policy": _semantic_policy_snapshot(cfg),
        "logic_assets": _logic_snapshot(cfg),
    }


def _source_snapshot(cfg, anchor, raw=None) -> dict:
    if not isinstance(anchor, dict) or set(anchor) not in (
            {"node_id", "start_byte", "end_byte"},
            {"node_id", "start_byte", "end_byte", "span_sha256"}):
        raise DeliberationError(
            "source_anchor requires node_id, start_byte, end_byte, and optional span_sha256"
        )
    node_id = _identifier(anchor["node_id"], "source_anchor.node_id")
    start = anchor["start_byte"]
    end = anchor["end_byte"]
    if (not isinstance(start, int) or isinstance(start, bool)
            or not isinstance(end, int) or isinstance(end, bool)
            or start < 0 or end <= start or end - start > MAX_SPAN_BYTES):
        raise DeliberationError("source_anchor byte bounds are invalid or too large")
    raw = load_raw(cfg) if raw is None else raw
    nodes = {item.get("id"): item for item in raw.get("nodes", []) if isinstance(item, dict)}
    node = nodes.get(node_id)
    if node is None or not isinstance(node.get("path"), str) or not node["path"]:
        raise DeliberationError("source_anchor node must exist and declare a file path")
    path = cfg.resolve(node["path"])
    try:
        data = events._stable_bounded_bytes(path, MAX_SOURCE_BYTES)
    except (OSError, ValueError, events.EventError) as exc:
        raise DeliberationError(f"cannot read stable source artifact {node['path']}: {exc}") from exc
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DeliberationError(
            "source artifact must be canonical UTF-8 text; convert PDFs first"
        ) from exc
    if end > len(data):
        raise DeliberationError("source_anchor end_byte exceeds the source artifact")
    span = data[start:end]
    try:
        excerpt = span.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DeliberationError("source_anchor splits a UTF-8 code point") from exc
    digest = hashlib.sha256(span).hexdigest()
    supplied = anchor.get("span_sha256")
    if supplied is not None and (
            not isinstance(supplied, str) or not SHA256_RE.fullmatch(supplied)
            or supplied != digest):
        raise DeliberationError("source_anchor span_sha256 does not match selected bytes")
    return {
        "node_id": node_id,
        "path": node["path"],
        "start_byte": start,
        "end_byte": end,
        "span_sha256": digest,
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "source_size": len(data),
        "excerpt": excerpt,
    }


def _configured_vocabulary(cfg, vocabulary_id):
    vocabulary_id = _identifier(vocabulary_id, "vocabulary_id")
    try:
        vocabularies, _rule_packs = logic.configured_logic_assets(cfg)
    except logic.LogicError as exc:
        raise DeliberationError(f"configured logic assets are invalid: {exc}") from exc
    vocabulary = vocabularies.get(vocabulary_id)
    if vocabulary is None:
        raise DeliberationError(
            f"vocabulary_id is not an exact configured vocabulary: {vocabulary_id}"
        )
    return vocabulary


def _normal_method_requirements(value):
    if not isinstance(value, list) or len(value) > MAX_LIST:
        raise DeliberationError("method_requirements must be a bounded list")
    normalized = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"method_id", "step_ids"}:
            raise DeliberationError(
                "each method requirement needs exactly method_id and step_ids"
            )
        normalized.append({
            "method_id": _identifier(item["method_id"], "method requirement method_id"),
            "step_ids": _text_list(
                item["step_ids"], "method requirement step_ids", required=True,
            ),
        })
    keys = [(item["method_id"], tuple(item["step_ids"])) for item in normalized]
    if len(keys) != len(set(keys)):
        raise DeliberationError("method_requirements must not contain duplicates")
    return sorted(normalized, key=lambda item: (item["method_id"], item["step_ids"]))


def _validate_formalization_dependencies(
        raw, evidence_plan, method_requirements, vocabulary):
    """Resolve planned or current IDs against graph declarations, not output files."""
    nodes = {
        item["id"]: item for item in raw.get("nodes", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    synthetic_claim = {
        "id": "deliberation:formalization-subject",
        "type": "claim",
        "logic_evidence_plan": evidence_plan,
    }
    try:
        logic._resolve_claim_evidence_plan(synthetic_claim, nodes, vocabulary)
    except logic.LogicError as exc:
        raise DeliberationError(
            f"formalization evidence plan does not resolve against graph declarations: {exc}"
        ) from exc
    for selection in evidence_plan["required_bindings"]:
        result_id = selection["result_id"]
        result = nodes[result_id]
        if result.get("status") not in {
                None, "planned", "current", "confirmed", "null"}:
            raise DeliberationError(
                f"formalization references inactive result node {result_id!r}"
            )
    for requirement in method_requirements:
        method_id = requirement["method_id"]
        method = nodes.get(method_id)
        if method is None:
            raise DeliberationError(
                f"formalization references unknown method node {method_id!r}"
            )
        if method.get("type") != "method":
            raise DeliberationError(
                f"formalization reference {method_id!r} is not a method node"
            )
        if method.get("status") not in {None, "planned", "current", "confirmed"}:
            raise DeliberationError(
                f"formalization references inactive method node {method_id!r}"
            )
        try:
            steps = engine._validate_method_spec(method, "deliberation graph")
        except engine.GraphError as exc:
            raise DeliberationError(
                f"formalization method {method_id!r} is not fully declared: {exc}"
            ) from exc
        missing = sorted(set(requirement["step_ids"]) - set(steps))
        if missing:
            raise DeliberationError(
                f"formalization references unknown method step(s) for {method_id!r}: "
                + ", ".join(missing)
            )


def _rule_dependency_cycle(rule_pack, vocabulary):
    derived = {
        item["id"] for item in vocabulary["predicates"] if item["kind"] == "derived"
    }
    adjacency = {predicate: set() for predicate in derived}
    for rule in rule_pack["rules"]:
        head = rule["then"]["predicate"]
        for premise in rule["when"]:
            if premise["predicate"] in derived:
                adjacency[premise["predicate"]].add(head)
    visiting = set()
    visited = set()

    def visit(node):
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for target in sorted(adjacency[node]):
            if visit(target):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in sorted(adjacency))


def _case_state(case, vocabulary, rule_pack):
    try:
        inputs = [
            logic._normal_ground_atom(item, vocabulary, expected_kind="input")
            for item in case["input_atoms"]
        ]
        target = logic._normal_ground_atom(case["target"], vocabulary)
    except (logic.LogicError, TypeError, KeyError, ValueError):
        return "input_rejected"
    try:
        facts, _steps = logic._closure(inputs, vocabulary, rule_pack)
    except logic.LogicError:
        return "execution_rejected"
    atoms = list(facts.values())
    positive = target in atoms
    opposite = copy.deepcopy(target)
    opposite["polarity"] = "negative" if target["polarity"] == "positive" else "positive"
    negative = opposite in atoms
    if positive and negative:
        return "conflict"
    if positive:
        return "derivable"
    if negative:
        return "refutable"
    return "unknown"


def _normal_competency_cases(value, vocabulary, rule_pack):
    if (not isinstance(value, list) or not value
            or len(value) > MAX_LIST):
        raise DeliberationError("competency_cases must be a non-empty bounded list")
    allowed_states = {
        "derivable", "refutable", "conflict", "unknown", "input_rejected",
        "execution_rejected",
    }
    normalized = []
    categories = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {
                "id", "category", "input_atoms", "target", "expected_state"}:
            raise DeliberationError(
                "each competency case requires id, category, input_atoms, target, "
                "and expected_state"
            )
        category = item["category"]
        if (not isinstance(category, str)
                or category not in MANDATORY_COMPETENCY_CATEGORIES):
            raise DeliberationError(f"unknown competency category: {category!r}")
        if (not isinstance(item["input_atoms"], list)
                or len(item["input_atoms"]) > logic.MAX_INPUT_FACTS):
            raise DeliberationError("competency input_atoms must be a bounded list")
        expected = item["expected_state"]
        if not isinstance(expected, str) or expected not in allowed_states:
            raise DeliberationError("competency expected_state is invalid")
        raw_case = {
            "id": _identifier(item["id"], "competency case id"),
            "category": category,
            "input_atoms": copy.deepcopy(item["input_atoms"]),
            "target": copy.deepcopy(item["target"]),
            "expected_state": expected,
        }
        observed = _case_state(raw_case, vocabulary, rule_pack)
        raw_case["observed_state"] = observed
        raw_case["passed"] = observed == expected
        normalized.append(raw_case)
        categories.append(category)
    if set(categories) != MANDATORY_COMPETENCY_CATEGORIES:
        missing = sorted(MANDATORY_COMPETENCY_CATEGORIES - set(categories))
        raise DeliberationError(
            "competency_cases omit mandatory categories: " + ", ".join(missing)
        )
    ids = [item["id"] for item in normalized]
    if len(ids) != len(set(ids)):
        raise DeliberationError("competency case ids must be unique")
    required_states = {
        "positive": {"derivable"},
        "explicit_negative": {"refutable"},
        "missing_premise": {"unknown"},
        "boundary": {"derivable", "refutable", "unknown"},
        "unit_mismatch": {"input_rejected"},
        "conflict": {"conflict"},
        "counterexample": {"refutable"},
    }
    requires_inputs = MANDATORY_COMPETENCY_CATEGORIES - {"missing_premise"}
    profiles = {}
    for case in normalized:
        if case["expected_state"] not in required_states[case["category"]]:
            raise DeliberationError(
                f"competency category {case['category']!r} requires expected_state "
                + ", ".join(sorted(required_states[case["category"]]))
            )
        if case["category"] in requires_inputs and not case["input_atoms"]:
            raise DeliberationError(
                f"competency category {case['category']!r} requires input atoms"
            )
        profile = _canonical_bytes({
            "input_atoms": case["input_atoms"], "target": case["target"],
        })
        previous = profiles.get(profile)
        if previous is not None:
            raise DeliberationError(
                "competency cases must not relabel the same inputs and target: "
                f"{previous!r} and {case['id']!r}"
            )
        profiles[profile] = case["id"]
    positive_cases = [
        item for item in normalized if item["category"] == "positive"
    ]
    negative_cases = [
        item for item in normalized if item["category"] == "explicit_negative"
    ]

    def atom_set(case):
        return {_canonical_bytes(item) for item in case["input_atoms"]}

    def same_target(first, second):
        return _canonical_bytes(first["target"]) == _canonical_bytes(second["target"])

    def typed_terms(value):
        terms = set()
        if isinstance(value, dict):
            if set(value) == {"type", "value", "unit"}:
                terms.add(_canonical_bytes(value))
            for nested in value.values():
                terms.update(typed_terms(nested))
        elif isinstance(value, list):
            for nested in value:
                terms.update(typed_terms(nested))
        return terms

    def preserves_target_terms(case):
        return typed_terms(case["target"]) <= typed_terms(case["input_atoms"])

    def difference_paths(first, second, path=()):
        if type(first) is not type(second):
            return [path]
        if isinstance(first, dict):
            if set(first) != set(second):
                return [path]
            result = []
            for key in sorted(first):
                result.extend(difference_paths(first[key], second[key], path + (key,)))
            return result
        if isinstance(first, list):
            if len(first) != len(second):
                return [path]
            result = []
            for index, (left, right) in enumerate(zip(first, second)):
                result.extend(difference_paths(left, right, path + (index,)))
            return result
        return [] if first == second else [path]

    for case in (
            item for item in normalized if item["category"] == "missing_premise"):
        missing_atoms = atom_set(case)
        if not any(
                same_target(case, positive)
                and missing_atoms < atom_set(positive)
                for positive in positive_cases):
            raise DeliberationError(
                "each missing_premise case must remove at least one atom from a "
                "positive case with the same target"
            )
    for case in (item for item in normalized if item["category"] == "boundary"):
        if not any(
                same_target(case, positive)
                and len(atom_set(case)) == len(atom_set(positive))
                and len(atom_set(case) ^ atom_set(positive)) == 2
                and preserves_target_terms(case)
                for positive in positive_cases):
            raise DeliberationError(
                "each boundary case must replace exactly one input atom from a "
                "positive case with the same target"
            )
    for case in (
            item for item in normalized if item["category"] == "counterexample"):
        matched = False
        for negative in negative_cases:
            if not same_target(case, negative) or not atom_set(negative) < atom_set(case):
                continue
            added = [
                atom for atom in case["input_atoms"]
                if _canonical_bytes(atom) not in atom_set(negative)
            ]
            for positive in positive_cases:
                positive_predicates = {
                    atom.get("predicate") for atom in positive["input_atoms"]
                    if isinstance(atom, dict)
                }
                target_terms = typed_terms(case["target"])
                if (same_target(case, positive) and added
                        and all(
                            isinstance(atom, dict)
                            and atom.get("predicate") in positive_predicates
                            and target_terms <= typed_terms(atom)
                            for atom in added
                        )):
                    matched = True
                    break
            if matched:
                break
        if not matched:
            raise DeliberationError(
                "each counterexample must extend an explicit_negative case only with "
                "target-related atoms from predicates used by a positive case"
            )
    for case in (item for item in normalized if item["category"] == "conflict"):
        if not any(
                same_target(case, positive) and same_target(case, negative)
                and atom_set(positive) | atom_set(negative) == atom_set(case)
                for positive in positive_cases for negative in negative_cases):
            raise DeliberationError(
                "each conflict case must equal the exact union of a positive and an "
                "explicit_negative case with the same target"
            )
    for case in (
            item for item in normalized if item["category"] == "unit_mismatch"):
        matched = False
        for positive in positive_cases:
            if not same_target(case, positive) or len(case["input_atoms"]) != len(
                    positive["input_atoms"]):
                continue
            left = sorted(positive["input_atoms"], key=_canonical_bytes)
            right = sorted(case["input_atoms"], key=_canonical_bytes)
            differences = difference_paths(left, right)
            if len(differences) == 1 and differences[0] and differences[0][-1] == "unit":
                matched = True
                break
        if not matched:
            raise DeliberationError(
                "each unit_mismatch case must change exactly one unit field from a "
                "positive case with the same target"
            )
    return sorted(normalized, key=lambda item: item["id"])


def _normal_payload(cfg, phase, payload, source_snapshot, *, raw=None):
    if not isinstance(payload, dict):
        raise DeliberationError("payload must be an object")
    if phase == "claim_extraction":
        expected = {
            "claim_text", "claim_kind", "speech_act", "polarity", "qualifiers",
        }
        if set(payload) != expected:
            raise DeliberationError(
                "claim_extraction payload requires claim_text, claim_kind, speech_act, "
                "polarity, and qualifiers"
            )
        claim_text = _bounded_text(payload["claim_text"], "claim_text")
        if claim_text not in source_snapshot["excerpt"]:
            raise DeliberationError(
                "claim_text must be an exact substring of the selected source bytes"
            )
        if payload["claim_kind"] not in CLAIM_KINDS:
            raise DeliberationError("claim_kind is invalid")
        if payload["speech_act"] not in SPEECH_ACTS:
            raise DeliberationError("speech_act is invalid")
        if payload["polarity"] not in {"positive", "negative"}:
            raise DeliberationError("polarity must be positive or negative")
        return {
            "claim_text": claim_text,
            "claim_kind": payload["claim_kind"],
            "speech_act": payload["speech_act"],
            "polarity": payload["polarity"],
            "qualifiers": _text_list(payload["qualifiers"], "qualifiers"),
        }, {"passed": True, "checks": ["exact_utf8_source_substring"]}

    if phase == "semantic_interpretation":
        expected = {
            "extraction_candidate_id", "normalized_claim", "frame", "modality",
            "quantifier", "negation_scope", "conditions", "ambiguities",
            "non_equivalences",
        }
        if set(payload) != expected:
            raise DeliberationError("semantic_interpretation payload has an invalid shape")
        if (not isinstance(payload["extraction_candidate_id"], str)
                or not CANDIDATE_ID_RE.fullmatch(payload["extraction_candidate_id"])):
            raise DeliberationError("extraction_candidate_id is invalid")
        frame_fields = {
            "population", "exposure", "comparator", "outcome", "direction",
            "magnitude", "time_scope", "inference_level",
        }
        frame = payload["frame"]
        if not isinstance(frame, dict) or set(frame) != frame_fields:
            raise DeliberationError("semantic frame must contain the exact eight dimensions")
        normalized_frame = {}
        for key in sorted(frame):
            value = frame[key]
            if value is not None:
                value = _bounded_text(value, f"frame.{key}", MAX_SHORT_TEXT)
            normalized_frame[key] = value
        if payload["modality"] not in MODALITIES:
            raise DeliberationError("modality is invalid")
        quantifier = payload["quantifier"]
        if (not isinstance(quantifier, dict)
                or set(quantifier) != {"kind", "range"}
                or quantifier["kind"] not in QUANTIFIERS
                or (quantifier["range"] is not None
                    and not isinstance(quantifier["range"], str))):
            raise DeliberationError("quantifier requires a known kind and string-or-null range")
        if quantifier["range"] is not None:
            _bounded_text(quantifier["range"], "quantifier.range", MAX_SHORT_TEXT)
        negation = payload["negation_scope"]
        if not isinstance(negation, dict) or set(negation) != {"polarity", "scope"}:
            raise DeliberationError("negation_scope requires polarity and scope")
        if negation["polarity"] not in {"positive", "negative"}:
            raise DeliberationError("negation_scope.polarity is invalid")
        if negation["scope"] is not None:
            _bounded_text(negation["scope"], "negation_scope.scope", MAX_SHORT_TEXT)
        return {
            "extraction_candidate_id": payload["extraction_candidate_id"],
            "normalized_claim": _bounded_text(payload["normalized_claim"], "normalized_claim"),
            "frame": normalized_frame,
            "modality": payload["modality"],
            "quantifier": copy.deepcopy(quantifier),
            "negation_scope": copy.deepcopy(negation),
            "conditions": _text_list(payload["conditions"], "conditions"),
            "ambiguities": _text_list(payload["ambiguities"], "ambiguities"),
            "non_equivalences": _text_list(
                payload["non_equivalences"], "non_equivalences",
            ),
        }, {"passed": True, "checks": ["closed_eight_dimension_frame"]}

    if phase == "formalization":
        expected = {
            "interpretation_candidate_id", "vocabulary_id", "target", "evidence_plan",
            "method_requirements", "assumptions", "non_equivalences",
        }
        if set(payload) != expected:
            raise DeliberationError("formalization payload has an invalid shape")
        if (not isinstance(payload["interpretation_candidate_id"], str)
                or not CANDIDATE_ID_RE.fullmatch(payload["interpretation_candidate_id"])):
            raise DeliberationError("interpretation_candidate_id is invalid")
        vocabulary = _configured_vocabulary(cfg, payload["vocabulary_id"])
        try:
            target = logic._normal_ground_atom(payload["target"], vocabulary)
            evidence_plan = logic._normal_evidence_plan(payload["evidence_plan"])
        except logic.LogicError as exc:
            raise DeliberationError(f"formalization is mechanically invalid: {exc}") from exc
        method_requirements = _normal_method_requirements(
            payload["method_requirements"]
        )
        if raw is None:
            raw = load_raw(cfg)
        _validate_formalization_dependencies(
            raw, evidence_plan, method_requirements, vocabulary,
        )
        return {
            "interpretation_candidate_id": payload["interpretation_candidate_id"],
            "vocabulary_id": vocabulary["id"],
            "target": target,
            "evidence_plan": evidence_plan,
            "method_requirements": method_requirements,
            "assumptions": _text_list(payload["assumptions"], "assumptions"),
            "non_equivalences": _text_list(
                payload["non_equivalences"], "non_equivalences", required=True,
            ),
        }, {
            "passed": True,
            "checks": [
                "typed_target", "closed_all_of_evidence_plan",
                "declared_graph_evidence_bindings", "declared_graph_method_steps",
            ],
        }

    expected = {
        "formalization_candidate_id", "vocabulary_id", "rule_pack", "warrant",
        "scope", "assumptions", "non_equivalences", "competency_cases",
    }
    if set(payload) != expected:
        raise DeliberationError("rule_validity payload has an invalid shape")
    if (not isinstance(payload["formalization_candidate_id"], str)
            or not CANDIDATE_ID_RE.fullmatch(payload["formalization_candidate_id"])):
        raise DeliberationError("formalization_candidate_id is invalid")
    vocabulary = _configured_vocabulary(cfg, payload["vocabulary_id"])
    try:
        rule_pack = logic.load_rule_pack(payload["rule_pack"], vocabulary)
    except logic.LogicError as exc:
        raise DeliberationError(f"proposed rule pack is mechanically invalid: {exc}") from exc
    cases = _normal_competency_cases(
        payload["competency_cases"], vocabulary, rule_pack,
    )
    warrant = _bounded_text(payload["warrant"], "warrant")
    if warrant not in source_snapshot["excerpt"]:
        raise DeliberationError(
            "rule warrant must be an exact substring of the selected source bytes"
        )
    cycle = _rule_dependency_cycle(rule_pack, vocabulary)
    failed_cases = [item["id"] for item in cases if not item["passed"]]
    mechanical = {
        "passed": not cycle and not failed_cases,
        "checks": [
            "finite_typed_rule_syntax", "acyclic_derived_predicate_dependencies",
                "legacy_relational_competency_matrix",
        ],
        "dependency_cycle": cycle,
        "failed_competency_case_ids": failed_cases,
    }
    return {
        "formalization_candidate_id": payload["formalization_candidate_id"],
        "vocabulary_id": vocabulary["id"],
        "rule_pack": rule_pack,
        "warrant": warrant,
        "scope": _bounded_text(payload["scope"], "scope"),
        "assumptions": _text_list(payload["assumptions"], "assumptions"),
        "non_equivalences": _text_list(
            payload["non_equivalences"], "non_equivalences", required=True,
        ),
        "competency_cases": cases,
    }, mechanical


def _proposal_candidate_core(document):
    return {
        "round_id": document["round_id"],
        "phase": document["phase"],
        "subject_key": document["subject_key"],
        "source_anchor": document["source_anchor"],
        "mechanical_snapshot": document["mechanical_snapshot"],
        "payload": document["payload"],
        "mechanical_validation": document["mechanical_validation"],
    }


def create_proposal(cfg, entry, *, actor, independence_group, recorded_at=None):
    """Create, but do not append, one exact source-anchored candidate proposal."""
    if not isinstance(entry, dict) or set(entry) not in (
            {"schema_version", "round_id", "phase", "subject_key", "source_anchor",
             "payload", "rationale"},
            {"schema_version", "round_id", "phase", "subject_key", "source_anchor",
             "payload", "rationale", "provenance"}):
        raise DeliberationError("proposal request has unknown or missing fields")
    if entry["schema_version"] != PROPOSAL_REQUEST_SCHEMA:
        raise DeliberationError("unsupported deliberation proposal-request schema")
    phase = _phase(entry["phase"])
    raw = load_raw(cfg)
    source = _source_snapshot(cfg, entry["source_anchor"], raw=raw)
    payload, mechanical_validation = _normal_payload(
        cfg, phase, entry["payload"], source, raw=raw,
    )
    proposal = {
        "schema_version": PROPOSAL_SCHEMA,
        "record_type": "proposal",
        "recorded_at": _timestamp(recorded_at),
        "round_id": _identifier(entry["round_id"], "round_id"),
        "phase": phase,
        "subject_key": _identifier(entry["subject_key"], "subject_key"),
        "source_anchor": source,
        "mechanical_snapshot": _project_snapshot(cfg, raw=raw),
        "payload": payload,
        "mechanical_validation": mechanical_validation,
        "actor": _actor(actor, independence_group, entry.get("provenance")),
        "rationale": _bounded_text(entry["rationale"], "rationale"),
    }
    proposal["candidate_id"] = _content_id(
        "deliberation-candidate", _proposal_candidate_core(proposal),
    )
    core = copy.deepcopy(proposal)
    proposal["proposal_id"] = _content_id("deliberation-proposal", core)
    validate_record(proposal)
    documents, issues = load_records(cfg)
    if issues:
        raise DeliberationError("deliberation store integrity failed before proposal")
    _validate_phase_reference(cfg, proposal, documents)
    return proposal


def _validate_phase_reference(cfg, proposal, documents):
    fields = {
        "semantic_interpretation": ("extraction_candidate_id", "claim_extraction"),
        "formalization": ("interpretation_candidate_id", "semantic_interpretation"),
        "rule_validity": ("formalization_candidate_id", "formalization"),
    }
    if proposal["phase"] == "claim_extraction":
        return
    scoped_documents = (
        documents if any(
            item.get("record_type") == "proposal"
            and item.get("proposal_id") == proposal["proposal_id"]
            for item in documents
        ) else [*documents, proposal]
    )
    phase_issues = [
        item for item in _cross_record_issues(scoped_documents)
        if item["path"] == proposal["proposal_id"]
        and item["code"].startswith("PHASE_REFERENCE_")
    ]
    if phase_issues:
        raise DeliberationError(
            "phase reference is not admissible: "
            + "; ".join(f"{item['code']}: {item['detail']}" for item in phase_issues)
        )
    field, _wanted_phase = fields[proposal["phase"]]
    upstream_id = proposal["payload"][field]
    upstream_set = next(
        item for item in documents
        if item.get("record_type") == "candidate_set"
        and upstream_id in item["candidate_ids"]
    )
    decision = next(
        item for item in documents
        if item.get("record_type") == "phase_decision"
        and item["candidate_set_id"] == upstream_set["candidate_set_id"]
    )
    live_findings = _phase_decision_live_findings(cfg, decision, documents)
    if live_findings:
        raise DeliberationError(
            "upstream phase decision is no longer current: "
            + "; ".join(f"{item['code']}: {item['detail']}" for item in live_findings)
        )


def freeze_candidate_set(cfg, entry, *, actor, recorded_at=None):
    """Freeze the complete current union of candidates for one exact subject/round."""
    if not isinstance(entry, dict) or set(entry) != {
            "schema_version", "round_id", "phase", "subject_key"}:
        raise DeliberationError("candidate-set request has unknown or missing fields")
    if entry["schema_version"] != CANDIDATE_SET_REQUEST_SCHEMA:
        raise DeliberationError("unsupported candidate-set request schema")
    round_id = _identifier(entry["round_id"], "round_id")
    phase = _phase(entry["phase"])
    subject_key = _identifier(entry["subject_key"], "subject_key")
    documents, issues = load_records(cfg)
    if issues:
        raise DeliberationError("deliberation store integrity failed before freeze")
    if any(
            item.get("record_type") == "candidate_set"
            and (item["round_id"], item["phase"], item["subject_key"])
            == (round_id, phase, subject_key)
            for item in documents):
        raise DeliberationError("this round/phase/subject already has a frozen candidate set")
    proposals = sorted(
        (
            item for item in documents
            if item.get("record_type") == "proposal"
            and (item["round_id"], item["phase"], item["subject_key"])
            == (round_id, phase, subject_key)
        ),
        key=lambda item: item["proposal_id"],
    )
    if not proposals:
        raise DeliberationError("cannot freeze an empty candidate set")
    candidate_ids = sorted({item["candidate_id"] for item in proposals})
    if len(candidate_ids) > MAX_CANDIDATES:
        raise DeliberationError("candidate set exceeds the deterministic candidate limit")
    raw = load_raw(cfg)
    current_snapshot = _project_snapshot(cfg, raw=raw)
    proposal_findings = [
        (item["proposal_id"], finding)
        for item in proposals
        for finding in _proposal_live_findings(
            cfg, item, current_snapshot, raw,
        )
        if finding["code"] != "MECHANICAL_VALIDATION_FAILED"
    ]
    if proposal_findings:
        raise DeliberationError(
            "one or more proposals failed live deterministic revalidation; "
            "start a new round from the current project: "
            + "; ".join(
                f"{proposal_id} {finding['code']}: {finding['detail']}"
                for proposal_id, finding in proposal_findings
            )
        )
    for proposal in proposals:
        if proposal["phase"] != "claim_extraction":
            _validate_phase_reference(cfg, proposal, documents)
    core = {
        "schema_version": CANDIDATE_SET_SCHEMA,
        "record_type": "candidate_set",
        "recorded_at": _timestamp(recorded_at),
        "round_id": round_id,
        "phase": phase,
        "subject_key": subject_key,
        "proposal_ids": [item["proposal_id"] for item in proposals],
        "candidate_ids": candidate_ids,
        "frozen_snapshot": current_snapshot,
        "actor": _identifier(actor, "actor"),
        "candidate_union_complete": True,
        "human_activation_required": True,
    }
    document = {
        "candidate_set_id": _content_id("deliberation-set", core), **core,
    }
    validate_record(document)
    return document


def _normal_evaluation(value):
    if not isinstance(value, dict) or set(value) != {
            "candidate_id", "decision", "reason_codes", "blocking", "rationale"}:
        raise DeliberationError("ballot evaluation has an invalid shape")
    candidate_id = value["candidate_id"]
    if not isinstance(candidate_id, str) or not CANDIDATE_ID_RE.fullmatch(candidate_id):
        raise DeliberationError("ballot candidate_id is invalid")
    decision = value["decision"]
    if decision not in DECISIONS:
        raise DeliberationError("ballot decision must be endorse, reject, or abstain")
    codes = value["reason_codes"]
    if (not isinstance(codes, list) or len(codes) > len(REASON_CODES)
            or any(not isinstance(code, str) or code not in REASON_CODES
                   for code in codes)
            or codes != sorted(set(codes))):
        raise DeliberationError("ballot reason_codes must be sorted, unique, and closed")
    if decision in {"reject", "abstain"} and not codes:
        raise DeliberationError("reject and abstain decisions require a reason code")
    if decision == "endorse" and codes:
        raise DeliberationError("endorse decisions cannot carry objection reason codes")
    if not isinstance(value["blocking"], bool):
        raise DeliberationError("ballot blocking must be a boolean")
    if decision == "endorse" and value["blocking"]:
        raise DeliberationError("an endorsement cannot be blocking")
    mandatory_blocking = {"MATERIAL_DISSENT", "CRITICAL_ABSTENTION"}
    if mandatory_blocking.intersection(codes) and not value["blocking"]:
        raise DeliberationError(
            "MATERIAL_DISSENT and CRITICAL_ABSTENTION must be blocking"
        )
    return {
        "candidate_id": candidate_id,
        "decision": decision,
        "reason_codes": list(codes),
        "blocking": value["blocking"],
        "rationale": _bounded_text(value["rationale"], "ballot rationale"),
    }


def create_ballot(cfg, entry, *, actor, independence_group, recorded_at=None):
    """Create one role-bound atomic ballot over every non-owned frozen candidate."""
    if not isinstance(entry, dict) or set(entry) not in (
            {"schema_version", "candidate_set_id", "role", "evaluations"},
            {"schema_version", "candidate_set_id", "role", "evaluations", "provenance"}):
        raise DeliberationError("ballot request has unknown or missing fields")
    if entry["schema_version"] != BALLOT_REQUEST_SCHEMA:
        raise DeliberationError("unsupported ballot-request schema")
    set_id = entry["candidate_set_id"]
    if not isinstance(set_id, str) or not CANDIDATE_SET_ID_RE.fullmatch(set_id):
        raise DeliberationError("candidate_set_id is invalid")
    role = entry["role"]
    if role not in REVIEW_ROLES:
        raise DeliberationError("ballot role is not a closed review role")
    documents, issues = load_records(cfg)
    if issues:
        raise DeliberationError("deliberation store integrity failed before ballot")
    candidate_sets = [
        item for item in documents
        if item.get("record_type") == "candidate_set"
        and item.get("candidate_set_id") == set_id
    ]
    if len(candidate_sets) != 1:
        raise DeliberationError("candidate_set_id does not resolve to one stored frozen set")
    candidate_set = candidate_sets[0]
    if any(
            item.get("record_type") == "phase_decision"
            and item["candidate_set_id"] == set_id
            for item in documents):
        raise DeliberationError(
            "candidate set already has a phase decision; late ballots are forbidden"
        )
    if role not in REQUIRED_ROLES[candidate_set["phase"]]:
        raise DeliberationError("ballot role is not eligible for this deliberation phase")
    actor_record = _actor(actor, independence_group, entry.get("provenance"))
    if any(
            item.get("record_type") == "ballot"
            and item["candidate_set_id"] == set_id
            and item["actor"]["id"] == actor_record["id"]
            for item in documents):
        raise DeliberationError("actor already submitted a ballot for this candidate set")
    own_candidates = {
        item["candidate_id"] for item in documents
        if item.get("record_type") == "proposal"
        and item["proposal_id"] in candidate_set["proposal_ids"]
        and item["actor"]["id"] == actor_record["id"]
    }
    evaluations = [_normal_evaluation(item) for item in entry["evaluations"]]
    evaluated = [item["candidate_id"] for item in evaluations]
    if len(evaluated) != len(set(evaluated)):
        raise DeliberationError("ballot evaluates a candidate more than once")
    required_candidates = set(candidate_set["candidate_ids"]) - own_candidates
    if set(evaluated) != required_candidates:
        raise DeliberationError(
            "ballot must evaluate every non-owned candidate and no owned candidate"
        )
    core = {
        "schema_version": BALLOT_SCHEMA,
        "record_type": "ballot",
        "recorded_at": _timestamp(recorded_at),
        "candidate_set_id": set_id,
        "role": role,
        "evaluations": sorted(evaluations, key=lambda item: item["candidate_id"]),
        "actor": actor_record,
    }
    document = {"ballot_id": _content_id("deliberation-ballot", core), **core}
    validate_record(document)
    return document


def create_phase_decision(cfg, entry, *, actor, recorded_at=None):
    """Create an attributed human-routing decision that activates no project state."""
    if not isinstance(entry, dict) or set(entry) != {
            "schema_version", "candidate_set_id", "candidate_id", "decision",
            "rationale"}:
        raise DeliberationError("phase-decision request has unknown or missing fields")
    if entry["schema_version"] != PHASE_DECISION_REQUEST_SCHEMA:
        raise DeliberationError("unsupported phase-decision request schema")
    set_id = entry["candidate_set_id"]
    candidate_id = entry["candidate_id"]
    if not isinstance(set_id, str) or not CANDIDATE_SET_ID_RE.fullmatch(set_id):
        raise DeliberationError("phase-decision candidate_set_id is invalid")
    if (not isinstance(candidate_id, str)
            or not CANDIDATE_ID_RE.fullmatch(candidate_id)):
        raise DeliberationError("phase-decision candidate_id is invalid")
    if entry["decision"] not in {"approved", "rejected"}:
        raise DeliberationError("phase decision must be approved or rejected")
    records, issues = load_records(cfg)
    if issues:
        raise DeliberationError("deliberation store integrity failed before phase decision")
    ballot_ids = sorted(
        item["ballot_id"] for item in records
        if item.get("record_type") == "ballot"
        and item["candidate_set_id"] == set_id
    )
    core = {
        "schema_version": PHASE_DECISION_SCHEMA,
        "record_type": "phase_decision",
        "recorded_at": _timestamp(recorded_at),
        "candidate_set_id": set_id,
        "candidate_id": candidate_id,
        "ballot_ids": ballot_ids,
        "decision": entry["decision"],
        "actor": _identifier(actor, "phase-decision actor"),
        "rationale": _bounded_text(entry["rationale"], "phase-decision rationale"),
        "human_identity_authenticated": False,
        "automatic_activation": False,
    }
    document = {
        "decision_id": _content_id("deliberation-decision", core), **core,
    }
    validate_record(document)
    findings = _phase_decision_live_findings(cfg, document, records)
    if findings:
        raise DeliberationError(
            "phase decision is not admissible: "
            + "; ".join(f"{item['code']}: {item['detail']}" for item in findings)
        )
    return document


def _validate_source_record(value):
    if not isinstance(value, dict) or set(value) != {
            "node_id", "path", "start_byte", "end_byte", "span_sha256",
            "source_sha256", "source_size", "excerpt"}:
        raise DeliberationError("stored source anchor has an invalid shape")
    _identifier(value["node_id"], "stored source node_id")
    _bounded_text(value["path"], "stored source path", 4_096)
    if (not isinstance(value["start_byte"], int)
            or isinstance(value["start_byte"], bool)
            or not isinstance(value["end_byte"], int)
            or isinstance(value["end_byte"], bool)
            or value["start_byte"] < 0
            or value["end_byte"] <= value["start_byte"]
            or value["end_byte"] - value["start_byte"] > MAX_SPAN_BYTES):
        raise DeliberationError("stored source byte bounds are invalid")
    if (not isinstance(value["source_size"], int)
            or isinstance(value["source_size"], bool)
            or value["source_size"] < value["end_byte"]):
        raise DeliberationError("stored source size is invalid")
    for key in ("span_sha256", "source_sha256"):
        if not isinstance(value[key], str) or not SHA256_RE.fullmatch(value[key]):
            raise DeliberationError(f"stored source {key} is invalid")
    excerpt = _bounded_text(value["excerpt"], "stored source excerpt", MAX_SPAN_BYTES)
    encoded = excerpt.encode("utf-8")
    if (len(encoded) != value["end_byte"] - value["start_byte"]
            or hashlib.sha256(encoded).hexdigest() != value["span_sha256"]):
        raise DeliberationError("stored source excerpt does not match its byte anchor")


def _validate_project_snapshot(value):
    if not isinstance(value, dict) or set(value) != {
            "graph_hash", "semantic_policy", "logic_assets"}:
        raise DeliberationError("mechanical project snapshot has an invalid shape")
    graph_hash = value["graph_hash"]
    if (not isinstance(graph_hash, str)
            or not re.fullmatch(r"graph:sha256:[0-9a-f]{64}", graph_hash)):
        raise DeliberationError("mechanical graph hash is invalid")
    policy = value["semantic_policy"]
    if not isinstance(policy, dict) or set(policy) != {
            "configured_policy_id", "active", "policy_sha256",
            "active_mapping_ids"}:
        raise DeliberationError("mechanical semantic-policy snapshot is invalid")
    configured_id = policy["configured_policy_id"]
    if configured_id is not None and (
            not isinstance(configured_id, str)
            or not semantics.POLICY_ID_RE.fullmatch(configured_id)):
        raise DeliberationError("mechanical active semantic policy id is invalid")
    if not isinstance(policy["active"], bool):
        raise DeliberationError("mechanical semantic-policy active state is invalid")
    policy_sha = policy["policy_sha256"]
    if policy_sha is not None and (
            not isinstance(policy_sha, str) or not SHA256_RE.fullmatch(policy_sha)):
        raise DeliberationError("mechanical semantic-policy hash is invalid")
    mapping_ids = policy["active_mapping_ids"]
    if (not isinstance(mapping_ids, list)
            or any(not isinstance(item, str)
                   or not semantics.MAPPING_ID_RE.fullmatch(item)
                   for item in mapping_ids)
            or mapping_ids != sorted(set(mapping_ids))):
        raise DeliberationError("mechanical active semantic mapping ids are invalid")
    if policy["active"]:
        if configured_id is None or policy_sha is None:
            raise DeliberationError("active semantic-policy snapshot is incomplete")
    elif configured_id is not None or policy_sha is not None or mapping_ids:
        raise DeliberationError("inactive semantic-policy snapshot must be empty")
    assets = value["logic_assets"]
    if not isinstance(assets, dict) or set(assets) != {"vocabularies", "rule_packs"}:
        raise DeliberationError("mechanical logic asset snapshot is invalid")
    for label in ("vocabularies", "rule_packs"):
        items = assets[label]
        if not isinstance(items, list) or len(items) > MAX_LIST:
            raise DeliberationError("mechanical logic asset list is invalid")
        keys = []
        for item in items:
            if (not isinstance(item, dict) or set(item) != {"id", "sha256"}
                    or not isinstance(item["sha256"], str)
                    or not SHA256_RE.fullmatch(item["sha256"])):
                raise DeliberationError("mechanical logic asset entry is invalid")
            keys.append(_identifier(item["id"], "logic asset id"))
        if keys != sorted(set(keys)):
            raise DeliberationError("mechanical logic assets must be sorted and unique")


def _validate_actor_record(value):
    if not isinstance(value, dict) or set(value) != {"id", "independence_group", "provenance"}:
        raise DeliberationError("actor record has an invalid shape")
    normalized = _actor(value["id"], value["independence_group"], value["provenance"])
    if normalized != value:
        raise DeliberationError("actor record is not canonical")


def _validate_stored_payload(phase, value):
    if not isinstance(value, dict):
        raise DeliberationError("stored proposal payload must be an object")
    expected = {
        "claim_extraction": {
            "claim_text", "claim_kind", "speech_act", "polarity", "qualifiers",
        },
        "semantic_interpretation": {
            "extraction_candidate_id", "normalized_claim", "frame", "modality",
            "quantifier", "negation_scope", "conditions", "ambiguities",
            "non_equivalences",
        },
        "formalization": {
            "interpretation_candidate_id", "vocabulary_id", "target", "evidence_plan",
            "method_requirements", "assumptions", "non_equivalences",
        },
        "rule_validity": {
            "formalization_candidate_id", "vocabulary_id", "rule_pack", "warrant",
            "scope", "assumptions", "non_equivalences", "competency_cases",
        },
    }[phase]
    if set(value) != expected:
        raise DeliberationError("stored proposal phase payload has an invalid shape")
    if phase == "claim_extraction":
        _bounded_text(value["claim_text"], "stored claim_text")
        if value["claim_kind"] not in CLAIM_KINDS or value["speech_act"] not in SPEECH_ACTS:
            raise DeliberationError("stored extraction kind or speech act is invalid")
        if value["polarity"] not in {"positive", "negative"}:
            raise DeliberationError("stored extraction polarity is invalid")
        _text_list(value["qualifiers"], "stored qualifiers")
    elif phase == "semantic_interpretation":
        if not CANDIDATE_ID_RE.fullmatch(str(value["extraction_candidate_id"])):
            raise DeliberationError("stored extraction candidate reference is invalid")
        _bounded_text(value["normalized_claim"], "stored normalized_claim")
        if value["modality"] not in MODALITIES:
            raise DeliberationError("stored modality is invalid")
        for key in ("conditions", "ambiguities", "non_equivalences"):
            _text_list(value[key], f"stored {key}")
        if not isinstance(value["frame"], dict) or set(value["frame"]) != {
                "population", "exposure", "comparator", "outcome", "direction",
                "magnitude", "time_scope", "inference_level"}:
            raise DeliberationError("stored semantic frame is invalid")
    elif phase == "formalization":
        if not CANDIDATE_ID_RE.fullmatch(str(value["interpretation_candidate_id"])):
            raise DeliberationError("stored interpretation candidate reference is invalid")
        _identifier(value["vocabulary_id"], "stored vocabulary_id")
        _normal_method_requirements(value["method_requirements"])
        _text_list(value["assumptions"], "stored assumptions")
        _text_list(value["non_equivalences"], "stored non_equivalences", required=True)
        if not isinstance(value["target"], dict) or not isinstance(value["evidence_plan"], dict):
            raise DeliberationError("stored target or evidence plan is invalid")
    else:
        if not CANDIDATE_ID_RE.fullmatch(str(value["formalization_candidate_id"])):
            raise DeliberationError("stored formalization candidate reference is invalid")
        _identifier(value["vocabulary_id"], "stored vocabulary_id")
        for key in ("warrant", "scope"):
            _bounded_text(value[key], f"stored {key}")
        for key in ("assumptions", "non_equivalences"):
            _text_list(value[key], f"stored {key}", required=key == "non_equivalences")
        cases = value["competency_cases"]
        if not isinstance(cases, list) or not cases:
            raise DeliberationError("stored competency cases are invalid")
        categories = set()
        for case in cases:
            if not isinstance(case, dict) or set(case) != {
                    "id", "category", "input_atoms", "target", "expected_state",
                    "observed_state", "passed"}:
                raise DeliberationError("stored competency case has an invalid shape")
            categories.add(case["category"])
            if not isinstance(case["passed"], bool):
                raise DeliberationError("stored competency passed flag is invalid")
        if categories != MANDATORY_COMPETENCY_CATEGORIES:
            raise DeliberationError("stored competency matrix is incomplete")


def validate_record(document):
    """Validate one immutable ledger record and its content address."""
    if not isinstance(document, dict):
        raise DeliberationError("deliberation record must be an object")
    record_type = document.get("record_type")
    if record_type == "proposal":
        expected = {
            "proposal_id", "candidate_id", "schema_version", "record_type",
            "recorded_at", "round_id", "phase", "subject_key", "source_anchor",
            "mechanical_snapshot", "payload", "mechanical_validation", "actor",
            "rationale",
        }
        if set(document) != expected or document["schema_version"] != PROPOSAL_SCHEMA:
            raise DeliberationError("proposal record has an invalid shape or schema")
        if not PROPOSAL_ID_RE.fullmatch(str(document["proposal_id"])):
            raise DeliberationError("proposal_id is invalid")
        if not CANDIDATE_ID_RE.fullmatch(str(document["candidate_id"])):
            raise DeliberationError("candidate_id is invalid")
        _timestamp(document["recorded_at"])
        _identifier(document["round_id"], "round_id")
        phase = _phase(document["phase"])
        _identifier(document["subject_key"], "subject_key")
        _validate_source_record(document["source_anchor"])
        _validate_project_snapshot(document["mechanical_snapshot"])
        _validate_stored_payload(phase, document["payload"])
        mechanical = document["mechanical_validation"]
        if (not isinstance(mechanical, dict) or not isinstance(mechanical.get("passed"), bool)
                or not isinstance(mechanical.get("checks"), list)):
            raise DeliberationError("mechanical_validation is invalid")
        _validate_actor_record(document["actor"])
        _bounded_text(document["rationale"], "rationale")
        expected_candidate = _content_id(
            "deliberation-candidate", _proposal_candidate_core(document),
        )
        if document["candidate_id"] != expected_candidate:
            raise DeliberationError("candidate_id does not match proposal meaning")
        core = {key: document[key] for key in document if key != "proposal_id"}
        if document["proposal_id"] != _content_id("deliberation-proposal", core):
            raise DeliberationError("proposal_id does not match record content")
        return document["proposal_id"]

    if record_type == "candidate_set":
        expected = {
            "candidate_set_id", "schema_version", "record_type", "recorded_at",
            "round_id", "phase", "subject_key", "proposal_ids", "candidate_ids",
            "frozen_snapshot", "actor", "candidate_union_complete",
            "human_activation_required",
        }
        if set(document) != expected or document["schema_version"] != CANDIDATE_SET_SCHEMA:
            raise DeliberationError("candidate-set record has an invalid shape or schema")
        if not CANDIDATE_SET_ID_RE.fullmatch(str(document["candidate_set_id"])):
            raise DeliberationError("candidate_set_id is invalid")
        _timestamp(document["recorded_at"])
        _identifier(document["round_id"], "round_id")
        _phase(document["phase"])
        _identifier(document["subject_key"], "subject_key")
        _identifier(document["actor"], "actor")
        for key, matcher in (
                ("proposal_ids", PROPOSAL_ID_RE), ("candidate_ids", CANDIDATE_ID_RE)):
            values = document[key]
            if (not isinstance(values, list) or not values or len(values) > MAX_CANDIDATES * 16
                    or any(not isinstance(item, str) or not matcher.fullmatch(item)
                           for item in values)
                    or values != sorted(set(values))):
                raise DeliberationError(f"candidate set {key} is invalid")
        _validate_project_snapshot(document["frozen_snapshot"])
        if document["candidate_union_complete"] is not True:
            raise DeliberationError("candidate-set union must be explicitly complete")
        if document["human_activation_required"] is not True:
            raise DeliberationError("candidate set cannot waive human activation")
        core = {key: document[key] for key in document if key != "candidate_set_id"}
        if document["candidate_set_id"] != _content_id("deliberation-set", core):
            raise DeliberationError("candidate_set_id does not match record content")
        return document["candidate_set_id"]

    if record_type == "ballot":
        expected = {
            "ballot_id", "schema_version", "record_type", "recorded_at",
            "candidate_set_id", "role", "evaluations", "actor",
        }
        if set(document) != expected or document["schema_version"] != BALLOT_SCHEMA:
            raise DeliberationError("ballot record has an invalid shape or schema")
        if not BALLOT_ID_RE.fullmatch(str(document["ballot_id"])):
            raise DeliberationError("ballot_id is invalid")
        if not CANDIDATE_SET_ID_RE.fullmatch(str(document["candidate_set_id"])):
            raise DeliberationError("ballot candidate_set_id is invalid")
        _timestamp(document["recorded_at"])
        if document["role"] not in REVIEW_ROLES:
            raise DeliberationError("ballot role is invalid")
        if not isinstance(document["evaluations"], list):
            raise DeliberationError("ballot evaluations must be a list")
        evaluations = [_normal_evaluation(item) for item in document["evaluations"]]
        if evaluations != document["evaluations"]:
            raise DeliberationError("ballot evaluations are not canonical")
        _validate_actor_record(document["actor"])
        core = {key: document[key] for key in document if key != "ballot_id"}
        if document["ballot_id"] != _content_id("deliberation-ballot", core):
            raise DeliberationError("ballot_id does not match record content")
        return document["ballot_id"]
    if record_type == "phase_decision":
        expected = {
            "decision_id", "schema_version", "record_type", "recorded_at",
            "candidate_set_id", "candidate_id", "ballot_ids", "decision",
            "actor", "rationale", "human_identity_authenticated",
            "automatic_activation",
        }
        if (set(document) != expected
                or document["schema_version"] != PHASE_DECISION_SCHEMA):
            raise DeliberationError(
                "phase-decision record has an invalid shape or schema"
            )
        if not PHASE_DECISION_ID_RE.fullmatch(str(document["decision_id"])):
            raise DeliberationError("phase-decision id is invalid")
        if not CANDIDATE_SET_ID_RE.fullmatch(str(document["candidate_set_id"])):
            raise DeliberationError("phase-decision candidate_set_id is invalid")
        if not CANDIDATE_ID_RE.fullmatch(str(document["candidate_id"])):
            raise DeliberationError("phase-decision candidate_id is invalid")
        _timestamp(document["recorded_at"])
        ballot_ids = document["ballot_ids"]
        if (not isinstance(ballot_ids, list) or not ballot_ids
                or len(ballot_ids) > MAX_LIST
                or any(not isinstance(item, str) or not BALLOT_ID_RE.fullmatch(item)
                       for item in ballot_ids)
                or ballot_ids != sorted(set(ballot_ids))):
            raise DeliberationError("phase-decision ballot_ids are invalid")
        if document["decision"] not in {"approved", "rejected"}:
            raise DeliberationError("phase-decision value is invalid")
        _identifier(document["actor"], "phase-decision actor")
        _bounded_text(document["rationale"], "phase-decision rationale")
        if document["human_identity_authenticated"] is not False:
            raise DeliberationError(
                "phase decision cannot claim authenticated human identity"
            )
        if document["automatic_activation"] is not False:
            raise DeliberationError("phase decision cannot activate project state")
        core = {key: document[key] for key in document if key != "decision_id"}
        if document["decision_id"] != _content_id("deliberation-decision", core):
            raise DeliberationError("phase-decision id does not match record content")
        return document["decision_id"]
    raise DeliberationError("unknown deliberation record_type")


def deliberation_path(cfg) -> Path:
    return Path(cfg.deliberation_path)


def _record_id(document):
    return {
        "proposal": document.get("proposal_id"),
        "candidate_set": document.get("candidate_set_id"),
        "ballot": document.get("ballot_id"),
        "phase_decision": document.get("decision_id"),
    }.get(document.get("record_type"))


def _record_path(root, record_id):
    return root / (record_id.rsplit(":", 1)[1] + ".json")


def _issue(code, detail, path="-"):
    return {"code": code, "detail": str(detail), "path": str(path)}


def _directory_signature(entries):
    signature = []
    for path in entries:
        try:
            info = path.lstat()
            identity = (
                info.st_dev, info.st_ino, info.st_mode, info.st_size,
                getattr(info, "st_mtime_ns", int(info.st_mtime * 1e9)),
            )
        except OSError as exc:
            identity = ("unreadable", type(exc).__name__, str(exc))
        signature.append((path.name, identity))
    return tuple(signature)


def _existing_store_ancestor_is_unsafe(cfg, root):
    base = Path(os.path.abspath(cfg.base))
    root = Path(os.path.abspath(root))
    try:
        root.relative_to(base)
    except ValueError:
        return True
    current = root
    while not current.exists() and current != base:
        current = current.parent
    return events._path_has_reparse_component(current)


def _stored_panel_recommendation(candidate_set, proposals, ballots):
    """Reconstruct the stored procedural recommendation without live project state."""
    if len(candidate_set["candidate_ids"]) != 1:
        return None
    candidate_id = candidate_set["candidate_ids"][0]
    selected = [
        item for item in proposals
        if item["proposal_id"] in candidate_set["proposal_ids"]
        and item["candidate_id"] == candidate_id
    ]
    if not selected or any(
            not item["mechanical_validation"]["passed"] for item in selected):
        return None
    proposer_groups = {item["actor"]["independence_group"] for item in selected}
    evaluations = []
    for ballot in ballots:
        if ballot["candidate_set_id"] != candidate_set["candidate_set_id"]:
            continue
        evaluation = next((
            item for item in ballot["evaluations"]
            if item["candidate_id"] == candidate_id
        ), None)
        if evaluation is not None:
            evaluations.append({
                **evaluation,
                "role": ballot["role"],
                "independence_group": ballot["actor"]["independence_group"],
            })
    decisions_by_group = {}
    for item in evaluations:
        decisions_by_group.setdefault(item["independence_group"], []).append(item)
    endorsement_groups = {
        group for group, items in decisions_by_group.items()
        if all(item["decision"] == "endorse" for item in items)
    }
    reject_groups = {
        group for group, items in decisions_by_group.items()
        if any(item["decision"] == "reject" for item in items)
    }
    external_groups = set(decisions_by_group) - proposer_groups
    external_endorsements = endorsement_groups & external_groups
    external_rejects = reject_groups & external_groups
    endorsing_evaluations = [
        item for item in evaluations
        if item["independence_group"] in external_endorsements
        and item["decision"] == "endorse"
    ]
    required_roles = set(REQUIRED_ROLES[candidate_set["phase"]])
    endorsed_roles = {item["role"] for item in endorsing_evaluations}
    nonabstaining = external_endorsements | external_rejects
    participant_groups = proposer_groups | set(decisions_by_group)
    quorum_shape = (
        (len(proposer_groups) >= 2 and len(external_endorsements) >= 1)
        or (len(proposer_groups) >= 1 and len(external_endorsements) >= 2)
    )
    eligible = (
        len(participant_groups) >= 3
        and quorum_shape
        and required_roles <= endorsed_roles
        and _distinct_role_group_coverage(endorsing_evaluations, required_roles)
        and not any(item["blocking"] for item in evaluations)
        and bool(nonabstaining)
        and 3 * len(external_endorsements) >= 2 * len(nonabstaining)
    )
    return candidate_id if eligible else None


def _cross_record_issues(documents):
    issues = []
    by_record_id = {}
    actor_groups = {}
    proposals = []
    sets = []
    ballots = []
    decisions = []
    for document in documents:
        record_id = _record_id(document)
        if record_id in by_record_id:
            issues.append(_issue("DUPLICATE_RECORD_ID", record_id))
        by_record_id[record_id] = document
        if document["record_type"] in {"proposal", "ballot"}:
            actor = document["actor"]["id"]
            group = document["actor"]["independence_group"]
            previous = actor_groups.setdefault(actor, group)
            if previous != group:
                issues.append(_issue(
                    "ACTOR_GROUP_DRIFT",
                    f"actor {actor!r} used both {previous!r} and {group!r}",
                ))
        {
            "proposal": proposals, "candidate_set": sets, "ballot": ballots,
            "phase_decision": decisions,
        }[
            document["record_type"]
        ].append(document)

    proposal_by_id = {item["proposal_id"]: item for item in proposals}
    sets_by_id = {item["candidate_set_id"]: item for item in sets}
    set_keys = {}
    for candidate_set in sets:
        key = (
            candidate_set["round_id"], candidate_set["phase"],
            candidate_set["subject_key"],
        )
        if key in set_keys:
            issues.append(_issue("DUPLICATE_FROZEN_SET", repr(key)))
        set_keys[key] = candidate_set["candidate_set_id"]
        selected = []
        missing = []
        for proposal_id in candidate_set["proposal_ids"]:
            proposal = proposal_by_id.get(proposal_id)
            if proposal is None:
                missing.append(proposal_id)
                continue
            selected.append(proposal)
        if missing:
            issues.append(_issue(
                "CANDIDATE_SET_MISSING_PROPOSAL", ", ".join(sorted(missing)),
                candidate_set["candidate_set_id"],
            ))
            continue
        if any(
                (item["round_id"], item["phase"], item["subject_key"]) != key
                for item in selected):
            issues.append(_issue(
                "CANDIDATE_SET_SCOPE_MISMATCH",
                "frozen proposal does not match round/phase/subject",
                candidate_set["candidate_set_id"],
            ))
        if any(
                item["mechanical_snapshot"] != candidate_set["frozen_snapshot"]
                for item in selected):
            issues.append(_issue(
                "CANDIDATE_SET_SNAPSHOT_MISMATCH",
                "frozen snapshot does not equal every selected proposal snapshot",
                candidate_set["candidate_set_id"],
            ))
        complete = sorted(
            item["proposal_id"] for item in proposals
            if (item["round_id"], item["phase"], item["subject_key"]) == key
        )
        if complete != candidate_set["proposal_ids"]:
            issues.append(_issue(
                "CANDIDATE_UNION_INCOMPLETE",
                "frozen set omits or predates a proposal in the same round",
                candidate_set["candidate_set_id"],
            ))
        candidate_ids = sorted({item["candidate_id"] for item in selected})
        if candidate_ids != candidate_set["candidate_ids"]:
            issues.append(_issue(
                "CANDIDATE_ID_UNION_MISMATCH",
                "candidate_ids do not equal proposal meaning union",
                candidate_set["candidate_set_id"],
            ))

    ballot_keys = set()
    for ballot in ballots:
        candidate_set = sets_by_id.get(ballot["candidate_set_id"])
        if candidate_set is None:
            issues.append(_issue(
                "BALLOT_SET_MISSING", ballot["candidate_set_id"], ballot["ballot_id"],
            ))
            continue
        key = (ballot["candidate_set_id"], ballot["actor"]["id"])
        if key in ballot_keys:
            issues.append(_issue(
                "DUPLICATE_ACTOR_BALLOT", repr(key), ballot["ballot_id"],
            ))
        ballot_keys.add(key)
        if ballot["role"] not in REQUIRED_ROLES[candidate_set["phase"]]:
            issues.append(_issue(
                "INELIGIBLE_BALLOT_ROLE", ballot["role"], ballot["ballot_id"],
            ))
        own = {
            proposal_by_id[proposal_id]["candidate_id"]
            for proposal_id in candidate_set["proposal_ids"]
            if proposal_id in proposal_by_id
            and proposal_by_id[proposal_id]["actor"]["id"] == ballot["actor"]["id"]
        }
        evaluated = {item["candidate_id"] for item in ballot["evaluations"]}
        wanted = set(candidate_set["candidate_ids"]) - own
        if evaluated != wanted:
            issues.append(_issue(
                "BALLOT_COVERAGE_INVALID",
                "ballot must evaluate every non-owned candidate exactly once",
                ballot["ballot_id"],
            ))

    decision_sets = set()
    decisions_by_set = {}
    for decision in decisions:
        set_id = decision["candidate_set_id"]
        candidate_set = sets_by_id.get(set_id)
        if set_id in decision_sets:
            issues.append(_issue(
                "DUPLICATE_PHASE_DECISION", set_id, decision["decision_id"],
            ))
        decision_sets.add(set_id)
        decisions_by_set.setdefault(set_id, []).append(decision)
        if candidate_set is None:
            issues.append(_issue(
                "PHASE_DECISION_SET_MISSING", set_id, decision["decision_id"],
            ))
            continue
        if decision["candidate_id"] not in candidate_set["candidate_ids"]:
            issues.append(_issue(
                "PHASE_DECISION_CANDIDATE_MISMATCH",
                decision["candidate_id"], decision["decision_id"],
            ))
        stored_recommendation = _stored_panel_recommendation(
            candidate_set, proposals, ballots,
        )
        if stored_recommendation != decision["candidate_id"]:
            issues.append(_issue(
                "PHASE_DECISION_PANEL_NOT_RECOMMENDED",
                "stored panel did not have exactly one procedurally eligible candidate",
                decision["decision_id"],
            ))
        set_ballots = sorted(
            item["ballot_id"] for item in ballots
            if item["candidate_set_id"] == set_id
        )
        if decision["ballot_ids"] != set_ballots:
            issues.append(_issue(
                "PHASE_DECISION_BALLOT_SET_MISMATCH",
                "decision does not pin the complete current ballot set",
                decision["decision_id"],
            ))
            late_ballots = sorted(
                item["ballot_id"] for item in ballots
                if item["candidate_set_id"] == set_id
                and item["ballot_id"] not in decision["ballot_ids"]
            )
            for ballot_id in late_ballots:
                issues.append(_issue(
                    "LATE_BALLOT_AFTER_PHASE_DECISION",
                    "ballot was not part of the immutable decision snapshot",
                    ballot_id,
                ))
        selected_proposals = [
            proposal_by_id[item] for item in candidate_set["proposal_ids"]
            if item in proposal_by_id
        ]
        excluded_actors = {
            item["actor"]["id"] for item in selected_proposals
        } | {
            item["actor"]["id"] for item in ballots
            if item["candidate_set_id"] == set_id
        }
        if decision["actor"] in excluded_actors:
            issues.append(_issue(
                "PHASE_DECISION_ACTOR_CONFLICT",
                "decision actor also proposed or balloted in this panel",
                decision["decision_id"],
            ))
        predecessor_times = [candidate_set["recorded_at"], *(
            item["recorded_at"] for item in ballots
            if item["candidate_set_id"] == set_id
        )]
        if any(
                _timestamp_value(decision["recorded_at"]) < _timestamp_value(value)
                for value in predecessor_times):
            issues.append(_issue(
                "PHASE_DECISION_TIME_INVALID",
                "decision predates its frozen set or a pinned ballot",
                decision["decision_id"],
            ))

    candidates = {item["candidate_id"]: item for item in proposals}
    references = {
        "semantic_interpretation": ("extraction_candidate_id", "claim_extraction"),
        "formalization": ("interpretation_candidate_id", "semantic_interpretation"),
        "rule_validity": ("formalization_candidate_id", "formalization"),
    }
    for proposal in proposals:
        if proposal["phase"] == "claim_extraction":
            continue
        field, wanted_phase = references[proposal["phase"]]
        target = candidates.get(proposal["payload"][field])
        if target is None:
            issues.append(_issue(
                "PHASE_REFERENCE_MISSING", field, proposal["proposal_id"],
            ))
            continue
        if target["phase"] != wanted_phase:
            issues.append(_issue(
                "PHASE_REFERENCE_WRONG_PHASE", field, proposal["proposal_id"],
            ))
        if target["round_id"] != proposal["round_id"]:
            issues.append(_issue(
                "PHASE_REFERENCE_ROUND_MISMATCH", field, proposal["proposal_id"],
            ))
        if target["subject_key"] != proposal["subject_key"]:
            issues.append(_issue(
                "PHASE_REFERENCE_SUBJECT_MISMATCH", field, proposal["proposal_id"],
            ))
        if target["mechanical_snapshot"] != proposal["mechanical_snapshot"]:
            issues.append(_issue(
                "PHASE_REFERENCE_SNAPSHOT_MISMATCH", field, proposal["proposal_id"],
            ))
        target_sets = [
            item for item in sets if target["candidate_id"] in item["candidate_ids"]
        ]
        if len(target_sets) != 1:
            issues.append(_issue(
                "PHASE_REFERENCE_SET_MISSING",
                "upstream candidate must belong to exactly one frozen set",
                proposal["proposal_id"],
            ))
            continue
        approvals = [
            item for item in decisions_by_set.get(
                target_sets[0]["candidate_set_id"], []
            )
            if item["candidate_id"] == target["candidate_id"]
            and item["decision"] == "approved"
        ]
        if len(approvals) != 1:
            issues.append(_issue(
                "PHASE_REFERENCE_NOT_APPROVED", field, proposal["proposal_id"],
            ))
    return sorted(issues, key=lambda item: (item["code"], item["path"], item["detail"]))


def _load_records_unlocked(cfg):
    """Load all immutable records, returning fail-closed integrity issues separately."""
    root = deliberation_path(cfg)
    if not root.exists():
        return [], []
    if events._path_has_reparse_component(root) or not root.is_dir():
        return [], [_issue(
            "DELIBERATION_STORE_UNSAFE",
            "store must be a regular non-link directory", root,
        )]
    documents = []
    issues = []
    total = 0
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        return [], [_issue("DELIBERATION_STORE_UNREADABLE", exc, root)]
    initial_signature = _directory_signature(entries)
    if len(entries) > MAX_RECORDS:
        issues.append(_issue(
            "DELIBERATION_STORE_LIMIT", "record-count limit exceeded", root,
        ))
        entries = entries[:MAX_RECORDS]
    seen_ids = set()
    for path in entries:
        try:
            info = path.lstat()
        except OSError as exc:
            issues.append(_issue("DELIBERATION_ENTRY_UNREADABLE", exc, path.name))
            continue
        if (path.suffix != ".json" or stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or events._path_has_reparse_component(path)):
            issues.append(_issue(
                "DELIBERATION_ENTRY_UNEXPECTED",
                "entry must be a flat regular non-link .json file", path.name,
            ))
            continue
        try:
            data = events._stable_bounded_bytes(path, MAX_DOCUMENT_BYTES)
            total += len(data)
            if total > MAX_STORE_BYTES:
                raise DeliberationError("aggregate store byte limit exceeded")
            document = strict_json_loads(data.decode("utf-8-sig"), path.name)
            record_id = validate_record(document)
            if path.name != record_id.rsplit(":", 1)[1] + ".json":
                raise DeliberationError("filename does not match content address")
            if record_id in seen_ids:
                raise DeliberationError("duplicate logical record id")
            seen_ids.add(record_id)
            documents.append(document)
        except (OSError, UnicodeError, TypeError, ValueError, RecursionError,
                DeliberationError, events.EventError) as exc:
            issues.append(_issue("DELIBERATION_RECORD_INVALID", exc, path.name))
    try:
        final_entries = sorted(root.iterdir(), key=lambda item: item.name)
        if _directory_signature(final_entries) != initial_signature:
            issues.append(_issue(
                "DELIBERATION_STORE_CHANGED_DURING_READ",
                "ledger directory changed while a read snapshot was being built",
                root,
            ))
    except OSError as exc:
        issues.append(_issue("DELIBERATION_STORE_UNREADABLE", exc, root))
    documents.sort(key=lambda item: (_record_id(item), _canonical_bytes(item)))
    issues.extend(_cross_record_issues(documents))
    return documents, sorted(
        issues, key=lambda item: (item["code"], item["path"], item["detail"]),
    )


def load_records(cfg):
    """Load one lock-consistent ledger snapshot and return integrity issues."""
    root = deliberation_path(cfg)
    try:
        with events._event_lock(root):
            return _load_records_unlocked(cfg)
    except events.EventError as exc:
        return [], [_issue(
            "DELIBERATION_STORE_LOCK_FAILED",
            f"cannot acquire deliberation store lock: {exc}", root,
        )]


def append_record(cfg, document):
    """Atomically append one immutable record after complete-store preflight."""
    record_id = validate_record(document)
    payload = _canonical_bytes(document) + b"\n"
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise DeliberationError("deliberation record exceeds the document byte limit")
    root = deliberation_path(cfg)
    try:
        with events._event_lock(root):
            if _existing_store_ancestor_is_unsafe(cfg, root):
                raise DeliberationError(
                    "deliberation store traverses a link, junction, or path escape"
                )
            root.mkdir(parents=True, exist_ok=True)
            if events._path_has_reparse_component(root) or not root.is_dir():
                raise DeliberationError("deliberation store is not a safe directory")
            documents, issues = _load_records_unlocked(cfg)
            if issues:
                raise DeliberationError(
                    "deliberation store integrity failed before append: "
                    + "; ".join(item["detail"] for item in issues)
                )
            destination = _record_path(root, record_id)
            if destination.exists():
                existing = events._stable_bounded_bytes(destination, MAX_DOCUMENT_BYTES)
                if existing.rstrip(b"\r\n") != payload.rstrip(b"\r\n"):
                    raise DeliberationError("refusing to overwrite a conflicting record")
                return destination
            hypothetical_issues = _cross_record_issues([*documents, document])
            if hypothetical_issues:
                raise DeliberationError(
                    "record would violate ledger integrity: "
                    + "; ".join(item["detail"] for item in hypothetical_issues)
                )
            if (document["record_type"] == "proposal"
                    and document["phase"] != "claim_extraction"):
                _validate_phase_reference(cfg, document, documents)
            if document["record_type"] == "phase_decision":
                decision_findings = _phase_decision_live_findings(
                    cfg, document, [*documents, document],
                )
                if decision_findings:
                    raise DeliberationError(
                        "phase decision failed live deterministic validation: "
                        + "; ".join(
                            f"{item['code']}: {item['detail']}"
                            for item in decision_findings
                        )
                    )
            proposals_to_revalidate = []
            if document["record_type"] == "proposal":
                proposals_to_revalidate = [document]
            elif document["record_type"] == "candidate_set":
                selected_ids = set(document["proposal_ids"])
                proposals_to_revalidate = [
                    item for item in documents
                    if item.get("record_type") == "proposal"
                    and item["proposal_id"] in selected_ids
                ]
            if proposals_to_revalidate:
                for proposal in proposals_to_revalidate:
                    if proposal["phase"] != "claim_extraction":
                        _validate_phase_reference(cfg, proposal, documents)
                try:
                    raw = load_raw(cfg)
                    current_snapshot = _project_snapshot(cfg, raw=raw)
                except (OSError, ValueError, DeliberationError) as exc:
                    raise DeliberationError(
                        f"cannot revalidate candidate against the current project: {exc}"
                    ) from exc
                live_findings = [
                    (item["proposal_id"], finding)
                    for item in proposals_to_revalidate
                    for finding in _proposal_live_findings(
                        cfg, item, current_snapshot, raw,
                    )
                    if finding["code"] != "MECHANICAL_VALIDATION_FAILED"
                ]
                if live_findings:
                    raise DeliberationError(
                        "candidate failed live deterministic revalidation before append: "
                        + "; ".join(
                            f"{proposal_id} {finding['code']}: {finding['detail']}"
                            for proposal_id, finding in live_findings
                        )
                    )
            existing_bytes = sum(len(_canonical_bytes(item)) + 1 for item in documents)
            if len(documents) >= MAX_RECORDS or existing_bytes + len(payload) > MAX_STORE_BYTES:
                raise DeliberationError("deliberation store limit would be exceeded")
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{root.name}-", suffix=".tmp", dir=root.parent,
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary_name, destination)
                except FileExistsError as exc:
                    raise DeliberationError(
                        "record appeared concurrently; refusing to overwrite it"
                    ) from exc
                events._fsync_directory(root)
            finally:
                try:
                    Path(temporary_name).unlink()
                    events._fsync_directory(root.parent)
                except FileNotFoundError:
                    pass
            return destination
    except events.EventError as exc:
        raise DeliberationError(f"cannot acquire deliberation store lock: {exc}") from exc


def _proposal_live_findings(cfg, proposal, current_snapshot, raw):
    findings = []
    snapshot_current = proposal["mechanical_snapshot"] == current_snapshot
    if not snapshot_current:
        findings.append({
            "code": "PROJECT_SNAPSHOT_DRIFT",
            "detail": "graph, active semantic policy, or configured logic assets changed",
        })
    stored = proposal["source_anchor"]
    source_current = False
    current = None
    try:
        current = _source_snapshot(cfg, {
            "node_id": stored["node_id"],
            "start_byte": stored["start_byte"],
            "end_byte": stored["end_byte"],
            "span_sha256": stored["span_sha256"],
        }, raw=raw)
        if current != stored:
            findings.append({
                "code": "SOURCE_ANCHOR_DRIFT",
                "detail": "source path, bytes, size, or exact excerpt changed",
            })
        else:
            source_current = True
    except DeliberationError as exc:
        findings.append({"code": "SOURCE_ANCHOR_INVALID", "detail": str(exc)})
    if snapshot_current and source_current:
        try:
            normalized_payload, mechanical_validation = _normal_payload(
                cfg, proposal["phase"], proposal["payload"], current, raw=raw,
            )
        except DeliberationError as exc:
            findings.append({
                "code": "MECHANICAL_REVALIDATION_FAILED",
                "detail": str(exc),
            })
        else:
            if normalized_payload != proposal["payload"]:
                findings.append({
                    "code": "PAYLOAD_NOT_CANONICAL",
                    "detail": "stored candidate payload differs from deterministic normalization",
                })
            if mechanical_validation != proposal["mechanical_validation"]:
                findings.append({
                    "code": "MECHANICAL_REVALIDATION_MISMATCH",
                    "detail": (
                        "stored deterministic checks differ from a fresh evaluation "
                        "against the frozen project snapshot"
                    ),
                })
    if not proposal["mechanical_validation"]["passed"]:
        findings.append({
            "code": "MECHANICAL_VALIDATION_FAILED",
            "detail": "candidate failed its deterministic syntax or competency gates",
        })
    return findings


def _distinct_role_group_coverage(evaluations, required_roles):
    """Return true only when required roles have a distinct endorsing group assignment."""
    choices = {
        role: sorted({
            item["independence_group"] for item in evaluations
            if item["role"] == role and item["decision"] == "endorse"
        })
        for role in sorted(required_roles)
    }

    def assign(roles, used):
        if not roles:
            return True
        role = min(roles, key=lambda item: (len(choices[item]), item))
        remaining = [item for item in roles if item != role]
        return any(
            group not in used and assign(remaining, used | {group})
            for group in choices[role]
        )

    return assign(list(choices), set())


def _phase_decision_live_findings(cfg, decision, records):
    findings = []
    set_id = decision["candidate_set_id"]
    candidate_sets = [
        item for item in records
        if item.get("record_type") == "candidate_set"
        and item["candidate_set_id"] == set_id
    ]
    if len(candidate_sets) != 1:
        return [{
            "code": "PHASE_DECISION_SET_MISSING",
            "detail": "candidate_set_id does not resolve to one frozen set",
        }]
    candidate_set = candidate_sets[0]
    ballots = sorted(
        (
            item for item in records if item.get("record_type") == "ballot"
            and item["candidate_set_id"] == set_id
        ),
        key=lambda item: item["ballot_id"],
    )
    ballot_ids = [item["ballot_id"] for item in ballots]
    if decision["ballot_ids"] != ballot_ids:
        findings.append({
            "code": "PHASE_DECISION_BALLOT_SET_MISMATCH",
            "detail": "decision does not pin the complete current ballot set",
        })
    selected_proposals = [
        item for item in records if item.get("record_type") == "proposal"
        and item["proposal_id"] in candidate_set["proposal_ids"]
    ]
    excluded_actors = {
        item["actor"]["id"] for item in selected_proposals
    } | {item["actor"]["id"] for item in ballots}
    if decision["actor"] in excluded_actors:
        findings.append({
            "code": "PHASE_DECISION_ACTOR_CONFLICT",
            "detail": "decision actor also proposed or balloted in this panel",
        })
    predecessor_times = [
        candidate_set["recorded_at"], *(item["recorded_at"] for item in ballots),
    ]
    if any(
            _timestamp_value(decision["recorded_at"]) < _timestamp_value(value)
            for value in predecessor_times):
        findings.append({
            "code": "PHASE_DECISION_TIME_INVALID",
            "detail": "decision predates its frozen set or a pinned ballot",
        })
    other_decisions = [
        item for item in records if item.get("record_type") == "phase_decision"
        and item["candidate_set_id"] == set_id
        and item["decision_id"] != decision["decision_id"]
    ]
    if other_decisions:
        findings.append({
            "code": "DUPLICATE_PHASE_DECISION",
            "detail": "candidate set already has a different phase decision",
        })
    try:
        panel = evaluate_candidate_set(cfg, set_id, records=records)
    except DeliberationError as exc:
        findings.append({
            "code": "PHASE_DECISION_PANEL_UNAVAILABLE", "detail": str(exc),
        })
    else:
        if panel["status"] != "recommended_for_human_review":
            findings.append({
                "code": "PHASE_DECISION_PANEL_NOT_RECOMMENDED",
                "detail": f"current panel state is {panel['status']}",
            })
        if panel["recommended_candidate_id"] != decision["candidate_id"]:
            findings.append({
                "code": "PHASE_DECISION_CANDIDATE_MISMATCH",
                "detail": "candidate_id is not the current procedural recommendation",
            })
    return sorted(findings, key=lambda item: (item["code"], item["detail"]))


def evaluate_candidate_set(
        cfg, candidate_set_id, records=None, *, preloaded_integrity_issues=None):
    """Evaluate one frozen panel without activating any meaning-bearing policy."""
    if (not isinstance(candidate_set_id, str)
            or not CANDIDATE_SET_ID_RE.fullmatch(candidate_set_id)):
        raise DeliberationError("candidate_set_id is invalid")
    if records is None:
        if preloaded_integrity_issues is not None:
            raise DeliberationError(
                "preloaded_integrity_issues requires preloaded records"
            )
        root = deliberation_path(cfg)
        try:
            with events._event_lock(root):
                loaded, issues = _load_records_unlocked(cfg)
                return evaluate_candidate_set(
                    cfg, candidate_set_id, records=loaded,
                    preloaded_integrity_issues=issues,
                )
        except events.EventError as exc:
            raise DeliberationError(
                f"cannot acquire deliberation store lock: {exc}"
            ) from exc
    if not isinstance(records, list):
        raise DeliberationError("records must be a list")
    for item in records:
        validate_record(item)
    records = sorted(copy.deepcopy(records), key=lambda item: _record_id(item))
    integrity_issues = _cross_record_issues(records)
    if preloaded_integrity_issues is not None:
        if not isinstance(preloaded_integrity_issues, list):
            raise DeliberationError("preloaded_integrity_issues must be a list")
        known = {
            (item.get("code"), item.get("detail"), item.get("path"))
            for item in integrity_issues
        }
        for item in preloaded_integrity_issues:
            if not isinstance(item, dict) or set(item) != {"code", "detail", "path"}:
                raise DeliberationError(
                    "preloaded deliberation integrity issue has an invalid shape"
                )
            key = (item["code"], item["detail"], item["path"])
            if key not in known:
                integrity_issues.append(copy.deepcopy(item))
                known.add(key)
        integrity_issues.sort(
            key=lambda item: (item["code"], item["path"], item["detail"])
        )
    candidate_sets = [
        item for item in records
        if item.get("record_type") == "candidate_set"
        and item.get("candidate_set_id") == candidate_set_id
    ]
    if len(candidate_sets) != 1:
        raise DeliberationError("candidate_set_id does not resolve to one record")
    candidate_set = candidate_sets[0]
    proposals = {
        item["proposal_id"]: item for item in records
        if item.get("record_type") == "proposal"
        and item["proposal_id"] in candidate_set["proposal_ids"]
    }
    ballots = sorted(
        (
            item for item in records
            if item.get("record_type") == "ballot"
            and item["candidate_set_id"] == candidate_set_id
        ),
        key=lambda item: item["ballot_id"],
    )
    global_findings = [
        {"code": item["code"], "detail": item["detail"], "path": item["path"]}
        for item in integrity_issues
    ]
    try:
        raw = load_raw(cfg)
        current_snapshot = _project_snapshot(cfg, raw=raw)
    except (OSError, ValueError, DeliberationError) as exc:
        raw = None
        current_snapshot = None
        global_findings.append({
            "code": "CURRENT_PROJECT_UNAVAILABLE", "detail": str(exc), "path": "-",
        })
    if (current_snapshot is not None
            and candidate_set["frozen_snapshot"] != current_snapshot):
        global_findings.append({
            "code": "FROZEN_SET_DRIFT",
            "detail": "the frozen graph/policy/logic snapshot is no longer current",
            "path": candidate_set_id,
        })

    proposal_findings = {}
    if raw is not None and current_snapshot is not None:
        for proposal_id, proposal in proposals.items():
            findings = _proposal_live_findings(
                cfg, proposal, current_snapshot, raw,
            )
            if proposal["phase"] != "claim_extraction":
                try:
                    _validate_phase_reference(cfg, proposal, records)
                except DeliberationError as exc:
                    findings.append({
                        "code": "PHASE_REFERENCE_NOT_CURRENT",
                        "detail": str(exc),
                    })
            proposal_findings[proposal_id] = findings
    else:
        proposal_findings = {
            proposal_id: [{
                "code": "CURRENT_PROJECT_UNAVAILABLE",
                "detail": "live proposal grounding could not be rechecked",
            }]
            for proposal_id in proposals
        }

    candidate_results = []
    required_roles = set(REQUIRED_ROLES[candidate_set["phase"]])
    for candidate_id in candidate_set["candidate_ids"]:
        candidate_proposals = sorted(
            (
                item for item in proposals.values()
                if item["candidate_id"] == candidate_id
            ),
            key=lambda item: item["proposal_id"],
        )
        proposer_actors = {item["actor"]["id"] for item in candidate_proposals}
        proposer_groups = {item["actor"]["independence_group"] for item in candidate_proposals}
        evaluations = []
        for ballot in ballots:
            match = next((
                item for item in ballot["evaluations"]
                if item["candidate_id"] == candidate_id
            ), None)
            if match is None:
                continue
            evaluations.append({
                **copy.deepcopy(match),
                "ballot_id": ballot["ballot_id"],
                "role": ballot["role"],
                "actor": ballot["actor"]["id"],
                "independence_group": ballot["actor"]["independence_group"],
            })
        decisions_by_group = {}
        for item in evaluations:
            bucket = decisions_by_group.setdefault(item["independence_group"], [])
            bucket.append(item)
        endorsement_groups = {
            group for group, items in decisions_by_group.items()
            if all(item["decision"] == "endorse" for item in items)
        }
        reject_groups = {
            group for group, items in decisions_by_group.items()
            if any(item["decision"] == "reject" for item in items)
        }
        abstain_groups = {
            group for group, items in decisions_by_group.items()
            if not any(item["decision"] in {"endorse", "reject"} for item in items)
        }
        # A different actor label does not make a review independent when it shares a
        # proposer model/context group.  Such ballots remain visible, including dissent,
        # but cannot satisfy external-review or required-role gates for that candidate.
        external_review_groups = set(decisions_by_group) - proposer_groups
        external_endorsement_groups = endorsement_groups & external_review_groups
        external_reject_groups = reject_groups & external_review_groups
        externally_endorsing_evaluations = [
            item for item in evaluations
            if item["independence_group"] in external_endorsement_groups
            and item["decision"] == "endorse"
        ]
        role_endorsements = {
            role for role in required_roles
            if any(item["role"] == role and item["decision"] == "endorse"
                   for item in externally_endorsing_evaluations)
        }
        missing_roles = sorted(required_roles - role_endorsements)
        independent_role_coverage = _distinct_role_group_coverage(
            externally_endorsing_evaluations, required_roles,
        )
        blocking = [item for item in evaluations if item["blocking"]]
        nonabstaining = external_endorsement_groups | external_reject_groups
        ratio_ok = bool(nonabstaining) and (
            3 * len(external_endorsement_groups) >= 2 * len(nonabstaining)
        )
        participant_groups = proposer_groups | set(decisions_by_group)
        quorum_shape = (
            (len(proposer_groups) >= 2 and len(external_endorsement_groups) >= 1)
            or (len(proposer_groups) >= 1 and len(external_endorsement_groups) >= 2)
        )
        candidate_live_findings = [
            {**item, "proposal_id": proposal_id}
            for proposal_id in [item["proposal_id"] for item in candidate_proposals]
            for item in proposal_findings.get(proposal_id, [])
        ]
        mechanically_blocked = bool(candidate_live_findings)
        eligible = (
            not global_findings
            and not mechanically_blocked
            and len(participant_groups) >= 3
            and quorum_shape
            and not missing_roles
            and independent_role_coverage
            and not blocking
            and ratio_ok
        )
        if eligible:
            state = "eligible_for_human_review"
        elif global_findings or mechanically_blocked:
            state = "blocked"
        elif blocking or reject_groups:
            state = "contested"
        else:
            state = "insufficient_review"
        candidate_results.append({
            "candidate_id": candidate_id,
            "state": state,
            "proposal_ids": [item["proposal_id"] for item in candidate_proposals],
            "proposer_actors": sorted(proposer_actors),
            "proposer_independence_groups": sorted(proposer_groups),
            "participant_independence_groups": sorted(participant_groups),
            "endorsement_independence_groups": sorted(endorsement_groups),
            "external_endorsement_independence_groups": sorted(
                external_endorsement_groups
            ),
            "reject_independence_groups": sorted(reject_groups),
            "external_reject_independence_groups": sorted(external_reject_groups),
            "abstain_independence_groups": sorted(abstain_groups),
            "required_roles": sorted(required_roles),
            "endorsed_roles": sorted(role_endorsements),
            "missing_roles": missing_roles,
            "distinct_role_independence_gate_passed": independent_role_coverage,
            "blocking_evaluations": sorted(
                (item["ballot_id"] for item in blocking),
            ),
            "endorsement_ratio_gate_passed": ratio_ok,
            "procedural_independence_gate_passed": (
                len(participant_groups) >= 3 and quorum_shape
            ),
            "mechanical_findings": candidate_live_findings,
            "evaluations": sorted(evaluations, key=lambda item: item["ballot_id"]),
            "score_for_review_scheduling_only": {
                "endorsement_groups": len(endorsement_groups),
                "proposer_groups": len(proposer_groups),
                "reject_groups": len(reject_groups),
            },
        })

    eligible = [
        item["candidate_id"] for item in candidate_results
        if item["state"] == "eligible_for_human_review"
    ]
    blocked_candidates = [
        item for item in candidate_results if item["state"] == "blocked"
    ]
    contested_candidates = [
        item for item in candidate_results if item["state"] == "contested"
    ]
    insufficient_candidates = [
        item for item in candidate_results if item["state"] == "insufficient_review"
    ]
    if global_findings or blocked_candidates:
        status = "blocked"
        recommendation = None
    elif len(eligible) > 1:
        status = "contested"
        recommendation = None
        global_findings.append({
            "code": "MULTIPLE_ELIGIBLE_ALTERNATIVES",
            "detail": "more than one incompatible candidate passed; no hash tie-break is used",
            "path": candidate_set_id,
        })
    elif contested_candidates:
        status = "contested"
        recommendation = None
    elif insufficient_candidates:
        status = "insufficient_review"
        recommendation = None
    elif len(eligible) == 1 and len(candidate_results) == 1:
        status = "recommended_for_human_review"
        recommendation = eligible[0]
    else:
        status = "insufficient_review"
        recommendation = None
    if len(eligible) == 1 and len(candidate_results) > 1:
        unresolved = sorted(
            f"{item['candidate_id']}={item['state']}"
            for item in candidate_results if item["candidate_id"] != eligible[0]
        )
        global_findings.append({
            "code": "UNRESOLVED_FROZEN_ALTERNATIVE",
            "detail": (
                "one candidate passed, but the frozen union still contains: "
                + ", ".join(unresolved)
            ),
            "path": candidate_set_id,
        })
    matching_decisions = sorted(
        (
            item for item in records
            if item.get("record_type") == "phase_decision"
            and item["candidate_set_id"] == candidate_set_id
        ),
        key=lambda item: item["decision_id"],
    )
    phase_decision = matching_decisions[0] if len(matching_decisions) == 1 else None
    if phase_decision is None:
        phase_routing_state = (
            "awaiting_attributed_phase_decision"
            if status == "recommended_for_human_review"
            else "panel_unresolved"
        )
    elif (status != "recommended_for_human_review"
            or phase_decision["candidate_id"] != recommendation):
        phase_routing_state = "recorded_decision_not_current"
    elif phase_decision["decision"] == "rejected":
        phase_routing_state = "rejected_by_attributed_reviewer"
    elif candidate_set["phase"] == "rule_validity":
        phase_routing_state = "approved_for_external_application_review"
    else:
        phase_routing_state = "approved_for_next_phase"
    if phase_routing_state == "awaiting_attributed_phase_decision":
        next_action = "record_attributed_phase_decision"
    elif phase_routing_state == "approved_for_next_phase":
        next_action = "propose_immediate_next_phase_on_same_snapshot"
    elif phase_routing_state == "approved_for_external_application_review":
        next_action = "human_review_then_isolated_testing_and_separate_activation"
    elif phase_routing_state == "rejected_by_attributed_reviewer":
        next_action = "start_a_new_round_if_revision_is_warranted"
    elif phase_routing_state == "recorded_decision_not_current":
        next_action = "start_a_new_round_from_current_project_state"
    else:
        next_action = "resolve_recorded_findings_or_collect_missing_review_roles"
    return {
        "schema_version": STATUS_SCHEMA,
        "candidate_set_id": candidate_set_id,
        "round_id": candidate_set["round_id"],
        "phase": candidate_set["phase"],
        "subject_key": candidate_set["subject_key"],
        "status": status,
        "recommended_candidate_id": recommendation,
        "human_activation_required": True,
        "automatic_activation": False,
        "procedural_independence_only": True,
        "human_identity_authenticated": False,
        "phase_decision_id": (
            phase_decision["decision_id"] if phase_decision is not None else None
        ),
        "phase_decision": copy.deepcopy(phase_decision),
        "phase_routing_state": phase_routing_state,
        "ballot_ids": [item["ballot_id"] for item in ballots],
        "candidates": sorted(candidate_results, key=lambda item: item["candidate_id"]),
        "findings": sorted(
            global_findings,
            key=lambda item: (item["code"], item.get("path", ""), item["detail"]),
        ),
        "next_action": next_action,
    }


def _deliberation_status_from_records(cfg, records, issues):
    candidate_sets = sorted(
        (item for item in records if item.get("record_type") == "candidate_set"),
        key=lambda item: item["candidate_set_id"],
    )
    frozen_keys = {
        (item["round_id"], item["phase"], item["subject_key"])
        for item in candidate_sets
    }
    open_groups = {}
    for proposal in records:
        if proposal.get("record_type") != "proposal":
            continue
        key = (proposal["round_id"], proposal["phase"], proposal["subject_key"])
        if key in frozen_keys:
            continue
        group = open_groups.setdefault(key, {"proposal_ids": [], "candidate_ids": set()})
        group["proposal_ids"].append(proposal["proposal_id"])
        group["candidate_ids"].add(proposal["candidate_id"])
    panels = []
    for candidate_set in candidate_sets:
        try:
            panels.append(evaluate_candidate_set(
                cfg, candidate_set["candidate_set_id"], records=records,
                preloaded_integrity_issues=issues,
            ))
        except DeliberationError as exc:
            panels.append({
                "schema_version": STATUS_SCHEMA,
                "candidate_set_id": candidate_set["candidate_set_id"],
                "round_id": candidate_set["round_id"],
                "phase": candidate_set["phase"],
                "subject_key": candidate_set["subject_key"],
                "status": "blocked",
                "recommended_candidate_id": None,
                "human_activation_required": True,
                "automatic_activation": False,
                "procedural_independence_only": True,
                "human_identity_authenticated": False,
                "phase_decision_id": None,
                "phase_decision": None,
                "phase_routing_state": "panel_unavailable",
                "ballot_ids": [], "candidates": [],
                "findings": [{"code": "PANEL_EVALUATION_FAILED", "detail": str(exc)}],
                "next_action": "repair_ledger_integrity",
            })
    return {
        "schema_version": STATUS_SCHEMA,
        "integrity": "ok" if not issues else "error",
        "integrity_issues": issues,
        "records": copy.deepcopy(records),
        "panels": panels,
        "open_groups": [
            {
                "round_id": key[0], "phase": key[1], "subject_key": key[2],
                "proposal_ids": sorted(value["proposal_ids"]),
                "candidate_ids": sorted(value["candidate_ids"]),
            }
            for key, value in sorted(open_groups.items())
        ],
        "human_activation_required": True,
        "automatic_activation": False,
    }


def deliberation_status(cfg):
    """Return one lock-consistent snapshot of all panels and open proposal groups."""
    root = deliberation_path(cfg)
    try:
        with events._event_lock(root):
            records, issues = _load_records_unlocked(cfg)
            return _deliberation_status_from_records(cfg, records, issues)
    except events.EventError as exc:
        issue = _issue(
            "DELIBERATION_STORE_LOCK_FAILED",
            f"cannot acquire deliberation store lock: {exc}", root,
        )
        return _deliberation_status_from_records(cfg, [], [issue])


__all__ = [
    "BALLOT_REQUEST_SCHEMA", "BALLOT_SCHEMA", "CANDIDATE_SET_REQUEST_SCHEMA",
    "CANDIDATE_SET_SCHEMA", "CANDIDATE_ID_RE", "CANDIDATE_SET_ID_RE",
    "DECISIONS", "DeliberationError", "MANDATORY_COMPETENCY_CATEGORIES",
    "PHASES", "PHASE_DECISION_ID_RE", "PHASE_DECISION_REQUEST_SCHEMA",
    "PHASE_DECISION_SCHEMA", "PROPOSAL_REQUEST_SCHEMA", "PROPOSAL_SCHEMA", "REASON_CODES",
    "REQUIRED_ROLES", "STATUS_SCHEMA", "append_record", "create_ballot",
    "create_phase_decision", "create_proposal", "deliberation_path", "deliberation_status",
    "evaluate_candidate_set", "freeze_candidate_set", "load_records",
    "validate_record",
]
