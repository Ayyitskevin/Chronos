# ADR-0045 — Release security scans complete reachable Git history

Status: **accepted design — owner authorized autonomous merge for this resumed sequence on
2026-08-29. This is release observation only; it grants no runtime or trading authority.**
Index entry: DECISIONS.md D-59.

## Context

ADR-0042 made the release gate scan every file tracked at the candidate tip. It deliberately
left Git history open. A credential can therefore be committed, deleted, and remain recoverable
from the repository while the tip-only scan passes. Ordinary `git log -p` is also incomplete:
Git does not show merge-commit diffs unless a merge-diff mode is requested, so a value introduced
only while resolving a conflict is absent from both parent histories and the ordinary patch
stream. Hosted CI compounded the gap by using checkout's default shallow history.

The detector and its reviewed policy already exist. The missing control is complete, bounded
selection of the bytes that policy must inspect without writing generated merge objects into the
repository or printing candidate plaintext.

## Decision

### 1. The gate requires complete, supported history

Hosted checkout uses `fetch-depth: 0`. The gate independently rejects a shallow repository,
missing `HEAD`, unavailable object store, or any reachable merge with three or more parents.
Remerge diffs are defined for two-parent merges; an octopus merge is unsupported evidence and
fails closed instead of silently receiving ordinary merge handling.

### 2. One exact traversal covers root, ordinary, deleted, and merge-resolution additions

The gate asks Git for every commit reachable from exact `HEAD` with root diffs, full ancestry,
zero-context patches, rename detection disabled, `--diff-merges=remerge`, and `--text`. External
diff drivers, text conversion, replace objects, and color are disabled. `--text` forces Git to
emit patch hunks even when its NUL-byte heuristic classifies a blob as binary; without it, Git
prints only `Binary files ... differ` and silently withholds the bytes from the detector. Parent
commits supply ordinary branch additions; remerge supplies only the resolution delta between
Git's reconstructed merge and the recorded two-parent merge result. A secret added and later
deleted is still an addition in its introducing commit.

The patch stream has a 128 MiB hard ceiling. The accepted Chronos ancestry at implementation time
is 396 commits and 17,585,006 bytes, leaving more than sevenfold headroom without making an
unbounded repository-controlled allocation part of the quality gate.

### 3. Remerge scratch objects never enter the repository object store

Git documents remerge as creating a temporary tree object. The traversal receives an empty
private `GIT_OBJECT_DIRECTORY` under `TemporaryDirectory` and the real object directory only as
`GIT_ALTERNATE_OBJECT_DIRECTORIES`. Reads still resolve the exact repository ancestry, while any
generated object is confined to private scratch state and removed after the scan. Tests inventory
the real object directory before and after a conflict-resolution scan.

### 4. History uses the same detector policy with one necessary filter change

`detect-secrets==1.5.0` remains the detector and `.secrets.baseline` remains the source of plugin
and filter settings. `unidiff==1.0.0` is an exact, hash-locked dev dependency because the pinned
detector's diff API requires it. The history scan disables only detect-secrets' ordinary
`is_invalid_file` filter: a deleted historical path does not exist at the tip, but its exact added
line is present in the authenticated Git patch. Keeping that filter would recreate the very
history-only blind spot this decision closes.

### 5. Historical exceptions are exact, reviewed, and self-pruning

The existing baseline gains a closed `history_results` section. Each entry binds path, detector
type, SHA-1 detector fingerprint, a reachable observation commit, an explicit false-positive
label, and a reason; raw candidate values are forbidden. The initial full-history pass found seven
historical-only high-entropy hex candidates. Sanitized context proved all seven are SHA-256
identities of generated source inventories, research artifacts, or a preregistration document.

An exception matches only exact path, detector type, and fingerprint. Every exception must still
appear in the reachable scan; history rewriting that removes it makes the baseline stale and
blocks until the record is reviewed and removed. New findings report only path, line, detector
type, and fingerprint, capped to ten displayed identities. Candidate plaintext is neither printed
nor retained as an artifact.

## What proves it

- A real temporary repository containing a credential-shaped value in one commit and deleting it
  in the next is rejected while its clean tip alone would pass.
- A NUL-bearing file containing a credential-shaped value is rejected after deletion rather than
  disappearing behind Git's binary-file heuristic.
- A real conflicted two-parent merge with a credential-shaped value only in the resolution is
  rejected, and the real object directory remains byte-name identical across the scan.
- Exact reviewed fingerprints pass; stale, malformed, duplicate, plaintext-bearing, or
  non-false-positive review entries fail closed.
- Shallow history, octopus merges, unavailable Git state, parser failure, version drift, and an
  oversized patch stream block the release gate.
- The failure surface never contains the synthetic candidate value.
- The complete Chronos ancestry passes with exactly seven reviewed historical SHA-256 identities.

## Consequences and limits

The release gate now detects recognized credential shapes anywhere in the exact ancestry reachable
from candidate `HEAD`, including deleted files, NUL-bearing files, and two-parent
merge-resolution-only additions.
This is stronger selection evidence, not proof that no secret ever existed. It does not scan
unreachable branches, tags outside `HEAD`, reflogs, or working-tree-only files, and forcing a text
patch does not make detect-secrets recognize arbitrary binary encodings.
detect-secrets remains heuristic and documents classes it does not recognize, including some
multi-line and default-password forms. A finding does not rotate a credential or rewrite history;
those are separate human incident actions. GitHub push protection, signing, malicious-package
provenance, independent rebuilds, and compromised-builder resistance remain separate controls.

## Sources

- [Git 2.43 `git log` documentation](https://git-scm.com/docs/git-log/2.43.0)
  — merge diffs are off by default; remerge reconstructs two-parent merges and diffs the result.
- [detect-secrets 1.5.0 documentation](https://github.com/Yelp/detect-secrets/blob/v1.5.0/README.md)
  — baseline/pre-commit behavior, programmatic API, heuristic scope, and documented caveats.
- [unidiff 1.0.0 release](https://pypi.org/project/unidiff/1.0.0/)
  — exact parser release used by the pinned detector's diff API.
- [actions/checkout v5 documentation](https://github.com/actions/checkout/blob/v5/README.md)
  — `fetch-depth: 0` retrieves all branch and tag history instead of the default single commit.
