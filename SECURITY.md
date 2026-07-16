# Security policy

## Supported versions

Until Claimtrace reaches 1.0, security fixes are made on the default branch and released in the
next available version. Only the most recent published release is supported; older releases should
be upgraded before a report is evaluated against current behavior.

## Reporting a vulnerability

Please use [GitHub private vulnerability reporting][private-report] so the report, reproduction,
and proposed fix are not exposed before users can update. Include:

- the affected Claimtrace version or commit;
- the operating system and Python version;
- the smallest safe reproduction you can provide;
- the security boundary that is crossed and the expected impact; and
- whether the issue is already public or has a known disclosure deadline.

Do not include secrets, sensitive research data, or harmful payloads that are unnecessary to
reproduce the issue. If private vulnerability reporting is unavailable, do not open a public issue
with exploit details; contact the repository owner through GitHub first to establish a private
channel.

The maintainer will assess reports in good faith, coordinate a fix and disclosure when warranted,
and credit reporters who want attribution. Please allow a reasonable remediation window before
public disclosure.

## Security boundaries

Useful reports include bypasses of path containment, symlink or race protections; provenance-store
tampering that is incorrectly reported as valid; command-argument disclosure despite documented
redaction; unsafe archive or package behavior; and unintended code execution in inspection-only
commands.

The following are important limitations but are not, by themselves, vulnerabilities:

- scientific conclusions, ontology mappings, agent judgements, and project-authored rules can be
  wrong even when their provenance is internally consistent;
- actor strings are self-asserted unless a project adds an external identity control;
- declared inputs are not proof that the child process actually read them, and write scanning is
  not complete causal attribution;
- a materialized intermediate is hashed before and after the whole child process, not instrumented
  at its declared stage; matching bytes do not identify which stage wrote them or exclude a later
  rewrite inside that process;
- replay children are not OS-sandboxed from the network or filesystem paths outside their fresh
  workspace, so those external effects are neither prevented nor observed by Claimtrace;
- a secret-bearing replay override is redacted but its exact argv is intentionally not stored or
  committed, so its attempt outcome is diagnostic rather than review-ready source-command evidence;
- project release manifests record explicitly configured external assets with exact absolute paths;
  review them before publication because they are host-specific and can disclose workspace layout;
- a locally stored hash cannot prove that evidence was not removed before the hash was created;
- `claimtrace verify` intentionally imports and executes the configured project verifier; run it
  only for projects you trust; and
- `claimtrace run` intentionally executes the command supplied after `--`.

When uncertain, report privately and let the maintainer classify the issue.

[private-report]: https://github.com/gabwainstein/claimtrace/security/advisories/new
