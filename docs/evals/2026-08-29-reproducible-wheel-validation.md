# Reproducible release wheel — 2026-08-29

```yaml
plan_phase: 2
primary_kpi: safety_integrity
gate_advanced: reproducible-package subgate only; not signing or the full Phase 2 exit
files: release-artifact verifier/tests, D-57, R-15, ADR-0043, release/deployment/vision documentation
verification: regression-first unit tests, real two-copy/two-output wheel builds, exact local make gates, exact-candidate hosted CI, independent review, and exact-main equality/CI
evidence_artifact: retained exact-main wheel/SBOM pair plus candidate/main gate output containing the wheel SHA-256 and normalized build epoch
owner_gate: not applicable; owner authorized autonomous merge on 2026-08-29, with no auth, credential, broker, schema, deployment, or trading authority change
open: signing, independent/cross-platform rebuild evidence, malicious-builder/package provenance, pip bootstrap identity, Git-history secret review, and remaining Phase 2 work
```

## Claim under test

For one exact repository source state and the pinned gate environment, two isolated source/output
builds produce one wheel with the same filename, exact bytes, and source-derived timestamp on every
ZIP member. Any mismatch blocks publication while the existing installed-artifact and SBOM checks
continue to run after a match.

## Regression-first observations

Before the implementation, the current wheel was built twice more than three seconds apart with
`SOURCE_DATE_EPOCH` manually set to `git show -s --format=%ct HEAD`. Both builds produced SHA-256
`7ce7adf5d322f45a970e2d0e3d44c8082637032754d94605be6b9a53337a6271`, confirming the pinned
setuptools path could support the proposed contract. The production gate itself still built only
once and did not verify that claim.

The focused suite now covers exact Git timestamp parsing, Git failure, negative/multiple/malformed
and out-of-range values, pre-1980 clamping, ZIP two-second precision, ambient epoch override,
Python import-path sanitization, exact pinned build arguments, matching bytes, filename drift, byte
drift with digest-only diagnostics, member timestamp drift, and the two-source/two-output
orchestration invariant.

## Real gate observation

The first real post-implementation gate copied the same working source into `source-a` and
`source-b`, built both with `SOURCE_DATE_EPOCH=1788033073`, and observed identical wheel SHA-256
`a68bb9f10141b18caf282f1fb0178cd43134af17aa849b366520ba975fdf3126`. Every member carried the
normalized UTC ZIP timestamp `(2026, 8, 29, 19, 51, 12)`. The verified wheel then passed source
member comparison, clean-runtime installation, v2-to-0010 migration across 34 model tables, five
module entry points, and validated CycloneDX 1.6 inventory over 64 runtime components.

That run covered uncommitted implementation bytes while `HEAD` still named the prior revision; it
is developmental evidence only. The exact candidate commit must rerun the complete gate so its
epoch and source identity coincide. Hosted candidate CI, non-author review, merge equality, and
exact-main CI are recorded in the pull request and final handoff because a commit cannot contain
its own identity.

## Scope and residuals

Both builds intentionally share one pinned builder environment while isolating source and output
trees. The result does not establish independent, cross-platform, cross-interpreter, or
cross-frontend reproducibility, and exact equality cannot show that a malicious shared builder
produced trustworthy bytes. No signing key or credential exists in this change, and neither the
wheel nor SBOM is signed.

No live service, deployment tree, broker, credential, account, schema, capital, runtime authority,
or trading behavior is touched. This closes the explicit timestamp-bearing wheel residual only
within the declared gate environment and does not satisfy the Phase 2 exit.
