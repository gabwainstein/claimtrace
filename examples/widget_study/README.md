# Demo: the widget-polishing study

A complete, synthetic `claimtrace` project. The "science": does polishing a widget longer make it
shinier? Three scripts take raw measurements → cleaned data → an OLS fit → a figure, and a single
claim rests on the fit. The graph in `claimtrace/graph.json` wires it all together.

## Run it

```bash
cd examples/widget_study
python analysis/01_clean.py      # data/raw_measurements.csv -> data/clean.csv
python analysis/02_fit.py        # -> results/fit.json   (slope ~ 2.0, R2 ~ 0.99)
python analysis/03_figure.py     # -> figures/fit.svg

claimtrace check        # OK — all paths exist, nothing stale, nothing on a retired branch
claimtrace verify       # PASS — slope ≈ 2.0 and R2 > 0.9, read live from results/fit.json
claimtrace snapshot     # lock figures/fit.svg's input hashes into a manifest
```

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
