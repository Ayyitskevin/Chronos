# ADR-0043 — Release-wheel reproducibility is a measured two-build invariant

Status: **accepted design — owner authorized autonomous merge for this resumed sequence on
2026-08-29. This changes release observation only; it grants no runtime or trading authority.**
Index entry: DECISIONS.md D-57.

## Context

Chronos already builds with an exact, hash-locked setuptools backend, installs the resulting wheel
into a separate runtime environment, exercises installed package and migration surfaces, and emits
a validated reproducible CycloneDX SBOM. Those controls constrain inputs and inventory, but none of
them proves that two executions produce the same wheel bytes.

Wheel members carry ZIP timestamps. Without a source-derived timestamp, two otherwise identical
builds can differ only because they happened at different times. The `SOURCE_DATE_EPOCH` standard
defines an integer Unix timestamp whose value depends only on the source, recommends a source or
packaging modification time, and requires malformed input to fail rather than silently become a
different build identity. The exact pinned setuptools 84.0.0 wheel writer reads that variable,
clamps it to ZIP's 1980 minimum, converts it in UTC, and assigns it to every wheel member.

A caller-supplied environment value is not trustworthy release identity: it can be stale,
malformed, or intentionally different while the source revision remains unchanged. Conversely,
requiring a clean checkout would make the ordinary pre-commit gate unusable because the existing
artifact contract deliberately tests the current tracked and non-ignored source set.

## Decision

### 1. Exact Git `HEAD` owns the build epoch

The verifier executes fixed-argument `git show -s --format=%ct HEAD` in the repository root. It
accepts exactly one non-negative decimal integer and refuses Git failure or a value later than
2107-12-31 23:59:59 UTC, the representable ZIP range. That value overrides any ambient
`SOURCE_DATE_EPOCH` only for each wheel subprocess. The usual `PYTHONHOME` and `PYTHONPATH`
sanitization remains in force.

The gate may still run over uncommitted source during development; only a final post-commit run is
candidate evidence, because only then does `HEAD` incorporate the candidate's packaging changes.

### 2. Reproducibility is observed across two isolated builds

The current non-ignored source set is copied independently into `source-a` and `source-b`. The same
hash-locked builder environment invokes the pinned backend once against each source copy, writing
to distinct output directories with pip's cache disabled. Both invocations receive the exact
repository-derived epoch.

The gate requires exactly one `chronos-*.whl` in each directory, matching filenames, and exact byte
equality. On mismatch it reports only the two SHA-256 digests, not archive contents. It then opens
the verified wheel and requires every member timestamp to equal the expected UTC timestamp after
the backend's pre-1980 clamp and ZIP's two-second precision. Empty archives or timestamp drift
block. Only after these checks does the existing archive inspection, runtime installation,
migration drill, command smoke, SBOM validation, and publication proceed.

### 3. The claim is intentionally bounded

This proves that one exact source state, backend, interpreter, frontend, operating system, and gate
environment produced identical wheel bytes twice. It detects current time, copied-tree path, and
other within-environment nondeterminism that reaches the archive. It does not prove cross-platform
or cross-Python reproducibility, independently rebuilt identity, builder integrity, package
publisher provenance, or artifact signing. The pip bootstrap remains outside the hash locks.

No broker, gateway, credential, account, capital, database schema, deployment tree, trading mode,
order path, or external notification is read or changed.

## Consequences

Exact-candidate and exact-main release gates now retain a wheel whose byte repeatability was
observed before publication. Backend changes that stop honoring the epoch, archive members with a
different timestamp, time/path-dependent generated content, multiple wheels, and byte drift fail
closed. The gate performs a second wheel build and therefore takes slightly longer.

Artifact signing, independent rebuild attestations, cross-platform verification, pip-bootstrap
identity, malicious-package provenance, and Git-history secret review remain explicit Phase 2
residuals. This decision alone does not satisfy the Phase 2 exit.

## Sources

- [SOURCE_DATE_EPOCH specification](https://reproducible-builds.org/specs/source-date-epoch/)
  — deterministic integer format, source-derived timestamp semantics, export, and malformed-input
  failure.
- [setuptools 84.0.0 wheel writer](https://github.com/pypa/setuptools/blob/v84.0.0/setuptools/_vendor/wheel/wheelfile.py)
  — exact pinned implementation of `SOURCE_DATE_EPOCH`, the 1980 clamp, UTC conversion, and wheel
  member timestamps.
- [setuptools 84.0.0 `bdist_wheel`](https://github.com/pypa/setuptools/blob/v84.0.0/setuptools/command/bdist_wheel.py)
  — exact pinned wheel-building command implementation used by the backend.
