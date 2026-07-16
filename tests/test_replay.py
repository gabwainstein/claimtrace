"""Fresh-workspace replay is exact at the boundary and honest about its limits."""
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from claimtrace import events as events_module
from claimtrace import replay as replay_module
from claimtrace.cli import main as cli_main
from claimtrace.events import run_command
from claimtrace.pipeline import resolve_pipeline_contract
from claimtrace.replay import (
    LEGACY_REPLAY_SCHEMA,
    LEGACY_WORKSPACE_WRITE_COVERAGE,
    ReplayError,
    STAGE_REPLAY_SCHEMA,
    WORKSPACE_FILE_WRITE_COVERAGE,
    evaluate_replay_certificate,
    load_replay_certificates,
    replay_run,
    validate_replay_certificate,
)
from test_pipeline import _instrumented_project, _materialized_project, _project


def _run(cfg, **kwargs):
    return run_command(
        cfg, [sys.executable, "analysis/pipeline.py"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="claimtrace/primary.pipeline.json",
        scan_writes=False, **kwargs,
    )


def _refresh_anchor(cfg, contract, contract_path):
    lines = (cfg.root / "analysis" / "pipeline.py").read_bytes().splitlines(keepends=True)
    for stage in contract["stages"]:
        for anchor in stage["code_anchors"]:
            start, end = anchor["start_line"], anchor["end_line"]
            anchor["text_sha256"] = hashlib.sha256(
                b"".join(lines[start - 1:end])
            ).hexdigest()
    contract_path.write_text(json.dumps(contract), encoding="utf-8")


def _readdress(certificate):
    core = {key: value for key, value in certificate.items() if key != "id"}
    certificate["id"] = "replay:sha256:" + hashlib.sha256(
        json.dumps(
            core, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _as_legacy_certificate(current):
    legacy = deepcopy(current)
    legacy["schema_version"] = LEGACY_REPLAY_SCHEMA
    legacy.pop("source_materialized_intermediates")
    for attempt in legacy["attempts"]:
        attempt.pop("materialized_intermediates")
    legacy["comparison"].pop("materialized_intermediates_equal")
    legacy["comparison"].pop(
        "all_materialized_intermediates_match_source_receipt"
    )
    legacy["coverage"].pop("materialized_intermediates")
    legacy["coverage"]["filesystem_writes"] = LEGACY_WORKSPACE_WRITE_COVERAGE
    _readdress(legacy)
    return legacy


def _legacy_source_pair(cfg, run_id):
    current, issues = events_module.load_events(cfg.events_path)
    assert issues == []
    current_start = next(
        item for item in current
        if item["run_id"] == run_id and item["type"] == "run.started"
    )
    current_finish = next(
        item for item in current
        if item["run_id"] == run_id and item["type"] == "run.finished"
    )

    plan = deepcopy(current_start["payload"]["plan"])
    plan.pop("declared_intermediates")
    plan["pipeline_contract"] = resolve_pipeline_contract(
        cfg, plan["pipeline_contract"]["source"]["path"],
        declared_inputs=[item["path"] for item in plan["declared_inputs"]],
        declared_outputs=list(plan["declared_outputs"]),
        parameters=dict(plan["parameters"]), seeds=dict(plan["seeds"]),
        snapshot_schema="claimtrace.pipeline-contract-snapshot/1",
    )
    plan["computation_id"] = (
        "computation:sha256:"
        + events_module.canonical_sha256(
            events_module._fingerprint_computation(plan)
        )
    )
    plan_id = (
        "recipe:sha256:"
        + events_module.canonical_sha256(events_module._fingerprint_plan(plan))
    )
    start_payload = deepcopy(current_start["payload"])
    start_payload["plan"] = plan
    start_payload["plan_id"] = plan_id
    legacy_start = events_module.make_event(
        "run.started", run_id, start_payload,
        recorded_at=current_start["recorded_at"],
        schema_version=events_module.LEGACY_CONTRACT_EVENT_SCHEMA,
    )

    finish_payload = deepcopy(current_finish["payload"])
    finish_payload["start_event_id"] = legacy_start["id"]
    finish_payload["plan_id"] = plan_id
    finish_payload.pop("intermediate_transitions")
    finish_payload["lineage_coverage"].pop("declared_intermediates")
    result_basis = {
        "plan_id": plan_id,
        "outcome": finish_payload["outcome"],
        "direct_child_returncode": finish_payload["direct_child_returncode"],
        "input_transitions": [
            events_module._fingerprint_transition(item)
            for item in finish_payload["input_transitions"]
        ],
        "output_transitions": [
            events_module._fingerprint_transition(item)
            for item in finish_payload["output_transitions"]
        ],
        "contract_errors": finish_payload["contract_errors"],
    }
    finish_payload["result_id"] = (
        "result:sha256:" + events_module.canonical_sha256(result_basis)
    )
    legacy_finish = events_module.make_event(
        "run.finished", run_id, finish_payload,
        recorded_at=current_finish["recorded_at"],
        schema_version=events_module.LEGACY_CONTRACT_EVENT_SCHEMA,
    )
    store = cfg.root / "claimtrace" / "legacy-events"
    events_module.append_event(store, legacy_start)
    events_module.append_event(store, legacy_finish)
    return store, legacy_start, legacy_finish


def test_replay_proves_only_boundary_byte_repeatability(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    source = _run(cfg)
    original = (cfg.root / "results" / "fit.json").read_bytes()

    result = replay_run(cfg, source["run_id"], attempts=2)

    assert result["exit_code"] == 0
    assert result["review_ready"] is True
    certificate = result["certificate"]
    assert certificate["outcome"] == "byte_repeatable"
    assert certificate["comparison"] == {
        "basis": "byte_exact_sha256_and_size",
        "minimum_attempts_met": True,
        "declared_outputs_equal": True,
        "all_declared_outputs_match_source_receipt": True,
        "materialized_intermediates_equal": True,
        "all_materialized_intermediates_match_source_receipt": True,
        "stdout_equal": True,
        "stderr_equal": True,
        "undeclared_workspace_deltas_equal": True,
        "undeclared_workspace_writes_present": False,
    }
    assert certificate["coverage"]["internal_stage_execution"] == (
        "declared_only_not_observed"
    )
    assert certificate["coverage"]["network"] == "not_isolated"
    assert certificate["coverage"]["external_filesystem_writes"] == (
        "not_observed_or_prevented"
    )
    assert certificate["coverage"]["filesystem_writes"] == (
        WORKSPACE_FILE_WRITE_COVERAGE
    )
    assert (cfg.root / "results" / "fit.json").read_bytes() == original
    documents, issues = load_replay_certificates(cfg.replays_path)
    assert issues == []
    assert documents == [certificate]
    evaluation = evaluate_replay_certificate(cfg, certificate)
    assert evaluation["byte_repeatable_current"] is True
    assert evaluation["review_ready_current"] is True


def test_ordinary_replay_scrubs_inherited_reserved_trace_environment(
        tmp_path, monkeypatch):
    cfg, _contract, _path = _instrumented_project(tmp_path)
    monkeypatch.setenv(
        "PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"),
    )
    source = _run(cfg)
    monkeypatch.setenv("CLAIMTRACE_STAGE_TRACE_PATH", str(tmp_path / "outer.jsonl"))

    result = replay_run(cfg, source["run_id"], attempts=2)

    assert result["exit_code"] == 0
    assert result["certificate"]["schema_version"] != STAGE_REPLAY_SCHEMA
    assert result["certificate"]["outcome"] == "byte_repeatable"


def test_replay_compares_complete_cooperative_stage_sequences(tmp_path, monkeypatch):
    cfg, _contract, _path = _instrumented_project(tmp_path)
    monkeypatch.setenv(
        "PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"),
    )
    source = _run(cfg, stage_checkpoints=True)

    result = replay_run(cfg, source["run_id"], attempts=2)

    assert result["exit_code"] == 0
    certificate = result["certificate"]
    assert certificate["schema_version"] == STAGE_REPLAY_SCHEMA
    assert certificate["outcome"] == "byte_repeatable"
    assert certificate["source_stage_trace"]["state"] == (
        "cooperative_report_complete"
    )
    assert certificate["comparison"]["all_stage_traces_complete"] is True
    assert certificate["comparison"]["stage_traces_equal"] is True
    assert certificate["comparison"][
        "all_stage_traces_match_source_receipt"
    ] is True
    assert all(
        attempt["stage_trace"]["state"] == "cooperative_report_complete"
        for attempt in certificate["attempts"]
    )
    evaluation = evaluate_replay_certificate(cfg, certificate)
    assert evaluation["stage_trace_repeatable_current"] is True
    assert evaluation["review_ready_current"] is True


def test_replay_rejects_checkpoint_sequences_emitted_by_descendants(
        tmp_path, monkeypatch):
    cfg, contract, contract_path = _instrumented_project(tmp_path)
    code = (
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n"
        "from claimtrace.pipeline import stage_checkpoint\n"
        "\n"
        "def work():\n"
        "    rows = Path('data/raw.csv').read_text().splitlines()\n"
        "    complete = [row for row in rows[1:] if row]\n"
        "    stage_checkpoint('complete')\n"
        "    slope = len(complete)\n"
        "    Path('results/fit.json').write_text(str(slope))\n"
        "    stage_checkpoint('fit')\n"
        "\n"
        "if os.environ.get('CLAIMTRACE_DESCENDANT') == '1':\n"
        "    work()\n"
        "elif Path.cwd().name.startswith('attempt-'):\n"
        "    child_env = os.environ.copy()\n"
        "    child_env['CLAIMTRACE_DESCENDANT'] = '1'\n"
        "    raise SystemExit(subprocess.run([sys.executable, __file__], env=child_env).returncode)\n"
        "else:\n"
        "    work()\n"
    )
    code_path = cfg.root / "analysis" / "pipeline.py"
    code_path.write_text(code, encoding="utf-8")
    lines = code_path.read_bytes().splitlines(keepends=True)
    contract["stages"][0]["code_anchors"][0].update({
        "start_line": 8, "end_line": 10,
        "text_sha256": hashlib.sha256(b"".join(lines[7:10])).hexdigest(),
    })
    contract["stages"][1]["code_anchors"][0].update({
        "start_line": 11, "end_line": 13,
        "text_sha256": hashlib.sha256(b"".join(lines[10:13])).hexdigest(),
    })
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    monkeypatch.setenv(
        "PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"),
    )
    source = _run(cfg, stage_checkpoints=True)
    assert source["outcome"] == "succeeded"

    result = replay_run(cfg, source["run_id"], attempts=2)

    assert result["exit_code"] != 0
    certificate = result["certificate"]
    assert certificate["outcome"] == "repeatability_mismatch"
    assert certificate["comparison"]["all_stage_traces_complete"] is False
    assert all(
        attempt["stage_trace"]["state"] == "cooperative_report_invalid"
        and any(
            issue["code"] == "STAGE_TRACE_REPORTER_PROCESS_MISMATCH"
            for issue in attempt["stage_trace"]["issues"]
        )
        for attempt in certificate["attempts"]
    )


def test_replay_wait_error_after_spawn_kills_and_reaps_child(
        tmp_path, monkeypatch):
    cfg, _contract, _path = _project(tmp_path)
    source = _run(cfg)
    processes = []

    class WaitFailsProcess:
        def __init__(self, pid):
            self.pid = pid
            self.killed = False
            self.wait_calls = 0

        def wait(self, timeout=None):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise OSError("synthetic replay wait failure")
            return -9

        def poll(self):
            return -9 if self.killed else None

        def kill(self):
            self.killed = True

    def spawn(*_args, **_kwargs):
        process = WaitFailsProcess(43000 + len(processes))
        processes.append(process)
        return process

    monkeypatch.setattr(replay_module.subprocess, "Popen", spawn)

    result = replay_run(cfg, source["run_id"], attempts=2)

    assert result["exit_code"] != 0
    assert len(processes) == 2
    assert all(item.killed and item.wait_calls == 2 for item in processes)
    assert all(
        attempt["outcome"] == "launch_error"
        and attempt["launch_error"]["type"] == "OSError"
        for attempt in result["certificate"]["attempts"]
    )


def test_materialized_intermediate_is_compared_and_review_ready(tmp_path):
    cfg, _contract, _path = _materialized_project(tmp_path)
    source = _run(cfg)

    result = replay_run(cfg, source["run_id"], attempts=2)

    certificate = result["certificate"]
    assert result["exit_code"] == 0
    assert result["review_ready"] is True
    assert certificate["outcome"] == "byte_repeatable"
    assert certificate["source_materialized_intermediates"] == [{
        "path": "data/clean.csv",
        "state": "stable",
        "sha256": hashlib.sha256(b"1,2").hexdigest(),
        "size": 3,
        "file_version_id": "file:sha256:" + hashlib.sha256(b"1,2").hexdigest(),
    }]
    assert all(
        attempt["materialized_intermediates"]
        == certificate["source_materialized_intermediates"]
        for attempt in certificate["attempts"]
    )
    assert certificate["comparison"]["materialized_intermediates_equal"] is True
    assert certificate["comparison"][
        "all_materialized_intermediates_match_source_receipt"
    ] is True
    assert certificate["comparison"]["undeclared_workspace_writes_present"] is False
    assert certificate["coverage"]["internal_stage_execution"] == (
        "declared_only_not_observed"
    )
    assert evaluate_replay_certificate(cfg, certificate)["review_ready_current"] is True


def test_random_materialized_intermediate_blocks_repeatability_with_stable_output(
        tmp_path):
    cfg, _contract, _path = _materialized_project(
        tmp_path, random_intermediate=True
    )
    source = _run(cfg)

    result = replay_run(cfg, source["run_id"], attempts=3)

    certificate = result["certificate"]
    assert result["exit_code"] == 3
    assert result["review_ready"] is False
    assert certificate["outcome"] == "repeatability_mismatch"
    assert certificate["comparison"]["declared_outputs_equal"] is True
    assert certificate["comparison"][
        "all_declared_outputs_match_source_receipt"
    ] is True
    assert certificate["comparison"]["materialized_intermediates_equal"] is False
    assert certificate["comparison"][
        "all_materialized_intermediates_match_source_receipt"
    ] is False
    assert evaluate_replay_certificate(cfg, certificate)["review_ready_current"] is False


def test_replay_cli_json_exposes_materialized_comparison_and_review_readiness(
        tmp_path, capsys):
    cfg, _contract, _path = _materialized_project(tmp_path)
    source = _run(cfg)

    exit_code = cli_main([
        "--config", str(cfg.config_path), "replay", "--json", "--repeat", "2",
        source["run_id"],
    ])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["review_ready"] is True
    assert payload["certificate"]["comparison"][
        "materialized_intermediates_equal"
    ] is True


def test_run_cli_prints_materialized_intermediate_transition(tmp_path, capsys):
    cfg, _contract, _path = _materialized_project(tmp_path)

    exit_code = cli_main([
        "--config", str(cfg.config_path), "run",
        "--input", "data/raw.csv", "--input", "analysis/pipeline.py",
        "--output", "results/fit.json", "--param", "model=ols",
        "--seed", "numpy=7", "--cwd", str(cfg.root),
        "--pipeline-contract", "claimtrace/primary.pipeline.json",
        "--", sys.executable, "analysis/pipeline.py",
    ])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert (
        "materialized intermediate: data/clean.csv  created  "
        "(pre/post created-or-changed; stage causation not proven)"
    ) in output


def test_legacy_v1_certificate_remains_valid_and_evaluable(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    source = _run(cfg)
    current = replay_run(cfg, source["run_id"])["certificate"]
    legacy = _as_legacy_certificate(current)

    assert legacy["coverage"]["filesystem_writes"] == (
        LEGACY_WORKSPACE_WRITE_COVERAGE
    )
    assert validate_replay_certificate(legacy) == legacy["id"]
    evaluation = evaluate_replay_certificate(cfg, legacy)
    assert evaluation["byte_repeatable_current"] is True
    assert evaluation["review_ready_current"] is True


def test_legacy_v1_lacks_review_ready_evidence_for_declared_intermediate(tmp_path):
    cfg, _contract, _path = _materialized_project(tmp_path)
    source = _run(cfg)
    current = replay_run(cfg, source["run_id"])["certificate"]
    legacy = _as_legacy_certificate(current)

    assert validate_replay_certificate(legacy) == legacy["id"]
    evaluation = evaluate_replay_certificate(cfg, legacy)
    assert evaluation["byte_repeatable_current"] is True
    assert evaluation["review_ready_current"] is False


def test_legacy_contract_source_cannot_be_current_replay_evidence(tmp_path):
    cfg, _contract, _path = _materialized_project(tmp_path)
    source = _run(cfg)
    current = replay_run(cfg, source["run_id"])["certificate"]
    store, legacy_start, legacy_finish = _legacy_source_pair(cfg, source["run_id"])
    legacy = _as_legacy_certificate(current)
    legacy["source_start_event_id"] = legacy_start["id"]
    legacy["source_finish_event_id"] = legacy_finish["id"]
    legacy["computation_id"] = legacy_start["payload"]["plan"]["computation_id"]
    legacy["pipeline_contract_id"] = legacy_start["payload"]["plan"][
        "pipeline_contract"
    ]["id"]
    _readdress(legacy)
    cfg.events_path = store

    assert validate_replay_certificate(legacy) == legacy["id"]
    evaluation = evaluate_replay_certificate(cfg, legacy)

    assert evaluation["current"] is False
    assert evaluation["byte_repeatable_current"] is False
    assert evaluation["review_ready_current"] is False
    assert any(
        item["code"] == "REPLAY_LEGACY_SOURCE_COVERAGE"
        for item in evaluation["findings"]
    )


def test_random_output_is_recorded_as_mismatch_not_determinism(tmp_path):
    cfg, contract, contract_path = _project(tmp_path)
    code = cfg.root / "analysis" / "pipeline.py"
    code.write_text(
        code.read_text(encoding="utf-8").replace(
            "Path('results/fit.json').write_text(str(slope))",
            "Path('results/fit.json').write_bytes(__import__('os').urandom(64))",
        ),
        encoding="utf-8",
    )
    _refresh_anchor(cfg, contract, contract_path)
    source = _run(cfg)

    result = replay_run(cfg, source["run_id"], attempts=3)

    assert result["exit_code"] == 3
    assert result["certificate"]["outcome"] == "repeatability_mismatch"
    assert result["certificate"]["comparison"]["declared_outputs_equal"] is False
    assert validate_replay_certificate(result["certificate"]) == result["certificate"]["id"]


def test_source_input_drift_blocks_replay_before_launch(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    source = _run(cfg)
    (cfg.root / "data" / "raw.csv").write_text("x,y\n2,3\n", encoding="utf-8")

    with pytest.raises(ReplayError, match="source input is stale"):
        replay_run(cfg, source["run_id"])
    assert not cfg.replays_path.exists()


def test_stored_certificate_becomes_noncurrent_after_input_drift(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    source = _run(cfg)
    certificate = replay_run(cfg, source["run_id"])["certificate"]
    (cfg.root / "data" / "raw.csv").write_text("x,y\n9,10\n", encoding="utf-8")

    evaluation = evaluate_replay_certificate(cfg, certificate)

    assert evaluation["current"] is False
    assert evaluation["byte_repeatable_current"] is False
    assert evaluation["review_ready_current"] is False
    assert "REPLAY_INPUT_DRIFT" in {item["code"] for item in evaluation["findings"]}


def test_undeclared_intermediate_file_is_visible_but_does_not_fake_stage_observation(tmp_path):
    cfg, contract, contract_path = _project(tmp_path)
    code = cfg.root / "analysis" / "pipeline.py"
    code.write_text(
        code.read_text(encoding="utf-8")
        + "Path('scratch.txt').write_text('internal file')\n",
        encoding="utf-8",
    )
    _refresh_anchor(cfg, contract, contract_path)
    source = _run(cfg)
    source_scratch = (cfg.root / "scratch.txt").read_bytes()

    result = replay_run(cfg, source["run_id"])

    assert result["exit_code"] == 3
    assert result["review_ready"] is False
    assert result["certificate"]["outcome"] == "byte_repeatable"
    assert all(
        any(item["path"] == "scratch.txt" for item in attempt["undeclared_writes"])
        for attempt in result["certificate"]["attempts"]
    )
    assert result["certificate"]["coverage"]["in_memory_intermediates"] == "not_observed"
    assert result["certificate"]["comparison"]["undeclared_workspace_writes_present"] is True
    evaluation = evaluate_replay_certificate(cfg, result["certificate"])
    assert evaluation["byte_repeatable_current"] is True
    assert evaluation["review_ready_current"] is False
    assert (cfg.root / "scratch.txt").read_bytes() == source_scratch


def test_empty_directory_is_outside_file_path_write_coverage(tmp_path):
    cfg, contract, contract_path = _project(tmp_path)
    code = cfg.root / "analysis" / "pipeline.py"
    code.write_text(
        code.read_text(encoding="utf-8") + "Path('empty-dir').mkdir()\n",
        encoding="utf-8",
    )
    _refresh_anchor(cfg, contract, contract_path)
    source = _run(cfg)

    result = replay_run(cfg, source["run_id"])

    certificate = result["certificate"]
    assert (cfg.root / "empty-dir").is_dir()
    assert certificate["coverage"]["filesystem_writes"] == (
        WORKSPACE_FILE_WRITE_COVERAGE
    )
    assert all(not attempt["undeclared_writes"] for attempt in certificate["attempts"])
    assert certificate["outcome"] == "byte_repeatable"
    assert result["review_ready"] is True


def test_workspace_scan_checks_links_only_at_or_below_its_boundary(tmp_path):
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    alias_parent = tmp_path / "aliased-parent"
    try:
        alias_parent.symlink_to(real_parent, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    workspace = alias_parent / "attempt"
    (workspace / "data").mkdir(parents=True)
    (workspace / "data" / "input.txt").write_text("input\n", encoding="utf-8")
    outside_directory = tmp_path / "outside-directory"
    outside_directory.mkdir()
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("outside\n", encoding="utf-8")
    try:
        (workspace / "linked-directory").symlink_to(
            outside_directory, target_is_directory=True,
        )
        (workspace / "linked-file.txt").symlink_to(outside_file)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"workspace symlinks are unavailable: {exc}")

    scanned = replay_module._scan_workspace(workspace)

    assert scanned["data/input.txt"]["state"] == "stable"
    assert "data" not in scanned
    assert scanned["linked-directory"] == {
        "path": "linked-directory", "state": "unsupported",
    }
    assert scanned["linked-file.txt"] == {
        "path": "linked-file.txt", "state": "unsupported",
    }
    assert not any(path.startswith("linked-directory/") for path in scanned)


def test_nondeterministic_stdout_prevents_byte_repeatable_outcome(tmp_path):
    cfg, contract, contract_path = _project(tmp_path)
    code = cfg.root / "analysis" / "pipeline.py"
    code.write_text(
        code.read_text(encoding="utf-8")
        + "print(__import__('os').urandom(16).hex())\n",
        encoding="utf-8",
    )
    _refresh_anchor(cfg, contract, contract_path)
    source = _run(cfg)

    result = replay_run(cfg, source["run_id"], attempts=3)

    assert result["certificate"]["outcome"] == "repeatability_mismatch"
    assert result["certificate"]["comparison"]["declared_outputs_equal"] is True
    assert result["certificate"]["comparison"]["stdout_equal"] is False


def test_redacted_source_argv_requires_matching_override_without_storing_secret(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    source = run_command(
        cfg, [sys.executable, "analysis/pipeline.py", "--token", "secret-value"],
        inputs=["data/raw.csv", "analysis/pipeline.py"],
        outputs=["results/fit.json"], cwd=str(cfg.root),
        parameters={"model": "ols"}, seeds={"numpy": "7"},
        pipeline_contract="claimtrace/primary.pipeline.json",
        scan_writes=False,
    )
    with pytest.raises(ReplayError, match="redacted values"):
        replay_run(cfg, source["run_id"])

    result = replay_run(
        cfg, source["run_id"],
        command_override=[
            sys.executable, "analysis/pipeline.py", "--token", "secret-value",
        ],
    )
    assert result["exit_code"] == 3
    assert result["review_ready"] is False
    assert result["certificate"]["outcome"] == "byte_repeatable"
    assert result["certificate"]["command"]["secret_override_used_not_stored"] is True
    assert result["certificate"]["command"]["argv"][3] == "[REDACTED]"
    assert "secret-value" not in json.dumps(result["certificate"], sort_keys=True)
    evaluation = evaluate_replay_certificate(cfg, result["certificate"])
    assert evaluation["current"] is False
    assert evaluation["byte_repeatable_current"] is False
    assert evaluation["review_ready_current"] is False
    assert "REPLAY_COMMAND_OVERRIDE_UNVERIFIABLE" in {
        item["code"] for item in evaluation["findings"]
    }

    forged = deepcopy(result["certificate"])
    forged["command"]["secret_override_used_not_stored"] = False
    _readdress(forged)
    with pytest.raises(ReplayError, match="requires an explicit uncommitted-override"):
        validate_replay_certificate(forged)


def test_unredacted_source_rejects_an_unnecessary_command_override(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    source = _run(cfg)

    with pytest.raises(ReplayError, match="allowed only for a redacted source argv"):
        replay_run(
            cfg, source["run_id"],
            command_override=[sys.executable, "analysis/pipeline.py"],
        )


def test_readdressed_argv_or_cwd_is_not_current_source_command_evidence(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    source = _run(cfg)
    base = replay_run(cfg, source["run_id"])["certificate"]
    for field in ("argv", "cwd"):
        forged = deepcopy(base)
        if field == "argv":
            forged["command"]["argv"][-1] = "analysis/not-the-source.py"
        else:
            forged["command"]["cwd"] = "analysis"
        _readdress(forged)

        assert validate_replay_certificate(forged) == forged["id"]
        evaluation = evaluate_replay_certificate(cfg, forged)
        assert evaluation["current"] is False
        assert evaluation["byte_repeatable_current"] is False
        assert evaluation["review_ready_current"] is False
        assert "REPLAY_COMMAND_SOURCE_MISMATCH" in {
            item["code"] for item in evaluation["findings"]
        }


def test_supplied_source_pair_with_readdressed_input_baseline_is_not_current(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    source = _run(cfg)
    certificate = replay_run(cfg, source["run_id"])["certificate"]
    events, integrity_issues = events_module.load_events(cfg.events_path)
    runs, pair_issues = events_module.materialize_runs(events)
    assert integrity_issues == []
    assert pair_issues == []
    run = next(item for item in runs if item["run_id"] == source["run_id"])
    start = deepcopy(run["start"])
    finish = deepcopy(run["finish"])

    transition = finish["payload"]["input_transitions"][0]
    fake_sha = "0" * 64
    fake_snapshot = {
        "path": transition["path"],
        "state": "stable",
        "sha256": fake_sha,
        "size": transition["before"]["size"],
        "file_version_id": f"file:sha256:{fake_sha}",
    }
    transition["before"] = deepcopy(fake_snapshot)
    transition["after"] = deepcopy(fake_snapshot)
    payload = finish["payload"]
    result_basis = {
        "plan_id": payload["plan_id"],
        "outcome": payload["outcome"],
        "direct_child_returncode": payload["direct_child_returncode"],
        "input_transitions": [
            events_module._fingerprint_transition(item)
            for item in payload["input_transitions"]
        ],
        "output_transitions": [
            events_module._fingerprint_transition(item)
            for item in payload["output_transitions"]
        ],
        "contract_errors": payload["contract_errors"],
        "intermediate_transitions": [
            events_module._fingerprint_transition(item)
            for item in payload["intermediate_transitions"]
        ],
    }
    payload["result_id"] = (
        "result:sha256:" + events_module.canonical_sha256(result_basis)
    )
    forged_finish = events_module.make_event(
        "run.finished", finish["run_id"], payload,
        recorded_at=finish["recorded_at"],
        schema_version=finish["schema_version"],
    )
    events_module.validate_event(forged_finish)

    evaluation = evaluate_replay_certificate(
        cfg, certificate, start=start, finish=forged_finish,
    )

    assert evaluation["current"] is False
    assert evaluation["byte_repeatable_current"] is False
    assert evaluation["review_ready_current"] is False
    assert "REPLAY_SOURCE_PAIR_INVALID" in {
        item["code"] for item in evaluation["findings"]
    }


def test_readdressed_override_and_argv_capture_fields_are_rejected(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    source = _run(cfg)
    base = replay_run(cfg, source["run_id"])["certificate"]

    forged_override = deepcopy(base)
    forged_override["command"]["secret_override_used_not_stored"] = True
    _readdress(forged_override)
    with pytest.raises(ReplayError, match="override flag contradicts"):
        validate_replay_certificate(forged_override)

    forged_capture = deepcopy(base)
    forged_capture["command"]["argv_capture"] = "unredacted"
    _readdress(forged_capture)
    with pytest.raises(ReplayError, match="unsupported replay argv capture mode"):
        validate_replay_certificate(forged_capture)


def test_replay_certificate_cannot_overclaim_observed_stages(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    source = _run(cfg)
    certificate = replay_run(cfg, source["run_id"])["certificate"]
    certificate["coverage"]["internal_stage_execution"] = "observed"
    with pytest.raises(ReplayError, match="id does not match"):
        validate_replay_certificate(certificate)


def test_readdressed_succeeded_attempt_with_nonzero_returncode_is_rejected(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    source = _run(cfg)
    certificate = deepcopy(replay_run(cfg, source["run_id"])["certificate"])
    certificate["attempts"][0]["direct_child_returncode"] = 7
    _readdress(certificate)

    with pytest.raises(
            ReplayError, match="attempt outcome contradicts direct execution evidence"):
        validate_replay_certificate(certificate)


def test_readdressed_attempt_outcome_variants_must_match_direct_evidence(tmp_path):
    cfg, _contract, _path = _project(tmp_path)
    source = _run(cfg)
    base = replay_run(cfg, source["run_id"])["certificate"]
    cases = [
        ({"outcome": "failed"}, False),
        ({"outcome": "launch_error", "direct_child_returncode": None}, False),
        ({
            "outcome": "timed_out",
            "direct_child_returncode": None,
            "launch_error": {"type": "OSError", "detail": "synthetic"},
        }, False),
        ({"outcome": "output_missing_or_unstable"}, False),
        ({"outcome": "succeeded"}, True),
    ]
    for updates, make_output_missing in cases:
        certificate = deepcopy(base)
        attempt = certificate["attempts"][0]
        attempt.update(updates)
        if make_output_missing:
            attempt["outputs"][0] = {
                "path": attempt["outputs"][0]["path"],
                "state": "missing",
            }
        _readdress(certificate)

        with pytest.raises(
                ReplayError,
                match="attempt outcome contradicts direct execution evidence"):
            validate_replay_certificate(certificate)
