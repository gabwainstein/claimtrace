"""Numeric checks for the widget study. Run with `claimtrace verify`.

Paths are relative to the project root (claimtrace sets the working directory there).
"""
import json

from claimtrace import check, approx


@check("claim:slope — OLS slope ~ 2.0")
def slope_matches_claim():
    fit = json.load(open("results/fit.json"))
    ok = approx(fit["slope"], 2.0, 0.2)
    return ok, f"slope={fit['slope']}", "2.0 +- 0.2"


@check("claim:slope — fit quality R2 > 0.9")
def fit_is_good():
    fit = json.load(open("results/fit.json"))
    ok = fit["r2"] > 0.9
    return ok, f"r2={fit['r2']}", "> 0.9"
