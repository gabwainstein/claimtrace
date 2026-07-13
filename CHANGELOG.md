# Changelog

All notable changes to `claimtrace` are documented here. This project adheres to
[semantic versioning](https://semver.org/).

## 0.2.0

First release under the name `claimtrace` (generalised and hardened from the internal `provkg`
prototype).

### Added
- **Structural-integrity checks** in `claimtrace check`: `DUPLICATE_ID`, `DANGLING_EDGE`,
  `SELF_EDGE`, `CYCLE`, `MALFORMED_EDGE` — a typo'd edge endpoint or an accidental cycle now fails
  loudly instead of silently mis-reporting OK.
- **`claimtrace lint`** — advisory warnings for non-standard node types/statuses/edge rels, a
  missing/mismatched `schema_version`, and load-bearing nodes with no `backbone` (which would
  otherwise never be drift-checked). `--strict` turns warnings into a non-zero exit.
- **`schema_version`** field in `graph.json` (currently `"1.0"`), with a forward-compat warning.
- **`input_types`** config option, so render-staleness inputs are no longer hard-coded to
  `data`/`artifact`/`code` — projects that type their nodes differently are no longer silently
  un-checked.
- **`python -m claimtrace`** entry point.
- Graceful, actionable errors on a missing / unparseable / structurally invalid graph (a clean
  message and exit code, never a raw traceback).
- UTF-8 BOM tolerance when reading `graph.json` / `claimtrace.config.json` (common on Windows).

### Changed
- Package, CLI, config file, and default graph directory renamed `provkg` → `claimtrace`.
- `snapshot` now imports the annotation-relation set from the engine instead of re-inlining it
  (single source of truth), and honours `input_types`.
- README documents the trust boundary (`verify` executes project code) and the deliberate
  mechanical-vs-scientific-adequacy line.

### Notes / not yet done (roadmap)
- Content hashing is still SHA-1 over raw bytes; normalized-content hashing (to avoid false
  `STALE_DATA` on non-deterministic figures/parquet) and a SHA-256 upgrade are planned.
- Declared-vs-observed dependency reconciliation (does the code actually read what the graph says?)
  is planned and is the feature that turns this from a discipline aid into a guarantee.
