"""claimtrace command-line interface."""
from __future__ import annotations
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from . import __version__
from .config import CONFIG_NAME, load_config
from .engine import (ANNOT_RELS, GraphError, compute_check, downstream, impact,
                     lint_issues, load_graph, log_entry, upstream)
from .snapshot import snapshot
from .verify import run_verifiers

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

GLYPH = {"current": "LIVE", "confirmed": "CONFIRMED", "null": "NULL", "dead_end": "DEAD-END",
         "retracted": "RETRACTED", "superseded": "SUPERSEDED", "stale": "STALE", "deprecated": "DEPRECATED"}
JOURNAL_ORDER = ["current", "confirmed", "null", "dead_end", "retracted", "superseded", "stale", "deprecated"]


def _cfg(args):
    return load_config(explicit=getattr(args, "config", None))


# --------------------------------------------------------------------------- commands

def cmd_check(args):
    cfg = _cfg(args)
    problems, pending = compute_check(cfg)
    if pending:
        print(f"PENDING MIGRATION — {len(pending)} node(s) flagged stale (run `claimtrace impact` for the full list):")
        for nid, typ, mv, path in pending:
            print(f"  [{typ:8s}] {nid:34s} {mv}{('  | ' + path) if path else ''}")
        print()
    if not problems:
        print("claimtrace check: OK — no silent drift, no broken structure, all paths exist."
              + (f" ({len(pending)} known-pending above.)" if pending else ""))
        return 0
    print(f"claimtrace check: {len(problems)} ERROR(s):\n")
    for kind, nid, detail in problems:
        print(f"  [{kind:24s}] {nid:30s} {detail}")
    return 1


def cmd_lint(args):
    cfg = _cfg(args)
    warnings = lint_issues(cfg)
    if not warnings:
        print("claimtrace lint: OK — vocabulary is standard and load-bearing nodes are annotated.")
        return 0
    print(f"claimtrace lint: {len(warnings)} warning(s):\n")
    for kind, nid, detail in warnings:
        print(f"  [{kind:18s}] {nid:30s} {detail}")
    if args.strict:
        print("\n(--strict) treating warnings as failure.")
        return 1
    return 0


def cmd_downstream(args):
    cfg = _cfg(args)
    res = downstream(cfg, args.node)
    if res is None:
        print(f"unknown node: {args.node}"); return 1
    print(f"DOWNSTREAM of {args.node} (depend on it):")
    for nb, r, n in res:
        print(f"  {nb:34s} [{n.get('type','?')}/{n.get('status','?')}]  via {r}")
    return 0


def cmd_upstream(args):
    cfg = _cfg(args)
    res = upstream(cfg, args.node)
    if res is None:
        print(f"unknown node: {args.node}"); return 1
    print(f"UPSTREAM of {args.node} (it depends on):")
    for nb, r, n in res:
        print(f"  {nb:34s} [{n.get('type','?')}/{n.get('status','?')}]  via {r}")
    return 0


def cmd_impact(args):
    cfg = _cfg(args)
    concept, _, value = args.set.partition("=")
    concept, value = concept.strip(), value.strip()
    items, info = impact(cfg, concept, value)
    if items is None:
        print(f"unknown concept: {concept} (known: {info})"); return 1
    print(f"IMPACT — set {concept} = {value}  (was canonical {info})\n")
    print(f"{len(items)} node(s) must update to {concept}={value}:\n")
    for nid, why, n in items:
        path = n.get("path", "")
        print(f"  [{n.get('type','?'):8s}] {nid:30s} {('— ' + n.get('value','')) if n.get('value') else ''}")
        print(f"             reason: {why}{('  | ' + path) if path else ''}")
    return 0


def cmd_node(args):
    cfg = _cfg(args)
    nodes, edges, _ = load_graph(cfg)
    n = nodes.get(args.node)
    if not n:
        print(f"unknown node: {args.node}"); return 1
    print(json.dumps(n, indent=2, ensure_ascii=False))
    print("\n  edges:")
    for e in edges:
        if e.get("from") == args.node or e.get("to") == args.node:
            print(f"    {e['from']} --{e['rel']}--> {e['to']}")
    return 0


def cmd_log(args):
    cfg = _cfg(args)
    entry = json.loads(Path(args.entry).read_text(encoding="utf-8"))
    ok, msg = log_entry(cfg, entry, update=args.update)
    print(msg)
    return 0 if ok else 1


def cmd_journal(args):
    cfg = _cfg(args)
    nodes, edges, _ = load_graph(cfg)
    annot = defaultdict(list)
    for e in edges:
        if e.get("rel") in ANNOT_RELS:
            annot[e["from"]].append((e["rel"] + " ->", e["to"]))
            annot[e["to"]].append(("<- " + e["rel"], e["from"]))
    groups = defaultdict(list)
    for nid, n in nodes.items():
        groups[n.get("status", "?")].append((nid, n))
    only = set(args.status.split(",")) if args.status else None
    print("LAB NOTEBOOK (claimtrace journal) — every attempt and where it ended up\n")
    for st in JOURNAL_ORDER + [s for s in groups if s not in JOURNAL_ORDER]:
        if only and st not in only:
            continue
        items = groups.get(st, [])
        if not items:
            continue
        print(f"== {GLYPH.get(st, st.upper())} ({len(items)}) ==")
        for nid, n in sorted(items, key=lambda kv: (kv[1].get("date", ""), kv[0])):
            d = n.get("date", ""); v = n.get("value") or n.get("verdict") or ""
            sc = n.get("script") or n.get("path") or ""
            print(f"  {nid:32s} {('[' + d + '] ') if d else ''}{v[:96]}")
            if sc:
                print(f"       . {sc}")
            for rel, tgt in annot.get(nid, []):
                print(f"       . {rel} {tgt}")
        print()
    return 0


def cmd_snapshot(args):
    return snapshot(_cfg(args))


def cmd_verify(args):
    return run_verifiers(_cfg(args))


def cmd_summary(args):
    cfg = _cfg(args)
    nodes, edges, concepts = load_graph(cfg)
    by_type, by_status = defaultdict(int), defaultdict(int)
    for n in nodes.values():
        by_type[n.get("type", "?")] += 1
        by_status[n.get("status", "?")] += 1
    print(f"claimtrace graph: {len(nodes)} nodes / {len(edges)} edges")
    print("  concepts:", ", ".join(f"{k}={v.get('canonical')}" for k, v in concepts.items()) or "(none)")
    print("  by type:  ", ", ".join(f"{k}:{v}" for k, v in sorted(by_type.items())))
    print("  by status:", ", ".join(f"{k}:{v}" for k, v in sorted(by_status.items())))
    return 0


SKELETON_GRAPH = {
    "schema_version": "1.0",
    "concepts": {"dataset_version": {"canonical": "v1", "note": "bump this when the dataset is re-cut"}},
    "nodes": [
        {"id": "data:raw", "type": "data", "status": "current", "backbone": "v1",
         "path": "data/raw.csv", "value": "raw measurements"}
    ],
    "edges": [],
}
SKELETON_VERIFIERS = '''"""Project-specific numeric checks. Run with `claimtrace verify`."""
from claimtrace import check, approx


@check("example: row count")
def row_count():
    # paths are relative to the project root
    import csv
    with open("data/raw.csv") as f:
        n = sum(1 for _ in csv.reader(f)) - 1
    return n > 0, f"{n} rows", "> 0 rows"
'''


def cmd_init(args):
    base = Path(args.dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    cfg_path = base / CONFIG_NAME
    if cfg_path.exists() and not args.force:
        print(f"{cfg_path} already exists (use --force to overwrite)"); return 1
    (base / "claimtrace").mkdir(exist_ok=True)
    cfg_path.write_text(json.dumps(
        {"root": ".", "graph": "claimtrace/graph.json", "verifiers": "claimtrace/verifiers.py",
         "render_types": ["figure"], "input_types": ["data", "artifact", "code"]}, indent=2) + "\n",
        encoding="utf-8")
    gp = base / "claimtrace" / "graph.json"
    if not gp.exists() or args.force:
        gp.write_text(json.dumps(SKELETON_GRAPH, indent=2) + "\n", encoding="utf-8")
    vp = base / "claimtrace" / "verifiers.py"
    if not vp.exists() or args.force:
        vp.write_text(SKELETON_VERIFIERS, encoding="utf-8")
    print(f"initialised claimtrace in {base}\n  {CONFIG_NAME}\n  claimtrace/graph.json\n  claimtrace/verifiers.py\n"
          f"Next: edit claimtrace/graph.json, then `claimtrace check`.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="claimtrace",
                                 description="Dependency-aware provenance + verification for data analysis")
    ap.add_argument("--version", action="version", version=f"claimtrace {__version__}")
    ap.add_argument("--config", help="explicit path to claimtrace.config.json (default: discover upward)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="validate the graph against disk + canonical concepts")
    p = sub.add_parser("lint", help="warn on non-standard vocabulary + un-annotated load-bearing nodes")
    p.add_argument("--strict", action="store_true", help="exit non-zero if any warnings")
    p = sub.add_parser("downstream", help="transitive dependents of a node"); p.add_argument("node")
    p = sub.add_parser("upstream", help="transitive dependencies of a node"); p.add_argument("node")
    p = sub.add_parser("impact", help="propagation to-do list for a canonical change"); p.add_argument("--set", required=True)
    p = sub.add_parser("node", help="show a node + its edges"); p.add_argument("node")
    p = sub.add_parser("log", help="append a lab-notebook entry from a JSON file"); p.add_argument("entry"); p.add_argument("--update", action="store_true")
    p = sub.add_parser("journal", help="lab-notebook view grouped by verdict"); p.add_argument("--status", default="")
    sub.add_parser("snapshot", help="lock each render's input hashes into a manifest")
    sub.add_parser("verify", help="run project-specific numeric checks")
    sub.add_parser("summary", help="node/edge/concept counts")
    p = sub.add_parser("init", help="scaffold claimtrace.config.json in a project"); p.add_argument("dir", nargs="?", default="."); p.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    dispatch = {
        "check": cmd_check, "lint": cmd_lint, "downstream": cmd_downstream, "upstream": cmd_upstream,
        "impact": cmd_impact, "node": cmd_node, "log": cmd_log, "journal": cmd_journal,
        "snapshot": cmd_snapshot, "verify": cmd_verify, "summary": cmd_summary, "init": cmd_init,
    }
    try:
        return dispatch[args.cmd](args)
    except GraphError as e:
        print(f"claimtrace: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
