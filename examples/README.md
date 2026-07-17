# ProvSleuth examples

The public examples keep ProvSleuth's core domain-neutral: scientific choices live in each
project's graph, verifiers, semantic-assessment inputs, and data-only rule packs.

The checked-in provenance stores were created before the Claimtrace-to-ProvSleuth rename. Their
`claimtrace.config.json`, `claimtrace/` paths, `claimtrace.*` schemas, hashes, and immutable ledgers
remain unchanged. Current read-only inspection uses the ProvSleuth CLI with the legacy config
explicitly; fresh ProvSleuth-native demos should start new provenance histories.

| example | use it for | setup | data/output terms |
|---|---|---|---|
| [`penguin_study`](penguin_study/) | Start here. A complete Palmer Penguins trajectory with pinned CC0 data, a deterministic raw-to-curated check, pooled and species-specific OLS results, narrow claims, semantic review, and a symbolic sign rule. | Frozen Claimtrace-era fixture readable by ProvSleuth; its hash-locked analysis is not a clean ProvSleuth-only rerun. | Palmer Penguins source CSVs: CC0 1.0. |
| [`eegbci_study`](eegbci_study/) | A compact neuroscience example using PhysioNet EEGMMIDB S001 motor-imagery runs, leave-one-run-out CSP + LDA, and a run-constrained permutation null. | An isolated optional MNE environment; an explicit fetch step downloads the three hash-pinned EDFs, which are not committed. | EEGMMIDB source and derived-data/produced-work notices: ODC Attribution 1.0; see the example README and root third-party notices. |
| [`widget_study`](widget_study/) | A tiny synthetic fixture used by ProvSleuth's engine tests and low-level tutorials. | Legacy-path fixture readable by ProvSleuth. It is not a scientific showcase. | Repository MIT license. |

The semantic records are attributed judgements about whether result meaning matches claim wording;
they are not certificates of scientific truth. The symbolic records answer only whether grounded
artifact values derive a configured target under the checked-in project rules. Both remain separate
from mechanical run receipts and project-specific numeric verifiers.
