"""Exact-byte project release manifests.

The manifest commits to the files Claimtrace can deterministically discover from a
loaded project configuration: the config and graph, immutable provenance stores,
configured semantic and logic assets, graph-referenced materialized files, and
render manifests.  Creation performs two complete collections and refuses to emit
a manifest when the collections differ.

This is an integrity and coverage mechanism, not a scientific certificate.  In
particular, a locally generated manifest cannot prove that evidence was not deleted
before collection or that the manifest itself was not replaced.  That requires an
externally retained signature, transparency-log entry, or other anchor.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath

from . import (
    __version__, assessment, engine, events, logic, method_assessment,
    pipeline, replay, semantics,
)
from .config import MAX_CONFIG_BYTES, strict_json_loads


MANIFEST_SCHEMA = "claimtrace.project-release/1"
RELEASE_ID_RE = re.compile(r"^release:sha256:([0-9a-f]{64})$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_GRAPH_BYTES = 256 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024
_FILE_ROLES = frozenset({
    "assessment", "config", "derivation", "event", "graph", "graph_artifact",
    "logic_rule_pack", "logic_vocabulary", "ontology_document", "ontology_index",
    "ontology_lock", "method_assessment", "pipeline_contract_current_source",
    "render_manifest", "replay_certificate",
    "semantic_mapping", "semantic_policy", "semantic_terminology", "verifier",
})

_INCLUSIONS = [
    "exact config and graph bytes",
    (
        "all valid JSON event, replay-certificate, method-assessment, assessment, "
        "semantic-mapping, semantic-policy, and derivation records in their "
        "configured stores"
    ),
    (
        "configured verifier, semantic terminology and ontology-lock assets, locked "
        "ontology documents and indexes, symbolic vocabulary and rule-pack assets, and "
        "the current files at pipeline-contract source paths referenced by included "
        "provenance records"
    ),
    (
        "every regular file referenced by a graph node path and every required "
        "render-manifest sidecar"
    ),
]
_EXCLUSIONS = [
    (
        "project files that are neither configured, locked, provenance-store JSON, "
        "nor referenced by a graph node path"
    ),
    "informational graph script references that are not also materialized as node paths",
    "non-JSON lock, temporary, and auxiliary files inside provenance stores",
    (
        "unobserved reads, undeclared writes, transitive subprocesses, and environment "
        "state not already captured by included run receipts"
    ),
]
_LIMITATIONS = [
    (
        "The manifest establishes exact-byte integrity and discoverable-scope coverage, "
        "not scientific truth, semantic correctness, methodological adequacy, or "
        "deterministic execution. Included replay certificates retain their own bounded "
        "coverage, and included method assessments remain reviewed judgements."
    ),
    (
        "Without an externally retained signed or anchored copy, it cannot prove that "
        "records were not deleted before creation or that the manifest and project were "
        "not replaced together."
    ),
    (
        "Two complete collections detect changes between collection passes but cannot "
        "prevent a change after the final pass; create releases only from a quiescent "
        "project."
    ),
    (
        "Imported ontology locks are included only when configured; an unconfigured "
        "import remains only a reference inside its configured lock manifest."
    ),
    (
        "A pipeline snapshot retains its normalized historical declaration and the "
        "historical source fingerprint. The pipeline_contract_current_source role "
        "contains only the file currently occupying that referenced path; it is not a "
        "copy of every historical raw source. Preserve versioned source paths, Git "
        "history, or an external content-addressed archive when prior raw JSON bytes "
        "must remain recoverable."
    ),
    (
        "Configured external assets are recorded with exact absolute paths. Those "
        "entries are host-specific and can disclose usernames or workspace layout in "
        "a published manifest."
    ),
]


class ReleaseError(RuntimeError):
    """The project cannot be represented by one stable release manifest."""


def canonical_bytes(value: object) -> bytes:
    """Return stable UTF-8 JSON bytes for release IDs and comparisons."""
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ReleaseError(f"release value is not canonical JSON: {exc}") from exc


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _stat_identity(value) -> tuple:
    identity = (
        value.st_dev, value.st_ino, value.st_mode, value.st_size,
        getattr(value, "st_mtime_ns", int(value.st_mtime * 1e9)),
    )
    if os.name != "nt":
        identity += (getattr(value, "st_ctime_ns", int(value.st_ctime * 1e9)),)
    return identity


def _read_file_once(path_value, *, capture: bool = False,
                    limit: int | None = None) -> tuple[dict, bytes | None]:
    """Hash one stable regular file without following links or reparse points."""
    path = Path(os.path.abspath(path_value))
    if events._path_has_reparse_component(path):
        raise ReleaseError(f"release file traverses a link or reparse point: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReleaseError(f"cannot open release file {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ReleaseError(f"release path is not a regular file: {path}")
        if limit is not None and before.st_size > limit:
            raise ReleaseError(f"release file exceeds its byte limit: {path}")
        digest = hashlib.sha256()
        chunks = [] if capture else None
        total = 0
        while True:
            chunk = os.read(descriptor, _CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if limit is not None and total > limit:
                raise ReleaseError(f"release file exceeds its byte limit: {path}")
            if chunks is not None:
                chunks.append(chunk)
        after = os.fstat(descriptor)
        try:
            current = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise ReleaseError(f"release file changed while being read: {path}: {exc}") from exc
        if (total != before.st_size
                or _stat_identity(before) != _stat_identity(after)
                or _stat_identity(before) != _stat_identity(current)):
            raise ReleaseError(f"release file changed while being read: {path}")
        snapshot = {"sha256": digest.hexdigest(), "size": total}
        return snapshot, (b"".join(chunks) if chunks is not None else None)
    finally:
        os.close(descriptor)


def _locator(cfg, path_value) -> tuple[str, str]:
    path = Path(os.path.abspath(path_value))
    root = Path(os.path.abspath(cfg.root))
    try:
        portable = path.relative_to(root).as_posix() or "."
        return "project", portable
    except ValueError:
        # External assets are allowed only when the project configuration explicitly
        # opts into them. Preserve their exact location so verification is unambiguous.
        return "external", path.as_posix()


def _target_key(path_value) -> str:
    return os.path.normcase(str(Path(os.path.abspath(path_value))))


def _add_target(targets: dict, cfg, path_value, role: str, *,
                logical_id: str | None = None, snapshot: dict | None = None) -> None:
    path = Path(os.path.abspath(path_value))
    key = _target_key(path)
    target = targets.get(key)
    if target is None:
        location, display = _locator(cfg, path)
        target = {
            "absolute": path,
            "location": location,
            "path": display,
            "roles": set(),
            "logical_ids": set(),
            "snapshot": None,
        }
        targets[key] = target
    target["roles"].add(role)
    if logical_id is not None:
        target["logical_ids"].add(logical_id)
    if snapshot is not None:
        normalized = {"sha256": snapshot["sha256"], "size": snapshot["size"]}
        if target["snapshot"] is not None and target["snapshot"] != normalized:
            raise ReleaseError(
                f"conflicting locked snapshots for release file {target['path']}"
            )
        target["snapshot"] = normalized


def _parse_json(data: bytes, path: Path):
    try:
        return strict_json_loads(data.decode("utf-8-sig"), path.name)
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ReleaseError(f"release JSON is invalid: {path}: {exc}") from exc


def _validate_graph(raw: object, cfg) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("nodes"), list):
        raise ReleaseError("graph must be an object with a nodes list")
    if "edges" in raw and not isinstance(raw["edges"], list):
        raise ReleaseError("graph edges must be a list")
    if "concepts" in raw and not isinstance(raw["concepts"], dict):
        raise ReleaseError("graph concepts must be an object")
    for index, node in enumerate(raw["nodes"]):
        if (not isinstance(node, dict) or not isinstance(node.get("id"), str)
                or not node["id"]):
            raise ReleaseError(f"graph node #{index} needs a non-empty string id")
        if node.get("path") is not None and not isinstance(node["path"], str):
            raise ReleaseError(f"graph node {node['id']!r} path must be a string")
    for index, edge in enumerate(raw.get("edges", [])):
        if not isinstance(edge, dict):
            raise ReleaseError(f"graph edge #{index} must be an object")
    structural = engine.structural_issues(cfg, raw=raw)
    if structural:
        detail = "; ".join(f"{code}:{node}: {message}" for code, node, message in structural)
        raise ReleaseError(f"graph structural integrity failed: {detail}")
    return raw


def _json_paths(root_value, *, recursive: bool) -> list[Path]:
    root = Path(os.path.abspath(root_value))
    if not root.exists():
        return []
    if events._path_has_reparse_component(root) or not root.is_dir():
        raise ReleaseError(f"provenance store must be a non-link directory: {root}")
    paths = []
    try:
        if recursive:
            for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
                current_path = Path(current)
                kept = []
                for name in sorted(dirs):
                    candidate = current_path / name
                    if events._path_has_reparse_component(candidate):
                        raise ReleaseError(
                            f"provenance store traverses a link or reparse point: {candidate}"
                        )
                    kept.append(name)
                dirs[:] = kept
                for name in sorted(files):
                    candidate = current_path / name
                    if candidate.suffix == ".json":
                        paths.append(candidate)
        else:
            for candidate in sorted(root.iterdir(), key=lambda item: item.name):
                if candidate.is_dir() or candidate.is_symlink():
                    raise ReleaseError(
                        "flat provenance store contains an unexpected directory or link: "
                        f"{candidate}"
                    )
                if candidate.suffix == ".json":
                    paths.append(candidate)
    except OSError as exc:
        raise ReleaseError(f"cannot enumerate provenance store {root}: {exc}") from exc
    return sorted(paths, key=lambda item: item.as_posix())


def _require_store_match(actual: list[Path], expected: list[Path], label: str) -> None:
    actual_keys = {_target_key(path) for path in actual}
    expected_keys = {_target_key(path) for path in expected}
    if actual_keys != expected_keys:
        omitted = sorted(str(path) for path in actual if _target_key(path) not in expected_keys)
        missing = sorted(str(path) for path in expected if _target_key(path) not in actual_keys)
        detail = []
        if omitted:
            detail.append("unvalidated JSON: " + ", ".join(omitted))
        if missing:
            detail.append("validated record missing from inventory: " + ", ".join(missing))
        raise ReleaseError(f"{label} inventory is incomplete: {'; '.join(detail)}")


def _collect_stores(cfg, targets: dict) -> list[dict]:
    inventories = []

    def add_pipeline_contract_current_source(snapshot: object, origin: str) -> None:
        try:
            pipeline.validate_pipeline_snapshot(snapshot)
            source_path = snapshot["source"]["path"]
            path = cfg.resolve(source_path)
        except (KeyError, OSError, RuntimeError, ValueError, pipeline.PipelineError) as exc:
            raise ReleaseError(
                f"{origin} has an invalid pipeline-contract source: {exc}"
            ) from exc
        _add_target(targets, cfg, path, "pipeline_contract_current_source")

    event_documents, event_issues = events.load_events(cfg.events_path)
    if event_issues:
        raise ReleaseError("event-store integrity failed: " + "; ".join(
            item.get("detail", str(item)) for item in event_issues
        ))
    event_expected = []
    for document in event_documents:
        match = events.EVENT_ID_RE.fullmatch(document["id"])
        if match is None:
            raise ReleaseError(f"event has an invalid logical id: {document.get('id')!r}")
        path = events._event_path(cfg.events_path, match.group(1))
        event_expected.append(path)
        _add_target(targets, cfg, path, "event", logical_id=document["id"])
        contract_snapshot = (
            document.get("payload", {}).get("plan", {}).get("pipeline_contract")
            if document.get("type") == "run.started" else None
        )
        if contract_snapshot is not None:
            add_pipeline_contract_current_source(
                contract_snapshot, f"event {document['id']}",
            )
    _require_store_match(
        _json_paths(cfg.events_path, recursive=True), event_expected, "event store",
    )
    inventories.append(_store_inventory(cfg, cfg.events_path, "event", len(event_expected)))

    try:
        replay_documents, replay_issues = replay.load_replay_certificates(
            cfg.replays_path
        )
    except replay.ReplayError as exc:
        raise ReleaseError(f"replay-certificate-store integrity failed: {exc}") from exc
    if replay_issues:
        raise ReleaseError("replay-certificate-store integrity failed: " + "; ".join(
            item.get("detail", str(item)) for item in replay_issues
        ))
    replay_expected = []
    for document in replay_documents:
        try:
            replay_id = replay.validate_replay_certificate(document)
        except replay.ReplayError as exc:
            raise ReleaseError(
                f"replay-certificate-store integrity failed: {exc}"
            ) from exc
        digest = replay_id.rsplit(":", 1)[1]
        path = cfg.replays_path / digest[:2] / f"{digest}.json"
        replay_expected.append(path)
        _add_target(
            targets, cfg, path, "replay_certificate", logical_id=replay_id,
        )
    _require_store_match(
        _json_paths(cfg.replays_path, recursive=True), replay_expected,
        "replay-certificate store",
    )
    inventories.append(_store_inventory(
        cfg, cfg.replays_path, "replay_certificate", len(replay_expected),
    ))

    try:
        method_documents, method_issues = method_assessment.load_method_assessments(cfg)
    except method_assessment.MethodAssessmentError as exc:
        raise ReleaseError(f"method-assessment-store integrity failed: {exc}") from exc
    if method_issues:
        raise ReleaseError("method-assessment-store integrity failed: " + "; ".join(
            item.get("detail", str(item)) for item in method_issues
        ))
    method_root = method_assessment.method_assessments_path(cfg)
    method_expected = []
    for document in method_documents:
        try:
            method_assessment.validate_method_assessment_document(document)
        except method_assessment.MethodAssessmentError as exc:
            raise ReleaseError(
                f"method-assessment-store integrity failed: {exc}"
            ) from exc
        match = method_assessment.ASSESSMENT_ID_RE.fullmatch(document["id"])
        if match is None:
            raise ReleaseError(
                "method assessment has an invalid logical id: "
                f"{document.get('id')!r}"
            )
        path = method_root / f"{match.group(1)}.json"
        method_expected.append(path)
        _add_target(
            targets, cfg, path, "method_assessment", logical_id=document["id"],
        )
        add_pipeline_contract_current_source(
            document["mechanical_snapshot"]["pipeline_contract"],
            f"method assessment {document['id']}",
        )
    _require_store_match(
        _json_paths(method_root, recursive=False), method_expected,
        "method-assessment store",
    )
    inventories.append(_store_inventory(
        cfg, method_root, "method_assessment", len(method_expected),
    ))

    assessment_documents, assessment_issues = assessment.load_assessments(cfg)
    if assessment_issues:
        raise ReleaseError("assessment-store integrity failed: " + "; ".join(
            item.get("detail", str(item)) for item in assessment_issues
        ))
    assessment_root = assessment.assessments_path(cfg)
    assessment_expected = []
    for document in assessment_documents:
        match = assessment.ASSESSMENT_ID_RE.fullmatch(document["id"])
        if match is None:
            raise ReleaseError(f"assessment has an invalid logical id: {document.get('id')!r}")
        path = assessment_root / f"{match.group(1)}.json"
        assessment_expected.append(path)
        _add_target(targets, cfg, path, "assessment", logical_id=document["id"])
    _require_store_match(
        _json_paths(assessment_root, recursive=False), assessment_expected,
        "assessment store",
    )
    inventories.append(_store_inventory(
        cfg, assessment_root, "assessment", len(assessment_expected),
    ))

    mapping_documents, mapping_issues = semantics.load_mappings(cfg)
    if mapping_issues:
        raise ReleaseError("semantic-mapping store integrity failed: " + "; ".join(
            item.get("detail", str(item)) for item in mapping_issues
        ))
    mapping_root = semantics.mappings_path(cfg)
    mapping_expected = []
    for document in mapping_documents:
        match = semantics.MAPPING_ID_RE.fullmatch(document["id"])
        assert match is not None
        path = mapping_root / f"{match.group(1)}.json"
        mapping_expected.append(path)
        _add_target(targets, cfg, path, "semantic_mapping", logical_id=document["id"])
    _require_store_match(
        _json_paths(mapping_root, recursive=False), mapping_expected,
        "semantic-mapping store",
    )
    inventories.append(_store_inventory(
        cfg, mapping_root, "semantic_mapping", len(mapping_expected),
    ))

    policy_documents, policy_issues = semantics.load_policies(cfg)
    if policy_issues:
        raise ReleaseError("semantic-policy store integrity failed: " + "; ".join(
            item.get("detail", str(item)) for item in policy_issues
        ))
    policy_root = semantics.policies_path(cfg)
    policy_expected = []
    for document in policy_documents:
        match = semantics.POLICY_ID_RE.fullmatch(document["id"])
        assert match is not None
        path = policy_root / f"{match.group(1)}.json"
        policy_expected.append(path)
        _add_target(targets, cfg, path, "semantic_policy", logical_id=document["id"])
    _require_store_match(
        _json_paths(policy_root, recursive=False), policy_expected,
        "semantic-policy store",
    )
    inventories.append(_store_inventory(
        cfg, policy_root, "semantic_policy", len(policy_expected),
    ))

    derivation_documents, derivation_issues = logic.load_derivations(cfg)
    if derivation_issues:
        raise ReleaseError("derivation-store integrity failed: " + "; ".join(
            item.get("detail", str(item)) for item in derivation_issues
        ))
    derivation_root = logic.derivations_path(cfg)
    derivation_expected = []
    for document in derivation_documents:
        match = logic.DERIVATION_ID_RE.fullmatch(document["id"])
        assert match is not None
        path = derivation_root / f"{match.group(1)}.json"
        derivation_expected.append(path)
        _add_target(targets, cfg, path, "derivation", logical_id=document["id"])
    _require_store_match(
        _json_paths(derivation_root, recursive=False), derivation_expected,
        "derivation store",
    )
    inventories.append(_store_inventory(
        cfg, derivation_root, "derivation", len(derivation_expected),
    ))
    return inventories


def _store_inventory(cfg, root: Path, role: str, count: int) -> dict:
    location, display = _locator(cfg, root)
    return {
        "role": role,
        "location": location,
        "path": display,
        "record_count": count,
    }


def _collect_semantic_assets(cfg, targets: dict) -> None:
    try:
        assets = semantics.configured_semantic_assets(cfg)
    except semantics.SemanticError as exc:
        raise ReleaseError(f"configured semantic assets are invalid: {exc}") from exc
    for logical_id, source in sorted(assets["terminology_paths"].items()):
        _add_target(
            targets, cfg, Path(source), "semantic_terminology", logical_id=logical_id,
        )
    for lock_id, source in sorted(assets["ontology_lock_paths"].items()):
        lock_path = Path(source)
        _add_target(targets, cfg, lock_path, "ontology_lock", logical_id=lock_id)
        lock = assets["ontology_locks"][lock_id]
        for item in lock["documents"]:
            _add_target(
                targets, cfg, lock_path.parent / item["path"], "ontology_document",
                snapshot=item,
            )
        index = lock["index"]
        _add_target(
            targets, cfg, lock_path.parent / index["path"], "ontology_index",
            logical_id=lock["ontology_id"], snapshot=index,
        )


def _collect_logic_assets(cfg, targets: dict) -> None:
    try:
        vocabularies, rule_packs = logic.configured_logic_assets(cfg)
    except logic.LogicError as exc:
        raise ReleaseError(f"configured symbolic assets are invalid: {exc}") from exc
    for path in cfg.logic_vocabulary_paths:
        raw_snapshot, raw_bytes = _read_file_once(
            path, capture=True, limit=logic.MAX_LOGIC_ASSET_BYTES,
        )
        try:
            document = logic.load_vocabulary(_parse_json(raw_bytes, Path(path)))
        except logic.LogicError as exc:
            raise ReleaseError(f"configured symbolic vocabulary is invalid: {path}: {exc}") from exc
        if document != vocabularies.get(document["id"]):
            raise ReleaseError(f"configured symbolic vocabulary changed while loading: {path}")
        _add_target(
            targets, cfg, path, "logic_vocabulary", logical_id=document["id"],
            snapshot=raw_snapshot,
        )
    for path in cfg.logic_rule_pack_paths:
        raw_snapshot, raw_bytes = _read_file_once(
            path, capture=True, limit=logic.MAX_LOGIC_ASSET_BYTES,
        )
        raw = _parse_json(raw_bytes, Path(path))
        vocabulary = vocabularies.get(raw.get("vocabulary_id")) if isinstance(raw, dict) else None
        if vocabulary is None:
            raise ReleaseError(f"configured rule pack has unavailable vocabulary: {path}")
        try:
            document = logic.load_rule_pack(raw, vocabulary)
        except logic.LogicError as exc:
            raise ReleaseError(f"configured symbolic rule pack is invalid: {path}: {exc}") from exc
        if document != rule_packs.get(document["id"]):
            raise ReleaseError(f"configured symbolic rule pack changed while loading: {path}")
        _add_target(
            targets, cfg, path, "logic_rule_pack", logical_id=document["id"],
            snapshot=raw_snapshot,
        )


def _collect_once(cfg) -> dict:
    targets = {}
    config_snapshot, config_bytes = _read_file_once(
        cfg.config_path, capture=True, limit=MAX_CONFIG_BYTES,
    )
    config_raw = _parse_json(config_bytes, Path(cfg.config_path))
    if config_raw != cfg.data:
        raise ReleaseError("config changed after it was loaded; reload the project before release")
    _add_target(targets, cfg, cfg.config_path, "config", snapshot=config_snapshot)

    graph_snapshot, graph_bytes = _read_file_once(
        cfg.graph_path, capture=True, limit=MAX_GRAPH_BYTES,
    )
    graph = _validate_graph(_parse_json(graph_bytes, Path(cfg.graph_path)), cfg)
    _add_target(targets, cfg, cfg.graph_path, "graph", snapshot=graph_snapshot)

    if cfg.verifiers is not None:
        _add_target(targets, cfg, cfg.verifiers, "verifier")

    graph_path_ids = []
    for node in graph["nodes"]:
        path_value = node.get("path")
        if not path_value:
            continue
        try:
            path = cfg.resolve(path_value)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ReleaseError(f"graph node {node['id']!r} has an invalid path: {exc}") from exc
        _add_target(targets, cfg, path, "graph_artifact", logical_id=node["id"])
        graph_path_ids.append(node["id"])
        if node.get("type") in cfg.render_types:
            _add_target(
                targets, cfg, cfg.resolve(path_value + ".manifest.json"),
                "render_manifest", logical_id=node["id"],
            )

    _collect_semantic_assets(cfg, targets)
    _collect_logic_assets(cfg, targets)
    stores = _collect_stores(cfg, targets)
    if cfg.semantic_active_policy is not None and not any(
            "semantic_policy" in target["roles"]
            and cfg.semantic_active_policy in target["logical_ids"]
            for target in targets.values()):
        raise ReleaseError(
            "configured active semantic policy is not present in the included policy store: "
            f"{cfg.semantic_active_policy}"
        )

    files = []
    for target in sorted(
            targets.values(), key=lambda item: (item["location"], item["path"])):
        snapshot = target["snapshot"]
        if snapshot is None:
            snapshot, _unused = _read_file_once(target["absolute"])
        files.append({
            "location": target["location"],
            "path": target["path"],
            "roles": sorted(target["roles"]),
            "logical_ids": sorted(target["logical_ids"]),
            "sha256": snapshot["sha256"],
            "size": snapshot["size"],
        })
    role_counts = []
    for role in sorted({role for item in files for role in item["roles"]}):
        role_counts.append({
            "role": role,
            "file_count": sum(role in item["roles"] for item in files),
        })
    return {
        "files": files,
        "inventory": {
            "graph_node_count": len(graph["nodes"]),
            "graph_edge_count": len(graph.get("edges", [])),
            "graph_path_node_ids": sorted(graph_path_ids),
            "active_semantic_policy_id": cfg.semantic_active_policy,
            "stores": sorted(stores, key=lambda item: item["role"]),
            "role_counts": role_counts,
        },
    }


def _schema_versions() -> dict:
    return {
        "assessment_current": assessment.SCHEMA_VERSION,
        "assessment_supported": sorted(assessment.SUPPORTED_SCHEMA_VERSIONS),
        "derivation": logic.DERIVATION_SCHEMA,
        "event_current": events.CONTRACT_EVENT_SCHEMA,
        "event_stage_checkpoint_current": events.STAGE_CONTRACT_EVENT_SCHEMA,
        "event_supported": sorted(events.SUPPORTED_EVENT_SCHEMAS),
        "graph": engine.SCHEMA_VERSION,
        "logic_rule_pack": logic.RULE_PACK_SCHEMA,
        "logic_vocabulary": logic.VOCABULARY_SCHEMA,
        "local_terminology": semantics.LOCAL_TERMINOLOGY_SCHEMA,
        "method_assessment": method_assessment.SCHEMA_VERSION,
        "method_requirements": engine.METHOD_REQUIREMENTS_SCHEMA,
        "method_spec": pipeline.METHOD_SPEC_SCHEMA,
        "ontology_index": semantics.ONTOLOGY_INDEX_SCHEMA,
        "ontology_lock": semantics.ONTOLOGY_LOCK_SCHEMA,
        "pipeline_contract": pipeline.CONTRACT_SCHEMA,
        "pipeline_contract_snapshot_current": pipeline.SNAPSHOT_SCHEMA,
        "pipeline_contract_snapshot_supported": sorted(
            pipeline.SUPPORTED_SNAPSHOT_SCHEMAS
        ),
        "render_manifest": engine.RENDER_MANIFEST_SCHEMA,
        "replay_certificate_current": replay.REPLAY_SCHEMA,
        "replay_certificate_stage_checkpoint_current": replay.STAGE_REPLAY_SCHEMA,
        "replay_certificate_supported": sorted(
            replay.SUPPORTED_REPLAY_SCHEMAS
        ),
        "semantic_mapping": semantics.MAPPING_SCHEMA,
        "semantic_policy": semantics.POLICY_SCHEMA,
        "stage_checkpoint_record": pipeline.STAGE_CHECKPOINT_SCHEMA,
        "stage_trace": pipeline.STAGE_TRACE_SCHEMA,
        "stage_trace_plan": pipeline.STAGE_TRACE_PLAN_SCHEMA,
    }


_PRE_STAGE_CHECKPOINT_SCHEMA_VERSIONS = {
    "assessment_current": "claimtrace.semantic-assessment/2",
    "assessment_supported": [
        "claimtrace.semantic-assessment/1", "claimtrace.semantic-assessment/2",
    ],
    "derivation": "claimtrace.symbolic-derivation/1",
    "event_current": "claimtrace.event/3",
    "event_supported": [
        "claimtrace.event/1", "claimtrace.event/2", "claimtrace.event/3",
    ],
    "graph": "1.0",
    "local_terminology": "claimtrace.local-terminology/1",
    "logic_rule_pack": "claimtrace.symbolic-rules/1",
    "logic_vocabulary": "claimtrace.symbolic-vocabulary/1",
    "method_assessment": "claimtrace.method-conformance-assessment/1",
    "method_requirements": "claimtrace.method-requirements/1",
    "method_spec": "claimtrace.method-spec/1",
    "ontology_index": "claimtrace.ontology-index/1",
    "ontology_lock": "claimtrace.ontology-lock/1",
    "pipeline_contract": "claimtrace.pipeline-contract/1",
    "pipeline_contract_snapshot_current": "claimtrace.pipeline-contract-snapshot/2",
    "pipeline_contract_snapshot_supported": [
        "claimtrace.pipeline-contract-snapshot/1",
        "claimtrace.pipeline-contract-snapshot/2",
    ],
    "render_manifest": "claimtrace.render-manifest/2",
    "replay_certificate_current": "claimtrace.replay-certificate/2",
    "replay_certificate_supported": [
        "claimtrace.replay-certificate/1", "claimtrace.replay-certificate/2",
    ],
    "semantic_mapping": "claimtrace.semantic-mapping/1",
    "semantic_policy": "claimtrace.semantic-policy/1",
}


def _manifest_core(state: dict) -> dict:
    return {
        "schema_version": MANIFEST_SCHEMA,
        "tool": {"name": "claimtrace", "version": __version__},
        "schemas": _schema_versions(),
        "scope": {
            "inclusions": list(_INCLUSIONS),
            "exclusions": list(_EXCLUSIONS),
            "limitations": list(_LIMITATIONS),
            "inventory": state["inventory"],
        },
        "files": state["files"],
    }


def create_release_manifest(cfg) -> dict:
    """Create a deterministic manifest after two complete, matching collections."""
    first = _collect_once(cfg)
    second = _collect_once(cfg)
    if canonical_bytes(first) != canonical_bytes(second):
        raise ReleaseError(
            "project changed between the two release-manifest collection passes"
        )
    core = _manifest_core(second)
    return {"release_id": f"release:sha256:{canonical_sha256(core)}", **core}


def validate_release_manifest(document: object) -> None:
    """Validate v1 shape, canonical ordering, and the deterministic release ID."""
    if not isinstance(document, dict) or set(document) != {
            "release_id", "schema_version", "tool", "schemas", "scope", "files"}:
        raise ReleaseError("release manifest has unknown or missing top-level fields")
    if document["schema_version"] != MANIFEST_SCHEMA:
        raise ReleaseError(f"unsupported release manifest schema: {document['schema_version']!r}")
    if (not isinstance(document["tool"], dict)
            or set(document["tool"]) != {"name", "version"}
            or document["tool"]["name"] != "claimtrace"
            or not isinstance(document["tool"]["version"], str)):
        raise ReleaseError("release manifest tool declaration is invalid")
    if not isinstance(document["schemas"], dict) or not all(
            isinstance(key, str)
            and (isinstance(value, str)
                 or (isinstance(value, list)
                     and all(isinstance(item, str) for item in value)))
            for key, value in document["schemas"].items()):
        raise ReleaseError("release manifest schema declarations are invalid")
    if document["schemas"] not in (
            _schema_versions(), _PRE_STAGE_CHECKPOINT_SCHEMA_VERSIONS):
        raise ReleaseError(
            "release manifest schema declarations are not canonical for schema v1"
        )
    scope = document["scope"]
    if (not isinstance(scope, dict)
            or set(scope) != {"inclusions", "exclusions", "limitations", "inventory"}
            or not all(isinstance(scope[key], list)
                       and all(isinstance(item, str) for item in scope[key])
                       for key in ("inclusions", "exclusions", "limitations"))
            or not isinstance(scope["inventory"], dict)):
        raise ReleaseError("release manifest scope is invalid")
    if (scope["inclusions"] != _INCLUSIONS or scope["exclusions"] != _EXCLUSIONS
            or scope["limitations"] != _LIMITATIONS):
        raise ReleaseError("release manifest scope policy is not canonical for schema v1")
    files = document["files"]
    if not isinstance(files, list):
        raise ReleaseError("release manifest files must be a list")
    keys = []
    for index, item in enumerate(files):
        if not isinstance(item, dict) or set(item) != {
                "location", "path", "roles", "logical_ids", "sha256", "size"}:
            raise ReleaseError(f"release manifest file #{index} has an invalid shape")
        if item["location"] not in {"project", "external"}:
            raise ReleaseError(f"release manifest file #{index} has an invalid location")
        if (not isinstance(item["path"], str) or not item["path"]
                or any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F
                       for char in item["path"])):
            raise ReleaseError(f"release manifest file #{index} has an invalid path")
        if item["location"] == "project":
            portable = PurePosixPath(item["path"])
            if ("\\" in item["path"] or portable.is_absolute()
                    or any(part in {"", ".", ".."} for part in portable.parts)):
                raise ReleaseError(
                    f"release manifest file #{index} has a non-portable project path"
                )
        for label in ("roles", "logical_ids"):
            values = item[label]
            if (not isinstance(values, list) or not all(
                    isinstance(value, str) and value
                    and not any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F
                                or 0xD800 <= ord(char) <= 0xDFFF
                                for char in value)
                    for value in values)
                    or values != sorted(set(values))):
                raise ReleaseError(
                    f"release manifest file #{index} {label} must be sorted and unique"
                )
        if not item["roles"]:
            raise ReleaseError(f"release manifest file #{index} has no role")
        if not set(item["roles"]) <= _FILE_ROLES:
            raise ReleaseError(f"release manifest file #{index} has an unknown role")
        if not isinstance(item["sha256"], str) or not SHA256_RE.fullmatch(item["sha256"]):
            raise ReleaseError(f"release manifest file #{index} has an invalid SHA-256")
        if (not isinstance(item["size"], int) or isinstance(item["size"], bool)
                or item["size"] < 0):
            raise ReleaseError(f"release manifest file #{index} has an invalid size")
        keys.append((item["location"], item["path"]))
    if keys != sorted(set(keys)):
        raise ReleaseError("release manifest files must be sorted with unique locations")
    inventory = scope["inventory"]
    if set(inventory) != {
            "graph_node_count", "graph_edge_count", "graph_path_node_ids",
            "active_semantic_policy_id", "stores", "role_counts"}:
        raise ReleaseError("release manifest inventory has an invalid shape")
    for field in ("graph_node_count", "graph_edge_count"):
        if (not isinstance(inventory[field], int) or isinstance(inventory[field], bool)
                or inventory[field] < 0):
            raise ReleaseError(f"release manifest inventory {field} is invalid")
    path_node_ids = inventory["graph_path_node_ids"]
    if (not isinstance(path_node_ids, list)
            or not all(isinstance(item, str) and item for item in path_node_ids)
            or path_node_ids != sorted(set(path_node_ids))):
        raise ReleaseError("release manifest graph_path_node_ids are invalid")
    active_policy = inventory["active_semantic_policy_id"]
    if active_policy is not None and (
            not isinstance(active_policy, str)
            or not semantics.POLICY_ID_RE.fullmatch(active_policy)):
        raise ReleaseError("release manifest active semantic policy id is invalid")
    stores = inventory["stores"]
    if not isinstance(stores, list):
        raise ReleaseError("release manifest store inventory must be a list")
    store_roles = []
    for item in stores:
        if (not isinstance(item, dict)
                or set(item) != {"role", "location", "path", "record_count"}
                or item["role"] not in {
                    "event", "assessment", "semantic_mapping", "semantic_policy",
                    "derivation", "replay_certificate", "method_assessment",
                }
                or item["location"] not in {"project", "external"}
                or not isinstance(item["path"], str) or not item["path"]
                or not isinstance(item["record_count"], int)
                or isinstance(item["record_count"], bool) or item["record_count"] < 0):
            raise ReleaseError("release manifest store inventory entry is invalid")
        store_roles.append(item["role"])
    if store_roles != sorted(set(store_roles)):
        raise ReleaseError("release manifest store inventory is not canonical")
    required_store_roles = {
        "event", "assessment", "semantic_mapping", "semantic_policy", "derivation",
        "replay_certificate", "method_assessment",
    }
    if set(store_roles) != required_store_roles:
        raise ReleaseError("release manifest store inventory is incomplete")
    expected_role_counts = [
        {"role": role, "file_count": sum(role in item["roles"] for item in files)}
        for role in sorted({role for item in files for role in item["roles"]})
    ]
    if inventory["role_counts"] != expected_role_counts:
        raise ReleaseError("release manifest role counts do not match its file inventory")
    role_count_map = {
        item["role"]: item["file_count"] for item in expected_role_counts
    }
    if role_count_map.get("config") != 1 or role_count_map.get("graph") != 1:
        raise ReleaseError("release manifest must include exactly one config and graph file")
    for item in stores:
        if item["record_count"] != role_count_map.get(item["role"], 0):
            raise ReleaseError(
                f"release manifest {item['role']} store count does not match included files"
            )
    represented_path_ids = sorted({
        logical_id
        for item in files if "graph_artifact" in item["roles"]
        for logical_id in item["logical_ids"]
    })
    if path_node_ids != represented_path_ids:
        raise ReleaseError(
            "release manifest graph path-node inventory does not match graph artifacts"
        )
    if active_policy is not None and active_policy not in {
            logical_id
            for item in files if "semantic_policy" in item["roles"]
            for logical_id in item["logical_ids"]
    }:
        raise ReleaseError(
            "release manifest active semantic policy is not included as a policy record"
        )
    core = {key: document[key] for key in document if key != "release_id"}
    expected = f"release:sha256:{canonical_sha256(core)}"
    if (not isinstance(document["release_id"], str)
            or not RELEASE_ID_RE.fullmatch(document["release_id"])
            or document["release_id"] != expected):
        raise ReleaseError("release_id does not match canonical manifest content")


def diff_release_manifests(before: dict, after: dict) -> dict:
    """Return a deterministic exact-byte and scope diff for two valid manifests."""
    validate_release_manifest(before)
    validate_release_manifest(after)
    before_files = {(item["location"], item["path"]): item for item in before["files"]}
    after_files = {(item["location"], item["path"]): item for item in after["files"]}
    added = [after_files[key] for key in sorted(after_files.keys() - before_files.keys())]
    removed = [before_files[key] for key in sorted(before_files.keys() - after_files.keys())]
    changed = []
    for key in sorted(before_files.keys() & after_files.keys()):
        left = before_files[key]
        right = after_files[key]
        fields = [
            field for field in ("roles", "logical_ids", "sha256", "size")
            if left[field] != right[field]
        ]
        if fields:
            changed.append({
                "location": key[0],
                "path": key[1],
                "fields": fields,
                "before": left,
                "after": right,
            })
    metadata_fields = [
        field for field in ("tool", "schemas", "scope")
        if before[field] != after[field]
    ]
    equal = not added and not removed and not changed and not metadata_fields
    return {
        "equal": equal,
        "before_release_id": before["release_id"],
        "after_release_id": after["release_id"],
        "added_files": added,
        "removed_files": removed,
        "changed_files": changed,
        "changed_metadata_fields": metadata_fields,
    }


def verify_release_manifest(cfg, document: dict) -> dict:
    """Recollect the project and report drift, omissions, or integrity failures."""
    validate_release_manifest(document)
    try:
        current = create_release_manifest(cfg)
    except ReleaseError as exc:
        return {
            "valid": False,
            "manifest_release_id": document["release_id"],
            "current_release_id": None,
            "errors": [str(exc)],
            "diff": None,
            "limitations": list(_LIMITATIONS),
        }
    difference = diff_release_manifests(document, current)
    file_scope_valid = (
        not difference["added_files"]
        and not difference["removed_files"]
        and not difference["changed_files"]
        and "scope" not in difference["changed_metadata_fields"]
    )
    return {
        "valid": file_scope_valid,
        "manifest_release_id": document["release_id"],
        "current_release_id": current["release_id"],
        "errors": [],
        "diff": difference,
        "limitations": list(_LIMITATIONS),
    }


__all__ = [
    "MANIFEST_SCHEMA", "RELEASE_ID_RE", "ReleaseError", "canonical_bytes",
    "canonical_sha256", "create_release_manifest", "validate_release_manifest",
    "verify_release_manifest", "diff_release_manifests",
]
