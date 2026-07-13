"""Project-specific numeric verification (plugin model).

`claimtrace check` validates the graph STRUCTURE (paths exist, no cycles/dangling edges, nothing
current depends on a retired branch, renders aren't stale). It cannot know whether your headline
NUMBERS still reproduce — that needs project knowledge. So you write checks in a `verifiers.py`,
registered with the `@check` decorator, and `claimtrace verify` runs them.

    # verifiers.py  (path set in claimtrace.config.json -> "verifiers")
    from claimtrace import check, approx

    @check("slope of shininess ~ polish")
    def slope():
        import json
        fit = json.load(open("results/fit.json"))      # path is relative to project root
        ok = approx(fit["slope"], 2.0, 0.2)
        return ok, f"slope={fit['slope']:.3f}", "~2.0 +-0.2"

Each check returns (ok: bool, live: str, expect: str). Checks run with the working directory set
to the project root, so relative data paths resolve. Only `verify` needs your analysis deps
(numpy/pandas/...); the core engine stays stdlib-only.

SECURITY: `claimtrace verify` imports and EXECUTES the project's own `verifiers.py` (a plugin
model, same trust level as running the project's analysis code, like `conftest.py` or a Makefile).
Only run `verify` on projects you trust; in CI, do not auto-run it on pull requests from forks.
`check` never executes graph data — it is pure inspection and safe on untrusted checkouts.
"""
from __future__ import annotations
import importlib.util
import os
from pathlib import Path

_REGISTRY: list[tuple[str, callable]] = []


def check(name: str):
    """Register a numeric verifier. The wrapped fn returns (ok, live, expect)."""
    def deco(fn):
        _REGISTRY.append((name, fn))
        return fn
    return deco


registry = _REGISTRY


def approx(a, b, tol) -> bool:
    try:
        return abs(float(a) - float(b)) <= float(tol)
    except (TypeError, ValueError):
        return False


def run_verifiers(cfg) -> int:
    _REGISTRY.clear()
    if not cfg.verifiers:
        print("claimtrace verify: no verifiers configured (set 'verifiers' in claimtrace.config.json).")
        return 0
    vp = Path(cfg.verifiers)
    if not vp.exists():
        print(f"claimtrace verify: verifiers file not found: {vp}")
        return 1
    old = os.getcwd()
    os.chdir(cfg.root)                       # so relative data paths in checks resolve
    try:
        spec = importlib.util.spec_from_file_location("_claimtrace_verifiers", vp)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)         # importing registers the checks
        print("claimtrace verify — live numeric checks\n")
        oks = []
        for name, fn in _REGISTRY:
            try:
                ok, live, expect = fn()
            except Exception as e:               # a broken check is a failure, not a crash
                ok, live, expect = False, f"ERROR: {e}", "(check raised)"
            oks.append(bool(ok))
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}\n         live={live}\n         expect={expect}")
        print()
        if not oks:
            print("claimtrace verify: no checks registered.")
            return 0
        if all(oks):
            print(f"claimtrace verify: OK — {len(oks)} check(s) pass.")
            return 0
        print(f"claimtrace verify: FAIL — {oks.count(False)}/{len(oks)} check(s) drifted from their artifacts.")
        return 1
    finally:
        os.chdir(old)
