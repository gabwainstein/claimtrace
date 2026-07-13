---
title: 'claimtrace: a dependency-aware provenance and verification engine for scientific data analysis'
tags:
  - Python
  - reproducibility
  - provenance
  - knowledge graph
  - research software
authors:
  - name: Gabriel Wainstein
    affiliation: 1
affiliations:
  - name: (add affiliation)
    index: 1
date: 28 June 2026
bibliography: paper.bib
---

# Summary

`claimtrace` is a small, dependency-free Python tool that models a data-analysis project as a typed
knowledge graph — raw data, preprocessing code, derived artifacts, figures, and the written claims
that rest on them — and uses that graph to keep the project internally consistent over time. Its
central operation is **propagation**: when a canonical analytic choice changes (a dataset version,
a model, a reference scheme), `claimtrace` enumerates every downstream node — including prose claims —
that is now out of date, so no result silently lags the decision. It additionally maintains a
lab-notebook journal of every attempt (including null results, dead-ends, and retractions) and runs
user-defined numeric checks that confirm headline numbers still reproduce from the artifacts on disk.

# Statement of need

Multi-month analyses accumulate silent inconsistencies. A dataset is re-cut, a preprocessing bug is
fixed, or a model is swapped — and weeks later a subset of figures, supplementary tables, and
sentences in a manuscript remain built on the superseded version, because no tool connected the
change to its consequences. Workflow managers (e.g. Snakemake, Make) rebuild *files* from rules;
experiment trackers (e.g. MLflow, Weights & Biases) log *runs*; data-versioning tools (e.g. DVC)
version *artifacts*. None of them track the **semantic** layer where scientific claims are tied to
the specific artifacts and canonical choices that justify them, nor do they treat "this figure is
now stale" or "this claim cites the old version" as first-class, checkable errors.

`claimtrace` targets exactly that gap. It is intentionally minimal — a single JSON graph and a
stdlib-only command-line engine — so a researcher can adopt it incrementally and audit it
completely. It was extracted from the provenance/verification system used to harden a neuroscience
manuscript, where an un-propagated canonical change had quietly left several figures and claims on a
superseded data partition; `claimtrace` makes that failure mode visible (`check`) and its remediation
explicit (`impact`). The accompanying lab-notebook model encourages recording negative results, which
are otherwise lost and silently re-attempted.

# Functionality

- `claimtrace check` — structural validation: missing files, "silent drift" (a current node off the
  canonical backbone), dependence on retired branches, and render staleness via both modification
  time and content hash.
- `claimtrace impact --set concept=value` — the ordered propagation to-do list for a canonical change.
- `claimtrace upstream`/`downstream` — transitive provenance queries.
- `claimtrace log` / `claimtrace journal` — append and review lab-notebook entries by verdict.
- `claimtrace snapshot` — lock a figure's input content-hashes for later drift detection.
- `claimtrace verify` — run project-specific numeric checks that open artifacts and confirm the claimed
  values still hold.

# Acknowledgements

Developed with the assistance of Claude Code.

# References
