# Complete Git-history secret scan — 2026-08-29

```yaml
plan_phase: 2
primary_kpi: safety_integrity
gate_advanced: complete reachable-history secret observation; not the full Phase 2 exit
files: release-security wrapper/tests, full-history CI checkout, exact dev lock, reviewed historical fingerprints, ADR-0045 and release documentation
verification: regression-first temporary repositories, protection mutants, full Chronos ancestry scan, exact local make gates, exact-candidate hosted CI, independent review, and exact-main equality/CI
evidence_artifact: .secrets.baseline history_results plus exact scanner and gate output
owner_gate: owner authorized autonomous merge on 2026-08-29; no credential remediation, auth, broker, schema, deployment, or trading authority change
open: unrecognized/arbitrarily-encoded/unreachable-ref secrets, credential rotation or history rewriting, signing, push protection, malicious-package provenance, and remaining Phase 2 work
```

## Claim under test

The exact release candidate must fail closed when a detector-recognized secret appears anywhere
in its complete reachable Git ancestry, including a value that was later deleted or introduced
only by resolving a two-parent merge. The scan must not print candidate plaintext, mutate the
repository object store, accept incomplete history, or retain stale review exceptions.

## Regression-first observations

Before implementation, the release wrapper had no history-scanning API and the first focused test
failed with that missing seam. Two real temporary repositories then pinned the selection gaps: one
committed and deleted a credential-shaped value, while the other introduced it only in a conflict
resolution. Both clean tips would evade a tracked-tree-only scan.

Independent review found a third Git-selection gap before merge: absent `--text`, a NUL-bearing
blob was reported only as `Binary files ... differ`, so detect-secrets received no hunk. A new
real-repository regression reproduces a NUL-bearing credential addition followed by deletion, and
the traversal now forces every blob through Git's text patch path before parsing.

Four controlled mutations demonstrated that distinct protections matter:

- disabling merge diffs let the resolution-only candidate escape;
- retaining detect-secrets' tip-file existence filter let the deleted candidate escape;
- removing the wrapper's history call let the integration test pass without the observation; and
- bypassing the shallow-repository refusal accepted incomplete evidence.

Each mutation made its targeted test fail and was reverted. A separate experiment removing the
scratch-object override did not make the object-inventory assertion fail in the synthetic fixture;
that negative result is not claimed as mutation proof.

## Accepted ancestry and review

The full Chronos traversal covered 396 commits and 17,585,006 patch bytes. It produced seven
historical-only high-entropy-hex findings. Sanitized context review established that all seven are
SHA-256 identities for generated source inventories, research artifacts, or a preregistration
document. The committed exceptions bind exact path, detector type, fingerprint, reachable
observation commit, false-positive label, and reason. They contain no candidate plaintext and
become blocking stale records if the corresponding finding disappears.

## Gate behavior

The focused suite uses real repositories to prove deleted and merge-resolution-only detection,
complete-history refusal, octopus refusal, bounded patch refusal, parser-failure translation,
review schema validation, reachable observation commits, stale-exception refusal, and repository
object-name preservation. Failure output is limited to path, line, detector type, and fingerprint.

The production wrapper pins `detect-secrets==1.5.0` and its diff parser `unidiff==1.0.0`, loads the
same plugin/filter policy as the tracked-tree scan, and disables only the filter that rejects paths
absent from the tip. Hosted CI fetches full history. Git replace objects, external diff drivers,
text conversion, rename detection, and optional locking are disabled; remerge scratch objects are
redirected to a private temporary object directory. Git's `--text` option prevents its binary-file
heuristic from suppressing NUL-bearing patches; the 128 MiB ceiling still fails closed on expanded
blob output.

## Scope and residuals

This is stronger selection evidence, not proof that no credential ever existed. detect-secrets is
heuristic, and the observation excludes unrecognized or arbitrarily encoded forms, unreachable
refs, reflogs, and untracked operator files. Forcing a text patch does not make detect-secrets
recognize arbitrary binary encodings. It does not rotate credentials, rewrite history, sign an
artifact, provide push protection, establish package provenance, or resist a compromised builder.

No live service, deployment tree, credential, account, schema, broker, capital, runtime authority,
or trading behavior is touched. The exact candidate commit, hosted candidate CI, non-author verdict,
and post-merge exact-main evidence are bound in the pull request and final handoff because a commit
cannot contain its own identity.
