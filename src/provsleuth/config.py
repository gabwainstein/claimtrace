"""Project configuration discovery for ProvSleuth.

A project is any directory tree containing a `provsleuth.config.json`. ProvSleuth walks up from the
current directory (like git) to find it, so commands work from anywhere inside the project. Legacy
`claimtrace.config.json` projects remain readable in place and retain their legacy default paths.

provsleuth.config.json schema (all paths relative to the config file's directory):
  {
    "root":         ".",                     # project root; node `path`s are relative to THIS
    "graph":        "provsleuth/graph.json", # the graph file
    "events":       "provsleuth/events",     # content-addressed mechanical run receipts
    "assessments":  "provsleuth/assessments", # content-addressed semantic reviews
    "deliberation": {                         # optional adversarial semantic proposals
      "records": "provsleuth/deliberations"
    },
    "verifiers":    "provsleuth/verifiers.py", # optional: project-specific numeric checks
    "render_types": ["figure"],              # node types whose staleness is checked
    "input_types":  ["data", "artifact", "code"], # types that count as staleness INPUTS to a render
    "run_output_types": ["artifact"],        # non-render node types expected to have run receipts
    "require_assessments": false,             # strict-block unreviewed result-to-claim links
    "logic": {                                # optional declarative symbolic-claim extension
      "derivations": "provsleuth/derivations",
      "vocabularies": ["provsleuth/logic/vocabulary.json"],
      "rule_packs": ["provsleuth/logic/rules.json"],
      "allow_external_packs": false,
      "require_derivations": false,
      "max_provenance_bytes": 68719476736
    },
    "semantics": {                            # optional reviewed terminology normalization
      "terminologies": ["provsleuth/semantics/local-terms.json"],
      "ontology_locks": ["provsleuth/semantics/example.lock.json"],
      "mappings": "provsleuth/semantics/mappings",
      "policies": "provsleuth/semantics/policies",
      "active_policy": null,
      "allow_external_sources": false,
      "require_active_policy": false,
      "language": "en",
      "max_candidates": 25,
      "max_ontology_bytes": 536870912
    }
  }
Canonical concepts live inside graph.json under "concepts" (so `impact` can read them).
"""
from __future__ import annotations
import json
import os
import re
import stat
from pathlib import Path

CONFIG_NAME = "provsleuth.config.json"
LEGACY_CONFIG_NAME = "claimtrace.config.json"
CONFIG_NAMES = (CONFIG_NAME, LEGACY_CONFIG_NAME)
SEMANTIC_POLICY_ID_RE = re.compile(r"^semantic-policy:sha256:[0-9a-f]{64}$")
SEMANTIC_LANGUAGE_RE = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
MAX_SEMANTIC_ONTOLOGY_BYTES = 256 * 1024 * 1024 * 1024
MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_JSON_NESTING_DEPTH = 256

DEFAULT_RENDER_TYPES = ["figure"]
DEFAULT_INPUT_TYPES = ["data", "artifact", "code"]
DEFAULT_RUN_OUTPUT_TYPES = ["artifact"]


def _safe_config_display(value: object) -> str:
    """Escape terminal controls and invalid Unicode in config diagnostics."""
    return "".join(
        f"\\u{ord(char):04x}"
        if (ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F
            or 0xD800 <= ord(char) <= 0xDFFF) else char
        for char in str(value)
    )


def _config_stat_identity(value, *, include_ctime: bool = True) -> tuple:
    identity = (
        value.st_dev, value.st_ino, value.st_mode, value.st_size,
        getattr(value, "st_mtime_ns", int(value.st_mtime * 1e9)),
    )
    if include_ctime:
        identity += (getattr(value, "st_ctime_ns", int(value.st_ctime * 1e9)),)
    return identity


def _check_json_nesting_depth(text: str) -> None:
    """Reject excessive structural nesting without depending on the JSON decoder stack."""
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_NESTING_DEPTH:
                raise ValueError(
                    "JSON nesting exceeds the "
                    f"{MAX_JSON_NESTING_DEPTH}-level limit"
                )
        elif char in "]}" and depth:
            depth -= 1


def strict_json_loads(text: str, source: str = "JSON"):
    """Parse bounded standards-compliant JSON with strict scalar and key handling."""
    _check_json_nesting_depth(text)

    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError(f"{source}: duplicate key {key!r}")
            out[key] = value
        return out

    def invalid_constant(value):
        raise ValueError(f"{source}: non-finite number {value}")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)


def _read_config_text(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("config is not a regular file")
        if before.st_size > MAX_CONFIG_BYTES:
            raise ValueError(f"config exceeds the {MAX_CONFIG_BYTES}-byte limit")
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, MAX_CONFIG_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_CONFIG_BYTES:
                raise ValueError(f"config exceeds the {MAX_CONFIG_BYTES}-byte limit")
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if (total != before.st_size
                or _config_stat_identity(before) != _config_stat_identity(after)
                or _config_stat_identity(
                    before, include_ctime=os.name != "nt",
                ) != _config_stat_identity(
                    current, include_ctime=os.name != "nt",
                )):
            raise ValueError("config changed while it was being read")
        return b"".join(chunks).decode("utf-8-sig")
    finally:
        os.close(descriptor)


def find_config(start: str | os.PathLike | None = None) -> Path | None:
    p = Path(start or os.getcwd()).resolve()
    for d in [p, *p.parents]:
        found = [d / name for name in CONFIG_NAMES if (d / name).exists()]
        if len(found) > 1:
            raise SystemExit(
                f"provsleuth: ambiguous project configuration in {d}: "
                f"both {CONFIG_NAME} and {LEGACY_CONFIG_NAME} exist; "
                "remove one or pass --config explicitly"
            )
        if found:
            return found[0]
    return None


class Config:
    def __init__(self, config_path: str | os.PathLike):
        self.config_path = Path(config_path).resolve()
        self.base = self.config_path.parent
        try:
            data = strict_json_loads(
                _read_config_text(self.config_path), self.config_path.name)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as e:
            raise SystemExit(f"provsleuth: {self.config_path.name} is not valid JSON - {e}")
        if not isinstance(data, dict):
            raise SystemExit(f"provsleuth: {self.config_path.name} must contain a JSON object")
        for key in ("root", "graph", "events", "assessments", "verifiers"):
            if key in data and data[key] is not None and not isinstance(data[key], str):
                raise SystemExit(f"provsleuth: config field {key!r} must be a string")
        for key in ("render_types", "input_types", "run_output_types"):
            if key in data and (not isinstance(data[key], list)
                                or not all(isinstance(item, str) and item for item in data[key])):
                raise SystemExit(f"provsleuth: config field {key!r} must be a list of strings")
        if "require_assessments" in data and not isinstance(data["require_assessments"], bool):
            raise SystemExit("provsleuth: config field 'require_assessments' must be a boolean")
        deliberation = data.get("deliberation", {})
        if not isinstance(deliberation, dict):
            raise SystemExit("provsleuth: config field 'deliberation' must be an object")
        unknown_deliberation = set(deliberation) - {"records"}
        if unknown_deliberation:
            raise SystemExit(
                "provsleuth: unknown deliberation config field(s): "
                + ", ".join(
                    _safe_config_display(item) for item in sorted(unknown_deliberation)
                )
            )
        if "records" in deliberation and (
                not isinstance(deliberation["records"], str)
                or not deliberation["records"]):
            raise SystemExit(
                "provsleuth: deliberation.records must be a non-empty string"
            )
        execution = data.get("execution", {})
        if not isinstance(execution, dict):
            raise SystemExit("provsleuth: config field 'execution' must be an object")
        allowed_execution = {
            "replays", "method_assessments", "require_contracts", "require_replay",
            "require_method_assessments", "require_stage_checkpoints", "replay_attempts",
        }
        unknown_execution = set(execution) - allowed_execution
        if unknown_execution:
            raise SystemExit(
                "provsleuth: unknown execution config field(s): "
                + ", ".join(_safe_config_display(item) for item in sorted(unknown_execution))
            )
        for key in ("replays", "method_assessments"):
            if key in execution and (
                    not isinstance(execution[key], str) or not execution[key]):
                raise SystemExit(f"provsleuth: execution.{key} must be a non-empty string")
        for key in (
            "require_contracts", "require_replay", "require_method_assessments",
            "require_stage_checkpoints",
        ):
            if key in execution and not isinstance(execution[key], bool):
                raise SystemExit(f"provsleuth: execution.{key} must be a boolean")
        if "replay_attempts" in execution and (
                not isinstance(execution["replay_attempts"], int)
                or isinstance(execution["replay_attempts"], bool)
                or not 2 <= execution["replay_attempts"] <= 10):
            raise SystemExit("provsleuth: execution.replay_attempts must be an integer from 2 to 10")
        logic = data.get("logic", {})
        if not isinstance(logic, dict):
            raise SystemExit("provsleuth: config field 'logic' must be an object")
        allowed_logic = {
            "derivations", "vocabularies", "rule_packs", "allow_external_packs",
            "require_derivations", "max_provenance_bytes",
        }
        unknown_logic = set(logic) - allowed_logic
        if unknown_logic:
            raise SystemExit(
                "provsleuth: unknown logic config field(s): "
                + ", ".join(_safe_config_display(item) for item in sorted(unknown_logic))
            )
        if ("derivations" in logic
                and (not isinstance(logic["derivations"], str) or not logic["derivations"])):
            raise SystemExit("provsleuth: logic.derivations must be a non-empty string")
        for key in ("vocabularies", "rule_packs"):
            if key in logic and (
                    not isinstance(logic[key], list)
                    or not all(isinstance(item, str) and item for item in logic[key])):
                raise SystemExit(f"provsleuth: logic.{key} must be a list of non-empty strings")
        for key in ("allow_external_packs", "require_derivations"):
            if key in logic and not isinstance(logic[key], bool):
                raise SystemExit(f"provsleuth: logic.{key} must be a boolean")
        if "max_provenance_bytes" in logic and (
                not isinstance(logic["max_provenance_bytes"], int)
                or isinstance(logic["max_provenance_bytes"], bool)
                or not 1 <= logic["max_provenance_bytes"] <= 2**63 - 1):
            raise SystemExit(
                "provsleuth: logic.max_provenance_bytes must be a positive 64-bit integer"
            )
        semantics = data.get("semantics", {})
        if not isinstance(semantics, dict):
            raise SystemExit("provsleuth: config field 'semantics' must be an object")
        allowed_semantics = {
            "terminologies", "ontology_locks", "mappings", "policies", "active_policy",
            "allow_external_sources", "require_active_policy", "language", "max_candidates",
            "max_ontology_bytes",
        }
        unknown_semantics = set(semantics) - allowed_semantics
        if unknown_semantics:
            raise SystemExit(
                "provsleuth: unknown semantics config field(s): "
                + ", ".join(
                    _safe_config_display(item) for item in sorted(unknown_semantics)
                )
            )
        for key in ("terminologies", "ontology_locks"):
            if key in semantics and (
                    not isinstance(semantics[key], list)
                    or not all(isinstance(item, str) and item for item in semantics[key])):
                raise SystemExit(
                    f"provsleuth: semantics.{key} must be a list of non-empty strings"
                )
        if len(semantics.get("terminologies", [])) > 1_000:
            raise SystemExit(
                "provsleuth: semantics.terminologies exceeds the 1000-path limit"
            )
        if len(semantics.get("ontology_locks", [])) > 256:
            raise SystemExit(
                "provsleuth: semantics.ontology_locks exceeds the 256-path limit"
            )
        for key in ("mappings", "policies", "language"):
            if key in semantics and (
                    not isinstance(semantics[key], str) or not semantics[key]):
                raise SystemExit(f"provsleuth: semantics.{key} must be a non-empty string")
        active_policy = semantics.get("active_policy")
        if active_policy is not None and (
                not isinstance(active_policy, str)
                or not SEMANTIC_POLICY_ID_RE.fullmatch(active_policy)):
            raise SystemExit(
                "provsleuth: semantics.active_policy must be null or an exact "
                "semantic-policy:sha256 ID"
            )
        for key in ("allow_external_sources", "require_active_policy"):
            if key in semantics and not isinstance(semantics[key], bool):
                raise SystemExit(f"provsleuth: semantics.{key} must be a boolean")
        if "max_candidates" in semantics and (
                not isinstance(semantics["max_candidates"], int)
                or isinstance(semantics["max_candidates"], bool)
                or not 1 <= semantics["max_candidates"] <= 1000):
            raise SystemExit(
                "provsleuth: semantics.max_candidates must be an integer from 1 to 1000"
            )
        if "max_ontology_bytes" in semantics and (
                not isinstance(semantics["max_ontology_bytes"], int)
                or isinstance(semantics["max_ontology_bytes"], bool)
                or not 1 <= semantics["max_ontology_bytes"] <= MAX_SEMANTIC_ONTOLOGY_BYTES):
            raise SystemExit(
                "provsleuth: semantics.max_ontology_bytes must be a positive integer no "
                f"larger than {MAX_SEMANTIC_ONTOLOGY_BYTES}"
            )
        if "language" in semantics and not SEMANTIC_LANGUAGE_RE.fullmatch(
                semantics["language"]):
            raise SystemExit(
                "provsleuth: semantics.language must be a simple BCP-47-style language tag"
            )
        self.data = data
        self.legacy_layout = self.config_path.name == LEGACY_CONFIG_NAME
        self.store_prefix = "claimtrace" if self.legacy_layout else "provsleuth"
        self.root = (self.base / data.get("root", ".")).resolve()
        self.graph_path = (
            self.base / data.get("graph", f"{self.store_prefix}/graph.json")
        ).resolve()

        def lexical_path(value: str) -> Path:
            candidate = Path(value)
            supplied = candidate if candidate.is_absolute() else self.base / candidate
            return Path(os.path.abspath(str(supplied)))

        # Provenance-store paths retain their declared lexical path. Resolving
        # here would erase a symlink or junction before the store loader can
        # reject it.
        self.events_path = lexical_path(
            data.get("events", f"{self.store_prefix}/events")
        )
        self.assessments_path = lexical_path(
            data.get("assessments", f"{self.store_prefix}/assessments")
        )
        self.deliberation = deliberation

        def project_store_path(value: str, label: str) -> Path:
            resolved = lexical_path(value)
            try:
                resolved.relative_to(self.base)
            except ValueError as exc:
                raise SystemExit(
                    f"provsleuth: {label} path escapes the project config directory: {value}"
                ) from exc
            return resolved

        self.deliberation_path = project_store_path(
            deliberation.get("records", f"{self.store_prefix}/deliberations"),
            "deliberation.records",
        )
        v = data.get("verifiers")
        self.verifiers = (self.base / v).resolve() if v else None
        # node types whose render-staleness (mtime / content hash) is checked
        self.render_types = set(data.get("render_types", DEFAULT_RENDER_TYPES))
        # node types that count as an input when deciding whether a render is stale
        self.input_types = set(data.get("input_types", DEFAULT_INPUT_TYPES))
        # materialized node types expected to have run receipts under strict checking
        self.run_output_types = set(data.get("run_output_types", DEFAULT_RUN_OUTPUT_TYPES))
        self.require_assessments = bool(data.get("require_assessments", False))
        self.execution = execution
        self.require_execution_contracts = bool(execution.get("require_contracts", False))
        self.require_replay = bool(execution.get("require_replay", False))
        self.require_method_assessments = bool(
            execution.get("require_method_assessments", False)
        )
        self.require_stage_checkpoints = bool(
            execution.get("require_stage_checkpoints", False)
        )
        self.replay_attempts = execution.get("replay_attempts", 2)

        def execution_path(value: str) -> Path:
            resolved = lexical_path(value)
            try:
                resolved.relative_to(self.base)
            except ValueError as exc:
                raise SystemExit(
                    f"provsleuth: execution path escapes the project config directory: {value}"
                ) from exc
            return resolved

        self.replays_path = execution_path(
            execution.get("replays", f"{self.store_prefix}/replays")
        )
        self.method_assessments_path = execution_path(
            execution.get(
                "method_assessments", f"{self.store_prefix}/method-assessments"
            )
        )
        self.logic = logic

        def logic_path(value: str, *, external_allowed: bool) -> Path:
            candidate = Path(value)
            resolved = (
                candidate.resolve() if candidate.is_absolute()
                else (self.base / candidate).resolve()
            )
            if not external_allowed:
                try:
                    resolved.relative_to(self.base)
                except ValueError as exc:
                    raise SystemExit(
                        f"provsleuth: logic path escapes the project config directory: {value}"
                    ) from exc
            return resolved

        derivations_value = logic.get(
            "derivations", f"{self.store_prefix}/derivations"
        )
        derivations_candidate = Path(derivations_value)
        self.derivations_declared_path = (
            derivations_candidate.absolute()
            if derivations_candidate.is_absolute()
            else (self.base / derivations_candidate).absolute()
        )
        self.derivations_path = logic_path(derivations_value, external_allowed=False)
        allow_external_packs = bool(logic.get("allow_external_packs", False))
        self.logic_vocabulary_paths = [
            logic_path(item, external_allowed=allow_external_packs)
            for item in logic.get("vocabularies", [])
        ]
        self.logic_rule_pack_paths = [
            logic_path(item, external_allowed=allow_external_packs)
            for item in logic.get("rule_packs", [])
        ]
        for label, paths in (
                ("vocabularies", self.logic_vocabulary_paths),
                ("rule_packs", self.logic_rule_pack_paths)):
            identities = [os.path.normcase(str(path)) for path in paths]
            if len(identities) != len(set(identities)):
                raise SystemExit(f"provsleuth: logic.{label} contains duplicate paths")
        self.allow_external_logic_packs = allow_external_packs
        self.require_derivations = bool(logic.get("require_derivations", False))
        self.logic_max_provenance_bytes = logic.get(
            "max_provenance_bytes", 64 * 1024 * 1024 * 1024,
        )
        self.semantics = semantics

        def semantic_path(value: str, *, external_allowed: bool) -> Path:
            if (len(value) > 4_096
                    or any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F
                           or 0xD800 <= ord(char) <= 0xDFFF
                           for char in value)):
                raise SystemExit(
                    "provsleuth: semantics paths must be bounded text without controls or "
                    "lone Unicode surrogates"
                )
            try:
                candidate = Path(value)
                resolved = (
                    candidate.resolve() if candidate.is_absolute()
                    else (self.base / candidate).resolve()
                )
            except (OSError, RuntimeError, ValueError) as exc:
                raise SystemExit(f"provsleuth: invalid semantics path: {value!r}") from exc
            if not external_allowed:
                try:
                    resolved.relative_to(self.base)
                except ValueError as exc:
                    raise SystemExit(
                        "provsleuth: semantics path escapes the project config directory: "
                        f"{value}"
                    ) from exc
            return resolved

        allow_external_semantics = bool(semantics.get("allow_external_sources", False))
        self.semantic_terminology_paths = [
            semantic_path(item, external_allowed=allow_external_semantics)
            for item in semantics.get("terminologies", [])
        ]
        self.semantic_ontology_lock_paths = [
            semantic_path(item, external_allowed=allow_external_semantics)
            for item in semantics.get("ontology_locks", [])
        ]
        for label, paths in (
                ("terminologies", self.semantic_terminology_paths),
                ("ontology_locks", self.semantic_ontology_lock_paths)):
            identities = [os.path.normcase(str(path)) for path in paths]
            if len(identities) != len(set(identities)):
                raise SystemExit(f"provsleuth: semantics.{label} contains duplicate paths")
        mappings_value = semantics.get(
            "mappings", f"{self.store_prefix}/semantics/mappings"
        )
        mappings_candidate = Path(mappings_value)
        self.semantic_mappings_declared_path = (
            mappings_candidate.absolute()
            if mappings_candidate.is_absolute()
            else (self.base / mappings_candidate).absolute()
        )
        self.semantic_mappings_path = semantic_path(
            mappings_value, external_allowed=False,
        )
        policies_value = semantics.get(
            "policies", f"{self.store_prefix}/semantics/policies"
        )
        policies_candidate = Path(policies_value)
        self.semantic_policies_declared_path = (
            policies_candidate.absolute()
            if policies_candidate.is_absolute()
            else (self.base / policies_candidate).absolute()
        )
        self.semantic_policies_path = semantic_path(
            policies_value, external_allowed=False,
        )
        if os.path.normcase(str(self.semantic_mappings_path)) == os.path.normcase(
                str(self.semantic_policies_path)):
            raise SystemExit("provsleuth: semantics.mappings and semantics.policies must differ")
        self.semantic_active_policy = active_policy
        self.allow_external_semantic_sources = allow_external_semantics
        self.require_active_semantic_policy = bool(
            semantics.get("require_active_policy", False)
        )
        self.semantic_language = semantics.get("language", "en").lower()
        self.semantic_max_candidates = semantics.get("max_candidates", 25)
        self.semantic_max_ontology_bytes = semantics.get(
            "max_ontology_bytes", 512 * 1024 * 1024,
        )

        protected_files = {
            os.path.normcase(str(path))
            for path in (
                self.config_path, self.graph_path,
                *([] if self.verifiers is None else [self.verifiers]),
                *self.logic_vocabulary_paths, *self.logic_rule_pack_paths,
            )
        }
        semantic_sources = [
            *self.semantic_terminology_paths, *self.semantic_ontology_lock_paths,
        ]
        for source in semantic_sources:
            if os.path.normcase(str(source)) in protected_files:
                raise SystemExit(
                    "provsleuth: semantic source path overlaps a protected config, graph, "
                    "verifier, or logic asset"
                )
        provenance_stores = [
            self.events_path, self.assessments_path, self.deliberation_path,
            self.derivations_path,
            self.semantic_mappings_path, self.semantic_policies_path,
            self.replays_path, self.method_assessments_path,
        ]
        for index, store in enumerate(provenance_stores):
            for other in provenance_stores[index + 1:]:
                try:
                    nested = store == other or store.is_relative_to(other) or other.is_relative_to(store)
                except AttributeError:  # Python 3.9
                    nested = store == other
                    for child, parent in ((store, other), (other, store)):
                        try:
                            child.relative_to(parent)
                            nested = True
                        except ValueError:
                            pass
                if nested:
                    raise SystemExit(
                        "provsleuth: event, assessment, deliberation, derivation, "
                        "semantic-mapping, semantic-policy, replay, and method-assessment "
                        "stores must be distinct "
                        "non-nested directories"
                    )
        for source in semantic_sources:
            for store in provenance_stores:
                try:
                    inside = source.is_relative_to(store)
                except AttributeError:  # Python 3.9
                    try:
                        source.relative_to(store)
                        inside = True
                    except ValueError:
                        inside = False
                if inside:
                    raise SystemExit(
                        "provsleuth: semantic source path must not be inside a provenance store"
                    )

    def resolve(self, relpath: str) -> Path:
        """Resolve a node path (relative to project root) to an absolute path.

        Node paths are graph content, so they are clamped to the project root the
        same way configured store paths are.  Without this an absolute or
        ``..``-traversing node path would make the engine read, hash, and publish
        a file outside the project -- including into a signed release manifest.
        Normalization is lexical (``abspath``, not ``resolve``) so a symlink or
        junction still reaches the loader that rejects it.
        """
        rp = Path(relpath)
        if rp.is_absolute():
            raise SystemExit(
                f"provsleuth: node path must be project-relative, not absolute: {relpath}"
            )
        resolved = Path(os.path.abspath(str(self.root / rp)))
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise SystemExit(
                f"provsleuth: node path escapes the project root: {relpath}"
            ) from exc
        return resolved

    def within_root(self, relpath: str) -> bool:
        """Whether ``resolve`` would accept this node path, without raising.

        Reporting commands use this to record an out-of-root path as a finding
        instead of aborting the whole run; publishing paths keep the hard failure.
        """
        try:
            self.resolve(relpath)
        except SystemExit:
            return False
        return True


def load_config(start: str | os.PathLike | None = None,
                explicit: str | os.PathLike | None = None) -> Config:
    cp = Path(explicit).resolve() if explicit else find_config(start)
    if not cp or not Path(cp).exists():
        raise SystemExit(
            "provsleuth: no provsleuth.config.json or legacy claimtrace.config.json "
            "found in this directory or any parent.\n"
            "Run `provsleuth init` to scaffold one."
        )
    return Config(cp)
