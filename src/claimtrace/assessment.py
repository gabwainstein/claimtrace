"""Grounded, content-addressed semantic assessments.

An assessment records an external reviewer's interpretation of a result-to-claim
link without treating that interpretation as mechanically verified truth.  The
document deliberately separates:

* ``mechanical_snapshot`` -- node and artifact hashes computed by claimtrace;
* ``agent_input`` -- a small, schema-constrained semantic judgement; and
* ``derived`` -- policy output computed by this module, never accepted as input.

Assessment documents are immutable.  Review changes create a new document that
supersedes the previous one, so the on-disk store is append-only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import engine
from .config import strict_json_loads


SCHEMA_VERSION = "claimtrace.semantic-assessment/1"
ASSESSMENT_ID_RE = re.compile(r"^assessment:sha256:([0-9a-f]{64})$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RFC3339_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)

VERDICTS = {
    "supports_as_written",
    "supports_narrower_claim",
    "contradicts_as_written",
    "insufficient",
    "ambiguous",
    "unrelated",
}
ALIGNMENTS = {"match", "partial", "mismatch", "not_stated", "not_applicable"}
INFERENCE_LEVELS = {
    "descriptive", "associational", "predictive", "causal", "mechanistic", "not_stated"
}
_INFERENCE_COMPATIBILITY = {
    "descriptive": {"descriptive", "associational", "predictive", "causal", "mechanistic"},
    "associational": {"associational", "predictive", "causal", "mechanistic"},
    "predictive": {"predictive"},
    "causal": {"causal", "mechanistic"},
    "mechanistic": {"mechanistic"},
    "not_stated": set(),
}
REVIEW_STATES = {"proposed", "accepted", "rejected", "contested", "superseded"}
_REVIEW_TRANSITIONS = {
    "proposed": {"accepted", "rejected", "contested", "superseded"},
    "accepted": {"contested", "superseded"},
    "rejected": {"superseded"},
    "contested": {"accepted", "rejected", "superseded"},
    "superseded": set(),
}
FRAME_FIELDS = {
    "population", "exposure", "comparator", "outcome", "direction", "magnitude",
    "time_scope", "inference_level",
}
ALIGNMENT_DIMENSIONS = (
    "population", "exposure", "comparator", "outcome", "direction", "magnitude",
    "time_scope", "inference_level",
)
CLAIM_TYPES = {"claim", "hypothesis", "prediction", "conclusion"}
RESULT_TYPES = {"artifact", "figure", "experiment", "data"}
_PROCESS_ASSESSMENT_LOCK = threading.Lock()


class AssessmentError(Exception):
    """The assessment is malformed, ungrounded, or cannot be safely recorded."""


def _canonical_bytes(value) -> bytes:
    try:
        text = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise AssessmentError(f"value is not canonical JSON: {exc}") from exc
    return text.encode("utf-8")


def _sha256(value) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _json_copy(value):
    """Detach caller-owned objects while preserving only canonical JSON values."""
    return json.loads(_canonical_bytes(value).decode("utf-8"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validate_recorded_at(value: object) -> None:
    if not isinstance(value, str) or not RFC3339_UTC_RE.fullmatch(value):
        raise AssessmentError("recorded_at must be an RFC 3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise AssessmentError("recorded_at must be a valid UTC ISO-8601 timestamp") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise AssessmentError("recorded_at must be UTC")


def _bounded_text(value: object, label: str, *, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise AssessmentError(f"{label} must be a non-empty string")
    if len(value) > maximum:
        raise AssessmentError(f"{label} exceeds {maximum} characters")
    return value


def _reject_reasoning_fields(value, path: str = "agent_input") -> None:
    """Reject hidden/free-form reasoning fields; only concise rationale is retained."""
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in {"chain_of_thought", "chain-of-thought", "reasoning", "cot"}:
                raise AssessmentError(f"{path}.{key} is not permitted; use concise rationale")
            _reject_reasoning_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_reasoning_fields(child, f"{path}[{index}]")


def _validate_frame(frame: object, label: str) -> None:
    if not isinstance(frame, dict):
        raise AssessmentError(f"{label} must be an object")
    missing = FRAME_FIELDS - set(frame)
    unknown = set(frame) - FRAME_FIELDS
    if missing or unknown:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            details.append("unknown " + ", ".join(sorted(unknown)))
        raise AssessmentError(f"{label} must contain every fixed frame field ({'; '.join(details)})")
    if frame.get("inference_level") not in INFERENCE_LEVELS:
        raise AssessmentError(
            f"{label}.inference_level must be one of: {', '.join(sorted(INFERENCE_LEVELS))}"
        )
    for key, value in frame.items():
        if key == "inference_level":
            continue
        if value is not None:
            _bounded_text(value, f"{label}.{key}", maximum=500)


def _validate_anchor(anchor: object, index: int, result_ids: set[str]) -> None:
    label = f"agent_input.evidence_anchors[{index}]"
    if not isinstance(anchor, dict):
        raise AssessmentError(f"{label} must be an object")
    kind = anchor.get("kind")
    if kind == "json_pointer":
        expected = {"result_id", "kind", "pointer", "expected_value"}
        if set(anchor) != expected:
            raise AssessmentError(f"{label} must contain exactly {', '.join(sorted(expected))}")
        if not isinstance(anchor.get("pointer"), str):
            raise AssessmentError(f"{label}.pointer must be a string")
        if len(_canonical_bytes(anchor.get("expected_value"))) > 65536:
            raise AssessmentError(f"{label}.expected_value exceeds 64 KiB")
    elif kind == "text_lines":
        expected = {"result_id", "kind", "start_line", "end_line", "text_sha256"}
        if set(anchor) != expected:
            raise AssessmentError(f"{label} must contain exactly {', '.join(sorted(expected))}")
        start, end = anchor.get("start_line"), anchor.get("end_line")
        if (not isinstance(start, int) or isinstance(start, bool) or start < 1
                or not isinstance(end, int) or isinstance(end, bool) or end < start):
            raise AssessmentError(f"{label} needs a 1-based inclusive valid line span")
        if not isinstance(anchor.get("text_sha256"), str) or not SHA256_RE.fullmatch(
                anchor["text_sha256"]):
            raise AssessmentError(f"{label}.text_sha256 must be a lowercase SHA-256 digest")
    else:
        raise AssessmentError(f"{label}.kind must be 'json_pointer' or 'text_lines'")
    if anchor.get("result_id") not in result_ids:
        raise AssessmentError(f"{label}.result_id is not one of the assessed results")


def validate_agent_input(agent_input: object, result_ids: list[str]) -> None:
    """Validate the complete external-agent section without adding defaults."""
    if not isinstance(agent_input, dict):
        raise AssessmentError("agent_input must be an object")
    _reject_reasoning_fields(agent_input)
    required = {
        "verdict", "claim_frame", "result_frame", "alignment", "evidence_anchors",
        "rationale", "limitations", "provenance",
    }
    optional = {"recommended_claim"}
    missing = required - set(agent_input)
    unknown = set(agent_input) - required - optional
    if missing:
        raise AssessmentError(f"agent_input is missing: {', '.join(sorted(missing))}")
    if unknown:
        raise AssessmentError(f"agent_input has unknown fields: {', '.join(sorted(unknown))}")
    if agent_input["verdict"] not in VERDICTS:
        raise AssessmentError(f"agent_input.verdict must be one of: {', '.join(sorted(VERDICTS))}")
    _validate_frame(agent_input["claim_frame"], "agent_input.claim_frame")
    _validate_frame(agent_input["result_frame"], "agent_input.result_frame")

    alignment = agent_input["alignment"]
    if not isinstance(alignment, dict) or set(alignment) != set(ALIGNMENT_DIMENSIONS):
        raise AssessmentError("agent_input.alignment must assess every fixed alignment dimension")
    for dimension, value in alignment.items():
        if value not in ALIGNMENTS:
            raise AssessmentError(
                f"agent_input.alignment.{dimension} must be one of: {', '.join(sorted(ALIGNMENTS))}"
            )
        claim_value = agent_input["claim_frame"][dimension]
        result_value = agent_input["result_frame"][dimension]
        if dimension == "inference_level":
            if value == "not_applicable":
                raise AssessmentError("agent_input.alignment.inference_level cannot be not_applicable")
            if value == "not_stated" and "not_stated" not in {claim_value, result_value}:
                raise AssessmentError(
                    "inference-level alignment cannot be not_stated when both levels are stated"
                )
            if value == "mismatch" and claim_value == result_value:
                raise AssessmentError(
                    "inference-level alignment cannot be mismatch when both levels are identical"
                )
            continue
        if value in {"match", "partial", "mismatch"} and (
                claim_value is None or result_value is None):
            raise AssessmentError(
                f"agent_input.alignment.{dimension} requires both frame values"
            )
        if value == "not_stated" and claim_value is not None and result_value is not None:
            raise AssessmentError(
                f"agent_input.alignment.{dimension} cannot be not_stated when both values exist"
            )
        if value == "not_applicable" and (claim_value is not None or result_value is not None):
            raise AssessmentError(
                f"agent_input.alignment.{dimension} requires null frame values when not_applicable"
            )
        if (value == "mismatch" and isinstance(claim_value, str)
                and isinstance(result_value, str)
                and claim_value.strip().casefold() == result_value.strip().casefold()):
            raise AssessmentError(
                f"agent_input.alignment.{dimension} cannot be mismatch for identical values"
            )

    anchors = agent_input["evidence_anchors"]
    if not isinstance(anchors, list) or not anchors:
        raise AssessmentError("agent_input.evidence_anchors must be a non-empty list")
    result_id_set = set(result_ids)
    for index, anchor in enumerate(anchors):
        _validate_anchor(anchor, index, result_id_set)
    anchored = {anchor["result_id"] for anchor in anchors}
    if anchored != result_id_set:
        missing_anchors = sorted(result_id_set - anchored)
        raise AssessmentError(f"every result needs an evidence anchor; missing: {', '.join(missing_anchors)}")

    _bounded_text(agent_input["rationale"], "agent_input.rationale", maximum=1000)
    limitations = agent_input["limitations"]
    if (not isinstance(limitations, list)
            or not all(isinstance(item, str) and item.strip() and len(item) <= 500
                       for item in limitations)
            or len(limitations) > 20):
        raise AssessmentError("agent_input.limitations must contain at most 20 concise strings")
    provenance = agent_input["provenance"]
    if not isinstance(provenance, dict) or not provenance:
        raise AssessmentError("agent_input.provenance must be a non-empty object")
    allowed_provenance = {"agent", "model", "skill_version", "prompt_sha256"}
    if set(provenance) - allowed_provenance:
        raise AssessmentError("agent_input.provenance contains an unknown field")
    _bounded_text(provenance.get("agent"), "agent_input.provenance.agent", maximum=200)
    for key in ("model", "skill_version"):
        if key in provenance:
            _bounded_text(provenance[key], f"agent_input.provenance.{key}", maximum=200)
    if "prompt_sha256" in provenance and (
            not isinstance(provenance["prompt_sha256"], str)
            or not SHA256_RE.fullmatch(provenance["prompt_sha256"])):
        raise AssessmentError("agent_input.provenance.prompt_sha256 must be a lowercase SHA-256 digest")

    recommendation = agent_input.get("recommended_claim")
    if agent_input["verdict"] == "supports_narrower_claim":
        _bounded_text(recommendation, "agent_input.recommended_claim", maximum=2000)
    elif recommendation is not None:
        _bounded_text(recommendation, "agent_input.recommended_claim", maximum=2000)


def _node_snapshot(node: dict) -> dict:
    digest = _sha256(node)
    return {
        "node_id": node["id"],
        "node_sha256": digest,
        "node_version_id": f"node:sha256:{digest}",
    }


def _artifact_bytes(cfg, node: dict) -> tuple[dict, bytes | None]:
    path_value = node.get("path")
    if not isinstance(path_value, str) or not path_value:
        return {"state": "not_declared"}, None
    path = Path(cfg.resolve(path_value))
    try:
        if not path.is_file():
            return {"state": "missing", "path": path_value}, None
        data = path.read_bytes()
    except OSError as exc:
        return {"state": "unreadable", "path": path_value, "error": type(exc).__name__}, None
    digest = hashlib.sha256(data).hexdigest()
    return {
        "state": "stable",
        "path": path_value,
        "sha256": digest,
        "file_version_id": f"file:sha256:{digest}",
        "size": len(data),
    }, data


def _json_pointer(document, pointer: str):
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise ValueError("JSON Pointer must be empty or start with '/'")
    current = document
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        # Any remaining '~' sequence is invalid under RFC 6901.
        if re.search(r"~(?![01])", raw_token):
            raise ValueError("JSON Pointer has an invalid escape")
        if isinstance(current, dict):
            if token not in current:
                raise KeyError(token)
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                raise ValueError("JSON Pointer array token is not a canonical index")
            index = int(token)
            if index >= len(current):
                raise IndexError(index)
            current = current[index]
        else:
            raise TypeError("JSON Pointer traverses a scalar")
    return current


def _check_anchor(anchor: dict, artifact: dict, data: bytes | None) -> dict:
    if artifact.get("state") != "stable" or data is None:
        return {"valid": False, "detail": f"result artifact is {artifact.get('state', 'unavailable')}"}
    if anchor["kind"] == "json_pointer":
        try:
            parsed = strict_json_loads(data.decode("utf-8-sig"), artifact.get("path", "artifact"))
            observed = _json_pointer(parsed, anchor["pointer"])
            valid = _canonical_bytes(observed) == _canonical_bytes(anchor["expected_value"])
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, KeyError, IndexError,
                TypeError, AssessmentError) as exc:
            return {"valid": False, "detail": f"JSON anchor does not resolve: {exc}"}
        return {
            "valid": valid,
            "detail": "exact JSON value matched" if valid else "JSON value differs from expected_value",
            "observed_value_sha256": _sha256(observed),
        }

    lines = data.splitlines(keepends=True)
    start, end = anchor["start_line"], anchor["end_line"]
    if end > len(lines):
        return {"valid": False, "detail": f"text span exceeds {len(lines)} lines"}
    observed_digest = hashlib.sha256(b"".join(lines[start - 1:end])).hexdigest()
    valid = observed_digest == anchor["text_sha256"]
    return {
        "valid": valid,
        "detail": "exact text span matched" if valid else "text span digest differs",
        "observed_text_sha256": observed_digest,
    }


def _snapshot_subject(cfg, claim_id: str, result_ids: list[str], agent_input: dict) -> dict:
    nodes, _edges, _concepts = engine.load_graph(cfg)
    claim = nodes.get(claim_id)
    if claim is None:
        raise AssessmentError(f"claim node does not exist: {claim_id}")
    if claim.get("type") not in CLAIM_TYPES:
        raise AssessmentError(f"node {claim_id!r} is not a claim-like node")

    result_snapshots = []
    artifact_data = {}
    for result_id in result_ids:
        node = nodes.get(result_id)
        if node is None:
            raise AssessmentError(f"result node does not exist: {result_id}")
        if node.get("type") not in RESULT_TYPES:
            raise AssessmentError(f"node {result_id!r} is not a result-like node")
        artifact, data = _artifact_bytes(cfg, node)
        result_snapshots.append({**_node_snapshot(node), "artifact": artifact})
        artifact_data[result_id] = (artifact, data)

    anchor_checks = []
    for index, anchor in enumerate(agent_input["evidence_anchors"]):
        artifact, data = artifact_data[anchor["result_id"]]
        anchor_checks.append({
            "anchor_index": index,
            "result_id": anchor["result_id"],
            **_check_anchor(anchor, artifact, data),
        })
    return {
        "claim": _node_snapshot(claim),
        "results": result_snapshots,
        "anchor_checks": anchor_checks,
    }


def _finding(code: str, severity: str, detail: str) -> dict:
    return {"code": code, "severity": severity, "detail": detail}


def _semantic_findings(mechanical_snapshot: dict, agent_input: dict) -> list[dict]:
    findings = []
    verdict = agent_input["verdict"]
    invalid = [check for check in mechanical_snapshot["anchor_checks"] if not check["valid"]]
    for check in invalid:
        findings.append(_finding(
            "EVIDENCE_ANCHOR_INVALID", "error",
            f"anchor {check['anchor_index']} for {check['result_id']}: {check['detail']}",
        ))

    claim_level = agent_input["claim_frame"]["inference_level"]
    result_level = agent_input["result_frame"]["inference_level"]
    inference_adequate = result_level in _INFERENCE_COMPATIBILITY[claim_level]
    inference_alignment = agent_input["alignment"]["inference_level"]
    if not inference_adequate:
        severity = (
            "error" if verdict in {"supports_as_written", "contradicts_as_written"}
            else "warning"
        )
        code = (
            "CAUSAL_MODALITY_MISMATCH"
            if claim_level in {"causal", "mechanistic"}
            else "INFERENCE_LEVEL_INADEQUATE"
        )
        findings.append(_finding(
            code, severity,
            f"{result_level} evidence cannot support a {claim_level} claim as written",
        ))
    elif inference_alignment == "mismatch":
        severity = (
            "error" if verdict in {"supports_as_written", "contradicts_as_written"}
            else "warning"
        )
        findings.append(_finding(
            "INFERENCE_LEVEL_MISMATCH", severity,
            "the agent declared an inference-level mismatch",
        ))
    if not inference_adequate and inference_alignment == "match":
        findings.append(_finding(
            "ALIGNMENT_INCONSISTENT", "error",
            "inference-level alignment is 'match' despite inadequate result modality",
        ))

    scope_mismatches = [
        dimension for dimension in ALIGNMENT_DIMENSIONS if dimension != "inference_level"
        and agent_input["alignment"][dimension] == "mismatch"
    ]
    relevant_scope_mismatches = list(scope_mismatches)
    if verdict == "contradicts_as_written":
        # A directional contradiction is the one conservative refutation shape supported by v1.
        # Other scope mismatches mean the result is not testing the same claim as written.
        relevant_scope_mismatches = [
            dimension for dimension in scope_mismatches if dimension != "direction"
        ]
        if agent_input["alignment"]["direction"] != "mismatch":
            findings.append(_finding(
                "CONTRADICTION_DIRECTION_NOT_ESTABLISHED", "error",
                "contradicts_as_written requires an explicit direction mismatch",
            ))
    if relevant_scope_mismatches:
        severity = "error" if verdict in {
            "supports_as_written", "contradicts_as_written"
        } else "warning"
        findings.append(_finding(
            "CLAIM_SCOPE_MISMATCH", severity,
            "result mismatches claim dimensions: " + ", ".join(relevant_scope_mismatches),
        ))
    partial = [
        dimension for dimension in ALIGNMENT_DIMENSIONS
        if agent_input["alignment"][dimension] in {"partial", "not_stated"}
    ]
    if partial:
        severity = "error" if verdict in {
            "supports_as_written", "contradicts_as_written"
        } else "warning"
        findings.append(_finding(
            "CLAIM_ALIGNMENT_INCOMPLETE", severity,
            "alignment is partial or unstated for: " + ", ".join(partial),
        ))
    return findings


def _derive(mechanical_snapshot: dict, agent_input: dict, review: dict,
            extra_findings: list[dict] | None = None) -> dict:
    findings = _semantic_findings(mechanical_snapshot, agent_input)
    findings.extend(extra_findings or [])
    findings.sort(key=lambda item: (item["code"], item["detail"]))
    verdict = agent_input["verdict"]
    errors = {item["code"] for item in findings if item["severity"] == "error"}
    hard_blocks = {"EVIDENCE_ANCHOR_INVALID", "ASSESSMENT_STALE", "ASSESSMENT_CONTESTED",
                   "ASSESSMENT_STORE_INTEGRITY", "ALIGNMENT_INCONSISTENT"}
    proposed_relation = None
    if verdict == "supports_as_written" and not errors:
        proposed_relation = "supports"
    elif verdict == "supports_narrower_claim" and not (errors & hard_blocks):
        # This is intentionally not support for the original claim.
        proposed_relation = "related"
    elif verdict == "contradicts_as_written" and not errors:
        proposed_relation = "refutes"

    effective_state = "contested" if any(
        item["code"] == "ASSESSMENT_CONTESTED" for item in findings
    ) else review["state"]
    active_relation = (
        proposed_relation if effective_state == "accepted" and not errors else None
    )
    return {
        "effective_review_state": effective_state,
        "proposed_relation": proposed_relation,
        "active_relation": active_relation,
        "stale": any(item["code"] == "ASSESSMENT_STALE" for item in findings),
        "findings": findings,
    }


def _assessment_id(core: dict) -> str:
    return f"assessment:sha256:{_sha256(core)}"


def create_assessment(cfg, claim_id: str, result_ids: list[str], agent_input: dict, *,
                      actor: str, recorded_at: str | None = None) -> dict:
    """Create a proposed assessment; external input cannot select acceptance or derived fields."""
    _bounded_text(claim_id, "claim_id", maximum=500)
    if (not isinstance(result_ids, list) or not result_ids
            or not all(isinstance(item, str) and item for item in result_ids)):
        raise AssessmentError("result_ids must be a non-empty string list")
    if len(result_ids) != 1:
        raise AssessmentError(
            "semantic assessment v1 requires exactly one result_id; record separate assessments"
        )
    if len(set(result_ids)) != len(result_ids):
        raise AssessmentError("result_ids must not contain duplicates")
    result_ids = sorted(result_ids)
    validate_agent_input(agent_input, result_ids)
    agent_input = _json_copy(agent_input)
    _bounded_text(actor, "actor", maximum=200)
    timestamp = recorded_at or _utc_now()
    _validate_recorded_at(timestamp)
    mechanical = _snapshot_subject(cfg, claim_id, result_ids, agent_input)
    review = {"state": "proposed", "actor": actor, "supersedes_assessment_id": None}
    subject = {"claim_id": claim_id, "result_ids": result_ids}
    derived = _derive(mechanical, agent_input, review)
    core = {
        "schema_version": SCHEMA_VERSION,
        "recorded_at": timestamp,
        "subject": subject,
        "mechanical_snapshot": mechanical,
        "agent_input": agent_input,
        "review": review,
        "derived": derived,
    }
    return {"id": _assessment_id(core), **core}


def _validate_mechanical_snapshot(snapshot: object, subject: dict, agent_input: dict) -> None:
    if not isinstance(snapshot, dict) or set(snapshot) != {"claim", "results", "anchor_checks"}:
        raise AssessmentError("mechanical_snapshot has an invalid shape")
    claim = snapshot["claim"]
    expected_node_keys = {"node_id", "node_sha256", "node_version_id"}
    if not isinstance(claim, dict) or set(claim) != expected_node_keys:
        raise AssessmentError("mechanical_snapshot.claim has an invalid shape")
    if claim["node_id"] != subject["claim_id"]:
        raise AssessmentError("mechanical claim node does not match subject")
    snapshots = snapshot["results"]
    if not isinstance(snapshots, list) or [item.get("node_id") for item in snapshots] != subject["result_ids"]:
        raise AssessmentError("mechanical result nodes do not match subject")
    for item in [claim, *snapshots]:
        if not isinstance(item, dict):
            raise AssessmentError("mechanical node snapshot must be an object")
        node_keys = expected_node_keys | ({"artifact"} if item is not claim else set())
        if set(item) != node_keys:
            raise AssessmentError("mechanical node snapshot has an invalid shape")
        digest = item.get("node_sha256")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise AssessmentError("mechanical node_sha256 is invalid")
        if item.get("node_version_id") != f"node:sha256:{digest}":
            raise AssessmentError("mechanical node_version_id does not match node_sha256")
    for item in snapshots:
        artifact = item["artifact"]
        if not isinstance(artifact, dict) or artifact.get("state") not in {
                "stable", "missing", "unreadable", "not_declared"}:
            raise AssessmentError("mechanical artifact snapshot is invalid")
        state = artifact["state"]
        if state == "stable":
            digest = artifact.get("sha256")
            if (set(artifact) != {"state", "path", "sha256", "file_version_id", "size"}
                    or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)
                    or artifact.get("file_version_id") != f"file:sha256:{digest}"
                    or not isinstance(artifact.get("size"), int) or artifact["size"] < 0):
                raise AssessmentError("stable artifact snapshot is invalid")
        elif state == "not_declared" and set(artifact) != {"state"}:
            raise AssessmentError("not-declared artifact snapshot is invalid")
        elif state == "missing" and set(artifact) != {"state", "path"}:
            raise AssessmentError("missing artifact snapshot is invalid")
        elif state == "unreadable" and set(artifact) != {"state", "path", "error"}:
            raise AssessmentError("unreadable artifact snapshot is invalid")
    checks = snapshot["anchor_checks"]
    if not isinstance(checks, list):
        raise AssessmentError("mechanical_snapshot.anchor_checks must be a list")
    if len(checks) != len(agent_input["evidence_anchors"]):
        raise AssessmentError("mechanical anchor check count does not match agent anchors")
    for index, check in enumerate(checks):
        required = {"anchor_index", "result_id", "valid", "detail"}
        if not isinstance(check, dict) or not required <= set(check):
            raise AssessmentError("mechanical anchor check has an invalid shape")
        if check["anchor_index"] != index or not isinstance(check["valid"], bool):
            raise AssessmentError("mechanical anchor check index or validity is invalid")
        if check["result_id"] != agent_input["evidence_anchors"][index]["result_id"]:
            raise AssessmentError("mechanical anchor check result does not match agent anchor")


def validate_assessment_document(document: object) -> None:
    """Validate structure, content address, and that ``derived`` was mechanically computed."""
    if not isinstance(document, dict):
        raise AssessmentError("assessment document must be an object")
    expected = {
        "id", "schema_version", "recorded_at", "subject", "mechanical_snapshot",
        "agent_input", "review", "derived",
    }
    if set(document) != expected:
        raise AssessmentError("assessment document has unknown or missing top-level fields")
    if document["schema_version"] != SCHEMA_VERSION:
        raise AssessmentError(f"unsupported assessment schema: {document['schema_version']!r}")
    _validate_recorded_at(document["recorded_at"])
    subject = document["subject"]
    if (not isinstance(subject, dict) or set(subject) != {"claim_id", "result_ids"}
            or not isinstance(subject["claim_id"], str)
            or not isinstance(subject["result_ids"], list)
            or len(subject["result_ids"]) != 1
            or subject["result_ids"] != sorted(set(subject["result_ids"]))
            or not all(isinstance(item, str) and item for item in subject["result_ids"])):
        raise AssessmentError(
            "assessment subject is invalid or non-canonical; schema v1 requires exactly one result_id"
        )
    validate_agent_input(document["agent_input"], subject["result_ids"])
    _validate_mechanical_snapshot(document["mechanical_snapshot"], subject, document["agent_input"])
    review = document["review"]
    if (not isinstance(review, dict)
            or set(review) != {"state", "actor", "supersedes_assessment_id"}
            or review.get("state") not in REVIEW_STATES):
        raise AssessmentError("assessment review is invalid")
    _bounded_text(review.get("actor"), "review.actor", maximum=200)
    supersedes = review.get("supersedes_assessment_id")
    if supersedes is not None and (
            not isinstance(supersedes, str) or not ASSESSMENT_ID_RE.fullmatch(supersedes)):
        raise AssessmentError("review.supersedes_assessment_id is invalid")
    if review["state"] == "proposed" and supersedes is not None:
        raise AssessmentError("a proposed assessment cannot supersede another assessment")
    if review["state"] != "proposed" and supersedes is None:
        raise AssessmentError("a review decision must supersede an earlier assessment")
    expected_derived = _derive(
        document["mechanical_snapshot"], document["agent_input"], review
    )
    relation_verdicts = {
        "supports_as_written", "supports_narrower_claim", "contradicts_as_written",
    }
    if (review["state"] == "accepted"
            and document["agent_input"]["verdict"] in relation_verdicts
            and expected_derived["active_relation"] is None):
        raise AssessmentError("an accepted relation verdict must have an eligible active relation")
    if document["derived"] != expected_derived:
        raise AssessmentError("derived content does not match claimtrace policy output")
    core = {key: document[key] for key in expected if key != "id"}
    expected_id = _assessment_id(core)
    if document.get("id") != expected_id:
        raise AssessmentError("assessment id does not match canonical document content")


def assessments_path(cfg) -> Path:
    configured = cfg.data.get("assessments", "claimtrace/assessments")
    if not isinstance(configured, str) or not configured:
        raise AssessmentError("config field 'assessments' must be a non-empty string")
    path = Path(configured)
    return path.resolve() if path.is_absolute() else (cfg.base / path).resolve()


@contextmanager
def _assessment_lock(path: Path, timeout: float = 30.0):
    lock_root = Path(tempfile.gettempdir()) / "claimtrace-assessment-locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(os.path.normcase(str(path.resolve())).encode("utf-8")).hexdigest()
    lock_path = lock_root / f"{key}.lock"
    with _PROCESS_ASSESSMENT_LOCK, lock_path.open("a+b") as handle:
        started = time.monotonic()
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() - started >= timeout:
                    raise AssessmentError("timed out waiting for assessment store lock")
                time.sleep(0.05)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _append_assessment_unlocked(root: Path, document: dict) -> Path:
    """Write a validated assessment while the caller holds the store lock."""
    match = ASSESSMENT_ID_RE.fullmatch(document["id"])
    assert match is not None  # already checked by validate_assessment_document
    destination = root / f"{match.group(1)}.json"
    root.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        try:
            existing = strict_json_loads(destination.read_text(encoding="utf-8"), destination.name)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise AssessmentError(f"existing assessment is unreadable: {destination.name}: {exc}") from exc
        if _canonical_bytes(existing) != _canonical_bytes(document):
            raise AssessmentError(f"refusing to overwrite conflicting assessment: {destination.name}")
        return destination
    payload = json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=".assessment-", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    finally:
        try:
            Path(temporary_name).unlink()
        except FileNotFoundError:
            pass
    return destination


def append_assessment(cfg, document: dict) -> Path:
    """Append one immutable assessment document and never overwrite evidence."""
    validate_assessment_document(document)
    root = assessments_path(cfg)
    with _assessment_lock(root):
        return _append_assessment_unlocked(root, document)


def load_assessments(cfg) -> tuple[list[dict], list[dict]]:
    """Load valid assessment documents plus fail-closed integrity issues."""
    root = assessments_path(cfg)
    if not root.exists():
        return [], []
    documents, issues = [], []
    for path in sorted(root.glob("*.json")):
        try:
            document = strict_json_loads(path.read_text(encoding="utf-8-sig"), path.name)
            validate_assessment_document(document)
            if path.name != ASSESSMENT_ID_RE.fullmatch(document["id"]).group(1) + ".json":
                raise AssessmentError("filename does not match assessment id")
            documents.append(document)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, AssessmentError) as exc:
            issues.append({"code": "ASSESSMENT_INTEGRITY", "path": path.name, "detail": str(exc)})
    documents.sort(key=lambda item: (item["recorded_at"], item["id"]))
    by_id = {item["id"]: item for item in documents}
    successors = {}
    for document in documents:
        predecessor_id = document["review"].get("supersedes_assessment_id")
        if predecessor_id is None:
            continue
        successors.setdefault(predecessor_id, []).append(document)
        predecessor = by_id.get(predecessor_id)
        if predecessor is None:
            issues.append({
                "code": "ASSESSMENT_REVIEW_CHAIN",
                "path": ASSESSMENT_ID_RE.fullmatch(document["id"]).group(1) + ".json",
                "detail": f"superseded assessment is missing: {predecessor_id}",
            })
            continue
        if document["review"]["state"] not in _REVIEW_TRANSITIONS[
                predecessor["review"]["state"]]:
            issues.append({
                "code": "ASSESSMENT_REVIEW_CHAIN",
                "path": ASSESSMENT_ID_RE.fullmatch(document["id"]).group(1) + ".json",
                "detail": (
                    "invalid review transition "
                    f"{predecessor['review']['state']} -> {document['review']['state']}"
                ),
            })
        if (predecessor["review"]["state"] == "proposed"
                and document["review"]["actor"] == predecessor["review"]["actor"]):
            issues.append({
                "code": "ASSESSMENT_REVIEW_CHAIN",
                "path": ASSESSMENT_ID_RE.fullmatch(document["id"]).group(1) + ".json",
                "detail": (
                    "proposal and first review use the same self-asserted actor identity: "
                    f"{document['review']['actor']}"
                ),
            })
        for field in ("subject", "mechanical_snapshot", "agent_input"):
            if document[field] != predecessor[field]:
                issues.append({
                    "code": "ASSESSMENT_REVIEW_CHAIN",
                    "path": ASSESSMENT_ID_RE.fullmatch(document["id"]).group(1) + ".json",
                    "detail": f"review transition changed immutable field: {field}",
                })
        predecessor_time = datetime.fromisoformat(
            predecessor["recorded_at"][:-1] + "+00:00"
        )
        document_time = datetime.fromisoformat(document["recorded_at"][:-1] + "+00:00")
        if document_time < predecessor_time:
            issues.append({
                "code": "ASSESSMENT_REVIEW_CHAIN",
                "path": ASSESSMENT_ID_RE.fullmatch(document["id"]).group(1) + ".json",
                "detail": "review transition predates the assessment it supersedes",
            })
    for predecessor_id, children in sorted(successors.items()):
        if len(children) <= 1:
            continue
        child_ids = ", ".join(sorted(item["id"] for item in children))
        for child in children:
            issues.append({
                "code": "ASSESSMENT_REVIEW_CHAIN",
                "path": ASSESSMENT_ID_RE.fullmatch(child["id"]).group(1) + ".json",
                "detail": f"review chain branches after {predecessor_id}: {child_ids}",
            })
    return documents, issues


def _active_documents(documents: list[dict]) -> list[dict]:
    superseded = {
        item["review"]["supersedes_assessment_id"] for item in documents
        if item["review"].get("supersedes_assessment_id")
    }
    return [item for item in documents if item["id"] not in superseded
            and item["review"]["state"] not in {"rejected", "superseded"}]


def detect_conflicts(documents: list[dict]) -> dict[str, list[str]]:
    """Return accepted current assessments whose verdicts disagree for one exact subject.

    Unreviewed proposals are deliberately excluded: untrusted input must not deactivate an
    accepted relation merely by proposing a different interpretation.
    """
    for document in documents:
        validate_assessment_document(document)
    grouped = {}
    for document in _active_documents(documents):
        if document["review"]["state"] != "accepted":
            continue
        key = (document["subject"]["claim_id"], tuple(document["subject"]["result_ids"]))
        grouped.setdefault(key, []).append(document)
    conflicts = {}
    for group in grouped.values():
        if len({item["agent_input"]["verdict"] for item in group}) < 2:
            continue
        ids = sorted(item["id"] for item in group)
        for assessment_id in ids:
            conflicts[assessment_id] = [item for item in ids if item != assessment_id]
    return conflicts


def _current_staleness(stored: dict, current: dict) -> list[dict]:
    details = []
    if stored["claim"]["node_sha256"] != current["claim"]["node_sha256"]:
        details.append(f"claim node changed: {stored['claim']['node_id']}")
    stored_results = {item["node_id"]: item for item in stored["results"]}
    current_results = {item["node_id"]: item for item in current["results"]}
    for result_id in sorted(stored_results):
        before, after = stored_results[result_id], current_results[result_id]
        if before["node_sha256"] != after["node_sha256"]:
            details.append(f"result node changed: {result_id}")
        if before["artifact"] != after["artifact"]:
            details.append(f"result artifact changed: {result_id}")
    if not details:
        return []
    return [_finding("ASSESSMENT_STALE", "error", "; ".join(details))]


def evaluate_assessment(cfg, document: dict,
                        assessments: list[dict] | tuple[dict, ...] | None = None) -> dict:
    """Evaluate an immutable assessment against the current graph and evidence bytes."""
    validate_assessment_document(document)
    store_issues = []
    if assessments is None:
        documents, store_issues = load_assessments(cfg)
    else:
        documents = list(assessments)
    if documents and all(item["id"] != document["id"] for item in documents):
        documents.append(document)
    conflicts = detect_conflicts(documents) if documents else {}
    try:
        current = _snapshot_subject(
            cfg, document["subject"]["claim_id"], document["subject"]["result_ids"],
            document["agent_input"],
        )
        extra = _current_staleness(document["mechanical_snapshot"], current)
    except (engine.GraphError, AssessmentError) as exc:
        current = document["mechanical_snapshot"]
        extra = [_finding("ASSESSMENT_STALE", "error", f"current subject is unavailable: {exc}")]
    if document["id"] in conflicts:
        extra.append(_finding(
            "ASSESSMENT_CONTESTED", "error",
            "conflicting active assessments: " + ", ".join(conflicts[document["id"]]),
        ))
    if store_issues:
        extra.append(_finding(
            "ASSESSMENT_STORE_INTEGRITY", "error",
            f"assessment store has {len(store_issues)} integrity issue(s)",
        ))
    return _derive(current, document["agent_input"], document["review"], extra)


def transition_review(cfg, document: dict, state: str, *, actor: str,
                      assessments: list[dict] | tuple[dict, ...] | None = None,
                      recorded_at: str | None = None) -> dict:
    """Create an immutable review transition that supersedes ``document``.

    Acceptance fails closed when the evidence is stale, contested, invalid, or the
    proposed verdict yields no relation to accept.
    """
    validate_assessment_document(document)
    _bounded_text(actor, "actor", maximum=200)
    if document["review"]["state"] == "proposed" and actor == document["review"]["actor"]:
        raise AssessmentError(
            "proposal and first review must use different actor identities "
            "(identities are self-asserted, not authenticated)"
        )
    if state not in _REVIEW_TRANSITIONS[document["review"]["state"]]:
        raise AssessmentError(
            f"review transition {document['review']['state']} -> {state} is not permitted"
        )
    evaluation = evaluate_assessment(cfg, document, assessments)
    if state == "accepted":
        verdict = document["agent_input"]["verdict"]
        errors = [item for item in evaluation["findings"] if item["severity"] == "error"]
        if verdict in {"supports_as_written", "contradicts_as_written"}:
            blocking = errors
        else:
            hard_blocks = {"EVIDENCE_ANCHOR_INVALID", "ASSESSMENT_STALE",
                           "ASSESSMENT_CONTESTED", "ASSESSMENT_STORE_INTEGRITY",
                           "ALIGNMENT_INCONSISTENT"}
            blocking = [item for item in errors if item["code"] in hard_blocks]
        if blocking:
            codes = ", ".join(sorted({item["code"] for item in blocking}))
            raise AssessmentError(f"assessment cannot be accepted: {codes}")
        existing = list(assessments) if assessments is not None else load_assessments(cfg)[0]
        for other in _active_documents(existing):
            if (other["id"] != document["id"]
                    and other["review"]["state"] == "accepted"
                    and other["subject"] == document["subject"]
                    and other["agent_input"]["verdict"] != verdict):
                raise AssessmentError(
                    "assessment cannot be accepted: ASSESSMENT_CONTESTED with " + other["id"]
                )
    timestamp = recorded_at or _utc_now()
    _validate_recorded_at(timestamp)
    review = {
        "state": state,
        "actor": actor,
        "supersedes_assessment_id": document["id"],
    }
    derived = _derive(document["mechanical_snapshot"], document["agent_input"], review)
    core = {
        "schema_version": SCHEMA_VERSION,
        "recorded_at": timestamp,
        "subject": _json_copy(document["subject"]),
        "mechanical_snapshot": _json_copy(document["mechanical_snapshot"]),
        "agent_input": _json_copy(document["agent_input"]),
        "review": review,
        "derived": derived,
    }
    return {"id": _assessment_id(core), **core}


def append_review_transition(cfg, assessment_id: str, state: str, *, actor: str,
                             recorded_at: str | None = None) -> tuple[dict, Path, dict]:
    """Validate and append one review while holding the store lock end to end.

    Keeping the leaf check, transition policy, and immutable append in one critical
    section prevents two concurrent reviewers from creating sibling successors through
    the supported API. Manually introduced branches remain detectable on load.
    """
    if not isinstance(assessment_id, str) or not ASSESSMENT_ID_RE.fullmatch(assessment_id):
        raise AssessmentError(f"invalid assessment id: {assessment_id!r}")
    root = assessments_path(cfg)
    with _assessment_lock(root):
        documents, issues = load_assessments(cfg)
        if issues:
            detail = "; ".join(
                f"{item.get('path', '<store>')}: {item['detail']}" for item in issues
            )
            raise AssessmentError(f"assessment store integrity failed: {detail}")
        by_id = {item["id"]: item for item in documents}
        document = by_id.get(assessment_id)
        if document is None:
            raise AssessmentError(f"unknown assessment: {assessment_id}")
        superseded = {
            item["review"]["supersedes_assessment_id"] for item in documents
            if item["review"].get("supersedes_assessment_id")
        }
        if assessment_id in superseded or document["review"]["state"] == "superseded":
            raise AssessmentError(
                f"assessment is not a current review-chain leaf: {assessment_id}"
            )
        transition = transition_review(
            cfg, document, state, actor=actor, assessments=documents,
            recorded_at=recorded_at,
        )
        path = _append_assessment_unlocked(root, transition)
        all_documents, appended_issues = load_assessments(cfg)
        if appended_issues:
            detail = "; ".join(
                f"{item.get('path', '<store>')}: {item['detail']}"
                for item in appended_issues
            )
            raise AssessmentError(f"assessment store integrity failed after review: {detail}")
        evaluation = evaluate_assessment(cfg, transition, all_documents)
    return transition, path, evaluation
