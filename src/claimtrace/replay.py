"""Fresh-workspace replay certificates for contract-bound computations.

Replay is deliberately a boundary test.  It can establish that declared terminal
output and materialized-intermediate bytes, captured streams, and visible
fresh-workspace deltas repeat under the current, partially recorded host
environment.  The child is not sandboxed: it can still access network services or
paths outside the fresh workspace.  Replay cannot observe in-memory stages, prove
code/method semantic equivalence, or establish universal determinism.  An argv
override used to fill redacted values is intentionally uncommitted and therefore
cannot become current or review-ready source-command evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .events import (
    CONTRACT_EVENT_SCHEMA,
    CONTRACT_EVENT_SCHEMAS,
    STAGE_CONTRACT_EVENT_SCHEMA,
    EVENT_ID_RE,
    FILE_STATES,
    FILE_TRANSITIONS,
    RFC3339_UTC_RE,
    RUN_ID_RE,
    EventError,
    _ensure_directory_durable,
    _event_lock,
    _executable_identity,
    _fingerprint_snapshot,
    _fsync_directory,
    _kill_and_reap_child,
    _lockfile_hashes,
    _path_has_reparse_component,
    _stable_file_descriptor,
    _stable_bounded_bytes,
    _window_deltas,
    load_events,
    materialize_runs,
    snapshot_file,
    validate_event,
)
from .config import strict_json_loads
from .pipeline import (
    MAX_STAGE_TRACE_BYTES,
    PipelineError,
    canonical_bytes,
    canonical_sha256,
    finalize_stage_trace,
    prepare_stage_trace,
    resolve_pipeline_contract,
    stage_trace_child_environment,
    validate_stage_trace,
    validate_stage_trace_against_snapshot,
    validate_pipeline_snapshot,
)


LEGACY_REPLAY_SCHEMA = "claimtrace.replay-certificate/1"
REPLAY_SCHEMA = "claimtrace.replay-certificate/2"
STAGE_REPLAY_SCHEMA = "claimtrace.replay-certificate/3"
SUPPORTED_REPLAY_SCHEMAS = frozenset({
    LEGACY_REPLAY_SCHEMA, REPLAY_SCHEMA, STAGE_REPLAY_SCHEMA,
})
LEGACY_WORKSPACE_WRITE_COVERAGE = (
    "all_workspace_paths_pre_post_scan_unattributed_no_follow"
)
WORKSPACE_FILE_WRITE_COVERAGE = (
    "all_workspace_file_paths_pre_post_scan_unattributed_no_follow"
)
REPLAY_ID_RE = re.compile(r"^replay:sha256:[0-9a-f]{64}$")
COMPUTATION_ID_RE = re.compile(r"^computation:sha256:[0-9a-f]{64}$")
CONTRACT_ID_RE = re.compile(r"^pipeline-contract:sha256:[0-9a-f]{64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_CERTIFICATE_BYTES = 8 * 1024 * 1024
MIN_REPLAY_ATTEMPTS = 2
MAX_CERTIFICATES = 100_000
MAX_REPLAY_ATTEMPTS = 10
MAX_REPLAY_PATHS = 20_000
MAX_REPLAY_SCAN_ENTRIES = 100_000
MAX_REPLAY_STORE_BYTES = 8 * 1024 * 1024 * 1024
REDACTION_MARKER = "[REDACTED]"

if MAX_STAGE_TRACE_BYTES * (MIN_REPLAY_ATTEMPTS + 1) * 2 > MAX_CERTIFICATE_BYTES:
    raise RuntimeError(
        "stage-trace byte limit exceeds the minimum replay-certificate safety budget"
    )


class ReplayError(RuntimeError):
    """Replay is unavailable, unsafe, stale, or its certificate store is invalid."""


def _portable_project_path(value: object, label: str) -> PurePosixPath:
    if (not isinstance(value, str) or not value or len(value) > 4_096
            or "\\" in value or any(ord(char) < 0x20 for char in value)):
        raise ReplayError(f"{label} must be a bounded POSIX project-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReplayError(f"{label} must be a normalized project-relative path")
    return path


def _project_file(root: Path, portable: PurePosixPath, label: str) -> Path:
    candidate = root.joinpath(*portable.parts)
    if _path_has_reparse_component(candidate):
        raise ReplayError(f"{label} traverses a symlink, junction, or reparse point")
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise ReplayError(f"{label} escapes the project root") from exc
    return candidate


def _source_run(cfg, run_id: str) -> tuple[dict, dict]:
    events, integrity_issues = load_events(cfg.events_path)
    if integrity_issues:
        raise ReplayError("event ledger has integrity errors; replay is blocked")
    runs, run_issues = materialize_runs(events)
    if run_issues:
        raise ReplayError("event ledger has run-link errors; replay is blocked")
    matching = [item for item in runs if item["run_id"] == run_id]
    if len(matching) != 1:
        raise ReplayError(f"contract-bound source run was not found: {run_id}")
    start, finish = matching[0]["start"], matching[0]["finish"]
    if start is None or finish is None:
        raise ReplayError("source run does not have a complete start/finish receipt")
    if (start.get("schema_version") not in CONTRACT_EVENT_SCHEMAS
            or finish.get("schema_version") not in CONTRACT_EVENT_SCHEMAS
            or start.get("schema_version") != finish.get("schema_version")):
        raise ReplayError("source run is legacy/uncontracted and cannot use strict replay")
    if finish["payload"].get("outcome") != "succeeded":
        raise ReplayError("source run did not succeed and is not replay-eligible")
    return start, finish


def _matches_redacted(stored: str, supplied: str) -> bool:
    if REDACTION_MARKER not in stored:
        return stored == supplied
    prefix, suffix = stored.split(REDACTION_MARKER, 1)
    return supplied.startswith(prefix) and supplied.endswith(suffix) and len(supplied) >= len(prefix) + len(suffix)


def _resolve_command(stored: list[str], override: list[str] | None) -> tuple[list[str], bool]:
    if (not isinstance(stored, list) or not stored
            or not all(isinstance(item, str) and item for item in stored)):
        raise ReplayError("source run argv is invalid")
    redacted = any(REDACTION_MARKER in item for item in stored)
    if redacted and override is None:
        raise ReplayError(
            "source argv contains redacted values; provide a shape-matching command after --"
        )
    if not redacted and override is not None:
        raise ReplayError(
            "a replay command override is allowed only for a redacted source argv"
        )
    command = list(override if override is not None else stored)
    if not command or len(command) != len(stored):
        raise ReplayError("replay command does not match the source argv shape")
    if not all(isinstance(item, str) and item for item in command):
        raise ReplayError("replay command tokens must be non-empty strings")
    if not all(_matches_redacted(expected, actual) for expected, actual in zip(stored, command)):
        raise ReplayError("replay command differs from the source redacted argv")
    return command, override is not None


def _rewrite_command(command: list[str], source_root: Path, temp_root: Path) -> list[str]:
    rewritten = []
    for index, token in enumerate(command):
        if not Path(token).is_absolute():
            rewritten.append(token)
            continue
        resolved = Path(token).resolve(strict=False)
        try:
            relative = resolved.relative_to(source_root)
        except ValueError:
            if index != 0:
                raise ReplayError(
                    "non-executable absolute argv paths outside the project are not portable"
                )
            rewritten.append(token)
        else:
            rewritten.append(str(temp_root / relative))
    return rewritten


def _hash_stream(handle) -> dict:
    handle.flush()
    handle.seek(0)
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = handle.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        digest.update(chunk)
    return {"sha256": digest.hexdigest(), "size": size, "content": "not_stored"}


def _copy_declared_inputs(source_root: Path, temp_root: Path,
                          input_snapshots: list[dict]) -> None:
    for item in input_snapshots:
        portable = _portable_project_path(item.get("path"), "declared input path")
        source = _project_file(source_root, portable, "declared input")
        destination = temp_root.joinpath(*portable.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with _stable_file_descriptor(source) as (descriptor, opened), open(
                    destination, "xb") as writer:
                copied_size = 0
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    copied_size += len(chunk)
                    writer.write(chunk)
                if copied_size != opened.st_size:
                    raise ReplayError("copied byte count differs from the opened source file")
                os.chmod(destination, stat.S_IMODE(opened.st_mode))
        except Exception as exc:
            raise ReplayError(f"cannot copy declared input {portable.as_posix()}: {exc}") from exc
        copied = snapshot_file(destination, portable.as_posix())
        if _fingerprint_snapshot(copied) != _fingerprint_snapshot(item):
            raise ReplayError(f"declared input changed while copying: {portable.as_posix()}")


def _scan_workspace(root: Path) -> dict[str, dict]:
    """Hash every fresh-workspace file without following links or ignoring build paths."""
    result = {}
    entries = 0
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept = []
        for name in sorted(dirs):
            entries += 1
            if entries > MAX_REPLAY_SCAN_ENTRIES:
                raise ReplayError("fresh replay workspace exceeds its scan-entry limit")
            candidate = current_path / name
            if _path_has_reparse_component(candidate):
                rel = candidate.relative_to(root).as_posix()
                result[rel] = {"path": rel, "state": "unsupported"}
            else:
                kept.append(name)
        dirs[:] = kept
        for name in sorted(files):
            entries += 1
            if entries > MAX_REPLAY_SCAN_ENTRIES:
                raise ReplayError("fresh replay workspace exceeds its scan-entry limit")
            path = current_path / name
            rel = path.relative_to(root).as_posix()
            result[rel] = snapshot_file(path, rel)
    return result


def _attempt(temp_parent: Path, index: int, command: list[str], cwd_label: str,
             input_snapshots: list[dict], output_paths: list[str],
             intermediate_paths: list[str],
             source_root: Path, timeout_seconds: float | None,
             stage_pipeline_snapshot: dict | None = None) -> dict:
    workspace = temp_parent / f"attempt-{index}"
    workspace.mkdir()
    _copy_declared_inputs(source_root, workspace, input_snapshots)
    cwd_portable = None if cwd_label == "." else _portable_project_path(cwd_label, "run cwd")
    run_cwd = workspace if cwd_portable is None else workspace.joinpath(*cwd_portable.parts)
    run_cwd.mkdir(parents=True, exist_ok=True)
    portable_outputs = [_portable_project_path(value, "declared output path") for value in output_paths]
    portable_intermediates = [
        _portable_project_path(value, "materialized intermediate path")
        for value in intermediate_paths
    ]
    declared_paths = [*portable_outputs, *portable_intermediates]
    for declared in declared_paths:
        path = workspace.joinpath(*declared.parts)
        if path.exists() or path.is_symlink():
            raise ReplayError(
                f"declared output or materialized intermediate overlaps a copied input: "
                f"{declared.as_posix()}"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
    before = _scan_workspace(workspace)
    replay_command = _rewrite_command(command, source_root, workspace)
    stage_capture = None
    from .pipeline import scrub_stage_trace_environment

    child_environment = scrub_stage_trace_environment()
    if stage_pipeline_snapshot is not None:
        try:
            stage_capture = prepare_stage_trace(
                temp_parent / f"attempt-{index}.stages.jsonl",
                stage_pipeline_snapshot,
                workspace,
            )
        except PipelineError as exc:
            raise ReplayError(f"replay stage checkpoint capture could not start: {exc}") from exc
        child_environment.update(stage_trace_child_environment(stage_capture))
    started = time.monotonic_ns()
    returncode = None
    launch_error = None
    timed_out = False
    stage_trace = None
    direct_child_pid = None
    stage_trace_cleanup_error = None
    try:
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = None
            try:
                process = subprocess.Popen(
                    replay_command, cwd=str(run_cwd), shell=False,
                    stdout=stdout, stderr=stderr, env=child_environment,
                )
            except OSError as exc:
                launch_error = {"type": type(exc).__name__, "detail": str(exc)[:2_000]}
            if process is not None:
                direct_child_pid = process.pid
                try:
                    returncode = process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    cleanup_issues = _kill_and_reap_child(process)
                    if cleanup_issues:
                        raise ReplayError("; ".join(cleanup_issues))
                except KeyboardInterrupt:
                    _kill_and_reap_child(process)
                    raise
                except OSError as exc:
                    cleanup_issues = _kill_and_reap_child(process)
                    detail = "direct child wait failed after launch: " + str(exc)
                    if cleanup_issues:
                        detail += "; " + "; ".join(cleanup_issues)
                    launch_error = {
                        "type": type(exc).__name__, "detail": detail[:2_000],
                    }
                except BaseException:
                    _kill_and_reap_child(process)
                    raise
            duration_ns = time.monotonic_ns() - started
            stdout_record = _hash_stream(stdout)
            stderr_record = _hash_stream(stderr)
        if stage_capture is not None:
            stage_trace = finalize_stage_trace(
                stage_capture,
                stage_pipeline_snapshot,
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
                    "replay private stage-checkpoint channel could not be removed: "
                    f"{type(exc).__name__}: {exc}"
                )
    if stage_trace_cleanup_error is not None:
        raise ReplayError(stage_trace_cleanup_error)
    outputs = [
        snapshot_file(workspace.joinpath(*path.parts), path.as_posix())
        for path in portable_outputs
    ]
    intermediates = [
        snapshot_file(workspace.joinpath(*path.parts), path.as_posix())
        for path in portable_intermediates
    ]
    after = _scan_workspace(workspace)
    deltas = _window_deltas(
        before, after, {path.as_posix() for path in declared_paths}
    )
    undeclared = [item for item in deltas if not item["declared_output"]]
    outcome = _attempt_outcome(returncode, launch_error, [*outputs, *intermediates])
    if timed_out != (outcome == "timed_out"):
        raise ReplayError("replay attempt timeout state is internally inconsistent")
    result = {
        "index": index,
        "outcome": outcome,
        "direct_child_returncode": returncode,
        "duration_ns": duration_ns,
        "launch_error": launch_error,
        "stdout": stdout_record,
        "stderr": stderr_record,
        "outputs": [_fingerprint_snapshot(item) for item in outputs],
        "materialized_intermediates": [
            _fingerprint_snapshot(item) for item in intermediates
        ],
        "undeclared_writes": undeclared,
    }
    if stage_trace is not None:
        result["stage_trace"] = stage_trace
    return result


def _source_outputs(finish: dict, expected_paths: list[str]) -> list[dict]:
    transitions = finish["payload"].get("output_transitions", [])
    if [item.get("path") for item in transitions] != expected_paths:
        raise ReplayError("source finish output roles differ from the source plan")
    snapshots = []
    for item in transitions:
        after = item.get("after", {})
        if after.get("state") != "stable":
            raise ReplayError(f"source receipt output is not stable: {item.get('path')}")
        snapshots.append(_fingerprint_snapshot(after))
    return snapshots


def _source_intermediates(finish: dict, expected_paths: list[str]) -> list[dict]:
    transitions = finish["payload"].get("intermediate_transitions", [])
    if [item.get("path") for item in transitions] != expected_paths:
        raise ReplayError(
            "source finish materialized-intermediate roles differ from the source plan"
        )
    snapshots = []
    for item in transitions:
        after = item.get("after", {})
        if after.get("state") != "stable":
            raise ReplayError(
                f"source receipt materialized intermediate is not stable: "
                f"{item.get('path')}"
            )
        snapshots.append(_fingerprint_snapshot(after))
    return snapshots


def _same_outputs(left: list[dict], right: list[dict]) -> bool:
    def basis(items):
        return [
            {key: item.get(key) for key in ("path", "state", "sha256", "size")}
            for item in items
        ]
    return basis(left) == basis(right)


def _same_attempt_stream(left: dict, right: dict, name: str) -> bool:
    return {
        key: left[name].get(key) for key in ("sha256", "size")
    } == {
        key: right[name].get(key) for key in ("sha256", "size")
    }


def _same_undeclared_writes(left: dict, right: dict) -> bool:
    return left["undeclared_writes"] == right["undeclared_writes"]


def _same_stage_trace(left: dict, right: dict) -> bool:
    """Compare the deterministic cooperative projection, excluding nonce/raw bytes."""
    return {
        "state": left.get("state"),
        "required_stage_ids": left.get("required_stage_ids"),
        "checkpoints": left.get("checkpoints"),
    } == {
        "state": right.get("state"),
        "required_stage_ids": right.get("required_stage_ids"),
        "checkpoints": right.get("checkpoints"),
    }


def _attempt_outcome(returncode: int | None, launch_error: dict | None,
                     snapshots: list[dict]) -> str:
    """Derive the only outcome consistent with directly recorded attempt facts."""
    if launch_error is not None:
        if returncode is not None:
            raise ReplayError(
                "replay attempt cannot record both a launch error and a return code"
            )
        return "launch_error"
    if returncode is None:
        return "timed_out"
    if returncode != 0:
        return "failed"
    if any(item.get("state") != "stable" for item in snapshots):
        return "output_missing_or_unstable"
    return "succeeded"


def replay_run(cfg, run_id: str, *, attempts: int | None = None,
               command_override: list[str] | None = None,
               timeout_seconds: float | None = None) -> dict:
    """Replay one successful contract-bound run in fresh workspaces.

    Claimtrace prepares declared terminal-output and materialized-intermediate paths
    only inside those workspaces.  The direct child is not an OS sandbox and may
    still write an absolute or otherwise external path; such writes are outside
    replay observation and prevention.
    """
    count = cfg.replay_attempts if attempts is None else attempts
    if (isinstance(count, bool) or not isinstance(count, int)
            or not MIN_REPLAY_ATTEMPTS <= count <= MAX_REPLAY_ATTEMPTS):
        raise ReplayError(
            "replay attempts must be an integer from "
            f"{MIN_REPLAY_ATTEMPTS} to {MAX_REPLAY_ATTEMPTS}"
        )
    if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0 or timeout_seconds > 7 * 24 * 60 * 60):
        raise ReplayError("replay timeout must be positive and no more than seven days")

    start, finish = _source_run(cfg, run_id)
    plan = start["payload"]["plan"]
    stored_command = plan.get("argv")
    command, override_used = _resolve_command(stored_command, command_override)
    input_snapshots = plan.get("declared_inputs", [])
    output_paths = plan.get("declared_outputs", [])
    intermediate_paths = plan.get("declared_intermediates", [])
    if (len(input_snapshots) > MAX_REPLAY_PATHS
            or len(output_paths) + len(intermediate_paths) > MAX_REPLAY_PATHS):
        raise ReplayError("source run exceeds replay path limits")
    for item in input_snapshots:
        portable = _portable_project_path(item.get("path"), "declared input path")
        current = snapshot_file(
            _project_file(cfg.root.resolve(), portable, "declared input"), portable.as_posix(),
        )
        if _fingerprint_snapshot(current) != _fingerprint_snapshot(item):
            raise ReplayError(f"source input is stale: {portable.as_posix()}")
    for path in output_paths:
        _portable_project_path(path, "declared output path")
    for path in intermediate_paths:
        _portable_project_path(path, "materialized intermediate path")

    contract = plan.get("pipeline_contract")
    try:
        validate_pipeline_snapshot(contract)
        current_contract = resolve_pipeline_contract(
            cfg, contract["source"]["path"],
            declared_inputs=[item["path"] for item in input_snapshots],
            declared_outputs=list(output_paths),
            parameters=dict(plan.get("parameters", {})),
            seeds=dict(plan.get("seeds", {})),
            snapshot_schema=contract["schema_version"],
        )
    except (KeyError, PipelineError) as exc:
        raise ReplayError(f"source pipeline contract is stale or invalid: {exc}") from exc
    if current_contract["id"] != contract["id"]:
        raise ReplayError("source pipeline contract, code, or method bytes have drifted")

    cwd_label = plan.get("cwd")
    if cwd_label != ".":
        _portable_project_path(cwd_label, "run cwd")
    source_cwd = cfg.root.resolve() if cwd_label == "." else cfg.root.joinpath(
        *_portable_project_path(cwd_label, "run cwd").parts
    )
    recorded_environment = plan.get("environment", {})
    current_executable = _executable_identity(command, source_cwd)
    recorded_executable = recorded_environment.get("executable")
    if not isinstance(recorded_executable, dict):
        raise ReplayError("source executable identity is missing")
    for key in ("state", "resolved", "sha256"):
        if recorded_executable.get(key) != current_executable.get(key):
            raise ReplayError("source executable identity no longer matches the replay command")
    if recorded_environment.get("root_lockfiles") != _lockfile_hashes(cfg.root.resolve()):
        raise ReplayError("source root environment lockfiles have drifted")

    source_outputs = _source_outputs(finish, list(output_paths))
    source_intermediates = _source_intermediates(finish, list(intermediate_paths))
    stage_traced_source = (
        start.get("schema_version") == STAGE_CONTRACT_EVENT_SCHEMA
        and finish.get("schema_version") == STAGE_CONTRACT_EVENT_SCHEMA
    )
    source_stage_trace = None
    if stage_traced_source:
        source_stage_trace = finish["payload"].get("stage_trace")
        try:
            validate_stage_trace_against_snapshot(source_stage_trace, contract)
        except (PipelineError, TypeError) as exc:
            raise ReplayError(f"source cooperative stage trace is invalid: {exc}") from exc
        if source_stage_trace["state"] != "cooperative_report_complete":
            raise ReplayError("source cooperative stage trace is not complete")
    with tempfile.TemporaryDirectory(prefix="claimtrace-replay-") as parent:
        records = [
            _attempt(
                Path(parent), index, command, cwd_label, input_snapshots,
                list(output_paths), list(intermediate_paths), cfg.root.resolve(),
                timeout_seconds,
                contract if stage_traced_source else None,
            )
            for index in range(1, count + 1)
        ]
    all_succeeded = all(item["outcome"] == "succeeded" for item in records)
    declared_outputs_equal = all_succeeded and all(
        _same_outputs(records[0]["outputs"], item["outputs"]) for item in records[1:]
    )
    matches_source = all_succeeded and all(
        _same_outputs(source_outputs, item["outputs"]) for item in records
    )
    intermediates_equal = all_succeeded and all(
        _same_outputs(
            records[0]["materialized_intermediates"],
            item["materialized_intermediates"],
        )
        for item in records[1:]
    )
    intermediates_match_source = all_succeeded and all(
        _same_outputs(source_intermediates, item["materialized_intermediates"])
        for item in records
    )
    stdout_equal = all_succeeded and all(
        _same_attempt_stream(records[0], item, "stdout") for item in records[1:]
    )
    stderr_equal = all_succeeded and all(
        _same_attempt_stream(records[0], item, "stderr") for item in records[1:]
    )
    undeclared_deltas_equal = all_succeeded and all(
        _same_undeclared_writes(records[0], item) for item in records[1:]
    )
    undeclared_writes_present = any(
        item["undeclared_writes"] for item in records
    )
    stage_traces_complete = (
        not stage_traced_source
        or all(
            item["stage_trace"]["state"] == "cooperative_report_complete"
            for item in records
        )
    )
    stage_traces_equal = (
        not stage_traced_source
        or (stage_traces_complete and all(
            _same_stage_trace(records[0]["stage_trace"], item["stage_trace"])
            for item in records[1:]
        ))
    )
    stage_traces_match_source = (
        not stage_traced_source
        or (stage_traces_complete and all(
            _same_stage_trace(source_stage_trace, item["stage_trace"])
            for item in records
        ))
    )
    if (all_succeeded and declared_outputs_equal and matches_source
            and intermediates_equal and intermediates_match_source
            and stdout_equal and stderr_equal and undeclared_deltas_equal
            and stage_traces_complete and stage_traces_equal
            and stage_traces_match_source):
        outcome = "byte_repeatable"
    elif all_succeeded:
        outcome = "repeatability_mismatch"
    else:
        outcome = "replay_failed"
    core = {
        "schema_version": (
            STAGE_REPLAY_SCHEMA if stage_traced_source else REPLAY_SCHEMA
        ),
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_run_id": run_id,
        "source_start_event_id": start["id"],
        "source_finish_event_id": finish["id"],
        "computation_id": plan["computation_id"],
        "pipeline_contract_id": contract["id"],
        "mode": "declared-project-files-fresh-workspace",
        "command": {
            "argv": list(stored_command),
            "argv_capture": plan.get("argv_capture"),
            "secret_override_used_not_stored": override_used,
            "cwd": cwd_label,
            "timeout_seconds": timeout_seconds,
        },
        "source_outputs": source_outputs,
        "source_materialized_intermediates": source_intermediates,
        "attempts": records,
        "comparison": {
            "basis": (
                "byte_exact_sha256_size_and_cooperative_stage_sequence"
                if stage_traced_source else "byte_exact_sha256_and_size"
            ),
            "minimum_attempts_met": count >= MIN_REPLAY_ATTEMPTS,
            "declared_outputs_equal": declared_outputs_equal,
            "all_declared_outputs_match_source_receipt": matches_source,
            "materialized_intermediates_equal": intermediates_equal,
            "all_materialized_intermediates_match_source_receipt": (
                intermediates_match_source
            ),
            "stdout_equal": stdout_equal,
            "stderr_equal": stderr_equal,
            "undeclared_workspace_deltas_equal": undeclared_deltas_equal,
            "undeclared_workspace_writes_present": undeclared_writes_present,
        },
        "outcome": outcome,
        "coverage": {
            "filesystem_inputs": "declared_project_files_only",
            "filesystem_writes": WORKSPACE_FILE_WRITE_COVERAGE,
            "external_filesystem_writes": "not_observed_or_prevented",
            "environment": "source_executable_and_root_lockfiles_rechecked_partial_host",
            "network": "not_isolated",
            "processes": "direct_child_exit_only",
            "internal_stage_execution": "declared_only_not_observed",
            "materialized_intermediates": "declared_paths_post_execution_hashed",
            "in_memory_intermediates": "not_observed",
            "method_code_equivalence": "requires_separate_review",
            "parameters_and_seeds": "recorded_declarations_not_runtime_injected",
        },
    }
    if stage_traced_source:
        core["source_stage_trace"] = source_stage_trace
        core["comparison"].update({
            "all_stage_traces_complete": stage_traces_complete,
            "stage_traces_equal": stage_traces_equal,
            "all_stage_traces_match_source_receipt": stage_traces_match_source,
        })
        core["coverage"]["cooperative_stage_trace"] = (
            "program_emitted_checkpoint_sequence_not_independent_observation"
        )
    certificate = {"id": f"replay:sha256:{canonical_sha256(core)}", **core}
    validate_replay_certificate(certificate)
    path = append_replay_certificate(cfg.replays_path, certificate)
    review_ready = (
        outcome == "byte_repeatable" and not undeclared_writes_present
        and not override_used
        and start.get("schema_version") in {
            CONTRACT_EVENT_SCHEMA, STAGE_CONTRACT_EVENT_SCHEMA,
        }
        and finish.get("schema_version") in {
            CONTRACT_EVENT_SCHEMA, STAGE_CONTRACT_EVENT_SCHEMA,
        }
    )
    return {
        "certificate": certificate,
        "path": path,
        "review_ready": review_ready,
        "exit_code": 0 if review_ready else 3,
    }


def evaluate_replay_certificate(cfg, document: dict, *, start: dict | None = None,
                                finish: dict | None = None) -> dict:
    """Recheck a stored certificate against current source receipts and project bytes.

    This does not execute project code.  A current certificate still has the fixed
    partial coverage recorded in the certificate itself.
    """
    validate_replay_certificate(document)
    findings = []

    def finding(code: str, detail: str) -> None:
        findings.append({"code": code, "detail": detail})

    if start is None or finish is None:
        try:
            start, finish = _source_run(cfg, document["source_run_id"])
        except ReplayError as exc:
            finding("REPLAY_SOURCE_STALE", str(exc))
            return {
                "current": False,
                "byte_repeatable_current": False,
                "review_ready_current": False,
                "stage_trace_repeatable_current": False,
                "outcome": document["outcome"],
                "findings": findings,
                "coverage": document["coverage"],
            }
    try:
        validate_event(start)
        validate_event(finish)
    except EventError as exc:
        finding("REPLAY_SOURCE_PAIR_INVALID", f"source event is invalid: {exc}")
    else:
        paired_runs, pair_issues = materialize_runs([start, finish])
        if (len(paired_runs) != 1 or pair_issues
                or paired_runs[0].get("start") is None
                or paired_runs[0].get("finish") is None):
            detail = "; ".join(
                f"{item['code']}: {item['detail']}" for item in pair_issues
            ) or "source events do not form exactly one complete run"
            finding("REPLAY_SOURCE_PAIR_INVALID", detail)
    if findings:
        findings.sort(key=lambda item: (item["code"], item["detail"]))
        return {
            "current": False,
            "byte_repeatable_current": False,
            "review_ready_current": False,
            "stage_trace_repeatable_current": False,
            "outcome": document["outcome"],
            "findings": findings,
            "coverage": document["coverage"],
        }
    plan = start.get("payload", {}).get("plan", {})
    expected_event_schema = {
        LEGACY_REPLAY_SCHEMA: CONTRACT_EVENT_SCHEMA,
        REPLAY_SCHEMA: CONTRACT_EVENT_SCHEMA,
        STAGE_REPLAY_SCHEMA: STAGE_CONTRACT_EVENT_SCHEMA,
    }.get(document["schema_version"])
    if (expected_event_schema is None
            or start.get("schema_version") != expected_event_schema
            or finish.get("schema_version") != expected_event_schema):
        finding(
            "REPLAY_LEGACY_SOURCE_COVERAGE",
            "replay schema does not match the source contract-receipt coverage",
        )
    if (start.get("id") != document["source_start_event_id"]
            or finish.get("id") != document["source_finish_event_id"]
            or start.get("run_id") != document["source_run_id"]
            or finish.get("run_id") != document["source_run_id"]):
        finding("REPLAY_SOURCE_STALE", "certificate source event links do not match")
    if finish.get("payload", {}).get("outcome") != "succeeded":
        finding("REPLAY_SOURCE_STALE", "source receipt no longer represents a successful run")
    contract = plan.get("pipeline_contract")
    if (plan.get("computation_id") != document["computation_id"]
            or not isinstance(contract, dict)
            or contract.get("id") != document["pipeline_contract_id"]):
        finding("REPLAY_SOURCE_STALE", "source computation or pipeline contract ID differs")
    try:
        expected_outputs = _source_outputs(finish, list(plan.get("declared_outputs", [])))
        if not _same_outputs(expected_outputs, document["source_outputs"]):
            finding("REPLAY_SOURCE_STALE", "certificate source-output snapshots differ from receipt")
    except ReplayError as exc:
        finding("REPLAY_SOURCE_STALE", str(exc))
    if document["schema_version"] in {REPLAY_SCHEMA, STAGE_REPLAY_SCHEMA}:
        try:
            expected_intermediates = _source_intermediates(
                finish, list(plan.get("declared_intermediates", []))
            )
            if not _same_outputs(
                    expected_intermediates,
                    document["source_materialized_intermediates"]):
                finding(
                    "REPLAY_SOURCE_STALE",
                    "certificate source materialized-intermediate snapshots differ "
                    "from receipt",
                )
        except ReplayError as exc:
            finding("REPLAY_SOURCE_STALE", str(exc))
    if document["schema_version"] == STAGE_REPLAY_SCHEMA:
        try:
            source_trace = finish["payload"]["stage_trace"]
            validate_stage_trace_against_snapshot(source_trace, contract)
            if not _same_stage_trace(source_trace, document["source_stage_trace"]):
                finding(
                    "REPLAY_SOURCE_STALE",
                    "certificate cooperative stage trace differs from the source receipt",
                )
        except (KeyError, PipelineError, TypeError) as exc:
            finding("REPLAY_SOURCE_STALE", f"source cooperative stage trace is invalid: {exc}")

    for item in plan.get("declared_inputs", []):
        try:
            portable = _portable_project_path(item.get("path"), "declared input path")
            current = snapshot_file(
                _project_file(cfg.root.resolve(), portable, "declared input"),
                portable.as_posix(),
            )
            if _fingerprint_snapshot(current) != _fingerprint_snapshot(item):
                finding("REPLAY_INPUT_DRIFT", f"declared source input changed: {portable.as_posix()}")
        except ReplayError as exc:
            finding("REPLAY_INPUT_DRIFT", str(exc))
    try:
        if not isinstance(contract, dict):
            raise ReplayError("source pipeline contract snapshot is missing")
        validate_pipeline_snapshot(contract)
        current_contract = resolve_pipeline_contract(
            cfg, contract["source"]["path"],
            declared_inputs=[item["path"] for item in plan.get("declared_inputs", [])],
            declared_outputs=list(plan.get("declared_outputs", [])),
            parameters=dict(plan.get("parameters", {})),
            seeds=dict(plan.get("seeds", {})),
            snapshot_schema=contract["schema_version"],
        )
        if current_contract["id"] != contract["id"]:
            raise ReplayError("pipeline contract, method, code, or anchor bytes changed")
    except (KeyError, PipelineError, ReplayError) as exc:
        finding("REPLAY_CONTRACT_DRIFT", str(exc))

    recorded_command = document["command"]
    if (recorded_command["argv"] != plan.get("argv")
            or recorded_command["cwd"] != plan.get("cwd")
            or recorded_command["argv_capture"] != plan.get("argv_capture")):
        finding(
            "REPLAY_COMMAND_SOURCE_MISMATCH",
            "certificate argv, cwd, or argv-capture mode differs from the source plan",
        )
    if recorded_command["secret_override_used_not_stored"]:
        finding(
            "REPLAY_COMMAND_OVERRIDE_UNVERIFIABLE",
            "the exact secret-bearing replay argv was neither stored nor committed",
        )

    stored_argv = plan.get("argv", [])
    if REDACTION_MARKER in stored_argv[0]:
        finding("REPLAY_ENVIRONMENT_UNAVAILABLE", "redacted executable cannot be rechecked")
    else:
        cwd_label = plan.get("cwd")
        source_cwd = cfg.root.resolve() if cwd_label == "." else cfg.root.joinpath(
            *_portable_project_path(cwd_label, "run cwd").parts
        )
        current_executable = _executable_identity(stored_argv, source_cwd)
        recorded_executable = plan.get("environment", {}).get("executable", {})
        if any(recorded_executable.get(key) != current_executable.get(key)
               for key in ("state", "resolved", "sha256")):
            finding("REPLAY_ENVIRONMENT_MISMATCH", "source executable identity changed")
    if plan.get("environment", {}).get("root_lockfiles") != _lockfile_hashes(cfg.root.resolve()):
        finding("REPLAY_ENVIRONMENT_MISMATCH", "root environment lockfiles changed")

    findings.sort(key=lambda item: (item["code"], item["detail"]))
    current = not findings
    undeclared_writes_present = any(
        attempt["undeclared_writes"] for attempt in document["attempts"]
    )
    materialized_evidence_complete = (
        document["schema_version"] in {REPLAY_SCHEMA, STAGE_REPLAY_SCHEMA}
        or not plan.get("declared_intermediates", [])
    )
    stage_trace_required = start.get("schema_version") == STAGE_CONTRACT_EVENT_SCHEMA
    stage_trace_evidence_complete = (
        not stage_trace_required
        or (
            document["schema_version"] == STAGE_REPLAY_SCHEMA
            and document["comparison"]["all_stage_traces_complete"]
            and document["comparison"]["stage_traces_equal"]
            and document["comparison"]["all_stage_traces_match_source_receipt"]
        )
    )
    return {
        "current": current,
        "byte_repeatable_current": current and document["outcome"] == "byte_repeatable",
        "review_ready_current": (
            current and document["outcome"] == "byte_repeatable"
            and not undeclared_writes_present and materialized_evidence_complete
            and stage_trace_evidence_complete
        ),
        "stage_trace_repeatable_current": (
            current and stage_trace_required and stage_trace_evidence_complete
            and document["outcome"] == "byte_repeatable"
        ),
        "outcome": document["outcome"],
        "findings": findings,
        "coverage": document["coverage"],
    }


def validate_replay_certificate(document: object) -> str:
    if not isinstance(document, dict):
        raise ReplayError("replay certificate must be an object")
    schema_version = document.get("schema_version")
    if schema_version not in SUPPORTED_REPLAY_SCHEMAS:
        raise ReplayError("replay certificate schema is unsupported")
    has_materialized = schema_version in {REPLAY_SCHEMA, STAGE_REPLAY_SCHEMA}
    has_stage_trace = schema_version == STAGE_REPLAY_SCHEMA
    expected = {
        "id", "schema_version", "observed_at", "source_run_id", "source_start_event_id",
        "source_finish_event_id", "computation_id", "pipeline_contract_id", "mode", "command",
        "source_outputs", "attempts", "comparison", "outcome", "coverage",
    }
    if has_materialized:
        expected.add("source_materialized_intermediates")
    if has_stage_trace:
        expected.add("source_stage_trace")
    if set(document) != expected:
        raise ReplayError("replay certificate keys differ from its schema version")
    replay_id = document.get("id")
    if not isinstance(replay_id, str) or not REPLAY_ID_RE.fullmatch(replay_id):
        raise ReplayError("replay certificate id is invalid")
    core = {key: document[key] for key in document if key != "id"}
    if replay_id != f"replay:sha256:{canonical_sha256(core)}":
        raise ReplayError("replay certificate id does not match canonical content")
    for key, pattern in (
            ("source_run_id", RUN_ID_RE),
            ("source_start_event_id", EVENT_ID_RE),
            ("source_finish_event_id", EVENT_ID_RE),
            ("computation_id", COMPUTATION_ID_RE),
            ("pipeline_contract_id", CONTRACT_ID_RE)):
        value = document.get(key)
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise ReplayError(f"replay certificate {key} is invalid")
    observed_at = document.get("observed_at")
    if not isinstance(observed_at, str) or not RFC3339_UTC_RE.fullmatch(observed_at):
        raise ReplayError("replay certificate observed_at must be RFC3339 UTC")
    command = document.get("command")
    if not isinstance(command, dict) or set(command) != {
            "argv", "argv_capture", "secret_override_used_not_stored", "cwd",
            "timeout_seconds"}:
        raise ReplayError("replay command record is invalid")
    if (not isinstance(command.get("argv"), list) or not command["argv"]
            or not all(isinstance(item, str) and item for item in command["argv"])):
        raise ReplayError("replay command argv is invalid")
    if command.get("argv_capture") != "redacted_best_effort":
        raise ReplayError("unsupported replay argv capture mode")
    override_used = command.get("secret_override_used_not_stored")
    if not isinstance(override_used, bool):
        raise ReplayError("replay secret-override flag must be boolean")
    contains_redaction = any(
        REDACTION_MARKER in token for token in command["argv"]
    )
    if contains_redaction and not override_used:
        raise ReplayError(
            "redacted replay argv requires an explicit uncommitted-override flag"
        )
    if has_materialized and override_used != contains_redaction:
        raise ReplayError(
            "replay override flag contradicts the stored command redaction"
        )
    cwd_value = command.get("cwd")
    if cwd_value != ".":
        _portable_project_path(cwd_value, "replay command cwd")
    timeout = command.get("timeout_seconds")
    if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or timeout <= 0 or timeout > 7 * 24 * 60 * 60):
        raise ReplayError("replay command timeout is invalid")

    def validate_snapshot(item: object, label: str) -> None:
        if not isinstance(item, dict) or not {"path", "state"} <= set(item):
            raise ReplayError(f"{label} must be a file snapshot")
        if set(item) - {"path", "state", "sha256", "size", "file_version_id"}:
            raise ReplayError(f"{label} contains unsupported snapshot fields")
        _portable_project_path(item.get("path"), f"{label} path")
        state = item.get("state")
        if state not in FILE_STATES:
            raise ReplayError(f"{label} has an invalid file state")
        if state == "stable":
            if set(item) != {"path", "state", "sha256", "size", "file_version_id"}:
                raise ReplayError(f"{label} stable snapshot is incomplete")
            sha = item.get("sha256")
            size = item.get("size")
            if (not isinstance(sha, str) or not SHA256_RE.fullmatch(sha)
                    or isinstance(size, bool) or not isinstance(size, int) or size < 0
                    or item.get("file_version_id") != f"file:sha256:{sha}"):
                raise ReplayError(f"{label} stable snapshot identity is invalid")
        elif set(item) != {"path", "state"}:
            raise ReplayError(f"{label} non-stable snapshot contains identity fields")

    source_outputs = document.get("source_outputs")
    if (not isinstance(source_outputs, list) or not source_outputs
            or len(source_outputs) > MAX_REPLAY_PATHS):
        raise ReplayError("replay source outputs must be a bounded non-empty list")
    for index, item in enumerate(source_outputs):
        validate_snapshot(item, f"source output #{index}")
        if item.get("state") != "stable":
            raise ReplayError("replay source outputs must all be stable")
    source_paths = [item["path"] for item in source_outputs]
    if len(source_paths) != len(set(source_paths)):
        raise ReplayError("replay source output paths contain duplicates")
    source_intermediates = document.get("source_materialized_intermediates", [])
    if has_materialized:
        if (not isinstance(source_intermediates, list)
                or len(source_intermediates) > MAX_REPLAY_PATHS):
            raise ReplayError(
                "replay source materialized intermediates must be a bounded list"
            )
        for index, item in enumerate(source_intermediates):
            validate_snapshot(item, f"source materialized intermediate #{index}")
            if item.get("state") != "stable":
                raise ReplayError(
                    "replay source materialized intermediates must all be stable"
                )
    intermediate_paths = [item["path"] for item in source_intermediates]
    if len(intermediate_paths) != len(set(intermediate_paths)):
        raise ReplayError(
            "replay source materialized-intermediate paths contain duplicates"
        )
    overlap = set(source_paths) & set(intermediate_paths)
    if overlap:
        raise ReplayError(
            "replay source output and materialized-intermediate paths overlap"
        )
    source_stage_trace = document.get("source_stage_trace")
    if has_stage_trace:
        try:
            validate_stage_trace(source_stage_trace)
        except PipelineError as exc:
            raise ReplayError(f"replay source stage trace is invalid: {exc}") from exc
        if source_stage_trace["state"] != "cooperative_report_complete":
            raise ReplayError("replay source stage trace must be complete")
    if (not isinstance(document.get("attempts"), list)
            or not 2 <= len(document["attempts"]) <= MAX_REPLAY_ATTEMPTS):
        raise ReplayError("replay certificate needs two to ten attempts")
    if [item.get("index") for item in document["attempts"]] != list(
            range(1, len(document["attempts"]) + 1)):
        raise ReplayError("replay attempt indices must be consecutive")
    attempt_keys = {
        "index", "outcome", "direct_child_returncode", "duration_ns", "launch_error",
        "stdout", "stderr", "outputs", "undeclared_writes",
    }
    if has_materialized:
        attempt_keys.add("materialized_intermediates")
    if has_stage_trace:
        attempt_keys.add("stage_trace")
    for attempt in document["attempts"]:
        if not isinstance(attempt, dict) or set(attempt) != attempt_keys:
            raise ReplayError("replay attempt fields differ from schema")
        if attempt.get("outcome") not in {
                "succeeded", "launch_error", "timed_out", "failed",
                "output_missing_or_unstable"}:
            raise ReplayError("replay attempt outcome is invalid")
        returncode = attempt.get("direct_child_returncode")
        if returncode is not None and (
                isinstance(returncode, bool) or not isinstance(returncode, int)):
            raise ReplayError("replay child return code is invalid")
        duration = attempt.get("duration_ns")
        if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
            raise ReplayError("replay attempt duration is invalid")
        launch_error = attempt.get("launch_error")
        if launch_error is not None and (
                not isinstance(launch_error, dict) or set(launch_error) != {"type", "detail"}
                or not all(isinstance(launch_error.get(key), str) for key in ("type", "detail"))):
            raise ReplayError("replay launch error record is invalid")
        for stream_name in ("stdout", "stderr"):
            stream = attempt.get(stream_name)
            if (not isinstance(stream, dict)
                    or set(stream) != {"sha256", "size", "content"}
                    or not isinstance(stream.get("sha256"), str)
                    or not SHA256_RE.fullmatch(stream["sha256"])
                    or isinstance(stream.get("size"), bool)
                    or not isinstance(stream.get("size"), int) or stream["size"] < 0
                    or stream.get("content") != "not_stored"):
                raise ReplayError(f"replay {stream_name} digest record is invalid")
        outputs = attempt.get("outputs")
        if not isinstance(outputs, list) or len(outputs) != len(source_outputs):
            raise ReplayError("replay attempt output roles differ from source outputs")
        for index, item in enumerate(outputs):
            validate_snapshot(item, f"attempt output #{index}")
        if [item["path"] for item in outputs] != source_paths:
            raise ReplayError("replay attempt output paths differ from source outputs")
        intermediates = attempt.get("materialized_intermediates", [])
        if has_materialized:
            if (not isinstance(intermediates, list)
                    or len(intermediates) != len(source_intermediates)):
                raise ReplayError(
                    "replay attempt materialized-intermediate roles differ from source"
                )
            for index, item in enumerate(intermediates):
                validate_snapshot(item, f"attempt materialized intermediate #{index}")
            if [item["path"] for item in intermediates] != intermediate_paths:
                raise ReplayError(
                    "replay attempt materialized-intermediate paths differ from source"
                )
        expected_attempt_outcome = _attempt_outcome(
            returncode, launch_error, [*outputs, *intermediates]
        )
        if attempt["outcome"] != expected_attempt_outcome:
            raise ReplayError(
                "replay attempt outcome contradicts direct execution evidence"
            )
        if has_stage_trace:
            try:
                validate_stage_trace(attempt["stage_trace"])
            except PipelineError as exc:
                raise ReplayError(f"replay attempt stage trace is invalid: {exc}") from exc
            if (attempt["stage_trace"]["required_stage_ids"]
                    != source_stage_trace["required_stage_ids"]):
                raise ReplayError(
                    "replay attempt stage requirements differ from the source trace"
                )
        writes = attempt.get("undeclared_writes")
        if not isinstance(writes, list) or len(writes) > MAX_REPLAY_SCAN_ENTRIES:
            raise ReplayError("replay undeclared-write list is invalid")
        for item in writes:
            if (not isinstance(item, dict)
                    or set(item) not in (
                        {"path", "transition", "declared_output", "evidence_basis"},
                        {"path", "transition", "declared_output", "evidence_basis", "after_sha256"},
                    )
                    or item.get("transition") not in FILE_TRANSITIONS
                    or item.get("declared_output") is not False
                    or item.get("evidence_basis") != "unattributed_pre_post_window"):
                raise ReplayError("replay undeclared-write evidence is invalid")
            _portable_project_path(item.get("path"), "undeclared write path")
            if "after_sha256" in item and (
                    not isinstance(item["after_sha256"], str)
                    or not SHA256_RE.fullmatch(item["after_sha256"])):
                raise ReplayError("replay undeclared-write digest is invalid")
    if document.get("mode") != "declared-project-files-fresh-workspace":
        raise ReplayError("unsupported replay mode")
    coverage = document.get("coverage")
    required_coverage = {
        "filesystem_inputs": "declared_project_files_only",
        "filesystem_writes": (
            WORKSPACE_FILE_WRITE_COVERAGE if has_materialized
            else LEGACY_WORKSPACE_WRITE_COVERAGE
        ),
        "external_filesystem_writes": "not_observed_or_prevented",
        "environment": "source_executable_and_root_lockfiles_rechecked_partial_host",
        "network": "not_isolated",
        "processes": "direct_child_exit_only",
        "internal_stage_execution": "declared_only_not_observed",
        "in_memory_intermediates": "not_observed",
        "method_code_equivalence": "requires_separate_review",
        "parameters_and_seeds": "recorded_declarations_not_runtime_injected",
    }
    if has_materialized:
        required_coverage["materialized_intermediates"] = (
            "declared_paths_post_execution_hashed"
        )
    if has_stage_trace:
        required_coverage["cooperative_stage_trace"] = (
            "program_emitted_checkpoint_sequence_not_independent_observation"
        )
    if coverage != required_coverage:
        raise ReplayError("replay certificate overstates or changes its coverage")
    comparison = document.get("comparison")
    comparison_keys = {
            "basis", "minimum_attempts_met", "declared_outputs_equal",
            "all_declared_outputs_match_source_receipt", "stdout_equal",
            "stderr_equal", "undeclared_workspace_deltas_equal",
            "undeclared_workspace_writes_present"}
    if has_materialized:
        comparison_keys |= {
            "materialized_intermediates_equal",
            "all_materialized_intermediates_match_source_receipt",
        }
    if has_stage_trace:
        comparison_keys |= {
            "all_stage_traces_complete", "stage_traces_equal",
            "all_stage_traces_match_source_receipt",
        }
    if not isinstance(comparison, dict) or set(comparison) != comparison_keys:
        raise ReplayError("replay comparison is invalid")
    expected_basis = (
        "byte_exact_sha256_size_and_cooperative_stage_sequence"
        if has_stage_trace else "byte_exact_sha256_and_size"
    )
    if comparison.get("basis") != expected_basis:
        raise ReplayError("unsupported replay comparison basis")
    boolean_comparison_keys = {
            "minimum_attempts_met", "declared_outputs_equal",
            "all_declared_outputs_match_source_receipt", "stdout_equal",
            "stderr_equal", "undeclared_workspace_deltas_equal",
            "undeclared_workspace_writes_present",
    }
    if has_materialized:
        boolean_comparison_keys |= {
            "materialized_intermediates_equal",
            "all_materialized_intermediates_match_source_receipt",
        }
    if has_stage_trace:
        boolean_comparison_keys |= {
            "all_stage_traces_complete", "stage_traces_equal",
            "all_stage_traces_match_source_receipt",
        }
    for key in boolean_comparison_keys:
        if not isinstance(comparison.get(key), bool):
            raise ReplayError("replay comparison flags must be boolean")
    all_succeeded = all(item["outcome"] == "succeeded" for item in document["attempts"])
    expected_equal = all_succeeded and all(
        _same_outputs(document["attempts"][0]["outputs"], item["outputs"])
        for item in document["attempts"][1:]
    )
    expected_source_match = all_succeeded and all(
        _same_outputs(source_outputs, item["outputs"]) for item in document["attempts"]
    )
    expected_intermediates_equal = all_succeeded and all(
        _same_outputs(
            document["attempts"][0].get("materialized_intermediates", []),
            item.get("materialized_intermediates", []),
        )
        for item in document["attempts"][1:]
    )
    expected_intermediates_source_match = all_succeeded and all(
        _same_outputs(
            source_intermediates, item.get("materialized_intermediates", [])
        )
        for item in document["attempts"]
    )
    expected_stdout_equal = all_succeeded and all(
        _same_attempt_stream(document["attempts"][0], item, "stdout")
        for item in document["attempts"][1:]
    )
    expected_stderr_equal = all_succeeded and all(
        _same_attempt_stream(document["attempts"][0], item, "stderr")
        for item in document["attempts"][1:]
    )
    expected_deltas_equal = all_succeeded and all(
        _same_undeclared_writes(document["attempts"][0], item)
        for item in document["attempts"][1:]
    )
    expected_undeclared_present = any(
        item["undeclared_writes"] for item in document["attempts"]
    )
    expected_stage_complete = (
        not has_stage_trace
        or all(
            item["stage_trace"]["state"] == "cooperative_report_complete"
            for item in document["attempts"]
        )
    )
    expected_stage_equal = (
        not has_stage_trace
        or (expected_stage_complete and all(
            _same_stage_trace(
                document["attempts"][0]["stage_trace"], item["stage_trace"],
            )
            for item in document["attempts"][1:]
        ))
    )
    expected_stage_source_match = (
        not has_stage_trace
        or (expected_stage_complete and all(
            _same_stage_trace(source_stage_trace, item["stage_trace"])
            for item in document["attempts"]
        ))
    )
    if (comparison["minimum_attempts_met"] is not True
            or comparison["declared_outputs_equal"] != expected_equal
            or comparison["all_declared_outputs_match_source_receipt"]
            != expected_source_match
            or (has_materialized and comparison["materialized_intermediates_equal"]
                != expected_intermediates_equal)
            or (has_materialized
                and comparison[
                    "all_materialized_intermediates_match_source_receipt"
                ] != expected_intermediates_source_match)
            or comparison["stdout_equal"] != expected_stdout_equal
            or comparison["stderr_equal"] != expected_stderr_equal
            or comparison["undeclared_workspace_deltas_equal"] != expected_deltas_equal
            or comparison["undeclared_workspace_writes_present"]
            != expected_undeclared_present
            or (has_stage_trace and comparison["all_stage_traces_complete"]
                != expected_stage_complete)
            or (has_stage_trace and comparison["stage_traces_equal"]
                != expected_stage_equal)
            or (has_stage_trace
                and comparison["all_stage_traces_match_source_receipt"]
                != expected_stage_source_match)):
        raise ReplayError("replay comparison contradicts the recorded attempt evidence")
    expected_outcome = (
        "byte_repeatable" if (
            all_succeeded and expected_equal and expected_source_match
            and (not has_materialized or (
                expected_intermediates_equal
                and expected_intermediates_source_match
            ))
            and expected_stdout_equal and expected_stderr_equal and expected_deltas_equal
            and expected_stage_complete and expected_stage_equal
            and expected_stage_source_match
        ) else "repeatability_mismatch" if all_succeeded else "replay_failed"
    )
    if document.get("outcome") != expected_outcome:
        raise ReplayError("replay outcome contradicts the recorded attempts/comparison")
    if document.get("outcome") not in {
            "byte_repeatable", "repeatability_mismatch", "replay_failed"}:
        raise ReplayError("unsupported replay outcome")
    return replay_id


def _certificate_path(store: Path, digest: str) -> Path:
    return store / digest[:2] / f"{digest}.json"


def append_replay_certificate(store: Path, document: dict) -> Path:
    replay_id = validate_replay_certificate(document)
    digest = replay_id.rsplit(":", 1)[1]
    destination = _certificate_path(Path(store), digest)
    _ensure_directory_durable(destination.parent)
    data = (json.dumps(
        document, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False,
    ) + "\n").encode("utf-8")
    if len(data) > MAX_CERTIFICATE_BYTES:
        raise ReplayError("replay certificate exceeds its byte limit")

    def validate_existing() -> Path:
        try:
            current = strict_json_loads(
                _stable_bounded_bytes(destination, MAX_CERTIFICATE_BYTES).decode("utf-8-sig"),
                str(destination),
            )
            validate_replay_certificate(current)
        except (OSError, UnicodeError, ValueError, RecursionError, EventError, ReplayError) as exc:
            raise ReplayError(f"existing replay certificate is invalid: {exc}") from exc
        if canonical_bytes(current) != canonical_bytes(document):
            raise ReplayError("refusing to overwrite a non-identical replay certificate")
        return destination

    with _event_lock(destination.with_suffix(".lock")):
        if destination.exists():
            return validate_existing()
        temporary = destination.parent / f".{digest}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temporary, "xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, destination)
                _fsync_directory(destination.parent)
            except FileExistsError:
                return validate_existing()
            except OSError as exc:
                raise ReplayError(f"cannot atomically append replay certificate: {exc}") from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return destination


def load_replay_certificates(store: Path) -> tuple[list[dict], list[dict]]:
    base = Path(store)
    if not base.exists():
        return [], []
    if _path_has_reparse_component(base) or not base.is_dir():
        return [], [{"code": "REPLAY_CERTIFICATE_INVALID", "path": ".", "detail": "store must be a non-link directory"}]
    paths = []
    total = 0
    issues = []
    try:
        for current, dirs, files in os.walk(base, topdown=True, followlinks=False):
            current_path = Path(current)
            retained_dirs = []
            for name in sorted(dirs):
                candidate = current_path / name
                if _path_has_reparse_component(candidate):
                    issues.append({
                        "code": "REPLAY_CERTIFICATE_INVALID",
                        "path": candidate.relative_to(base).as_posix(),
                        "detail": (
                            "replay store directory is a symlink, junction, "
                            "or reparse point"
                        ),
                    })
                    continue
                retained_dirs.append(name)
            dirs[:] = retained_dirs
            for name in sorted(files):
                path = current_path / name
                if path.suffix != ".json":
                    continue
                size = path.lstat().st_size
                paths.append(path)
                total += size
                if (len(paths) > MAX_CERTIFICATES or size > MAX_CERTIFICATE_BYTES
                        or total > MAX_REPLAY_STORE_BYTES):
                    raise ReplayError("replay certificate store exceeds its limits")
    except (OSError, ReplayError) as exc:
        return [], [{"code": "REPLAY_CERTIFICATE_INVALID", "path": ".", "detail": str(exc)}]
    documents = []
    for path in sorted(paths):
        label = path.relative_to(base).as_posix()
        try:
            payload = _stable_bounded_bytes(path, MAX_CERTIFICATE_BYTES)
            document = strict_json_loads(payload.decode("utf-8-sig"), str(path))
            replay_id = validate_replay_certificate(document)
            digest = replay_id.rsplit(":", 1)[1]
            if path != _certificate_path(base, digest):
                raise ReplayError("certificate path does not match its content id")
            documents.append(document)
        except (OSError, UnicodeError, ValueError, RecursionError, EventError, ReplayError) as exc:
            issues.append({"code": "REPLAY_CERTIFICATE_INVALID", "path": label, "detail": str(exc)})
    documents.sort(key=lambda item: (item["source_run_id"], item["observed_at"], item["id"]))
    issues.sort(key=lambda item: (item["path"], item["detail"]))
    return documents, issues


__all__ = [
    "LEGACY_REPLAY_SCHEMA", "REPLAY_SCHEMA", "SUPPORTED_REPLAY_SCHEMAS",
    "ReplayError", "replay_run", "validate_replay_certificate",
    "append_replay_certificate", "load_replay_certificates",
    "evaluate_replay_certificate",
]
