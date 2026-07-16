"""Content-addressed mechanical run receipts for claimtrace.

The run ledger intentionally records only facts a portable wrapper can defend:
declared paths, stable content snapshots, the direct child's argv/outcome, and
unattributed before/after filesystem deltas.  It never claims that a file was
read or that the child process caused a detected write.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from .config import _check_json_nesting_depth

EVENT_SCHEMA = "claimtrace.event/1"
LEGACY_CONTRACT_EVENT_SCHEMA = "claimtrace.event/2"
CONTRACT_EVENT_SCHEMA = "claimtrace.event/3"
STAGE_CONTRACT_EVENT_SCHEMA = "claimtrace.event/4"
CONTRACT_EVENT_SCHEMAS = frozenset({
    LEGACY_CONTRACT_EVENT_SCHEMA,
    CONTRACT_EVENT_SCHEMA,
    STAGE_CONTRACT_EVENT_SCHEMA,
})
CURRENT_CONTRACT_EVENT_SCHEMAS = frozenset({
    CONTRACT_EVENT_SCHEMA,
    STAGE_CONTRACT_EVENT_SCHEMA,
})
SUPPORTED_EVENT_SCHEMAS = frozenset({EVENT_SCHEMA, *CONTRACT_EVENT_SCHEMAS})
ACTIVE_SCHEMA = "claimtrace.active-run/1"
EVENT_TYPES = {"run.started", "run.finished"}
RUN_OUTCOMES = {"succeeded", "failed", "contract_failed", "capture_precondition_failed",
                "launch_error", "interrupted"}
FILE_TRANSITIONS = {"created", "content_changed", "unchanged", "deleted", "missing", "unstable"}
FILE_STATES = {"stable", "missing", "unstable", "unreadable", "unsupported"}
CAPTURE_SCOPE_VALUES = {
    "reads": {"declared_only_not_observed"},
    "writes": {
        "declared_snapshots_plus_unattributed_project_window",
        "declared_snapshots_only",
    },
    "processes": {"direct_child_only"},
}
LINEAGE_COVERAGE_VALUES = {
    "overall": {"partial"},
    "reads": {"declared_only_not_observed"},
    "declared_inputs": {"pre_and_post_hashed"},
    "declared_outputs": {"pre_and_post_hashed"},
    "write_attribution": {"unattributed_pre_post_delta", "not_scanned"},
    "processes": {"direct_child_only"},
    "environment": {"partial"},
    "git": {"best_effort"},
}
CONTRACT_LINEAGE_COVERAGE_VALUES = {
    **LINEAGE_COVERAGE_VALUES,
    "declared_intermediates": {"pre_and_post_hashed"},
}
STAGE_CONTRACT_LINEAGE_COVERAGE_VALUES = {
    **CONTRACT_LINEAGE_COVERAGE_VALUES,
    "cooperative_stage_trace": {"cooperative_child_self_report"},
}
CAPTURE_WRITE_ATTRIBUTION = {
    "declared_snapshots_plus_unattributed_project_window": "unattributed_pre_post_delta",
    "declared_snapshots_only": "not_scanned",
}
RUN_ID_RE = re.compile(r"^run:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
EVENT_ID_RE = re.compile(r"^event:sha256:([0-9a-f]{64})$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RFC3339_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"
)
GLOB_CHARS = set("*?[]")
DEFAULT_IGNORED_DIRS = {
    ".git", ".hg", ".svn", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "__pycache__", ".venv", "venv", "node_modules", "build", "dist",
}
LOCKFILE_NAMES = {
    "requirements.txt", "requirements.lock", "poetry.lock", "pdm.lock",
    "uv.lock", "Pipfile.lock", "environment.yml", "environment.yaml",
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
    "renv.lock", "Manifest.toml",
}
DEFAULT_SENSITIVE_FLAGS = {
    "--api-key", "--apikey", "--authorization", "--password", "--secret",
    "--token", "--access-token", "--refresh-token", "--private-key",
}
MAX_EVENT_DOCUMENT_BYTES = 32 * 1024 * 1024
MAX_EVENT_FILES = 200_000
MAX_EVENT_STORE_BYTES = 2 * 1024 * 1024 * 1024
MAX_EVENT_STORE_ENTRIES = 400_000
MAX_ACTIVE_MARKER_FILES = 10_000
MAX_ACTIVE_MARKER_STORE_BYTES = 512 * 1024 * 1024
MAX_SEMANTIC_ONTOLOGY_DOCUMENTS = 10_000
_PROCESS_EVENT_LOCK = threading.Lock()
_OUTPUT_THREAD_LOCKS_GUARD = threading.Lock()
_OUTPUT_THREAD_LOCKS: dict[str, threading.Lock] = {}


class EventError(RuntimeError):
    """A run receipt could not be captured or validated safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical_bytes(value) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise EventError(f"value is not canonical JSON: {exc}") from exc


def canonical_sha256(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _validate_recorded_at(value) -> None:
    if not isinstance(value, str) or not RFC3339_UTC_RE.fullmatch(value):
        raise EventError("recorded_at must be a valid RFC 3339 UTC timestamp ending in Z")
    try:
        # ``fromisoformat`` validates calendar and clock ranges. The regular expression
        # above deliberately limits the accepted offset spelling to RFC 3339's UTC ``Z``.
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise EventError(
            "recorded_at must be a valid RFC 3339 UTC timestamp ending in Z"
        ) from exc


def _strict_json(text: str, source: str):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise EventError(f"{source}: duplicate JSON key {key!r}")
            out[key] = value
        return out

    def invalid_constant(value):
        raise EventError(f"{source}: non-finite JSON number {value}")

    try:
        _check_json_nesting_depth(text)
        return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise EventError(f"{source}: invalid JSON: {exc}") from exc


def make_event(event_type: str, run_id: str, payload: dict, recorded_at: str | None = None,
               *, schema_version: str = EVENT_SCHEMA) -> dict:
    if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
        raise EventError(f"unsupported event type: {event_type}")
    if not RUN_ID_RE.fullmatch(run_id):
        raise EventError(f"invalid run id: {run_id}")
    if not isinstance(payload, dict):
        raise EventError("event payload must be an object")
    if schema_version not in SUPPORTED_EVENT_SCHEMAS:
        raise EventError(f"unsupported event schema: {schema_version!r}")
    core = {
        "schema_version": schema_version,
        "type": event_type,
        "run_id": run_id,
        "recorded_at": recorded_at or _utc_now(),
        "payload": payload,
    }
    digest = canonical_sha256(core)
    return {"id": f"event:sha256:{digest}", **core}


def _validate_shape(value: dict, required: set[str], optional: set[str], label: str) -> None:
    if not isinstance(value, dict):
        raise EventError(f"{label} must be an object")
    actual = set(value)
    missing = required - actual
    extra = actual - required - optional
    if missing or extra:
        raise EventError(
            f"{label} keys differ from schema "
            f"(missing={sorted(missing)}, extra={sorted(repr(key) for key in extra)})"
        )


def _validate_snapshot(snapshot: dict, expected_path: str, label: str) -> None:
    if not isinstance(snapshot, dict):
        raise EventError(f"{label} snapshot must be an object")
    if snapshot.get("path") != expected_path:
        raise EventError(f"{label} snapshot path does not match transition path {expected_path!r}")
    state = snapshot.get("state")
    if not isinstance(state, str) or state not in FILE_STATES:
        raise EventError(f"{label} snapshot state is invalid: {state!r}")
    optional = {"method"}
    if state == "stable":
        optional |= {"sha256", "size", "file_version_id", "mtime_ns"}
    elif state == "unsupported":
        optional.add("file_kind")
    elif state == "unstable":
        optional.add("reason")
    elif state == "unreadable":
        optional.add("error")
    _validate_shape(snapshot, {"path", "state"}, optional, f"{label} snapshot")
    if state == "stable":
        sha256 = snapshot.get("sha256")
        if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
            raise EventError(f"{label} stable snapshot needs a lowercase SHA-256 digest")
        if snapshot.get("file_version_id") != f"file:sha256:{sha256}":
            raise EventError(f"{label} stable snapshot file_version_id does not match sha256")
        size = snapshot.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise EventError(f"{label} stable snapshot needs a non-negative integer size")


def _validate_transition_item(item: dict, role: str) -> None:
    if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
            or not item["path"] or item.get("transition") not in FILE_TRANSITIONS
            or not isinstance(item.get("before"), dict)
            or not isinstance(item.get("after"), dict)):
        raise EventError(f"run.finished {role} entries need path, transition, before, and after")
    _validate_shape(
        item,
        {"path", "transition", "before", "after"},
        {"produced"},
        f"run.finished {role} entry",
    )
    path = item["path"]
    _validate_snapshot(item["before"], path, f"run.finished {role} before")
    _validate_snapshot(item["after"], path, f"run.finished {role} after")
    expected_transition = _transition(item["before"], item["after"])
    if item["transition"] != expected_transition:
        raise EventError(
            f"run.finished {role} transition {item['transition']!r} disagrees with "
            f"before/after snapshots ({expected_transition!r})"
        )
    if role == "input_transitions":
        if "produced" in item:
            raise EventError("run.finished input transitions cannot claim production")
        return
    produced = item.get("produced")
    if not isinstance(produced, bool):
        raise EventError(
            f"run.finished {role} entries need a boolean produced field"
        )
    expected_produced = expected_transition in {"created", "content_changed"}
    if produced != expected_produced:
        raise EventError(
            f"run.finished {role} produced={produced} disagrees with transition "
            f"{expected_transition!r}"
        )


def _validate_outcome(payload: dict) -> None:
    outcome = payload["outcome"]
    returncode = payload["direct_child_returncode"]
    contract_errors = payload["contract_errors"]
    launch_error = payload["launch_error"]
    if returncode is not None and (isinstance(returncode, bool) or not isinstance(returncode, int)):
        raise EventError("run.finished direct_child_returncode must be an integer or null")
    if launch_error is not None and not isinstance(launch_error, dict):
        raise EventError("run.finished launch_error must be an object or null")

    if outcome in {"succeeded", "contract_failed"} and returncode != 0:
        raise EventError(f"run.finished outcome {outcome} requires direct_child_returncode 0")
    if outcome == "failed" and (returncode is None or returncode == 0):
        raise EventError("run.finished outcome failed requires a non-zero direct_child_returncode")
    if outcome in {"capture_precondition_failed", "launch_error", "interrupted"} and returncode is not None:
        raise EventError(f"run.finished outcome {outcome} requires a null direct_child_returncode")
    if outcome == "succeeded" and contract_errors:
        raise EventError("run.finished outcome succeeded cannot contain contract_errors")
    if outcome in {"contract_failed", "capture_precondition_failed"} and not contract_errors:
        raise EventError(f"run.finished outcome {outcome} requires contract_errors")
    if outcome == "launch_error":
        if not isinstance(launch_error, dict):
            raise EventError("run.finished outcome launch_error requires launch_error details")
        _validate_shape(
            launch_error, {"type", "detail"}, set(), "run.finished launch_error",
        )
        if not isinstance(launch_error.get("type"), str) or not launch_error["type"]:
            raise EventError("run.finished launch_error needs a non-empty type")
        if not isinstance(launch_error.get("detail"), str):
            raise EventError("run.finished launch_error needs string detail")
    elif launch_error is not None:
        raise EventError(f"run.finished outcome {outcome} cannot contain launch_error details")

    if outcome == "succeeded":
        if any(item["transition"] != "unchanged" for item in payload["input_transitions"]):
            raise EventError("run.finished succeeded receipt cannot have a changed input")
        invalid_outputs = {"missing", "deleted", "unstable"}
        declared_writes = [
            *payload["output_transitions"],
            *payload.get("intermediate_transitions", []),
        ]
        if any(item["transition"] in invalid_outputs for item in declared_writes):
            raise EventError(
                "run.finished succeeded receipt cannot have a missing, deleted, or "
                "unstable output or materialized intermediate"
            )


def _validate_token_object(value: dict, schema: dict[str, set[str]], label: str) -> None:
    if not isinstance(value, dict):
        raise EventError(f"{label} must be an object")
    expected = set(schema)
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(repr(key) for key in actual - expected)
        raise EventError(f"{label} keys differ from schema (missing={missing}, extra={extra})")
    for key in sorted(schema):
        token = value[key]
        if not isinstance(token, str) or token not in schema[key]:
            raise EventError(f"{label} {key} token is invalid: {token!r}")


def _validate_event_payload(event_type: str, payload: dict, schema_version: str) -> None:
    if event_type == "run.started":
        required_start = {"plan_id", "plan", "capture_scope"}
        if schema_version == STAGE_CONTRACT_EVENT_SCHEMA:
            required_start.add("stage_trace_binding")
        _validate_shape(
            payload, required_start, {"name"},
            "run.started payload",
        )
        for key in ("plan_id", "plan", "capture_scope"):
            if key not in payload:
                raise EventError(f"run.started payload is missing {key}")
        if not isinstance(payload["plan_id"], str) or not payload["plan_id"].startswith("recipe:sha256:"):
            raise EventError("run.started plan_id is invalid")
        if not isinstance(payload["plan"], dict):
            raise EventError("run.started plan must be an object")
        _validate_token_object(
            payload["capture_scope"], CAPTURE_SCOPE_VALUES, "run.started capture_scope")
        plan = payload["plan"]
        required_plan = {"argv", "cwd", "declared_inputs", "declared_outputs"}
        optional_plan = {
            "argv_capture", "parameters", "seeds", "environment", "git",
            "control_plane_before",
        }
        if schema_version in CONTRACT_EVENT_SCHEMAS:
            required_plan |= {"pipeline_contract", "computation_id"}
        if schema_version in CURRENT_CONTRACT_EVENT_SCHEMAS:
            required_plan.add("declared_intermediates")
        if schema_version == STAGE_CONTRACT_EVENT_SCHEMA:
            required_plan.add("stage_trace_plan")
        _validate_shape(plan, required_plan, optional_plan, "run.started plan")
        if not isinstance(plan.get("argv"), list) or not all(isinstance(v, str) for v in plan["argv"]):
            raise EventError("run.started plan argv must be a string list")
        if not isinstance(plan.get("cwd"), str):
            raise EventError("run.started plan cwd must be a string")
        if ("argv_capture" in plan
                and plan["argv_capture"] != "redacted_best_effort"):
            raise EventError("run.started plan argv_capture is invalid")
        for field in ("parameters", "seeds", "environment", "git", "control_plane_before"):
            if field in plan and not isinstance(plan[field], dict):
                raise EventError(f"run.started plan {field} must be an object")
        if "name" in payload and payload["name"] is not None and not isinstance(
                payload["name"], str):
            raise EventError("run.started name must be a string or null")
        declared_inputs = plan.get("declared_inputs")
        declared_outputs = plan.get("declared_outputs")
        declared_intermediates = plan.get("declared_intermediates", [])
        if (not isinstance(declared_inputs, list)
                or not all(isinstance(item, dict) and isinstance(item.get("path"), str)
                           and item["path"] for item in declared_inputs)):
            raise EventError("run.started declared_inputs must be snapshot objects with paths")
        if (not isinstance(declared_outputs, list)
                or not all(isinstance(item, str) and item for item in declared_outputs)):
            raise EventError("run.started declared_outputs must be a string list")
        input_paths = [item["path"] for item in declared_inputs]
        if len(input_paths) != len(set(input_paths)):
            raise EventError("run.started declared_inputs contains duplicate paths")
        if len(declared_outputs) != len(set(declared_outputs)):
            raise EventError("run.started declared_outputs contains duplicate paths")
        if schema_version in CURRENT_CONTRACT_EVENT_SCHEMAS:
            if (not isinstance(declared_intermediates, list)
                    or not all(isinstance(item, str) and item
                               for item in declared_intermediates)):
                raise EventError(
                    "run.started declared_intermediates must be a string list"
                )
            if len(declared_intermediates) != len(set(declared_intermediates)):
                raise EventError(
                    "run.started declared_intermediates contains duplicate paths"
                )
            overlap = set(declared_outputs) & set(declared_intermediates)
            if overlap:
                raise EventError(
                    "run.started terminal outputs and materialized intermediates "
                    f"overlap: {sorted(overlap)}"
                )
            input_overlap = set(input_paths) & set(declared_intermediates)
            if input_overlap:
                raise EventError(
                    "run.started declared inputs and materialized intermediates "
                    f"overlap: {sorted(input_overlap)}"
                )
        for item in declared_inputs:
            _validate_snapshot(item, item["path"], "run.started declared input")
        if schema_version in CONTRACT_EVENT_SCHEMAS:
            from .pipeline import (
                LEGACY_SNAPSHOT_SCHEMA,
                PREVIOUS_SNAPSHOT_SCHEMA,
                SNAPSHOT_SCHEMA,
                PipelineError,
                validate_pipeline_snapshot,
            )

            try:
                validate_pipeline_snapshot(plan["pipeline_contract"])
            except PipelineError as exc:
                raise EventError(f"run.started pipeline contract is invalid: {exc}") from exc
            expected_snapshot_schemas = {
                LEGACY_CONTRACT_EVENT_SCHEMA: (LEGACY_SNAPSHOT_SCHEMA,),
                CONTRACT_EVENT_SCHEMA: (PREVIOUS_SNAPSHOT_SCHEMA, SNAPSHOT_SCHEMA),
                STAGE_CONTRACT_EVENT_SCHEMA: (
                    PREVIOUS_SNAPSHOT_SCHEMA, SNAPSHOT_SCHEMA,
                ),
            }[schema_version]
            if (plan["pipeline_contract"].get("schema_version")
                    not in expected_snapshot_schemas):
                allowed = " or ".join(
                    f"a {item} contract" for item in expected_snapshot_schemas
                )
                raise EventError(
                    f"{schema_version} requires {allowed}"
                )
            _validate_pipeline_entrypoint_argv(
                plan["argv"], plan["cwd"], plan["pipeline_contract"],
            )
            computation_id = plan.get("computation_id")
            expected_computation_id = (
                "computation:sha256:" + canonical_sha256(_fingerprint_computation(plan))
            )
            if computation_id != expected_computation_id:
                raise EventError(
                    "run.started computation_id does not match the portable computation content"
                )
            if schema_version in CURRENT_CONTRACT_EVENT_SCHEMAS:
                expected_intermediates = _materialized_intermediate_role_paths(
                    plan["pipeline_contract"]
                )
                if declared_intermediates != expected_intermediates:
                    raise EventError(
                        "run.started declared_intermediates do not match the pipeline "
                        "contract materialized-intermediate roles"
                    )
            if schema_version == STAGE_CONTRACT_EVENT_SCHEMA:
                from .pipeline import stage_trace_plan

                if plan.get("stage_trace_plan") != stage_trace_plan(
                        plan["pipeline_contract"]):
                    raise EventError(
                        "run.started stage_trace_plan does not match the pipeline contract"
                    )
                binding = payload.get("stage_trace_binding")
                if (not isinstance(binding, dict)
                        or set(binding) != {
                            "nonce_sha256", "transport", "reporter_scope",
                        }
                        or not SHA256_RE.fullmatch(str(binding.get("nonce_sha256")))
                        or binding.get("transport")
                        != "controller_created_private_file"
                        or binding.get("reporter_scope")
                        != "direct_child_process_id_checked"):
                    raise EventError("run.started stage_trace_binding is invalid")
        expected_plan_id = f"recipe:sha256:{canonical_sha256(_fingerprint_plan(plan))}"
        if payload["plan_id"] != expected_plan_id:
            raise EventError("run.started plan_id does not match canonical plan content")
    elif event_type == "run.finished":
        required_finish = {
            "start_event_id", "plan_id", "result_id", "outcome",
            "direct_child_returncode", "launch_error", "contract_errors",
            "input_transitions", "output_transitions", "receipt_integrity",
            "lineage_coverage",
        }
        if schema_version in CURRENT_CONTRACT_EVENT_SCHEMAS:
            required_finish.add("intermediate_transitions")
        if schema_version == STAGE_CONTRACT_EVENT_SCHEMA:
            required_finish.add("stage_trace")
        _validate_shape(
            payload,
            required_finish,
            {"duration_ns", "window_deltas", "git_after", "control_plane"},
            "run.finished payload",
        )
        for key in ("start_event_id", "plan_id", "result_id", "outcome",
                    "direct_child_returncode", "launch_error", "contract_errors",
                    "input_transitions", "output_transitions", "receipt_integrity",
                    "lineage_coverage"):
            if key not in payload:
                raise EventError(f"run.finished payload is missing {key}")
        if not EVENT_ID_RE.fullmatch(str(payload["start_event_id"])):
            raise EventError("run.finished start_event_id is invalid")
        if not isinstance(payload["plan_id"], str) or not payload["plan_id"].startswith("recipe:sha256:"):
            raise EventError("run.finished plan_id is invalid")
        if not isinstance(payload["result_id"], str) or not payload["result_id"].startswith("result:sha256:"):
            raise EventError("run.finished result_id is invalid")
        if not isinstance(payload["outcome"], str) or payload["outcome"] not in RUN_OUTCOMES:
            raise EventError("run.finished outcome is invalid")
        if not isinstance(payload["input_transitions"], list) or not isinstance(payload["output_transitions"], list):
            raise EventError("run.finished transitions must be lists")
        if (schema_version in CURRENT_CONTRACT_EVENT_SCHEMAS
                and not isinstance(payload["intermediate_transitions"], list)):
            raise EventError("run.finished intermediate_transitions must be a list")
        if payload["receipt_integrity"] != "finalized":
            raise EventError("run.finished receipt integrity is invalid")
        if ("duration_ns" in payload
                and (isinstance(payload["duration_ns"], bool)
                     or not isinstance(payload["duration_ns"], int)
                     or payload["duration_ns"] < 0)):
            raise EventError("run.finished duration_ns must be a non-negative integer")
        if "window_deltas" in payload and not isinstance(payload["window_deltas"], list):
            raise EventError("run.finished window_deltas must be a list")
        for item in payload.get("window_deltas", []):
            _validate_shape(
                item,
                {"path", "transition", "declared_output", "evidence_basis"},
                {"after_sha256"},
                "run.finished window delta",
            )
            if not isinstance(item["path"], str) or not item["path"]:
                raise EventError("run.finished window delta path must be a non-empty string")
            if (not isinstance(item["transition"], str)
                    or item["transition"] not in FILE_TRANSITIONS):
                raise EventError("run.finished window delta transition is invalid")
            if not isinstance(item["declared_output"], bool):
                raise EventError("run.finished window delta declared_output must be boolean")
            if item["evidence_basis"] != "unattributed_pre_post_window":
                raise EventError("run.finished window delta evidence_basis is invalid")
            if ("after_sha256" in item
                    and (not isinstance(item["after_sha256"], str)
                         or not SHA256_RE.fullmatch(item["after_sha256"]))):
                raise EventError("run.finished window delta digest is invalid")
        for field in ("git_after", "control_plane"):
            if field in payload and not isinstance(payload[field], dict):
                raise EventError(f"run.finished {field} must be an object")
        lineage_schema = (
            STAGE_CONTRACT_LINEAGE_COVERAGE_VALUES
            if schema_version == STAGE_CONTRACT_EVENT_SCHEMA
            else CONTRACT_LINEAGE_COVERAGE_VALUES
            if schema_version == CONTRACT_EVENT_SCHEMA
            else LINEAGE_COVERAGE_VALUES
        )
        _validate_token_object(
            payload["lineage_coverage"], lineage_schema,
            "run.finished lineage_coverage")
        if (not isinstance(payload.get("contract_errors", []), list)
                or not all(isinstance(item, str) for item in payload.get("contract_errors", []))):
            raise EventError("run.finished contract_errors must be a string list")
        transition_roles = ["input_transitions", "output_transitions"]
        if schema_version in CURRENT_CONTRACT_EVENT_SCHEMAS:
            transition_roles.append("intermediate_transitions")
        role_paths = {}
        for role in transition_roles:
            paths = []
            for item in payload[role]:
                _validate_transition_item(item, role)
                paths.append(item["path"])
            if len(paths) != len(set(paths)):
                raise EventError(f"run.finished {role} contains duplicate paths")
            role_paths[role] = set(paths)
        if schema_version in CURRENT_CONTRACT_EVENT_SCHEMAS:
            overlap = role_paths["output_transitions"] & role_paths["intermediate_transitions"]
            if overlap:
                raise EventError(
                    "run.finished output and intermediate transition paths overlap: "
                    f"{sorted(overlap)}"
                )
            input_overlap = role_paths["input_transitions"] & role_paths["intermediate_transitions"]
            if input_overlap:
                raise EventError(
                    "run.finished input and intermediate transition paths overlap: "
                    f"{sorted(input_overlap)}"
                )
        _validate_outcome(payload)
        result_basis = {
            "plan_id": payload["plan_id"],
            "outcome": payload["outcome"],
            "direct_child_returncode": payload.get("direct_child_returncode"),
            "input_transitions": [_fingerprint_transition(item) for item in payload["input_transitions"]],
            "output_transitions": [_fingerprint_transition(item) for item in payload["output_transitions"]],
            "contract_errors": payload.get("contract_errors", []),
        }
        if schema_version in CURRENT_CONTRACT_EVENT_SCHEMAS:
            result_basis["intermediate_transitions"] = [
                _fingerprint_transition(item)
                for item in payload["intermediate_transitions"]
            ]
        if schema_version == STAGE_CONTRACT_EVENT_SCHEMA:
            from .pipeline import PipelineError, validate_stage_trace

            try:
                validate_stage_trace(payload["stage_trace"])
            except PipelineError as exc:
                raise EventError(f"run.finished stage trace is invalid: {exc}") from exc
            if (payload["outcome"] == "succeeded"
                    and payload["stage_trace"]["state"]
                    != "cooperative_report_complete"):
                raise EventError(
                    "run.finished succeeded event/4 requires a complete cooperative stage trace"
                )
            result_basis["stage_trace"] = _fingerprint_stage_trace(
                payload["stage_trace"]
            )
        expected_result_id = f"result:sha256:{canonical_sha256(result_basis)}"
        if payload["result_id"] != expected_result_id:
            raise EventError("run.finished result_id does not match canonical result content")


def validate_event(event: dict) -> str:
    if not isinstance(event, dict):
        raise EventError("event must be a JSON object")
    expected = {"id", "schema_version", "type", "run_id", "recorded_at", "payload"}
    extras = set(event) - expected
    missing = expected - set(event)
    if extras or missing:
        raise EventError(f"event keys differ from schema (missing={sorted(missing)}, extra={sorted(extras)})")
    if event["schema_version"] not in SUPPORTED_EVENT_SCHEMAS:
        raise EventError(f"unsupported event schema: {event['schema_version']!r}")
    if not isinstance(event["type"], str) or event["type"] not in EVENT_TYPES:
        raise EventError(f"unsupported event type: {event['type']!r}")
    if not RUN_ID_RE.fullmatch(str(event["run_id"])):
        raise EventError(f"invalid run id: {event['run_id']!r}")
    _validate_recorded_at(event["recorded_at"])
    if not isinstance(event["payload"], dict):
        raise EventError("event payload must be an object")
    _validate_event_payload(event["type"], event["payload"], event["schema_version"])
    match = EVENT_ID_RE.fullmatch(str(event["id"]))
    if not match:
        raise EventError(f"invalid event id: {event['id']!r}")
    core = {key: event[key] for key in ("schema_version", "type", "run_id", "recorded_at", "payload")}
    actual = canonical_sha256(core)
    if actual != match.group(1):
        raise EventError(f"event content hash mismatch: id says {match.group(1)}, content is {actual}")
    return actual


def _open_stable_lock_file(path: Path, label: str):
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise EventError(f"cannot open {label} lock: {exc}") from exc
    handle = None
    try:
        handle = os.fdopen(descriptor, "a+b")
        opened = os.fstat(handle.fileno())
        current = path.lstat()
        if (not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
            raise EventError(f"{label} lock path is unstable")
        return handle
    except Exception:
        try:
            if handle is not None:
                handle.close()
            else:
                os.close(descriptor)
        except OSError:
            pass
        raise


def _verify_lock_file_identity(handle, path: Path, label: str) -> None:
    try:
        opened = os.fstat(handle.fileno())
        current = path.lstat()
    except OSError as exc:
        raise EventError(f"{label} lock path changed: {exc}") from exc
    if (not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
        raise EventError(f"{label} lock path changed while acquiring it")


@contextmanager
def _event_lock(path: Path, timeout: float = 30.0):
    lock_root = _private_runtime_root("claimtrace-event-locks")
    lock_name = hashlib.sha256(os.path.normcase(str(path.resolve())).encode("utf-8")).hexdigest() + ".lock"
    lock_path = lock_root / lock_name
    deadline = time.monotonic() + timeout
    with _PROCESS_EVENT_LOCK, _open_stable_lock_file(lock_path, "event") as handle:
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise EventError(f"timed out waiting for event lock: {path.name}")
                time.sleep(0.02)
        try:
            _verify_lock_file_identity(handle, lock_path, "event")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _output_thread_lock(identity: str) -> threading.Lock:
    with _OUTPUT_THREAD_LOCKS_GUARD:
        return _OUTPUT_THREAD_LOCKS.setdefault(identity, threading.Lock())


@contextmanager
def _one_output_lock(identity: str, display: str, lock_root: Path, deadline: float):
    """Hold one in-process and advisory cross-process output-path lock."""
    thread_lock = _output_thread_lock(identity)
    remaining = max(0.0, deadline - time.monotonic())
    if not thread_lock.acquire(timeout=remaining):
        raise EventError(f"timed out waiting for declared output lock: {display}")

    handle = None
    advisory_locked = False
    try:
        lock_name = hashlib.sha256(identity.encode("utf-8")).hexdigest() + ".lock"
        lock_path = lock_root / lock_name
        handle = _open_stable_lock_file(lock_path, "declared output")
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                advisory_locked = True
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise EventError(
                        f"timed out waiting for declared output lock: {display}"
                    )
                time.sleep(0.02)
        _verify_lock_file_identity(handle, lock_path, "declared output")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        yield
    finally:
        try:
            if advisory_locked and handle is not None:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                if handle is not None:
                    handle.close()
            finally:
                thread_lock.release()


@contextmanager
def _output_locks(declared_outputs, timeout: float = 30.0):
    """Lock declared output paths in stable order for the complete receipt window."""
    lock_root = _private_runtime_root("claimtrace-output-locks")
    ordered = sorted(
        (os.path.normcase(str(path.resolve(strict=False))), display)
        for path, display in declared_outputs
    )
    deadline = time.monotonic() + timeout
    with ExitStack() as stack:
        for identity, display in ordered:
            stack.enter_context(_one_output_lock(identity, display, lock_root, deadline))
        yield


def _event_path(events_path: Path, digest: str) -> Path:
    return events_path / digest[:2] / f"{digest}.json"


def _fsync_directory(path: Path) -> bool:
    """Best-effort directory-entry durability where directory fsync is supported."""
    if os.name == "nt":
        return False
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        return False
    return True


def _ensure_directory_durable(path: Path, *, mode: int = 0o777) -> None:
    """Create each missing directory level and sync its parent where supported."""
    path = Path(path)
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=mode)
        except FileExistsError:
            pass
        if (_path_has_reparse_component(directory) or not directory.is_dir()
                or directory.is_symlink()):
            raise EventError(f"directory path is not trustworthy: {directory}")
        _fsync_directory(directory.parent)
    if (_path_has_reparse_component(path) or not path.is_dir() or path.is_symlink()):
        raise EventError(f"directory path is not trustworthy: {path}")


def append_event(events_path: Path, event: dict) -> Path:
    """Append one content-addressed event without overwriting existing evidence."""
    digest = validate_event(event)
    destination = _event_path(Path(events_path), digest)
    _ensure_directory_durable(destination.parent)
    data = (json.dumps(event, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(data) > MAX_EVENT_DOCUMENT_BYTES:
        raise EventError("event document exceeds its byte limit")

    def validate_existing():
        try:
            payload = _stable_bounded_bytes(destination, MAX_EVENT_DOCUMENT_BYTES)
            existing = _strict_json(payload.decode("utf-8-sig"), str(destination))
            validate_event(existing)
        except (OSError, UnicodeError, ValueError, RecursionError, EventError) as exc:
            raise EventError(f"existing event is not stable and valid: {destination}: {exc}") from exc
        if canonical_bytes(existing) != canonical_bytes(event):
            raise EventError(f"refusing to overwrite non-identical event: {destination}")
        return destination

    lock = destination.with_suffix(".lock")
    with _event_lock(lock):
        if destination.exists():
            return validate_existing()
        tmp = destination.parent / f".{digest}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(tmp, destination)
                _fsync_directory(destination.parent)
            except FileExistsError:
                return validate_existing()
            except OSError as exc:
                raise EventError(f"cannot atomically append event: {exc}") from exc
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
    return destination


def load_events(events_path: Path):
    """Return validated events and deterministic structured integrity issues."""
    base = Path(events_path)
    if not base.exists():
        return [], []
    events, issues = [], []
    if _path_has_reparse_component(base) or not base.is_dir():
        return [], [{
            "code": "EVENT_INTEGRITY", "event_path": ".",
            "detail": "event store must be a non-link directory",
        }]
    paths = []
    aggregate_bytes = 0
    entry_count = 0
    try:
        for current, dirs, files in os.walk(base, topdown=True, followlinks=False):
            current_path = Path(current)
            kept = []
            for name in sorted(dirs):
                entry_count += 1
                if entry_count > MAX_EVENT_STORE_ENTRIES:
                    raise EventError("event store exceeds its entry-count limit")
                candidate = current_path / name
                if _path_has_reparse_component(candidate):
                    issues.append({
                        "code": "EVENT_INTEGRITY",
                        "event_path": candidate.relative_to(base).as_posix(),
                        "detail": "event store must not traverse a link or reparse point",
                    })
                else:
                    kept.append(name)
            dirs[:] = kept
            for name in sorted(files):
                entry_count += 1
                if entry_count > MAX_EVENT_STORE_ENTRIES:
                    raise EventError("event store exceeds its entry-count limit")
                path = current_path / name
                if path.suffix != ".json":
                    continue
                try:
                    size = path.lstat().st_size
                except OSError as exc:
                    raise EventError(f"cannot inspect event document: {exc}") from exc
                paths.append((path, size))
                if len(paths) > MAX_EVENT_FILES:
                    raise EventError("event store exceeds its event-count limit")
                if size > MAX_EVENT_DOCUMENT_BYTES:
                    raise EventError("event document exceeds its byte limit")
                aggregate_bytes += size
                if aggregate_bytes > MAX_EVENT_STORE_BYTES:
                    raise EventError("event store exceeds its aggregate-byte limit")
    except (OSError, EventError) as exc:
        issues.append({"code": "EVENT_INTEGRITY", "event_path": ".", "detail": str(exc)})
        return [], sorted(
            issues, key=lambda item: (item["code"], item["event_path"], item["detail"]),
        )
    seen = set()
    for path, preflight_size in sorted(paths, key=lambda item: item[0].as_posix()):
        try:
            payload = _stable_bounded_bytes(path, preflight_size)
            event = _strict_json(payload.decode("utf-8-sig"), str(path))
            digest = validate_event(event)
            expected = _event_path(base, digest).resolve()
            if path.resolve() != expected:
                raise EventError("event filename or fan-out directory does not match its content hash")
            if event["id"] in seen:
                raise EventError(f"duplicate event id: {event['id']}")
            seen.add(event["id"])
            events.append(event)
        except (OSError, UnicodeError, TypeError, ValueError, RecursionError, EventError) as exc:
            try:
                rel = path.relative_to(base).as_posix()
            except ValueError:
                rel = path.name
            issues.append({"code": "EVENT_INTEGRITY", "event_path": rel, "detail": str(exc)})
    events.sort(key=lambda e: (e["recorded_at"], e["id"]))
    issues.sort(key=lambda i: (i["code"], i["event_path"], i["detail"]))
    return events, issues


def _private_runtime_root(name: str) -> Path:
    root_name = f"{name}-{os.getuid()}" if hasattr(os, "getuid") else name
    try:
        # The OS-provided temporary directory can legitimately be reached through
        # a system symlink (for example, /var -> /private/var on macOS). Resolve
        # that trusted base once, then reject links introduced below it.
        temporary_base = Path(tempfile.gettempdir()).resolve(strict=True)
    except OSError as exc:
        raise EventError(f"cannot resolve the system temporary directory: {exc}") from exc
    root = temporary_base / root_name
    if _path_has_reparse_component(root):
        raise EventError(f"{name} path traverses a link or reparse point")
    _ensure_directory_durable(root, mode=0o700)
    try:
        info = root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or _path_has_reparse_component(root)
                or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
            raise EventError(f"{name} directory is not private and trustworthy")
        if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
            os.chmod(root, 0o700)
            info = root.lstat()
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise EventError(f"{name} directory permissions are not private")
    except OSError as exc:
        raise EventError(f"cannot inspect {name} directory: {exc}") from exc
    return root


def _marker_directory(project_root: Path) -> Path:
    key = hashlib.sha256(os.path.normcase(str(project_root.resolve())).encode("utf-8")).hexdigest()[:20]
    return _private_runtime_root("claimtrace-active-runs") / key


def create_active_marker(project_root: Path, run_id: str, start_event: dict) -> Path:
    directory = _marker_directory(project_root)
    if _path_has_reparse_component(directory):
        raise EventError("active-marker path traverses a link or reparse point")
    _ensure_directory_durable(directory, mode=0o700)
    if (_path_has_reparse_component(directory) or not directory.is_dir()
            or directory.is_symlink()):
        raise EventError("active-marker directory is not a trustworthy local directory")
    _fsync_directory(directory.parent)
    marker = directory / f"{run_id[4:]}.json"
    payload = {
        "schema_version": ACTIVE_SCHEMA,
        "project_root": str(project_root.resolve()),
        "run_id": run_id,
        "start_event": start_event,
    }
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(data) > MAX_EVENT_DOCUMENT_BYTES:
        raise EventError("active marker exceeds its byte limit")
    if marker.exists():
        raise EventError(f"active marker already exists for {run_id}")
    temporary = directory / f".{run_id[4:]}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            if _path_has_reparse_component(directory):
                raise EventError("active-marker path changed before publication")
            os.link(temporary, marker)
            if _path_has_reparse_component(directory):
                raise EventError("active-marker path changed during publication")
            _fsync_directory(directory)
        except FileExistsError as exc:
            raise EventError(f"active marker already exists for {run_id}") from exc
        except OSError as exc:
            raise EventError(f"cannot atomically create active marker: {exc}") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return marker


def remove_active_marker(marker: Path) -> None:
    parent = marker.parent
    marker.unlink()
    _fsync_directory(parent)
    try:
        parent.rmdir()
        _fsync_directory(parent.parent)
    except OSError:
        pass


def load_active_markers(project_root: Path):
    directory = _marker_directory(project_root)
    if not directory.exists():
        return [], []
    if _path_has_reparse_component(directory) or not directory.is_dir():
        return [], [{
            "code": "ACTIVE_MARKER_INTEGRITY", "marker": ".",
            "detail": "active-marker store must be a non-link directory",
        }]
    markers, issues = [], []
    entries = []
    try:
        with os.scandir(directory) as iterator:
            for entry in iterator:
                if len(entries) >= MAX_ACTIVE_MARKER_FILES:
                    raise EventError(
                        "active-marker store exceeds its file-count limit"
                    )
                entries.append(Path(entry.path))
    except EventError as exc:
        return [], [{
            "code": "ACTIVE_MARKER_INTEGRITY", "marker": ".",
            "detail": str(exc),
        }]
    except OSError as exc:
        return [], [{
            "code": "ACTIVE_MARKER_INTEGRITY", "marker": ".", "detail": str(exc),
        }]
    paths = sorted((path for path in entries if path.suffix == ".json"), key=lambda p: p.name)
    aggregate_bytes = 0
    for path in paths:
        try:
            size = path.lstat().st_size
        except OSError as exc:
            issues.append({
                "code": "ACTIVE_MARKER_INTEGRITY", "marker": path.name,
                "detail": str(exc),
            })
            continue
        aggregate_bytes += size
        if size > MAX_EVENT_DOCUMENT_BYTES or aggregate_bytes > MAX_ACTIVE_MARKER_STORE_BYTES:
            issues.append({
                "code": "ACTIVE_MARKER_INTEGRITY", "marker": path.name,
                "detail": "active-marker store exceeds its byte limit",
            })
            continue
        try:
            payload = _stable_bounded_bytes(path, size)
            item = _strict_json(payload.decode("utf-8-sig"), str(path))
            _validate_shape(
                item, {"schema_version", "project_root", "run_id", "start_event"},
                set(), "active marker",
            )
            if item["schema_version"] != ACTIVE_SCHEMA:
                raise EventError("unsupported active marker schema")
            if (not isinstance(item["project_root"], str)
                    or Path(item["project_root"]).resolve() != project_root.resolve()):
                raise EventError("active marker belongs to a different project root")
            if not isinstance(item["start_event"], dict):
                raise EventError("active marker start_event must be an object")
            if item["run_id"] != item["start_event"].get("run_id"):
                raise EventError("active marker run id does not match start event")
            validate_event(item["start_event"])
            markers.append(item)
        except (OSError, UnicodeError, TypeError, ValueError, RecursionError, EventError) as exc:
            issues.append({"code": "ACTIVE_MARKER_INTEGRITY", "marker": path.name, "detail": str(exc)})
    markers.sort(key=lambda item: item["run_id"])
    issues.sort(key=lambda item: (item["code"], item["marker"], item["detail"]))
    return markers, issues


def materialize_runs(events: list[dict]):
    starts, finishes, issues = {}, {}, []
    for event in events:
        run_id = event["run_id"]
        if event["type"] == "run.started":
            if run_id in starts:
                issues.append({"code": "DUPLICATE_RUN_START", "run_id": run_id, "detail": "multiple start events"})
            else:
                starts[run_id] = event
        elif event["type"] == "run.finished":
            if run_id in finishes:
                issues.append({"code": "DUPLICATE_RUN_FINISH", "run_id": run_id, "detail": "multiple finish events"})
            else:
                finishes[run_id] = event
    runs = []
    for run_id in sorted(set(starts) | set(finishes)):
        start, finish = starts.get(run_id), finishes.get(run_id)
        if not start:
            issues.append({"code": "RUN_FINISH_WITHOUT_START", "run_id": run_id, "detail": "finish event has no stored start"})
        if not finish:
            issues.append({"code": "RUN_START_WITHOUT_FINISH", "run_id": run_id, "detail": "start event has no stored finish"})
        if start and finish and finish["payload"].get("start_event_id") != start["id"]:
            issues.append({"code": "RUN_EVENT_LINK_MISMATCH", "run_id": run_id, "detail": "finish does not reference its start event"})
        if start and finish and finish["payload"].get("plan_id") != start["payload"].get("plan_id"):
            issues.append({"code": "RUN_PLAN_LINK_MISMATCH", "run_id": run_id, "detail": "finish plan id differs from start plan id"})
        if start and finish and finish.get("schema_version") != start.get("schema_version"):
            issues.append({
                "code": "RUN_SCHEMA_LINK_MISMATCH", "run_id": run_id,
                "detail": "finish event schema differs from start event schema",
            })
        if start and finish:
            plan = start["payload"].get("plan", {})
            expected_inputs = [item.get("path") for item in plan.get("declared_inputs", [])]
            expected_outputs = list(plan.get("declared_outputs", []))
            expected_intermediates = list(plan.get("declared_intermediates", []))
            actual_inputs = [item.get("path") for item in finish["payload"].get("input_transitions", [])]
            actual_outputs = [item.get("path") for item in finish["payload"].get("output_transitions", [])]
            actual_intermediates = [
                item.get("path")
                for item in finish["payload"].get("intermediate_transitions", [])
            ]
            if actual_inputs != expected_inputs:
                issues.append({
                    "code": "RUN_INPUT_ROLE_MISMATCH", "run_id": run_id,
                    "detail": "finish input transitions do not match start declared inputs",
                })
            if actual_outputs != expected_outputs:
                issues.append({
                    "code": "RUN_OUTPUT_ROLE_MISMATCH", "run_id": run_id,
                    "detail": "finish output transitions do not match start declared outputs",
                })
            if actual_intermediates != expected_intermediates:
                issues.append({
                    "code": "RUN_INTERMEDIATE_ROLE_MISMATCH", "run_id": run_id,
                    "detail": (
                        "finish intermediate transitions do not match start declared "
                        "materialized intermediates"
                    ),
                })
            if actual_inputs == expected_inputs:
                for planned, transition in zip(
                        plan.get("declared_inputs", []),
                        finish["payload"].get("input_transitions", [])):
                    if _fingerprint_snapshot(planned) != _fingerprint_snapshot(transition["before"]):
                        issues.append({
                            "code": "RUN_INPUT_SNAPSHOT_MISMATCH", "run_id": run_id,
                            "detail": (
                                f"finish before snapshot for {planned.get('path')} differs from "
                                "the start declared-input snapshot"
                            ),
                        })
            capture_scope = start["payload"].get("capture_scope", {})
            lineage_coverage = finish["payload"].get("lineage_coverage", {})
            expected_write_attribution = CAPTURE_WRITE_ATTRIBUTION.get(
                capture_scope.get("writes"))
            if (capture_scope.get("reads") != lineage_coverage.get("reads")
                    or capture_scope.get("processes") != lineage_coverage.get("processes")
                    or expected_write_attribution != lineage_coverage.get("write_attribution")):
                issues.append({
                    "code": "RUN_COVERAGE_MISMATCH", "run_id": run_id,
                    "detail": "finish lineage coverage contradicts the start capture scope",
                })
            if start.get("schema_version") == STAGE_CONTRACT_EVENT_SCHEMA:
                from .pipeline import (
                    PipelineError,
                    validate_stage_trace_against_snapshot,
                )

                trace = finish["payload"].get("stage_trace")
                binding = start["payload"].get("stage_trace_binding", {})
                if (not isinstance(trace, dict)
                        or trace.get("binding", {}).get("nonce_sha256")
                        != binding.get("nonce_sha256")
                        or trace.get("binding", {}).get("reporter_scope")
                        != binding.get("reporter_scope")):
                    issues.append({
                        "code": "RUN_STAGE_TRACE_BINDING_MISMATCH",
                        "run_id": run_id,
                        "detail": "finish stage trace is not bound to the run start",
                    })
                else:
                    try:
                        validate_stage_trace_against_snapshot(
                            trace, plan["pipeline_contract"],
                        )
                    except (KeyError, PipelineError) as exc:
                        issues.append({
                            "code": "RUN_STAGE_TRACE_CONTRACT_MISMATCH",
                            "run_id": run_id,
                            "detail": str(exc),
                        })
        runs.append({"run_id": run_id, "start": start, "finish": finish})
    issues.sort(key=lambda item: (item["code"], item["run_id"], item["detail"]))
    return runs, issues


def _contains_glob(value: str) -> bool:
    return any(char in value for char in GLOB_CHARS)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _path_has_reparse_component(path: Path, *, stop_at: Path | None = None) -> bool:
    """Return whether a lexical path traverses a link/reparse component.

    ``stop_at`` is an inclusive trusted boundary: the boundary itself and every
    component below it are checked, while aliases in its parents are outside the
    scan.  Callers that do not supply it retain the original filesystem-root
    check.
    """
    current = Path(os.path.abspath(path))
    boundary = None
    if stop_at is not None:
        boundary = Path(os.path.abspath(stop_at))
        if not _is_within(current, boundary):
            return True
    while True:
        if current.exists() or current.is_symlink():
            try:
                st = current.lstat()
            except OSError:
                return True
            if stat.S_ISLNK(st.st_mode):
                return True
            attrs = getattr(st, "st_file_attributes", 0)
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if reparse and attrs & reparse:
                return True
        if boundary is not None and current == boundary:
            return False
        if current.parent == current:
            return boundary is not None
        current = current.parent


def resolve_declared_path(project_root: Path, value: str, allow_external: bool = False):
    if not value or _contains_glob(value):
        raise EventError(f"declared path must be a literal file path, not a glob: {value!r}")
    supplied = Path(value)
    candidate = supplied if supplied.is_absolute() else project_root / supplied
    absolute = candidate.absolute()
    if _path_has_reparse_component(absolute):
        raise EventError(f"symlinks, junctions, and reparse paths are not supported: {value}")
    resolved = absolute.resolve(strict=False)
    if not _is_within(resolved, project_root) and not allow_external:
        raise EventError(f"declared path is outside the project root: {value}")
    display = resolved.relative_to(project_root).as_posix() if _is_within(resolved, project_root) else str(resolved)
    return resolved, display


def _stat_identity(st) -> tuple:
    return (
        st.st_dev, st.st_ino, st.st_mode, st.st_size,
        getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)),
        getattr(st, "st_ctime_ns", int(st.st_ctime * 1e9)),
    )


class _SnapshotUnstable(Exception):
    pass


class _SnapshotUnsupported(Exception):
    def __init__(self, mode):
        self.mode = mode


class _SnapshotLimit(Exception):
    pass


@contextmanager
def _stable_file_descriptor(path: Path):
    """Open one regular path and prove that the descriptor/path identity stayed fixed."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise _SnapshotUnsupported(before.st_mode)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise _SnapshotUnstable("disappeared_before_open") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise _SnapshotUnsupported(opened.st_mode)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise _SnapshotUnstable("path_changed_before_open")
        yield descriptor, opened
        after_descriptor = os.fstat(descriptor)
        try:
            after_path = path.lstat()
        except FileNotFoundError as exc:
            raise _SnapshotUnstable("disappeared_while_hashing") from exc
        if _stat_identity(opened) != _stat_identity(after_descriptor):
            raise _SnapshotUnstable("descriptor_metadata_changed_while_hashing")
        if (not stat.S_ISREG(after_path.st_mode)
                or (opened.st_dev, opened.st_ino) != (after_path.st_dev, after_path.st_ino)):
            raise _SnapshotUnstable("path_identity_changed_while_hashing")
        if _stat_identity(before) != _stat_identity(after_path):
            raise _SnapshotUnstable("metadata_changed_while_hashing")
    finally:
        os.close(descriptor)


def snapshot_file(path: Path, display: str, *, limit: int | None = None) -> dict:
    base = {"path": display, "method": "sha256_stat_before_after"}
    try:
        digest = hashlib.sha256()
        total = 0
        with _stable_file_descriptor(path) as (descriptor, opened):
            if limit is not None and opened.st_size > limit:
                raise _SnapshotLimit
            while True:
                read_size = (
                    1024 * 1024
                    if limit is None
                    else min(1024 * 1024, limit + 1 - total)
                )
                chunk = os.read(descriptor, read_size)
                if not chunk:
                    break
                total += len(chunk)
                if limit is not None and total > limit:
                    raise _SnapshotLimit
                digest.update(chunk)
            if total != opened.st_size:
                raise _SnapshotUnstable("hashed_size_does_not_match_opened_file")
    except FileNotFoundError:
        return {**base, "state": "missing"}
    except _SnapshotUnsupported as exc:
        return {**base, "state": "unsupported", "file_kind": stat.filemode(exc.mode)}
    except _SnapshotUnstable as exc:
        return {**base, "state": "unstable", "reason": str(exc)}
    except _SnapshotLimit:
        return {**base, "state": "unreadable", "error": "SizeLimitExceeded"}
    except (OSError, ValueError) as exc:
        return {**base, "state": "unreadable", "error": type(exc).__name__}
    sha = digest.hexdigest()
    return {
        **base,
        "state": "stable",
        "sha256": sha,
        "size": total,
        "mtime_ns": getattr(opened, "st_mtime_ns", int(opened.st_mtime * 1e9)),
        "file_version_id": f"file:sha256:{sha}",
    }


def _stable_bounded_bytes(path: Path, limit: int) -> bytes:
    try:
        with _stable_file_descriptor(path) as (descriptor, opened):
            if opened.st_size > limit:
                raise EventError(f"file exceeds the {limit}-byte limit")
            chunks = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(1024 * 1024, limit + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > limit:
                    raise EventError(f"file exceeds the {limit}-byte limit")
            if total != opened.st_size:
                raise _SnapshotUnstable("read_size_does_not_match_opened_file")
            return b"".join(chunks)
    except (_SnapshotUnsupported, _SnapshotUnstable) as exc:
        raise EventError(f"file is not a stable regular file: {exc}") from exc


def _transition(before: dict, after: dict) -> str:
    bstate, astate = before["state"], after["state"]
    if bstate == "missing" and astate == "stable":
        return "created"
    if bstate == "stable" and astate == "missing":
        return "deleted"
    if bstate == "missing" and astate == "missing":
        return "missing"
    if bstate == "stable" and astate == "stable":
        return "unchanged" if before["sha256"] == after["sha256"] else "content_changed"
    return "unstable"


def _fingerprint_snapshot(snapshot: dict) -> dict:
    """Keep content identity and state, excluding display-only filesystem timestamps."""
    return {key: snapshot[key] for key in ("path", "state", "sha256", "file_version_id", "size")
            if key in snapshot}


def _fingerprint_plan(plan: dict) -> dict:
    result = dict(plan)
    result["declared_inputs"] = [_fingerprint_snapshot(item) for item in plan.get("declared_inputs", [])]
    control = plan.get("control_plane_before")
    if isinstance(control, dict):
        # Prior receipt count is operational ledger history, not part of the analysis recipe.
        result["control_plane_before"] = {"files": control.get("files", [])}
    return result


def _fingerprint_computation(plan: dict) -> dict:
    """Portable identity of the declared computation, excluding ledger/run history.

    The contract ID already commits the exact code and method snapshots.  Git state,
    control-plane ledger counts, timestamps, and the run UUID are audit context rather
    than computation identity and are deliberately excluded.
    """
    contract = plan.get("pipeline_contract") or {}
    result = {
        "argv": list(plan.get("argv", [])),
        "argv_capture": plan.get("argv_capture"),
        "cwd": plan.get("cwd"),
        "declared_inputs": [
            _fingerprint_snapshot(item) for item in plan.get("declared_inputs", [])
        ],
        "declared_outputs": list(plan.get("declared_outputs", [])),
        "parameters": dict(plan.get("parameters", {})),
        "seeds": dict(plan.get("seeds", {})),
        "environment": dict(plan.get("environment", {})),
        "pipeline_contract_id": contract.get("id"),
    }
    if "declared_intermediates" in plan:
        result["declared_intermediates"] = list(plan["declared_intermediates"])
    return result


def _fingerprint_transition(item: dict) -> dict:
    return {
        "path": item["path"],
        "transition": item["transition"],
        **({"produced": item["produced"]} if "produced" in item else {}),
        "before": _fingerprint_snapshot(item["before"]),
        "after": _fingerprint_snapshot(item["after"]),
    }


def _fingerprint_stage_trace(trace: dict) -> dict:
    """Exclude per-execution nonce, raw-journal, and process binding material."""
    return {
        key: trace[key]
        for key in (
            "schema_version", "mode", "state", "trust", "required_stage_ids",
            "checkpoints", "checkpoint_sequence_sha256", "issues",
        )
    }


def _kill_and_reap_child(process, *, timeout_seconds: float = 5.0) -> list[str]:
    """Best-effort bounded cleanup for a child whose normal wait did not complete."""
    issues = []
    try:
        running = process.poll() is None
    except OSError as exc:
        running = True
        issues.append(f"child poll failed: {type(exc).__name__}: {exc}")
    if running:
        try:
            process.kill()
        except OSError as exc:
            issues.append(f"child kill failed: {type(exc).__name__}: {exc}")
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        issues.append("child did not exit within the bounded cleanup wait")
    except OSError as exc:
        issues.append(f"child reap failed: {type(exc).__name__}: {exc}")
    return issues


def _scan_project(project_root: Path, events_path: Path):
    result = {}
    events_resolved = events_path.resolve(strict=False)
    for current, dirs, files in os.walk(project_root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept = []
        for name in sorted(dirs):
            candidate = current_path / name
            if name in DEFAULT_IGNORED_DIRS or candidate.resolve(strict=False) == events_resolved:
                continue
            if _path_has_reparse_component(candidate):
                continue
            kept.append(name)
        dirs[:] = kept
        for name in sorted(files):
            path = current_path / name
            if _path_has_reparse_component(path):
                continue
            rel = path.relative_to(project_root).as_posix()
            snap = snapshot_file(path, rel)
            if snap["state"] in {"stable", "unstable", "unreadable"}:
                result[rel] = snap
    return result


def _window_deltas(before: dict, after: dict, declared_outputs: set[str]):
    deltas = []
    for path in sorted(set(before) | set(after)):
        b = before.get(path, {"path": path, "state": "missing"})
        a = after.get(path, {"path": path, "state": "missing"})
        transition = _transition(b, a)
        if transition == "unchanged":
            continue
        item = {
            "path": path,
            "transition": transition,
            "declared_output": path in declared_outputs,
            "evidence_basis": "unattributed_pre_post_window",
        }
        if a.get("sha256"):
            item["after_sha256"] = a["sha256"]
        deltas.append(item)
    return deltas


def _git_command(project_root: Path, args: list[str]):
    try:
        proc = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(project_root), *args],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        )
    except OSError:
        return None
    return proc.stdout if proc.returncode == 0 else None


def capture_git(project_root: Path) -> dict:
    inside = _git_command(project_root, ["rev-parse", "--is-inside-work-tree"])
    if not inside or inside.strip() != b"true":
        return {"state": "not_a_repository"}
    head_raw = _git_command(project_root, ["rev-parse", "--verify", "HEAD"])
    branch_raw = _git_command(project_root, ["symbolic-ref", "--short", "-q", "HEAD"])
    unstaged = _git_command(project_root, ["diff", "--no-ext-diff", "--binary", "--", "."])
    staged = _git_command(project_root, ["diff", "--cached", "--no-ext-diff", "--binary", "--", "."])
    if unstaged is None or staged is None:
        return {"state": "capture_failed"}
    tracked_patch = staged + b"\0" + unstaged
    return {
        "state": "captured",
        "head": head_raw.decode("ascii", "replace").strip() if head_raw else "unborn",
        "branch": branch_raw.decode("utf-8", "replace").strip() if branch_raw else "detached",
        "tracked_dirty": bool(staged or unstaged),
        "tracked_patch_sha256": hashlib.sha256(tracked_patch).hexdigest(),
        "untracked_files": "not_captured",
    }


def _semantic_control_paths(cfg) -> list[tuple[Path, int]]:
    """Return configured meaning-bearing files without following untrusted trees."""
    terminology_paths = list(getattr(cfg, "semantic_terminology_paths", []))
    ontology_lock_paths = list(getattr(cfg, "semantic_ontology_lock_paths", []))
    if len(terminology_paths) > 1_000 or len(ontology_lock_paths) > 256:
        raise EventError("semantic control-plane configured-path count exceeds its limit")
    max_json_bytes = 32 * 1024 * 1024
    max_store_document_bytes = 2 * 1024 * 1024
    ontology_budget = int(getattr(cfg, "semantic_max_ontology_bytes", 512 * 1024 * 1024))
    for path in [*terminology_paths, *ontology_lock_paths]:
        if _path_has_reparse_component(Path(path)):
            raise EventError(
                "semantic control-plane source traverses a link or reparse point"
            )
    paths = []
    aggregate_json_bytes = 0
    for terminology_path in terminology_paths:
        try:
            size = Path(terminology_path).lstat().st_size
        except FileNotFoundError:
            size = 0
        except (OSError, ValueError) as exc:
            raise EventError(f"cannot inspect semantic terminology: {exc}") from exc
        aggregate_json_bytes += size
        if size > max_json_bytes or aggregate_json_bytes > 128 * 1024 * 1024:
            raise EventError("semantic control-plane JSON assets exceed their byte limit")
        paths.append((Path(terminology_path), size))
    # Extract portable member paths even when another configured asset is
    # invalid. This keeps a parseable lock's raw/index bytes in the runtime
    # control plane instead of dropping every member on all-or-nothing load.
    aggregate_manifest_bytes = 0
    aggregate_ontology_documents = 0
    for lock_path in ontology_lock_paths:
        lock_path = Path(lock_path)
        try:
            lock_size = lock_path.lstat().st_size
        except FileNotFoundError:
            lock_size = 0
        except (OSError, ValueError) as exc:
            raise EventError(f"cannot inspect semantic ontology lock: {exc}") from exc
        if lock_size > max_json_bytes:
            raise EventError("semantic control-plane lock manifest exceeds its byte limit")
        paths.append((lock_path, lock_size))
        try:
            payload = _stable_bounded_bytes(lock_path, lock_size)
            aggregate_manifest_bytes += len(payload)
            aggregate_json_bytes += len(payload)
            if aggregate_manifest_bytes > 128 * 1024 * 1024:
                raise EventError(
                    "semantic control-plane lock manifests exceed the aggregate byte limit"
                )
            if aggregate_json_bytes > 128 * 1024 * 1024:
                raise EventError("semantic control-plane JSON assets exceed their byte limit")
            raw = _strict_json(payload.decode("utf-8-sig"), str(lock_path))
        except EventError:
            raise
        except (OSError, UnicodeError, ValueError, RecursionError):
            continue
        if not isinstance(raw, dict):
            continue
        members = []
        if isinstance(raw.get("documents"), list):
            if len(raw["documents"]) > 1_000:
                raise EventError(
                    "semantic control-plane ontology document count exceeds its limit"
                )
            aggregate_ontology_documents += len(raw["documents"])
            if aggregate_ontology_documents > MAX_SEMANTIC_ONTOLOGY_DOCUMENTS:
                raise EventError(
                    "semantic control-plane ontology document count exceeds its aggregate limit"
                )
            members.extend(
                (item.get("path"), ontology_budget, False) for item in raw["documents"]
                if isinstance(item, dict)
            )
        if isinstance(raw.get("index"), dict):
            members.append((raw["index"].get("path"), max_json_bytes, True))
        if len(members) > 1_001:
            raise EventError("semantic control-plane lock member count exceeds its limit")
        try:
            base = lock_path.resolve(strict=False).parent
        except (OSError, RuntimeError, ValueError) as exc:
            raise EventError(f"invalid semantic ontology-lock path: {lock_path}") from exc
        for member, member_limit, is_json_member in members:
            if not isinstance(member, str) or not member or "\\" in member:
                continue
            if (len(member) > 4_096
                    or any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F
                           for char in member)):
                raise EventError("semantic ontology-lock member path contains controls")
            portable = PurePosixPath(member)
            if portable.is_absolute() or any(part in {"", ".", ".."} for part in portable.parts):
                continue
            try:
                candidate = base.joinpath(*portable.parts).resolve(strict=False)
                candidate.relative_to(base)
            except (OSError, RuntimeError, ValueError):
                continue
            try:
                actual_size = candidate.lstat().st_size
            except FileNotFoundError:
                actual_size = 0
            except (OSError, ValueError) as exc:
                raise EventError(f"cannot inspect semantic lock member: {exc}") from exc
            if actual_size > member_limit:
                raise EventError("semantic control-plane member exceeds its byte limit")
            if is_json_member:
                aggregate_json_bytes += actual_size
                if aggregate_json_bytes > 128 * 1024 * 1024:
                    raise EventError("semantic control-plane JSON assets exceed their byte limit")
            else:
                ontology_budget -= actual_size
                if ontology_budget < 0:
                    raise EventError(
                        "semantic control-plane ontology members exceed the configured byte budget"
                    )
            paths.append((candidate, actual_size))
    for declared_attribute, resolved_attribute in (
            ("semantic_mappings_declared_path", "semantic_mappings_path"),
            ("semantic_policies_declared_path", "semantic_policies_path")):
        root = getattr(cfg, declared_attribute, getattr(cfg, resolved_attribute, None))
        if root is None:
            continue
        root = Path(os.path.abspath(root))
        try:
            root.relative_to(Path(os.path.abspath(cfg.base)))
        except ValueError as exc:
            raise EventError("semantic control-plane store escapes the project") from exc
        if _path_has_reparse_component(root):
            raise EventError(
                "semantic control-plane store traverses a link or reparse point"
            )
        try:
            if root.is_dir() and not root.is_symlink():
                children = []
                for child in root.iterdir():
                    children.append(child)
                    if len(children) > 10_000:
                        raise EventError(
                            "semantic control-plane store entry count exceeds its limit"
                        )
                aggregate_store_bytes = 0
                for child in children:
                    try:
                        size = child.lstat().st_size
                    except (OSError, ValueError) as exc:
                        raise EventError(f"cannot inspect semantic store entry: {exc}") from exc
                    aggregate_store_bytes += size
                    if (size > max_store_document_bytes
                            or aggregate_store_bytes > 256 * 1024 * 1024):
                        raise EventError(
                            "semantic control-plane store exceeds its byte limit"
                        )
                    paths.append((child, size))
        except EventError:
            raise
        except OSError:
            # The report/status path owns semantic-integrity diagnostics.  The
            # control plane still snapshots every configured path it can name.
            pass
    unique = {}
    try:
        for path, limit in paths:
            resolved = Path(path).resolve(strict=False)
            key = os.path.normcase(str(resolved))
            previous = unique.get(key)
            if previous is None or limit < previous[1]:
                unique[key] = (Path(path), limit)
        return sorted(
            unique.values(),
            key=lambda item: os.path.normcase(str(item[0].resolve(strict=False))),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise EventError("semantic control-plane contains an invalid path") from exc


def _control_plane_state(cfg) -> dict:
    files = []
    items = [(cfg.config_path, None), (cfg.graph_path, None), *_semantic_control_paths(cfg)]
    for path, limit in items:
        try:
            display = path.resolve().relative_to(cfg.root.resolve()).as_posix()
        except (OSError, RuntimeError, ValueError):
            try:
                display = str(path.resolve(strict=False))
            except (OSError, RuntimeError, ValueError) as exc:
                raise EventError(f"invalid control-plane path: {path!r}") from exc
        snapshot = snapshot_file(path, display, limit=limit)
        if limit is not None and snapshot.get("error") == "SizeLimitExceeded":
            raise EventError(f"semantic control-plane file exceeds its byte limit: {display}")
        files.append(_fingerprint_snapshot(snapshot))
    events, issues = load_events(cfg.events_path)
    event_ids = sorted(event["id"] for event in events)
    return {
        "files": files,
        "event_ledger": {
            "state": "valid" if not issues else "invalid",
            "event_count": len(event_ids),
            "event_ids_sha256": canonical_sha256(event_ids),
            "integrity_issue_count": len(issues),
            "event_ids": event_ids,
        },
    }


def _compact_control_plane(state: dict) -> dict:
    result = {"files": state["files"], "event_ledger": dict(state["event_ledger"])}
    result["event_ledger"].pop("event_ids", None)
    return result


def _control_plane_change(before: dict, after: dict):
    files_changed = canonical_bytes(before["files"]) != canonical_bytes(after["files"])
    before_events = before["event_ledger"]
    after_events = after["event_ledger"]
    appended = sorted(set(after_events["event_ids"]) - set(before_events["event_ids"]))
    append_only = (before_events["state"] == after_events["state"] == "valid"
                   and set(before_events["event_ids"]) <= set(after_events["event_ids"]))
    return files_changed or not append_only, appended


def _lockfile_hashes(project_root: Path):
    items = []
    try:
        children = sorted(project_root.iterdir(), key=lambda p: p.name)
    except OSError:
        return items
    for path in children:
        if path.name not in LOCKFILE_NAMES or not path.is_file() or path.is_symlink():
            continue
        snap = snapshot_file(path, path.name)
        if snap["state"] == "stable":
            items.append({"path": path.name, "sha256": snap["sha256"]})
    return items


def _executable_identity(command: list[str], cwd: Path):
    token = command[0]
    if os.path.isabs(token) or "/" in token or "\\" in token:
        candidate = Path(token) if os.path.isabs(token) else cwd / token
        resolved = candidate.resolve(strict=False)
    else:
        found = shutil.which(token)
        resolved = Path(found).resolve() if found else None
    if resolved is None:
        return {"requested": token, "state": "not_resolved"}
    snap = snapshot_file(resolved, str(resolved))
    result = {"requested": token, "resolved": str(resolved), "state": snap["state"]}
    if snap.get("sha256"):
        result["sha256"] = snap["sha256"]
    return result


def redact_argv(command: list[str], extra_flags: list[str] | None = None):
    flags = {flag.lower() for flag in DEFAULT_SENSITIVE_FLAGS | set(extra_flags or [])}
    result, redact_next = [], False
    for token in command:
        if redact_next:
            result.append("[REDACTED]")
            redact_next = False
            continue
        lower = token.lower()
        matched = next((flag for flag in flags if lower.startswith(flag + "=")), None)
        if matched:
            result.append(token[:len(matched) + 1] + "[REDACTED]")
        else:
            result.append(token)
            if lower in flags:
                redact_next = True
    return result


def _cwd_label(cwd: Path, project_root: Path) -> str:
    return cwd.relative_to(project_root).as_posix() or "."


def _canonical_event_path_parts(
        value: object, label: str, *, allow_project_root: bool = False) -> tuple[str, ...]:
    """Return canonical project-relative POSIX parts for self-contained event checks."""
    if value == "." and allow_project_root:
        return ()
    if (not isinstance(value, str) or not value or len(value) > 4_096
            or "\\" in value
            or any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value)):
        raise EventError(f"{label} must be a canonical project-relative path")
    path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (path.is_absolute() or windows_path.is_absolute() or windows_path.drive
            or value == "." or ".." in path.parts or path.as_posix() != value):
        raise EventError(f"{label} must be a canonical project-relative path")
    return tuple(path.parts)


def _resolve_relative_argv_path(token: str, cwd_parts: tuple[str, ...]):
    """Resolve one non-absolute argv token lexically within the recorded project root."""
    if (not token or token.startswith("-") or len(token) > 32_768
            or any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in token)):
        return None
    posix_path = PurePosixPath(token)
    windows_path = PureWindowsPath(token)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        # An event records only a project-relative cwd. Without an authenticated project-root
        # address, an absolute argv token cannot be revalidated from stored content alone.
        return None
    parts = windows_path.parts if "\\" in token else posix_path.parts
    resolved = list(cwd_parts)
    for part in parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not resolved:
                return None
            resolved.pop()
        else:
            resolved.append(part)
    return tuple(resolved)


def _validate_pipeline_entrypoint_argv(
        argv: list[str], cwd: str, pipeline_snapshot: dict) -> None:
    """Require stored argv to resolve the contract entrypoint from its recorded cwd.

    The check is deliberately lexical and project-relative so the exact same invariant can be
    applied before launch and whenever a content-addressed event is loaded on another host.
    It establishes only that argv names the entrypoint path, not interpreter semantics.
    """
    cwd_parts = _canonical_event_path_parts(
        cwd, "run.started plan cwd", allow_project_root=True,
    )
    entrypoint_id = pipeline_snapshot["entrypoint_code_node_id"]
    entrypoint = next(
        item for item in pipeline_snapshot["roles"]["code"]
        if item["node_id"] == entrypoint_id
    )
    expected_parts = _canonical_event_path_parts(
        entrypoint["path"], "pipeline contract entrypoint code path",
    )
    if any(
            _resolve_relative_argv_path(token, cwd_parts) == expected_parts
            for token in argv):
        return
    raise EventError(
        "pipeline contract entrypoint code path is not present as an exact, "
        "project-relative child argv path resolved from the recorded cwd; module strings, "
        "absolute paths, and inferred imports are not accepted as portable execution evidence"
    )


def _paired_snapshots(items, phase: str):
    return [snapshot_file(path, display) for path, display in items]


def _materialized_intermediate_role_paths(pipeline_snapshot: dict) -> list[str]:
    """Select only intermediates declared as file boundaries by snapshot policy."""
    roles = pipeline_snapshot.get("roles", {})
    raw_items = roles.get("intermediates", [])
    if not isinstance(raw_items, list):
        raise EventError(
            "pipeline contract intermediates role must be a list"
        )
    result: list[str] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise EventError(
                f"pipeline contract intermediate role {index} must be an object"
            )
        materialization = item.get("materialization")
        path = item.get("path")
        if materialization == "unobserved_in_memory_or_ephemeral":
            if path is not None:
                raise EventError(
                    "an unobserved in-memory or ephemeral intermediate cannot declare "
                    f"a path: role {index}"
                )
            continue
        if materialization != "declared_file_boundary":
            raise EventError(
                "pipeline contract intermediate role has an invalid materialization "
                f"token at index {index}: {materialization!r}"
            )
        if not isinstance(path, str) or not path:
            raise EventError(
                "pipeline contract declared file-boundary intermediate role "
                f"{index} needs a non-empty path"
            )
        result.append(path)
    return result


def _materialized_intermediate_paths(project_root: Path, pipeline_snapshot: dict):
    """Resolve path-bearing intermediate roles pinned by a pipeline snapshot."""
    result = []
    for path in _materialized_intermediate_role_paths(pipeline_snapshot):
        resolved, display = resolve_declared_path(project_root, path)
        if display != path:
            raise EventError(
                "pipeline contract materialized intermediate path must be canonical and "
                f"project-relative: {path!r}"
            )
        result.append((resolved, display))
    identities = [os.path.normcase(str(path)) for path, _display in result]
    if len(identities) != len(set(identities)):
        raise EventError(
            "pipeline contract contains duplicate materialized intermediate paths"
        )
    return result


def run_command(
    cfg,
    command: list[str],
    *,
    inputs: list[str],
    outputs: list[str],
    no_inputs: bool = False,
    no_outputs: bool = False,
    cwd: str | None = None,
    name: str | None = None,
    parameters: dict | None = None,
    seeds: dict | None = None,
    allow_external: bool = False,
    scan_writes: bool = True,
    redact_flags: list[str] | None = None,
    pipeline_contract: str | None = None,
    stage_checkpoints: bool = False,
) -> dict:
    """Run a direct child and append content-addressed start/finish receipts.

    Cooperative claimtrace runs that declare the same output are serialized across
    threads and processes. Locks are path-scoped, so disjoint output sets can proceed
    concurrently.
    """
    if not command:
        raise EventError("a command is required after --")
    if bool(inputs) == bool(no_inputs):
        raise EventError("declare one or more --input paths, or pass --no-inputs")
    if bool(outputs) == bool(no_outputs):
        raise EventError("declare one or more --output paths, or pass --no-outputs")
    if os.name == "nt" and Path(command[0]).suffix.lower() in {".bat", ".cmd"}:
        raise EventError("direct .bat/.cmd execution is not supported; name a trusted interpreter explicitly")
    configured_stage_checkpoints = bool(
        getattr(cfg, "require_stage_checkpoints", False)
    )
    if stage_checkpoints and pipeline_contract is None:
        raise EventError("cooperative stage checkpoints require --pipeline-contract")
    require_stage_checkpoints = bool(
        stage_checkpoints
        or (configured_stage_checkpoints and pipeline_contract is not None)
    )

    project_root = cfg.root.resolve()
    run_cwd = Path(cwd).resolve() if cwd else Path.cwd().resolve()
    if not _is_within(run_cwd, project_root):
        raise EventError(f"command cwd is outside the project root: {run_cwd}")
    declared_inputs = [resolve_declared_path(project_root, value, allow_external) for value in inputs]
    declared_outputs = [resolve_declared_path(project_root, value, allow_external) for value in outputs]
    for role, values in (("input", declared_inputs), ("output", declared_outputs)):
        identities = [os.path.normcase(str(path)) for path, _ in values]
        if len(identities) != len(set(identities)):
            raise EventError(f"duplicate declared {role} path")

    pipeline_lock_snapshot_id = None
    declared_intermediates = []
    if pipeline_contract is not None:
        from .pipeline import PipelineError, resolve_pipeline_contract

        try:
            pipeline_for_lock = resolve_pipeline_contract(
                cfg, pipeline_contract,
                declared_inputs=[display for _path, display in declared_inputs],
                declared_outputs=[display for _path, display in declared_outputs],
                parameters=parameters or {},
                seeds=seeds or {},
            )
        except PipelineError as exc:
            raise EventError(f"invalid pipeline contract: {exc}") from exc
        pipeline_lock_snapshot_id = pipeline_for_lock["id"]
        declared_intermediates = _materialized_intermediate_paths(
            project_root, pipeline_for_lock,
        )
        input_identities = {
            os.path.normcase(str(path)) for path, _display in declared_inputs
        }
        output_identities = {
            os.path.normcase(str(path)) for path, _display in declared_outputs
        }
        intermediate_identities = {
            os.path.normcase(str(path)) for path, _display in declared_intermediates
        }
        if intermediate_identities & input_identities:
            raise EventError(
                "pipeline materialized intermediates overlap declared input paths"
            )
        if intermediate_identities & output_identities:
            raise EventError(
                "pipeline materialized intermediates overlap terminal output paths"
            )

    with _output_locks([*declared_outputs, *declared_intermediates]):
        return _run_command_locked(
            cfg,
            command,
            project_root=project_root,
            run_cwd=run_cwd,
            declared_inputs=declared_inputs,
            declared_outputs=declared_outputs,
            declared_intermediates=declared_intermediates,
            name=name,
            parameters=parameters,
            seeds=seeds,
            scan_writes=scan_writes,
            redact_flags=redact_flags,
            pipeline_contract=pipeline_contract,
            pipeline_lock_snapshot_id=pipeline_lock_snapshot_id,
            stage_checkpoints=require_stage_checkpoints,
        )


def _run_command_locked(
    cfg,
    command: list[str],
    *,
    project_root: Path,
    run_cwd: Path,
    declared_inputs,
    declared_outputs,
    declared_intermediates,
    name: str | None,
    parameters: dict | None,
    seeds: dict | None,
    scan_writes: bool,
    redact_flags: list[str] | None,
    pipeline_contract: str | None,
    pipeline_lock_snapshot_id: str | None,
    stage_checkpoints: bool,
) -> dict:
    """Capture and finalize one run while all of its declared output locks are held."""

    parameter_values = parameters or {}
    seed_values = seeds or {}
    pipeline_before = None
    if pipeline_contract is not None:
        from .pipeline import PipelineError, resolve_pipeline_contract

        try:
            pipeline_before = resolve_pipeline_contract(
                cfg, pipeline_contract,
                declared_inputs=[display for _path, display in declared_inputs],
                declared_outputs=[display for _path, display in declared_outputs],
                parameters=parameter_values,
                seeds=seed_values,
            )
        except PipelineError as exc:
            raise EventError(f"invalid pipeline contract: {exc}") from exc
        if pipeline_before["id"] != pipeline_lock_snapshot_id:
            raise EventError(
                "pipeline contract changed while acquiring declared write locks"
            )
        current_intermediates = _materialized_intermediate_paths(
            project_root, pipeline_before,
        )
        if current_intermediates != declared_intermediates:
            raise EventError(
                "pipeline materialized intermediate roles changed while acquiring "
                "declared write locks"
            )
    input_before = _paired_snapshots(declared_inputs, "before")
    output_before = _paired_snapshots(declared_outputs, "before")
    intermediate_before = _paired_snapshots(declared_intermediates, "before")
    control_before_full = _control_plane_state(cfg)
    control_before = _compact_control_plane(control_before_full)
    precondition_errors = [
        f"input {snap['path']} is {snap['state']}"
        for snap in input_before if snap["state"] != "stable"
    ]
    if any(snap["state"] not in {"stable", "missing"} for snap in output_before):
        precondition_errors.extend(
            f"output {snap['path']} is {snap['state']}"
            for snap in output_before if snap["state"] not in {"stable", "missing"}
        )
    if any(snap["state"] not in {"stable", "missing"} for snap in intermediate_before):
        precondition_errors.extend(
            f"materialized intermediate {snap['path']} is {snap['state']}"
            for snap in intermediate_before
            if snap["state"] not in {"stable", "missing"}
        )
    if control_before["event_ledger"]["state"] != "valid":
        precondition_errors.append("event ledger has integrity errors before launch")

    redacted = redact_argv(command, redact_flags)
    if pipeline_before is not None:
        _validate_pipeline_entrypoint_argv(
            redacted, _cwd_label(run_cwd, project_root), pipeline_before,
        )
    environment = {
        "capture": "partial_no_environment_variables",
        "controller_python": platform.python_version(),
        "controller_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "executable": _executable_identity(command, run_cwd),
        "root_lockfiles": _lockfile_hashes(project_root),
    }
    git_before = capture_git(project_root)
    plan_basis = {
        "argv": redacted,
        "argv_capture": "redacted_best_effort",
        "cwd": _cwd_label(run_cwd, project_root),
        "declared_inputs": input_before,
        "declared_outputs": [snap["path"] for snap in output_before],
        "parameters": parameter_values,
        "seeds": seed_values,
        "environment": environment,
        "git": git_before,
        "control_plane_before": control_before,
    }
    event_schema = EVENT_SCHEMA
    if pipeline_before is not None:
        event_schema = CONTRACT_EVENT_SCHEMA
        plan_basis["pipeline_contract"] = pipeline_before
        plan_basis["declared_intermediates"] = [
            snap["path"] for snap in intermediate_before
        ]
        plan_basis["computation_id"] = (
            "computation:sha256:" + canonical_sha256(_fingerprint_computation(plan_basis))
        )
    run_id = f"run:{uuid.uuid4()}"
    stage_capture = None
    if stage_checkpoints:
        from .pipeline import (
            PipelineError,
            prepare_stage_trace,
        )

        event_schema = STAGE_CONTRACT_EVENT_SCHEMA
        trace_name = run_id.replace(":", "-") + ".stages.jsonl"
        try:
            stage_capture = prepare_stage_trace(
                _marker_directory(project_root) / trace_name,
                pipeline_before,
                project_root,
            )
        except PipelineError as exc:
            raise EventError(f"stage checkpoint capture could not start: {exc}") from exc
        plan_basis["stage_trace_plan"] = stage_capture["plan"]
    plan_id = f"recipe:sha256:{canonical_sha256(_fingerprint_plan(plan_basis))}"
    start_payload = {
        "name": name,
        "plan_id": plan_id,
        "plan": plan_basis,
        "capture_scope": {
            "reads": "declared_only_not_observed",
            "writes": "declared_snapshots_plus_unattributed_project_window" if scan_writes else "declared_snapshots_only",
            "processes": "direct_child_only",
        },
    }
    if stage_capture is not None:
        start_payload["stage_trace_binding"] = {
            "nonce_sha256": stage_capture["nonce_sha256"],
            "transport": "controller_created_private_file",
            "reporter_scope": "direct_child_process_id_checked",
        }
    start_event = make_event(
        "run.started", run_id, start_payload, schema_version=event_schema,
    )
    stage_trace_cleanup_error = None
    try:
        marker = create_active_marker(project_root, run_id, start_event)
        scan_before = _scan_project(project_root, cfg.events_path) if scan_writes else {}
        started_ns = time.monotonic_ns()
        return_code = None
        launch_error = None
        interrupted = False
        child_launch_attempted = False
        child_cleanup_errors = []
        direct_child_pid = None
        from .pipeline import scrub_stage_trace_environment

        child_environment = scrub_stage_trace_environment()
        if stage_capture is not None:
            from .pipeline import stage_trace_child_environment

            child_environment.update(stage_trace_child_environment(stage_capture))
        if not precondition_errors:
            process = None
            try:
                child_launch_attempted = True
                process = subprocess.Popen(
                    command, cwd=str(run_cwd), shell=False,
                    env=child_environment,
                )
            except OSError as exc:
                launch_error = {"type": type(exc).__name__, "detail": str(exc)}
            if process is not None:
                direct_child_pid = process.pid
                try:
                    return_code = process.wait()
                except KeyboardInterrupt:
                    interrupted = True
                    child_cleanup_errors.extend(_kill_and_reap_child(process))
                except OSError as exc:
                    launch_error = {
                        "type": type(exc).__name__,
                        "detail": "direct child wait failed after launch: " + str(exc),
                    }
                    child_cleanup_errors.extend(_kill_and_reap_child(process))
                except BaseException:
                    _kill_and_reap_child(process)
                    raise
        duration_ns = time.monotonic_ns() - started_ns

        input_after = _paired_snapshots(declared_inputs, "after")
        output_after = _paired_snapshots(declared_outputs, "after")
        intermediate_after = _paired_snapshots(declared_intermediates, "after")
        scan_after = _scan_project(project_root, cfg.events_path) if scan_writes else {}
        control_after_full = _control_plane_state(cfg)
        control_after = _compact_control_plane(control_after_full)
        control_changed, concurrent_event_appends = _control_plane_change(
            control_before_full, control_after_full)
        input_transitions = []
        for before, after in zip(input_before, input_after):
            input_transitions.append({
                "path": before["path"], "transition": _transition(before, after),
                "before": before, "after": after,
            })
        output_transitions = []
        for before, after in zip(output_before, output_after):
            transition = _transition(before, after)
            output_transitions.append({
                "path": before["path"], "transition": transition,
                "produced": transition in {"created", "content_changed"},
                "before": before, "after": after,
            })
        intermediate_transitions = []
        for before, after in zip(intermediate_before, intermediate_after):
            transition = _transition(before, after)
            intermediate_transitions.append({
                "path": before["path"], "transition": transition,
                "produced": transition in {"created", "content_changed"},
                "before": before, "after": after,
            })

        stage_trace = None
        if stage_capture is not None:
            from .pipeline import finalize_stage_trace

            stage_trace = finalize_stage_trace(
                stage_capture,
                pipeline_before,
                launched=direct_child_pid is not None,
                expected_reporter_pid=direct_child_pid,
            )
    finally:
        if stage_capture is not None:
            try:
                Path(stage_capture["path"]).unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                stage_trace_cleanup_error = (
                    "controller private stage-checkpoint channel could not be removed: "
                    f"{type(exc).__name__}: {exc}"
                )

    contract_errors = list(precondition_errors)
    contract_errors.extend(child_cleanup_errors)
    contract_errors.extend(
        f"input {item['path']} changed during the run; consumed version is ambiguous"
        for item in input_transitions if item["transition"] != "unchanged"
    )
    contract_errors.extend(
        f"output {item['path']} ended as {item['transition']}"
        for item in output_transitions if item["transition"] in {"missing", "deleted", "unstable"}
    )
    contract_errors.extend(
        f"materialized intermediate {item['path']} ended as {item['transition']}"
        for item in intermediate_transitions
        if item["transition"] in {"missing", "deleted", "unstable"}
    )
    if (stage_trace is not None
            and stage_trace["state"] != "cooperative_report_complete"
            and child_launch_attempted):
        contract_errors.append(
            "cooperative stage checkpoint trace ended as " + stage_trace["state"]
        )
    if stage_trace_cleanup_error is not None:
        contract_errors.append(stage_trace_cleanup_error)
    if pipeline_before is not None:
        from .pipeline import PipelineError, resolve_pipeline_contract

        try:
            pipeline_after = resolve_pipeline_contract(
                cfg, pipeline_contract,
                declared_inputs=[display for _path, display in declared_inputs],
                declared_outputs=[display for _path, display in declared_outputs],
                parameters=parameter_values,
                seeds=seed_values,
            )
            if pipeline_after["id"] != pipeline_before["id"]:
                contract_errors.append(
                    "pipeline contract, method specification, or code anchors changed during the run"
                )
        except PipelineError as exc:
            contract_errors.append(f"pipeline contract became invalid during the run: {exc}")
    if control_changed:
        contract_errors.append(
            "claimtrace config, graph, or event ledger changed during child execution; "
            "configured semantic assets and stores are part of this control plane"
        )
    if precondition_errors:
        outcome = "capture_precondition_failed"
        cli_exit = 3
    elif interrupted:
        outcome = "interrupted"
        cli_exit = 130
    elif launch_error:
        outcome = "launch_error"
        cli_exit = 127
    elif return_code != 0:
        outcome = "failed"
        cli_exit = return_code if return_code is not None else 1
    elif contract_errors:
        outcome = "contract_failed"
        cli_exit = 3
    else:
        outcome = "succeeded"
        cli_exit = 0

    declared_write_paths = {
        display for _path, display in [*declared_outputs, *declared_intermediates]
    }
    window_deltas = (
        _window_deltas(scan_before, scan_after, declared_write_paths)
        if scan_writes else []
    )
    git_after = capture_git(project_root)
    result_basis = {
        "plan_id": plan_id,
        "outcome": outcome,
        "direct_child_returncode": return_code,
        "input_transitions": [_fingerprint_transition(item) for item in input_transitions],
        "output_transitions": [_fingerprint_transition(item) for item in output_transitions],
        "contract_errors": contract_errors,
    }
    if event_schema in CURRENT_CONTRACT_EVENT_SCHEMAS:
        result_basis["intermediate_transitions"] = [
            _fingerprint_transition(item) for item in intermediate_transitions
        ]
    if event_schema == STAGE_CONTRACT_EVENT_SCHEMA:
        result_basis["stage_trace"] = _fingerprint_stage_trace(stage_trace)
    result_id = f"result:sha256:{canonical_sha256(result_basis)}"
    finish_payload = {
        "start_event_id": start_event["id"],
        "plan_id": plan_id,
        "result_id": result_id,
        "outcome": outcome,
        "direct_child_returncode": return_code,
        "duration_ns": duration_ns,
        "launch_error": launch_error,
        "contract_errors": contract_errors,
        "input_transitions": input_transitions,
        "output_transitions": output_transitions,
        "window_deltas": window_deltas,
        "git_after": git_after,
        "control_plane": {
            "before": control_before,
            "after": control_after,
            "forbidden_change_during_child": control_changed,
            "concurrent_append_only_event_count": len(concurrent_event_appends),
        },
        "receipt_integrity": "finalized",
        "lineage_coverage": {
            "overall": "partial",
            "reads": "declared_only_not_observed",
            "declared_inputs": "pre_and_post_hashed",
            "declared_outputs": "pre_and_post_hashed",
            "write_attribution": "unattributed_pre_post_delta" if scan_writes else "not_scanned",
            "processes": "direct_child_only",
            "environment": "partial",
            "git": "best_effort",
        },
    }
    if event_schema in CURRENT_CONTRACT_EVENT_SCHEMAS:
        finish_payload["intermediate_transitions"] = intermediate_transitions
        finish_payload["lineage_coverage"]["declared_intermediates"] = (
            "pre_and_post_hashed"
        )
    if event_schema == STAGE_CONTRACT_EVENT_SCHEMA:
        finish_payload["stage_trace"] = stage_trace
        finish_payload["lineage_coverage"]["cooperative_stage_trace"] = (
            "cooperative_child_self_report"
        )
    finish_event = make_event(
        "run.finished", run_id, finish_payload, schema_version=event_schema,
    )
    try:
        append_event(cfg.events_path, start_event)
        append_event(cfg.events_path, finish_event)
        remove_active_marker(marker)
    except Exception:
        # The out-of-worktree marker intentionally remains as recoverable evidence.
        raise
    return {
        "run_id": run_id,
        "plan_id": plan_id,
        "computation_id": plan_basis.get("computation_id"),
        "pipeline_contract_id": (
            pipeline_before.get("id") if pipeline_before is not None else None
        ),
        "result_id": result_id,
        "outcome": outcome,
        "direct_child_returncode": return_code,
        "exit_code": cli_exit,
        "output_transitions": output_transitions,
        "intermediate_transitions": intermediate_transitions,
        "stage_trace": stage_trace,
        "contract_errors": contract_errors,
        "lineage_coverage": finish_payload["lineage_coverage"],
    }
