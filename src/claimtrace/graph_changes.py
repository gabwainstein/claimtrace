"""Deterministic, reviewable transactions for ``graph.json``.

An agent or user may construct a bounded change request, but Claimtrace is the component that
turns it into a content-addressed proposal.  Proposal creation checks the exact canonical base
graph and validates the resulting graph without writing it.  Application repeats those checks
under the existing graph lock and atomically replaces the graph only when its base hash still
matches.

The content address detects accidental or unreviewed proposal mutation.  It is not an identity
or authorization signature; deployments that need authenticated approval must add that policy at
the caller boundary.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path

from . import engine

GRAPH_CHANGE_REQUEST_SCHEMA = "claimtrace.graph-change-request/1"
GRAPH_CHANGE_PROPOSAL_SCHEMA = "claimtrace.graph-change-proposal/1"
GRAPH_HASH_PREFIX = "graph:sha256:"
PROPOSAL_ID_PREFIX = "graph-change:sha256:"

MAX_GRAPH_CHANGE_OPERATIONS = 1000
MAX_GRAPH_CHANGE_REQUEST_BYTES = 2 * 1024 * 1024
MAX_GRAPH_CHANGE_DESCRIPTION_BYTES = 16 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_VALUES = 200_000
MAX_JSON_STRING_BYTES = 1024 * 1024
MAX_JSON_INTEGER_BITS = 4096

_HASH_RE = re.compile(r"^graph:sha256:[0-9a-f]{64}$")
_PROPOSAL_ID_RE = re.compile(r"^graph-change:sha256:[0-9a-f]{64}$")
_CHANGE_FIELDS = (
    "add_nodes",
    "replace_nodes",
    "remove_nodes",
    "add_edges",
    "remove_edges",
    "set_concepts",
    "remove_concepts",
)


class GraphChangeError(engine.GraphError):
    """A graph change request/proposal is invalid or cannot be applied safely."""


def _canonical_bytes(value) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise GraphChangeError(f"value is not canonical JSON: {exc}") from exc


def canonical_graph_hash(raw: dict) -> str:
    """Return the SHA-256 address of a graph's canonical JSON value.

    Whitespace, a UTF-8 BOM, and object-key order do not affect this hash.  Array order and every
    JSON value do, so the address identifies one exact logical ``graph.json`` state.
    """
    if not isinstance(raw, dict):
        raise GraphChangeError("graph must be a JSON object")
    return GRAPH_HASH_PREFIX + hashlib.sha256(_canonical_bytes(raw)).hexdigest()


def _validate_json_value(value, *, path: str, state: dict, depth: int = 0, active=None):
    if active is None:
        active = set()
    if depth > MAX_JSON_DEPTH:
        raise GraphChangeError(f"{path} exceeds maximum JSON depth {MAX_JSON_DEPTH}")
    state["values"] += 1
    if state["values"] > MAX_JSON_VALUES:
        raise GraphChangeError(
            f"request exceeds maximum JSON value count {MAX_JSON_VALUES}"
        )
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_JSON_STRING_BYTES:
            raise GraphChangeError(
                f"{path} exceeds maximum string size {MAX_JSON_STRING_BYTES} bytes"
            )
        return
    if isinstance(value, int):
        if value.bit_length() > MAX_JSON_INTEGER_BITS:
            raise GraphChangeError(
                f"{path} exceeds maximum integer size {MAX_JSON_INTEGER_BITS} bits"
            )
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GraphChangeError(f"{path} contains a non-finite number")
        return
    if not isinstance(value, (list, dict)):
        raise GraphChangeError(f"{path} contains non-JSON value {type(value).__name__}")
    marker = id(value)
    if marker in active:
        raise GraphChangeError(f"{path} contains a cyclic container")
    active.add(marker)
    try:
        if isinstance(value, list):
            for index, item in enumerate(value):
                _validate_json_value(
                    item,
                    path=f"{path}[{index}]",
                    state=state,
                    depth=depth + 1,
                    active=active,
                )
        else:
            for key, item in value.items():
                if not isinstance(key, str):
                    raise GraphChangeError(f"{path} has a non-string object key")
                _validate_json_value(
                    key,
                    path=f"{path}.<key>",
                    state=state,
                    depth=depth + 1,
                    active=active,
                )
                _validate_json_value(
                    item,
                    path=f"{path}.{key}",
                    state=state,
                    depth=depth + 1,
                    active=active,
                )
    finally:
        active.remove(marker)


def _strict_keys(value: dict, allowed: set, path: str):
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise GraphChangeError(f"{path} has unknown field(s): {', '.join(unknown)}")


def _normalize_string_list(value, path: str) -> list[str]:
    if not isinstance(value, list):
        raise GraphChangeError(f"{path} must be a list")
    if not all(isinstance(item, str) and item for item in value):
        raise GraphChangeError(f"{path} must contain non-empty strings")
    if len(set(value)) != len(value):
        raise GraphChangeError(f"{path} contains a duplicate")
    return sorted(value)


def _normalize_nodes(value, path: str) -> list[dict]:
    if not isinstance(value, list):
        raise GraphChangeError(f"{path} must be a list")
    out = []
    seen = set()
    for index, node in enumerate(value):
        if not isinstance(node, dict):
            raise GraphChangeError(f"{path}[{index}] must be an object")
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise GraphChangeError(f"{path}[{index}] needs a non-empty string id")
        if node_id in seen:
            raise GraphChangeError(f"{path} contains duplicate node id {node_id!r}")
        seen.add(node_id)
        out.append(copy.deepcopy(node))
    return sorted(out, key=lambda node: node["id"])


def _edge_key(edge: dict) -> tuple[str, str, str]:
    return edge["from"], edge["to"], edge["rel"]


def _normalize_edges(value, path: str) -> list[dict]:
    if not isinstance(value, list):
        raise GraphChangeError(f"{path} must be a list")
    out = []
    seen = set()
    allowed = {"from", "to", "rel"}
    for index, edge in enumerate(value):
        if not isinstance(edge, dict):
            raise GraphChangeError(f"{path}[{index}] must be an object")
        _strict_keys(edge, allowed, f"{path}[{index}]")
        if not all(isinstance(edge.get(key), str) and edge[key] for key in allowed):
            raise GraphChangeError(
                f"{path}[{index}] needs non-empty string from, to, and rel"
            )
        normalized = {key: edge[key] for key in ("from", "to", "rel")}
        key = _edge_key(normalized)
        if key in seen:
            raise GraphChangeError(f"{path} contains duplicate edge {key!r}")
        seen.add(key)
        out.append(normalized)
    return sorted(out, key=_edge_key)


def _normalize_concepts(value, path: str) -> dict:
    if not isinstance(value, dict):
        raise GraphChangeError(f"{path} must be an object")
    out = {}
    for name, concept in value.items():
        if not isinstance(name, str) or not name:
            raise GraphChangeError(f"{path} concept names must be non-empty strings")
        if not isinstance(concept, dict):
            raise GraphChangeError(f"{path}.{name} must be an object")
        out[name] = copy.deepcopy(concept)
    return {name: out[name] for name in sorted(out)}


def _normalize_changes(value) -> dict:
    if not isinstance(value, dict):
        raise GraphChangeError("request.changes must be an object")
    _strict_keys(value, set(_CHANGE_FIELDS), "request.changes")
    out = {
        "add_nodes": _normalize_nodes(value.get("add_nodes", []), "changes.add_nodes"),
        "replace_nodes": _normalize_nodes(
            value.get("replace_nodes", []), "changes.replace_nodes"
        ),
        "remove_nodes": _normalize_string_list(
            value.get("remove_nodes", []), "changes.remove_nodes"
        ),
        "add_edges": _normalize_edges(value.get("add_edges", []), "changes.add_edges"),
        "remove_edges": _normalize_edges(
            value.get("remove_edges", []), "changes.remove_edges"
        ),
        "set_concepts": _normalize_concepts(
            value.get("set_concepts", {}), "changes.set_concepts"
        ),
        "remove_concepts": _normalize_string_list(
            value.get("remove_concepts", []), "changes.remove_concepts"
        ),
    }

    add_ids = {node["id"] for node in out["add_nodes"]}
    replace_ids = {node["id"] for node in out["replace_nodes"]}
    remove_ids = set(out["remove_nodes"])
    overlap = (add_ids & replace_ids) | (add_ids & remove_ids) | (replace_ids & remove_ids)
    if overlap:
        raise GraphChangeError(
            "node ids cannot occur in more than one add/replace/remove operation: "
            + ", ".join(sorted(overlap))
        )
    edge_overlap = (
        {_edge_key(edge) for edge in out["add_edges"]}
        & {_edge_key(edge) for edge in out["remove_edges"]}
    )
    if edge_overlap:
        raise GraphChangeError(
            "edges cannot be both added and removed: "
            + ", ".join(repr(item) for item in sorted(edge_overlap))
        )
    concept_overlap = set(out["set_concepts"]) & set(out["remove_concepts"])
    if concept_overlap:
        raise GraphChangeError(
            "concepts cannot be both set and removed: "
            + ", ".join(sorted(concept_overlap))
        )

    operation_count = sum(
        len(out[field])
        for field in _CHANGE_FIELDS
    )
    if operation_count == 0:
        raise GraphChangeError("request contains no graph changes")
    if operation_count > MAX_GRAPH_CHANGE_OPERATIONS:
        raise GraphChangeError(
            f"request has {operation_count} operations; maximum is "
            f"{MAX_GRAPH_CHANGE_OPERATIONS}"
        )
    return out


def validate_change_request(request: dict) -> dict:
    """Validate and deterministically normalize a graph change request."""
    if not isinstance(request, dict):
        raise GraphChangeError("graph change request must be an object")
    _validate_json_value(request, path="request", state={"values": 0})
    if len(_canonical_bytes(request)) > MAX_GRAPH_CHANGE_REQUEST_BYTES:
        raise GraphChangeError(
            f"request exceeds {MAX_GRAPH_CHANGE_REQUEST_BYTES} canonical JSON bytes"
        )
    _strict_keys(
        request,
        {"schema_version", "base_graph_hash", "description", "changes"},
        "request",
    )
    if request.get("schema_version") != GRAPH_CHANGE_REQUEST_SCHEMA:
        raise GraphChangeError(
            f"request.schema_version must be {GRAPH_CHANGE_REQUEST_SCHEMA!r}"
        )
    base_hash = request.get("base_graph_hash")
    if not isinstance(base_hash, str) or not _HASH_RE.fullmatch(base_hash):
        raise GraphChangeError("request.base_graph_hash must be a canonical graph SHA-256 id")
    description = request.get("description", "")
    if not isinstance(description, str):
        raise GraphChangeError("request.description must be a string")
    if len(description.encode("utf-8")) > MAX_GRAPH_CHANGE_DESCRIPTION_BYTES:
        raise GraphChangeError(
            f"request.description exceeds {MAX_GRAPH_CHANGE_DESCRIPTION_BYTES} bytes"
        )
    return {
        "schema_version": GRAPH_CHANGE_REQUEST_SCHEMA,
        "base_graph_hash": base_hash,
        "description": description,
        "changes": _normalize_changes(request.get("changes")),
    }


def _validate_graph_shape(raw: dict):
    if not isinstance(raw, dict):
        raise GraphChangeError("result graph must be an object")
    if not isinstance(raw.get("nodes"), list):
        raise GraphChangeError("result graph needs a nodes list")
    if "edges" in raw and not isinstance(raw["edges"], list):
        raise GraphChangeError("result graph edges must be a list")
    if "concepts" in raw and not isinstance(raw["concepts"], dict):
        raise GraphChangeError("result graph concepts must be an object")
    for index, node in enumerate(raw["nodes"]):
        if (
            not isinstance(node, dict)
            or not isinstance(node.get("id"), str)
            or not node["id"]
        ):
            raise GraphChangeError(f"result graph node #{index} needs a non-empty string id")
        for key in ("type", "status", "path", "script"):
            if key in node and node[key] is not None and not isinstance(node[key], str):
                raise GraphChangeError(
                    f"result graph node {node['id']!r} field {key!r} must be a string"
                )
        if "run_ids" in node and (
            not isinstance(node["run_ids"], list)
            or not all(isinstance(item, str) for item in node["run_ids"])
        ):
            raise GraphChangeError(
                f"result graph node {node['id']!r} run_ids must be a string list"
            )
    for index, edge in enumerate(raw.get("edges", [])):
        if not isinstance(edge, dict):
            raise GraphChangeError(f"result graph edge #{index} must be an object")
    for name, concept in raw.get("concepts", {}).items():
        if not isinstance(name, str) or not isinstance(concept, dict):
            raise GraphChangeError(
                "result graph concepts must have string names and object values"
            )


def _validate_graph(cfg, raw: dict, label: str):
    _validate_graph_shape(raw)
    issues = engine.structural_issues(cfg, raw=raw)
    if issues:
        kind, node_id, detail = issues[0]
        raise GraphChangeError(f"{label} graph is structurally invalid: {kind} at {node_id}: {detail}")


def _apply_changes(raw: dict, changes: dict) -> dict:
    current_ids = {node["id"] for node in raw["nodes"]}
    add_by_id = {node["id"]: node for node in changes["add_nodes"]}
    replace_by_id = {node["id"]: node for node in changes["replace_nodes"]}
    remove_ids = set(changes["remove_nodes"])

    already_present = sorted(set(add_by_id) & current_ids)
    if already_present:
        raise GraphChangeError("cannot add existing node(s): " + ", ".join(already_present))
    missing_replace = sorted(set(replace_by_id) - current_ids)
    if missing_replace:
        raise GraphChangeError(
            "cannot replace missing node(s): " + ", ".join(missing_replace)
        )
    missing_remove = sorted(remove_ids - current_ids)
    if missing_remove:
        raise GraphChangeError("cannot remove missing node(s): " + ", ".join(missing_remove))

    nodes = []
    for node in raw["nodes"]:
        node_id = node["id"]
        if node_id in remove_ids:
            continue
        nodes.append(replace_by_id.get(node_id, node))
    nodes.extend(add_by_id[node_id] for node_id in sorted(add_by_id))

    current_edges = list(raw.get("edges", []))
    edge_positions = {}
    for index, edge in enumerate(current_edges):
        if all(key in edge for key in ("from", "to", "rel")):
            edge_positions.setdefault(_edge_key(edge), []).append(index)
    add_keys = {_edge_key(edge) for edge in changes["add_edges"]}
    existing_add = sorted(add_keys & set(edge_positions))
    if existing_add:
        raise GraphChangeError(
            "cannot add existing edge(s): "
            + ", ".join(repr(item) for item in existing_add)
        )
    remove_keys = {_edge_key(edge) for edge in changes["remove_edges"]}
    missing_edges = sorted(remove_keys - set(edge_positions))
    if missing_edges:
        raise GraphChangeError(
            "cannot remove missing edge(s): "
            + ", ".join(repr(item) for item in missing_edges)
        )
    ambiguous = sorted(key for key in remove_keys if len(edge_positions[key]) != 1)
    if ambiguous:
        raise GraphChangeError(
            "cannot deterministically remove duplicate edge(s): "
            + ", ".join(repr(item) for item in ambiguous)
        )
    edges = [edge for edge in current_edges if _edge_key(edge) not in remove_keys]
    edges.extend(changes["add_edges"])

    concepts = dict(raw.get("concepts", {}))
    missing_concepts = sorted(set(changes["remove_concepts"]) - set(concepts))
    if missing_concepts:
        raise GraphChangeError(
            "cannot remove missing concept(s): " + ", ".join(missing_concepts)
        )
    for name in changes["remove_concepts"]:
        del concepts[name]
    concepts.update(changes["set_concepts"])

    candidate = dict(raw)
    candidate["nodes"] = nodes
    candidate["edges"] = edges
    candidate["concepts"] = concepts
    return candidate


def _proposal_core(normalized_request: dict, result_hash: str) -> dict:
    return {
        "schema_version": GRAPH_CHANGE_PROPOSAL_SCHEMA,
        "base_graph_hash": normalized_request["base_graph_hash"],
        "result_graph_hash": result_hash,
        "description": normalized_request["description"],
        "changes": normalized_request["changes"],
    }


def _content_address(core: dict) -> str:
    return PROPOSAL_ID_PREFIX + hashlib.sha256(_canonical_bytes(core)).hexdigest()


def build_graph_change_proposal(cfg, request: dict) -> dict:
    """Build a content-addressed proposal against the exact current graph.

    This function is read-only.  A concurrent graph replacement can make the returned proposal
    stale, in which case :func:`apply_graph_change_proposal` refuses it rather than rebasing it.
    """
    normalized = validate_change_request(request)
    raw = engine.load_raw(cfg)
    _validate_graph(cfg, raw, "base")
    current_hash = canonical_graph_hash(raw)
    if normalized["base_graph_hash"] != current_hash:
        raise GraphChangeError(
            "proposal base hash does not match the current graph: "
            f"expected {normalized['base_graph_hash']}, found {current_hash}"
        )
    candidate = _apply_changes(raw, normalized["changes"])
    _validate_graph(cfg, candidate, "result")
    result_hash = canonical_graph_hash(candidate)
    if result_hash == current_hash:
        raise GraphChangeError("request is a no-op; result graph equals the base graph")
    core = _proposal_core(normalized, result_hash)
    return {"proposal_id": _content_address(core), **core}


def validate_graph_change_proposal(proposal: dict) -> dict:
    """Validate proposal shape, bounds, normalization, and content address."""
    if not isinstance(proposal, dict):
        raise GraphChangeError("graph change proposal must be an object")
    _validate_json_value(proposal, path="proposal", state={"values": 0})
    _strict_keys(
        proposal,
        {
            "schema_version",
            "proposal_id",
            "base_graph_hash",
            "result_graph_hash",
            "description",
            "changes",
        },
        "proposal",
    )
    if proposal.get("schema_version") != GRAPH_CHANGE_PROPOSAL_SCHEMA:
        raise GraphChangeError(
            f"proposal.schema_version must be {GRAPH_CHANGE_PROPOSAL_SCHEMA!r}"
        )
    proposal_id = proposal.get("proposal_id")
    if not isinstance(proposal_id, str) or not _PROPOSAL_ID_RE.fullmatch(proposal_id):
        raise GraphChangeError("proposal.proposal_id must be a graph-change SHA-256 id")
    result_hash = proposal.get("result_graph_hash")
    if not isinstance(result_hash, str) or not _HASH_RE.fullmatch(result_hash):
        raise GraphChangeError("proposal.result_graph_hash must be a canonical graph SHA-256 id")
    request = {
        "schema_version": GRAPH_CHANGE_REQUEST_SCHEMA,
        "base_graph_hash": proposal.get("base_graph_hash"),
        "description": proposal.get("description", ""),
        "changes": proposal.get("changes"),
    }
    normalized = validate_change_request(request)
    if normalized["changes"] != proposal.get("changes"):
        raise GraphChangeError("proposal changes are not in deterministic normalized order")
    if normalized["description"] != proposal.get("description"):
        raise GraphChangeError("proposal description is not normalized")
    if normalized["base_graph_hash"] == result_hash:
        raise GraphChangeError("proposal result hash equals its base hash")
    core = _proposal_core(normalized, result_hash)
    expected_id = _content_address(core)
    if proposal_id != expected_id:
        raise GraphChangeError(
            f"proposal content-address mismatch: declared {proposal_id}, expected {expected_id}"
        )
    return {"proposal_id": proposal_id, **core}


def _atomic_write_graph(graph_path: Path, raw: dict):
    graph_path = Path(graph_path)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=graph_path.parent,
            prefix=graph_path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(raw, handle, indent=2, ensure_ascii=False, allow_nan=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, graph_path)
        temp_path = None
    except (OSError, TypeError, ValueError, RecursionError) as exc:
        raise GraphChangeError(f"cannot atomically write graph {graph_path}: {exc}") from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def apply_graph_change_proposal(cfg, proposal: dict) -> dict:
    """Apply one proposal under the graph lock, with drift and tamper checks.

    Returns a JSON-ready object whose status is ``applied`` or ``already_applied``.  The latter
    makes retries safe when a caller did not receive the first success response.
    """
    normalized = validate_graph_change_proposal(proposal)
    try:
        with engine._graph_lock(cfg.graph_path):
            raw = engine.load_raw(cfg)
            _validate_graph(cfg, raw, "current")
            current_hash = canonical_graph_hash(raw)
            if current_hash == normalized["result_graph_hash"]:
                return {
                    "status": "already_applied",
                    "proposal_id": normalized["proposal_id"],
                    "graph_hash": current_hash,
                }
            if current_hash != normalized["base_graph_hash"]:
                raise GraphChangeError(
                    "graph drifted from the proposal base: "
                    f"expected {normalized['base_graph_hash']}, found {current_hash}"
                )
            candidate = _apply_changes(raw, normalized["changes"])
            _validate_graph(cfg, candidate, "result")
            result_hash = canonical_graph_hash(candidate)
            if result_hash != normalized["result_graph_hash"]:
                raise GraphChangeError(
                    "proposal result hash mismatch after deterministic application: "
                    f"declared {normalized['result_graph_hash']}, computed {result_hash}"
                )
            _atomic_write_graph(Path(cfg.graph_path), candidate)
            return {
                "status": "applied",
                "proposal_id": normalized["proposal_id"],
                "graph_hash": result_hash,
            }
    except TimeoutError as exc:
        raise GraphChangeError(f"cannot acquire graph lock for {cfg.graph_path}: {exc}") from exc


__all__ = [
    "GRAPH_CHANGE_REQUEST_SCHEMA",
    "GRAPH_CHANGE_PROPOSAL_SCHEMA",
    "MAX_GRAPH_CHANGE_OPERATIONS",
    "GraphChangeError",
    "canonical_graph_hash",
    "validate_change_request",
    "build_graph_change_proposal",
    "validate_graph_change_proposal",
    "apply_graph_change_proposal",
]
