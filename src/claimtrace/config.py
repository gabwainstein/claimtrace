"""Project configuration discovery for claimtrace.

A project is any directory tree containing a `claimtrace.config.json`. claimtrace walks up from the
current directory (like git) to find it, so commands work from anywhere inside the project.

claimtrace.config.json schema (all paths relative to the config file's directory):
  {
    "root":         ".",                     # project root; node `path`s are relative to THIS
    "graph":        "claimtrace/graph.json", # the graph file
    "events":       "claimtrace/events",     # content-addressed mechanical run receipts
    "assessments":  "claimtrace/assessments", # content-addressed semantic reviews
    "verifiers":    "claimtrace/verifiers.py", # optional: project-specific numeric checks
    "render_types": ["figure"],              # node types whose staleness is checked
    "input_types":  ["data", "artifact", "code"], # types that count as staleness INPUTS to a render
    "run_output_types": ["artifact"],        # non-render node types expected to have run receipts
    "require_assessments": false,             # strict-block unassessed supports/refutes links
    "logic": {                                # optional declarative symbolic-claim extension
      "derivations": "claimtrace/derivations",
      "vocabularies": ["claimtrace/logic/vocabulary.json"],
      "rule_packs": ["claimtrace/logic/rules.json"],
      "allow_external_packs": false,
      "require_derivations": false,
      "max_provenance_bytes": 68719476736
    }
  }
Canonical concepts live inside graph.json under "concepts" (so `impact` can read them).
"""
from __future__ import annotations
import json
import os
from pathlib import Path

CONFIG_NAME = "claimtrace.config.json"

DEFAULT_RENDER_TYPES = ["figure"]
DEFAULT_INPUT_TYPES = ["data", "artifact", "code"]
DEFAULT_RUN_OUTPUT_TYPES = ["artifact"]


def strict_json_loads(text: str, source: str = "JSON"):
    """Parse standards-compliant JSON, rejecting duplicate keys and non-finite numbers."""
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


def find_config(start: str | os.PathLike | None = None) -> Path | None:
    p = Path(start or os.getcwd()).resolve()
    for d in [p, *p.parents]:
        c = d / CONFIG_NAME
        if c.exists():
            return c
    return None


class Config:
    def __init__(self, config_path: str | os.PathLike):
        self.config_path = Path(config_path).resolve()
        self.base = self.config_path.parent
        try:
            data = strict_json_loads(
                self.config_path.read_text(encoding="utf-8-sig"), self.config_path.name)
        except (json.JSONDecodeError, ValueError) as e:
            raise SystemExit(f"claimtrace: {self.config_path.name} is not valid JSON - {e}")
        if not isinstance(data, dict):
            raise SystemExit(f"claimtrace: {self.config_path.name} must contain a JSON object")
        for key in ("root", "graph", "events", "assessments", "verifiers"):
            if key in data and data[key] is not None and not isinstance(data[key], str):
                raise SystemExit(f"claimtrace: config field {key!r} must be a string")
        for key in ("render_types", "input_types", "run_output_types"):
            if key in data and (not isinstance(data[key], list)
                                or not all(isinstance(item, str) and item for item in data[key])):
                raise SystemExit(f"claimtrace: config field {key!r} must be a list of strings")
        if "require_assessments" in data and not isinstance(data["require_assessments"], bool):
            raise SystemExit("claimtrace: config field 'require_assessments' must be a boolean")
        logic = data.get("logic", {})
        if not isinstance(logic, dict):
            raise SystemExit("claimtrace: config field 'logic' must be an object")
        allowed_logic = {
            "derivations", "vocabularies", "rule_packs", "allow_external_packs",
            "require_derivations", "max_provenance_bytes",
        }
        unknown_logic = set(logic) - allowed_logic
        if unknown_logic:
            raise SystemExit(
                "claimtrace: unknown logic config field(s): "
                + ", ".join(sorted(unknown_logic))
            )
        if ("derivations" in logic
                and (not isinstance(logic["derivations"], str) or not logic["derivations"])):
            raise SystemExit("claimtrace: logic.derivations must be a non-empty string")
        for key in ("vocabularies", "rule_packs"):
            if key in logic and (
                    not isinstance(logic[key], list)
                    or not all(isinstance(item, str) and item for item in logic[key])):
                raise SystemExit(f"claimtrace: logic.{key} must be a list of non-empty strings")
        for key in ("allow_external_packs", "require_derivations"):
            if key in logic and not isinstance(logic[key], bool):
                raise SystemExit(f"claimtrace: logic.{key} must be a boolean")
        if "max_provenance_bytes" in logic and (
                not isinstance(logic["max_provenance_bytes"], int)
                or isinstance(logic["max_provenance_bytes"], bool)
                or not 1 <= logic["max_provenance_bytes"] <= 2**63 - 1):
            raise SystemExit(
                "claimtrace: logic.max_provenance_bytes must be a positive 64-bit integer"
            )
        self.data = data
        self.root = (self.base / data.get("root", ".")).resolve()
        self.graph_path = (self.base / data.get("graph", "claimtrace/graph.json")).resolve()
        self.events_path = (self.base / data.get("events", "claimtrace/events")).resolve()
        self.assessments_path = (
            self.base / data.get("assessments", "claimtrace/assessments")
        ).resolve()
        v = data.get("verifiers")
        self.verifiers = (self.base / v).resolve() if v else None
        # node types whose render-staleness (mtime / content hash) is checked
        self.render_types = set(data.get("render_types", DEFAULT_RENDER_TYPES))
        # node types that count as an input when deciding whether a render is stale
        self.input_types = set(data.get("input_types", DEFAULT_INPUT_TYPES))
        # materialized node types expected to have run receipts under strict checking
        self.run_output_types = set(data.get("run_output_types", DEFAULT_RUN_OUTPUT_TYPES))
        self.require_assessments = bool(data.get("require_assessments", False))
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
                        f"claimtrace: logic path escapes the project config directory: {value}"
                    ) from exc
            return resolved

        derivations_value = logic.get("derivations", "claimtrace/derivations")
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
                raise SystemExit(f"claimtrace: logic.{label} contains duplicate paths")
        self.allow_external_logic_packs = allow_external_packs
        self.require_derivations = bool(logic.get("require_derivations", False))
        self.logic_max_provenance_bytes = logic.get(
            "max_provenance_bytes", 64 * 1024 * 1024 * 1024,
        )

    def resolve(self, relpath: str) -> Path:
        """Resolve a node path (relative to project root) to an absolute path."""
        rp = Path(relpath)
        return rp if rp.is_absolute() else (self.root / rp)


def load_config(start: str | os.PathLike | None = None,
                explicit: str | os.PathLike | None = None) -> Config:
    cp = Path(explicit).resolve() if explicit else find_config(start)
    if not cp or not Path(cp).exists():
        raise SystemExit(
            "claimtrace: no claimtrace.config.json found in this directory or any parent.\n"
            "Run `claimtrace init` to scaffold one."
        )
    return Config(cp)
