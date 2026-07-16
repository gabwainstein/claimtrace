"""Tests for the Phase-0 hardening: structural integrity, graceful errors, and lint."""
import hashlib
import json
import multiprocessing as mp
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import claimtrace.cli as cli_module
import claimtrace.events as events_module
from claimtrace import engine
from claimtrace.cli import main
from claimtrace.config import Config, MAX_JSON_NESTING_DEPTH, strict_json_loads
from claimtrace.snapshot import snapshot


def _process_log(config_path, node_id, start, results):
    cfg = Config(config_path)
    start.wait()
    results.put(engine.log_entry(cfg, {
        "node": {"id": node_id, "type": "experiment", "status": "null"},
        "edges": [],
    }))


def _project(tmp_path, graph, config=None):
    (tmp_path / "claimtrace").mkdir()
    (tmp_path / "claimtrace.config.json").write_text(
        json.dumps(config or {"root": ".", "graph": "claimtrace/graph.json"}))
    (tmp_path / "claimtrace" / "graph.json").write_text(
        graph if isinstance(graph, str) else json.dumps(graph))
    return Config(tmp_path / "claimtrace.config.json")


def _make_directory_link(target, link):
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError) as exc:
        if os.name != "nt":
            pytest.skip(f"directory links are unavailable on this platform: {exc}")
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if created.returncode != 0:
            pytest.skip(f"directory links and junctions are unavailable: {exc}")


def _remove_directory_link(link):
    if link.is_symlink():
        link.unlink()
    else:
        os.rmdir(link)


def test_graph_lock_rejects_linked_private_runtime_root(tmp_path, monkeypatch):
    temporary_base = tmp_path / "temporary"
    external = tmp_path / "external"
    temporary_base.mkdir()
    external.mkdir()
    suffix = f"-{os.getuid()}" if hasattr(os, "getuid") else ""
    linked_root = temporary_base / f"claimtrace-event-locks{suffix}"
    _make_directory_link(external, linked_root)
    monkeypatch.setattr(
        events_module.tempfile, "gettempdir", lambda: str(temporary_base),
    )
    try:
        with pytest.raises(engine.GraphError, match="path traverses a link or reparse point"):
            with engine._graph_lock(tmp_path / "graph.json"):
                pass
    finally:
        _remove_directory_link(linked_root)


def test_packaged_skill_installs_both_layouts_without_silent_overwrite(tmp_path, capsys):
    repository = Path(__file__).resolve().parents[1]
    canonical_root = repository / "src" / "claimtrace" / "templates" / "claimtrace-log"
    source_roots = [
        repository / ".agents" / "skills" / "claimtrace-log",
        repository / ".claude" / "skills" / "claimtrace-log",
    ]
    manifest = ("SKILL.md", "references/semantic-authoring.md")
    expected_files = {
        relative: (canonical_root / relative).read_text(encoding="utf-8")
        for relative in manifest
    }
    for source_root in source_roots:
        for relative, expected_content in expected_files.items():
            assert (source_root / relative).read_text(encoding="utf-8") == expected_content
    expected = expected_files["SKILL.md"]
    assert "do not add a direct `supports` edge" in expected
    assert "assess <proposal.json> --actor <agent-id> --json" in expected
    assert "Never provide `mechanical_snapshot`, `derived`" in expected
    assert '`provenance.agent` is required' in expected
    assert 'never use the literal string `"not stated"`' in expected
    assert '"rel": "supports"' not in expected
    assert "command-scoped source fallback" in expected
    assert "automatically classified as a materialized" in expected
    assert "does not identify which stage wrote the file" in expected
    assert "pathless internal stage output remains unobserved" in expected
    assert "Legacy replay certificates" in expected
    assert '"candidate_query"' in expected_files[
        "references/semantic-authoring.md"
    ]

    assert main(["install-skill", "--dir", str(tmp_path)]) == 0
    agents = tmp_path / ".agents" / "skills" / "claimtrace-log" / "SKILL.md"
    claude = tmp_path / ".claude" / "skills" / "claimtrace-log" / "SKILL.md"
    agents_reference = agents.parent / "references" / "semantic-authoring.md"
    claude_reference = claude.parent / "references" / "semantic-authoring.md"
    assert agents.read_text(encoding="utf-8") == expected
    assert claude.read_text(encoding="utf-8") == expected
    assert agents_reference.read_text(encoding="utf-8") == expected_files[
        "references/semantic-authoring.md"
    ]
    assert claude_reference.read_text(encoding="utf-8") == expected_files[
        "references/semantic-authoring.md"
    ]
    assert "installed" in capsys.readouterr().out

    agents.write_text("project-specific skill\n", encoding="utf-8")
    assert main([
        "install-skill", "--dir", str(tmp_path), "--target", "agents",
    ]) == 1
    assert agents.read_text(encoding="utf-8") == "project-specific skill\n"
    assert "refusing to overwrite" in capsys.readouterr().err

    assert main([
        "install-skill", "--dir", str(tmp_path), "--target", "agents", "--force",
    ]) == 0
    assert agents.read_text(encoding="utf-8") == expected

    extra = agents.parent / "references" / "project-specific.md"
    extra.write_text("preserve me\n", encoding="utf-8")
    agents_reference.write_text("project-specific reference\n", encoding="utf-8")
    assert main([
        "install-skill", "--dir", str(tmp_path), "--target", "agents",
    ]) == 1
    assert agents_reference.read_text(encoding="utf-8") == "project-specific reference\n"
    assert main([
        "install-skill", "--dir", str(tmp_path), "--target", "agents", "--force",
    ]) == 0
    assert agents_reference.read_text(encoding="utf-8") == expected_files[
        "references/semantic-authoring.md"
    ]
    assert extra.read_text(encoding="utf-8") == "preserve me\n"


def test_concurrent_force_skill_installs_remain_bundle_coherent(
        tmp_path, monkeypatch):
    local = threading.local()
    start = threading.Barrier(2)
    guard = threading.Lock()
    active_versions = set()
    overlaps = []
    real_publish = cli_module._publish_text

    def packaged_files():
        version = local.version
        return {
            "SKILL.md": f"{version}-skill\n",
            "references/semantic-authoring.md": f"{version}-reference\n",
        }

    def observed_publish(*args, **kwargs):
        version = local.version
        with guard:
            active_versions.add(version)
            if len(active_versions) > 1:
                overlaps.append(tuple(sorted(active_versions)))
        try:
            time.sleep(0.04)
            return real_publish(*args, **kwargs)
        finally:
            with guard:
                active_versions.remove(version)

    monkeypatch.setattr(cli_module, "_packaged_skill_files", packaged_files)
    monkeypatch.setattr(cli_module, "_publish_text", observed_publish)

    def install(version):
        local.version = version
        start.wait()
        return cli_module.cmd_install_skill(SimpleNamespace(
            dir=str(tmp_path), target="agents", force=True,
        ))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(install, ("A", "B")))

    root = tmp_path / ".agents" / "skills" / "claimtrace-log"
    installed = (
        (root / "SKILL.md").read_text(encoding="utf-8"),
        (root / "references" / "semantic-authoring.md").read_text(encoding="utf-8"),
    )
    assert results == [0, 0]
    assert overlaps == []
    assert installed in {
        ("A-skill\n", "A-reference\n"),
        ("B-skill\n", "B-reference\n"),
    }


def test_concurrent_force_init_remains_scaffold_coherent(tmp_path, monkeypatch):
    local = threading.local()
    start = threading.Barrier(2)
    guard = threading.Lock()
    active_versions = set()
    overlaps = []
    real_publish = cli_module._publish_text

    def versioned_publish(base, destination, _content, *, force, label):
        version = local.version
        with guard:
            active_versions.add(version)
            if len(active_versions) > 1:
                overlaps.append(tuple(sorted(active_versions)))
        try:
            time.sleep(0.04)
            return real_publish(
                base, destination, f"{version}:{label}\n", force=force, label=label,
            )
        finally:
            with guard:
                active_versions.remove(version)

    monkeypatch.setattr(cli_module, "_publish_text", versioned_publish)
    project = tmp_path / "project"

    def initialise(version):
        local.version = version
        start.wait()
        return cli_module.cmd_init(SimpleNamespace(dir=str(project), force=True))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(initialise, ("A", "B")))

    installed = [
        (project / "claimtrace.config.json").read_text(encoding="utf-8"),
        (project / "claimtrace" / "graph.json").read_text(encoding="utf-8"),
    ]
    assert results == [0, 0]
    assert overlaps == []
    assert {item.split(":", 1)[0] for item in installed} in ({"A"}, {"B"})


def test_default_init_is_a_green_planning_project_without_invented_assets(
        tmp_path, capsys):
    project = tmp_path / "planning-project"
    config_path = project / "claimtrace.config.json"

    assert main(["init", str(project)]) == 0
    init_output = capsys.readouterr().out
    config = json.loads(config_path.read_text(encoding="utf-8"))
    graph = json.loads(
        (project / "claimtrace" / "graph.json").read_text(encoding="utf-8")
    )

    assert "initialised planning claimtrace" in init_output
    assert config["verifiers"] is None
    assert config["execution"]["require_stage_checkpoints"] is False
    assert graph == {
        "schema_version": "1.0", "concepts": {}, "nodes": [], "edges": [],
    }
    assert not (project / "claimtrace" / "verifiers.py").exists()
    assert not (project / "data").exists()
    assert "data/raw.csv" not in config_path.read_text(encoding="utf-8")

    assert main([
        "--config", str(config_path), "check", "--strict", "--json",
    ]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["summary"]["blocking"] == 0

    assert main(["--config", str(config_path), "verify"]) == 2
    verify_output = capsys.readouterr().out
    assert "NOT_CONFIGURED" in verify_output
    assert "no verifiers configured" in verify_output


def test_explicit_init_example_is_complete_and_runnable(tmp_path, capsys):
    project = tmp_path / "example-project"
    config_path = project / "claimtrace.config.json"

    assert main(["init", str(project), "--example"]) == 0
    init_output = capsys.readouterr().out
    config = json.loads(config_path.read_text(encoding="utf-8"))
    graph = json.loads(
        (project / "claimtrace" / "graph.json").read_text(encoding="utf-8")
    )

    assert "initialised example claimtrace" in init_output
    assert config["verifiers"] == "claimtrace/verifiers.py"
    assert graph["nodes"][0]["path"] == "data/claimtrace-example.csv"
    assert (project / graph["nodes"][0]["path"]).read_text(encoding="utf-8") == (
        "measurement\n1\n2\n"
    )
    assert (project / "claimtrace" / "verifiers.py").is_file()

    assert main(["--config", str(config_path), "verify"]) == 0
    verify_output = capsys.readouterr().out
    assert "[PASS] example: row count" in verify_output

    assert main([
        "--config", str(config_path), "check", "--strict", "--json",
    ]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["summary"]["blocking"] == 0


def test_configured_verifier_with_no_registered_checks_fails_closed(tmp_path, capsys):
    project = tmp_path / "empty-verifier-project"
    config_path = project / "claimtrace.config.json"

    assert main(["init", str(project), "--example"]) == 0
    capsys.readouterr()
    (project / "claimtrace" / "verifiers.py").write_text(
        "# Intentionally registers no checks.\n", encoding="utf-8",
    )

    assert main(["--config", str(config_path), "verify"]) == 2
    verify_output = capsys.readouterr().out
    assert "NO_CHECKS" in verify_output
    assert "registered zero checks" in verify_output


def test_registered_verifier_failure_remains_nonzero(tmp_path, capsys):
    project = tmp_path / "failing-verifier-project"
    config_path = project / "claimtrace.config.json"

    assert main(["init", str(project), "--example"]) == 0
    capsys.readouterr()
    (project / "claimtrace" / "verifiers.py").write_text(
        "from claimtrace import check\n\n"
        "@check('intentional drift')\n"
        "def drift():\n"
        "    return False, 'live=changed', 'live=expected'\n",
        encoding="utf-8",
    )

    assert main(["--config", str(config_path), "verify"]) == 1
    verify_output = capsys.readouterr().out
    assert "[FAIL] intentional drift" in verify_output
    assert "1/1 check(s) drifted" in verify_output


def test_install_skill_final_verification_detects_bundle_drift(
        tmp_path, monkeypatch, capsys):
    real_publish = cli_module._publish_text

    def drifting_publish(base, destination, content, **kwargs):
        state = real_publish(base, destination, content, **kwargs)
        if Path(destination).name == "semantic-authoring.md":
            Path(destination).parent.parent.joinpath("SKILL.md").write_text(
                "drifted after publication\n", encoding="utf-8",
            )
        return state

    monkeypatch.setattr(cli_module, "_publish_text", drifting_publish)
    assert main([
        "install-skill", "--dir", str(tmp_path), "--target", "agents", "--force",
    ]) == 1
    assert "final bundle verification failed" in capsys.readouterr().err


def test_no_force_publication_explains_hardlink_requirement(
        tmp_path, monkeypatch, capsys):
    def unavailable_hardlink(*_args, **_kwargs):
        raise OSError("hard links disabled by filesystem policy")

    monkeypatch.setattr(cli_module.os, "link", unavailable_hardlink)
    assert main([
        "install-skill", "--dir", str(tmp_path), "--target", "agents",
    ]) == 1
    assert "must support same-filesystem hard links" in capsys.readouterr().err


def test_install_skill_rejects_directory_link_escape(tmp_path, capsys):
    project = tmp_path / "project"
    external = tmp_path / "external-skill-target"
    project.mkdir()
    external.mkdir()
    link = project / ".agents"
    _make_directory_link(external, link)

    try:
        assert main([
            "install-skill", "--dir", str(project), "--target", "agents",
        ]) == 1
        assert list(external.iterdir()) == []
        assert "refusing unsafe path" in capsys.readouterr().err
    finally:
        _remove_directory_link(link)


def test_init_rejects_claimtrace_directory_link_escape(tmp_path, capsys):
    project = tmp_path / "project"
    external = tmp_path / "external-init-target"
    project.mkdir()
    external.mkdir()
    link = project / "claimtrace"
    _make_directory_link(external, link)

    try:
        assert main(["init", str(project)]) == 1
        assert not (project / "claimtrace.config.json").exists()
        assert list(external.iterdir()) == []
        assert "refusing unsafe path" in capsys.readouterr().err
    finally:
        _remove_directory_link(link)


def test_init_example_rejects_data_directory_link_escape(tmp_path, capsys):
    project = tmp_path / "project"
    external = tmp_path / "external-example-target"
    project.mkdir()
    external.mkdir()
    link = project / "data"
    _make_directory_link(external, link)

    try:
        assert main(["init", str(project), "--example"]) == 1
        assert not (project / "claimtrace.config.json").exists()
        assert list(external.iterdir()) == []
        assert "refusing unsafe path" in capsys.readouterr().err
    finally:
        _remove_directory_link(link)


def test_install_skill_no_force_race_preserves_competing_file(
        tmp_path, monkeypatch, capsys):
    target = tmp_path / ".agents" / "skills" / "claimtrace-log" / "SKILL.md"
    real_link = os.link
    raced = False

    def competing_link(source, destination, *args, **kwargs):
        nonlocal raced
        if Path(destination) == target and not raced:
            raced = True
            target.write_text("concurrent project skill\n", encoding="utf-8")
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(cli_module.os, "link", competing_link)
    assert main([
        "install-skill", "--dir", str(tmp_path), "--target", "agents",
    ]) == 1
    assert raced is True
    assert target.read_text(encoding="utf-8") == "concurrent project skill\n"
    assert "refusing to overwrite" in capsys.readouterr().err


def test_init_no_force_race_preserves_competing_config(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    target = project / "claimtrace.config.json"
    real_link = os.link
    raced = False

    def competing_link(source, destination, *args, **kwargs):
        nonlocal raced
        if Path(destination) == target and not raced:
            raced = True
            target.write_text("concurrent config\n", encoding="utf-8")
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(cli_module.os, "link", competing_link)
    assert main(["init", str(project)]) == 1
    assert raced is True
    assert target.read_text(encoding="utf-8") == "concurrent config\n"
    assert "refusing to overwrite config" in capsys.readouterr().err
    assert not (project / "claimtrace" / "graph.json").exists()


def test_no_force_preflight_conflicts_do_not_create_other_scaffold_paths(
        tmp_path, capsys):
    project = tmp_path / "project"
    skill_root = project / ".agents" / "skills" / "claimtrace-log"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("project skill\n", encoding="utf-8")

    assert main([
        "install-skill", "--dir", str(project), "--target", "agents",
    ]) == 1
    assert not (skill_root / "references").exists()
    assert "refusing to overwrite" in capsys.readouterr().err

    config = project / "claimtrace.config.json"
    config.write_text("existing config\n", encoding="utf-8")
    assert main(["init", str(project)]) == 1
    assert not (project / "claimtrace").exists()


def test_cli_json_inputs_are_bounded_and_recursion_safe(
        tmp_path, monkeypatch, capsys):
    cfg = _project(tmp_path, {
        "schema_version": "1.0", "nodes": [], "edges": [], "concepts": {},
    })
    deep = tmp_path / "deep.json"
    deep.write_text("[" * 5000 + "0" + "]" * 5000, encoding="utf-8")

    assert main([
        "--config", str(cfg.config_path), "log", str(deep),
    ]) == 2
    error = capsys.readouterr().err
    assert "cannot read log entry" in error
    assert "JSON nesting exceeds the 256-level limit" in error
    assert main([
        "--config", str(cfg.config_path), "assess", str(deep),
        "--actor", "agent:test",
    ]) == 2
    error = capsys.readouterr().err
    assert "cannot read assessment proposal" in error
    assert "JSON nesting exceeds the 256-level limit" in error

    shallow_list = tmp_path / "shallow-list.json"
    shallow_list.write_text("[]", encoding="utf-8")
    assert main([
        "--config", str(cfg.config_path), "log", str(shallow_list),
    ]) == 2
    error = capsys.readouterr().err
    assert "log entry must contain one JSON object" in error
    assert "cannot read log entry" not in error

    monkeypatch.setattr(cli_module, "MAX_CLI_JSON_INPUT_BYTES", 1024)
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * 1025)
    assert main([
        "--config", str(cfg.config_path), "log", str(oversized),
    ]) == 2
    assert "byte limit" in capsys.readouterr().err


def test_strict_json_nesting_limit_is_fixed_and_string_aware():
    at_limit = "[" * MAX_JSON_NESTING_DEPTH + "0" + "]" * MAX_JSON_NESTING_DEPTH
    value = strict_json_loads(at_limit)
    for _ in range(MAX_JSON_NESTING_DEPTH):
        assert isinstance(value, list) and len(value) == 1
        value = value[0]
    assert value == 0

    over_limit = "[" * (MAX_JSON_NESTING_DEPTH + 1) + "0" + "]" * (
        MAX_JSON_NESTING_DEPTH + 1
    )
    with pytest.raises(ValueError, match="JSON nesting exceeds the 256-level limit"):
        strict_json_loads(over_limit)

    structural_text = "[" * 5000 + "]" * 5000 + '\\"' + "\\\\"
    assert strict_json_loads(json.dumps({"text": structural_text})) == {
        "text": structural_text,
    }


def test_logic_config_is_project_scoped_and_requires_explicit_external_opt_in(tmp_path):
    graph = {"schema_version": "1.0", "nodes": [], "edges": [], "concepts": {}}
    cfg = _project(tmp_path, graph, config={
        "root": ".",
        "graph": "claimtrace/graph.json",
        "logic": {
            "derivations": "claimtrace/derivations",
            "vocabularies": ["claimtrace/logic/vocabulary.json"],
            "rule_packs": ["claimtrace/logic/rules.json"],
            "require_derivations": True,
        },
    })
    assert cfg.derivations_path == (tmp_path / "claimtrace" / "derivations").resolve()
    assert cfg.logic_vocabulary_paths == [
        (tmp_path / "claimtrace" / "logic" / "vocabulary.json").resolve()
    ]
    assert cfg.require_derivations is True

    config = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    config["logic"]["vocabularies"] = ["../external-vocabulary.json"]
    cfg.config_path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(SystemExit, match="logic path escapes"):
        Config(cfg.config_path)

    config["logic"]["allow_external_packs"] = True
    cfg.config_path.write_text(json.dumps(config), encoding="utf-8")
    allowed = Config(cfg.config_path)
    assert allowed.logic_vocabulary_paths == [
        (tmp_path.parent / "external-vocabulary.json").resolve()
    ]

    config["logic"]["vocabularies"] = [
        "claimtrace/logic/vocabulary.json", "claimtrace/logic/vocabulary.json",
    ]
    cfg.config_path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(SystemExit, match="duplicate paths"):
        Config(cfg.config_path)


def test_semantics_config_is_strict_project_scoped_and_explicit(tmp_path):
    graph = {"schema_version": "1.0", "nodes": [], "edges": [], "concepts": {}}
    cfg = _project(tmp_path, graph, config={
        "root": ".",
        "graph": "claimtrace/graph.json",
        "semantics": {
            "terminologies": ["claimtrace/semantics/local.json"],
            "ontology_locks": ["claimtrace/semantics/example.lock.json"],
            "mappings": "claimtrace/semantics/mappings",
            "policies": "claimtrace/semantics/policies",
            "active_policy": "semantic-policy:sha256:" + "a" * 64,
            "require_active_policy": True,
            "language": "en",
            "max_candidates": 50,
            "max_ontology_bytes": 1048576,
        },
    })
    assert cfg.semantic_terminology_paths == [
        (tmp_path / "claimtrace" / "semantics" / "local.json").resolve()
    ]
    assert cfg.semantic_ontology_lock_paths == [
        (tmp_path / "claimtrace" / "semantics" / "example.lock.json").resolve()
    ]
    assert cfg.semantic_mappings_path == (
        tmp_path / "claimtrace" / "semantics" / "mappings"
    ).resolve()
    assert cfg.require_active_semantic_policy is True
    assert cfg.semantic_max_candidates == 50
    assert cfg.semantic_max_ontology_bytes == 1048576

    config = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    config["semantics"]["terminologies"] = ["../external-terms.json"]
    cfg.config_path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(SystemExit, match="semantics path escapes"):
        Config(cfg.config_path)

    config["semantics"]["allow_external_sources"] = True
    cfg.config_path.write_text(json.dumps(config), encoding="utf-8")
    allowed = Config(cfg.config_path)
    assert allowed.semantic_terminology_paths == [
        (tmp_path.parent / "external-terms.json").resolve()
    ]

    config["semantics"]["ontology_locks"] = [
        "claimtrace/semantics/example.lock.json",
        "claimtrace/semantics/example.lock.json",
    ]
    cfg.config_path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(SystemExit, match="duplicate paths"):
        Config(cfg.config_path)


@pytest.mark.parametrize("field,value,message", [
    ("max_candidates", True, "integer from 1 to 1000"),
    ("max_candidates", 0, "integer from 1 to 1000"),
    ("max_ontology_bytes", True, "positive integer"),
    ("max_ontology_bytes", 274877906945, "positive integer"),
    ("active_policy", "", "null or an exact semantic-policy"),
    ("active_policy", "claimtrace/semantics/active.json", "null or an exact semantic-policy"),
    ("language", "", "non-empty string"),
    ("language", "@@@", "BCP-47-style"),
    ("require_active_policy", 1, "must be a boolean"),
])
def test_semantics_config_rejects_ambiguous_scalar_values(
        tmp_path, field, value, message):
    graph = {"schema_version": "1.0", "nodes": [], "edges": [], "concepts": {}}
    config = {
        "root": ".", "graph": "claimtrace/graph.json",
        "semantics": {field: value},
    }
    (tmp_path / "claimtrace").mkdir()
    config_path = tmp_path / "claimtrace.config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "claimtrace" / "graph.json").write_text(
        json.dumps(graph), encoding="utf-8",
    )
    with pytest.raises(SystemExit, match=message):
        Config(config_path)


@pytest.mark.parametrize("semantics,message", [
    ({"ontology_locks": ["claimtrace/graph.json"]}, "overlaps a protected"),
    ({"mappings": "claimtrace/events"}, "distinct non-nested"),
    ({"terminologies": ["claimtrace/semantics/mappings/terms.json"]},
     "inside a provenance store"),
])
def test_semantics_config_rejects_policy_and_provenance_path_overlap(
        tmp_path, semantics, message):
    graph = {"schema_version": "1.0", "nodes": [], "edges": [], "concepts": {}}
    config = {
        "root": ".", "graph": "claimtrace/graph.json",
        "events": "claimtrace/events", "semantics": semantics,
    }
    with pytest.raises(SystemExit, match=message):
        _project(tmp_path, graph, config=config)


def test_semantics_config_rejects_control_paths_and_excessive_counts(tmp_path):
    graph = {"schema_version": "1.0", "nodes": [], "edges": [], "concepts": {}}
    trace = tmp_path / "claimtrace"
    trace.mkdir()
    (trace / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    config_path = tmp_path / "claimtrace.config.json"

    config_path.write_text(json.dumps({
        "root": ".", "graph": "claimtrace/graph.json",
        "semantics": {"terminologies": ["bad\u0000path"]},
    }), encoding="utf-8")
    with pytest.raises(SystemExit, match="without controls"):
        Config(config_path)

    config_path.write_text(json.dumps({
        "root": ".", "graph": "claimtrace/graph.json",
        "semantics": {"mappings": "bad\ud800path"},
    }), encoding="utf-8")
    with pytest.raises(SystemExit, match="Unicode surrogates"):
        Config(config_path)

    config_path.write_text(json.dumps({
        "root": ".", "graph": "claimtrace/graph.json",
        "semantics": {"ontology_locks": [f"locks/{index}.json" for index in range(257)]},
    }), encoding="utf-8")
    with pytest.raises(SystemExit, match="256-path limit"):
        Config(config_path)


def test_unknown_semantics_key_is_terminal_safe(tmp_path):
    graph = {"schema_version": "1.0", "nodes": [], "edges": [], "concepts": {}}
    trace = tmp_path / "claimtrace"
    trace.mkdir()
    (trace / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    config_path = tmp_path / "claimtrace.config.json"
    config_path.write_text(json.dumps({
        "root": ".", "graph": "claimtrace/graph.json",
        "semantics": {"\u001b[31mspoof": True},
    }), encoding="utf-8")

    with pytest.raises(SystemExit) as caught:
        Config(config_path)
    assert "\u001b" not in str(caught.value)
    assert "\\u001b[31mspoof" in str(caught.value)


def test_config_read_is_bounded_and_recursion_errors_are_clean(tmp_path):
    config_path = tmp_path / "claimtrace.config.json"
    config_path.write_text("[" * 5000 + "]" * 5000, encoding="utf-8")
    with pytest.raises(
        SystemExit, match="not valid JSON.*JSON nesting exceeds the 256-level limit",
    ):
        Config(config_path)

    config_path.write_bytes(b" " * (2 * 1024 * 1024 + 1))
    with pytest.raises(SystemExit, match="byte limit"):
        Config(config_path)


# --------------------------------------------------------------------------- structural integrity

def test_dangling_edge_is_caught(tmp_path):
    cfg = _project(tmp_path, {
        "nodes": [{"id": "a", "type": "data", "status": "current"}],
        "edges": [{"from": "a", "to": "ghost", "rel": "produces"}]})
    kinds = {k for k, _, _ in engine.structural_issues(cfg)}
    assert "DANGLING_EDGE" in kinds


def test_duplicate_id_is_caught(tmp_path):
    cfg = _project(tmp_path, {
        "nodes": [{"id": "a", "type": "data", "status": "current"},
                  {"id": "a", "type": "artifact", "status": "current"}],
        "edges": []})
    issues = engine.structural_issues(cfg)
    assert any(k == "DUPLICATE_ID" and nid == "a" for k, nid, _ in issues)


def test_cycle_is_detected(tmp_path):
    # a -> b -> c -> a  (all `produces`, so each depends on the previous)
    cfg = _project(tmp_path, {
        "nodes": [{"id": x, "type": "artifact", "status": "current"} for x in ("a", "b", "c")],
        "edges": [{"from": "a", "to": "b", "rel": "produces"},
                  {"from": "b", "to": "c", "rel": "produces"},
                  {"from": "c", "to": "a", "rel": "produces"}]})
    kinds = {k for k, _, _ in engine.structural_issues(cfg)}
    assert "CYCLE" in kinds


def test_self_edge_is_caught(tmp_path):
    cfg = _project(tmp_path, {
        "nodes": [{"id": "a", "type": "data", "status": "current"}],
        "edges": [{"from": "a", "to": "a", "rel": "produces"}]})
    kinds = {k for k, _, _ in engine.structural_issues(cfg)}
    assert "SELF_EDGE" in kinds


def test_dag_has_no_cycle(tmp_path):
    cfg = _project(tmp_path, {
        "nodes": [{"id": x, "type": "artifact", "status": "current"} for x in ("a", "b", "c")],
        "edges": [{"from": "a", "to": "b", "rel": "produces"},
                  {"from": "b", "to": "c", "rel": "produces"}]})
    assert engine.structural_issues(cfg) == []


def test_research_trajectory_vocabulary_is_standard(tmp_path):
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "nodes": [
            {"id": "q", "type": "question", "status": "current"},
            {"id": "h", "type": "hypothesis", "status": "current"},
            {"id": "p", "type": "prediction", "status": "current"},
            {"id": "e", "type": "experiment", "status": "confirmed"},
            {"id": "c", "type": "conclusion", "status": "confirmed"},
        ],
        "edges": [
            {"from": "q", "to": "h", "rel": "motivates"},
            {"from": "h", "to": "p", "rel": "predicts"},
            {"from": "p", "to": "e", "rel": "tested_by"},
            {"from": "e", "to": "c", "rel": "concludes"},
        ],
    })
    kinds = {kind for kind, _node, _detail in engine.lint_issues(cfg)}
    assert "UNKNOWN_TYPE" not in kinds
    assert "UNKNOWN_REL" not in kinds


def test_structural_issues_surface_in_check(tmp_path):
    cfg = _project(tmp_path, {
        "nodes": [{"id": "a", "type": "data", "status": "current"}],
        "edges": [{"from": "a", "to": "ghost", "rel": "produces"}]})
    problems, _ = engine.compute_check(cfg)
    assert any(k == "DANGLING_EDGE" for k, _, _ in problems)


# --------------------------------------------------------------------------- graceful errors

def test_malformed_json_raises_graph_error(tmp_path):
    cfg = _project(tmp_path, "{ this is not valid json ")
    with pytest.raises(engine.GraphError):
        engine.load_graph(cfg)


@pytest.mark.parametrize("graph", [
    '{"nodes": [], "nodes": [], "edges": []}',
    '{"nodes": [{"id": "x", "value": NaN}], "edges": []}',
])
def test_noncanonical_json_is_rejected(tmp_path, graph):
    cfg = _project(tmp_path, graph)
    with pytest.raises(engine.GraphError):
        engine.load_graph(cfg)


def test_missing_nodes_key_raises_graph_error(tmp_path):
    cfg = _project(tmp_path, {"edges": []})
    with pytest.raises(engine.GraphError):
        engine.load_graph(cfg)


def test_node_without_id_raises_graph_error(tmp_path):
    cfg = _project(tmp_path, {"nodes": [{"type": "data"}], "edges": []})
    with pytest.raises(engine.GraphError):
        engine.load_graph(cfg)


@pytest.mark.parametrize("node", [
    {"id": [], "type": "data"},
    {"id": "x", "type": "data", "path": ["not", "a", "path"]},
])
def test_invalid_node_field_types_raise_graph_error(tmp_path, node):
    cfg = _project(tmp_path, {"nodes": [node], "edges": []})
    with pytest.raises(engine.GraphError):
        engine.load_graph(cfg)


def test_non_string_edge_fields_are_malformed_not_a_traceback(tmp_path):
    cfg = _project(tmp_path, {
        "nodes": [{"id": "a", "type": "data", "status": "current"}],
        "edges": [{"from": ["a"], "to": "a", "rel": "produces"}],
    })
    issues = engine.structural_issues(cfg)
    assert any(kind == "MALFORMED_EDGE" for kind, _node, _detail in issues)
    problems, _pending = engine.compute_check(cfg)
    assert any(kind == "MALFORMED_EDGE" for kind, _node, _detail in problems)


def test_plain_text_graph_commands_escape_terminal_controls(tmp_path, capsys):
    dangerous = "\x1b]52;c;dGVzdA==\x07\u202eresult"
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "nodes": [{
            "id": "exp:terminal", "type": "experiment", "status": "null",
            "value": dangerous,
        }],
        "edges": [],
    })

    assert main(["--config", str(cfg.config_path), "journal"]) == 0
    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "\x07" not in output
    assert "\u202e" not in output
    assert "\\u001b]52;c;dGVzdA==\\u0007\\u202eresult" in output


# --------------------------------------------------------------------------- lint

def test_lint_flags_unknown_type_and_missing_schema_version(tmp_path):
    cfg = _project(tmp_path, {
        "nodes": [{"id": "a", "type": "widget", "status": "current"}],
        "edges": []})
    kinds = {k for k, _, _ in engine.lint_issues(cfg)}
    assert "UNKNOWN_TYPE" in kinds
    assert "NO_SCHEMA_VERSION" in kinds


def test_lint_flags_unknown_rel(tmp_path):
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "nodes": [{"id": "a", "type": "data", "status": "current"},
                  {"id": "b", "type": "artifact", "status": "current"}],
        "edges": [{"from": "a", "to": "b", "rel": "frobnicates"}]})
    kinds = {k for k, _, _ in engine.lint_issues(cfg)}
    assert "UNKNOWN_REL" in kinds


@pytest.mark.parametrize("status", ["null", "dead_end", "retracted"])
def test_lint_blocks_non_supporting_status_from_supports_edge(tmp_path, status):
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "nodes": [
            {"id": "exp:x", "type": "experiment", "status": status},
            {"id": "claim:x", "type": "claim", "status": "current"},
        ],
        "edges": [{"from": "exp:x", "to": "claim:x", "rel": "supports"}],
    })
    issues = engine.lint_issues(cfg)
    assert any(kind == "STATUS_RELATION_MISMATCH" for kind, _node, _detail in issues)


def test_lint_flags_load_bearing_node_without_backbone(tmp_path):
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "concepts": {"dataset_version": {"canonical": "v1"}},
        "nodes": [{"id": "claim:x", "type": "claim", "status": "current"}],
        "edges": []})
    kinds = {k for k, _, _ in engine.lint_issues(cfg)}
    assert "NO_BACKBONE" in kinds


# --------------------------------------------------------------------------- deterministic false-green regressions

def test_multiple_concepts_are_scoped_by_backbone_mapping(tmp_path):
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "concepts": {
            "dataset_version": {"canonical": "v2"},
            "model_version": {"canonical": "m1"},
        },
        "nodes": [
            {"id": "data:x", "type": "data", "status": "current",
             "backbone": {"dataset_version": "v2"}},
            {"id": "art:model", "type": "artifact", "status": "current",
             "backbone": {"model_version": "m1"}},
        ],
        "edges": []})
    problems, _ = engine.compute_check(cfg)
    assert not {"SILENT_DRIFT", "AMBIGUOUS_BACKBONE"} & {k for k, _, _ in problems}
    impacted, _ = engine.impact(cfg, "dataset_version", "v3")
    assert [nid for nid, _, _ in impacted] == ["data:x"]


def test_scalar_backbone_is_rejected_when_multiple_concepts_exist(tmp_path):
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "concepts": {"dataset_version": {"canonical": "v2"},
                     "model_version": {"canonical": "m1"}},
        "nodes": [{"id": "data:x", "type": "data", "status": "current", "backbone": "v2"}],
        "edges": []})
    problems, _ = engine.compute_check(cfg)
    assert any(k == "AMBIGUOUS_BACKBONE" for k, _, _ in problems)
    assert any(k == "AMBIGUOUS_BACKBONE" for k, _, _ in engine.lint_issues(cfg))


def test_impact_is_topologically_ordered(tmp_path):
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "concepts": {"model": {"canonical": "m1"}},
        "nodes": [
            {"id": "art:fit", "type": "artifact", "status": "current", "backbone": "m1"},
            {"id": "code:fit", "type": "code", "status": "current", "backbone": "m1"},
        ],
        "edges": [{"from": "code:fit", "to": "art:fit", "rel": "produces"}]})
    impacted, _ = engine.impact(cfg, "model", "m2")
    assert [nid for nid, _, _ in impacted] == ["code:fit", "art:fit"]


def test_claim_support_binding_is_checked_in_dependency_direction(tmp_path):
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "concepts": {"dataset_version": {"canonical": "v2"}},
        "nodes": [
            {"id": "art:old", "type": "artifact", "status": "confirmed", "backbone": "v1"},
            {"id": "claim:new", "type": "claim", "status": "current", "backbone": "v2"},
        ],
        "edges": [{"from": "art:old", "to": "claim:new", "rel": "supports"}]})
    problems, _ = engine.compute_check(cfg)
    assert any(k == "CLAIM_CITES_OFFBACKBONE" and nid == "claim:new"
               for k, nid, _ in problems)


def test_claim_checks_transitive_evidence_bindings(tmp_path):
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "concepts": {"dataset_version": {"canonical": "v2"}},
        "nodes": [
            {"id": "art:old", "type": "artifact", "status": "confirmed", "backbone": "v1"},
            {"id": "art:new", "type": "artifact", "status": "current", "backbone": "v2"},
            {"id": "claim:new", "type": "claim", "status": "current", "backbone": "v2"},
        ],
        "edges": [
            {"from": "art:old", "to": "art:new", "rel": "derives_from"},
            {"from": "art:new", "to": "claim:new", "rel": "supports"},
        ]})
    problems, _ = engine.compute_check(cfg)
    assert any(k == "CLAIM_CITES_OFFBACKBONE" and nid == "claim:new" and "art:old" in detail
               for k, nid, detail in problems)


def _render_project(tmp_path, claim_status="current"):
    (tmp_path / "data.csv").write_text("x\n1\n")
    (tmp_path / "analysis.py").write_text("print('ok')\n")
    (tmp_path / "figure.svg").write_text("<svg/>\n")
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "concepts": {"dataset_version": {"canonical": "v1"}},
        "nodes": [
            {"id": "data:x", "type": "data", "status": "current", "backbone": "v1",
             "path": "data.csv"},
            {"id": "code:plot", "type": "code", "status": "current", "path": "analysis.py"},
            {"id": "fig:x", "type": "figure", "status": "current", "backbone": "v1",
             "path": "figure.svg"},
            {"id": "claim:x", "type": "claim", "status": claim_status, "backbone": "v1"},
        ],
        "edges": [
            {"from": "data:x", "to": "fig:x", "rel": "renders"},
            {"from": "code:plot", "to": "fig:x", "rel": "renders"},
            {"from": "fig:x", "to": "claim:x", "rel": "supports"},
        ]})
    return cfg


def test_missing_manifest_is_an_error(tmp_path):
    cfg = _render_project(tmp_path)
    problems, _ = engine.compute_check(cfg)
    assert any(k == "MISSING_MANIFEST" for k, _, _ in problems)


def test_snapshot_writes_versioned_sha256_manifest(tmp_path):
    cfg = _render_project(tmp_path)
    assert snapshot(cfg) == 0

    record = json.loads((tmp_path / "figure.svg.manifest.json").read_text())
    assert record["schema_version"] == engine.RENDER_MANIFEST_SCHEMA
    assert len(record["output_sha256"]) == 64
    assert "output_sha1" not in record
    assert record["inputs"]
    assert all(len(item["sha256"]) == 64 and "sha1" not in item
               for item in record["inputs"])
    problems, _ = engine.compute_check(cfg)
    assert not [item for item in problems if item[0] in {
        "INVALID_MANIFEST", "OUTPUT_DRIFT", "STALE_DATA",
    }]


def test_unversioned_sha1_manifest_remains_checkable_and_migrates(tmp_path):
    cfg = _render_project(tmp_path)
    nodes, edges, _ = engine.load_graph(cfg)
    manifest = {
        "node": "fig:x",
        "output": "figure.svg",
        "output_sha1": hashlib.sha1((tmp_path / "figure.svg").read_bytes()).hexdigest(),
        "backbone": "v1",
        "value": "",
        "locked_at": "2026-01-01T00:00:00+00:00",
        "inputs": [
            {"path": path, "sha1": hashlib.sha1(file.read_bytes()).hexdigest()}
            for path, file in sorted(engine.expected_inputs(
                cfg, nodes, edges, "fig:x").items())
        ],
    }
    path = tmp_path / "figure.svg.manifest.json"
    path.write_text(json.dumps(manifest))

    problems, _ = engine.compute_check(cfg)
    assert not [item for item in problems if item[0] in {
        "INVALID_MANIFEST", "OUTPUT_DRIFT", "STALE_DATA",
    }]

    # The documented migration is safe after the legacy check succeeds: snapshot rewrites the
    # same current files under the explicit SHA-256 schema.
    assert snapshot(cfg) == 0
    migrated = json.loads(path.read_text())
    assert migrated["schema_version"] == engine.RENDER_MANIFEST_SCHEMA
    assert "output_sha256" in migrated and "output_sha1" not in migrated
    assert all("sha256" in item and "sha1" not in item for item in migrated["inputs"])


def test_legacy_sha1_manifest_still_detects_input_drift(tmp_path):
    cfg = _render_project(tmp_path)
    nodes, edges, _ = engine.load_graph(cfg)
    manifest = {
        "node": "fig:x",
        "output": "figure.svg",
        "output_sha1": hashlib.sha1((tmp_path / "figure.svg").read_bytes()).hexdigest(),
        "backbone": "v1",
        "inputs": [
            {"path": path, "sha1": hashlib.sha1(file.read_bytes()).hexdigest()}
            for path, file in sorted(engine.expected_inputs(
                cfg, nodes, edges, "fig:x").items())
        ],
    }
    (tmp_path / "figure.svg.manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "data.csv").write_text("x\n2\n")

    problems, _ = engine.compute_check(cfg)
    assert any(kind == "STALE_DATA" and node == "fig:x"
               for kind, node, _detail in problems)


@pytest.mark.parametrize("mutation", ["unknown_schema", "mixed_hashes", "invalid_digest"])
def test_versioned_manifest_hash_contract_fails_closed(tmp_path, mutation):
    cfg = _render_project(tmp_path)
    assert snapshot(cfg) == 0
    path = tmp_path / "figure.svg.manifest.json"
    record = json.loads(path.read_text())
    if mutation == "unknown_schema":
        record["schema_version"] = "claimtrace.render-manifest/999"
    elif mutation == "mixed_hashes":
        record["output_sha1"] = "0" * 40
    else:
        record["inputs"][0]["sha256"] = "0" * 63
    path.write_text(json.dumps(record))

    problems, _ = engine.compute_check(cfg)
    assert any(kind == "INVALID_MANIFEST" and node == "fig:x"
               for kind, node, _detail in problems)


def test_manifest_must_cover_every_declared_input(tmp_path):
    cfg = _render_project(tmp_path)
    assert snapshot(cfg) == 0
    manifest = tmp_path / "figure.svg.manifest.json"
    record = json.loads(manifest.read_text())
    record["inputs"] = [item for item in record["inputs"] if item["path"] != "analysis.py"]
    manifest.write_text(json.dumps(record))
    problems, _ = engine.compute_check(cfg)
    assert any(k == "MANIFEST_INPUT_MISMATCH" and "analysis.py" in detail
               for k, _, detail in problems)


def test_malformed_manifest_fails_closed(tmp_path):
    cfg = _render_project(tmp_path)
    (tmp_path / "figure.svg.manifest.json").write_text("{not json")
    problems, _ = engine.compute_check(cfg)
    assert any(k == "INVALID_MANIFEST" for k, _, _ in problems)


def test_output_hash_is_checked(tmp_path):
    cfg = _render_project(tmp_path)
    assert snapshot(cfg) == 0
    (tmp_path / "figure.svg").write_text("<svg>tampered</svg>\n")
    problems, _ = engine.compute_check(cfg)
    assert any(k == "OUTPUT_DRIFT" for k, _, _ in problems)
    assert any(k == "UPSTREAM_STALE" and nid == "claim:x" for k, nid, _ in problems)


def test_manifest_backbone_relabel_is_detected(tmp_path):
    cfg = _render_project(tmp_path)
    assert snapshot(cfg) == 0
    graph = json.loads(cfg.graph_path.read_text())
    graph["concepts"]["dataset_version"]["canonical"] = "v2"
    for node in graph["nodes"]:
        if node.get("backbone"):
            node["backbone"] = "v2"
    cfg.graph_path.write_text(json.dumps(graph))
    problems, _ = engine.compute_check(cfg)
    assert any(k == "MANIFEST_BACKBONE_MISMATCH" and nid == "fig:x"
               for k, nid, _ in problems)
    assert any(k == "UPSTREAM_STALE" and nid == "claim:x" for k, nid, _ in problems)


def test_changed_input_propagates_to_claim(tmp_path):
    cfg = _render_project(tmp_path)
    assert snapshot(cfg) == 0
    (tmp_path / "data.csv").write_text("x\n2\n")
    problems, _ = engine.compute_check(cfg)
    assert any(k == "STALE_DATA" and nid == "fig:x" for k, nid, _ in problems)
    assert any(k == "UPSTREAM_STALE" and nid == "claim:x" for k, nid, _ in problems)


def test_changed_input_propagates_to_confirmed_claim(tmp_path):
    cfg = _render_project(tmp_path, claim_status="confirmed")
    assert snapshot(cfg) == 0
    (tmp_path / "data.csv").write_text("x\n2\n")
    problems, _ = engine.compute_check(cfg)
    assert any(k == "UPSTREAM_STALE" and nid == "claim:x" for k, nid, _ in problems)


def test_log_rejects_invalid_edge_without_writing(tmp_path):
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "nodes": [{"id": "data:x", "type": "data", "status": "current"}],
        "edges": []})
    before = cfg.graph_path.read_text()
    ok, message = engine.log_entry(cfg, {
        "node": {"id": "exp:x", "type": "experiment", "status": "null"},
        "edges": [{"from": "exp:x", "to": "missing", "rel": "related"}]})
    assert not ok and "DANGLING_EDGE" in message
    assert cfg.graph_path.read_text() == before


def test_concurrent_logs_do_not_lose_entries(tmp_path):
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "nodes": [{"id": "data:x", "type": "data", "status": "current"}],
        "edges": []})

    def log_one(i):
        return engine.log_entry(cfg, {
            "node": {"id": f"exp:{i}", "type": "experiment", "status": "null"},
            "edges": []})

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(log_one, range(12)))
    assert all(ok for ok, _message in results)
    nodes, _edges, _concepts = engine.load_graph(cfg)
    assert {f"exp:{i}" for i in range(12)} <= set(nodes)


def test_cross_process_logs_do_not_lose_entries(tmp_path):
    cfg = _project(tmp_path, {
        "schema_version": "1.0",
        "nodes": [{"id": "data:x", "type": "data", "status": "current"}],
        "edges": []})
    ctx = mp.get_context("spawn")
    start, results = ctx.Event(), ctx.Queue()
    processes = [ctx.Process(target=_process_log,
                             args=(str(cfg.config_path), f"exp:process-{i}", start, results))
                 for i in range(4)]
    for process in processes:
        process.start()
    start.set()
    outcomes = [results.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert all(ok for ok, _message in outcomes)
    nodes, _edges, _concepts = engine.load_graph(cfg)
    assert {f"exp:process-{i}" for i in range(4)} <= set(nodes)
