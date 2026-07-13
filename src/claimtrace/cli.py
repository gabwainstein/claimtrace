"""claimtrace command-line interface."""
from __future__ import annotations
import argparse
import json
import os
import sys
import uuid
from collections import defaultdict
from importlib import resources
from pathlib import Path

from . import __version__
from .config import CONFIG_NAME, load_config, strict_json_loads
from .engine import (ANNOT_RELS, GraphError, compute_check, downstream, impact,
                     lint_issues, load_graph, log_entry, upstream)
from .events import EventError, run_command
from .report import build_fatal_report, build_report, dumps_report
from .snapshot import snapshot
from .verify import run_verifiers
from .view import render_view

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
    if args.json or args.strict:
        try:
            report = build_report(_cfg(args), strict=args.strict)
        except GraphError as exc:
            report = build_fatal_report(exc, strict=args.strict, code="GRAPH_ERROR")
        except EventError as exc:
            report = build_fatal_report(exc, strict=args.strict, code="RECEIPT_ERROR")
        except OSError as exc:
            report = build_fatal_report(exc, strict=args.strict, code="IO_ERROR")
        except SystemExit as exc:
            report = build_fatal_report(exc, strict=args.strict, code="CONFIG_ERROR")
        if args.json:
            sys.stdout.write(dumps_report(report))
            return report["exit_code"]
        if report["fatal"]:
            print(f"claimtrace check --strict: {report['fatal']['code']} - {report['fatal']['detail']}",
                  file=sys.stderr)
            return 2
        if report["ok"]:
            print("claimtrace check --strict: OK within declared/partial runtime-capture scope - "
                  "no blocking graph, receipt, lint, or pending findings.")
            return 0
        print(f"claimtrace check --strict: {report['summary']['blocking']} blocking finding(s):\n")
        for item in report["findings"]:
            if item["blocking"]:
                node = item["node_id"] or "-"
                print(f"  [{item['severity'].upper():7s} {item['code']:28s}] {node:30s} {item['detail']}")
        return report["exit_code"]

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
    try:
        entry = strict_json_loads(Path(args.entry).read_text(encoding="utf-8-sig"), args.entry)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise GraphError(f"cannot read log entry {args.entry}: {exc}") from exc
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


def _key_values(values, label):
    result = {}
    for item in values or []:
        key, separator, value = item.partition("=")
        key = key.strip()
        if not separator or not key:
            raise EventError(f"{label} must use KEY=VALUE syntax: {item!r}")
        if key in result:
            raise EventError(f"duplicate {label} key: {key}")
        result[key] = value
    return result


def cmd_run(args):
    if not args.command or args.command[0] != "--":
        raise EventError("separate claimtrace options from the child command with --")
    command = args.command[1:]
    result = run_command(
        _cfg(args), command,
        inputs=args.input or [], outputs=args.output or [],
        no_inputs=args.no_inputs, no_outputs=args.no_outputs,
        cwd=args.cwd, name=args.name,
        parameters=_key_values(args.param, "parameter"),
        seeds=_key_values(args.seed, "seed"),
        allow_external=args.allow_external,
        scan_writes=not args.no_scan_writes,
        redact_flags=args.redact_flag,
    )
    print(f"claimtrace run: {result['outcome']}  {result['run_id']}")
    print("  lineage coverage: partial (declared inputs; direct child; unattributed before/after writes)")
    for item in result["output_transitions"]:
        produced = "produced" if item["produced"] else "not proven produced"
        print(f"  output: {item['path']}  {item['transition']}  ({produced})")
    for detail in result["contract_errors"]:
        print(f"  contract: {detail}")
    return result["exit_code"]


def cmd_view(args):
    try:
        summary = render_view(_cfg(args), args.output)
    except OSError as exc:
        raise GraphError(f"cannot write view {args.output}: {exc}") from exc
    print(f"claimtrace view: wrote {summary['path']}")
    print(f"  {summary['nodes']} semantic nodes / {summary['edges']} semantic edges / "
          f"{summary['runs']} run receipts / {summary['layers']} layers")
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
         "events": "claimtrace/events", "render_types": ["figure"],
         "input_types": ["data", "artifact", "code"], "run_output_types": ["artifact"]},
        indent=2) + "\n",
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


def _packaged_skill_text():
    resource = (resources.files("claimtrace") / "templates" / "claimtrace-log" / "SKILL.md")
    return resource.read_text(encoding="utf-8")


def _atomic_text(destination: Path, content: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def cmd_install_skill(args):
    """Install the packaged research skill into explicit project-local discovery paths."""
    base = Path(args.dir).expanduser().resolve()
    content = _packaged_skill_text()
    choices = {
        "agents": base / ".agents" / "skills" / "claimtrace-log" / "SKILL.md",
        "claude": base / ".claude" / "skills" / "claimtrace-log" / "SKILL.md",
    }
    selected = list(choices) if args.target == "both" else [args.target]
    destinations = [choices[key] for key in selected]
    conflicts = []
    matches = {}
    for destination in destinations:
        if not destination.exists():
            continue
        try:
            existing = destination.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            conflicts.append(f"{destination}: cannot verify existing file ({exc})")
            continue
        if existing != content:
            conflicts.append(f"{destination}: existing skill differs")
        else:
            matches[destination] = True
    if conflicts and not args.force:
        print("claimtrace install-skill: refusing to overwrite:", file=sys.stderr)
        for detail in conflicts:
            print(f"  {detail}", file=sys.stderr)
        print("Re-run with --force only after reviewing the existing skill.", file=sys.stderr)
        return 1

    for destination in destinations:
        if matches.get(destination):
            print(f"claimtrace install-skill: up to date  {destination}")
            continue
        _atomic_text(destination, content)
        print(f"claimtrace install-skill: installed  {destination}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="claimtrace",
                                 description="Dependency-aware provenance + verification for data analysis")
    ap.add_argument("--version", action="version", version=f"claimtrace {__version__}")
    ap.add_argument("--config", help="explicit path to claimtrace.config.json (default: discover upward)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check", help="validate the graph against disk + canonical concepts")
    p.add_argument("--strict", action="store_true",
                   help="also fail on lint warnings, stale nodes, and incomplete run provenance")
    p.add_argument("--json", action="store_true", help="emit one deterministic JSON report")
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
    p = sub.add_parser("run", help="execute a command and append content-addressed mechanical receipts")
    p.add_argument("--input", action="append", help="declared input file, relative to the project root")
    p.add_argument("--no-inputs", action="store_true", help="assert that this command has no file inputs")
    p.add_argument("--output", action="append", help="declared output file, relative to the project root")
    p.add_argument("--no-outputs", action="store_true", help="assert that this command has no file outputs")
    p.add_argument("--cwd", help="child working directory (must be inside the project root)")
    p.add_argument("--name", help="human-readable run label")
    p.add_argument("--param", action="append", help="explicit reproducibility parameter as KEY=VALUE")
    p.add_argument("--seed", action="append", help="explicit random seed as KEY=VALUE")
    p.add_argument("--allow-external", action="store_true", help="allow declared files outside the project root")
    p.add_argument("--no-scan-writes", action="store_true", help="skip the project-wide pre/post content scan")
    p.add_argument("--redact-flag", action="append", help="additional child argv flag whose following value is secret")
    p.add_argument("command", nargs=argparse.REMAINDER, help="-- COMMAND [ARG ...]")
    p = sub.add_parser("view", help="render a deterministic standalone research-trajectory map")
    p.add_argument("--output", required=True, help="HTML file to write")
    p = sub.add_parser("init", help="scaffold claimtrace.config.json in a project"); p.add_argument("dir", nargs="?", default="."); p.add_argument("--force", action="store_true")
    p = sub.add_parser("install-skill", help="install the packaged claimtrace-log agent skill")
    p.add_argument("--dir", default=".", help="project directory receiving the skill")
    p.add_argument("--target", choices=("agents", "claude", "both"), default="both",
                   help="project-local skill layout to install (default: both)")
    p.add_argument("--force", action="store_true", help="replace a differing existing skill")
    args = ap.parse_args(argv)
    dispatch = {
        "check": cmd_check, "lint": cmd_lint, "downstream": cmd_downstream, "upstream": cmd_upstream,
        "impact": cmd_impact, "node": cmd_node, "log": cmd_log, "journal": cmd_journal,
        "snapshot": cmd_snapshot, "verify": cmd_verify, "summary": cmd_summary, "run": cmd_run,
        "view": cmd_view, "init": cmd_init, "install-skill": cmd_install_skill,
    }
    try:
        return dispatch[args.cmd](args)
    except GraphError as e:
        print(f"claimtrace: {e}", file=sys.stderr)
        return 2
    except EventError as e:
        print(f"claimtrace: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
