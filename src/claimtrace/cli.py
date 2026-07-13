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
from .assessment import (AssessmentError, append_assessment, append_review_transition,
                         create_assessment, evaluate_assessment, load_assessments)
from .config import CONFIG_NAME, load_config, strict_json_loads
from .engine import (ANNOT_RELS, GraphError, compute_check, downstream, impact,
                     lint_issues, load_graph, log_entry, upstream)
from .events import EventError, run_command
from .logic import (SELECTION_SCHEMA, LogicError, append_derivation,
                    configured_logic_assets, create_derivation,
                    create_derivation_from_bindings, evaluate_derivation,
                    load_derivations, load_logic_asset)
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


def _read_json_object(path_value, label):
    try:
        value = strict_json_loads(
            Path(path_value).read_text(encoding="utf-8-sig"), str(path_value)
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise AssessmentError(f"cannot read {label} {path_value}: {exc}") from exc
    if not isinstance(value, dict):
        raise AssessmentError(f"{label} must contain one JSON object")
    return value


def _write_json(value):
    sys.stdout.write(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ) + "\n")


def _assessment_output(document, evaluation, path=None):
    return {
        "id": document["id"],
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
            "mechanical_snapshot and derived are computed by claimtrace"
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
        print(f"claimtrace assess: recorded {document['id']}")
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
        "integrity": "error" if issues else "ok",
        "issues": issues,
        "items": items,
    }
    if args.json:
        _write_json(output)
    else:
        print(f"claimtrace assessments: {len(items)} assessment(s), integrity {output['integrity']}")
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
        print(f"claimtrace review: {transition['id']}")
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
    if set(entry) == selection_expected and entry.get("schema_version") == SELECTION_SCHEMA:
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
            "derivation proposal must be either a claimtrace.symbolic-selection/1 "
            "binding selection or the exact low-level claim_id/result_ids/vocabulary_id/"
            "rule_pack_id/agent_input form; mechanical_snapshot and derived are computed "
            "by claimtrace, and target or policy fields cannot be added to selection proposals"
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
        print(f"claimtrace derive: recorded {document['id']}")
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
            f"claimtrace derivations: {len(items)} derivation(s), "
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
         "events": "claimtrace/events", "assessments": "claimtrace/assessments",
         "render_types": ["figure"], "input_types": ["data", "artifact", "code"],
         "run_output_types": ["artifact"], "require_assessments": False,
         "logic": {
             "derivations": "claimtrace/derivations",
             "vocabularies": [],
             "rule_packs": [],
             "allow_external_packs": False,
             "require_derivations": False,
             "max_provenance_bytes": 64 * 1024 * 1024 * 1024,
         }},
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
    p = sub.add_parser("derive", help="append a grounded project-rule symbolic derivation")
    p.add_argument(
        "entry",
        help=("recommended claimtrace.symbolic-selection/1 binding proposal; "
              "the exact low-level typed-fact form is also accepted"),
    )
    p.add_argument("--actor", required=True, help="identity submitting the grounded premises")
    p.add_argument("--json", action="store_true", help="emit the recorded derivation as JSON")
    p = sub.add_parser("derivations", help="list symbolic derivations under current rule assets")
    p.add_argument("--state", choices=("derivable", "refutable", "conflict", "unknown"))
    p.add_argument("--json", action="store_true", help="emit one deterministic JSON document")
    p = sub.add_parser("explain", help="show one symbolic derivation or canonical proof")
    p.add_argument("derivation_id", help="content-addressed derivation or proof id")
    p.add_argument("--json", action="store_true", help="emit one deterministic JSON document")
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
        "assess": cmd_assess, "assessments": cmd_assessments, "review": cmd_review,
        "derive": cmd_derive, "derivations": cmd_derivations, "explain": cmd_explain,
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
    except AssessmentError as e:
        print(f"claimtrace: {e}", file=sys.stderr)
        return 2
    except LogicError as e:
        print(f"claimtrace: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
