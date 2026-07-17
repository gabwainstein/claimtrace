"""Deterministic contracts for opaque, multi-stage analysis programs.

The contract is intentionally a declaration layer.  It pins the exact graph nodes,
method specifications, code bytes, and text anchors that a reviewer should inspect,
but it never claims that an internal stage was observed at runtime.  Mechanical run
receipts and replay certificates consume the resolved snapshot from this module.
"""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import re
import secrets
import stat
import threading
from pathlib import Path, PurePosixPath, PureWindowsPath

from .config import strict_json_loads
from .engine import load_graph, load_raw


CONTRACT_SCHEMA = "claimtrace.pipeline-contract/1"
LEGACY_SNAPSHOT_SCHEMA = "claimtrace.pipeline-contract-snapshot/1"
PREVIOUS_SNAPSHOT_SCHEMA = "claimtrace.pipeline-contract-snapshot/2"
SNAPSHOT_SCHEMA = "claimtrace.pipeline-contract-snapshot/3"
SUPPORTED_SNAPSHOT_SCHEMAS = frozenset({
    LEGACY_SNAPSHOT_SCHEMA, PREVIOUS_SNAPSHOT_SCHEMA, SNAPSHOT_SCHEMA,
})
_INTERMEDIATE_SNAPSHOT_SCHEMAS = frozenset({
    PREVIOUS_SNAPSHOT_SCHEMA, SNAPSHOT_SCHEMA,
})
_MTIME_SNAPSHOT_SCHEMAS = frozenset({
    LEGACY_SNAPSHOT_SCHEMA, PREVIOUS_SNAPSHOT_SCHEMA,
})
METHOD_SPEC_SCHEMA = "claimtrace.method-spec/1"
CONTRACT_ID_RE = re.compile(r"^pipeline-contract:sha256:[0-9a-f]{64}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_CONTRACT_BYTES = 4 * 1024 * 1024
MAX_STAGES = 2_000
MAX_NODE_ROLES = 20_000
MAX_ANCHORS = 20_000
STAGE_CHECKPOINT_SCHEMA = "claimtrace.stage-checkpoint/1"
STAGE_TRACE_SCHEMA = "claimtrace.stage-trace/1"
STAGE_TRACE_PLAN_SCHEMA = "claimtrace.stage-trace-plan/1"
STAGE_TRACE_MODE = "cooperative_child_checkpoint_log"
STAGE_TRACE_TRUST = "cooperative_child_self_report_not_independent_observation"
STAGE_TRACE_PATH_ENV = "PROVSLEUTH_STAGE_TRACE_PATH"
STAGE_TRACE_NONCE_ENV = "PROVSLEUTH_STAGE_TRACE_NONCE"
STAGE_TRACE_CONTRACT_ENV = "PROVSLEUTH_PIPELINE_CONTRACT_ID"
STAGE_TRACE_ROOT_ENV = "PROVSLEUTH_STAGE_SOURCE_ROOT"
LEGACY_STAGE_TRACE_PATH_ENV = "CLAIMTRACE_STAGE_TRACE_PATH"
LEGACY_STAGE_TRACE_NONCE_ENV = "CLAIMTRACE_STAGE_TRACE_NONCE"
LEGACY_STAGE_TRACE_CONTRACT_ENV = "CLAIMTRACE_PIPELINE_CONTRACT_ID"
LEGACY_STAGE_TRACE_ROOT_ENV = "CLAIMTRACE_STAGE_SOURCE_ROOT"
STAGE_TRACE_ENVIRONMENT_KEYS = (
    STAGE_TRACE_PATH_ENV,
    STAGE_TRACE_NONCE_ENV,
    STAGE_TRACE_CONTRACT_ENV,
    STAGE_TRACE_ROOT_ENV,
    LEGACY_STAGE_TRACE_PATH_ENV,
    LEGACY_STAGE_TRACE_NONCE_ENV,
    LEGACY_STAGE_TRACE_CONTRACT_ENV,
    LEGACY_STAGE_TRACE_ROOT_ENV,
)
MAX_STAGE_CHECKPOINT_BYTES = 64 * 1024
MAX_STAGE_TRACE_BYTES = 1 * 1024 * 1024
_STAGE_TRACE_NONCE_RE = re.compile(r"^[0-9a-f]{64}$")
_STAGE_CHECKPOINT_WRITE_LOCK = threading.Lock()

_LEGACY_SNAPSHOT_COVERAGE = {
    "boundary_files": "exact_declared_paths_and_sha256",
    "code": "exact_files_text_anchors_and_declared_entrypoint",
    "methods": "exact_nodes_and_optional_files",
    "stage_execution": "declared_only_not_observed",
    "hidden_intermediates": "not_observed",
    "semantic_equivalence": "requires_separate_review",
}
_SNAPSHOT_COVERAGE = {
    **_LEGACY_SNAPSHOT_COVERAGE,
    "boundary_files": (
        "declared_paths_and_graph_roles_input_bytes_captured_by_run_receipt"
    ),
    "materialized_intermediates": (
        "declared_file_paths_for_process_boundary_capture_not_stage_attributed"
    ),
    "in_memory_intermediates": "not_observed",
}


class PipelineError(RuntimeError):
    """A pipeline contract is malformed, stale, or inconsistent with a run."""


def _stage_trace_environment() -> dict[str, str] | None:
    def value(current: str, legacy: str, label: str) -> str | None:
        current_value = os.environ.get(current)
        legacy_value = os.environ.get(legacy)
        if (current_value is not None and legacy_value is not None
                and current_value != legacy_value):
            raise PipelineError(
                f"conflicting ProvSleuth and legacy Claimtrace {label} environment values"
            )
        return current_value if current_value is not None else legacy_value

    values = {
        "path": value(
            STAGE_TRACE_PATH_ENV, LEGACY_STAGE_TRACE_PATH_ENV, "stage-trace path",
        ),
        "nonce": value(
            STAGE_TRACE_NONCE_ENV, LEGACY_STAGE_TRACE_NONCE_ENV,
            "stage-trace nonce",
        ),
        "contract_id": value(
            STAGE_TRACE_CONTRACT_ENV, LEGACY_STAGE_TRACE_CONTRACT_ENV,
            "pipeline-contract",
        ),
        "source_root": value(
            STAGE_TRACE_ROOT_ENV, LEGACY_STAGE_TRACE_ROOT_ENV,
            "stage source-root",
        ),
    }
    present = {key for key, value in values.items() if value is not None}
    if not present:
        return None
    if present != set(values):
        raise PipelineError(
            "ProvSleuth stage-checkpoint environment is incomplete; refusing a partial trace"
        )
    if not Path(values["path"]).is_absolute():
        raise PipelineError("ProvSleuth stage-checkpoint path must be absolute")
    if not _STAGE_TRACE_NONCE_RE.fullmatch(values["nonce"]):
        raise PipelineError("ProvSleuth stage-checkpoint nonce is invalid")
    if not CONTRACT_ID_RE.fullmatch(values["contract_id"]):
        raise PipelineError("ProvSleuth stage-checkpoint contract id is invalid")
    if not Path(values["source_root"]).is_absolute():
        raise PipelineError("ProvSleuth stage-checkpoint source root must be absolute")
    return values


def stage_checkpoint(stage_id: str) -> bool:
    """Emit one cooperative, run-bound stage checkpoint when tracing is enabled.

    Outside a ProvSleuth-instrumented child this is a no-op and returns ``False``.
    Inside one, it appends a bounded canonical JSON record to the controller-created
    private channel and returns ``True``.  The record means only that the program
    reached this callsite; it is not independent proof that the declared computation
    occurred or that the stage had its intended scientific meaning.
    """
    environment = _stage_trace_environment()
    if environment is None:
        return False
    stage_id = _bounded_token(stage_id, "stage checkpoint id")
    frame = inspect.currentframe()
    caller = frame.f_back if frame is not None else None
    try:
        if caller is None:
            raise PipelineError("stage checkpoint caller could not be resolved")
        source_root = Path(environment["source_root"]).resolve()
        caller_path = Path(caller.f_code.co_filename).resolve()
        try:
            relative_path = caller_path.relative_to(source_root).as_posix()
        except ValueError as exc:
            raise PipelineError(
                "stage checkpoint callsite is outside the declared project root"
            ) from exc
        if not relative_path or relative_path.startswith("../"):
            raise PipelineError("stage checkpoint callsite path is not project-relative")
        record = {
            "schema_version": STAGE_CHECKPOINT_SCHEMA,
            "nonce": environment["nonce"],
            "pipeline_contract_id": environment["contract_id"],
            "stage_id": stage_id,
            "reporter_pid": os.getpid(),
            "callsite": {"path": relative_path, "line": caller.f_lineno},
        }
        try:
            encoded = canonical_bytes(record) + b"\n"
        except (TypeError, ValueError, RecursionError) as exc:
            raise PipelineError(f"stage checkpoint is not canonical JSON: {exc}") from exc
        if len(encoded) > MAX_STAGE_CHECKPOINT_BYTES:
            raise PipelineError(
                f"stage checkpoint exceeds {MAX_STAGE_CHECKPOINT_BYTES} bytes"
            )
        flags = os.O_WRONLY | os.O_APPEND
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        with _STAGE_CHECKPOINT_WRITE_LOCK:
            try:
                descriptor = os.open(environment["path"], flags)
                try:
                    written = os.write(descriptor, encoded)
                    if written != len(encoded):
                        raise PipelineError("stage checkpoint append was incomplete")
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except OSError as exc:
                raise PipelineError(f"stage checkpoint append failed: {exc}") from exc
    finally:
        del caller
        del frame
    return True


def stage_trace_plan(snapshot: dict) -> dict:
    """Return the closed, deterministic checkpoint policy for a resolved contract."""
    validate_pipeline_snapshot(snapshot)
    return {
        "schema_version": STAGE_TRACE_PLAN_SCHEMA,
        "record_schema": STAGE_CHECKPOINT_SCHEMA,
        "mode": STAGE_TRACE_MODE,
        "requirement": "all_contract_stages_once_after_dependencies",
        "transport": "controller_private_jsonl_environment",
        "reporter_scope": "direct_child_process_id_checked",
        "trust": STAGE_TRACE_TRUST,
        "required_stage_ids": [stage["id"] for stage in snapshot["stages"]],
    }


def prepare_stage_trace(path: Path, snapshot: dict, source_root: Path) -> dict:
    """Create one exclusive private checkpoint channel and its child environment."""
    plan = stage_trace_plan(snapshot)
    path = Path(path)
    source_root = Path(source_root).resolve()
    if not path.is_absolute() or not source_root.is_absolute():
        raise PipelineError("stage checkpoint channel and source root must be absolute")
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(path, flags, 0o600)
        try:
            identity = os.fstat(descriptor)
            if not stat.S_ISREG(identity.st_mode):
                raise PipelineError("stage checkpoint channel is not a regular file")
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise PipelineError(f"stage checkpoint channel could not be created: {exc}") from exc
    nonce = secrets.token_hex(32)
    return {
        "path": path,
        "source_root": source_root,
        "nonce": nonce,
        "nonce_sha256": hashlib.sha256(nonce.encode("ascii")).hexdigest(),
        "pipeline_contract_id": snapshot["id"],
        "identity": (identity.st_dev, identity.st_ino, stat.S_IFMT(identity.st_mode)),
        "plan": plan,
    }


def stage_trace_child_environment(capture: dict) -> dict[str, str]:
    """Return only the environment values a cooperative child needs to report."""
    return {
        STAGE_TRACE_PATH_ENV: str(capture["path"]),
        STAGE_TRACE_NONCE_ENV: capture["nonce"],
        STAGE_TRACE_CONTRACT_ENV: capture["pipeline_contract_id"],
        STAGE_TRACE_ROOT_ENV: str(capture["source_root"]),
    }


def scrub_stage_trace_environment(environment: dict[str, str] | None = None) -> dict[str, str]:
    """Return a child environment with every reserved checkpoint binding removed."""
    cleaned = dict(os.environ if environment is None else environment)
    for key in STAGE_TRACE_ENVIRONMENT_KEYS:
        cleaned.pop(key, None)
    return cleaned


def _stage_trace_issue(issues: list[dict], code: str, detail: str) -> None:
    issues.append({"code": code, "detail": detail[:2_000]})


def _stable_stage_trace_bytes(capture: dict) -> tuple[bytes | None, list[dict]]:
    path = Path(capture["path"])
    issues: list[dict] = []
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        before = path.lstat()
        expected = tuple(capture["identity"])
        actual = (before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode))
        if actual != expected or not stat.S_ISREG(before.st_mode):
            _stage_trace_issue(
                issues, "STAGE_TRACE_CHANNEL_REPLACED",
                "controller-created checkpoint channel identity changed",
            )
            return None, issues
        descriptor = os.open(path, flags)
        try:
            opened_before = os.fstat(descriptor)
            opened_identity = (
                opened_before.st_dev, opened_before.st_ino,
                stat.S_IFMT(opened_before.st_mode),
            )
            if opened_identity != expected or not stat.S_ISREG(opened_before.st_mode):
                _stage_trace_issue(
                    issues, "STAGE_TRACE_CHANNEL_REPLACED",
                    "opened checkpoint channel identity differs from its creation identity",
                )
                return None, issues
            chunks = []
            remaining = MAX_STAGE_TRACE_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            opened_after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after = path.lstat()
        after_identity = (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode))
        if (opened_identity != (
                opened_after.st_dev, opened_after.st_ino,
                stat.S_IFMT(opened_after.st_mode),
        ) or after_identity != expected or opened_before.st_size != opened_after.st_size
                or len(data) != opened_after.st_size):
            _stage_trace_issue(
                issues, "STAGE_TRACE_CHANNEL_UNSTABLE",
                "checkpoint channel changed while it was being finalized",
            )
            return None, issues
        if len(data) > MAX_STAGE_TRACE_BYTES:
            _stage_trace_issue(
                issues, "STAGE_TRACE_TOO_LARGE",
                f"checkpoint channel exceeds {MAX_STAGE_TRACE_BYTES} bytes",
            )
            return None, issues
        return data, issues
    except (OSError, ValueError) as exc:
        _stage_trace_issue(
            issues, "STAGE_TRACE_CHANNEL_UNREADABLE",
            f"checkpoint channel could not be finalized: {type(exc).__name__}: {exc}",
        )
        return None, issues


def finalize_stage_trace(
    capture: dict,
    snapshot: dict,
    *,
    launched: bool,
    expected_reporter_pid: int | None = None,
) -> dict:
    """Normalize and validate one cooperative child checkpoint journal.

    This checks protocol binding, exact contract stages, dependency order, uniqueness,
    and anchored callsites.  It intentionally does not upgrade the trace into an
    independent observation of the stage computation.
    """
    validate_pipeline_snapshot(snapshot)
    expected_plan = stage_trace_plan(snapshot)
    if capture.get("plan") != expected_plan:
        raise PipelineError("stage checkpoint capture plan differs from the contract")
    if capture.get("pipeline_contract_id") != snapshot["id"]:
        raise PipelineError("stage checkpoint capture contract id differs from the contract")
    if launched:
        if (isinstance(expected_reporter_pid, bool)
                or not isinstance(expected_reporter_pid, int)
                or expected_reporter_pid < 1):
            raise PipelineError(
                "launched stage checkpoint capture requires the direct child process id"
            )
    elif expected_reporter_pid is not None:
        raise PipelineError(
            "not-launched stage checkpoint capture cannot have a reporter process id"
        )
    data, issues = _stable_stage_trace_bytes(capture)
    raw = data if data is not None else b""
    checkpoints = []
    structural_invalid = bool(issues)
    if data is not None and data and not data.endswith(b"\n"):
        _stage_trace_issue(
            issues, "STAGE_TRACE_TRUNCATED",
            "checkpoint journal does not end with a complete JSON line",
        )
        structural_invalid = True
    lines = data.splitlines() if data is not None else []
    if len(lines) > MAX_STAGES:
        _stage_trace_issue(
            issues, "STAGE_TRACE_RECORD_LIMIT",
            f"checkpoint journal exceeds the {MAX_STAGES}-record limit",
        )
        structural_invalid = True
        lines = lines[:MAX_STAGES]
    stage_by_id = {stage["id"]: stage for stage in snapshot["stages"]}
    code_path_by_id = {
        item["node_id"]: item["path"] for item in snapshot["roles"]["code"]
    }
    seen: set[str] = set()
    for index, line in enumerate(lines, start=1):
        if not line or len(line) + 1 > MAX_STAGE_CHECKPOINT_BYTES:
            _stage_trace_issue(
                issues, "STAGE_TRACE_RECORD_SIZE",
                f"checkpoint record {index} is empty or exceeds its byte limit",
            )
            structural_invalid = True
            continue
        try:
            document = strict_json_loads(
                line.decode("utf-8"), f"stage checkpoint record {index}",
            )
        except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
            _stage_trace_issue(
                issues, "STAGE_TRACE_RECORD_INVALID_JSON",
                f"checkpoint record {index} is invalid JSON: {exc}",
            )
            structural_invalid = True
            continue
        expected_keys = {
            "schema_version", "nonce", "pipeline_contract_id", "stage_id",
            "reporter_pid", "callsite",
        }
        if not isinstance(document, dict) or set(document) != expected_keys:
            _stage_trace_issue(
                issues, "STAGE_TRACE_RECORD_SHAPE",
                f"checkpoint record {index} has fields outside the closed protocol",
            )
            structural_invalid = True
            continue
        if document.get("schema_version") != STAGE_CHECKPOINT_SCHEMA:
            _stage_trace_issue(
                issues, "STAGE_TRACE_RECORD_SCHEMA",
                f"checkpoint record {index} has an unsupported schema",
            )
            structural_invalid = True
            continue
        if (document.get("nonce") != capture["nonce"]
                or document.get("pipeline_contract_id") != snapshot["id"]):
            _stage_trace_issue(
                issues, "STAGE_TRACE_BINDING_MISMATCH",
                f"checkpoint record {index} is not bound to this execution",
            )
            structural_invalid = True
            continue
        reporter_pid = document.get("reporter_pid")
        if (isinstance(reporter_pid, bool) or not isinstance(reporter_pid, int)
                or reporter_pid < 1):
            _stage_trace_issue(
                issues, "STAGE_TRACE_REPORTER_PROCESS_INVALID",
                f"checkpoint record {index} has an invalid reporter process id",
            )
            structural_invalid = True
            continue
        if reporter_pid != expected_reporter_pid:
            _stage_trace_issue(
                issues, "STAGE_TRACE_REPORTER_PROCESS_MISMATCH",
                f"checkpoint record {index} was not emitted by the direct child process",
            )
            structural_invalid = True
            continue
        stage_id = document.get("stage_id")
        stage = stage_by_id.get(stage_id)
        if stage is None:
            _stage_trace_issue(
                issues, "STAGE_TRACE_UNKNOWN_STAGE",
                f"checkpoint record {index} names unknown stage {stage_id!r}",
            )
            structural_invalid = True
            continue
        if stage_id in seen:
            _stage_trace_issue(
                issues, "STAGE_TRACE_DUPLICATE_STAGE",
                f"stage {stage_id} was checkpointed more than once",
            )
            structural_invalid = True
            continue
        missing_dependencies = sorted(set(stage["depends_on"]) - seen)
        if missing_dependencies:
            _stage_trace_issue(
                issues, "STAGE_TRACE_DEPENDENCY_ORDER",
                f"stage {stage_id} was checkpointed before dependencies "
                + ", ".join(missing_dependencies),
            )
            structural_invalid = True
            continue
        callsite = document.get("callsite")
        if (not isinstance(callsite, dict) or set(callsite) != {"path", "line"}
                or not isinstance(callsite.get("path"), str)
                or not callsite["path"]
                or isinstance(callsite.get("line"), bool)
                or not isinstance(callsite.get("line"), int)
                or callsite["line"] < 1):
            _stage_trace_issue(
                issues, "STAGE_TRACE_CALLSITE_INVALID",
                f"checkpoint record {index} has an invalid callsite",
            )
            structural_invalid = True
            continue
        matched_code_node = None
        for anchor in stage["code_anchors"]:
            path = code_path_by_id.get(anchor["code_node_id"])
            if (path == callsite["path"]
                    and anchor["start_line"] <= callsite["line"] <= anchor["end_line"]):
                matched_code_node = anchor["code_node_id"]
                break
        if matched_code_node is None:
            _stage_trace_issue(
                issues, "STAGE_TRACE_CALLSITE_UNANCHORED",
                f"stage {stage_id} checkpoint callsite is outside its locked code anchors",
            )
            structural_invalid = True
            continue
        seen.add(stage_id)
        checkpoints.append({
            "sequence": len(checkpoints) + 1,
            "stage_id": stage_id,
            "code_node_id": matched_code_node,
            "callsite": {"path": callsite["path"], "line": callsite["line"]},
            "observation": "program_emitted_checkpoint_reached",
        })
    missing = [stage_id for stage_id in expected_plan["required_stage_ids"] if stage_id not in seen]
    if launched and missing:
        _stage_trace_issue(
            issues, "STAGE_TRACE_MISSING_STAGES",
            "missing cooperative checkpoints for: " + ", ".join(missing),
        )
    if not launched:
        state = "cooperative_report_not_attempted"
    elif structural_invalid:
        state = "cooperative_report_invalid"
    elif missing:
        state = "cooperative_report_incomplete"
    else:
        state = "cooperative_report_complete"
    trace = {
        "schema_version": STAGE_TRACE_SCHEMA,
        "mode": STAGE_TRACE_MODE,
        "state": state,
        "trust": STAGE_TRACE_TRUST,
        "binding": {
            "nonce_sha256": capture["nonce_sha256"],
            "transport": "controller_created_private_file",
            "reporter_scope": "direct_child_process_id_checked",
            "reporter_pid": expected_reporter_pid,
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "raw_size": len(raw),
        },
        "required_stage_ids": list(expected_plan["required_stage_ids"]),
        "checkpoints": checkpoints,
        "checkpoint_sequence_sha256": canonical_sha256(checkpoints),
        "issues": issues,
    }
    validate_stage_trace(trace)
    return trace


def validate_stage_trace(trace: object) -> str:
    """Validate the closed stored stage-trace projection and return its sequence digest."""
    expected = {
        "schema_version", "mode", "state", "trust", "binding",
        "required_stage_ids", "checkpoints", "checkpoint_sequence_sha256", "issues",
    }
    if not isinstance(trace, dict) or set(trace) != expected:
        raise PipelineError("stage trace fields differ from the closed schema")
    if (trace.get("schema_version") != STAGE_TRACE_SCHEMA
            or trace.get("mode") != STAGE_TRACE_MODE
            or trace.get("trust") != STAGE_TRACE_TRUST):
        raise PipelineError("stage trace schema, mode, or trust boundary is invalid")
    if trace.get("state") not in {
        "cooperative_report_not_attempted", "cooperative_report_complete",
        "cooperative_report_incomplete", "cooperative_report_invalid",
    }:
        raise PipelineError("stage trace state is invalid")
    required = trace.get("required_stage_ids")
    if (not isinstance(required, list) or not required or len(required) > MAX_STAGES
            or not all(isinstance(item, str) and TOKEN_RE.fullmatch(item) for item in required)
            or len(required) != len(set(required))):
        raise PipelineError("stage trace required stage ids are invalid")
    binding = trace.get("binding")
    if (not isinstance(binding, dict)
            or set(binding) != {
                "nonce_sha256", "transport", "reporter_scope",
                "reporter_pid", "raw_sha256", "raw_size",
            }
            or not _STAGE_TRACE_NONCE_RE.fullmatch(str(binding.get("nonce_sha256")))
            or binding.get("transport") != "controller_created_private_file"
            or binding.get("reporter_scope") != "direct_child_process_id_checked"
            or (
                binding.get("reporter_pid") is not None
                and (
                    isinstance(binding.get("reporter_pid"), bool)
                    or not isinstance(binding.get("reporter_pid"), int)
                    or binding["reporter_pid"] < 1
                )
            )
            or not SHA256_RE.fullmatch(str(binding.get("raw_sha256")))
            or isinstance(binding.get("raw_size"), bool)
            or not isinstance(binding.get("raw_size"), int)
            or not 0 <= binding["raw_size"] <= MAX_STAGE_TRACE_BYTES):
        raise PipelineError("stage trace binding is invalid")
    checkpoints = trace.get("checkpoints")
    if not isinstance(checkpoints, list) or len(checkpoints) > MAX_STAGES:
        raise PipelineError("stage trace checkpoints are invalid")
    seen = set()
    for index, checkpoint in enumerate(checkpoints, start=1):
        if (not isinstance(checkpoint, dict)
                or set(checkpoint) != {
                    "sequence", "stage_id", "code_node_id", "callsite", "observation",
                }
                or checkpoint.get("sequence") != index
                or checkpoint.get("observation") != "program_emitted_checkpoint_reached"
                or not isinstance(checkpoint.get("stage_id"), str)
                or not TOKEN_RE.fullmatch(checkpoint["stage_id"])
                or not isinstance(checkpoint.get("code_node_id"), str)
                or not TOKEN_RE.fullmatch(checkpoint["code_node_id"])):
            raise PipelineError(f"stage trace checkpoint {index} is invalid")
        callsite = checkpoint.get("callsite")
        if (not isinstance(callsite, dict) or set(callsite) != {"path", "line"}
                or not isinstance(callsite.get("path"), str) or not callsite["path"]
                or isinstance(callsite.get("line"), bool)
                or not isinstance(callsite.get("line"), int) or callsite["line"] < 1):
            raise PipelineError(f"stage trace checkpoint {index} callsite is invalid")
        if checkpoint["stage_id"] in seen:
            raise PipelineError("stage trace contains duplicate normalized stages")
        seen.add(checkpoint["stage_id"])
    issues = trace.get("issues")
    if (not isinstance(issues, list) or len(issues) > MAX_STAGES * 4
            or not all(
                isinstance(item, dict) and set(item) == {"code", "detail"}
                and isinstance(item["code"], str) and TOKEN_RE.fullmatch(item["code"])
                and isinstance(item["detail"], str)
                for item in issues
            )):
        raise PipelineError("stage trace issues are invalid")
    digest = canonical_sha256(checkpoints)
    if trace.get("checkpoint_sequence_sha256") != digest:
        raise PipelineError("stage trace checkpoint sequence digest is invalid")
    if trace["state"] == "cooperative_report_complete":
        checkpoint_ids = [item["stage_id"] for item in checkpoints]
        if issues or len(checkpoint_ids) != len(required) or set(checkpoint_ids) != set(required):
            raise PipelineError("complete stage trace must contain every required stage exactly once")
    elif trace["state"] == "cooperative_report_not_attempted" and checkpoints:
        raise PipelineError("not-attempted stage trace cannot contain checkpoints")
    elif trace["state"] in {
            "cooperative_report_incomplete", "cooperative_report_invalid"} and not issues:
        raise PipelineError("incomplete or invalid stage trace must contain issues")
    if (trace["state"] == "cooperative_report_not_attempted") != (
            binding["reporter_pid"] is None):
        raise PipelineError(
            "stage trace reporter process id contradicts whether a child was launched"
        )
    return digest


def validate_stage_trace_against_snapshot(trace: object, snapshot: object) -> str:
    """Validate normalized checkpoints against one exact pipeline snapshot."""
    digest = validate_stage_trace(trace)
    validate_pipeline_snapshot(snapshot)
    expected = stage_trace_plan(snapshot)["required_stage_ids"]
    if trace["required_stage_ids"] != expected:
        raise PipelineError("stage trace required stages differ from the pipeline snapshot")
    stage_by_id = {stage["id"]: stage for stage in snapshot["stages"]}
    code_path_by_id = {
        item["node_id"]: item["path"] for item in snapshot["roles"]["code"]
    }
    seen = set()
    for checkpoint in trace["checkpoints"]:
        stage = stage_by_id.get(checkpoint["stage_id"])
        if stage is None:
            raise PipelineError("stage trace checkpoint names an unknown pipeline stage")
        if not set(stage["depends_on"]).issubset(seen):
            raise PipelineError("stage trace checkpoint violates pipeline dependency order")
        callsite = checkpoint["callsite"]
        code_node_id = checkpoint["code_node_id"]
        anchored = any(
            anchor["code_node_id"] == code_node_id
            and code_path_by_id.get(code_node_id) == callsite["path"]
            and anchor["start_line"] <= callsite["line"] <= anchor["end_line"]
            for anchor in stage["code_anchors"]
        )
        if not anchored:
            raise PipelineError("stage trace checkpoint callsite is outside its locked anchor")
        seen.add(stage["id"])
    if (trace["state"] == "cooperative_report_complete"
            and seen != set(expected)):
        raise PipelineError("complete stage trace does not cover every pipeline stage")
    return digest


_ACTIVE_NODE_STATUSES = frozenset({None, "current", "confirmed"})


def canonical_bytes(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _bounded_token(value, label: str) -> str:
    if not isinstance(value, str) or not TOKEN_RE.fullmatch(value):
        raise PipelineError(
            f"{label} must be a 1-256 character identifier using letters, digits, . _ : or -"
        )
    return value


def _string_list(value, label: str, *, maximum: int = MAX_NODE_ROLES,
                 allow_empty: bool = True) -> list[str]:
    if (not isinstance(value, list) or len(value) > maximum
            or (not allow_empty and not value)
            or not all(isinstance(item, str) and item for item in value)):
        qualifier = "non-empty " if not allow_empty else ""
        raise PipelineError(f"{label} must be a bounded {qualifier}string list")
    if len(value) != len(set(value)):
        raise PipelineError(f"{label} contains duplicate values")
    return list(value)


def _node_snapshot(node: dict) -> dict:
    digest = canonical_sha256(node)
    return {
        "node_id": node["id"],
        "node_sha256": digest,
        "node_version_id": f"node:sha256:{digest}",
    }


def _project_label(cfg, path: Path) -> str:
    try:
        return path.resolve().relative_to(cfg.root.resolve()).as_posix()
    except ValueError as exc:
        raise PipelineError(f"pipeline contract path escapes the project root: {path}") from exc


def _canonical_snapshot_path(value: object, label: str) -> str:
    """Validate the portable project-relative path form stored in a snapshot."""
    if (not isinstance(value, str) or not value or len(value) > 4_096
            or "\\" in value or "\x00" in value):
        raise PipelineError(f"{label} must be a canonical project-relative path")
    path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (path.is_absolute() or windows_path.is_absolute() or windows_path.drive
            or value == "." or ".." in path.parts or path.as_posix() != value):
        raise PipelineError(f"{label} must be a canonical project-relative path")
    return value


def _canonical_graph_path(cfg, value: object, label: str) -> str:
    """Resolve a graph path and require its authored spelling to be canonical."""
    authored = _canonical_snapshot_path(value, label)
    display = _project_label(cfg, cfg.resolve(authored).resolve())
    if display != authored:
        raise PipelineError(f"{label} must be a canonical project-relative path")
    return display


def _assert_disjoint_role_values(
        role_values: dict[str, list[str] | set[str]], *, label: str,
        reject_within_role_repeats: bool = False) -> None:
    """Reject one identity being assigned to incompatible execution roles."""
    seen: dict[str, str] = {}
    for role in role_values:
        for value in role_values[role]:
            previous = seen.get(value)
            if previous is not None and (
                    previous != role or reject_within_role_repeats):
                raise PipelineError(
                    f"{label} overlap across {previous} and {role} roles: {value}"
                )
            seen[value] = role


def _stable_bytes_and_snapshot(path: Path, label: str, display: str) -> tuple[bytes, dict]:
    # Local imports avoid an events -> pipeline -> events import cycle.
    from .events import _stable_bounded_bytes, snapshot_file

    try:
        data = _stable_bounded_bytes(path, MAX_CONTRACT_BYTES)
    except Exception as exc:
        raise PipelineError(f"cannot read stable {label} {display}: {exc}") from exc
    snapshot = snapshot_file(path, display, limit=MAX_CONTRACT_BYTES)
    if snapshot.get("state") != "stable":
        raise PipelineError(
            f"{label} {display} is not a stable regular file: {snapshot.get('state')}"
        )
    if hashlib.sha256(data).hexdigest() != snapshot.get("sha256"):
        raise PipelineError(f"{label} {display} changed while it was read")
    return data, snapshot


def _line_anchor(data: bytes, anchor: dict, label: str) -> dict:
    if set(anchor) != {"code_node_id", "kind", "start_line", "end_line", "text_sha256"}:
        raise PipelineError(
            f"{label} must contain exactly code_node_id, kind, start_line, end_line, "
            "and text_sha256"
        )
    if anchor.get("kind") != "text_lines":
        raise PipelineError(f"{label}.kind must be text_lines")
    start, end = anchor.get("start_line"), anchor.get("end_line")
    if (isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int)
            or not isinstance(end, int) or start < 1 or end < start or end > 10_000_000):
        raise PipelineError(f"{label} has an invalid inclusive line range")
    expected = anchor.get("text_sha256")
    if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
        raise PipelineError(f"{label}.text_sha256 must be a lowercase SHA-256 digest")
    lines = data.splitlines(keepends=True)
    if end > len(lines):
        raise PipelineError(
            f"{label} ends at line {end}, but the code file has only {len(lines)} lines"
        )
    observed = hashlib.sha256(b"".join(lines[start - 1:end])).hexdigest()
    if observed != expected:
        raise PipelineError(
            f"{label} does not match current code bytes (expected {expected}, observed {observed})"
        )
    return {
        "code_node_id": anchor["code_node_id"],
        "kind": "text_lines",
        "start_line": start,
        "end_line": end,
        "text_sha256": observed,
        "valid": True,
    }


def _method_spec(node: dict) -> dict:
    spec = node.get("method_spec")
    if not isinstance(spec, dict) or set(spec) != {"schema_version", "steps"}:
        raise PipelineError(
            f"method node {node['id']} needs method_spec with exactly schema_version and steps"
        )
    if spec.get("schema_version") != METHOD_SPEC_SCHEMA:
        raise PipelineError(
            f"method node {node['id']} has unsupported method_spec schema "
            f"{spec.get('schema_version')!r}"
        )
    steps = spec.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > MAX_STAGES:
        raise PipelineError(f"method node {node['id']} method_spec.steps must be a bounded list")
    normalized = []
    seen = set()
    for index, step in enumerate(steps):
        label = f"method node {node['id']} step #{index}"
        if not isinstance(step, dict) or set(step) != {"id", "statement", "required"}:
            raise PipelineError(f"{label} must contain exactly id, statement, and required")
        step_id = _bounded_token(step.get("id"), f"{label}.id")
        statement = step.get("statement")
        if (not isinstance(statement, str) or not statement.strip()
                or len(statement) > 16_384):
            raise PipelineError(f"{label}.statement must be bounded non-empty text")
        if not isinstance(step.get("required"), bool):
            raise PipelineError(f"{label}.required must be boolean")
        if step_id in seen:
            raise PipelineError(f"method node {node['id']} has duplicate step {step_id}")
        seen.add(step_id)
        normalized.append({
            "id": step_id,
            "statement": statement,
            "required": step["required"],
        })
    return {"schema_version": METHOD_SPEC_SCHEMA, "steps": normalized}


def _topological_stages(stages: list[dict]) -> list[dict]:
    by_id = {stage["id"]: stage for stage in stages}
    indegree = {stage_id: 0 for stage_id in by_id}
    children = {stage_id: [] for stage_id in by_id}
    for stage in stages:
        for parent in stage["depends_on"]:
            if parent not in by_id:
                raise PipelineError(
                    f"stage {stage['id']} depends on unknown stage {parent}"
                )
            if parent == stage["id"]:
                raise PipelineError(f"stage {stage['id']} depends on itself")
            indegree[stage["id"]] += 1
            children[parent].append(stage["id"])
    ready = sorted(stage_id for stage_id, degree in indegree.items() if degree == 0)
    ordered = []
    while ready:
        stage_id = ready.pop(0)
        ordered.append(by_id[stage_id])
        for child in sorted(children[stage_id]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort()
    if len(ordered) != len(stages):
        raise PipelineError("pipeline stages contain a dependency cycle")
    return ordered


def _ancestors(stage_id: str, by_id: dict[str, dict]) -> set[str]:
    seen, stack = set(), list(by_id[stage_id]["depends_on"])
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(by_id[current]["depends_on"])
    return seen


def _resolve_contract_path(cfg, value: str) -> tuple[Path, str]:
    if not isinstance(value, str) or not value or len(value) > 4_096:
        raise PipelineError("pipeline contract path must be bounded non-empty text")
    candidate = Path(value)
    if candidate.is_absolute():
        raise PipelineError("pipeline contract path must be project-relative")
    path = (cfg.root / candidate).resolve()
    display = _project_label(cfg, path)
    return path, display


def resolve_pipeline_contract(
    cfg,
    value: str,
    *,
    declared_inputs: list[str],
    declared_outputs: list[str],
    parameters: dict,
    seeds: dict,
    snapshot_schema: str = SNAPSHOT_SCHEMA,
) -> dict:
    """Validate and snapshot one authored contract against an exact run boundary."""
    if snapshot_schema not in SUPPORTED_SNAPSHOT_SCHEMAS:
        raise PipelineError(
            f"unsupported requested pipeline snapshot schema: {snapshot_schema!r}"
        )
    contract_path, contract_display = _resolve_contract_path(cfg, value)
    data, source_snapshot = _stable_bytes_and_snapshot(
        contract_path, "pipeline contract", contract_display,
    )
    try:
        document = strict_json_loads(data.decode("utf-8-sig"), contract_display)
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise PipelineError(f"invalid pipeline contract JSON {contract_display}: {exc}") from exc
    expected_keys = {
        "schema_version", "name", "entrypoint_code_node_id", "code_node_ids",
        "input_node_ids", "output_node_ids", "required_parameters", "required_seeds", "stages",
    }
    if not isinstance(document, dict) or set(document) != expected_keys:
        raise PipelineError(
            "pipeline contract must contain exactly schema_version, name, entrypoint_code_node_id, "
            "code_node_ids, input_node_ids, output_node_ids, required_parameters, required_seeds, "
            "and stages"
        )
    if document.get("schema_version") != CONTRACT_SCHEMA:
        raise PipelineError(
            f"unsupported pipeline contract schema: {document.get('schema_version')!r}"
        )
    name = _bounded_token(document.get("name"), "pipeline contract name")
    code_ids = _string_list(document["code_node_ids"], "code_node_ids", allow_empty=False)
    entrypoint_code_id = _bounded_token(
        document.get("entrypoint_code_node_id"), "entrypoint_code_node_id",
    )
    if entrypoint_code_id not in code_ids:
        raise PipelineError("entrypoint_code_node_id must be present in code_node_ids")
    input_ids = _string_list(document["input_node_ids"], "input_node_ids")
    output_ids = _string_list(document["output_node_ids"], "output_node_ids", allow_empty=False)
    _assert_disjoint_role_values({
        "code": code_ids,
        "input": input_ids,
        "terminal output": output_ids,
    }, label="pipeline contract node ids")
    required_parameters = _string_list(
        document["required_parameters"], "required_parameters",
    )
    required_seeds = _string_list(document["required_seeds"], "required_seeds")
    if set(parameters) != set(required_parameters):
        raise PipelineError(
            "pipeline contract parameter keys differ from the run "
            f"(required={sorted(required_parameters)}, supplied={sorted(parameters)})"
        )
    if set(seeds) != set(required_seeds):
        raise PipelineError(
            "pipeline contract seed keys differ from the run "
            f"(required={sorted(required_seeds)}, supplied={sorted(seeds)})"
        )

    raw = load_raw(cfg)
    nodes, _edges, _concepts = load_graph(cfg, raw=raw)
    boundary_paths: dict[str, list[str]] = {
        "code": [], "input": [], "terminal output": [],
    }
    for role, ids, expected_type in (
        ("code", code_ids, "code"),
        ("input", input_ids, None),
        ("terminal output", output_ids, None),
    ):
        for node_id in ids:
            node = nodes.get(node_id)
            if node is None:
                raise PipelineError(f"pipeline contract {role} node is missing: {node_id}")
            if expected_type is not None and node.get("type") != expected_type:
                raise PipelineError(
                    f"pipeline contract {role} node {node_id} has type {node.get('type')!r}, "
                    f"expected {expected_type!r}"
                )
            if node.get("status") not in _ACTIVE_NODE_STATUSES:
                raise PipelineError(
                    f"pipeline contract {role} node {node_id} is not active: {node.get('status')}"
                )
            if not isinstance(node.get("path"), str) or not node["path"]:
                raise PipelineError(f"pipeline contract {role} node {node_id} needs a path")
            boundary_paths[role].append(_canonical_graph_path(
                cfg, node["path"], f"pipeline contract {role} node {node_id} path",
            ))
    _assert_disjoint_role_values(
        boundary_paths,
        label="pipeline contract normalized paths",
        reject_within_role_repeats=True,
    )

    raw_stages = document.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages or len(raw_stages) > MAX_STAGES:
        raise PipelineError("pipeline contract stages must be a bounded non-empty list")
    stages = []
    stage_ids = set()
    anchor_count = 0
    for index, stage in enumerate(raw_stages):
        label = f"pipeline stage #{index}"
        keys = {
            "id", "depends_on", "method_id", "method_step_id", "consumes_node_ids",
            "produces_node_ids", "code_anchors",
        }
        if not isinstance(stage, dict) or set(stage) != keys:
            raise PipelineError(
                f"{label} must contain exactly id, depends_on, method_id, method_step_id, "
                "consumes_node_ids, produces_node_ids, and code_anchors"
            )
        stage_id = _bounded_token(stage.get("id"), f"{label}.id")
        if stage_id in stage_ids:
            raise PipelineError(f"duplicate pipeline stage id: {stage_id}")
        stage_ids.add(stage_id)
        depends_on = _string_list(stage["depends_on"], f"stage {stage_id}.depends_on")
        consumes = _string_list(
            stage["consumes_node_ids"], f"stage {stage_id}.consumes_node_ids",
        )
        produces = _string_list(
            stage["produces_node_ids"], f"stage {stage_id}.produces_node_ids",
        )
        method_id = _bounded_token(stage.get("method_id"), f"stage {stage_id}.method_id")
        method_step_id = _bounded_token(
            stage.get("method_step_id"), f"stage {stage_id}.method_step_id",
        )
        anchors = stage.get("code_anchors")
        if not isinstance(anchors, list) or not anchors:
            raise PipelineError(f"stage {stage_id}.code_anchors must be a non-empty list")
        anchor_count += len(anchors)
        if anchor_count > MAX_ANCHORS:
            raise PipelineError("pipeline contract exceeds the code-anchor limit")
        stages.append({
            "id": stage_id,
            "depends_on": depends_on,
            "method_id": method_id,
            "method_step_id": method_step_id,
            "consumes_node_ids": consumes,
            "produces_node_ids": produces,
            "code_anchors": anchors,
        })
    ordered = _topological_stages(stages)
    by_stage = {stage["id"]: stage for stage in ordered}

    output_producer = {}
    for stage in ordered:
        for node_id in [*stage["consumes_node_ids"], *stage["produces_node_ids"]]:
            node = nodes.get(node_id)
            if node is None:
                raise PipelineError(f"stage {stage['id']} references missing graph node {node_id}")
            if node.get("status") not in _ACTIVE_NODE_STATUSES:
                raise PipelineError(
                    f"stage {stage['id']} graph node {node_id} is not active: "
                    f"{node.get('status')}"
                )
            if node.get("path") is not None:
                _canonical_graph_path(
                    cfg, node["path"], f"stage {stage['id']} graph node {node_id} path",
                )
        for node_id in stage["produces_node_ids"]:
            if node_id in output_producer:
                raise PipelineError(
                    f"graph node {node_id} is produced by both {output_producer[node_id]} "
                    f"and {stage['id']}"
                )
            output_producer[node_id] = stage["id"]
    for stage in ordered:
        ancestors = _ancestors(stage["id"], by_stage)
        for node_id in stage["consumes_node_ids"]:
            producer = output_producer.get(node_id)
            if producer is not None and producer not in ancestors:
                raise PipelineError(
                    f"stage {stage['id']} consumes {node_id} from {producer} without depending "
                    "on that stage"
                )
    external_consumed = {
        node_id for stage in ordered for node_id in stage["consumes_node_ids"]
        if node_id not in output_producer
    }
    if external_consumed != set(input_ids):
        raise PipelineError(
            "pipeline stage external inputs differ from input_node_ids "
            f"(stage={sorted(external_consumed)}, contract={sorted(input_ids)})"
        )
    depended_on = {parent for stage in ordered for parent in stage["depends_on"]}
    terminal_ids = {stage["id"] for stage in ordered} - depended_on
    terminal_outputs = {
        node_id for stage in ordered if stage["id"] in terminal_ids
        for node_id in stage["produces_node_ids"]
    }
    if terminal_outputs != set(output_ids):
        raise PipelineError(
            "terminal stage outputs differ from output_node_ids "
            f"(terminal={sorted(terminal_outputs)}, contract={sorted(output_ids)})"
        )
    internal_output_ids = sorted(set(output_producer) - set(output_ids))
    _assert_disjoint_role_values({
        "code": code_ids,
        "input": input_ids,
        "terminal output": output_ids,
        "intermediate": internal_output_ids,
    }, label="pipeline contract node ids")

    materialized_internal_paths = [
        _canonical_graph_path(
            cfg, nodes[node_id]["path"],
            f"pipeline intermediate node {node_id} path",
        )
        for node_id in internal_output_ids
        if nodes[node_id].get("path") is not None
    ]
    _assert_disjoint_role_values({
        **boundary_paths,
        "intermediate": materialized_internal_paths,
    }, label="pipeline contract normalized paths", reject_within_role_repeats=True)

    code_files = {}
    code_snapshots = {}
    for code_id in code_ids:
        node = nodes[code_id]
        path = cfg.resolve(node["path"])
        data_bytes, file_snapshot = _stable_bytes_and_snapshot(
            path, "code file", node["path"],
        )
        code_files[code_id] = data_bytes
        code_snapshots[code_id] = {
            **_node_snapshot(node),
            "path": node["path"],
            "file": _snapshot_file_for_schema(file_snapshot, snapshot_schema),
        }

    method_ids = sorted({stage["method_id"] for stage in ordered})
    method_snapshots = {}
    required_method_steps = set()
    for method_id in method_ids:
        node = nodes.get(method_id)
        if node is None:
            raise PipelineError(f"pipeline stage method node is missing: {method_id}")
        if node.get("type") != "method":
            raise PipelineError(
                f"pipeline method node {method_id} has type {node.get('type')!r}, expected method"
            )
        if node.get("status") not in _ACTIVE_NODE_STATUSES:
            raise PipelineError(f"pipeline method node {method_id} is not active")
        spec = _method_spec(node)
        required_method_steps.update(
            (method_id, step["id"]) for step in spec["steps"] if step["required"]
        )
        snap = {**_node_snapshot(node), "method_spec": spec}
        if node.get("path") is not None:
            method_path = _canonical_graph_path(
                cfg, node["path"], f"pipeline method node {method_id} path",
            )
            method_data, method_file = _stable_bytes_and_snapshot(
                cfg.resolve(method_path), "method file", method_path,
            )
            # The whole-file lock is intentional. Fine-grained method prose anchors can be
            # added later without weakening this exact-byte basis.
            del method_data
            snap.update({
                "path": method_path,
                "file": _snapshot_file_for_schema(method_file, snapshot_schema),
            })
        method_snapshots[method_id] = snap

    observed_method_steps = []
    resolved_stages = []
    for stage in ordered:
        method_key = (stage["method_id"], stage["method_step_id"])
        method_steps = {
            step["id"] for step in method_snapshots[stage["method_id"]]["method_spec"]["steps"]
        }
        if stage["method_step_id"] not in method_steps:
            raise PipelineError(
                f"stage {stage['id']} references unknown method step "
                f"{stage['method_id']}/{stage['method_step_id']}"
            )
        if method_key in observed_method_steps:
            raise PipelineError(
                f"method step {method_key[0]}/{method_key[1]} maps to more than one stage"
            )
        observed_method_steps.append(method_key)
        resolved_anchors = []
        for anchor_index, anchor in enumerate(stage["code_anchors"]):
            anchor_label = f"stage {stage['id']} code anchor #{anchor_index}"
            if not isinstance(anchor, dict):
                raise PipelineError(f"{anchor_label} must be an object")
            code_id = anchor.get("code_node_id")
            if code_id not in code_ids:
                raise PipelineError(
                    f"{anchor_label} targets {code_id!r}, which is not a declared code node"
                )
            resolved_anchors.append(_line_anchor(code_files[code_id], anchor, anchor_label))
        resolved_stages.append({
            "id": stage["id"],
            "depends_on": list(stage["depends_on"]),
            "method_id": stage["method_id"],
            "method_step_id": stage["method_step_id"],
            "consumes_node_ids": list(stage["consumes_node_ids"]),
            "produces_node_ids": list(stage["produces_node_ids"]),
            "code_anchors": resolved_anchors,
            "execution_observation": "declared_only_not_observed",
        })
    missing_required = sorted(required_method_steps - set(observed_method_steps))
    if missing_required:
        raise PipelineError(
            "required method steps are absent from the pipeline stages: "
            + ", ".join(f"{method}/{step}" for method, step in missing_required)
        )

    def role_items(ids):
        return [
            {**_node_snapshot(nodes[node_id]), "path": nodes[node_id]["path"]}
            for node_id in sorted(ids)
        ]

    def intermediate_role_items(ids):
        items = []
        for node_id in sorted(ids):
            node = nodes[node_id]
            item = _node_snapshot(node)
            path = node.get("path")
            if path is None:
                item["materialization"] = "unobserved_in_memory_or_ephemeral"
            else:
                if not isinstance(path, str) or not path:
                    raise PipelineError(
                        f"pipeline intermediate node {node_id} path must be non-empty text"
                    )
                _canonical_graph_path(
                    cfg, path, f"pipeline intermediate node {node_id} path",
                )
                item.update({
                    "materialization": "declared_file_boundary",
                    "path": path,
                })
            items.append(item)
        materialized_paths = [
            item["path"] for item in items
            if item["materialization"] == "declared_file_boundary"
        ]
        if len(materialized_paths) != len(set(materialized_paths)):
            raise PipelineError("pipeline materialized intermediate paths contain duplicates")
        return items

    expected_input_paths = {
        nodes[node_id]["path"] for node_id in [*input_ids, *code_ids]
    }
    expected_output_paths = {nodes[node_id]["path"] for node_id in output_ids}
    if set(declared_inputs) != expected_input_paths:
        raise PipelineError(
            "run input paths differ from contract data+code paths "
            f"(run={sorted(declared_inputs)}, contract={sorted(expected_input_paths)})"
        )
    if set(declared_outputs) != expected_output_paths:
        raise PipelineError(
            "run output paths differ from contract paths "
            f"(run={sorted(declared_outputs)}, contract={sorted(expected_output_paths)})"
        )

    roles = {
        "code": [code_snapshots[node_id] for node_id in sorted(code_snapshots)],
        "inputs": role_items(input_ids),
        "outputs": role_items(output_ids),
        "methods": [method_snapshots[node_id] for node_id in sorted(method_snapshots)],
    }
    if snapshot_schema in _INTERMEDIATE_SNAPSHOT_SCHEMAS:
        roles["intermediates"] = intermediate_role_items(internal_output_ids)
        _assert_disjoint_role_values({
            "code": [item["path"] for item in roles["code"]],
            "input": [item["path"] for item in roles["inputs"]],
            "terminal output": [item["path"] for item in roles["outputs"]],
            "intermediate": [
                item["path"] for item in roles["intermediates"]
                if item["materialization"] == "declared_file_boundary"
            ],
        }, label="pipeline contract normalized paths", reject_within_role_repeats=True)
    core = {
        "schema_version": snapshot_schema,
        "name": name,
        "entrypoint_code_node_id": entrypoint_code_id,
        "source": {
            "path": contract_display,
            "sha256": source_snapshot["sha256"],
            "size": source_snapshot["size"],
            "file_version_id": source_snapshot["file_version_id"],
        },
        "roles": roles,
        "required_parameters": sorted(required_parameters),
        "required_seeds": sorted(required_seeds),
        "stages": resolved_stages,
        "coverage": dict(
            _SNAPSHOT_COVERAGE
            if snapshot_schema in _INTERMEDIATE_SNAPSHOT_SCHEMAS
            else _LEGACY_SNAPSHOT_COVERAGE
        ),
    }
    return {
        "id": f"pipeline-contract:sha256:{canonical_sha256(core)}",
        **core,
    }


def validate_pipeline_snapshot(snapshot: object) -> str:
    """Deeply validate a stored resolved snapshot and return its contract ID.

    Content addressing detects mutation, but a recomputed address does not make
    malformed content trustworthy.  This validator therefore reproduces every
    closed structural invariant emitted by :func:`resolve_pipeline_contract`,
    including role snapshots, method/step mappings, anchors, stage ordering, and
    boundary dataflow.  Consumers must call this function rather than treating a
    syntactically valid ``pipeline-contract:sha256:...`` identifier as validation.
    """
    if not isinstance(snapshot, dict):
        raise PipelineError("pipeline contract snapshot must be an object")
    expected = {
        "id", "schema_version", "name", "entrypoint_code_node_id", "source", "roles",
        "required_parameters", "required_seeds", "stages", "coverage",
    }
    if set(snapshot) != expected:
        raise PipelineError("pipeline contract snapshot keys differ from schema")
    schema_version = snapshot.get("schema_version")
    if schema_version not in SUPPORTED_SNAPSHOT_SCHEMAS:
        raise PipelineError(
            f"unsupported pipeline contract snapshot schema: {schema_version!r}"
        )
    contract_id = snapshot.get("id")
    if not isinstance(contract_id, str) or not CONTRACT_ID_RE.fullmatch(contract_id):
        raise PipelineError("pipeline contract snapshot id is invalid")
    core = {key: snapshot[key] for key in expected if key != "id"}
    try:
        expected_id = f"pipeline-contract:sha256:{canonical_sha256(core)}"
    except (TypeError, ValueError, RecursionError) as exc:
        raise PipelineError(
            f"pipeline contract snapshot is not canonical JSON: {exc}"
        ) from exc
    if contract_id != expected_id:
        raise PipelineError("pipeline contract snapshot id does not match canonical content")

    _bounded_token(snapshot.get("name"), "pipeline contract snapshot name")
    expected_coverage = (
        _SNAPSHOT_COVERAGE
        if schema_version in _INTERMEDIATE_SNAPSHOT_SCHEMAS
        else _LEGACY_SNAPSHOT_COVERAGE
    )
    if snapshot.get("coverage") != expected_coverage:
        raise PipelineError("pipeline contract snapshot coverage is invalid or overstated")

    source = snapshot.get("source")
    if (not isinstance(source, dict)
            or set(source) != {"path", "sha256", "size", "file_version_id"}):
        raise PipelineError("pipeline contract snapshot source is invalid")
    source_digest = source.get("sha256")
    if (not isinstance(source.get("path"), str) or not source["path"]
            or len(source["path"]) > 4_096
            or not isinstance(source_digest, str) or not SHA256_RE.fullmatch(source_digest)
            or source.get("file_version_id") != f"file:sha256:{source_digest}"
            or isinstance(source.get("size"), bool)
            or not isinstance(source.get("size"), int) or source["size"] < 0):
        raise PipelineError("pipeline contract snapshot source has invalid metadata")

    roles = snapshot.get("roles")
    expected_roles = {"code", "inputs", "outputs", "methods"}
    if schema_version in _INTERMEDIATE_SNAPSHOT_SCHEMAS:
        expected_roles.add("intermediates")
    if not isinstance(roles, dict) or set(roles) != expected_roles:
        raise PipelineError("pipeline contract snapshot roles are invalid")
    role_ids: dict[str, set[str]] = {}
    role_order = ["code", "inputs", "outputs", "methods"]
    if schema_version in _INTERMEDIATE_SNAPSHOT_SCHEMAS:
        role_order.append("intermediates")
    materialized_paths = []
    execution_role_paths: dict[str, list[str]] = {
        "code": [], "input": [], "terminal output": [], "intermediate": [],
    }
    for role in role_order:
        items = roles[role]
        require_nonempty = role in {"code", "outputs", "methods"}
        if (not isinstance(items, list) or len(items) > MAX_NODE_ROLES
                or (require_nonempty and not items)):
            raise PipelineError(
                f"pipeline contract snapshot role {role} must be a bounded "
                + ("non-empty " if require_nonempty else "") + "list"
            )
        if any(
                not isinstance(item, dict)
                or not isinstance(item.get("node_id"), str)
                or not item["node_id"]
                for item in items):
            raise PipelineError(
                f"pipeline contract snapshot role {role} has an invalid node id"
            )
        ordered_ids = [item["node_id"] for item in items]
        if ordered_ids != sorted(ordered_ids) or len(ordered_ids) != len(set(ordered_ids)):
            raise PipelineError(f"pipeline contract snapshot role {role} is not canonical")
        for index, item in enumerate(items):
            label = f"pipeline contract snapshot roles.{role}[{index}]"
            _validate_snapshot_node_version(item, label)
            if role in {"inputs", "outputs"}:
                if set(item) != {"node_id", "node_sha256", "node_version_id", "path"}:
                    raise PipelineError(f"{label} has unknown or missing fields")
            elif role == "code":
                if set(item) != {
                        "node_id", "node_sha256", "node_version_id", "path", "file"}:
                    raise PipelineError(f"{label} has unknown or missing fields")
                _validate_snapshot_file(
                    item["file"], f"{label}.file", schema_version,
                )
                if item["file"]["path"] != item.get("path"):
                    raise PipelineError(f"{label} file path does not match node path")
            elif role == "methods":
                allowed = {
                    "node_id", "node_sha256", "node_version_id", "method_spec",
                    "path", "file",
                }
                required = {"node_id", "node_sha256", "node_version_id", "method_spec"}
                if not required <= set(item) <= allowed:
                    raise PipelineError(f"{label} has unknown or missing fields")
                if ("path" in item) != ("file" in item):
                    raise PipelineError(f"{label} path and file must appear together")
                _method_spec({"id": item["node_id"], "method_spec": item["method_spec"]})
                if "file" in item:
                    _validate_snapshot_file(
                        item["file"], f"{label}.file", schema_version,
                    )
                    if item["file"]["path"] != item["path"]:
                        raise PipelineError(f"{label} file path does not match method path")
            else:
                materialization = item.get("materialization")
                if materialization == "declared_file_boundary":
                    if set(item) != {
                            "node_id", "node_sha256", "node_version_id",
                            "materialization", "path"}:
                        raise PipelineError(f"{label} has unknown or missing fields")
                    if not isinstance(item.get("path"), str) or not item["path"]:
                        raise PipelineError(f"{label}.path is invalid")
                    materialized_paths.append(item["path"])
                elif materialization == "unobserved_in_memory_or_ephemeral":
                    if set(item) != {
                            "node_id", "node_sha256", "node_version_id",
                            "materialization"}:
                        raise PipelineError(f"{label} has unknown or missing fields")
                else:
                    raise PipelineError(f"{label}.materialization is invalid")
            if "path" in item and (
                    not isinstance(item["path"], str) or not item["path"]):
                raise PipelineError(f"{label}.path is invalid")
            if schema_version in _INTERMEDIATE_SNAPSHOT_SCHEMAS and "path" in item:
                _canonical_snapshot_path(item["path"], f"{label}.path")
                role_name = {
                    "code": "code",
                    "inputs": "input",
                    "outputs": "terminal output",
                    "intermediates": "intermediate",
                }.get(role)
                if role_name is not None:
                    execution_role_paths[role_name].append(item["path"])
        role_ids[role] = set(ordered_ids)
    if len(materialized_paths) != len(set(materialized_paths)):
        raise PipelineError(
            "pipeline contract snapshot materialized intermediate paths repeat"
        )
    if schema_version in _INTERMEDIATE_SNAPSHOT_SCHEMAS:
        _assert_disjoint_role_values({
            "code": role_ids["code"],
            "input": role_ids["inputs"],
            "terminal output": role_ids["outputs"],
            "intermediate": role_ids["intermediates"],
        }, label="pipeline contract snapshot node ids")
        _assert_disjoint_role_values(
            execution_role_paths,
            label="pipeline contract snapshot normalized paths",
            reject_within_role_repeats=True,
        )

    entrypoint = snapshot.get("entrypoint_code_node_id")
    _bounded_token(entrypoint, "pipeline contract snapshot entrypoint_code_node_id")
    if entrypoint not in role_ids["code"]:
        raise PipelineError("pipeline contract snapshot entrypoint is not a code role")

    for key in ("required_parameters", "required_seeds"):
        values = snapshot.get(key)
        if (not isinstance(values, list) or len(values) > MAX_NODE_ROLES
                or not all(isinstance(item, str) and item for item in values)
                or values != sorted(set(values))):
            raise PipelineError(f"pipeline contract snapshot {key} is not canonical")

    stages = snapshot.get("stages")
    if not isinstance(stages, list) or not stages or len(stages) > MAX_STAGES:
        raise PipelineError(
            "pipeline contract snapshot stages must be a bounded non-empty list"
        )
    stage_ids = set()
    method_step_pairs = set()
    anchor_count = 0
    for index, stage in enumerate(stages):
        label = f"pipeline contract snapshot stages[{index}]"
        stage_keys = {
            "id", "depends_on", "method_id", "method_step_id", "consumes_node_ids",
            "produces_node_ids", "code_anchors", "execution_observation",
        }
        if not isinstance(stage, dict) or set(stage) != stage_keys:
            raise PipelineError(f"{label} has unknown or missing fields")
        stage_id = _bounded_token(stage.get("id"), f"{label}.id")
        method_id = _bounded_token(stage.get("method_id"), f"{label}.method_id")
        method_step_id = _bounded_token(
            stage.get("method_step_id"), f"{label}.method_step_id",
        )
        if stage_id in stage_ids:
            raise PipelineError(f"pipeline contract snapshot repeats stage {stage_id}")
        stage_ids.add(stage_id)
        pair = (method_id, method_step_id)
        if pair in method_step_pairs:
            raise PipelineError(
                f"pipeline contract snapshot repeats method-step mapping "
                f"{method_id}/{method_step_id}"
            )
        method_step_pairs.add(pair)
        if method_id not in role_ids["methods"]:
            raise PipelineError(f"{label} references an undeclared method")
        for key in ("depends_on", "consumes_node_ids", "produces_node_ids"):
            _string_list(stage.get(key), f"{label}.{key}")
        anchors = stage.get("code_anchors")
        if not isinstance(anchors, list) or not anchors:
            raise PipelineError(f"{label}.code_anchors must be a non-empty list")
        anchor_count += len(anchors)
        if anchor_count > MAX_ANCHORS:
            raise PipelineError("pipeline contract snapshot exceeds the code-anchor limit")
        for anchor_index, anchor in enumerate(anchors):
            anchor_label = f"{label}.code_anchors[{anchor_index}]"
            if (not isinstance(anchor, dict) or set(anchor) != {
                    "code_node_id", "kind", "start_line", "end_line", "text_sha256",
                    "valid",
            }):
                raise PipelineError(f"{anchor_label} has an invalid shape")
            start, end = anchor.get("start_line"), anchor.get("end_line")
            if (anchor.get("code_node_id") not in role_ids["code"]
                    or anchor.get("kind") != "text_lines"
                    or anchor.get("valid") is not True
                    or isinstance(start, bool) or not isinstance(start, int) or start < 1
                    or isinstance(end, bool) or not isinstance(end, int)
                    or end < start or end > 10_000_000
                    or not isinstance(anchor.get("text_sha256"), str)
                    or not SHA256_RE.fullmatch(anchor["text_sha256"])):
                raise PipelineError(f"{anchor_label} is invalid")
        if stage.get("execution_observation") != "declared_only_not_observed":
            raise PipelineError(f"{label} overstates stage execution observation")

    ordered_stages = _topological_stages(stages)
    if [stage["id"] for stage in stages] != [stage["id"] for stage in ordered_stages]:
        raise PipelineError("pipeline contract snapshot stages are not in canonical DAG order")
    by_stage = {stage["id"]: stage for stage in stages}

    stage_method_ids = {stage["method_id"] for stage in stages}
    if stage_method_ids != role_ids["methods"]:
        raise PipelineError(
            "pipeline contract snapshot method roles differ from stage method ids"
        )
    methods_by_id = {item["node_id"]: item for item in roles["methods"]}
    for method_id, method in methods_by_id.items():
        declared_steps = {
            step["id"]: step for step in method["method_spec"]["steps"]
        }
        mapped = {
            stage["method_step_id"] for stage in stages
            if stage["method_id"] == method_id
        }
        unknown = sorted(mapped - set(declared_steps))
        if unknown:
            raise PipelineError(
                f"pipeline contract snapshot maps an unknown step for method {method_id}"
            )
        required = {
            step_id for step_id, step in declared_steps.items() if step["required"]
        }
        missing = sorted(required - mapped)
        if missing:
            raise PipelineError(
                f"pipeline contract snapshot omits a required step for method {method_id}"
            )

    output_producer = {}
    for stage in stages:
        for node_id in stage["produces_node_ids"]:
            if node_id in output_producer:
                raise PipelineError(
                    f"pipeline contract snapshot node {node_id} is produced by both "
                    f"{output_producer[node_id]} and {stage['id']}"
                )
            output_producer[node_id] = stage["id"]
    for stage in stages:
        ancestors = _ancestors(stage["id"], by_stage)
        for node_id in stage["consumes_node_ids"]:
            producer = output_producer.get(node_id)
            if producer is not None and producer not in ancestors:
                raise PipelineError(
                    f"pipeline contract snapshot stage {stage['id']} consumes {node_id} "
                    f"from {producer} without depending on that stage"
                )
    external_consumed = {
        node_id for stage in stages for node_id in stage["consumes_node_ids"]
        if node_id not in output_producer
    }
    if external_consumed != role_ids["inputs"]:
        raise PipelineError(
            "pipeline contract snapshot stage external inputs differ from input roles"
        )
    depended_on = {parent for stage in stages for parent in stage["depends_on"]}
    terminal_ids = stage_ids - depended_on
    terminal_outputs = {
        node_id for stage in stages if stage["id"] in terminal_ids
        for node_id in stage["produces_node_ids"]
    }
    if terminal_outputs != role_ids["outputs"]:
        raise PipelineError(
            "pipeline contract snapshot terminal outputs differ from output roles"
        )
    if schema_version in _INTERMEDIATE_SNAPSHOT_SCHEMAS:
        internal_outputs = set(output_producer) - role_ids["outputs"]
        if internal_outputs != role_ids["intermediates"]:
            raise PipelineError(
                "pipeline contract snapshot intermediate roles differ from internal "
                "stage outputs"
            )
    return contract_id


def _validate_snapshot_node_version(item: dict, label: str) -> None:
    digest = item.get("node_sha256")
    if (not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)
            or item.get("node_version_id") != f"node:sha256:{digest}"):
        raise PipelineError(f"{label} has an invalid node version")


def _snapshot_file_for_schema(value: dict, schema_version: str) -> dict:
    if schema_version == SNAPSHOT_SCHEMA:
        return {key: item for key, item in value.items() if key != "mtime_ns"}
    return value


def _validate_snapshot_file(value: object, label: str, schema_version: str) -> None:
    expected = {
        "path", "method", "state", "sha256", "size", "file_version_id",
    }
    if schema_version in _MTIME_SNAPSHOT_SCHEMAS:
        expected.add("mtime_ns")
    if not isinstance(value, dict) or set(value) != expected:
        raise PipelineError(f"{label} has an invalid file snapshot shape")
    if value.get("method") != "sha256_stat_before_after" or value.get("state") != "stable":
        raise PipelineError(f"{label} is not a stable SHA-256 file snapshot")
    digest = value.get("sha256")
    if (not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)
            or value.get("file_version_id") != f"file:sha256:{digest}"):
        raise PipelineError(f"{label} has an invalid file digest")
    if (not isinstance(value.get("path"), str) or not value["path"]
            or isinstance(value.get("size"), bool)
            or not isinstance(value.get("size"), int) or value["size"] < 0):
        raise PipelineError(f"{label} has invalid path or stat metadata")
    if schema_version in _MTIME_SNAPSHOT_SCHEMAS and (
            isinstance(value.get("mtime_ns"), bool)
            or not isinstance(value.get("mtime_ns"), int)
            or value["mtime_ns"] < 0):
        raise PipelineError(f"{label} has invalid path or stat metadata")


def pipeline_snapshots_equivalent(stored: object, current: object) -> bool:
    """Compare validated snapshots, tolerating only legacy file mtimes.

    Snapshot v2 content IDs included volatile filesystem mtimes for code and
    optional method files.  After both inputs pass their exact historical
    validators, equality may therefore ignore only those fields.  Legacy v1
    and current v3 snapshots remain exact content-address comparisons.
    """
    validate_pipeline_snapshot(stored)
    validate_pipeline_snapshot(current)
    stored_schema = stored["schema_version"]
    if current["schema_version"] != stored_schema:
        return False
    if current["id"] == stored["id"]:
        return True
    if stored_schema != PREVIOUS_SNAPSHOT_SCHEMA:
        return False

    def comparison_value(snapshot: dict) -> dict:
        value = copy.deepcopy(snapshot)
        del value["id"]
        for role in ("code", "methods"):
            for item in value["roles"][role]:
                file_snapshot = item.get("file")
                if file_snapshot is not None:
                    del file_snapshot["mtime_ns"]
        return value

    return comparison_value(stored) == comparison_value(current)


def pipeline_method_ids(snapshot: dict) -> list[str]:
    validate_pipeline_snapshot(snapshot)
    return sorted(item["node_id"] for item in snapshot["roles"]["methods"])


__all__ = [
    "CONTRACT_SCHEMA", "LEGACY_SNAPSHOT_SCHEMA", "PREVIOUS_SNAPSHOT_SCHEMA",
    "SNAPSHOT_SCHEMA",
    "SUPPORTED_SNAPSHOT_SCHEMAS", "METHOD_SPEC_SCHEMA", "STAGE_CHECKPOINT_SCHEMA",
    "STAGE_TRACE_SCHEMA", "STAGE_TRACE_PLAN_SCHEMA", "STAGE_TRACE_MODE",
    "STAGE_TRACE_TRUST", "PipelineError",
    "canonical_bytes", "canonical_sha256", "resolve_pipeline_contract",
    "validate_pipeline_snapshot", "pipeline_snapshots_equivalent",
    "pipeline_method_ids", "stage_checkpoint",
    "stage_trace_plan", "prepare_stage_trace", "stage_trace_child_environment",
    "scrub_stage_trace_environment", "finalize_stage_trace", "validate_stage_trace",
    "validate_stage_trace_against_snapshot",
]
