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
review step. A separate normalization ledger lets projects define local terms, search only exact
matches in locally locked ontology indexes, review an attributed SKOS mapping, and activate an
explicit release made from exact accepted mapping IDs. For formalizable claims, a separate
data-only symbolic layer grounds complete typed
  facts from result artifacts and computes whether a pinned target is derivable under project-owned
  rules, without presenting that conditional proof as scientific truth. Opaque scripts can be
  wrapped in an exact multistage contract: Claimtrace pins their input, code, method, anchor,
  parameter, seed, and output declarations; a fresh-workspace replay then tests byte repeatability
  at the process boundary. Path-bearing internal outputs are automatically hashed as materialized
  intermediates. For code that can import Claimtrace, an optional cooperative checkpoint API can
  record that the child program reached every locked stage callsite in dependency order, and replay
  can compare that self-reported sequence. Pathless or in-memory values remain unobserved. A
  separately reviewed assessment checks whether the cited code anchors actually implement the
  written method steps.

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
- **Reviewed semantic normalization.** Local terminology, exact-byte ontology/index locks,
  deterministic exact candidate sets, immutable mapping reviews, and explicit releases make the
  chosen meaning inspectable without letting an agent silently invent or activate an identifier.
- **Reviewable opaque-script provenance.** Closed pipeline contracts map written method steps to
  exact code line anchors. Contract-bound receipts and fresh-workspace replay test the declared
  input-to-output boundary, including path-bearing internal outputs. Optional cooperative
  checkpoints add a fail-closed, replayable record that program control reached each locked
  callsite, without pretending a child self-report independently observes the computation or hidden
  in-memory values.

No database, daemon, or cloud is required. The mutable semantic graph is plain JSON; run receipts,
semantic assessments, semantic mappings/releases, and symbolic derivations use separate
content-addressed JSON stores. All remain readable, diffable, and Git-auditable.

## Beta capability boundary

Claimtrace is usable today for local, regular-file research workflows, but it is not a universal
execution monitor or a scientific-truth oracle. This matrix states the current trust boundary:

| Area | Beta status | Current boundary |
| --- | --- | --- |
| Child commands | **Supported now** | Any language or tool that can be launched as a child CLI can be wrapped at its process boundary. |
| Graph, receipts, and replay | **Supported now** | Deterministic project graph, content-addressed receipts, staleness checks, and repeated comparison of declared regular-file outputs. |
| Stage checkpoints | **Supported now, scoped** | The Python API accepts cooperative reports only from the exact launched direct-child PID. |
| Semantic and symbolic policy | **Supported with review** | Humans or agents may submit constrained proposals; separate review, pinned vocabularies/rules, and deterministic policy decide what becomes active or derivable. |
| Actual I/O and isolation | **Not provided** | Pre/post file bytes do not prove actual reads or write causation; replay is not hermetic against network or external-filesystem access. |
| Notebooks and distributed work | **Partial** | Materialized outputs can be wrapped, but persistent kernels and worker processes cannot report protocol-v1 stage checkpoints directly. |
| Databases, object stores, and directory datasets | **Partial** | Export regular files or a reviewed deterministic manifest/adapter; these stores are not first-class captured inputs. |
| Identity and scientific validity | **Not provided** | Actor labels are self-asserted, and no receipt, review, normalization, or proof certificate authenticates a person or establishes scientific truth. |

---

## Install

For a published release:

```bash
pip install claimtrace-provenance            # core engine (stdlib only)
pip install "claimtrace-provenance[verify]"  # + numpy/pandas if your verifiers use them
```

The PyPI distribution is named `claimtrace-provenance`; the installed Python package and command
remain `claimtrace`. PyPI's shorter `claimtrace` distribution belongs to an unrelated molecular-
figure project that exposes the same import and console-command names. Do not install both
distributions in one environment: package files and the `claimtrace` entry point can overwrite one
another. Use a fresh virtual environment and remove the unrelated distribution before installing
this one.

From a source checkout (including before the first renamed distribution is published):

```bash
git clone https://github.com/gabwainstein/claimtrace && cd claimtrace && pip install -e .
```

## Quickstart

```bash
cd my-analysis-project
claimtrace init                       # empty planning-safe config + graph; strict checks pass now
# claimtrace init --example           # optional runnable toy CSV + verifier instead
claimtrace install-skill --dir .      # optional: install agent + Claude project-local skills
# ... author and review the graph; agents use graph-propose/graph-apply as described below ...
claimtrace run --input data/raw.csv --input analysis/fit.py --output results/fit.json \
  -- python analysis/fit.py           # execute + capture a mechanical receipt
# for an opaque multistep script, add a reviewed claimtrace.pipeline-contract/1 file;
# --stage-checkpoints also requires one instrumented stage_checkpoint("<id>") call per stage:
claimtrace run --pipeline-contract claimtrace/fit.pipeline.json --stage-checkpoints \
  --input data/raw.csv --input analysis/fit.py --output results/fit.json \
  --param model=ols --seed numpy=7 -- python analysis/fit.py
claimtrace replay <run-id>             # >=2 fresh workspaces; exact output-byte comparison
claimtrace assess-method method-proposal.json --actor analysis-agent
claimtrace review-method <assessment-id> --state accepted --actor methods-reviewer
claimtrace check                      # paths exist? nothing stale? nothing on a retired branch?
claimtrace check --strict --json      # deterministic graph + receipt reconciliation gate
claimtrace impact --set model=v2      # what must change if I switch the canonical model?
claimtrace downstream art:clean_data  # what depends on this artifact?
claimtrace verify                     # do my headline numbers still reproduce from disk?
claimtrace assess proposal.json --actor analysis-agent  # propose a grounded semantic judgement
claimtrace assessments --json        # inspect proposals, reviews, findings, and staleness
# optional normalization, after adding project terminology + locked ontology/index assets:
claimtrace ontology-candidates "memory score" --language en --limit 25 --json  # exact matches only
claimtrace map-term mapping.json --actor analysis-agent --language en --limit 25  # reuse that profile
claimtrace review-mapping <mapping-id> --state accepted --actor reviewer
claimtrace compile-semantic-policy policy.json --actor policy-owner  # still inactive
claimtrace semantic-status --json    # recheck locks, reviews, release, and explicit activation
claimtrace evidence-plan claim:gate --json  # inspect the claim-owned exact required bindings
claimtrace derive plan-request.json --actor analysis-agent  # request automatic all-of materialization
claimtrace derivations --json        # inspect conditional proof states and current validity
claimtrace explain <derivation-or-proof-id> --json  # inspect one composite proof certificate
claimtrace journal --status dead_end  # show me everything I already tried that didn't work
claimtrace view --output research-map.html  # semantic trajectory + mechanical receipt overlay
```

Default `claimtrace init` creates an empty valid graph, configures no executable verifier, and
invents no dataset or analysis path. It therefore can accompany a study before data, code, claims,
or terminology policy exist, and `claimtrace check --strict` is green immediately. Add graph nodes
only as their real project objects are planned or created. Use explicit `--example` when you want
the small runnable CSV and row-count verifier instead. Both modes deliberately leave semantic
assets empty. The
[Penguins normalization walkthrough](examples/penguin_study/SEMANTICS.md) shows the complete
proposal, separate review, inactive release, and explicit activation lifecycle.

## Opaque or multistep scripts

Many real analyses clean data, transform variables, fit models, and export results inside one
script without materializing or printing every intermediate. Claimtrace handles that case as five
separate evidence questions instead of treating one successful exit code as proof of everything:

1. A method node declares small, stable steps in `claimtrace.method-spec/1`. A claim explicitly
   names the method steps it relies on with `claimtrace.method-requirements/1`; that set may be a
   subset of the full method specification but must exactly equal its selected producer ancestry.
2. A `claimtrace.pipeline-contract/1` document maps each method step to exact code nodes and
   SHA-256-pinned text-line anchors, declares a stage DAG, and gives every declared input/output an
   exact graph role. Parameters and seed names are an exact allow-list.
3. `claimtrace run --pipeline-contract ...` records an event-v3 receipt and a portable computation
   identity. Any non-terminal stage output whose existing graph node has a path is automatically a
   materialized intermediate: its pre/post process-boundary bytes are captured separately from the
   terminal outputs. `claimtrace replay` repeats that successful computation at least twice in
   newly created workspaces populated only with the declared project inputs, compares every
   terminal output and materialized intermediate with the original and other attempts by SHA-256
   plus size, compares stdout/stderr and visible undeclared workspace file-path deltas across
   attempts, and reports undeclared workspace file writes.
4. When cooperative instrumentation is explicitly enabled, project code calls
   `stage_checkpoint("<contract-stage-id>")` once after each stage body. The controller requires the
   exact contract stage set, dependency order, uniqueness, execution binding, and a caller path and
   line inside that stage's locked anchor. Each record also carries `reporter_pid`, which must equal
   the exact PID returned when Claimtrace launched the direct child. Such a run uses event-v4;
   replay-v3 repeats the checkpoint protocol with fresh bindings and compares the normalized
   callsite sequence with the source run and every attempt.

   ```python
   from claimtrace.pipeline import stage_checkpoint

   # Run and validate the declared stage body first.
   stage_checkpoint("fit")
   ```

   The call returns `False` and writes nothing outside a checkpoint-enabled Claimtrace child. It
   returns `True` after appending the bound record inside one. Recompute the code-anchor line range
   and digest after adding the call, and keep the call itself inside that stage's anchor. Claimtrace
   always removes all reserved checkpoint environment variables before launching a run/replay child,
   then adds a fresh controller-created binding only for a traced run or replay attempt.
5. An agent may submit a bounded `assess-method` proposal over the exact method, contract, code, and
   anchors. Claimtrace computes the mechanical snapshot and drift checks; a different self-asserted
   actor must accept, reject, or contest the semantic judgement.

This intentionally produces five distinct statements:

- **Boundary byte-repeatable:** the declared outputs matched in fresh-workspace attempts under the
  recorded partial environment scope. For event-v3/replay-v2 evidence, every materialized
  intermediate also matched the source receipt and all attempts.
- **Intermediate file observed:** a declared path had exact pre/post whole-process fingerprints.
  This does not show which stage wrote it, whether an earlier stage produced the final bytes, or
  whether it was rewritten later.
- **Stages declared:** the contract says which code spans correspond to which method steps; it does
  not prove those branches ran. Pathless, ephemeral, or in-memory intermediates remain unobserved.
- **Cooperative checkpoint reached:** when enabled, the child reported that program control reached
  every required locked callsite once and after its declared dependencies. This is child self-report,
  not independent observation of the computation, intermediate values, method meaning, or scientific
  support. Only the launched direct child PID is accepted: a subprocess, multiprocessing worker, or
  persistent notebook kernel cannot satisfy checkpoint protocol v1 through the API. Join workers and
  validate their results first, then emit the stage checkpoint from the launched parent process.
- **Method conformance reviewed:** an attributed reviewer accepted that the pinned code anchors
  implement the written method. This remains a semantic judgement, not proof that the method is
  scientifically appropriate or that the claim is true.

Replay does not isolate network access, external filesystem paths, databases, GPUs, clocks,
schedulers, or every host-library and kernel detail. A child with an absolute path or inherited
environment value can still write outside the fresh workspace; Claimtrace neither observes nor
prevents that write. It observes the direct child and workspace file-path pre/post state, not write
causation or background descendants. Ordinary directory-only changes, including empty directories,
are not recorded. Seeds and parameters are exact recorded declarations; Claimtrace does not
magically inject them into arbitrary code. If a script depends on hidden external state, either
materialize and declare that state, wrap it in a deterministic adapter, or leave the result marked
as only partially covered. A child can emit a cooperative checkpoint without performing the
intended computation, so the trace is not an adversarial attestation. Legacy event-v2 receipts and
replay-v1 certificates remain readable as historical evidence, but contain no materialized-
intermediate evidence; Claimtrace does not infer it from a matching terminal output. Current
review-ready claim provenance requires an event-v3 source, or event-v4 when stage checkpoints are
required.
A replay-v1 certificate can retain eligibility only when its source is event-v3 and declares no
materialized intermediate; otherwise its terminal-output repeatability remains historical scope.

The controller caps the raw cooperative JSONL trace at 1 MiB so the source plus repeated attempt
traces remain representable inside the bounded replay-certificate document. The normalized
`result_id` deliberately excludes per-execution nonce, raw-journal hash/size, and reporter PID so an
otherwise identical result has a stable identity. The content-addressed finish event still commits
the complete stored trace, including those binding fields. Direct-child PID checking and fresh
bindings prevent accidental or stale mixing, not raw protocol forgery: cooperating code that knows
the inherited binding can bypass the API and name the expected direct-child PID in raw JSON.

For an unredacted source run, replayed argv, cwd, and argv-capture mode must exactly match the
content-addressed start plan. A redacted source argv instead requires a caller-supplied command that
matches the stored non-secret shape. The current certificate deliberately stores neither the secret
values nor a guessable digest of them, so it cannot later prove that this override was the original
command. Such a replay may retain its attempt-level byte outcome, but returns non-review-ready with
exit code 3 and must be described only as replay of the supplied override.

Projects can adopt this gradually. The four execution policy switches default to `false` so an
empty or existing project remains usable; enable them only after its contracts, instrumentation,
and review records exist. Unlike the other gates, `require_stage_checkpoints` also turns on
checkpoint capture for subsequent contract-bound runs:

```json
{
  "execution": {
    "replays": "claimtrace/replays",
    "method_assessments": "claimtrace/method-assessments",
    "require_contracts": true,
    "require_replay": true,
    "require_method_assessments": true,
    "require_stage_checkpoints": true,
    "replay_attempts": 2
  }
}
```

The report and interactive map deterministically start at the exact stage that declares the claimed
terminal result and follow its transitive stage dependencies. The claim's method requirements must
equal the method/step pairs on that producer ancestry, and accepted current method-conformance
reviews must cover each exact stage. Every path-bearing intermediate on that ancestry must also have
a current receipt binding. Producer-ancestry selection, claim requirement equality, and ancestry
intermediate binding are branch-local. Replay is deliberately whole-command: any terminal output
or materialized intermediate in the producing contract can block the replay. Method conformance is
also whole-method within that contract, so an off-ancestry stage that reuses the same method ID can
block that method assessment. Use separate commands/contracts and method IDs when branches need
independent readiness. The projection then joins those facts with the accepted result-to-claim
semantic relation, producing receipt, current contract, and review-ready replay.
`ready_under_reviewed_provenance` means those reviewed provenance layers are current under the
documented partial boundary coverage. When `require_stage_checkpoints` is true, it additionally
requires a complete source cooperative trace whose normalized stage/callsite sequence repeats in a
current replay-v3 certificate. It explicitly does **not** mean scientific validity.
Strict report schema 1.7 exposes `stage_checkpoint_state` and keeps
`stage_execution_observation` explicitly labeled as cooperative self-report rather than independent
observation.

Readiness also fails closed on ledger integrity. An unreadable or invalid event file quarantines
all runs because the event store is no longer established; a start/finish link or snapshot mismatch
quarantines only that run. An invalid replay-store sibling suppresses current/review-ready status
for every replay certificate in that store. Semantic- and method-assessment stores use the same
flat, non-link, fail-closed boundary. If current replay certificates for one source run disagree on
review readiness, the positive certificate is suppressed and the run exposes a replay conflict;
fix or materialize the hidden state so the old computation, contract, inputs, or recorded lock
environment is no longer current, then record a new source run. An identical new run does not erase
the immutable contradiction. The records and issues remain visible in reports and the map, but
quarantined evidence cannot create a current claim binding.

Natural drift in an older contract-bound receipt, its replay, or its method assessment remains
visible but can become nonblocking historical information only after a genuine replacement exists.
For run/replay drift, Claimtrace requires a strictly later successful run with the exact same
pipeline output-role set, a current contract, current exact graph-output bindings, and a
review-ready replay; when checkpoint policy is enabled, that replacement must be event-v4 with a
current repeatable replay-v3 checkpoint trace. Method drift also needs a current accepted
implementation assessment for the same method. Merely re-running without replay is insufficient.
Integrity failures, replay non-repeatability or conflict, undeclared workspace writes, and capture
contract failures are not demoted by this historical-replacement rule.

## Reviewable graph changes and release commitments

Agents should not edit `graph.json` opportunistically. `graph-propose` accepts a closed, bounded
`claimtrace.graph-change-request/1` containing the exact current `base_graph_hash` and explicit
node, edge, and concept operations. It normalizes the request, validates the complete result graph,
and creates a content-addressed proposal without changing the graph. `graph-apply` holds the graph
lock, rechecks the proposal and base hash, and atomically writes only that reviewed result. If the
graph moved since proposal, application fails instead of rebasing or guessing.

Graph proposal v1 records content and base/result identities, not an authenticated approval or an
immutable in-tool reviewer chain. The human-review evidence therefore lives in the repository's PR,
commit-signing, or equivalent governance system. Proposal output paths are caller-chosen and are
not automatically discovered by the release manifest; retain a reviewed proposal in Git or model
it as an explicit graph-backed document when that artifact must be part of the published record.

`release-create` performs two complete collections and emits `claimtrace.project-release/1` only
when both inventories match. The manifest pins exact bytes, sizes, roles, and logical IDs for the
configured graph, graph-backed artifacts, render locks, the current files at pipeline-contract
source paths referenced by included records,
event and review stores, semantic assets, symbolic assets, replay certificates, and method
assessments. `release-verify` recollects the
project and fails on changed, missing, or newly in-scope files; `release-diff` gives an exact change
set between two valid manifests.

Release schema v1 remains compatible with both its canonical pre-checkpoint schema inventory and
the current inventory. Newly created manifests use the current inventory, which now also names
`claimtrace.stage-checkpoint/1`, `claimtrace.stage-trace-plan/1`, and
`claimtrace.stage-trace/1`; validation still accepts an otherwise canonical release-v1 manifest
created before those inventory keys existed.

The content-addressed `release_id` is the correct object to sign, place in a transparency log, or
anchor on a blockchain. That external commitment can make later substitution or omission
detectable relative to the anchored manifest. It does not authenticate Claimtrace's self-asserted
actor strings, prove that the committed release was complete before anchoring, or validate any
scientific interpretation.

Pipeline snapshots preserve the normalized historical contract plus its exact source fingerprint.
The release role `pipeline_contract_current_source` means only the file currently at a referenced
path; it is not a retained copy of every prior raw contract document. Use versioned contract paths,
Git history, or an external content-addressed archive when those historical JSON bytes must remain
recoverable. Manifest entries for explicitly configured external assets contain absolute paths;
they are host-specific and can expose usernames or workspace layout in a public release.

## Try the real-data demo

From a source checkout, start with the stdlib-only [Palmer Penguins study](examples/penguin_study/).
Its pinned CC0 source
CSVs, deterministic raw-to-curated transformation, results, figure, run receipts, semantic-review
history, and symbolic proof are checked in:

```bash
cd examples/penguin_study
claimtrace verify
claimtrace check --strict --json
claimtrace assessments --all --json
claimtrace derivations --json
claimtrace view --output research-map.html
```

The narrow result is the classical aggregation reversal: among 342 complete records, the pooled
bill-depth-on-bill-length slope is negative (`-0.085021`), while the Adelie, Chinstrap, and Gentoo
slopes are positive. The demo records that as a descriptive association, not a causal effect or a
general claim about penguin biology.

For a compact neuroscience example, see [EEGBCI motor-imagery decoding](examples/eegbci_study/).
It pins three PhysioNet EEGMMIDB files by official SHA-256, fits every CSP + LDA fold without using
the held-out run, and compares the observed score with a within-run label-permutation null. Its raw
EDFs are deliberately downloaded by the explicit fetch step rather than committed, and its optional MNE
environment is isolated from Claimtrace's stdlib-only core. In the pinned S001 analysis the mean
balanced accuracy is `0.842262`, versus a null 95th percentile of `0.657738` (`p = 0.005`); that is
a result for these three recordings, not a population or clinical-performance claim.

The [examples index](examples/) explains the scope and data-license boundary of each project. Run
the EEG example's explicit fetch step to download its raw files; opening the example alone does not
perform network access. The
synthetic [widget study](examples/widget_study/) remains as a small engine-test fixture, not the
public scientific showcase.

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
    { "from": "art:fit",  "to": "claim:slope", "rel": "derives_from" }
  ]
}
```

Use a structural relation such as `derives_from` to preserve result-to-claim propagation without
silently declaring that the result's meaning supports the prose. A reviewed semantic assessment
can project an attributed `supports`, `refutes`, or `related` relation separately.

Full vocabulary (node types, edge relations, statuses) is in [`docs/SCHEMA.md`](docs/SCHEMA.md).

## Commands

| command | what it does |
|---|---|
| `claimtrace check` | structural integrity · missing files/manifests · scoped canonical drift · retired dependencies · manifest completeness · input/output hash drift · downstream staleness |
| `claimtrace check --strict --json` | deterministic graph/receipt report; also blocks on lint, stale nodes, missing receipts, and declaration mismatches |
| `claimtrace lint` | warn on non-standard vocabulary + load-bearing nodes with no `backbone` (`--strict` to fail) |
| `claimtrace run ... -- COMMAND` | execute a direct child with explicit inputs/outputs and append content-addressed pre/post SHA-256 receipts |
| `claimtrace run --pipeline-contract FILE ... -- COMMAND` | bind an opaque command to exact code/method/stage/input/output declarations and write event-v3 receipts with path-bearing intermediate capture |
| `claimtrace run --pipeline-contract FILE --stage-checkpoints ... -- COMMAND` | additionally require one cooperative child checkpoint per locked contract stage; complete traces use event-v4 |
| `claimtrace replay RUN_ID [--repeat N]` | repeat a successful contract-bound run in fresh workspaces and compare terminal/intermediate bytes plus any event-v4 cooperative sequence |
| `claimtrace assess-method ENTRY --actor ID` | append a bounded external method-to-code conformance proposal over exact anchors |
| `claimtrace method-assessments [--json]` / `review-method ID ...` | inspect immutable method-review leaves or append a distinct review decision |
| `claimtrace view --output FILE` | render a standalone interactive trajectory with semantic, proof, contract, replay, method, and receipt details |
| `claimtrace graph-hash` | print the canonical current graph hash for review or external anchoring |
| `claimtrace graph-propose REQUEST --output FILE` | compile a bounded change request into a content-addressed proposal without mutating the graph |
| `claimtrace graph-apply PROPOSAL` | atomically apply a reviewed proposal only if its exact base graph still matches |
| `claimtrace release-create [--output FILE]` | collect a stable, exact-byte release manifest across configured graph and provenance stores |
| `claimtrace release-verify MANIFEST` / `release-diff BEFORE AFTER` | recollect and verify a release, or compare two valid manifests |
| `claimtrace impact --set concept=value` | ordered propagation to-do list for a canonical change |
| `claimtrace downstream <id>` / `upstream <id>` | transitive dependents / dependencies |
| `claimtrace node <id>` | a node and its edges |
| `claimtrace log entry.json` | append a lab-notebook node (+ edges); use for nulls/dead-ends too |
| `claimtrace journal [--status ...]` | every attempt grouped by verdict |
| `claimtrace assess ENTRY --actor ID` | append an external-agent semantic proposal; claimtrace computes evidence snapshots and policy output |
| `claimtrace assessments [--state ...] [--all] [--json]` | list current semantic assessments, or their immutable history with `--all` |
| `claimtrace review ASSESSMENT_ID --state ... --actor ID` | append a separate-actor acceptance, rejection, contest, or supersession decision |
| `claimtrace lock-ontology ENTRY --output FILE` | pin exact local ontology document and project-supplied index bytes; performs no fetch or OWL parsing |
| `claimtrace ontology-candidates QUERY [--language TAG] [--limit N] [--json]` | return a content-addressed set of exact IRI, preferred-label, or synonym matches from configured locks |
| `claimtrace map-term ENTRY --actor ID [--language TAG] [--limit N]` | append an attributed, inactive mapping proposal using the same candidate-search profile |
| `claimtrace mappings [--state ...] [--all] [--json]` | list mapping history, current review leaves, drift, conflicts, and release eligibility |
| `claimtrace review-mapping MAPPING_ID --state ... --actor ID` | append a separate-actor mapping review successor |
| `claimtrace compile-semantic-policy ENTRY --actor ID` | compile exact accepted current mapping leaf IDs into an immutable inactive release |
| `claimtrace semantic-status [--policy ID] [--json]` | recheck semantic assets, mappings, the active release, and optionally one exact inactive release |
| `claimtrace evidence-plan CLAIM_ID [--json]` | show the claim-owned exact all-of binding plan and its pinned vocabulary/rule pack |
| `claimtrace derive ENTRY --actor ID` | materialize a claim-owned evidence plan (preferred), select bindings for an unplanned claim, or import explicit typed facts |
| `claimtrace derivations [--state ...] [--json]` | list derivations reevaluated against the current graph, artifacts, vocabulary, and rules |
| `claimtrace explain DERIVATION_OR_PROOF_ID [--json]` | show one composite proof certificate, premises, assumptions, equivalent submissions, and effective state |
| `claimtrace snapshot` | lock each render and its declared inputs into a versioned SHA-256 manifest |
| `claimtrace verify` | run your project-specific numeric checks |
| `claimtrace summary` | node/edge/concept counts |
| `claimtrace init [--example]` | scaffold an empty planning-safe project, or explicitly install a runnable toy dataset and verifier |
| `claimtrace install-skill [--target ...]` | install the packaged `claimtrace-log` skill without silently overwriting project customizations |

New render locks use schema `claimtrace.render-manifest/2` and lowercase SHA-256 for the output and
every declared transitive input. Unversioned SHA-1 manifests from earlier releases remain readable
so their existing locks can be checked, but Claimtrace never writes that legacy form. To migrate
without silently blessing changed files, first run `claimtrace check`; only after the legacy lock
passes, run `claimtrace snapshot` and check again. Explicit unknown schemas, mixed SHA-1/SHA-256
fields, and malformed digests fail closed as `INVALID_MANIFEST`.

The standalone trajectory is also a navigable canvas: use the mouse wheel or the visible controls
to zoom around the pointer, drag empty background to pan, and use **Fit** to recover the full view.
The zoom and pan camera is page-local and never enters the graph or saved node-layout record.
The trajectory can be rearranged without changing the research record. Drag any node,
or choose it in **Focus** and use the keyboard-accessible directional buttons; connected edges and
labels follow immediately. **Auto-arrange** restores the deterministic logical-layer
layout. Manual coordinates live only in browser storage for the current origin and output path:
they are never written into the graph, provenance stores, or generated HTML. Consequently they
survive a refresh at the same URL but are not a portable or shared layout, and a different
localhost port has separate storage. Edges follow orthogonal lanes with rounded turns around padded node boundaries;
moving any node reroutes the graph so unrelated relationships cannot remain hidden underneath it.
Click an edge, its label, or choose it under **Relationship** to highlight exactly that edge and its
direct source and target, with the full relation and traversal status shown in the details panel.
The header distinguishes no selector, a configured-but-invalid selector, and a valid active
release. Its mapping drilldown shows the local definition, proposed relation and rationale,
limitations, reviewer, live findings, release eligibility, and whether the exact mapping leaf is
selected in the active release. Mapping policy remains a separate context layer rather than being
drawn as a scientific-support edge.

## Grounded semantic assessments

A graph edge can declare that a result supports a claim, but a hash cannot tell whether the result
actually has the same population, exposure, comparator, outcome, direction, magnitude, time scope,
or inference level as the claim. Semantic assessments add that meaning check without pretending it
is fully mechanical.

An external agent submits a JSON object containing exactly `claim_id`, `result_ids`, and
`agent_input`. Both supported assessment schemas accept exactly one result per assessment; new
records use v2 and legacy v1 records retain v1 policy semantics. The agent authors the verdict,
complete structured claim and result frames, all eight fixed
alignment dimensions, exact evidence anchors, a concise rationale, limitations, and its provenance.
Allowed verdicts are `supports_as_written`, `supports_narrower_claim`,
`contradicts_as_written`, `insufficient`, `ambiguous`, and `unrelated`. Evidence anchors must point
to exact JSON values with JSON Pointer or exact text line spans with a SHA-256 digest.

The external agent must not provide `mechanical_snapshot`, `derived`, or a review decision.
Claimtrace computes the node and artifact SHA-256 identities, resolves every anchor, detects later
node/file drift and conflicting active assessments, and derives the eligible relation and findings.
A narrowing can activate `related`; it never becomes support for the original wording.
`supports_as_written` and `contradicts_as_written` otherwise require complete alignment. The one
narrow specificity exception is magnitude: a result may state a magnitude when a qualitative
directional claim does not. Missing result magnitude, partial or mismatched magnitudes, and any
unstated population, exposure, comparator, outcome, direction, time scope, or inference level still
fail closed.

```bash
claimtrace assess semantic-proposal.json --actor analysis-agent --json
claimtrace assessments --state proposed --json
claimtrace review assessment:sha256:<digest> --state accepted --actor separate-reviewer --json
claimtrace assessments --all --json
```

Assessments are immutable, content-addressed JSON documents. A review appends one successor; it does
not rewrite or branch the proposal, and it preserves the predecessor's schema-bound policy. Mixed
v1/v2 stores are supported, but one review chain cannot switch policy versions. The first reviewer
string must differ from the proposer string,
but those identities are self-asserted rather than authenticated. Acceptance fails closed for
invalid anchors, staleness, accepted-review conflicts, store-integrity failures, and inconsistent
modality declarations. An unreviewed conflicting proposal cannot deactivate an accepted relation.
Set `"require_assessments": true` to make semantic coverage a strict requirement. A direct
`supports` or `refutes` declaration must have a matching accepted relation. A structural
`derives_from` edge from a result-like node to a claim-like node must have a current accepted
assessment with an active `supports`, `refutes`, or `related` relation. An unreviewed pair, or an
accepted judgement that activates no relation, then blocks strict checking. The default is
advisory for gradual adoption, and an accepted opposite-polarity assessment against a direct
declaration is always a hard conflict.

Acceptance records an attributed judgement under this policy. It is not proof that the analysis is
valid or the scientific claim is true. The checked-in widget demo makes the distinction concrete:
`art:fit` has accepted support for the associational `claim:slope`, while the same OLS artifact is
only `related` to the causal `hyp:linear` after an accepted `supports_narrower_claim` assessment.
There is no direct support edge from the fit to that hypothesis.

Staleness pins the exact claim node, result node, and result artifact. It does not snapshot every
upstream graph edge or canonical concept; those remain the separate graph/receipt integrity layer.

## Reviewed semantic normalization

Scientific projects rarely use one vocabulary. Claimtrace now provides an optional normalization
layer that keeps the flexible judgement small and reviewable while making discovery, identity,
drift, conflict, and activation deterministic. A project declares its own terminology and one or
more local ontology locks:

```json
{
  "semantics": {
    "terminologies": ["claimtrace/semantics/local-terms.json"],
    "ontology_locks": ["claimtrace/semantics/domain.lock.json"],
    "mappings": "claimtrace/semantics/mappings",
    "policies": "claimtrace/semantics/policies",
    "active_policy": null,
    "allow_external_sources": false,
    "require_active_policy": false,
    "language": "en",
    "max_candidates": 25,
    "max_ontology_bytes": 536870912
  }
}
```

Normal operation is offline. Candidate search uses only configured local locks and accepts exact
IRI identity or exact preferred-label/synonym text after a versioned Unicode/case/whitespace
normalization. IRI identity remains case-sensitive. There is no fuzzy search, embedding search,
remote resolver, or agent-generated fallback IRI. No match and ambiguity are records to preserve,
not errors to hide.

If discovery uses `--language` or `--limit`, pass the same flags to `map-term`. The proposal pins
the candidate-set ID, and Claimtrace rejects a proposal whose recomputed profile does not reproduce
that ID. Candidate JSON also carries the fixed project-supplied-index assertion and explicit
limitations, so exact-byte integrity is not mistaken for verified RDF/OWL extraction.
The candidate profile pins the runtime Unicode data version used for NFC, case folding, and
whitespace classification. A runtime upgrade therefore makes the changed profile visible and
requires affected mappings to be re-evaluated instead of silently changing their meaning.

An ontology lock content-addresses its identity/version metadata and pins the SHA-256 and size of
every supplied source document and the supplied term index. The index is a bounded,
project-supplied, unverified assertion used for deterministic search; v1 does **not** parse OWL/RDF or prove
that each indexed label, definition, parent, or IRI was extracted from the locked ontology bytes.
Likewise, declared imports are checked only against configured lock identities. Claimtrace does not
discover or prove a complete `owl:imports` closure. Generate and review the index outside this
trust boundary, record its generator/version in project provenance, and do not describe a green
lock as ontology-semantic validation.

`lock-ontology` only creates the exact project-local output path already declared in
`semantics.ontology_locks`. It never replaces differing lock bytes, even on request; configure a
new path for a new ontology release. This prevents a lock-creation command from overwriting the
graph, config, another policy asset, or a provenance ledger.

The lock request is path-based and differs from the stored lock document. Source paths are resolved
relative to the configured `--output` file's directory. Place `domain.owl` and a reviewed
`index.json` beside the future lock, declare
`claimtrace/semantics/domain/domain.lock.json` in `semantics.ontology_locks`, and submit:

```json
{
  "ontology_id": "project:domain-ontology",
  "ontology_iri": "https://example.org/domain/",
  "version": "2026-07-15",
  "version_iri": "https://example.org/domain/releases/2026-07-15",
  "license_iri": "https://creativecommons.org/licenses/by/4.0/",
  "documents": ["domain.owl"],
  "index": "index.json",
  "imports": [],
  "declared_imports_available": false
}
```

```bash
claimtrace lock-ontology lock-request.json \
  --output claimtrace/semantics/domain/domain.lock.json --json
```

`max_ontology_bytes` bounds all configured locked ontology and index bytes hashed by one semantic
operation. It defaults to 536,870,912 bytes (512 MiB); larger projects must opt in explicitly, up
to the hard 274,877,906,944-byte (256 GiB) ceiling. Raising it increases worst-case validation time.

The mapping proposal contains only a local-term identity and agent-authored input. Claimtrace
recomputes the bounded candidate set and snapshots the local term and selected candidate. An
untruncated set contains every exact match; a truncated set preserves only the visible prefix plus
the total count/truncation evidence and is never policy-eligible. The
allowed decisions are `skos:exactMatch`, `skos:closeMatch`, `skos:broadMatch`,
`skos:narrowMatch`, `skos:relatedMatch`, and `unmapped`. Here, broad/narrow are read from the local
term toward the external target: `broadMatch` means the target is broader; `narrowMatch` means the
target is narrower. `exactMatch` requires scope-level interchangeability, not merely the same
label. Incompatible entity kinds and truncated candidate sets cannot enter a policy.

```json
{
  "terminology_id": "study:terms",
  "term_id": "study:memory-score",
  "agent_input": {
    "relation": "skos:closeMatch",
    "target": {
      "ontology_lock_id": "ontology-lock:sha256:<64-hex-digest>",
      "iri": "<IRI copied exactly from ontology-candidates output>"
    },
    "candidate_query": "memory score",
    "candidate_set_id": "candidates:sha256:<64-hex-digest>",
    "rationale": "Concise definition-level comparison.",
    "limitations": ["The project instrument is narrower than the indexed definition."],
    "provenance": {"agent": "analysis-agent"}
  }
}
```

The first reviewer string must differ from the proposer string, but both are self-asserted
attribution, not authentication. Acceptance still does not activate a mapping. A release request
must enumerate exact accepted, current, non-stale mapping leaf IDs:

```json
{
  "mapping_ids": ["mapping:sha256:<64-hex-digest>"],
  "note": "Reviewed terminology release for this analysis."
}
```

`claimtrace compile-semantic-policy` stores that release without activating it. Check an inactive
release explicitly with
`claimtrace semantic-status --policy semantic-policy:sha256:<digest> --json`. Activation is a
separate reviewed edit that pins the returned ID in `semantics.active_policy`; Claimtrace never
chooses “latest” or collects every accepted mapping implicitly. `semantic-status` and the strict
report then re-evaluate the active release against current terminology, locked bytes, candidate
sets, review leaves, conflicts, and store integrity. Set `require_active_policy` only after the
project is ready to make absence or invalidation blocking.

This v1 release is a terminology-policy ledger. It does not rewrite graph nodes, create scientific
support edges, assert `owl:sameAs`, or change a symbolic proof. The current symbolic-derivation
schema still pins its vocabulary/rule assets separately; binding those assets to an active semantic
release requires a future proof-schema migration so historical certificates cannot be silently
reinterpreted.

## Portable symbolic derivations

Projects that need reproducible claim logic can add a small, data-only symbolic layer. Claimtrace
does not hard-code a scientific domain or ontology: each project defines portable JSON
vocabularies (types, units, predicates, and renderers), function-free rules, result bindings, and
claim targets. A user, workflow, or external agent can therefore adopt the same engine in any
project without writing a Claimtrace plugin or allowing executable rule code.

The project owns the meaning-bearing policy. Each result node's `logic_bindings` contains a
**complete fact-binding profile** that pins one input predicate, its polarity, and an extractor for
every argument. Each claim-like node's `logic` declaration pins its exact target plus both the
`vocabulary_id` and `rule_pack_id`. A claim can additionally own an exact all-of
`logic_evidence_plan`: the requester then names only the claim, and Claimtrace materializes every
required project-declared binding. For a claim without a plan, the binding-selection interface
remains available as a fallback. Claimtrace reads the target and policy IDs from the claim, extracts
and canonicalizes every typed argument from the result artifacts, and computes the proof. The
request cannot supply a pointer, atom, target, polarity, rule, or proof step. One grounded fact has
exactly one evidence binding. This keeps the adaptable part declarative while making extraction and
inference deterministic. One result may expose profiles for multiple configured vocabularies; each
proof still uses the single vocabulary pinned by its claim. `claimtrace check` validates every
declared target, binding, and evidence plan, including extraction against the current artifact.

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
  "logic_evidence_plan": {
    "schema_version": "claimtrace.symbolic-evidence-plan/1",
    "required_bindings": [
      {"result_id": "art:test", "binding_id": "gate:test-completed"}
    ]
  }
}
```

The plan is a top-level field on the claim, hypothesis, prediction, or conclusion node. Its
`required_bindings` is a non-empty, duplicate-free list that Claimtrace canonicalizes by result and
binding ID. Inspect the resolved plan with `claimtrace evidence-plan claim:gate --json`, then submit
the claim-only request:

```json
{
  "schema_version": "claimtrace.symbolic-plan-request/1",
  "claim_id": "claim:gate",
  "note": "Materialize the project-reviewed test-gate plan.",
  "provenance": {"agent": "analysis-agent"}
}
```

`claimtrace.symbolic-plan-request/1` contains exactly the schema, claim ID, public note, and
attribution. It cannot add, remove, or replace a required binding. On a plan-governed claim, a
`claimtrace.symbolic-selection/1` proposal is accepted only when its canonical binding set exactly
matches the plan. A low-level proposal that omits or adds premises, or adds an assumption, cannot
produce an active plan-compliant proof.

For a claim with no `logic_evidence_plan`, `claimtrace.symbolic-selection/1` is the fallback: it
contains exactly `schema_version`, `claim_id`, a non-empty list of existing `{result_id,
binding_id}` objects, `note`, and `provenance`. The explicit
`claim_id`/`result_ids`/`vocabulary_id`/`rule_pack_id`/`agent_input` form remains available as a
low-level import, debugging, and explicit-assumption interface. It requires the caller to transcribe
canonical typed atoms, but still cannot submit computed snapshots or proof fields.

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
prose-to-target and binding-to-predicate mappings remain repository policy that needs separate
review; call that review independent only when the surrounding workflow establishes it. The
semantic-normalization ledger can review term-to-ontology mappings, but symbolic proof schema v1
does not yet bind those mapping releases to a proof. Set
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
policy and a claim-owned plan, the same skill submits a `claimtrace.symbolic-plan-request/1`
document containing only the claim ID, public note, and provenance. Claimtrace materializes every
required binding, the typed facts, and the pinned target. For an unplanned claim, the skill can fall
back to `claimtrace.symbolic-selection/1` with existing result/binding IDs. The skill never creates
pointers, atoms, rules, proof steps, or proof states.
Agents still need the user or a scientifically independent reviewer to judge scientific meaning;
Claimtrace automates integrity, grounding, conditional inference, reconciliation, propagation,
policy checks, and display. Claimtrace itself enforces only that the first reviewer actor string
differs from the proposer string; it does not authenticate people or establish independence.

The skill has two explicit modes. Routine analysis mode may log work, assess result-to-claim
meaning, and submit premises under already configured policy, but it cannot edit meaning-bearing
semantic assets. Semantic-authoring mode is entered only when the user asks for it. There an agent
may inspect locked candidates, preserve ambiguity or no-match, and submit a concise mapping,
vocabulary, or restricted-rule proposal. It still cannot invent an IRI, review its own proposal,
activate a mapping release, or describe accepted normalization as scientific truth. The bundled
`references/semantic-authoring.md` contains that operational boundary and is installed with the
skill.

An exact plan prevents the requesting agent from cherry-picking within that reviewed list. It does
not establish that the list includes every scientifically relevant result; that remains repository
policy and review.

## Optional: a git pre-commit hook

```bash
cp hooks/pre-commit .git/hooks/pre-commit   # runs `claimtrace check` (blocking) + `verify` (soft)
```

## Trust boundary

`claimtrace check`, `lint`, and the query commands are pure inspection. `claimtrace view` only reads
the project and writes the explicitly requested HTML output. `claimtrace run` executes exactly the
argv after `--` as a direct child with `shell=False`; `claimtrace verify` **executes your project's
`verifiers.py`** (a plugin model, like `conftest.py` or a `Makefile`). Run executable commands only
in projects you trust, and do not auto-run them on untrusted pull requests. `verify` exits 0 only
after at least one registered check ran and all registered checks passed; it exits 2 with
`NOT_CONFIGURED` or `NO_CHECKS` when no numeric verification actually occurred.

`claimtrace` verifies that the *machinery* is internally consistent — paths exist, no cycles or
dangling edges, claims cite on-backbone artifacts, headline numbers reproduce. It deliberately does
**not** claim your science is correct: a green check means the declared provenance is internally
coherent, not that the conclusion is right. That boundary is the point — it tells you what has *not*
been re-derived, so a human still does the judging.

The system deliberately keeps nine evidence layers separate:

- The **semantic graph** contains declared scientific assertions: hypotheses, predictions,
  methods, claims, conclusions, and their declared dependencies. A generic command wrapper must not
  invent or silently mutate those assertions.
- The **pipeline contract** stores an authored stage DAG, exact graph roles, method-step mappings,
  code files and line anchors, and required parameter/seed names. Its stages are declarations, not
  runtime traces. An internal produced node with a path is automatically classified as a
  materialized intermediate; a pathless internal node remains unobserved. Snapshot v3 binds stable
  file content and size but excludes clone-local filesystem timestamps from the contract identity.
  Stored v2 snapshots still validate their original exact content addresses; compatibility
  currentness ignores only their code/method `mtime_ns` fields, never bytes or scientific
  structure. Snapshot v1 retains its original exact comparison and coverage limits.
- The **mechanical receipt ledger** records declared inputs/outputs, stable pre/post SHA-256 file
  versions, direct-child argv and return code, best-effort Git/lockfile context, and project-window
  deltas. Event-v3 and event-v4 record materialized-intermediate paths and transitions separately
  from terminal outputs, after the whole process rather than at a stage boundary.
- The optional **cooperative stage trace** in event-v4 records a nonce-bound child report that every
  contract stage callsite was reached exactly once, inside its locked anchor and after its DAG
  dependencies. Reserved trace environment variables are scrubbed before launch and freshly bound;
  `reporter_pid` must match the exact launched direct child. A missing, duplicate, unknown,
  out-of-order, wrong-process, or unanchored checkpoint fails the run contract. Descendant processes
  cannot report through protocol v1, while the cooperative child can still forge raw records, so
  this is not independent execution observation, value capture, semantic validation, or scientific
  support.
- The **replay-certificate ledger** records multiple fresh-workspace attempts and exact output-byte
  comparisons against each other and the original receipt. Replay-v2 includes materialized
  intermediates; replay-v3 also requires complete matching cooperative stage/callsite sequences. It
  tests repeatability within its stated partial environment coverage, not stage attribution,
  adversarial attestation, or universal determinism.
- The **method-conformance ledger** stores an agent's bounded comparison of written method steps with
  exact contract/code anchors plus a distinct actor's immutable decision. It cannot observe hidden
  runtime stages or establish that the method is scientifically appropriate.
- The **semantic assessment ledger** stores an external agent's schema-constrained interpretation,
  exact evidence anchors, claimtrace-computed hashes and policy findings, and a separate actor's
  immutable decision. It can detect drift and disagreement; it cannot make the interpretation true.
- The **semantic normalization ledger** stores local-term definitions, exact locked source/index
  identities, attributed mapping proposals, immutable review leaves, and explicit policy releases.
  It can make normalization reproducible under those declarations; it cannot prove that a supplied
  index reflects OWL/RDF source semantics or that an accepted mapping is scientifically correct.
- The **symbolic derivation ledger** stores typed premises grounded through project-owned complete
  fact profiles, the pinned vocabulary and rule pack, and a Claimtrace-computed composite proof.
  It establishes conditional derivability only; it cannot certify premise truth, scientific
  support, or equivalence between a formal target and the prose claim.

Mechanical receipts intentionally preserve the observed executable and working-directory paths.
That can reveal usernames or workspace layout when a ledger is published. Argument-secret
redaction does not anonymize those environment paths; review them before release or generate the
public ledger in a neutral build environment. Never edit a content-addressed receipt in place.

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
scientific construct. Semantic assessment plus repository review can cover that different question;
neither layer should be described as proof of scientific truth or as independently reviewed unless
the surrounding workflow actually establishes that independence.

A claim-owned evidence plan provides completeness only relative to its reviewed exact list. It
prevents a requester from omitting, adding, or replacing bindings in that list, but an authorized
editor can still omit relevant evidence from the plan itself. Exact schema v1 also does not
automatically discover a newly added result. Graph-query plans are deliberately deferred until the
schema can represent corroborating bindings that yield duplicate logical atoms, keep unused matched
evidence from spuriously deactivating a proof, and record a deterministic query-resolution
certificate with explicit inclusion and exclusion decisions.

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

Event, replay-certificate, semantic-assessment, method-assessment, mapping, policy-release, review,
and derivation files are create-only or append-only through the Claimtrace API, and
content addressing detects modification of surviving files and broken surviving references. Their
local directories have no independently anchored head: deleting or omitting a complete event pair,
replay certificate, assessment/review chain, mapping/review chain, semantic release, or derivation
may be invisible
unless a separate coverage policy happens to require it. Use Git, signed release manifests, a
transparency log, or another external ledger commitment when completeness or deletion evidence is
required. A blockchain anchor can commit to such a release/root hash, but putting hashes on-chain
does not validate the scientific meaning, restore omitted records, or make self-asserted actor
strings authenticated.

Path checks reject static symbolic-link, junction, and reparse-point escapes, and cooperative local
writers are serialized. Claimtrace does not defend a privileged process against an untrusted
same-identity process that actively swaps workspace directories between individual filesystem
operations; do not run it elevated over an attacker-controlled checkout. Create-only publication
also requires local hardlink support and fails closed with an actionable error when the filesystem
cannot provide that atomic primitive.

Runtime locks coordinate cooperating Claimtrace processes on one host; they are not distributed
locks for a shared network workspace. Direct concurrent edits to terminology, ontology, or index
files are also outside the mapping/policy store lock. Such a race can publish an immediately stale
record, but status, reports, and activation reload the assets and suppress stale policy rather than
silently activating it. Quiesce semantic-asset edits while publishing a reviewed mapping release,
and commit the assets and release selector together through the project's normal review workflow.

The current stores, reconciliation report, and standalone view use project-local JSON and in-memory
aggregation. They are intended for ordinary research-project graphs, not Arkham/MetaSleuth-scale
multi-tenant ingestion. The schemas are portable, but a large deployment will need indexed storage,
incremental reporting, and adapter boundaries around this deterministic core.

## License

Claimtrace's original software and documentation are MIT licensed © Gabriel Wainstein. The example
datasets and data-derived artifacts keep their upstream terms; see the
[third-party notices](https://github.com/gabwainstein/claimtrace/blob/main/THIRD_PARTY_NOTICES.md).
Developed with the assistance of Claude Code and
Codex.
