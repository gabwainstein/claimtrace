---
name: claimtrace-log
description: Log analysis work to the claimtrace knowledge graph — successes, nulls, dead-ends, and retractions. Invoke after running any analysis, or when the user says "log this to the graph" / "sweep this session into claimtrace".
---

# claimtrace-log

Keep the project's `claimtrace` graph an honest lab notebook: **every substantive attempt is recorded,
including the ones that did not work**, so nothing is silently re-tried and every live result has
visible provenance.

## When to use
- Right after an analysis produces a result (positive, null, or mixed).
- When something is abandoned (a dead-end), retracted, or superseded by a better approach.
- When the user asks to "log this", "add this to the graph", or "sweep the session into claimtrace".

## How to log
1. Find the project config: `claimtrace.config.json` (walk up from the analysis dir). If there is none,
   tell the user to run `claimtrace init` first.
2. Write an `entry.json`:
   ```json
   {
     "node": {
       "id": "exp:<short-slug>",
       "type": "experiment",
       "status": "confirmed | null | dead_end | retracted | superseded | current",
       "date": "YYYY-MM-DD",
       "script": "path/to/script.py",
       "value": "one-line verdict — what was tested and how it ended",
       "note": "why it ended there / key caveat",
       "backbone": "<canonical value if relevant>"
     },
     "edges": [
       {"from": "exp:<slug>", "to": "<claim-or-artifact>", "rel": "supports | refutes | tried_before | supersedes | superseded_by | produces | related"}
     ]
   }
   ```
   Use `script` (not `path`) for the code, so dead/deleted scripts don't trip `MISSING_FILE`.
3. `claimtrace log entry.json` (add `--update` to overwrite an existing node).
4. `claimtrace check` to confirm the graph is still consistent.

## Choosing status + relation
- A result that **lived** → `confirmed` (or `current` if it's a canonical artifact), `supports`/`produces`.
- A result that came back **null** → `null`, edge `refutes` or `related` to the hypothesis it tested.
- An approach **abandoned** → `dead_end`, edge `tried_before` to the approach that replaced it.
- A claim **withdrawn** → `retracted`; the replacing node gets `supersedes` → the old one (mark the
  old one `superseded`).

## Sweep-a-session mode
If asked to back-log a whole session: list each substantive analysis chronologically, then write one
`entry.json` per analysis and `claimtrace log` them in order. Prefer a few well-scoped nodes over one
giant node. End with `claimtrace check` and a one-line summary of what was logged.

## Discipline
Logging a dead-end is as valuable as logging a success — it's the record that stops the team (and
future-you) from silently re-running something that already failed.
