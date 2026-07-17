# Demo: motor-imagery decoding with EEGBCI

This is a compact, real-data ProvSleuth example. It asks whether a fixed sensor-space pipeline can
distinguish imagined movement of both hands from imagined movement of both feet in one participant.
It uses only participant `S001`, motor-imagery runs `06`, `10`, and `14` from PhysioNet's EEG Motor
Movement/Imagery Dataset version 1.0.0.

The example is deliberately narrow. It is a within-participant, within-dataset pipeline
demonstration, not evidence of population-level BCI performance, neural mechanism, source
localization, clinical usefulness, or generalization to another recording session. CSP components
are discriminative sensor-space filters, not localized brain sources.

## Data and attribution

- Dataset: [EEG Motor Movement/Imagery Dataset v1.0.0](https://physionet.org/content/eegmmidb/1.0.0/)
- Dataset DOI: [10.13026/C28G6P](https://doi.org/10.13026/C28G6P)
- License: [Open Data Commons Attribution License v1.0](https://physionet.org/content/eegmmidb/view-license/1.0.0/)
- Analysis pattern: [MNE's CSP motor-imagery example](https://mne.tools/stable/auto_examples/decoding/decoding_csp_eeg.html)
- BCI2000 system paper: Schalk G, McFarland DJ, Hinterberger T, Birbaumer N, Wolpaw JR
  (2004), <https://doi.org/10.1109/TBME.2004.827072>.
- Standard PhysioNet paper: Goldberger AL et al. (2000),
  <https://doi.org/10.1161/01.CIR.101.23.E215>.

PhysioNet documents that runs 6, 10, and 14 are imagined movement of both fists versus both feet.
For these runs, annotation `T1` means both fists/hands and `T2` means both feet. The fetch script
pins the three versioned EDF URLs to the SHA-256 values published in PhysioNet's
`SHA256SUMS.txt`. The EDF files total about 7.5 MB and are ignored by Git.

The ProvSleuth example code is covered by the repository's MIT license. The PhysioNet source data
are licensed separately under ODC Attribution 1.0. The prepared epochs, result artifacts, and SVG
are a derived dataset and produced works; they are not relicensed as MIT. They carry this notice:
“Contains information from the EEG Motor Movement/Imagery Dataset v1.0.0, which is made available
under the ODC Attribution License v1.0.” Reusers should preserve the notice, upstream license URL,
dataset DOI, and requested citations. See the root [`THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md). This note is not legal
advice.

## Fixed final analysis

1. Download only `S001R06.edf`, `S001R10.edf`, and `S001R14.edf`; reject any hash mismatch.
2. Standardize channel names, add a fixed average-reference projection, and band-pass filter each
   run separately at 7-30 Hz.
3. Epoch 1-2 seconds after `T1`/`T2` onset and retain nine fixed central channels.
4. Evaluate four-component regularized CSP plus shrinkage LDA with leave-one-run-out validation.
   Every CSP and classifier fit is confined to the two training runs.
5. Compare the mean fold balanced accuracy with 199 deterministic null repetitions. Labels are
   shuffled independently within each run, preserving each run's exact class counts, and the full
   CSP plus LDA pipeline is refit for every fold of every repetition.

The analysis script overrides the common BLAS/OpenMP thread-count variables to `1` before importing
NumPy or MNE and records the effective values. This removes one ambient numerical source. The
result records MNE, NumPy, and scikit-learn versions plus those thread variables; the pinned example
requirements also include SciPy. It does not capture the full OS, BLAS, wheel, or transitive-package
environment, so cross-platform byte identity is not claimed.

The scripts write tracked JSON artifacts with explicit LF newlines. This keeps their checked-out
bytes identical to the bytes hashed in ProvSleuth receipts even when Git is configured to normalize
line endings on Windows.

The fixed final criterion is intentionally simple: all three folds must be valid and the observed
mean balanced accuracy must exceed the null distribution's higher-method 95th percentile. Whether
that criterion is met is a result, not an assumption.

“Fixed final” means the declared final recorded configuration. It does not establish preregistration,
outcome-blind locking, or that the final configuration was chosen before any earlier analysis run.
The immutable semantic and symbolic ledgers retain superseded or stale wording rather than rewriting
it in place. Reported scores are rounded to six decimal places.

## Set up

From a source checkout, enter this directory, create an isolated environment, and install the
example dependencies plus the local ProvSleuth checkout. The pinned scientific stack requires
Python 3.11 or newer; this is an example-only constraint, while ProvSleuth's core remains Python
3.9+ and dependency-free.

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -e ../..
```

On Windows PowerShell, replace `.venv/bin/python` with `.venv\Scripts\python.exe`.

## Run with receipts

The explicit config and working directory prevent an unrelated ancestor project from being
mutated. The following PowerShell commands assume `$python = ".venv/Scripts/python.exe"` and
`$root = (Resolve-Path ".").Path`:

This checked-in demo predates the rename to ProvSleuth. Its existing content-addressed records
retain the legacy `claimtrace.config.json`, `claimtrace/` store paths, and `claimtrace.*` protocol
identifiers. Invoke the current `provsleuth` module with that explicit legacy config. Do not rename
the existing paths or records in place; a new run appends new records under the legacy-configured
store.

The raw EDFs are intentionally not bundled. Before the initial fetch, `provsleuth check` and strict
checking report `MISSING_FILE` for those three current graph nodes; this is an explicit setup
boundary, not a green clean-checkout state. Run the complete wrapped sequence below before
interpreting validation results. After the fetch and analysis steps, the checked-in declarations
can be reconciled with the locally materialized source files and receipts.

```powershell
$config = Join-Path $root "claimtrace.config.json"

& $python -m provsleuth --config $config run --input analysis/00_fetch.py `
  --output data/S001R06.edf --output data/S001R10.edf --output data/S001R14.edf `
  --output data/source_manifest.json --param dataset=eegmmidb-1.0.0 --cwd $root `
  -- $python analysis/00_fetch.py

& $python -m provsleuth --config $config run `
  --input data/S001R06.edf --input data/S001R10.edf --input data/S001R14.edf `
  --input data/source_manifest.json --input analysis/01_prepare.py `
  --output data/prepared_epochs.npz --output results/preprocessing.json `
  --param band_hz=7-30 --param epoch_seconds=1-2 --cwd $root `
  -- $python analysis/01_prepare.py

& $python -m provsleuth --config $config run `
  --input data/prepared_epochs.npz --input results/preprocessing.json `
  --input analysis/02_analyze.py --output results/decoding.json `
  --param validation=leave-one-run-out --param permutations=199 --param thread_limits=1 `
  --seed numpy=20260714 `
  --cwd $root -- $python analysis/02_analyze.py

& $python -m provsleuth --config $config run --input results/decoding.json `
  --input analysis/03_figure.py --output figures/decoding.svg --cwd $root `
  -- $python analysis/03_figure.py

& $python -m provsleuth --config $config snapshot
& $python -m provsleuth --config $config verify
& $python -m provsleuth --config $config evidence-plan claim:above-null --json
& $python -m provsleuth --config $config derivations --json
& $python -m provsleuth --config $config assessments --json
& $python -m provsleuth --config $config check --strict --json
& $python -m provsleuth --config $config lint --strict
& $python -m provsleuth --config $config view --output research-map.html
```

The equivalent POSIX shell sequence is:

```bash
python=".venv/bin/python"
config="$PWD/claimtrace.config.json"
root="$PWD"

"$python" -m provsleuth --config "$config" run \
  --input analysis/00_fetch.py \
  --output data/S001R06.edf --output data/S001R10.edf --output data/S001R14.edf \
  --output data/source_manifest.json --param dataset=eegmmidb-1.0.0 --cwd "$root" \
  -- "$python" analysis/00_fetch.py

"$python" -m provsleuth --config "$config" run \
  --input data/S001R06.edf --input data/S001R10.edf --input data/S001R14.edf \
  --input data/source_manifest.json --input analysis/01_prepare.py \
  --output data/prepared_epochs.npz --output results/preprocessing.json \
  --param band_hz=7-30 --param epoch_seconds=1-2 --cwd "$root" \
  -- "$python" analysis/01_prepare.py

"$python" -m provsleuth --config "$config" run \
  --input data/prepared_epochs.npz --input results/preprocessing.json \
  --input analysis/02_analyze.py --output results/decoding.json \
  --param validation=leave-one-run-out --param permutations=199 --param thread_limits=1 \
  --seed numpy=20260714 --cwd "$root" -- "$python" analysis/02_analyze.py

"$python" -m provsleuth --config "$config" run \
  --input results/decoding.json --input analysis/03_figure.py \
  --output figures/decoding.svg --cwd "$root" -- "$python" analysis/03_figure.py

"$python" -m provsleuth --config "$config" snapshot
"$python" -m provsleuth --config "$config" verify
"$python" -m provsleuth --config "$config" evidence-plan claim:above-null --json
"$python" -m provsleuth --config "$config" derivations --json
"$python" -m provsleuth --config "$config" assessments --json
"$python" -m provsleuth --config "$config" check --strict --json
"$python" -m provsleuth --config "$config" lint --strict
"$python" -m provsleuth --config "$config" view --output research-map.html
```

`semantic-above-null.proposal.json` records the current agent input authored by
`codex:/root/lf_release_hardening`. A separate same-session audit task,
`codex:/root/eeg_lf_review`, accepted the narrow result-to-claim alignment only after independently
running the live verifier. That is task separation, not an external independent review. The earlier
`release_hardening` → `eeg_release_review` and `build_eegbci` → `audit_examples` chains remain in the
immutable ledger and are explicitly superseded after their underlying tracked bytes or claim
wording became stale. The live verifier provides the reproducible check that matters here: it
independently refits all three CSP + LDA folds and all 199 seeded within-run permutations from
`prepared_epochs.npz` and compares every ordered score with `decoding.json`.
Actor strings are self-asserted metadata, not authenticated identities, and acceptance remains an
attributed judgement rather than certification of scientific truth.

Users adapting the example must copy the proposal, replace `provenance.agent` with a truthful value,
and pass that same identity to `provsleuth assess`. Do not resubmit the checked-in proposal under
its recorded actor.

`symbolic-decoding.proposal.json` is not a semantic assessment. It is a plan request with no
agent-chosen facts: ProvSleuth resolves the claim-owned evidence plan and materializes the complete
binding from `results/decoding.json`. A `derivable` result means derivable under
`eegbci:decoding-rules`; it does not make the scientific claim true or validate that the rule and
binding capture the intended meaning.

The checked-in plan request records the current LF-stable release-hardening task; earlier
derivations remain inspectable as stale history. A new actor who wants to record another derivation
must copy the request, replace `provenance.agent`, and pass that same truthful identity to
`provsleuth derive`; the README intentionally does not provide a command that impersonates a
recorded actor.

The checked-in mechanical receipts retain the executable and working-directory paths observed on
the machine that produced them. That is authentic environment provenance but may disclose local
workspace layout. Audit this boundary before republishing a copied ledger; do not edit
content-addressed receipts in place.
