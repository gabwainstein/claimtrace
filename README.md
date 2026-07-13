# claimtrace

**A dependency-aware provenance + verification engine for scientific data analysis.**

Long analyses drift. You re-cut a dataset, swap a model, or fix a preprocessing bug — and three
weeks later a figure, a supplementary table, and a sentence in your draft are *still* built on the
old version, silently. Nobody re-ran them because nothing *told* anyone to.

`claimtrace` is a tiny (stdlib-only) typed knowledge graph of your project — `question → hypothesis
→ prediction → data/code → artifacts → figures → claims → conclusion` — whose job is
**propagation**: when a canonical choice changes, it lists every downstream result that is now
stale. It also keeps a **lab-notebook journal** of every attempt (including dead-ends and
retractions), captures content-addressed mechanical receipts around analysis commands, and runs
**project-specific numeric checks** that confirm headline numbers still reproduce from disk.

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

No database, daemon, or cloud is required. The semantic graph and optional run receipts are plain,
content-addressed JSON files that remain readable, diffable, and Git-auditable.

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
claimtrace install-skill --dir .      # optional: install agent + Claude project-local skills
# ... edit claimtrace/graph.json to describe your data → code → figures → claims ...
claimtrace run --input data/raw.csv --input analysis/fit.py --output results/fit.json \
  -- python analysis/fit.py           # execute + capture a mechanical receipt
claimtrace check                      # paths exist? nothing stale? nothing on a retired branch?
claimtrace check --strict --json      # deterministic graph + receipt reconciliation gate
claimtrace impact --set model=v2      # what must change if I switch the canonical model?
claimtrace downstream art:clean_data  # what depends on this artifact?
claimtrace verify                     # do my headline numbers still reproduce from disk?
claimtrace journal --status dead_end  # show me everything I already tried that didn't work
claimtrace view --output research-map.html  # semantic trajectory + mechanical receipt overlay
```

## Try the demo (nothing to set up)

A complete synthetic project lives in [`examples/widget_study/`](examples/widget_study/):

```bash
cd examples/widget_study
claimtrace run --input data/raw_measurements.csv --input analysis/01_clean.py \
  --output data/clean.csv -- python analysis/01_clean.py
claimtrace run --input data/clean.csv --input analysis/02_fit.py \
  --output results/fit.json -- python analysis/02_fit.py
claimtrace run --input data/clean.csv --input results/fit.json --input analysis/03_figure.py \
  --output figures/fit.svg -- python analysis/03_figure.py
claimtrace check        # green
claimtrace check --strict --json  # declarations agree with captured run boundaries
claimtrace verify       # confirms slope ≈ 2.0, R² > 0.9 from results/fit.json
claimtrace snapshot     # lock the figure's input hashes
claimtrace view --output research-map.html
claimtrace impact --set dataset_version=v2   # see the propagation list
claimtrace journal      # the lab notebook, including a logged dead-end
```

Then edit `data/raw_measurements.csv` and re-run `claimtrace check` — it now reports `STALE_DATA` on
the figure, because its locked inputs changed.

---

## How it works

A node is a typed thing in your project; an edge is information flow (`from → to` means *to depends
on from*). Each node can carry a `backbone` (the canonical choices it is built on) and a `status`
(its lifecycle/verdict). A scalar backbone remains valid for a graph with one concept. With multiple
concepts, use a concept-keyed object such as `{"dataset_version": "v2", "model": "m1"}` so impact
and drift checks cannot mix unrelated version axes.

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
| `claimtrace check` | structural integrity · missing files/manifests · scoped canonical drift · retired dependencies · manifest completeness · input/output hash drift · downstream staleness |
| `claimtrace check --strict --json` | deterministic graph/receipt report; also blocks on lint, stale nodes, missing receipts, and declaration mismatches |
| `claimtrace lint` | warn on non-standard vocabulary + load-bearing nodes with no `backbone` (`--strict` to fail) |
| `claimtrace run ... -- COMMAND` | execute a direct child with explicit inputs/outputs and append content-addressed pre/post SHA-256 receipts |
| `claimtrace view --output FILE` | render a standalone interactive trajectory with semantic and receipt layers |
| `claimtrace impact --set concept=value` | ordered propagation to-do list for a canonical change |
| `claimtrace downstream <id>` / `upstream <id>` | transitive dependents / dependencies |
| `claimtrace node <id>` | a node and its edges |
| `claimtrace log entry.json` | append a lab-notebook node (+ edges); use for nulls/dead-ends too |
| `claimtrace journal [--status ...]` | every attempt grouped by verdict |
| `claimtrace snapshot` | lock each render's input content-hashes into a manifest |
| `claimtrace verify` | run your project-specific numeric checks |
| `claimtrace summary` | node/edge/concept counts |
| `claimtrace init` | scaffold a config in a project |
| `claimtrace install-skill [--target ...]` | install the packaged `claimtrace-log` skill without silently overwriting project customizations |

## Agent add-on

The wheel includes a canonical `claimtrace-log` research skill. Install it into a project with
`claimtrace install-skill --dir /path/to/project`; the default target writes both supported layouts.
Use `--target agents` or `--target claude` for one layout. A differing existing skill is preserved
unless you explicitly pass `--force` after reviewing it.

The source repository keeps synchronized, directly discoverable copies in:

- [`.agents/skills/claimtrace-log/`](.agents/skills/claimtrace-log/) for agents that support the
  shared agent-skill layout.
- [`.claude/skills/claimtrace-log/`](.claude/skills/claimtrace-log/) for Claude-compatible project
  skills.

The skill makes the safe workflow explicit: resolve and pin the absolute config, inspect the real
script and file roles, execute new substantive analyses through `claimtrace run`, then log the
scientific verdict separately and finish with deterministic strict checks. It records nulls,
dead-ends, retractions, and superseded work as first-class outcomes. It never fabricates a receipt
for historical work or infers a dependency from a filename. Agents still need the user or project
to define scientific meaning; claimtrace automates integrity, reconciliation, propagation, and
display.

## Optional: a git pre-commit hook

```bash
cp hooks/pre-commit .git/hooks/pre-commit   # runs `claimtrace check` (blocking) + `verify` (soft)
```

## Trust boundary

`claimtrace check`, `lint`, and the query commands are pure inspection. `claimtrace view` only reads
the project and writes the explicitly requested HTML output. `claimtrace run` executes exactly the
argv after `--` as a direct child with `shell=False`; `claimtrace verify` **executes your project's
`verifiers.py`** (a plugin model, like `conftest.py` or a `Makefile`). Run executable commands only
in projects you trust, and do not auto-run them on untrusted pull requests.

`claimtrace` verifies that the *machinery* is internally consistent — paths exist, no cycles or
dangling edges, claims cite on-backbone artifacts, headline numbers reproduce. It deliberately does
**not** claim your science is correct: a green check means the declared provenance is internally
coherent, not that the conclusion is right. That boundary is the point — it tells you what has *not*
been re-derived, so a human still does the judging.

The system deliberately has two evidence layers:

- The **semantic graph** contains attributed scientific assertions: hypotheses, predictions,
  methods, claims, conclusions, and their declared dependencies. A generic command wrapper must not
  invent or silently mutate those assertions.
- The **mechanical receipt ledger** records declared inputs/outputs, stable pre/post SHA-256 file
  versions, direct-child argv and return code, best-effort Git/lockfile context, and project-window
  deltas.

A semantic node can explicitly cite the receipt for its tested verdict with
`"run_ids": ["run:<uuid>"]`. This is how pathless, null, and dead-end results stay visibly linked to
their execution. Strict checking rejects missing or invalid references and prevents `current`,
`confirmed`, or `null` results from citing an unsuccessful command. Materialized outputs are linked
separately by their declared path; neither link invents scientific dependencies.

The wrapper cannot prove that a declared input was actually read, that the child caused a detected
write, that background descendants finished, or that network/database state was captured. Reports
therefore say `declared_only_not_observed`, `direct_child_only`, and
`unattributed_pre_post_delta`; matching graph/run declarations means **declarations agree**, not
independent runtime proof. `check --strict` is a deterministic gate within that explicit partial
capture scope. Existing projects with render nodes must also run `claimtrace snapshot` once after a
trusted render so manifest checks can pass.

Event files are append-only through the claimtrace API and content addressing detects modification
of surviving files. The local directory alone cannot prove that complete event pairs were never
deleted; use Git or another external ledger commitment when deletion evidence is required.

## License

MIT © Gabriel Wainstein. Developed with the assistance of Claude Code.
