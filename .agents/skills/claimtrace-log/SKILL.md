---
name: claimtrace-log
description: Capture substantive research commands, record positive and negative outcomes, propose grounded semantic assessments, and submit typed symbolic premises to project-owned Claimtrace rule packs. Use while running or immediately after analysis, when interpreting whether evidence supports a claim, when testing whether a formal claim is derivable under configured rules, or when asked to log or sweep research work. Do not infer unobserved dependencies, author computed proofs, or directly certify support.
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
   New assessments use schema v2; legacy v1 records remain readable under v1 policy. Both supported
   versions accept exactly one result ID and require every one of the eight keys in both frames and
   in `alignment`. Use JSON `null` for a frame value that is not stated or not applicable;
   never use the literal string `"not stated"`. `not_stated` requires at least one corresponding
   frame value to be `null`, while `not_applicable` requires both to be `null`.
   In v2, a qualitative directional claim may leave `magnitude` null while the result states a
   magnitude and the alignment records `not_stated`, but both frames must state a direction and
   direction must align (`match`, or the explicit `mismatch` required by
   `contradicts_as_written`). Do not extend that exception to missing result magnitude, partial
   alignment, or any other dimension.
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
11. If the project config declares `logic.vocabularies` and `logic.rule_packs`, inspect those exact
    JSON files, the claim node's complete `logic` declaration, and each selected result node's
    complete fact profile. Do not add or edit predicates, a vocabulary, a rule pack, the claim
    target, a claim-owned evidence plan, or a result binding during routine logging. Changing any of
    those trusted project declarations requires explicit user or project-policy authority and
    separate review; Claimtrace does not semantically certify them. A valid result profile pins the
    vocabulary, input predicate, polarity, and extractor for every predicate argument, for example:

    ```json
    {
      "logic_bindings": [{
        "id": "project:observed-measure-complete",
        "vocabulary_id": "project:vocabulary",
        "predicate": "project:observed_measure",
        "polarity": "positive",
        "arguments": {
          "subject": {"kind": "json_pointer", "pointer": "/subject"},
          "value": {"kind": "json_pointer", "pointer": "/estimate"}
        }
      }]
    }
    ```

    Treat the whole profile as one reviewed meaning-bearing binding. Do not combine individual
    arguments from separate profiles, rows, or results.

    If the claim declares `logic_evidence_plan`, treat that plan as exact all-of premise policy.
    Inspect the resolved plan before deriving:

    ```bash
    claimtrace --config /absolute/project/claimtrace.config.json \
      evidence-plan claim:<existing-id> --json
    ```

    Then submit exactly a `claimtrace.symbolic-plan-request/1` with the claim, a concise public note,
    and reported provenance:

    ```json
    {
      "schema_version": "claimtrace.symbolic-plan-request/1",
      "claim_id": "claim:<existing-id>",
      "note": "Materialize every premise in the reviewed claim-owned evidence plan.",
      "provenance": {"agent": "<reported-agent-id>"}
    }
    ```

    A plan request deliberately contains no binding choices: Claimtrace resolves and materializes
    every required binding. Never choose, add, omit, or substitute a binding for a plan-governed
    claim, and never use a selection or verbose proposal to bypass its plan. If plan resolution
    fails, stop and report the exact error. This is complete only relative to the reviewed plan; it
    does not establish that the plan includes all scientifically relevant evidence or that its
    meaning is correct.

    Only when the claim has no `logic_evidence_plan`, use a
    `claimtrace.symbolic-selection/1` proposal. Supply only the schema, claim, approved complete
    binding identities, a concise public note, and reported provenance:

    ```json
    {
      "schema_version": "claimtrace.symbolic-selection/1",
      "claim_id": "claim:<existing-id>",
      "bindings": [{
        "result_id": "art:<first-result>",
        "binding_id": "project:observed-measure-complete"
      }],
      "note": "Concise public rationale; no hidden chain-of-thought.",
      "provenance": {"agent": "<reported-agent-id>"}
    }
    ```

    Select only complete profile IDs already declared in each result node's reviewed
    `logic_bindings`; never supply a target, vocabulary, rule pack, JSON Pointer, text locator,
    predicate, polarity, type, unit, or observed value in a selection proposal. Claimtrace resolves
    the claim's pinned target and policy assets, then materializes each fact's predicate, polarity,
    extractors, types, units, and values from the selected result binding. It rejects incomplete,
    duplicate, unknown, or unused selections.

    For an unplanned claim, use the verbose typed-fact proposal only for an explicit bounded
    assumption or a deliberate low-level import. In that form, every predicate, type, unit,
    argument, polarity, and target must exactly match the configured vocabulary and claim policy;
    integers and decimals are canonical JSON strings. Never use the verbose form to override a
    project binding or claim-owned evidence plan. Assumption-dependent conclusions remain visible
    but inactive.

    Submit with `claimtrace --config <absolute-config> derive <proposal.json> --actor <agent-id>
    --json`. Never provide a closure, proof state, proof steps, proof ID, mechanical snapshot, or
    `active` flag. Claimtrace validates the assets and anchors, computes the paraconsistent closure,
    and records one composite proof for all selected results. Report `derivable`, `refutable`,
    `conflict`, or `unknown` exactly. It groups equivalent proofs, surfaces target-versus-opposite
    conflicts across proof submissions, and marks proofs inactive when artifacts, graph ancestry, or
    policy assets drift. Do not choose a convenient proof when a conflict is reported. Say
    “derivable under rule pack X,” never “proved true” or “scientifically supported.” Formal
    derivation checks declared logic only; a separate semantic assessment is still required to judge
    whether the declaration and result actually mean what the prose claim says.
12. Run `claimtrace --config <absolute-config> check --strict --json` and
   `claimtrace --config <absolute-config> lint --strict`. The strict JSON report
   reconciles graph declarations with content-addressed run receipts and is the machine-facing gate;
   it also surfaces pending, stale, contested, invalid, or missing semantic assessments. It does not
   execute project verifiers or establish scientific truth. Report exact failures and distinguish a
   successful write from a project-wide validation pass. Run `claimtrace verify` only when the
   project is trusted and numeric verification is part of the requested workflow.
13. Delete disposable temporary entry and proposal files after validation. Do not delete
    intentionally checked-in or reviewed example proposals. If `claimtrace` is not on `PATH`, use
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
- Treat vocabularies, rule packs, claim targets, and result bindings as reviewed project policy, not
  as semantically certified facts. “Project-owned” is a workflow convention, not access control: an
  agent with workspace write access could edit those files, and actor/provenance strings are
  self-asserted rather than authenticated. Protect policy files through the project's own review,
  ownership, signature, or CI controls and report which controls were actually verified.
- For a plan-governed claim, use only a plan request; for an unplanned claim, prefer approved binding
  selections. Never provide computed proof fields or silently modify the evidence plan, rules,
  bindings, or other policy that decides what follows. Keep the separate semantic assessment even
  when formal derivation succeeds; symbolic consistency does not establish meaning, truth, or
  scientific support.
- Keep symbolic derivations composite. Multiple result IDs are premises of one proof; never flatten
  that proof into separate per-result `supports` edges. Missing premises produce `unknown`; explicit
  positive and negative conclusions produce `conflict` rather than arbitrary explosion. Treat
  cross-proof conflicts and drift-inactivated proofs as unresolved, visible states.

## Sweep a session

List substantive analyses chronologically, then log one focused node per result. Include negative
and abandoned work. Link existing run receipts when available; never backfill them by inference.
Propose one grounded assessment per material result-claim comparison and preserve disagreement rather
than choosing the most convenient review. Where configured, submit typed grounded premises for the
project-owned symbolic rules without editing those rules. Finish with explicit-config `check
--strict --json` and `lint --strict`, then summarize what was recorded, what remains
declaration-only, which proofs are conditional or unknown, and which semantic assessments still need
review.
