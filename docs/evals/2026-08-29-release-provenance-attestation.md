# Exact-main release provenance attestation — 2026-08-29

```yaml
plan_phase: 2
primary_kpi: safety_integrity
gate_advanced: signed GitHub build provenance for the exact-main wheel/SBOM pair; not the full Phase 2 exit
files: CI provenance job, structural regression, D-60/ADR-0046, R-62, and Phase 2 status
verification: red/green structural test, exact local gates, hosted candidate CI, independent security review, exact-main CI, and gh attestation verification of both downloaded subjects
evidence_artifact: GitHub attestation plus the retained chronos-release-${github.sha} wheel/SBOM artifact
owner_gate: owner authorized autonomous merge on 2026-08-29; no credential, broker, schema, deployment, or trading-authority change
open: package-native signing, independent/cross-platform rebuild agreement, publisher identity outside GitHub, compromised-builder resistance, and remaining Phase 2 work
```

## Claim under test

Only a successful exact-main quality run may create signed build provenance for the exact wheel and
CycloneDX SBOM it retained. Pull-request jobs must not receive OIDC or attestation-write authority,
artifact transfer must fail on digest mismatch, and the signing action must not infer an unbounded
subject set.

## Regression-first observation

The first focused run failed with `KeyError: 'release_provenance'`: the workflow retained validated
files but defined no provenance job. The test names the new public CI seam and pins its trigger,
dependency, timeout, permissions, action commits, same-run artifact identity, download destination,
digest policy, and two explicit subject paths.

## Implemented boundary

The quality job remains under workflow-level `contents: read`. A downstream job exists only for an
exact `main` push after quality succeeds and alone receives `contents: read`, `id-token: write`, and
`attestations: write`. It downloads `chronos-release-${{ github.sha }}` from the current run and
passes exactly the wheel and SBOM globs to the pinned official attestation action in one invocation.

All five external actions in the workflow are now pinned to exact release commits. The previously
moving `actions/checkout@v5` and `actions/setup-python@v6` quality-job inputs became explicit
v5.1.0 and v6.3.0 commits so the attested builder inputs do not move independently of review.

No package or release is published. No registry push or artifact-metadata storage record is
requested. No application, broker, data, schema, order, account, capital, promotion, or runtime
authority changes.

## Independent review and upstream-contract checks

The first non-author review returned HOLD with one disputed High and three Medium findings. Two
Medium findings were valid: the test did not pin the workflow-level `contents: read` default, and
the quality job still used moving checkout/setup major tags. The test now pins the read-only
default and exact commits for all builder actions; a regression-first run failed on
`actions/checkout@v5` before the workflow was corrected.

The claimed High was disproved against the exact upstream action rather than accepted by rank.
`actions/attest` v4.2.2 documents three automatically selected modes: with no `sbom-path` or custom
predicate inputs, Provenance mode auto-generates SLSA build provenance. Its action manifest makes
all predicate inputs optional. The remaining Medium requested upstream verification; live GitHub
API checks established:

- `actions/checkout` v5.1.0 resolves to
  `fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09`;
- `actions/setup-python` v6.3.0 resolves to
  `ece7cb06caefa5fff74198d8649806c4678c61a1`;
- `actions/download-artifact` v8.0.1 resolves to
  `3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c`, and its exact action manifest accepts
  `ignore`, `info`, `warn`, or `error` for `digest-mismatch`, with `error` as the failing default;
- `actions/attest` v4.2.2 resolves to
  `1e69f48acb82d1966a394da916b4c1698aa569d6`.

The corrected exact candidate requires the original review seat to withdraw its HOLD after
re-verifying these facts and the amended tests.

## Residuals

The post-merge main run is the first live proof because the job is deliberately unavailable to a
pull request. A valid attestation proves subject digest and workflow origin; it does not prove
correctness, safety, independent reproducibility, external publisher identity, or package-native
signing. A compromised authorized workflow, runner, pinned action, or GitHub/Sigstore service can
still attest bad bytes. Exact-main run and CLI verification identities are therefore recorded in
the pull request and final handoff rather than predicted here.
