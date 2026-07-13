---
name: claimtrace-log
description: Capture substantive research commands, record positive and negative outcomes, and propose grounded semantic assessments between results and claims in claimtrace. Use while running or immediately after analysis, when interpreting whether evidence supports a claim, or when asked to log or sweep research work. Do not infer unobserved dependencies or directly certify support.
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
5. Classify the result node honestly:
   - `confirmed`: the tested result held under the recorded analysis.
   - `null`: the planned test returned no supported effect.
   - `dead_end`: the approach was abandoned and why is known.
   - `retracted`: an earlier claim was withdrawn.
   - `superseded`: a newer result or method replaced this one.
6. Write a small temporary result-entry JSON. Record the result before interpreting its meaning;
   do not add a direct `supports` edge:

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
     "edges": []
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
8. When a result may bear on an existing claim, inspect the exact claim text, result artifact, method,
   and uncertainty. Write a separate temporary assessment proposal containing only the subject and
   agent-authored section:

   ```json
   {
     "claim_id": "claim:<existing-id>",
     "result_ids": ["art:<verified-result-id>"],
     "agent_input": {
       "verdict": "supports_narrower_claim",
       "claim_frame": {
         "population": "declared population",
         "exposure": "declared predictor or intervention",
         "comparator": null,
         "outcome": "declared outcome",
         "direction": "declared direction",
         "magnitude": null,
         "time_scope": "single study",
         "inference_level": "causal"
       },
       "result_frame": {
         "population": "analysed population",
         "exposure": "tested predictor or intervention",
         "comparator": null,
         "outcome": "measured outcome",
         "direction": "observed direction",
         "magnitude": null,
         "time_scope": "single study",
         "inference_level": "associational"
       },
       "alignment": {
         "population": "match",
         "exposure": "match",
         "comparator": "not_applicable",
         "outcome": "match",
         "direction": "match",
         "magnitude": "not_stated",
         "time_scope": "match",
         "inference_level": "mismatch"
       },
       "evidence_anchors": [{
         "result_id": "art:<verified-result-id>",
         "kind": "json_pointer",
         "pointer": "/estimate",
         "expected_value": 0.41
       }],
       "rationale": "The direction matches, but the recorded design identifies association rather than causation.",
       "limitations": ["The analysis does not identify a causal effect."],
       "recommended_claim": "Exposure was positively associated with the measured outcome in the analysed population.",
       "provenance": {
         "agent": "<reported-agent-id>",
         "model": "<reported-model-id>"
       }
     }
   }
   ```

   Use one verdict: `supports_as_written`, `supports_narrower_claim`,
   `contradicts_as_written`, `insufficient`, `ambiguous`, or `unrelated`. Use every fixed alignment
   Assessment v1 accepts exactly one result ID and requires every one of the eight keys in both
   frames and in `alignment`. Use JSON `null` for a frame value that is not stated or not applicable;
   never use the literal string `"not stated"`. `not_stated` requires at least one corresponding
   frame value to be `null`, while `not_applicable` requires both to be `null`.
   Do not call inference levels a match when the result modality is inadequate: descriptive evidence
   is not predictive, associational evidence is not causal, and only mechanistic evidence can match
   a mechanistic claim.
   Use `match`, `partial`, `mismatch`,
   `not_stated`, or `not_applicable`; do not omit an inconvenient dimension. Anchor each result to an
   exact JSON value or byte-exact text line span. `provenance.agent` is required and must identify
   the submitting agent truthfully; `model`, `skill_version`, and `prompt_sha256` are optional and
   must be omitted rather than guessed. Use a concise rationale, not hidden chain-of-thought.
9. Submit the proposal:

   ```bash
   claimtrace --config <absolute-config> assess <proposal.json> --actor <agent-id> --json
   ```

   Never provide `mechanical_snapshot`, `derived`, a review decision, or
   a direct semantic edge; Claimtrace computes hashes, grounding checks, policy findings, and the
   proposed relation. Treat a non-zero result as blocked, not as permission to rewrite the verdict.
10. Leave the assessment in `proposed` state. A scientist or explicitly independent reviewer may run
     `claimtrace review <assessment-id> --state accepted|rejected|contested --actor <reviewer-id>`.
     The first reviewer actor must differ from the proposer. Actor strings are self-asserted rather
     than authenticated, so do not describe this as cryptographic authorization.
     `supports_narrower_claim` relates evidence to the original claim but never
    supports the original wording; create a new narrowed claim only after review.
11. Run `claimtrace --config <absolute-config> check --strict --json` and
   `claimtrace --config <absolute-config> lint --strict`. The strict JSON report
   reconciles graph declarations with content-addressed run receipts and is the machine-facing gate;
   it also surfaces pending, stale, contested, invalid, or missing semantic assessments. It does not
   execute project verifiers or establish scientific truth. Report exact failures and distinguish a
   successful write from a project-wide validation pass. Run `claimtrace verify` only when the
   project is trusted and numeric verification is part of the requested workflow.
12. Delete temporary entry and proposal files after validation. If `claimtrace` is not on `PATH`, use
   `python -m claimtrace` only when the package is already importable; otherwise stop with the exact
   setup failure rather than changing the environment silently.

## Evidence rules

- Never invent node IDs, file paths, citations, identifiers, values, or relations.
- Use `path` only for a file confirmed to exist. Use `script` for informational references to code
  that may later be removed; `script` is not existence-checked.
- Add mechanical relations (`produces`, `renders`, `reads`, `derives_from`) only when a run receipt
  and graph declaration agree, or an existing trusted declaration establishes them. Never infer
  them from filenames or prose. The wrapper never mutates semantic graph edges automatically.
- Let an accepted assessment project a semantic result-to-claim relation. Do not manually duplicate
  that relation or write a new direct `supports` edge from an agent judgement.
- Use annotation relations (`refutes`, `tried_before`, `supersedes`, `related`) for notebook context;
  they do not participate in stale propagation.
- Do not infer `supports`, `refutes`, or `related` from the desired narrative. Express the evidence
  comparison through the assessment verdict and let Claimtrace derive the eligible relation.
- Treat run inputs as `declared_only_not_observed`, matching graph inputs as `declarations_agree`,
  project-window delta evidence as `unattributed_pre_post_window`, and receipt-level write
  attribution as `unattributed_pre_post_delta`. Never rename these as observed reads or causally
  attributed writes.
- A green strict check establishes internal consistency within declared graph, partial runtime
  capture, and configured semantic-review policy. An accepted assessment remains an attributed
  judgement; it does not establish scientific truth, observed reads, complete writes, or scientific
  validity.

## Sweep a session

List substantive analyses chronologically, then log one focused node per result. Include negative
and abandoned work. Link existing run receipts when available; never backfill them by inference.
Propose one grounded assessment per material result-claim comparison and preserve disagreement rather
than choosing the most convenient review. Finish with explicit-config `check --strict --json` and
`lint --strict`, then summarize what was recorded, what remains declaration-only, and which semantic
assessments still need review.
