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
import heapq
import json
import os
import re
import tempfile
from collections import defaultdict, deque
from contextlib import contextmanager
from pathlib import Path

from .config import strict_json_loads

SCHEMA_VERSION = "1.0"
RENDER_MANIFEST_SCHEMA = "claimtrace.render-manifest/2"
METHOD_SPEC_SCHEMA = "claimtrace.method-spec/1"
METHOD_REQUIREMENTS_SCHEMA = "claimtrace.method-requirements/1"

# edges whose `to` DEPENDS ON `from` (information flows from -> to)
DEP_RELS = {"produces", "renders", "supports", "cites", "derives_from", "reads",
            "motivates", "predicts", "tested_by", "concludes"}
# provenance ANNOTATIONS (lab-notebook) — recorded + queryable but NOT dependency edges
ANNOT_RELS = {"supersedes", "superseded_by", "refutes", "retracts", "tried_before", "related",
              "evidenced_by"}
# statuses that nothing CURRENT may depend on (a live result must not build on a killed branch)
RETIRED = {"deprecated", "retracted", "dead_end", "superseded"}
# statuses that nothing LIVE may depend on (a live result must not build on unexecuted work)
UNEXECUTED = {"planned"}
# lab-notebook verdict statuses (an attempt + where it ended up)
NOTEBOOK_STATUSES = {"planned", "current", "confirmed", "null", "dead_end", "retracted",
                     "superseded", "stale", "deprecated"}
# node types the tool understands (free-form is allowed, but unknown types get no type checks)
KNOWN_TYPES = {"question", "hypothesis", "prediction", "data", "artifact", "code", "figure",
               "claim", "conclusion", "doc", "doc_span", "experiment", "method", "decision",
               "reference", "concept"}
KNOWN_RELS = DEP_RELS | ANNOT_RELS
CLAIM_LIKE_TYPES = {"claim", "hypothesis", "prediction", "conclusion"}
METHOD_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
MAX_METHOD_STEPS = 2_000
MAX_CLAIM_METHODS = 2_000
MAX_CLAIM_METHOD_STEP_REFS = 20_000

TYPE_RANK = {"question": 0, "hypothesis": 1, "prediction": 2, "data": 3, "code": 4,
             "method": 4, "experiment": 5, "artifact": 6, "figure": 7, "claim": 8,
             "conclusion": 9, "decision": 10, "doc_span": 11, "doc": 12}
class GraphError(Exception):
    """The graph file is missing, unparseable, or structurally invalid."""


def _method_token(value, label, graph_name):
    if not isinstance(value, str) or not METHOD_TOKEN_RE.fullmatch(value):
        raise GraphError(
            f"{graph_name}: {label} must be a 1-256 character identifier using "
            "letters, digits, . _ : or -"
        )
    return value


def _validate_method_spec(node, graph_name):
    """Validate one closed method declaration and return its declared step ids."""
    node_id = node["id"]
    if node.get("type") != "method":
        raise GraphError(
            f"{graph_name}: node {node_id!r} field 'method_spec' is only valid on method nodes"
        )
    spec = node["method_spec"]
    if not isinstance(spec, dict) or set(spec) != {"schema_version", "steps"}:
        raise GraphError(
            f"{graph_name}: method node {node_id!r} field 'method_spec' must contain "
            "exactly schema_version and steps"
        )
    if spec.get("schema_version") != METHOD_SPEC_SCHEMA:
        raise GraphError(
            f"{graph_name}: method node {node_id!r} has unsupported method_spec schema "
            f"{spec.get('schema_version')!r}"
        )
    steps = spec.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > MAX_METHOD_STEPS:
        raise GraphError(
            f"{graph_name}: method node {node_id!r} method_spec.steps must be a "
            f"non-empty list of at most {MAX_METHOD_STEPS} steps"
        )
    step_ids = set()
    for index, step in enumerate(steps):
        label = f"method node {node_id!r} method_spec.steps[{index}]"
        if not isinstance(step, dict) or set(step) != {"id", "statement", "required"}:
            raise GraphError(
                f"{graph_name}: {label} must contain exactly id, statement, and required"
            )
        step_id = _method_token(step.get("id"), f"{label}.id", graph_name)
        statement = step.get("statement")
        if (not isinstance(statement, str) or not statement.strip()
                or len(statement) > 16_384):
            raise GraphError(
                f"{graph_name}: {label}.statement must be non-empty text of at most "
                "16384 characters"
            )
        if not isinstance(step.get("required"), bool):
            raise GraphError(f"{graph_name}: {label}.required must be a boolean")
        if step_id in step_ids:
            raise GraphError(
                f"{graph_name}: method node {node_id!r} has duplicate method step {step_id!r}"
            )
        step_ids.add(step_id)
    return frozenset(step_ids)


def _validate_method_requirements(node, graph_name):
    """Validate one closed claim-to-method declaration without inferring any relation."""
    node_id = node["id"]
    if node.get("type") not in CLAIM_LIKE_TYPES:
        raise GraphError(
            f"{graph_name}: node {node_id!r} field 'method_requirements' is only valid on "
            "claim, hypothesis, prediction, or conclusion nodes"
        )
    requirements = node["method_requirements"]
    if (not isinstance(requirements, dict)
            or set(requirements) != {"schema_version", "methods"}):
        raise GraphError(
            f"{graph_name}: claim-like node {node_id!r} field 'method_requirements' must "
            "contain exactly schema_version and methods"
        )
    if requirements.get("schema_version") != METHOD_REQUIREMENTS_SCHEMA:
        raise GraphError(
            f"{graph_name}: claim-like node {node_id!r} has unsupported "
            f"method_requirements schema {requirements.get('schema_version')!r}"
        )
    methods = requirements.get("methods")
    if not isinstance(methods, list) or not methods or len(methods) > MAX_CLAIM_METHODS:
        raise GraphError(
            f"{graph_name}: claim-like node {node_id!r} method_requirements.methods must "
            f"be a non-empty list of at most {MAX_CLAIM_METHODS} methods"
        )
    normalized = []
    seen_methods = set()
    total_step_refs = 0
    for index, method in enumerate(methods):
        label = f"claim-like node {node_id!r} method_requirements.methods[{index}]"
        if not isinstance(method, dict) or set(method) != {"method_id", "step_ids"}:
            raise GraphError(
                f"{graph_name}: {label} must contain exactly method_id and step_ids"
            )
        method_id = _method_token(method.get("method_id"), f"{label}.method_id", graph_name)
        if method_id in seen_methods:
            raise GraphError(
                f"{graph_name}: claim-like node {node_id!r} references method "
                f"{method_id!r} more than once"
            )
        seen_methods.add(method_id)
        step_ids = method.get("step_ids")
        if (not isinstance(step_ids, list) or not step_ids
                or len(step_ids) > MAX_METHOD_STEPS):
            raise GraphError(
                f"{graph_name}: {label}.step_ids must be a non-empty list of at most "
                f"{MAX_METHOD_STEPS} step identifiers"
            )
        checked_steps = []
        seen_steps = set()
        for step_index, value in enumerate(step_ids):
            step_id = _method_token(
                value, f"{label}.step_ids[{step_index}]", graph_name,
            )
            if step_id in seen_steps:
                raise GraphError(
                    f"{graph_name}: claim-like node {node_id!r} references method step "
                    f"{method_id!r}/{step_id!r} more than once"
                )
            seen_steps.add(step_id)
            checked_steps.append(step_id)
        total_step_refs += len(checked_steps)
        if total_step_refs > MAX_CLAIM_METHOD_STEP_REFS:
            raise GraphError(
                f"{graph_name}: claim-like node {node_id!r} exceeds the limit of "
                f"{MAX_CLAIM_METHOD_STEP_REFS} method step references"
            )
        normalized.append((method_id, checked_steps))
    return normalized


def _validate_method_semantics(graph, graph_name):
    """Validate authored method meaning and exact claim references, without inference."""
    method_steps = {}
    for node in graph["nodes"]:
        if "method_spec" in node:
            method_steps[id(node)] = _validate_method_spec(node, graph_name)
        if "method_requirements" in node:
            _validate_method_requirements(node, graph_name)

    # Match load_graph's documented last-definition-wins view. Duplicate ids are still
    # reported by structural checking; this pass never guesses between definitions.
    nodes = {node["id"]: node for node in graph["nodes"]}
    for claim in graph["nodes"]:
        if "method_requirements" not in claim:
            continue
        for method_id, required_steps in _validate_method_requirements(claim, graph_name):
            method = nodes.get(method_id)
            if method is None:
                raise GraphError(
                    f"{graph_name}: claim-like node {claim['id']!r} references missing "
                    f"method node {method_id!r}"
                )
            if method.get("type") != "method":
                raise GraphError(
                    f"{graph_name}: claim-like node {claim['id']!r} reference {method_id!r} "
                    "does not identify a method node"
                )
            # Inactive claims are part of the audit trail and must be able to retain
            # the exact inactive method that produced them.  Only a live claim is
            # forbidden from relying on an inactive method; missing nodes, wrong
            # node types, absent method specs, and unknown steps remain invalid for
            # every claim lifecycle state.  A missing status remains live for
            # backward compatibility with older graphs.
            active_statuses = {None, "current", "confirmed"}
            claim_is_active = claim.get("status") in active_statuses
            method_is_active = method.get("status") in active_statuses
            if claim_is_active and not method_is_active:
                raise GraphError(
                    f"{graph_name}: claim-like node {claim['id']!r} references inactive "
                    f"method node {method_id!r}"
                )
            declared_steps = method_steps.get(id(method))
            if declared_steps is None:
                raise GraphError(
                    f"{graph_name}: referenced method node {method_id!r} needs a method_spec"
                )
            for step_id in required_steps:
                if step_id not in declared_steps:
                    raise GraphError(
                        f"{graph_name}: claim-like node {claim['id']!r} references unknown "
                        f"method step {method_id!r}/{step_id!r}"
                    )


# --------------------------------------------------------------------------- loading

def load_raw(cfg):
    """Parse graph.json into a validated dict, raising GraphError on any structural problem."""
    p = Path(cfg.graph_path)
    try:
        text = p.read_text(encoding="utf-8-sig")   # tolerate a UTF-8 BOM (common on Windows)
    except FileNotFoundError:
        raise GraphError(f"graph file not found: {p} (run `provsleuth init`?)")
    except OSError as e:
        raise GraphError(f"cannot read graph file {p}: {e}")
    try:
        g = strict_json_loads(text, p.name)
    except (json.JSONDecodeError, ValueError) as e:
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
        if (not isinstance(n, dict) or not isinstance(n.get("id"), str)
                or not n["id"]):
            raise GraphError(f"{p.name}: node #{i} needs a non-empty string 'id'")
        for key in ("type", "status", "path", "script"):
            if key in n and n[key] is not None and not isinstance(n[key], str):
                raise GraphError(f"{p.name}: node {n['id']!r} field {key!r} must be a string")
        if ("run_ids" in n and (not isinstance(n["run_ids"], list)
                               or not all(isinstance(item, str) for item in n["run_ids"]))):
            raise GraphError(f"{p.name}: node {n['id']!r} field 'run_ids' must be a string list")
    for i, e in enumerate(g.get("edges", [])):
        if not isinstance(e, dict):
            raise GraphError(f"{p.name}: edge #{i} must be an object")
    for name, concept in g.get("concepts", {}).items():
        if not isinstance(name, str) or not isinstance(concept, dict):
            raise GraphError(f"{p.name}: each concept must have a string name and object value")
    _validate_method_semantics(g, p.name)
    return g


def load_graph(cfg, raw=None):
    """Return (nodes-by-id, edges, concepts). Duplicate ids collapse (last wins) — see check().

    Pass an already validated ``raw`` graph to keep a larger read-only operation on one graph-file
    snapshot. Existing callers may continue to omit it.
    """
    g = raw if raw is not None else load_raw(cfg)
    nodes = {n["id"]: n for n in g["nodes"]}
    return nodes, g.get("edges", []), g.get("concepts", {})


def build_adj(edges):
    """down[x] = nodes that depend on x; up[x] = nodes x depends on. Annotations are skipped."""
    down, up = defaultdict(list), defaultdict(list)
    for e in edges:
        if not all(k in e for k in ("from", "to", "rel")):
            continue
        f, t, r = e["from"], e["to"], e["rel"]
        if not all(isinstance(value, str) and value for value in (f, t, r)):
            continue
        if r in ANNOT_RELS:
            continue
        if r == "reads":          # code READS artifact => code depends on artifact
            up[f].append((t, r))
            down[t].append((f, r))
        else:                      # from PRODUCES/SUPPORTS/... to => to depends on from
            down[f].append((t, r))
            up[t].append((f, r))
    for values in down.values():
        values.sort()
    for values in up.values():
        values.sort()
    return down, up


def closure(start, adj):
    seen, order, q = set(), [], deque([start])
    while q:
        x = q.popleft()
        for (nb, r) in adj.get(x, []):
            if nb not in seen:
                seen.add(nb)
                order.append((nb, r, x))
                q.append(nb)
    return order


def _ancestors(nid, up):
    seen, q = set(), deque([nid])
    while q:
        x = q.popleft()
        for (u, r) in up.get(x, []):
            if u not in seen:
                seen.add(u)
                q.append(u)
    return seen


def _find_cycle(edges, idset):
    """Return a node-list describing one dependency cycle, or None. Iterative DFS (colouring)."""
    down, _ = build_adj(edges)
    color = {}  # 0/absent = unvisited, 1 = on stack, 2 = done
    for root in sorted(idset):
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


def _file_hash(fp, algorithm):
    """Hash one file with an explicitly selected supported manifest algorithm."""
    if algorithm not in {"sha1", "sha256"}:
        raise ValueError(f"unsupported file hash algorithm: {algorithm}")
    h = hashlib.new(algorithm)
    with open(fp, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _valid_hex_digest(value, length):
    return (isinstance(value, str) and len(value) == length
            and all(char in "0123456789abcdef" for char in value))


@contextmanager
def _graph_lock(graph_path):
    """Serialize graph read-modify-write operations across local processes.

    Reuse the hardened per-user runtime lock used by event publication: it rejects
    link/reparse lock paths and verifies descriptor/path identity after acquisition.
    Atomic replacement still protects readers from partial JSON; this lock prevents
    lost updates without trusting a shared, predictable temp-directory entry.
    """
    # Local import keeps the dependency graph engine independent at import time;
    # events does not import engine and the shared helper is stdlib-only.
    from .events import EventError, _event_lock

    graph_path = Path(graph_path).resolve()
    try:
        with _event_lock(graph_path):
            yield
    except EventError as exc:
        raise GraphError(f"cannot acquire graph lock for {graph_path}: {exc}") from exc


def backbone_bindings(node, concepts):
    """Return concept-keyed backbone bindings plus an ambiguity message, if any.

    Schema 1.0 used a scalar ``backbone``. Keep that form working for projects with exactly one
    concept, but require an object once a graph has multiple independent canonical choices.
    """
    raw = node.get("backbone")
    if raw is None:
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    if len(concepts) == 1:
        return {next(iter(concepts)): raw}, None
    if len(concepts) > 1:
        return {}, ("scalar backbone is ambiguous with multiple concepts; use "
                    "{'concept_name': 'value', ...}")
    return {}, None


def _backbone_conflicts(left, right, concepts):
    """Return shared concept bindings whose values disagree."""
    lb, _ = backbone_bindings(left, concepts)
    rb, _ = backbone_bindings(right, concepts)
    return [(name, lb[name], rb[name]) for name in sorted(set(lb) & set(rb))
            if lb[name] != rb[name]]


def expected_inputs(cfg, nodes, edges, nid):
    """Return the declared, transitive file inputs for a materialized node, keyed by graph path."""
    _, up = build_adj(edges)
    out = {}
    for ancestor in _ancestors(nid, up):
        node = nodes.get(ancestor, {})
        path = node.get("path")
        if path and node.get("type") in cfg.input_types:
            out[path] = cfg.resolve(path)
    return out


def direct_inputs(cfg, nodes, edges, nid):
    """Return immediate graph-declared file inputs for one executable output boundary."""
    _, up = build_adj(edges)
    out = {}
    for source, _relation in up.get(nid, []):
        node = nodes.get(source, {})
        path = node.get("path")
        if path and node.get("type") in cfg.input_types:
            out[path] = cfg.resolve(path)
    return out


def _ordered_subset(node_ids, nodes, down):
    """Return a stable topological order for a subset of the dependency graph."""
    node_ids = set(node_ids)
    indegree = {nid: 0 for nid in node_ids}
    for source in node_ids:
        for target, _rel in down.get(source, []):
            if target in indegree:
                indegree[target] += 1
    ready = [(TYPE_RANK.get(nodes.get(nid, {}).get("type", ""), 9), nid)
             for nid, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    ordered = []
    while ready:
        _rank, nid = heapq.heappop(ready)
        ordered.append(nid)
        for target, _rel in down.get(nid, []):
            if target not in indegree:
                continue
            indegree[target] -= 1
            if indegree[target] == 0:
                heapq.heappush(ready, (TYPE_RANK.get(nodes.get(target, {}).get("type", ""), 9),
                                       target))
    if len(ordered) != len(node_ids):
        ordered.extend(sorted(node_ids - set(ordered)))
    return ordered


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
    direct = []
    for nid, node in nodes.items():
        bindings, _ambiguous = backbone_bindings(node, concepts)
        if (concept in bindings and bindings[concept] != value
                and node.get("status") != "deprecated"):
            direct.append(nid)
    stale = {}
    for nid in direct:
        bindings, _ambiguous = backbone_bindings(nodes[nid], concepts)
        stale.setdefault(nid, f"on {concept}={bindings[concept]}")
        for nb, r, _ in closure(nid, down):
            stale.setdefault(nb, f"downstream of {nid} via {r}")
    items = [(nid, stale[nid]) for nid in _ordered_subset(stale, nodes, down)]
    return [(nid, why, nodes.get(nid, {})) for nid, why in items], concepts[concept].get("canonical")


# --------------------------------------------------------------------------- structural integrity

def structural_issues(cfg, raw=None):
    """Hard errors that break the graph's own promises: duplicate ids, dangling/self edges, cycles."""
    g = raw if raw is not None else load_raw(cfg)
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

    # A node path is graph content, so it must stay inside the project root; otherwise
    # the engine would read, hash, and publish a file the project does not own.
    for n in node_list:
        p = n.get("path")
        if isinstance(p, str) and p and not cfg.within_root(p):
            out.append(("INVALID_NODE_PATH", n.get("id", "-"),
                        f"path must be project-relative and inside the project root: {p}"))

    valid_edges = []
    for e in edges:
        if (not all(k in e for k in ("from", "to", "rel"))
                or not all(isinstance(e.get(k), str) and e[k]
                           for k in ("from", "to", "rel"))):
            out.append(("MALFORMED_EDGE", "-",
                        f"edge needs non-empty string from/to/rel: {e}"))
            continue
        valid_edges.append(e)
        if e["from"] == e["to"]:
            out.append(("SELF_EDGE", e["from"], f"edge points at itself via {e['rel']}"))
        for endp in ("from", "to"):
            if e[endp] not in idset:
                out.append(("DANGLING_EDGE", e[endp],
                            f"edge {e['from']} --{e['rel']}--> {e['to']}: '{e[endp]}' is not a declared node"))

    cyc = _find_cycle(valid_edges, idset)
    if cyc:
        out.append(("CYCLE", cyc[0], "dependency cycle: " + " -> ".join(cyc)))
    return out


def lint_issues(cfg, raw=None):
    """Soft warnings: unknown vocabulary and un-annotated load-bearing nodes that disable checks.

    ``raw`` may be supplied by a report builder so checking and linting inspect the same atomic
    graph-file snapshot. Omitting it preserves the public API and loads the graph normally.
    """
    g = raw if raw is not None else load_raw(cfg)
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
        _bindings, ambiguity = backbone_bindings(n, concepts)
        if ambiguity:
            out.append(("AMBIGUOUS_BACKBONE", nid, ambiguity))
    for e in edges:
        r = e.get("rel")
        if r and r not in KNOWN_RELS:
            out.append(("UNKNOWN_REL", e.get("from", "-"),
                        f"edge rel '{r}' is unknown — it is treated as a dependency edge"))
        source = nodes.get(e.get("from"), {})
        if r == "supports" and source.get("status") in {"null", "dead_end", "retracted"}:
            out.append(("STATUS_RELATION_MISMATCH", e.get("from", "-"),
                        f"{source.get('status')} node cannot support a claim; "
                        "use related/refutes only when warranted"))

    if concepts:
        load_bearing = set(cfg.render_types) | {"claim"}
        for nid, n in nodes.items():
            if (n.get("type") in load_bearing and not n.get("backbone")
                    and n.get("status") in (None, "current", "confirmed")):
                out.append(("NO_BACKBONE", nid,
                            f"{n.get('type')} declares no 'backbone' -> never drift-checked against a concept"))
    return out


# --------------------------------------------------------------------------- check

def compute_check(cfg, raw=None):
    """Return (problems, pending). problems = hard errors; pending = known-stale (status='stale').

    When ``raw`` is provided, every check uses that already-loaded graph snapshot. When omitted,
    this function loads once and reuses the result, preserving the existing call signature while
    avoiding mixed results if another process atomically replaces the graph during a check.
    """
    g = raw if raw is not None else load_raw(cfg)
    problems = list(structural_issues(cfg, raw=g))     # structural integrity first
    nodes, edges, concepts = load_graph(cfg, raw=g)
    down, up = build_adj(edges)
    pending = []
    dirty_roots = {}

    # An out-of-root node path is already reported by structural_issues above. Checking
    # is a reporting command, so the remaining checks skip that path rather than letting
    # the resolve guard abort the whole run.
    unsafe_paths = {nid for (code, nid, _detail) in problems if code == "INVALID_NODE_PATH"}

    def _node_path(nid, node):
        """The declared node path, or None when absent or not root-contained."""
        p = node.get("path")
        if not p or nid in unsafe_paths:
            return None
        return p

    # 1. file existence
    for nid, n in nodes.items():
        p = _node_path(nid, n)
        if p and not cfg.resolve(p).exists():
            problems.append(("MISSING_FILE", nid, p))

    # 2a. SILENT drift: node marked current but off one of its declared canonical bindings
    for nid, n in nodes.items():
        bindings, ambiguity = backbone_bindings(n, concepts)
        if ambiguity:
            problems.append(("AMBIGUOUS_BACKBONE", nid, ambiguity))
            continue
        for cname, bound in bindings.items():
            if cname not in concepts:
                problems.append(("UNKNOWN_BACKBONE_CONCEPT", nid,
                                 f"backbone binds unknown concept '{cname}'"))
                continue
            canon = concepts[cname].get("canonical")
            if n.get("status") == "current" and bound != canon:
                problems.append(("SILENT_DRIFT", nid,
                                 f"current but {cname}={bound} != {cname}={canon}"))

    # 2b. PENDING migration: node explicitly marked stale
    for nid, n in nodes.items():
        if n.get("status") == "stale":
            bindings, _ambiguity = backbone_bindings(n, concepts)
            movement = ", ".join(
                f"{name}:{bound} -> {concepts.get(name, {}).get('canonical', '?')}"
                for name, bound in sorted(bindings.items()))
            pending.append((nid, n.get("type", "?"),
                            movement,
                            n.get("path", "")))

    # 3. a CURRENT node must not depend on a RETIRED one
    for nid, n in nodes.items():
        for (u, r) in up.get(nid, []):
            ust = nodes.get(u, {}).get("status")
            if ust in RETIRED and n.get("status") == "current":
                problems.append(("READS_RETIRED", nid, f"depends on {ust} {u}"))

    # 3b. a live node must not depend on work that has not been executed yet.
    #     `planned` is a standard planning-mode status, so it produces no lint of
    #     its own; a settled result resting on it is the silent-staleness case
    #     this tool exists to catch.
    for nid, n in nodes.items():
        nst = n.get("status")
        if nst not in ("current", "confirmed"):
            continue
        for (u, r) in up.get(nid, []):
            if nodes.get(u, {}).get("status") in UNEXECUTED:
                problems.append(("DEPENDS_ON_PLANNED", nid,
                                 f"{nst} but depends on planned {u}"))

    # 4. a claim must not transitively depend on evidence from a conflicting canonical binding
    for dependent, claim in nodes.items():
        if claim.get("type") != "claim":
            continue
        for source in sorted(_ancestors(dependent, up)):
            evidence = nodes.get(source, {})
            conflicts = _backbone_conflicts(claim, evidence, concepts)
            if conflicts:
                detail = ", ".join(f"{name}: claim={cv}, {source}={ev}"
                                   for name, cv, ev in conflicts)
                problems.append(("CLAIM_CITES_OFFBACKBONE", dependent, detail))

    def _mtime(p):
        fp = cfg.resolve(p)
        return fp.stat().st_mtime if fp.exists() else None

    # 5. mtime staleness: a render-type node older than a file it (transitively) depends on
    #    (NOTE: mtime is unreliable after `git clone`/checkout; treat STALE_DATA below as primary.)
    for nid, n in nodes.items():
        if n.get("type") not in cfg.render_types:
            continue
        own_p = _node_path(nid, n)
        t = _mtime(own_p) if own_p else None
        if t is None:
            continue
        for u in _ancestors(nid, up):
            un = nodes.get(u, {})
            up_p = _node_path(u, un)
            if not up_p or un.get("status") == "deprecated" or un.get("type") not in cfg.input_types:
                continue
            tu = _mtime(up_p)
            if tu and tu > t + 2:
                problems.append(("STALE_RENDER", nid, f"rendered before upstream {u} changed ({up_p}) — re-render"))
                break

    # 6. content-hash staleness: a render's locked manifest inputs changed since the snapshot
    for nid, n in nodes.items():
        if n.get("type") not in cfg.render_types or not _node_path(nid, n):
            continue
        man = cfg.resolve(n["path"] + ".manifest.json")
        if not man.exists():
            problems.append(("MISSING_MANIFEST", nid,
                             f"{man.name} not found — run `provsleuth snapshot` after rendering"))
            continue
        try:
            rec = strict_json_loads(man.read_text(encoding="utf-8-sig"), man.name)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            problems.append(("INVALID_MANIFEST", nid, f"cannot parse {man.name}: {exc}"))
            continue
        if not isinstance(rec, dict) or not isinstance(rec.get("inputs"), list):
            problems.append(("INVALID_MANIFEST", nid, f"{man.name} needs an object with an inputs list"))
            continue

        # Historical manifests were unversioned and used SHA-1. Keep them readable so an
        # existing lock can be checked before migration, but never guess when a manifest uses an
        # explicit unknown schema or mixes legacy and current hash fields.
        if "schema_version" not in rec:
            hash_algorithm = "sha1"
            input_hash_field = "sha1"
            output_hash_field = "output_sha1"
            digest_length = 40
            mixed_output_field = "output_sha256"
            mixed_input_field = "sha256"
            legacy_manifest = True
        elif rec["schema_version"] == RENDER_MANIFEST_SCHEMA:
            hash_algorithm = "sha256"
            input_hash_field = "sha256"
            output_hash_field = "output_sha256"
            digest_length = 64
            mixed_output_field = "output_sha1"
            mixed_input_field = "sha1"
            legacy_manifest = False
        else:
            problems.append(("INVALID_MANIFEST", nid,
                             f"{man.name} has unsupported schema_version "
                             f"{rec.get('schema_version')!r}"))
            continue

        if mixed_output_field in rec or any(
                isinstance(inp, dict) and mixed_input_field in inp for inp in rec["inputs"]):
            problems.append(("INVALID_MANIFEST", nid,
                             f"{man.name} mixes {hash_algorithm} fields with "
                             f"{mixed_input_field}/{mixed_output_field}"))
            continue

        allowed_node_ids = (None, nid) if legacy_manifest else (nid,)
        if rec.get("node") not in allowed_node_ids:
            problems.append(("INVALID_MANIFEST", nid,
                             f"{man.name} belongs to {rec.get('node')}, not {nid}"))
        if rec.get("output") != n["path"]:
            problems.append(("INVALID_MANIFEST", nid,
                             f"{man.name} records output {rec.get('output')!r}, not {n['path']!r}"))

        node_bindings, node_ambiguity = backbone_bindings(n, concepts)
        manifest_bindings, manifest_ambiguity = backbone_bindings(
            {"backbone": rec.get("backbone")}, concepts)
        if node_ambiguity or manifest_ambiguity or manifest_bindings != node_bindings:
            problems.append(("MANIFEST_BACKBONE_MISMATCH", nid,
                             f"locked {rec.get('backbone')!r} != graph {n.get('backbone')!r}"))
            dirty_roots.setdefault(nid, f"canonical binding changed: {n['path']}")

        expected = set(expected_inputs(cfg, nodes, edges, nid))
        recorded = set()
        malformed_input = False
        for inp in rec["inputs"]:
            if (not isinstance(inp, dict) or not isinstance(inp.get("path"), str)
                    or not _valid_hex_digest(inp.get(input_hash_field), digest_length)):
                malformed_input = True
                continue
            if inp["path"] in recorded:
                malformed_input = True
            recorded.add(inp["path"])
        if malformed_input:
            problems.append(("INVALID_MANIFEST", nid,
                             f"{man.name} has a duplicate input or an input without path + "
                             f"valid {input_hash_field}"))
        if recorded != expected:
            missing = sorted(expected - recorded)
            extra = sorted(recorded - expected)
            detail = []
            if missing:
                detail.append("missing: " + ", ".join(missing))
            if extra:
                detail.append("undeclared: " + ", ".join(extra))
            problems.append(("MANIFEST_INPUT_MISMATCH", nid, "; ".join(detail)))

        output = cfg.resolve(n["path"])
        if not _valid_hex_digest(rec.get(output_hash_field), digest_length):
            problems.append(("INVALID_MANIFEST", nid,
                             f"{man.name} has no valid {output_hash_field}"))
        elif output.exists() and _file_hash(output, hash_algorithm) != rec[output_hash_field]:
            problems.append(("OUTPUT_DRIFT", nid,
                             f"output changed since lock: {n['path']} — re-render + re-snapshot"))
            dirty_roots.setdefault(nid, f"output content changed: {n['path']}")

        for inp in rec["inputs"]:
            if (not isinstance(inp, dict) or not isinstance(inp.get("path"), str)
                    or not _valid_hex_digest(inp.get(input_hash_field), digest_length)):
                continue
            ip = cfg.resolve(inp["path"]) if not os.path.isabs(inp["path"]) else Path(inp["path"])
            if not Path(ip).exists():
                problems.append(("STALE_DATA", nid, f"locked input now missing: {inp['path']}"))
                for candidate, inode in nodes.items():
                    if inode.get("path") == inp["path"]:
                        dirty_roots.setdefault(candidate, f"locked input missing: {inp['path']}")
                break
            if _file_hash(ip, hash_algorithm) != inp[input_hash_field]:
                problems.append(("STALE_DATA", nid, f"input changed since lock: {inp['path']} — re-render + re-snapshot"))
                for candidate, inode in nodes.items():
                    if inode.get("path") == inp["path"]:
                        dirty_roots.setdefault(candidate, f"content changed: {inp['path']}")
                break

    # 7. a dirty file version invalidates every declared downstream result, not only its render.
    seen_stale = set()
    for root, reason in sorted(dirty_roots.items()):
        for nid, _rel, _parent in closure(root, down):
            if nid in seen_stale or nodes.get(nid, {}).get("status") not in (None, "current", "confirmed"):
                continue
            seen_stale.add(nid)
            problems.append(("UPSTREAM_STALE", nid, f"downstream of {root}: {reason}"))

    return problems, pending


# --------------------------------------------------------------------------- log

def log_entry(cfg, entry, update=False):
    """Append a lab-notebook node (+ optional edges) under an exclusive graph lock."""
    try:
        with _graph_lock(cfg.graph_path):
            return _log_entry_locked(cfg, entry, update=update)
    except TimeoutError as exc:
        return False, str(exc)


def _log_entry_locked(cfg, entry, update=False):
    """Validate and atomically write one entry. Caller must hold the graph lock."""
    raw = load_raw(cfg)
    existing_issues = structural_issues(cfg, raw=raw)
    if existing_issues:
        kind, nid, detail = existing_issues[0]
        return False, f"graph is already invalid: {kind} at {nid}: {detail}"
    if not isinstance(entry, dict) or "node" not in entry:
        return False, "entry must be an object with a 'node'"
    node = entry["node"]
    new_edges = entry.get("edges", [])
    if (not isinstance(node, dict) or not isinstance(node.get("id"), str)
            or not node["id"] or not isinstance(node.get("type"), str) or not node["type"]):
        return False, "entry.node needs at least id + type"
    if not isinstance(new_edges, list):
        return False, "entry.edges must be a list"
    for edge in new_edges:
        if (not isinstance(edge, dict)
                or not all(isinstance(edge.get(k), str) and edge[k] for k in ("from", "to", "rel"))):
            return False, "each entry edge needs non-empty string from + to + rel"
    ids = {n["id"] for n in raw["nodes"]}
    if node["id"] in ids and not update:
        return False, f"node {node['id']} already exists (use --update to overwrite)"
    candidate = dict(raw)
    candidate["nodes"] = [n for n in raw["nodes"] if n["id"] != node["id"]]
    candidate["nodes"].append(node)
    candidate["edges"] = list(raw.get("edges", []))
    have = {(e["from"], e["to"], e["rel"]) for e in candidate["edges"]
            if all(k in e for k in ("from", "to", "rel"))}
    added = 0
    for e in new_edges:
        k = (e["from"], e["to"], e["rel"])
        if k not in have:
            candidate["edges"].append(e)
            have.add(k)
            added += 1
    issues = structural_issues(cfg, raw=candidate)
    if issues:
        kind, nid, detail = issues[0]
        return False, f"entry rejected: {kind} at {nid}: {detail}"

    graph_path = Path(cfg.graph_path)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=graph_path.parent,
                                         prefix=graph_path.name + ".", suffix=".tmp",
                                         delete=False) as handle:
            temp_path = Path(handle.name)
            json.dump(candidate, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, graph_path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()
    return True, (f"logged {node['id']} [{node.get('type','?')}/{node.get('status','?')}] + {added} edge(s) "
                  f"-> {len(candidate['nodes'])} nodes / {len(candidate['edges'])} edges")
