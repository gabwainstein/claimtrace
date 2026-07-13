"""The dependency-graph engine (stdlib only).

A TYPED DEPENDENCY GRAPH: nodes (data / artifact / code / figure / claim / doc / experiment / ...)
connected by directional information-flow edges. The point is PROPAGATION: when a canonical value
changes, list every downstream node that is now stale, so nothing silently lags the decision.

graph.json:
  {"schema_version": "1.0",
   "nodes":   [{"id","type","path"?,"backbone"?,"status","value"?,"note"?,"date"?,"script"?}],
   "edges":   [{"from","to","rel"}],
   "concepts":{"<name>": {"canonical": "<value>", "legacy"?: "...", "note"?: "..."}}}

`backbone` records which canonical value a node is built on; `status` is its lifecycle/verdict.
"""
from __future__ import annotations
import hashlib
import json
import os
from collections import defaultdict, deque
from pathlib import Path

SCHEMA_VERSION = "1.0"

# edges whose `to` DEPENDS ON `from` (information flows from -> to)
DEP_RELS = {"produces", "renders", "supports", "cites", "derives_from", "reads"}
# provenance ANNOTATIONS (lab-notebook) — recorded + queryable but NOT dependency edges
ANNOT_RELS = {"supersedes", "superseded_by", "refutes", "retracts", "tried_before", "related",
              "evidenced_by"}
# statuses that nothing CURRENT may depend on (a live result must not build on a killed branch)
RETIRED = {"deprecated", "retracted", "dead_end", "superseded"}
# lab-notebook verdict statuses (an attempt + where it ended up)
NOTEBOOK_STATUSES = {"current", "confirmed", "null", "dead_end", "retracted",
                     "superseded", "stale", "deprecated"}
# node types the tool understands (free-form is allowed, but unknown types get no type checks)
KNOWN_TYPES = {"data", "artifact", "code", "figure", "claim", "doc", "doc_span",
               "experiment", "method", "decision", "reference", "concept"}
KNOWN_RELS = DEP_RELS | ANNOT_RELS

TYPE_RANK = {"data": 0, "artifact": 1, "code": 2, "figure": 3,
             "claim": 4, "doc_span": 5, "doc": 6}


class GraphError(Exception):
    """The graph file is missing, unparseable, or structurally invalid."""


# --------------------------------------------------------------------------- loading

def load_raw(cfg):
    """Parse graph.json into a validated dict, raising GraphError on any structural problem."""
    p = Path(cfg.graph_path)
    try:
        text = p.read_text(encoding="utf-8-sig")   # tolerate a UTF-8 BOM (common on Windows)
    except FileNotFoundError:
        raise GraphError(f"graph file not found: {p} (run `claimtrace init`?)")
    except OSError as e:
        raise GraphError(f"cannot read graph file {p}: {e}")
    try:
        g = json.loads(text)
    except json.JSONDecodeError as e:
        raise GraphError(f"{p.name}: invalid JSON - {e}")
    if not isinstance(g, dict):
        raise GraphError(f"{p.name}: top level must be a JSON object")
    if not isinstance(g.get("nodes"), list):
        raise GraphError(f"{p.name}: missing or non-list 'nodes'")
    if "edges" in g and not isinstance(g["edges"], list):
        raise GraphError(f"{p.name}: 'edges' must be a list")
    if "concepts" in g and not isinstance(g["concepts"], dict):
        raise GraphError(f"{p.name}: 'concepts' must be an object")
    for i, n in enumerate(g["nodes"]):
        if not isinstance(n, dict) or "id" not in n:
            raise GraphError(f"{p.name}: node #{i} is not an object with an 'id'")
    return g


def load_graph(cfg):
    """Return (nodes-by-id, edges, concepts). Duplicate ids collapse (last wins) — see check()."""
    g = load_raw(cfg)
    nodes = {n["id"]: n for n in g["nodes"]}
    return nodes, g.get("edges", []), g.get("concepts", {})


def build_adj(edges):
    """down[x] = nodes that depend on x; up[x] = nodes x depends on. Annotations are skipped."""
    down, up = defaultdict(list), defaultdict(list)
    for e in edges:
        if not all(k in e for k in ("from", "to", "rel")):
            continue
        f, t, r = e["from"], e["to"], e["rel"]
        if r in ANNOT_RELS:
            continue
        if r == "reads":          # code READS artifact => code depends on artifact
            up[f].append((t, r)); down[t].append((f, r))
        else:                      # from PRODUCES/SUPPORTS/... to => to depends on from
            down[f].append((t, r)); up[t].append((f, r))
    return down, up


def closure(start, adj):
    seen, order, q = set(), [], deque([start])
    while q:
        x = q.popleft()
        for (nb, r) in adj.get(x, []):
            if nb not in seen:
                seen.add(nb); order.append((nb, r, x)); q.append(nb)
    return order


def _ancestors(nid, up):
    seen, q = set(), deque([nid])
    while q:
        x = q.popleft()
        for (u, r) in up.get(x, []):
            if u not in seen:
                seen.add(u); q.append(u)
    return seen


def _find_cycle(edges, idset):
    """Return a node-list describing one dependency cycle, or None. Iterative DFS (colouring)."""
    down, _ = build_adj(edges)
    color = {}  # 0/absent = unvisited, 1 = on stack, 2 = done
    for root in idset:
        if color.get(root, 0) != 0:
            continue
        # iterative DFS carrying the current path
        stack = [(root, iter(down.get(root, [])))]
        color[root] = 1
        path = [root]
        while stack:
            node, it = stack[-1]
            advanced = False
            for (v, _r) in it:
                if v not in idset:
                    continue
                c = color.get(v, 0)
                if c == 1:                      # back-edge -> cycle
                    return path[path.index(v):] + [v]
                if c == 0:
                    color[v] = 1
                    path.append(v)
                    stack.append((v, iter(down.get(v, []))))
                    advanced = True
                    break
            if not advanced:
                color[node] = 2
                path.pop()
                stack.pop()
    return None


def _sha1(fp):
    h = hashlib.sha1()
    with open(fp, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# --------------------------------------------------------------------------- queries

def downstream(cfg, node):
    nodes, edges, _ = load_graph(cfg)
    if node not in nodes:
        return None
    down, _ = build_adj(edges)
    return [(nb, r, nodes.get(nb, {})) for nb, r, _ in closure(node, down)]


def upstream(cfg, node):
    nodes, edges, _ = load_graph(cfg)
    if node not in nodes:
        return None
    _, up = build_adj(edges)
    return [(nb, r, nodes.get(nb, {})) for nb, r, _ in closure(node, up)]


def impact(cfg, concept, value):
    """The propagation to-do list for setting `concept = value`."""
    nodes, edges, concepts = load_graph(cfg)
    if concept not in concepts:
        return None, list(concepts)
    down, _ = build_adj(edges)
    direct = [nid for nid, n in nodes.items()
              if n.get("backbone") and n["backbone"] != value and n.get("status") != "deprecated"]
    stale = {}
    for nid in direct:
        stale.setdefault(nid, "on " + str(nodes[nid].get("backbone")))
        for nb, r, _ in closure(nid, down):
            stale.setdefault(nb, f"downstream of {nid} via {r}")
    items = sorted(stale.items(), key=lambda kv: TYPE_RANK.get(nodes.get(kv[0], {}).get("type", ""), 9))
    return [(nid, why, nodes.get(nid, {})) for nid, why in items], concepts[concept].get("canonical")


# --------------------------------------------------------------------------- structural integrity

def structural_issues(cfg):
    """Hard errors that break the graph's own promises: duplicate ids, dangling/self edges, cycles."""
    g = load_raw(cfg)
    node_list = g["nodes"]
    edges = g.get("edges", [])
    ids = [n["id"] for n in node_list]
    idset = set(ids)
    out = []

    seen, dups = set(), []
    for i in ids:
        if i in seen and i not in dups:
            dups.append(i)
        seen.add(i)
    for d in dups:
        out.append(("DUPLICATE_ID", d, "id declared more than once (later silently wins, earlier lost)"))

    for e in edges:
        if not all(k in e for k in ("from", "to", "rel")):
            out.append(("MALFORMED_EDGE", "-", f"edge missing from/to/rel: {e}"))
            continue
        if e["from"] == e["to"]:
            out.append(("SELF_EDGE", e["from"], f"edge points at itself via {e['rel']}"))
        for endp in ("from", "to"):
            if e[endp] not in idset:
                out.append(("DANGLING_EDGE", e[endp],
                            f"edge {e['from']} --{e['rel']}--> {e['to']}: '{e[endp]}' is not a declared node"))

    cyc = _find_cycle(edges, idset)
    if cyc:
        out.append(("CYCLE", cyc[0], "dependency cycle: " + " -> ".join(cyc)))
    return out


def lint_issues(cfg):
    """Soft warnings: unknown vocabulary and un-annotated load-bearing nodes that disable checks."""
    g = load_raw(cfg)
    nodes = {n["id"]: n for n in g["nodes"]}
    edges = g.get("edges", [])
    concepts = g.get("concepts", {})
    out = []

    sv = g.get("schema_version")
    if sv is None:
        out.append(("NO_SCHEMA_VERSION", "-",
                    f"graph has no 'schema_version' (tool is {SCHEMA_VERSION}); add it for forward-compat"))
    elif str(sv) != SCHEMA_VERSION:
        out.append(("SCHEMA_VERSION", "-", f"graph schema_version={sv} != tool {SCHEMA_VERSION}"))

    for nid, n in nodes.items():
        t = n.get("type")
        if t and t not in KNOWN_TYPES:
            out.append(("UNKNOWN_TYPE", nid,
                        f"type '{t}' is non-standard — it gets no type-specific checks (staleness/citation)"))
        s = n.get("status")
        if s and s not in NOTEBOOK_STATUSES:
            out.append(("UNKNOWN_STATUS", nid,
                        f"status '{s}' is non-standard — READS_RETIRED / journal grouping may skip it"))
    for e in edges:
        r = e.get("rel")
        if r and r not in KNOWN_RELS:
            out.append(("UNKNOWN_REL", e.get("from", "-"),
                        f"edge rel '{r}' is unknown — it is treated as a dependency edge"))

    if concepts:
        load_bearing = set(cfg.render_types) | {"claim"}
        for nid, n in nodes.items():
            if (n.get("type") in load_bearing and not n.get("backbone")
                    and n.get("status") in (None, "current", "confirmed")):
                out.append(("NO_BACKBONE", nid,
                            f"{n.get('type')} declares no 'backbone' -> never drift-checked against a concept"))
    return out


# --------------------------------------------------------------------------- check

def compute_check(cfg):
    """Return (problems, pending). problems = hard errors; pending = known-stale (status='stale')."""
    problems = list(structural_issues(cfg))     # structural integrity first
    nodes, edges, concepts = load_graph(cfg)
    down, up = build_adj(edges)
    pending = []
    canon_vals = {c.get("canonical") for c in concepts.values()}

    # 1. file existence
    for nid, n in nodes.items():
        p = n.get("path")
        if p and not cfg.resolve(p).exists():
            problems.append(("MISSING_FILE", nid, p))

    # 2a. SILENT drift: node marked current but off the canonical backbone
    for cname, c in concepts.items():
        canon = c.get("canonical")
        for nid, n in nodes.items():
            if n.get("backbone") and n.get("status") == "current" and n["backbone"] != canon:
                problems.append(("SILENT_DRIFT", nid, f"current but {n['backbone']} != {cname}={canon}"))

    # 2b. PENDING migration: node explicitly marked stale
    for nid, n in nodes.items():
        if n.get("status") == "stale":
            pending.append((nid, n.get("type", "?"),
                            f"{n.get('backbone','')} -> {'/'.join(str(v) for v in canon_vals)}",
                            n.get("path", "")))

    # 3. a CURRENT node must not depend on a RETIRED one
    for nid, n in nodes.items():
        for (u, r) in up.get(nid, []):
            ust = nodes.get(u, {}).get("status")
            if ust in RETIRED and n.get("status") == "current":
                problems.append(("READS_RETIRED", nid, f"depends on {ust} {u}"))

    # 4. claim cites an off-backbone artifact
    for e in edges:
        if e.get("rel") == "cites":
            c, a = nodes.get(e["from"], {}), nodes.get(e["to"], {})
            if c.get("type") == "claim" and c.get("backbone") and a.get("backbone") and c["backbone"] != a["backbone"]:
                problems.append(("CLAIM_CITES_OFFBACKBONE", e["from"],
                                 f"claim {c['backbone']} cites {e['to']} {a['backbone']}"))

    def _mtime(p):
        fp = cfg.resolve(p)
        return fp.stat().st_mtime if fp.exists() else None

    # 5. mtime staleness: a render-type node older than a file it (transitively) depends on
    #    (NOTE: mtime is unreliable after `git clone`/checkout; treat STALE_DATA below as primary.)
    for nid, n in nodes.items():
        if n.get("type") not in cfg.render_types:
            continue
        t = _mtime(n.get("path", "")) if n.get("path") else None
        if t is None:
            continue
        for u in _ancestors(nid, up):
            un = nodes.get(u, {})
            up_p = un.get("path")
            if not up_p or un.get("status") == "deprecated" or un.get("type") not in cfg.input_types:
                continue
            tu = _mtime(up_p)
            if tu and tu > t + 2:
                problems.append(("STALE_RENDER", nid, f"rendered before upstream {u} changed ({up_p}) — re-render"))
                break

    # 6. content-hash staleness: a render's locked manifest inputs changed since the snapshot
    for nid, n in nodes.items():
        if n.get("type") not in cfg.render_types or not n.get("path"):
            continue
        man = cfg.resolve(n["path"] + ".manifest.json")
        if not man.exists():
            continue
        try:
            rec = json.loads(man.read_text(encoding="utf-8"))
        except Exception:
            continue
        for inp in rec.get("inputs", []):
            ip = cfg.resolve(inp["path"]) if not os.path.isabs(inp["path"]) else Path(inp["path"])
            if "sha1" not in inp:
                continue
            if not Path(ip).exists():
                problems.append(("STALE_DATA", nid, f"locked input now missing: {inp['path']}"))
                break
            if _sha1(ip) != inp["sha1"]:
                problems.append(("STALE_DATA", nid, f"input changed since lock: {inp['path']} — re-render + re-snapshot"))
                break

    return problems, pending


# --------------------------------------------------------------------------- log

def log_entry(cfg, entry, update=False):
    """Append a lab-notebook node (+ optional edges) from a dict. Returns (ok, message)."""
    raw = load_raw(cfg)
    if not isinstance(entry, dict) or "node" not in entry:
        return False, "entry must be an object with a 'node'"
    node = entry["node"]
    new_edges = entry.get("edges", [])
    if not isinstance(node, dict) or "id" not in node or "type" not in node:
        return False, "entry.node needs at least id + type"
    ids = {n["id"] for n in raw["nodes"]}
    if node["id"] in ids and not update:
        return False, f"node {node['id']} already exists (use --update to overwrite)"
    if node["id"] in ids:
        raw["nodes"] = [n for n in raw["nodes"] if n["id"] != node["id"]]
    raw["nodes"].append(node)
    raw.setdefault("edges", [])
    have = {(e["from"], e["to"], e["rel"]) for e in raw["edges"] if all(k in e for k in ("from", "to", "rel"))}
    added = 0
    for e in new_edges:
        k = (e["from"], e["to"], e["rel"])
        if k not in have:
            raw["edges"].append(e); have.add(k); added += 1
    Path(cfg.graph_path).write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True, (f"logged {node['id']} [{node.get('type','?')}/{node.get('status','?')}] + {added} edge(s) "
                  f"-> {len(raw['nodes'])} nodes / {len(raw['edges'])} edges")
