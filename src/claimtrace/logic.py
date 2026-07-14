"""Portable, deterministic symbolic derivations for project-defined claims.

The logic layer is deliberately data-only. Projects provide a typed vocabulary and
function-free Horn rules as JSON. The normal agent interface selects project-owned
bindings; a low-level import may provide grounded input facts and a target. Neither can
provide the computed closure, proof state, or proof certificate.

Logical derivability is conditional on the project-authored rules and fact mappings. It
is not a claim that the rules are scientifically sound or that the premises are true.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import stat
import string
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import engine
from .config import strict_json_loads


VOCABULARY_SCHEMA = "claimtrace.symbolic-vocabulary/1"
RULE_PACK_SCHEMA = "claimtrace.symbolic-rules/1"
DERIVATION_SCHEMA = "claimtrace.symbolic-derivation/1"
SELECTION_SCHEMA = "claimtrace.symbolic-selection/1"
EVIDENCE_PLAN_SCHEMA = "claimtrace.symbolic-evidence-plan/1"
PLAN_REQUEST_SCHEMA = "claimtrace.symbolic-plan-request/1"
DERIVATION_ID_RE = re.compile(r"^derivation:sha256:([0-9a-f]{64})$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._:/-]{0,199}$")
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
RFC3339_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
CORE_TYPES = {
    "ct:string", "ct:symbol", "ct:node_id", "ct:integer", "ct:decimal", "ct:boolean",
}
BASE_TYPES = CORE_TYPES
POLARITIES = {"positive", "negative"}
COMPARATORS = {"eq", "ne", "gt", "gte", "lt", "lte"}
PROOF_STATES = {"derivable", "refutable", "conflict", "unknown"}
RESULT_TYPES = {"artifact", "figure", "experiment", "data"}
MAX_FACTS = 10000
MAX_FIRINGS = 50000
MAX_ROUNDS = 100
MAX_PROOF_CANDIDATES = 50000
MAX_INPUT_FACTS = 5000
MAX_RESULT_IDS = 5000
MAX_TYPES = 1000
MAX_UNITS = 1000
MAX_PREDICATES = 2000
MAX_PREDICATE_ARITY = 64
MAX_RENDERERS = 4000
MAX_RULES = 2000
MAX_RULE_BODY_ATOMS = 32
MAX_RULE_CONSTRAINTS = 128
MAX_LOGIC_BINDINGS = 2000
MAX_NUMERIC_LEXICAL = 1000
MAX_JOIN_WORK = 200000
MAX_PROOF_WORK = 200000
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_ARTIFACT_TOTAL_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_PROVENANCE_BYTES = 64 * 1024 * 1024 * 1024
MAX_LOGIC_ASSET_BYTES = 16 * 1024 * 1024
MAX_DERIVATION_DOCUMENT_BYTES = 64 * 1024 * 1024
MAX_DERIVATION_FILES = 10000
MAX_DERIVATION_STORE_BYTES = 256 * 1024 * 1024
MAX_DRIFT_ITEMS = 1000
_PROCESS_LOCK = threading.Lock()


class LogicError(Exception):
    """A vocabulary, rule pack, derivation, or grounded fact is invalid."""


class _JsonNumber:
    """Preserve whether an artifact scalar was JSON numeric rather than a string."""

    __slots__ = ("kind", "lexical")

    def __init__(self, kind: str, lexical: str):
        self.kind = kind
        self.lexical = lexical


def _canonical_bytes(value) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise LogicError("symbolic data contains a lone Unicode surrogate") from exc


def _sha256(value) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


@contextmanager
def _checked_regular_file(path_value, label: str, *, limit: int | None = None):
    """Yield one stable regular-file descriptor after rejecting symlink traversal."""
    path = Path(path_value)
    absolute = Path(os.path.abspath(path))
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LogicError(f"{label} is unavailable: {path}: {exc}") from exc
    if os.path.normcase(str(resolved)) != os.path.normcase(str(absolute)):
        raise LogicError(f"{label} must not traverse a symbolic link: {path}")
    try:
        declared_size = path.stat().st_size
    except OSError as exc:
        raise LogicError(f"cannot inspect {label} {path}: {exc}") from exc
    if limit is not None and declared_size > limit:
        raise LogicError(f"{label} exceeds the {limit}-byte read limit")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise LogicError(f"cannot open {label} {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise LogicError(f"{label} is not a regular file: {path}")
        if limit is not None and before.st_size > limit:
            raise LogicError(f"{label} exceeds the {limit}-byte read limit")
        yield descriptor, before
        after = os.fstat(descriptor)
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise LogicError(f"{label} changed while it was being read: {path}")
        try:
            final_path = path.resolve(strict=True)
            current = os.stat(path, follow_symlinks=False)
        except (OSError, RuntimeError) as exc:
            raise LogicError(f"{label} changed while it was being read: {path}") from exc
        if (os.path.normcase(str(final_path)) != os.path.normcase(str(absolute))
                or not stat.S_ISREG(current.st_mode)
                or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)):
            raise LogicError(f"{label} path changed or traversed a symbolic link: {path}")
    finally:
        os.close(descriptor)


def _safe_regular_file_bytes(path_value, label: str, limit: int) -> bytes:
    """Read a bounded regular file through one checked handle without symlink traversal."""
    with _checked_regular_file(path_value, label, limit=limit) as (descriptor, before):
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > limit:
            raise LogicError(f"{label} exceeds the {limit}-byte read limit")
        if len(data) != before.st_size:
            raise LogicError(f"{label} changed while it was being read")
        return data


def _safe_regular_file_sha256(path_value, label: str, limit: int) -> tuple[str, int]:
    """Stream-hash one stable regular file without retaining its bytes."""
    with _checked_regular_file(path_value, label, limit=limit) as (descriptor, before):
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise LogicError(f"{label} exceeds the {limit}-byte hash limit")
            digest.update(chunk)
        if total != before.st_size:
            raise LogicError(f"{label} changed while it was being hashed")
        return digest.hexdigest(), total


def load_logic_asset(source) -> object:
    """Strictly read one bounded, non-symlinked data-only logic asset."""
    if not isinstance(source, (str, os.PathLike)):
        raise LogicError("logic asset source must be a filesystem path")
    path = Path(source)
    try:
        data = _safe_regular_file_bytes(path, "logic asset", MAX_LOGIC_ASSET_BYTES)
        return strict_json_loads(data.decode("utf-8-sig"), str(path))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RecursionError,
            LogicError) as exc:
        if isinstance(exc, LogicError):
            raise
        raise LogicError(f"cannot read logic asset {path}: {exc}") from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_time(value: object) -> None:
    if not isinstance(value, str) or not RFC3339_UTC_RE.fullmatch(value):
        raise LogicError("recorded_at must be a strict RFC 3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise LogicError("recorded_at is not a valid timestamp") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise LogicError("recorded_at must be UTC")


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise LogicError(f"{label} must be a portable ASCII identifier")
    return value


def _name(value: object, label: str) -> str:
    if not isinstance(value, str) or not NAME_RE.fullmatch(value):
        raise LogicError(f"{label} must be a portable ASCII name")
    return value


def _bounded_text(value: object, label: str, limit: int, *, nonempty=False) -> str:
    if (not isinstance(value, str) or len(value) > limit
            or (nonempty and not value)
            or any(0xD800 <= ord(char) <= 0xDFFF for char in value)):
        qualifier = "non-empty " if nonempty else ""
        raise LogicError(f"{label} must be a bounded {qualifier}Unicode scalar string")
    return value


def _is_link_like(path: Path) -> bool:
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        attributes = 0
    is_junction = getattr(path, "is_junction", lambda: False)
    return path.is_symlink() or bool(is_junction()) or bool(attributes & 0x400)


def _base_type(type_id: str, types: dict[str, str]) -> str:
    if not isinstance(type_id, str):
        raise LogicError("type ids must be strings")
    return types.get(type_id, type_id)


def _normal_decimal(value: object) -> str:
    if not isinstance(value, str):
        raise LogicError("decimal values must be JSON strings")
    if len(value) > MAX_NUMERIC_LEXICAL:
        raise LogicError("decimal value exceeds the canonical length limit")
    if not re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?", value):
        raise LogicError(f"decimal value is not canonical: {value!r}")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise LogicError(f"invalid decimal value: {value!r}") from exc
    if not number.is_finite():
        raise LogicError("decimal values must be finite")
    if number == 0:
        return "0"
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _normal_artifact_decimal(value: str) -> str:
    if not isinstance(value, str) or len(value) > 200:
        raise LogicError("artifact decimal lexical form is invalid or too long")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise LogicError("artifact decimal lexical form is invalid") from exc
    if not number.is_finite() or abs(number.adjusted()) > 10000:
        raise LogicError("artifact decimal magnitude exceeds the grounding limit")
    return _normal_decimal(format(number, "f"))


def _normal_integer(value: object) -> str:
    if (not isinstance(value, str) or len(value) > MAX_NUMERIC_LEXICAL
            or not re.fullmatch(r"-?(?:0|[1-9]\d*)", value)):
        raise LogicError("integer values must be canonical JSON strings")
    return "0" if value == "-0" else value


def _normal_value(type_id: str, value: object, types: dict[str, str]):
    base = _base_type(type_id, types)
    if base == "ct:decimal":
        return _normal_decimal(value)
    if base == "ct:integer":
        return _normal_integer(value)
    if base == "ct:boolean":
        if not isinstance(value, bool):
            raise LogicError("boolean values must be JSON booleans")
        return value
    if base in {"ct:string", "ct:symbol", "ct:node_id"}:
        _bounded_text(value, f"{base} value", 2000)
        if base in {"ct:symbol", "ct:node_id"} and not value:
            raise LogicError(f"{base} values must not be empty")
        if base == "ct:node_id":
            _identifier(value, "ct:node_id value")
        return value
    raise LogicError(f"unknown value type: {type_id}")


def _strict_file(path_value) -> object:
    return load_logic_asset(path_value)


def _template_fields(template: str) -> set[str]:
    _bounded_text(template, "renderer template", 4000, nonempty=True)
    fields = set()
    try:
        parsed = string.Formatter().parse(template)
    except ValueError as exc:
        raise LogicError(f"invalid renderer template: {exc}") from exc
    for _literal, field, spec, conversion in parsed:
        if field is None:
            continue
        if not NAME_RE.fullmatch(field) or spec or conversion:
            raise LogicError("renderer templates allow simple {argument} placeholders only")
        fields.add(field)
    return fields


def load_vocabulary(source) -> dict:
    """Load and canonicalize one project-owned symbolic vocabulary."""
    raw = _strict_file(source) if not isinstance(source, dict) else _copy(source)
    if not isinstance(raw, dict) or set(raw) != {
            "schema_version", "id", "version", "types", "units", "predicates", "renderers"}:
        raise LogicError("symbolic vocabulary has unknown or missing top-level fields")
    if raw["schema_version"] != VOCABULARY_SCHEMA:
        raise LogicError(f"unsupported symbolic vocabulary schema: {raw['schema_version']!r}")
    vocabulary_id = _identifier(raw["id"], "vocabulary.id")
    _bounded_text(raw["version"], "vocabulary.version", 100, nonempty=True)

    if not isinstance(raw["types"], list):
        raise LogicError("vocabulary.types must be a list")
    if len(raw["types"]) > MAX_TYPES:
        raise LogicError("vocabulary type count exceeds the deterministic limit")
    types = {}
    normalized_types = []
    for item in raw["types"]:
        if not isinstance(item, dict) or set(item) != {"id", "base"}:
            raise LogicError("each vocabulary type requires exactly id and base")
        type_id = _identifier(item["id"], "type.id")
        if type_id in CORE_TYPES or type_id in types:
            raise LogicError(f"duplicate or reserved type id: {type_id}")
        if not isinstance(item["base"], str) or item["base"] not in BASE_TYPES:
            raise LogicError(f"custom type {type_id} has unknown base {item['base']!r}")
        types[type_id] = item["base"]
        normalized_types.append({"id": type_id, "base": item["base"]})

    if (not isinstance(raw["units"], list)
            or not all(isinstance(item, str) and ID_RE.fullmatch(item) for item in raw["units"])):
        raise LogicError("vocabulary.units must be a list of portable identifiers")
    if len(raw["units"]) > MAX_UNITS:
        raise LogicError("vocabulary unit count exceeds the deterministic limit")
    if len(raw["units"]) != len(set(raw["units"])):
        raise LogicError("vocabulary contains duplicate unit ids")
    units = set(raw["units"])

    if not isinstance(raw["predicates"], list) or not raw["predicates"]:
        raise LogicError("vocabulary.predicates must be a non-empty list")
    if len(raw["predicates"]) > MAX_PREDICATES:
        raise LogicError("vocabulary predicate count exceeds the deterministic limit")
    predicates = {}
    normalized_predicates = []
    available_types = CORE_TYPES | set(types)
    for item in raw["predicates"]:
        if not isinstance(item, dict) or set(item) != {"id", "kind", "arguments"}:
            raise LogicError("each predicate requires exactly id, kind, and arguments")
        predicate_id = _identifier(item["id"], "predicate.id")
        if predicate_id in predicates:
            raise LogicError(f"duplicate predicate id: {predicate_id}")
        if not isinstance(item["kind"], str) or item["kind"] not in {"input", "derived"}:
            raise LogicError(f"predicate {predicate_id} has invalid kind")
        if not isinstance(item["arguments"], list):
            raise LogicError(f"predicate {predicate_id} arguments must be a list")
        if len(item["arguments"]) > MAX_PREDICATE_ARITY:
            raise LogicError(f"predicate {predicate_id} exceeds the arity limit")
        arguments = []
        seen_names = set()
        for argument in item["arguments"]:
            if not isinstance(argument, dict) or set(argument) != {"name", "type", "unit"}:
                raise LogicError("predicate arguments require exactly name, type, and unit")
            name = _name(argument["name"], "predicate argument")
            if name in seen_names:
                raise LogicError(f"duplicate argument {name} in {predicate_id}")
            seen_names.add(name)
            if (not isinstance(argument["type"], str)
                    or argument["type"] not in available_types):
                raise LogicError(f"argument {name} uses unknown type {argument['type']!r}")
            if (argument["unit"] is not None
                    and (not isinstance(argument["unit"], str)
                         or argument["unit"] not in units)):
                raise LogicError(f"argument {name} uses unknown unit {argument['unit']!r}")
            arguments.append({
                "name": name, "type": argument["type"], "unit": argument["unit"],
            })
        normalized = {
            "id": predicate_id, "kind": item["kind"],
            "arguments": arguments,
        }
        predicates[predicate_id] = normalized
        normalized_predicates.append(normalized)

    if not isinstance(raw["renderers"], list):
        raise LogicError("vocabulary.renderers must be a list")
    if len(raw["renderers"]) > MAX_RENDERERS:
        raise LogicError("vocabulary renderer count exceeds the deterministic limit")
    normalized_renderers = []
    renderer_ids = set()
    renderer_keys = set()
    for item in raw["renderers"]:
        if not isinstance(item, dict) or set(item) != {
                "id", "predicate", "polarity", "language", "template"}:
            raise LogicError("each renderer has an invalid shape")
        renderer_id = _identifier(item["id"], "renderer.id")
        if renderer_id in renderer_ids:
            raise LogicError(f"duplicate renderer id: {renderer_id}")
        renderer_ids.add(renderer_id)
        if not isinstance(item["predicate"], str) or item["predicate"] not in predicates:
            raise LogicError(f"renderer references unknown predicate: {item['predicate']}")
        if not isinstance(item["polarity"], str) or item["polarity"] not in POLARITIES:
            raise LogicError("renderer polarity must be positive or negative")
        _bounded_text(item["language"], "renderer.language", 100, nonempty=True)
        key = (item["predicate"], item["polarity"], item["language"])
        if key in renderer_keys:
            raise LogicError(f"duplicate renderer for {key}")
        renderer_keys.add(key)
        expected_fields = {
            argument["name"] for argument in predicates[item["predicate"]]["arguments"]
        }
        fields = _template_fields(item["template"])
        if fields != expected_fields:
            raise LogicError(
                f"renderer {renderer_id} placeholders do not exactly match predicate arguments"
            )
        normalized_renderers.append({
            "id": renderer_id, "predicate": item["predicate"],
            "polarity": item["polarity"], "language": item["language"],
            "template": item["template"],
        })

    return {
        "schema_version": VOCABULARY_SCHEMA,
        "id": vocabulary_id,
        "version": raw["version"],
        "types": sorted(normalized_types, key=lambda item: item["id"]),
        "units": sorted(raw["units"]),
        "predicates": sorted(normalized_predicates, key=lambda item: item["id"]),
        "renderers": sorted(normalized_renderers, key=lambda item: item["id"]),
    }


def _vocabulary_maps(vocabulary: dict):
    types = {item["id"]: item["base"] for item in vocabulary["types"]}
    predicates = {item["id"]: item for item in vocabulary["predicates"]}
    return types, predicates


def _normal_ground_term(value, expected, types):
    if not isinstance(value, dict) or set(value) != {"type", "value", "unit"}:
        raise LogicError("ground terms require exactly type, value, and unit")
    if value["type"] != expected["type"]:
        raise LogicError(
            f"term type {value['type']!r} does not match expected {expected['type']!r}"
        )
    if value["unit"] != expected["unit"]:
        raise LogicError(
            f"term unit {value['unit']!r} does not match expected {expected['unit']!r}"
        )
    return {
        "type": value["type"],
        "value": _normal_value(value["type"], value["value"], types),
        "unit": value["unit"],
    }


def _normal_ground_atom(atom, vocabulary, *, expected_kind=None):
    types, predicates = _vocabulary_maps(vocabulary)
    if not isinstance(atom, dict) or set(atom) != {"predicate", "polarity", "arguments"}:
        raise LogicError("ground atoms require exactly predicate, polarity, and arguments")
    if not isinstance(atom["predicate"], str):
        raise LogicError("atom predicate must be a string")
    predicate = predicates.get(atom["predicate"])
    if predicate is None:
        raise LogicError(f"unknown predicate: {atom.get('predicate')!r}")
    if expected_kind and predicate["kind"] != expected_kind:
        raise LogicError(
            f"predicate {predicate['id']} must be {expected_kind}, not {predicate['kind']}"
        )
    if not isinstance(atom["polarity"], str) or atom["polarity"] not in POLARITIES:
        raise LogicError("atom polarity must be positive or negative")
    if not isinstance(atom["arguments"], dict):
        raise LogicError("atom arguments must be an object")
    expected_arguments = {item["name"]: item for item in predicate["arguments"]}
    if set(atom["arguments"]) != set(expected_arguments):
        raise LogicError(f"atom arguments do not match predicate {predicate['id']}")
    arguments = {
        name: _normal_ground_term(atom["arguments"][name], expected_arguments[name], types)
        for name in sorted(expected_arguments)
    }
    return {
        "predicate": predicate["id"], "polarity": atom["polarity"],
        "arguments": arguments,
    }


def _normal_const(value, expected, types):
    if not isinstance(value, dict) or set(value) != {"type", "value", "unit"}:
        raise LogicError("rule constants require exactly type, value, and unit")
    return _normal_ground_term(value, expected, types)


def _normal_pattern_term(term, expected, types, variables, *, allow_new):
    if not isinstance(term, dict) or len(term) != 1:
        raise LogicError("rule terms must contain exactly var or const")
    if "var" in term:
        variable = _name(term["var"], "rule variable")
        signature = (expected["type"], expected["unit"])
        existing = variables.get(variable)
        if existing is None:
            if not allow_new:
                raise LogicError(f"unbound rule variable: {variable}")
            variables[variable] = signature
        elif existing != signature:
            raise LogicError(f"rule variable {variable} is used with incompatible types or units")
        return {"var": variable}
    if "const" in term:
        return {"const": _normal_const(term["const"], expected, types)}
    raise LogicError("rule terms must contain var or const")


def _normal_pattern_atom(atom, predicates, types, variables, *, head=False):
    if not isinstance(atom, dict) or set(atom) != {"predicate", "polarity", "arguments"}:
        raise LogicError("rule atoms have an invalid shape")
    if not isinstance(atom["predicate"], str):
        raise LogicError("rule predicate must be a string")
    predicate = predicates.get(atom["predicate"])
    if predicate is None:
        raise LogicError(f"rule references unknown predicate: {atom.get('predicate')!r}")
    if head and predicate["kind"] != "derived":
        raise LogicError("rule heads must use derived predicates")
    if not isinstance(atom["polarity"], str) or atom["polarity"] not in POLARITIES:
        raise LogicError("rule atom polarity must be positive or negative")
    if not isinstance(atom["arguments"], dict):
        raise LogicError("rule atom arguments must be an object")
    expected = {item["name"]: item for item in predicate["arguments"]}
    if set(atom["arguments"]) != set(expected):
        raise LogicError(f"rule atom arguments do not match predicate {predicate['id']}")
    arguments = {
        name: _normal_pattern_term(
            atom["arguments"][name], expected[name], types, variables, allow_new=not head,
        )
        for name in sorted(expected)
    }
    return {
        "predicate": predicate["id"], "polarity": atom["polarity"],
        "arguments": arguments,
    }


def _normal_constraint(item, variables, types, units):
    if not isinstance(item, dict) or set(item) != {"op", "left", "right"}:
        raise LogicError("rule constraints require exactly op, left, and right")
    if not isinstance(item["op"], str) or item["op"] not in COMPARATORS:
        raise LogicError(f"unsupported rule comparator: {item['op']!r}")

    def term(value):
        if not isinstance(value, dict) or len(value) != 1:
            raise LogicError("constraint terms must contain exactly var or const")
        if "var" in value:
            variable = _name(value["var"], "constraint variable")
            if variable not in variables:
                raise LogicError(f"unbound constraint variable: {variable}")
            return {"var": variable}, variables[variable]
        if "const" in value:
            constant = value["const"]
            if not isinstance(constant, dict) or set(constant) != {"type", "value", "unit"}:
                raise LogicError("constraint constants require type, value, and unit")
            expected = {"type": constant["type"], "unit": constant["unit"]}
            if (not isinstance(constant["type"], str)
                    or constant["type"] not in CORE_TYPES | set(types)):
                raise LogicError(f"constraint uses unknown type: {constant['type']!r}")
            if (constant["unit"] is not None
                    and (not isinstance(constant["unit"], str)
                         or constant["unit"] not in units)):
                raise LogicError(f"constraint uses unknown unit: {constant['unit']!r}")
            return {"const": _normal_const(constant, expected, types)}, (
                constant["type"], constant["unit"],
            )
        raise LogicError("constraint terms must contain var or const")

    left, left_signature = term(item["left"])
    right, right_signature = term(item["right"])
    left_base = _base_type(left_signature[0], types)
    if left_signature != right_signature:
        raise LogicError("constraint operands have incompatible types or units")
    if item["op"] in {"gt", "gte", "lt", "lte"} and left_base not in {
            "ct:decimal", "ct:integer"}:
        raise LogicError("ordered comparisons require numeric operands")
    return {"op": item["op"], "left": left, "right": right}


def load_rule_pack(source, vocabulary: dict) -> dict:
    """Load and canonicalize one finite, project-owned rule pack."""
    vocabulary = load_vocabulary(vocabulary)
    raw = _strict_file(source) if not isinstance(source, dict) else _copy(source)
    if not isinstance(raw, dict) or set(raw) != {
            "schema_version", "id", "version", "vocabulary_id", "rules"}:
        raise LogicError("symbolic rule pack has unknown or missing top-level fields")
    if raw["schema_version"] != RULE_PACK_SCHEMA:
        raise LogicError(f"unsupported symbolic rule schema: {raw['schema_version']!r}")
    rule_pack_id = _identifier(raw["id"], "rule_pack.id")
    _bounded_text(raw["version"], "rule_pack.version", 100, nonempty=True)
    if raw["vocabulary_id"] != vocabulary["id"]:
        raise LogicError("rule pack vocabulary_id does not match the selected vocabulary")
    if not isinstance(raw["rules"], list) or not raw["rules"]:
        raise LogicError("rule_pack.rules must be a non-empty list")
    if len(raw["rules"]) > MAX_RULES:
        raise LogicError("rule-pack rule count exceeds the deterministic limit")
    types, predicates = _vocabulary_maps(vocabulary)
    normalized_rules = []
    rule_ids = set()
    for item in raw["rules"]:
        if not isinstance(item, dict) or set(item) != {"id", "when", "where", "then"}:
            raise LogicError("each symbolic rule requires exactly id, when, where, and then")
        rule_id = _identifier(item["id"], "rule.id")
        if rule_id in rule_ids:
            raise LogicError(f"duplicate rule id: {rule_id}")
        rule_ids.add(rule_id)
        if not isinstance(item["when"], list) or not item["when"]:
            raise LogicError(f"rule {rule_id} must have at least one body atom")
        if len(item["when"]) > MAX_RULE_BODY_ATOMS:
            raise LogicError(f"rule {rule_id} exceeds the body-atom limit")
        variables = {}
        when = [
            _normal_pattern_atom(atom, predicates, types, variables)
            for atom in item["when"]
        ]
        if not isinstance(item["where"], list):
            raise LogicError(f"rule {rule_id} where must be a list")
        if len(item["where"]) > MAX_RULE_CONSTRAINTS:
            raise LogicError(f"rule {rule_id} exceeds the constraint limit")
        where = [
            _normal_constraint(value, variables, types, set(vocabulary["units"]))
            for value in item["where"]
        ]
        then = _normal_pattern_atom(item["then"], predicates, types, variables, head=True)
        normalized_rules.append({
            "id": rule_id,
            "when": sorted(when, key=_canonical_bytes),
            "where": sorted(where, key=_canonical_bytes),
            "then": then,
        })
    return {
        "schema_version": RULE_PACK_SCHEMA,
        "id": rule_pack_id,
        "version": raw["version"],
        "vocabulary_id": vocabulary["id"],
        "rules": sorted(normalized_rules, key=lambda item: item["id"]),
    }


def _fact_id(atom: dict) -> str:
    return f"atom:sha256:{_sha256(atom)}"


def _term_value(term, environment):
    if "var" in term:
        return environment[term["var"]]
    return term["const"]


def _unify(pattern, fact, environment):
    if (pattern["predicate"] != fact["predicate"]
            or pattern["polarity"] != fact["polarity"]):
        return None
    result = dict(environment)
    for name in sorted(pattern["arguments"]):
        wanted = pattern["arguments"][name]
        observed = fact["arguments"][name]
        if "const" in wanted:
            if wanted["const"] != observed:
                return None
            continue
        variable = wanted["var"]
        if variable in result and result[variable] != observed:
            return None
        result[variable] = observed
    return result


def _compare(left, right, op, types):
    base = _base_type(left["type"], types)
    if base == "ct:decimal":
        a, b = Decimal(left["value"]), Decimal(right["value"])
    elif base == "ct:integer":
        a, b = int(left["value"]), int(right["value"])
    else:
        a, b = left["value"], right["value"]
    return {
        "eq": a == b,
        "ne": a != b,
        "gt": a > b,
        "gte": a >= b,
        "lt": a < b,
        "lte": a <= b,
    }[op]


def _instantiate(pattern, environment):
    return {
        "predicate": pattern["predicate"],
        "polarity": pattern["polarity"],
        "arguments": {
            name: _copy(_term_value(term, environment))
            for name, term in sorted(pattern["arguments"].items())
        },
    }


def _rule_sha(rule):
    return f"rule:sha256:{_sha256(rule)}"


def _closure(input_atoms: list[dict], vocabulary: dict, rule_pack: dict):
    """Compute a finite positive closure with explicit, paraconsistent polarity."""
    types, predicates = _vocabulary_maps(vocabulary)
    facts = {_fact_id(atom): atom for atom in input_atoms}
    if len(facts) > MAX_FACTS:
        raise LogicError("symbolic fact limit exceeded before closure")
    steps = []
    seen_steps = set()
    firings = 0
    join_work = 0
    for _round in range(MAX_ROUNDS):
        changed = False
        ordered_facts = [facts[key] for key in sorted(facts)]
        fact_buckets = {}
        for fact in ordered_facts:
            fact_buckets.setdefault(
                (fact["predicate"], fact["polarity"]), [],
            ).append(fact)
        for rule in rule_pack["rules"]:
            candidates = [({}, [])]
            for pattern in rule["when"]:
                expanded = []
                for environment, premise_ids in candidates:
                    for fact in fact_buckets.get(
                            (pattern["predicate"], pattern["polarity"]), []):
                        join_work += 1
                        if join_work > MAX_JOIN_WORK:
                            raise LogicError(
                                "symbolic join work limit exceeded; no partial proof is valid"
                            )
                        unified = _unify(pattern, fact, environment)
                        if unified is not None:
                            expanded.append((unified, [*premise_ids, _fact_id(fact)]))
                            if len(expanded) > MAX_FIRINGS:
                                raise LogicError(
                                    "symbolic join candidate limit exceeded; "
                                    "no partial proof is valid"
                                )
                candidates = sorted(
                    expanded,
                    key=lambda item: (_canonical_bytes(item[0]), tuple(item[1])),
                )
                if not candidates:
                    break
            for environment, premise_ids in candidates:
                if not all(_compare(
                        _term_value(item["left"], environment),
                        _term_value(item["right"], environment),
                        item["op"], types,
                ) for item in rule["where"]):
                    continue
                conclusion = _instantiate(rule["then"], environment)
                conclusion_id = _fact_id(conclusion)
                step = {
                    "rule_id": rule["id"], "rule_sha256": _rule_sha(rule),
                    "premise_fact_ids": sorted(set(premise_ids)),
                    "conclusion_fact_id": conclusion_id,
                    "substitution": {
                        key: _copy(value) for key, value in sorted(environment.items())
                    },
                }
                step_id = _sha256(step)
                if step_id not in seen_steps:
                    firings += 1
                    if firings > MAX_FIRINGS:
                        raise LogicError(
                            "symbolic rule firing limit exceeded; no partial proof is valid"
                        )
                    seen_steps.add(step_id)
                    steps.append(step)
                if conclusion_id in facts:
                    continue
                facts[conclusion_id] = conclusion
                changed = True
                if len(facts) > MAX_FACTS:
                    raise LogicError("symbolic fact limit exceeded; no partial proof is valid")
        if not changed:
            return facts, sorted(
                steps, key=lambda item: (item["conclusion_fact_id"], item["rule_id"]),
            )
    raise LogicError("symbolic closure round limit exceeded; no partial proof is valid")


def _render_atom(atom, vocabulary):
    renderers = [
        item for item in vocabulary["renderers"]
        if item["predicate"] == atom["predicate"]
        and item["polarity"] == atom["polarity"]
        and item["language"] == "en"
    ]
    if not renderers:
        arguments = []
        for name, term in sorted(atom["arguments"].items()):
            value = json.dumps(term["value"], ensure_ascii=False, allow_nan=False)
            unit = f"[{term['unit']}]" if term["unit"] is not None else ""
            arguments.append(f"{name}={term['type']}({value}){unit}")
        return f"{atom['polarity']} {atom['predicate']}({', '.join(arguments)})"
    values = {
        name: ("true" if term["value"] is True else
               "false" if term["value"] is False else str(term["value"]))
        for name, term in atom["arguments"].items()
    }
    return renderers[0]["template"].format_map(values)


def _normal_anchor(anchor, result_ids, fact, fact_index):
    if not isinstance(anchor, dict):
        raise LogicError("fact evidence anchors must be objects")
    allowed = {"result_id", "binding_id"}
    if set(anchor) not in (allowed, allowed | {"fact_index"}):
        raise LogicError(
            "fact evidence anchors require exactly result_id and project binding_id"
        )
    result_id = anchor.get("result_id")
    if result_id not in result_ids:
        raise LogicError(f"anchor result_id is not in the derivation subject: {result_id!r}")
    binding_id = _identifier(anchor.get("binding_id"), "evidence binding_id")
    return {
        "result_id": result_id, "binding_id": binding_id, "fact_index": fact_index,
    }


def _normal_agent_input(agent_input, result_ids, vocabulary):
    if not isinstance(agent_input, dict) or set(agent_input) != {
            "target", "facts", "note", "provenance"}:
        raise LogicError("agent_input requires exactly target, facts, note, and provenance")
    target = _normal_ground_atom(agent_input["target"], vocabulary, expected_kind="derived")
    if not isinstance(agent_input["facts"], list) or not agent_input["facts"]:
        raise LogicError("agent_input.facts must be a non-empty list")
    if len(agent_input["facts"]) > MAX_INPUT_FACTS:
        raise LogicError("agent_input fact count exceeds the deterministic limit")
    facts = []
    anchored_results = set()
    for index, item in enumerate(agent_input["facts"]):
        if not isinstance(item, dict) or set(item) != {"atom", "evidence", "assumption"}:
            raise LogicError("each input fact requires exactly atom, evidence, and assumption")
        atom = _normal_ground_atom(item["atom"], vocabulary, expected_kind="input")
        if not isinstance(item["evidence"], list):
            raise LogicError("fact.evidence must be a list")
        assumption = item["assumption"]
        if assumption is not None:
            _bounded_text(assumption, "fact.assumption", 2000, nonempty=True)
        if bool(item["evidence"]) == bool(assumption):
            raise LogicError("each fact requires evidence or an explicit assumption, not both")
        if assumption is None and len(item["evidence"]) != 1:
            raise LogicError(
                "each grounded fact requires exactly one complete project fact binding"
            )
        provisional = {"atom": atom}
        evidence = [
            _normal_anchor(anchor, result_ids, provisional, index)
            for anchor in item["evidence"]
        ]
        evidence.sort(key=lambda anchor: (anchor["result_id"], anchor["binding_id"]))
        evidence_keys = [
            (anchor["result_id"], anchor["binding_id"]) for anchor in evidence
        ]
        if len(evidence_keys) != len(set(evidence_keys)):
            raise LogicError("fact evidence must not repeat a project binding")
        anchored_results.update(anchor["result_id"] for anchor in evidence)
        facts.append({"atom": atom, "evidence": evidence, "assumption": assumption})
    if set(result_ids) != anchored_results:
        missing = sorted(set(result_ids) - anchored_results)
        raise LogicError(f"every result_id must have evidence in at least one fact: {missing}")
    if agent_input["note"] is not None:
        _bounded_text(agent_input["note"], "agent_input.note", 4000)
    provenance = agent_input["provenance"]
    allowed = {"agent", "model", "skill_version", "prompt_sha256"}
    if not isinstance(provenance, dict) or not set(provenance) <= allowed:
        raise LogicError("agent_input.provenance has unknown fields")
    _bounded_text(
        provenance.get("agent"), "agent_input.provenance.agent", 500, nonempty=True,
    )
    for key, value in provenance.items():
        _bounded_text(value, f"agent_input.provenance.{key}", 500, nonempty=True)
    if "prompt_sha256" in provenance and not SHA256_RE.fullmatch(provenance["prompt_sha256"]):
        raise LogicError("agent_input.provenance.prompt_sha256 must be SHA-256")
    facts = sorted(facts, key=lambda item: _canonical_bytes(item["atom"]))
    fact_ids = [_fact_id(item["atom"]) for item in facts]
    if len(fact_ids) != len(set(fact_ids)):
        raise LogicError("agent_input.facts must not contain duplicate atoms")
    for index, fact in enumerate(facts):
        for anchor in fact["evidence"]:
            anchor["fact_index"] = index
    return {
        "target": target,
        "facts": facts,
        "note": agent_input["note"],
        "provenance": {key: provenance[key] for key in sorted(provenance)},
    }


def _json_with_decimal_strings(data: bytes, source: str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    return json.loads(
        data.decode("utf-8-sig"),
        object_pairs_hook=pairs,
        parse_float=lambda value: _JsonNumber("decimal", value),
        parse_int=lambda value: _JsonNumber("integer", value),
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"{source}: non-finite number {value}")
        ),
    )


def _json_pointer(value, pointer):
    if not isinstance(pointer, str) or len(pointer) > 2000:
        raise LogicError("JSON Pointer must be a bounded string")
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise LogicError("JSON Pointer must be empty or start with /")
    current = value
    for raw in pointer[1:].split("/"):
        if len(raw) > 1000 or re.search(r"~(?:[^01]|$)", raw):
            raise LogicError("JSON Pointer contains an invalid or oversized token")
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                raise LogicError(f"JSON Pointer key not found: {token}")
            current = current[token]
        elif isinstance(current, list):
            if len(token) > 20 or not re.fullmatch(r"0|[1-9]\d*", token):
                raise LogicError(f"JSON Pointer list index is invalid: {token}")
            try:
                index = int(token)
            except (ValueError, OverflowError) as exc:
                raise LogicError("JSON Pointer list index is invalid") from exc
            if index >= len(current):
                raise LogicError(f"JSON Pointer list index out of range: {token}")
            current = current[index]
        else:
            raise LogicError("JSON Pointer traverses a scalar value")
    return current


def _portable_path(cfg, path_value, label):
    if not isinstance(path_value, str) or not path_value:
        raise LogicError(f"{label} must be a non-empty project-relative path")
    candidate = Path(path_value)
    if candidate.is_absolute():
        raise LogicError(f"{label} must be project-relative, not absolute")
    unresolved = cfg.root / candidate
    current = cfg.root
    for part in candidate.parts:
        current = current / part
        if current.is_symlink():
            raise LogicError(f"{label} must not traverse a symbolic link")
    resolved = unresolved.resolve(strict=False)
    try:
        relative = resolved.relative_to(cfg.root.resolve())
    except ValueError as exc:
        raise LogicError(f"{label} escapes the configured project root") from exc
    return relative.as_posix(), resolved


def _node_hash(cfg, node):
    portable = _copy(node)
    if portable.get("path") is not None:
        portable["path"], _resolved = _portable_path(cfg, portable["path"], "node path")
    if isinstance(portable.get("logic_bindings"), list):
        portable["logic_bindings"] = sorted(
            portable["logic_bindings"], key=_canonical_bytes,
        )
    evidence_plan = portable.get("logic_evidence_plan")
    if (isinstance(evidence_plan, dict)
            and isinstance(evidence_plan.get("required_bindings"), list)):
        evidence_plan["required_bindings"] = sorted(
            evidence_plan["required_bindings"], key=_canonical_bytes,
        )
    digest = _sha256(portable)
    return digest, f"node:sha256:{digest}"


def _read_artifact(cfg, node):
    path_value = node.get("path")
    if not isinstance(path_value, str) or not path_value:
        return {"state": "not_declared"}, None
    try:
        portable_path, path = _portable_path(cfg, path_value, "result artifact path")
    except LogicError as exc:
        return {"state": "unreadable", "path": path_value, "reason": str(exc)}, None
    if not path.exists():
        return {"state": "missing", "path": portable_path}, None
    try:
        data = _safe_regular_file_bytes(path, "result artifact", MAX_ARTIFACT_BYTES)
    except LogicError as exc:
        return {"state": "unreadable", "path": portable_path, "reason": str(exc)}, None
    digest = hashlib.sha256(data).hexdigest()
    return {
        "state": "stable", "path": portable_path, "sha256": digest,
        "file_version_id": f"file:sha256:{digest}", "size": len(data),
    }, data


def _snapshot_provenance_file(cfg, node, remaining_bytes):
    """Stream-hash a scoped provenance file without loading it into memory."""
    path_value = node.get("path")
    if not isinstance(path_value, str) or not path_value:
        return {"state": "not_declared"}, 0
    try:
        portable_path, path = _portable_path(cfg, path_value, "provenance file path")
    except LogicError as exc:
        return {"state": "unreadable", "path": path_value, "reason": str(exc)}, 0
    if not path.exists():
        return {"state": "missing", "path": portable_path}, 0
    try:
        digest, size = _safe_regular_file_sha256(
            path, "provenance file", remaining_bytes,
        )
    except LogicError as exc:
        return {"state": "unreadable", "path": portable_path, "reason": str(exc)}, 0
    return {
        "state": "stable", "path": portable_path, "sha256": digest,
        "file_version_id": f"file:sha256:{digest}", "size": size,
    }, size


def _observed_term(value, expected_term, types):
    base = _base_type(expected_term["type"], types)
    if base in {"ct:decimal", "ct:integer"}:
        if not isinstance(value, _JsonNumber):
            raise LogicError("artifact JSON value is not a JSON number")
        if base == "ct:integer" and value.kind != "integer":
            raise LogicError("artifact JSON value is not an integer")
        raw = (
            _normal_artifact_decimal(value.lexical)
            if base == "ct:decimal" else value.lexical
        )
    else:
        if isinstance(value, _JsonNumber):
            raise LogicError("artifact JSON numeric value does not match the declared type")
        raw = value
    return {
        "type": expected_term["type"],
        "value": _normal_value(expected_term["type"], raw, types),
        "unit": expected_term["unit"],
    }


def _observed_text_term(value, expected_term, types):
    base = _base_type(expected_term["type"], types)
    if base == "ct:boolean":
        if value not in {"true", "false"}:
            raise LogicError("anchored text boolean must be exactly true or false")
        raw = value == "true"
    else:
        raw = value
    return {
        "type": expected_term["type"],
        "value": _normal_value(expected_term["type"], raw, types),
        "unit": expected_term["unit"],
    }


def _logic_bindings(node, vocabulary):
    _types, predicates = _vocabulary_maps(vocabulary)
    raw_bindings = node.get("logic_bindings", [])
    if not isinstance(raw_bindings, list):
        raise LogicError("result node logic_bindings must be a list")
    if len(raw_bindings) > MAX_LOGIC_BINDINGS:
        raise LogicError("result node logic binding count exceeds the deterministic limit")
    bindings = {}
    seen_ids = set()
    for item in raw_bindings:
        if not isinstance(item, dict) or set(item) != {
                "id", "vocabulary_id", "predicate", "polarity", "arguments"}:
            raise LogicError("each result logic binding must declare one complete fact profile")
        binding_id = _identifier(item["id"], "logic binding id")
        if binding_id in seen_ids:
            raise LogicError(f"duplicate result logic binding id: {binding_id}")
        seen_ids.add(binding_id)
        binding_vocabulary_id = _identifier(
            item["vocabulary_id"], "logic binding vocabulary_id",
        )
        if binding_vocabulary_id != vocabulary["id"]:
            continue
        if not isinstance(item["predicate"], str):
            raise LogicError(f"logic binding {binding_id} predicate must be a string")
        predicate = predicates.get(item["predicate"])
        if predicate is None or predicate["kind"] != "input":
            raise LogicError(f"logic binding {binding_id} must reference an input predicate")
        if not isinstance(item["polarity"], str) or item["polarity"] not in POLARITIES:
            raise LogicError(f"logic binding {binding_id} has an invalid polarity")
        expected_arguments = {argument["name"] for argument in predicate["arguments"]}
        if not isinstance(item["arguments"], dict) or set(item["arguments"]) != expected_arguments:
            raise LogicError(
                f"logic binding {binding_id} must map every predicate argument exactly once"
            )
        arguments = {}
        for argument in sorted(expected_arguments):
            extractor = item["arguments"][argument]
            if (not isinstance(extractor, dict)
                    or not isinstance(extractor.get("kind"), str)
                    or extractor.get("kind") not in {"json_pointer", "text_lines"}):
                raise LogicError(
                    f"logic binding {binding_id} argument {argument} has no supported extractor"
                )
            if extractor["kind"] == "json_pointer":
                if (set(extractor) != {"kind", "pointer"}
                        or not isinstance(extractor["pointer"], str)
                        or len(extractor["pointer"]) > 2000):
                    raise LogicError("json_pointer fact extractor has an invalid shape")
                # Parse the syntax now, before opening an artifact. Traversal errors are
                # checked later against the actual value.
                for token in extractor["pointer"].split("/")[1:]:
                    if len(token) > 1000 or re.search(r"~(?:[^01]|$)", token):
                        raise LogicError("json_pointer fact extractor has invalid escaping")
            else:
                if set(extractor) != {
                        "kind", "start_line", "end_line", "prefix", "suffix"}:
                    raise LogicError("text_lines fact extractor has an invalid shape")
                if (not isinstance(extractor["start_line"], int)
                        or isinstance(extractor["start_line"], bool)
                        or not isinstance(extractor["end_line"], int)
                        or isinstance(extractor["end_line"], bool)
                        or extractor["start_line"] < 1
                        or extractor["end_line"] < extractor["start_line"]
                        or not isinstance(extractor["prefix"], str)
                        or not isinstance(extractor["suffix"], str)
                        or len(extractor["prefix"]) > 2000
                        or len(extractor["suffix"]) > 2000):
                    raise LogicError("text_lines fact extractor bounds are invalid")
            arguments[argument] = _copy(extractor)
        bindings[binding_id] = {
            "id": binding_id, "vocabulary_id": vocabulary["id"],
            "predicate": predicate["id"], "polarity": item["polarity"],
            "arguments": arguments,
        }
    return bindings


def _normal_binding_selections(
        selections, *, collection_label="bindings", entry_label="binding selection"):
    if (not isinstance(selections, list) or not selections
            or len(selections) > MAX_INPUT_FACTS):
        raise LogicError(f"{collection_label} must be a non-empty bounded list")
    normalized = []
    for item in selections:
        if not isinstance(item, dict) or set(item) != {"result_id", "binding_id"}:
            raise LogicError(
                f"each {entry_label} requires exactly result_id and binding_id"
            )
        normalized.append({
            "result_id": _identifier(item["result_id"], f"{entry_label} result_id"),
            "binding_id": _identifier(item["binding_id"], f"{entry_label} binding_id"),
        })
    keys = [(item["result_id"], item["binding_id"]) for item in normalized]
    if len(keys) != len(set(keys)):
        raise LogicError(f"{collection_label} must not contain duplicates")
    return sorted(normalized, key=lambda item: (item["result_id"], item["binding_id"]))


def _normal_evidence_plan(value):
    if not isinstance(value, dict) or set(value) != {
            "schema_version", "required_bindings"}:
        raise LogicError(
            "logic_evidence_plan requires exactly schema_version and required_bindings"
        )
    if value["schema_version"] != EVIDENCE_PLAN_SCHEMA:
        raise LogicError(
            f"unsupported symbolic evidence-plan schema: {value['schema_version']!r}"
        )
    return {
        "schema_version": EVIDENCE_PLAN_SCHEMA,
        "required_bindings": _normal_binding_selections(
            value["required_bindings"],
            collection_label="evidence-plan required_bindings",
            entry_label="evidence-plan binding",
        ),
    }


def _resolve_claim_evidence_plan(claim, nodes, vocabulary):
    raw_plan = claim.get("logic_evidence_plan")
    if raw_plan is None:
        return None
    if claim.get("type") not in {"claim", "hypothesis", "prediction", "conclusion"}:
        raise LogicError(
            "logic_evidence_plan is only valid on claim, hypothesis, prediction, or conclusion"
        )
    plan = _normal_evidence_plan(raw_plan)
    for selection in plan["required_bindings"]:
        result_id = selection["result_id"]
        result = nodes.get(result_id)
        if result is None:
            raise LogicError(
                f"evidence plan references unknown result node {result_id!r}"
            )
        if result.get("type") not in RESULT_TYPES:
            raise LogicError(
                f"evidence plan result {result_id!r} is not an eligible result-node type"
            )
        bindings = _logic_bindings(result, vocabulary)
        if selection["binding_id"] not in bindings:
            raise LogicError(
                f"evidence plan result {result_id!r} does not declare compatible binding "
                f"{selection['binding_id']!r}"
            )
    return plan


def _extract_binding_arguments(binding, predicate, types, artifact, artifact_data,
                               json_value, lines, json_error):
    """Extract one complete typed atom profile from a stable result artifact."""
    if artifact.get("state") != "stable" or artifact_data is None:
        raise LogicError("result artifact is not a readable stable regular file")
    signatures = {
        item["name"]: {"type": item["type"], "unit": item["unit"]}
        for item in predicate["arguments"]
    }
    observed_arguments = {}
    check_details = {}
    for argument, extractor in binding["arguments"].items():
        expected = signatures[argument]
        if extractor["kind"] == "json_pointer":
            if json_error is not None:
                raise LogicError(json_error)
            if json_value is None:
                raise LogicError("artifact JSON extraction context is unavailable")
            observed = _json_pointer(json_value, extractor["pointer"])
            normalized = _observed_term(observed, expected, types)
            check_details[argument] = {
                "argument": argument, "kind": "json_pointer",
                "pointer": extractor["pointer"],
                "observed_value_sha256": _sha256(normalized),
            }
        else:
            if lines is None:
                raise LogicError("artifact text extraction context is unavailable")
            start, end = extractor["start_line"], extractor["end_line"]
            if end > len(lines):
                raise LogicError("text line extractor exceeds artifact line count")
            selected = b"".join(lines[start - 1:end])
            if len(selected) > 1024 * 1024:
                raise LogicError("text line extractor exceeds the 1 MiB grounding limit")
            digest = hashlib.sha256(selected).hexdigest()
            try:
                text = selected.decode("utf-8")
            except UnicodeError as exc:
                raise LogicError("anchored text is not valid UTF-8") from exc
            prefix, suffix = extractor["prefix"], extractor["suffix"]
            if (not text.startswith(prefix) or not text.endswith(suffix)
                    or len(text) < len(prefix) + len(suffix)):
                raise LogicError("extracted text does not match the declared prefix and suffix")
            stop = len(text) - len(suffix) if suffix else len(text)
            normalized = _observed_text_term(text[len(prefix):stop], expected, types)
            check_details[argument] = {
                "argument": argument, "kind": "text_lines",
                "start_line": start, "end_line": end,
                "prefix": prefix, "suffix": suffix,
                "observed_text_sha256": digest,
                "observed_value_sha256": _sha256(normalized),
            }
        observed_arguments[argument] = normalized
    return observed_arguments, check_details


def _check_anchor(anchor, agent_input, bindings, predicates, types, artifact,
                  artifact_data, json_value, lines, context_error, json_error):
    fact = agent_input["facts"][anchor["fact_index"]]
    base = {
        "fact_index": anchor["fact_index"], "result_id": anchor["result_id"],
        "binding_id": anchor["binding_id"],
    }
    try:
        if context_error is not None:
            raise LogicError(context_error)
        binding = bindings.get(anchor["binding_id"])
        if binding is None:
            raise LogicError("result node does not declare the selected project logic binding")
        if (binding["predicate"] != fact["atom"]["predicate"]
                or binding["polarity"] != fact["atom"]["polarity"]):
            raise LogicError(
                "project fact binding predicate or polarity does not match the proposed fact"
            )
        predicate = predicates[binding["predicate"]]
        observed_arguments, check_details = _extract_binding_arguments(
            binding, predicate, types, artifact, artifact_data,
            json_value, lines, json_error,
        )
        argument_checks = []
        for argument in binding["arguments"]:
            expected_term = fact["atom"]["arguments"][argument]
            argument_checks.append({
                **check_details[argument],
                "valid": observed_arguments[argument] == expected_term,
            })
        valid = all(item["valid"] for item in argument_checks)
        return {
            **base, "predicate": binding["predicate"], "polarity": binding["polarity"],
            "valid": valid,
            "detail": (
                "complete project-bound fact matched exact typed artifact values"
                if valid else "one or more project-bound fact arguments differed"
            ),
            "argument_checks": argument_checks,
        }
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RecursionError,
            LogicError) as exc:
        return {**base, "valid": False, "detail": str(exc)}


def _normal_claim_logic(value, vocabulary, rule_pack):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
            "vocabulary_id", "rule_pack_id", "target"}:
        raise LogicError(
            "claim node logic requires exactly vocabulary_id, rule_pack_id, and target"
        )
    if value["vocabulary_id"] != vocabulary["id"]:
        raise LogicError("claim node logic vocabulary_id does not match the derivation")
    if value["rule_pack_id"] != rule_pack["id"]:
        raise LogicError("claim node logic rule_pack_id does not match the derivation")
    return {
        "vocabulary_id": value["vocabulary_id"], "rule_pack_id": value["rule_pack_id"],
        "target": _normal_ground_atom(value["target"], vocabulary, expected_kind="derived"),
    }


def _snapshot_subject(cfg, claim_id, result_ids, agent_input, vocabulary, rule_pack):
    raw = engine.load_raw(cfg)
    structural = list(engine.structural_issues(cfg, raw=raw))
    if structural:
        code, node_id, detail = structural[0]
        raise LogicError(f"graph structural integrity failed: {code} {node_id}: {detail}")
    nodes, _edges, _concepts = engine.load_graph(cfg, raw=raw)
    problems, pending = engine.compute_check(cfg, raw=raw)
    _down, up = engine.build_adj(_edges)
    provenance_scope = {claim_id, *result_ids}
    queue = [claim_id, *result_ids]
    while queue:
        node_id = queue.pop()
        for upstream_id, _relation in up.get(node_id, []):
            if upstream_id not in provenance_scope:
                provenance_scope.add(upstream_id)
                queue.append(upstream_id)
    scoped_problems = sorted(
        [
            {"code": str(code), "node_id": str(node_id), "detail": str(detail)}
            for code, node_id, detail in problems
            if node_id in provenance_scope
        ],
        key=_canonical_bytes,
    )
    scoped_pending = sorted(
        [
            {
                "node_id": str(node_id), "type": str(node_type),
                "movement": str(movement), "path": str(path),
            }
            for node_id, node_type, movement, path in pending
            if node_id in provenance_scope
        ],
        key=_canonical_bytes,
    )
    for node_id in sorted(provenance_scope):
        status = nodes[node_id].get("status")
        if isinstance(status, str) and status in engine.RETIRED:
            scoped_problems.append({
                "code": "PROVENANCE_RETIRED_ANCESTOR", "node_id": node_id,
                "detail": f"scoped provenance node has retired status {status}",
            })
        elif (status != "stale"
                and (status is not None and not isinstance(status, str)
                     or status not in {None, "current", "confirmed", "null"})):
            scoped_problems.append({
                "code": "PROVENANCE_STATUS_INVALID", "node_id": node_id,
                "detail": f"scoped provenance node has non-standard status {status!r}",
            })
    claim = nodes.get(claim_id)
    if claim is None:
        raise LogicError(f"unknown claim node: {claim_id}")
    claim_digest, claim_version = _node_hash(cfg, claim)
    try:
        claim_logic = _normal_claim_logic(claim.get("logic"), vocabulary, rule_pack)
    except LogicError as exc:
        claim_logic = {"invalid": str(exc)}
    claim_type = claim.get("type")
    claim_status = claim.get("status")
    claim_eligible = (
        isinstance(claim_type, str)
        and claim_type in {"claim", "hypothesis", "prediction", "conclusion"}
        and (claim_status is None or isinstance(claim_status, str))
        and claim_status in {None, "current", "confirmed"}
    )
    claim_snapshot = {
        "node_id": claim_id, "node_sha256": claim_digest,
        "node_version_id": claim_version, "type": claim.get("type"),
        "status": claim.get("status"), "eligible": claim_eligible,
        "logic": claim_logic,
    }
    evidence_plan = _resolve_claim_evidence_plan(claim, nodes, vocabulary)
    if evidence_plan is not None:
        claim_snapshot["evidence_plan"] = _copy(evidence_plan)
        actual_bindings = sorted(
            [
                {
                    "result_id": anchor["result_id"],
                    "binding_id": anchor["binding_id"],
                }
                for fact in agent_input["facts"]
                for anchor in fact["evidence"]
            ],
            key=lambda item: (item["result_id"], item["binding_id"]),
        )
        has_assumptions = any(
            fact["assumption"] is not None for fact in agent_input["facts"]
        )
        if (has_assumptions
                or actual_bindings != evidence_plan["required_bindings"]):
            scoped_problems.append({
                "code": "LOGIC_EVIDENCE_PLAN_MISMATCH",
                "node_id": claim_id,
                "detail": (
                    "grounded premises must exactly equal every required evidence-plan "
                    "binding and must not add assumptions"
                ),
            })
    types, predicates = _vocabulary_maps(vocabulary)
    anchors = sorted(
        [
            anchor
            for fact in agent_input["facts"]
            for anchor in fact["evidence"]
        ],
        key=lambda item: (item["fact_index"], item["result_id"], item["binding_id"]),
    )
    anchors_by_result = {result_id: [] for result_id in result_ids}
    for anchor in anchors:
        anchors_by_result[anchor["result_id"]].append(anchor)
    results = []
    anchor_checks = []
    artifact_bytes_read = 0
    for result_id in result_ids:
        node = nodes.get(result_id)
        if node is None:
            raise LogicError(f"unknown result node: {result_id}")
        digest, version = _node_hash(cfg, node)
        result_type = node.get("type")
        result_status = node.get("status")
        result_eligible = (
            isinstance(result_type, str) and result_type in RESULT_TYPES
            and (result_status is None or isinstance(result_status, str))
            and result_status in {None, "current", "confirmed", "null"}
        )
        artifact, data = _read_artifact(cfg, node)
        artifact_bytes_read += len(data) if data is not None else 0
        if artifact_bytes_read > MAX_ARTIFACT_TOTAL_BYTES:
            raise LogicError(
                "result artifacts exceed the aggregate 256 MiB grounding limit"
            )
        results.append({
            "node_id": result_id, "node_sha256": digest, "node_version_id": version,
            "type": node.get("type"), "status": node.get("status"),
            "eligible": result_eligible,
            "artifact": artifact,
        })
        bindings = {}
        binding_error = None
        json_value = None
        json_error = None
        lines = None
        try:
            bindings = _logic_bindings(node, vocabulary)
        except LogicError as exc:
            binding_error = str(exc)
        selected_bindings = [
            bindings.get(anchor["binding_id"])
            for anchor in anchors_by_result[result_id]
        ]
        selected_bindings = [item for item in selected_bindings if item is not None]
        if data is not None and any(
                extractor["kind"] == "json_pointer"
                for binding in selected_bindings
                for extractor in binding["arguments"].values()):
            try:
                json_value = _json_with_decimal_strings(data, artifact.get("path", result_id))
            except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError,
                    LogicError) as exc:
                json_error = str(exc)
        if data is not None and any(
                extractor["kind"] == "text_lines"
                for binding in selected_bindings
                for extractor in binding["arguments"].values()):
            lines = data.splitlines(keepends=True)
        anchor_checks.extend(
            _check_anchor(
                anchor, agent_input, bindings, predicates, types, artifact, data,
                json_value, lines, binding_error, json_error,
            )
            for anchor in anchors_by_result[result_id]
        )
    anchor_checks.sort(
        key=lambda item: (item["fact_index"], item["result_id"], item["binding_id"]),
    )
    result_artifacts = {
        item["node_id"]: item["artifact"] for item in results
    }
    provenance_budget = getattr(
        cfg, "logic_max_provenance_bytes", DEFAULT_MAX_PROVENANCE_BYTES,
    )
    provenance_bytes_hashed = sum(
        item["size"] for item in result_artifacts.values()
        if item.get("state") == "stable"
    )
    if provenance_bytes_hashed > provenance_budget:
        raise LogicError("result files exceed the configured provenance hash budget")
    node_versions = []
    for node_id in sorted(provenance_scope):
        node = nodes[node_id]
        node_digest, node_version = _node_hash(cfg, node)
        artifact = result_artifacts.get(node_id)
        if artifact is None:
            artifact, hashed_bytes = _snapshot_provenance_file(
                cfg, node, provenance_budget - provenance_bytes_hashed,
            )
            provenance_bytes_hashed += hashed_bytes
        if node.get("path") and artifact.get("state") != "stable":
            scoped_problems.append({
                "code": "PROVENANCE_FILE_UNREADABLE", "node_id": node_id,
                "detail": str(artifact.get("reason") or artifact.get("state")),
            })
        node_versions.append({
            "node_id": node_id, "node_sha256": node_digest,
            "node_version_id": node_version, "artifact": artifact,
        })
    edge_versions = sorted(
        [
            {"edge": _copy(edge), "edge_sha256": _sha256(edge)}
            for edge in _edges
            if (edge.get("rel") not in engine.ANNOT_RELS
                and edge.get("from") in provenance_scope
                and edge.get("to") in provenance_scope)
        ],
        key=_canonical_bytes,
    )
    scoped_problems = sorted(scoped_problems, key=_canonical_bytes)
    provenance_check = {
        "scope_node_ids": sorted(provenance_scope),
        "node_versions": node_versions,
        "edge_versions": edge_versions,
        "problems": scoped_problems,
        "pending": scoped_pending,
        "eligible": not scoped_problems and not scoped_pending,
    }
    return {
        "claim": claim_snapshot,
        "results": sorted(results, key=lambda item: item["node_id"]),
        "provenance_check": provenance_check,
        "vocabulary": {
            "id": vocabulary["id"], "version": vocabulary["version"],
            "sha256": _sha256(vocabulary), "document": _copy(vocabulary),
        },
        "rule_pack": {
            "id": rule_pack["id"], "version": rule_pack["version"],
            "vocabulary_id": rule_pack["vocabulary_id"],
            "sha256": _sha256(rule_pack), "document": _copy(rule_pack),
        },
        "anchor_checks": anchor_checks,
    }


def _finding(code, severity, detail):
    return {"code": code, "severity": severity, "detail": detail}


def _proof_core(target, proof_state, outcome_atoms, used_input_fact_ids, steps,
                used_result_ids, snapshot, agent_input, *, closure_fact_ids=None):
    used_inputs = set(used_input_fact_ids)
    used_results = set(used_result_ids)
    used_fact_indexes = {
        index for index, item in enumerate(agent_input["facts"])
        if _fact_id(item["atom"]) in used_inputs
    }
    if closure_fact_ids is None:
        proof_fact_ids = set(used_inputs)
        for step in steps:
            proof_fact_ids.update(step["premise_fact_ids"])
            proof_fact_ids.add(step["conclusion_fact_id"])
    else:
        proof_fact_ids = set(closure_fact_ids)
    return {
        "schema_version": "claimtrace.symbolic-proof/1",
        "target": target,
        "proof_state": proof_state,
        "outcome_atoms": _copy(outcome_atoms),
        "input_fact_ids": sorted(used_inputs),
        "closure_fact_ids": sorted(proof_fact_ids),
        "proof_steps": steps,
        "premise_grounding": [
            {
                "fact_id": _fact_id(item["atom"]),
                "evidence": _copy(item["evidence"]),
                "assumption": item["assumption"],
            }
            for item in agent_input["facts"]
            if _fact_id(item["atom"]) in used_inputs
        ],
        "vocabulary_sha256": snapshot["vocabulary"]["sha256"],
        "rule_pack_sha256": snapshot["rule_pack"]["sha256"],
        "claim_node_sha256": snapshot["claim"]["node_sha256"],
        "provenance_check": _copy(snapshot["provenance_check"]),
        "result_versions": [
            {
                "node_id": item["node_id"], "node_sha256": item["node_sha256"],
                "artifact": item["artifact"],
            }
            for item in snapshot["results"] if item["node_id"] in used_results
        ],
        "anchor_checks": [
            item for item in snapshot["anchor_checks"]
            if item["fact_index"] in used_fact_indexes
        ],
    }


def _best_proof_slice(goal_fact_ids, steps, input_records, valid_input_fact_ids,
                      subject_result_ids):
    """Select a grounded, fully scoped proof independent of rule ordering or names."""
    step_by_id = {_sha256(item): item for item in steps}
    input_results = {
        fact_id: frozenset(
            anchor["result_id"] for anchor in input_records[fact_id]["evidence"]
        )
        for fact_id in valid_input_fact_ids
    }
    assumed_inputs = {
        fact_id for fact_id in valid_input_fact_ids
        if input_records[fact_id]["assumption"] is not None
    }
    subject_results = frozenset(subject_result_ids)

    def properties(candidate):
        input_ids, step_ids = candidate
        assumptions = frozenset(input_ids & assumed_inputs)
        results = frozenset().union(
            *(input_results[fact_id] for fact_id in input_ids),
        ) if input_ids else frozenset()
        return assumptions, results, len(step_ids)

    def canonical(candidate):
        return _canonical_bytes({
            "inputs": sorted(candidate[0]), "steps": sorted(candidate[1]),
        })

    def dominates(left, right):
        left_assumptions, left_results, left_steps = properties(left)
        right_assumptions, right_results, right_steps = properties(right)
        if (left_assumptions <= right_assumptions
                and left_results >= right_results
                and left_steps <= right_steps):
            if (left_assumptions, left_results, left_steps) != (
                    right_assumptions, right_results, right_steps):
                return True
            return canonical(left) <= canonical(right)
        return False

    candidate_count = 0
    proof_work = 0
    frontiers = {}

    def spend_work():
        nonlocal proof_work
        proof_work += 1
        if proof_work > MAX_PROOF_WORK:
            raise LogicError(
                "symbolic proof selection work limit exceeded; no partial proof is valid"
            )

    def add_candidate(fact_id, candidate):
        nonlocal candidate_count
        frontier = frontiers.setdefault(fact_id, [])
        for item in frontier:
            spend_work()
            if candidate == item or dominates(item, candidate):
                return False
        retained = []
        for item in frontier:
            spend_work()
            if not dominates(candidate, item):
                retained.append(item)
        frontier[:] = retained
        frontier.append(candidate)
        frontier.sort(key=canonical)
        candidate_count += 1
        if candidate_count > MAX_PROOF_CANDIDATES:
            raise LogicError(
                "symbolic proof alternatives exceed the deterministic candidate limit"
            )
        return True

    for fact_id in sorted(valid_input_fact_ids):
        add_candidate(fact_id, (frozenset({fact_id}), frozenset()))

    ordered_steps = sorted(steps, key=_canonical_bytes)
    for _round in range(MAX_ROUNDS):
        changed = False
        for step in ordered_steps:
            premise_frontiers = [frontiers.get(fact_id, []) for fact_id in step["premise_fact_ids"]]
            if not premise_frontiers or any(not items for items in premise_frontiers):
                continue
            step_id = _sha256(step)
            for combination in itertools.product(*premise_frontiers):
                spend_work()
                candidate = (
                    frozenset().union(*(item[0] for item in combination)),
                    frozenset({step_id}).union(*(item[1] for item in combination)),
                )
                changed = add_candidate(step["conclusion_fact_id"], candidate) or changed
        if not changed:
            break
    else:
        raise LogicError("symbolic proof selection did not converge")

    goal_frontiers = [frontiers.get(fact_id, []) for fact_id in goal_fact_ids]
    if not goal_frontiers:
        return [], []
    if any(not items for items in goal_frontiers):
        raise LogicError("derived conclusion has no grounded proof candidate")
    def rank(candidate):
        assumptions, results, step_count = properties(candidate)
        return (
            len(assumptions), len(subject_results - results), step_count, canonical(candidate),
        )

    selected = None
    selected_rank = None
    for combination in itertools.product(*goal_frontiers):
        spend_work()
        candidate = (
            frozenset().union(*(item[0] for item in combination)),
            frozenset().union(*(item[1] for item in combination)),
        )
        candidate_rank = rank(candidate)
        if selected is None or candidate_rank < selected_rank:
            selected, selected_rank = candidate, candidate_rank
    if selected is None:
        raise LogicError("derived conclusion has no grounded proof candidate")
    return sorted(selected[0]), [step_by_id[key] for key in sorted(selected[1])]


def _derive(agent_input, snapshot):
    vocabulary = load_vocabulary(snapshot["vocabulary"]["document"])
    rule_pack = load_rule_pack(snapshot["rule_pack"]["document"], vocabulary)
    invalid_fact_indexes = {
        item["fact_index"] for item in snapshot["anchor_checks"] if not item["valid"]
    }
    input_atoms = [
        item["atom"] for index, item in enumerate(agent_input["facts"])
        if item["assumption"] is not None or index not in invalid_fact_indexes
    ]
    facts, steps = _closure(input_atoms, vocabulary, rule_pack)
    target = agent_input["target"]
    target_id = _fact_id(target)
    opposite = _copy(target)
    opposite["polarity"] = "negative" if target["polarity"] == "positive" else "positive"
    opposite_id = _fact_id(opposite)
    has_target, has_opposite = target_id in facts, opposite_id in facts
    if has_target and has_opposite:
        state = "conflict"
    elif has_target:
        state = "derivable"
    elif has_opposite:
        state = "refutable"
    else:
        state = "unknown"
    goal_ids = []
    if has_target:
        goal_ids.append(target_id)
    if has_opposite:
        goal_ids.append(opposite_id)
    outcome_atoms = []
    if has_target:
        outcome_atoms.append(_copy(target))
    if has_opposite:
        outcome_atoms.append(_copy(opposite))
    outcome_relation = {
        "derivable": "target_derived",
        "refutable": "target_refuted",
        "conflict": "target_and_opposite_derived",
        "unknown": "undetermined",
    }[state]
    input_records = {
        _fact_id(item["atom"]): item for item in agent_input["facts"]
    }
    subject_result_ids = [item["node_id"] for item in snapshot["results"]]
    used_input_fact_ids, proof_steps = _best_proof_slice(
        goal_ids,
        steps,
        input_records,
        {_fact_id(atom) for atom in input_atoms},
        subject_result_ids,
    )
    used_result_ids = sorted({
        anchor["result_id"]
        for fact_id in used_input_fact_ids
        for anchor in input_records[fact_id]["evidence"]
    })
    used_assumptions = sorted({
        input_records[fact_id]["assumption"]
        for fact_id in used_input_fact_ids
        if input_records[fact_id]["assumption"] is not None
    })
    valid_input_fact_ids = sorted(_fact_id(atom) for atom in input_atoms)
    unused_input_fact_ids = sorted(
        set(valid_input_fact_ids) - set(used_input_fact_ids)
    )
    findings = []
    if invalid_fact_indexes:
        findings.append(_finding(
            "EVIDENCE_ANCHOR_INVALID", "error",
            "one or more proposed premise facts failed exact artifact grounding",
        ))
    claim_logic = snapshot["claim"]["logic"]
    claim_bound = bool(
        isinstance(claim_logic, dict)
        and claim_logic.get("vocabulary_id") == vocabulary["id"]
        and claim_logic.get("rule_pack_id") == rule_pack["id"]
        and claim_logic.get("target") == target
    )
    if claim_logic is None:
        findings.append(_finding(
            "CLAIM_LOGIC_UNDECLARED", "warning",
            "the proof target is a candidate; the graph claim has no matching logic declaration",
        ))
    elif not claim_bound:
        findings.append(_finding(
            "CLAIM_LOGIC_MISMATCH", "error",
            "the proof target does not exactly match the graph claim logic declaration",
        ))
    ineligible_results = sorted(
        item["node_id"] for item in snapshot["results"] if not item["eligible"]
    )
    if not snapshot["claim"]["eligible"] or ineligible_results:
        detail = "formal claim node is not active and claim-like"
        if ineligible_results:
            detail += "; ineligible result nodes: " + ", ".join(ineligible_results)
        findings.append(_finding("LOGIC_SUBJECT_INELIGIBLE", "error", detail))
    provenance_check = snapshot["provenance_check"]
    if any(
            item.get("code") == "LOGIC_EVIDENCE_PLAN_MISMATCH"
            for item in provenance_check["problems"]):
        findings.append(_finding(
            "LOGIC_EVIDENCE_PLAN_MISMATCH", "error",
            "grounded premises do not exactly match the claim-owned evidence plan",
        ))
    if not provenance_check["eligible"]:
        labels = [
            f"{item['code']}:{item['node_id']}"
            for item in provenance_check["problems"]
        ] + [
            f"PENDING:{item['node_id']}" for item in provenance_check["pending"]
        ]
        findings.append(_finding(
            "PROVENANCE_SUBJECT_INVALID", "error",
            "the formal subject has unresolved mechanical provenance findings: "
            + ", ".join(labels),
        ))
    if state == "conflict":
        findings.append(_finding(
            "SYMBOLIC_CONFLICT", "error",
            "both the target and its explicit opposite are derivable under the selected rule pack",
        ))
    elif state == "unknown":
        findings.append(_finding(
            "SYMBOLIC_UNKNOWN", "info",
            "neither the target nor its explicit opposite is derivable from the grounded premises",
        ))
    if used_assumptions:
        findings.append(_finding(
            "SYMBOLIC_ASSUMPTION", "warning",
            "the formal conclusion depends on one or more explicit assumptions",
        ))
    if state != "unknown" and unused_input_fact_ids:
        findings.append(_finding(
            "UNUSED_INPUT_PREMISE", "warning",
            "one or more proposed input facts do not contribute to the selected proof slice",
        ))
    unused_result_ids = sorted(set(subject_result_ids) - set(used_result_ids))
    if state != "unknown" and unused_result_ids:
        findings.append(_finding(
            "UNUSED_RESULT_PREMISE", "error",
            "one or more listed results do not contribute to the backward proof slice: "
            + ", ".join(unused_result_ids),
        ))
    certificate_input_ids = (
        used_input_fact_ids if state != "unknown" else valid_input_fact_ids
    )
    certificate_result_ids = (
        used_result_ids if state != "unknown" else subject_result_ids
    )
    proof_core = _proof_core(
        target, state, outcome_atoms, certificate_input_ids, proof_steps,
        certificate_result_ids, snapshot, agent_input,
        closure_fact_ids=sorted(facts) if state == "unknown" else None,
    )
    proof_digest = _sha256(proof_core)
    errors = [item for item in findings if item["severity"] == "error"]
    return {
        "proof_state": state,
        "target": _copy(target),
        "rendered_target": _render_atom(target, vocabulary),
        "outcome_relation": outcome_relation,
        "outcome_atoms": outcome_atoms,
        "rendered_outcomes": [_render_atom(atom, vocabulary) for atom in outcome_atoms],
        "claim_bound": claim_bound,
        "active": (
            state in {"derivable", "refutable"}
            and claim_bound and not errors and not used_assumptions
        ),
        "stale": False,
        "proof_id": f"proof:sha256:{proof_digest}",
        "input_fact_ids": valid_input_fact_ids,
        "used_input_fact_ids": used_input_fact_ids,
        "unused_input_fact_ids": unused_input_fact_ids,
        "used_result_ids": used_result_ids,
        "supporting_fact_id": target_id if has_target else None,
        "refuting_fact_id": opposite_id if has_opposite else None,
        "proof_steps": proof_steps,
        "assumptions": used_assumptions,
        "drift": [],
        "findings": findings,
    }


def _derivation_id(core):
    return f"derivation:sha256:{_sha256(core)}"


def create_derivation(cfg, claim_id: str, result_ids: list[str], agent_input: dict, *,
                      vocabulary, rule_pack, actor: str, recorded_at: str | None = None) -> dict:
    """Create one immutable derivation from agent-proposed premises and trusted project rules."""
    _identifier(claim_id, "claim_id")
    if (not isinstance(result_ids, list) or not result_ids
            or not all(isinstance(item, str) and item for item in result_ids)
            or result_ids != sorted(set(result_ids))):
        raise LogicError("result_ids must be a non-empty sorted unique string list")
    if len(result_ids) > MAX_RESULT_IDS:
        raise LogicError("result_ids count exceeds the deterministic limit")
    for result_id in result_ids:
        _identifier(result_id, "result_id")
    _bounded_text(actor, "actor", 500, nonempty=True)
    vocabulary = load_vocabulary(vocabulary)
    rule_pack = load_rule_pack(rule_pack, vocabulary)
    normalized_input = _normal_agent_input(agent_input, result_ids, vocabulary)
    timestamp = recorded_at or _utc_now()
    _validate_time(timestamp)
    snapshot = _snapshot_subject(
        cfg, claim_id, result_ids, normalized_input, vocabulary, rule_pack,
    )
    derived = _derive(normalized_input, snapshot)
    subject = {
        "claim_id": claim_id, "result_ids": result_ids,
        "vocabulary_id": vocabulary["id"], "rule_pack_id": rule_pack["id"],
    }
    core = {
        "schema_version": DERIVATION_SCHEMA,
        "recorded_at": timestamp,
        "actor": actor,
        "subject": subject,
        "agent_input": normalized_input,
        "mechanical_snapshot": snapshot,
        "derived": derived,
    }
    return {"id": _derivation_id(core), **core}


def _claim_logic_context(cfg, claim_id):
    raw = engine.load_raw(cfg)
    structural = list(engine.structural_issues(cfg, raw=raw))
    if structural:
        code, node_id, detail = structural[0]
        raise LogicError(f"graph structural integrity failed: {code} {node_id}: {detail}")
    nodes, _edges, _concepts = engine.load_graph(cfg, raw=raw)
    claim = nodes.get(claim_id)
    if claim is None:
        raise LogicError(f"unknown claim node: {claim_id}")
    raw_claim_logic = claim.get("logic")
    if not isinstance(raw_claim_logic, dict) or set(raw_claim_logic) != {
            "vocabulary_id", "rule_pack_id", "target"}:
        raise LogicError(
            "automatic binding materialization requires a complete claim.logic declaration"
        )
    vocabulary_id = _identifier(
        raw_claim_logic["vocabulary_id"], "claim logic vocabulary_id",
    )
    rule_pack_id = _identifier(
        raw_claim_logic["rule_pack_id"], "claim logic rule_pack_id",
    )
    vocabularies, rule_packs = configured_logic_assets(cfg)
    vocabulary = vocabularies.get(vocabulary_id)
    rule_pack = rule_packs.get(rule_pack_id)
    if vocabulary is None or rule_pack is None:
        raise LogicError("claim logic must select configured vocabulary and rule-pack ids")
    if rule_pack["vocabulary_id"] != vocabulary["id"]:
        raise LogicError("claim logic selects an incompatible vocabulary and rule pack")
    claim_logic = _normal_claim_logic(raw_claim_logic, vocabulary, rule_pack)
    assert claim_logic is not None
    return nodes, claim, claim_logic, vocabulary, rule_pack


def resolve_claim_evidence_plan(cfg, claim_id: str) -> dict:
    """Resolve and validate one exact, claim-owned required-premise plan."""
    claim_id = _identifier(claim_id, "claim_id")
    nodes, claim, _claim_logic, vocabulary, rule_pack = _claim_logic_context(
        cfg, claim_id,
    )
    plan = _resolve_claim_evidence_plan(claim, nodes, vocabulary)
    if plan is None:
        raise LogicError(f"claim {claim_id!r} has no logic_evidence_plan")
    return {
        "schema_version": EVIDENCE_PLAN_SCHEMA,
        "claim_id": claim_id,
        "vocabulary_id": vocabulary["id"],
        "rule_pack_id": rule_pack["id"],
        "required_bindings": _copy(plan["required_bindings"]),
    }


def create_derivation_from_bindings(
        cfg, claim_id: str, selections: list[dict], *, actor: str,
        provenance: dict, note: str | None = None,
        recorded_at: str | None = None) -> dict:
    """Materialize trusted graph bindings, then create one symbolic derivation.

    The caller selects only project-declared result/binding identities. Claimtrace
    resolves the pinned target and policy assets from the claim, and extracts every
    typed fact value from the selected artifacts.
    """
    claim_id = _identifier(claim_id, "claim_id")
    _bounded_text(actor, "actor", 500, nonempty=True)
    normalized_selections = _normal_binding_selections(selections)
    result_ids = sorted({item["result_id"] for item in normalized_selections})
    if len(result_ids) > MAX_RESULT_IDS:
        raise LogicError("binding selections reference too many result nodes")

    nodes, claim, claim_logic, vocabulary, rule_pack = _claim_logic_context(
        cfg, claim_id,
    )
    evidence_plan = _resolve_claim_evidence_plan(claim, nodes, vocabulary)
    if (evidence_plan is not None
            and normalized_selections != evidence_plan["required_bindings"]):
        raise LogicError(
            "binding selections must exactly match the claim-owned evidence plan; "
            "use claimtrace.symbolic-plan-request/1 to materialize it automatically"
        )
    types, predicates = _vocabulary_maps(vocabulary)

    selections_by_result = {result_id: [] for result_id in result_ids}
    for item in normalized_selections:
        selections_by_result[item["result_id"]].append(item["binding_id"])
    facts = []
    artifact_bytes_read = 0
    for result_id in result_ids:
        node = nodes.get(result_id)
        if node is None:
            raise LogicError(f"unknown result node: {result_id}")
        bindings = _logic_bindings(node, vocabulary)
        selected = []
        for binding_id in selections_by_result[result_id]:
            binding = bindings.get(binding_id)
            if binding is None:
                raise LogicError(
                    f"result {result_id} does not declare binding {binding_id}"
                )
            selected.append(binding)
        artifact, data = _read_artifact(cfg, node)
        artifact_bytes_read += len(data) if data is not None else 0
        if artifact_bytes_read > MAX_ARTIFACT_TOTAL_BYTES:
            raise LogicError(
                "result artifacts exceed the aggregate 256 MiB grounding limit"
            )
        needs_json = any(
            extractor["kind"] == "json_pointer"
            for binding in selected for extractor in binding["arguments"].values()
        )
        needs_text = any(
            extractor["kind"] == "text_lines"
            for binding in selected for extractor in binding["arguments"].values()
        )
        json_value = None
        json_error = None
        if data is not None and needs_json:
            try:
                json_value = _json_with_decimal_strings(
                    data, artifact.get("path", result_id),
                )
            except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError,
                    LogicError) as exc:
                json_error = str(exc)
        lines = data.splitlines(keepends=True) if data is not None and needs_text else None
        for binding in selected:
            predicate = predicates[binding["predicate"]]
            arguments, _checks = _extract_binding_arguments(
                binding, predicate, types, artifact, data,
                json_value, lines, json_error,
            )
            atom = _normal_ground_atom({
                "predicate": binding["predicate"],
                "polarity": binding["polarity"],
                "arguments": arguments,
            }, vocabulary, expected_kind="input")
            facts.append({
                "atom": atom,
                "evidence": [{"result_id": result_id, "binding_id": binding["id"]}],
                "assumption": None,
            })

    agent_input = {
        "target": claim_logic["target"],
        "facts": facts,
        "note": note,
        "provenance": provenance,
    }
    return create_derivation(
        cfg, claim_id, result_ids, agent_input,
        vocabulary=vocabulary, rule_pack=rule_pack, actor=actor,
        recorded_at=recorded_at,
    )


def create_derivation_from_evidence_plan(
        cfg, claim_id: str, *, actor: str, provenance: dict,
        note: str | None = None, recorded_at: str | None = None) -> dict:
    """Materialize every binding in a claim-owned exact all-of evidence plan."""
    resolved = resolve_claim_evidence_plan(cfg, claim_id)
    return create_derivation_from_bindings(
        cfg, resolved["claim_id"], resolved["required_bindings"], actor=actor,
        provenance=provenance, note=note, recorded_at=recorded_at,
    )


def _validate_snapshot(snapshot, subject, agent_input):
    if not isinstance(snapshot, dict) or set(snapshot) != {
            "claim", "results", "provenance_check", "vocabulary", "rule_pack",
            "anchor_checks"}:
        raise LogicError("mechanical_snapshot has an invalid shape")
    if not isinstance(snapshot["vocabulary"], dict):
        raise LogicError("mechanical vocabulary snapshot must be an object")
    vocabulary_document = snapshot["vocabulary"].get("document")
    if not isinstance(vocabulary_document, dict):
        raise LogicError("mechanical vocabulary document must be an object")
    vocabulary = load_vocabulary(vocabulary_document)
    vocabulary_expected = {
        "id": vocabulary["id"], "version": vocabulary["version"],
        "sha256": _sha256(vocabulary), "document": vocabulary,
    }
    if snapshot["vocabulary"] != vocabulary_expected:
        raise LogicError("stored vocabulary snapshot is internally inconsistent")
    if not isinstance(snapshot["rule_pack"], dict):
        raise LogicError("mechanical rule-pack snapshot must be an object")
    rule_document = snapshot["rule_pack"].get("document")
    if not isinstance(rule_document, dict):
        raise LogicError("mechanical rule-pack document must be an object")
    rule_pack = load_rule_pack(rule_document, vocabulary)
    rule_expected = {
        "id": rule_pack["id"], "version": rule_pack["version"],
        "vocabulary_id": rule_pack["vocabulary_id"],
        "sha256": _sha256(rule_pack), "document": rule_pack,
    }
    if snapshot["rule_pack"] != rule_expected:
        raise LogicError("stored rule-pack snapshot is internally inconsistent")
    if (subject["vocabulary_id"] != vocabulary["id"]
            or subject["rule_pack_id"] != rule_pack["id"]):
        raise LogicError("subject logic asset ids do not match the stored snapshots")
    claim = snapshot["claim"]
    claim_base_keys = {
        "node_id", "node_sha256", "node_version_id", "type", "status",
        "eligible", "logic",
    }
    if (not isinstance(claim, dict)
            or frozenset(claim) not in {frozenset(claim_base_keys),
                                        frozenset(claim_base_keys | {"evidence_plan"})}):
        raise LogicError("mechanical claim snapshot has an invalid shape")
    if claim["node_id"] != subject["claim_id"]:
        raise LogicError("mechanical claim snapshot does not match the subject")
    if (not isinstance(claim["node_sha256"], str)
            or not SHA256_RE.fullmatch(claim["node_sha256"])
            or claim["node_version_id"] != f"node:sha256:{claim['node_sha256']}"
            or not isinstance(claim["eligible"], bool)):
        raise LogicError("mechanical claim node hash is invalid")
    expected_plan_mismatch = False
    if "evidence_plan" in claim:
        evidence_plan = _normal_evidence_plan(claim["evidence_plan"])
        if evidence_plan != claim["evidence_plan"]:
            raise LogicError("mechanical evidence-plan snapshot is not canonical")
        actual_bindings = sorted(
            [
                {
                    "result_id": anchor["result_id"],
                    "binding_id": anchor["binding_id"],
                }
                for fact in agent_input["facts"]
                for anchor in fact["evidence"]
            ],
            key=lambda item: (item["result_id"], item["binding_id"]),
        )
        expected_plan_mismatch = (
            actual_bindings != evidence_plan["required_bindings"]
            or any(fact["assumption"] is not None for fact in agent_input["facts"])
        )
    results = snapshot["results"]
    if (not isinstance(results, list)
            or not all(isinstance(item, dict) for item in results)
            or [item.get("node_id") for item in results] != subject["result_ids"]):
        raise LogicError("mechanical result snapshots do not match the subject")
    for item in results:
        if not isinstance(item, dict) or set(item) != {
                "node_id", "node_sha256", "node_version_id", "type", "status",
                "eligible", "artifact"}:
            raise LogicError("mechanical result snapshot has an invalid shape")
        if (not isinstance(item["node_sha256"], str)
                or not SHA256_RE.fullmatch(item["node_sha256"])
                or item["node_version_id"] != f"node:sha256:{item['node_sha256']}"
                or not isinstance(item["eligible"], bool)):
            raise LogicError("mechanical result node hash is invalid")
        artifact = item["artifact"]
        if (not isinstance(artifact, dict)
                or not isinstance(artifact.get("state"), str)
                or artifact.get("state") not in {
                    "stable", "missing", "unreadable", "not_declared"}):
            raise LogicError("mechanical artifact snapshot is invalid")
        if artifact["state"] == "stable":
            expected_keys = {"state", "path", "sha256", "file_version_id", "size"}
            if (set(artifact) != expected_keys
                    or not isinstance(artifact["sha256"], str)
                    or not SHA256_RE.fullmatch(artifact["sha256"])
                    or artifact["file_version_id"] != f"file:sha256:{artifact['sha256']}"
                    or not isinstance(artifact["size"], int)
                    or isinstance(artifact["size"], bool)
                    or artifact["size"] < 0):
                raise LogicError("stable artifact snapshot is invalid")
    provenance = snapshot["provenance_check"]
    if not isinstance(provenance, dict) or set(provenance) != {
            "scope_node_ids", "node_versions", "edge_versions", "problems", "pending",
            "eligible"}:
        raise LogicError("mechanical provenance check has an invalid shape")
    scope = provenance["scope_node_ids"]
    if (not isinstance(scope, list)
            or not all(isinstance(item, str) and ID_RE.fullmatch(item) for item in scope)
            or scope != sorted(set(scope))
            or not {subject["claim_id"], *subject["result_ids"]} <= set(scope)):
        raise LogicError("mechanical provenance scope is invalid")
    node_versions = provenance["node_versions"]
    if (not isinstance(node_versions, list)
            or not all(isinstance(item, dict) for item in node_versions)
            or [item.get("node_id") for item in node_versions] != scope):
        raise LogicError("mechanical provenance node versions do not match the scope")
    for item in node_versions:
        if (not isinstance(item, dict) or set(item) != {
                "node_id", "node_sha256", "node_version_id", "artifact"}
                or not isinstance(item["node_sha256"], str)
                or not SHA256_RE.fullmatch(item["node_sha256"])
                or item["node_version_id"] != f"node:sha256:{item['node_sha256']}"):
            raise LogicError("mechanical provenance node version is invalid")
        artifact = item["artifact"]
        if (not isinstance(artifact, dict)
                or not isinstance(artifact.get("state"), str)
                or artifact.get("state") not in {
                    "stable", "missing", "unreadable", "not_declared"}):
            raise LogicError("mechanical provenance artifact version is invalid")
        if artifact["state"] == "stable" and (
                set(artifact) != {"state", "path", "sha256", "file_version_id", "size"}
                or not isinstance(artifact["sha256"], str)
                or not SHA256_RE.fullmatch(artifact["sha256"])
                or artifact["file_version_id"] != f"file:sha256:{artifact['sha256']}"
                or not isinstance(artifact["size"], int)
                or isinstance(artifact["size"], bool)
                or artifact["size"] < 0):
            raise LogicError("stable mechanical provenance artifact is invalid")
    node_versions_by_id = {item["node_id"]: item for item in node_versions}
    if (node_versions_by_id[subject["claim_id"]]["node_sha256"]
            != claim["node_sha256"]):
        raise LogicError("claim and provenance node-version snapshots disagree")
    results_by_id = {item["node_id"]: item for item in results}
    for result_id, result in results_by_id.items():
        provenance_result = node_versions_by_id[result_id]
        if (provenance_result["node_sha256"] != result["node_sha256"]
                or provenance_result["artifact"] != result["artifact"]):
            raise LogicError("result and provenance node-version snapshots disagree")
    edge_versions = provenance["edge_versions"]
    if (not isinstance(edge_versions, list)
            or edge_versions != sorted(edge_versions, key=_canonical_bytes)):
        raise LogicError("mechanical provenance edge versions are not canonical")
    for item in edge_versions:
        if (not isinstance(item, dict) or set(item) != {"edge", "edge_sha256"}
                or not isinstance(item["edge"], dict)
                or not isinstance(item["edge"].get("from"), str)
                or item["edge"].get("from") not in scope
                or not isinstance(item["edge"].get("to"), str)
                or item["edge"].get("to") not in scope
                or not isinstance(item["edge"].get("rel"), str)
                or item["edge"].get("rel") in engine.ANNOT_RELS
                or not isinstance(item["edge_sha256"], str)
                or item["edge_sha256"] != _sha256(item["edge"])):
            raise LogicError("mechanical provenance edge version is invalid")
    if not isinstance(provenance["problems"], list):
        raise LogicError("mechanical provenance problems must be a list")
    for item in provenance["problems"]:
        if (not isinstance(item, dict) or set(item) != {"code", "node_id", "detail"}
                or item["node_id"] not in scope
                or not all(isinstance(item[key], str) for key in item)):
            raise LogicError("mechanical provenance problem is invalid")
    if not isinstance(provenance["pending"], list):
        raise LogicError("mechanical provenance pending list is invalid")
    for item in provenance["pending"]:
        if (not isinstance(item, dict)
                or set(item) != {"node_id", "type", "movement", "path"}
                or item["node_id"] not in scope
                or not all(isinstance(item[key], str) for key in item)):
            raise LogicError("mechanical provenance pending item is invalid")
    if (provenance["problems"] != sorted(provenance["problems"], key=_canonical_bytes)
            or provenance["pending"] != sorted(
                provenance["pending"], key=_canonical_bytes,
            )
            or provenance["eligible"] is not (
                not provenance["problems"] and not provenance["pending"]
            )):
        raise LogicError("mechanical provenance check is not canonical")
    has_plan_mismatch = any(
        item.get("code") == "LOGIC_EVIDENCE_PLAN_MISMATCH"
        and item.get("node_id") == subject["claim_id"]
        for item in provenance["problems"]
    )
    if has_plan_mismatch is not expected_plan_mismatch:
        raise LogicError(
            "mechanical evidence-plan mismatch state does not match grounded premises"
        )
    checks = snapshot["anchor_checks"]
    expected_checks = [
        {
            "fact_index": anchor["fact_index"], "result_id": anchor["result_id"],
            "binding_id": anchor["binding_id"],
        }
        for fact in agent_input["facts"] for anchor in fact["evidence"]
    ]
    expected_checks.sort(
        key=lambda item: (item["fact_index"], item["result_id"], item["binding_id"]),
    )
    if not isinstance(checks, list) or len(checks) != len(expected_checks):
        raise LogicError("mechanical anchor checks do not match agent evidence")
    for item, expected in zip(checks, expected_checks):
        required = {"fact_index", "result_id", "binding_id", "valid", "detail"}
        if (not isinstance(item, dict) or not required <= set(item)
                or not isinstance(item["valid"], bool)
                or not isinstance(item["detail"], str)
                or {key: item[key] for key in expected} != expected):
            raise LogicError("mechanical anchor check is invalid")
        fact = agent_input["facts"][expected["fact_index"]]
        full_keys = required | {"predicate", "polarity", "argument_checks"}
        if set(item) == required:
            if item["valid"]:
                raise LogicError("a valid mechanical anchor check lacks argument checks")
            continue
        if (set(item) != full_keys
                or item["predicate"] != fact["atom"]["predicate"]
                or item["polarity"] != fact["atom"]["polarity"]
                or not isinstance(item["argument_checks"], list)
                or not all(
                    isinstance(value, dict) for value in item["argument_checks"]
                )):
            raise LogicError("mechanical anchor check does not match its input fact")
        expected_arguments = sorted(fact["atom"]["arguments"])
        if [value.get("argument") for value in item["argument_checks"]] != expected_arguments:
            raise LogicError("mechanical anchor argument checks are incomplete")
        argument_validity = []
        for value in item["argument_checks"]:
            if not isinstance(value, dict) or not isinstance(value.get("valid"), bool):
                raise LogicError("mechanical anchor argument check is invalid")
            kind = value.get("kind")
            expected_keys = (
                {"argument", "kind", "pointer", "valid", "observed_value_sha256"}
                if kind == "json_pointer" else
                {"argument", "kind", "start_line", "end_line", "prefix", "suffix",
                 "valid", "observed_text_sha256", "observed_value_sha256"}
                if kind == "text_lines" else set()
            )
            if (set(value) != expected_keys
                    or not isinstance(value["observed_value_sha256"], str)
                    or not SHA256_RE.fullmatch(value["observed_value_sha256"])
                    or (kind == "text_lines"
                        and (not isinstance(value["observed_text_sha256"], str)
                             or not SHA256_RE.fullmatch(value["observed_text_sha256"])))
                    or (kind == "json_pointer"
                        and not isinstance(value["pointer"], str))
                    or (kind == "text_lines" and (
                        not isinstance(value["start_line"], int)
                        or isinstance(value["start_line"], bool)
                        or not isinstance(value["end_line"], int)
                        or isinstance(value["end_line"], bool)
                        or not isinstance(value["prefix"], str)
                        or not isinstance(value["suffix"], str)))):
                raise LogicError("mechanical anchor argument-check shape is invalid")
            if (value["valid"] and value["observed_value_sha256"]
                    != _sha256(fact["atom"]["arguments"][value["argument"]])):
                raise LogicError("mechanical anchor value hash does not match the input fact")
            argument_validity.append(value["valid"])
        if item["valid"] is not all(argument_validity):
            raise LogicError("mechanical anchor validity does not match its arguments")


def validate_derivation_document(document: object) -> None:
    """Validate content address and recompute the stored symbolic certificate."""
    if not isinstance(document, dict) or set(document) != {
            "id", "schema_version", "recorded_at", "actor", "subject", "agent_input",
            "mechanical_snapshot", "derived"}:
        raise LogicError("derivation document has unknown or missing top-level fields")
    if document["schema_version"] != DERIVATION_SCHEMA:
        raise LogicError(f"unsupported derivation schema: {document['schema_version']!r}")
    _validate_time(document["recorded_at"])
    _bounded_text(document["actor"], "derivation actor", 500, nonempty=True)
    subject = document["subject"]
    if not isinstance(subject, dict) or set(subject) != {
            "claim_id", "result_ids", "vocabulary_id", "rule_pack_id"}:
        raise LogicError("derivation subject has an invalid shape")
    _identifier(subject["claim_id"], "subject.claim_id")
    if (not isinstance(subject["result_ids"], list) or not subject["result_ids"]
            or not all(isinstance(item, str) and item for item in subject["result_ids"])
            or subject["result_ids"] != sorted(set(subject["result_ids"]))):
        raise LogicError("derivation subject result_ids are invalid")
    if len(subject["result_ids"]) > MAX_RESULT_IDS:
        raise LogicError("derivation subject result_ids exceed the deterministic limit")
    for result_id in subject["result_ids"]:
        _identifier(result_id, "subject result_id")
    _identifier(subject["vocabulary_id"], "subject.vocabulary_id")
    _identifier(subject["rule_pack_id"], "subject.rule_pack_id")
    snapshot = document["mechanical_snapshot"]
    if not isinstance(snapshot, dict):
        raise LogicError("derivation mechanical_snapshot must be an object")
    vocabulary_record = snapshot.get("vocabulary")
    vocabulary_document = (
        vocabulary_record.get("document") if isinstance(vocabulary_record, dict) else None
    )
    if not isinstance(vocabulary_document, dict):
        raise LogicError("derivation vocabulary snapshot must contain a document object")
    vocabulary = load_vocabulary(vocabulary_document)
    normalized_input = _normal_agent_input(
        document["agent_input"], subject["result_ids"], vocabulary,
    )
    if normalized_input != document["agent_input"]:
        raise LogicError("derivation agent_input is not canonical")
    _validate_snapshot(snapshot, subject, normalized_input)
    expected_derived = _derive(normalized_input, snapshot)
    if document["derived"] != expected_derived:
        raise LogicError("stored derivation does not match the deterministic proof certificate")
    core = {key: document[key] for key in document if key != "id"}
    if document["id"] != _derivation_id(core):
        raise LogicError("derivation id does not match canonical document content")


def derivations_path(cfg) -> Path:
    path = getattr(cfg, "derivations_declared_path", None)
    if path is None:
        path = getattr(cfg, "derivations_path", None)
    if path is not None:
        return Path(os.path.abspath(path))
    logic = cfg.data.get("logic", {}) if isinstance(cfg.data, dict) else {}
    configured = logic.get("derivations", "claimtrace/derivations")
    candidate = Path(configured)
    return Path(os.path.abspath(candidate if candidate.is_absolute() else cfg.base / candidate))


def _derivation_store_problem(cfg):
    declared = Path(getattr(cfg, "derivations_declared_path", derivations_path(cfg)))
    base = cfg.base.absolute()
    try:
        relative = declared.absolute().relative_to(base)
    except ValueError:
        return "derivation store escapes the project config directory"
    current = base
    for part in relative.parts:
        current = current / part
        if _is_link_like(current):
            return f"derivation store must not traverse a link or reparse point: {current}"
    root = derivations_path(cfg)
    if root.exists() and not root.is_dir():
        return "derivation store must be a directory"
    return None


def configured_logic_assets(cfg) -> tuple[dict[str, dict], dict[str, dict]]:
    """Load every configured data-only logic asset, rejecting ambiguous ids."""
    vocabularies = {}
    for path in getattr(cfg, "logic_vocabulary_paths", []):
        vocabulary = load_vocabulary(path)
        if vocabulary["id"] in vocabularies:
            raise LogicError(f"duplicate configured vocabulary id: {vocabulary['id']}")
        vocabularies[vocabulary["id"]] = vocabulary
    rule_packs = {}
    for path in getattr(cfg, "logic_rule_pack_paths", []):
        raw = _strict_file(path)
        if not isinstance(raw, dict) or not isinstance(raw.get("vocabulary_id"), str):
            raise LogicError(f"configured rule pack has no vocabulary_id: {path}")
        vocabulary = vocabularies.get(raw["vocabulary_id"])
        if vocabulary is None:
            raise LogicError(
                f"configured rule pack references unavailable vocabulary: {raw['vocabulary_id']}"
            )
        rule_pack = load_rule_pack(raw, vocabulary)
        if rule_pack["id"] in rule_packs:
            raise LogicError(f"duplicate configured rule-pack id: {rule_pack['id']}")
        rule_packs[rule_pack["id"]] = rule_pack
    return vocabularies, rule_packs


def validate_graph_logic_declarations(
        cfg, raw: dict, vocabularies: dict[str, dict], rule_packs: dict[str, dict],
) -> list[dict]:
    """Validate graph-owned formal targets and complete result bindings.

    Derivation creation validates only the declarations selected for one proof.  The
    project report uses this pass to fail early when any currently eligible formal
    claim, or any declared result binding, is structurally unusable under the
    configured policy assets.
    """
    issues = []
    nodes = raw.get("nodes", []) if isinstance(raw, dict) else []
    if not isinstance(nodes, list):
        return issues
    nodes_by_id = {
        item["id"]: item
        for item in nodes
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }

    def add(node_id, declaration, detail):
        issues.append({
            "node_id": node_id,
            "declaration": declaration,
            "detail": str(detail),
        })

    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            # Structural graph validation owns malformed node identities.
            continue

        eligible_claim = (
            node.get("type") in {"claim", "hypothesis", "prediction", "conclusion"}
            and node.get("status") in {None, "current", "confirmed"}
        )
        if eligible_claim and "logic" in node:
            try:
                declaration = node["logic"]
                if not isinstance(declaration, dict):
                    raise LogicError("claim logic declaration must be an object")
                vocabulary_id = _identifier(
                    declaration.get("vocabulary_id"),
                    "claim logic vocabulary_id",
                )
                rule_pack_id = _identifier(
                    declaration.get("rule_pack_id"),
                    "claim logic rule_pack_id",
                )
                vocabulary = vocabularies.get(vocabulary_id)
                if vocabulary is None:
                    raise LogicError(
                        f"claim logic references unconfigured vocabulary {vocabulary_id!r}"
                    )
                rule_pack = rule_packs.get(rule_pack_id)
                if rule_pack is None:
                    raise LogicError(
                        f"claim logic references unconfigured rule pack {rule_pack_id!r}"
                    )
                if rule_pack["vocabulary_id"] != vocabulary_id:
                    raise LogicError(
                        "claim logic selects an incompatible vocabulary and rule pack"
                    )
                _normal_claim_logic(declaration, vocabulary, rule_pack)
            except (KeyError, TypeError, LogicError) as exc:
                add(node_id, "logic", exc)

        if "logic_evidence_plan" in node:
            try:
                declaration = node.get("logic")
                if not isinstance(declaration, dict) or set(declaration) != {
                        "vocabulary_id", "rule_pack_id", "target"}:
                    raise LogicError(
                        "logic_evidence_plan requires a complete claim.logic declaration"
                    )
                vocabulary_id = _identifier(
                    declaration.get("vocabulary_id"),
                    "claim logic vocabulary_id",
                )
                rule_pack_id = _identifier(
                    declaration.get("rule_pack_id"),
                    "claim logic rule_pack_id",
                )
                vocabulary = vocabularies.get(vocabulary_id)
                rule_pack = rule_packs.get(rule_pack_id)
                if vocabulary is None or rule_pack is None:
                    raise LogicError(
                        "logic_evidence_plan claim policy assets are not configured"
                    )
                if rule_pack["vocabulary_id"] != vocabulary_id:
                    raise LogicError(
                        "logic_evidence_plan claim selects incompatible policy assets"
                    )
                _normal_claim_logic(declaration, vocabulary, rule_pack)
                _resolve_claim_evidence_plan(node, nodes_by_id, vocabulary)
            except (KeyError, TypeError, LogicError) as exc:
                add(node_id, "logic_evidence_plan", exc)

        if "logic_bindings" not in node:
            continue
        try:
            if node.get("type") not in RESULT_TYPES:
                raise LogicError(
                    "logic_bindings are only valid on artifact, figure, experiment, or data nodes"
                )
            declarations = node["logic_bindings"]
            if not isinstance(declarations, list):
                raise LogicError("result node logic_bindings must be a list")
            if not declarations:
                continue
            vocabulary_ids = []
            for item in declarations:
                if not isinstance(item, dict):
                    raise LogicError(
                        "each result logic binding must declare one complete fact profile"
                    )
                vocabulary_ids.append(_identifier(
                    item.get("vocabulary_id"),
                    "logic binding vocabulary_id",
                ))
            normalized_groups = []
            for vocabulary_id in sorted(set(vocabulary_ids)):
                vocabulary = vocabularies.get(vocabulary_id)
                if vocabulary is None:
                    raise LogicError(
                        f"result logic bindings reference unconfigured vocabulary "
                        f"{vocabulary_id!r}"
                    )
                normalized_groups.append((
                    vocabulary, _logic_bindings(node, vocabulary),
                ))

            artifact, data = _read_artifact(cfg, node)
            all_bindings = [
                binding
                for _vocabulary, bindings in normalized_groups
                for binding in bindings.values()
            ]
            needs_json = any(
                extractor["kind"] == "json_pointer"
                for binding in all_bindings
                for extractor in binding["arguments"].values()
            )
            needs_text = any(
                extractor["kind"] == "text_lines"
                for binding in all_bindings
                for extractor in binding["arguments"].values()
            )
            json_value = None
            json_error = None
            if data is not None and needs_json:
                try:
                    json_value = _json_with_decimal_strings(
                        data, artifact.get("path", node_id),
                    )
                except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError,
                        LogicError) as exc:
                    json_error = str(exc)
            lines = data.splitlines(keepends=True) if data is not None and needs_text else None
            for vocabulary, bindings in normalized_groups:
                types, predicates = _vocabulary_maps(vocabulary)
                for binding in bindings.values():
                    try:
                        _extract_binding_arguments(
                            binding, predicates[binding["predicate"]], types,
                            artifact, data, json_value, lines, json_error,
                        )
                    except LogicError as exc:
                        raise LogicError(
                            f"logic binding {binding['id']} cannot ground the current "
                            f"artifact: {exc}"
                        ) from exc
        except (KeyError, TypeError, LogicError) as exc:
            add(node_id, "logic_bindings", exc)

    return sorted(
        issues,
        key=lambda item: (item["node_id"], item["declaration"], item["detail"]),
    )


def _configured_assets(cfg, document):
    vocabularies, rule_packs = configured_logic_assets(cfg)
    subject = document["subject"]
    vocabulary = vocabularies.get(subject["vocabulary_id"])
    rule_pack = rule_packs.get(subject["rule_pack_id"])
    if vocabulary is None or rule_pack is None:
        raise LogicError("derivation assets must be explicitly configured by id")
    snapshot = document["mechanical_snapshot"]
    if (snapshot["vocabulary"]["document"] != vocabulary
            or snapshot["rule_pack"]["document"] != rule_pack):
        raise LogicError("derivation snapshots do not match the configured project assets")
    return vocabulary, rule_pack


@contextmanager
def _store_lock(root: Path, timeout: float = 30.0):
    lock_root = Path(tempfile.gettempdir()) / "claimtrace-derivation-locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(os.path.normcase(str(root.absolute())).encode("utf-8")).hexdigest()
    lock_path = lock_root / f"{key}.lock"
    with _PROCESS_LOCK, lock_path.open("a+b") as handle:
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
                    raise LogicError("timed out waiting for derivation store lock")
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


def _stable_derivation_bytes(root: Path, documents: list[dict]) -> int:
    """Recount stored derivation bytes through checked, stable file reads."""
    total = 0
    for item in documents:
        path = root / f"{item['id'].rsplit(':', 1)[1]}.json"
        total += len(_safe_regular_file_bytes(
            path, "stored derivation", MAX_DERIVATION_DOCUMENT_BYTES,
        ))
        if total > MAX_DERIVATION_STORE_BYTES:
            raise LogicError("derivation store exceeds the aggregate byte limit")
    return total


def append_derivation(cfg, document: dict) -> Path:
    """Append one immutable, content-addressed symbolic derivation."""
    validate_derivation_document(document)
    vocabulary, rule_pack = _configured_assets(cfg, document)
    live = evaluate_derivation(
        cfg, document, vocabulary=vocabulary, rule_pack=rule_pack,
    )
    if live.get("stale") or live != document["derived"]:
        raise LogicError("refusing to append a derivation that does not match live grounded inputs")
    store_problem = _derivation_store_problem(cfg)
    if store_problem:
        raise LogicError(store_problem)
    root = derivations_path(cfg)
    match = DERIVATION_ID_RE.fullmatch(document["id"])
    assert match is not None
    destination = root / f"{match.group(1)}.json"
    payload = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if len(payload.encode("utf-8")) > MAX_DERIVATION_DOCUMENT_BYTES:
        raise LogicError("derivation document exceeds the storage size limit")
    with _store_lock(root):
        store_problem = _derivation_store_problem(cfg)
        if store_problem:
            raise LogicError(store_problem)
        root.mkdir(parents=True, exist_ok=True)
        store_problem = _derivation_store_problem(cfg)
        if store_problem:
            raise LogicError(store_problem)
        existing_documents, integrity_issues = load_derivations(cfg)
        if integrity_issues:
            detail = "; ".join(
                f"{item['path']}: {item['detail']}" for item in integrity_issues
            )
            raise LogicError(f"derivation store integrity failed before append: {detail}")
        if not destination.exists() and len(existing_documents) >= MAX_DERIVATION_FILES:
            raise LogicError("derivation store file-count limit would be exceeded")
        existing_bytes = _stable_derivation_bytes(root, existing_documents)
        if (not destination.exists()
                and existing_bytes + len(payload.encode("utf-8"))
                > MAX_DERIVATION_STORE_BYTES):
            raise LogicError("derivation store aggregate byte limit would be exceeded")
        locked_vocabulary, locked_rule_pack = _configured_assets(cfg, document)
        locked_live = evaluate_derivation(
            cfg, document,
            vocabulary=locked_vocabulary, rule_pack=locked_rule_pack,
        )
        if locked_live.get("stale") or locked_live != document["derived"]:
            raise LogicError(
                "refusing to append a derivation whose grounded inputs changed before storage"
            )
        if destination.exists():
            if _is_link_like(destination) or not destination.is_file():
                raise LogicError("existing derivation entry is not a regular non-symlink file")
            existing = strict_json_loads(
                _safe_regular_file_bytes(
                    destination, "existing derivation", MAX_DERIVATION_DOCUMENT_BYTES,
                ).decode("utf-8-sig"),
                destination.name,
            )
            if _canonical_bytes(existing) != _canonical_bytes(document):
                raise LogicError(f"refusing to overwrite conflicting derivation: {destination.name}")
            return destination
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".claimtrace-derivation-", suffix=".tmp", dir=root.parent,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            final_vocabulary, final_rule_pack = _configured_assets(cfg, document)
            locked_live = evaluate_derivation(
                cfg, document,
                vocabulary=final_vocabulary, rule_pack=final_rule_pack,
            )
            if locked_live.get("stale") or locked_live != document["derived"]:
                raise LogicError(
                    "refusing to append a derivation whose grounded inputs changed during storage"
                )
            store_problem = _derivation_store_problem(cfg)
            if store_problem:
                raise LogicError(store_problem)
            if destination.exists():
                raise LogicError(
                    "refusing to overwrite a derivation entry that appeared during storage"
                )
            final_documents, final_issues = load_derivations(cfg)
            if final_issues:
                detail = "; ".join(
                    f"{item['path']}: {item['detail']}" for item in final_issues
                )
                raise LogicError(
                    f"derivation store integrity changed during append: {detail}"
                )
            if len(final_documents) >= MAX_DERIVATION_FILES:
                raise LogicError("derivation store file-count limit would be exceeded")
            if (_stable_derivation_bytes(root, final_documents)
                    + len(payload.encode("utf-8")) > MAX_DERIVATION_STORE_BYTES):
                raise LogicError(
                    "derivation store aggregate byte limit would be exceeded"
                )
            os.replace(temporary_name, destination)
        finally:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass
    return destination


def load_derivations(cfg) -> tuple[list[dict], list[dict]]:
    """Load valid derivations and return fail-closed store-integrity issues separately."""
    root = derivations_path(cfg)
    store_problem = _derivation_store_problem(cfg)
    if store_problem:
        return [], [{
            "code": "DERIVATION_INTEGRITY", "path": root.name, "detail": store_problem,
        }]
    if not root.exists():
        return [], []
    paths = []
    aggregate_bytes = 0
    try:
        iterator = root.iterdir()
        for path in iterator:
            paths.append(path)
            if len(paths) > MAX_DERIVATION_FILES:
                return [], [{
                    "code": "DERIVATION_INTEGRITY", "path": root.name,
                    "detail": "derivation store exceeds the file-count limit",
                }]
            try:
                aggregate_bytes += path.lstat().st_size
            except OSError as exc:
                return [], [{
                    "code": "DERIVATION_INTEGRITY", "path": path.name,
                    "detail": f"cannot inspect derivation-store entry: {exc}",
                }]
            if aggregate_bytes > MAX_DERIVATION_STORE_BYTES:
                return [], [{
                    "code": "DERIVATION_INTEGRITY", "path": root.name,
                    "detail": "derivation store exceeds the aggregate byte limit",
                }]
    except OSError as exc:
        return [], [{
            "code": "DERIVATION_INTEGRITY", "path": root.name,
            "detail": f"cannot enumerate derivation store: {exc}",
        }]
    documents, issues = [], []
    stable_aggregate_bytes = 0
    for path in sorted(paths, key=lambda item: item.name):
        if _is_link_like(path) or not path.is_file() or path.suffix != ".json":
            issues.append({
                "code": "DERIVATION_INTEGRITY", "path": path.name,
                "detail": "unexpected or non-regular derivation-store entry",
            })
            continue
        try:
            data = _safe_regular_file_bytes(
                path, "derivation document", MAX_DERIVATION_DOCUMENT_BYTES,
            )
            stable_aggregate_bytes += len(data)
            if stable_aggregate_bytes > MAX_DERIVATION_STORE_BYTES:
                return [], [{
                    "code": "DERIVATION_INTEGRITY", "path": root.name,
                    "detail": "derivation store exceeds the aggregate byte limit",
                }]
            document = strict_json_loads(
                data.decode("utf-8-sig"),
                path.name,
            )
            validate_derivation_document(document)
            match = DERIVATION_ID_RE.fullmatch(document["id"])
            if match is None or path.name != f"{match.group(1)}.json":
                raise LogicError("filename does not match derivation id")
            documents.append(document)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RecursionError,
                TypeError, AttributeError, KeyError, LogicError) as exc:
            issues.append({
                "code": "DERIVATION_INTEGRITY", "path": path.name, "detail": str(exc),
            })
    documents.sort(key=lambda item: (item["recorded_at"], item["id"]))
    return documents, issues


def _snapshot_identity(snapshot):
    return {
        "claim": snapshot["claim"],
        "results": snapshot["results"],
        "provenance_check": snapshot["provenance_check"],
        "vocabulary": {
            key: snapshot["vocabulary"][key] for key in ("id", "version", "sha256")
        },
        "rule_pack": {
            key: snapshot["rule_pack"][key]
            for key in ("id", "version", "vocabulary_id", "sha256")
        },
        "anchor_checks": snapshot["anchor_checks"],
    }


def _drift_record(kind, identity, stored, current):
    def digest(value):
        return _sha256(value) if value is not None else None

    def version(value):
        if not isinstance(value, dict):
            return None
        artifact = value.get("artifact")
        return (
            value.get("node_version_id")
            or value.get("file_version_id")
            or (artifact.get("file_version_id") if isinstance(artifact, dict) else None)
            or value.get("sha256")
        )

    return {
        "kind": kind,
        "id": identity,
        "stored_sha256": digest(stored),
        "current_sha256": digest(current),
        "stored_version": version(stored),
        "current_version": version(current),
    }


def _snapshot_drift(stored, current):
    """Return a bounded deterministic identity-level diff between proof snapshots."""
    records = []

    def add(kind, identity, old, new):
        if old != new:
            records.append(_drift_record(kind, str(identity), old, new))

    add("claim", stored["claim"].get("node_id"), stored["claim"], current["claim"])
    for kind in ("vocabulary", "rule_pack"):
        old = {key: value for key, value in stored[kind].items() if key != "document"}
        new = {key: value for key, value in current[kind].items() if key != "document"}
        add(kind, old.get("id") or new.get("id") or kind, old, new)

    stored_results = {item["node_id"]: item for item in stored["results"]}
    current_results = {item["node_id"]: item for item in current["results"]}
    for node_id in sorted(set(stored_results) | set(current_results)):
        add("result", node_id, stored_results.get(node_id), current_results.get(node_id))

    old_provenance = stored["provenance_check"]
    new_provenance = current["provenance_check"]
    old_nodes = {item["node_id"]: item for item in old_provenance["node_versions"]}
    new_nodes = {item["node_id"]: item for item in new_provenance["node_versions"]}
    result_ids = set(stored_results) | set(current_results)
    for node_id in sorted((set(old_nodes) | set(new_nodes)) - result_ids):
        add("provenance_node", node_id, old_nodes.get(node_id), new_nodes.get(node_id))

    old_edges = {item["edge_sha256"]: item for item in old_provenance["edge_versions"]}
    new_edges = {item["edge_sha256"]: item for item in new_provenance["edge_versions"]}
    for edge_id in sorted(set(old_edges) | set(new_edges)):
        add("provenance_edge", edge_id, old_edges.get(edge_id), new_edges.get(edge_id))
    old_health = {
        key: old_provenance[key] for key in ("problems", "pending", "eligible")
    }
    new_health = {
        key: new_provenance[key] for key in ("problems", "pending", "eligible")
    }
    add("provenance_health", "scoped", old_health, new_health)

    def anchor_map(snapshot):
        return {
            f"{item['fact_index']}:{item['result_id']}:{item['binding_id']}": item
            for item in snapshot["anchor_checks"]
        }

    old_anchors, new_anchors = anchor_map(stored), anchor_map(current)
    for anchor_id in sorted(set(old_anchors) | set(new_anchors)):
        add("binding_anchor", anchor_id, old_anchors.get(anchor_id), new_anchors.get(anchor_id))

    records.sort(key=lambda item: (item["kind"], item["id"]))
    if len(records) > MAX_DRIFT_ITEMS:
        omitted = len(records) - (MAX_DRIFT_ITEMS - 1)
        records = records[:MAX_DRIFT_ITEMS - 1]
        records.append({
            "kind": "truncated", "id": str(omitted),
            "stored_sha256": None, "current_sha256": None,
            "stored_version": None, "current_version": None,
        })
    return records


def evaluate_derivation(cfg, document: dict, *, vocabulary, rule_pack) -> dict:
    """Re-evaluate one immutable proof against live nodes, artifacts, and rule assets."""
    validate_derivation_document(document)
    vocabulary = load_vocabulary(vocabulary)
    rule_pack = load_rule_pack(rule_pack, vocabulary)
    subject = document["subject"]
    findings = []
    try:
        current = _snapshot_subject(
            cfg, subject["claim_id"], subject["result_ids"], document["agent_input"],
            vocabulary, rule_pack,
        )
        evaluated = _derive(document["agent_input"], current)
        stale = _snapshot_identity(current) != _snapshot_identity(document["mechanical_snapshot"])
        evaluated["drift"] = (
            _snapshot_drift(document["mechanical_snapshot"], current) if stale else []
        )
    except (engine.GraphError, LogicError) as exc:
        evaluated = _copy(document["derived"])
        stale = True
        evaluated["drift"] = [_drift_record(
            "unavailable", "live_snapshot",
            _snapshot_identity(document["mechanical_snapshot"]), None,
        )]
        findings.append(_finding(
            "DERIVATION_STALE", "error", f"live proof inputs are unavailable: {exc}",
        ))
    if stale and not findings:
        findings.append(_finding(
            "DERIVATION_STALE", "error",
            "claim, premise, artifact, vocabulary, rule pack, or evidence anchor changed",
        ))
    if stale:
        evaluated["active"] = False
        evaluated["stale"] = True
    evaluated["findings"] = [*evaluated.get("findings", []), *findings]
    return evaluated
