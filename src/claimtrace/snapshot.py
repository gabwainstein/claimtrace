"""Lock each render's data state into a content-hash manifest.

For every render-type node (default `figure`) this collects the input files it transitively
depends on (types in cfg.input_types) and writes `<output>.manifest.json` recording each input's
SHA1. Run it AFTER (re)producing the outputs to "lock" the render<->data state. Thereafter
`claimtrace check` re-hashes the recorded inputs and raises STALE_DATA if any changed — a
content-hash staleness signal that survives mtime quirks (touch, checkout, copy).
"""
from __future__ import annotations
import hashlib
import json
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from .engine import ANNOT_RELS, load_graph


def _sha1(fp: Path) -> str:
    h = hashlib.sha1()
    with open(fp, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def snapshot(cfg) -> int:
    nodes, edges, _ = load_graph(cfg)
    up = defaultdict(list)
    for e in edges:
        if not all(k in e for k in ("from", "to", "rel")):
            continue
        if e["rel"] == "reads":
            up[e["from"]].append(e["to"])      # code depends on artifact
        elif e["rel"] not in ANNOT_RELS:
            up[e["to"]].append(e["from"])      # to depends on from

    def ancestors(nid):
        seen, q = set(), deque([nid])
        while q:
            x = q.popleft()
            for u in up.get(x, []):
                if u not in seen:
                    seen.add(u); q.append(u)
        return seen

    written = 0
    for nid, n in nodes.items():
        if n.get("type") not in cfg.render_types or not n.get("path"):
            continue
        out = cfg.resolve(n["path"])
        if not out.exists():
            print(f"  skip (no output yet): {nid}")
            continue
        inputs = []
        for a in ancestors(nid):
            an = nodes.get(a, {})
            if an.get("type") in cfg.input_types and an.get("path"):
                fp = cfg.resolve(an["path"])
                if fp.exists():
                    inputs.append((an["path"], fp))
        manifest = {
            "node": nid,
            "output": n["path"],
            "output_sha1": _sha1(out),
            "backbone": n.get("backbone"),
            "value": n.get("value", ""),
            "locked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "inputs": [{"path": rel, "sha1": _sha1(fp)} for rel, fp in sorted(inputs)],
        }
        man = cfg.resolve(n["path"] + ".manifest.json")
        man.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written += 1
        print(f"  locked {nid}: {len(inputs)} input(s) -> {man.name}")
    print(f"[snapshot] wrote {written} manifest(s).")
    return 0
