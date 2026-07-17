"""Closed graph-schema tests for authored method meaning and claim requirements."""
import copy
import json

import pytest

from provsleuth import engine
from provsleuth.config import Config


def _method(method_id="method:primary", *, status="current", steps=None):
    if steps is None:
        steps = [
            {"id": "clean", "statement": "Remove incomplete rows.", "required": True},
            {"id": "fit", "statement": "Fit ordinary least squares.", "required": True},
            {"id": "plot", "statement": "Render a diagnostic plot.", "required": False},
        ]
    return {
        "id": method_id,
        "type": "method",
        "status": status,
        "method_spec": {
            "schema_version": "claimtrace.method-spec/1",
            "steps": steps,
        },
    }


def _claim(claim_type="claim", *, methods=None):
    if methods is None:
        methods = [{"method_id": "method:primary", "step_ids": ["clean", "fit"]}]
    return {
        "id": f"{claim_type}:primary",
        "type": claim_type,
        "status": "current",
        "method_requirements": {
            "schema_version": "claimtrace.method-requirements/1",
            "methods": methods,
        },
    }


def _config(tmp_path, nodes):
    trace = tmp_path / "provsleuth"
    trace.mkdir(parents=True)
    (trace / "graph.json").write_text(json.dumps({
        "schema_version": "1.0", "concepts": {}, "nodes": nodes, "edges": [],
    }), encoding="utf-8")
    config_path = tmp_path / "provsleuth.config.json"
    config_path.write_text(json.dumps({
        "root": ".", "graph": "provsleuth/graph.json",
    }), encoding="utf-8")
    return Config(config_path)


@pytest.mark.parametrize("claim_type", ["claim", "hypothesis", "prediction", "conclusion"])
def test_closed_method_spec_and_claim_requirements_are_valid(tmp_path, claim_type):
    cfg = _config(tmp_path, [_method(), _claim(claim_type)])

    nodes, _edges, _concepts = engine.load_graph(cfg)

    assert nodes[f"{claim_type}:primary"]["method_requirements"]["methods"] == [{
        "method_id": "method:primary", "step_ids": ["clean", "fit"],
    }]


def test_method_semantic_fields_are_optional(tmp_path):
    cfg = _config(tmp_path, [
        {"id": "method:unformalized", "type": "method", "status": "current"},
        {"id": "claim:unformalized", "type": "claim", "status": "current"},
    ])

    assert set(engine.load_graph(cfg)[0]) == {"method:unformalized", "claim:unformalized"}


@pytest.mark.parametrize("node, message", [
    (
        {
            "id": "code:x", "type": "code", "status": "current",
            "method_spec": {"schema_version": "claimtrace.method-spec/1", "steps": []},
        },
        "method_spec.*only valid on method nodes",
    ),
    (
        {
            "id": "method:x", "type": "method", "status": "current",
            "method_requirements": {
                "schema_version": "claimtrace.method-requirements/1", "methods": [],
            },
        },
        "method_requirements.*only valid on claim",
    ),
])
def test_method_semantic_fields_are_rejected_on_wrong_node_types(tmp_path, node, message):
    cfg = _config(tmp_path, [node])

    with pytest.raises(engine.GraphError, match=message):
        engine.load_raw(cfg)


@pytest.mark.parametrize("mutate, message", [
    (
        lambda node: node["method_spec"].update({"extra": True}),
        "must contain exactly schema_version and steps",
    ),
    (
        lambda node: node["method_spec"].update({"schema_version": "claimtrace.method-spec/2"}),
        "unsupported method_spec schema",
    ),
    (
        lambda node: node["method_spec"].update({"steps": []}),
        "non-empty list",
    ),
    (
        lambda node: node["method_spec"]["steps"][0].update({"extra": True}),
        "must contain exactly id, statement, and required",
    ),
    (
        lambda node: node["method_spec"]["steps"][0].update({"id": "not allowed"}),
        "must be a 1-256 character identifier",
    ),
    (
        lambda node: node["method_spec"]["steps"][0].update({"statement": "   "}),
        "statement must be non-empty text",
    ),
    (
        lambda node: node["method_spec"]["steps"][0].update({"required": 1}),
        "required must be a boolean",
    ),
    (
        lambda node: node["method_spec"]["steps"].append(
            {"id": "clean", "statement": "A duplicate.", "required": False}
        ),
        "duplicate method step",
    ),
])
def test_method_spec_is_closed_and_typed(tmp_path, mutate, message):
    node = _method()
    mutate(node)
    cfg = _config(tmp_path, [node])

    with pytest.raises(engine.GraphError, match=message):
        engine.load_raw(cfg)


def test_method_spec_step_count_is_bounded(tmp_path):
    steps = [
        {"id": f"s{index}", "statement": "Bounded step.", "required": True}
        for index in range(engine.MAX_METHOD_STEPS + 1)
    ]
    cfg = _config(tmp_path, [_method(steps=steps)])

    with pytest.raises(engine.GraphError, match="at most 2000 steps"):
        engine.load_raw(cfg)


@pytest.mark.parametrize("mutate, message", [
    (
        lambda node: node["method_requirements"].update({"extra": True}),
        "must contain exactly schema_version and methods",
    ),
    (
        lambda node: node["method_requirements"].update({
            "schema_version": "claimtrace.method-requirements/2",
        }),
        "unsupported method_requirements schema",
    ),
    (
        lambda node: node["method_requirements"].update({"methods": []}),
        "non-empty list",
    ),
    (
        lambda node: node["method_requirements"]["methods"][0].update({"extra": True}),
        "must contain exactly method_id and step_ids",
    ),
    (
        lambda node: node["method_requirements"]["methods"][0].update({"method_id": "bad id"}),
        "must be a 1-256 character identifier",
    ),
    (
        lambda node: node["method_requirements"]["methods"][0].update({"step_ids": []}),
        "step_ids must be a non-empty list",
    ),
])
def test_method_requirements_are_closed_and_typed(tmp_path, mutate, message):
    claim = _claim()
    mutate(claim)
    cfg = _config(tmp_path, [_method(), claim])

    with pytest.raises(engine.GraphError, match=message):
        engine.load_raw(cfg)


def test_duplicate_method_and_step_references_are_rejected(tmp_path):
    duplicate_method = _claim(methods=[
        {"method_id": "method:primary", "step_ids": ["clean"]},
        {"method_id": "method:primary", "step_ids": ["fit"]},
    ])
    with pytest.raises(engine.GraphError, match="references method .* more than once"):
        engine.load_raw(_config(tmp_path / "method", [_method(), duplicate_method]))

    duplicate_step = _claim(methods=[{
        "method_id": "method:primary", "step_ids": ["clean", "clean"],
    }])
    with pytest.raises(engine.GraphError, match="references method step .* more than once"):
        engine.load_raw(_config(tmp_path / "step", [_method(), duplicate_step]))


@pytest.mark.parametrize("target, message", [
    (None, "references missing method node"),
    ({"id": "method:primary", "type": "code", "status": "current"},
     "does not identify a method node"),
    ({"id": "method:primary", "type": "method", "status": "current"},
     "needs a method_spec"),
    (_method(status="deprecated"), "references inactive method node"),
])
def test_claim_requirements_resolve_to_active_formalized_methods(tmp_path, target, message):
    nodes = [_claim()]
    if target is not None:
        nodes.insert(0, target)
    cfg = _config(tmp_path, nodes)

    with pytest.raises(engine.GraphError, match=message):
        engine.load_raw(cfg)


def test_claim_requirements_reject_unknown_method_steps(tmp_path):
    claim = _claim(methods=[{
        "method_id": "method:primary", "step_ids": ["clean", "invented"],
    }])
    cfg = _config(tmp_path, [_method(), claim])

    with pytest.raises(engine.GraphError, match="references unknown method step"):
        engine.load_raw(cfg)


def test_claim_may_explicitly_require_an_optional_declared_step(tmp_path):
    claim = copy.deepcopy(_claim())
    claim["method_requirements"]["methods"][0]["step_ids"] = ["plot"]
    cfg = _config(tmp_path, [_method(), claim])

    assert engine.load_raw(cfg)["nodes"][1]["method_requirements"]["methods"][0] == {
        "method_id": "method:primary", "step_ids": ["plot"],
    }
