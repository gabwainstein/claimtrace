# claimtrace

**A dependency-aware provenance + verification engine for scientific data analysis.**

Long analyses drift. You re-cut a dataset, swap a model, or fix a preprocessing bug — and three
weeks later a figure, a supplementary table, and a sentence in your draft are *still* built on the
old version, silently. Nobody re-ran them because nothing *told* anyone to.

`claimtrace` is a tiny (stdlib-only) typed knowledge graph of your project — `raw data → preprocessing
→ artifacts → figures → claims` — whose job is **propagation**: when a canonical choice changes,
it lists every downstream result that is now stale. It also keeps a **lab-notebook journal** of
every attempt (including the dead-ends and retractions you'd otherwise forget), and runs
**project-specific numeric checks** that confirm your headline numbers still reproduce from disk.

It was extracted from the system used to harden a neuroscience manuscript end-to-end — where a
single un-propagated "use dataset version B, not A" decision had quietly left several figures and
claims on the old partition. `claimtrace` exists so that can't happen silently.

---

## Why it's different from a pipeline / experiment tracker

Tools like Make/Snakemake rebuild *files*; W&B/MLflow log *runs*; DVC versions *data*. `claimtrace`
tracks the **semantic** layer they don't:

- **Canonical-change propagation.** Declare a *concept* (`dataset_version`, `model`, `reference`)
  with a canonical value. `claimtrace impact --set dataset_version=v3` prints the exact, ordered list
  of every node that must update — across data, code, figures, *and the prose claims*.
- **Staleness as a first-class error.** `claimtrace check` flags a figure that was rendered before its
  inputs changed (mtime *and* content-hash), a "current" result that secretly depends on a
  retired branch, and a claim that cites an off-version artifact.
- **A real lab notebook.** Log nulls, dead-ends, and retractions — not just successes — so you
  never silently re-try something that already failed. `claimtrace journal` reads it back by verdict.
- **Numeric verification.** Structure isn't enough: `claimtrace verify` runs your own checks that open
  the artifacts and confirm the actual numbers your claims assert still hold.

No database, no daemon, no cloud. One JSON file and a stdlib CLI you can read in an afternoon.

---

## Install

```bash
pip install claimtrace            # core engine (stdlib only)
pip install "claimtrace[verify]"  # + numpy/pandas if your verifiers use them
```

Or from source:

```bash
git clone https://github.com/gabwainstein/claimtrace && cd claimtrace && pip install -e .
```

## Quickstart

```bash
cd my-analysis-project
claimtrace init                       # scaffold claimtrace.config.json + claimtrace/graph.json
# ... edit claimtrace/graph.json to describe your data → code → figures → claims ...
claimtrace check                      # paths exist? nothing stale? nothing on a retired branch?
claimtrace impact --set model=v2      # what must change if I switch the canonical model?
claimtrace downstream art:clean_data  # what depends on this artifact?
claimtrace verify                     # do my headline numbers still reproduce from disk?
claimtrace journal --status dead_end  # show me everything I already tried that didn't work
```

## Try the demo (nothing to set up)

A complete synthetic project lives in [`examples/widget_study/`](examples/widget_study/):

```bash
cd examples/widget_study
python analysis/01_clean.py && python analysis/02_fit.py && python analysis/03_figure.py
claimtrace check        # green
claimtrace verify       # confirms slope ≈ 2.0, R² > 0.9 from results/fit.json
claimtrace snapshot     # lock the figure's input hashes
claimtrace impact --set dataset_version=v2   # see the propagation list
claimtrace journal      # the lab notebook, including a logged dead-end
```

Then edit `data/raw_measurements.csv` and re-run `claimtrace check` — it now reports `STALE_DATA` on
the figure, because its locked inputs changed.

---

## How it works

A node is a typed thing in your project; an edge is information flow (`from → to` means *to depends
on from*). Each node carries a `backbone` (which canonical value it's built on) and a `status`
(its lifecycle/verdict).

```json
{
  "concepts": { "dataset_version": { "canonical": "v2" } },
  "nodes": [
    { "id": "data:raw",   "type": "data",     "status": "current", "backbone": "v2", "path": "data/raw.csv" },
    { "id": "code:fit",   "type": "code",     "status": "current", "path": "analysis/02_fit.py" },
    { "id": "art:fit",    "type": "artifact", "status": "current", "backbone": "v2", "path": "results/fit.json" },
    { "id": "fig:fit",    "type": "figure",   "status": "current", "backbone": "v2", "path": "figures/fit.svg" },
    { "id": "claim:slope","type": "claim",    "status": "current", "backbone": "v2", "value": "polishing predicts shininess, slope ~2.0" }
  ],
  "edges": [
    { "from": "data:raw", "to": "art:fit",     "rel": "produces" },
    { "from": "code:fit", "to": "art:fit",     "rel": "produces" },
    { "from": "art:fit",  "to": "fig:fit",     "rel": "renders" },
    { "from": "art:fit",  "to": "claim:slope", "rel": "supports" }
  ]
}
```

Full vocabulary (node types, edge relations, statuses) is in [`docs/SCHEMA.md`](docs/SCHEMA.md).

## Commands

| command | what it does |
|---|---|
| `claimtrace check` | structural integrity (cycles · dangling edges · duplicate ids) · missing files · silent drift · retired-dependency · stale renders (mtime + hash) |
| `claimtrace lint` | warn on non-standard vocabulary + load-bearing nodes with no `backbone` (`--strict` to fail) |
| `claimtrace impact --set concept=value` | ordered propagation to-do list for a canonical change |
| `claimtrace downstream <id>` / `upstream <id>` | transitive dependents / dependencies |
| `claimtrace node <id>` | a node and its edges |
| `claimtrace log entry.json` | append a lab-notebook node (+ edges); use for nulls/dead-ends too |
| `claimtrace journal [--status ...]` | every attempt grouped by verdict |
| `claimtrace snapshot` | lock each render's input content-hashes into a manifest |
| `claimtrace verify` | run your project-specific numeric checks |
| `claimtrace summary` | node/edge/concept counts |
| `claimtrace init` | scaffold a config in a project |

## Optional: a git pre-commit hook

```bash
cp hooks/pre-commit .git/hooks/pre-commit   # runs `claimtrace check` (blocking) + `verify` (soft)
```

## Trust boundary

`claimtrace check`, `lint`, and the query commands are pure inspection — safe to run on any
checkout. `claimtrace verify` **executes your project's `verifiers.py`** (a plugin model, like
`conftest.py` or a `Makefile`), so only run `verify` on projects you trust, and don't auto-run it in
CI on pull requests from forks.

`claimtrace` verifies that the *machinery* is internally consistent — paths exist, no cycles or
dangling edges, claims cite on-backbone artifacts, headline numbers reproduce. It deliberately does
**not** claim your science is correct: a green check means the provenance is sound, not that the
conclusion is right. That boundary is the point — it tells you what has *not* been re-derived, so a
human still does the judging.

## License

MIT © Gabriel Wainstein. Developed with the assistance of Claude Code.
