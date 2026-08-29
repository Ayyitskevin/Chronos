# Hash-locked pip bootstrap — 2026-08-29

```yaml
plan_phase: 2
primary_kpi: safety_integrity
gate_advanced: exact post-ensurepip release-frontend identity subgate only; not signing or the full Phase 2 exit
files: bootstrap input/lock/verifier, CI and release-artifact gates, SBOM/security checks, build-environment skill, D-58, R-15, ADR-0044, deployment/security/vision documentation
verification: focused bootstrap/release/security/skill contract tests, real clean-venv bootstrap and release gate, exact local make gates, exact-candidate hosted CI, independent review, and exact-main equality/CI
evidence_artifact: retained exact-main wheel/SBOM pair plus candidate/main logs showing both pip bootstrap verifications
owner_gate: not applicable; owner authorized autonomous merge on 2026-08-29, with no auth, credential, broker, schema, deployment, or trading authority change
open: interpreter/ensurepip trust, signing, independent/cross-platform rebuild evidence, malicious-builder/package publisher provenance, Git-history secret review, and remaining Phase 2 work
```

## Claim under test

After Python creates a venv with its offline bundled pip, every supported Chronos build path uses
that frontend only to install one exact pip artifact from a dedicated two-hash lock. A separate
stdlib-only check must first refuse a broadened bootstrap-file grammar, then the target interpreter
must observe the locked pip distribution version before any backend, dependency, wheel, or SBOM-tool
operation. The runtime SBOM must contain that exact pip
identity and no unlocked environment component.

## Regression-first observations

Before this change, CI executed `python -m pip install --upgrade pip`, so the latest index result at
run time selected the frontend. The release verifier created builder/runtime venvs with
`with_pip=True` and immediately used their interpreter-bundled version. SBOM validation explicitly
allowed any pip or setuptools version as an unlocked bootstrap component.

Focused tests now freeze the sole bootstrap declaration and both published SHA-256 hashes; reject
empty, inexact, or multi-package bootstrap locks; reject missing/drifted installed pip; bind CI's
exact command order; bind both release venvs to validate-lock/install/verify; and mutate the SBOM to prove
pip-version drift and an unlocked setuptools component both fail.

## Scope and residuals

This control begins after the Python interpreter has created a venv and installed its bundled pip.
That first executable frontend is not authenticated by Chronos. The lock proves the downloaded
artifact matches a reviewed digest; it does not independently prove who published or built those
bytes, whether the interpreter is trustworthy, or whether pip is free of malicious behavior or
unknown vulnerabilities.

No live service, deployment tree, broker, credential, account, schema, capital, runtime authority,
or trading behavior is touched. Candidate/main identities and final gate evidence are recorded in
the pull request and handoff because a commit cannot contain its own identity.
