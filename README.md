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
For the part that cannot be reduced to hashes, an external agent can submit a schema-constrained
semantic assessment that compares what a result means with what a claim says. Claimtrace pins the
exact nodes and evidence bytes, applies deterministic policy, and leaves acceptance to a separate
review step. For formalizable claims, a separate data-only symbolic layer grounds complete typed
facts from result artifacts and computes whether a pinned target is derivable under project-owned
rules, without presenting that conditional proof as scientific truth.

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
- **Portable symbolic claims.** Project-owned JSON vocabularies, complete result-to-fact bindings,
  and finite rules let users or agents compute conditional claim states without executable policy
  plugins or agent-authored proof steps.

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
claimtrace assess proposal.json --actor analysis-agent  # propose a grounded semantic judgement
claimtrace assessments --json        # inspect proposals, reviews, findings, and staleness
claimtrace derive selection.json --actor analysis-agent   # select approved bindings; Claimtrace grounds them
claimtrace derivations --json        # inspect conditional proof states and current validity
claimtrace explain <derivation-or-proof-id> --json  # inspect one composite proof certificate
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
claimtrace assessments  # accepted support plus a visible causal-language narrowing
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

The historical `reads` relation is the one direction exception: write
`{"from":"code:fit","to":"data:raw","rel":"reads"}` to mean that `code:fit` depends on and reads
`data:raw`. All other dependency relations use the general `from` source → `to` dependent form.

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
| `claimtrace assess ENTRY --actor ID` | append an external-agent semantic proposal; claimtrace computes evidence snapshots and policy output |
| `claimtrace assessments [--state ...] [--all] [--json]` | list current semantic assessments, or their immutable history with `--all` |
| `claimtrace review ASSESSMENT_ID --state ... --actor ID` | append an independent acceptance, rejection, contest, or supersession decision |
| `claimtrace derive ENTRY --actor ID` | select approved result bindings (preferred) or import explicit typed facts, then append a deterministic symbolic derivation |
| `claimtrace derivations [--state ...] [--json]` | list derivations reevaluated against the current graph, artifacts, vocabulary, and rules |
| `claimtrace explain DERIVATION_OR_PROOF_ID [--json]` | show one composite proof certificate, premises, assumptions, equivalent submissions, and effective state |
| `claimtrace snapshot` | lock each render's input content-hashes into a manifest |
| `claimtrace verify` | run your project-specific numeric checks |
| `claimtrace summary` | node/edge/concept counts |
| `claimtrace init` | scaffold a config in a project |
| `claimtrace install-skill [--target ...]` | install the packaged `claimtrace-log` skill without silently overwriting project customizations |

## Grounded semantic assessments

A graph edge can declare that a result supports a claim, but a hash cannot tell whether the result
actually has the same population, exposure, comparator, outcome, direction, magnitude, time scope,
or inference level as the claim. Semantic assessments add that meaning check without pretending it
is fully mechanical.

An external agent submits a JSON object containing exactly `claim_id`, `result_ids`, and
`agent_input`. Schema v1 accepts exactly one result per assessment. The agent authors the verdict,
complete structured claim and result frames, all eight fixed
alignment dimensions, exact evidence anchors, a concise rationale, limitations, and its provenance.
Allowed verdicts are `supports_as_written`, `supports_narrower_claim`,
`contradicts_as_written`, `insufficient`, `ambiguous`, and `unrelated`. Evidence anchors must point
to exact JSON values with JSON Pointer or exact text line spans with a SHA-256 digest.

The external agent must not provide `mechanical_snapshot`, `derived`, or a review decision.
Claimtrace computes the node and artifact SHA-256 identities, resolves every anchor, detects later
node/file drift and conflicting active assessments, and derives the eligible relation and findings.
A narrowing can activate `related`; it never becomes support for the original wording.

```bash
claimtrace assess semantic-proposal.json --actor analysis-agent --json
claimtrace assessments --state proposed --json
claimtrace review assessment:sha256:<digest> --state accepted --actor independent-reviewer --json
claimtrace assessments --all --json
```

Assessments are immutable, content-addressed JSON documents. A review appends one successor; it does
not rewrite or branch the proposal. The first reviewer string must differ from the proposer string,
but those identities are self-asserted rather than authenticated. Acceptance fails closed for
invalid anchors, staleness, accepted-review conflicts, store-integrity failures, and inconsistent
modality declarations. An unreviewed conflicting proposal cannot deactivate an accepted relation.
Set `"require_assessments": true` to make a direct, active `supports` or `refutes` edge without
matching accepted semantic coverage block strict checking; the default is advisory for gradual
adoption. An accepted opposite-polarity assessment is always a hard conflict.

Acceptance records an attributed judgement under this policy. It is not proof that the analysis is
valid or the scientific claim is true. The checked-in widget demo makes the distinction concrete:
`art:fit` has accepted support for the associational `claim:slope`, while the same OLS artifact is
only `related` to the causal `hyp:linear` after an accepted `supports_narrower_claim` assessment.
There is no direct support edge from the fit to that hypothesis.

Staleness pins the exact claim node, result node, and result artifact. It does not snapshot every
upstream graph edge or canonical concept; those remain the separate graph/receipt integrity layer.

## Portable symbolic derivations

Projects that need reproducible claim logic can add a small, data-only symbolic layer. Claimtrace
does not hard-code a scientific domain or ontology: each project defines portable JSON
vocabularies (types, units, predicates, and renderers), function-free rules, result bindings, and
claim targets. A user, workflow, or external agent can therefore adopt the same engine in any
project without writing a Claimtrace plugin or allowing executable rule code.

The project owns the meaning-bearing policy. Each result node's `logic_bindings` contains a
**complete fact-binding profile** that pins one input predicate, its polarity, and an extractor for
every argument. Each claim-like node's `logic` declaration pins its exact target plus both the
`vocabulary_id` and `rule_pack_id`. In the preferred interface, a user or agent selects only
project-declared result/binding IDs. Claimtrace reads the target and policy IDs from the claim,
extracts and canonicalizes every typed argument from the result artifacts, and computes the proof.
The proposal cannot supply a pointer, atom, target, polarity, rule, or proof step. One grounded fact
has exactly one evidence binding. This keeps the adaptable part declarative while making extraction
and inference deterministic. One result may expose profiles for multiple configured vocabularies;
each proof still uses the single vocabulary pinned by its claim. `claimtrace check` validates every
declared target and binding, including extraction against the current artifact, before an agent can
select it.

```json
{
  "logic": {
    "derivations": "claimtrace/derivations",
    "vocabularies": ["claimtrace/logic/vocabulary.json"],
    "rule_packs": ["claimtrace/logic/rules.json"],
    "allow_external_packs": false,
    "require_derivations": false,
    "max_provenance_bytes": 68719476736
  }
}
```

Asset paths are project-relative by default. `allow_external_packs` permits explicitly configured
vocabulary and rule assets outside the project when they are separately trusted; it never enables
executable rules. The derivation store always remains project-local. `max_provenance_bytes` is a
positive 64-bit byte budget for stream-hashing the scoped upstream provenance files; it defaults to
64 GiB and does not raise the separate 64 MiB-per-result or 256 MiB aggregate grounding limits.

```json
{
  "schema_version": "claimtrace.symbolic-selection/1",
  "claim_id": "claim:gate",
  "bindings": [
    {"result_id": "art:test", "binding_id": "gate:test-completed"}
  ],
  "note": "Use the project-reviewed test-gate profile.",
  "provenance": {"agent": "analysis-agent"}
}
```

This `claimtrace.symbolic-selection/1` form is the normal integration surface for agents and other
tools. The explicit `claim_id`/`result_ids`/`vocabulary_id`/`rule_pack_id`/`agent_input` form remains
available as a low-level import, debugging, and explicit-assumption interface. It requires the
caller to transcribe canonical typed atoms, but still cannot submit computed snapshots or proof
fields. Prefer selections for grounded facts; use the low-level form only when that extra control is
intentional.

Evaluation is open-world and paraconsistent. The target state is `derivable` when its requested
polarity follows, `refutable` when only the opposite follows, `conflict` when both follow, and
`unknown` when neither follows. Missing information is not false, and a contradiction does not
make arbitrary claims derivable. A fact with an explicit assumption remains visible in its proof,
but any proof that depends on it is inactive. Multiple results remain premises of one composite
proof; Claimtrace does not flatten them into misleading per-result support edges.

Equivalent active submissions share a canonical `proof_id` and reports group their
`derivation_ids` rather than drawing duplicate proof nodes. If separate active derivations establish
both the target and its explicit opposite for the same claim, vocabulary, rule pack, and target,
Claimtrace emits `SYMBOLIC_CROSS_DERIVATION_CONFLICT` and suppresses both at claim level. This is
distinct from the single-derivation `conflict` state, but both fail closed for
`require_derivations`.

These states mean **conditional derivability under the pinned project rules and grounded facts**.
They do not establish that the rules are scientifically valid, that a premise is true, that the
formal target accurately expresses the prose claim, or that a binding accurately expresses the
artifact's scientific construct. Semantic assessments cover result-to-prose meaning only. The
prose-to-target and binding-to-predicate mappings remain repository policy that needs independent
review; Claimtrace does not yet store a dedicated review record for either mapping. Set
`logic.require_derivations` only when a project wants strict checks to require an active
`derivable` certificate for each configured claim target.

Eligibility is deliberately narrow. Formal targets attach only to `claim`, `hypothesis`,
`prediction`, or `conclusion` nodes with no status or status `current`/`confirmed`. Grounded result
nodes must be `artifact`, `figure`, `experiment`, or `data` with no status or status
`current`/`confirmed`/`null`. Any graph structural error blocks the snapshot; stale, retired,
invalid-status, or otherwise unhealthy nodes in the scoped upstream provenance closure deactivate
the proof. Live reevaluation returns bounded, structured drift records for changed claims, results,
policy assets, provenance nodes and edges, health state, and binding anchors; it does not reduce
drift to one opaque hash mismatch.

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
for historical work or infers a dependency from a filename. When asked to compare a result with a
claim, the skill prepares only the external semantic `agent_input` proposal with exact anchors; it
does not self-accept its judgement or inject computed fields. Where a project configures symbolic
policy, the same skill submits a `claimtrace.symbolic-selection/1` document containing only existing
result/binding IDs plus public note and provenance. Claimtrace materializes the typed facts and
pinned target. The skill never creates pointers, atoms, rules, proof steps, or proof states.
Agents still need the user or an independent reviewer to judge scientific meaning; Claimtrace
automates integrity, grounding, conditional inference, reconciliation, propagation, policy checks,
and display.

The selection interface prevents callers from inventing a mapping, but it does not yet prevent
cherry-picking among valid mappings. A caller can omit an approved binding unless the repository's
own workflow requires it. The next policy layer should let each formal claim declare an exhaustive
evidence plan (required binding IDs or a deterministic graph query plus inclusion/exclusion rules)
that Claimtrace materializes without caller choice.

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

The system deliberately has four evidence layers:

- The **semantic graph** contains attributed scientific assertions: hypotheses, predictions,
  methods, claims, conclusions, and their declared dependencies. A generic command wrapper must not
  invent or silently mutate those assertions.
- The **mechanical receipt ledger** records declared inputs/outputs, stable pre/post SHA-256 file
  versions, direct-child argv and return code, best-effort Git/lockfile context, and project-window
  deltas.
- The **semantic assessment ledger** stores an external agent's schema-constrained interpretation,
  exact evidence anchors, claimtrace-computed hashes and policy findings, and a separate reviewer's
  immutable decision. It can detect drift and disagreement; it cannot make the interpretation true.
- The **symbolic derivation ledger** stores typed premises grounded through project-owned complete
  fact profiles, the pinned vocabulary and rule pack, and a Claimtrace-computed composite proof.
  It establishes conditional derivability only; it cannot certify premise truth, scientific
  support, or equivalence between a formal target and the prose claim.

“Project-owned” means declared in project files; it is a governance convention, not an access
control boundary. A process that can edit the graph, bindings, vocabulary, or rules can change the
formal interpretation and produce a fresh derivation under that changed policy. Asset IDs are
logical identifiers, not authorization or immutable content-hash pins. Stored derivations snapshot
policy content and become stale when it changes, but Claimtrace does not decide whether a new
version was authorized. Protect meaning-bearing files with repository review, `CODEOWNERS`, CI hash
pins, signatures, or an equivalent control appropriate to the project. CLI `--actor` values and
`provenance.agent` strings are attributed, bounded text, not authenticated identities.

Automatic materialization proves only that a configured extractor returned a typed value from the
pinned bytes. The choice of predicate, polarity, extractor, rules, and formal target remains a
human-reviewed semantic mapping. It can be internally exact and still represent the wrong
scientific construct. Semantic assessments and independent review cover that different question;
neither layer should be described as proof of scientific truth.

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

Event, assessment, review, and derivation files are append-only through the Claimtrace API, and
content addressing detects modification of surviving files and broken surviving references. Their
local directories have no independently anchored head: deleting or omitting a complete event pair,
assessment/review chain, or derivation may be invisible unless a separate coverage policy happens to
require it. Use Git or another external ledger commitment when completeness or deletion evidence is
required.

The current stores, reconciliation report, and standalone view use project-local JSON and in-memory
aggregation. They are intended for ordinary research-project graphs, not Arkham/MetaSleuth-scale
multi-tenant ingestion. The schemas are portable, but a large deployment will need indexed storage,
incremental reporting, and adapter boundaries around this deterministic core.

## License

MIT © Gabriel Wainstein. Developed with the assistance of Claude Code.
