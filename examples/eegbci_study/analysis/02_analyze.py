"""Leave-one-run-out CSP+LDA decoding with a within-run permutation null."""
from __future__ import annotations

import os

# Use a fixed one-thread numerical boundary instead of inheriting ambient BLAS settings.
for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[variable] = "1"

import json
from pathlib import Path

import mne
import numpy as np
import sklearn
from mne.decoding import CSP
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis
from sklearn.metrics import accuracy_score, balanced_accuracy_score
from sklearn.pipeline import Pipeline


SEED = 20260714
N_PERMUTATIONS = 199
N_COMPONENTS = 4


def make_pipeline() -> Pipeline:
    return Pipeline(
        [
            (
                "csp",
                CSP(
                    n_components=N_COMPONENTS,
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


def evaluate(x: np.ndarray, y: np.ndarray, groups: np.ndarray, details: bool = False):
    scores: list[float] = []
    folds: list[dict[str, object]] = []
    for held_out in sorted(np.unique(groups).tolist()):
        test = groups == held_out
        train = ~test
        train_classes = np.unique(y[train])
        test_classes = np.unique(y[test])
        valid = len(train_classes) == 2 and len(test_classes) == 2
        if not valid:
            scores.append(float("nan"))
            if details:
                folds.append(
                    {
                        "test_run": int(held_out),
                        "train_runs": [int(value) for value in sorted(np.unique(groups[train]))],
                        "valid": False,
                        "reason": "both classes are required in train and test",
                    }
                )
            continue
        model = make_pipeline()
        model.fit(x[train], y[train])
        predicted = model.predict(x[test])
        balanced = float(balanced_accuracy_score(y[test], predicted))
        scores.append(balanced)
        if details:
            folds.append(
                {
                    "test_run": int(held_out),
                    "train_runs": [int(value) for value in sorted(np.unique(groups[train]))],
                    "valid": True,
                    "n_train": int(train.sum()),
                    "n_test": int(test.sum()),
                    "train_class_counts": {
                        str(int(label)): int((y[train] == label).sum()) for label in train_classes
                    },
                    "test_class_counts": {
                        str(int(label)): int((y[test] == label).sum()) for label in test_classes
                    },
                    "balanced_accuracy": round(balanced, 6),
                    "accuracy": round(float(accuracy_score(y[test], predicted)), 6),
                }
            )
    return np.asarray(scores, dtype=float), folds


def permute_within_run(y: np.ndarray, groups: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    permuted = y.copy()
    for run in sorted(np.unique(groups).tolist()):
        indices = np.flatnonzero(groups == run)
        permuted[indices] = rng.permutation(permuted[indices])
    return permuted


def main() -> None:
    mne.set_log_level("ERROR")
    with np.load("data/prepared_epochs.npz", allow_pickle=False) as prepared:
        x = prepared["x"]
        y = prepared["y"].astype(np.int64)
        groups = prepared["runs"].astype(np.int64)

    observed_folds, fold_details = evaluate(x, y, groups, details=True)
    all_folds_valid = bool(np.isfinite(observed_folds).all() and len(observed_folds) == 3)
    if not all_folds_valid:
        raise RuntimeError("leave-one-run-out validation requires three finite two-class folds")
    observed = float(observed_folds.mean())

    rng = np.random.default_rng(SEED)
    null_scores = []
    for _ in range(N_PERMUTATIONS):
        permuted_y = permute_within_run(y, groups, rng)
        scores, _ = evaluate(x, permuted_y, groups)
        if not np.isfinite(scores).all():
            raise RuntimeError("a run-constrained permutation produced an invalid fold")
        null_scores.append(float(scores.mean()))
    null = np.asarray(null_scores, dtype=float)
    null_95 = float(np.quantile(null, 0.95, method="higher"))
    empirical_p = float((1 + np.count_nonzero(null >= observed)) / (N_PERMUTATIONS + 1))
    criterion_met = bool(all_folds_valid and observed > null_95)

    result = {
        "schema_version": 1,
        "study_id": "eegbci-s001-r06-r10-r14-v1.0.0",
        "participant": "S001",
        "runs": [6, 10, 14],
        "contrast": "both_hands_imagery versus both_feet_imagery",
        "validation": "leave-one-run-out",
        "model": {
            "features": "CSP log average power",
            "csp_components": N_COMPONENTS,
            "csp_covariance_regularization": "Ledoit-Wolf",
            "classifier": "LDA with automatic shrinkage",
            "fitting_boundary": "CSP and LDA refit using training runs only in every fold and permutation",
        },
        "folds": fold_details,
        "all_folds_valid": all_folds_valid,
        "observed_mean_balanced_accuracy": round(observed, 6),
        "criterion_met": criterion_met,
        "criterion": "all folds valid and observed mean balanced accuracy > run-constrained null 95th percentile",
        "permutation": {
            "scheme": "labels shuffled independently within each run; full pipeline refit",
            "seed": SEED,
            "n_permutations": N_PERMUTATIONS,
            "null_mean_balanced_accuracy": round(float(null.mean()), 6),
            "null_95th_percentile": round(null_95, 6),
            "quantile_method": "higher",
            "empirical_p_one_sided": round(empirical_p, 6),
            "scores": [round(value, 6) for value in null.tolist()],
        },
        "software": {
            "mne": mne.__version__,
            "numpy": np.__version__,
            "scikit_learn": sklearn.__version__,
            "thread_limits": {
                variable: os.environ[variable]
                for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")
            },
        },
        "limitations": [
            "This validates across three runs from one participant, not across participants.",
            "The run-constrained null addresses label exchangeability within run but not every form of temporal or recording confounding.",
            "The pipeline and threshold were not benchmarked against alternative preprocessing or models in this demo.",
            "Balanced accuracy above this null would not establish neural mechanism, source localization, or clinical BCI utility.",
        ],
    }
    output = Path("results/decoding.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    verdict = "met" if criterion_met else "not met"
    print(
        f"observed mean balanced accuracy={observed:.4f}; null95={null_95:.4f}; "
        f"p={empirical_p:.4f}; criterion {verdict} -> {output}"
    )


if __name__ == "__main__":
    main()
