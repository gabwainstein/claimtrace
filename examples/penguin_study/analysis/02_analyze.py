"""Fit pooled and species-specific OLS associations using only the stdlib."""
from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from pathlib import Path


SPECIES = ("Adelie", "Chinstrap", "Gentoo")


def ols(rows: list[tuple[float, float]]) -> dict[str, float | int]:
    xs = [row[0] for row in rows]
    ys = [row[1] for row in rows]
    n = len(rows)
    x_bar = sum(xs) / n
    y_bar = sum(ys) / n
    sxx = sum((x - x_bar) ** 2 for x in xs)
    syy = sum((y - y_bar) ** 2 for y in ys)
    sxy = sum((x - x_bar) * (y - y_bar) for x, y in rows)
    slope = sxy / sxx
    intercept = y_bar - slope * x_bar
    pearson_r = sxy / math.sqrt(sxx * syy)
    return {
        "n": n,
        "slope": round(slope, 6),
        "intercept": round(intercept, 6),
        "pearson_r": round(pearson_r, 6),
        "r2": round(pearson_r * pearson_r, 6),
    }


def main() -> None:
    complete: list[tuple[str, float, float]] = []
    total = 0
    with Path("data/penguins_clean.csv").open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            total += 1
            if row["bill_length_mm"] == "NA" or row["bill_depth_mm"] == "NA":
                continue
            complete.append(
                (row["species"], float(row["bill_length_mm"]), float(row["bill_depth_mm"]))
            )

    grouped: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for species, bill_length, bill_depth in complete:
        grouped[species].append((bill_length, bill_depth))
    if tuple(sorted(grouped)) != tuple(sorted(SPECIES)):
        raise RuntimeError(f"expected species {SPECIES}, observed {tuple(sorted(grouped))}")

    pooled_rows = [(bill_length, bill_depth) for _, bill_length, bill_depth in complete]
    pooled = ols(pooled_rows)
    by_species = {species: ols(grouped[species]) for species in SPECIES}

    centered_sxx = 0.0
    centered_sxy = 0.0
    for species in SPECIES:
        rows = grouped[species]
        x_bar = sum(x for x, _ in rows) / len(rows)
        y_bar = sum(y for _, y in rows) / len(rows)
        centered_sxx += sum((x - x_bar) ** 2 for x, _ in rows)
        centered_sxy += sum((x - x_bar) * (y - y_bar) for x, y in rows)
    common_within_species_slope = centered_sxy / centered_sxx

    reversal = pooled["slope"] < 0 and all(
        by_species[species]["slope"] > 0 for species in SPECIES
    )
    result = {
        "schema_version": "claimtrace.penguin-slopes/1",
        "study_id": "palmerpenguins-v0.1.0-bill-complete-cases",
        "dataset_version": "palmerpenguins-v0.1.0",
        "analysis": "OLS bill_depth_mm ~ bill_length_mm",
        "total_rows": total,
        "complete_case_n": len(complete),
        "missing_excluded": total - len(complete),
        "pooled": pooled,
        "by_species": by_species,
        "common_within_species_slope": round(common_within_species_slope, 6),
        "pattern": (
            "pooled_negative_each_species_positive"
            if reversal
            else "configured_sign_reversal_not_observed"
        ),
        "interpretation_boundary": (
            "Descriptive association in these complete records; no causal or population-level "
            "biological inference."
        ),
    }
    Path("results").mkdir(exist_ok=True)
    with Path("results/slopes.json").open(
        "w", encoding="utf-8", newline="\n"
    ) as handle:
        json.dump(result, handle, indent=2, sort_keys=True)
        handle.write("\n")
    signs = ", ".join(
        f"{species}={by_species[species]['slope']:.6f}" for species in SPECIES
    )
    print(f"pooled={pooled['slope']:.6f}; {signs}; n={len(complete)}")


if __name__ == "__main__":
    main()
