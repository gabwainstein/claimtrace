"""Reviewable, content-addressed method-to-code conformance assessments.

This module deliberately keeps three things separate:

* :mod:`provsleuth.pipeline` mechanically resolves the exact contract, method
  specification, code files, and code anchors;
* an external agent supplies only a bounded semantic judgement about those
  declared method-step/code-stage pairs; and
* a distinct, self-asserted reviewer may accept or reject that judgement.

An accepted ``implements`` judgement is current only while the exact resolved
pipeline snapshot is reproducible and every contract stage for the selected
method is assessed as ``match``.  This is a method/code conformance judgement.
It never means that internal stages were observed at runtime or that a method,
result, or scientific claim is valid.
"""
from __future__ import annotations

import copy
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

from .config import strict_json_loads
from .events import (
    EventError,
    _ensure_directory_durable,
    _path_has_reparse_component,
    _stable_bounded_bytes,
)
from .pipeline import (
    PREVIOUS_SNAPSHOT_SCHEMA,
    PipelineError,
    pipeline_snapshots_equivalent,
    resolve_pipeline_contract,
    validate_pipeline_snapshot,
)


SCHEMA_VERSION = "claimtrace.method-conformance-assessment/1"
ASSESSMENT_ID_RE = re.compile(r"^method-assessment:sha256:([0-9a-f]{64})$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RFC3339_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)

VERDICTS = frozenset({
    "implements", "partially_implements", "contradicts", "insufficient",
})
STEP_ALIGNMENTS = frozenset({"match", "partial", "mismatch", "not_found"})
REVIEW_STATES = frozenset({
    "proposed", "accepted", "rejected", "contested", "superseded",
})
_REVIEW_TRANSITIONS = {
    "proposed": {"accepted", "rejected", "contested", "superseded"},
    "accepted": {"contested", "superseded"},
    "rejected": {"superseded"},
    "contested": {"accepted", "rejected", "superseded"},
    "superseded": set(),
}

MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_AGENT_INPUT_BYTES = 2 * 1024 * 1024
MAX_STEP_ALIGNMENTS = 2_000
MAX_LIMITATIONS = 100
MAX_TEXT = 16_384
MAX_LIMITATION_TEXT = 4_096
MAX_IDENTITY_TEXT = 512

_PROCESS_STORE_LOCK = threading.Lock()


class MethodAssessmentError(RuntimeError):
    """A method conformance record is malformed, stale, or unsafe to write."""


def _canonical_bytes(value) -> bytes:
    try:
        text = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise MethodAssessmentError(f"value is not canonical JSON: {exc}") from exc
    return text.encode("utf-8")


def _json_copy(value):
    return json.loads(_canonical_bytes(value).decode("utf-8"))


def _sha256(value) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _assessment_id(core: dict) -> str:
    return f"method-assessment:sha256:{_sha256(core)}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _validate_recorded_at(value: object) -> None:
    if not isinstance(value, str) or not RFC3339_UTC_RE.fullmatch(value):
        raise MethodAssessmentError(
            "recorded_at must be an RFC 3339 UTC timestamp ending in Z"
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise MethodAssessmentError("recorded_at is not a valid timestamp") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise MethodAssessmentError("recorded_at must be UTC")


def _bounded_text(value: object, label: str, *, maximum: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MethodAssessmentError(f"{label} must be non-empty text")
    if len(value) > maximum:
        raise MethodAssessmentError(f"{label} exceeds {maximum} characters")
    return value


def _reject_reasoning_fields(value, path: str = "agent_input") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in {
                "chain_of_thought", "chain-of-thought", "reasoning", "cot",
            }:
                raise MethodAssessmentError(
                    f"{path}.{key} is not permitted; use concise rationale"
                )
            _reject_reasoning_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_reasoning_fields(child, f"{path}[{index}]")


def _validate_agent_input_shape(agent_input: object) -> None:
    if not isinstance(agent_input, dict):
        raise MethodAssessmentError("agent_input must be an object")
    if len(_canonical_bytes(agent_input)) > MAX_AGENT_INPUT_BYTES:
        raise MethodAssessmentError("agent_input exceeds the 2 MiB limit")
    _reject_reasoning_fields(agent_input)
    expected = {
        "verdict", "step_alignments", "rationale", "limitations", "provenance",
    }
    if set(agent_input) != expected:
        raise MethodAssessmentError(
            "agent_input must contain exactly verdict, step_alignments, rationale, "
            "limitations, and provenance"
        )
    if agent_input.get("verdict") not in VERDICTS:
        raise MethodAssessmentError(
            "agent_input.verdict must be one of: " + ", ".join(sorted(VERDICTS))
        )
    _bounded_text(agent_input.get("rationale"), "agent_input.rationale")

    limitations = agent_input.get("limitations")
    if (not isinstance(limitations, list) or len(limitations) > MAX_LIMITATIONS
            or not all(isinstance(item, str) and item.strip() for item in limitations)):
        raise MethodAssessmentError(
            "agent_input.limitations must be a bounded list of non-empty strings"
        )
    for index, limitation in enumerate(limitations):
        _bounded_text(
            limitation, f"agent_input.limitations[{index}]",
            maximum=MAX_LIMITATION_TEXT,
        )

    provenance = agent_input.get("provenance")
    if not isinstance(provenance, dict):
        raise MethodAssessmentError("agent_input.provenance must be an object")
    allowed_provenance = {"agent", "model", "skill_version", "prompt_sha256"}
    if "agent" not in provenance or not set(provenance) <= allowed_provenance:
        raise MethodAssessmentError(
            "agent_input.provenance requires agent and permits only model, "
            "skill_version, and prompt_sha256"
        )
    for field in ("agent", "model", "skill_version"):
        if field in provenance:
            _bounded_text(
                provenance[field], f"agent_input.provenance.{field}",
                maximum=MAX_IDENTITY_TEXT,
            )
    if "prompt_sha256" in provenance and (
            not isinstance(provenance["prompt_sha256"], str)
            or not SHA256_RE.fullmatch(provenance["prompt_sha256"])):
        raise MethodAssessmentError(
            "agent_input.provenance.prompt_sha256 must be a lowercase SHA-256 digest"
        )

    alignments = agent_input.get("step_alignments")
    if (not isinstance(alignments, list) or not alignments
            or len(alignments) > MAX_STEP_ALIGNMENTS):
        raise MethodAssessmentError(
            "agent_input.step_alignments must be a bounded non-empty list"
        )
    seen = set()
    for index, alignment in enumerate(alignments):
        label = f"agent_input.step_alignments[{index}]"
        if (not isinstance(alignment, dict)
                or set(alignment) != {"method_step_id", "stage_id", "alignment"}):
            raise MethodAssessmentError(
                f"{label} must contain exactly method_step_id, stage_id, and alignment"
            )
        method_step_id = _bounded_text(
            alignment.get("method_step_id"), f"{label}.method_step_id",
            maximum=256,
        )
        stage_id = _bounded_text(
            alignment.get("stage_id"), f"{label}.stage_id", maximum=256,
        )
        if alignment.get("alignment") not in STEP_ALIGNMENTS:
            raise MethodAssessmentError(
                f"{label}.alignment must be one of: "
                + ", ".join(sorted(STEP_ALIGNMENTS))
            )
        key = (method_step_id, stage_id)
        if key in seen:
            raise MethodAssessmentError(
                f"agent_input.step_alignments repeats {method_step_id}/{stage_id}"
            )
        seen.add(key)


def _validate_pipeline_snapshot_deep(snapshot: object) -> None:
    """Translate the central pipeline validator's error at this API boundary."""
    try:
        validate_pipeline_snapshot(snapshot)
    except PipelineError as exc:
        raise MethodAssessmentError(f"invalid pipeline snapshot: {exc}") from exc


def _selected_stages(snapshot: dict, method_id: str) -> list[dict]:
    return [stage for stage in snapshot["stages"] if stage["method_id"] == method_id]


def _selected_method(snapshot: dict, method_id: str) -> dict:
    matches = [
        item for item in snapshot["roles"]["methods"] if item["node_id"] == method_id
    ]
    if len(matches) != 1:
        raise MethodAssessmentError(
            f"pipeline contract does not contain exactly one method node {method_id!r}"
        )
    if not _selected_stages(snapshot, method_id):
        raise MethodAssessmentError(
            f"pipeline contract has no stages for method node {method_id!r}"
        )
    return matches[0]


def _validate_agent_alignment_scope(
    agent_input: dict, snapshot: dict, method_id: str,
) -> None:
    stages = _selected_stages(snapshot, method_id)
    expected = {(stage["method_step_id"], stage["id"]) for stage in stages}
    supplied = {
        (item["method_step_id"], item["stage_id"])
        for item in agent_input["step_alignments"]
    }
    if supplied != expected:
        missing = sorted(expected - supplied)
        extra = sorted(supplied - expected)
        detail = []
        if missing:
            detail.append(
                "missing " + ", ".join(f"{step}/{stage}" for step, stage in missing)
            )
        if extra:
            detail.append(
                "unknown " + ", ".join(f"{step}/{stage}" for step, stage in extra)
            )
        raise MethodAssessmentError(
            "agent_input.step_alignments must assess every exact contract stage for "
            f"method {method_id} once ({'; '.join(detail)})"
        )


def validate_agent_input(agent_input: object, snapshot: dict, method_id: str) -> None:
    """Validate external input against the exact mechanically resolved stage set."""
    _validate_agent_input_shape(agent_input)
    assert isinstance(agent_input, dict)
    _selected_method(snapshot, method_id)
    _validate_agent_alignment_scope(agent_input, snapshot, method_id)


def _canonical_agent_input(agent_input: dict, snapshot: dict, method_id: str) -> dict:
    value = _json_copy(agent_input)
    by_pair = {
        (item["method_step_id"], item["stage_id"]): item
        for item in value["step_alignments"]
    }
    value["step_alignments"] = [
        by_pair[(stage["method_step_id"], stage["id"])]
        for stage in _selected_stages(snapshot, method_id)
    ]
    return value


def _mechanical_snapshot(snapshot: dict) -> dict:
    return {"pipeline_contract": _json_copy(snapshot)}


def _finding(code: str, severity: str, detail: str) -> dict:
    return {"code": code, "severity": severity, "detail": detail}


def _policy_findings(mechanical: dict, method_id: str, agent_input: dict) -> list[dict]:
    snapshot = mechanical["pipeline_contract"]
    method = _selected_method(snapshot, method_id)
    stages = _selected_stages(snapshot, method_id)
    required = {
        step["id"] for step in method["method_spec"]["steps"] if step["required"]
    }
    counts = {step_id: 0 for step_id in required}
    for stage in stages:
        if stage["method_step_id"] in counts:
            counts[stage["method_step_id"]] += 1
    findings = []
    incomplete = sorted(step_id for step_id, count in counts.items() if count != 1)
    if incomplete:
        findings.append(_finding(
            "METHOD_REQUIRED_STEP_MAPPING_INVALID", "error",
            "required method steps are not each mapped exactly once: " + ", ".join(incomplete),
        ))

    alignments = [item["alignment"] for item in agent_input["step_alignments"]]
    verdict = agent_input["verdict"]
    if verdict == "implements" and any(item != "match" for item in alignments):
        findings.append(_finding(
            "METHOD_IMPLEMENTATION_ALIGNMENT_INCOMPLETE", "error",
            "implements requires every assessed method-step/code-stage alignment to be match",
        ))
    elif verdict == "partially_implements" and all(item == "match" for item in alignments):
        findings.append(_finding(
            "METHOD_VERDICT_ALIGNMENT_INCONSISTENT", "error",
            "partially_implements conflicts with an all-match step assessment",
        ))
    elif verdict == "contradicts" and "mismatch" not in alignments:
        findings.append(_finding(
            "METHOD_VERDICT_ALIGNMENT_INCONSISTENT", "error",
            "contradicts requires at least one mismatch step alignment",
        ))
    findings.extend([
        _finding(
            "METHOD_STAGE_EXECUTION_NOT_OBSERVED", "info",
            "internal stage execution remains declared_only_not_observed",
        ),
        _finding(
            "METHOD_SCIENTIFIC_VALIDITY_NOT_ASSESSED", "info",
            "method-to-code conformance does not establish scientific validity, result "
            "validity, or claim support",
        ),
    ])
    return findings


def _derive(
    mechanical: dict,
    method_id: str,
    agent_input: dict,
    review: dict,
    extra_findings: list[dict] | None = None,
) -> dict:
    findings = _policy_findings(mechanical, method_id, agent_input)
    findings.extend(_json_copy(extra_findings or []))
    errors = [item for item in findings if item.get("severity") == "error"]
    all_match = all(
        item["alignment"] == "match" for item in agent_input["step_alignments"]
    )
    eligible = agent_input["verdict"] == "implements" and all_match and not errors
    effective_state = "contested" if any(
        item["code"] == "METHOD_ASSESSMENT_CONTESTED" for item in findings
    ) else review["state"]
    active_verdict = (
        agent_input["verdict"]
        if effective_state == "accepted" and not errors
        else None
    )
    return {
        "effective_review_state": effective_state,
        "eligible_for_implements": eligible,
        "active_verdict": active_verdict,
        "implementation_current": active_verdict == "implements" and eligible,
        "stale": any(item["code"] == "METHOD_CONFORMANCE_STALE" for item in findings),
        "stage_execution_observation": "declared_only_not_observed",
        "hidden_intermediates": "not_observed",
        "scientific_validity": "not_assessed",
        "findings": findings,
    }


def create_method_assessment(
    cfg,
    pipeline_contract: str,
    method_id: str,
    agent_input: dict,
    *,
    actor: str,
    declared_inputs: list[str],
    declared_outputs: list[str],
    parameters: dict,
    seeds: dict,
    recorded_at: str | None = None,
) -> dict:
    """Create a proposed assessment while computing every mechanical field locally."""
    _bounded_text(method_id, "method_id", maximum=512)
    _bounded_text(actor, "actor", maximum=MAX_IDENTITY_TEXT)
    _validate_agent_input_shape(agent_input)
    if agent_input["provenance"]["agent"] != actor:
        raise MethodAssessmentError(
            "agent_input.provenance.agent must equal the self-asserted proposal actor"
        )
    try:
        snapshot = resolve_pipeline_contract(
            cfg, pipeline_contract,
            declared_inputs=declared_inputs,
            declared_outputs=declared_outputs,
            parameters=parameters,
            seeds=seeds,
        )
    except PipelineError as exc:
        raise MethodAssessmentError(f"cannot resolve pipeline contract: {exc}") from exc
    _validate_pipeline_snapshot_deep(snapshot)
    validate_agent_input(agent_input, snapshot, method_id)
    agent_input = _canonical_agent_input(agent_input, snapshot, method_id)
    mechanical = _mechanical_snapshot(snapshot)
    timestamp = recorded_at or _utc_now()
    _validate_recorded_at(timestamp)
    subject = {
        "method_id": method_id,
        "pipeline_contract_id": snapshot["id"],
    }
    review = {"state": "proposed", "actor": actor, "supersedes_assessment_id": None}
    derived = _derive(mechanical, method_id, agent_input, review)
    core = {
        "schema_version": SCHEMA_VERSION,
        "recorded_at": timestamp,
        "subject": subject,
        "mechanical_snapshot": mechanical,
        "agent_input": agent_input,
        "review": review,
        "derived": derived,
    }
    document = {"id": _assessment_id(core), **core}
    validate_method_assessment_document(document)
    return document


def _validate_mechanical_snapshot(mechanical: object, subject: dict) -> None:
    if not isinstance(mechanical, dict) or set(mechanical) != {"pipeline_contract"}:
        raise MethodAssessmentError(
            "mechanical_snapshot must contain exactly pipeline_contract"
        )
    snapshot = mechanical["pipeline_contract"]
    _validate_pipeline_snapshot_deep(snapshot)
    if snapshot["id"] != subject["pipeline_contract_id"]:
        raise MethodAssessmentError(
            "mechanical pipeline contract does not match assessment subject"
        )
    _selected_method(snapshot, subject["method_id"])


def validate_method_assessment_document(document: object) -> None:
    """Validate closed schemas, policy-derived fields, and the content address."""
    if not isinstance(document, dict):
        raise MethodAssessmentError("method assessment document must be an object")
    if len(_canonical_bytes(document)) > MAX_DOCUMENT_BYTES:
        raise MethodAssessmentError("method assessment document exceeds the 16 MiB limit")
    expected = {
        "id", "schema_version", "recorded_at", "subject", "mechanical_snapshot",
        "agent_input", "review", "derived",
    }
    if set(document) != expected:
        raise MethodAssessmentError(
            "method assessment has unknown or missing top-level fields"
        )
    if document.get("schema_version") != SCHEMA_VERSION:
        raise MethodAssessmentError(
            f"unsupported method assessment schema: {document.get('schema_version')!r}"
        )
    _validate_recorded_at(document.get("recorded_at"))
    subject = document.get("subject")
    if (not isinstance(subject, dict)
            or set(subject) != {"method_id", "pipeline_contract_id"}
            or not isinstance(subject.get("method_id"), str) or not subject["method_id"]
            or len(subject["method_id"]) > 512
            or not isinstance(subject.get("pipeline_contract_id"), str)):
        raise MethodAssessmentError("method assessment subject is invalid")
    _validate_mechanical_snapshot(document["mechanical_snapshot"], subject)
    snapshot = document["mechanical_snapshot"]["pipeline_contract"]
    validate_agent_input(document["agent_input"], snapshot, subject["method_id"])
    if document["agent_input"] != _canonical_agent_input(
            document["agent_input"], snapshot, subject["method_id"]):
        raise MethodAssessmentError(
            "agent_input.step_alignments are not in canonical pipeline-stage order"
        )

    review = document.get("review")
    if (not isinstance(review, dict)
            or set(review) != {"state", "actor", "supersedes_assessment_id"}
            or review.get("state") not in REVIEW_STATES):
        raise MethodAssessmentError("method assessment review is invalid")
    _bounded_text(review.get("actor"), "review.actor", maximum=MAX_IDENTITY_TEXT)
    supersedes = review.get("supersedes_assessment_id")
    if supersedes is not None and (
            not isinstance(supersedes, str) or not ASSESSMENT_ID_RE.fullmatch(supersedes)):
        raise MethodAssessmentError("review.supersedes_assessment_id is invalid")
    if review["state"] == "proposed" and supersedes is not None:
        raise MethodAssessmentError("a proposed method assessment cannot supersede another")
    if review["state"] != "proposed" and supersedes is None:
        raise MethodAssessmentError("a review decision must supersede an earlier assessment")

    expected_derived = _derive(
        document["mechanical_snapshot"], subject["method_id"],
        document["agent_input"], review,
    )
    if document.get("derived") != expected_derived:
        raise MethodAssessmentError("derived content does not match ProvSleuth policy output")
    if review["state"] == "accepted" and expected_derived["active_verdict"] is None:
        raise MethodAssessmentError("an accepted method assessment must be policy-eligible")
    core = {key: document[key] for key in expected if key != "id"}
    expected_id = _assessment_id(core)
    if document.get("id") != expected_id:
        raise MethodAssessmentError(
            "method assessment id does not match canonical document content"
        )


def method_assessments_path(cfg) -> Path:
    value = getattr(cfg, "method_assessments_path", None)
    if value is None:
        raise MethodAssessmentError("config does not define a method-assessment store")
    return Path(value)


@contextmanager
def _store_lock(path: Path, timeout: float = 30.0):
    lock_root = Path(tempfile.gettempdir()) / "claimtrace-method-assessment-locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(os.path.normcase(str(path.resolve())).encode("utf-8")).hexdigest()
    lock_path = lock_root / f"{key}.lock"
    with _PROCESS_STORE_LOCK, lock_path.open("a+b") as handle:
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
                    raise MethodAssessmentError(
                        "timed out waiting for method-assessment store lock"
                    )
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


def _read_existing(destination: Path, document: dict) -> Path:
    try:
        if _path_has_reparse_component(destination) or not destination.is_file():
            raise MethodAssessmentError(
                f"existing method assessment is not a regular file: {destination.name}"
            )
        existing = strict_json_loads(
            _stable_bounded_bytes(
                destination, MAX_DOCUMENT_BYTES,
            ).decode("utf-8-sig"),
            destination.name,
        )
        validate_method_assessment_document(existing)
    except (
        OSError, UnicodeError, json.JSONDecodeError, ValueError,
        EventError, MethodAssessmentError,
    ) as exc:
        raise MethodAssessmentError(
            f"existing method assessment is unreadable: {destination.name}: {exc}"
        ) from exc
    if _canonical_bytes(existing) != _canonical_bytes(document):
        raise MethodAssessmentError(
            f"refusing to overwrite conflicting method assessment: {destination.name}"
        )
    return destination


def _append_unlocked(root: Path, document: dict) -> Path:
    match = ASSESSMENT_ID_RE.fullmatch(document["id"])
    assert match is not None
    try:
        _ensure_directory_durable(root)
    except EventError as exc:
        raise MethodAssessmentError(
            f"method-assessment store is not trustworthy: {exc}"
        ) from exc
    destination = root / f"{match.group(1)}.json"
    if destination.exists() or destination.is_symlink():
        return _read_existing(destination, document)
    payload = json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    fd, temporary_name = tempfile.mkstemp(
        prefix=".method-assessment-", suffix=".tmp", dir=root,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # A same-directory hard link publishes the fully flushed file atomically
            # and, unlike os.replace(), can never overwrite an existing evidence file.
            os.link(temporary, destination)
        except FileExistsError:
            return _read_existing(destination, document)
        try:
            directory_fd = os.open(root, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
        return destination
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def append_method_assessment(cfg, document: dict) -> Path:
    """Append one immutable assessment with atomic create-without-overwrite semantics."""
    validate_method_assessment_document(document)
    root = method_assessments_path(cfg)
    with _store_lock(root):
        return _append_unlocked(root, document)


def load_method_assessments(cfg) -> tuple[list[dict], list[dict]]:
    """Load valid records and return fail-closed store/chain integrity issues."""
    root = method_assessments_path(cfg)
    if not root.exists() and not root.is_symlink():
        return [], []
    if _path_has_reparse_component(root) or not root.is_dir():
        return [], [{
            "code": "METHOD_ASSESSMENT_STORE_INTEGRITY",
            "path": root.name,
            "detail": "method-assessment store is not a regular directory",
        }]
    documents, issues = [], []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.suffix != ".json":
            issues.append({
                "code": "METHOD_ASSESSMENT_STORE_INTEGRITY",
                "path": path.name,
                "detail": "unexpected non-JSON entry in append-only store",
            })
            continue
        try:
            if _path_has_reparse_component(path) or not path.is_file():
                raise MethodAssessmentError("assessment entry is not a regular file")
            document = strict_json_loads(
                _stable_bounded_bytes(
                    path, MAX_DOCUMENT_BYTES,
                ).decode("utf-8-sig"),
                path.name,
            )
            validate_method_assessment_document(document)
            match = ASSESSMENT_ID_RE.fullmatch(document["id"])
            assert match is not None
            if path.name != match.group(1) + ".json":
                raise MethodAssessmentError("filename does not match assessment id")
            documents.append(document)
        except (
            OSError, UnicodeError, json.JSONDecodeError, ValueError,
            EventError, MethodAssessmentError,
        ) as exc:
            issues.append({
                "code": "METHOD_ASSESSMENT_STORE_INTEGRITY",
                "path": path.name,
                "detail": str(exc),
            })
    documents.sort(key=lambda item: (item["recorded_at"], item["id"]))
    by_id = {item["id"]: item for item in documents}
    successors = {}
    for document in documents:
        predecessor_id = document["review"]["supersedes_assessment_id"]
        if predecessor_id is None:
            continue
        successors.setdefault(predecessor_id, []).append(document)
        predecessor = by_id.get(predecessor_id)
        if predecessor is None:
            issues.append({
                "code": "METHOD_ASSESSMENT_REVIEW_CHAIN",
                "path": ASSESSMENT_ID_RE.fullmatch(document["id"]).group(1) + ".json",
                "detail": f"superseded method assessment is missing: {predecessor_id}",
            })
            continue
        if document["review"]["state"] not in _REVIEW_TRANSITIONS[
                predecessor["review"]["state"]]:
            issues.append({
                "code": "METHOD_ASSESSMENT_REVIEW_CHAIN",
                "path": ASSESSMENT_ID_RE.fullmatch(document["id"]).group(1) + ".json",
                "detail": (
                    "invalid review transition "
                    f"{predecessor['review']['state']} -> {document['review']['state']}"
                ),
            })
        if (predecessor["review"]["state"] == "proposed"
                and predecessor["review"]["actor"] == document["review"]["actor"]):
            issues.append({
                "code": "METHOD_ASSESSMENT_REVIEW_CHAIN",
                "path": ASSESSMENT_ID_RE.fullmatch(document["id"]).group(1) + ".json",
                "detail": (
                    "proposal and first review use the same self-asserted actor identity"
                ),
            })
        for field in ("schema_version", "subject", "mechanical_snapshot", "agent_input"):
            if document[field] != predecessor[field]:
                issues.append({
                    "code": "METHOD_ASSESSMENT_REVIEW_CHAIN",
                    "path": ASSESSMENT_ID_RE.fullmatch(document["id"]).group(1) + ".json",
                    "detail": f"review transition changed immutable field: {field}",
                })
        before = datetime.fromisoformat(predecessor["recorded_at"][:-1] + "+00:00")
        after = datetime.fromisoformat(document["recorded_at"][:-1] + "+00:00")
        if after < before:
            issues.append({
                "code": "METHOD_ASSESSMENT_REVIEW_CHAIN",
                "path": ASSESSMENT_ID_RE.fullmatch(document["id"]).group(1) + ".json",
                "detail": "review transition predates the record it supersedes",
            })
    for predecessor_id, children in sorted(successors.items()):
        if len(children) > 1:
            child_ids = ", ".join(sorted(item["id"] for item in children))
            for child in children:
                issues.append({
                    "code": "METHOD_ASSESSMENT_REVIEW_CHAIN",
                    "path": ASSESSMENT_ID_RE.fullmatch(child["id"]).group(1) + ".json",
                    "detail": f"review chain branches after {predecessor_id}: {child_ids}",
                })
    return documents, issues


def _leaf_documents(documents: list[dict]) -> list[dict]:
    superseded = {
        item["review"]["supersedes_assessment_id"] for item in documents
        if item["review"]["supersedes_assessment_id"] is not None
    }
    return [item for item in documents if item["id"] not in superseded]


def detect_method_assessment_conflicts(documents: list[dict]) -> dict[str, list[str]]:
    """Find accepted leaf judgements with different verdicts for one exact subject."""
    for document in documents:
        validate_method_assessment_document(document)
    grouped = {}
    for document in _leaf_documents(documents):
        if document["review"]["state"] == "accepted":
            key = (
                document["subject"]["pipeline_contract_id"],
                document["subject"]["method_id"],
            )
            grouped.setdefault(key, []).append(document)
    conflicts = {}
    for group in grouped.values():
        if len({item["agent_input"]["verdict"] for item in group}) < 2:
            continue
        ids = sorted(item["id"] for item in group)
        for assessment_id in ids:
            conflicts[assessment_id] = [item for item in ids if item != assessment_id]
    return conflicts


def _resolve_current_snapshot(cfg, stored: dict) -> dict:
    inputs = [
        item["path"] for item in [
            *stored["roles"]["inputs"], *stored["roles"]["code"],
        ]
    ]
    outputs = [item["path"] for item in stored["roles"]["outputs"]]
    parameters = {key: "method-assessment-recheck" for key in stored["required_parameters"]}
    seeds = {key: "method-assessment-recheck" for key in stored["required_seeds"]}
    return resolve_pipeline_contract(
        cfg, stored["source"]["path"],
        declared_inputs=inputs,
        declared_outputs=outputs,
        parameters=parameters,
        seeds=seeds,
        snapshot_schema=stored["schema_version"],
    )


def _role_for_currentness(snapshot: dict, role: str) -> list[dict]:
    items = copy.deepcopy(snapshot["roles"][role])
    if snapshot["schema_version"] == PREVIOUS_SNAPSHOT_SCHEMA:
        for item in items:
            file_snapshot = item.get("file")
            if file_snapshot is not None:
                file_snapshot.pop("mtime_ns")
    return items


def _staleness_details(stored: dict, current: dict, method_id: str) -> list[str]:
    if pipeline_snapshots_equivalent(stored, current):
        return []
    details = []
    if stored["source"] != current["source"]:
        details.append("pipeline contract source changed")
    stored_method = next(
        item for item in _role_for_currentness(stored, "methods")
        if item["node_id"] == method_id
    )
    current_method = next(
        item for item in _role_for_currentness(current, "methods")
        if item["node_id"] == method_id
    )
    if stored_method != current_method:
        details.append("method node, specification, or method file changed")
    if (_role_for_currentness(stored, "code")
            != _role_for_currentness(current, "code")):
        details.append("code node or code file changed")
    if _selected_stages(stored, method_id) != _selected_stages(current, method_id):
        details.append("method-stage mapping or code anchor changed")
    if stored["id"] != current["id"] and not details:
        details.append("another pipeline contract dependency changed")
    return details


def evaluate_method_assessment(
    cfg,
    document: dict,
    assessments: list[dict] | tuple[dict, ...] | None = None,
    store_issues: list[dict] | tuple[dict, ...] | None = None,
) -> dict:
    """Evaluate one immutable judgement against the current locked project bytes."""
    validate_method_assessment_document(document)
    if assessments is None:
        documents, loaded_issues = load_method_assessments(cfg)
        issues = loaded_issues
    else:
        documents = list(assessments)
        issues = list(store_issues or [])
    if all(item["id"] != document["id"] for item in documents):
        documents.append(document)
    extra = []
    stored = document["mechanical_snapshot"]["pipeline_contract"]
    method_id = document["subject"]["method_id"]
    try:
        current = _resolve_current_snapshot(cfg, stored)
        _validate_pipeline_snapshot_deep(current)
        details = _staleness_details(stored, current, method_id)
        if details:
            extra.append(_finding(
                "METHOD_CONFORMANCE_STALE", "error", "; ".join(details),
            ))
    except (PipelineError, MethodAssessmentError) as exc:
        extra.append(_finding(
            "METHOD_CONFORMANCE_STALE", "error",
            f"current pipeline contract cannot reproduce the assessed snapshot: {exc}",
        ))
    conflicts = detect_method_assessment_conflicts(documents)
    if document["id"] in conflicts:
        extra.append(_finding(
            "METHOD_ASSESSMENT_CONTESTED", "error",
            "conflicting accepted method assessments: "
            + ", ".join(conflicts[document["id"]]),
        ))
    if issues:
        extra.append(_finding(
            "METHOD_ASSESSMENT_STORE_INTEGRITY", "error",
            f"method-assessment store has {len(issues)} integrity issue(s)",
        ))
    return _derive(
        document["mechanical_snapshot"], method_id, document["agent_input"],
        document["review"], extra,
    )


def transition_method_review(
    cfg,
    document: dict,
    state: str,
    *,
    actor: str,
    assessments: list[dict] | tuple[dict, ...] | None = None,
    store_issues: list[dict] | tuple[dict, ...] | None = None,
    recorded_at: str | None = None,
) -> dict:
    """Create an immutable review transition; the first reviewer must be distinct."""
    validate_method_assessment_document(document)
    _bounded_text(actor, "actor", maximum=MAX_IDENTITY_TEXT)
    if document["review"]["state"] == "proposed" and actor == document["review"]["actor"]:
        raise MethodAssessmentError(
            "proposal and first review must use different self-asserted actor identities"
        )
    current_state = document["review"]["state"]
    if state not in _REVIEW_TRANSITIONS[current_state]:
        raise MethodAssessmentError(
            f"review transition {current_state} -> {state} is not permitted"
        )
    evaluation = evaluate_method_assessment(
        cfg, document, assessments=assessments, store_issues=store_issues,
    )
    if state == "accepted":
        errors = [item for item in evaluation["findings"] if item["severity"] == "error"]
        if errors:
            codes = ", ".join(sorted({item["code"] for item in errors}))
            raise MethodAssessmentError(
                f"method assessment cannot be accepted: {codes}"
            )
        existing = (
            list(assessments) if assessments is not None
            else load_method_assessments(cfg)[0]
        )
        for other in _leaf_documents(existing):
            if (other["id"] != document["id"]
                    and other["review"]["state"] == "accepted"
                    and other["subject"] == document["subject"]
                    and other["agent_input"]["verdict"] != document["agent_input"]["verdict"]):
                raise MethodAssessmentError(
                    "method assessment cannot be accepted: METHOD_ASSESSMENT_CONTESTED with "
                    + other["id"]
                )
    timestamp = recorded_at or _utc_now()
    _validate_recorded_at(timestamp)
    if datetime.fromisoformat(timestamp[:-1] + "+00:00") < datetime.fromisoformat(
            document["recorded_at"][:-1] + "+00:00"):
        raise MethodAssessmentError("review transition cannot predate its predecessor")
    review = {
        "state": state,
        "actor": actor,
        "supersedes_assessment_id": document["id"],
    }
    derived = _derive(
        document["mechanical_snapshot"], document["subject"]["method_id"],
        document["agent_input"], review,
    )
    core = {
        "schema_version": document["schema_version"],
        "recorded_at": timestamp,
        "subject": _json_copy(document["subject"]),
        "mechanical_snapshot": _json_copy(document["mechanical_snapshot"]),
        "agent_input": _json_copy(document["agent_input"]),
        "review": review,
        "derived": derived,
    }
    transition = {"id": _assessment_id(core), **core}
    validate_method_assessment_document(transition)
    return transition


def append_method_review_transition(
    cfg,
    assessment_id: str,
    state: str,
    *,
    actor: str,
    recorded_at: str | None = None,
) -> tuple[dict, Path, dict]:
    """Check the leaf and append one review while holding the store lock end to end."""
    if not isinstance(assessment_id, str) or not ASSESSMENT_ID_RE.fullmatch(assessment_id):
        raise MethodAssessmentError(f"invalid method assessment id: {assessment_id!r}")
    root = method_assessments_path(cfg)
    with _store_lock(root):
        documents, issues = load_method_assessments(cfg)
        if issues:
            detail = "; ".join(
                f"{item.get('path', '<store>')}: {item['detail']}" for item in issues
            )
            raise MethodAssessmentError(
                f"method-assessment store integrity failed: {detail}"
            )
        by_id = {item["id"]: item for item in documents}
        document = by_id.get(assessment_id)
        if document is None:
            raise MethodAssessmentError(f"unknown method assessment: {assessment_id}")
        if document not in _leaf_documents(documents):
            raise MethodAssessmentError(
                f"method assessment is not a current review-chain leaf: {assessment_id}"
            )
        transition = transition_method_review(
            cfg, document, state, actor=actor, assessments=documents,
            store_issues=issues, recorded_at=recorded_at,
        )
        path = _append_unlocked(root, transition)
        all_documents, appended_issues = load_method_assessments(cfg)
        if appended_issues:
            detail = "; ".join(
                f"{item.get('path', '<store>')}: {item['detail']}"
                for item in appended_issues
            )
            raise MethodAssessmentError(
                f"method-assessment store integrity failed after review: {detail}"
            )
        evaluation = evaluate_method_assessment(
            cfg, transition, assessments=all_documents, store_issues=appended_issues,
        )
    return transition, path, evaluation


def method_assessment_statuses(cfg) -> tuple[list[dict], list[dict]]:
    """Return concise statuses for current review-chain leaves plus store issues."""
    documents, issues = load_method_assessments(cfg)
    statuses = []
    for document in _leaf_documents(documents):
        evaluation = evaluate_method_assessment(
            cfg, document, assessments=documents, store_issues=issues,
        )
        statuses.append({
            "id": document["id"],
            "recorded_at": document["recorded_at"],
            "method_id": document["subject"]["method_id"],
            "pipeline_contract_id": document["subject"]["pipeline_contract_id"],
            "review_state": document["review"]["state"],
            "effective_review_state": evaluation["effective_review_state"],
            "verdict": document["agent_input"]["verdict"],
            "active_verdict": evaluation["active_verdict"],
            "eligible_for_implements": evaluation["eligible_for_implements"],
            "implementation_current": evaluation["implementation_current"],
            "stale": evaluation["stale"],
            "stage_execution_observation": evaluation["stage_execution_observation"],
            "scientific_validity": evaluation["scientific_validity"],
            "finding_codes": [item["code"] for item in evaluation["findings"]],
        })
    statuses.sort(key=lambda item: (item["recorded_at"], item["id"]))
    return statuses, issues


__all__ = [
    "SCHEMA_VERSION", "VERDICTS", "STEP_ALIGNMENTS", "REVIEW_STATES",
    "MethodAssessmentError", "create_method_assessment",
    "validate_method_assessment_document", "validate_agent_input",
    "append_method_assessment", "load_method_assessments",
    "evaluate_method_assessment", "transition_method_review",
    "append_method_review_transition", "detect_method_assessment_conflicts",
    "method_assessment_statuses", "method_assessments_path",
]
