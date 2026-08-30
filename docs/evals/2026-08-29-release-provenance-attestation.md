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

## Exact-main observation

PR #135 squash-merged the independently reviewed candidate
`454d1debe28675bf6d23333c30953a3507f53967` as
`a296880e09a82981b769caf661812fd7249df707`. Both commits resolve to tree
`7c75dbe8609a3101ca99a33f972dff733681af5d`, so the reviewed final state and the main result
are byte-identical across the entire repository.

Exact-main run `33283092740` passed `quality` and `release_provenance`. The quality job reported
4,530 passing tests, one expected owner-opt-in read-only IBKR smoke skip, the complete release
security gate, reproducible wheel validation, and a validated CycloneDX 1.6 SBOM with 64 runtime
components. The retained `chronos-release-a296880e09a82981b769caf661812fd7249df707`
artifact is ID `9723680514` and contains exactly:

- `chronos-0.1.0-py3-none-any.whl`, SHA-256
  `50390d70c327f025ac6d33281508cb7e2731094ef90f488886468d46795a8d68`;
- `chronos-0.1.0.cdx.json`, SHA-256
  `d8867fea341a525884b8f78a3f78801392ce6dec033474f300bea115c35aeeee`.

The downstream job created GitHub attestation `43903455` for both subjects, reported SLSA
provenance v1, signed through the Public Good Sigstore instance, and uploaded the signature to
Rekor. The host's installed GitHub CLI predated attestation support, so verification used an
official GitHub CLI v2.98.0 tarball only after its bytes matched GitHub's published checksum.
Each downloaded subject then passed:

```text
gh attestation verify <subject> --repo Ayyitskevin/Chronos \
  --signer-workflow Ayyitskevin/Chronos/.github/workflows/ci.yml
```

Both verification results resolved to the same SLSA provenance statement and the same complete
two-subject name/digest set above. This is the post-merge observation ADR-0046 required; R-62 is
therefore mitigated operationally rather than merely in code.

## Residuals

A valid attestation proves subject digest and workflow origin; it does not prove correctness,
safety, independent reproducibility, external publisher identity, or package-native signing. A
compromised authorized workflow, runner, pinned action, or GitHub/Sigstore service can still attest
bad bytes. The retained Actions artifact and attestation also depend on GitHub availability and
retention. Nothing in this observation publishes a package or grants runtime or trading authority.
