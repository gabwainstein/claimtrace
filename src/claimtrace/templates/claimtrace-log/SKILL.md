---
name: claimtrace-log
description: Capture substantive research commands and record their outcomes in claimtrace, including positive results, nulls, dead ends, retractions, and superseded work. Use while running or immediately after analysis, or when asked to log or sweep research work. Do not infer unobserved file dependencies.
---

# Log research work

Use `claimtrace` as the system of record. Preserve failed and abandoned attempts as carefully as
successful ones so another scientist or agent can see what happened and avoid silently repeating it.

## Workflow

1. Find `claimtrace.config.json` by walking upward from the analysis directory. Resolve and state
   both its absolute path and configured project root before writing so an unrelated ancestor project
   is not mutated. Use that explicit config path in every command below and the project root as
   `--cwd` whenever executing a child. If none exists, stop the logging step and tell the user to run
   `claimtrace init` in the real project root.
2. Inspect relevant existing nodes with
   `claimtrace --config <absolute-config> node <id>` and inspect the command or script
   itself before declaring file roles. Use only verified literal inputs/outputs; do not infer them
   from filenames, prose, or a hoped-for workflow.
3. Before a substantive analysis command, declare those files and run it through the
   mechanical receipt wrapper:

   ```bash
   claimtrace --config /absolute/project/claimtrace.config.json run \
     --input data/clean.csv --input analysis/fit.py \
     --output results/fit.json \
     --param model=ols --seed numpy=123 \
     --cwd /absolute/project/root \
     -- python analysis/fit.py
   ```

   Use `--no-inputs` or `--no-outputs` when that role is genuinely empty; absence must be explicit.
   Paths are project-root-relative regular files, not globs or directories. Do not use
   `--allow-external` unless the user explicitly placed that external file in scope. A failed child
   command still receives a receipt and must not be promoted as a successful result.
4. If the substantive command already ran without the wrapper, do not invent a receipt or claim its
   files were observed. Do not re-run an expensive, stochastic, or state-changing analysis solely
   to manufacture provenance. Record the semantic outcome below and report the missing run receipt.
5. Classify the result honestly:
   - `confirmed`: the tested result held under the recorded analysis.
   - `null`: the planned test returned no supported effect.
   - `dead_end`: the approach was abandoned and why is known.
   - `retracted`: an earlier claim was withdrawn.
   - `superseded`: a newer result or method replaced this one.
6. Write a small temporary entry JSON:

   ```json
   {
     "node": {
       "id": "exp:<short-stable-slug>",
       "type": "experiment",
       "status": "confirmed",
       "date": "YYYY-MM-DD",
       "script": "path/to/script.py",
       "run_ids": ["run:<uuid printed by claimtrace run>"],
       "value": "one-line tested result and verdict",
       "note": "method boundary, failure reason, or key caveat",
       "backbone": {"dataset_version": "v2", "model": "m1"}
     },
     "edges": [
       {"from": "exp:<short-stable-slug>", "to": "claim:<existing-id>", "rel": "supports"}
     ]
   }
   ```

   A scalar `backbone` is valid only when the graph has exactly one concept. Omit fields that cannot
   be verified rather than guessing. Omit `run_ids` for historical or unwrapped analysis; never
   invent one. Every declared output must also have one active graph node with its verified `path`.
   If the run created a genuinely new output, log that materialized node and verified dependency
   edges before the semantic outcome so strict reconciliation does not leave it unbound.
   Render-type outputs also require a content-hash manifest. Run `claimtrace --config
   <absolute-config> snapshot` only after every render it will lock has been intentionally generated
   and checked in the current scope; never snapshot merely to silence a stale or missing-manifest
   finding.
7. Run `claimtrace --config <absolute-config> log <entry.json>`. Use `--update` only for a deliberate
   correction to the same stable node, never to make an ID collision disappear.
8. Run `claimtrace --config <absolute-config> check --strict --json` and
   `claimtrace --config <absolute-config> lint --strict`. The strict JSON report
   reconciles graph declarations with content-addressed run receipts and is the machine-facing gate;
   it does not execute project verifiers. Report exact failures and distinguish a successful graph
   write from a project-wide validation pass. Run `claimtrace verify` only when the project is trusted
   and numeric verification is part of the requested workflow.
9. Delete the temporary entry file after validation. If `claimtrace` is not on `PATH`, use
   `python -m claimtrace` only when the package is already importable; otherwise stop with the exact
   setup failure rather than changing the environment silently.

## Evidence rules

- Never invent node IDs, file paths, citations, identifiers, values, or relations.
- Use `path` only for a file confirmed to exist. Use `script` for informational references to code
  that may later be removed; `script` is not existence-checked.
- Add mechanical relations (`produces`, `renders`, `reads`, `derives_from`) only when a run receipt
  and graph declaration agree, or an existing trusted declaration establishes them. Never infer
  them from filenames or prose. The wrapper never mutates semantic graph edges automatically.
- Direct dependency edges from evidence to dependent result: `evidence -> claim -> document`.
- Use annotation relations (`refutes`, `tried_before`, `supersedes`, `related`) for notebook context;
  they do not participate in stale propagation.
- A null normally gets `related`, not `refutes`. Use `refutes` only when the result is genuinely
  incompatible with the target claim. Use `supports` only for positive evidence, and `tried_before`
  only when a specific replacement approach exists.
- Treat run inputs as `declared_only_not_observed`, matching graph inputs as `declarations_agree`,
  project-window delta evidence as `unattributed_pre_post_window`, and receipt-level write
  attribution as `unattributed_pre_post_delta`. Never rename these as observed reads or causally
  attributed writes.
- A green strict check establishes internal consistency within declared graph and partial runtime
  capture scope. It does not establish scientific truth, observed reads, complete writes, or
  scientific validity.

## Sweep a session

List substantive analyses chronologically, then log one focused node per result. Include negative
and abandoned work. Link existing run receipts when available; never backfill them by inference.
Finish with explicit-config `check --strict --json` and `lint --strict` for the whole graph, and
summarize exactly what was recorded and what remains declaration-only.
