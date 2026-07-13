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
from pathlib import Path

EVENT_SCHEMA = "claimtrace.event/1"
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
    except (TypeError, ValueError) as exc:
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
        return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except json.JSONDecodeError as exc:
        raise EventError(f"{source}: invalid JSON: {exc}") from exc


def make_event(event_type: str, run_id: str, payload: dict, recorded_at: str | None = None) -> dict:
    if event_type not in EVENT_TYPES:
        raise EventError(f"unsupported event type: {event_type}")
    if not RUN_ID_RE.fullmatch(run_id):
        raise EventError(f"invalid run id: {run_id}")
    if not isinstance(payload, dict):
        raise EventError("event payload must be an object")
    core = {
        "schema_version": EVENT_SCHEMA,
        "type": event_type,
        "run_id": run_id,
        "recorded_at": recorded_at or _utc_now(),
        "payload": payload,
    }
    digest = canonical_sha256(core)
    return {"id": f"event:sha256:{digest}", **core}


def _validate_snapshot(snapshot: dict, expected_path: str, label: str) -> None:
    if not isinstance(snapshot, dict):
        raise EventError(f"{label} snapshot must be an object")
    if snapshot.get("path") != expected_path:
        raise EventError(f"{label} snapshot path does not match transition path {expected_path!r}")
    state = snapshot.get("state")
    if state not in FILE_STATES:
        raise EventError(f"{label} snapshot state is invalid: {state!r}")
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
        raise EventError("run.finished output transitions need a boolean produced field")
    expected_produced = expected_transition in {"created", "content_changed"}
    if produced != expected_produced:
        raise EventError(
            f"run.finished output produced={produced} disagrees with transition "
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
        if any(item["transition"] in invalid_outputs for item in payload["output_transitions"]):
            raise EventError("run.finished succeeded receipt cannot have a missing, deleted, or unstable output")


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


def _validate_event_payload(event_type: str, payload: dict) -> None:
    if event_type == "run.started":
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
        if not isinstance(plan.get("argv"), list) or not all(isinstance(v, str) for v in plan["argv"]):
            raise EventError("run.started plan argv must be a string list")
        if not isinstance(plan.get("cwd"), str):
            raise EventError("run.started plan cwd must be a string")
        declared_inputs = plan.get("declared_inputs")
        declared_outputs = plan.get("declared_outputs")
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
        for item in declared_inputs:
            _validate_snapshot(item, item["path"], "run.started declared input")
        expected_plan_id = f"recipe:sha256:{canonical_sha256(_fingerprint_plan(plan))}"
        if payload["plan_id"] != expected_plan_id:
            raise EventError("run.started plan_id does not match canonical plan content")
    elif event_type == "run.finished":
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
        if payload["outcome"] not in RUN_OUTCOMES:
            raise EventError("run.finished outcome is invalid")
        if not isinstance(payload["input_transitions"], list) or not isinstance(payload["output_transitions"], list):
            raise EventError("run.finished transitions must be lists")
        if payload["receipt_integrity"] != "finalized":
            raise EventError("run.finished receipt integrity is invalid")
        _validate_token_object(
            payload["lineage_coverage"], LINEAGE_COVERAGE_VALUES,
            "run.finished lineage_coverage")
        if (not isinstance(payload.get("contract_errors", []), list)
                or not all(isinstance(item, str) for item in payload.get("contract_errors", []))):
            raise EventError("run.finished contract_errors must be a string list")
        for role in ("input_transitions", "output_transitions"):
            paths = []
            for item in payload[role]:
                _validate_transition_item(item, role)
                paths.append(item["path"])
            if len(paths) != len(set(paths)):
                raise EventError(f"run.finished {role} contains duplicate paths")
        _validate_outcome(payload)
        result_basis = {
            "plan_id": payload["plan_id"],
            "outcome": payload["outcome"],
            "direct_child_returncode": payload.get("direct_child_returncode"),
            "input_transitions": [_fingerprint_transition(item) for item in payload["input_transitions"]],
            "output_transitions": [_fingerprint_transition(item) for item in payload["output_transitions"]],
            "contract_errors": payload.get("contract_errors", []),
        }
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
    if event["schema_version"] != EVENT_SCHEMA:
        raise EventError(f"unsupported event schema: {event['schema_version']!r}")
    if event["type"] not in EVENT_TYPES:
        raise EventError(f"unsupported event type: {event['type']!r}")
    if not RUN_ID_RE.fullmatch(str(event["run_id"])):
        raise EventError(f"invalid run id: {event['run_id']!r}")
    _validate_recorded_at(event["recorded_at"])
    if not isinstance(event["payload"], dict):
        raise EventError("event payload must be an object")
    _validate_event_payload(event["type"], event["payload"])
    match = EVENT_ID_RE.fullmatch(str(event["id"]))
    if not match:
        raise EventError(f"invalid event id: {event['id']!r}")
    core = {key: event[key] for key in ("schema_version", "type", "run_id", "recorded_at", "payload")}
    actual = canonical_sha256(core)
    if actual != match.group(1):
        raise EventError(f"event content hash mismatch: id says {match.group(1)}, content is {actual}")
    return actual


@contextmanager
def _event_lock(path: Path, timeout: float = 30.0):
    lock_root = Path(tempfile.gettempdir()) / "claimtrace-event-locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_name = hashlib.sha256(os.path.normcase(str(path.resolve())).encode("utf-8")).hexdigest() + ".lock"
    lock_path = lock_root / lock_name
    deadline = time.monotonic() + timeout
    with _PROCESS_EVENT_LOCK, lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
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
        handle = (lock_root / lock_name).open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
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
    lock_root = Path(tempfile.gettempdir()) / "claimtrace-output-locks"
    lock_root.mkdir(parents=True, exist_ok=True)
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


def append_event(events_path: Path, event: dict) -> Path:
    """Append one content-addressed event without overwriting existing evidence."""
    digest = validate_event(event)
    destination = _event_path(Path(events_path), digest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(event, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    lock = destination.with_suffix(".lock")
    with _event_lock(lock):
        if destination.exists():
            existing = _strict_json(destination.read_text(encoding="utf-8-sig"), str(destination))
            validate_event(existing)
            if canonical_bytes(existing) != canonical_bytes(event):
                raise EventError(f"refusing to overwrite non-identical event: {destination}")
            return destination
        tmp = destination.parent / f".{digest}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, destination)
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
    seen = set()
    for path in sorted(base.rglob("*.json"), key=lambda p: p.as_posix()):
        try:
            event = _strict_json(path.read_text(encoding="utf-8-sig"), str(path))
            digest = validate_event(event)
            expected = _event_path(base, digest).resolve()
            if path.resolve() != expected:
                raise EventError("event filename or fan-out directory does not match its content hash")
            if event["id"] in seen:
                raise EventError(f"duplicate event id: {event['id']}")
            seen.add(event["id"])
            events.append(event)
        except (OSError, UnicodeError, EventError) as exc:
            try:
                rel = path.relative_to(base).as_posix()
            except ValueError:
                rel = path.name
            issues.append({"code": "EVENT_INTEGRITY", "event_path": rel, "detail": str(exc)})
    events.sort(key=lambda e: (e["recorded_at"], e["id"]))
    issues.sort(key=lambda i: (i["code"], i["event_path"], i["detail"]))
    return events, issues


def _marker_directory(project_root: Path) -> Path:
    key = hashlib.sha256(os.path.normcase(str(project_root.resolve())).encode("utf-8")).hexdigest()[:20]
    return Path(tempfile.gettempdir()) / "claimtrace-active-runs" / key


def create_active_marker(project_root: Path, run_id: str, start_event: dict) -> Path:
    directory = _marker_directory(project_root)
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / f"{run_id[4:]}.json"
    payload = {
        "schema_version": ACTIVE_SCHEMA,
        "project_root": str(project_root.resolve()),
        "run_id": run_id,
        "start_event": start_event,
    }
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if marker.exists():
        raise EventError(f"active marker already exists for {run_id}")
    temporary = directory / f".{run_id[4:]}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, marker)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return marker


def remove_active_marker(marker: Path) -> None:
    marker.unlink()
    try:
        marker.parent.rmdir()
    except OSError:
        pass


def load_active_markers(project_root: Path):
    directory = _marker_directory(project_root)
    if not directory.exists():
        return [], []
    markers, issues = [], []
    for path in sorted(directory.glob("*.json"), key=lambda p: p.name):
        try:
            item = _strict_json(path.read_text(encoding="utf-8-sig"), str(path))
            if not isinstance(item, dict) or item.get("schema_version") != ACTIVE_SCHEMA:
                raise EventError("unsupported active marker schema")
            if Path(item.get("project_root", "")).resolve() != project_root.resolve():
                raise EventError("active marker belongs to a different project root")
            if item.get("run_id") != item.get("start_event", {}).get("run_id"):
                raise EventError("active marker run id does not match start event")
            validate_event(item["start_event"])
            markers.append(item)
        except (OSError, UnicodeError, EventError) as exc:
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
        if start and finish:
            plan = start["payload"].get("plan", {})
            expected_inputs = [item.get("path") for item in plan.get("declared_inputs", [])]
            expected_outputs = list(plan.get("declared_outputs", []))
            actual_inputs = [item.get("path") for item in finish["payload"].get("input_transitions", [])]
            actual_outputs = [item.get("path") for item in finish["payload"].get("output_transitions", [])]
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


def _path_has_reparse_component(path: Path) -> bool:
    current = path
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
        if current.parent == current:
            return False
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


def snapshot_file(path: Path, display: str) -> dict:
    base = {"path": display, "method": "sha256_stat_before_after"}
    try:
        before = path.lstat()
    except FileNotFoundError:
        return {**base, "state": "missing"}
    except OSError as exc:
        return {**base, "state": "unreadable", "error": type(exc).__name__}
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        return {**base, "state": "unsupported", "file_kind": stat.filemode(before.st_mode)}
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.lstat()
    except FileNotFoundError:
        return {**base, "state": "unstable", "reason": "disappeared_while_hashing"}
    except OSError as exc:
        return {**base, "state": "unreadable", "error": type(exc).__name__}
    if _stat_identity(before) != _stat_identity(after):
        return {**base, "state": "unstable", "reason": "metadata_changed_while_hashing"}
    sha = digest.hexdigest()
    return {
        **base,
        "state": "stable",
        "sha256": sha,
        "size": after.st_size,
        "mtime_ns": getattr(after, "st_mtime_ns", int(after.st_mtime * 1e9)),
        "file_version_id": f"file:sha256:{sha}",
    }


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


def _fingerprint_transition(item: dict) -> dict:
    return {
        "path": item["path"],
        "transition": item["transition"],
        **({"produced": item["produced"]} if "produced" in item else {}),
        "before": _fingerprint_snapshot(item["before"]),
        "after": _fingerprint_snapshot(item["after"]),
    }


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


def _control_plane_state(cfg) -> dict:
    files = []
    for path in (cfg.config_path, cfg.graph_path):
        try:
            display = path.resolve().relative_to(cfg.root.resolve()).as_posix()
        except ValueError:
            display = str(path.resolve())
        files.append(_fingerprint_snapshot(snapshot_file(path, display)))
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


def _paired_snapshots(items, phase: str):
    return [snapshot_file(path, display) for path, display in items]


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

    with _output_locks(declared_outputs):
        return _run_command_locked(
            cfg,
            command,
            project_root=project_root,
            run_cwd=run_cwd,
            declared_inputs=declared_inputs,
            declared_outputs=declared_outputs,
            name=name,
            parameters=parameters,
            seeds=seeds,
            scan_writes=scan_writes,
            redact_flags=redact_flags,
        )


def _run_command_locked(
    cfg,
    command: list[str],
    *,
    project_root: Path,
    run_cwd: Path,
    declared_inputs,
    declared_outputs,
    name: str | None,
    parameters: dict | None,
    seeds: dict | None,
    scan_writes: bool,
    redact_flags: list[str] | None,
) -> dict:
    """Capture and finalize one run while all of its declared output locks are held."""

    input_before = _paired_snapshots(declared_inputs, "before")
    output_before = _paired_snapshots(declared_outputs, "before")
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
    if control_before["event_ledger"]["state"] != "valid":
        precondition_errors.append("event ledger has integrity errors before launch")

    redacted = redact_argv(command, redact_flags)
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
        "parameters": parameters or {},
        "seeds": seeds or {},
        "environment": environment,
        "git": git_before,
        "control_plane_before": control_before,
    }
    plan_id = f"recipe:sha256:{canonical_sha256(_fingerprint_plan(plan_basis))}"
    run_id = f"run:{uuid.uuid4()}"
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
    start_event = make_event("run.started", run_id, start_payload)
    marker = create_active_marker(project_root, run_id, start_event)
    scan_before = _scan_project(project_root, cfg.events_path) if scan_writes else {}
    started_ns = time.monotonic_ns()
    return_code = None
    launch_error = None
    interrupted = False
    if not precondition_errors:
        try:
            proc = subprocess.run(command, cwd=str(run_cwd), shell=False, check=False)
            return_code = proc.returncode
        except KeyboardInterrupt:
            interrupted = True
        except OSError as exc:
            launch_error = {"type": type(exc).__name__, "detail": str(exc)}
    duration_ns = time.monotonic_ns() - started_ns

    input_after = _paired_snapshots(declared_inputs, "after")
    output_after = _paired_snapshots(declared_outputs, "after")
    scan_after = _scan_project(project_root, cfg.events_path) if scan_writes else {}
    control_after_full = _control_plane_state(cfg)
    control_after = _compact_control_plane(control_after_full)
    control_changed, concurrent_event_appends = _control_plane_change(
        control_before_full, control_after_full)
    input_transitions = []
    for before, after in zip(input_before, input_after):
        input_transitions.append({"path": before["path"], "transition": _transition(before, after), "before": before, "after": after})
    output_transitions = []
    for before, after in zip(output_before, output_after):
        transition = _transition(before, after)
        output_transitions.append({
            "path": before["path"], "transition": transition,
            "produced": transition in {"created", "content_changed"},
            "before": before, "after": after,
        })

    contract_errors = list(precondition_errors)
    contract_errors.extend(
        f"input {item['path']} changed during the run; consumed version is ambiguous"
        for item in input_transitions if item["transition"] != "unchanged"
    )
    contract_errors.extend(
        f"output {item['path']} ended as {item['transition']}"
        for item in output_transitions if item["transition"] in {"missing", "deleted", "unstable"}
    )
    if control_changed:
        contract_errors.append("claimtrace config, graph, or event ledger changed during child execution")
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

    window_deltas = _window_deltas(scan_before, scan_after, {display for _, display in declared_outputs}) if scan_writes else []
    git_after = capture_git(project_root)
    result_basis = {
        "plan_id": plan_id,
        "outcome": outcome,
        "direct_child_returncode": return_code,
        "input_transitions": [_fingerprint_transition(item) for item in input_transitions],
        "output_transitions": [_fingerprint_transition(item) for item in output_transitions],
        "contract_errors": contract_errors,
    }
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
    finish_event = make_event("run.finished", run_id, finish_payload)
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
        "result_id": result_id,
        "outcome": outcome,
        "direct_child_returncode": return_code,
        "exit_code": cli_exit,
        "output_transitions": output_transitions,
        "contract_errors": contract_errors,
        "lineage_coverage": finish_payload["lineage_coverage"],
    }
