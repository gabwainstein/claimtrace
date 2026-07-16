"""Lock each render's data state into a content-hash manifest.

For every render-type node (default `figure`) this collects the input files it transitively
depends on (types in cfg.input_types) and writes a versioned `<output>.manifest.json` recording
SHA-256 for the output and every input. Run it AFTER (re)producing the outputs to "lock" the
render<->data state. Thereafter
`claimtrace check` re-hashes the recorded inputs and raises STALE_DATA if any changed — a
content-hash staleness signal that survives mtime quirks (touch, checkout, copy).
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

from .engine import RENDER_MANIFEST_SCHEMA, _file_hash, expected_inputs, load_graph


def snapshot(cfg) -> int:
    nodes, edges, _ = load_graph(cfg)
    written = 0
    for nid, n in nodes.items():
        if n.get("type") not in cfg.render_types or not n.get("path"):
            continue
        out = cfg.resolve(n["path"])
        if not out.exists():
            print(f"  skip (no output yet): {nid}")
            continue
        inputs = [(path, fp) for path, fp in expected_inputs(cfg, nodes, edges, nid).items()
                  if fp.exists()]
        manifest = {
            "schema_version": RENDER_MANIFEST_SCHEMA,
            "node": nid,
            "output": n["path"],
            "output_sha256": _file_hash(out, "sha256"),
            "backbone": n.get("backbone"),
            "value": n.get("value", ""),
            "locked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "inputs": [
                {"path": rel, "sha256": _file_hash(fp, "sha256")}
                for rel, fp in sorted(inputs)
            ],
        }
        man = cfg.resolve(n["path"] + ".manifest.json")
        with man.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        written += 1
        print(f"  locked {nid}: {len(inputs)} input(s) -> {man.name}")
    print(f"[snapshot] wrote {written} manifest(s).")
    return 0
