"""Mechanical and numerical integrity checks for the EEGBCI example."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

# Match the analysis's deterministic numerical boundary before importing the
# scientific stack. The verifier still rebuilds the model independently.
for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[variable] = "1"

import mne
import numpy as np
from mne.decoding import CSP
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis
from sklearn.metrics import balanced_accuracy_score
from sklearn.pipeline import Pipeline

from claimtrace import check


EXPECTED_HASHES = {
    "data/S001R06.edf": "5369364f2c4e81ca141679d6dd2ba6ece61c7eb53d7fae31241b308876e1b6b3",
    "data/S001R10.edf": "20de1c7746c2349d16bda5e9f1b0ac7b7ad1581102a2e30dd2ac422696f62fb1",
    "data/S001R14.edf": "2110c48e3106898e3dbca47e39b330637afd3d3b8bc2da3ba1e44f4ac1118137",
}
EXPECTED_RUNS = (6, 10, 14)
EXPECTED_SEED = 20260714
EXPECTED_PERMUTATIONS = 199
SCORE_DECIMALS = 6


def sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def score_sha256(scores: list[float]) -> str:
    payload = json.dumps(scores, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def decoding_pipeline() -> Pipeline:
    """Build the pinned model without importing the analysis implementation."""
    return Pipeline(
        [
            (
                "csp",
                CSP(
                    n_components=4,
                    reg="ledoit_wolf",
                    log=True,
                    norm_trace=False,
                    cov_est="epoch",
                    transform_into="average_power",
                ),
            ),
            ("lda", LinearDiscriminantAnalysis(solver="lsqr", shrinkage="auto")),
        ]
    )


def recompute_fold_scores(
        x: np.ndarray, y: np.ndarray, runs: np.ndarray) -> dict[int, float]:
    """Refit CSP and LDA for each held-out run and return full-precision scores."""
    scores = {}
    for held_out in EXPECTED_RUNS:
        test = runs == held_out
        train = ~test
        model = decoding_pipeline()
        model.fit(x[train], y[train])
        predicted = model.predict(x[test])
        scores[held_out] = float(balanced_accuracy_score(y[test], predicted))
    return scores


def permute_labels_within_runs(
        y: np.ndarray, runs: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    permuted = y.copy()
    for run in EXPECTED_RUNS:
        indices = np.flatnonzero(runs == run)
        permuted[indices] = rng.permutation(permuted[indices])
    return permuted


@check("source EDF files match PhysioNet's published SHA-256 values")
def source_hashes_match():
    observed = {path: sha256(path) for path in EXPECTED_HASHES}
    ok = observed == EXPECTED_HASHES
    return ok, json.dumps(observed, sort_keys=True), json.dumps(EXPECTED_HASHES, sort_keys=True)


@check("prepared epochs contain the fixed runs and both classes per run")
def prepared_structure_matches_plan():
    with np.load("data/prepared_epochs.npz", allow_pickle=False) as prepared:
        x = prepared["x"]
        y = prepared["y"]
        runs = prepared["runs"]
        channels = prepared["channels"].tolist()
    run_set = sorted(np.unique(runs).tolist())
    class_sets = {int(run): sorted(np.unique(y[runs == run]).tolist()) for run in run_set}
    report = json.loads(Path("results/preprocessing.json").read_text(encoding="utf-8"))
    report_counts = {
        item["run"]: (item["retained_epochs"], item["class_counts"])
        for item in report["run_reports"]
    }
    expected_counts = {
        run: (15, {"both_hands_imagery": 7, "both_feet_imagery": 8})
        for run in (6, 10, 14)
    }
    ok = (
        run_set == [6, 10, 14]
        and all(values == [0, 1] for values in class_sets.values())
        and x.shape[0] == y.shape[0] == runs.shape[0]
        and x.shape[1] == len(channels) == 9
        and report["total_epochs"] == 45
        and report_counts == expected_counts
    )
    return ok, f"shape={x.shape}, runs={run_set}, classes={class_sets}, counts={report_counts}", "15 epochs/run (7 hands, 8 feet), 9 channels"


@check("leave-one-run-out folds keep train and test runs disjoint")
def folds_are_group_disjoint():
    result = json.loads(Path("results/decoding.json").read_text(encoding="utf-8"))
    folds = result["folds"]
    held_out = sorted(fold["test_run"] for fold in folds)
    disjoint = all(fold["test_run"] not in fold["train_runs"] for fold in folds)
    ok = held_out == [6, 10, 14] and disjoint and all(fold["valid"] for fold in folds)
    return ok, f"held_out={held_out}, disjoint={disjoint}", "each of 6, 10, 14 held out once"


@check("observed folds and every seeded permutation score reproduce from prepared epochs")
def decoding_scores_reproduce_from_prepared_epochs():
    mne.set_log_level("ERROR")
    with np.load("data/prepared_epochs.npz", allow_pickle=False) as prepared:
        x = prepared["x"]
        y = prepared["y"].astype(np.int64)
        runs = prepared["runs"].astype(np.int64)
    result = json.loads(Path("results/decoding.json").read_text(encoding="utf-8"))

    recomputed_folds_raw = recompute_fold_scores(x, y, runs)
    recomputed_folds = {
        run: round(score, SCORE_DECIMALS)
        for run, score in recomputed_folds_raw.items()
    }
    rng = np.random.default_rng(EXPECTED_SEED)
    recomputed_null = []
    for _ in range(EXPECTED_PERMUTATIONS):
        permuted = permute_labels_within_runs(y, runs, rng)
        fold_scores = recompute_fold_scores(x, permuted, runs)
        recomputed_null.append(round(
            float(np.mean([fold_scores[run] for run in EXPECTED_RUNS])),
            SCORE_DECIMALS,
        ))

    reported_folds = {
        int(fold["test_run"]): float(fold["balanced_accuracy"])
        for fold in result["folds"]
    }
    reported_null = [float(value) for value in result["permutation"]["scores"]]
    null_mismatches = [
        index for index in range(max(len(recomputed_null), len(reported_null)))
        if index >= len(recomputed_null)
        or index >= len(reported_null)
        or recomputed_null[index] != reported_null[index]
    ]
    recomputed_mean = round(
        float(np.mean([recomputed_folds_raw[run] for run in EXPECTED_RUNS])),
        SCORE_DECIMALS,
    )
    ok = (
        sorted(np.unique(runs).tolist()) == list(EXPECTED_RUNS)
        and recomputed_folds == reported_folds
        and recomputed_mean == result["observed_mean_balanced_accuracy"]
        and len(recomputed_null) == len(reported_null) == EXPECTED_PERMUTATIONS
        and not null_mismatches
    )
    live = {
        "folds": recomputed_folds,
        "observed_mean": recomputed_mean,
        "null_count": len(recomputed_null),
        "null_scores_sha256": score_sha256(recomputed_null),
        "null_mismatch_count": len(null_mismatches),
        "first_null_mismatches": null_mismatches[:10],
    }
    expected = {
        "folds": reported_folds,
        "observed_mean": result["observed_mean_balanced_accuracy"],
        "null_count": len(reported_null),
        "null_scores_sha256": score_sha256(reported_null),
        "seed": EXPECTED_SEED,
    }
    return ok, json.dumps(live, sort_keys=True), json.dumps(expected, sort_keys=True)


@check("reported observed mean and null criterion are arithmetically consistent")
def reported_criterion_is_consistent():
    result = json.loads(Path("results/decoding.json").read_text(encoding="utf-8"))
    fold_mean = round(sum(fold["balanced_accuracy"] for fold in result["folds"]) / 3, 6)
    observed = result["observed_mean_balanced_accuracy"]
    threshold = result["permutation"]["null_95th_percentile"]
    scores = np.asarray(result["permutation"]["scores"], dtype=float)
    recomputed_threshold = round(float(np.quantile(scores, 0.95, method="higher")), 6)
    recomputed_p = round(float((1 + np.count_nonzero(scores >= observed)) / 200), 6)
    expected_criterion = result["all_folds_valid"] and observed > threshold
    ok = (
        fold_mean == observed
        and result["criterion_met"] == expected_criterion
        and result["permutation"]["n_permutations"] == 199
        and result["permutation"]["seed"] == 20260714
        and len(scores) == 199
        and threshold == recomputed_threshold
        and result["permutation"]["empirical_p_one_sided"] == recomputed_p
    )
    return ok, f"mean={observed}, null95={threshold}, p={recomputed_p}, criterion={result['criterion_met']}", "recomputed null95 and p match"
