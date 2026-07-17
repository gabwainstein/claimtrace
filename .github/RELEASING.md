# Releasing ProvSleuth

Releases are built from an exact tag by GitHub Actions. PyPI publication uses OpenID Connect (OIDC)
Trusted Publishing; do not create a long-lived PyPI token for this workflow and do not publish the
archives from a workstation.

## One-time repository setup

1. Protect `main` with required CI checks and required review for changes to CODEOWNERS paths.
2. Create a protected GitHub environment named `pypi`. Require a maintainer approval and restrict
   deployment to version tags.
3. Add a GitHub Trusted Publisher for `provsleuth`. For an existing PyPI project, add it
   from that project's Publishing settings. Before the first release, register a pending publisher
   from the PyPI account's Publishing page. Use these exact values:

   - owner: `gabwainstein`
   - repository: `provsleuth`
   - workflow: `release.yml`
   - environment: `pypi`

4. Add a repository ruleset for `v*` tags so release tags cannot be silently updated or deleted.
5. Enable GitHub private vulnerability reporting and confirm that artifact attestations are
   available for the repository.

OIDC configuration is security-sensitive. A different owner, repository, workflow, or environment
can grant the wrong workflow permission to publish.

## Prepare a release

Work from a clean, reviewed commit on `main`. Before tagging:

1. Leave one empty `## Unreleased` section and move all of its content under a new
   `## X.Y.Z - YYYY-MM-DD` heading. The workflow rejects a populated `Unreleased` section, an
   undated heading, and reuse of an older version heading.
2. Set the same version in `pyproject.toml`, `src/provsleuth/__init__.py`, and `CITATION.cff`.
3. Run the full test and package gates locally or confirm them on the exact commit in CI:

   ```bash
   python -m pytest -q
   python -m build
   python -m twine check dist/*
   check-wheel-contents dist/*.whl
   ```

4. Review the source archive and wheel inventories. Tests are intentionally excluded from the
   source distribution, while the packaged ProvSleuth skill and its references must be present.
5. Create a signed, annotated tag whose name is exactly `v` plus the package version, then push only
   that tag. The signing key or email must be configured so GitHub marks the tag object's signature
   as verified; local signature success alone does not satisfy the automated gate:

   ```bash
   git tag -s vX.Y.Z -m "ProvSleuth X.Y.Z"
   git push origin vX.Y.Z
   ```

Do not move a published tag. If a tagged build fails before publication, fix the cause on a new
commit and create a new version tag.

## Automated release gates

`.github/workflows/release.yml` performs the following from the tagged commit:

- checks that the ref is an annotated tag object whose signature GitHub reports as verified, that
  its commit is on `main`, and that package metadata, runtime version, citation metadata, and the
  empty-Unreleased/datetime changelog cut agree;
- runs the complete test suite;
- builds twice with pinned build tools and a commit-derived `SOURCE_DATE_EPOCH`, then requires the
  wheels to be byte-identical and the source archives to contain identical ordered payloads;
- checks archive metadata, wheel contents, source-archive policy, and a clean-wheel installation;
- emits deterministic SHA-256 checksums and GitHub/Sigstore build provenance attestations;
- pauses at the protected `pypi` environment;
- publishes the already-built archives to PyPI through OIDC; and
- creates a GitHub release containing those same archives and checksums.

The publish job never checks out or rebuilds source. It receives only the artifact produced by the
gated build job and verifies its checksums before publication.

The Python sdist format permits build-time tar metadata that is not semantically part of its file
payload. The gate therefore compares every sdist member's path, type, mode, link target, size, and
content hash rather than claiming that gzip/tar metadata is reproducible. `SHA256SUMS` identifies
the exact sdist archive that was approved and published.

## Verify a published release

Download the GitHub release assets, then verify both the checksums and provenance:

```bash
sha256sum -c SHA256SUMS
gh attestation verify provsleuth-X.Y.Z-py3-none-any.whl \
  --repo gabwainstein/provsleuth
python -m pip install --no-cache-dir provsleuth==X.Y.Z
provsleuth --help
```

PyPI files are immutable. If a harmful release is published, do not replace its tag or archives.
Yank it on PyPI, document the reason, and release a corrected higher version. If PyPI succeeds but
GitHub release creation fails, do not rerun the entire publish workflow: verify the retained Actions
artifact and create the GitHub release from those exact files and `SHA256SUMS`.
