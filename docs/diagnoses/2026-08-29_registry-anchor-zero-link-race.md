# Registry anchor atomic-replacement zero-link race

## Bug

Concurrent canonical and Five-Tool registry writers intermittently refused a valid local head
anchor as having “multiple hard links,” failing an otherwise green test or research attempt.

## Root cause

Every registry transaction constructs a descriptor-bound path capability before taking the shared
`flock`. That pre-lock inspection uses `os.stat(..., follow_symlinks=False)` to reject unsafe leaf
entries. A concurrent writer publishes `*.head.json` with `os.replace`, as required for atomic
anchor updates.

Linux documents that atomic `rename(2)` replacement can briefly expose both names. More
importantly for this failure, an overlapping lookup can return metadata for the old destination
inode after replacement has removed its last name. The captured result has `st_nlink == 0`; it is
not a named file with multiple hard links. The old check used `st_nlink != 1` and reported both
zero and greater-than-one counts as the same hard-link attack.

Temporary refusal-branch instrumentation reproduced the failure with:

```text
[DEBUG-anchor-nlink-a6f2] checked='processes.head.json' ... nlink=0 peers=[]
```

The tag and instrumentation were removed before the fix. No Chronos code creates a hard link for
the registry anchor. See the Linux [`rename(2)` manual](https://man7.org/linux/man-pages/man2/rename.2.html)
for the atomic-replacement name-overlap semantics.

## Feedback loop

The minimized loop exercises the public Five-Tool trial writer and failed before the correction
on iteration 37 (and on iteration 4 with the temporary diagnostic):

```bash
for iteration in $(seq 1 200); do
  .venv/bin/pytest -q \
    tests/unit/test_five_tool_trials.py::test_concurrent_processes_keep_chain_and_anchor_consistent \
    || { echo "failed iteration: $iteration"; exit 1; }
done
```

The original hosted symptom remains in exact-main CI run `33275996933`, first attempt:
`tests/unit/test_registry_trials.py::test_process_concurrent_starts_share_one_intact_chain`
failed because one child observed the same zero-link anchor state. The second attempt passed the
unchanged exact-main commit, which demonstrates intermittency but does not erase the first result.

## Fix

Leaf metadata lookup now re-stats an `st_nlink == 0` result a small, fixed number of times. A
stable safe inode proceeds. Exhausting the bound refuses with an accurate “remained unlinked”
error, and a zero-link observation followed by disappearance also refuses instead of becoming safe
absence. A named inode with `st_nlink > 1` still refuses immediately as a real hard-link attack;
symlink, file-type, ownership, descriptor-binding, pre/post-open identity, `flock`, anchor-chain,
and fsync checks are unchanged.

This is availability hardening in the research registry. It grants no data, holdout, broker,
order, promotion, or trading authority.

## Regression test

`tests/unit/test_registry_ledger.py` covers four independent outcomes through the public
`RegistryLedger` interface:

- one zero-link result followed by the safe named anchor is accepted and verifies;
- a persistently zero-link anchor exhausts the bound and refuses;
- zero-link metadata followed by a missing path refuses rather than creating or trusting a replacement;
- a real second hard link still refuses.

Run the deterministic cases with:

```bash
.venv/bin/pytest -q \
  tests/unit/test_registry_ledger.py::test_reader_rechecks_a_zero_link_stat_from_concurrent_anchor_replace \
  tests/unit/test_registry_ledger.py::test_reader_refuses_an_anchor_that_remains_unlinked_after_bounded_rechecks \
  tests/unit/test_registry_ledger.py::test_reader_does_not_downgrade_zero_then_missing_to_safe_absence \
  tests/unit/test_registry_ledger.py::test_reader_still_refuses_a_real_anchor_hard_link
```

After the correction, the formerly failing high-rate loop passed 200/200 iterations.
