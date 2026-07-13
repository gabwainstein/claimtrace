# Demo: the widget-polishing study

A complete, synthetic `claimtrace` project. The "science": does polishing a widget longer make it
shinier? Three scripts take raw measurements → cleaned data → an OLS fit → a figure, and a single
claim rests on the fit. The graph in `claimtrace/graph.json` wires it all together.

## Run it

```bash
cd examples/widget_study
claimtrace run --input data/raw_measurements.csv --input analysis/01_clean.py \
  --output data/clean.csv -- python analysis/01_clean.py
claimtrace run --input data/clean.csv --input analysis/02_fit.py \
  --output results/fit.json -- python analysis/02_fit.py
claimtrace run --input data/clean.csv --input results/fit.json --input analysis/03_figure.py \
  --output figures/fit.svg -- python analysis/03_figure.py

claimtrace check        # OK — all paths exist, nothing stale, nothing on a retired branch
claimtrace check --strict --json  # graph declarations reconcile with the run receipts
claimtrace verify       # PASS — slope ≈ 2.0 and R2 > 0.9, read live from results/fit.json
claimtrace assessments  # accepted support plus an explicit causal-language narrowing
claimtrace snapshot     # lock figures/fit.svg's input hashes into a manifest
claimtrace view --output research-map.html
```

## See the meaning check

The demo has two deterministic, checked-in proposal→acceptance chains in
`claimtrace/assessments/`. Both cite exact `/slope` and `/r2` values in `results/fit.json`:

- `art:fit` has an accepted `supports_as_written` assessment for `claim:slope`. Both frames are
  associational, so the accepted active relation is `supports`.
- `art:fit` has an accepted `supports_narrower_claim` assessment for `hyp:linear`. The hypothesis
  says polishing *causally* increases shininess, but this demo runs only an OLS association. The
  inference-level mismatch stays visible and the accepted active relation is only `related`.

There is deliberately no direct `supports` edge from `art:fit` to `hyp:linear`. The named demo agent
and reviewer are synthetic actors that make the external-proposal/independent-review boundary
visible; their acceptance records a judgement, not proof that the science is true.

```bash
claimtrace assessments --json       # current accepted leaves and live findings
claimtrace assessments --all --json # proposals plus their immutable review decisions
```

For a new assessment, an external agent writes only `claim_id`, `result_ids`, and `agent_input`,
then submits it with `claimtrace assess proposal.json --actor <agent-id>`. A separate actor reviews
the returned content-addressed ID with:

```bash
claimtrace review assessment:sha256:<digest> --state accepted --actor <reviewer-id>
```

Claimtrace itself computes node/file hashes, resolves the exact evidence anchors, and derives the
eligible relation. Edit `results/fit.json` after review and `claimtrace assessments` will mark both
chains stale (and the exact anchors invalid); strict checking blocks until the evidence and reviews
are brought back into alignment.

## See it catch drift

```bash
# pretend you re-collected the data but forgot to re-run the figure:
echo "17,99" >> data/raw_measurements.csv
claimtrace check        # STALE_DATA on fig:fit — its locked input (clean.csv? raw) changed since snapshot
```

(Re-run the three scripts + `claimtrace snapshot` to go green again.)

## See the propagation list

```bash
claimtrace impact --set dataset_version=v3
# lists every node on v2 that must update if you re-cut the dataset:
# data:raw, art:clean, art:fit, fig:fit, claim:slope ...
```

## See the lab notebook

```bash
claimtrace journal                  # groups every node by verdict
claimtrace journal --status dead_end  # shows exp:loglog — a log-log fit that was tried and abandoned
```

`exp:loglog` is a node with `status: dead_end` and a `tried_before` annotation to `claim:slope`:
the record that you already tried a log-log model and it was worse, so you don't silently re-try it.
