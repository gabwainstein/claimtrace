"""Adversarial tests for content-addressed mechanical run receipts."""
import json
import multiprocessing as mp
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from claimtrace.cli import main
from claimtrace.config import Config
from claimtrace.events import (
    EventError,
    append_event,
    canonical_sha256,
    load_active_markers,
    load_events,
    make_event,
    materialize_runs,
    run_command,
)
from claimtrace.report import build_report


def _capture_scope(writes="declared_snapshots_plus_unattributed_project_window"):
    return {
        "reads": "declared_only_not_observed",
        "writes": writes,
        "processes": "direct_child_only",
    }


def _lineage_coverage(write_attribution="unattributed_pre_post_delta"):
    return {
        "overall": "partial",
        "reads": "declared_only_not_observed",
        "declared_inputs": "pre_and_post_hashed",
        "declared_outputs": "pre_and_post_hashed",
        "write_attribution": write_attribution,
        "processes": "direct_child_only",
        "environment": "partial",
        "git": "best_effort",
    }


def _process_append(events_path, event, start, results):
    start.wait()
    try:
        append_event(Path(events_path), event)
        results.put("ok")
    except Exception as exc:  # pragma: no cover - failure detail crosses process boundary
        results.put(type(exc).__name__ + ": " + str(exc))


def _project(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    trace = tmp_path / "claimtrace"
    trace.mkdir()
    config_path = tmp_path / "claimtrace.config.json"
    config_path.write_text(json.dumps({
        "root": ".",
        "graph": "claimtrace/graph.json",
        "events": "claimtrace/events",
        "render_types": ["figure"],
        "input_types": ["data", "artifact", "code"],
        "run_output_types": ["artifact"],
    }), encoding="utf-8")
    (trace / "graph.json").write_text(json.dumps({
        "schema_version": "1.0",
        "concepts": {},
        "nodes": [],
        "edges": [],
    }), encoding="utf-8")
    return Config(config_path)


def _copy_command():
    return [
        sys.executable,
        "-c",
        "from pathlib import Path; Path('out.txt').write_text(Path('in.txt').read_text())",
    ]


def _start_event(recorded_at="2026-07-13T00:00:00.000Z"):
    plan = {"argv": [], "cwd": ".", "declared_inputs": [], "declared_outputs": []}
    return make_event(
        "run.started",
        "run:12345678-1234-4123-8123-123456789abc",
        {
            "plan_id": "recipe:sha256:" + canonical_sha256(plan),
            "plan": plan,
            "capture_scope": _capture_scope(),
        },
        recorded_at=recorded_at,
    )


def _stable_snapshot(path, sha256="0" * 64):
    return {
        "path": path,
        "state": "stable",
        "sha256": sha256,
        "size": 1,
        "file_version_id": "file:sha256:" + sha256,
    }


def _planned_start(*, inputs=(), outputs=(), capture_scope=None):
    plan = {
        "argv": ["example"],
        "cwd": ".",
        "declared_inputs": list(inputs),
        "declared_outputs": list(outputs),
    }
    return make_event(
        "run.started",
        "run:12345678-1234-4123-8123-123456789abc",
        {
            "plan_id": "recipe:sha256:" + canonical_sha256(plan),
            "plan": plan,
            "capture_scope": (_capture_scope() if capture_scope is None else capture_scope),
        },
        recorded_at="2026-07-13T00:00:00.000Z",
    )


def _transition_fingerprint(item):
    def snapshot(value):
        return {key: value[key] for key in
                ("path", "state", "sha256", "file_version_id", "size")
                if key in value}

    return {
        "path": item["path"],
        "transition": item["transition"],
        **({"produced": item["produced"]} if "produced" in item else {}),
        "before": snapshot(item["before"]),
        "after": snapshot(item["after"]),
    }


def _finish_event(start, *, outcome="succeeded", returncode=0, contract_errors=(),
                  input_transitions=(), output_transitions=(), launch_error=None,
                  lineage_coverage=None):
    result_basis = {
        "plan_id": start["payload"]["plan_id"],
        "outcome": outcome,
        "direct_child_returncode": returncode,
        "input_transitions": [_transition_fingerprint(item) for item in input_transitions],
        "output_transitions": [_transition_fingerprint(item) for item in output_transitions],
        "contract_errors": list(contract_errors),
    }
    return make_event(
        "run.finished",
        start["run_id"],
        {
            "start_event_id": start["id"],
            "plan_id": start["payload"]["plan_id"],
            "result_id": "result:sha256:" + canonical_sha256(result_basis),
            "outcome": outcome,
            "direct_child_returncode": returncode,
            "launch_error": launch_error,
            "contract_errors": list(contract_errors),
            "input_transitions": list(input_transitions),
            "output_transitions": list(output_transitions),
            "receipt_integrity": "finalized",
            "lineage_coverage": (_lineage_coverage()
                                 if lineage_coverage is None else lineage_coverage),
        },
        recorded_at="2026-07-13T00:00:01.000Z",
    )


def test_event_is_content_addressed_and_tamper_is_detected(tmp_path):
    event = _start_event()
    destination = append_event(tmp_path, event)
    append_event(tmp_path, event)
    assert len(list(tmp_path.rglob("*.json"))) == 1

    stored = json.loads(destination.read_text(encoding="utf-8"))
    stored["payload"]["plan"]["cwd"] = "changed"
    destination.write_text(json.dumps(stored), encoding="utf-8")
    events, issues = load_events(tmp_path)
    assert events == []
    assert [item["code"] for item in issues] == ["EVENT_INTEGRITY"]
    assert "does not match" in issues[0]["detail"]


@pytest.mark.parametrize(("outcome", "returncode", "errors", "launch_error", "match"), [
    ("succeeded", 7, (), None, "requires direct_child_returncode 0"),
    ("succeeded", 0, ("contract drift",), None, "cannot contain contract_errors"),
    ("contract_failed", 0, (), None, "requires contract_errors"),
    ("failed", 0, (), None, "requires a non-zero"),
    ("capture_precondition_failed", None, (), None, "requires contract_errors"),
    ("launch_error", None, (), None, "requires launch_error details"),
    ("interrupted", 0, (), None, "requires a null"),
])
def test_impossible_outcome_contract_is_rejected(
        tmp_path, outcome, returncode, errors, launch_error, match):
    finish = _finish_event(
        _planned_start(), outcome=outcome, returncode=returncode,
        contract_errors=errors, launch_error=launch_error,
    )
    with pytest.raises(EventError, match=match):
        append_event(tmp_path, finish)


def test_transition_snapshots_and_produced_flag_must_agree(tmp_path):
    start = _planned_start(outputs=("out.txt",))
    valid = {
        "path": "out.txt",
        "transition": "created",
        "produced": True,
        "before": {"path": "out.txt", "state": "missing"},
        "after": _stable_snapshot("out.txt"),
    }
    invalid = [
        ({**valid, "produced": False}, "produced=False disagrees"),
        ({**valid, "transition": "unchanged"}, "disagrees with before/after"),
        ({**valid, "before": {"path": "other.txt", "state": "missing"}},
         "snapshot path does not match"),
    ]
    for index, (transition, match) in enumerate(invalid):
        finish = _finish_event(start, output_transitions=(transition,))
        with pytest.raises(EventError, match=match):
            append_event(tmp_path / str(index), finish)


def test_duplicate_declared_and_transition_paths_are_rejected(tmp_path):
    duplicate_plan = _planned_start(outputs=("out.txt", "out.txt"))
    with pytest.raises(EventError, match="declared_outputs contains duplicate paths"):
        append_event(tmp_path / "plan", duplicate_plan)

    start = _planned_start(outputs=("out.txt",))
    transition = {
        "path": "out.txt", "transition": "created", "produced": True,
        "before": {"path": "out.txt", "state": "missing"},
        "after": _stable_snapshot("out.txt"),
    }
    finish = _finish_event(start, output_transitions=(transition, transition))
    with pytest.raises(EventError, match="output_transitions contains duplicate paths"):
        append_event(tmp_path / "finish", finish)


def test_input_transition_cannot_claim_production(tmp_path):
    snapshot = _stable_snapshot("in.txt")
    start = _planned_start(inputs=(snapshot,))
    transition = {
        "path": "in.txt", "transition": "unchanged", "produced": False,
        "before": snapshot, "after": snapshot,
    }
    finish = _finish_event(start, input_transitions=(transition,))
    with pytest.raises(EventError, match="cannot claim production"):
        append_event(tmp_path, finish)


def test_materialized_run_roles_must_match_start_plan(tmp_path):
    start = _planned_start(
        inputs=(_stable_snapshot("in.txt"),), outputs=("out.txt",))
    transition = {
        "path": "other.txt", "transition": "created", "produced": True,
        "before": {"path": "other.txt", "state": "missing"},
        "after": _stable_snapshot("other.txt"),
    }
    finish = _finish_event(start, output_transitions=(transition,))
    append_event(tmp_path, start)
    append_event(tmp_path, finish)
    events, event_issues = load_events(tmp_path)
    _runs, run_issues = materialize_runs(events)
    assert event_issues == []
    assert {item["code"] for item in run_issues} == {
        "RUN_INPUT_ROLE_MISMATCH", "RUN_OUTPUT_ROLE_MISMATCH",
    }


def test_materialized_run_input_before_must_match_start_snapshot(tmp_path):
    start_snapshot = _stable_snapshot("in.txt", "0" * 64)
    forged_snapshot = _stable_snapshot("in.txt", "1" * 64)
    start = _planned_start(inputs=(start_snapshot,))
    finish = _finish_event(start, input_transitions=({
        "path": "in.txt", "transition": "unchanged",
        "before": forged_snapshot, "after": forged_snapshot,
    },))
    cfg = _project(tmp_path)
    append_event(cfg.events_path, start)
    append_event(cfg.events_path, finish)

    events, event_issues = load_events(cfg.events_path)
    _runs, run_issues = materialize_runs(events)
    assert event_issues == []
    assert [item["code"] for item in run_issues] == ["RUN_INPUT_SNAPSHOT_MISMATCH"]
    report = build_report(cfg, strict=True)
    assert report["ok"] is False
    assert any(item["code"] == "RUN_INPUT_SNAPSHOT_MISMATCH" and item["blocking"]
               for item in report["findings"])


def test_coverage_tokens_reject_overclaims_and_cross_event_mismatch(tmp_path):
    capture_overclaim = _capture_scope()
    capture_overclaim["reads"] = "observed_complete"
    with pytest.raises(EventError, match="capture_scope reads token is invalid"):
        append_event(tmp_path / "capture", _planned_start(capture_scope=capture_overclaim))

    start = _planned_start()
    lineage_overclaim = _lineage_coverage()
    lineage_overclaim["overall"] = "complete"
    with pytest.raises(EventError, match="lineage_coverage overall token is invalid"):
        append_event(
            tmp_path / "lineage",
            _finish_event(start, lineage_coverage=lineage_overclaim),
        )

    no_scan_start = _planned_start(
        capture_scope=_capture_scope(writes="declared_snapshots_only"))
    inconsistent_finish = _finish_event(no_scan_start)
    cfg = _project(tmp_path / "mismatch")
    append_event(cfg.events_path, no_scan_start)
    append_event(cfg.events_path, inconsistent_finish)
    events, event_issues = load_events(cfg.events_path)
    _runs, run_issues = materialize_runs(events)
    assert event_issues == []
    assert [item["code"] for item in run_issues] == ["RUN_COVERAGE_MISMATCH"]
    report = build_report(cfg, strict=True)
    assert report["ok"] is False
    assert any(item["code"] == "RUN_COVERAGE_MISMATCH" and item["blocking"]
               for item in report["findings"])


def test_concurrent_identical_append_never_overwrites(tmp_path):
    event = _start_event()
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(lambda _: append_event(tmp_path, event), range(24)))
    assert len(set(paths)) == 1
    events, issues = load_events(tmp_path)
    assert issues == []
    assert events == [event]


def test_concurrent_process_append_never_overwrites(tmp_path):
    event = _start_event()
    context = mp.get_context("spawn")
    start, results = context.Event(), context.Queue()
    processes = [
        context.Process(target=_process_append,
                        args=(str(tmp_path), event, start, results))
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    start.set()
    outcomes = [results.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert outcomes == ["ok"] * 4
    events, issues = load_events(tmp_path)
    assert events == [event]
    assert issues == []


def test_successful_run_records_partial_lineage_without_mutating_graph(tmp_path):
    cfg = _project(tmp_path)
    (tmp_path / "in.txt").write_text("evidence", encoding="utf-8")
    graph_before = cfg.graph_path.read_bytes()

    result = run_command(
        cfg,
        _copy_command(),
        inputs=["in.txt"], outputs=["out.txt"], cwd=str(tmp_path),
    )

    assert result["exit_code"] == 0
    assert result["outcome"] == "succeeded"
    assert result["output_transitions"][0]["transition"] == "created"
    assert result["output_transitions"][0]["produced"] is True
    assert result["lineage_coverage"]["reads"] == "declared_only_not_observed"
    assert cfg.graph_path.read_bytes() == graph_before
    events, issues = load_events(cfg.events_path)
    runs, run_issues = materialize_runs(events)
    assert issues == run_issues == []
    assert len(events) == 2
    assert runs[0]["finish"]["payload"]["start_event_id"] == runs[0]["start"]["id"]
    markers, marker_issues = load_active_markers(cfg.root)
    assert markers == marker_issues == []


def test_unchanged_output_is_not_called_produced(tmp_path):
    cfg = _project(tmp_path)
    (tmp_path / "out.txt").write_text("old", encoding="utf-8")
    result = run_command(
        cfg, [sys.executable, "-c", "pass"],
        inputs=[], outputs=["out.txt"], no_inputs=True, cwd=str(tmp_path),
    )
    item = result["output_transitions"][0]
    assert result["exit_code"] == 0
    assert item["transition"] == "unchanged"
    assert item["produced"] is False


def test_zero_exit_with_missing_output_is_contract_failure(tmp_path):
    cfg = _project(tmp_path)
    result = run_command(
        cfg, [sys.executable, "-c", "pass"],
        inputs=[], outputs=["missing.txt"], no_inputs=True, cwd=str(tmp_path),
    )
    assert result["exit_code"] == 3
    assert result["outcome"] == "contract_failed"
    assert "ended as missing" in result["contract_errors"][0]
    events, issues = load_events(cfg.events_path)
    assert issues == []
    finish = next(event for event in events if event["type"] == "run.finished")
    assert finish["payload"]["outcome"] == "contract_failed"


def test_missing_input_blocks_launch_but_still_finalizes_receipt(tmp_path):
    cfg = _project(tmp_path)
    result = run_command(
        cfg,
        [sys.executable, "-c", "from pathlib import Path; Path('should_not_exist').touch()"],
        inputs=["missing.txt"], outputs=[], no_outputs=True, cwd=str(tmp_path),
    )
    assert result["outcome"] == "capture_precondition_failed"
    assert result["direct_child_returncode"] is None
    assert not (tmp_path / "should_not_exist").exists()
    events, issues = load_events(cfg.events_path)
    assert len(events) == 2
    assert issues == []


def test_nonzero_child_records_changed_output_and_preserves_returncode(tmp_path):
    cfg = _project(tmp_path)
    command = [
        sys.executable, "-c",
        "from pathlib import Path; Path('failed.txt').write_text('partial'); raise SystemExit(7)",
    ]
    result = run_command(
        cfg, command,
        inputs=[], outputs=["failed.txt"], no_inputs=True, cwd=str(tmp_path),
    )
    assert result["exit_code"] == 7
    assert result["outcome"] == "failed"
    assert result["output_transitions"][0]["transition"] == "created"
    events, _ = load_events(cfg.events_path)
    finish = next(event for event in events if event["type"] == "run.finished")
    assert finish["payload"]["direct_child_returncode"] == 7


def test_run_preserves_metacharacters_unicode_and_spaces(tmp_path):
    cfg = _project(tmp_path)
    argument = "space & | $ ; ünicode"
    command = [
        sys.executable, "-c",
        "import json,sys; from pathlib import Path; Path('argv.json').write_text(json.dumps(sys.argv[1:]))",
        argument,
    ]
    result = run_command(
        cfg, command,
        inputs=[], outputs=["argv.json"], no_inputs=True, cwd=str(tmp_path),
    )
    assert result["exit_code"] == 0
    assert json.loads((tmp_path / "argv.json").read_text()) == [argument]


def test_external_path_requires_explicit_opt_in(tmp_path):
    cfg = _project(tmp_path / "project")
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(EventError, match="outside the project root"):
        run_command(
            cfg, [sys.executable, "-c", "pass"],
            inputs=[str(outside)], outputs=[], no_outputs=True,
            cwd=str(cfg.root),
        )


def test_cli_requires_separator_and_explicit_roles(tmp_path, capsys):
    cfg = _project(tmp_path)
    config = str(cfg.config_path)
    code = main([
        "--config", config, "run", "--no-inputs", "--no-outputs",
        sys.executable, "-c", "pass",
    ])
    assert code == 2
    assert "with --" in capsys.readouterr().err

    code = main([
        "--config", config, "run", "--no-inputs", "--no-outputs",
        "--cwd", str(tmp_path), "--", sys.executable, "-c", "pass",
    ])
    captured = capsys.readouterr()
    assert code == 0
    assert "lineage coverage: partial" in captured.out


def test_secret_flag_values_are_redacted_from_receipt(tmp_path):
    cfg = _project(tmp_path)
    secret = "do-not-store-this-secret"
    result = run_command(
        cfg,
        [sys.executable, "-c", "pass", "--token", secret],
        inputs=[], outputs=[], no_inputs=True, no_outputs=True, cwd=str(tmp_path),
    )
    assert result["exit_code"] == 0
    stored = "\n".join(path.read_text(encoding="utf-8") for path in cfg.events_path.rglob("*.json"))
    assert secret not in stored
    assert "[REDACTED]" in stored


def test_recipe_and_result_fingerprints_ignore_file_mtime(tmp_path):
    cfg = _project(tmp_path)
    source = tmp_path / "input.txt"
    source.write_text("same bytes", encoding="utf-8")
    first = run_command(
        cfg, [sys.executable, "-c", "pass"],
        inputs=["input.txt"], outputs=[], no_outputs=True, cwd=str(tmp_path),
    )
    stat_before = source.stat()
    os.utime(source, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns + 2_000_000_000))
    second = run_command(
        cfg, [sys.executable, "-c", "pass"],
        inputs=["input.txt"], outputs=[], no_outputs=True, cwd=str(tmp_path),
    )
    assert second["plan_id"] == first["plan_id"]
    assert second["result_id"] == first["result_id"]


def test_child_control_plane_mutation_is_a_contract_failure(tmp_path):
    cfg = _project(tmp_path)
    result = run_command(
        cfg,
        [sys.executable, "-c",
         "from pathlib import Path; Path('claimtrace/graph.json').write_text('{}')"],
        inputs=[], outputs=[], no_inputs=True, no_outputs=True, cwd=str(tmp_path),
    )
    assert result["exit_code"] == 3
    assert result["outcome"] == "contract_failed"
    assert any("config, graph, or event ledger changed" in item
               for item in result["contract_errors"])
    events, issues = load_events(cfg.events_path)
    assert len(events) == 2
    assert issues == []


def test_concurrent_runs_accept_append_only_receipt_activity(tmp_path):
    cfg = _project(tmp_path)
    commands = [
        [sys.executable, "-c",
         "import time; from pathlib import Path; time.sleep(0.2); Path('a.txt').write_text('a')"],
        [sys.executable, "-c",
         "import time; from pathlib import Path; time.sleep(0.6); Path('b.txt').write_text('b')"],
    ]

    def execute(index):
        return run_command(
            cfg, commands[index], inputs=[], outputs=[f"{'ab'[index]}.txt"],
            no_inputs=True, cwd=str(tmp_path),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, range(2)))
    assert [item["exit_code"] for item in results] == [0, 0]
    assert not any("event ledger changed" in error
                   for item in results for error in item["contract_errors"])
    events, issues = load_events(cfg.events_path)
    runs, run_issues = materialize_runs(events)
    assert len(events) == 4
    assert len(runs) == 2
    assert issues == run_issues == []
