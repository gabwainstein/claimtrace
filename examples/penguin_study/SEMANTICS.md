# Deterministic semantic-normalization walkthrough

This optional walkthrough adds one reviewed terminology mapping to the Palmer Penguins project.
It is deliberately small: an external agent proposes a definition-level mapping, a different
human actor reviews it, a release owner compiles the accepted leaf, and the project activates that
exact release in configuration. ProvSleuth computes the candidate set, snapshots, content IDs,
review chain, release validity, and drift checks.

Run these commands in a **disposable copy** of this example. The checked-in configuration starts
with `active_policy: null` and `require_active_policy: false`, so a study can begin before its
terminology review is complete. The last step turns the policy into a strict project requirement.

This checked-in project preserves its pre-rename `claimtrace.config.json`, `claimtrace/` paths,
`claimtrace.*` schemas, and `urn:claimtrace:...` example namespace because those values are pinned
by immutable records. The current executable and Python module are named `provsleuth`.

The example ontology uses the explicitly project-owned
`urn:claimtrace:example:penguin-study:` namespace. It does not pretend to use a canonical external
ontology. Its Turtle document and reviewed index demonstrate exact-byte locking and the review
lifecycle only. ProvSleuth does not parse the Turtle or prove that the supplied index was
correctly extracted from it.

## 1. Load and verify the local semantic assets

From a source checkout, enter `examples/penguin_study`, then install ProvSleuth or point Python at
the source package as described in the main example README. In PowerShell:

```powershell
$config = (Resolve-Path claimtrace.config.json).Path

python -m provsleuth --config $config lock-ontology `
  claimtrace/semantics/penguin-example-ontology/lock-request.json `
  --output claimtrace/semantics/penguin-example-ontology/ontology.lock.json

$candidates = python -m provsleuth --config $config ontology-candidates `
  "bill length" --language en --limit 25 --json | ConvertFrom-Json

if ($candidates.total_match_count -ne 1 -or $candidates.truncated) {
  throw "Expected one untruncated exact candidate; stop for review"
}
$candidates.candidates | Format-Table iri, matched_on, label
```

`lock-ontology` succeeds only when the generated manifest is byte-identical to the checked-in
lock; it will not replace different immutable bytes. Candidate discovery is exact and offline.

## 2. Let the external agent fill only the bounded proposal section

[`semantic-mapping.proposal.template.json`](semantic-mapping.proposal.template.json) is the agent
input boundary. It contains the proposed SKOS relation, concise rationale, limitations, and
self-asserted agent identity. The mechanical candidate identifiers are copied from the live exact
candidate result rather than guessed by the agent:

```powershell
$agentInput = Get-Content -Raw semantic-mapping.proposal.template.json | ConvertFrom-Json
$agentInput.agent_input.target.ontology_lock_id = `
  $candidates.candidates[0].ontology_lock_id
$agentInput.agent_input.candidate_set_id = $candidates.id

$proposalPath = Join-Path (Get-Location) semantic-mapping.proposal.json
[IO.File]::WriteAllText(
  $proposalPath,
  (($agentInput | ConvertTo-Json -Depth 20) + "`n"),
  [Text.UTF8Encoding]::new($false)
)

$proposal = python -m provsleuth --config $config map-term $proposalPath `
  --actor "agent:semantic-demo" --language en --limit 25 --json | ConvertFrom-Json

$proposal.id
$proposal.current_derived.effective_review_state
```

The expected state is `proposed`. The actor string intentionally matches
`agent_input.provenance.agent`; ProvSleuth records that attribution but does not authenticate it.
The content ID may differ across runs because the immutable record includes its timestamp and
Unicode normalization profile.

## 3. Require a separate human review

The reviewer must inspect the local definition, selected candidate, rationale, and limitations.
They should choose `rejected` or `contested` instead if the proposed meaning is wrong. This teaching
walkthrough records `accepted` under a different actor string:

```powershell
$review = python -m provsleuth --config $config review-mapping $proposal.id `
  --state accepted --actor "human:semantic-reviewer" --json | ConvertFrom-Json

$review.id
$review.current_derived.effective_review_state
$review.current_derived.eligible_for_policy
```

The expected state is `accepted` and `eligible_for_policy` is `true`. The proposal remains in the
append-only history; the accepted decision is a successor with its own content ID.

## 4. Compile an explicit, still-inactive release

The release request enumerates the exact accepted leaf. It never asks ProvSleuth to choose the
newest mapping or collect every accepted mapping implicitly:

```powershell
$policyRequest = [ordered]@{
  mapping_ids = @($review.id)
  note = "Reviewed Palmer Penguins terminology release."
}
$policyRequestPath = Join-Path (Get-Location) semantic-policy.request.json
[IO.File]::WriteAllText(
  $policyRequestPath,
  (($policyRequest | ConvertTo-Json -Depth 10) + "`n"),
  [Text.UTF8Encoding]::new($false)
)

$policy = python -m provsleuth --config $config compile-semantic-policy `
  $policyRequestPath --actor "human:semantic-release-owner" --json | ConvertFrom-Json

$policy.id
$policy.evaluation.valid
$policy.evaluation.active
python -m provsleuth --config $config semantic-status --policy $policy.id
```

The release should be valid but inactive. `semantic-status --policy` checks that exact inactive
release without silently selecting it.

## 5. Activate the exact release as a reviewed configuration change

For this disposable walkthrough, the following deterministic edit pins the returned policy ID and
makes its absence or invalidation blocking. In a real project, review this configuration diff in
version control; the command itself does not authenticate an approver or create a signature.

```powershell
$configDocument = Get-Content -Raw $config | ConvertFrom-Json
$configDocument.semantics.active_policy = $policy.id
$configDocument.semantics.require_active_policy = $true
[IO.File]::WriteAllText(
  $config,
  (($configDocument | ConvertTo-Json -Depth 20) + "`n"),
  [Text.UTF8Encoding]::new($false)
)

python -m provsleuth --config $config semantic-status
$check = python -m provsleuth --config $config check --strict --json | ConvertFrom-Json
$check.summary
$check.semantics.active_policy.evaluation
```

A successful replay reports `semantic-status: OK`, one active mapping ID, an active and valid
policy evaluation, and zero blocking/errors/warnings in the strict report. Editing the terminology,
ontology, index, lock, accepted review leaf, policy ID, search language/limit, or Unicode profile
causes the relevant deterministic recheck to fail closed.

The accepted mapping is still an attributed human judgement, not proof that the scientific
meaning is correct. It does not create a result-to-claim support edge, assert `owl:sameAs`, or make
the project-owned example vocabulary canonical.

## POSIX shell equivalent

The same lifecycle uses only Python and the ProvSleuth CLI; `jq` is not required:

```bash
set -euo pipefail
config="$PWD/claimtrace.config.json"

python3 -m provsleuth --config "$config" lock-ontology \
  claimtrace/semantics/penguin-example-ontology/lock-request.json \
  --output claimtrace/semantics/penguin-example-ontology/ontology.lock.json

candidates_json=$(python3 -m provsleuth --config "$config" ontology-candidates \
  "bill length" --language en --limit 25 --json)
CANDIDATES_JSON="$candidates_json" python3 - <<'PY'
import json
import os
from pathlib import Path

candidates = json.loads(os.environ["CANDIDATES_JSON"])
if candidates["total_match_count"] != 1 or candidates["truncated"]:
    raise SystemExit("Expected one untruncated exact candidate; stop for review")
proposal = json.loads(Path("semantic-mapping.proposal.template.json").read_text())
proposal["agent_input"]["target"]["ontology_lock_id"] = (
    candidates["candidates"][0]["ontology_lock_id"]
)
proposal["agent_input"]["candidate_set_id"] = candidates["id"]
Path("semantic-mapping.proposal.json").write_text(
    json.dumps(proposal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
PY

proposal_json=$(python3 -m provsleuth --config "$config" map-term \
  semantic-mapping.proposal.json --actor "agent:semantic-demo" \
  --language en --limit 25 --json)
proposal_id=$(printf '%s' "$proposal_json" | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

review_json=$(python3 -m provsleuth --config "$config" review-mapping "$proposal_id" \
  --state accepted --actor "human:semantic-reviewer" --json)
review_id=$(printf '%s' "$review_json" | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

REVIEW_ID="$review_id" python3 - <<'PY'
import json
import os
from pathlib import Path

request = {
    "mapping_ids": [os.environ["REVIEW_ID"]],
    "note": "Reviewed Palmer Penguins terminology release.",
}
Path("semantic-policy.request.json").write_text(
    json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
PY

policy_json=$(python3 -m provsleuth --config "$config" compile-semantic-policy \
  semantic-policy.request.json --actor "human:semantic-release-owner" --json)
policy_id=$(printf '%s' "$policy_json" | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
python3 -m provsleuth --config "$config" semantic-status --policy "$policy_id"

POLICY_ID="$policy_id" python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path("claimtrace.config.json")
config = json.loads(path.read_text(encoding="utf-8"))
config["semantics"]["active_policy"] = os.environ["POLICY_ID"]
config["semantics"]["require_active_policy"] = True
path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

python3 -m provsleuth --config "$config" semantic-status
python3 -m provsleuth --config "$config" check --strict --json | python3 -c \
  'import json,sys; d=json.load(sys.stdin); print(d["summary"], d["semantics"]["active_policy"]["evaluation"])'
```
