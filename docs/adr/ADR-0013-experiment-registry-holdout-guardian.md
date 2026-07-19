# ADR-0013: Experiment registry + holdout guardian (Milestone C2)

Status: accepted (two-reviewer adversarial review completed post-merge; findings
remediated — §11 records them and corrects the overclaims)
Date: 2026-07-19

## Context

AI Quant plan C2: every research run recorded (config hash, data hashes, criteria
version, stage, git commit); the multiple-testing trial count **derived automatically
from the registry** (self-reported N is theater); holdout reads **mediated by the
registry** — consuming a holdout requires an explicit, logged, **once-only unlock
typed by the owner**, and **no scheduled job, proposal-execution path, or copilot
artifact can invoke it**; a budget policy rations unlocks against newly accrued data.
The M5 review's **"burned holdout"** failure class — a holdout window silently reused
as if fresh — must become **structurally impossible**.

This builds on two existing pieces (discovery-confirmed):

- **`chronos.auditlog.AuditLog`** — a file-based, `fsync`'d, hash-chained JSONL log
  (`AuditRecord{sequence, at_utc, kind, payload, previous_hash, record_hash}`;
  `_hash_record = sha256("{seq}|{at}|{kind}|{payload_json}|{prev}")`; `verify_chain`
  detects sequence-gap / chain-break / hash-mismatch). The registry **reuses it** as
  its ledger — tamper-evidence for free.
- **`chronos.histdata.holdout`** (C1) — `HoldoutWindow` + `embargoed_view(...,
  unlocked=False)` default-masks declared windows; today `unlocked=True` is a plain
  boolean with **no token, no logging, no once-only enforcement** (its docstring names
  that gap as C2's job).

Two facts constrain the design:

- **There is no TTY/`getpass`/`isatty` guard anywhere in `src/`.** The codebase already
  distinguishes owner-typed from automated actions **structurally**: typed phrases enter
  only through the UI/HTTP boundary, and the automated service loop simply never calls
  `arm()`/`confirm()`. C2 follows that doctrine — a module-constant phrase plus a
  structural test that automated modules cannot import the unlock — rather than
  inventing a runtime interactivity check the rest of the system doesn't use.
- **No "once-only / consumed" flag exists today.** `LiveArmingService` (fixed
  `REQUIRED_ARM_PHRASE` constant, `hmac.compare_digest`, TTL, never-logged-raw) is
  *reusable* within its TTL; per-order confirmations are single-use only by
  approximation (order-bound hash + short TTL). C2 **adds** genuine consume-and-invalidate.

## Decision

A new **research-plane** package `chronos.registry`. It records runs and guards
holdouts; it opens no trading database, places no order, and imports no
order/broker/execution module.

### 1. The registry ledger — hash-chained, append-only, reused from auditlog

`registry/ledger.py` wraps `AuditLog` over `research/registry/registry.jsonl`
(separate from the trading `data/platform_audit.jsonl`). Typed record kinds:

- `experiment_run` — one research run (§2).
- `holdout_unlock` — an owner-typed unlock was granted for a window (§3).
- `holdout_consume` — a holdout window was read under a granted unlock; the window is
  now **burned** (§4).

Every record inherits the chain's tamper-evidence; `verify_chain` (and a new CLI
`registry verify`) proves the ledger was not edited, reordered, or truncated. The
ledger is the **single source of truth** for trial counts and burned-holdout status —
not any in-memory or self-reported value.

### 2. Experiment-run recording + honest data fingerprints

`register_run(ledger, *, stage, strategy_id, config_hash, code_commit, data_hashes,
criteria_ref, touched_data)` appends an `experiment_run`. Fields:

- `stage` ∈ {`dev`, `validation`, `holdout`} — which data partition the run consumed.
- `config_hash` — the run config (params) hash; `code_commit` — `git rev-parse HEAD`
  (reusing `research.runner.current_commit`); `criteria_ref` — the frozen-criteria
  identity (the `selection_manifest.json` `frozen_at_utc` / `re_frozen_at_utc` pair, the
  repo's existing "criteria version" — there is no version integer, so the freeze
  timestamps are the reference).
- `data_hashes` — **honest, per the discovery gap**: a `data_fingerprint(root, symbols)`
  helper hashes the C1 store's **bars + corporate-actions** pair per symbol (from the
  histdata `MANIFEST.json`), a dict `{symbol: {bars_sha, actions_sha}}` — not the
  legacy single-CSV sha the runner stamps today. A run that used the legacy raw corpus
  records that sha under a `legacy_raw` key, labeled, so the two provenance regimes are
  never conflated.
- `touched_data: bool` — whether the run consumed data (see §5).

### 3. The holdout guardian — once-only, owner-typed, logged

`registry/holdout_guardian.py`:

- **Phrase:** `REQUIRED_HOLDOUT_UNLOCK_PHRASE` — a **module constant**, never a setting
  (so it can never land in a serialized/logged config), validated with
  `hmac.compare_digest`; the raw phrase is never stored, logged, or echoed. Mirrors
  `orders.arming`.
- `request_unlock(ledger, window_name, *, typed_phrase, reason, now, accrued_sessions)`
  → validates the phrase; checks the window is **declared** and **not already burned**
  (§4); checks the **budget** (§6); appends a `holdout_unlock` record carrying a fresh
  random `unlock_id` (`secrets.token_hex`), the window, the reason (masked — no raw
  phrase), and an `expires_at` (TTL setting); returns an `UnlockGrant{unlock_id,
  window, expires_at}`. A grant is **single-use**: it authorizes exactly one consume.
- `mediated_holdout_read(root, ledger, symbol, *, grant, now)` → verifies the grant is
  present in the ledger, unexpired, **not yet consumed** (no prior `holdout_consume`
  for that `unlock_id`), and covers the symbol's window; appends a `holdout_consume`
  record (burning the window); **only then** calls the C1
  `embargoed_view(..., unlocked=True)` and returns the unmasked series. This is the
  **only** sanctioned path that passes `unlocked=True`.

### 4. "Burned" is structural — the ledger is the authority

A window with a `holdout_consume` record is **burned**. `is_burned(ledger, window)` and
`burned_windows(ledger)` derive from the ledger. Consequences enforced in code:

- `request_unlock` **refuses** a burned window (fail-closed) — a burned holdout cannot be
  re-unlocked; re-using it as "fresh" is impossible without a new, explicitly
  re-declared window (a deliberate, logged human act, itself recorded).
- A consumed grant cannot be replayed (`mediated_holdout_read` refuses a second consume
  of the same `unlock_id`).
- The M5 failure ("holdout read once, then silently treated as fresh in a later run")
  cannot occur: the ledger records the burn, and any run over a burned window is
  self-evidently over seen data.

### 5. Trial counting — derived, not declared

`trial_count(ledger, *, strategy_id=None, since_criteria=None)` counts every
`experiment_run` with `touched_data=True` (optionally scoped). Because C3's
walk-forward inner-loop configurations and D4's AI-drafted proposals will each
`register_run` before touching data, they are **auto-counted** — the multiple-testing N
is a property of the ledger, never a human claim. C2 delivers the recording API +
derivation; the deflated-Sharpe / trial-adjusted statistics that *consume* N are C3.

### 6. Budget — unlocks rationed against newly accrued data

`registry/budget.py`: `available_budget(ledger, *, accrued_sessions, policy)` grants
one unlock credit per `sessions_per_unlock` (setting) of **newly accrued** capture
sessions since the last unlock (accrual measured from the C1 store, e.g. new option
snapshot dates / bar sessions), capped at `max_outstanding_unlocks`. `request_unlock`
spends a credit; with zero budget it fails closed. This rations holdout consumption
against real new information rather than letting an operator burn every window at once.

### 7. Structural isolation — the unlock is unreachable from automated paths

Two test layers (mirroring `test_histdata_isolation` + `test_single_transmit_site`):

- **Package isolation:** `chronos.registry` imports nothing from
  `chronos.{orders,api,services,service,execution,risk,control,broker,runtime,ui}` /
  `sqlalchemy` / `sqlite3` (AST + subprocess `sys.modules` probe). It may import
  `chronos.{auditlog,histdata,config,utils}`.
- **Unlock unreachability:** an AST walk over the **scheduler / service / proposal /
  promotion / submission** modules (`service/*`, `services/*`, `control/promotion.py`,
  `execution/engine.py`, `orders/submission.py`) asserts none import
  `chronos.registry.holdout_guardian` nor call `request_unlock` / `mediated_holdout_read`
  by name. **No `copilot` module exists yet** — the test's forbidden list is written to
  include the future copilot package path prospectively, and the assumption is flagged
  so D3/D4 inherit the bar.

### 8. CLI (owner-run) + settings

- `chronos registry stats|trials|verify` (read-only reporting + chain verification).
- `chronos holdout status` (declared/burned windows, budget) and `chronos holdout
  unlock --window W --reason R` — the **owner-run** unlock, which reads the phrase and
  calls `request_unlock`. Following the arming precedent, the phrase is not a CLI flag
  echoed into process listings where avoidable.
- Settings: `holdout_unlock_ttl_minutes` (`Field(gt=0, le=120)`), `sessions_per_unlock`
  (`Field(ge=1)`), `max_outstanding_unlocks` (`Field(ge=0)`). The phrase stays a module
  constant, never a setting.

## Honesty bounds

- **"Typed by the owner" is enforced structurally + by phrase**, not by a runtime
  interactivity check (the codebase has none). An owner who scripts the phrase into an
  automated caller defeats it — but the structural test guarantees no *shipped*
  scheduled/proposal/copilot path can, which is the DoD's actual requirement.
- **The research runner is not rewired** to consume the histdata store or to call
  `register_run` automatically in this milestone; C2 delivers the registry + guardian
  and the `data_fingerprint`, and records runs through the new API. Wiring the existing
  runner/shadow paths to auto-register is a follow-on (kept out to avoid changing
  established research provenance mid-milestone).
- **The budget policy is a first cut** (linear accrual credits); it rations, it does not
  model statistical power — that lands with C3/C4.
- **No copilot module exists**; its bar is prospective (§7).

## What proves it

- Ledger: append + `verify_chain` passes; a tampered line fails with the right class;
  records round-trip.
- Guardian: a correct phrase grants an unlock (logged, no raw phrase in the record); a
  wrong phrase fails; a grant authorizes exactly **one** consume (second consume
  refused); an expired grant is refused; a burned window cannot be re-unlocked;
  `mediated_holdout_read` is the only path that unmasks, and it records the burn first.
- Budget: zero budget fails closed; accrual grants exactly the rationed number.
- Trial count: N equals the number of data-touching runs in the ledger, not a
  self-reported field.
- Structural: `chronos.registry` imports nothing forbidden; the scheduler / service /
  proposal / promotion / submission modules import neither the guardian module nor call
  its unlock functions; subprocess probe leaks nothing.
- Burned-holdout scenario: read a window under a grant → it is burned → a subsequent
  unlock request and a subsequent "fresh" run over it are both refused/flagged.

## 11. Two-reviewer review remediation (record)

A safety + a correctness reviewer ran to completion (post-merge) and both broke the
original "structurally impossible" framing. All confirmed findings were remediated; the
claims below are the corrected, honest ones.

- **F1/safety-1 (CRITICAL) — chain truncation un-burned a window undetected.** A bare
  hash chain can't detect tail deletion (a valid prefix is a valid chain), so dropping
  the trailing `holdout_consume` line un-burned a window while `verify()` still said
  "intact". **Fix:** an out-of-band **head anchor** (`registry.head.json`: expected
  count + last hash) that `verify()` checks, so truncation / whole-file deletion /
  rollback are detected.
- **F2/safety-1 (HIGH) — nothing verified before trusting.** The guardian read the
  ledger without verifying it. **Fix:** `request_unlock` and `mediated_holdout_read`
  call `verify()` first and **fail closed**; `registry stats` / `holdout status` exit
  non-zero on a broken chain.
- **safety-2 (HIGH) — TOCTOU let one grant be consumed twice (double unmask).** The
  arming service it mirrors uses a lock; the guardian had dropped it. **Fix:** the
  read-verify-append critical section holds an exclusive **OS file lock** (`fcntl.flock`).
- **safety-3 (HIGH) — the guardian was bypassable.** `embargoed_view(unlocked=True)` had
  no single-site guard. **Fix:** `test_single_unmask_site.py` asserts `unlocked=True` is
  passed from exactly one site (the guardian).
- **safety-4 (HIGH) — the no-automated-unlock test had coverage holes.** **Fix:** it now
  scans the whole automated tree (`service`/`services`/`control`/`execution`/`orders` +
  `runtime.py`), not a hand-picked list.
- **F4/F5/F7 (MEDIUM) — honesty.** Budget now counts burns + *active* grants (an expired
  unused grant is refunded); `register_run` fails closed on null provenance (`""`/
  `"unknown"` commit, empty criteria); `data_fingerprint` carries an `actions_captured`
  flag and takes the history root directly.

**Corrected claims (the honest guarantee).** The M5 burned-holdout failure is **detected
and refused**, not "structurally impossible" in an absolute sense: the anchor + verify
catch accidental/incidental truncation, deletion, in-place edits, and rollback, and no
*shipped* automated path can invoke the unlock. Out of scope (disclosed): an actor who
rewrites **both** the ledger and its anchor consistently (the anchor is not a signed,
off-host root of trust), a determined **runtime-reflection** evasion of the unlock guard,
and completeness of the **trial count** (it is derived from *registered* runs; auto-
registration of the runner is a follow-on). Single-writer concurrency is enforced by the
file lock. §3's `mediated_holdout_read(ledger, history_root, symbol, ...)` argument order
is authoritative.

## Consequences

The research plane gains a tamper-evident, anchor-verified registry whose ledger is the
authority on how many trials were **registered** and which holdouts are spent, and a
holdout guardian that makes the M5 burned-holdout failure **detected and refused**: no
shipped automated path can unlock a holdout, an unlock is owner-typed, single-use, and
file-locked, and a consumed window is burned in a hash-chained log whose truncation is
caught by the head anchor. The honest residuals are recorded in §11 and
`docs/limitations.md`. Nothing in the trading/live plane changes; the single
`transmit=True` boundary and the C1 isolation are untouched; the registry ships empty.
