# gates/

Every PR in the merge lane runs `gates/run-all.sh` from the repo root with `PR_NUMBER` and
`PR_HEAD_SHA` set. Exit 0 iff every gate passes. Adding a gate = adding its file and one row
below; there is no central registry.

Gate script contract: `NN-short-name.sh`, numbered in run order, self-contained, under ~60 lines.
On success it prints exactly one `PASS: <gate> — <reason>` line on stdout; on failure one
`FAIL: <gate> — <reason + what to do>` line on stderr and exit 1. A tool failure inside a gate
(`make`, `git fetch`, `gh`) is its own FAIL line, never a silent exit. Gates report state; they
never perform a lane transition (no gate marks a PR ready, merges, or edits it).
`tests/gates/test_gates_10_30.sh` pins the contract for the gates below.

| # | Gate | One-line rule | Came from |
|---|------|---------------|-----------|
| 10 | `current-state-fresh` | `docs/generated/CURRENT_STATE.md` must be fresh (`make current-state` + no diff) | chronos #219/#221 — main broke on a stale page, 2026-09-12 |
| 20 | `pr-ready` | PR must not be a draft at verification time; the FAIL remediation is "request Muse's mark-ready; seats never mark ready themselves" | standing lane practice (draft→ready is an explicit act; #244–#246, #260) |
| 30 | `base-fresh` | PR base sha (`baseRefOid`, read via GitHub GraphQL) must equal `origin/main` at verification time | standing practice — "base == origin/main (no rebase needed)" / "rebased by helm" across #243, #151, #257 |
