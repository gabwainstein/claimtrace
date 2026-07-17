"""Deterministic, reviewable semantic normalization policy.

This module deliberately separates four concerns:

* project-local terminology, which defines what a local term means;
* exact-byte ontology locks plus a bounded, deterministic term index;
* agent-authored mapping proposals with an immutable separate-review chain; and
* explicit policy releases compiled from selected accepted mapping leaves.

The implementation is stdlib-only and performs no network access, ontology
reasoning, fuzzy matching, or graph mutation.  A valid mapping means only that a
reviewed normalization decision is internally consistent with pinned bytes.  It
does not establish that the mapping, ontology, or scientific claim is true.

JSON asset schemas
------------------

``claimtrace.local-terminology/1``::

    {"schema_version", "id", "version", "terms"}

Each term contains exactly ``id``, ``kind``, ``label``, ``definition``, and
``aliases``.  Kinds are ``concept``, ``relation``, ``unit``, or ``individual``.

``claimtrace.ontology-index/1``::

    {"schema_version", "ontology_id", "version", "terms"}

Each term contains exactly ``iri``, ``kind``, ``labels``, ``synonyms``,
``definitions``, ``deprecated``, and ``parents``.  Labels, synonyms, and
definitions are ``{"text", "language"}`` objects.

``claimtrace.ontology-lock/1``::

    {"id", "schema_version", "ontology_id", "ontology_iri", "version",
     "version_iri", "license_iri", "documents", "index", "imports",
     "declared_imports_available"}

Every referenced path is portable and relative to the lock manifest.  The lock
ID commits to the manifest, while every entry also pins the exact file size and
SHA-256 digest.  Loading verifies those bytes and validates the declared index
schema and ontology identity.  The index is explicitly a project-supplied,
unverified assertion; without an ontology parser this module does *not* verify that
the indexed terms were completely or correctly extracted from the RDF/OWL bytes.

``claimtrace.semantic-mapping/1`` stores one local-term mapping proposal and its
immutable review successor chain.  ``claimtrace.semantic-policy/1`` lists exact
current accepted mapping leaf IDs selected by the caller; accepted mappings are
never gathered or activated implicitly.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .config import strict_json_loads


LOCAL_TERMINOLOGY_SCHEMA = "claimtrace.local-terminology/1"
ONTOLOGY_INDEX_SCHEMA = "claimtrace.ontology-index/1"
ONTOLOGY_LOCK_SCHEMA = "claimtrace.ontology-lock/1"
CANDIDATE_SET_SCHEMA = "claimtrace.ontology-candidates/1"
MAPPING_SCHEMA = "claimtrace.semantic-mapping/1"
POLICY_SCHEMA = "claimtrace.semantic-policy/1"

LOCAL_TERM_KINDS = frozenset({"concept", "relation", "unit", "individual"})
ONTOLOGY_TERM_KINDS = frozenset({
    "class", "object_property", "data_property", "annotation_property",
    "individual", "concept", "relation", "unit", "unknown",
})
MAPPING_RELATIONS = frozenset({
    "skos:exactMatch", "skos:closeMatch", "skos:broadMatch",
    "skos:narrowMatch", "skos:relatedMatch", "unmapped",
})
REVIEW_STATES = frozenset({
    "proposed", "accepted", "rejected", "contested", "superseded",
})
REVIEW_TRANSITIONS = {
    "proposed": frozenset({"accepted", "rejected", "contested", "superseded"}),
    "accepted": frozenset({"contested", "superseded"}),
    "rejected": frozenset({"superseded"}),
    "contested": frozenset({"accepted", "rejected", "superseded"}),
    "superseded": frozenset(),
}

ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._:/-]{0,199}$")
IRI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:.+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ONTOLOGY_LOCK_ID_RE = re.compile(r"^ontology-lock:sha256:([0-9a-f]{64})$")
CANDIDATE_SET_ID_RE = re.compile(r"^candidates:sha256:([0-9a-f]{64})$")
MAPPING_ID_RE = re.compile(r"^mapping:sha256:([0-9a-f]{64})$")
POLICY_ID_RE = re.compile(r"^semantic-policy:sha256:([0-9a-f]{64})$")
RFC3339_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)

SEARCH_ALGORITHM = "exact-iri-label-synonym-nfc-casefold-ws/1"
INDEX_ASSERTION = "project-supplied-index-not-verified-extraction"
MAX_ASSET_BYTES = 32 * 1024 * 1024
MAX_LOCK_DOCUMENT_BYTES = 64 * 1024 * 1024 * 1024
MAX_LOCK_DOCUMENTS = 1_000
MAX_TERMS = 100_000
MAX_TEXT = 4_000
MAX_LABELS_PER_TERM = 100
MAX_ALIASES_PER_TERM = 100
MAX_PARENTS_PER_TERM = 1_000
MAX_QUERY = 1_000
MAX_CANDIDATES = 1_000
MAX_LIMITATIONS = 20
MAX_MAPPING_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_MAPPING_FILES = 10_000
MAX_MAPPING_STORE_BYTES = 256 * 1024 * 1024
MAX_POLICY_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_POLICY_FILES = 10_000
MAX_POLICY_STORE_BYTES = 256 * 1024 * 1024
MAX_POLICY_MAPPINGS = 10_000
MAX_CONFIGURED_TERMINOLOGIES = 1_000
MAX_CONFIGURED_ONTOLOGY_LOCKS = 256
MAX_CONFIGURED_ONTOLOGY_DOCUMENTS = 10_000
MAX_CONFIGURED_ASSET_BYTES = 128 * 1024 * 1024
DEFAULT_CONFIGURED_ONTOLOGY_BYTES = 512 * 1024 * 1024
MAX_CONFIGURED_ONTOLOGY_BYTES = 256 * 1024 * 1024 * 1024
MAX_LOCK_TOTAL_BYTES = MAX_CONFIGURED_ONTOLOGY_BYTES
MAX_CONFIGURED_TERMS = 250_000
MAX_SEARCH_MATCHES = 10_000
MAX_BATCH_LOOKUP_REFERENCES = 2_000_000

KIND_COMPATIBILITY = {
    "concept": frozenset({"class", "concept"}),
    "relation": frozenset({
        "object_property", "data_property", "annotation_property", "relation",
    }),
    "unit": frozenset({"unit", "individual"}),
    "individual": frozenset({"individual"}),
}

_PROCESS_MAPPING_LOCK = threading.Lock()
_PROCESS_POLICY_LOCK = threading.Lock()


class SemanticError(Exception):
    """A semantic asset, record, store, or policy is invalid."""


def canonical_bytes(value) -> bytes:
    """Return canonical UTF-8 JSON bytes or fail on non-JSON data."""
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise SemanticError(f"value is not canonical JSON: {exc}") from exc


def canonical_sha256(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _copy(value):
    return strict_json_loads(canonical_bytes(value).decode("utf-8"), "semantic value")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_time(value: object) -> str:
    if not isinstance(value, str) or not RFC3339_UTC_RE.fullmatch(value):
        raise SemanticError("recorded_at must be a strict RFC 3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise SemanticError("recorded_at is not a valid UTC timestamp") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise SemanticError("recorded_at must be UTC")
    return value


def _bounded_text(value: object, label: str, maximum: int = MAX_TEXT, *,
                  nonempty: bool = False) -> str:
    if not isinstance(value, str):
        raise SemanticError(f"{label} must be a string")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise SemanticError(f"{label} contains a lone Unicode surrogate")
    value = unicodedata.normalize("NFC", value)
    if nonempty and not value.strip():
        raise SemanticError(f"{label} must be non-empty")
    if len(value) > maximum:
        raise SemanticError(f"{label} exceeds {maximum} characters")
    return value


def _bounded_historical_text(value: object, label: str, maximum: int = MAX_TEXT, *,
                             nonempty: bool = False) -> str:
    """Validate stored text without applying a newer runtime's Unicode tables."""
    if not isinstance(value, str):
        raise SemanticError(f"{label} must be a string")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise SemanticError(f"{label} contains a lone Unicode surrogate")
    if nonempty and not value.strip(" \t\r\n\f\v"):
        raise SemanticError(f"{label} must be non-empty")
    if len(value) > maximum:
        raise SemanticError(f"{label} exceeds {maximum} characters")
    return value


def _historical_iri(value: object, label: str) -> str:
    value = _bounded_historical_text(value, label, MAX_TEXT, nonempty=True)
    if (not IRI_RE.fullmatch(value)
            or any(char in " \t\r\n\f\v" or ord(char) < 0x20 or ord(char) == 0x7F
                   for char in value)):
        raise SemanticError(f"{label} must be an absolute IRI without ASCII whitespace or controls")
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise SemanticError(f"{label} must be a portable ASCII identifier")
    return value


def _iri(value: object, label: str) -> str:
    value = _bounded_text(value, label, MAX_TEXT, nonempty=True)
    if (not IRI_RE.fullmatch(value)
            or any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in value)):
        raise SemanticError(f"{label} must be an absolute IRI without whitespace or controls")
    return value


def _language(value: object, label: str, *, allow_none: bool = True) -> str | None:
    if value is None and allow_none:
        return None
    value = _bounded_text(value, label, 100, nonempty=True)
    if not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", value):
        raise SemanticError(f"{label} must be a simple BCP-47-style language tag")
    return value.lower()


def _is_link_like(path: Path) -> bool:
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        attributes = 0
    is_junction = getattr(path, "is_junction", lambda: False)
    return path.is_symlink() or bool(is_junction()) or bool(attributes & 0x400)


def _semantic_stat_identity(value, *, include_ctime: bool = True) -> tuple:
    identity = (
        value.st_dev, value.st_ino, value.st_mode, value.st_size,
        getattr(value, "st_mtime_ns", int(value.st_mtime * 1e9)),
    )
    if include_ctime:
        identity += (getattr(value, "st_ctime_ns", int(value.st_ctime * 1e9)),)
    return identity


@contextmanager
def _checked_regular_file(path_value, label: str, *, limit: int):
    """Yield one stable regular-file descriptor without symlink traversal."""
    path = Path(path_value)
    absolute = Path(os.path.abspath(path))
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SemanticError(f"{label} is unavailable: {path}: {exc}") from exc
    if os.path.normcase(str(resolved)) != os.path.normcase(str(absolute)):
        raise SemanticError(f"{label} must not traverse a symbolic link: {path}")
    if _is_link_like(path):
        raise SemanticError(f"{label} must be a regular non-link file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SemanticError(f"cannot open {label} {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SemanticError(f"{label} is not a regular file: {path}")
        if before.st_size > limit:
            raise SemanticError(f"{label} exceeds the {limit}-byte limit")
        yield descriptor, before
        after = os.fstat(descriptor)
        if _semantic_stat_identity(before) != _semantic_stat_identity(after):
            raise SemanticError(f"{label} changed while it was being read: {path}")
        try:
            final = path.resolve(strict=True)
            current = os.stat(path, follow_symlinks=False)
        except (OSError, RuntimeError) as exc:
            raise SemanticError(f"{label} changed while it was being read: {path}") from exc
        if (os.path.normcase(str(final)) != os.path.normcase(str(absolute))
                or not stat.S_ISREG(current.st_mode)
                or _semantic_stat_identity(
                    before, include_ctime=os.name != "nt",
                ) != _semantic_stat_identity(
                    current, include_ctime=os.name != "nt",
                )):
            raise SemanticError(f"{label} path changed while it was being read: {path}")
    finally:
        os.close(descriptor)


def _safe_file_bytes(path_value, label: str, limit: int) -> bytes:
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
            raise SemanticError(f"{label} exceeds the {limit}-byte limit")
        if len(data) != before.st_size:
            raise SemanticError(f"{label} changed while it was being read")
        return data


def stable_semantic_file_bytes(path_value, label: str, limit: int) -> bytes:
    """Read one bounded regular non-link file with descriptor/path stability checks."""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise SemanticError("stable semantic file byte limit must be a positive integer")
    return _safe_file_bytes(path_value, label, limit)


def fsync_semantic_directory(path_value) -> bool:
    """Best-effort directory fsync after immutable publication where supported."""
    if os.name == "nt":
        return False
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path_value, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        return False
    return True


def _load_json_source(source, label: str, *, limit: int = MAX_ASSET_BYTES):
    if isinstance(source, dict):
        return _copy(source)
    if not isinstance(source, (str, os.PathLike)):
        raise SemanticError(f"{label} source must be a path or JSON object")
    path = Path(source)
    try:
        data = _safe_file_bytes(path, label, limit)
        return strict_json_loads(data.decode("utf-8-sig"), str(path))
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise SemanticError(f"cannot read {label} {path}: {exc}") from exc


def _portable_path(value: object, label: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 4_096 or "\\" in value
            or any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F
                   for char in value)):
        raise SemanticError(f"{label} must be a non-empty portable POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise SemanticError(f"{label} must be a normalized relative path without traversal")
    normalized = path.as_posix()
    if normalized != value:
        raise SemanticError(f"{label} must be a normalized portable POSIX path")
    return normalized


def _path_under(base: Path, portable: str, label: str) -> Path:
    base = base.resolve()
    candidate = base.joinpath(*PurePosixPath(portable).parts)
    try:
        candidate.resolve(strict=False).relative_to(base)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SemanticError(f"{label} escapes its lock directory") from exc
    return candidate


def _snapshot_file(path: Path, label: str, *, limit: int = MAX_LOCK_DOCUMENT_BYTES) -> dict:
    """Stream-hash a stable file without materializing ontology bytes in memory."""
    with _checked_regular_file(path, label, limit=limit) as (descriptor, before):
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise SemanticError(f"{label} exceeds the {limit}-byte hash limit")
            digest.update(chunk)
        if total != before.st_size:
            raise SemanticError(f"{label} changed while it was being hashed")
        return {"sha256": digest.hexdigest(), "size": total}


def _snapshot_bytes(path: Path, label: str, *, limit: int) -> tuple[bytes, dict]:
    """Read bounded stable bytes once and derive their exact content snapshot."""
    data = _safe_file_bytes(path, label, limit)
    return data, {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def _ontology_byte_budget(value: object) -> int:
    if (not isinstance(value, int) or isinstance(value, bool)
            or not 1 <= value <= MAX_CONFIGURED_ONTOLOGY_BYTES):
        raise SemanticError(
            "ontology byte budget must be a positive integer no larger than "
            f"{MAX_CONFIGURED_ONTOLOGY_BYTES}"
        )
    return min(value, MAX_LOCK_TOTAL_BYTES)


def load_local_terminology(source, *, max_bytes: int = MAX_ASSET_BYTES) -> dict:
    """Load and canonicalize one strict project-local terminology asset."""
    raw = _load_json_source(source, "local terminology", limit=max_bytes)
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "id", "version", "terms"}:
        raise SemanticError("local terminology has unknown or missing top-level fields")
    if raw["schema_version"] != LOCAL_TERMINOLOGY_SCHEMA:
        raise SemanticError(f"unsupported local terminology schema: {raw['schema_version']!r}")
    terminology_id = _identifier(raw["id"], "terminology.id")
    version = _bounded_text(raw["version"], "terminology.version", 100, nonempty=True)
    if not isinstance(raw["terms"], list):
        raise SemanticError("terminology.terms must be a list")
    if len(raw["terms"]) > MAX_TERMS:
        raise SemanticError("terminology term count exceeds the deterministic limit")
    terms = []
    term_ids = set()
    for index, item in enumerate(raw["terms"]):
        if not isinstance(item, dict) or set(item) != {
                "id", "kind", "label", "definition", "aliases"}:
            raise SemanticError(f"terminology term #{index} has an invalid shape")
        term_id = _identifier(item["id"], f"terminology.terms[{index}].id")
        if term_id in term_ids:
            raise SemanticError(f"duplicate local terminology term id: {term_id}")
        term_ids.add(term_id)
        if not isinstance(item["kind"], str) or item["kind"] not in LOCAL_TERM_KINDS:
            raise SemanticError(f"local term {term_id} has unsupported kind {item['kind']!r}")
        label = _bounded_text(item["label"], f"local term {term_id} label", nonempty=True)
        definition = _bounded_text(
            item["definition"], f"local term {term_id} definition", nonempty=True)
        aliases = item["aliases"]
        if (not isinstance(aliases, list) or len(aliases) > MAX_ALIASES_PER_TERM
                or not all(isinstance(alias, str) for alias in aliases)):
            raise SemanticError(
                f"local term {term_id} aliases must be a bounded string list")
        normalized_aliases = [
            _bounded_text(alias, f"local term {term_id} alias", nonempty=True)
            for alias in aliases
        ]
        if len(normalized_aliases) != len(set(normalized_aliases)):
            raise SemanticError(f"local term {term_id} has duplicate aliases")
        terms.append({
            "id": term_id, "kind": item["kind"], "label": label,
            "definition": definition, "aliases": sorted(normalized_aliases),
        })
    return {
        "schema_version": LOCAL_TERMINOLOGY_SCHEMA,
        "id": terminology_id,
        "version": version,
        "terms": sorted(terms, key=lambda item: item["id"]),
    }


def _text_language_items(value: object, label: str, *, nonempty: bool,
                         historical: bool = False) -> list[dict]:
    if not isinstance(value, list) or (nonempty and not value):
        qualifier = "non-empty " if nonempty else ""
        raise SemanticError(f"{label} must be a {qualifier}list")
    if len(value) > MAX_LABELS_PER_TERM:
        raise SemanticError(f"{label} exceeds the deterministic item limit")
    items = []
    keys = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {"text", "language"}:
            raise SemanticError(f"{label}[{index}] must contain exactly text and language")
        text_validator = _bounded_historical_text if historical else _bounded_text
        normalized = {
            "text": text_validator(
                item["text"], f"{label}[{index}].text", nonempty=True,
            ),
            "language": _language(item["language"], f"{label}[{index}].language"),
        }
        key = (normalized["text"], normalized["language"])
        if key in keys:
            raise SemanticError(f"{label} contains a duplicate text/language value")
        keys.add(key)
        items.append(normalized)
    return sorted(items, key=lambda item: (
        "" if item["language"] is None else item["language"], item["text"],
    ))


def load_ontology_index(source) -> dict:
    """Load and canonicalize a bounded ontology term index."""
    raw = _load_json_source(source, "ontology index")
    if not isinstance(raw, dict) or set(raw) != {
            "schema_version", "ontology_id", "version", "terms"}:
        raise SemanticError("ontology index has unknown or missing top-level fields")
    if raw["schema_version"] != ONTOLOGY_INDEX_SCHEMA:
        raise SemanticError(f"unsupported ontology index schema: {raw['schema_version']!r}")
    ontology_id = _identifier(raw["ontology_id"], "ontology_index.ontology_id")
    version = _bounded_text(raw["version"], "ontology_index.version", 500, nonempty=True)
    if not isinstance(raw["terms"], list):
        raise SemanticError("ontology_index.terms must be a list")
    if len(raw["terms"]) > MAX_TERMS:
        raise SemanticError("ontology index term count exceeds the deterministic limit")
    terms = []
    iris = set()
    for index, item in enumerate(raw["terms"]):
        if not isinstance(item, dict) or set(item) != {
                "iri", "kind", "labels", "synonyms", "definitions",
                "deprecated", "parents"}:
            raise SemanticError(f"ontology term #{index} has an invalid shape")
        iri = _iri(item["iri"], f"ontology_index.terms[{index}].iri")
        if iri in iris:
            raise SemanticError(f"duplicate ontology term IRI: {iri}")
        iris.add(iri)
        if not isinstance(item["kind"], str) or item["kind"] not in ONTOLOGY_TERM_KINDS:
            raise SemanticError(f"ontology term {iri} has unsupported kind {item['kind']!r}")
        if not isinstance(item["deprecated"], bool):
            raise SemanticError(f"ontology term {iri} deprecated must be a boolean")
        parents = item["parents"]
        if (not isinstance(parents, list) or len(parents) > MAX_PARENTS_PER_TERM
                or not all(isinstance(parent, str) for parent in parents)):
            raise SemanticError(f"ontology term {iri} parents must be a bounded IRI list")
        normalized_parents = [
            _iri(parent, f"ontology term {iri} parent") for parent in parents
        ]
        if len(normalized_parents) != len(set(normalized_parents)):
            raise SemanticError(f"ontology term {iri} contains duplicate parents")
        terms.append({
            "iri": iri,
            "kind": item["kind"],
            "labels": _text_language_items(
                item["labels"], f"ontology term {iri} labels", nonempty=True),
            "synonyms": _text_language_items(
                item["synonyms"], f"ontology term {iri} synonyms", nonempty=False),
            "definitions": _text_language_items(
                item["definitions"], f"ontology term {iri} definitions", nonempty=False),
            "deprecated": item["deprecated"],
            "parents": sorted(normalized_parents),
        })
    return {
        "schema_version": ONTOLOGY_INDEX_SCHEMA,
        "ontology_id": ontology_id,
        "version": version,
        "terms": sorted(terms, key=lambda item: item["iri"]),
    }


def _load_ontology_index_bytes(data: bytes, source: object) -> dict:
    try:
        raw = strict_json_loads(data.decode("utf-8-sig"), str(source))
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise SemanticError(f"cannot read ontology index {source}: {exc}") from exc
    return load_ontology_index(raw)


def _normal_file_snapshot(value: object, label: str, *, with_path: bool = True) -> dict:
    expected = {"path", "sha256", "size"} if with_path else {"sha256", "size"}
    if not isinstance(value, dict) or set(value) != expected:
        raise SemanticError(f"{label} must contain exactly {', '.join(sorted(expected))}")
    normalized = {}
    if with_path:
        normalized["path"] = _portable_path(value["path"], f"{label}.path")
    digest = value["sha256"]
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise SemanticError(f"{label}.sha256 must be a lowercase SHA-256 digest")
    if (not isinstance(value["size"], int) or isinstance(value["size"], bool)
            or not 0 <= value["size"] <= MAX_LOCK_DOCUMENT_BYTES):
        raise SemanticError(f"{label}.size must be a non-negative bounded integer")
    normalized.update({"sha256": digest, "size": value["size"]})
    return normalized


def _normal_index_snapshot(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {
            "path", "sha256", "size", "assertion"}:
        raise SemanticError(
            "ontology_lock.index must contain path, sha256, size, and assertion")
    if value["assertion"] != INDEX_ASSERTION:
        raise SemanticError(
            "ontology_lock.index assertion must disclose that extraction is not verified")
    normalized = _normal_file_snapshot(
        {key: value[key] for key in ("path", "sha256", "size")},
        "ontology_lock.index",
    )
    if normalized["size"] > MAX_ASSET_BYTES:
        raise SemanticError("ontology_lock.index exceeds the ontology-index byte limit")
    return {**normalized, "assertion": INDEX_ASSERTION}


def _ontology_lock_id(core: dict) -> str:
    return f"ontology-lock:sha256:{canonical_sha256(core)}"


def _optional_iri(value: object, label: str) -> str | None:
    return None if value is None else _iri(value, label)


def _normal_import(value: object, index: int) -> dict:
    label = f"ontology_lock.imports[{index}]"
    if not isinstance(value, dict) or set(value) != {
            "ontology_iri", "version_iri", "ontology_lock_id"}:
        raise SemanticError(
            f"{label} must contain ontology_iri, version_iri, and ontology_lock_id")
    lock_id = value["ontology_lock_id"]
    if not isinstance(lock_id, str) or not ONTOLOGY_LOCK_ID_RE.fullmatch(lock_id):
        raise SemanticError(f"{label}.ontology_lock_id is invalid")
    return {
        "ontology_iri": _iri(value["ontology_iri"], f"{label}.ontology_iri"),
        "version_iri": _optional_iri(value["version_iri"], f"{label}.version_iri"),
        "ontology_lock_id": lock_id,
    }


def _normal_ontology_lock(raw: object) -> dict:
    if not isinstance(raw, dict) or set(raw) != {
            "id", "schema_version", "ontology_id", "ontology_iri", "version",
            "version_iri", "license_iri", "documents", "index", "imports",
            "declared_imports_available"}:
        raise SemanticError("ontology lock has unknown or missing top-level fields")
    if raw["schema_version"] != ONTOLOGY_LOCK_SCHEMA:
        raise SemanticError(f"unsupported ontology lock schema: {raw['schema_version']!r}")
    ontology_id = _identifier(raw["ontology_id"], "ontology_lock.ontology_id")
    ontology_iri = _iri(raw["ontology_iri"], "ontology_lock.ontology_iri")
    version = _bounded_text(raw["version"], "ontology_lock.version", 500, nonempty=True)
    version_iri = _optional_iri(raw["version_iri"], "ontology_lock.version_iri")
    license_iri = _optional_iri(raw["license_iri"], "ontology_lock.license_iri")
    if not isinstance(raw["declared_imports_available"], bool):
        raise SemanticError("ontology_lock.declared_imports_available must be a boolean")
    if not isinstance(raw["imports"], list) or len(raw["imports"]) > MAX_LOCK_DOCUMENTS:
        raise SemanticError("ontology_lock.imports must be a bounded list")
    imports = [_normal_import(item, index) for index, item in enumerate(raw["imports"])]
    imports = sorted(imports, key=lambda item: (
        item["ontology_iri"], "" if item["version_iri"] is None else item["version_iri"],
        item["ontology_lock_id"],
    ))
    if len(imports) != len({canonical_bytes(item) for item in imports}):
        raise SemanticError("ontology lock contains duplicate imports")
    if (not isinstance(raw["documents"], list) or not raw["documents"]
            or len(raw["documents"]) > MAX_LOCK_DOCUMENTS):
        raise SemanticError("ontology_lock.documents must be a bounded non-empty list")
    documents = [
        _normal_file_snapshot(item, f"ontology_lock.documents[{index}]")
        for index, item in enumerate(raw["documents"])
    ]
    paths = [item["path"] for item in documents]
    if len(paths) != len(set(paths)):
        raise SemanticError("ontology lock contains duplicate document paths")
    index = _normal_index_snapshot(raw["index"])
    if index["path"] in set(paths):
        raise SemanticError("ontology lock index path must be distinct from ontology documents")
    core = {
        "schema_version": ONTOLOGY_LOCK_SCHEMA,
        "ontology_id": ontology_id,
        "ontology_iri": ontology_iri,
        "version": version,
        "version_iri": version_iri,
        "license_iri": license_iri,
        "documents": sorted(documents, key=lambda item: item["path"]),
        "index": index,
        "imports": imports,
        "declared_imports_available": raw["declared_imports_available"],
    }
    if not isinstance(raw["id"], str) or not ONTOLOGY_LOCK_ID_RE.fullmatch(raw["id"]):
        raise SemanticError("ontology lock id is invalid")
    expected = _ontology_lock_id(core)
    if raw["id"] != expected:
        raise SemanticError("ontology lock id does not match canonical lock content")
    return {"id": expected, **core}


def create_ontology_lock(*, ontology_id: str, ontology_iri: str, version: str,
                         version_iri: str | None = None,
                         license_iri: str | None = None,
                         imports: list[dict] | None = None,
                         declared_imports_available: bool = False,
                         documents: list[str | os.PathLike],
                         index_path: str | os.PathLike, base: str | os.PathLike,
                         max_total_bytes: int = DEFAULT_CONFIGURED_ONTOLOGY_BYTES) -> dict:
    """Create an exact-byte lock for local files beneath ``base``.

    The returned manifest is not written automatically.  Its portable paths are
    relative to ``base``; pass ``base`` again when loading the dict, or store the
    manifest in that directory and load it by path.
    """
    ontology_id = _identifier(ontology_id, "ontology_id")
    ontology_iri = _iri(ontology_iri, "ontology_iri")
    version = _bounded_text(version, "version", 500, nonempty=True)
    version_iri = _optional_iri(version_iri, "version_iri")
    license_iri = _optional_iri(license_iri, "license_iri")
    if not isinstance(declared_imports_available, bool):
        raise SemanticError("declared_imports_available must be a boolean")
    if imports is None:
        imports = []
    if not isinstance(imports, list) or len(imports) > MAX_LOCK_DOCUMENTS:
        raise SemanticError("imports must be a bounded list")
    normalized_imports = [_normal_import(item, index) for index, item in enumerate(imports)]
    normalized_imports = sorted(normalized_imports, key=lambda item: (
        item["ontology_iri"], "" if item["version_iri"] is None else item["version_iri"],
        item["ontology_lock_id"],
    ))
    if len(normalized_imports) != len({canonical_bytes(item) for item in normalized_imports}):
        raise SemanticError("imports contains duplicates")
    if not isinstance(base, (str, os.PathLike)):
        raise SemanticError("ontology-lock base must be a local directory path")
    try:
        base_path = Path(base).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise SemanticError("ontology-lock base path is invalid") from exc
    if not isinstance(documents, list) or not documents:
        raise SemanticError("documents must be a non-empty list of local files")
    if len(documents) > MAX_LOCK_DOCUMENTS:
        raise SemanticError("ontology document count exceeds the deterministic limit")
    byte_budget = _ontology_byte_budget(max_total_bytes)

    def portable_path(value, label):
        if not isinstance(value, (str, os.PathLike)):
            raise SemanticError(f"{label} must be a local file path")
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = base_path / candidate
        try:
            return candidate.resolve(strict=False).relative_to(base_path).as_posix()
        except (OSError, RuntimeError, ValueError) as exc:
            raise SemanticError(f"{label} must be beneath the ontology-lock base") from exc

    resolved_documents = []
    declared_total = 0
    for index, value in enumerate(documents):
        portable = _portable_path(portable_path(value, f"documents[{index}]"),
                                  f"documents[{index}]")
        path = _path_under(base_path, portable, "ontology document")
        try:
            size = path.lstat().st_size
        except (OSError, ValueError) as exc:
            raise SemanticError(f"cannot inspect ontology document {portable}: {exc}") from exc
        if size > MAX_LOCK_DOCUMENT_BYTES:
            raise SemanticError("ontology document exceeds its deterministic byte limit")
        declared_total += size
        if declared_total > byte_budget:
            raise SemanticError("ontology lock exceeds its aggregate byte limit")
        resolved_documents.append((portable, path, size))
    index_portable = _portable_path(portable_path(index_path, "index_path"), "index_path")
    index_file = _path_under(base_path, index_portable, "ontology index")
    try:
        index_size = index_file.lstat().st_size
        declared_total += index_size
    except (OSError, ValueError) as exc:
        raise SemanticError(f"cannot inspect ontology index {index_portable}: {exc}") from exc
    if index_size > MAX_ASSET_BYTES:
        raise SemanticError("ontology index exceeds its deterministic byte limit")
    if declared_total > byte_budget:
        raise SemanticError("ontology lock exceeds its aggregate byte limit")

    document_entries = []
    for portable, path, preflight_size in resolved_documents:
        snapshot = _snapshot_file(path, "ontology document", limit=preflight_size)
        if snapshot["size"] != preflight_size:
            raise SemanticError(
                f"ontology document changed after byte-budget preflight: {portable}"
            )
        document_entries.append({"path": portable, **snapshot})
    index_bytes, index_snapshot = _snapshot_bytes(
        index_file, "ontology index", limit=index_size,
    )
    if index_snapshot["size"] != index_size:
        raise SemanticError("ontology index changed after byte-budget preflight")
    observed_total = sum(item["size"] for item in document_entries) + index_snapshot["size"]
    if observed_total > byte_budget:
        raise SemanticError("ontology lock exceeds its aggregate byte limit")
    index = _load_ontology_index_bytes(index_bytes, index_file)
    if index["ontology_id"] != ontology_id or index["version"] != version:
        raise SemanticError("ontology index identity/version does not match the requested lock")
    core = {
        "schema_version": ONTOLOGY_LOCK_SCHEMA,
        "ontology_id": ontology_id,
        "ontology_iri": ontology_iri,
        "version": version,
        "version_iri": version_iri,
        "license_iri": license_iri,
        "documents": sorted(document_entries, key=lambda item: item["path"]),
        "index": {
            "path": index_portable, **index_snapshot,
            "assertion": INDEX_ASSERTION,
        },
        "imports": normalized_imports,
        "declared_imports_available": declared_imports_available,
    }
    return _normal_ontology_lock({"id": _ontology_lock_id(core), **core})


def load_ontology_bundle(source, *, base: str | os.PathLike | None = None,
                         max_total_bytes: int = DEFAULT_CONFIGURED_ONTOLOGY_BYTES,
                         source_max_bytes: int = MAX_ASSET_BYTES,
                         ) -> tuple[dict, dict]:
    """Load a lock, verify every pinned byte, and return ``(lock, index)``."""
    source_path = Path(source) if isinstance(source, (str, os.PathLike)) else None
    raw = _load_json_source(source, "ontology lock", limit=source_max_bytes)
    lock = _normal_ontology_lock(raw)
    byte_budget = _ontology_byte_budget(max_total_bytes)
    declared_total = sum(item["size"] for item in lock["documents"])
    declared_total += lock["index"]["size"]
    if declared_total > byte_budget:
        raise SemanticError("ontology lock exceeds its aggregate byte limit")
    if base is None:
        if source_path is None:
            raise SemanticError("base is required when loading an ontology lock object")
        base_path = source_path.resolve().parent
    else:
        base_path = Path(base).resolve()
    for item in lock["documents"]:
        path = _path_under(base_path, item["path"], "ontology document")
        try:
            actual_size = path.lstat().st_size
        except (OSError, ValueError) as exc:
            raise SemanticError(
                f"cannot inspect ontology document {item['path']}: {exc}"
            ) from exc
        if actual_size != item["size"]:
            raise SemanticError(f"ontology document bytes drifted: {item['path']}")
        observed = _snapshot_file(
            path, f"ontology document {item['path']}", limit=item["size"],
        )
        if observed != {"sha256": item["sha256"], "size": item["size"]}:
            raise SemanticError(f"ontology document bytes drifted: {item['path']}")
    index_meta = lock["index"]
    index_path = _path_under(base_path, index_meta["path"], "ontology index")
    try:
        actual_index_size = index_path.lstat().st_size
    except (OSError, ValueError) as exc:
        raise SemanticError(
            f"cannot inspect ontology index {index_meta['path']}: {exc}"
        ) from exc
    if actual_index_size != index_meta["size"]:
        raise SemanticError(f"ontology index bytes drifted: {index_meta['path']}")
    index_bytes, observed_index = _snapshot_bytes(
        index_path, "ontology index", limit=min(index_meta["size"], MAX_ASSET_BYTES),
    )
    if observed_index != {"sha256": index_meta["sha256"], "size": index_meta["size"]}:
        raise SemanticError(f"ontology index bytes drifted: {index_meta['path']}")
    index = _load_ontology_index_bytes(index_bytes, index_path)
    if (index["ontology_id"] != lock["ontology_id"]
            or index["version"] != lock["version"]):
        raise SemanticError("ontology index identity/version does not match its lock")
    return lock, index


def load_ontology_lock(source, *, base: str | os.PathLike | None = None,
                       max_total_bytes: int = DEFAULT_CONFIGURED_ONTOLOGY_BYTES) -> dict:
    """Load and fully verify one ontology lock manifest."""
    lock, _index = load_ontology_bundle(
        source, base=base, max_total_bytes=max_total_bytes,
    )
    return lock


def configured_semantic_assets(cfg) -> dict:
    """Load every configured terminology and exact-byte ontology bundle."""
    configured_terminologies = list(getattr(cfg, "semantic_terminology_paths", []))
    configured_locks = list(getattr(cfg, "semantic_ontology_lock_paths", []))
    if len(configured_terminologies) > MAX_CONFIGURED_TERMINOLOGIES:
        raise SemanticError("configured terminology count exceeds the aggregate limit")
    if len(configured_locks) > MAX_CONFIGURED_ONTOLOGY_LOCKS:
        raise SemanticError("configured ontology-lock count exceeds the aggregate limit")
    terminologies = {}
    terminology_paths = {}
    aggregate_asset_bytes = 0
    aggregate_source_asset_bytes = 0
    aggregate_terms = 0
    for path in configured_terminologies:
        try:
            declared_size = Path(path).lstat().st_size
        except (OSError, ValueError) as exc:
            raise SemanticError(f"cannot inspect configured terminology {path}: {exc}") from exc
        aggregate_source_asset_bytes += declared_size
        if declared_size > MAX_ASSET_BYTES:
            raise SemanticError("configured terminology exceeds its per-asset byte limit")
        if aggregate_source_asset_bytes > MAX_CONFIGURED_ASSET_BYTES:
            raise SemanticError("configured semantic JSON assets exceed the aggregate byte limit")
        terminology = load_local_terminology(path, max_bytes=declared_size)
        if terminology["id"] in terminologies:
            raise SemanticError(f"duplicate configured terminology id: {terminology['id']}")
        terminologies[terminology["id"]] = terminology
        terminology_paths[terminology["id"]] = str(Path(path).resolve())
        aggregate_asset_bytes += len(canonical_bytes(terminology))
        aggregate_terms += len(terminology["terms"])
        if aggregate_asset_bytes > MAX_CONFIGURED_ASSET_BYTES:
            raise SemanticError("configured semantic JSON assets exceed the aggregate byte limit")
        if aggregate_terms > MAX_CONFIGURED_TERMS:
            raise SemanticError("configured semantic term count exceeds the aggregate limit")
    locks = {}
    indexes = {}
    lock_paths = {}
    aggregate_ontology_bytes = 0
    aggregate_ontology_documents = 0
    semantic_identities = {}
    logical_identities = {}
    ontology_byte_budget = _ontology_byte_budget(getattr(
        cfg, "semantic_max_ontology_bytes", DEFAULT_CONFIGURED_ONTOLOGY_BYTES,
    ))
    for path in configured_locks:
        try:
            lock_source_size = Path(path).lstat().st_size
        except (OSError, ValueError) as exc:
            raise SemanticError(f"cannot inspect configured ontology lock {path}: {exc}") from exc
        aggregate_source_asset_bytes += lock_source_size
        if lock_source_size > MAX_ASSET_BYTES:
            raise SemanticError("configured ontology lock exceeds its per-asset byte limit")
        if aggregate_source_asset_bytes > MAX_CONFIGURED_ASSET_BYTES:
            raise SemanticError("configured semantic JSON assets exceed the aggregate byte limit")
        preview = _normal_ontology_lock(_load_json_source(
            path, "ontology lock", limit=lock_source_size,
        ))
        declared_ontology_bytes = (
            sum(item["size"] for item in preview["documents"])
            + preview["index"]["size"]
        )
        aggregate_ontology_documents += len(preview["documents"])
        if aggregate_ontology_documents > MAX_CONFIGURED_ONTOLOGY_DOCUMENTS:
            raise SemanticError(
                "configured ontology document count exceeds the aggregate limit"
            )
        if (aggregate_ontology_bytes + declared_ontology_bytes
                > ontology_byte_budget):
            raise SemanticError("configured locked ontology bytes exceed the aggregate limit")
        aggregate_source_asset_bytes += preview["index"]["size"]
        if aggregate_source_asset_bytes > MAX_CONFIGURED_ASSET_BYTES:
            raise SemanticError("configured semantic JSON assets exceed the aggregate byte limit")
        declared_json_bytes = len(canonical_bytes(preview)) + preview["index"]["size"]
        if aggregate_asset_bytes + declared_json_bytes > MAX_CONFIGURED_ASSET_BYTES:
            raise SemanticError("configured semantic JSON assets exceed the aggregate byte limit")
        lock, index = load_ontology_bundle(
            path, max_total_bytes=ontology_byte_budget,
            source_max_bytes=lock_source_size,
        )
        if lock != preview:
            raise SemanticError("ontology lock changed while configured assets were loading")
        if lock["id"] in locks:
            raise SemanticError(f"duplicate configured ontology lock id: {lock['id']}")
        locks[lock["id"]] = lock
        indexes[lock["id"]] = index
        lock_paths[lock["id"]] = str(Path(path).resolve())
        aggregate_asset_bytes += len(canonical_bytes(lock)) + len(canonical_bytes(index))
        aggregate_terms += len(index["terms"])
        aggregate_ontology_bytes += sum(item["size"] for item in lock["documents"])
        aggregate_ontology_bytes += lock["index"]["size"]
        if aggregate_asset_bytes > MAX_CONFIGURED_ASSET_BYTES:
            raise SemanticError("configured semantic JSON assets exceed the aggregate byte limit")
        if aggregate_ontology_bytes > ontology_byte_budget:
            raise SemanticError("configured locked ontology bytes exceed the aggregate limit")
        if aggregate_terms > MAX_CONFIGURED_TERMS:
            raise SemanticError("configured semantic term count exceeds the aggregate limit")
        semantic_identity = (
            lock["ontology_iri"],
            lock["version_iri"] if lock["version_iri"] is not None else lock["version"],
        )
        previous = semantic_identities.get(semantic_identity)
        if previous is not None and previous != lock["id"]:
            raise SemanticError(
                "conflicting configured ontology locks claim the same ontology/version IRI: "
                f"{previous}, {lock['id']}"
            )
        semantic_identities[semantic_identity] = lock["id"]
        logical_identity = (lock["ontology_id"], lock["version"])
        previous = logical_identities.get(logical_identity)
        if previous is not None and previous != lock["id"]:
            raise SemanticError(
                "conflicting configured ontology locks claim the same logical identity/version: "
                f"{previous}, {lock['id']}"
            )
        logical_identities[logical_identity] = lock["id"]
    for lock in locks.values():
        for imported in lock["imports"]:
            imported_lock = locks.get(imported["ontology_lock_id"])
            if imported_lock is None:
                if lock["declared_imports_available"]:
                    raise SemanticError(
                        f"ontology lock {lock['id']} requires every manifest-listed import "
                        f"to be configured, but {imported['ontology_lock_id']} is unavailable"
                    )
                continue
            if imported_lock["ontology_iri"] != imported["ontology_iri"]:
                raise SemanticError(
                    f"ontology import identity does not match lock {imported['ontology_lock_id']}"
                )
            if (imported["version_iri"] is not None
                    and imported_lock["version_iri"] != imported["version_iri"]):
                raise SemanticError(
                    f"ontology import version does not match lock {imported['ontology_lock_id']}"
                )
    return {
        "terminologies": terminologies,
        "terminology_paths": terminology_paths,
        "ontology_locks": locks,
        "ontology_indexes": indexes,
        "ontology_lock_paths": lock_paths,
    }


def normalize_candidate_query(value: object) -> str:
    """Return the versioned exact-search key: NFC, collapsed whitespace, casefold."""
    # The caller-facing query is separately capped at MAX_QUERY.  Ontology labels
    # may use the wider bounded semantic-text limit, so the shared normalizer must
    # be able to normalize them without making an otherwise valid index unsearchable.
    value = _bounded_text(value, "search text", MAX_TEXT, nonempty=True)
    return " ".join(value.split()).casefold()


def _display_label(term: dict, language: str | None) -> dict:
    labels = term["labels"]

    def rank(item):
        lang = item["language"]
        return (
            0 if language is not None and lang == language else
            1 if lang is None else 2,
            "" if lang is None else lang,
            item["text"],
        )

    return min(labels, key=rank)


def _candidate_from_term(lock: dict, term: dict, query_text: str, normalized_query: str,
                         language: str | None) -> dict | None:
    matches = []
    # IRI identity is case-sensitive and whitespace-sensitive.  Human-facing labels
    # and synonyms use the explicitly versioned Unicode/case/whitespace normalizer.
    if term["iri"] == query_text:
        matches.append((0, "iri", term["iri"], None))
    for item in term["labels"]:
        if normalize_candidate_query(item["text"]) == normalized_query:
            matches.append((1, "preferred_label", item["text"], item["language"]))
    for item in term["synonyms"]:
        if normalize_candidate_query(item["text"]) == normalized_query:
            matches.append((2, "synonym", item["text"], item["language"]))
    if not matches:
        return None

    def match_rank(item):
        _tier, _kind, text, lang = item
        return (
            item[0],
            0 if language is not None and lang == language else
            1 if lang is None else 2,
            "" if lang is None else lang,
            text,
        )

    tier, matched_on, matched_text, matched_language = min(matches, key=match_rank)
    display = _display_label(term, language)
    return {
        "ontology_lock_id": lock["id"],
        "ontology_id": lock["ontology_id"],
        "ontology_version": lock["version"],
        "iri": term["iri"],
        "kind": term["kind"],
        "deprecated": term["deprecated"],
        "matched_on": matched_on,
        "matched_text": matched_text,
        "matched_language": matched_language,
        "label": display["text"],
        "label_language": display["language"],
        "definitions": _copy(term["definitions"]),
        "parents": list(term["parents"]),
        "term_sha256": canonical_sha256(term),
        "_match_tier": tier,
    }


def _build_batch_candidate_lookup(locks: dict[str, dict], indexes: dict[str, dict],
                                  queries: list[str]) -> dict:
    """Index only requested exact keys in one bounded pass over configured terms."""
    if set(locks) != set(indexes):
        raise SemanticError("configured ontology locks and indexes do not have identical ids")
    wanted_iris = set()
    wanted_normalized = set()
    for query in queries:
        query_text = _bounded_text(query, "candidate query", MAX_QUERY, nonempty=True)
        wanted_iris.add(query_text)
        wanted_normalized.add(normalize_candidate_query(query_text))
    iri_matches = {key: [] for key in wanted_iris}
    normalized_matches = {key: [] for key in wanted_normalized}
    references = 0
    for lock_id in sorted(locks):
        lock = locks[lock_id]
        index = indexes[lock_id]
        if (index["ontology_id"], index["version"]) != (
                lock["ontology_id"], lock["version"]):
            raise SemanticError(f"ontology index does not match lock {lock_id}")
        for term in index["terms"]:
            reference = (lock, term)
            if term["iri"] in iri_matches:
                iri_matches[term["iri"]].append(reference)
                references += 1
            matching_keys = {
                key
                for item in (*term["labels"], *term["synonyms"])
                for key in [normalize_candidate_query(item["text"])]
                if key in normalized_matches
            }
            for key in matching_keys:
                normalized_matches[key].append(reference)
                references += 1
            if references > MAX_BATCH_LOOKUP_REFERENCES:
                raise SemanticError(
                    "semantic mapping batch exceeds the candidate-lookup work limit"
                )
    candidate_passes = 0
    for query in queries:
        query_text = _bounded_text(query, "candidate query", MAX_QUERY, nonempty=True)
        normalized = normalize_candidate_query(query_text)
        # This conservative sum can double-count a term that matches both its IRI
        # and a label.  It intentionally bounds the actual per-query candidate pass
        # without constructing potentially huge union sets merely to count them.
        candidate_passes += len(iri_matches[query_text])
        candidate_passes += len(normalized_matches[normalized])
        if candidate_passes > MAX_BATCH_LOOKUP_REFERENCES:
            raise SemanticError(
                "semantic mapping batch exceeds the candidate-evaluation work limit"
            )
    return {"iri": iri_matches, "normalized": normalized_matches}


def _bound_candidate_evaluation_profiles(lookup: dict, profiles: set[tuple]) -> None:
    """Bound candidate passes for the exact raw-query/language/limit cache keys."""
    candidate_passes = 0
    for query, language, limit in sorted(
            profiles, key=lambda item: (item[0], "" if item[1] is None else item[1], item[2])):
        query_text = _bounded_text(query, "candidate query", MAX_QUERY, nonempty=True)
        _language(language, "candidate language") if language is not None else None
        if (not isinstance(limit, int) or isinstance(limit, bool)
                or not 1 <= limit <= MAX_CANDIDATES):
            raise SemanticError("candidate evaluation profile limit is invalid")
        normalized = normalize_candidate_query(query_text)
        candidate_passes += len(lookup["iri"].get(query_text, []))
        candidate_passes += len(lookup["normalized"].get(normalized, []))
        if candidate_passes > MAX_BATCH_LOOKUP_REFERENCES:
            raise SemanticError(
                "semantic mapping batch exceeds the candidate-profile work limit"
            )


def _candidate_set_from_assets(query: object, locks: dict[str, dict],
                               indexes: dict[str, dict], *, language: str | None,
                               limit: int, lookup: dict | None = None) -> dict:
    query_text = _bounded_text(query, "candidate query", MAX_QUERY, nonempty=True)
    normalized_query = normalize_candidate_query(query_text)
    language = _language(language, "candidate language") if language is not None else None
    if (not isinstance(limit, int) or isinstance(limit, bool)
            or not 1 <= limit <= MAX_CANDIDATES):
        raise SemanticError(f"candidate limit must be between 1 and {MAX_CANDIDATES}")
    if set(locks) != set(indexes):
        raise SemanticError("configured ontology locks and indexes do not have identical ids")
    candidates = []
    if lookup is None:
        term_sources = (
            (locks[lock_id], term)
            for lock_id in sorted(locks)
            for term in indexes[lock_id]["terms"]
        )
    else:
        references = [
            *lookup["iri"].get(query_text, []),
            *lookup["normalized"].get(normalized_query, []),
        ]
        unique_references = {
            (lock["id"], term["iri"]): (lock, term)
            for lock, term in references
        }
        term_sources = (
            unique_references[key] for key in sorted(unique_references)
        )
    for lock, term in term_sources:
        if lookup is None:
            index = indexes[lock["id"]]
            if (index["ontology_id"], index["version"]) != (
                    lock["ontology_id"], lock["version"]):
                raise SemanticError(f"ontology index does not match lock {lock['id']}")
        candidate = _candidate_from_term(
            lock, term, query_text, normalized_query, language)
        if candidate is not None:
            candidates.append(candidate)
            if len(candidates) > MAX_SEARCH_MATCHES:
                raise SemanticError(
                    "exact ontology matches exceed the deterministic search-work limit")
    candidates.sort(key=lambda item: (
        item["_match_tier"],
        1 if item["deprecated"] else 0,
        0 if language is not None and item["matched_language"] == language else
        1 if item["matched_language"] is None else 2,
        item["ontology_id"], item["ontology_version"], item["iri"],
        item["matched_on"], item["matched_text"],
        "" if item["matched_language"] is None else item["matched_language"],
        item["ontology_lock_id"], item["term_sha256"],
    ))
    total = len(candidates)
    visible = []
    for item in candidates[:limit]:
        item = dict(item)
        item.pop("_match_tier", None)
        visible.append(item)
    core = {
        "schema_version": CANDIDATE_SET_SCHEMA,
        "algorithm": SEARCH_ALGORITHM,
        "unicode_data_version": unicodedata.unidata_version,
        "index_assertion": INDEX_ASSERTION,
        "limitations": [
            "project_supplied_index_extraction_not_verified",
            "no_rdf_owl_reasoning",
            "exact_matching_only",
        ],
        "query": query_text,
        "normalized_query": normalized_query,
        "language": language,
        "limit": limit,
        "ontology_lock_ids": sorted(locks),
        "total_match_count": total,
        "truncated": total > limit,
        "candidates": visible,
    }
    return {"id": f"candidates:sha256:{canonical_sha256(core)}", **core}


def search_ontology_candidates(cfg, query: object, *, language: str | None = None,
                               limit: int | None = None) -> dict:
    """Search configured locked indexes without network or fuzzy matching."""
    assets = configured_semantic_assets(cfg)
    if language is None:
        language = getattr(cfg, "semantic_language", None)
    if limit is None:
        limit = getattr(cfg, "semantic_max_candidates", 25)
    return _candidate_set_from_assets(
        query, assets["ontology_locks"], assets["ontology_indexes"],
        language=language, limit=limit,
    )


def _candidate_sort_key(item: dict, language: str | None) -> tuple:
    tiers = {"iri": 0, "preferred_label": 1, "synonym": 2}
    matched_language = item["matched_language"]
    return (
        tiers[item["matched_on"]],
        1 if item["deprecated"] else 0,
        0 if language is not None and matched_language == language else
        1 if matched_language is None else 2,
        item["ontology_id"], item["ontology_version"], item["iri"],
        item["matched_on"], item["matched_text"],
        "" if matched_language is None else matched_language,
        item["ontology_lock_id"], item["term_sha256"],
    )


def validate_candidate_set(document: object) -> None:
    """Validate one stored deterministic candidate set and its content address."""
    if not isinstance(document, dict) or set(document) != {
            "id", "schema_version", "algorithm", "query", "normalized_query",
            "language", "limit", "ontology_lock_ids", "total_match_count",
            "truncated", "candidates", "unicode_data_version", "index_assertion",
            "limitations"}:
        raise SemanticError("candidate set has unknown or missing top-level fields")
    if document["schema_version"] != CANDIDATE_SET_SCHEMA:
        raise SemanticError(f"unsupported candidate-set schema: {document['schema_version']!r}")
    if document["algorithm"] != SEARCH_ALGORITHM:
        raise SemanticError(f"unsupported candidate-search algorithm: {document['algorithm']!r}")
    unicode_version = document["unicode_data_version"]
    if (not isinstance(unicode_version, str)
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", unicode_version)):
        raise SemanticError("candidate-set Unicode data version is invalid")
    current_unicode_profile = unicode_version == unicodedata.unidata_version
    if document["index_assertion"] != INDEX_ASSERTION:
        raise SemanticError("candidate-set index assertion is invalid")
    if document["limitations"] != [
            "project_supplied_index_extraction_not_verified",
            "no_rdf_owl_reasoning",
            "exact_matching_only"]:
        raise SemanticError("candidate-set limitations are invalid")
    text_validator = _bounded_text if current_unicode_profile else _bounded_historical_text
    iri_validator = _iri if current_unicode_profile else _historical_iri
    query = text_validator(
        document["query"], "candidate_set.query", MAX_QUERY, nonempty=True,
    )
    if current_unicode_profile and query != document["query"]:
        raise SemanticError("candidate_set.query is not canonical NFC text")
    normalized_query = text_validator(
        document["normalized_query"], "candidate_set.normalized_query", MAX_QUERY,
        nonempty=True,
    )
    if current_unicode_profile and normalized_query != normalize_candidate_query(query):
        raise SemanticError("candidate_set.normalized_query does not match its query")
    language = (
        _language(document["language"], "candidate_set.language")
        if document["language"] is not None else None
    )
    if document["language"] != language:
        raise SemanticError("candidate_set.language is not canonical")
    limit = document["limit"]
    if (not isinstance(limit, int) or isinstance(limit, bool)
            or not 1 <= limit <= MAX_CANDIDATES):
        raise SemanticError("candidate_set.limit is invalid")
    lock_ids = document["ontology_lock_ids"]
    if (not isinstance(lock_ids, list)
            or not all(isinstance(item, str) and ONTOLOGY_LOCK_ID_RE.fullmatch(item)
                       for item in lock_ids)
            or lock_ids != sorted(set(lock_ids))):
        raise SemanticError("candidate_set.ontology_lock_ids must be sorted unique lock ids")
    total = document["total_match_count"]
    if (not isinstance(total, int) or isinstance(total, bool)
            or not 0 <= total <= MAX_SEARCH_MATCHES):
        raise SemanticError("candidate_set.total_match_count must be a non-negative integer")
    if not isinstance(document["truncated"], bool):
        raise SemanticError("candidate_set.truncated must be a boolean")
    candidates = document["candidates"]
    if not isinstance(candidates, list) or len(candidates) > limit:
        raise SemanticError("candidate_set.candidates exceeds its declared limit")
    normalized = []
    keys = set()
    expected_candidate_fields = {
        "ontology_lock_id", "ontology_id", "ontology_version", "iri", "kind",
        "deprecated", "matched_on", "matched_text", "matched_language", "label",
        "label_language", "definitions", "parents", "term_sha256",
    }
    for index, item in enumerate(candidates):
        if not isinstance(item, dict) or set(item) != expected_candidate_fields:
            raise SemanticError(f"candidate_set.candidates[{index}] has an invalid shape")
        lock_id = item["ontology_lock_id"]
        if (not isinstance(lock_id, str) or not ONTOLOGY_LOCK_ID_RE.fullmatch(lock_id)
                or lock_id not in lock_ids):
            raise SemanticError(f"candidate_set.candidates[{index}] has an unknown lock id")
        ontology_id = _identifier(item["ontology_id"], f"candidate[{index}].ontology_id")
        ontology_version = text_validator(
            item["ontology_version"], f"candidate[{index}].ontology_version", 500,
            nonempty=True,
        )
        iri = iri_validator(item["iri"], f"candidate[{index}].iri")
        if not isinstance(item["kind"], str) or item["kind"] not in ONTOLOGY_TERM_KINDS:
            raise SemanticError(f"candidate[{index}].kind is invalid")
        if not isinstance(item["deprecated"], bool):
            raise SemanticError(f"candidate[{index}].deprecated must be a boolean")
        if (not isinstance(item["matched_on"], str)
                or item["matched_on"] not in {"iri", "preferred_label", "synonym"}):
            raise SemanticError(f"candidate[{index}].matched_on is invalid")
        matched_text = text_validator(
            item["matched_text"], f"candidate[{index}].matched_text", nonempty=True)
        matched_language = (
            _language(item["matched_language"], f"candidate[{index}].matched_language")
            if item["matched_language"] is not None else None
        )
        label = text_validator(
            item["label"], f"candidate[{index}].label", nonempty=True,
        )
        label_language = (
            _language(item["label_language"], f"candidate[{index}].label_language")
            if item["label_language"] is not None else None
        )
        definitions = _text_language_items(
            item["definitions"], f"candidate[{index}].definitions", nonempty=False,
            historical=not current_unicode_profile,
        )
        if definitions != item["definitions"]:
            raise SemanticError(f"candidate[{index}].definitions is not canonical")
        parents = item["parents"]
        if (not isinstance(parents, list) or len(parents) > MAX_PARENTS_PER_TERM
                or not all(isinstance(parent, str) for parent in parents)):
            raise SemanticError(f"candidate[{index}].parents must be a bounded IRI list")
        normalized_parents = [
            iri_validator(parent, f"candidate[{index}].parent") for parent in parents
        ]
        if normalized_parents != sorted(set(normalized_parents)):
            raise SemanticError(f"candidate[{index}].parents is not sorted and unique")
        digest = item["term_sha256"]
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise SemanticError(f"candidate[{index}].term_sha256 is invalid")
        normalized_item = {
            "ontology_lock_id": lock_id,
            "ontology_id": ontology_id,
            "ontology_version": ontology_version,
            "iri": iri,
            "kind": item["kind"],
            "deprecated": item["deprecated"],
            "matched_on": item["matched_on"],
            "matched_text": matched_text,
            "matched_language": matched_language,
            "label": label,
            "label_language": label_language,
            "definitions": definitions,
            "parents": normalized_parents,
            "term_sha256": digest,
        }
        if normalized_item != item:
            raise SemanticError(f"candidate[{index}] is not canonical")
        key = (lock_id, iri)
        if key in keys:
            raise SemanticError("candidate set contains a duplicate locked IRI")
        keys.add(key)
        normalized.append(normalized_item)
    if normalized != sorted(normalized, key=lambda item: _candidate_sort_key(item, language)):
        raise SemanticError("candidate set is not in deterministic total order")
    if total < len(candidates):
        raise SemanticError("candidate-set total is smaller than its visible candidate count")
    if document["truncated"] != (total > len(candidates)):
        raise SemanticError("candidate-set truncated flag disagrees with its counts")
    if not document["truncated"] and total != len(candidates):
        raise SemanticError("untruncated candidate set must expose every match")
    if document["truncated"] and len(candidates) != limit:
        raise SemanticError("truncated candidate set must fill its declared limit")
    core = {key: document[key] for key in document if key != "id"}
    expected_id = f"candidates:sha256:{canonical_sha256(core)}"
    if (not isinstance(document["id"], str)
            or not CANDIDATE_SET_ID_RE.fullmatch(document["id"])
            or document["id"] != expected_id):
        raise SemanticError("candidate-set id does not match canonical content")


def _validate_provenance(value: object, label: str, *, historical: bool = False) -> dict:
    if not isinstance(value, dict) or not value:
        raise SemanticError(f"{label} must be a non-empty object")
    allowed = {"agent", "model", "skill_version", "prompt_sha256"}
    if set(value) - allowed:
        raise SemanticError(f"{label} contains an unknown field")
    text_validator = _bounded_historical_text if historical else _bounded_text
    provenance = {
        "agent": text_validator(
            value.get("agent"), f"{label}.agent", 200, nonempty=True,
        ),
    }
    for key in ("model", "skill_version"):
        if key in value:
            provenance[key] = text_validator(
                value[key], f"{label}.{key}", 200, nonempty=True)
    if "prompt_sha256" in value:
        digest = value["prompt_sha256"]
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise SemanticError(f"{label}.prompt_sha256 must be a lowercase SHA-256 digest")
        provenance["prompt_sha256"] = digest
    return provenance


def _reject_reasoning_fields(value, path: str = "agent_input") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in {"chain_of_thought", "chain-of-thought", "reasoning", "cot"}:
                raise SemanticError(f"{path}.{key} is not permitted; use concise rationale")
            _reject_reasoning_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_reasoning_fields(child, f"{path}[{index}]")


def validate_mapping_agent_input(value: object, *, historical: bool = False) -> dict:
    """Validate and canonicalize the complete agent-authored mapping section."""
    if not isinstance(value, dict) or set(value) != {
            "relation", "target", "candidate_query", "candidate_set_id",
            "rationale", "limitations", "provenance"}:
        raise SemanticError("mapping agent_input has unknown or missing fields")
    try:
        _reject_reasoning_fields(value)
    except RecursionError as exc:
        raise SemanticError("mapping agent_input nesting exceeds the deterministic limit") from exc
    relation = value["relation"]
    if not isinstance(relation, str) or relation not in MAPPING_RELATIONS:
        raise SemanticError(
            "mapping relation must be an allowed SKOS mapping relation or unmapped")
    target = value["target"]
    if relation == "unmapped":
        if target is not None:
            raise SemanticError("an unmapped decision must have a null target")
        normalized_target = None
    else:
        if not isinstance(target, dict) or set(target) != {"ontology_lock_id", "iri"}:
            raise SemanticError("a mapped decision target needs ontology_lock_id and iri")
        lock_id = target["ontology_lock_id"]
        if not isinstance(lock_id, str) or not ONTOLOGY_LOCK_ID_RE.fullmatch(lock_id):
            raise SemanticError("mapping target ontology_lock_id is invalid")
        iri_validator = _historical_iri if historical else _iri
        normalized_target = {
            "ontology_lock_id": lock_id,
            "iri": iri_validator(target["iri"], "mapping target iri"),
        }
    text_validator = _bounded_historical_text if historical else _bounded_text
    candidate_query = text_validator(
        value["candidate_query"], "mapping candidate_query", MAX_QUERY, nonempty=True)
    candidate_set_id = value["candidate_set_id"]
    if (not isinstance(candidate_set_id, str)
            or not CANDIDATE_SET_ID_RE.fullmatch(candidate_set_id)):
        raise SemanticError("mapping candidate_set_id is invalid")
    rationale = text_validator(
        value["rationale"], "mapping rationale", 2_000, nonempty=True,
    )
    limitations = value["limitations"]
    if (not isinstance(limitations, list) or len(limitations) > MAX_LIMITATIONS
            or not all(isinstance(item, str) for item in limitations)):
        raise SemanticError("mapping limitations must be a bounded string list")
    normalized_limitations = [
        text_validator(item, "mapping limitation", 500, nonempty=True)
        for item in limitations
    ]
    return {
        "relation": relation,
        "target": normalized_target,
        "candidate_query": candidate_query,
        "candidate_set_id": candidate_set_id,
        "rationale": rationale,
        "limitations": normalized_limitations,
        "provenance": _validate_provenance(
            value["provenance"], "mapping provenance", historical=historical,
        ),
    }


def _normal_subject(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"terminology_id", "term_id"}:
        raise SemanticError("mapping subject needs exactly terminology_id and term_id")
    return {
        "terminology_id": _identifier(value["terminology_id"], "mapping terminology_id"),
        "term_id": _identifier(value["term_id"], "mapping term_id"),
    }


def _local_term_snapshot(terminology: dict, term_id: str) -> dict:
    term = next((item for item in terminology["terms"] if item["id"] == term_id), None)
    if term is None:
        raise SemanticError(
            f"local term {term_id!r} is not declared by terminology {terminology['id']!r}")
    return {
        "terminology_id": terminology["id"],
        "terminology_version": terminology["version"],
        "terminology_sha256": canonical_sha256(terminology),
        "term": _copy(term),
        "term_sha256": canonical_sha256(term),
    }


def _selected_candidate(agent_input: dict, candidate_set: dict) -> dict | None:
    target = agent_input["target"]
    if target is None:
        return None
    matches = [
        item for item in candidate_set["candidates"]
        if (item["ontology_lock_id"], item["iri"])
        == (target["ontology_lock_id"], target["iri"])
    ]
    if len(matches) != 1:
        raise SemanticError(
            "mapping target is not one unique member of the recomputed candidate set")
    return _copy(matches[0])


def _mapping_snapshot_from_assets(subject: dict, agent_input: dict, assets: dict, *,
                                  language: str | None, limit: int,
                                  require_candidate_set_id: bool = True) -> dict:
    terminology = assets["terminologies"].get(subject["terminology_id"])
    if terminology is None:
        raise SemanticError(
            f"mapping references unconfigured terminology {subject['terminology_id']!r}")
    candidate_set = _candidate_set_from_assets(
        agent_input["candidate_query"], assets["ontology_locks"],
        assets["ontology_indexes"], language=language, limit=limit,
    )
    if require_candidate_set_id and candidate_set["id"] != agent_input["candidate_set_id"]:
        raise SemanticError(
            "agent candidate_set_id does not match deterministic candidates under current locks")
    selected = _selected_candidate(agent_input, candidate_set)
    return {
        "local_term": _local_term_snapshot(terminology, subject["term_id"]),
        "candidate_set": candidate_set,
        "selected_candidate": selected,
    }


def _validate_local_term_snapshot(value: object, subject: dict, *,
                                  historical: bool = False) -> None:
    if not isinstance(value, dict) or set(value) != {
            "terminology_id", "terminology_version", "terminology_sha256", "term",
            "term_sha256"}:
        raise SemanticError("mapping local-term snapshot has an invalid shape")
    if value["terminology_id"] != subject["terminology_id"]:
        raise SemanticError("mapping local-term snapshot terminology does not match subject")
    text_validator = _bounded_historical_text if historical else _bounded_text
    terminology_version = text_validator(
        value["terminology_version"], "snapshot terminology version", 100,
        nonempty=True,
    )
    if not historical and terminology_version != value["terminology_version"]:
        raise SemanticError("snapshot terminology version is not canonical")
    for key in ("terminology_sha256", "term_sha256"):
        if not isinstance(value[key], str) or not SHA256_RE.fullmatch(value[key]):
            raise SemanticError(f"mapping local-term snapshot {key} is invalid")
    term = value["term"]
    if historical:
        if not isinstance(term, dict) or set(term) != {
                "id", "kind", "label", "definition", "aliases"}:
            raise SemanticError("mapping historical local term has an invalid shape")
        _identifier(term["id"], "mapping historical local term id")
        if not isinstance(term["kind"], str) or term["kind"] not in LOCAL_TERM_KINDS:
            raise SemanticError("mapping historical local term kind is invalid")
        text_validator(term["label"], "mapping historical local term label", nonempty=True)
        text_validator(
            term["definition"], "mapping historical local term definition", nonempty=True,
        )
        aliases = term["aliases"]
        if (not isinstance(aliases, list) or len(aliases) > MAX_ALIASES_PER_TERM
                or not all(isinstance(alias, str) for alias in aliases)):
            raise SemanticError("mapping historical local term aliases are invalid")
        normalized_aliases = [
            text_validator(alias, "mapping historical local term alias", nonempty=True)
            for alias in aliases
        ]
        if normalized_aliases != sorted(set(normalized_aliases)):
            raise SemanticError("mapping historical local term aliases are not canonical")
    else:
        terminology = {
            "schema_version": LOCAL_TERMINOLOGY_SCHEMA,
            "id": value["terminology_id"],
            "version": value["terminology_version"],
            "terms": [term],
        }
        normalized_term = load_local_terminology(terminology)["terms"][0]
        if normalized_term != term:
            raise SemanticError("mapping local-term snapshot is not canonical")
    if term["id"] != subject["term_id"]:
        raise SemanticError("mapping local-term snapshot term does not match subject")
    if canonical_sha256(term) != value["term_sha256"]:
        raise SemanticError("mapping local-term snapshot term hash is invalid")


def _validate_mapping_snapshot(value: object, subject: dict, agent_input: dict) -> None:
    if not isinstance(value, dict) or set(value) != {
            "local_term", "candidate_set", "selected_candidate"}:
        raise SemanticError("mapping mechanical_snapshot has an invalid shape")
    validate_candidate_set(value["candidate_set"])
    historical = (
        value["candidate_set"]["unicode_data_version"] != unicodedata.unidata_version
    )
    _validate_local_term_snapshot(
        value["local_term"], subject, historical=historical,
    )
    if value["candidate_set"]["id"] != agent_input["candidate_set_id"]:
        raise SemanticError("mapping snapshot candidate set does not match agent input")
    expected_selected = _selected_candidate(agent_input, value["candidate_set"])
    if value["selected_candidate"] != expected_selected:
        raise SemanticError("mapping snapshot selected candidate is invalid")


def _mapping_finding(code: str, severity: str, detail: str) -> dict:
    return {"code": code, "severity": severity, "detail": detail}


def _derive_mapping(snapshot: dict, agent_input: dict, review: dict, *,
                    stale_details: list[str] | None = None,
                    conflict_ids: list[str] | None = None,
                    integrity_details: list[str] | None = None,
                    live: bool = False) -> dict:
    findings = []
    selected = snapshot["selected_candidate"]
    if snapshot["candidate_set"]["truncated"]:
        findings.append(_mapping_finding(
            "SEMANTIC_CANDIDATE_SET_TRUNCATED", "error",
            "candidate set is truncated; increase the deterministic search limit before review",
        ))
    if selected is not None and selected["deprecated"]:
        findings.append(_mapping_finding(
            "SEMANTIC_MAPPING_DEPRECATED_TARGET", "error",
            f"selected ontology term is deprecated: {selected['iri']}",
        ))
    if selected is not None:
        local_kind = snapshot["local_term"]["term"]["kind"]
        if selected["kind"] not in KIND_COMPATIBILITY[local_kind]:
            findings.append(_mapping_finding(
                "SEMANTIC_MAPPING_KIND_MISMATCH", "error",
                f"local {local_kind} cannot normalize to ontology {selected['kind']} under "
                "the configured kind-compatibility policy",
            ))
    for detail in stale_details or []:
        findings.append(_mapping_finding("SEMANTIC_MAPPING_STALE", "error", detail))
    if conflict_ids:
        findings.append(_mapping_finding(
            "SEMANTIC_MAPPING_CONFLICT", "error",
            "current accepted mapping conflicts with: " + ", ".join(sorted(conflict_ids)),
        ))
    for detail in integrity_details or []:
        findings.append(_mapping_finding("SEMANTIC_MAPPING_INTEGRITY", "error", detail))
    errors = any(item["severity"] == "error" for item in findings)
    effective_state = "contested" if conflict_ids else review["state"]
    result = {
        "effective_review_state": effective_state,
        "stale": bool(stale_details),
        "findings": findings,
    }
    eligibility = effective_state == "accepted" and not errors
    if live:
        result["eligible_for_policy"] = eligibility
    else:
        result["snapshot_eligible_for_policy"] = eligibility
    return result


def _mapping_id(core: dict) -> str:
    return f"mapping:sha256:{canonical_sha256(core)}"


def create_mapping_proposal(cfg, terminology_id: str, term_id: str, agent_input: dict, *,
                            actor: str, recorded_at: str | None = None,
                            language: str | None = None,
                            limit: int | None = None) -> dict:
    """Create a proposed mapping grounded in current configured local assets."""
    subject = _normal_subject({"terminology_id": terminology_id, "term_id": term_id})
    normalized_input = validate_mapping_agent_input(agent_input)
    actor = _bounded_text(actor, "mapping actor", 500, nonempty=True)
    timestamp = _validate_time(recorded_at or _utc_now())
    assets = configured_semantic_assets(cfg)
    if language is None:
        language = getattr(cfg, "semantic_language", None)
    language = _language(language, "semantic language") if language is not None else None
    if limit is None:
        limit = getattr(cfg, "semantic_max_candidates", 25)
    snapshot = _mapping_snapshot_from_assets(
        subject, normalized_input, assets, language=language, limit=limit,
    )
    review = {"state": "proposed", "actor": actor, "supersedes_mapping_id": None}
    derived = _derive_mapping(snapshot, normalized_input, review)
    core = {
        "schema_version": MAPPING_SCHEMA,
        "recorded_at": timestamp,
        "subject": subject,
        "mechanical_snapshot": snapshot,
        "agent_input": normalized_input,
        "review": review,
        "derived": derived,
    }
    return {"id": _mapping_id(core), **core}


def validate_mapping_document(document: object) -> None:
    """Validate mapping shape, content address, snapshots, and computed fields."""
    if not isinstance(document, dict) or set(document) != {
            "id", "schema_version", "recorded_at", "subject", "mechanical_snapshot",
            "agent_input", "review", "derived"}:
        raise SemanticError("semantic mapping has unknown or missing top-level fields")
    if document["schema_version"] != MAPPING_SCHEMA:
        raise SemanticError(f"unsupported semantic mapping schema: {document['schema_version']!r}")
    _validate_time(document["recorded_at"])
    subject = _normal_subject(document["subject"])
    if subject != document["subject"]:
        raise SemanticError("mapping subject is not canonical")
    candidate_profile = (
        document.get("mechanical_snapshot", {}).get("candidate_set", {})
        if isinstance(document.get("mechanical_snapshot"), dict) else {}
    )
    historical = (
        candidate_profile.get("unicode_data_version") != unicodedata.unidata_version
    )
    agent_input = validate_mapping_agent_input(
        document["agent_input"], historical=historical,
    )
    if agent_input != document["agent_input"]:
        raise SemanticError("mapping agent_input is not canonical")
    _validate_mapping_snapshot(document["mechanical_snapshot"], subject, agent_input)
    review = document["review"]
    if not isinstance(review, dict) or set(review) != {
            "state", "actor", "supersedes_mapping_id"}:
        raise SemanticError("mapping review has an invalid shape")
    if not isinstance(review["state"], str) or review["state"] not in REVIEW_STATES:
        raise SemanticError("mapping review state is invalid")
    text_validator = _bounded_historical_text if historical else _bounded_text
    review_actor = text_validator(
        review["actor"], "mapping review actor", 500, nonempty=True,
    )
    if not historical and review_actor != review["actor"]:
        raise SemanticError("mapping review actor is not canonical")
    predecessor = review["supersedes_mapping_id"]
    if predecessor is not None and (
            not isinstance(predecessor, str) or not MAPPING_ID_RE.fullmatch(predecessor)):
        raise SemanticError("mapping review predecessor id is invalid")
    if review["state"] == "proposed" and predecessor is not None:
        raise SemanticError("a mapping proposal cannot supersede another mapping")
    if review["state"] != "proposed" and predecessor is None:
        raise SemanticError("a mapping review decision must supersede an earlier mapping")
    expected_derived = _derive_mapping(document["mechanical_snapshot"], agent_input, review)
    if document["derived"] != expected_derived:
        raise SemanticError("mapping derived content does not match deterministic policy")
    core = {key: document[key] for key in document if key != "id"}
    expected_id = _mapping_id(core)
    if (not isinstance(document["id"], str) or not MAPPING_ID_RE.fullmatch(document["id"])
            or document["id"] != expected_id):
        raise SemanticError("mapping id does not match canonical document content")


def create_mapping_review(document: dict, state: str, *, actor: str,
                          recorded_at: str | None = None) -> dict:
    """Create one immutable successor decision without changing proposal content."""
    validate_mapping_document(document)
    if (document["mechanical_snapshot"]["candidate_set"]["unicode_data_version"]
            != unicodedata.unidata_version):
        raise SemanticError(
            "mapping uses an older Unicode normalization profile; create a new proposal "
            "under the current runtime before review"
        )
    if not isinstance(state, str) or state not in REVIEW_STATES - {"proposed"}:
        raise SemanticError("mapping review state must be accepted, rejected, contested, or superseded")
    previous_state = document["review"]["state"]
    if state not in REVIEW_TRANSITIONS[previous_state]:
        raise SemanticError(f"invalid mapping review transition {previous_state} -> {state}")
    actor = _bounded_text(actor, "mapping review actor", 500, nonempty=True)
    if previous_state == "proposed" and actor == document["review"]["actor"]:
        raise SemanticError("mapping proposal and first review must use different actor strings")
    timestamp = _validate_time(recorded_at or _utc_now())
    previous_time = datetime.fromisoformat(document["recorded_at"][:-1] + "+00:00")
    next_time = datetime.fromisoformat(timestamp[:-1] + "+00:00")
    if next_time < previous_time:
        raise SemanticError("mapping review cannot predate its predecessor")
    review = {
        "state": state,
        "actor": actor,
        "supersedes_mapping_id": document["id"],
    }
    core = {
        "schema_version": MAPPING_SCHEMA,
        "recorded_at": timestamp,
        "subject": _copy(document["subject"]),
        "mechanical_snapshot": _copy(document["mechanical_snapshot"]),
        "agent_input": _copy(document["agent_input"]),
        "review": review,
        "derived": _derive_mapping(
            document["mechanical_snapshot"], document["agent_input"], review),
    }
    return {"id": _mapping_id(core), **core}


def mappings_path(cfg) -> Path:
    value = getattr(
        cfg, "semantic_mappings_declared_path",
        getattr(cfg, "semantic_mappings_path", None),
    )
    if value is None:
        raise SemanticError("semantic mappings path is not configured")
    root = Path(os.path.abspath(value))
    problem = _store_root_problem(root, "semantic_mapping", base=cfg.base)
    if problem:
        raise SemanticError(problem)
    return root


def policies_path(cfg) -> Path:
    value = getattr(
        cfg, "semantic_policies_declared_path",
        getattr(cfg, "semantic_policies_path", None),
    )
    if value is None:
        raise SemanticError("semantic policies path is not configured")
    root = Path(os.path.abspath(value))
    problem = _store_root_problem(root, "semantic_policy", base=cfg.base)
    if problem:
        raise SemanticError(problem)
    return root


@contextmanager
def _store_lock(root: Path, process_lock: threading.Lock, label: str,
                timeout: float = 30.0):
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or timeout <= 0):
        raise SemanticError(f"{label} store lock timeout must be positive")
    deadline = time.monotonic() + timeout
    key = hashlib.sha256(
        (label + "\0" + os.path.normcase(str(Path(os.path.abspath(root))))).encode("utf-8")
    ).hexdigest()
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        create_mutex.restype = ctypes.c_void_p
        wait_for = kernel32.WaitForSingleObject
        wait_for.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        wait_for.restype = ctypes.c_ulong
        release = kernel32.ReleaseMutex
        release.argtypes = [ctypes.c_void_p]
        release.restype = ctypes.c_int
        close = kernel32.CloseHandle
        close.argtypes = [ctypes.c_void_p]
        close.restype = ctypes.c_int
        handle = create_mutex(None, 0, "Local\\ClaimtraceSemantic-" + key)
        if not handle:
            raise SemanticError(f"cannot create {label} named lock")
        mutex_acquired = False
        thread_acquired = False
        try:
            thread_acquired = process_lock.acquire(
                timeout=max(0.0, deadline - time.monotonic()),
            )
            if not thread_acquired:
                raise SemanticError(f"timed out waiting for {label} store lock")
            remaining_ms = max(
                0, min(int((deadline - time.monotonic()) * 1000), 0xFFFFFFFE),
            )
            wait_result = wait_for(handle, remaining_ms)
            if wait_result == 0x00000102:
                raise SemanticError(f"timed out waiting for {label} store lock")
            if wait_result not in {0x00000000, 0x00000080}:
                raise SemanticError(f"cannot acquire {label} store lock")
            mutex_acquired = True
            yield
        finally:
            if mutex_acquired:
                release(handle)
            if thread_acquired:
                process_lock.release()
            close(handle)
        return

    user_suffix = str(os.getuid()) if hasattr(os, "getuid") else "user"
    lock_root = Path(tempfile.gettempdir()) / f"claimtrace-semantic-locks-{user_suffix}"
    lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root_info = lock_root.lstat()
    except OSError as exc:
        raise SemanticError(f"cannot inspect {label} lock directory: {exc}") from exc
    if (_is_link_like(lock_root) or not stat.S_ISDIR(root_info.st_mode)
            or (hasattr(os, "getuid") and root_info.st_uid != os.getuid())
            or stat.S_IMODE(root_info.st_mode) & 0o077):
        raise SemanticError(f"{label} lock directory is not private and trustworthy")
    lock_path = lock_root / f"{key}.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise SemanticError(f"cannot open {label} store lock: {exc}") from exc
    thread_acquired = process_lock.acquire(
        timeout=max(0.0, deadline - time.monotonic()),
    )
    if not thread_acquired:
        os.close(descriptor)
        raise SemanticError(f"timed out waiting for {label} store lock")
    try:
        with os.fdopen(descriptor, "a+b") as handle:
            opened = os.fstat(handle.fileno())
            current = lock_path.lstat()
            if (not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
                raise SemanticError(f"{label} store lock path is unstable")
            while True:
                try:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (OSError, BlockingIOError):
                    if time.monotonic() >= deadline:
                        raise SemanticError(f"timed out waiting for {label} store lock")
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            try:
                try:
                    locked = os.fstat(handle.fileno())
                    current = lock_path.lstat()
                except OSError as exc:
                    raise SemanticError(f"{label} store lock path changed: {exc}") from exc
                if ((locked.st_dev, locked.st_ino) != (current.st_dev, current.st_ino)
                        or not stat.S_ISREG(current.st_mode)):
                    raise SemanticError(f"{label} store lock path changed while acquiring it")
                yield
            finally:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        process_lock.release()


def _store_root_problem(root: Path, label: str, *, base: Path | None = None) -> str | None:
    if base is not None:
        base = Path(os.path.abspath(base))
        root = Path(os.path.abspath(root))
        try:
            relative = root.relative_to(base)
        except ValueError:
            return f"{label} store escapes the project config directory"
        current = base
        if _is_link_like(current):
            return f"{label} store must not traverse a link or reparse point: {current}"
        for part in relative.parts:
            current = current / part
            if _is_link_like(current):
                return f"{label} store must not traverse a link or reparse point: {current}"
    if _is_link_like(root):
        return f"{label} store must not be a symbolic link or junction"
    if root.exists() and not root.is_dir():
        return f"{label} store must be a directory"
    return None


def _enumerate_store(root: Path, label: str, *, max_files: int,
                     max_bytes: int, base: Path | None = None) -> tuple[list[Path], list[dict]]:
    problem = _store_root_problem(root, label, base=base)
    if problem:
        return [], [{"code": f"{label.upper()}_INTEGRITY", "path": root.name,
                     "detail": problem}]
    if not root.exists():
        return [], []
    paths = []
    aggregate = 0
    try:
        for path in root.iterdir():
            paths.append(path)
            if len(paths) > max_files:
                return [], [{
                    "code": f"{label.upper()}_INTEGRITY", "path": root.name,
                    "detail": f"{label} store exceeds its file-count limit",
                }]
            try:
                aggregate += path.lstat().st_size
            except OSError as exc:
                return [], [{
                    "code": f"{label.upper()}_INTEGRITY", "path": path.name,
                    "detail": f"cannot inspect store entry: {exc}",
                }]
            if aggregate > max_bytes:
                return [], [{
                    "code": f"{label.upper()}_INTEGRITY", "path": root.name,
                    "detail": f"{label} store exceeds its aggregate-byte limit",
                }]
    except OSError as exc:
        return [], [{
            "code": f"{label.upper()}_INTEGRITY", "path": root.name,
            "detail": f"cannot enumerate store: {exc}",
        }]
    return sorted(paths, key=lambda item: item.name), []


def _store_directory_signature(paths: list[Path]) -> tuple:
    """Return bounded entry identities so concurrent additions/deletions are visible."""
    signature = []
    for path in paths:
        try:
            info = path.lstat()
            identity = (
                info.st_dev, info.st_ino, info.st_mode, info.st_size,
                info.st_mtime_ns, getattr(info, "st_ctime_ns", 0),
            )
        except OSError as exc:
            identity = ("unavailable", type(exc).__name__)
        signature.append((path.name, identity))
    return tuple(signature)


def _finish_store_read(root: Path, label: str, paths: list[Path],
                       initial_signature: tuple, *, max_files: int,
                       max_bytes: int, issues: list[dict], base: Path | None = None) -> None:
    final_paths, final_issues = _enumerate_store(
        root, label, max_files=max_files, max_bytes=max_bytes, base=base,
    )
    issues.extend(final_issues)
    if final_issues or _store_directory_signature(final_paths) != initial_signature:
        issues.append({
            "code": f"{label.upper()}_INTEGRITY",
            "path": root.name,
            "detail": f"{label} store changed while it was being read",
        })


def load_mappings(cfg) -> tuple[list[dict], list[dict]]:
    """Load valid mapping records plus fail-closed store/chain integrity issues."""
    root = mappings_path(cfg)
    paths, issues = _enumerate_store(
        root, "semantic_mapping", max_files=MAX_MAPPING_FILES,
        max_bytes=MAX_MAPPING_STORE_BYTES, base=cfg.base,
    )
    if issues:
        return [], issues
    initial_signature = _store_directory_signature(paths)
    documents = []
    stable_bytes = 0
    for path in paths:
        if _is_link_like(path) or not path.is_file() or path.suffix != ".json":
            issues.append({
                "code": "SEMANTIC_MAPPING_INTEGRITY", "path": path.name,
                "detail": "unexpected or non-regular mapping-store entry",
            })
            continue
        try:
            data = _safe_file_bytes(path, "semantic mapping document", MAX_MAPPING_DOCUMENT_BYTES)
            stable_bytes += len(data)
            if stable_bytes > MAX_MAPPING_STORE_BYTES:
                raise SemanticError("mapping store exceeds its stable aggregate-byte limit")
            document = strict_json_loads(data.decode("utf-8-sig"), path.name)
            validate_mapping_document(document)
            match = MAPPING_ID_RE.fullmatch(document["id"])
            if match is None or path.name != match.group(1) + ".json":
                raise SemanticError("mapping filename does not match its content id")
            documents.append(document)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RecursionError,
                SemanticError) as exc:
            issues.append({
                "code": "SEMANTIC_MAPPING_INTEGRITY", "path": path.name,
                "detail": str(exc),
            })
    _finish_store_read(
        root, "semantic_mapping", paths, initial_signature,
        max_files=MAX_MAPPING_FILES, max_bytes=MAX_MAPPING_STORE_BYTES,
        issues=issues, base=cfg.base,
    )
    documents.sort(key=lambda item: (item["recorded_at"], item["id"]))
    by_id = {item["id"]: item for item in documents}
    successors = {}
    for document in documents:
        predecessor_id = document["review"]["supersedes_mapping_id"]
        if predecessor_id is None:
            continue
        successors.setdefault(predecessor_id, []).append(document)
        predecessor = by_id.get(predecessor_id)
        if predecessor is None:
            issues.append({
                "code": "SEMANTIC_MAPPING_REVIEW_CHAIN", "path": document["id"],
                "detail": f"superseded mapping is missing: {predecessor_id}",
            })
            continue
        before_state = predecessor["review"]["state"]
        after_state = document["review"]["state"]
        if after_state not in REVIEW_TRANSITIONS[before_state]:
            issues.append({
                "code": "SEMANTIC_MAPPING_REVIEW_CHAIN", "path": document["id"],
                "detail": f"invalid mapping review transition {before_state} -> {after_state}",
            })
        if (before_state == "proposed"
                and document["review"]["actor"] == predecessor["review"]["actor"]):
            issues.append({
                "code": "SEMANTIC_MAPPING_REVIEW_CHAIN", "path": document["id"],
                "detail": "mapping proposal and first review use the same actor string",
            })
        for field in ("schema_version", "subject", "mechanical_snapshot", "agent_input"):
            if document[field] != predecessor[field]:
                issues.append({
                    "code": "SEMANTIC_MAPPING_REVIEW_CHAIN", "path": document["id"],
                    "detail": f"mapping review changed immutable field: {field}",
                })
        before_time = datetime.fromisoformat(predecessor["recorded_at"][:-1] + "+00:00")
        after_time = datetime.fromisoformat(document["recorded_at"][:-1] + "+00:00")
        if after_time < before_time:
            issues.append({
                "code": "SEMANTIC_MAPPING_REVIEW_CHAIN", "path": document["id"],
                "detail": "mapping review predates its predecessor",
            })
    for predecessor_id, children in sorted(successors.items()):
        if len(children) <= 1:
            continue
        child_ids = ", ".join(sorted(item["id"] for item in children))
        for child in children:
            issues.append({
                "code": "SEMANTIC_MAPPING_REVIEW_CHAIN", "path": child["id"],
                "detail": f"mapping review chain branches after {predecessor_id}: {child_ids}",
            })
    return documents, issues


def current_mapping_leaves(documents: list[dict]) -> list[dict]:
    """Return current immutable review-chain leaves, including rejected leaves."""
    for document in documents:
        validate_mapping_document(document)
    superseded = {
        item["review"]["supersedes_mapping_id"] for item in documents
        if item["review"]["supersedes_mapping_id"] is not None
    }
    return sorted(
        (item for item in documents if item["id"] not in superseded),
        key=lambda item: (item["subject"]["terminology_id"],
                          item["subject"]["term_id"], item["id"]),
    )


def detect_mapping_conflicts(documents: list[dict]) -> dict[str, list[str]]:
    """Identify disagreeing accepted current leaves for the same local term."""
    grouped = {}
    for document in current_mapping_leaves(documents):
        if document["review"]["state"] != "accepted":
            continue
        key = (document["subject"]["terminology_id"], document["subject"]["term_id"])
        grouped.setdefault(key, []).append(document)
    conflicts = {}
    for group in grouped.values():
        signatures = {
            canonical_bytes({
                "relation": item["agent_input"]["relation"],
                "target": item["agent_input"]["target"],
            })
            for item in group
        }
        if len(signatures) <= 1:
            continue
        ids = sorted(item["id"] for item in group)
        for mapping_id in ids:
            conflicts[mapping_id] = [item for item in ids if item != mapping_id]
    return conflicts


def _evaluate_mapping_with_assets(document: dict, assets: dict, *,
                                  conflict_ids: list[str] | None,
                                  candidate_cache: dict, candidate_lookup: dict) -> dict:
    validate_mapping_document(document)
    stale_details = []
    stored = document["mechanical_snapshot"]
    try:
        candidate_set = stored["candidate_set"]
        cache_key = (
            document["agent_input"]["candidate_query"],
            candidate_set["language"], candidate_set["limit"],
        )
        current_candidates = candidate_cache.get(cache_key)
        if current_candidates is None:
            current_candidates = _candidate_set_from_assets(
                document["agent_input"]["candidate_query"],
                assets["ontology_locks"], assets["ontology_indexes"],
                language=candidate_set["language"], limit=candidate_set["limit"],
                lookup=candidate_lookup,
            )
            candidate_cache[cache_key] = current_candidates
        terminology = assets["terminologies"].get(document["subject"]["terminology_id"])
        if terminology is None:
            raise SemanticError(
                "mapping references unconfigured terminology "
                f"{document['subject']['terminology_id']!r}"
            )
        current = {
            "local_term": _local_term_snapshot(
                terminology, document["subject"]["term_id"],
            ),
            "candidate_set": current_candidates,
            "selected_candidate": _selected_candidate(
                document["agent_input"], current_candidates,
            ),
        }
        if current["local_term"] != stored["local_term"]:
            stale_details.append("local terminology or term definition changed")
        # Compare the complete candidate record, not only the selected IRI.  New
        # alternatives, definition changes, deprecation, or lock-set changes all
        # require a fresh semantic review.
        if current["candidate_set"] != stored["candidate_set"]:
            stale_details.append("deterministic ontology candidate set changed")
        if current["selected_candidate"] != stored["selected_candidate"]:
            stale_details.append("selected ontology candidate changed or disappeared")
    except SemanticError as exc:
        stale_details.append(f"current semantic inputs are unavailable or invalid: {exc}")
    return _derive_mapping(
        stored, document["agent_input"], document["review"],
        stale_details=stale_details, conflict_ids=conflict_ids, live=True,
    )


def _evaluate_mappings_from_assets(documents: list[dict], assets: dict, *,
                                   conflicts: dict[str, list[str]] | None = None
                                   ) -> list[dict]:
    """Evaluate mappings from one caller-owned immutable semantic asset snapshot."""
    if not isinstance(documents, list) or len(documents) > MAX_MAPPING_FILES:
        raise SemanticError("mapping evaluation batch exceeds its deterministic limit")
    for document in documents:
        validate_mapping_document(document)
    if not documents:
        return []
    if conflicts is None:
        conflicts = {}
    if not isinstance(conflicts, dict):
        raise SemanticError("mapping conflicts must be a mapping-id keyed object")
    candidate_cache = {}
    try:
        candidate_lookup = _build_batch_candidate_lookup(
            assets["ontology_locks"], assets["ontology_indexes"],
            sorted({document["agent_input"]["candidate_query"] for document in documents}),
        )
        _bound_candidate_evaluation_profiles(candidate_lookup, {
            (
                document["agent_input"]["candidate_query"],
                document["mechanical_snapshot"]["candidate_set"]["language"],
                document["mechanical_snapshot"]["candidate_set"]["limit"],
            )
            for document in documents
        })
    except SemanticError as exc:
        detail = f"current semantic inputs are unavailable or invalid: {exc}"
        return [
            _derive_mapping(
                document["mechanical_snapshot"], document["agent_input"],
                document["review"], stale_details=[detail],
                conflict_ids=conflicts.get(document["id"]), live=True,
            )
            for document in documents
        ]
    evaluations = [
        _evaluate_mapping_with_assets(
            document, assets, conflict_ids=conflicts.get(document["id"]),
            candidate_cache=candidate_cache, candidate_lookup=candidate_lookup,
        )
        for document in documents
    ]
    return evaluations


def evaluate_mappings(cfg, documents: list[dict], *,
                      conflicts: dict[str, list[str]] | None = None) -> list[dict]:
    """Reevaluate a bounded mapping batch after loading locked assets exactly once."""
    if not isinstance(documents, list) or len(documents) > MAX_MAPPING_FILES:
        raise SemanticError("mapping evaluation batch exceeds its deterministic limit")
    for document in documents:
        validate_mapping_document(document)
    if not documents:
        return []
    if conflicts is None:
        conflicts = {}
    if not isinstance(conflicts, dict):
        raise SemanticError("mapping conflicts must be a mapping-id keyed object")
    try:
        assets = configured_semantic_assets(cfg)
    except SemanticError as exc:
        detail = f"current semantic inputs are unavailable or invalid: {exc}"
        return [
            _derive_mapping(
                document["mechanical_snapshot"], document["agent_input"],
                document["review"], stale_details=[detail],
                conflict_ids=conflicts.get(document["id"]), live=True,
            )
            for document in documents
        ]
    evaluations = _evaluate_mappings_from_assets(
        documents, assets, conflicts=conflicts,
    )
    try:
        current_assets = configured_semantic_assets(cfg)
        assets_stable = canonical_bytes(current_assets) == canonical_bytes(assets)
    except SemanticError:
        assets_stable = False
    if assets_stable:
        return evaluations
    detail = "semantic assets changed while mappings were being evaluated"
    return [
        _derive_mapping(
            document["mechanical_snapshot"], document["agent_input"],
            document["review"], stale_details=[detail],
            conflict_ids=conflicts.get(document["id"]), live=True,
        )
        for document in documents
    ]


def evaluate_mapping(cfg, document: dict, *, conflict_ids: list[str] | None = None) -> dict:
    """Reevaluate one stored mapping against current terminology and ontology bytes."""
    validate_mapping_document(document)
    return evaluate_mappings(
        cfg, [document], conflicts={document["id"]: conflict_ids or []},
    )[0]


def _append_record_unlocked(root: Path, document: dict, *, id_re, prefix: str,
                            document_limit: int, file_limit: int, store_limit: int,
                            label: str, validator, base: Path) -> Path:
    match = id_re.fullmatch(document["id"])
    assert match is not None
    destination = root / f"{match.group(1)}.json"
    payload = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    payload_bytes = payload.encode("utf-8")
    if len(payload_bytes) > document_limit:
        raise SemanticError(f"{label} document exceeds its byte limit")
    problem = _store_root_problem(root, label, base=base)
    if problem:
        raise SemanticError(problem)
    root.mkdir(parents=True, exist_ok=True)
    problem = _store_root_problem(root, label, base=base)
    if problem:
        raise SemanticError(problem)
    if destination.exists():
        if _is_link_like(destination) or not destination.is_file():
            raise SemanticError(f"existing {label} entry is not a regular non-link file")
        try:
            existing = strict_json_loads(
                _safe_file_bytes(destination, f"existing {label}", document_limit)
                .decode("utf-8-sig"), destination.name,
            )
            validator(existing)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RecursionError,
                SemanticError) as exc:
            raise SemanticError(
                f"existing {label} entry is malformed or unstable: {destination.name}: {exc}"
            ) from exc
        if canonical_bytes(existing) != canonical_bytes(document):
            raise SemanticError(f"refusing to overwrite conflicting {label}: {destination.name}")
        return destination
    paths, issues = _enumerate_store(
        root, label, max_files=file_limit, max_bytes=store_limit, base=base,
    )
    if issues:
        raise SemanticError("; ".join(item["detail"] for item in issues))
    if len(paths) >= file_limit:
        raise SemanticError(f"{label} store file-count limit would be exceeded")
    stable_size = 0
    for path in paths:
        if _is_link_like(path) or not path.is_file() or path.suffix != ".json":
            raise SemanticError(f"unexpected entry in {label} store: {path.name}")
        stable_size += len(_safe_file_bytes(path, f"stored {label}", document_limit))
    if stable_size + len(payload_bytes) > store_limit:
        raise SemanticError(f"{label} store aggregate-byte limit would be exceeded")
    problem = _store_root_problem(root, label, base=base)
    if problem:
        raise SemanticError(problem)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{prefix}-", suffix=".tmp", dir=root,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # Hard-linking a fully flushed temporary file is an atomic create-if-absent
        # operation on the same filesystem.  Unlike os.replace, it can never
        # overwrite a record that appeared after the preflight check.
        try:
            os.link(temporary_name, destination)
            fsync_semantic_directory(root)
        except FileExistsError as exc:
            raise SemanticError(
                f"refusing to overwrite {label} that appeared during append") from exc
        except OSError as exc:
            raise SemanticError(f"cannot atomically append {label}: {exc}") from exc
        problem = _store_root_problem(root, label, base=base)
        if problem:
            raise SemanticError(f"{label} store changed during append: {problem}")
    finally:
        try:
            Path(temporary_name).unlink()
            fsync_semantic_directory(root)
        except FileNotFoundError:
            pass
    return destination


def append_mapping(cfg, document: dict) -> Path:
    """Atomically append one immutable mapping after fail-closed store validation."""
    validate_mapping_document(document)
    root = mappings_path(cfg)
    with _store_lock(root, _PROCESS_MAPPING_LOCK, "semantic mapping"):
        live = evaluate_mapping(cfg, document)
        if live["stale"]:
            raise SemanticError(
                "refusing to append a mapping whose semantic inputs are stale: "
                + "; ".join(item["detail"] for item in live["findings"])
            )
        existing, issues = load_mappings(cfg)
        if issues:
            raise SemanticError(
                "mapping store integrity failed before append: "
                + "; ".join(f"{item['path']}: {item['detail']}" for item in issues)
            )
        if document["review"]["supersedes_mapping_id"] is not None:
            predecessor = next((
                item for item in existing
                if item["id"] == document["review"]["supersedes_mapping_id"]
            ), None)
            if predecessor is None:
                raise SemanticError("mapping review predecessor is absent from the store")
            leaf_ids = {item["id"] for item in current_mapping_leaves(existing)}
            if predecessor["id"] not in leaf_ids:
                raise SemanticError("mapping review predecessor is no longer a current leaf")
            expected = create_mapping_review(
                predecessor, document["review"]["state"],
                actor=document["review"]["actor"], recorded_at=document["recorded_at"],
            )
            if expected != document:
                raise SemanticError("mapping review does not match its stored predecessor")
        return _append_record_unlocked(
            root, document, id_re=MAPPING_ID_RE, prefix="mapping",
            document_limit=MAX_MAPPING_DOCUMENT_BYTES, file_limit=MAX_MAPPING_FILES,
            store_limit=MAX_MAPPING_STORE_BYTES, label="semantic_mapping",
            validator=validate_mapping_document, base=cfg.base,
        )


def append_mapping_review(cfg, mapping_id: str, state: str, *, actor: str,
                          recorded_at: str | None = None) -> tuple[dict, Path]:
    """Create and atomically append a review successor to the current mapping leaf."""
    if not isinstance(mapping_id, str) or not MAPPING_ID_RE.fullmatch(mapping_id):
        raise SemanticError("mapping_id is invalid")
    root = mappings_path(cfg)
    with _store_lock(root, _PROCESS_MAPPING_LOCK, "semantic mapping"):
        documents, issues = load_mappings(cfg)
        if issues:
            raise SemanticError(
                "mapping store integrity failed before review: "
                + "; ".join(f"{item['path']}: {item['detail']}" for item in issues)
            )
        leaves = {item["id"]: item for item in current_mapping_leaves(documents)}
        predecessor = leaves.get(mapping_id)
        if predecessor is None:
            if any(item["id"] == mapping_id for item in documents):
                raise SemanticError("mapping_id is not a current review leaf")
            raise SemanticError("mapping_id was not found")
        review = create_mapping_review(
            predecessor, state, actor=actor, recorded_at=recorded_at,
        )
        live = evaluate_mapping(cfg, review)
        if state == "accepted" and live["stale"]:
            raise SemanticError(
                "refusing to accept a mapping whose semantic inputs are stale: "
                + "; ".join(item["detail"] for item in live["findings"])
            )
        path = _append_record_unlocked(
            root, review, id_re=MAPPING_ID_RE, prefix="mapping",
            document_limit=MAX_MAPPING_DOCUMENT_BYTES, file_limit=MAX_MAPPING_FILES,
            store_limit=MAX_MAPPING_STORE_BYTES, label="semantic_mapping",
            validator=validate_mapping_document, base=cfg.base,
        )
        return review, path


def _policy_id(core: dict) -> str:
    return f"semantic-policy:sha256:{canonical_sha256(core)}"


def validate_semantic_policy(document: object) -> None:
    """Validate one immutable explicit mapping-policy release."""
    if not isinstance(document, dict) or set(document) != {
            "id", "schema_version", "unicode_data_version", "recorded_at", "actor",
            "mapping_ids", "note"}:
        raise SemanticError("semantic policy has unknown or missing top-level fields")
    if document["schema_version"] != POLICY_SCHEMA:
        raise SemanticError(f"unsupported semantic policy schema: {document['schema_version']!r}")
    unicode_version = document["unicode_data_version"]
    if (not isinstance(unicode_version, str)
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", unicode_version)):
        raise SemanticError("semantic policy Unicode data version is invalid")
    historical = unicode_version != unicodedata.unidata_version
    _validate_time(document["recorded_at"])
    text_validator = _bounded_historical_text if historical else _bounded_text
    actor = text_validator(
        document["actor"], "semantic policy actor", 500, nonempty=True,
    )
    if not historical and actor != document["actor"]:
        raise SemanticError("semantic policy actor is not canonical")
    mapping_ids = document["mapping_ids"]
    if (not isinstance(mapping_ids, list) or not mapping_ids
            or len(mapping_ids) > MAX_POLICY_MAPPINGS
            or not all(isinstance(item, str) and MAPPING_ID_RE.fullmatch(item)
                       for item in mapping_ids)
            or mapping_ids != sorted(set(mapping_ids))):
        raise SemanticError("semantic policy mapping_ids must be a bounded sorted unique list")
    note = text_validator(
        document["note"], "semantic policy note", MAX_TEXT, nonempty=True,
    )
    if not historical and note != document["note"]:
        raise SemanticError("semantic policy note is not canonical")
    core = {key: document[key] for key in document if key != "id"}
    expected = _policy_id(core)
    if (not isinstance(document["id"], str) or not POLICY_ID_RE.fullmatch(document["id"])
            or document["id"] != expected):
        raise SemanticError("semantic policy id does not match canonical document content")


def _policy_findings(cfg, document: dict, documents: list[dict],
                     integrity_issues: list[dict], *, assets: dict | None = None) -> list[dict]:
    findings = []

    def add(code, detail):
        findings.append({"code": code, "severity": "error", "detail": detail})

    if document["unicode_data_version"] != unicodedata.unidata_version:
        add(
            "SEMANTIC_POLICY_UNICODE_PROFILE",
            "semantic policy was compiled under a different Unicode normalization profile",
        )
    if integrity_issues:
        for item in integrity_issues:
            add("SEMANTIC_POLICY_MAPPING_INTEGRITY",
                f"{item.get('path', '?')}: {item.get('detail', 'mapping-store integrity error')}")
        return findings
    by_id = {item["id"]: item for item in documents}
    leaf_ids = {item["id"] for item in current_mapping_leaves(documents)}
    conflicts = detect_mapping_conflicts(documents)
    selected_documents = [
        by_id[mapping_id] for mapping_id in document["mapping_ids"]
        if mapping_id in by_id
    ]
    selected_evaluations = {
        mapping["id"]: evaluation
        for mapping, evaluation in zip(
            selected_documents,
            (
                evaluate_mappings(cfg, selected_documents, conflicts=conflicts)
                if assets is None
                else _evaluate_mappings_from_assets(
                    selected_documents, assets, conflicts=conflicts,
                )
            ),
        )
    }
    subjects = {}
    for mapping_id in document["mapping_ids"]:
        mapping = by_id.get(mapping_id)
        if mapping is None:
            add("SEMANTIC_POLICY_MAPPING_MISSING", f"mapping is unavailable: {mapping_id}")
            continue
        if mapping_id not in leaf_ids:
            add("SEMANTIC_POLICY_MAPPING_NOT_CURRENT",
                f"mapping is not a current review leaf: {mapping_id}")
        if mapping["review"]["state"] != "accepted":
            add("SEMANTIC_POLICY_MAPPING_NOT_ACCEPTED",
                f"mapping is not accepted: {mapping_id}")
        evaluation = selected_evaluations[mapping_id]
        if not evaluation["eligible_for_policy"]:
            details = "; ".join(item["detail"] for item in evaluation["findings"])
            add("SEMANTIC_POLICY_MAPPING_INELIGIBLE",
                f"mapping is not currently eligible: {mapping_id}: {details or 'not accepted'}")
        subject_key = (
            mapping["subject"]["terminology_id"], mapping["subject"]["term_id"],
        )
        previous = subjects.get(subject_key)
        if previous is not None:
            add("SEMANTIC_POLICY_DUPLICATE_SUBJECT",
                f"policy selects multiple mappings for {subject_key[0]} / {subject_key[1]}: "
                f"{previous}, {mapping_id}")
        else:
            subjects[subject_key] = mapping_id
    return findings


def create_semantic_policy(cfg, mapping_ids: list[str], *, actor: str, note: str,
                           recorded_at: str | None = None) -> dict:
    """Compile exactly the caller-specified accepted, current, non-stale leaves.

    No accepted mapping is discovered or added implicitly.  A policy release is
    valid independently of activation; ``cfg.semantic_active_policy`` is the only
    activation selector.
    """
    if (not isinstance(mapping_ids, list) or not mapping_ids
            or len(mapping_ids) > MAX_POLICY_MAPPINGS
            or not all(isinstance(item, str) and MAPPING_ID_RE.fullmatch(item)
                       for item in mapping_ids)):
        raise SemanticError("mapping_ids must be a bounded non-empty mapping-id list")
    if len(mapping_ids) != len(set(mapping_ids)):
        raise SemanticError("mapping_ids must not contain duplicates")
    mapping_ids = sorted(mapping_ids)
    actor = _bounded_text(actor, "semantic policy actor", 500, nonempty=True)
    note = _bounded_text(note, "semantic policy note", MAX_TEXT, nonempty=True)
    timestamp = _validate_time(recorded_at or _utc_now())
    documents, issues = load_mappings(cfg)
    provisional = {
        "id": "semantic-policy:sha256:" + "0" * 64,
        "schema_version": POLICY_SCHEMA,
        "unicode_data_version": unicodedata.unidata_version,
        "recorded_at": timestamp,
        "actor": actor,
        "mapping_ids": mapping_ids,
        "note": note,
    }
    findings = _policy_findings(cfg, provisional, documents, issues)
    if findings:
        raise SemanticError(
            "cannot compile semantic policy: "
            + "; ".join(item["detail"] for item in findings)
        )
    core = {key: value for key, value in provisional.items() if key != "id"}
    return {"id": _policy_id(core), **core}


def load_policies(cfg) -> tuple[list[dict], list[dict]]:
    """Load content-addressed semantic policies plus integrity issues."""
    root = policies_path(cfg)
    paths, issues = _enumerate_store(
        root, "semantic_policy", max_files=MAX_POLICY_FILES,
        max_bytes=MAX_POLICY_STORE_BYTES, base=cfg.base,
    )
    if issues:
        return [], issues
    initial_signature = _store_directory_signature(paths)
    documents = []
    stable_bytes = 0
    for path in paths:
        if _is_link_like(path) or not path.is_file() or path.suffix != ".json":
            issues.append({
                "code": "SEMANTIC_POLICY_INTEGRITY", "path": path.name,
                "detail": "unexpected or non-regular semantic-policy store entry",
            })
            continue
        try:
            data = _safe_file_bytes(path, "semantic policy document", MAX_POLICY_DOCUMENT_BYTES)
            stable_bytes += len(data)
            if stable_bytes > MAX_POLICY_STORE_BYTES:
                raise SemanticError("semantic-policy store exceeds its aggregate-byte limit")
            document = strict_json_loads(data.decode("utf-8-sig"), path.name)
            validate_semantic_policy(document)
            match = POLICY_ID_RE.fullmatch(document["id"])
            if match is None or path.name != match.group(1) + ".json":
                raise SemanticError("semantic policy filename does not match its content id")
            documents.append(document)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RecursionError,
                SemanticError) as exc:
            issues.append({
                "code": "SEMANTIC_POLICY_INTEGRITY", "path": path.name,
                "detail": str(exc),
            })
    _finish_store_read(
        root, "semantic_policy", paths, initial_signature,
        max_files=MAX_POLICY_FILES, max_bytes=MAX_POLICY_STORE_BYTES,
        issues=issues, base=cfg.base,
    )
    return sorted(documents, key=lambda item: (item["recorded_at"], item["id"])), issues


def append_semantic_policy(cfg, document: dict) -> Path:
    """Atomically append a policy after rechecking all selected live mappings."""
    validate_semantic_policy(document)
    root = policies_path(cfg)
    mapping_root = mappings_path(cfg)
    # Mapping review appenders acquire only the mapping lock.  Taking that lock
    # first, then the policy lock, makes the eligibility check and policy append
    # one cross-store critical section; no accepted leaf can become non-current
    # between validation and publication.
    with _store_lock(mapping_root, _PROCESS_MAPPING_LOCK, "semantic mapping"):
        with _store_lock(root, _PROCESS_POLICY_LOCK, "semantic policy"):
            policies, policy_issues = load_policies(cfg)
            if policy_issues:
                raise SemanticError(
                    "semantic-policy store integrity failed before append: "
                    + "; ".join(f"{item['path']}: {item['detail']}" for item in policy_issues)
                )
            mappings, mapping_issues = load_mappings(cfg)
            findings = _policy_findings(cfg, document, mappings, mapping_issues)
            if findings:
                raise SemanticError(
                    "semantic policy no longer matches eligible live mappings: "
                    + "; ".join(item["detail"] for item in findings)
                )
            return _append_record_unlocked(
                root, document, id_re=POLICY_ID_RE, prefix="semantic-policy",
                document_limit=MAX_POLICY_DOCUMENT_BYTES, file_limit=MAX_POLICY_FILES,
                store_limit=MAX_POLICY_STORE_BYTES, label="semantic_policy",
                validator=validate_semantic_policy, base=cfg.base,
            )


def _active_policy_reference(cfg) -> object:
    return getattr(cfg, "semantic_active_policy", None)


def _load_active_reference(cfg, policies: list[dict]) -> dict | None:
    reference = _active_policy_reference(cfg)
    if reference in (None, ""):
        return None
    if not isinstance(reference, str) or not POLICY_ID_RE.fullmatch(reference):
        raise SemanticError("semantic_active_policy must be an exact policy id or null")
    document = next((item for item in policies if item["id"] == reference), None)
    if document is None:
        raise SemanticError(f"configured active semantic policy is unavailable: {reference}")
    return document


def load_active_semantic_policy(cfg) -> dict | None:
    """Resolve the explicit configured activation selector to a stored policy."""
    root = policies_path(cfg)
    with _store_lock(root, _PROCESS_POLICY_LOCK, "semantic policy"):
        policies, issues = load_policies(cfg)
        if issues:
            raise SemanticError(
                "semantic-policy store integrity failed: "
                + "; ".join(f"{item['path']}: {item['detail']}" for item in issues)
            )
        return _load_active_reference(cfg, policies)


def _evaluate_semantic_policy_from_stores(cfg, document: dict, policies: list[dict],
                                          policy_issues: list[dict], mappings: list[dict],
                                          mapping_issues: list[dict], *,
                                          assets: dict | None = None) -> dict:
    findings = [{
        "code": "SEMANTIC_POLICY_INTEGRITY", "severity": "error",
        "detail": f"{item['path']}: {item['detail']}",
    } for item in policy_issues]
    stored_policy = next(
        (item for item in policies if item["id"] == document["id"]), None,
    )
    if stored_policy is None or stored_policy != document:
        findings.append({
            "code": "SEMANTIC_POLICY_UNAVAILABLE", "severity": "error",
            "detail": f"semantic policy is not present in the current store: {document['id']}",
        })
    findings.extend(_policy_findings(
        cfg, document, mappings, mapping_issues, assets=assets,
    ))
    selected = False
    try:
        active = _load_active_reference(cfg, policies)
        selected = active is not None and active["id"] == document["id"]
    except SemanticError as exc:
        findings.append({
            "code": "SEMANTIC_POLICY_ACTIVATION_INTEGRITY", "severity": "error",
            "detail": str(exc),
        })
    valid = not any(item["severity"] == "error" for item in findings)
    return {
        "policy_id": document["id"],
        "selected": selected,
        "valid": valid,
        "active": selected and valid,
        "mapping_ids": list(document["mapping_ids"]),
        "active_mapping_ids": list(document["mapping_ids"]) if selected and valid else [],
        "findings": findings,
    }


def _evaluate_semantic_policy_from_snapshot(
        cfg, document: dict, *, assets: dict, policies: list[dict],
        policy_issues: list[dict], mappings: list[dict], mapping_issues: list[dict]) -> dict:
    """Evaluate one release solely from one caller-owned asset/store snapshot."""
    validate_semantic_policy(document)
    return _evaluate_semantic_policy_from_stores(
        cfg, document, policies, policy_issues, mappings, mapping_issues,
        assets=assets,
    )


def _evaluate_active_semantic_policy_from_snapshot(
        cfg, *, assets: dict, policies: list[dict], policy_issues: list[dict],
        mappings: list[dict], mapping_issues: list[dict]) -> dict:
    """Project activation from one coherent caller-owned semantic snapshot."""
    reference = _active_policy_reference(cfg)
    if reference in (None, ""):
        return {
            "configured": False, "configured_policy_id": None,
            "policy": None, "evaluation": None, "findings": [],
        }
    try:
        if policy_issues:
            raise SemanticError(
                "semantic-policy store integrity failed: "
                + "; ".join(
                    f"{item['path']}: {item['detail']}" for item in policy_issues
                )
            )
        policy = _load_active_reference(cfg, policies)
        assert policy is not None
        evaluation = _evaluate_semantic_policy_from_stores(
            cfg, policy, policies, policy_issues, mappings, mapping_issues,
            assets=assets,
        )
        return {
            "configured": True,
            "configured_policy_id": reference,
            "policy": policy,
            "evaluation": evaluation,
            "findings": list(evaluation["findings"]),
        }
    except SemanticError as exc:
        finding = {
            "code": "SEMANTIC_POLICY_ACTIVATION_INTEGRITY", "severity": "error",
            "detail": str(exc),
        }
        return {
            "configured": True, "configured_policy_id": reference,
            "policy": None, "evaluation": None, "findings": [finding],
        }


def evaluate_semantic_policy(cfg, document: dict) -> dict:
    """Evaluate one release against one locked mapping/policy store state."""
    validate_semantic_policy(document)
    mapping_root = mappings_path(cfg)
    policy_root = policies_path(cfg)
    with _store_lock(mapping_root, _PROCESS_MAPPING_LOCK, "semantic mapping"):
        with _store_lock(policy_root, _PROCESS_POLICY_LOCK, "semantic policy"):
            policies, policy_issues = load_policies(cfg)
            mappings, mapping_issues = load_mappings(cfg)
            return _evaluate_semantic_policy_from_stores(
                cfg, document, policies, policy_issues, mappings, mapping_issues,
            )


def evaluate_active_semantic_policy(cfg) -> dict:
    """Return a fail-closed projection for the configured active policy, if any."""
    reference = _active_policy_reference(cfg)
    if reference in (None, ""):
        return {
            "configured": False, "configured_policy_id": None,
            "policy": None, "evaluation": None,
            "findings": [],
        }
    mapping_root = mappings_path(cfg)
    policy_root = policies_path(cfg)
    try:
        with _store_lock(mapping_root, _PROCESS_MAPPING_LOCK, "semantic mapping"):
            with _store_lock(policy_root, _PROCESS_POLICY_LOCK, "semantic policy"):
                policies, policy_issues = load_policies(cfg)
                if policy_issues:
                    raise SemanticError(
                        "semantic-policy store integrity failed: "
                        + "; ".join(
                            f"{item['path']}: {item['detail']}" for item in policy_issues
                        )
                    )
                policy = _load_active_reference(cfg, policies)
                assert policy is not None
                mappings, mapping_issues = load_mappings(cfg)
                evaluation = _evaluate_semantic_policy_from_stores(
                    cfg, policy, policies, policy_issues, mappings, mapping_issues,
                )
                return {
                    "configured": True,
                    "configured_policy_id": reference,
                    "policy": policy,
                    "evaluation": evaluation,
                    "findings": list(evaluation["findings"]),
                }
    except SemanticError as exc:
        finding = {
            "code": "SEMANTIC_POLICY_ACTIVATION_INTEGRITY", "severity": "error",
            "detail": str(exc),
        }
        return {
            "configured": True, "configured_policy_id": reference,
            "policy": None, "evaluation": None,
            "findings": [finding],
        }


__all__ = [
    "LOCAL_TERMINOLOGY_SCHEMA", "ONTOLOGY_INDEX_SCHEMA", "ONTOLOGY_LOCK_SCHEMA",
    "CANDIDATE_SET_SCHEMA", "MAPPING_SCHEMA", "POLICY_SCHEMA", "MAPPING_RELATIONS",
    "SemanticError", "canonical_bytes", "canonical_sha256",
    "stable_semantic_file_bytes", "fsync_semantic_directory",
    "load_local_terminology", "load_ontology_index", "create_ontology_lock",
    "load_ontology_bundle", "load_ontology_lock", "configured_semantic_assets",
    "normalize_candidate_query", "validate_candidate_set", "search_ontology_candidates",
    "validate_mapping_agent_input", "create_mapping_proposal",
    "validate_mapping_document", "create_mapping_review", "mappings_path",
    "append_mapping", "load_mappings", "current_mapping_leaves",
    "detect_mapping_conflicts", "evaluate_mapping", "evaluate_mappings",
    "append_mapping_review",
    "validate_semantic_policy", "create_semantic_policy", "policies_path",
    "append_semantic_policy", "load_policies", "load_active_semantic_policy",
    "evaluate_semantic_policy", "evaluate_active_semantic_policy",
]
