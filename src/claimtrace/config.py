"""Project configuration discovery for claimtrace.

A project is any directory tree containing a `claimtrace.config.json`. claimtrace walks up from the
current directory (like git) to find it, so commands work from anywhere inside the project.

claimtrace.config.json schema (all paths relative to the config file's directory):
  {
    "root":         ".",                     # project root; node `path`s are relative to THIS
    "graph":        "claimtrace/graph.json", # the graph file
    "verifiers":    "claimtrace/verifiers.py", # optional: project-specific numeric checks
    "render_types": ["figure"],              # node types whose staleness is checked
    "input_types":  ["data", "artifact", "code"] # types that count as staleness INPUTS to a render
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
            data = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as e:
            raise SystemExit(f"claimtrace: {self.config_path.name} is not valid JSON - {e}")
        self.data = data
        self.root = (self.base / data.get("root", ".")).resolve()
        self.graph_path = (self.base / data.get("graph", "claimtrace/graph.json")).resolve()
        v = data.get("verifiers")
        self.verifiers = (self.base / v).resolve() if v else None
        # node types whose render-staleness (mtime / content hash) is checked
        self.render_types = set(data.get("render_types", DEFAULT_RENDER_TYPES))
        # node types that count as an input when deciding whether a render is stale
        self.input_types = set(data.get("input_types", DEFAULT_INPUT_TYPES))

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
