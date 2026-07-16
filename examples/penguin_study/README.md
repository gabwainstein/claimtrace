# Palmer Penguins: a real-data Claimtrace demo

This example follows one compact scientific question through source acquisition, deterministic
preprocessing, analysis, a figure, narrow claims, semantic-review history, and a symbolic rule.
It uses the open Palmer Penguins data because the pooled bill-length/bill-depth association has the
opposite sign from each species-specific association. That makes the limits of a superficially
reasonable claim visible without inventing a result.

This is a teaching reanalysis, not a replication of the biological conclusions in the original
paper and not a new biological discovery.

## Data and scope

- Package data: Horst AM, Hill AP, Gorman KB (2020), `palmerpenguins` v0.1.0,
  <https://doi.org/10.5281/zenodo.3960218>. The data are released under
  [CC0 1.0](https://allisonhorst.github.io/palmerpenguins/LICENSE.html).
- Original study: Gorman KB, Williams TD, Fraser WR (2014),
  <https://doi.org/10.1371/journal.pone.0090081>.
- Teaching article: Horst AM, Hill AP, Gorman KB (2022),
  <https://journal.r-project.org/articles/RJ-2022-020/>.

The example code and Claimtrace configuration follow this repository's MIT license. The Palmer
Penguins CSVs remain separately available under CC0 and are not relicensed as MIT here; their source
citation is retained even though CC0 does not require attribution.

The two package CSVs are pinned to the `v0.1.0` Git tag, byte counts, and SHA-256 hashes. The local
preprocessing script repeats the package's documented transformation and refuses to continue unless
its output is byte-identical to the official curated CSV. The slope analysis then uses only records
with non-missing bill length and depth.

## Rebuild with mechanical receipts

From a source checkout, enter this directory and use the explicit project config and root. The
first command requires network access; the pinned hash gates fail closed if upstream bytes differ.

Install Claimtrace first (`python -m pip install -e ../..` from this directory), or point an
uninstalled source checkout at the package for the current PowerShell session:

```powershell
$env:PYTHONPATH = (Resolve-Path ..\..\src).Path
```

```powershell
$config = (Resolve-Path claimtrace.config.json).Path
$root = (Get-Location).Path

python -m claimtrace --config $config run `
  --input analysis/00_fetch.py `
  --output data/source/penguins_raw.csv `
  --output data/source/penguins.csv `
  --output results/source_manifest.json `
  --cwd $root -- python analysis/00_fetch.py

python -m claimtrace --config $config run `
  --input data/source/penguins_raw.csv `
  --input data/source/penguins.csv `
  --input analysis/01_prepare.py `
  --output data/penguins_clean.csv `
  --output results/preprocess.json `
  --cwd $root -- python analysis/01_prepare.py

python -m claimtrace --config $config run `
  --pipeline-contract claimtrace/analyze.pipeline.json `
  --stage-checkpoints `
  --input data/penguins_clean.csv `
  --input analysis/02_analyze.py `
  --output results/slopes.json `
  --cwd $root -- python analysis/02_analyze.py

# Replace this placeholder with the run ID printed by the preceding command.
$analysisRun = "run:<printed-analysis-run-id>"
python -m claimtrace --config $config replay --repeat 2 $analysisRun

python -m claimtrace --config $config run `
  --input data/penguins_clean.csv `
  --input results/slopes.json `
  --input analysis/03_figure.py `
  --output figures/bill_slopes.svg `
  --cwd $root -- python analysis/03_figure.py

python -m claimtrace --config $config snapshot
python -m claimtrace --config $config evidence-plan claim:sign-reversal --json
python -m claimtrace --config $config derivations --json
python -m claimtrace --config $config assessments --all --json
python -m claimtrace --config $config method-assessments --json
python -m claimtrace --config $config verify
python -m claimtrace --config $config lint --strict
python -m claimtrace --config $config check --strict --json
```

The equivalent POSIX shell sequence is:

```bash
export PYTHONPATH="$(cd ../../src && pwd)"
config="$PWD/claimtrace.config.json"
root="$PWD"

python -m claimtrace --config "$config" run \
  --input analysis/00_fetch.py \
  --output data/source/penguins_raw.csv \
  --output data/source/penguins.csv \
  --output results/source_manifest.json \
  --cwd "$root" -- python analysis/00_fetch.py

python -m claimtrace --config "$config" run \
  --input data/source/penguins_raw.csv \
  --input data/source/penguins.csv \
  --input analysis/01_prepare.py \
  --output data/penguins_clean.csv \
  --output results/preprocess.json \
  --cwd "$root" -- python analysis/01_prepare.py

python -m claimtrace --config "$config" run \
  --pipeline-contract claimtrace/analyze.pipeline.json \
  --stage-checkpoints \
  --input data/penguins_clean.csv \
  --input analysis/02_analyze.py \
  --output results/slopes.json \
  --cwd "$root" -- python analysis/02_analyze.py

# Replace this placeholder with the run ID printed by the preceding command.
analysis_run="run:<printed-analysis-run-id>"
python -m claimtrace --config "$config" replay --repeat 2 "$analysis_run"

python -m claimtrace --config "$config" run \
  --input data/penguins_clean.csv \
  --input results/slopes.json \
  --input analysis/03_figure.py \
  --output figures/bill_slopes.svg \
  --cwd "$root" -- python analysis/03_figure.py

python -m claimtrace --config "$config" snapshot
python -m claimtrace --config "$config" evidence-plan claim:sign-reversal --json
python -m claimtrace --config "$config" derivations --json
python -m claimtrace --config "$config" assessments --all --json
python -m claimtrace --config "$config" method-assessments --json
python -m claimtrace --config "$config" verify
python -m claimtrace --config "$config" lint --strict
python -m claimtrace --config "$config" check --strict --json
```

The generated files are checked in. The analysis contract names four exact code-backed stages:
complete-case selection, species grouping, descriptive OLS estimation, and result export. Its
current producing event is `run:deda38a0-b5df-47f2-b5ed-9d88e599143b`; the current two-attempt
certificate is `replay:sha256:72f560d01958bba46f405655b61e95b5668dbc7dd9a99d3bbc1345825b6a26a2`.
Both replay attempts reproduced the output bytes, stdout, and normalized four-checkpoint sequence
and created no visible undeclared workspace files. Each checkpoint record matched the exact
direct-child process ID launched by the controller, preventing accidental descendant mixing.

The first three stages exchange only in-memory values. The cooperative records show that the exact
direct child reported reaching each locked callsite after its local stage code and checks. They do
not independently observe the computation, intermediate values, or scientific meaning. The
terminal JSON remains the only stage output observed at the process boundary in this contract.

A deterministic rerun over a pre-existing identical output is a validation-only receipt, not
evidence that the wrapper produced that file. To observe a fresh production transition, use a
disposable copy and remove only the generated output before the contract-bound analysis run. The
checked-in event, replay, assessment, method-assessment, and derivation records are immutable
history; do not recreate them under a recorded actor's identity.
The older event-v3 run and replay remain visible as stale historical records. Their natural
input/contract drift is nonblocking only because the later event-v4 run has the exact same output
role, current bindings, and a current review-ready replay; integrity faults, undeclared writes, and
non-repeatability are never demoted by this replacement rule.

## Meaning and logic remain separate

Two bounded agent inputs compare the exact result artifact with two deliberately different claims:

- `semantic-estimated-slopes.proposal.json` targets `claim:estimated-slopes`, which states the complete
  sample size and all four numerical slopes.
- `semantic-qualitative-sign.proposal.json` targets `claim:sign-reversal`, which states only the
  pooled-negative/species-positive direction pattern and is also the symbolic rule's target.

The current proposals were recorded by `codex:/root/public-demo-regeneration`. A separate
`codex:/root/penguin_evidence_review` task accepted both narrow alignments after the live verifier
passed 4/4 checks and an independent sum-of-products calculation reproduced the sample count,
species counts, four slopes, and sign pattern. That is task separation, not an external independent
review. The resulting active `supports` relations are assessment projections; there are no manual
`supports` edges in the graph.

The checkpoint-instrumented method proposal was recorded by
`codex:/root/stage-checkpoint-regeneration`. A distinct
`codex:/root/checkpoint_adversarial` task recomputed all five anchor hashes, inspected each pinned
span, and accepted the method-conformance proposal for `method:complete-case-ols`. That judgement
says the pinned spans implement the four written steps. It does not independently show that each
in-memory computation occurred, establish that the method is scientifically appropriate, or
certify either claim as true.

Inspect the complete immutable history with:

```powershell
python -m claimtrace --config $config assessments --all --json
python -m claimtrace --config $config method-assessments --json
```

The history intentionally retains a v1 framing mistake, its prematurely accepted review, and a
scientifically honest v1 qualitative proposal that the older blanket alignment policy could not
accept. Those chains were superseded rather than deleted or rewritten. The equivalent qualitative
input was resubmitted under schema v2, whose narrow magnitude-specificity rule makes the governing
policy explicit. Review successors stay on their predecessor's schema, so old records are never
silently reinterpreted after a policy change.

All recorded `codex:/root/...` actor strings, including the current proposer and reviewer, are
self-asserted task identities. Claimtrace records but does not authenticate them. Acceptance is an
attributed judgement, not proof that the scientific interpretation is true.

The reported OLS estimates are rounded to six decimal places. The checked-in mechanical receipts
also retain the executable and working-directory paths observed on the producing machine. That is
authentic environment provenance but may disclose local workspace layout; audit this boundary
before republishing a copied ledger, and do not edit content-addressed receipts in place.

To submit a new semantic or method assessment, copy the relevant proposal, edit
`provenance.agent` to your truthful actor ID, and pass that same ID to `--actor`. Do not reuse a
recorded actor identity.

The symbolic layer asks a narrower deterministic question: does the recorded complete slope profile
satisfy the project-owned sign rule?

For a separate end-to-end example of exact ontology locking, bounded external-agent mapping input,
human review, explicit policy compilation, activation, and strict verification, follow the
[semantic-normalization walkthrough](SEMANTICS.md) in a disposable copy of this study.

```powershell
python -m claimtrace --config $config evidence-plan claim:sign-reversal --json
python -m claimtrace --config $config derivations --json
```

The derivation ledger retains the earlier proofs and the current proof rebuilt after checkpoint
instrumentation refreshed the locked code basis. For a new derivation after evidence or policy
changes, copy the request, replace `provenance.agent` with your truthful actor ID, and use the same
value for `--actor`.

The rule can return `derivable` or `refutable` for the configured target. It does not prove that the
scientific interpretation is true, that the chosen policy captures every relevant alternative, or
that the prose means the same thing as the formal predicates. That is why the semantic assessment
remains a separate review object.

## Interpretation boundary

The pooled and species-specific models describe associations in the complete records from this
dataset. They do not establish that bill length causes bill depth, that species causes the sign
change, or that these slopes generalize beyond the sampled birds, islands, and years. A broad claim
such as “longer penguin bills are shallower” is therefore under-scoped even though the pooled slope
is negative.

Generate the interactive graph without declaring the HTML as a scientific graph node:

```powershell
python -m claimtrace --config $config view --output research-map.html
```
