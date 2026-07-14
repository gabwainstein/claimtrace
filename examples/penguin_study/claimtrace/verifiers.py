"""Independent deterministic checks for the Palmer Penguins demo."""
from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

from claimtrace import check


EXPECTED_HASHES = {
    "data/source/penguins_raw.csv": "144f623143c9360fd77322a4f86acb06dc198814dbd2669724c63e6457b907bd",
    "data/source/penguins.csv": "f204db2c753b0937caac3cb35258562c14f073e4bbc76be24b4c51ce22767a93",
}


def sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def slope(rows: list[tuple[float, float]]) -> float:
    x_bar = sum(x for x, _ in rows) / len(rows)
    y_bar = sum(y for _, y in rows) / len(rows)
    return sum((x - x_bar) * (y - y_bar) for x, y in rows) / sum(
        (x - x_bar) ** 2 for x, _ in rows
    )


def complete_rows() -> dict[str, list[tuple[float, float]]]:
    grouped = defaultdict(list)
    with Path("data/penguins_clean.csv").open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["bill_length_mm"] == "NA" or row["bill_depth_mm"] == "NA":
                continue
            grouped[row["species"]].append(
                (float(row["bill_length_mm"]), float(row["bill_depth_mm"]))
            )
    return grouped


@check("source CSVs match pinned palmerpenguins v0.1.0 SHA-256 hashes")
def source_hashes_match():
    actual = {path: sha256(path) for path in EXPECTED_HASHES}
    ok = actual == EXPECTED_HASHES
    return ok, actual, EXPECTED_HASHES


@check("raw-to-curated transform is byte-identical to the official curated CSV")
def curated_transform_matches():
    clean_hash = sha256("data/penguins_clean.csv")
    official_hash = sha256("data/source/penguins.csv")
    report = json.loads(Path("results/preprocess.json").read_text(encoding="utf-8"))
    ok = (
        clean_hash == official_hash == EXPECTED_HASHES["data/source/penguins.csv"]
        and report["byte_identical_to_official_curated"] is True
        and report["source_rows"] == report["output_rows"] == 344
    )
    return ok, {"clean_sha256": clean_hash, "report_match": report["byte_identical_to_official_curated"]}, {"official_sha256": official_hash, "rows": 344}


@check("reported slopes reproduce independent complete-case calculations")
def reported_slopes_recompute():
    grouped = complete_rows()
    result = json.loads(Path("results/slopes.json").read_text(encoding="utf-8"))
    pooled_rows = [row for species in sorted(grouped) for row in grouped[species]]
    recomputed = {"pooled": slope(pooled_rows)}
    recomputed.update({species: slope(grouped[species]) for species in sorted(grouped)})
    reported = {"pooled": result["pooled"]["slope"]}
    reported.update(
        {species: result["by_species"][species]["slope"] for species in sorted(grouped)}
    )
    ok = all(math.isclose(reported[key], value, abs_tol=5e-7) for key, value in recomputed.items())
    return ok, reported, {key: round(value, 6) for key, value in recomputed.items()}


@check("configured sign reversal and complete-case counts are present")
def sign_pattern_and_counts():
    result = json.loads(Path("results/slopes.json").read_text(encoding="utf-8"))
    counts = {species: result["by_species"][species]["n"] for species in sorted(result["by_species"])}
    signs_ok = result["pooled"]["slope"] < 0 and all(
        result["by_species"][species]["slope"] > 0 for species in result["by_species"]
    )
    ok = (
        signs_ok
        and result["pattern"] == "pooled_negative_each_species_positive"
        and result["complete_case_n"] == 342
        and result["missing_excluded"] == 2
        and counts == {"Adelie": 151, "Chinstrap": 68, "Gentoo": 123}
    )
    return ok, {"pattern": result["pattern"], "complete_case_n": result["complete_case_n"], "counts": counts}, {"pattern": "pooled_negative_each_species_positive", "complete_case_n": 342, "counts": {"Adelie": 151, "Chinstrap": 68, "Gentoo": 123}}
