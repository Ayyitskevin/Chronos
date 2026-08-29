# Release security gate — 2026-08-29

```yaml
plan_phase: 2
primary_kpi: safety_integrity
gate_advanced: dependency, tracked-secret, and static-analysis release observations; not the full Phase 2 exit
files: release-security wrapper/tests, CI/Make wiring, exact dev/runtime locks, reviewed secret baseline, private research halt path, ADR-0042 and release documentation
verification: regression-first unit tests, real scanner abuse cases, exact local make gates, exact-candidate hosted CI, independent review, and exact-main equality/CI
evidence_artifact: .secrets.baseline plus exact scanner and gate output
owner_gate: not applicable; owner authorized autonomous merge on 2026-08-29, with no auth, credential, broker, schema, deployment, or trading authority change
open: artifact signing, pip bootstrap identity, reproducible wheel bytes, Git-history secret review, malicious-package provenance, lower-confidence static findings, and remaining Phase 2 work
```

## Claim under test

One release command can fail closed over the exact hash-locked runtime dependency identity,
meaningful high-confidence Python security patterns, and every tracked-file secret candidate,
without resolving dependencies, fixing code, mutating the committed baseline, or echoing a raw
credential-shaped value.

## Regression-first observations

Before implementation, pip-audit reported twelve current advisories against runtime-transitive
GitPython 3.1.52. The two application locks now share GitPython 3.1.61, with no unrelated
pre-existing runtime version change. A full Bandit probe produced 152 findings; only the fixed
shared `/tmp/claude-research-halt` path met the selected medium-severity and medium-confidence
threshold. It is now a per-run private temporary directory.

The initial detect-secrets inventory contained 263 candidates across 53 tracked files: 252 hex
high-entropy fingerprints and 11 secret-keyword candidates. Sanitized shape and context review
classified them as deterministic SHA-256/policy/source-control identities or deliberate
documentation/test placeholders. No provider-token or private-key detector fired. Every committed
entry is explicitly marked false positive, and the baseline stores no `secret_value` field.

## Gate behavior

The focused tests pin the exact command arguments, roots, thresholds, versions, and tracked-file
set. Negative cases prove that version drift, an empty Git inventory, any individual scanner
failure, and a baseline rewrite each block. The wrapper uses a private copy for the secret scan and
asserts the committed baseline remains byte-identical.

Two real abuse cases complement mocks: a credential-shaped AWS key assembled only at test runtime
is rejected without appearing in captured output, and a temporary Python file containing `eval`
is rejected by Bandit's B307 rule. The normal command reports all three scanners passing and emits
only scanner identity and sanitized fingerprint metadata.

## Scope and residuals

This is current revision evidence. The vulnerability result depends on the advisory service at
gate time; failure is blocking rather than cached as success. Heuristic thresholds deliberately do
not claim that low-confidence or low-severity findings are absent. The secret scan covers the
tracked tree, not Git history or untracked operator files. None of these checks proves a package's
publisher or code is trustworthy.

No live service, deployment tree, broker, credential, account, schema, capital, runtime authority,
or trading behavior is touched. Artifact signing, pip bootstrapping, deterministic wheel bytes,
history scanning, provenance attestation, and the rest of Phase 2 remain open.

## Candidate verification

Final local verification after implementation, documentation, and the reviewed baseline were
present reported:

```text
make gates
ruff: All checks passed; 582 files already formatted
mypy: 302 Chronos source files and 10 worker files clean
pytest: 4486 passed, 1 skipped, 25 warnings in 190.46s
security gate: no known vulnerabilities; Bandit and tracked-file secret scan passed
installed-wheel gate: PASS; migration head 0010, 34 model tables, 5 module entry points
CycloneDX 1.6 SBOM: valid, reproducible, 64 runtime components
```

The skip is the expected owner-opt-in read-only IBKR smoke test; no gateway was configured or
contacted. The warnings are existing Starlette/FastAPI and multiprocessing deprecations. The exact
candidate commit, hosted candidate CI, non-author verdict, and post-merge exact-main evidence are
bound in the pull request and final handoff because a commit cannot contain its own identity.
