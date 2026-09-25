# gates/

Every PR in the merge lane runs the TRUSTED `run-all.sh` (see Trust model) with the candidate's repo
root as the working directory and `PR_NUMBER` and `PR_HEAD_SHA` set. Exit 0 iff every gate passes.
Adding a gate = adding its file and one row below; there is no central registry, and a new gate takes
effect once it is merged to the trusted base.

Gate script contract: `NN-short-name.sh`, numbered in run order, self-contained, under ~60 lines.
On success it prints exactly one `PASS: <gate> — <reason>` line on stdout; on failure one
`FAIL: <gate> — <reason + what to do>` line on stderr and exit 1. A tool failure inside a gate
(`make`, `git fetch`, `gh`) is its own FAIL line, never a silent exit. Gates report state; they
never perform a lane transition (no gate marks a PR ready, merges, or edits it).
`tests/gates/test_gates_10_30.sh` (05/10/20/30, run-all) and `tests/gates/test_gates_40_70.sh` (40–70) pin the
contract for the gates below. Gates 60 and 70 use `.venv/bin/python` (the Makefile's `PY`);
`CHRONOS_PY` overrides it, which is the harness's only knob.

| # | Gate | One-line rule | Came from |
|---|------|---------------|-----------|
| 05 | `head-bound` | runs first: `git rev-parse HEAD` == `PR_HEAD_SHA` (40-hex, required) == the PR's `headRefOid`, and `gh` resolves this checkout to `Ayyitskevin/Chronos`; gates 20/30 query that repository explicitly | Daybreak's GATES-1 security HOLD (P1-1) — without it, 20/30 accept metadata borrowed from another PR |
| 10 | `current-state-fresh` | `docs/generated/` must match its state inputs: the read-only `scripts/build_current_state.py --check` (writes nothing), after refusing any tracked symlink/gitlink/non-regular path and any output that is not a regular file inside the tree | chronos #219/#221 — main broke on a stale page, 2026-09-12 |
| 20 | `pr-ready` | PR must not be a draft at verification time; the FAIL remediation is "request Muse's mark-ready; seats never mark ready themselves" | standing lane practice (draft→ready is an explicit act; #244–#246, #260) |
| 30 | `base-fresh` | PR base sha (`baseRefOid`, read via GitHub GraphQL) must equal `origin/main` at verification time | standing practice — "base == origin/main (no rebase needed)" / "rebased by helm" across #243, #151, #257 |
| 40 | `evidence-at-head` | runs the existing `make gates` inside a read-only exact-head snapshot (see Trust model) and writes the receipt `.gates/40-evidence-at-head.json` = `{sha, exit, pytest counts, tree_verified}` (gitignored; 0700 dir, 0600 file, never through a link); PASS iff receipt.sha == `PR_HEAD_SHA` == `git rev-parse HEAD`, exit 0, the snapshot's tracked bytes still equal `PR_HEAD_SHA`'s blobs and the lane is untouched, and exactly one pytest summary shows passed > 0 with failed == errors == 0 | standing practice — "exact-head gates green", "tests run at head" on nearly every merge |
| 50 | `fixture-integrity` | the `sha256sum -c` equivalent (chronos has no SHA256SUMS files): every tracked `tests/fixtures/**/manifest.json` `files` entry and `tests/fixtures/**/*.meta.json` pin (`trace_sha256` → sibling `<stem>.csv`, `pine_sha256` → the one tracked `research/pine/<catalog_number>_*.pine`) must match its bytes. Every manifest and pinned file must be a tracked regular file whose real path stays under its root, opened without following links; a link, FIFO or untracked file is refused before it is read or hashed. A pinned file missing, an unmapped `*_sha256` key, a non-object manifest, or zero manifests fails. `input_config_sha256` hashes an in-document object, not a file, and is the loader's check. A reviewed re-mint (bytes AND manifest changed together) passes by construction; it is visible as a manifest diff | chronos #259 — fixture sha256s verified, no silent re-mint |
| 60 | `secrets-baseline` | a fresh detect-secrets scan of TRACKED files (the release security gate's own scan path, against a temp copy of the baseline) shows no finding beyond `.secrets.baseline`; findings are printed as `file:line type` only; the baseline is never written (a stale baseline fails: hand-edit and review it) | chronos #259 — reviewed `.secrets.baseline` pins |
| 70 | `docs-claims-pinned` | the existing doc-pinning contract tests pass, run by named selection: `tests/unit/test_limitations_*_contract.py`, `test_adr_point_in_time_claims.py`, `test_docs_map_skill_contract.py`, `test_vision_completion_plan_prose.py` — each a tracked regular file inside `tests/unit`. The verdict comes from the junit report the run writes into a 0700 scratch dir: tests > 0, failures == errors == skipped == 0, every selected file executed, and no deselected/skipped/xfail in the summary | chronos #233, #239, #254 — "limitations.md tells the truth, pinned by contract tests (DOC-9)" |

## Trust model

The gates are executable acceptance authority: whoever runs them holds repository, network and
`gh` credential access. So the code that JUDGES a PR must not come from the PR.

- **Trusted:** `run-all.sh` and every `NN-*.sh` it runs come from a trusted checkout — `origin/main`'s
  `gates/` (for this bootstrap PR, the reviewed branch copy). The lane invokes
  `"$GATES_TRUSTED_DIR/run-all.sh"` (or that copy directly: its own directory is the default trusted
  set) with the candidate repo root as the working directory. A candidate `gates/NN-*.sh` absent from
  the trusted set is reported as a FAIL and never executed. Invoking the candidate's own
  `gates/run-all.sh` gives none of this protection — the lane must not do it.
- **Trusted, with the runner's environment:** 05, 20, 30 (`gh` reads + `git fetch`) and 50 (system
  `python3` over fixture data). They run no candidate code.
- **Candidate code, contained:** 10 (the current-state generator's `--check`), 40 (`make gates`),
  60 (the release security script and its scanners) and 70 (the doc-contract tests) execute code from
  the PR. Each runs it under `env -i` with exactly `PATH=/usr/local/bin:/usr/bin:/bin`, `HOME=<a fresh
  temp dir, removed on exit>`, `LANG=C.UTF-8`, `BROKER_MODE=demo`, `ALLOW_ORDER_TRANSMIT=false`,
  `ALLOW_LIVE_TRADING=false`, `PYTHONDONTWRITEBYTECODE=1` — no `GH_TOKEN`, no gh config directory,
  no other inherited variable.
- **Gate 40's snapshot:** `make gates` runs in a local clone at `PR_HEAD_SHA` inside a 0700 scratch
  dir (removed afterwards), with the lane's `.venv` shimmed in by link and `PYTHONPATH=<snapshot>/src`
  added to the allowlist. The snapshot is `a-w` except the gitignored outputs `make gates` writes
  (`dist/`, `.ruff_cache/`, `.mypy_cache/`, `.pytest_cache/`). Afterwards the trusted gate hashes every
  tracked file and compares it with `PR_HEAD_SHA`'s blobs from the LANE's object store, and checks the
  lane is untouched. The raw make log never leaves the scratch dir.
- **Output paths:** gates write only inside the tree (`.gates/`, 0700, refused if it is a link or not a
  directory; the receipt created 0600 with O_EXCL|O_NOFOLLOW) or inside their own 0700 scratch dir.
- **Fail-closed aggregation:** a gate counts as passed only if it exits 0 and prints exactly one
  `PASS: ` line. Any FAIL, a refused unregistered gate, a gate that cannot run, or an empty gate set
  makes `run-all.sh` print `GATES FAILED` and exit 1.
- **Not contained (residuals, stated):** candidate code still runs as the runner's user, so files
  readable by that user (e.g. `~/.config/gh/hosts.yml`, `~/.ssh/`) remain readable by path, and the
  network is not denied (`make gates` needs it for `pip-audit`). A user-namespace sandbox (`bwrap`:
  hide `$HOME`, deny the network for 10/70) would close these; it is not built here.
- **Not contained, gate 40:** `a-w` stops naive writes, but candidate code running as the same user can
  `chmod u+w`, change bytes and restore them before the post-run check; only a read-only bind mount
  (`bwrap --ro-bind`) makes the snapshot immutable for the run. A change that persists is caught.
- **Not contained, gate 70:** the junit report is written by the candidate's own pytest and conftest,
  so a hostile conftest could forge it; the gate proves the adopted runner's report, not an
  independent execution trace.
