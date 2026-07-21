"""provsleuth command-line interface."""
from __future__ import annotations
import argparse
import builtins
import copy
import json
import os
import stat
import sys
import unicodedata
import uuid
from collections import defaultdict
from importlib import resources
from pathlib import Path

from . import __version__
from .assessment import (SCHEMA_VERSION as ASSESSMENT_SCHEMA_VERSION,
                         SUPPORTED_SCHEMA_VERSIONS as SUPPORTED_ASSESSMENT_SCHEMA_VERSIONS,
                         AssessmentError, append_assessment, append_review_transition,
                         create_assessment, evaluate_assessment, load_assessments)
from .config import CONFIG_NAME, LEGACY_CONFIG_NAME, load_config, strict_json_loads
from .deliberation import (DeliberationError, append_record as append_deliberation_record,
                           create_ballot as create_deliberation_ballot,
                           create_phase_decision as create_deliberation_phase_decision,
                           create_proposal as create_deliberation_proposal,
                           deliberation_status, evaluate_candidate_set,
                           freeze_candidate_set)
from .engine import (ANNOT_RELS, GraphError, compute_check, downstream, impact,
                     lint_issues, load_graph, load_raw, log_entry, upstream)
from .events import EventError, _event_lock, run_command
from .graph_changes import (GraphChangeError, apply_graph_change_proposal,
                            build_graph_change_proposal, canonical_graph_hash)
from .graphrag import (DEFAULT_MAX_CONTEXT_BYTES, GraphRAGError,
                       bounded_context, build_projection)
from .logic import (PLAN_REQUEST_SCHEMA, SELECTION_SCHEMA, LogicError, append_derivation,
                    configured_logic_assets, create_derivation,
                    create_derivation_from_bindings,
                    create_derivation_from_evidence_plan, evaluate_derivation,
                    load_derivations, load_logic_asset, resolve_claim_evidence_plan)
from .method_assessment import (
    MethodAssessmentError, append_method_assessment, append_method_review_transition,
    create_method_assessment, evaluate_method_assessment, load_method_assessments,
    method_assessment_statuses,
)
from .report import build_fatal_report, build_report, dumps_report
from .release import (ReleaseError, create_release_manifest, diff_release_manifests,
                      verify_release_manifest)
from .replay import STAGE_REPLAY_SCHEMA, ReplayError, replay_run
from .semantics import (MAPPING_SCHEMA, POLICY_SCHEMA, SemanticError, append_mapping,
                        append_mapping_review, append_semantic_policy,
                        canonical_bytes,
                        configured_semantic_assets, create_mapping_proposal,
                        create_ontology_lock, create_semantic_policy,
                        current_mapping_leaves, detect_mapping_conflicts,
                        evaluate_active_semantic_policy,
                        _evaluate_active_semantic_policy_from_snapshot,
                        evaluate_mapping, evaluate_mappings,
                        _evaluate_mappings_from_assets,
                        _evaluate_semantic_policy_from_snapshot,
                        evaluate_semantic_policy,
                        fsync_semantic_directory,
                        load_mappings, load_policies,
                        search_ontology_candidates, stable_semantic_file_bytes)
from .snapshot import snapshot
from .verify import run_verifiers
from .view import render_view

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

GLYPH = {"planned": "PLANNED", "current": "LIVE", "confirmed": "CONFIRMED", "null": "NULL", "dead_end": "DEAD-END",
         "retracted": "RETRACTED", "superseded": "SUPERSEDED", "stale": "STALE", "deprecated": "DEPRECATED"}
JOURNAL_ORDER = ["planned", "current", "confirmed", "null", "dead_end", "retracted", "superseded", "stale", "deprecated"]
MAX_CLI_JSON_INPUT_BYTES = 8 * 1024 * 1024
MAX_CLI_TEXT_PUBLICATION_BYTES = 8 * 1024 * 1024


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
        except DeliberationError as exc:
            report = build_fatal_report(exc, strict=args.strict, code="DELIBERATION_ERROR")
        except OSError as exc:
            report = build_fatal_report(exc, strict=args.strict, code="IO_ERROR")
        except SystemExit as exc:
            report = build_fatal_report(exc, strict=args.strict, code="CONFIG_ERROR")
        if args.json:
            sys.stdout.write(dumps_report(report))
            return report["exit_code"]
        if report["fatal"]:
            print(f"provsleuth check --strict: {report['fatal']['code']} - {report['fatal']['detail']}",
                  file=sys.stderr)
            return 2
        if report["ok"]:
            print("provsleuth check --strict: OK within declared/partial runtime-capture scope - "
                  "no blocking graph, receipt, lint, or pending findings.")
            return 0
        print(f"provsleuth check --strict: {report['summary']['blocking']} blocking finding(s):\n")
        for item in report["findings"]:
            if item["blocking"]:
                node = item["node_id"] or "-"
                print(f"  [{item['severity'].upper():7s} {item['code']:28s}] {node:30s} {item['detail']}")
        return report["exit_code"]

    cfg = _cfg(args)
    problems, pending = compute_check(cfg)
    if pending:
        print(f"PENDING MIGRATION — {len(pending)} node(s) flagged stale (run `provsleuth impact` for the full list):")
        for nid, typ, mv, path in pending:
            print(f"  [{typ:8s}] {nid:34s} {mv}{('  | ' + path) if path else ''}")
        print()
    if not problems:
        print("provsleuth check: OK — no silent drift, no broken structure, all paths exist."
              + (f" ({len(pending)} known-pending above.)" if pending else ""))
        return 0
    print(f"provsleuth check: {len(problems)} ERROR(s):\n")
    for kind, nid, detail in problems:
        print(f"  [{kind:24s}] {nid:30s} {detail}")
    return 1


def cmd_lint(args):
    cfg = _cfg(args)
    warnings = lint_issues(cfg)
    if not warnings:
        print("provsleuth lint: OK — vocabulary is standard and load-bearing nodes are annotated.")
        return 0
    print(f"provsleuth lint: {len(warnings)} warning(s):\n")
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
        print(f"unknown node: {args.node}")
        return 1
    print(f"DOWNSTREAM of {args.node} (depend on it):")
    for nb, r, n in res:
        print(f"  {nb:34s} [{n.get('type','?')}/{n.get('status','?')}]  via {r}")
    return 0


def cmd_upstream(args):
    cfg = _cfg(args)
    res = upstream(cfg, args.node)
    if res is None:
        print(f"unknown node: {args.node}")
        return 1
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
        print(f"unknown concept: {concept} (known: {info})")
        return 1
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
        print(f"unknown node: {args.node}")
        return 1
    print(json.dumps(n, indent=2, ensure_ascii=False))
    print("\n  edges:")
    for e in edges:
        if e.get("from") == args.node or e.get("to") == args.node:
            print(f"    {e['from']} --{e['rel']}--> {e['to']}")
    return 0


def cmd_log(args):
    cfg = _cfg(args)
    entry = _read_cli_json_object(
        args.entry, "log entry", GraphError, limit=MAX_CLI_JSON_INPUT_BYTES,
    )
    ok, msg = log_entry(cfg, entry, update=args.update)
    print(msg)
    return 0 if ok else 1


def _read_deliberation_object(path_value, label):
    return _read_cli_json_object(
        path_value, label, DeliberationError, limit=MAX_CLI_JSON_INPUT_BYTES,
    )


def _deliberation_record_output(cfg, document, path):
    record_id = (
        document.get("proposal_id") or document.get("candidate_set_id")
        or document.get("ballot_id") or document.get("decision_id")
    )
    return {
        "record": document,
        "record_id": record_id,
        "stored_at": path.relative_to(cfg.deliberation_path).as_posix(),
        "automatic_activation": False,
        "human_activation_required": True,
    }


def cmd_deliberate_propose(args):
    cfg = _cfg(args)
    document = create_deliberation_proposal(
        cfg,
        _read_deliberation_object(args.entry, "deliberation proposal request"),
        actor=args.actor,
        independence_group=args.independence_group,
    )
    path = append_deliberation_record(cfg, document)
    output = _deliberation_record_output(cfg, document, path)
    if args.json:
        _write_json(output)
    else:
        print(f"provsleuth deliberate-propose: {document['proposal_id']}")
        print(f"  candidate: {document['candidate_id']}")
        print("  state: proposed only; no semantic or rule activation")
    return 0


def cmd_deliberate_freeze(args):
    cfg = _cfg(args)
    document = freeze_candidate_set(
        cfg,
        _read_deliberation_object(args.entry, "candidate-set freeze request"),
        actor=args.actor,
    )
    path = append_deliberation_record(cfg, document)
    output = _deliberation_record_output(cfg, document, path)
    if args.json:
        _write_json(output)
    else:
        print(f"provsleuth deliberate-freeze: {document['candidate_set_id']}")
        print(
            f"  {len(document['candidate_ids'])} exact candidate(s) from "
            f"{len(document['proposal_ids'])} proposal(s)"
        )
        print(
            "  state: frozen for attributed review; reviewer identity and "
            "independence labels are self-asserted"
        )
    return 0


def cmd_deliberate_ballot(args):
    cfg = _cfg(args)
    document = create_deliberation_ballot(
        cfg,
        _read_deliberation_object(args.entry, "deliberation ballot request"),
        actor=args.actor,
        independence_group=args.independence_group,
    )
    path = append_deliberation_record(cfg, document)
    output = _deliberation_record_output(cfg, document, path)
    if args.json:
        _write_json(output)
    else:
        print(f"provsleuth deliberate-ballot: {document['ballot_id']}")
        print(f"  role: {document['role']}; set: {document['candidate_set_id']}")
        print("  state: attributed ballot; not a truth vote or activation")
    return 0


def cmd_deliberate_decide(args):
    cfg = _cfg(args)
    document = create_deliberation_phase_decision(
        cfg,
        _read_deliberation_object(args.entry, "deliberation phase-decision request"),
        actor=args.actor,
    )
    path = append_deliberation_record(cfg, document)
    output = _deliberation_record_output(cfg, document, path)
    output["human_identity_authenticated"] = False
    if args.json:
        _write_json(output)
    else:
        print(f"provsleuth deliberate-decide: {document['decision_id']}")
        print(
            f"  decision: {document['decision']}; candidate: "
            f"{document['candidate_id']}"
        )
        print(
            "  state: attributed routing record only; no authenticated-human claim "
            "and no graph, semantic, or rule activation"
        )
    return 0


def cmd_deliberations(args):
    cfg = _cfg(args)
    output = (
        evaluate_candidate_set(cfg, args.candidate_set_id)
        if args.candidate_set_id else deliberation_status(cfg)
    )
    if args.json:
        _write_json(output)
    elif args.candidate_set_id:
        print(f"provsleuth deliberations: {output['status']}")
        print(f"  set: {output['candidate_set_id']}")
        print(f"  recommendation: {output['recommended_candidate_id'] or 'none'}")
        print("  human activation required: yes")
    else:
        print(
            f"provsleuth deliberations: {len(output['panels'])} frozen panel(s), "
            f"{len(output['open_groups'])} open group(s), integrity {output['integrity']}"
        )
        for item in output["panels"]:
            print(
                f"  {item['candidate_set_id']}  {item['phase']}  {item['status']}  "
                f"recommendation={item['recommended_candidate_id'] or 'none'}"
            )
    if args.candidate_set_id:
        return {
            "recommended_for_human_review": 0,
            "contested": 1,
            "insufficient_review": 1,
            "blocked": 2,
        }[output["status"]]
    return 0 if output.get("integrity", "ok") == "ok" else 2


def _read_cli_json_object(path_value, label, error_type, *, limit):
    """Read one stable, bounded JSON object and translate failures for its CLI surface."""
    try:
        payload = stable_semantic_file_bytes(path_value, label, limit)
        value = strict_json_loads(payload.decode("utf-8-sig"), str(path_value))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError,
            RecursionError, TypeError, SemanticError) as exc:
        raise error_type(f"cannot read {label} {path_value}: {exc}") from exc
    if not isinstance(value, dict):
        raise error_type(f"{label} must contain one JSON object")
    return value


def _read_json_object(path_value, label):
    return _read_cli_json_object(
        path_value, label, AssessmentError, limit=MAX_CLI_JSON_INPUT_BYTES,
    )


def _write_json(value):
    sys.stdout.write(json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ) + "\n")


def _terminal_text(value, *, allow_newlines=False):
    """Render untrusted single-line text without emitting terminal controls."""
    return "".join(
        f"\\u{ord(char):04x}"
        if ((ord(char) < 0x20 and not (allow_newlines and char == "\n"))
            or 0x7F <= ord(char) <= 0x9F
            or 0xD800 <= ord(char) <= 0xDFFF
            or unicodedata.category(char) == "Cf") else char
        for char in str(value)
    )


def _terminal_print(*values, **kwargs):
    """Print plain-text CLI output without forwarding terminal control bytes."""
    builtins.print(
        *(_terminal_text(value, allow_newlines=True) for value in values), **kwargs,
    )


# All JSON output bypasses print and is encoded by the JSON serializer.  Shadowing
# print here gives every human-readable command one centralized safety boundary,
# including older graph, assessment, logic, and journal paths.
print = _terminal_print


def _read_semantic_object(path_value, label):
    return _read_cli_json_object(
        path_value, label, SemanticError, limit=2 * 1024 * 1024,
    )


def _semantic_output_path(cfg, value):
    if (not isinstance(value, str) or len(value) > 4_096
            or any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value)):
        raise SemanticError("semantic output path must be bounded text without controls")
    try:
        candidate = Path(value)
        resolved = (
            candidate.resolve() if candidate.is_absolute()
            else (cfg.base / candidate).resolve()
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise SemanticError(f"invalid semantic output path: {value!r}") from exc
    try:
        resolved.relative_to(cfg.base)
    except ValueError as exc:
        raise SemanticError(
            f"semantic output path escapes the project config directory: {value}"
        ) from exc
    configured = {
        os.path.normcase(str(Path(path).resolve()))
        for path in cfg.semantic_ontology_lock_paths
    }
    if os.path.normcase(str(resolved)) not in configured:
        raise SemanticError(
            "ontology-lock output must exactly match a project-local path already listed in "
            "semantics.ontology_locks"
        )
    return resolved


def _atomic_create_text(destination: Path, content: str) -> bool:
    """Create one immutable text asset atomically; never replace existing bytes."""
    payload = content.encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)

    def verify_parent():
        absolute = Path(os.path.abspath(destination.parent))
        try:
            resolved = destination.parent.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SemanticError(
                f"ontology-lock output parent is unavailable: {destination.parent}"
            ) from exc
        if os.path.normcase(str(absolute)) != os.path.normcase(str(resolved)):
            raise SemanticError("ontology-lock output parent must not traverse a link")

    verify_parent()
    if destination.exists():
        existing = stable_semantic_file_bytes(
            destination, "existing ontology lock", 32 * 1024 * 1024,
        )
        if existing == payload:
            return False
        raise SemanticError(
            f"refusing to replace immutable ontology lock {destination}; configure a new path"
        )
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        verify_parent()
        try:
            os.link(temporary, destination)
            fsync_semantic_directory(destination.parent)
        except FileExistsError:
            existing = stable_semantic_file_bytes(
                destination, "racing ontology lock", 32 * 1024 * 1024,
            )
            if existing == payload:
                return False
            raise SemanticError(
                f"refusing to overwrite ontology lock that appeared during publication: "
                f"{destination}"
            )
        except OSError as exc:
            raise SemanticError(f"cannot atomically create ontology lock: {exc}") from exc
    finally:
        try:
            temporary.unlink()
            fsync_semantic_directory(destination.parent)
        except FileNotFoundError:
            pass
    return True


def cmd_lock_ontology(args):
    """Create an exact-byte local ontology lock without fetching or parsing remote data."""
    cfg = _cfg(args)
    entry = _read_semantic_object(args.entry, "ontology-lock request")
    required = {"ontology_id", "ontology_iri", "version", "documents", "index"}
    optional = {
        "version_iri", "license_iri", "imports", "declared_imports_available",
    }
    if required - set(entry) or set(entry) - required - optional:
        raise SemanticError(
            "ontology-lock request must contain ontology_id, ontology_iri, version, documents, "
            "and index, plus only documented optional metadata"
        )
    output = _semantic_output_path(cfg, args.output)
    lock = create_ontology_lock(
        ontology_id=entry["ontology_id"],
        ontology_iri=entry["ontology_iri"],
        version=entry["version"],
        version_iri=entry.get("version_iri"),
        license_iri=entry.get("license_iri"),
        imports=entry.get("imports", []),
        declared_imports_available=entry.get("declared_imports_available", False),
        documents=entry["documents"],
        index_path=entry["index"],
        base=output.parent,
        max_total_bytes=cfg.semantic_max_ontology_bytes,
    )
    content = json.dumps(lock, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    _atomic_create_text(output, content)
    result = {"path": str(output), "lock": lock}
    if args.json:
        _write_json(result)
    else:
        print(f"provsleuth lock-ontology: {lock['id']}")
        print(f"  path: {_terminal_text(output)}")
        print(f"  exact-byte documents: {len(lock['documents'])}")
    return 0


def cmd_ontology_candidates(args):
    candidates = search_ontology_candidates(
        _cfg(args), args.query, language=args.language, limit=args.limit,
    )
    if args.json:
        _write_json(candidates)
    else:
        print(
            f"provsleuth ontology-candidates: {len(candidates['candidates'])} shown / "
            f"{candidates['total_match_count']} exact match(es)"
        )
        print(f"  candidate set: {candidates['id']}")
        print("  source: project-supplied index; RDF/OWL extraction is not verified")
        if candidates["truncated"]:
            print(
                "  warning: result is truncated; increase --limit and reuse the same "
                "--limit/--language on map-term"
            )
        for item in candidates["candidates"]:
            deprecated = " deprecated" if item["deprecated"] else ""
            print(
                f"  {_terminal_text(item['iri'])}  "
                f"[{item['matched_on']}{deprecated}]  {_terminal_text(item['label'])}"
            )
    return 0


def _mapping_output(document, evaluation, path=None, *, is_current=True):
    return {
        "id": document["id"],
        "schema_version": document["schema_version"],
        "path": str(path) if path is not None else None,
        "recorded_at": document["recorded_at"],
        "subject": document["subject"],
        "agent_input": document["agent_input"],
        "mechanical_snapshot": document["mechanical_snapshot"],
        "review": document["review"],
        "stored_derived": document["derived"],
        "current_derived": evaluation,
        "is_current": bool(is_current),
    }


def cmd_map_term(args):
    """Append one agent-authored mapping proposal grounded in a locked candidate set."""
    cfg = _cfg(args)
    _existing, issues = load_mappings(cfg)
    if issues:
        raise SemanticError(
            "mapping store integrity failed before proposal: "
            + "; ".join(f"{item['path']}: {item['detail']}" for item in issues)
        )
    entry = _read_semantic_object(args.entry, "semantic-mapping proposal")
    if set(entry) != {"terminology_id", "term_id", "agent_input"}:
        raise SemanticError(
            "semantic-mapping proposal must contain exactly terminology_id, term_id, and "
            "agent_input; snapshots, review, and derived fields are computed by ProvSleuth"
        )
    document = create_mapping_proposal(
        cfg, entry["terminology_id"], entry["term_id"], entry["agent_input"],
        actor=args.actor, language=args.language, limit=args.limit,
    )
    path = append_mapping(cfg, document)
    evaluation = evaluate_mapping(cfg, document)
    output = _mapping_output(document, evaluation, path)
    if args.json:
        _write_json(output)
    else:
        print(f"provsleuth map-term: recorded {document['id']}")
        print(f"  subject: {document['subject']['terminology_id']} / "
              f"{document['subject']['term_id']}")
        print(f"  relation: {document['agent_input']['relation']}")
        print("  review: proposed (not active policy)")
        for finding in evaluation["findings"]:
            print(
                f"  {finding['severity']}: {finding['code']} - "
                f"{_terminal_text(finding['detail'])}"
            )
    return 1 if any(item["severity"] == "error" for item in evaluation["findings"]) else 0


def _mapping_current_ids(documents):
    return {item["id"] for item in current_mapping_leaves(documents)}


def _integrity_block_mapping(evaluation, issues):
    if not issues:
        return evaluation
    result = dict(evaluation)
    result["eligible_for_policy"] = False
    result["findings"] = [*evaluation.get("findings", []), {
        "code": "SEMANTIC_MAPPING_INTEGRITY",
        "severity": "error",
        "detail": "mapping store integrity failed; no mapping may enter active policy",
    }]
    return result


def cmd_mappings(args):
    cfg = _cfg(args)
    documents, issues = load_mappings(cfg)
    current_ids = _mapping_current_ids(documents)
    conflicts = detect_mapping_conflicts(documents) if not issues else {}
    selected = []
    for document in sorted(documents, key=lambda item: item["id"]):
        is_current = document["id"] in current_ids
        if not args.all and not is_current:
            continue
        selected.append(document)
    items = []
    for document, evaluation in zip(
            selected, evaluate_mappings(cfg, selected, conflicts=conflicts)):
        is_current = document["id"] in current_ids
        evaluation = _integrity_block_mapping(evaluation, issues)
        if args.state and evaluation["effective_review_state"] != args.state:
            continue
        items.append(_mapping_output(document, evaluation, is_current=is_current))
    output = {
        "schema_version": "claimtrace.semantic-mapping-list/1",
        "mapping_schema_version": MAPPING_SCHEMA,
        "integrity": "error" if issues else "ok",
        "issues": issues,
        "items": items,
    }
    if args.json:
        _write_json(output)
    else:
        print(f"provsleuth mappings: {len(items)} mapping record(s), "
              f"integrity {output['integrity']}")
        for item in items:
            current = item["current_derived"]
            print(
                f"  {item['id']}  {current['effective_review_state']}  "
                f"{item['agent_input']['relation']}  "
                f"{item['subject']['terminology_id']}/{item['subject']['term_id']}"
            )
            print(
                "    current: " + ("yes" if item["is_current"] else "no")
                + "; stale: " + ("yes" if current["stale"] else "no")
                + "; eligible for policy: "
                + ("yes" if current["eligible_for_policy"] else "no")
            )
            for finding in current["findings"]:
                print(
                    f"    {finding['severity']}: {finding['code']} - "
                    f"{_terminal_text(finding['detail'])}"
                )
        for issue in issues:
            print(
                f"  error: {issue['code']} - {_terminal_text(issue['path'])}: "
                f"{_terminal_text(issue['detail'])}"
            )
    return 2 if issues else 0


def cmd_review_mapping(args):
    cfg = _cfg(args)
    document, path = append_mapping_review(
        cfg, args.mapping_id, args.state, actor=args.actor,
    )
    documents, issues = load_mappings(cfg)
    if issues:
        raise SemanticError(
            "mapping store integrity failed after review: "
            + "; ".join(f"{item['path']}: {item['detail']}" for item in issues)
        )
    conflicts = detect_mapping_conflicts(documents)
    evaluation = evaluate_mapping(
        cfg, document, conflict_ids=conflicts.get(document["id"]),
    )
    output = _mapping_output(document, evaluation, path)
    if args.json:
        _write_json(output)
    else:
        print(f"provsleuth review-mapping: {document['id']}")
        print(
            f"  {args.mapping_id} -> {args.state} by {_terminal_text(args.actor)}"
        )
        print(
            "  eligible for policy: "
            + ("yes" if evaluation["eligible_for_policy"] else "no")
        )
        print("  active policy: no (compile and pin a policy explicitly)")
        for finding in evaluation["findings"]:
            print(
                f"  {finding['severity']}: {finding['code']} - "
                f"{_terminal_text(finding['detail'])}"
            )
    return 1 if any(item["severity"] == "error" for item in evaluation["findings"]) else 0


def cmd_compile_semantic_policy(args):
    """Append one explicit release from caller-selected accepted mapping leaves."""
    cfg = _cfg(args)
    entry = _read_semantic_object(args.entry, "semantic-policy request")
    if set(entry) != {"mapping_ids", "note"}:
        raise SemanticError(
            "semantic-policy request must contain exactly mapping_ids and note"
        )
    document = create_semantic_policy(
        cfg, entry["mapping_ids"], actor=args.actor, note=entry["note"],
    )
    path = append_semantic_policy(cfg, document)
    evaluation = evaluate_semantic_policy(cfg, document)
    output = {
        "id": document["id"],
        "schema_version": document["schema_version"],
        "path": str(path),
        "policy": document,
        "evaluation": evaluation,
        "activation_instruction": {
            "config_field": "semantics.active_policy",
            "value": document["id"],
            "automatic": False,
        },
    }
    if args.json:
        _write_json(output)
    else:
        print(f"provsleuth compile-semantic-policy: recorded {document['id']}")
        print(f"  mappings: {len(document['mapping_ids'])}")
        print("  active: no")
        print("  currently valid: " + ("yes" if evaluation["valid"] else "no"))
        print(
            "  activation requires a separate reviewed config change: "
            f"semantics.active_policy = {document['id']}"
        )
        for finding in evaluation["findings"]:
            print(
                f"  {finding['severity']}: {finding['code']} - "
                f"{_terminal_text(finding['detail'])}"
            )
    return 0 if evaluation["valid"] else 1


def _semantic_mapping_status(cfg, documents, issues, assets=None):
    current_ids = _mapping_current_ids(documents)
    conflicts = detect_mapping_conflicts(documents) if not issues else {}
    selected = sorted(documents, key=lambda item: item["id"])
    items = []
    evaluations = (
        evaluate_mappings(cfg, selected, conflicts=conflicts)
        if assets is None
        else _evaluate_mappings_from_assets(selected, assets, conflicts=conflicts)
    )
    for document, evaluation in zip(selected, evaluations):
        items.append(_mapping_output(
            document,
            _integrity_block_mapping(evaluation, issues),
            is_current=document["id"] in current_ids,
        ))
    return items


def cmd_semantic_status(args):
    """Recompute semantic assets, reviews, releases, and explicit activation."""
    cfg = _cfg(args)
    findings = []
    assets_output = None
    assets_snapshot = None
    try:
        assets = configured_semantic_assets(cfg)
        assets_snapshot = assets
        assets_output = {
            "terminology_ids": sorted(assets["terminologies"]),
            "ontology_locks": [
                {
                    "id": lock["id"],
                    "ontology_id": lock["ontology_id"],
                    "version": lock["version"],
                }
                for lock in sorted(
                    assets["ontology_locks"].values(), key=lambda item: item["id"]
                )
            ],
        }
    except SemanticError as exc:
        findings.append({
            "code": "SEMANTIC_ASSET_INTEGRITY",
            "severity": "error",
            "detail": str(exc),
        })

    mappings, mapping_issues = load_mappings(cfg)
    for issue in mapping_issues:
        findings.append({
            "code": issue["code"], "severity": "error",
            "detail": f"{issue['path']}: {issue['detail']}",
        })
    mapping_items = _semantic_mapping_status(
        cfg, mappings, mapping_issues, assets=assets_snapshot,
    )
    for item in mapping_items:
        current = item["current_derived"]
        if (item["is_current"]
                and current["effective_review_state"] in {"proposed", "accepted", "contested"}):
            findings.extend(copy.deepcopy(current.get("findings", [])))

    policies, policy_issues = load_policies(cfg)
    for issue in policy_issues:
        findings.append({
            "code": issue["code"], "severity": "error",
            "detail": f"{issue['path']}: {issue['detail']}",
        })
    active = (
        evaluate_active_semantic_policy(cfg)
        if assets_snapshot is None
        else _evaluate_active_semantic_policy_from_snapshot(
            cfg, assets=assets_snapshot, policies=policies,
            policy_issues=policy_issues, mappings=mappings,
            mapping_issues=mapping_issues,
        )
    )
    active_id = (active.get("policy") or {}).get("id")
    requested_evaluation = None
    if args.policy:
        requested = next(
            (document for document in policies if document["id"] == args.policy), None,
        )
        if requested is None:
            findings.append({
                "code": "SEMANTIC_POLICY_NOT_FOUND",
                "severity": "error",
                "detail": f"requested semantic policy is unavailable: {args.policy}",
            })
        elif requested["id"] == active_id:
            requested_evaluation = active.get("evaluation")
        else:
            requested_evaluation = (
                evaluate_semantic_policy(cfg, requested)
                if assets_snapshot is None
                else _evaluate_semantic_policy_from_snapshot(
                    cfg, requested, assets=assets_snapshot, policies=policies,
                    policy_issues=policy_issues, mappings=mappings,
                    mapping_issues=mapping_issues,
                )
            )
            findings.extend(requested_evaluation["findings"])
    policy_items = [
        {
            "policy": document,
            "evaluation": (
                active.get("evaluation") if document["id"] == active_id
                else requested_evaluation if document["id"] == args.policy
                else None
            ),
        }
        for document in sorted(policies, key=lambda item: item["id"])
    ]
    findings.extend(active["findings"])
    if cfg.require_active_semantic_policy and not (
            active.get("evaluation") and active["evaluation"].get("active")):
        findings.append({
            "code": "ACTIVE_SEMANTIC_POLICY_REQUIRED",
            "severity": "error",
            "detail": "project policy requires one valid explicitly configured semantic release",
        })

    snapshot_problems = []
    if assets_snapshot is None:
        snapshot_problems.append(
            "a coherent semantic asset snapshot was unavailable"
        )
    if assets_snapshot is not None:
        try:
            if canonical_bytes(configured_semantic_assets(cfg)) != canonical_bytes(assets_snapshot):
                snapshot_problems.append("semantic assets changed during status evaluation")
        except SemanticError:
            snapshot_problems.append("semantic assets became unavailable during status evaluation")
    final_mappings, final_mapping_issues = load_mappings(cfg)
    final_policies, final_policy_issues = load_policies(cfg)
    if ([item["id"] for item in final_mappings] != [item["id"] for item in mappings]
            or canonical_bytes(final_mapping_issues) != canonical_bytes(mapping_issues)
            or [item["id"] for item in final_policies] != [item["id"] for item in policies]
            or canonical_bytes(final_policy_issues) != canonical_bytes(policy_issues)):
        snapshot_problems.append("semantic stores changed during status evaluation")
    active_evaluation = active.get("evaluation")
    if active_evaluation is not None and active_evaluation.get("active"):
        policy_ids = {item["id"] for item in policies}
        mapping_by_id = {
            item["id"]: item["current_derived"] for item in mapping_items
        }
        if active_evaluation.get("policy_id") not in policy_ids:
            snapshot_problems.append("active policy disagrees with the listed policy snapshot")
        if any(
                mapping_id not in mapping_by_id
                or not mapping_by_id[mapping_id].get("eligible_for_policy")
                for mapping_id in active_evaluation.get("active_mapping_ids", [])):
            snapshot_problems.append("active mappings disagree with listed mapping evaluations")
    if snapshot_problems:
        snapshot_finding = {
            "code": "SEMANTIC_STATUS_SNAPSHOT_CHANGED",
            "severity": "error",
            "detail": "; ".join(snapshot_problems),
        }
        findings.append(snapshot_finding)
        active = copy.deepcopy(active)
        active["findings"] = [*active.get("findings", []), snapshot_finding]
        if active.get("evaluation") is not None:
            active["evaluation"]["active"] = False
            active["evaluation"]["valid"] = False
            active["evaluation"]["active_mapping_ids"] = []
            active["evaluation"]["findings"] = [
                *active["evaluation"].get("findings", []), snapshot_finding,
            ]
        for item in policy_items:
            if item.get("evaluation") is None:
                continue
            item["evaluation"] = copy.deepcopy(item["evaluation"])
            item["evaluation"]["active"] = False
            item["evaluation"]["valid"] = False
            item["evaluation"]["active_mapping_ids"] = []
            item["evaluation"]["findings"] = [
                *item["evaluation"].get("findings", []), snapshot_finding,
            ]
    has_error = any(item["severity"] == "error" for item in findings)
    output = {
        "schema_version": "claimtrace.semantic-status/1",
        "mapping_schema_version": MAPPING_SCHEMA,
        "policy_schema_version": POLICY_SCHEMA,
        "ok": not has_error,
        "assets": assets_output,
        "mappings": {
            "integrity": "error" if mapping_issues else "ok",
            "items": mapping_items,
        },
        "policies": {
            "integrity": "error" if policy_issues else "ok",
            "items": policy_items,
        },
        "active_policy": active,
        "require_active_policy": cfg.require_active_semantic_policy,
        "findings": findings,
    }
    if args.json:
        _write_json(output)
    else:
        displayed_active_id = (
            active["policy"]["id"]
            if active.get("evaluation") and active["evaluation"].get("active")
            else "none"
        )
        print(
            "provsleuth semantic-status: "
            f"{'OK' if output['ok'] else 'BLOCKED'}; "
            f"{len(mapping_items)} mapping record(s), {len(policy_items)} release(s)"
        )
        print(f"  active policy: {displayed_active_id}")
        if args.policy:
            print(f"  explicitly checked release: {_terminal_text(args.policy)}")
        for finding in findings:
            print(
                f"  {finding['severity']}: {finding['code']} - "
                f"{_terminal_text(finding['detail'])}"
            )
    return 2 if has_error else 0


def _assessment_output(document, evaluation, path=None):
    return {
        "id": document["id"],
        "schema_version": document["schema_version"],
        "path": str(path) if path is not None else None,
        "recorded_at": document["recorded_at"],
        "subject": document["subject"],
        "review": document["review"],
        "agent_input": document["agent_input"],
        "mechanical_snapshot": document["mechanical_snapshot"],
        "current_derived": evaluation,
    }


def _fail_closed_assessment_evaluation(evaluation, integrity_issues):
    """Suppress current relations when any part of the assessment store is invalid."""
    if not integrity_issues:
        return evaluation
    projected = dict(evaluation)
    projected["active_relation"] = None
    return projected


def cmd_assess(args):
    """Validate and append a schema-constrained external-agent semantic proposal."""
    cfg = _cfg(args)
    _documents, existing_issues = load_assessments(cfg)
    if existing_issues:
        detail = "; ".join(
            f"{item['path']}: {item['detail']}" for item in existing_issues
        )
        raise AssessmentError(f"assessment store integrity failed before append: {detail}")
    entry = _read_json_object(args.entry, "assessment proposal")
    expected = {"claim_id", "result_ids", "agent_input"}
    if set(entry) != expected:
        raise AssessmentError(
            "assessment proposal must contain exactly claim_id, result_ids, and agent_input; "
            "mechanical_snapshot and derived are computed by provsleuth"
        )
    document = create_assessment(
        cfg, entry["claim_id"], entry["result_ids"], entry["agent_input"], actor=args.actor,
    )
    path = append_assessment(cfg, document)
    documents, issues = load_assessments(cfg)
    if issues:
        detail = "; ".join(f"{item['path']}: {item['detail']}" for item in issues)
        raise AssessmentError(f"assessment store integrity failed after append: {detail}")
    evaluation = evaluate_assessment(cfg, document, documents)
    output = _assessment_output(document, evaluation, path)
    if args.json:
        _write_json(output)
    else:
        print(f"provsleuth assess: recorded {document['id']}")
        print(f"  verdict: {document['agent_input']['verdict']}")
        print(f"  proposed relation: {evaluation.get('proposed_relation') or 'none'}")
        print(f"  review: {evaluation['effective_review_state']}")
        for finding in evaluation["findings"]:
            print(f"  {finding['severity']}: {finding['code']} - {finding['detail']}")
    return 1 if any(item["severity"] == "error" for item in evaluation["findings"]) else 0


def _current_assessment_ids(documents):
    superseded = {
        item["review"].get("supersedes_assessment_id")
        for item in documents
        if item["review"].get("supersedes_assessment_id")
    }
    return {
        item["id"] for item in documents
        if item["id"] not in superseded and item["review"]["state"] != "superseded"
    }


def cmd_assessments(args):
    cfg = _cfg(args)
    documents, issues = load_assessments(cfg)
    current_ids = _current_assessment_ids(documents)
    items = []
    for document in sorted(documents, key=lambda item: item["id"]):
        is_current = document["id"] in current_ids
        if not args.all and not is_current:
            continue
        evaluation = evaluate_assessment(cfg, document, documents)
        if args.state and evaluation["effective_review_state"] != args.state:
            continue
        item = _assessment_output(
            document,
            _fail_closed_assessment_evaluation(evaluation, issues),
        )
        item["is_current"] = is_current
        items.append(item)
    output = {
        "schema": "claimtrace.assessment-list/1",
        "current_assessment_schema_version": ASSESSMENT_SCHEMA_VERSION,
        "supported_assessment_schema_versions": sorted(
            SUPPORTED_ASSESSMENT_SCHEMA_VERSIONS
        ),
        "integrity": "error" if issues else "ok",
        "issues": issues,
        "items": items,
    }
    if args.json:
        _write_json(output)
    else:
        print(f"provsleuth assessments: {len(items)} assessment(s), integrity {output['integrity']}")
        for item in items:
            derived = item["current_derived"]
            print(
                f"  {item['id']}  {derived['effective_review_state']}  "
                f"{item['agent_input']['verdict']}  "
                f"{item['subject']['result_ids']} -> {item['subject']['claim_id']}"
            )
        for issue in issues:
            print(f"  error: {issue['code']} - {issue['path']}: {issue['detail']}")
    return 2 if issues else 0


def cmd_review(args):
    cfg = _cfg(args)
    transition, path, evaluation = append_review_transition(
        cfg, args.assessment_id, args.state, actor=args.actor,
    )
    output = _assessment_output(transition, evaluation, path)
    if args.json:
        _write_json(output)
    else:
        print(f"provsleuth review: {transition['id']}")
        print(f"  {args.assessment_id} -> {args.state} by {args.actor}")
        print(f"  active relation: {evaluation.get('active_relation') or 'none'}")
    return 0


def _read_logic_object(path_value, label):
    """Strictly read one bounded JSON object through the shared safe reader."""
    try:
        value = load_logic_asset(path_value)
    except LogicError as exc:
        raise LogicError(f"cannot read {label} {path_value}: {exc}") from exc
    if not isinstance(value, dict):
        raise LogicError(f"{label} must contain one JSON object")
    return value


def _configured_logic_assets(cfg):
    """Resolve every configured data-only logic asset, failing on any ambiguity."""
    return configured_logic_assets(cfg)


def _derivation_assets(document, vocabularies, rule_packs):
    subject = document["subject"]
    vocabulary_id = subject["vocabulary_id"]
    rule_pack_id = subject["rule_pack_id"]
    vocabulary = vocabularies.get(vocabulary_id)
    if vocabulary is None:
        raise LogicError(
            f"derivation {document['id']} references unconfigured vocabulary {vocabulary_id}"
        )
    rule_pack = rule_packs.get(rule_pack_id)
    if rule_pack is None:
        raise LogicError(
            f"derivation {document['id']} references unconfigured rule pack {rule_pack_id}"
        )
    if rule_pack["vocabulary_id"] != vocabulary_id:
        raise LogicError(
            f"derivation {document['id']} selects an incompatible vocabulary and rule pack"
        )
    return vocabulary, rule_pack


def _derivation_output(document, evaluation, path=None, claim_level=None):
    return {
        "id": document["id"],
        "path": str(path) if path is not None else None,
        "recorded_at": document["recorded_at"],
        "actor": document["actor"],
        "subject": document["subject"],
        "agent_input": document["agent_input"],
        "mechanical_snapshot": document["mechanical_snapshot"],
        "stored_derived": document["derived"],
        "current_derived": evaluation,
        "claim_level": claim_level or {
            "conditional_active": bool(evaluation.get("active")),
            "active": bool(evaluation.get("active")),
            "state": "active" if evaluation.get("active") else "inactive",
            "findings": [],
        },
    }


def _formal_outcome_text(evaluation):
    rendered = [item for item in evaluation.get("rendered_outcomes", []) if item]
    detail = "; ".join(rendered) if rendered else "no derived outcome atom"
    return f"{evaluation.get('outcome_relation', 'undetermined')}: {detail}"


def _claim_level_projection(report, document, evaluation):
    claim_id = document["subject"]["claim_id"]
    proof_id = evaluation.get("proof_id")
    active_proof = next(
        (
            item for item in (report.get("derivations") or {}).get("active_proofs", [])
            if item.get("proof_id") == proof_id
        ),
        None,
    )
    findings = [
        item for item in report.get("findings", [])
        if (item.get("node_id") == claim_id
            and item.get("code") == "SYMBOLIC_CROSS_DERIVATION_CONFLICT")
    ]
    active = bool(active_proof and active_proof.get("claim_level_active"))
    return {
        "conditional_active": bool(evaluation.get("active")),
        "active": active,
        "state": (
            "conflict" if findings else evaluation.get("outcome_relation", "active")
            if active else "inactive"
        ),
        "findings": findings,
    }


def _fail_closed_derivation_evaluation(evaluation, integrity_issues):
    if not integrity_issues:
        return evaluation
    projected = dict(evaluation)
    projected["active"] = False
    return projected


def _raise_derivation_integrity(label, issues):
    if not issues:
        return
    detail = "; ".join(f"{item['path']}: {item['detail']}" for item in issues)
    raise LogicError(f"derivation store integrity failed {label}: {detail}")


def cmd_derive(args):
    """Create a deterministic proof from agent-proposed facts and project-owned rules."""
    cfg = _cfg(args)
    _documents, existing_issues = load_derivations(cfg)
    _raise_derivation_integrity("before append", existing_issues)
    entry = _read_logic_object(args.entry, "derivation proposal")
    verbose_expected = {
        "claim_id", "result_ids", "vocabulary_id", "rule_pack_id", "agent_input",
    }
    selection_expected = {
        "schema_version", "claim_id", "bindings", "note", "provenance",
    }
    plan_expected = {"schema_version", "claim_id", "note", "provenance"}
    if set(entry) == plan_expected and entry.get("schema_version") == PLAN_REQUEST_SCHEMA:
        document = create_derivation_from_evidence_plan(
            cfg, entry["claim_id"], actor=args.actor,
            note=entry["note"], provenance=entry["provenance"],
        )
    elif set(entry) == selection_expected and entry.get("schema_version") == SELECTION_SCHEMA:
        document = create_derivation_from_bindings(
            cfg, entry["claim_id"], entry["bindings"], actor=args.actor,
            note=entry["note"], provenance=entry["provenance"],
        )
    elif set(entry) == verbose_expected:
        vocabularies, rule_packs = _configured_logic_assets(cfg)
        vocabulary_id = entry["vocabulary_id"]
        if not isinstance(vocabulary_id, str) or not vocabulary_id:
            raise LogicError("derivation proposal vocabulary_id must be a non-empty string")
        rule_pack_id = entry["rule_pack_id"]
        if not isinstance(rule_pack_id, str) or not rule_pack_id:
            raise LogicError("derivation proposal rule_pack_id must be a non-empty string")
        vocabulary = vocabularies.get(vocabulary_id)
        if vocabulary is None:
            raise LogicError(f"unconfigured vocabulary id: {vocabulary_id!r}")
        rule_pack = rule_packs.get(rule_pack_id)
        if rule_pack is None:
            raise LogicError(f"unconfigured rule-pack id: {rule_pack_id!r}")
        if rule_pack["vocabulary_id"] != vocabulary["id"]:
            raise LogicError("selected rule pack does not use the selected vocabulary")
        document = create_derivation(
            cfg, entry["claim_id"], entry["result_ids"], entry["agent_input"],
            vocabulary=vocabulary, rule_pack=rule_pack, actor=args.actor,
        )
    else:
        raise LogicError(
            "derivation proposal must be a claimtrace.symbolic-plan-request/1 claim-only "
            "request, a claimtrace.symbolic-selection/1 binding selection, or the exact "
            "low-level claim_id/result_ids/vocabulary_id/rule_pack_id/agent_input form; "
            "mechanical_snapshot and derived are computed by provsleuth, and target or "
            "policy fields cannot be added to plan or selection proposals"
        )
    path = append_derivation(cfg, document)
    _stored, issues = load_derivations(cfg)
    _raise_derivation_integrity("after append", issues)
    vocabularies, rule_packs = _configured_logic_assets(cfg)
    vocabulary, rule_pack = _derivation_assets(document, vocabularies, rule_packs)
    evaluation = evaluate_derivation(
        cfg, document, vocabulary=vocabulary, rule_pack=rule_pack,
    )
    project_report = build_report(cfg, strict=False)
    claim_level = _claim_level_projection(project_report, document, evaluation)
    output = _derivation_output(document, evaluation, path, claim_level)
    if args.json:
        _write_json(output)
    else:
        print(f"provsleuth derive: recorded {document['id']}")
        print(f"  proof state: {evaluation['proof_state']}")
        print(f"  active: {'yes' if evaluation['active'] else 'no'}")
        print(f"  claim-level active: {'yes' if claim_level['active'] else 'no'}")
        print(f"  target: {evaluation['rendered_target']}")
        print(f"  outcome: {_formal_outcome_text(evaluation)}")
        for finding in evaluation["findings"]:
            print(f"  {finding['severity']}: {finding['code']} - {finding['detail']}")
        for finding in claim_level["findings"]:
            print(f"  {finding['severity']}: {finding['code']} - {finding['detail']}")
        if evaluation.get("drift"):
            print("  changed proof inputs:")
            for item in evaluation["drift"]:
                before = item.get("stored_version") or item.get("stored_sha256") or "absent"
                after = item.get("current_version") or item.get("current_sha256") or "absent"
                print(f"    {item['kind']} {item['id']}: {before} -> {after}")
    has_error = any(
        item["severity"] == "error"
        for item in [*evaluation["findings"], *claim_level["findings"]]
    )
    return 1 if has_error else 0


def cmd_evidence_plan(args):
    plan = resolve_claim_evidence_plan(_cfg(args), args.claim_id)
    if args.json:
        _write_json(plan)
    else:
        print(f"EVIDENCE PLAN {plan['claim_id']}")
        print(f"  vocabulary: {plan['vocabulary_id']}")
        print(f"  rule pack: {plan['rule_pack_id']}")
        print(f"  required bindings: {len(plan['required_bindings'])}")
        for item in plan["required_bindings"]:
            print(f"    {item['result_id']} / {item['binding_id']}")
    return 0


def cmd_derivations(args):
    cfg = _cfg(args)
    documents, issues = load_derivations(cfg)
    vocabularies, rule_packs = _configured_logic_assets(cfg)
    project_report = build_report(cfg, strict=False)
    items = []
    for document in documents:
        vocabulary, rule_pack = _derivation_assets(document, vocabularies, rule_packs)
        evaluation = evaluate_derivation(
            cfg, document, vocabulary=vocabulary, rule_pack=rule_pack,
        )
        evaluation = _fail_closed_derivation_evaluation(evaluation, issues)
        if args.state and evaluation["proof_state"] != args.state:
            continue
        claim_level = _claim_level_projection(project_report, document, evaluation)
        items.append(_derivation_output(document, evaluation, claim_level=claim_level))
    output = {
        "schema": "claimtrace.derivation-list/1",
        "integrity": "error" if issues else "ok",
        "issues": issues,
        "items": items,
    }
    if args.json:
        _write_json(output)
    else:
        print(
            f"provsleuth derivations: {len(items)} derivation(s), "
            f"integrity {output['integrity']}"
        )
        for item in items:
            derived = item["current_derived"]
            print(
                f"  {item['id']}  {derived['proof_state']}  "
                f"active={'yes' if derived['active'] else 'no'}  "
                f"claim-level={item['claim_level']['state']}  "
                f"{_formal_outcome_text(derived)}"
            )
        for issue in issues:
            print(f"  error: {issue['code']} - {issue['path']}: {issue['detail']}")
    claim_error = any(
        finding.get("severity") == "error"
        for item in items for finding in item["claim_level"]["findings"]
    )
    return 2 if issues else 1 if claim_error else 0


def cmd_explain(args):
    cfg = _cfg(args)
    documents, issues = load_derivations(cfg)
    _raise_derivation_integrity("while explaining a proof", issues)
    matches = [item for item in documents if item["id"] == args.derivation_id]
    if not matches:
        matches = [
            item for item in documents
            if item.get("derived", {}).get("proof_id") == args.derivation_id
        ]
    if not matches:
        raise LogicError(f"unknown derivation or proof id: {args.derivation_id}")
    document = sorted(matches, key=lambda item: item["id"])[0]
    stored_proof_id = document["derived"]["proof_id"]
    equivalent_ids = sorted(
        item["id"] for item in documents
        if item["derived"]["proof_id"] == stored_proof_id
    )
    vocabularies, rule_packs = _configured_logic_assets(cfg)
    vocabulary, rule_pack = _derivation_assets(document, vocabularies, rule_packs)
    evaluation = evaluate_derivation(
        cfg, document, vocabulary=vocabulary, rule_pack=rule_pack,
    )
    project_report = build_report(cfg, strict=False)
    claim_level = _claim_level_projection(project_report, document, evaluation)
    output = _derivation_output(document, evaluation, claim_level=claim_level)
    output["equivalent_derivation_ids"] = equivalent_ids
    if args.json:
        _write_json(output)
    else:
        print(f"DERIVATION {document['id']}")
        if len(equivalent_ids) > 1 or args.derivation_id == stored_proof_id:
            print(f"  canonical proof: {stored_proof_id}")
            print(f"  equivalent submissions: {', '.join(equivalent_ids)}")
        print(f"  recorded: {document['recorded_at']} by {document['actor']}")
        print(f"  proof state: {evaluation['proof_state']}")
        print(f"  active: {'yes' if evaluation['active'] else 'no'}")
        print(f"  claim-level active: {'yes' if claim_level['active'] else 'no'}")
        print(f"  target: {evaluation['rendered_target']}")
        print(f"  outcome: {_formal_outcome_text(evaluation)}")
        premise_label = (
            "evaluated inputs" if evaluation["proof_state"] == "unknown"
            else "proof premises"
        )
        premise_ids = (
            evaluation["input_fact_ids"] if evaluation["proof_state"] == "unknown"
            else evaluation["used_input_fact_ids"]
        )
        print(f"  {premise_label}:")
        for fact_id in premise_ids:
            print(f"    {fact_id}")
        if evaluation.get("unused_input_fact_ids"):
            print("  submitted but unused inputs:")
            for fact_id in evaluation["unused_input_fact_ids"]:
                print(f"    {fact_id}")
        print("  rule firings:")
        for step in evaluation["proof_steps"]:
            print(f"    {step['rule_id']} -> {step['conclusion_fact_id']}")
        for finding in evaluation["findings"]:
            print(f"  {finding['severity']}: {finding['code']} - {finding['detail']}")
        for finding in claim_level["findings"]:
            print(f"  {finding['severity']}: {finding['code']} - {finding['detail']}")
        if evaluation.get("drift"):
            print("  changed proof inputs:")
            for item in evaluation["drift"]:
                before = item.get("stored_version") or item.get("stored_sha256") or "absent"
                after = item.get("current_version") or item.get("current_sha256") or "absent"
                print(f"    {item['kind']} {item['id']}: {before} -> {after}")
    has_error = any(
        item["severity"] == "error"
        for item in [*evaluation["findings"], *claim_level["findings"]]
    )
    return 1 if has_error else 0


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
    print("LAB NOTEBOOK (provsleuth journal) — every attempt and where it ended up\n")
    for st in JOURNAL_ORDER + [s for s in groups if s not in JOURNAL_ORDER]:
        if only and st not in only:
            continue
        items = groups.get(st, [])
        if not items:
            continue
        print(f"== {GLYPH.get(st, st.upper())} ({len(items)}) ==")
        for nid, n in sorted(items, key=lambda kv: (kv[1].get("date", ""), kv[0])):
            d = n.get("date", "")
            v = n.get("value") or n.get("verdict") or ""
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
    print(f"provsleuth graph: {len(nodes)} nodes / {len(edges)} edges")
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
        raise EventError("separate provsleuth options from the child command with --")
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
        pipeline_contract=args.pipeline_contract,
        stage_checkpoints=args.stage_checkpoints,
    )
    print(f"provsleuth run: {result['outcome']}  {result['run_id']}")
    print("  lineage coverage: partial (declared inputs; direct child; unattributed before/after writes)")
    if result.get("pipeline_contract_id"):
        print(f"  pipeline contract: {result['pipeline_contract_id']}")
        if result.get("stage_trace") is not None:
            trace = result["stage_trace"]
            print(
                "  cooperative stage trace: "
                f"{trace['state']}  {len(trace['checkpoints'])}/"
                f"{len(trace['required_stage_ids'])} checkpoints"
            )
            print(
                "  stage meaning: exact direct child emitted the locked checkpoint; "
                "not independent computation or value observation"
            )
        else:
            print("  internal stages: declared only, not runtime-observed")
        print(f"  computation: {result['computation_id']}")
    for item in result["output_transitions"]:
        window_state = (
            "pre/post created-or-changed; causation not proven"
            if item["produced"] else "pre/post not created-or-changed"
        )
        print(f"  output: {item['path']}  {item['transition']}  ({window_state})")
    for item in result.get("intermediate_transitions", []):
        window_state = (
            "pre/post created-or-changed; stage causation not proven"
            if item["produced"] else "pre/post not created-or-changed"
        )
        print(
            f"  materialized intermediate: {item['path']}  "
            f"{item['transition']}  ({window_state})"
        )
    for detail in result["contract_errors"]:
        print(f"  contract: {detail}")
    return result["exit_code"]


def cmd_replay(args):
    if args.command and args.command[0] != "--":
        raise ReplayError("separate a redacted source command override with --")
    cfg = _cfg(args)
    result = replay_run(
        cfg, args.run_id, attempts=args.repeat,
        timeout_seconds=args.timeout,
        command_override=(args.command[1:] if args.command else None),
    )
    certificate = result["certificate"]
    try:
        stored_at = result["path"].relative_to(cfg.root).as_posix()
    except ValueError:
        stored_at = str(result["path"])
    if args.json:
        _write_json({
            "certificate": certificate,
            "review_ready": result["review_ready"],
            "stored_at": stored_at,
        })
    else:
        print(f"provsleuth replay: {certificate['outcome']}  {certificate['id']}")
        print(f"  source run: {certificate['source_run_id']}")
        print(
            f"  attempts: {len(certificate['attempts'])}; comparison: terminal output "
            "bytes, declared materialized-intermediate bytes, stdout/stderr, and "
            "visible workspace deltas"
        )
        undeclared = sorted({
            item["path"] for attempt in certificate["attempts"]
            for item in attempt["undeclared_writes"]
        })
        if undeclared:
            print("  undeclared workspace writes: " + ", ".join(undeclared))
        print(
            "  review ready: "
            + ("yes" if result["review_ready"] else "no (standalone replay exits non-zero)")
        )
        print(
            "  scope: declared project files in fresh workspaces; host environment partial; "
            "network and external filesystem writes are not isolated"
        )
        if certificate["schema_version"] == STAGE_REPLAY_SCHEMA:
            trace = certificate["source_stage_trace"]
            comparison = certificate["comparison"]
            print(
                "  cooperative stage trace: "
                f"{len(trace['checkpoints'])}/{len(trace['required_stage_ids'])} "
                "source checkpoints; "
                + (
                    "complete and repeated across all attempts"
                    if comparison["all_stage_traces_complete"]
                    and comparison["stage_traces_equal"]
                    and comparison["all_stage_traces_match_source_receipt"]
                    else "not repeatable across all attempts"
                )
            )
            print(
                "  checkpoint meaning: exact direct-child PID emitted locked callsites; "
                "not independent computation or value observation"
            )
        print("  internal stages: declared only, not runtime-observed")
        print(f"  stored: {stored_at}")
    return result["exit_code"]


def cmd_assess_method(args):
    cfg = _cfg(args)
    _documents, issues = load_method_assessments(cfg)
    if issues:
        raise MethodAssessmentError(
            "method-assessment store integrity failed before append: "
            + "; ".join(f"{item['path']}: {item['detail']}" for item in issues)
        )
    entry = _read_cli_json_object(
        args.entry, "method conformance proposal", MethodAssessmentError,
        limit=MAX_CLI_JSON_INPUT_BYTES,
    )
    expected = {
        "pipeline_contract", "method_id", "declared_inputs", "declared_outputs",
        "parameters", "seeds", "agent_input",
    }
    if set(entry) != expected:
        raise MethodAssessmentError(
            "method proposal must contain exactly pipeline_contract, method_id, "
            "declared_inputs, declared_outputs, parameters, seeds, and agent_input"
        )
    document = create_method_assessment(
        cfg, entry["pipeline_contract"], entry["method_id"], entry["agent_input"],
        actor=args.actor, declared_inputs=entry["declared_inputs"],
        declared_outputs=entry["declared_outputs"], parameters=entry["parameters"],
        seeds=entry["seeds"],
    )
    path = append_method_assessment(cfg, document)
    documents, issues = load_method_assessments(cfg)
    if issues:
        raise MethodAssessmentError("method-assessment store integrity failed after append")
    evaluation = evaluate_method_assessment(cfg, document, documents, issues)
    payload = {
        "document": document,
        "current_derived": evaluation,
        "stored_at": path.relative_to(cfg.method_assessments_path).as_posix(),
    }
    if args.json:
        _write_json(payload)
    else:
        print(f"provsleuth assess-method: recorded {document['id']}")
        print(f"  method: {document['subject']['method_id']}")
        print(f"  verdict: {document['agent_input']['verdict']}; review: proposed")
        print("  internal stages: declared only, not runtime-observed")
    return 0


def cmd_method_assessments(args):
    cfg = _cfg(args)
    statuses, issues = method_assessment_statuses(cfg)
    payload = {
        "schema_version": "claimtrace.method-conformance-status/1",
        "integrity": "valid" if not issues else "invalid",
        "issues": issues,
        "items": statuses,
    }
    if args.json:
        _write_json(payload)
    else:
        print(f"provsleuth method-assessments: {len(statuses)} current assessment(s); integrity {payload['integrity']}")
        for item in statuses:
            print(
                f"  {item['id']}  {item['method_id']}  {item['effective_review_state']}  "
                f"{item['verdict']}  current={'yes' if item['implementation_current'] else 'no'}"
            )
    return 0 if not issues else 3


def cmd_review_method(args):
    cfg = _cfg(args)
    transition, path, evaluation = append_method_review_transition(
        cfg, args.assessment_id, args.state, actor=args.actor,
    )
    payload = {
        "document": transition,
        "current_derived": evaluation,
        "stored_at": path.relative_to(cfg.method_assessments_path).as_posix(),
    }
    if args.json:
        _write_json(payload)
    else:
        print(f"provsleuth review-method: {args.assessment_id} -> {args.state} by {args.actor}")
        print(f"  successor: {transition['id']}")
        print(f"  implementation current: {'yes' if evaluation['implementation_current'] else 'no'}")
    return 0


def cmd_graph_hash(args):
    value = canonical_graph_hash(load_raw(_cfg(args)))
    if args.json:
        _write_json({"schema_version": "claimtrace.graph-hash/1", "graph_hash": value})
    else:
        print(value)
    return 0


def cmd_graph_propose(args):
    cfg = _cfg(args)
    request = _read_cli_json_object(
        args.request, "graph change request", GraphChangeError,
        limit=MAX_CLI_JSON_INPUT_BYTES,
    )
    proposal = build_graph_change_proposal(cfg, request)
    content = json.dumps(
        proposal, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False,
    ) + "\n"
    output = Path(args.output)
    if not output.is_absolute():
        output = cfg.root / output
    try:
        state = _publish_text(
            cfg.root, output, content, force=args.force, label="graph change proposal",
        )
    except (_PublicationConflict, _UnsafePublicationPath, OSError) as exc:
        raise GraphChangeError(f"cannot publish graph change proposal: {exc}") from exc
    payload = {
        "proposal": proposal,
        "output": output.resolve().relative_to(cfg.root.resolve()).as_posix(),
        "publication_state": state,
    }
    if args.json:
        _write_json(payload)
    else:
        print(f"provsleuth graph-propose: {proposal['proposal_id']}")
        print(f"  base: {proposal['base_graph_hash']}")
        print(f"  result: {proposal['result_graph_hash']}")
        print(f"  proposal: {payload['output']} ({state})")
    return 0


def cmd_graph_apply(args):
    cfg = _cfg(args)
    proposal = _read_cli_json_object(
        args.proposal, "graph change proposal", GraphChangeError,
        limit=MAX_CLI_JSON_INPUT_BYTES,
    )
    result = apply_graph_change_proposal(cfg, proposal)
    if args.json:
        _write_json(result)
    else:
        print(f"provsleuth graph-apply: {result['status']}  {result['proposal_id']}")
        print(f"  graph: {result['graph_hash']}")
    return 0


def _release_document(path_value, label):
    return _read_cli_json_object(
        path_value, label, ReleaseError, limit=MAX_CLI_TEXT_PUBLICATION_BYTES,
    )


def cmd_release_create(args):
    cfg = _cfg(args)
    document = create_release_manifest(cfg)
    output = None
    state = None
    if args.output:
        output = Path(args.output)
        if not output.is_absolute():
            output = cfg.root / output
        content = json.dumps(
            document, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False,
        ) + "\n"
        try:
            state = _publish_text(
                cfg.root, output, content, force=args.force, label="project release manifest",
            )
        except (_PublicationConflict, _UnsafePublicationPath, OSError) as exc:
            raise ReleaseError(f"cannot publish project release manifest: {exc}") from exc
    if args.json or output is None:
        _write_json(document)
    else:
        label = output.resolve().relative_to(cfg.root.resolve()).as_posix()
        print(f"provsleuth release-create: {document['release_id']}")
        print(f"  files: {len(document['files'])}")
        print(f"  manifest: {label} ({state})")
        print("  scope: exact-byte integrity and discoverable coverage; not scientific truth or execution determinism")
    return 0


def cmd_release_verify(args):
    result = verify_release_manifest(
        _cfg(args), _release_document(args.manifest, "project release manifest"),
    )
    if args.json:
        _write_json(result)
    else:
        print(f"provsleuth release-verify: {'valid' if result['valid'] else 'drifted'}")
        print(f"  manifest: {result['manifest_release_id']}")
        if result["current_release_id"]:
            print(f"  current: {result['current_release_id']}")
        for error in result["errors"]:
            print(f"  error: {error}")
    return 0 if result["valid"] else 3


def cmd_release_diff(args):
    result = diff_release_manifests(
        _release_document(args.before, "earlier project release manifest"),
        _release_document(args.after, "later project release manifest"),
    )
    if args.json:
        _write_json(result)
    else:
        print(f"provsleuth release-diff: {'equal' if result['equal'] else 'different'}")
        print(f"  added: {len(result['added_files'])}; removed: {len(result['removed_files'])}; changed: {len(result['changed_files'])}")
    return 0 if result["equal"] else 1


def cmd_view(args):
    try:
        summary = render_view(_cfg(args), args.output)
    except OSError as exc:
        raise GraphError(f"cannot write view {args.output}: {exc}") from exc
    print(f"provsleuth view: wrote {summary['path']}")
    print(f"  {summary['nodes']} semantic nodes / {summary['edges']} semantic edges / "
          f"{summary['runs']} run receipts / {summary['layers']} layers")
    return 0


def _path_is_inside(path, directory):
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def _assert_safe_graphrag_destination(cfg, report, destination):
    """Keep derived retrieval exports away from source, policy, and provenance bytes."""
    destination = destination.resolve(strict=False)
    protected = {cfg.config_path.resolve(), cfg.graph_path.resolve()}
    if cfg.verifiers is not None:
        protected.add(Path(cfg.verifiers).resolve(strict=False))
    for path in (
        *cfg.logic_vocabulary_paths,
        *cfg.logic_rule_pack_paths,
        *cfg.semantic_terminology_paths,
        *cfg.semantic_ontology_lock_paths,
    ):
        protected.add(Path(path).resolve(strict=False))
    for node in (report.get("graph") or {}).get("nodes") or []:
        if node.get("path"):
            declared = cfg.resolve(node["path"]).resolve(strict=False)
            protected.add(declared)
            protected.add(Path(str(declared) + ".manifest.json").resolve(strict=False))
    for lock in (((report.get("semantics") or {}).get("assets") or {})
                 .get("ontology_locks") or []):
        declared = Path(lock["path"])
        lock_path = (
            declared if declared.is_absolute() else cfg.base / declared
        ).resolve(strict=False)
        for item in lock.get("documents") or []:
            protected.add((lock_path.parent / item["path"]).resolve(strict=False))
        index = lock.get("index") or {}
        if index.get("path"):
            protected.add((lock_path.parent / index["path"]).resolve(strict=False))
    stores = (
        cfg.events_path, cfg.assessments_path, cfg.deliberation_path,
        cfg.derivations_path, cfg.semantic_mappings_path,
        cfg.semantic_policies_path, cfg.replays_path,
        cfg.method_assessments_path,
    )
    if destination in protected or any(
            _path_is_inside(destination, Path(store).resolve(strict=False))
            for store in stores):
        raise GraphRAGError(
            f"refusing to overwrite a graph-declared, policy, or provenance path: {destination}"
        )


def _publish_json_document(cfg, report, value, output_value, *, force, label):
    """Publish canonical pretty JSON inside the project through the shared safe writer."""
    output = Path(output_value)
    if not output.is_absolute():
        output = cfg.root / output
    _assert_safe_graphrag_destination(cfg, report, output)
    content = json.dumps(
        value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False,
    ) + "\n"
    try:
        state = _publish_text(
            cfg.root, output, content, force=force, label=label,
        )
    except (_PublicationConflict, _UnsafePublicationPath, OSError) as exc:
        raise GraphRAGError(f"cannot publish {label}: {exc}") from exc
    return output, state


def cmd_graphrag_export(args):
    """Project the complete canonical report into deterministic graph retrieval data."""
    cfg = _cfg(args)
    report = build_report(cfg, strict=False)
    projection = build_projection(report)
    output = None
    state = None
    if args.output:
        output, state = _publish_json_document(
            cfg, report, projection, args.output, force=args.force,
            label="GraphRAG projection",
        )
    if args.json or output is None:
        _write_json(projection)
    else:
        display = output.resolve().relative_to(cfg.root.resolve()).as_posix()
        print(f"provsleuth graphrag-export: {projection['projection_id']}")
        print(
            f"  {len(projection['nodes'])} nodes / {len(projection['edges'])} edges / "
            f"{len(projection['unresolved_references'])} unresolved reference(s)"
        )
        print(f"  projection: {display} ({state})")
        print("  scope: deterministic retrieval projection; no semantic endorsement or generation")
    return 0


def cmd_graphrag_context(args):
    """Return one explicitly bounded deterministic neighborhood for an external retriever."""
    cfg = _cfg(args)
    report = build_report(cfg, strict=False)
    projection = build_projection(report)
    context = bounded_context(
        projection,
        args.seed,
        max_hops=args.max_hops,
        max_nodes=args.max_nodes,
        max_edges=args.max_edges,
        max_bytes=args.max_bytes,
        traversable_only=not args.include_nontraversable,
    )
    output = None
    state = None
    if args.output:
        output, state = _publish_json_document(
            cfg, report, context, args.output, force=args.force,
            label="GraphRAG bounded context",
        )
    if args.json or output is None:
        _write_json(context)
    else:
        display = output.resolve().relative_to(cfg.root.resolve()).as_posix()
        print(f"provsleuth graphrag-context: {context['context_id']}")
        print(
            f"  {len(context['nodes'])} nodes / {len(context['edges'])} edges / "
            f"{len(context['request']['unknown_seed_ids'])} unknown seed(s)"
        )
        print(f"  context: {display} ({state})")
        print("  scope: bounded retrieval context; exclusions remain explicit")
    return 0


PLANNING_GRAPH = {
    "schema_version": "1.0",
    "concepts": {},
    "nodes": [],
    "edges": [],
}
EXAMPLE_GRAPH = {
    "schema_version": "1.0",
    "concepts": {
        "dataset_version": {
            "canonical": "v1",
            "note": "bump this when the example dataset is re-cut",
        }
    },
    "nodes": [
        {"id": "data:example", "type": "data", "status": "current", "backbone": "v1",
         "path": "data/provsleuth-example.csv", "value": "two example measurements"}
    ],
    "edges": [],
}
EXAMPLE_DATA = "measurement\n1\n2\n"
EXAMPLE_VERIFIERS = '''"""Checks for the explicit ProvSleuth example scaffold."""
from provsleuth import check


@check("example: row count")
def row_count():
    # paths are relative to the project root
    import csv
    with open("data/provsleuth-example.csv", encoding="utf-8", newline="") as f:
        n = sum(1 for _ in csv.reader(f)) - 1
    return n == 2, f"{n} rows", "2 rows"
'''


class _PublicationConflict(RuntimeError):
    """A no-force CLI publication collided with different or unstable content."""


class _UnsafePublicationPath(RuntimeError):
    """A CLI publication target escaped through a link or non-directory component."""


def _publication_path_is_link_like(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise _UnsafePublicationPath(f"cannot inspect publication path {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _assert_safe_publication_target(base: Path, target: Path, label: str) -> Path:
    """Reject link/junction components below one explicit resolved project root."""
    base = Path(os.path.abspath(base))
    target = Path(os.path.abspath(target))
    try:
        relative = target.relative_to(base)
    except ValueError as exc:
        raise _UnsafePublicationPath(f"{label} escapes the selected project directory") from exc
    if _publication_path_is_link_like(base) or not base.is_dir():
        raise _UnsafePublicationPath(f"selected project directory is not trustworthy: {base}")
    current = base
    for index, part in enumerate(relative.parts):
        current = current / part
        if _publication_path_is_link_like(current):
            raise _UnsafePublicationPath(
                f"{label} traverses a symbolic link or junction: {current}"
            )
        if index < len(relative.parts) - 1 and current.exists():
            try:
                info = current.lstat()
            except OSError as exc:
                raise _UnsafePublicationPath(
                    f"cannot inspect {label} component {current}: {exc}"
                ) from exc
            if not stat.S_ISDIR(info.st_mode):
                raise _UnsafePublicationPath(
                    f"{label} component is not a directory: {current}"
                )
    return target


def _ensure_safe_publication_directory(base: Path, directory: Path, label: str) -> Path:
    directory = _assert_safe_publication_target(base, directory, label)
    base = Path(os.path.abspath(base))
    current = base
    for part in directory.relative_to(base).parts:
        current = current / part
        if _publication_path_is_link_like(current):
            raise _UnsafePublicationPath(
                f"{label} traverses a symbolic link or junction: {current}"
            )
        created = False
        try:
            current.mkdir()
            created = True
        except FileExistsError:
            pass
        try:
            info = current.lstat()
        except OSError as exc:
            raise _UnsafePublicationPath(
                f"cannot inspect {label} directory {current}: {exc}"
            ) from exc
        if (_publication_path_is_link_like(current) or not stat.S_ISDIR(info.st_mode)):
            raise _UnsafePublicationPath(f"{label} component is not a directory: {current}")
        if created:
            fsync_semantic_directory(current.parent)
    return directory


def _stable_publication_text(base: Path, destination: Path, label: str) -> str:
    destination = _assert_safe_publication_target(base, destination, label)
    payload = stable_semantic_file_bytes(
        destination, label, MAX_CLI_TEXT_PUBLICATION_BYTES,
    )
    try:
        return payload.decode("utf-8")
    except UnicodeError as exc:
        raise _PublicationConflict(f"{label} is not valid UTF-8: {exc}") from exc


def _publish_text(base: Path, destination: Path, content: str, *, force: bool,
                  label: str) -> str:
    """Publish text atomically; no-force mode can never replace an existing entry."""
    payload = content.encode("utf-8")
    if len(payload) > MAX_CLI_TEXT_PUBLICATION_BYTES:
        raise _PublicationConflict(
            f"{label} exceeds the {MAX_CLI_TEXT_PUBLICATION_BYTES}-byte publication limit"
        )
    destination = Path(os.path.abspath(destination))
    _ensure_safe_publication_directory(base, destination.parent, label)
    _assert_safe_publication_target(base, destination, label)
    if force and destination.exists():
        try:
            if _stable_publication_text(base, destination, label) == content:
                return "up_to_date"
        except (OSError, SemanticError, _PublicationConflict, _UnsafePublicationPath):
            # Explicit force authorizes replacing a differing or unreadable regular file,
            # but the path checks above still prohibit links and directory escapes.
            pass
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary, "xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _assert_safe_publication_target(base, destination.parent, label)
        if force:
            os.replace(temporary, destination)
            fsync_semantic_directory(destination.parent)
            _assert_safe_publication_target(base, destination, label)
            return "installed"
        try:
            os.link(temporary, destination)
            fsync_semantic_directory(destination.parent)
        except FileExistsError:
            try:
                existing = _stable_publication_text(base, destination, label)
            except (OSError, SemanticError, _UnsafePublicationPath,
                    _PublicationConflict) as exc:
                raise _PublicationConflict(
                    f"cannot verify the existing {label}: {exc}"
                ) from exc
            if existing == content:
                return "up_to_date"
            raise _PublicationConflict(f"existing {label} differs")
        except OSError as exc:
            raise _PublicationConflict(
                f"cannot atomically create {label}: this filesystem or policy must "
                "support same-filesystem hard links for no-force publication; choose "
                f"a compatible local filesystem or review the destination before --force: {exc}"
            ) from exc
        _assert_safe_publication_target(base, destination, label)
        return "installed"
    finally:
        try:
            temporary.unlink()
            fsync_semantic_directory(destination.parent)
        except FileNotFoundError:
            pass


def _init_locked(base: Path, args) -> int:
    """Publish one coherent scaffold while the per-project runtime lock is held."""
    example = bool(getattr(args, "example", False))
    cfg_path = base / CONFIG_NAME
    legacy_cfg_path = base / LEGACY_CONFIG_NAME
    trace = base / "provsleuth"
    gp = trace / "graph.json"
    vp = trace / "verifiers.py"
    dp = base / "data" / "provsleuth-example.csv"
    config_content = json.dumps(
        {"root": ".", "graph": "provsleuth/graph.json",
         "verifiers": "provsleuth/verifiers.py" if example else None,
         "events": "provsleuth/events", "assessments": "provsleuth/assessments",
         "deliberation": {"records": "provsleuth/deliberations"},
         "render_types": ["figure"], "input_types": ["data", "artifact", "code"],
         "run_output_types": ["artifact"], "require_assessments": False,
         "execution": {
             "replays": "provsleuth/replays",
             "method_assessments": "provsleuth/method-assessments",
             "require_contracts": False,
             "require_replay": False,
             "require_method_assessments": False,
             "require_stage_checkpoints": False,
             "replay_attempts": 2,
         },
         "logic": {
             "derivations": "provsleuth/derivations",
             "vocabularies": [],
             "rule_packs": [],
             "allow_external_packs": False,
             "require_derivations": False,
             "max_provenance_bytes": 64 * 1024 * 1024 * 1024,
         },
         "semantics": {
             "terminologies": [],
             "ontology_locks": [],
             "mappings": "provsleuth/semantics/mappings",
             "policies": "provsleuth/semantics/policies",
             "active_policy": None,
             "allow_external_sources": False,
             "require_active_policy": False,
             "language": "en",
             "max_candidates": 25,
             "max_ontology_bytes": 512 * 1024 * 1024,
          }},
        indent=2) + "\n"
    graph_content = json.dumps(EXAMPLE_GRAPH if example else PLANNING_GRAPH, indent=2) + "\n"
    scaffold_files = [(gp, graph_content, "ProvSleuth graph")]
    if example:
        scaffold_files.extend((
            (vp, EXAMPLE_VERIFIERS, "ProvSleuth verifier"),
            (dp, EXAMPLE_DATA, "ProvSleuth example data"),
        ))
    if legacy_cfg_path.exists():
        print(
            f"provsleuth init: refusing to create {CONFIG_NAME} because legacy "
            f"{LEGACY_CONFIG_NAME} already exists in {base}; use that project in place "
            "or migrate it explicitly",
            file=sys.stderr,
        )
        return 1
    try:
        _assert_safe_publication_target(base, cfg_path, "ProvSleuth config")
    except _UnsafePublicationPath as exc:
        print(f"provsleuth init: refusing unsafe path: {_terminal_text(exc)}", file=sys.stderr)
        return 1

    if cfg_path.exists() and not args.force:
        print(f"{cfg_path} already exists (use --force to overwrite)")
        return 1
    try:
        _ensure_safe_publication_directory(base, trace, "provsleuth scaffold")
        for destination, _content, label in scaffold_files:
            _assert_safe_publication_target(base, destination, label)
    except _UnsafePublicationPath as exc:
        print(f"provsleuth init: refusing unsafe path: {_terminal_text(exc)}", file=sys.stderr)
        return 1
    try:
        config_state = _publish_text(
            base, cfg_path, config_content, force=args.force, label="ProvSleuth config",
        )
    except (_PublicationConflict, _UnsafePublicationPath, OSError) as exc:
        print(f"provsleuth init: refusing to overwrite config: {_terminal_text(exc)}",
              file=sys.stderr)
        return 1
    if not args.force and config_state != "installed":
        print(f"{cfg_path} already exists (use --force to overwrite)")
        return 1

    for destination, content, label in scaffold_files:
        if destination.exists() and not args.force:
            continue
        try:
            _publish_text(base, destination, content, force=args.force, label=label)
        except _PublicationConflict as exc:
            # A no-force concurrent creator is equivalent to a pre-existing optional scaffold.
            if args.force:
                print(f"provsleuth init: cannot publish scaffold: {_terminal_text(exc)}",
                      file=sys.stderr)
                return 1
        except (_UnsafePublicationPath, OSError) as exc:
            print(f"provsleuth init: refusing unsafe path: {_terminal_text(exc)}",
                  file=sys.stderr)
            return 1
    installed = [CONFIG_NAME, "provsleuth/graph.json"]
    if example:
        installed.extend(("provsleuth/verifiers.py", "data/provsleuth-example.csv"))
    print(f"initialised {'example' if example else 'planning'} ProvSleuth in {base}\n  "
          + "\n  ".join(installed))
    if example:
        print("Next: run `provsleuth verify`, then `provsleuth check --strict`.")
    else:
        print("Next: run `provsleuth check --strict`; add nodes only when their real "
              "project objects exist.")
    return 0


def cmd_init(args):
    requested = Path(args.dir).expanduser()
    try:
        requested.mkdir(parents=True, exist_ok=True)
        base = requested.resolve(strict=True)
    except OSError as exc:
        print(f"provsleuth init: cannot create project directory: {_terminal_text(exc)}",
              file=sys.stderr)
        return 1
    try:
        # Config, graph, and verifier are one scaffold bundle.  Serializing the
        # complete operation prevents concurrent force installs from mixing them.
        with _event_lock(base):
            return _init_locked(base, args)
    except EventError as exc:
        print(f"provsleuth init: cannot acquire project scaffold lock: "
              f"{_terminal_text(exc)}", file=sys.stderr)
        return 1


_SKILL_FILES = (
    "SKILL.md",
    "references/adversarial-deliberation.md",
    "references/semantic-authoring.md",
)


def _packaged_skill_files():
    root = resources.files("provsleuth") / "templates" / "provsleuth-log"
    return {
        relative: root.joinpath(*relative.split("/")).read_text(encoding="utf-8")
        for relative in _SKILL_FILES
    }


def _install_skill_locked(base: Path, args) -> int:
    """Install and verify one complete managed skill bundle under its project lock."""
    contents = _packaged_skill_files()
    choices = {
        "agents": base / ".agents" / "skills" / "provsleuth-log",
        "claude": base / ".claude" / "skills" / "provsleuth-log",
    }
    selected = list(choices) if args.target == "both" else [args.target]
    destinations = [
        (choices[key] / Path(relative), content)
        for key in selected
        for relative, content in contents.items()
    ]
    try:
        for destination, _content in destinations:
            _assert_safe_publication_target(base, destination, "skill installation")
    except _UnsafePublicationPath as exc:
        print("provsleuth install-skill: refusing unsafe path:", file=sys.stderr)
        print(f"  {_terminal_text(exc)}", file=sys.stderr)
        return 1

    conflicts = []
    if not args.force:
        for destination, content in destinations:
            if not destination.exists():
                continue
            try:
                existing = _stable_publication_text(
                    base, destination, "existing skill file",
                )
            except (OSError, SemanticError, _PublicationConflict,
                    _UnsafePublicationPath) as exc:
                conflicts.append(f"{destination}: cannot verify existing file ({exc})")
                continue
            if existing != content:
                conflicts.append(f"{destination}: existing skill differs")
    if conflicts:
        print("provsleuth install-skill: refusing to overwrite:", file=sys.stderr)
        for detail in conflicts:
            print(f"  {_terminal_text(detail)}", file=sys.stderr)
        print("Re-run with --force only after reviewing the existing skill.", file=sys.stderr)
        return 1

    published = []
    for destination, content in destinations:
        try:
            state = _publish_text(
                base, destination, content, force=args.force, label="skill file",
            )
        except (_PublicationConflict, _UnsafePublicationPath, OSError) as exc:
            print("provsleuth install-skill: refusing to overwrite:", file=sys.stderr)
            print(f"  {_terminal_text(destination)}: {_terminal_text(exc)}", file=sys.stderr)
            return 1
        published.append((destination, content, state))

    verification_errors = []
    for destination, expected, _state in published:
        try:
            actual = _stable_publication_text(base, destination, "installed skill file")
        except (OSError, SemanticError, _PublicationConflict,
                _UnsafePublicationPath) as exc:
            verification_errors.append(f"{destination}: cannot verify installed file ({exc})")
            continue
        if actual != expected:
            verification_errors.append(f"{destination}: installed content differs")
    if verification_errors:
        print("provsleuth install-skill: final bundle verification failed:", file=sys.stderr)
        for detail in verification_errors:
            print(f"  {_terminal_text(detail)}", file=sys.stderr)
        return 1

    for destination, _content, state in published:
        action = "up to date" if state == "up_to_date" else "installed"
        print(f"provsleuth install-skill: {action}  {destination}")
    return 0


def cmd_install_skill(args):
    """Install the packaged research skill into explicit project-local discovery paths."""
    requested = Path(args.dir).expanduser()
    try:
        requested.mkdir(parents=True, exist_ok=True)
        base = requested.resolve(strict=True)
    except OSError as exc:
        print(f"provsleuth install-skill: cannot create project directory: {_terminal_text(exc)}",
              file=sys.stderr)
        return 1
    try:
        # One per-project lock covers preflight, every managed file, and final
        # verification, so concurrent package versions cannot interleave.
        with _event_lock(base):
            return _install_skill_locked(base, args)
    except EventError as exc:
        print(f"provsleuth install-skill: cannot acquire project install lock: "
              f"{_terminal_text(exc)}", file=sys.stderr)
        return 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="provsleuth",
                                 description="Dependency-aware provenance + verification for data analysis")
    ap.add_argument("--version", action="version", version=f"provsleuth {__version__}")
    ap.add_argument(
        "--config",
        help=(
            "explicit path to provsleuth.config.json or legacy "
            "claimtrace.config.json (default: discover upward)"
        ),
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check", help="validate the graph against disk + canonical concepts")
    p.add_argument("--strict", action="store_true",
                   help="also fail on lint warnings, stale nodes, and incomplete run provenance")
    p.add_argument("--json", action="store_true", help="emit one deterministic JSON report")
    p = sub.add_parser("lint", help="warn on non-standard vocabulary + un-annotated load-bearing nodes")
    p.add_argument("--strict", action="store_true", help="exit non-zero if any warnings")
    p = sub.add_parser("downstream", help="transitive dependents of a node")
    p.add_argument("node")
    p = sub.add_parser("upstream", help="transitive dependencies of a node")
    p.add_argument("node")
    p = sub.add_parser("impact", help="propagation to-do list for a canonical change")
    p.add_argument("--set", required=True)
    p = sub.add_parser("node", help="show a node + its edges")
    p.add_argument("node")
    p = sub.add_parser("log", help="append a lab-notebook entry from a JSON file")
    p.add_argument("entry")
    p.add_argument("--update", action="store_true")
    p = sub.add_parser("assess", help="append a grounded external-agent semantic assessment")
    p.add_argument("entry", help="JSON proposal containing claim_id, result_ids, and agent_input")
    p.add_argument("--actor", required=True, help="identity submitting the assessment proposal")
    p.add_argument("--json", action="store_true", help="emit the recorded assessment as JSON")
    p = sub.add_parser("assessments", help="list semantic assessments and current review state")
    p.add_argument("--state", choices=("proposed", "accepted", "rejected", "contested", "superseded"))
    p.add_argument("--all", action="store_true", help="include superseded assessment history")
    p.add_argument("--json", action="store_true", help="emit one deterministic JSON document")
    p = sub.add_parser("review", help="append an immutable assessment review decision")
    p.add_argument("assessment_id", help="content-addressed assessment id to review")
    p.add_argument("--state", required=True,
                   choices=("accepted", "rejected", "contested", "superseded"))
    p.add_argument("--actor", required=True, help="identity making the review decision")
    p.add_argument("--json", action="store_true", help="emit the review transition as JSON")
    p = sub.add_parser(
        "deliberate-propose",
        help="append one exact source-anchored claim, semantics, formalization, or rule candidate",
    )
    p.add_argument("entry", help="claimtrace.deliberation-proposal-request/1 JSON file")
    p.add_argument("--actor", required=True, help="self-asserted proposal actor identity")
    p.add_argument(
        "--independence-group", required=True,
        help="human-declared procedural independence class",
    )
    p.add_argument("--json", action="store_true")
    p = sub.add_parser(
        "deliberate-freeze",
        help="freeze the complete current candidate union for one round and subject",
    )
    p.add_argument("entry", help="claimtrace.deliberation-candidate-set-request/1 JSON file")
    p.add_argument("--actor", required=True, help="identity freezing the candidate union")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser(
        "deliberate-ballot",
        help="append one role-bound ballot over every non-owned frozen candidate",
    )
    p.add_argument("entry", help="claimtrace.deliberation-ballot-request/1 JSON file")
    p.add_argument("--actor", required=True, help="self-asserted reviewer identity")
    p.add_argument(
        "--independence-group", required=True,
        help="human-declared procedural independence class",
    )
    p.add_argument("--json", action="store_true")
    p = sub.add_parser(
        "deliberate-decide",
        help="append an attributed phase-routing decision without activating project state",
    )
    p.add_argument(
        "entry", help="claimtrace.deliberation-phase-decision-request/1 JSON file"
    )
    p.add_argument(
        "--actor", required=True,
        help="self-asserted decision-maker identity; not authenticated by ProvSleuth",
    )
    p.add_argument("--json", action="store_true")
    p = sub.add_parser(
        "deliberations",
        help="show deterministic panel status without activating semantics or rules",
    )
    p.add_argument("candidate_set_id", nargs="?", help="optional exact frozen-set ID")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser(
        "lock-ontology",
        help="create a local exact-byte ontology/index lock; never fetches remote resources",
    )
    p.add_argument(
        "entry", help="JSON request with ontology identity, local documents, and term index"
    )
    p.add_argument("--output", required=True, help="ontology-lock JSON path to create")
    p.add_argument("--json", action="store_true", help="emit the created lock as JSON")
    p = sub.add_parser(
        "ontology-candidates",
        help="search configured locked ontology indexes with deterministic exact matching",
    )
    p.add_argument("query", help="exact IRI, preferred-label, or synonym query")
    p.add_argument("--language", help="preferred BCP-47 language tag")
    p.add_argument("--limit", type=int, help="maximum candidates (default: semantics config)")
    p.add_argument("--json", action="store_true", help="emit the content-addressed candidate set")
    p = sub.add_parser(
        "map-term", help="append a locked-candidate semantic-mapping proposal"
    )
    p.add_argument(
        "entry", help="JSON proposal containing terminology_id, term_id, and agent_input"
    )
    p.add_argument("--actor", required=True, help="identity submitting the mapping proposal")
    p.add_argument("--language", help="same language profile used for ontology-candidates")
    p.add_argument("--limit", type=int, help="same candidate limit used for ontology-candidates")
    p.add_argument("--json", action="store_true", help="emit the recorded mapping as JSON")
    p = sub.add_parser(
        "mappings", help="list semantic mappings, review state, drift, and policy eligibility"
    )
    p.add_argument(
        "--state", choices=("proposed", "accepted", "rejected", "contested", "superseded")
    )
    p.add_argument("--all", action="store_true", help="include superseded mapping history")
    p.add_argument("--json", action="store_true", help="emit one deterministic JSON document")
    p = sub.add_parser(
        "review-mapping", help="append an immutable mapping review decision"
    )
    p.add_argument("mapping_id", help="content-addressed mapping leaf to review")
    p.add_argument(
        "--state", required=True,
        choices=("accepted", "rejected", "contested", "superseded"),
    )
    p.add_argument("--actor", required=True, help="identity making the review decision")
    p.add_argument("--json", action="store_true", help="emit the review successor as JSON")
    p = sub.add_parser(
        "compile-semantic-policy",
        help="append a reviewed release from exact accepted mapping leaf IDs",
    )
    p.add_argument("entry", help="JSON request containing exact mapping_ids and a release note")
    p.add_argument("--actor", required=True, help="identity compiling the reviewed release")
    p.add_argument("--json", action="store_true", help="emit the inactive release as JSON")
    p = sub.add_parser(
        "semantic-status",
        help="recheck semantic assets, mapping reviews, releases, and explicit activation",
    )
    p.add_argument(
        "--policy", help="also re-evaluate exactly one stored inactive semantic-policy ID",
    )
    p.add_argument("--json", action="store_true", help="emit one deterministic JSON document")
    p = sub.add_parser("derive", help="append a grounded project-rule symbolic derivation")
    p.add_argument(
        "entry",
        help=("preferred claimtrace.symbolic-plan-request/1 claim-only proposal; "
              "binding-selection and exact low-level typed-fact forms are also accepted"),
    )
    p.add_argument("--actor", required=True, help="identity submitting the grounded premises")
    p.add_argument("--json", action="store_true", help="emit the recorded derivation as JSON")
    p = sub.add_parser(
        "evidence-plan", help="show a claim-owned exact required-premise plan",
    )
    p.add_argument("claim_id", help="formalized claim, hypothesis, prediction, or conclusion id")
    p.add_argument("--json", action="store_true", help="emit one deterministic JSON document")
    p = sub.add_parser("derivations", help="list symbolic derivations under current rule assets")
    p.add_argument("--state", choices=("derivable", "refutable", "conflict", "unknown"))
    p.add_argument("--json", action="store_true", help="emit one deterministic JSON document")
    p = sub.add_parser("explain", help="show one symbolic derivation or canonical proof")
    p.add_argument("derivation_id", help="content-addressed derivation or proof id")
    p.add_argument("--json", action="store_true", help="emit one deterministic JSON document")
    p = sub.add_parser("journal", help="lab-notebook view grouped by verdict")
    p.add_argument("--status", default="")
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
    p.add_argument(
        "--pipeline-contract",
        help=("project-relative claimtrace.pipeline-contract/1 file binding exact code, "
              "method steps, and graph input/output roles"),
    )
    p.add_argument(
        "--stage-checkpoints", action="store_true",
        help=("require one cooperative, anchored child checkpoint for every contract "
              "stage; also enabled by execution.require_stage_checkpoints"),
    )
    p.add_argument("command", nargs=argparse.REMAINDER, help="-- COMMAND [ARG ...]")
    p = sub.add_parser(
        "replay", help="repeat a successful contract-bound run in fresh workspaces",
    )
    p.add_argument("run_id", help="successful contract-bound run receipt id")
    p.add_argument("--repeat", type=int, help="fresh attempts (default: execution.replay_attempts)")
    p.add_argument("--timeout", type=float, help="per-attempt timeout in seconds")
    p.add_argument(
        "--json", action="store_true",
        help="emit the replay certificate and review-readiness decision as JSON",
    )
    p.add_argument(
        "command", nargs=argparse.REMAINDER,
        help="-- COMMAND [ARG ...], required only when the stored argv was redacted",
    )
    p = sub.add_parser(
        "assess-method",
        help="append an external-agent method-to-code conformance proposal",
    )
    p.add_argument("entry", help="provsleuth method-conformance proposal JSON")
    p.add_argument("--actor", required=True, help="self-asserted proposal actor identity")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser(
        "method-assessments", help="list current method conformance review leaves",
    )
    p.add_argument("--json", action="store_true")
    p = sub.add_parser(
        "review-method", help="append a distinct immutable method-conformance review",
    )
    p.add_argument("assessment_id")
    p.add_argument(
        "--state", required=True,
        choices=("accepted", "rejected", "contested", "superseded"),
    )
    p.add_argument("--actor", required=True, help="self-asserted reviewer identity")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("graph-hash", help="print the canonical current graph hash")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser(
        "graph-propose", help="validate a bounded graph-change request without mutating the graph",
    )
    p.add_argument("request", help="claimtrace.graph-change-request/1 JSON file")
    p.add_argument("--output", required=True, help="project-relative proposal JSON to create")
    p.add_argument("--force", action="store_true", help="replace a differing proposal file")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser(
        "graph-apply", help="atomically apply one reviewed content-addressed graph proposal",
    )
    p.add_argument("proposal", help="claimtrace.graph-change-proposal/1 JSON file")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser(
        "release-create", help="collect a stable exact-byte project release manifest",
    )
    p.add_argument("--output", help="project-relative manifest JSON to create")
    p.add_argument("--force", action="store_true", help="replace a differing manifest file")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser(
        "release-verify", help="recollect the project and verify an exact release manifest",
    )
    p.add_argument("manifest")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("release-diff", help="compare two valid exact release manifests")
    p.add_argument("before")
    p.add_argument("after")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("view", help="render a deterministic standalone research-trajectory map")
    p.add_argument("--output", required=True, help="HTML file to write")
    p = sub.add_parser(
        "graphrag-export",
        help="export the canonical report as a deterministic read-only retrieval graph",
    )
    p.add_argument("--output", help="project-relative JSON projection to create")
    p.add_argument("--force", action="store_true", help="replace a differing projection file")
    p.add_argument("--json", action="store_true", help="emit the projection as canonical JSON")
    p = sub.add_parser(
        "graphrag-context",
        help="extract a bounded deterministic neighborhood from the report graph",
    )
    p.add_argument("seed", nargs="+", help="one or more exact projection node IDs")
    p.add_argument("--max-hops", type=int, default=2)
    p.add_argument("--max-nodes", type=int, default=64)
    p.add_argument("--max-edges", type=int, default=128)
    p.add_argument(
        "--max-bytes", type=int, default=DEFAULT_MAX_CONTEXT_BYTES,
        help="maximum canonical JSON bytes in the returned context",
    )
    p.add_argument(
        "--include-nontraversable", action="store_true",
        help="include explicitly non-traversable diagnostic relationships",
    )
    p.add_argument("--output", help="project-relative JSON context to create")
    p.add_argument("--force", action="store_true", help="replace a differing context file")
    p.add_argument("--json", action="store_true", help="emit the context as canonical JSON")
    p = sub.add_parser("init", help="scaffold a planning-safe ProvSleuth project")
    p.add_argument("dir", nargs="?", default=".")
    p.add_argument("--example", action="store_true",
                   help="also install a small runnable example dataset and verifier")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("install-skill", help="install the packaged provsleuth-log agent skill")
    p.add_argument("--dir", default=".", help="project directory receiving the skill")
    p.add_argument("--target", choices=("agents", "claude", "both"), default="both",
                   help="project-local skill layout to install (default: both)")
    p.add_argument("--force", action="store_true", help="replace a differing existing skill")
    args = ap.parse_args(argv)
    dispatch = {
        "check": cmd_check, "lint": cmd_lint, "downstream": cmd_downstream, "upstream": cmd_upstream,
        "impact": cmd_impact, "node": cmd_node, "log": cmd_log, "journal": cmd_journal,
        "assess": cmd_assess, "assessments": cmd_assessments, "review": cmd_review,
        "deliberate-propose": cmd_deliberate_propose,
        "deliberate-freeze": cmd_deliberate_freeze,
        "deliberate-ballot": cmd_deliberate_ballot,
        "deliberate-decide": cmd_deliberate_decide,
        "deliberations": cmd_deliberations,
        "lock-ontology": cmd_lock_ontology,
        "ontology-candidates": cmd_ontology_candidates,
        "map-term": cmd_map_term, "mappings": cmd_mappings,
        "review-mapping": cmd_review_mapping,
        "compile-semantic-policy": cmd_compile_semantic_policy,
        "semantic-status": cmd_semantic_status,
        "derive": cmd_derive, "evidence-plan": cmd_evidence_plan,
        "derivations": cmd_derivations, "explain": cmd_explain,
        "snapshot": cmd_snapshot, "verify": cmd_verify, "summary": cmd_summary, "run": cmd_run,
        "replay": cmd_replay,
        "assess-method": cmd_assess_method,
        "method-assessments": cmd_method_assessments,
        "review-method": cmd_review_method,
        "graph-hash": cmd_graph_hash, "graph-propose": cmd_graph_propose,
        "graph-apply": cmd_graph_apply,
        "release-create": cmd_release_create, "release-verify": cmd_release_verify,
        "release-diff": cmd_release_diff,
        "view": cmd_view,
        "graphrag-export": cmd_graphrag_export,
        "graphrag-context": cmd_graphrag_context,
        "init": cmd_init, "install-skill": cmd_install_skill,
    }
    try:
        return dispatch[args.cmd](args)
    except GraphError as e:
        print(f"provsleuth: {_terminal_text(e)}", file=sys.stderr)
        return 2
    except EventError as e:
        print(f"provsleuth: {_terminal_text(e)}", file=sys.stderr)
        return 2
    except AssessmentError as e:
        print(f"provsleuth: {_terminal_text(e)}", file=sys.stderr)
        return 2
    except LogicError as e:
        print(f"provsleuth: {_terminal_text(e)}", file=sys.stderr)
        return 2
    except SemanticError as e:
        print(f"provsleuth: {_terminal_text(e)}", file=sys.stderr)
        return 2
    except ReplayError as e:
        print(f"provsleuth: {_terminal_text(e)}", file=sys.stderr)
        return 2
    except ReleaseError as e:
        print(f"provsleuth: {_terminal_text(e)}", file=sys.stderr)
        return 2
    except MethodAssessmentError as e:
        print(f"provsleuth: {_terminal_text(e)}", file=sys.stderr)
        return 2
    except GraphRAGError as e:
        print(f"provsleuth: {_terminal_text(e)}", file=sys.stderr)
        return 2
    except DeliberationError as e:
        print(f"provsleuth: {_terminal_text(e)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
