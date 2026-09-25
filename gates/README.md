# gates/

Every PR in the merge lane runs `gates/run-all.sh` from the repo root with `PR_NUMBER` and
`PR_HEAD_SHA` set. Exit 0 iff every gate passes. Adding a gate = adding its file and one row
below; there is no central registry.

Gate script contract: `NN-short-name.sh`, numbered in run order, self-contained, under ~60 lines.
On success it prints exactly one `PASS: <gate> — <reason>` line on stdout; on failure one
`FAIL: <gate> — <reason + what to do>` line on stderr and exit 1. A tool failure inside a gate
(`make`, `git fetch`, `gh`) is its own FAIL line, never a silent exit. Gates report state; they
never perform a lane transition (no gate marks a PR ready, merges, or edits it).
`tests/gates/test_gates_10_30.sh` (10/20/30, run-all) and `tests/gates/test_gates_40_70.sh` (40–70) pin the
contract for the gates below. Gates 60 and 70 use `.venv/bin/python` (the Makefile's `PY`);
`CHRONOS_PY` overrides it, which is the harness's only knob.

| # | Gate | One-line rule | Came from |
|---|------|---------------|-----------|
| 10 | `current-state-fresh` | `docs/generated/CURRENT_STATE.md` must be fresh (`make current-state` + no diff) | chronos #219/#221 — main broke on a stale page, 2026-09-12 |
| 20 | `pr-ready` | PR must not be a draft at verification time; the FAIL remediation is "request Muse's mark-ready; seats never mark ready themselves" | standing lane practice (draft→ready is an explicit act; #244–#246, #260) |
| 30 | `base-fresh` | PR base sha (`baseRefOid`, read via GitHub GraphQL) must equal `origin/main` at verification time | standing practice — "base == origin/main (no rebase needed)" / "rebased by helm" across #243, #151, #257 |
| 40 | `evidence-at-head` | runs the existing `make gates` at a clean HEAD and writes the receipt `.gates/40-evidence-at-head.json` = `{sha, exit, pytest counts}` (gitignored); PASS iff receipt.sha == `PR_HEAD_SHA` == `git rev-parse HEAD`, exit 0, and a pytest summary was recorded | standing practice — "exact-head gates green", "tests run at head" on nearly every merge |
| 50 | `fixture-integrity` | the `sha256sum -c` equivalent (chronos has no SHA256SUMS files): every tracked `tests/fixtures/**/manifest.json` `files` entry and `tests/fixtures/**/*.meta.json` pin (`trace_sha256` → sibling `<stem>.csv`, `pine_sha256` → the one tracked `research/pine/<catalog_number>_*.pine`) must match its bytes. A pinned file missing, an unmapped `*_sha256` key, or zero manifests fails. `input_config_sha256` hashes an in-document object, not a file, and is the loader's check. A reviewed re-mint (bytes AND manifest changed together) passes by construction; it is visible as a manifest diff | chronos #259 — fixture sha256s verified, no silent re-mint |
| 60 | `secrets-baseline` | a fresh detect-secrets scan of TRACKED files (the release security gate's own scan path, against a temp copy of the baseline) shows no finding beyond `.secrets.baseline`; findings are printed as `file:line type` only; the baseline is never written (a stale baseline fails: hand-edit and review it) | chronos #259 — reviewed `.secrets.baseline` pins |
| 70 | `docs-claims-pinned` | the existing doc-pinning contract tests pass, run by named selection: `tests/unit/test_limitations_*_contract.py`, `test_adr_point_in_time_claims.py`, `test_docs_map_skill_contract.py`, `test_vision_completion_plan_prose.py` (none skipped; an empty selection fails) | chronos #233, #239, #254 — "limitations.md tells the truth, pinned by contract tests (DOC-9)" |
