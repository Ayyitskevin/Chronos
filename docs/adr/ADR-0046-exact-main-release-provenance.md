# ADR-0046 — Exact-main release evidence receives signed build provenance

Status: **accepted design — owner authorized autonomous merge for this resumed sequence on
2026-08-29. This changes release evidence only and grants no runtime or trading authority.**
Index entry: DECISIONS.md D-60.

## Context

The release gate reproducibly builds and validates a wheel, cross-checks its exact runtime
inventory against a CycloneDX SBOM, and retains both files from exact-main CI. Hashes prove byte
identity only after a trusted party supplies the expected digest. They do not let a downstream
consumer prove which repository workflow produced a downloaded file.

GitHub artifact attestations bind named subject digests to a signed in-toto statement. Public
repositories use the public-good Sigstore instance and GitHub's workflow OIDC identity. The
credential-bearing operation is therefore a new security boundary: it must not widen the token of
every pull-request or quality run, and it must not discover subjects implicitly.

## Decision

### 1. Attestation is a downstream exact-main job

`release_provenance` runs only for `push` on `refs/heads/main`, requires `quality`, and receives a
five-minute timeout. `quality` continues to build, validate, and upload
`chronos-release-${{ github.sha }}`. The downstream job downloads that exact same-run artifact into
`dist/` with digest-mismatch handling fixed to `error`.

This split matters. The existing workflow-level permission remains `contents: read`; only the
main-only job receives `id-token: write` and `attestations: write`, plus the documented
`contents: read`. A pull request cannot exercise or inherit the signing job's OIDC/attestation
authority.

### 2. Subjects are explicit and complete

One invocation of `actions/attest` receives exactly:

- `dist/chronos-*.whl`
- `dist/chronos-*.cdx.json`

With no SBOM or custom-predicate input, v4.2.2 selects its documented Provenance mode and creates
one SLSA build-provenance attestation containing both resolved subjects. No
automatic subject discovery, registry push, artifact-metadata storage record, package upload, or
release publication is enabled. The release gate already proves that exactly one wheel and one
matching runtime SBOM exist; the attestation binds the bytes that crossed the job boundary.

### 3. Executable dependencies are immutable

Every external action in the workflow is pinned to a full release commit. The quality job uses
`actions/checkout` v5.1.0, `actions/setup-python` v6.3.0, and the already-pinned
`actions/upload-artifact` v7.0.1. The provenance job uses `actions/download-artifact` v8.0.1 and
`actions/attest` v4.2.2. Structural tests pin the workflow-level read-only default, job trigger,
dependency, timeout, exact permissions, action identities, artifact name, destination,
digest-mismatch behavior, and subject list. Updating any of those values requires an explicit,
reviewed test change.

## Threat model

The assets are release byte identity, repository build identity, and the short-lived workflow OIDC
credential. The trust boundaries are the quality-job artifact transfer, the two pinned actions,
GitHub's hosted runner and attestations API, and Sigstore's signing service.

The design prevents a pull-request job from receiving signing authority, refuses a corrupted
same-run artifact download, and prevents an action default from silently expanding the subject
set. It does not make a hostile main workflow, compromised runner/action, or compromised
GitHub/Sigstore control plane trustworthy. Such a builder can produce and honestly attest bad
bytes. The existing security, reproducibility, installed-artifact, and SBOM gates remain necessary
independent observations.

## What proves it

- The structural regression fails before the job exists and passes only with the exact bounded
  job contract.
- Pull-request CI runs the complete read-only quality job while the provenance job is skipped.
- After merge, exact-main CI must complete both jobs and retain the wheel/SBOM artifact.
- Downloaded wheel and SBOM files must each pass `gh attestation verify` against
  `Ayyitskevin/Chronos` and the exact `.github/workflows/ci.yml` signer workflow.

## Consequences and limits

Consumers can verify that exact retained release files were subjects of a signed provenance
statement issued to Chronos's main workflow. This is not a claim that the files are safe, that a
package index published them, or that another builder reproduced them. The files carry no
package-native signature, the attestation depends on GitHub/Sigstore availability and retention,
and compromised-builder resistance remains open.

## Sources

- [GitHub Docs: using artifact attestations](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations)
  — required binary-provenance permissions, `actions/attest@v4`, and CLI verification.
- [GitHub `actions/attest` v4.2.2](https://github.com/actions/attest/tree/v4.2.2)
  — newline-delimited subjects, one attestation for multiple subjects, and Sigstore behavior.
- [GitHub Actions workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)
  — job-scoped token permissions and unspecified-permission denial.
- [GitHub `actions/download-artifact` v8.0.1](https://github.com/actions/download-artifact/tree/v8.0.1)
  — same-run artifact download and digest-mismatch semantics.
