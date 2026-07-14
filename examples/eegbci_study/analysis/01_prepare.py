"""Prepare fixed motor-imagery epochs from S001 runs 06, 10, and 14."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import mne
import numpy as np
from mne.datasets import eegbci


RUNS = (6, 10, 14)
CHANNELS = ("FC3", "FCz", "FC4", "C3", "Cz", "C4", "CP3", "CPz", "CP4")
EVENT_ID = {"T1": 1, "T2": 2}
LABELS = {0: "both_hands_imagery", 1: "both_feet_imagery"}
L_FREQ = 7.0
H_FREQ = 30.0
TMIN = 1.0
TMAX = 2.0


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    mne.set_log_level("ERROR")
    all_x: list[np.ndarray] = []
    all_y: list[np.ndarray] = []
    all_runs: list[np.ndarray] = []
    run_reports: list[dict[str, object]] = []

    for run in RUNS:
        path = Path("data") / f"S001R{run:02d}.edf"
        raw = mne.io.read_raw_edf(path, preload=True, verbose="ERROR")
        eegbci.standardize(raw)
        missing = sorted(set(CHANNELS) - set(raw.ch_names))
        if missing:
            raise RuntimeError(f"missing prespecified channels in run {run}: {missing}")
        if float(raw.info["sfreq"]) != 160.0:
            raise RuntimeError(f"unexpected sampling frequency in run {run}: {raw.info['sfreq']}")

        raw.set_eeg_reference("average", projection=True, verbose="ERROR")
        raw.filter(L_FREQ, H_FREQ, method="fir", phase="zero", fir_design="firwin", verbose="ERROR")
        events, _ = mne.events_from_annotations(raw, event_id=EVENT_ID, verbose="ERROR")
        epochs = mne.Epochs(
            raw,
            events,
            event_id={"both_hands_imagery": 1, "both_feet_imagery": 2},
            tmin=TMIN,
            tmax=TMAX,
            baseline=None,
            picks=list(CHANNELS),
            preload=True,
            proj=True,
            reject_by_annotation=True,
            verbose="ERROR",
        )
        x = epochs.get_data(copy=True)
        y = epochs.events[:, 2].astype(np.int64) - 1
        if set(np.unique(y).tolist()) != {0, 1}:
            raise RuntimeError(f"run {run} does not contain both T1 and T2 epochs")
        all_x.append(x)
        all_y.append(y)
        all_runs.append(np.full(y.shape, run, dtype=np.int64))
        counts = Counter(int(value) for value in y)
        run_reports.append(
            {
                "run": run,
                "source_path": path.as_posix(),
                "source_sha256": sha256(path),
                "retained_epochs": int(len(y)),
                "class_counts": {LABELS[key]: counts[key] for key in sorted(counts)},
                "dropped_epochs": int(sum(len(item) > 0 for item in epochs.drop_log)),
            }
        )

    x = np.concatenate(all_x, axis=0)
    y = np.concatenate(all_y, axis=0)
    groups = np.concatenate(all_runs, axis=0)
    data_dir = Path("data")
    results_dir = Path("results")
    data_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        data_dir / "prepared_epochs.npz",
        x=x,
        y=y,
        runs=groups,
        channels=np.asarray(CHANNELS),
        sfreq=np.asarray(160.0),
        tmin=np.asarray(TMIN),
        tmax=np.asarray(TMAX),
    )
    report = {
        "schema_version": 1,
        "study_id": "eegbci-s001-r06-r10-r14-v1.0.0",
        "dataset_version": "PhysioNet EEGMMIDB 1.0.0",
        "participant": "S001",
        "runs": list(RUNS),
        "task": "imagined movement of both hands versus both feet",
        "event_mapping": {"T1": "both_hands_imagery", "T2": "both_feet_imagery"},
        "channels": list(CHANNELS),
        "sampling_frequency_hz": 160.0,
        "filter_hz": [L_FREQ, H_FREQ],
        "epoch_seconds_after_onset": [TMIN, TMAX],
        "reference": "fixed average-reference projection",
        "total_epochs": int(len(y)),
        "array_shape": list(x.shape),
        "mne_version": mne.__version__,
        "numpy_version": np.__version__,
        "run_reports": run_reports,
        "limitations": [
            "Only one participant and three repeated runs are included.",
            "No amplitude-based artifact rejection or manually judged ICA was applied.",
            "The fixed nine-channel subset and 7-30 Hz band do not establish source localization.",
        ],
    }
    output = results_dir / "preprocessing.json"
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"prepared {x.shape[0]} epochs with shape {x.shape} -> data/prepared_epochs.npz")
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
