# Recovery measurement capability evaluation — 2026-08-29

## Claim under test

Exact base `6034e1064c63df65a87411f0b668db015dab8c6f` can capture the five bounded
Chronos recovery artifacts without changing their source bytes, restore them into a new isolated
directory, and emit honest snapshot-age/local-duration observations. This evaluation does **not**
claim an operational RPO or RTO.

## Boundary

- Disposable DEMO/PAPER state only; no credential, live service, gateway, broker, mandate, or
  external network was accessed.
- `platform_ledger.db` and `chronos.db` were open in WAL mode during capture.
- The live kill switch was engaged, the deterministic platform was halted, and the audit chain was
  non-empty.
- Snapshot and restore roots were temporary and did not exist before the command.
- The command captured no `.env`, account identifier, or owner mandate. The source label was the
  non-sensitive string `disposable-evaluation`.

## Positive observation

Command: an isolated Python 3.12 process constructed the real `SqliteLedger` and `Database`, then
called `capture_snapshot(...)` followed by `restore_snapshot(...)` through the packaged module.

Observed result:

| Observation | Value |
|---|---:|
| Snapshot result | `chronos-recovery-snapshot-v1` |
| Required artifacts | 5 / 5, each SHA-256 and byte-size bound |
| Artifact capture elapsed | 0.044943684 s |
| Snapshot capture window | 0.020347 s |
| Oldest snapshot age at restore start | 0.047581 s |
| Local restore copy | 0.010234581 s |
| Local verification | 0.023113364 s |
| Local recovery elapsed | 0.033347945 s |
| Restore verdict | `PASS` |
| Snapshot manifest SHA-256 | `ea97361168e6b7888fe0f5c3519e6f04191cef94c3c339dc8c7b2dc6dfed6039` |

The values above are one same-host disposable observation. They are not performance thresholds and
must not be compared with an unstated objective.

## Failure-path evidence

`tests/integration/test_recovery_measurement.py` exercises:

- exact deterministic duration/age arithmetic across separate wall and monotonic clocks;
- source artifacts remaining byte-identical after capture;
- refusal before writing when the destination already exists;
- refusal before writing when the live kill switch is disengaged or corrupt;
- refusal before writing when halt evidence is corrupt;
- refusal before writing when a snapshot member's size/digest changes;
- refusal before writing when the restore wall clock predates the capture window; and
- the installed `python -m chronos.recovery capture|restore` command.

`tests/safety/test_recovery_measurement_isolation.py` scans the shipped package for broker,
network, subprocess, and destructive filesystem surfaces and proves its scanner sees both direct
and `from ... import ...` import forms plus deletion calls.

Focused verification after all mutation reversions:

```text
.venv/bin/python -m pytest -q \
  tests/integration/test_recovery_measurement.py \
  tests/safety/test_recovery_measurement_isolation.py \
  tests/integration/test_backup_restore_drill.py
18 passed in 4.88s

.venv/bin/ruff check .
All checks passed!

.venv/bin/ruff format --check .
569 files already formatted

.venv/bin/mypy src/chronos
Success: no issues found in 297 source files
```

Manual mutation checks, each applied alone and then reverted:

| Mutation | Named detector | Observed failure |
|---|---|---|
| Replace SQLite online backup with a plain main-file copy | `test_capture_and_restore_emit_bounded_measurements` | Refused the WAL-stale platform copy because required tables/evidence were absent |
| Compute oldest snapshot age from the newest artifact instead | `test_capture_and_restore_emit_bounded_measurements` | Exact assertion observed 90.0 s instead of the conservative 99.0 s |
| Skip pre-write snapshot digest verification | `test_restore_refuses_tampered_snapshot_bytes_before_writing` | Restore directory was created, violating the before-writing assertion |
| Treat every fail-closed `engaged=True` read as valid kill evidence | `test_capture_refuses_a_corrupt_kill_file_even_though_runtime_fails_closed` | Corrupt JSON was accepted and the expected refusal disappeared |
| Open newly created private database copies without SQLite `immutable=1` | `test_capture_and_restore_emit_bounded_measurements` | Unbound `platform_ledger.db-wal` and `platform_ledger.db-shm` appeared in the snapshot directory |
| Omit the restored `data/` directory fsync | `test_restore_fsyncs_the_restored_data_directory` | The fsync-spy assertion observed only the parent restore-directory fsync |
| Treat a nonzero-byte audit file as record-bearing evidence | `test_capture_refuses_whitespace_only_audit_evidence_before_writing` | A two-newline, zero-record audit file was accepted |
| Omit the snapshot/restore parent-directory fsyncs | `test_capture_fsyncs_the_snapshot_root_and_parent` and `test_restore_fsyncs_the_restored_data_directory` | Both tests failed because the new root entries were not made durable in their existing parents |

Initial candidate gate after the implementation and documentation were present:

```text
make gates
ruff: All checks passed; 569 files already formatted
mypy: 297 Chronos source files and 10 worker files clean
pytest: 4383 passed, 1 skipped, 24 warnings in 176.45s
installed-wheel gate: PASS; migration head 0010, 34 model tables, 5 module entry points
```

The single skip is the expected owner-opt-in read-only IBKR smoke test; no gateway was configured
or contacted.

## Independent review and hardening

Kimi independently reviewed exact commit `375a360436e574fc28556e9d3b0b6954226604cc`
from a detached worktree and returned **PASS** with no Critical, High, or Medium findings. It
reproduced the full gate (`4383 passed, 1 skipped`), the 18 focused tests, live and crash-state WAL
capture, latest-state restore, exact source-artifact bytes, refusal-before-write behavior, owner-only
permissions, installed CLI behavior, and the timing arithmetic. Its probes found three Low issues and
one documentation nit:

1. restored artifact directory entries were not explicitly fsynced;
2. whitespace-only audit evidence counted as non-empty;
3. read-only verification left unbound SQLite WAL/SHM sidecars beside private database copies; and
4. copy timing included isolated-directory creation, while live WAL reads may update transient source
   `-shm` coordination bytes.

The follow-up change fsyncs `<restore-root>/data`, requires at least one non-whitespace audit record,
uses SQLite's documented `immutable=1` mode only for newly created private snapshot/restore copies,
asserts exact successful bundle contents, and documents both timing and source-WAL behavior. Live
source databases deliberately remain ordinary `mode=ro` connections so committed WAL content stays
visible. The immutable-mode constraint follows SQLite's official URI documentation:
<https://www.sqlite.org/uri.html#recognized_query_parameters>.

Post-remediation verification:

```text
.venv/bin/python -m pytest -q \
  tests/integration/test_recovery_measurement.py \
  tests/safety/test_recovery_measurement_isolation.py \
  tests/integration/test_backup_restore_drill.py
20 passed in 5.39s

make gates
ruff: All checks passed; 569 files already formatted
mypy: 297 Chronos source files and 10 worker files clean
pytest: 4385 passed, 1 skipped, 24 warnings in 175.91s
installed-wheel gate: PASS; migration head 0010, 34 model tables, 5 module entry points
```

The skip remains the owner-opt-in read-only IBKR smoke test. The warnings remain the existing
Starlette/FastAPI and multiprocessing deprecations; no gateway was configured or contacted.

Kimi then reviewed exact remediation commit `8552788c4070923a565d43bdefe2148dac1509c2`
and returned **PASS** with no Critical, High, or Medium findings. Its detached-worktree run observed
20 focused tests passing, scoped Ruff/format and mypy passing, and an independent live-WAL probe in
which latest committed rows survived capture while immutable private copies contained no sidecars.
It identified three remaining Low findings: the audit-presence guard buffered the complete log; the
new snapshot/restore root entries were not fsynced in their existing parent directories; and the
runbook wording did not distinguish low-level immutable reads from Chronos's separate application
schema check. The final follow-up streams the presence check in 1 MiB blocks, fsyncs both parent
directories, adds mutation-sensitive ordering assertions, and makes that documentation distinction
explicit.

Final follow-up verification:

```text
.venv/bin/python -m pytest -q \
  tests/integration/test_recovery_measurement.py \
  tests/safety/test_recovery_measurement_isolation.py \
  tests/integration/test_backup_restore_drill.py
21 passed in 5.60s

make gates
ruff: All checks passed; 569 files already formatted
mypy: 297 Chronos source files and 10 worker files clean
pytest: 4386 passed, 1 skipped, 24 warnings in 178.12s
installed-wheel gate: PASS; migration head 0010, 34 model tables, 5 module entry points
```

## Residuals

Operational evidence still requires an owner-run campaign on the intended recovery host with:
verified clock health; stated RPO/RTO targets; representative state and snapshot age; off-host and
encrypted transport/retention; external manifest anchoring; incident detection and infrastructure
provisioning; mandate/secrets review; and broker order/position reconciliation. Recovery remains
read-only and unreconciled until those steps finish, and this capability grants no permission to
rearm.
