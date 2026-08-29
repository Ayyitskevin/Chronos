# Release SBOM validation — 2026-08-29

Status: owner-gated proposal; no release, deployment, or trading authority granted.

## Task contract

```yaml
plan_phase: 2
primary_kpi: safety_integrity
gate_advanced: "Reproducible package/release validation — SBOM generation; not the full Phase 2 composite exit"
files: ".github/workflows/ci.yml; requirements-runtime.lock; requirements-sbom.in; requirements-sbom.lock; scripts/verify_release_artifact.py; tests/unit/test_release_artifact.py; docs/SECURITY.md; docs/DEPLOYMENT.md; docs/VISION_COMPLETION_PLAN.md; docs/TEST_RESULTS.md; docs/evals/2026-08-29-release-sbom-validation.md; docs/diagnoses/2026-08-29_test-results-current-summary-schema.md; RISK_REGISTER.md"
verification: "baseline/final make gates; focused release tests; repeated real release gate; exact PR CI; non-author review"
evidence_artifact: "dist/chronos-0.1.0-py3-none-any.whl + dist/chronos-0.1.0.cdx.json"
owner_gate: required
open: "owner merge; dependency/secret/static scans; signing; pip bootstrap; reproducible wheel bytes"
```

## Why the environment boundary matters

Before this change, the installed-wheel smoke used `requirements-dev.lock`. Generating an
environment SBOM there would truthfully inventory pytest, mypy, ruff, Hypothesis, and their
transitive packages, but it would not describe a deployable Chronos runtime. A disposable probe
also showed that enriching such an environment from `pyproject.toml` attaches installed optional
dependencies to the application root. Calling that a release SBOM would overstate the evidence.

`requirements-runtime.lock` is therefore compiled from the same project without the `dev` extra.
It contains 63 packages, preserves every shared version from the 76-package dev lock, and removes
only 13 dev/test packages. The builder, runtime, and SBOM-tool dependency domains remain separate.

## Implemented evidence path

The release verifier now:

1. copies the current non-ignored source set and builds the wheel with the exact hash-locked
   backend in a disposable builder venv;
2. installs the runtime-only hash lock and wheel into a second venv, with `--no-deps` preventing
   an unreviewed resolver pass;
3. replays the installed package, console, static-asset, module-entrypoint, and v2-to-head
   migration checks in that runtime venv;
4. installs `cyclonedx-bom==7.3.1` from its separate hash lock into the builder and invokes only
   the documented stable `cyclonedx-py environment` CLI against the runtime interpreter;
5. requests reproducible CycloneDX 1.6 JSON with the tool's schema validation enabled;
6. independently requires the wheel's application name/version, the 13 non-extra wheel
   requirements as the exact root edge, every runtime-lock component at its locked version, no
   unlocked component except pip/setuptools bootstrap, and a closed dependency graph; and
7. publishes the verified wheel/SBOM pair to ignored `dist/`.

The CLI and reproducible-output flags follow the
[official CycloneDX Python usage documentation](https://cyclonedx-bom-tool.readthedocs.io/en/latest/usage.html).
The tool dependency is isolated because it is build evidence, not a Chronos runtime feature.

On an exact `main` push, CI requests 90-day retention for both files under
`chronos-release-<commit-sha>`. The upload action is pinned to the immutable v7.0.1 commit rather
than a moving tag; its inputs follow the
[official action documentation](https://github.com/actions/upload-artifact/tree/v7.0.1).

## Focused evidence

- Unchanged baseline `make gates`: 4,372 passed, 1 explicit credential-gated IBKR skip, 24
  warnings; lint, format, both mypy lanes, and the prior installed-wheel gate passed.
- Focused release tests: 19 passed. They pin the lock split, exact upstream CLI command, exact-main
  upload contract, valid runtime BOM, and mutations for an unlocked dev component, a missing locked
  component, locked-version drift, and an application self-dependency.
- Two complete release-gate executions passed. The generated BOM identified Chronos 0.1.0 as an
  application, contained 64 components (63 locked runtime packages plus the venv's pip bootstrap),
  contained exactly 13 direct runtime edges, and contained no dev/test component.
- The repeated SBOM SHA-256 was byte-identical:
  `f947e62ab4e52878f0233ae875945d7a65d9d04f50dbf8bc857f0747aa60355f`.
- The wheel SHA-256 changed between the same two runs (`3c2ed84b…` to `20754bb3…`). This confirms
  the SBOM's reproducible mode while preserving the existing disclosure that wheel ZIP bytes are
  not reproducible across build times.

Final candidate-wide gates, independent review, and PR CI are recorded in the PR/handoff because
their identities do not exist until after this document is committed.

## What this does not prove

An SBOM makes the installed inventory inspectable; it does not decide whether a component is
vulnerable, malicious, licensed acceptably, or safe for Chronos. Dependency vulnerability scans,
secret scans, static analysis, artifact signing, deterministic wheel construction, and a locked
pip bootstrap remain separate open release controls. CI retention also begins only after the owner
merges this security-sensitive dependency/build-input proposal to `main`.
