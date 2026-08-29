# ADR-0042 — Release security checks are exact, distinct, and fail closed

Status: **accepted design — owner authorized autonomous merge for this resumed sequence on
2026-08-29. This is release observation only; it grants no runtime or trading authority.**
Index entry: DECISIONS.md D-56.

## Context

ADR-0028 and ADR-0029 made application and build inputs hash-verifiable. The release-artifact
gate later proved that the wheel, migration tree, command surfaces, and CycloneDX runtime SBOM
matched those inputs. That inventory did not answer three separate questions:

1. Does the exact runtime set have a currently published vulnerability?
2. Does shipped Python contain a high-confidence security pattern at a meaningful severity?
3. Does any tracked file introduce a credential-shaped value outside a reviewed baseline?

The checks must not silently resolve dependencies, edit code, update a baseline, or treat an
unavailable scanner as a pass. They also must not scan only package files while missing the
out-of-package worker, scripts, configuration, or documentation that Chronos ships and operates.

Baseline probes found twelve published advisories against transitive runtime GitPython 3.1.52.
They also found one medium-severity, medium-confidence Bandit issue: the research runner used a
fixed shared `/tmp/claude-research-halt` path. The initial secret scan found only deterministic
hashes, source-control identities, policy digests, documentation examples, and explicit test
placeholders; none matched a provider-token or private-key detector.

## Decision

### 1. Scanner identity is part of the gate

The dev dependency set pins `pip-audit==2.10.1`, `bandit==1.9.4`, and
`detect-secrets==1.5.0` with hashes. `scripts/verify_release_security.py` independently checks
their installed versions before invoking them. Missing tools, drifted versions, empty tracked-file
inventory, execution errors, and nonzero scanner results all block.

### 2. Dependency audit uses the exact runtime identity

The wrapper runs:

```text
python -m pip_audit --require-hashes --disable-pip --progress-spinner off \
  -r requirements-runtime.lock
```

Hash enforcement keeps the audit input aligned with release installation. `--disable-pip`
prevents pip-audit from resolving a different environment, and the gate never applies fixes.
GitPython is intentionally upgraded from 3.1.52 to 3.1.61 in both application locks; no other
pre-existing runtime package version changes as part of that advisory closure.

### 3. Static analysis has an explicit source and confidence boundary

Bandit recursively scans `src/chronos`, `worker`, and `scripts`, blocking findings at medium
severity and medium confidence or stronger. Lower-confidence or lower-severity output is not
represented as clean; it remains outside this release threshold. No broad skip list is added.

The shared fixed research halt path is replaced with a private `TemporaryDirectory`, preserving
the same halt lifecycle while removing cross-process collision and symlink exposure.

### 4. Secret review is tracked-file complete and non-mutating

detect-secrets receives the complete NUL-delimited `git ls-files` inventory except
`.secrets.baseline` itself. That sole exclusion avoids scanning the file's fingerprint values back
into itself; it contains no raw candidates. Every baseline result is marked as an explicitly
reviewed false positive, and the committed JSON contains fingerprints, detector types, paths, and
line numbers—not candidate values.

The wrapper copies the baseline to a private temporary directory, scans against the copy, and then
requires its bytes to remain unchanged. A stale baseline therefore blocks without rewriting the
checkout. New candidate output is machine-readable fingerprint metadata and must not echo the raw
candidate. Baseline regeneration remains a reviewed source change, never a CI side effect.

### 5. Local and hosted release paths share one entry point

`make security-gate` runs the wrapper alone. `make gates` runs it after tests and before artifact
validation. Hosted CI invokes the same Make target under the existing safety environment. The gate
is evidence about a revision, not a runtime monitor or order predicate.

## Consequences

The current runtime lock is free of vulnerabilities known to pip-audit's advisory source at gate
time, and the declared static and tracked-secret thresholds are mechanically enforced on every
push and pull request. Scanner or advisory-service failure is visible and blocking.

These checks do not prove that a dependency is non-malicious, cover all historical secrets, audit
untracked local files, or establish that lower-confidence findings are harmless. They do not sign
artifacts, pin the initial pip bootstrap, or make wheel ZIP bytes reproducible. Exact tool, lock,
baseline, and threshold changes remain reviewable source changes. No broker, credential, account,
capital, schema, deployment, trading mode, or external notification is touched.

## Sources

- [pip-audit 2.10.1 documentation](https://github.com/pypa/pip-audit/tree/v2.10.1)
  — hash-required inputs, disabled pip resolution, vulnerability exits, and no-fix default.
- [Bandit 1.9.4 command reference](https://bandit.readthedocs.io/en/1.9.4/man/bandit.html)
  — recursive source scanning plus severity and confidence thresholds.
- [detect-secrets 1.5.0 documentation](https://github.com/Yelp/detect-secrets/blob/v1.5.0/README.md)
  — baseline, audit, and pre-commit behavior; baselines omit raw secret values.
- [Python 3.12 temporary-file documentation](https://docs.python.org/3.12/library/tempfile.html#tempfile.TemporaryDirectory)
  — private temporary-directory lifecycle and cleanup semantics.

> **Update (2026-08-29):** ADR-0045 closes this record's tip-only Git-history gap for
> detector-recognized textual additions reachable from candidate `HEAD`. Its disclosed detector,
> binary, unreachable-ref, and credential-remediation limits remain.
